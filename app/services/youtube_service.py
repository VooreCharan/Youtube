"""
YouTube upload service.
Handles OAuth 2.0 authentication and resumable video uploads via the
YouTube Data API v3.
"""

from __future__ import annotations

import pickle
import random
import time
from pathlib import Path

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.video import Video
from app.utils.helpers import json_loads_safe

# YouTube API constants
_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
_API_SERVICE_NAME = "youtube"
_API_VERSION = "v3"
_MAX_RETRIES = 5
_RETRIABLE_STATUS_CODES = [500, 502, 503, 504]

# YouTube category mapping (subset)
_CATEGORY_MAP = {
    "Film & Animation": "1",
    "Autos & Vehicles": "2",
    "Music": "10",
    "Pets & Animals": "15",
    "Sports": "17",
    "Gaming": "20",
    "People & Blogs": "22",
    "Comedy": "23",
    "Entertainment": "24",
    "News & Politics": "25",
    "Howto & Style": "26",
    "Education": "27",
    "Science & Technology": "28",
    "Technology": "28",
    "AI": "28",
    "Finance": "22",
    "Business & Finance": "22",
    "Science": "28",
    "Health": "26",
    "World News": "25",
    "Education": "27",
    "Self-Improvement": "26",
}


class YouTubeAuthRequired(RuntimeError):
    """Raised when no cached YouTube login exists and a browser login is not allowed."""


def authenticate(allow_browser: bool = True):
    """
    Authenticate with YouTube API using OAuth 2.0.

    First-time usage opens a browser for consent. Subsequent calls reuse
    a cached token from ``token.pickle``.

    Returns a YouTube API service resource.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    settings = get_settings()
    token_path = settings.token_path
    secrets_path = settings.client_secrets_file

    if not secrets_path.exists() and not token_path.exists():
        raise FileNotFoundError(
            f"OAuth client secrets file not found: {settings.youtube_client_secrets_path}. "
            "Download it from Google Cloud Console → APIs & Services → Credentials."
        )

    credentials = None

    # Load cached credentials
    if token_path.exists():
        with open(token_path, "rb") as f:
            credentials = pickle.load(f)

    # Refresh or obtain new credentials
    if credentials and credentials.expired and credentials.refresh_token:
        logger.info("Refreshing expired YouTube credentials")
        try:
            credentials.refresh(Request())
        except Exception as e:
            logger.warning("Token refresh failed ({}). Forcing re-authentication.", e)
            credentials = None

    if not credentials or not credentials.valid:
        if not allow_browser:
            raise YouTubeAuthRequired(
                "YouTube is not connected yet. On a machine with a browser run "
                "`python -m app.youtube_auth` once, then provide token.pickle to the server "
                "(file next to the app, or YOUTUBE_TOKEN_B64 — see `python -m app.export_railway_env`)."
            )
        logger.info("Starting OAuth flow for YouTube API")
        flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), _SCOPES)
        credentials = flow.run_local_server(port=0)

    # Cache credentials
    with open(token_path, "wb") as f:
        pickle.dump(credentials, f)

    service = build(_API_SERVICE_NAME, _API_VERSION, credentials=credentials)
    logger.info("YouTube API authenticated successfully")
    return service


async def upload_video(db: AsyncSession, video_id: int, allow_browser: bool = True) -> str:
    """
    Upload a video to YouTube with metadata.

    The blocking Google API calls run in a worker thread so the event loop
    (API server or Telegram bot) stays responsive during long uploads.

    Returns the YouTube video URL.
    """
    import asyncio

    video = await db.get(Video, video_id)
    if video is None:
        raise ValueError(f"Video {video_id} not found")
    if not video.video_path or not Path(video.video_path).exists():
        raise ValueError(f"Video {video_id} has no video file — generate video first")

    video.upload_status = "uploading"
    await db.flush()

    logger.info("Starting YouTube upload for Video id={}", video_id)

    try:
        body = _build_upload_body(video)
        thumbnail = video.thumbnail_path if video.thumbnail_path and Path(video.thumbnail_path).exists() else None
        yt_video_id = await asyncio.to_thread(
            _upload_blocking, body, video.video_path, thumbnail, allow_browser
        )
        yt_url = f"https://www.youtube.com/watch?v={yt_video_id}"

        video.youtube_video_id = yt_video_id
        video.youtube_url = yt_url
        video.upload_status = "uploaded"
        video.error_message = None
        await db.flush()

        logger.info("Upload complete: {} → {}", video_id, yt_url)
        return yt_url

    except Exception as exc:
        video.upload_status = "failed"
        video.error_message = str(exc)[:2000]
        video.retry_count = (video.retry_count or 0) + 1
        await db.flush()
        logger.error("Upload failed for Video id={}: {}", video_id, exc)
        raise


def _build_upload_body(video: Video) -> dict:
    tags = json_loads_safe(video.tags, [])
    if isinstance(tags, str):
        tags = [tags]
    hashtags = json_loads_safe(video.hashtags, [])

    description = video.description or ""
    if video.chapters:
        description += f"\n\n📋 Chapters:\n{video.chapters}"
    if hashtags:
        description += "\n\n" + " ".join(hashtags)

    body = {
        "snippet": {
            "title": (video.title or video.topic)[:100],
            "description": description[:5000],
            "tags": tags[:500] if tags else [],
            "categoryId": _CATEGORY_MAP.get(video.category, "22"),
        },
        "status": {
            "privacyStatus": video.privacy_status or "private",
            "selfDeclaredMadeForKids": False,
        },
    }
    if video.language:
        body["snippet"]["defaultLanguage"] = video.language
        body["snippet"]["defaultAudioLanguage"] = video.language

    # Scheduled publishing
    if video.scheduled_at and video.privacy_status == "private":
        body["status"]["publishAt"] = video.scheduled_at.isoformat() + "Z"
    return body


def _upload_blocking(body: dict, video_path: str, thumbnail_path: str | None, allow_browser: bool) -> str:
    """Resumable upload with exponential backoff. Returns the YouTube video id."""
    from googleapiclient.http import MediaFileUpload
    from googleapiclient.errors import HttpError

    youtube = authenticate(allow_browser=allow_browser)

    media = MediaFileUpload(
        video_path,
        mimetype="video/mp4",
        resumable=True,
        chunksize=1024 * 1024 * 10,  # 10 MB chunks
    )
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    retry = 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                logger.info("Upload progress: {}%", int(status.progress() * 100))
        except HttpError as e:
            if e.resp.status in _RETRIABLE_STATUS_CODES and retry < _MAX_RETRIES:
                retry += 1
                wait = 2 ** retry + random.random()
                logger.warning(
                    "Retriable upload error (attempt {}/{}), waiting {:.1f}s: {}",
                    retry, _MAX_RETRIES, wait, e,
                )
                time.sleep(wait)
            else:
                raise

    yt_video_id = response["id"]

    if thumbnail_path:
        try:
            set_thumbnail(youtube, yt_video_id, thumbnail_path)
        except Exception as exc:
            logger.warning("Thumbnail upload failed (non-fatal): {}", exc)

    return yt_video_id


def set_thumbnail(youtube_service, youtube_video_id: str, thumbnail_path: str) -> None:
    """Upload a custom thumbnail for a YouTube video."""
    from googleapiclient.http import MediaFileUpload

    media = MediaFileUpload(thumbnail_path, mimetype="image/jpeg")
    youtube_service.thumbnails().set(
        videoId=youtube_video_id,
        media_body=media,
    ).execute()
    logger.info("Custom thumbnail set for {}", youtube_video_id)
