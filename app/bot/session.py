"""
Per-chat conversation state.

The bot is a small state machine: each chat has a ``Session`` whose ``step``
decides which buttons / messages are currently valid. Sessions live in memory;
everything durable (scripts, audio, videos, status) is in the database, so a
restart only means the user says "hi" again.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple
from pathlib import Path

from app.services.languages import Voice


class Flow(str, Enum):
    TOPIC = "topic"    # 1. find a trending topic → AI script
    SCRIPT = "script"  # 2. user pastes a script
    AUDIO = "audio"    # 3. user uploads narration audio


class Step(str, Enum):
    IDLE = "idle"
    CHOOSE_LANGUAGE = "choose_language"
    CHOOSE_CATEGORY = "choose_category"
    CHOOSE_TOPIC = "choose_topic"
    AWAIT_CUSTOM_TOPIC = "await_custom_topic"
    REVIEW_SCRIPT = "review_script"
    AWAIT_SCRIPT_EDIT = "await_script_edit"
    AWAIT_SCRIPT_TEXT = "await_script_text"
    CHOOSE_VOICE = "choose_voice"
    REVIEW_AUDIO = "review_audio"
    AWAIT_AUDIO_FILE = "await_audio_file"
    REVIEW_VIDEO = "review_video"


@dataclass
class Session:
    step: Step = Step.IDLE
    flow: Optional[Flow] = None
    language: Optional[str] = None
    category: Optional[str] = None
    topics: List[dict] = field(default_factory=list)        # currently shown batch
    shown_topics: List[str] = field(default_factory=list)   # every topic shown, to avoid repeats
    video_id: Optional[int] = None
    voices: List[Tuple[Voice, Path]] = field(default_factory=list)
    # Held while a step runs, so double taps / extra messages can't start parallel work.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def reset(self) -> None:
        """Forget the current flow (keeps the lock, which may be held by the caller)."""
        self.step = Step.IDLE
        self.flow = None
        self.language = None
        self.category = None
        self.topics = []
        self.shown_topics = []
        self.video_id = None
        self.voices = []


_sessions: Dict[int, Session] = {}


def get_session(chat_id: int) -> Session:
    if chat_id not in _sessions:
        _sessions[chat_id] = Session()
    return _sessions[chat_id]
