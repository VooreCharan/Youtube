"""
Shared helper functions used across services.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def sanitize_filename(name: str) -> str:
    """Remove characters not safe for filenames."""
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    name = re.sub(r"\s+", "_", name.strip())
    return name[:200]  # cap length


def format_srt_timestamp(seconds: float) -> str:
    """Convert a float seconds value into SRT timestamp format HH:MM:SS,mmm."""
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds - int(seconds)) * 1000)
    return f"{hrs:02d}:{mins:02d}:{secs:02d},{millis:03d}"


def ensure_dir(path: str | Path) -> Path:
    """Create directory (and parents) if it doesn't exist. Returns the Path."""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def json_loads_safe(text: str, fallback: Any = None) -> Any:
    """Parse JSON, returning *fallback* on failure instead of raising."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return fallback


def extract_json_from_text(text: str) -> str | None:
    """
    Extract the first JSON object or array from a text block that may contain
    markdown fences or surrounding prose.
    """
    # Try to find JSON inside ```json ... ``` fences
    match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()

    # Try to find a raw JSON object or array
    for start_char, end_char in [("{", "}"), ("[", "]")]:
        start = text.find(start_char)
        if start == -1:
            continue
        depth = 0
        for i in range(start, len(text)):
            if text[i] == start_char:
                depth += 1
            elif text[i] == end_char:
                depth -= 1
            if depth == 0:
                return text[start : i + 1]

    return None


def truncate(text: str, max_length: int = 5000) -> str:
    """Truncate text to a maximum length with an ellipsis indicator."""
    if len(text) <= max_length:
        return text
    return text[: max_length - 3] + "..."
