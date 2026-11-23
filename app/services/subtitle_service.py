"""
Subtitle generation service using faster-whisper.
Transcribes audio to SRT format.
"""

from __future__ import annotations

import asyncio
from functools import lru_cache
from pathlib import Path
from typing import Optional, Tuple

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.video import Video


async def generate_subtitles(db: AsyncSession, video_id: int) -> str:
    """
    Produce an English SRT for *video_id*, whatever the narration language.

    Timed lines come from the voice-over's saved sentence timings, or from a
    Whisper transcription; they are then translated to English and split into
    short caption chunks. Returns the path to the saved .srt file.
    """
    from app.services import captions_service
    from app.services.tts_service import narration_from_script
    from app.services.video_service import _get_audio_duration

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

    timing = captions_service.load_timing(video_id)
    text_language = None
    segments = None
    if timing:
        segments, text_language = timing
        logger.info("Captions for Video id={} from saved timings ({} lines)", video_id, len(segments))
    if not segments:
        try:
            raw, _language = await transcribe(audio_path)
            segments = [captions_service.Segment(s.start, s.end, s.text.strip()) for s in raw if s.text.strip()]
            logger.info("Captions for Video id={} from Whisper ({} lines)", video_id, len(segments))
        except ImportError:
            logger.warning("faster-whisper not available — timing captions proportionally from the script")
            duration = await asyncio.to_thread(_get_audio_duration, audio_path)
            segments = captions_service.proportional_segments(narration_from_script(video.script or ""), duration)

    if text_language == "en":
        pass  # already English (speech translation, polished when the audio was uploaded)
    else:
        segments = await captions_service.translate_to_english(segments, video.language)
    segments = captions_service.split_long(segments)
    output_path.write_text(captions_service.to_srt(segments), encoding="utf-8")

    video.subtitle_path = str(output_path)
    await db.flush()

    logger.info("English subtitles saved: {} ({} captions)", output_path, len(segments))
    return str(output_path)


@lru_cache(maxsize=1)
def _whisper_model():
    from faster_whisper import WhisperModel

    settings = get_settings()
    logger.info("Loading Whisper model '{}' (first use only)", settings.whisper_model_size)
    return WhisperModel(settings.whisper_model_size, device="cpu", compute_type="int8")


async def transcribe(audio_path: Path) -> Tuple[list, str]:
    """
    Transcribe *audio_path* with timestamps.

    Uses Groq's hosted Whisper large-v3 when GROQ_API_KEY is set (far more
    accurate for Indian languages, free), otherwise the local faster-whisper
    model. Returns ``(segments, language_code)``; each segment has
    ``.start``, ``.end`` and ``.text``. Raises ImportError if no engine is available.
    """
    settings = get_settings()
    if settings.groq_api_key and settings.groq_whisper_model:
        try:
            return await _transcribe_groq(audio_path, settings)
        except Exception as exc:  # noqa: BLE001 — fall back to the local model
            logger.warning("Groq transcription failed ({}), using local Whisper", exc)
    return await _transcribe_local(audio_path)


async def _transcribe_groq(audio_path: Path, settings) -> Tuple[list, str]:
    from types import SimpleNamespace

    from openai import AsyncOpenAI

    from app.services.languages import LANGUAGES

    client = AsyncOpenAI(base_url="https://api.groq.com/openai/v1", api_key=settings.groq_api_key, max_retries=1)
    with open(audio_path, "rb") as fh:
        result = await client.audio.transcriptions.create(
            model=settings.groq_whisper_model,
            file=(audio_path.name, fh.read()),
            response_format="verbose_json",
            timestamp_granularities=["word", "segment"],
        )
    data = result.model_dump() if hasattr(result, "model_dump") else dict(result)
    raw_segments = [
        SimpleNamespace(start=float(s["start"]), end=float(s["end"]), text=str(s["text"]).strip())
        for s in data.get("segments") or []
        if str(s.get("text", "")).strip()
    ]
    if not raw_segments and data.get("text"):
        raw_segments = [SimpleNamespace(start=0.0, end=float(data.get("duration") or 0), text=data["text"])]

    words = data.get("words") or []
    if _words_trustworthy(words, raw_segments, float(data.get("duration") or 0)):
        segments = _sentences_from_words(words)
    else:
        # Word timings are unreliable for some languages (gaps, a last "word"
        # stretched past the end): split each segment into sentences instead.
        segments = [piece for seg in raw_segments for piece in _split_sentences(seg)]

    # Groq reports the language as a name ("telugu") or a code ("te").
    reported = str(data.get("language") or "en").lower()
    by_name = {lang.name.lower(): code for code, lang in LANGUAGES.items()}
    language = by_name.get(reported, reported.split("-")[0])

    logger.info("Groq {} transcribed {} ({} segments, language={})",
                settings.groq_whisper_model, audio_path.name, len(segments), language)
    return segments, language


async def translate_speech_to_english(audio_path: Path) -> Optional[list]:
    """
    Groq Whisper speech→English translation with segment timings.

    More complete and better timed than transcribing some languages (e.g.
    Telugu) and translating the text. Returns None if unavailable.
    """
    settings = get_settings()
    if not (settings.groq_api_key and settings.groq_whisper_model):
        return None
    from openai import AsyncOpenAI

    client = AsyncOpenAI(base_url="https://api.groq.com/openai/v1", api_key=settings.groq_api_key, max_retries=1)
    try:
        with open(audio_path, "rb") as fh:
            result = await client.audio.translations.create(
                model="whisper-large-v3",  # turbo does not support translation
                file=(audio_path.name, fh.read()),
                response_format="verbose_json",
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Groq speech translation failed: {}", exc)
        return None

    from types import SimpleNamespace

    data = result.model_dump() if hasattr(result, "model_dump") else dict(result)
    segments = [
        SimpleNamespace(start=float(s["start"]), end=float(s["end"]), text=str(s["text"]).strip())
        for s in data.get("segments") or []
        if str(s.get("text", "")).strip()
    ]
    logger.info("Groq translated speech to English: {} segments", len(segments))
    return [piece for seg in segments for piece in _split_sentences(seg)] or None


_SENTENCE_END = (".", "!", "?", "।", "。", "؟")


def _words_trustworthy(words: list, segments: list, duration: float) -> bool:
    if not words or not segments:
        return False
    word_chars = sum(len(str(w.get("word", "")).strip()) for w in words)
    text_chars = sum(len(s.text.replace(" ", "")) for s in segments)
    last_end = max(float(w.get("end", 0)) for w in words)
    return word_chars >= 0.9 * text_chars and (not duration or last_end <= duration + 0.5)


def _split_sentences(seg) -> list:
    """Split one timed segment into sentences, sharing its time by length."""
    import re
    from types import SimpleNamespace

    parts = [p.strip() for p in re.split(r"(?<=[.!?।。؟])\s+", seg.text) if p.strip()]
    if len(parts) <= 1:
        return [seg]
    total = sum(len(p) for p in parts)
    cursor, result = seg.start, []
    for p in parts:
        end = cursor + (seg.end - seg.start) * len(p) / total
        result.append(SimpleNamespace(start=cursor, end=end, text=p))
        cursor = end
    return result


def _sentences_from_words(words: list, max_words: int = 14) -> list:
    """Group word timestamps into sentence-sized caption segments."""
    from types import SimpleNamespace

    segments, current = [], []
    for w in words:
        text = str(w.get("word", "")).strip()
        if not text:
            continue
        current.append(w)
        if text.endswith(_SENTENCE_END) or len(current) >= max_words:
            segments.append(current)
            current = []
    if current:
        segments.append(current)
    return [
        SimpleNamespace(
            start=float(group[0]["start"]),
            end=float(group[-1]["end"]),
            text=" ".join(str(w["word"]).strip() for w in group),
        )
        for group in segments
    ]


async def _transcribe_local(audio_path: Path) -> Tuple[list, str]:
    def _run():
        model = _whisper_model()
        segs, info = model.transcribe(
            str(audio_path),
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
        )
        return list(segs), info.language  # materialise generator inside the thread

    return await asyncio.to_thread(_run)
