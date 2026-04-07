"""
Storage manager — disk monitoring, cleanup, retention policy.
"""
import shutil
import logging
from pathlib import Path
from datetime import datetime, timedelta
from config import settings

logger = logging.getLogger(__name__)


def get_disk_usage() -> dict:
    """Return disk usage info for the storage root."""
    root = settings.STORAGE_ROOT
    root.mkdir(parents=True, exist_ok=True)

    total, used, free = shutil.disk_usage(str(root))
    return {
        "total_gb": round(total / (1024**3), 1),
        "used_gb": round(used / (1024**3), 1),
        "free_gb": round(free / (1024**3), 1),
        "used_percent": round((used / total) * 100, 1),
    }


def cleanup_source_video(job_id: str) -> bool:
    """Delete the downloaded source video for a job to free disk space."""
    download_dir = settings.STORAGE_ROOT / "downloads" / job_id
    if download_dir.exists():
        shutil.rmtree(download_dir)
        logger.info(f"Cleaned up source video for job {job_id}")
        return True
    return False


def cleanup_job_clips(job_id: str) -> bool:
    """Delete all generated clips for a job."""
    clips_dir = settings.STORAGE_ROOT / "final" / job_id
    if clips_dir.exists():
        shutil.rmtree(clips_dir)
        logger.info(f"Cleaned up clips for job {job_id}")
        return True
    return False


def cleanup_old_clips(days: int = None) -> int:
    """Delete clip directories older than N days. Returns count of cleaned dirs."""
    days = days or settings.CLIP_RETENTION_DAYS
    cutoff = datetime.now() - timedelta(days=days)
    final_dir = settings.STORAGE_ROOT / "final"

    if not final_dir.exists():
        return 0

    cleaned = 0
    for job_dir in final_dir.iterdir():
        if job_dir.is_dir():
            # Use directory modification time as proxy for age
            mtime = datetime.fromtimestamp(job_dir.stat().st_mtime)
            if mtime < cutoff:
                shutil.rmtree(job_dir)
                logger.info(f"Auto-cleaned old clips: {job_dir.name}")
                cleaned += 1

    return cleaned


def emergency_cleanup() -> bool:
    """Force cleanup if disk usage exceeds threshold."""
    usage = get_disk_usage()
    if usage["used_percent"] < settings.DISK_WARN_PERCENT:
        return False

    logger.warning(f"Disk usage at {usage['used_percent']}% — running emergency cleanup")

    # Step 1: Delete all source videos (downloads)
    downloads_dir = settings.STORAGE_ROOT / "downloads"
    if downloads_dir.exists():
        shutil.rmtree(downloads_dir)
        downloads_dir.mkdir(parents=True, exist_ok=True)
        logger.warning("Emergency: deleted all source videos")

    # Step 2: If still full, delete old clips
    usage = get_disk_usage()
    if usage["used_percent"] >= settings.DISK_WARN_PERCENT:
        cleaned = cleanup_old_clips(days=7)  # More aggressive: 7 days
        logger.warning(f"Emergency: cleaned {cleaned} old clip directories")

    return True


def get_screenshots_dir() -> Path:
    """Get directory for storing failure screenshots."""
    d = settings.STORAGE_ROOT / "screenshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_storage_dirs():
    """Create all required storage directories."""
    dirs = [
        settings.STORAGE_ROOT / "downloads",
        settings.STORAGE_ROOT / "final",
        settings.STORAGE_ROOT / "screenshots",
        settings.COOKIES_PATH.parent,
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
