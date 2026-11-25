"""
SQLAlchemy ORM model for the Video entity.
Tracks every artifact and status throughout the generation → upload pipeline.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String, Text
from app.database import Base


class Video(Base):
    __tablename__ = "videos"

    id = Column(Integer, primary_key=True, autoincrement=True)
    topic = Column(String(500), nullable=False)
    category = Column(String(100), nullable=False, default="General")
    title = Column(String(500), nullable=True)
    description = Column(Text, nullable=True)
    tags = Column(Text, nullable=True)            # JSON list
    hashtags = Column(Text, nullable=True)         # JSON list
    chapters = Column(Text, nullable=True)         # Chapter markers text
    script = Column(Text, nullable=True)           # Full narration script
    image_prompts = Column(Text, nullable=True)    # JSON list of image prompts

    # Generated file paths
    audio_path = Column(String(500), nullable=True)
    image_paths = Column(Text, nullable=True)      # JSON list of image file paths
    video_path = Column(String(500), nullable=True)
    subtitle_path = Column(String(500), nullable=True)
    thumbnail_path = Column(String(500), nullable=True)

    # YouTube metadata
    youtube_url = Column(String(500), nullable=True)
    youtube_video_id = Column(String(50), nullable=True)
    upload_status = Column(
        String(20), nullable=False, default="pending"
    )  # pending | generating | ready | awaiting_approval | uploading | uploaded | failed | cancelled
    privacy_status = Column(String(20), nullable=False, default="private")

    # Scheduling
    scheduled_at = Column(DateTime, nullable=True)

    # Preferences
    video_format = Column(String(20), nullable=False, default="horizontal")
    voice = Column(String(100), nullable=True)
    language = Column(String(10), nullable=True)          # e.g. "en", "te"
    source = Column(String(20), nullable=True)            # topic | script | audio | api
    telegram_chat_id = Column(String(50), nullable=True)  # chat that owns this video
    topic_en = Column(String(500), nullable=True)        # English meaning of the topic (used for research)
    target_seconds = Column(Integer, nullable=True)       # requested length (Shorts: ≤ 59)
    sources = Column(Text, nullable=True)                 # JSON list of {title, url, publisher, published, kind}
    fact_check = Column(Text, nullable=True)              # JSON FactCheck result

    # Timestamps
    created_at = Column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = Column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # Error tracking
    error_message = Column(Text, nullable=True)
    retry_count = Column(Integer, nullable=False, default=0)

    def __repr__(self) -> str:
        return (
            f"<Video id={self.id} topic='{self.topic[:40]}' "
            f"status={self.upload_status}>"
        )
