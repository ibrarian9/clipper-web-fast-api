"""
Central configuration — semua settings dari environment variables.
"""
from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    # ── Database ──
    DATABASE_URL: str = "mysql+pymysql://root@localhost:3306/clipper"

    # ── Redis ──
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_BACKEND: str = "redis://localhost:6379/1"

    # ── Storage ──
    STORAGE_ROOT: Path = Path("/opt/clipper/storage")
    COOKIES_PATH: Path = Path("/opt/clipper/cookies/tiktok_session.json")
    CLEANUP_AFTER_CLIP: bool = True          # Delete source video after clipping
    DISK_WARN_PERCENT: int = 85              # Alert when disk usage > this %
    CLIP_RETENTION_DAYS: int = 30            # Auto-delete clips older than N days

    # ── Whisper ──
    WHISPER_MODEL: str = "small"             # tiny | base | small
    WHISPER_DEVICE: str = "cpu"
    WHISPER_COMPUTE_TYPE: str = "int8"
    WHISPER_LANGUAGE: str = "id"             # Indonesian

    # ── FFmpeg ──
    FFMPEG_THREADS: int = 2                  # Match vCPU count
    FFMPEG_PRESET: str = "fast"
    FFMPEG_CRF: int = 23
    VIDEO_MAX_HEIGHT: int = 720              # Download quality cap

    # ── Clipping ──
    DEFAULT_CLIP_MIN_DURATION: int = 60      # Minimum clip length (seconds)
    DEFAULT_CLIP_MAX_DURATION: int = 90      # Maximum clip length (seconds)

    # ── AI / Groq ──
    GROQ_API_KEY: str = ""                   # Required for AI clip detection
    GROQ_MODEL: str = "meta-llama/llama-4-scout-17b-16e-instruct"
    AI_CLIP_ENABLED: bool = True             # True = AI finds viral clips, False = blind cut
    AI_MIN_VIRAL_SCORE: int = 7              # Only keep clips rated >= this (1-10)
    DEFAULT_NICHE: str = "finance"           # finance | bisnis | motivasi

    # ── TikTok Upload ──
    TIKTOK_UPLOAD_URL: str = "https://www.tiktok.com/creator#/upload"
    TIKTOK_MAX_UPLOADS_PER_DAY: int = 10
    TIKTOK_DELAY_BETWEEN_UPLOADS_MIN: int = 300   # 5 minutes
    TIKTOK_DELAY_BETWEEN_UPLOADS_MAX: int = 900   # 15 minutes
    TIKTOK_AUTO_UPLOAD: bool = False               # False = review mode

    # ── Celery ──
    CELERY_CONCURRENCY: int = 1             # 1 job at a time on 2vCPU

    class Config:
        env_file = str(Path(__file__).resolve().parent.parent / ".env")
        env_file_encoding = "utf-8"


settings = Settings()
