"""
Text-to-Speech service.

Primary:  gTTS  (Google Translate TTS — free, no API key, very reliable)
Fallback: edge-tts (Microsoft, may return 403 when token is rotated)
"""

from __future__ import annotations

import asyncio
from functools import partial
from pathlib import Path

from gtts import gTTS
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.video import Video


# Map common Edge-TTS locale prefixes → gTTS language codes
_LOCALE_TO_GTTS: dict[str, str] = {
    "en": "en",
    "es": "es",
    "fr": "fr",
    "de": "de",
    "it": "it",
    "pt": "pt",
    "hi": "hi",
    "ja": "ja",
    "ko": "ko",
    "zh": "zh",
    "ar": "ar",
    "ru": "ru",
    "nl": "nl",
    "pl": "pl",
    "tr": "tr",
    "sv": "sv",
    "da": "da",
    "fi": "fi",
    "nb": "no",
    "id": "id",
    # Indian languages
    "te": "te",
    "ta": "ta",
    "bn": "bn",
    "mr": "mr",
    "gu": "gu",
}

# Default gTTS language when the voice locale can't be resolved
_DEFAULT_LANG = "en"


def _voice_to_lang(voice: str) -> str:
    """
    Derive a gTTS language code from an Edge-TTS voice name or locale string.

    Examples:
        'en-US-AvaNeural'  → 'en'
        'fr-FR-DeniseNeural' → 'fr'
        'en'               → 'en'
    """
    prefix = voice.split("-")[0].lower()
    return _LOCALE_TO_GTTS.get(prefix, _DEFAULT_LANG)


def _run_gtts(text: str, lang: str, output_path: Path) -> None:
    """Blocking call — run inside a thread via asyncio.to_thread."""
    tts = gTTS(text=text, lang=lang, slow=False)
    tts.save(str(output_path))


async def generate_audio(
    db: AsyncSession,
    video_id: int,
    voice: str | None = None,
) -> str:
    """
    Generate an MP3 narration file from the video's script.

    Uses gTTS (Google Translate TTS) as the primary engine.
    Falls back to edge-tts if gTTS fails.

    Returns the absolute path to the saved audio file.
    """
    settings = get_settings()

    video = await db.get(Video, video_id)
    if video is None:
        raise ValueError(f"Video {video_id} not found")
    if not video.script:
        raise ValueError(f"Video {video_id} has no script — generate script first")

    selected_voice = voice or video.voice or settings.default_voice
    output_path = settings.audio_dir / f"{video_id}.mp3"
    lang = _voice_to_lang(selected_voice)

    logger.info(
        "Generating TTS audio for Video id={} | voice='{}' → lang='{}'",
        video_id,
        selected_voice,
        lang,
    )

    # ── Clean script ─────────────────────────────────────────────────
    lines = video.script.split("\n")
    narration_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.lower().startswith("scene ") and ":" in stripped:
            continue  # skip scene headers
        if stripped:
            narration_lines.append(stripped)
    narration = " ".join(narration_lines)

    if not narration.strip():
        raise ValueError(f"Video {video_id} script has no narration content")

    # ── Primary: gTTS ────────────────────────────────────────────────
    try:
        await asyncio.to_thread(_run_gtts, narration, lang, output_path)
        logger.info(
            "gTTS audio saved: {} ({:.1f} KB)",
            output_path,
            output_path.stat().st_size / 1024,
        )
    except Exception as gtts_exc:
        logger.warning("gTTS failed ({}), trying edge-tts fallback …", gtts_exc)

        # ── Fallback: edge-tts ────────────────────────────────────────
        try:
            import edge_tts  # noqa: PLC0415

            communicate = edge_tts.Communicate(narration, selected_voice)
            await communicate.save(str(output_path))
            logger.info(
                "edge-tts fallback audio saved: {} ({:.1f} KB)",
                output_path,
                output_path.stat().st_size / 1024,
            )
        except Exception as edge_exc:
            logger.error("Both gTTS and edge-tts failed. gTTS={} | edge={}", gtts_exc, edge_exc)
            raise RuntimeError(
                f"All TTS engines failed.\n"
                f"  • gTTS error:     {gtts_exc}\n"
                f"  • edge-tts error: {edge_exc}"
            ) from edge_exc

    video.audio_path = str(output_path)
    video.voice = selected_voice
    await db.flush()

    return str(output_path)


# ── Voice listing ─────────────────────────────────────────────────────────────

_GTTS_VOICES: list[dict] = [
    {"name": "en-US", "gender": "Neutral", "locale": "en-US"},
    {"name": "en-GB", "gender": "Neutral", "locale": "en-GB"},
    {"name": "en-AU", "gender": "Neutral", "locale": "en-AU"},
    {"name": "en-IN", "gender": "Neutral", "locale": "en-IN"},
    {"name": "es-ES", "gender": "Neutral", "locale": "es-ES"},
    {"name": "es-MX", "gender": "Neutral", "locale": "es-MX"},
    {"name": "fr-FR", "gender": "Neutral", "locale": "fr-FR"},
    {"name": "de-DE", "gender": "Neutral", "locale": "de-DE"},
    {"name": "it-IT", "gender": "Neutral", "locale": "it-IT"},
    {"name": "pt-BR", "gender": "Neutral", "locale": "pt-BR"},
    {"name": "pt-PT", "gender": "Neutral", "locale": "pt-PT"},
    {"name": "hi-IN", "gender": "Neutral", "locale": "hi-IN"},
    {"name": "ja-JP", "gender": "Neutral", "locale": "ja-JP"},
    {"name": "ko-KR", "gender": "Neutral", "locale": "ko-KR"},
    {"name": "zh-CN", "gender": "Neutral", "locale": "zh-CN"},
    {"name": "zh-TW", "gender": "Neutral", "locale": "zh-TW"},
    {"name": "ar-SA", "gender": "Neutral", "locale": "ar-SA"},
    {"name": "ru-RU", "gender": "Neutral", "locale": "ru-RU"},
    {"name": "nl-NL", "gender": "Neutral", "locale": "nl-NL"},
    {"name": "pl-PL", "gender": "Neutral", "locale": "pl-PL"},
    {"name": "tr-TR", "gender": "Neutral", "locale": "tr-TR"},
    {"name": "sv-SE", "gender": "Neutral", "locale": "sv-SE"},
    {"name": "id-ID", "gender": "Neutral", "locale": "id-ID"},
    {"name": "te-IN", "gender": "Neutral", "locale": "te-IN"},  # Telugu
    {"name": "ta-IN", "gender": "Neutral", "locale": "ta-IN"},  # Tamil
    {"name": "bn-IN", "gender": "Neutral", "locale": "bn-IN"},  # Bengali
    {"name": "mr-IN", "gender": "Neutral", "locale": "mr-IN"},  # Marathi
    {"name": "gu-IN", "gender": "Neutral", "locale": "gu-IN"},  # Gujarati
]


async def list_voices() -> list[dict]:
    """Return available TTS voices (gTTS locale list)."""
    return _GTTS_VOICES
