"""
AI-powered clip analyzer — uses Groq/Llama to find viral-potential segments.

Instead of blindly cutting every 60-90 seconds, this module:
1. Sends the full transcript to Llama
2. AI identifies the most engaging/viral moments
3. Returns optimal clip timestamps with captions and viral scores
"""
import json
import logging
from groq import Groq
from config import settings
from dataclasses import dataclass, field


logger = logging.getLogger(__name__)

# ── Groq client (lazy init) ──
_client = None


def get_groq_client() -> Groq:
    global _client
    if _client is None:
        _client = Groq(api_key=settings.GROQ_API_KEY)
    return _client

# ── Niche Config ───────────────────────────────────────────────────────────────
@dataclass
class NicheConfig:
    label: str
    audience: str
    hook_examples: list[str]
    hashtags: list[str]
    avoid_topics: list[str] = field(default_factory=list)
 
 
NICHE_CONFIG: dict[str, NicheConfig] = {
    "finance": NicheConfig(
        label="Finance & Investasi",
        audience="Indonesian investors and professionals aged 20–40",
        hook_examples=[
            "Surprising financial statistics (e.g., '90% orang Indonesia tidak punya dana darurat')",
            "Counterintuitive money advice ('Menabung itu salah, ini yang benar...')",
            "Revealing common money mistakes that cost people silently",
            "Specific investment returns, percentages, or numbers that shock",
        ],
        hashtags=["#InvestasiIndonesia", "#FinancialFreedom", "#TipsKeuangan", "#Saham"],
    ),
    "bisnis": NicheConfig(
        label="Podcast Bisnis",
        audience="Indonesian entrepreneurs and startup founders aged 22–40",
        hook_examples=[
            "Controversial business takes ('Bisnis tanpa modal itu mitos...')",
            "Surprising revenue, growth, or failure numbers",
            "Founder failure stories with concrete lessons",
            "Contrarian advice against conventional business wisdom",
        ],
        hashtags=["#BisnisIndonesia", "#Entrepreneur", "#StartupIndonesia", "#UKM"],
    ),
    "motivasi": NicheConfig(
        label="Motivasi",
        audience="Indonesian youth aged 17–30 seeking personal growth",
        hook_examples=[
            "Relatable struggle most people feel but don't say out loud",
            "Shocking truth about success ('Kerja keras saja tidak cukup...')",
            "Personal transformation story with clear before/after contrast",
            "Bold challenge to conventional success narrative",
        ],
        hashtags=["#MotivasiHidup", "#SuksesIndonesia", "#MindsetPositif", "#PengembanganDiri"],
    ),
    "kesehatan": NicheConfig(
        label="Podcast Kesehatan",
        audience="Health-conscious Indonesians aged 20–45",
        hook_examples=[
            "Surprising health facts that contradict popular belief",
            "Common daily habits that secretly damage health",
            "Simple but underrated health tips backed by science",
            "Shocking statistics about Indonesian health trends",
        ],
        hashtags=["#KesehatanIndonesia", "#HidupSehat", "#TipsKesehatan", "#MedisIndonesia"],
    ),
    "pengembangan_diri": NicheConfig(
        label="Pengembangan Diri",
        audience="Indonesian millennials and Gen Z aged 18–35",
        hook_examples=[
            "Habits that high performers do differently than most people",
            "Common productivity myths debunked with evidence",
            "Surprising research on human behavior or psychology",
            "Personal story of radical self-improvement with specific results",
        ],
        hashtags=["#PengembanganDiri", "#SelfImprovement", "#ProductivityTips", "#GrowthMindset"],
    ),
    "edukasi": NicheConfig(
        label="Podcast Edukasi",
        audience="Indonesian students and lifelong learners aged 15–35",
        hook_examples=[
            "Surprising facts most people never learn in formal education",
            "Complex topics explained in a shockingly simple way",
            "Historical or scientific facts that challenge common assumptions",
            "Practical knowledge with immediate real-world application",
        ],
        hashtags=["#EdukasiIndonesia", "#BelajarOnline", "#IlmuPengetahuan", "#LearnOnTikTok"],
    ),
    "teknologi": NicheConfig(
        label="Teknologi / IT",
        audience="Indonesian tech enthusiasts and developers aged 18–35",
        hook_examples=[
            "Tech trends that will change Indonesia in the next 5 years",
            "Surprising AI or tech capabilities most people don't know yet",
            "How big tech companies actually work behind the scenes",
            "Simple tech skills that dramatically increase earning potential",
        ],
        hashtags=["#TeknologiIndonesia", "#AI", "#ProgrammerIndonesia", "#TechTips"],
    ),
    "agama": NicheConfig(
        label="Kajian / Agama",
        audience="Indonesian Muslim community aged 18–45",
        hook_examples=[
            "Commonly misunderstood Islamic concepts explained clearly and gently",
            "Relevant religious guidance for modern life challenges",
            "Inspiring story of faith with real-world positive impact",
            "Thought-provoking question about daily Islamic practice",
        ],
        hashtags=["#KajianIslam", "#DakwahDigital", "#IslamIndonesia", "#Ngaji"],
        avoid_topics=[
            "controversial interfaith debates",
            "political Islam or partisan religious topics",
            "content that could be divisive between Islamic schools of thought",
        ],
    ),
    "hiburan": NicheConfig(
        label="Hiburan / Entertainment",
        audience="General Indonesian audience aged 15–35",
        hook_examples=[
            "Shocking behind-the-scenes stories from the entertainment industry",
            "Surprising facts about celebrities or popular Indonesian culture",
            "Funny but insightful observations about Indonesian pop culture",
            "Dramatic story arc with an unexpected twist or reveal",
        ],
        hashtags=["#EntertainmentIndonesia", "#PopCulture", "#CeritaViral", "#Hiburan"],
    ),
    "gaming": NicheConfig(
        label="Gaming",
        audience="Indonesian gamers aged 15–30",
        hook_examples=[
            "Surprising gaming industry facts or statistics relevant to Indonesia",
            "Pro tips that most casual gamers don't know but immediately useful",
            "Controversial takes on popular games or Indonesian gaming culture",
            "Inspiring story of Indonesian gamer or esports achievement",
        ],
        hashtags=["#GamingIndonesia", "#MobileGaming", "#GameTips", "#Esports"],
    ),
    "other": NicheConfig(
        label="Lainnya",
        audience="General Indonesian audience aged 18–35",
        hook_examples=[
            "Counterintuitive or surprising statement that stops the scroll",
            "Relatable everyday experience with an unexpected insight",
            "Specific numbers or facts that immediately grab attention",
            "Bold opinion that naturally sparks debate or discussion",
        ],
        hashtags=["#PodcastIndonesia", "#ContenIndonesia", "#Edukasi", "#Inspirasi"],
    ),
}

# ── Prompt Builder ─────────────────────────────────────────────────────────────
def build_system_prompt(niche: str) -> str:
    """Build a niche-specific system prompt for the Groq model."""
    config = NICHE_CONFIG.get(niche, NICHE_CONFIG["other"])
 
    avoid_section = ""
    if config.avoid_topics:
        avoid_list = "\n   - ".join(config.avoid_topics)
        avoid_section = f"\nAVOID TOPICS for this niche:\n   - {avoid_list}\n"
 
    hook_examples = "\n   - ".join(config.hook_examples)
    hashtags = " ".join(config.hashtags)
 
    return f"""You are a TikTok clip editor specializing in Indonesian {config.label} content
for {config.audience}.
 
Your task: analyze Indonesian podcast transcripts and select 3–5 clips
with the highest viral potential for TikTok's For You Page (FYP).
 
SELECTION CRITERIA (ranked by priority):
 
1. KILLER HOOK — The clip must START with one of these (tailored for {config.label}):
   - {hook_examples}
 
2. SELF-CONTAINED — Must be 100% understandable without watching the full video.
   STRICTLY FORBID starting clips from sentences containing:
   - "Tadi kita udah bahas..." / "Seperti yang dia bilang..."
   - "Nah, jadi..." / "Oke, jadi..." (filler transition phrases)
   - References to names or concepts not introduced within the clip itself
 
3. SAVE-WORTHY — Contains frameworks, specific numbers, or insights
   that make people want to save and rewatch the video.
 
4. SHARE-WORTHY — Content people would forward to a friend saying
   "eh ini relevan buat lo" or "harus nonton ini".
 
5. EMOTIONAL TRIGGER — Triggers FOMO, inspiration, shock, challenge, or laughter.
{avoid_section}
TECHNICAL RULES:
- Ideal duration: 30–75 seconds. Hard reject: under 25s or over 90s.
- start = timestamp EXACTLY at the hook sentence (not one sentence before it)
- end = timestamp after punchline or conclusion that feels naturally complete
- Only return clips with viral_score >= 7
- Use these hashtags as reference for caption: {hashtags}
- Return ONLY valid JSON. No explanation, no markdown, no text outside JSON."""
 
 
USER_PROMPT_TEMPLATE = """Analyze this Indonesian {niche_label} podcast transcript.
Timestamps are in seconds (float) for FFmpeg precision.
 
--- TRANSCRIPT START ---
{transcript}
--- TRANSCRIPT END ---
 
Select 3–5 best clips. Each clip MUST start exactly at its hook sentence — not before.
 
Return this exact JSON structure:
{{
  "clips": [
    {{
      "rank": 1,
      "start": 125.5,
      "end": 195.0,
      "duration_seconds": 69.5,
      "viral_score": 9,
      "hook_type": "controversial_statement | surprising_stat | strong_opinion | reveal_twist | rhetorical_question | relatable_moment",
      "hook_sentence": "Verbatim first sentence from transcript at start timestamp",
      "caption": "Caption TikTok bahasa Indonesia hook-style max 150 chars #HashtagNiche",
      "reason": "Why this clip can hit FYP in 1-2 sentences"
    }}
  ],
  "total_viral_potential": 8,
  "content_summary": "One-sentence summary of main topic in Indonesian"
}}"""
 
 
# ── Main Analyzer ──────────────────────────────────────────────────────────────
def analyze_transcript_for_viral_clips(
    transcript_segments: list[dict],
    niche: str = "other",
    min_viral_score: int = 7,
) -> list[dict]:
    """
    Send transcript to Groq/Llama to identify viral clip segments.
 
    Args:
        transcript_segments: List of {start, end, text} dicts from Whisper/SRT
        niche: Content niche key (must match NICHE_CONFIG keys)
        min_viral_score: Minimum viral score to include (1–10)
 
    Returns:
        List of validated clip dicts sorted by viral_score descending
    """
    if not transcript_segments:
        logger.warning("Empty transcript segments, skipping analysis.")
        return []
 
    client = get_groq_client()
    config = NICHE_CONFIG.get(niche, NICHE_CONFIG["other"])
 
    # Build transcript text with float timestamps for model precision
    transcript_text = ""
    for seg in transcript_segments:
        transcript_text += f"[{seg['start']:.1f}s] {seg['text'].strip()}\n"
 
    system_prompt = build_system_prompt(niche)
    user_prompt = USER_PROMPT_TEMPLATE.format(
        niche_label=config.label,
        transcript=transcript_text,
    )
 
    logger.info(
        f"Sending {len(transcript_segments)} segments to Groq "
        f"(niche: {config.label}, model: {settings.GROQ_MODEL})"
    )
 
    result_text = ""
    try:
        response = client.chat.completions.create(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            model=settings.GROQ_MODEL,
            temperature=0.3,   # low = consistent JSON structure
            max_tokens=4096,
            response_format={"type": "json_object"},
        )
 
        result_text = response.choices[0].message.content or ""
        result = json.loads(result_text)
        clips: list[dict] = result.get("clips", [])
 
        logger.info(
            f"Raw clips from model: {len(clips)} "
            f"(total_viral_potential: {result.get('total_viral_potential', 'N/A')}/10)"
        )
        logger.info(f"Content summary: {result.get('content_summary', '-')}")
 
        # ── Validation & Filtering ─────────────────────────────────────────────
        max_time = transcript_segments[-1]["end"]
        validated: list[dict] = []
 
        for clip in clips:
            start          = clip.get("start", -1)
            end            = clip.get("end", -1)
            viral_score    = clip.get("viral_score", 0)
            duration       = round(end - start, 1) if end > start else 0
 
            # Score filter
            if viral_score < min_viral_score:
                logger.debug(f"Clip rejected (low score {viral_score}): {clip.get('hook_sentence', '')[:60]}")
                continue
 
            # Timestamp sanity checks
            if start < 0 or end < 0:
                logger.debug(f"Clip rejected (negative timestamp): start={start} end={end}")
                continue
 
            if end <= start:
                logger.debug(f"Clip rejected (end <= start): start={start} end={end}")
                continue
 
            if start >= max_time:
                logger.debug(f"Clip rejected (start beyond transcript): start={start} max={max_time}")
                continue
 
            if end > max_time + 5:  # allow 5s buffer for transcript edge
                logger.debug(f"Clip rejected (end too far beyond transcript): end={end} max={max_time}")
                continue
 
            # Duration enforcement
            if duration < 25:
                logger.debug(f"Clip rejected (too short {duration}s): {clip.get('hook_sentence', '')[:60]}")
                continue
 
            if duration > 90:
                logger.debug(f"Clip rejected (too long {duration}s): {clip.get('hook_sentence', '')[:60]}")
                continue
 
            # Recalculate duration from actual timestamps (don't trust model's value)
            clip["duration_seconds"] = duration
            validated.append(clip)
 
        # Sort by viral_score descending, re-rank
        validated.sort(key=lambda c: c.get("viral_score", 0), reverse=True)
        for i, clip in enumerate(validated, start=1):
            clip["rank"] = i
 
        logger.info(f"Validated clips after filtering: {len(validated)}/{len(clips)}")
        return validated
 
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse Groq response as JSON: {e}")
        logger.error(f"Raw response (500 chars): {result_text[:500]}")
        return []
    except Exception as e:
        logger.error(f"Groq API error: {e}")
        return []
 
 
# ── Caption Generator (fallback) ───────────────────────────────────────────────
def generate_tiktok_caption(
    clip_text: str,
    niche: str = "other",
    ai_caption: str | None = None,
) -> str:
    """
    Return a TikTok caption for a clip.
 
    Priority:
    1. Use AI-generated caption from analyze_transcript_for_viral_clips (preferred)
    2. Generate a new one via Groq if ai_caption is missing or too short
    3. Fallback to truncated clip text if Groq fails
    """
    if ai_caption and len(ai_caption.strip()) > 10:
        return ai_caption.strip()
 
    config = NICHE_CONFIG.get(niche, NICHE_CONFIG["other"])
    hashtags_str = " ".join(config.hashtags[:4])
 
    client = get_groq_client()
    try:
        response = client.chat.completions.create(
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"You write short TikTok captions in Indonesian for {config.label} content. "
                        "Hook-style only — make people curious or deeply relate, not summarize. "
                        "Max 150 characters total including hashtags. "
                        f"Use 3–4 niche-specific hashtags like: {hashtags_str}. "
                        "NEVER use #fyp #viral #foryou — they are oversaturated and ineffective."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Write a caption for this clip:\n\n{clip_text[:500]}",
                },
            ],
            model=settings.GROQ_MODEL,
            temperature=0.6,  # slightly higher for creative captions
            max_tokens=200,
        )
        caption = response.choices[0].message.content.strip()
        logger.debug(f"Generated caption: {caption}")
        return caption
 
    except Exception as e:
        logger.warning(f"Caption generation failed, using fallback: {e}")
        return clip_text[:120].strip() + "..."
 
 
# ── Utility ────────────────────────────────────────────────────────────────────
def get_niche_label(niche: str) -> str:
    """Get human-readable label for a niche key."""
    config = NICHE_CONFIG.get(niche, NICHE_CONFIG["other"])
    return config.label
 
 
def get_niche_hashtags(niche: str) -> list[str]:
    """Get hashtag list for a niche key."""
    config = NICHE_CONFIG.get(niche, NICHE_CONFIG["other"])
    return config.hashtags