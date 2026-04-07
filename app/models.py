from sqlalchemy import Column, String, Integer, Enum, DateTime, Text, ForeignKey
from sqlalchemy.orm import relationship
from database import Base
import enum
import datetime


class JobStatus(str, enum.Enum):
    pending = "pending"
    downloading = "downloading"
    transcribing = "transcribing"
    clipping = "clipping"
    uploading = "uploading"
    done = "done"
    failed = "failed"


class ClipStatus(str, enum.Enum):
    ready = "ready"            # Clip ready, waiting for review/upload
    approved = "approved"      # User approved for upload
    uploading = "uploading"    # Currently uploading to TikTok
    done = "done"              # Successfully uploaded
    failed = "failed"          # Upload failed


class Job(Base):
    __tablename__ = "jobs"
    id          = Column(String(36), primary_key=True)
    youtube_url = Column(String(500), nullable=False)
    title       = Column(String(500))
    niche       = Column(String(100), default="finance")
    status      = Column(Enum(JobStatus), default=JobStatus.pending)
    progress    = Column(String(500), default="Waiting in queue...")     # live step detail
    error       = Column(Text)
    clip_count  = Column(Integer, default=0)
    created_at  = Column(DateTime, default=datetime.datetime.utcnow)

    clips = relationship("Clip", back_populates="job", lazy="selectin")


class Clip(Base):
    __tablename__ = "clips"
    id          = Column(String(36), primary_key=True)
    job_id      = Column(String(36), ForeignKey("jobs.id"), nullable=False)
    filename    = Column(String(500))
    filepath    = Column(String(1000))
    caption     = Column(Text)
    duration    = Column(Integer)                                      # clip duration in seconds
    file_size   = Column(Integer, default=0)                             # file size in KB
    viral_score = Column(Integer, default=0)                            # AI viral score 1-10 (0=no AI)
    status      = Column(Enum(ClipStatus), default=ClipStatus.ready)
    cover_path  = Column(String(1000))                                  # thumbnail/cover image path
    tiktok_url  = Column(String(500))
    error       = Column(Text)
    created_at  = Column(DateTime, default=datetime.datetime.utcnow)
    uploaded_at = Column(DateTime, nullable=True)

    job = relationship("Job", back_populates="clips")