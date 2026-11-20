"""
Pydantic request / response schemas for the FastAPI endpoints.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


# ── Request schemas ──────────────────────────────────────────────────

class TopicRequest(BaseModel):
    topic: Optional[str] = Field(None, description="Custom topic (auto-generated if omitted)")
    category: str = Field(default="Technology", description="Video category")


class ScriptRequest(BaseModel):
    video_id: int
    voice: Optional[str] = Field(None, description="Voice override for language detection")


class AudioRequest(BaseModel):
    video_id: int
    voice: Optional[str] = Field(None, description="Edge-TTS voice name override")


class ImageRequest(BaseModel):
    video_id: int


class VideoRequest(BaseModel):
    video_id: int
    format: Optional[str] = Field(None, description="horizontal | vertical")


class SubtitleRequest(BaseModel):
    video_id: int


class ThumbnailRequest(BaseModel):
    video_id: int


class UploadRequest(BaseModel):
    video_id: int
    privacy: str = Field(default="private", description="private | unlisted | public")
    scheduled_at: Optional[str] = Field(None, description="ISO datetime for scheduled publish")


class FullPipelineRequest(BaseModel):
    topic: Optional[str] = None
    category: str = Field(default="Technology")
    voice: Optional[str] = None
    format: Optional[str] = None


# ── Response schemas ─────────────────────────────────────────────────

class StatusResponse(BaseModel):
    video_id: int
    status: str
    message: str


class VideoResponse(BaseModel):
    id: int
    topic: str
    category: str
    title: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[str] = None
    hashtags: Optional[str] = None
    chapters: Optional[str] = None
    upload_status: str
    privacy_status: str
    youtube_url: Optional[str] = None
    youtube_video_id: Optional[str] = None
    video_format: str
    voice: Optional[str] = None
    scheduled_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    error_message: Optional[str] = None

    model_config = {"from_attributes": True}


class HistoryResponse(BaseModel):
    total: int
    videos: List[VideoResponse]
