"""
Subtitle generation service using faster-whisper.
Transcribes audio to SRT format.
"""

from __future__ import annotations

import asyncio
from functools import lru_cache
from pathlib import Path
from typing import Tuple

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.video import Video
from app.utils.helpers import format_srt_timestamp


async def generate_subtitles(db: AsyncSession, video_id: int) -> str:
    """
    Transcribe the audio for *video_id* and produce an SRT subtitle file.

    Returns the path to the saved .srt file.
    """
    settings = get_settings()

    video = await db.get(Video, video_id)
    if video is None:
        raise ValueError(f"Video {video_id} not found")
    if not video.audio_path:
        raise ValueError(f"Video {video_id} has no audio — generate audio first")

    audio_path = Path(video.audio_path)
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    output_path = settings.subtitles_dir / f"{video_id}.srt"

    logger.info(
        "Generating subtitles for Video id={} (model={})",
        video_id,
        settings.whisper_model_size,
    )

    try:
        segments, _language = await transcribe(audio_path)
        srt_content = segments_to_srt(segments)

    except ImportError:
        logger.warning("faster-whisper not available — generating simple subtitles from script")
        srt_content = _generate_simple_srt(video)

    output_path.write_text(srt_content, encoding="utf-8")

    video.subtitle_path = str(output_path)
    await db.flush()

    logger.info("Subtitles saved: {} ({} bytes)", output_path, output_path.stat().st_size)
    return str(output_path)


@lru_cache(maxsize=1)
def _whisper_model():
    from faster_whisper import WhisperModel

    settings = get_settings()
    logger.info("Loading Whisper model '{}' (first use only)", settings.whisper_model_size)
    return WhisperModel(settings.whisper_model_size, device="cpu", compute_type="int8")


async def transcribe(audio_path: Path) -> Tuple[list, str]:
    """
    Transcribe *audio_path* with faster-whisper.

    Returns ``(segments, detected_language_code)``. Raises ImportError if
    faster-whisper is not installed.
    """

    def _run():
        model = _whisper_model()
        segs, info = model.transcribe(
            str(audio_path),
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
        )
        return list(segs), info.language  # materialise generator inside the thread

    return await asyncio.to_thread(_run)


def segments_to_srt(segments: list) -> str:
    srt_lines: list[str] = []
    for idx, segment in enumerate(segments, start=1):
        srt_lines.append(str(idx))
        srt_lines.append(f"{format_srt_timestamp(segment.start)} --> {format_srt_timestamp(segment.end)}")
        srt_lines.append(segment.text.strip())
        srt_lines.append("")
    return "\n".join(srt_lines)


def _generate_simple_srt(video: Video) -> str:
    """
    Fallback when faster-whisper is unavailable: split the narration into
    sentences and spread them over the real audio duration, proportionally
    to their length.
    """
    import re

    from app.services.tts_service import narration_from_script
    from app.services.video_service import _get_audio_duration

    narration = narration_from_script(video.script or "")
    sentences = [s.strip() for s in re.split(r"(?<=[.!?।。])\s+", narration) if s.strip()]
    if not sentences:
        return ""

    try:
        total = _get_audio_duration(Path(video.audio_path))
    except Exception:  # noqa: BLE001
        total = 4.0 * len(sentences)
    total_chars = sum(len(s) for s in sentences)

    srt_lines: list[str] = []
    current = 0.0
    for idx, sentence in enumerate(sentences, start=1):
        end = current + total * len(sentence) / total_chars
        srt_lines += [str(idx), f"{format_srt_timestamp(current)} --> {format_srt_timestamp(end)}", sentence, ""]
        current = end
    return "\n".join(srt_lines)
