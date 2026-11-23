"""
Script-aware font selection for thumbnails (Pillow) and burned-in subtitles (libass).

Latin fonts have no glyphs for Telugu, Hindi, Arabic, Japanese…, which renders
as empty boxes. Docker installs the Noto fonts listed here; macOS paths are
included so local runs work too.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

_NOTO = "/usr/share/fonts/truetype/noto"
_MAC = "/System/Library/Fonts"
_MAC_SUP = "/System/Library/Fonts/Supplemental"

# language code → (libass family name, candidate font files, bold first)
_SCRIPT_FONTS = {
    "hi": ("Noto Sans Devanagari", [f"{_NOTO}/NotoSansDevanagari-Bold.ttf", f"{_MAC_SUP}/Devanagari Sangam MN.ttc"]),
    "mr": ("Noto Sans Devanagari", [f"{_NOTO}/NotoSansDevanagari-Bold.ttf", f"{_MAC_SUP}/Devanagari Sangam MN.ttc"]),
    "te": ("Noto Sans Telugu", [f"{_NOTO}/NotoSansTelugu-Bold.ttf", f"{_MAC}/KohinoorTelugu.ttc", f"{_MAC_SUP}/Telugu Sangam MN.ttc"]),
    "ta": ("Noto Sans Tamil", [f"{_NOTO}/NotoSansTamil-Bold.ttf", f"{_MAC_SUP}/Tamil Sangam MN.ttc"]),
    "kn": ("Noto Sans Kannada", [f"{_NOTO}/NotoSansKannada-Bold.ttf", f"{_MAC}/NotoSansKannada.ttc", f"{_MAC_SUP}/Kannada Sangam MN.ttc"]),
    "ml": ("Noto Sans Malayalam", [f"{_NOTO}/NotoSansMalayalam-Bold.ttf", f"{_MAC_SUP}/Malayalam Sangam MN.ttc"]),
    "bn": ("Noto Sans Bengali", [f"{_NOTO}/NotoSansBengali-Bold.ttf", f"{_MAC}/KohinoorBangla.ttc"]),
    "gu": ("Noto Sans Gujarati", [f"{_NOTO}/NotoSansGujarati-Bold.ttf", f"{_MAC}/KohinoorGujarati.ttc", f"{_MAC_SUP}/Gujarati Sangam MN.ttc"]),
    "ar": ("Noto Sans Arabic", [f"{_NOTO}/NotoSansArabic-Bold.ttf", f"{_MAC}/SFArabic.ttf", f"{_MAC}/GeezaPro.ttc"]),
    "ja": ("Noto Sans CJK JP", [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        f"{_MAC}/ヒラギノ角ゴシック W6.ttc",
        f"{_MAC}/Hiragino Sans GB.ttc",
    ]),
}

_LATIN_FILES = [
    "arialbd.ttf",
    "arial.ttf",
    f"{_NOTO}/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    f"{_MAC_SUP}/Arial Bold.ttf",
]


def subtitle_font_family(language: Optional[str]) -> Optional[str]:
    """Font family name for libass's force_style, or None for the default."""
    entry = _SCRIPT_FONTS.get((language or "").split("-")[0])
    return entry[0] if entry else None


@lru_cache(maxsize=64)
def load_font(language: Optional[str], size: int):
    """Return a Pillow font that can render *language*, falling back to Latin/default."""
    from PIL import ImageFont

    entry = _SCRIPT_FONTS.get((language or "").split("-")[0])
    candidates = (entry[1] if entry else []) + _LATIN_FILES
    for path in candidates:
        if "/" in path and not Path(path).exists():
            continue
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()
