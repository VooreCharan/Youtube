"""
Background music for videos.

Tracks are organised by mood. Two sources, in this order:

1. Your own tracks: put MP3s in ``<storage>/music/<mood>/`` (for example from the
   YouTube Audio Library, which is safe to use on YouTube). An optional
   ``<track>.txt`` next to a file holds a credit line for the description.
2. A starter pack of Kevin MacLeod (incompetech.com) tracks, licensed under
   Creative Commons Attribution 4.0. They are downloaded the first time a
   mood is needed, and the required credit is added to the YouTube description.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote

import httpx
from loguru import logger

from app.config import get_settings

MOODS = ("inspiring", "mysterious", "calm", "dramatic", "upbeat", "serious")
NO_MUSIC = "none"

# Starter pack: Kevin MacLeod (incompetech.com), CC BY 4.0.
_STARTER = {
    "inspiring": ["Inspired", "Wholesome"],
    "mysterious": ["Lost Frontier", "Floating Cities", "Spacial Harvest", "Ossuary 6 - Air"],
    "calm": ["Healing", "Meditation Impromptu 01", "Long Note Two"],
    "dramatic": ["Heroic Age", "Volatile Reaction"],
    "upbeat": ["Carefree", "Easy Lemon", "Local Forecast - Elevator"],
    "serious": ["Deliberate Thought", "Investigations", "Crypto"],
}
_STARTER_URL = "https://incompetech.com/music/royalty-free/mp3-royaltyfree/{}.mp3"
_MACLEOD_CREDIT = (
    '"{title}" Kevin MacLeod (incompetech.com)\n'
    "Licensed under Creative Commons: By Attribution 4.0 License\n"
    "http://creativecommons.org/licenses/by/4.0/"
)

# Fallback mood when the AI can't classify the script.
_CATEGORY_MOOD = {
    "Space": "mysterious", "Science": "mysterious", "History": "dramatic", "World News": "serious",
    "Technology": "upbeat", "AI": "upbeat", "Business & Finance": "serious", "Health": "calm",
    "Human Body": "calm", "Animals & Nature": "calm", "Entertainment": "upbeat", "Sports": "upbeat",
    "Education": "inspiring", "Self-Improvement": "inspiring",
}


@dataclass
class Track:
    key: str          # "<mood>/<file name>" — stored on the video
    title: str
    mood: str
    path: Path
    credit: str       # text for the YouTube description ("" if none needed)


def _music_dir() -> Path:
    return get_settings().storage_root / "music"


def _starter_dir(mood: str) -> Path:
    return _music_dir() / "_starter" / mood


def _own_tracks(mood: str) -> List[Track]:
    folder = _music_dir() / mood
    tracks = []
    for path in sorted(folder.glob("*.mp3")) if folder.exists() else []:
        credit_file = path.with_suffix(".txt")
        credit = credit_file.read_text(encoding="utf-8").strip() if credit_file.exists() else ""
        tracks.append(Track(f"{mood}/{path.name}", path.stem, mood, path, credit))
    return tracks


async def _starter_tracks(mood: str) -> List[Track]:
    """Starter-pack tracks for *mood*, downloading any that are missing."""
    folder = _starter_dir(mood)
    folder.mkdir(parents=True, exist_ok=True)
    tracks = []
    async with httpx.AsyncClient(timeout=60, follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0 (YouTubeFactsBot)"}) as client:
        for title in _STARTER.get(mood, []):
            path = folder / f"{title}.mp3"
            if not path.exists() or path.stat().st_size < 50_000:
                try:
                    resp = await client.get(_STARTER_URL.format(quote(title)))
                    resp.raise_for_status()
                    path.write_bytes(resp.content)
                    logger.info("Downloaded music '{}' ({} KB)", title, len(resp.content) // 1024)
                except Exception as exc:  # noqa: BLE001 — skip unavailable tracks
                    logger.warning("Could not download music '{}': {}", title, exc)
                    continue
            tracks.append(Track(f"_starter/{mood}/{path.name}", title, mood, path,
                                _MACLEOD_CREDIT.format(title=title)))
    return tracks


async def tracks_for(mood: str) -> List[Track]:
    """Your own tracks for this mood if any, otherwise the starter pack."""
    return _own_tracks(mood) or await _starter_tracks(mood)


async def pick(mood: str, exclude: Optional[List[str]] = None) -> Optional[Track]:
    """A random track for *mood* (avoiding *exclude* keys when possible)."""
    tracks = await tracks_for(mood)
    fresh = [t for t in tracks if t.key not in (exclude or [])]
    return random.choice(fresh or tracks) if tracks else None


def resolve(key: Optional[str]) -> Optional[Track]:
    """Track stored on a video → Track (None for no music / missing file)."""
    if not key or key == NO_MUSIC:
        return None
    path = _music_dir() / key
    if not path.exists():
        return None
    parts = key.split("/")
    if parts[0] == "_starter":
        mood, title = parts[1], path.stem
        return Track(key, title, mood, path, _MACLEOD_CREDIT.format(title=title))
    credit_file = path.with_suffix(".txt")
    credit = credit_file.read_text(encoding="utf-8").strip() if credit_file.exists() else ""
    return Track(key, path.stem, parts[0], path, credit)


_MOOD_PROMPT = """Pick the background-music mood for this YouTube video.

Title: {title}
Category: {category}
Opening of the narration: {excerpt}

Moods: inspiring, mysterious, calm, dramatic, upbeat, serious.
Return JSON: {{"mood": "one of the moods"}}
"""


async def choose_mood(title: str, category: str, script: str) -> str:
    """AI picks a mood from the script; falls back to the category's usual mood."""
    fallback = _CATEGORY_MOOD.get(category, "inspiring")
    try:
        from app.services.llm_service import call_llm_json

        data = await call_llm_json(
            _MOOD_PROMPT.format(title=title, category=category, excerpt=(script or "")[:400]),
            validate=lambda d: isinstance(d, dict) and str(d.get("mood", "")).lower().strip() in MOODS,
        )
        return str(data["mood"]).lower().strip()
    except Exception as exc:  # noqa: BLE001 — music must never block a video
        logger.warning("Mood selection failed ({}); using '{}'", exc, fallback)
        return fallback


def all_moods_cycle(current: str) -> List[str]:
    """Moods to try after *current*, for the "different music" button."""
    i = MOODS.index(current) if current in MOODS else -1
    return [MOODS[(i + k) % len(MOODS)] for k in range(1, len(MOODS) + 1)]


def credit_for(key: Optional[str]) -> str:
    track = resolve(key)
    return track.credit if track else ""


def describe(key: Optional[str]) -> str:
    """Short label for Telegram, e.g. '"Lost Frontier" · mysterious'."""
    if key == NO_MUSIC:
        return "no music"
    track = resolve(key)
    return f'"{track.title}" · {track.mood}' if track else "no music"


__all__ = ["MOODS", "NO_MUSIC", "Track", "pick", "resolve", "choose_mood", "credit_for", "describe",
           "all_moods_cycle", "tracks_for"]
