"""
Upload & history router — YouTube upload, status checks, and video history.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.database import get_db
from app.models.video import Video
from app.models.schemas import (
    HistoryResponse,
    StatusResponse,
    UploadRequest,
    VideoResponse,
)
from app.services import youtube_service, notification_service

router = APIRouter(tags=["upload"])


@router.post("/upload-video", response_model=StatusResponse)
async def upload_video(req: UploadRequest, db: AsyncSession = Depends(get_db)):
    """Upload a ready video to YouTube."""
    try:
        video = await db.get(Video, req.video_id)
        if video is None:
            raise HTTPException(status_code=404, detail=f"Video {req.video_id} not found")

        # Update privacy & schedule
        video.privacy_status = req.privacy
        if req.scheduled_at:
            from datetime import datetime
            video.scheduled_at = datetime.fromisoformat(req.scheduled_at)
        await db.flush()

        url = await youtube_service.upload_video(db, req.video_id)

        # Send success notification
        await notification_service.send_upload_success(
            video.title or video.topic, url
        )

        return StatusResponse(
            video_id=req.video_id,
            status="uploaded",
            message=f"Uploaded successfully: {url}",
        )
    except HTTPException:
        raise
    except Exception as exc:
        # Send failure notification
        video = await db.get(Video, req.video_id)
        if video:
            await notification_service.send_upload_failure(
                video.title or video.topic, str(exc)
            )
        logger.error("Upload failed: {}", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/history", response_model=HistoryResponse)
async def get_history(db: AsyncSession = Depends(get_db)):
    """Return all video records ordered by creation date (newest first)."""
    result = await db.execute(
        select(Video).order_by(Video.created_at.desc())
    )
    videos = result.scalars().all()
    return HistoryResponse(
        total=len(videos),
        videos=[VideoResponse.model_validate(v) for v in videos],
    )


@router.get("/status/{video_id}", response_model=VideoResponse)
async def get_status(video_id: int, db: AsyncSession = Depends(get_db)):
    """Return the full record for a specific video."""
    video = await db.get(Video, video_id)
    if video is None:
        raise HTTPException(status_code=404, detail=f"Video {video_id} not found")
    return VideoResponse.model_validate(video)
