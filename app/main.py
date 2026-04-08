"""
FastAPI web app — dashboard, job management, clip review workflow.
"""
from fastapi import FastAPI, Request, Form, Depends, HTTPException, Query
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import func
import uuid
import asyncio
import json
import logging

from app.database import get_db, init_db, SessionLocal
from app.models import Job, Clip, JobStatus, ClipStatus
from app.config import settings
from app.storage import get_disk_usage, ensure_storage_dirs

logger = logging.getLogger(__name__)

app = FastAPI(title="Clipper", version="1.0.0")
templates = Jinja2Templates(directory="/opt/clipper-app/clipper-web-fast-api/app/templates")


# ── Jinja2 custom filters ──
def filesize_filter(size_kb: int) -> str:
    """Convert KB to human-readable size."""
    if not size_kb:
        return "—"
    if size_kb < 1024:
        return f"{size_kb} KB"
    return f"{size_kb / 1024:.1f} MB"


templates.env.filters["filesize"] = filesize_filter

# Available niches for AI analysis
NICHE_OPTIONS = [
    {"value": "finance", "label": "Finance / Investasi"},
    {"value": "bisnis", "label": "Podcast Bisnis"},
    {"value": "motivasi", "label": "Motivasi"},
    {"value": "kesehatan", "label": "Podcast Kesehatan"},
    {"value": "pengembangan_diri", "label": "Pengembangan Diri"},
    {"value": "edukasi", "label": "Podcast Edukasi"},
    {"value": "teknologi", "label": "Teknologi / IT"},
    {"value": "agama", "label": "Kajian / Agama"},
    {"value": "hiburan", "label": "Hiburan / Entertainment"},
    {"value": "gaming", "label": "Gaming"},
    {"value": "other", "label": "Lainnya"},
]


@app.on_event("startup")
def startup():
    init_db()
    ensure_storage_dirs()

    # Fix #9: Auto-cleanup old clips on startup
    from app.storage import cleanup_old_clips
    cleaned = cleanup_old_clips()
    if cleaned:
        logger.info(f"Startup cleanup: removed {cleaned} old clip directories")

    logger.info("Clipper started — storage dirs ready, DB tables created")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Dashboard
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)):
    jobs = db.query(Job).order_by(Job.created_at.desc()).limit(20).all()

    # Compute stats
    total_jobs = db.query(func.count(Job.id)).scalar() or 0
    total_clips = db.query(func.count(Clip.id)).scalar() or 0
    active_jobs = db.query(func.count(Job.id)).filter(
        Job.status.in_([JobStatus.downloading, JobStatus.transcribing, JobStatus.clipping])
    ).scalar() or 0
    failed_jobs = db.query(func.count(Job.id)).filter(Job.status == JobStatus.failed).scalar() or 0

    # Disk info
    disk = get_disk_usage()

    return templates.TemplateResponse("index.html", {
        "request": request,
        "active_page": "dashboard",
        "jobs": jobs,
        "niches": NICHE_OPTIONS,
        "stats": {
            "total": total_jobs,
            "clips": total_clips,
            "active": active_jobs,
            "failed": failed_jobs,
        },
        "disk_used": disk["used_gb"],
        "disk_pct": int(disk["used_percent"]),
    })


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Job CRUD
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.post("/jobs")
async def create_job(
    youtube_url: str = Form(...),
    max_clip_duration: int = Form(90),
    niche: str = Form("finance"),
    db: Session = Depends(get_db),
):
    from app.tasks import run_pipeline

    job_id = str(uuid.uuid4())
    job = Job(id=job_id, youtube_url=youtube_url, niche=niche)
    db.add(job)
    db.commit()

    # Kick off async pipeline via Celery
    run_pipeline.delay(job_id, youtube_url, max_clip_duration, niche)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.get("/jobs", response_class=HTMLResponse)
async def all_jobs(request: Request, db: Session = Depends(get_db)):
    """All Jobs page."""
    jobs = db.query(Job).order_by(Job.created_at.desc()).all()
    disk = get_disk_usage()
    return templates.TemplateResponse("jobs.html", {
        "request": request,
        "active_page": "jobs",
        "jobs": jobs,
        "disk_used": disk["used_gb"],
        "disk_pct": int(disk["used_percent"]),
    })


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail(job_id: str, request: Request, db: Session = Depends(get_db)):
    job = db.query(Job).filter_by(id=job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    clips = db.query(Clip).filter_by(job_id=job_id).order_by(Clip.viral_score.desc()).all()
    disk = get_disk_usage()
    return templates.TemplateResponse("job.html", {
        "request": request,
        "active_page": "jobs",
        "job": job,
        "clips": clips,
        "disk_used": disk["used_gb"],
        "disk_pct": int(disk["used_percent"]),
    })


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Clips Page
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.get("/clips", response_class=HTMLResponse)
async def all_clips(request: Request, db: Session = Depends(get_db)):
    """All Clips page — browse all clips across jobs."""
    clips = db.query(Clip).order_by(Clip.created_at.desc()).all()
    disk = get_disk_usage()
    return templates.TemplateResponse("clips.html", {
        "request": request,
        "active_page": "clips",
        "clips": clips,
        "disk_used": disk["used_gb"],
        "disk_pct": int(disk["used_percent"]),
    })


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Storage Page
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.get("/storage", response_class=HTMLResponse)
async def storage_page(request: Request, db: Session = Depends(get_db)):
    """Storage management page."""
    disk = get_disk_usage()

    # Count files per category
    import os
    downloads_dir = settings.STORAGE_ROOT / "downloads"
    final_dir = settings.STORAGE_ROOT / "final"

    download_jobs = len(list(downloads_dir.iterdir())) if downloads_dir.exists() else 0
    clip_jobs = len(list(final_dir.iterdir())) if final_dir.exists() else 0

    return templates.TemplateResponse("storage.html", {
        "request": request,
        "active_page": "storage",
        "disk": disk,
        "disk_used": disk["used_gb"],
        "disk_pct": int(disk["used_percent"]),
        "download_jobs": download_jobs,
        "clip_jobs": clip_jobs,
    })


@app.post("/storage/cleanup")
async def storage_cleanup(cleanup_type: str = Form("downloads")):
    """Trigger manual storage cleanup."""
    from app.storage import cleanup_old_clips, emergency_cleanup
    import shutil

    if cleanup_type == "downloads":
        downloads_dir = settings.STORAGE_ROOT / "downloads"
        if downloads_dir.exists():
            shutil.rmtree(downloads_dir)
            downloads_dir.mkdir(parents=True, exist_ok=True)
    elif cleanup_type == "old_clips":
        cleanup_old_clips(days=7)
    elif cleanup_type == "emergency":
        emergency_cleanup()

    return RedirectResponse("/storage", status_code=303)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Settings Page
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    """System settings page."""
    from app.uploader import check_session_valid
    disk = get_disk_usage()
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "active_page": "settings",
        "settings": settings,
        "niches": NICHE_OPTIONS,
        "tiktok_session_valid": check_session_valid(),
        "disk_used": disk["used_gb"],
        "disk_pct": int(disk["used_percent"]),
    })


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TikTok Session Management
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.post("/api/tiktok/upload-cookies")
async def upload_tiktok_cookies(cookies_file: bytes = Form(...)):
    """Upload TikTok cookies JSON from browser extension (e.g. EditThisCookie)."""
    try:
        cookies = json.loads(cookies_file)
        if not isinstance(cookies, list):
            raise HTTPException(status_code=400, detail="Cookies JSON must be an array")

        # Save cookies
        settings.COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(settings.COOKIES_PATH, "w") as f:
            json.dump(cookies, f)

        logger.info(f"TikTok cookies uploaded: {len(cookies)} cookies saved")
        return RedirectResponse("/settings", status_code=303)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON file")


@app.post("/api/tiktok/clear-session")
async def clear_tiktok_session():
    """Remove saved TikTok session."""
    if settings.COOKIES_PATH.exists():
        settings.COOKIES_PATH.unlink()
        logger.info("TikTok session cleared")
    return RedirectResponse("/settings", status_code=303)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Clip Management (Review Workflow)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.post("/api/clips/{clip_id}/approve")
async def approve_clip(clip_id: str, db: Session = Depends(get_db)):
    """Approve a clip for TikTok upload."""
    clip = db.query(Clip).filter_by(id=clip_id).first()
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    if clip.status != ClipStatus.ready:
        raise HTTPException(status_code=400, detail=f"Clip not in 'ready' state (current: {clip.status.value})")

    clip.status = ClipStatus.approved
    db.commit()

    # Queue the upload task
    from app.uploader import upload_to_tiktok
    upload_to_tiktok.delay(clip.id, clip.filepath, clip.caption)

    return {"status": "approved", "message": "Clip queued for TikTok upload"}


@app.post("/api/clips/approve-all")
async def approve_all_clips(job_id: str = Form(...), db: Session = Depends(get_db)):
    """Approve all ready clips for a job."""
    clips = db.query(Clip).filter_by(job_id=job_id, status=ClipStatus.ready).all()
    if not clips:
        raise HTTPException(status_code=404, detail="No ready clips found")

    from app.uploader import upload_to_tiktok
    for clip in clips:
        clip.status = ClipStatus.approved
        upload_to_tiktok.delay(clip.id, clip.filepath, clip.caption)

    db.commit()
    return {"status": "approved", "count": len(clips)}


@app.get("/api/clips/{clip_id}/preview")
async def clip_preview(clip_id: str, db: Session = Depends(get_db)):
    """Serve clip video file for preview."""
    clip = db.query(Clip).filter_by(id=clip_id).first()
    if not clip or not clip.filepath:
        raise HTTPException(status_code=404, detail="Clip not found")

    from pathlib import Path
    path = Path(clip.filepath)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Clip file not found on disk")

    return FileResponse(str(path), media_type="video/mp4", filename=clip.filename)


@app.get("/api/clips/{clip_id}/cover")
async def clip_cover(clip_id: str, db: Session = Depends(get_db)):
    """Serve clip cover/thumbnail image."""
    clip = db.query(Clip).filter_by(id=clip_id).first()
    if not clip or not clip.cover_path:
        raise HTTPException(status_code=404, detail="Cover not found")

    from pathlib import Path
    path = Path(clip.cover_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Cover file not found on disk")

    return FileResponse(str(path), media_type="image/jpeg")


@app.post("/api/jobs/{job_id}/delete")
async def delete_job(job_id: str, db: Session = Depends(get_db)):
    """Delete a job and all its clips."""
    from app.storage import cleanup_source_video, cleanup_job_clips

    job = db.query(Job).filter_by(id=job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Delete clips from DB
    db.query(Clip).filter_by(job_id=job_id).delete()
    db.delete(job)
    db.commit()

    # Delete files
    cleanup_source_video(job_id)
    cleanup_job_clips(job_id)

    return RedirectResponse("/jobs", status_code=303)


@app.post("/api/jobs/{job_id}/retry")
async def retry_job(job_id: str, db: Session = Depends(get_db)):
    """Retry a failed or interrupted job."""
    from app.tasks import run_pipeline

    job = db.query(Job).filter_by(id=job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Delete existing clips (will be regenerated)
    db.query(Clip).filter_by(job_id=job_id).delete()

    # Reset job state
    job.status = JobStatus.pending
    job.progress = "⟳ Retrying..."
    job.error = None
    job.clip_count = 0
    db.commit()

    # Re-dispatch pipeline
    run_pipeline.delay(job_id, job.youtube_url, 90, job.niche or "finance")
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Startup: Recover stale jobs
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.on_event("startup")
async def recover_stale_jobs():
    """Mark jobs stuck in processing state (from server crash) as failed."""
    db = SessionLocal()
    try:
        stale_statuses = [JobStatus.downloading, JobStatus.transcribing, JobStatus.clipping]
        stale_jobs = db.query(Job).filter(Job.status.in_(stale_statuses)).all()
        for job in stale_jobs:
            logger.warning(f"Recovering stale job {job.id[:8]} (was {job.status.value})")
            job.status = JobStatus.failed
            job.progress = "⚠ Interrupted — server was restarted. Click Retry to resume."
            job.error = f"Job was interrupted during {job.status.value}. Server was restarted."
        if stale_jobs:
            db.commit()
            logger.info(f"Recovered {len(stale_jobs)} stale jobs")
    finally:
        db.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# API Endpoints
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.get("/api/jobs/{job_id}/status")
async def job_status(job_id: str, db: Session = Depends(get_db)):
    job = db.query(Job).filter_by(id=job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    clips = db.query(Clip).filter_by(job_id=job_id).all()
    return {
        "status": job.status.value,
        "title": job.title,
        "progress": job.progress,
        "clip_count": job.clip_count,
        "error": job.error,
        "clips": [
            {
                "id": c.id,
                "filename": c.filename,
                "caption": c.caption,
                "status": c.status.value,
                "duration": c.duration,
                "viral_score": c.viral_score,
            }
            for c in clips
        ],
    }


@app.get("/api/jobs/{job_id}/stream")
async def job_stream(job_id: str, db: Session = Depends(get_db)):
    """Server-Sent Events for real-time job status updates."""
    job = db.query(Job).filter_by(id=job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    async def event_generator():
        while True:
            # Re-query in fresh session
            from app.database import SessionLocal
            _db = SessionLocal()
            try:
                _job = _db.query(Job).filter_by(id=job_id).first()
                if not _job:
                    break

                data = json.dumps({
                    "status": _job.status.value,
                    "title": _job.title,
                    "progress": _job.progress,
                    "clip_count": _job.clip_count,
                    "error": _job.error,
                })
                yield f"data: {data}\n\n"

                # Stop streaming if job is done or failed
                if _job.status in (JobStatus.done, JobStatus.failed):
                    break
            finally:
                _db.close()

            await asyncio.sleep(3)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/api/system/health")
async def system_health(db: Session = Depends(get_db)):
    """System health check: disk, DB, worker status."""
    disk = get_disk_usage()
    from app.uploader import check_session_valid

    return {
        "disk": disk,
        "tiktok_session_valid": check_session_valid(),
        "active_jobs": db.query(func.count(Job.id)).filter(
            Job.status.in_([JobStatus.downloading, JobStatus.transcribing, JobStatus.clipping])
        ).scalar(),
        "whisper_model": settings.WHISPER_MODEL,
        "auto_upload": settings.TIKTOK_AUTO_UPLOAD,
    }