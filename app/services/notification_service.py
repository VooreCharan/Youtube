"""
Notification service — sends upload status alerts via Telegram.
"""

from __future__ import annotations

import httpx
from loguru import logger

from app.config import get_settings


async def send_notification(
    title: str,
    message: str,
    youtube_url: str | None = None,
) -> bool:
    """
    Send a Telegram notification about a video event.

    Returns True if the notification was sent successfully, False otherwise.
    Gracefully handles missing configuration — logs a warning instead of crashing.
    """
    settings = get_settings()

    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.debug("Telegram not configured — skipping notification")
        return False

    # Build the message
    text_parts = [
        f"🎬 *YouTube Automation*",
        f"",
        f"📌 *{title}*",
        f"",
        message,
    ]
    if youtube_url:
        text_parts.append(f"")
        text_parts.append(f"🔗 [Watch on YouTube]({youtube_url})")

    text = "\n".join(text_parts)

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    payload = {
        "chat_id": settings.telegram_chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()

        logger.info("Telegram notification sent: {}", title)
        return True

    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Telegram API error {}: {}",
            exc.response.status_code,
            exc.response.text[:200],
        )
        return False
    except Exception as exc:
        logger.warning("Telegram notification failed: {}", exc)
        return False


async def send_upload_success(video_title: str, youtube_url: str) -> bool:
    """Convenience: notify that a video was uploaded successfully."""
    return await send_notification(
        title="Upload Successful ✅",
        message=f"*{video_title}* has been uploaded to YouTube!",
        youtube_url=youtube_url,
    )


async def send_upload_failure(video_title: str, error: str) -> bool:
    """Convenience: notify that an upload failed."""
    return await send_notification(
        title="Upload Failed ❌",
        message=f"*{video_title}* upload failed.\n\nError: `{error[:500]}`",
    )


async def send_generation_complete(video_title: str, video_id: int) -> bool:
    """Convenience: notify that video generation is complete."""
    return await send_notification(
        title="Video Ready 🎥",
        message=f"*{video_title}* (ID: {video_id}) is ready for upload.",
    )
