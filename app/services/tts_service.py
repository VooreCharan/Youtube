"""
Text-to-Speech service.

Neural voices:  edge-tts (Microsoft, free, several male/female voices per language)
Basic voice:    gTTS     (Google Translate TTS, one voice per language)

Each engine falls back to the other so narration still gets produced
if one service is unavailable.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path
from typing import List, Optional, Tuple

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.video import Video
from app.services.languages import (
    GTTS_VOICE_PREFIX,
    LANGUAGES,
    Language,
    Voice,
    get_language,
    language_from_voice,
)


def _voice_to_lang(voice: str) -> str:
    """Kept for backwards compatibility: 'en-US-AvaNeural' / 'gtts:te' → 'en' / 'te'."""
    return language_from_voice(voice).code


# ── Script cleaning ─────────────────────────────────────────────────

_SCENE_HEADER = re.compile(r"^\s*scene\s+\d+\s*[:\-–]", re.IGNORECASE)


def narration_from_script(script: str) -> str:
    """Strip 'Scene N: …' headers and bracketed stage directions, keep spoken text."""
    lines = []
    for line in (script or "").splitlines():
        stripped = line.strip()
        if not stripped or _SCENE_HEADER.match(stripped):
            continue
        stripped = re.sub(r"^\[(.*)\]$", r"\1", stripped)  # "[text]" placeholders → text
        lines.append(stripped)
    return " ".join(lines)


def sample_text(narration: str, max_chars: int = 220) -> str:
    """First sentence(s) of the narration, used for voice previews."""
    if len(narration) <= max_chars:
        return narration
    cut = narration[:max_chars]
    end = max(cut.rfind(p) for p in (".", "!", "?", "।", "。"))
    return cut[: end + 1] if end > 60 else cut.rsplit(" ", 1)[0] + "…"


# ── Engines ─────────────────────────────────────────────────────────

def _run_gtts(text: str, lang: str, output_path: Path) -> None:
    from gtts import gTTS  # imported lazily so a missing package only breaks this engine

    gTTS(text=text, lang=lang, slow=False).save(str(output_path))


async def _edge(text: str, voice: str, output_path: Path) -> None:
    import edge_tts

    await edge_tts.Communicate(text, voice).save(str(output_path))


async def synthesize(text: str, voice: str, output_path: Path) -> str:
    """
    Render *text* with *voice* to *output_path* (MP3).

    Returns the voice actually used (may differ if a fallback engine was needed).
    """
    language = language_from_voice(voice)
    neural_fallback = language.voices[0].id if language.voices else None

    attempts: List[Tuple[str, str]] = []  # (engine, voice)
    if voice.startswith(GTTS_VOICE_PREFIX):
        attempts.append(("gtts", voice))
        if neural_fallback:
            attempts.append(("edge", neural_fallback))
    else:
        attempts.append(("edge", voice))
        attempts.append(("gtts", f"{GTTS_VOICE_PREFIX}{language.code}"))

    errors = []
    for engine, v in attempts:
        try:
            if engine == "edge":
                await _edge(text, v, output_path)
            else:
                await asyncio.to_thread(_run_gtts, text, language.code, output_path)
            if not output_path.exists() or output_path.stat().st_size == 0:
                raise RuntimeError("engine produced an empty file")
            if v != voice:
                logger.warning("TTS fell back from '{}' to '{}'", voice, v)
            return v
        except Exception as exc:  # noqa: BLE001 — try the next engine
            logger.warning("TTS engine {} failed for voice '{}': {}", engine, v, exc)
            errors.append(f"{engine}({v}): {exc}")

    raise RuntimeError("All TTS engines failed: " + " | ".join(errors))


# ── Public API ──────────────────────────────────────────────────────

async def generate_audio(
    db: AsyncSession,
    video_id: int,
    voice: Optional[str] = None,
) -> str:
    """Generate the full MP3 narration for a video. Returns the audio path."""
    settings = get_settings()

    video = await db.get(Video, video_id)
    if video is None:
        raise ValueError(f"Video {video_id} not found")
    if not video.script:
        raise ValueError(f"Video {video_id} has no script — generate script first")

    selected_voice = voice or video.voice or settings.default_voice
    narration = narration_from_script(video.script)
    if not narration.strip():
        raise ValueError(f"Video {video_id} script has no narration content")

    output_path = settings.audio_dir / f"{video_id}.mp3"
    logger.info("Generating narration for Video id={} with voice '{}'", video_id, selected_voice)

    used_voice = await synthesize(narration, selected_voice, output_path)
    logger.info("Narration saved: {} ({:.1f} KB)", output_path, output_path.stat().st_size / 1024)

    video.audio_path = str(output_path)
    video.voice = used_voice
    video.subtitle_path = None  # audio changed → subtitles must be regenerated
    await db.flush()
    return str(output_path)


async def generate_voice_samples(text: str, language: Language) -> List[Tuple[Voice, Path]]:
    """
    Render *text* with every voice offered for *language*.

    Samples are cached by (voice, text) so re-opening the voice list is instant.
    Voices that fail to render are skipped.
    """
    settings = get_settings()
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]

    async def _one(voice: Voice) -> Optional[Tuple[Voice, Path]]:
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", voice.id)
        path = settings.voice_samples_dir / f"{safe_id}_{digest}.mp3"
        if path.exists() and path.stat().st_size > 0:
            return voice, path
        try:
            used = await synthesize(text, voice.id, path)
            if used != voice.id:  # don't present a fallback as if it were this voice
                path.unlink(missing_ok=True)
                return None
            return voice, path
        except Exception as exc:  # noqa: BLE001
            logger.warning("Voice sample failed for {}: {}", voice.id, exc)
            return None

    results = await asyncio.gather(*(_one(v) for v in language.all_voices()))
    return [r for r in results if r]


async def list_voices() -> list[dict]:
    """Return all available voices, grouped by language."""
    voices = []
    for lang in LANGUAGES.values():
        for v in lang.all_voices():
            voices.append({"name": v.id, "label": v.label, "language": lang.name, "locale": lang.code})
    return voices


__all__ = [
    "generate_audio",
    "generate_voice_samples",
    "list_voices",
    "narration_from_script",
    "sample_text",
    "synthesize",
    "get_language",
]
