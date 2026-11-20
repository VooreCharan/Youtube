"""
Generation router — endpoints for each step of the content-creation pipeline.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.database import get_db
from app.models.schemas import (
    AudioRequest,
    FullPipelineRequest,
    ImageRequest,
    ScriptRequest,
    StatusResponse,
    SubtitleRequest,
    ThumbnailRequest,
    TopicRequest,
    VideoRequest,
)
from app.services import (
    topic_service,
    script_service,
    tts_service,
    image_service,
    subtitle_service,
    video_service,
    thumbnail_service,
    notification_service,
)

router = APIRouter(tags=["generation"])


@router.post("/generate-topic", response_model=StatusResponse)
async def generate_topic(req: TopicRequest, db: AsyncSession = Depends(get_db)):
    """Create a new video record with a topic (user-provided or AI-generated)."""
    try:
        video = await topic_service.generate_topic(
            db, category=req.category, user_topic=req.topic
        )
        return StatusResponse(
            video_id=video.id,
            status="success",
            message=f"Topic created: {video.topic}",
        )
    except Exception as exc:
        logger.error("Topic generation failed: {}", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/generate-script", response_model=StatusResponse)
async def generate_script(req: ScriptRequest, db: AsyncSession = Depends(get_db)):
    """Generate a full script with metadata for an existing video."""
    try:
        video = await script_service.generate_script(db, req.video_id, voice=req.voice)
        return StatusResponse(
            video_id=video.id,
            status="success",
            message=f"Script generated: {video.title}",
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.error("Script generation failed: {}", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/generate-audio", response_model=StatusResponse)
async def generate_audio(req: AudioRequest, db: AsyncSession = Depends(get_db)):
    """Generate TTS narration audio from the video's script."""
    try:
        path = await tts_service.generate_audio(db, req.video_id, voice=req.voice)
        return StatusResponse(
            video_id=req.video_id,
            status="success",
            message=f"Audio generated: {path}",
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.error("Audio generation failed: {}", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/generate-images", response_model=StatusResponse)
async def generate_images(req: ImageRequest, db: AsyncSession = Depends(get_db)):
    """Generate scene images (Pexels or gradient fallback)."""
    try:
        paths = await image_service.generate_images(db, req.video_id)
        return StatusResponse(
            video_id=req.video_id,
            status="success",
            message=f"Generated {len(paths)} images",
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.error("Image generation failed: {}", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/generate-subtitles", response_model=StatusResponse)
async def generate_subtitles(req: SubtitleRequest, db: AsyncSession = Depends(get_db)):
    """Generate SRT subtitles from the audio track."""
    try:
        path = await subtitle_service.generate_subtitles(db, req.video_id)
        return StatusResponse(
            video_id=req.video_id,
            status="success",
            message=f"Subtitles generated: {path}",
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.error("Subtitle generation failed: {}", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/generate-video", response_model=StatusResponse)
async def generate_video(req: VideoRequest, db: AsyncSession = Depends(get_db)):
    """Assemble the final MP4 video from images, audio, and subtitles."""
    try:
        path = await video_service.create_video(
            db, req.video_id, video_format=req.format
        )
        return StatusResponse(
            video_id=req.video_id,
            status="success",
            message=f"Video assembled: {path}",
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.error("Video generation failed: {}", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/generate-thumbnail", response_model=StatusResponse)
async def generate_thumbnail(req: ThumbnailRequest, db: AsyncSession = Depends(get_db)):
    """Generate a YouTube thumbnail for the video."""
    try:
        path = await thumbnail_service.generate_thumbnail(db, req.video_id)
        return StatusResponse(
            video_id=req.video_id,
            status="success",
            message=f"Thumbnail generated: {path}",
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.error("Thumbnail generation failed: {}", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/generate-all", response_model=StatusResponse)
async def generate_all(
    req: FullPipelineRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """
    Kick off the full generation pipeline in the background.

    Returns HTTP 202 immediately with a video_id.
    Poll GET /history to watch status change: generating → ready.
    """
    # Step 1 (topic) runs synchronously so we can return the video_id right away
    try:
        video = await topic_service.generate_topic(
            db,
            category=req.category,
            user_topic=req.topic,
            voice=req.voice,
            video_format=req.format,
        )
        vid = video.id
        video_class = video.__class__
        logger.info("Pipeline step 1/7 complete: topic (id={})", vid)
    except Exception as exc:
        logger.error("Topic generation failed: {}", exc)
        raise HTTPException(status_code=500, detail=str(exc))

    async def _run_pipeline():
        """Run steps 2-7 in the background."""
        # Each service opens its own DB session via the engine directly
        from app.database import async_session_factory as AsyncSessionLocal
        async with AsyncSessionLocal() as bg_db:
            try:
                # Step 2: Script
                await script_service.generate_script(bg_db, vid)
                await bg_db.commit()
                logger.info("Pipeline step 2/7 complete: script")

                # Step 3: Audio
                await tts_service.generate_audio(bg_db, vid, voice=req.voice)
                await bg_db.commit()
                logger.info("Pipeline step 3/7 complete: audio")

                # Step 4: Images
                await image_service.generate_images(bg_db, vid)
                await bg_db.commit()
                logger.info("Pipeline step 4/7 complete: images")

                # Step 5: Subtitles
                await subtitle_service.generate_subtitles(bg_db, vid)
                await bg_db.commit()
                logger.info("Pipeline step 5/7 complete: subtitles")

                # Step 6: Video (CPU-heavy — runs in thread pool inside service)
                await video_service.create_video(bg_db, vid, video_format=req.format)
                await bg_db.commit()
                logger.info("Pipeline step 6/7 complete: video")

                # Step 7: Thumbnail
                await thumbnail_service.generate_thumbnail(bg_db, vid)
                await bg_db.commit()
                logger.info("Pipeline step 7/7 complete: thumbnail")

                # Notify
                refreshed = await bg_db.get(video_class, vid)
                await notification_service.send_generation_complete(
                    refreshed.title or refreshed.topic, vid
                )
                logger.info("Full pipeline complete for video id={}", vid)

            except Exception as exc:
                logger.error("Background pipeline failed for id={}: {}", vid, exc)
                async with AsyncSessionLocal() as err_db:
                    v = await err_db.get(video_class, vid)
                    if v:
                        v.upload_status = "failed"
                        v.error_message = str(exc)
                        await err_db.commit()

    background_tasks.add_task(_run_pipeline)

    return StatusResponse(
        video_id=vid,
        status="generating",
        message=(
            f"Pipeline started for video id={vid}. "
            "Steps 2-7 are running in the background — poll /history to track progress."
        ),
    )


@router.get("/voices")
async def list_voices():
    """Return all available TTS voices."""
    voices = await tts_service.list_voices()
    return {"voices": voices}
