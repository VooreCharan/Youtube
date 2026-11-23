"""Inline keyboards. Callback data format: ``<action>:<argument>``."""

from __future__ import annotations

from typing import List, Sequence

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup

from app.services.languages import LANGUAGES, Voice
from app.services.trends_service import CATEGORIES

CANCEL = Btn("✖️ Cancel", callback_data="nav:cancel")
MENU = Btn("🏠 Main menu", callback_data="nav:menu")


def _rows(buttons: Sequence[Btn], per_row: int) -> List[List[Btn]]:
    return [list(buttons[i : i + per_row]) for i in range(0, len(buttons), per_row)]


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [Btn("1️⃣ Find a trending topic", callback_data="menu:topic")],
        [Btn("2️⃣ I have a script", callback_data="menu:script")],
        [Btn("3️⃣ I have the audio", callback_data="menu:audio")],
    ])


def languages() -> InlineKeyboardMarkup:
    buttons = [Btn(lang.button_label, callback_data=f"lang:{code}") for code, lang in LANGUAGES.items()]
    return InlineKeyboardMarkup(_rows(buttons, 2) + [[CANCEL]])


def categories() -> InlineKeyboardMarkup:
    buttons = [Btn(c.button_label, callback_data=f"cat:{key}") for key, c in CATEGORIES.items()]
    return InlineKeyboardMarkup(
        _rows(buttons, 2) + [[Btn("🌐 Change language", callback_data="nav:language"), CANCEL]]
    )


def topics(count: int) -> InlineKeyboardMarkup:
    numbers = [Btn(str(i + 1), callback_data=f"topic:{i}") for i in range(count)]
    return InlineKeyboardMarkup(
        _rows(numbers, 4)
        + [
            [Btn("🔄 Show other topics", callback_data="topics:more")],
            [Btn("✍️ Type my own topic", callback_data="topics:custom")],
            [Btn("⬅️ Categories", callback_data="nav:categories"), CANCEL],
        ]
    )


def script_review() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [Btn("✅ Approve script", callback_data="script:ok")],
        [Btn("🔄 Regenerate", callback_data="script:regen"), Btn("✏️ Edit it myself", callback_data="script:edit")],
        [Btn("⬅️ Pick another topic", callback_data="nav:topics"), CANCEL],
    ])


def voice_pick(voice: Voice, index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[Btn(f"✅ Use {voice.label.split(' · ')[0]}", callback_data=f"voice:{index}")]])


def voice_list(voices: Sequence[Voice]) -> InlineKeyboardMarkup:
    buttons = [Btn(v.label, callback_data=f"voice:{i}") for i, v in enumerate(voices)]
    return InlineKeyboardMarkup(_rows(buttons, 1) + [[CANCEL]])


def audio_review() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [Btn("✅ Approve voice-over", callback_data="audio:ok")],
        [Btn("🎙 Try another voice", callback_data="audio:revoice")],
        [CANCEL],
    ])


def video_review() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [Btn("🚀 Approve & publish to YouTube", callback_data="video:ok")],
        [Btn("🔄 New visuals", callback_data="video:regen")],
        [CANCEL],
    ])


def retry(callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[Btn("🔁 Try again", callback_data=callback_data), MENU]])


def only_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[MENU]])
