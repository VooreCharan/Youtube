"""
Pipeline facade used by the Telegram bot.

Every function opens its own DB session and commits, so a bot restart never
leaves half-written state. Handlers never touch the services directly.
"""

from __future__ import annotations

import asyncio
import random
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

from loguru import logger

from app.config import get_settings
from app.database import async_session_factory
from app.models.video import Video
from app.services import (
    captions_service,
    music_service,
    image_service,
    script_service,
    subtitle_service,
    thumbnail_service,
    topic_service,
    tts_service,
    video_service,
    youtube_service,
)
from app.services.languages import Language, Voice, get_language

# Telegram bots may send files up to 50 MB.
TELEGRAM_UPLOAD_LIMIT = 49 * 1024 * 1024
# …and download files up to 20 MB.
TELEGRAM_DOWNLOAD_LIMIT = 20 * 1024 * 1024

SHORT = -1  # create_video(target=SHORT) → vertical Short under one minute

STATUS_AWAITING_APPROVAL = "awaiting_approval"
STATUS_CANCELLED = "cancelled"


async def get_video(video_id: int) -> Video:
    async with async_session_factory() as db:
        video = await db.get(Video, video_id)
        if video is None:
            raise ValueError(f"Video {video_id} not found")
        return video


async def create_video(
    *,
    topic: str,
    category: str,
    language: Language,
    source: str,
    chat_id: int,
    target: Optional[int] = None,
    topic_en: Optional[str] = None,
) -> int:
    """*target*: seconds for long videos, ``SHORT`` for a vertical Short, None = default length."""
    short = target == SHORT
    async with async_session_factory() as db:
        video = await topic_service.generate_topic(
            db,
            category=category,
            user_topic=topic,
            voice=language.voices[0].id if language.voices else None,
            video_format="vertical" if short else "horizontal",
            language=language.code,
            source=source,
            telegram_chat_id=str(chat_id),
        )
        video.target_seconds = script_service.SHORT_MAX_SECONDS if short else target
        video.topic_en = topic_en or None
        await db.commit()
        return video.id


async def make_long(video_id: int) -> Video:
    """Turn a Short into a regular horizontal video (keeps the script)."""
    async with async_session_factory() as db:
        video = await db.get(Video, video_id)
        video.video_format = "horizontal"
        if video.target_seconds and video.target_seconds <= 60:
            video.target_seconds = None
        video.image_paths = None  # landscape footage needed
        await db.commit()
        return video


# ── Script ──────────────────────────────────────────────────────────

async def write_script(video_id: int, regenerate: bool = False) -> Video:
    async with async_session_factory() as db:
        video = await script_service.generate_script(db, video_id, avoid_previous=regenerate)
        await db.commit()
        return video


async def replace_script_text(video_id: int, text: str) -> Video:
    """User edited the AI script: keep metadata, swap the narration."""
    async with async_session_factory() as db:
        video = await db.get(Video, video_id)
        video.script = text.strip()
        video.audio_path = None
        video.subtitle_path = None
        captions_service.clear_timing(video_id)
        await db.flush()
        video = await _check_facts(db, video_id) or video
        await db.commit()
        await db.refresh(video)
        return video


async def use_own_script(video_id: int, text: str, language: Language) -> Video:
    async with async_session_factory() as db:
        video = await script_service.apply_user_script(db, video_id, text, language.name)
        video = await _check_facts(db, video_id) or video
        await db.commit()
        return video


async def _check_facts(db, video_id: int) -> Optional[Video]:
    """Fact-check a user-written script (report only). Never blocks the flow."""
    try:
        return await script_service.check_script(db, video_id)
    except Exception as exc:  # noqa: BLE001 — a failed check must not lose the user's script
        logger.warning("Fact-check of user script failed for video {}: {}", video_id, exc)
        return None


# ── Voice ───────────────────────────────────────────────────────────

async def voice_samples(video_id: int) -> List[Tuple[Voice, Path]]:
    video = await get_video(video_id)
    language = get_language(video.language)
    narration = tts_service.narration_from_script(video.script or "")
    text = tts_service.sample_text(narration) or video.title or video.topic
    return await tts_service.generate_voice_samples(text, language)


async def narrate(video_id: int, voice_id: str) -> Video:
    """Record the voice-over. Shorts are kept under a minute (raises TooLongForShort)."""
    async with async_session_factory() as db:
        video = await db.get(Video, video_id)
        short = script_service.is_short(video)
        await tts_service.generate_audio(
            db, video_id, voice=voice_id,
            max_seconds=script_service.SHORT_MAX_SECONDS if short else None,
        )
        video = await db.get(Video, video_id)
        if not short:
            duration = await asyncio.to_thread(video_service._get_audio_duration, Path(video.audio_path))
            video.chapters = script_service.compute_chapters(video.script, duration)
        await db.commit()
        return video


# ── User-provided audio ─────────────────────────────────────────────

async def use_own_audio(video_id: int, audio_file: Path) -> Video:
    """
    Store an uploaded narration: detect its language, build timed English
    captions, and generate the title/description/tags from what is said.
    """
    async with async_session_factory() as db:
        video = await db.get(Video, video_id)

        segments, lang_code = await subtitle_service.transcribe(audio_file)
        transcript = " ".join(s.text.strip() for s in segments).strip()
        language = get_language(lang_code)

        english = None
        if language.code != "en":
            english = await subtitle_service.translate_speech_to_english(audio_file)

        if english:
            # Fix speech-translation slips now, so the title/description are built from clean text.
            english = await captions_service.polish_english(
                [captions_service.Segment(s.start, s.end, s.text) for s in english], language.code
            )
            captions_service.save_timing(video_id, english, language="en")
            # The speech translation is complete even when the transcript is not
            # (some languages get cut short), so metadata is written from it.
            content = " ".join(s.text for s in english)
        else:
            captions_service.save_timing(
                video_id,
                [captions_service.Segment(s.start, s.end, s.text.strip()) for s in segments if s.text.strip()],
            )
            content = transcript

        if not content:
            raise RuntimeError("I couldn't hear any speech in that audio file.")

        video.language = language.code
        video.audio_path = str(audio_file)
        video.subtitle_path = None
        if script_service.is_short(video):
            duration = await asyncio.to_thread(video_service._get_audio_duration, audio_file)
            if duration > script_service.SHORT_MAX_SECONDS + 0.5:
                # Too long for a Short: make a regular video instead (handler tells the user).
                video.video_format = "horizontal"
                video.target_seconds = None
        await db.flush()

        video = await script_service.apply_user_script(db, video_id, content, language.name)
        await db.commit()
        return video


# ── Video ───────────────────────────────────────────────────────────

async def build_video(video_id: int, fresh_visuals: bool = False) -> Video:
    """Fetch visuals, subtitles, render the MP4 and thumbnail. Leaves the video awaiting approval."""
    async with async_session_factory() as db:
        video = await db.get(Video, video_id)
        if not video.image_paths or fresh_visuals:
            await image_service.generate_images(db, video_id)
        if not video.subtitle_path or not Path(video.subtitle_path).exists():
            await subtitle_service.generate_subtitles(db, video_id)
        await _ensure_music(video)
        await video_service.create_video(db, video_id, music_path=_music_path(video))
        await thumbnail_service.generate_thumbnail(db, video_id)

        video = await db.get(Video, video_id)
        # create_video marks it "ready"; the bot needs an explicit human approval
        # first, and "ready" would let the optional scheduler auto-upload it.
        video.upload_status = STATUS_AWAITING_APPROVAL
        await db.commit()
        return video


async def _ensure_music(video: Video) -> None:
    """Pick a track by mood the first time a video is built (if music is enabled)."""
    if video.music is not None:
        return
    if not get_settings().enable_music:
        video.music = music_service.NO_MUSIC
        return
    mood = await music_service.choose_mood(video.title or video.topic, video.category, video.script or "")
    track = await music_service.pick(mood)
    video.music = track.key if track else music_service.NO_MUSIC
    logger.info("Music for video {}: {}", video.id, music_service.describe(video.music))


def _music_path(video: Video) -> Optional[Path]:
    track = music_service.resolve(video.music)
    return track.path if track else None


async def change_music(video_id: int, choice: str) -> Tuple[Video, Optional[Path]]:
    """
    *choice*: "next" (another track — same mood first, then other moods),
    "none" (remove music) or "add" (pick music again after removing it).
    Re-renders the video (visuals and captions are kept).
    """
    async with async_session_factory() as db:
        video = await db.get(Video, video_id)
        if choice == "none":
            video.music = music_service.NO_MUSIC
        elif choice == "add" or video.music in (None, music_service.NO_MUSIC):
            video.music = None
            await _ensure_music(video)
        else:
            current = music_service.resolve(video.music)
            mood = current.mood if current else "inspiring"
            track = None
            for candidate_mood in [mood] + music_service.all_moods_cycle(mood):
                tracks = await music_service.tracks_for(candidate_mood)
                others = [t for t in tracks if t.key != video.music]
                if others:
                    track = random.choice(others)
                    break
            video.music = track.key if track else video.music
        await video_service.create_video(db, video_id, music_path=_music_path(video))
        video = await db.get(Video, video_id)
        video.upload_status = STATUS_AWAITING_APPROVAL
        await db.commit()
    return video, await telegram_preview(video)


async def telegram_preview(video: Video) -> Optional[Path]:
    """Return a file small enough to send via Telegram (re-encodes if needed), or None."""
    path = Path(video.video_path)
    if path.stat().st_size <= TELEGRAM_UPLOAD_LIMIT:
        return path

    settings = get_settings()
    preview = settings.previews_dir / f"{video.id}_preview.mp4"
    for height, crf in ((720, 30), (480, 32), (360, 34)):
        cmd = [
            video_service._FFMPEG_EXE, "-y", "-i", str(path),
            "-vf", f"scale=-2:{height}", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", str(crf), "-c:a", "aac", "-b:a", "96k", str(preview),
        ]
        await asyncio.to_thread(subprocess.run, cmd, capture_output=True, check=False)
        if preview.exists() and preview.stat().st_size <= TELEGRAM_UPLOAD_LIMIT:
            logger.info("Preview for video {} encoded at {}p", video.id, height)
            return preview
    return None


# ── Publish / cancel ────────────────────────────────────────────────

async def publish(video_id: int) -> str:
    settings = get_settings()
    async with async_session_factory() as db:
        video = await db.get(Video, video_id)
        video.privacy_status = settings.youtube_privacy
        try:
            url = await youtube_service.upload_video(db, video_id, allow_browser=False)
        finally:
            await db.commit()  # persist "uploaded" or the failure reason
        return url


async def cancel(video_id: Optional[int]) -> None:
    if not video_id:
        return
    async with async_session_factory() as db:
        video = await db.get(Video, video_id)
        if video and video.upload_status not in ("uploaded", "uploading"):
            video.upload_status = STATUS_CANCELLED
            await db.commit()


def save_upload_path(video_id: int, original_name: str) -> Path:
    suffix = Path(original_name or "audio.mp3").suffix.lower() or ".mp3"
    if suffix not in (".mp3", ".m4a", ".ogg", ".oga", ".opus", ".wav", ".aac", ".flac", ".webm", ".mp4"):
        suffix = ".mp3"
    return get_settings().uploads_dir / f"{video_id}_narration{suffix}"


def ffmpeg_available() -> bool:
    exe = video_service._FFMPEG_EXE
    return Path(exe).exists() or shutil.which(exe) is not None
