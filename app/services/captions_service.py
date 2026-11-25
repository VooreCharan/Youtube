"""
English captions for every video, whatever the narration language.

1. Timed source segments come from (best first):
   • Edge-TTS sentence boundaries saved when the voice-over was generated
     (exact timings + the original script text), or
   • Whisper transcription of the audio (uploaded voice-overs, gTTS fallback).
2. Non-English segments are translated to English through the LLM chain in
   batches (timings are kept).
3. Long lines are split into short, readable caption chunks.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from loguru import logger

from app.config import get_settings
from app.services.languages import get_language
from app.services.llm_service import call_llm_json
from app.utils.helpers import format_srt_timestamp

MAX_WORDS_PER_CAPTION = 12
_BATCH = 40


@dataclass
class Segment:
    start: float
    end: float
    text: str


# ── Timing files (written by TTS or audio upload, read by the subtitle step) ──

def timing_path(video_id: int) -> Path:
    return get_settings().subtitles_dir / f"{video_id}.timing.json"


def save_timing(video_id: int, segments: List[Segment], language: Optional[str] = None) -> None:
    """Store timed caption lines; *language* is the language of their text (None = narration language)."""
    timing_path(video_id).write_text(
        json.dumps({"language": language, "segments": [asdict(s) for s in segments]}, ensure_ascii=False),
        encoding="utf-8",
    )


def load_timing(video_id: int) -> Optional[Tuple[List[Segment], Optional[str]]]:
    """Return ``(segments, text_language)`` or None."""
    path = timing_path(video_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):  # older format: plain list in the narration language
            data = {"language": None, "segments": data}
        return [Segment(**s) for s in data["segments"]], data.get("language")
    except (ValueError, TypeError, KeyError):
        return None


def clear_timing(video_id: int) -> None:
    timing_path(video_id).unlink(missing_ok=True)


# ── Translation ──────────────────────────────────────────────────────

_TRANSLATE_PROMPT = """Translate these video caption lines from {language} into natural, fluent English.

Rules:
- Return a JSON array with exactly {count} strings, in the same order: one translation per input line.
- Keep each translation short and spoken-sounding; do not merge or split lines.
- Keep names, numbers and brand names as they are.

Lines:
{lines}
"""


async def translate_to_english(segments: List[Segment], language_code: Optional[str]) -> List[Segment]:
    """Translate segment texts to English (no-op for English)."""
    language = get_language(language_code)
    if language.code == "en" or not segments:
        return segments

    translated: List[Segment] = []
    for i in range(0, len(segments), _BATCH):
        batch = segments[i : i + _BATCH]
        prompt = _TRANSLATE_PROMPT.format(
            language=language.name,
            count=len(batch),
            lines=json.dumps([s.text for s in batch], ensure_ascii=False, indent=0),
        )
        data = await call_llm_json(
            prompt,
            validate=lambda d, n=len(batch): isinstance(_as_list(d), list)
            and len(_as_list(d)) == n
            and all(isinstance(x, str) and x.strip() for x in _as_list(d)),
        )
        for seg, text in zip(batch, _as_list(data)):
            translated.append(Segment(seg.start, seg.end, text.strip()))

    logger.info("Translated {} caption lines from {} to English", len(translated), language.name)
    return translated


_POLISH_PROMPT = """These English video captions were machine-translated from {language} speech.
Fix mistranslations, and replace any romanized {language} words that slipped through
(for example "Krutrima Medhassu" means "artificial intelligence") with the proper English.

Rules:
- Return a JSON array with exactly {count} strings, in the same order.
- Keep each line's meaning and length; do not merge or split lines.
- If a line is already fine, return it unchanged.

Captions:
{lines}
"""


async def polish_english(segments: List[Segment], language_code: Optional[str]) -> List[Segment]:
    """Clean up captions that speech-translation already produced in English."""
    language = get_language(language_code)
    if language.code == "en" or not segments:
        return segments
    polished: List[Segment] = []
    for i in range(0, len(segments), _BATCH):
        batch = segments[i : i + _BATCH]
        try:
            data = await call_llm_json(
                _POLISH_PROMPT.format(
                    language=language.name,
                    count=len(batch),
                    lines=json.dumps([s.text for s in batch], ensure_ascii=False, indent=0),
                ),
                validate=lambda d, n=len(batch): isinstance(_as_list(d), list)
                and len(_as_list(d)) == n
                and all(isinstance(x, str) and x.strip() for x in _as_list(d)),
            )
            polished += [Segment(seg.start, seg.end, text.strip()) for seg, text in zip(batch, _as_list(data))]
        except Exception as exc:  # noqa: BLE001 — unpolished English captions are still usable
            logger.warning("Caption polish failed, keeping speech translation: {}", exc)
            polished += batch
    return polished


def _as_list(data):
    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), None)
    return data


# ── Formatting ───────────────────────────────────────────────────────

def split_long(segments: List[Segment], max_words: int = MAX_WORDS_PER_CAPTION) -> List[Segment]:
    """Break long lines into chunks of ≤ max_words, sharing the time proportionally."""
    result: List[Segment] = []
    for seg in segments:
        words = seg.text.split()
        if len(words) <= max_words:
            result.append(seg)
            continue
        chunks = _balanced_chunks(words, max_words)
        total_chars = sum(len(" ".join(c)) for c in chunks)
        cursor = seg.start
        for chunk in chunks:
            text = " ".join(chunk)
            end = cursor + (seg.end - seg.start) * len(text) / total_chars
            result.append(Segment(cursor, end, text))
            cursor = end
    return result


def _balanced_chunks(words: List[str], max_words: int) -> List[List[str]]:
    count = -(-len(words) // max_words)  # ceil
    size = -(-len(words) // count)
    return [words[i : i + size] for i in range(0, len(words), size)]


# Typographic characters some caption fonts lack (rendered as empty boxes).
_PLAIN = str.maketrans({
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "–", "\u2015": "—",
    "\u00a0": " ", "\u202f": " ", "\u2009": " ", "\u200b": "", "\u2060": "",
})


def to_srt(segments: List[Segment]) -> str:
    lines: List[str] = []
    for idx, seg in enumerate(segments, start=1):
        end = max(seg.end, seg.start + 0.3)
        if idx < len(segments):
            end = min(end, segments[idx].start)  # never overlap the next caption
        lines += [
            str(idx),
            f"{format_srt_timestamp(seg.start)} --> {format_srt_timestamp(end)}",
            seg.text.translate(_PLAIN),
            "",
        ]
    return "\n".join(lines)


def proportional_segments(text: str, duration: float) -> List[Segment]:
    """Last resort: spread sentences over the audio duration by length."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?।。])\s+", text) if s.strip()]
    total = sum(len(s) for s in sentences) or 1
    cursor, segments = 0.0, []
    for s in sentences:
        end = cursor + duration * len(s) / total
        segments.append(Segment(cursor, end, s))
        cursor = end
    return segments
