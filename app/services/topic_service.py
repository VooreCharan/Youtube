"""
Topic generation service.
Generates or accepts a video topic and creates the initial DB record.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.config import get_settings
from app.models.video import Video
from app.services.llm_service import call_llm


async def generate_topic(
    db: AsyncSession,
    category: str,
    user_topic: str | None = None,
    voice: str | None = None,
    video_format: str | None = None,
    language: str | None = None,
    source: str = "api",
    telegram_chat_id: str | None = None,
) -> Video:
    """
    Create a new Video record with a topic.

    If *user_topic* is provided it is used directly; otherwise the LLM
    generates a trending topic for the given *category*.
    """
    settings = get_settings()

    if user_topic:
        topic = user_topic.strip()
        logger.info("Using user-provided topic: {}", topic)
    else:
        prompt = (
            f"Generate ONE unique, trending, and highly engaging YouTube video topic "
            f"for the category '{category}'. "
            f"The topic should be specific enough to create a 5-10 minute video. "
            f"Reply with ONLY the topic text, no numbering, no quotes, no explanation."
        )
        topic = (await call_llm(prompt)).strip().strip('"').strip("'")
        logger.info("LLM generated topic: {}", topic)

    video = Video(
        topic=topic,
        category=category,
        upload_status="pending",
        voice=voice or settings.default_voice,
        video_format=video_format or settings.default_video_format,
        language=language,
        source=source,
        telegram_chat_id=telegram_chat_id,
    )
    db.add(video)
    await db.flush()
    await db.refresh(video)

    logger.info("Created Video id={} — topic='{}'", video.id, topic)
    return video
