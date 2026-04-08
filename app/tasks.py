"""
Celery task pipeline: Download → Transcribe → Smart Clip → (Queue Upload)

Fixes applied:
- faster-whisper API: tuple unpacking + attribute access
- parse_srt(): implemented
- get_title(): implemented via yt-dlp metadata extraction
- queue_upload: replaced with proper upload_to_tiktok task
- Clip records: now saved to database
- FFmpeg: thread-limited for VPS, path escaping fixed
- Storage: auto-cleanup source video after clipping
"""
import sys
import os
from pathlib import Path

# Ensure project root is in Python path AND app/ is cwd (Celery forked workers need this)
_app_dir = str(Path(__file__).resolve().parent)
_project_root = str(Path(__file__).resolve().parent.parent)
for _p in [_app_dir, _project_root]:
    if _p not in sys.path:
        sys.path.insert(0, _p)
os.chdir(_app_dir)

from celery import Celery
from faster_whisper import WhisperModel
import yt_dlp
import subprocess
import uuid
import re
import logging

from app.config import settings
from app.database import SessionLocal
from app.models import Job, Clip, JobStatus, ClipStatus

logger = logging.getLogger(__name__)

# ── Celery app ──
app = Celery(
    "clipper",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_BACKEND,
)
app.conf.update(
    worker_concurrency=settings.CELERY_CONCURRENCY,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
)

# ── Whisper model (loaded once per worker) ──
WHISPER_MODEL = None


def get_whisper_model() -> WhisperModel:
    """Lazy-load Whisper model to avoid issues during import."""
    global WHISPER_MODEL
    if WHISPER_MODEL is None:
        logger.info(f"Loading Whisper model '{settings.WHISPER_MODEL}' ({settings.WHISPER_COMPUTE_TYPE})...")
        WHISPER_MODEL = WhisperModel(
            settings.WHISPER_MODEL,
            device=settings.WHISPER_DEVICE,
            compute_type=settings.WHISPER_COMPUTE_TYPE,
        )
        logger.info("Whisper model loaded.")
    return WHISPER_MODEL


STORAGE = settings.STORAGE_ROOT


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Main pipeline task
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.task(bind=True, max_retries=1, time_limit=7200)
def run_pipeline(self, job_id: str, youtube_url: str, max_clip_duration: int = 90, niche: str = "finance"):
    """Full pipeline: download → transcribe → clip → save to DB."""
    db = SessionLocal()
    job = db.query(Job).filter_by(id=job_id).first()

    if not job:
        db.close()
        raise ValueError(f"Job {job_id} not found")

    try:
        # ── Step 1: Download ──
        logger.info(f"[{job_id[:8]}] Downloading: {youtube_url}")
        job.status = JobStatus.downloading
        job.progress = "Downloading video from YouTube..."
        db.commit()

        video_path, title = download_video(youtube_url, job_id)
        job.title = title
        job.progress = f"Downloaded: {title}"
        db.commit()

        # ── Step 2: Transcribe ──
        logger.info(f"[{job_id[:8]}] Transcribing: {title}")
        job.status = JobStatus.transcribing
        job.progress = f"Loading Whisper {settings.WHISPER_MODEL} model..."
        db.commit()

        srt_path = transcribe_video(video_path, job_id)
        job.progress = "Transcription complete"
        db.commit()

        # ── Step 3: Analyze transcript for viral clips (AI) ──
        logger.info(f"[{job_id[:8]}] Analyzing clips...")
        job.status = JobStatus.clipping
        db.commit()

        srt_segments = parse_srt(srt_path)
        clips_data = []

        if settings.AI_CLIP_ENABLED and settings.GROQ_API_KEY:
            # 🧠 AI-powered: let Llama find the viral moments
            job.progress = f"🧠 AI analyzing transcript ({len(srt_segments)} segments)..."
            db.commit()

            logger.info(f"[{job_id[:8]}] Using AI to find viral clips (Groq/{settings.GROQ_MODEL})...")
            from app.clip_analyzer import analyze_transcript_for_viral_clips

            viral_clips = analyze_transcript_for_viral_clips(
                srt_segments,
                niche=niche,
                min_viral_score=settings.AI_MIN_VIRAL_SCORE,
            )

            if viral_clips:
                job.progress = f"🔥 AI found {len(viral_clips)} viral clips — cutting..."
                db.commit()

                for i, vc in enumerate(viral_clips):
                    job.progress = f"Cutting clip {i+1}/{len(viral_clips)} (score: {vc.get('viral_score', '?')}/10)..."
                    db.commit()

                    clip_path = cut_and_burn(
                        video_path, srt_path,
                        vc["start"], vc["end"],
                        job_id, i,
                    )
                    caption = vc.get("caption", vc.get("hook", ""))
                    duration = vc["end"] - vc["start"]
                    viral_score = vc.get("viral_score", 0)
                    clips_data.append((clip_path, caption, duration, viral_score))

                logger.info(f"[{job_id[:8]}] AI found {len(clips_data)} viral clips")
            else:
                job.progress = "AI returned no clips — falling back to smart cut..."
                db.commit()
                logger.warning(f"[{job_id[:8]}] AI returned no clips, falling back to smart_clip")

        # Fallback: blind cut if AI disabled, no API key, or AI returned nothing
        if not clips_data:
            job.progress = "Cutting at sentence boundaries..."
            db.commit()

            logger.info(f"[{job_id[:8]}] Using smart_clip (sentence-boundary cuts)...")
            min_dur = settings.DEFAULT_CLIP_MIN_DURATION
            raw_clips = smart_clip(video_path, srt_path, job_id, min_dur, max_clip_duration)
            clips_data = [(p, c, d, 0) for p, c, d in raw_clips]  # viral_score=0

        # ── Cap at 5 clips max ──
        MAX_CLIPS = 5
        if len(clips_data) > MAX_CLIPS:
            # Sort by viral_score descending, keep top 5
            clips_data.sort(key=lambda x: x[3], reverse=True)
            clips_data = clips_data[:MAX_CLIPS]
            logger.info(f"[{job_id[:8]}] Capped to {MAX_CLIPS} clips (dropped {len(clips_data) - MAX_CLIPS} lower-scored)")

        # ── Step 4: Save clips to DB ──
        job.progress = f"Saving {len(clips_data)} clips to database..."
        db.commit()

        for i, (clip_path, caption, duration, viral_score) in enumerate(clips_data):
            # Calculate file size
            file_size_kb = int(clip_path.stat().st_size / 1024) if clip_path.exists() else 0

            # Generate cover image
            cover = generate_cover(clip_path, caption or "", job_id, i)
            cover_str = str(cover) if cover else None

            clip = Clip(
                id=str(uuid.uuid4()),
                job_id=job_id,
                filename=clip_path.name,
                filepath=str(clip_path),
                caption=caption,
                duration=int(duration),
                file_size=file_size_kb,
                viral_score=viral_score,
                cover_path=cover_str,
                status=ClipStatus.ready,   # Review mode: clips wait for approval
            )
            db.add(clip)

        job.status = JobStatus.done
        job.clip_count = len(clips_data)
        job.progress = f"✅ Done — {len(clips_data)} clips ready for review"
        db.commit()

        logger.info(f"[{job_id[:8]}] Done — {len(clips_data)} clips created")

        # ── Step 5: Cleanup source video ──
        if settings.CLEANUP_AFTER_CLIP:
            from app.storage import cleanup_source_video
            cleanup_source_video(job_id)

    except Exception as e:
        logger.error(f"[{job_id[:8]}] Pipeline failed: {e}", exc_info=True)
        job.status = JobStatus.failed
        job.progress = f"❌ Failed: {str(e)[:200]}"
        job.error = str(e)[:2000]  # Truncate long errors
        db.commit()
        raise
    finally:
        db.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Step 1: Download
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def download_video(url: str, job_id: str) -> tuple[Path, str]:
    """Download YouTube video, returns (video_path, title)."""
    out_path = STORAGE / "downloads" / job_id
    out_path.mkdir(parents=True, exist_ok=True)

    # Progress hook — updates job.progress for live UI
    def _progress_hook(d):
        if d.get("status") == "downloading":
            pct = d.get("_percent_str", "?%").strip()
            speed = d.get("_speed_str", "?").strip()
            eta = d.get("_eta_str", "?").strip()
            logger.info(f"[{job_id[:8]}] Download: {pct} at {speed} ETA {eta}")

            # Update job.progress for SSE live display
            try:
                _db = SessionLocal()
                _job = _db.query(Job).filter_by(id=job_id).first()
                if _job:
                    _job.progress = f"📥 Downloading: {pct} ({speed}, ETA {eta})"
                    _db.commit()
                _db.close()
            except Exception:
                pass  # non-critical

    ydl_opts = {
        # Cascading format: try high quality first → fall back to format 18 (360p, always available)
        "format": (
            f"best[height<={settings.VIDEO_MAX_HEIGHT}][ext=mp4]/"
            f"bestvideo[height<={settings.VIDEO_MAX_HEIGHT}]+bestaudio/"
            "18/best"
        ),
        "cookiefile": "/opt/clipper-app/clipper-web-fast-api/www.youtube.com_cookies.txt",
        "outtmpl": str(out_path / "%(title)s.%(ext)s"),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "allow_unsecure_tools": True,
        "progress_hooks": [_progress_hook],
        "js_runtimes": {"node": {"executable": "/home/vinzel/.nvm/versions/node/v24.14.1/bin/node"}},
        "remote_components": {"ejs:github"},
        # Use android client to bypass PO Token requirement
        "extractor_args": {
            "youtube": {
                "player_client": ["ios", "android", "web"]}
            },
        # Speed & reliability
        "socket_timeout": 30,
        "retries": 5,
        "concurrent_fragment_downloads": 4,
        "ignore_no_formats_error": True,
    }

    title = "Untitled"
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        title = info.get("title", "Untitled")

    video_file = next(out_path.glob("*.mp4"))
    return video_file, title


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Step 2: Transcribe
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def transcribe_video(video_path: Path, job_id: str) -> Path:
    """Transcribe video to SRT using faster-whisper.

    FIX: faster-whisper returns (generator, info) tuple, not a dict.
         Segments have attributes (.text, .start, .end), not dict keys.
    """
    model = get_whisper_model()

    # ✅ FIXED: Tuple unpacking — faster-whisper returns (segments_generator, info)
    segments_gen, info = model.transcribe(
        str(video_path),
        language=settings.WHISPER_LANGUAGE,
        task="transcribe",
    )

    total_duration = info.duration or 1  # total audio length in seconds

    # Consume generator with live progress updates
    segments = []
    srt_path = STORAGE / "downloads" / job_id / "transcript.srt"

    with open(srt_path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(segments_gen):
            segments.append(seg)
            f.write(f"{i + 1}\n")
            f.write(f"{fmt_time(seg.start)} --> {fmt_time(seg.end)}\n")
            f.write(f"{seg.text.strip()}\n\n")

            # Update progress every 10 segments
            if i % 10 == 0:
                pct = min(int(seg.end / total_duration * 100), 99)
                elapsed_fmt = fmt_time(seg.end)
                total_fmt = fmt_time(total_duration)
                try:
                    db = SessionLocal()
                    job = db.query(Job).filter_by(id=job_id).first()
                    if job:
                        job.progress = f"🎙 Transcribing: {pct}% ({elapsed_fmt} / {total_fmt})"
                        db.commit()
                    db.close()
                except Exception:
                    pass

    logger.info(f"Transcription done: {len(segments)} segments, language={info.language}")
    return srt_path


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Step 3: Smart Clip
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def smart_clip(
    video_path: Path,
    srt_path: Path,
    job_id: str,
    min_dur: int = 60,
    max_dur: int = 90,
) -> list[tuple[Path, str, float]]:
    """Cut at sentence boundaries, create clips of min_dur-max_dur seconds.

    Returns list of (clip_path, caption, duration).
    """
    segments = parse_srt(srt_path)

    if not segments:
        raise ValueError("No segments found in SRT file — transcription may have failed")

    # First pass: figure out cut points to know total count
    cut_points = []
    current_start = segments[0]["start"]
    current_segs = []

    for seg in segments:
        current_segs.append(seg)
        duration = seg["end"] - current_start
        is_sentence_end = seg["text"].strip().endswith((".", "!", "?"))
        should_cut = duration >= min_dur and (duration >= max_dur or is_sentence_end)

        if should_cut:
            caption = " ".join(s["text"].strip() for s in current_segs[:3])
            if len(current_segs) > 3:
                caption += "..."
            cut_points.append((current_start, seg["end"], caption))
            current_start = seg["end"]
            current_segs = []

    # Remaining segments
    if current_segs:
        remaining_dur = current_segs[-1]["end"] - current_start
        if remaining_dur > 30:
            caption = " ".join(s["text"].strip() for s in current_segs[:3])
            if len(current_segs) > 3:
                caption += "..."
            cut_points.append((current_start, current_segs[-1]["end"], caption))

    # Cap at 5 clips max
    MAX_CLIPS = 10
    if len(cut_points) > MAX_CLIPS:
        cut_points = cut_points[:MAX_CLIPS]

    total = len(cut_points)
    logger.info(f"[{job_id[:8]}] smart_clip: {total} clips to cut")

    # Second pass: actually cut with progress
    clips = []
    for i, (start, end, caption) in enumerate(cut_points):
        # Update progress
        start_fmt = fmt_time(start)
        end_fmt = fmt_time(end)
        try:
            db = SessionLocal()
            job = db.query(Job).filter_by(id=job_id).first()
            if job:
                job.progress = f"✂ Cutting clip {i+1}/{total} ({start_fmt}-{end_fmt})"
                db.commit()
            db.close()
        except Exception:
            pass

        clip_path = cut_and_burn(video_path, srt_path, start, end, job_id, i)
        clip_duration = end - start
        clips.append((clip_path, caption, clip_duration))

    return clips


def cut_and_burn(
    video_path: Path,
    srt_path: Path,
    start: float,
    end: float,
    job_id: str,
    idx: int,
) -> Path:
    """FFmpeg: crop to 9:16, scale to 1080x1920, burn subtitles."""
    out_dir = STORAGE / "final" / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"clip_{idx:03d}.mp4"

    # ✅ FIXED: Escape path for FFmpeg subtitle filter
    escaped_srt = str(srt_path).replace("\\", "/").replace(":", "\\\\:").replace("'", "\\'")

    # FFmpeg: crop to 9:16, scale to TikTok resolution, burn subtitles
    vf = (
        f"crop=ih*9/16:ih,"
        f"scale=1080:1920,"
        f"subtitles='{escaped_srt}':"
        f"force_style='FontName=Arial,FontSize=18,PrimaryColour=&HFFFFFF,"
        f"OutlineColour=&H000000,Bold=1,Outline=2,Shadow=1,"
        f"Alignment=2,MarginV=80'"
    )

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start), "-to", str(end),
        "-i", str(video_path),
        "-vf", vf,
        "-c:v", "libx264",
        "-preset", settings.FFMPEG_PRESET,
        "-crf", str(settings.FFMPEG_CRF),
        "-threads", str(settings.FFMPEG_THREADS),   # ✅ FIXED: Limit CPU usage on VPS
        "-c:a", "aac", "-b:a", "128k",
        str(out_path),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed for clip {idx}: {result.stderr[-500:]}")

    return out_path


def generate_cover(clip_path: Path, caption: str, job_id: str, idx: int) -> Path:
    """Generate a TikTok-style cover image from the clip.
    
    Extracts a frame from the middle of the clip, adds text overlay with
    dark gradient background for readable hook text.
    """
    out_dir = STORAGE / "final" / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    cover_path = out_dir / f"cover_{idx:03d}.jpg"

    # Get clip duration to extract frame from middle
    probe_cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(clip_path),
    ]
    probe = subprocess.run(probe_cmd, capture_output=True, text=True)
    try:
        duration = float(probe.stdout.strip())
        seek_time = duration * 0.3  # 30% in — usually a good frame
    except (ValueError, AttributeError):
        seek_time = 2.0  # fallback: 2 seconds in

    # Truncate caption for overlay (max 2 lines)
    hook_text = caption[:80].replace("'", "\\'").replace('"', '\\"')
    if len(caption) > 80:
        hook_text += "..."

    # FFmpeg: extract frame, crop 9:16, add text overlay with gradient
    vf = (
        "crop=ih*9/16:ih,"
        "scale=1080:1920,"
        # Dark gradient at bottom for text readability
        "drawbox=x=0:y=ih*0.65:w=iw:h=ih*0.35:color=black@0.6:t=fill,"
        # Hook text
        f"drawtext=text='{hook_text}':"
        "fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
        "fontsize=42:fontcolor=white:"
        "x=(w-tw)/2:y=h*0.75:"
        "borderw=2:bordercolor=black@0.8"
    )

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(seek_time),
        "-i", str(clip_path),
        "-vf", vf,
        "-frames:v", "1",
        "-q:v", "2",
        str(cover_path),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.warning(f"Cover generation failed for clip {idx}: {result.stderr[-300:]}")
        return None

    logger.info(f"Cover generated: {cover_path.name}")
    return cover_path


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Utilities
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def parse_srt(srt_path: Path) -> list[dict]:
    """Parse an SRT file into a list of {start, end, text} dicts.

    ✅ FIXED: This function was called but never defined.
    """
    segments = []
    content = srt_path.read_text(encoding="utf-8")

    # SRT format: index, timestamp line, text, blank line
    blocks = content.strip().split("\n\n")
    time_pattern = re.compile(
        r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
    )

    for block in blocks:
        lines = block.strip().split("\n")
        if len(lines) < 2:
            continue

        # Find the timestamp line
        time_match = None
        text_lines = []
        for i, line in enumerate(lines):
            m = time_pattern.match(line.strip())
            if m:
                time_match = m
                text_lines = lines[i + 1:]
                break

        if not time_match:
            continue

        g = time_match.groups()
        start = int(g[0]) * 3600 + int(g[1]) * 60 + int(g[2]) + int(g[3]) / 1000
        end = int(g[4]) * 3600 + int(g[5]) * 60 + int(g[6]) + int(g[7]) / 1000
        text = " ".join(line.strip() for line in text_lines if line.strip())

        if text:
            segments.append({"start": start, "end": end, "text": text})

    return segments


def fmt_time(seconds: float) -> str:
    """Format seconds to SRT timestamp: HH:MM:SS,mmm"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"