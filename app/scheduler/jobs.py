"""
APScheduler jobs for automated video generation and uploading.

Jobs:
  • daily_generate  — generate a new video at GENERATION_HOUR
  • daily_upload    — upload any 'ready' videos at UPLOAD_HOUR
  • retry_failed    — retry failed uploads every 30 minutes (max 3 attempts)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger
from sqlalchemy import select

from app.config import get_settings
from app.database import async_session_factory
from app.models.video import Video

_scheduler: AsyncIOScheduler | None = None


def start_scheduler() -> None:
    """Create and start the background scheduler."""
    global _scheduler
    settings = get_settings()

    _scheduler = AsyncIOScheduler(timezone="UTC")

    # Daily video generation
    _scheduler.add_job(
        daily_generate_job,
        CronTrigger(hour=settings.generation_hour, minute=0),
        id="daily_generate",
        name="Daily Video Generation",
        replace_existing=True,
    )

    # Daily upload of ready videos
    _scheduler.add_job(
        daily_upload_job,
        CronTrigger(hour=settings.upload_hour, minute=0),
        id="daily_upload",
        name="Daily Video Upload",
        replace_existing=True,
    )

    # Retry failed uploads every 30 minutes
    _scheduler.add_job(
        retry_failed_job,
        IntervalTrigger(minutes=30),
        id="retry_failed",
        name="Retry Failed Uploads",
        replace_existing=True,
    )

    _scheduler.start()
    logger.info(
        "Scheduler started — generate@{}:00 UTC, upload@{}:00 UTC, retry every 30m",
        settings.generation_hour,
        settings.upload_hour,
    )


def shutdown_scheduler() -> None:
    """Gracefully shut down the scheduler."""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler shut down")


# ── Job implementations ─────────────────────────────────────────────

async def daily_generate_job() -> None:
    """Generate a new video using the full pipeline."""
    logger.info("⏰ Scheduled job: daily_generate started")
    try:
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

        async with async_session_factory() as db:
            # Pick a random category
            import random
            categories = [
                "Technology", "AI", "Science", "Finance",
                "Education", "Productivity", "Health",
            ]
            category = random.choice(categories)

            video = await topic_service.generate_topic(db, category=category)
            vid = video.id

            await script_service.generate_script(db, vid)
            await tts_service.generate_audio(db, vid)
            await image_service.generate_images(db, vid)
            await subtitle_service.generate_subtitles(db, vid)
            await video_service.create_video(db, vid)
            await thumbnail_service.generate_thumbnail(db, vid)

            await db.commit()

            refreshed = await db.get(Video, vid)
            await notification_service.send_generation_complete(
                refreshed.title or refreshed.topic, vid
            )

        logger.info("✅ daily_generate complete (video id={})", vid)

    except Exception as exc:
        logger.error("❌ daily_generate failed: {}", exc)


async def daily_upload_job() -> None:
    """Upload all videos with status='ready'."""
    logger.info("⏰ Scheduled job: daily_upload started")
    try:
        from app.services import youtube_service, notification_service

        async with async_session_factory() as db:
            result = await db.execute(
                select(Video).where(Video.upload_status == "ready")
            )
            ready_videos = result.scalars().all()

            if not ready_videos:
                logger.info("No ready videos to upload")
                return

            for video in ready_videos:
                try:
                    url = await youtube_service.upload_video(db, video.id)
                    await notification_service.send_upload_success(
                        video.title or video.topic, url
                    )
                    await db.commit()
                except Exception as exc:
                    logger.error("Upload failed for video {}: {}", video.id, exc)
                    await notification_service.send_upload_failure(
                        video.title or video.topic, str(exc)
                    )
                    await db.rollback()

        logger.info("✅ daily_upload complete")

    except Exception as exc:
        logger.error("❌ daily_upload failed: {}", exc)


async def retry_failed_job() -> None:
    """Retry uploads for videos that failed (max 3 retries)."""
    logger.info("⏰ Scheduled job: retry_failed started")
    try:
        from app.services import youtube_service, notification_service

        async with async_session_factory() as db:
            result = await db.execute(
                select(Video).where(
                    Video.upload_status == "failed",
                    Video.retry_count < 3,
                    Video.video_path.isnot(None),
                )
            )
            failed_videos = result.scalars().all()

            if not failed_videos:
                logger.debug("No failed videos to retry")
                return

            for video in failed_videos:
                try:
                    logger.info(
                        "Retrying upload for video {} (attempt {})",
                        video.id,
                        video.retry_count + 1,
                    )
                    url = await youtube_service.upload_video(db, video.id)
                    await notification_service.send_upload_success(
                        video.title or video.topic, url
                    )
                    await db.commit()
                except Exception as exc:
                    logger.error("Retry failed for video {}: {}", video.id, exc)
                    await db.rollback()

        logger.info("✅ retry_failed complete")

    except Exception as exc:
        logger.error("❌ retry_failed failed: {}", exc)
