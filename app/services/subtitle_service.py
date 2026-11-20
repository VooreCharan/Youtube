"""
Subtitle generation service using faster-whisper.
Transcribes audio to SRT format.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

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

    def _transcribe():
        from faster_whisper import WhisperModel
        model = WhisperModel(
            settings.whisper_model_size,
            device="cpu",
            compute_type="int8",
        )
        segs, info = model.transcribe(
            str(audio_path),
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
        )
        return list(segs)  # materialise generator before leaving thread

    try:
        segments = await asyncio.to_thread(_transcribe)

        srt_lines: list[str] = []
        for idx, segment in enumerate(segments, start=1):
            start_ts = format_srt_timestamp(segment.start)
            end_ts = format_srt_timestamp(segment.end)
            text = segment.text.strip()
            srt_lines.append(f"{idx}")
            srt_lines.append(f"{start_ts} --> {end_ts}")
            srt_lines.append(text)
            srt_lines.append("")  # blank separator

        srt_content = "\n".join(srt_lines)

    except ImportError:
        logger.warning("faster-whisper not available — generating simple subtitles from script")
        srt_content = _generate_simple_srt(video)

    output_path.write_text(srt_content, encoding="utf-8")

    video.subtitle_path = str(output_path)
    await db.flush()

    logger.info("Subtitles saved: {} ({} bytes)", output_path, output_path.stat().st_size)
    return str(output_path)


def _generate_simple_srt(video: Video) -> str:
    """
    Fallback: create basic SRT from the script text when whisper is unavailable.
    Splits script into sentences and assigns approximate timestamps.
    """
    if not video.script:
        return ""

    import re
    sentences = re.split(r'(?<=[.!?])\s+', video.script)
    # Remove scene headers
    sentences = [
        s for s in sentences
        if not s.strip().lower().startswith("scene ") or ":" not in s
    ]

    srt_lines: list[str] = []
    time_per_sentence = 4.0  # rough average seconds per sentence
    current_time = 0.0

    for idx, sentence in enumerate(sentences, start=1):
        sentence = sentence.strip()
        if not sentence:
            continue
        start_ts = format_srt_timestamp(current_time)
        end_time = current_time + time_per_sentence
        end_ts = format_srt_timestamp(end_time)

        srt_lines.append(str(idx))
        srt_lines.append(f"{start_ts} --> {end_ts}")
        srt_lines.append(sentence)
        srt_lines.append("")

        current_time = end_time

    return "\n".join(srt_lines)
