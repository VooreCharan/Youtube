"""
Telegram conversation handlers.

Flows (every step needs the user's approval before moving on):

  1. Trending topic → language → category → pick/type topic → AI script (approve / regenerate / edit)
     → voice samples → full voice-over (approve / other voice) → video (approve / new visuals) → YouTube
  2. Own script    → language → paste script → voice samples → voice-over → video → YouTube
  3. Own audio     → upload audio → video → YouTube
"""

from __future__ import annotations

import asyncio
import html
import json
from pathlib import Path
from contextlib import suppress
from typing import Any, Awaitable, Callable, Optional

from loguru import logger
from telegram import Message, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from app.bot import keyboards as kb
from app.bot import pipeline
from app.bot.session import Flow, Session, Step, get_session
from app.config import get_settings
from app.services import trends_service
from app.services.languages import get_language
from app.services.llm_service import LLMUnavailableError
from app.services import music_service, script_service
from app.services.research_service import FactCheck
from app.services.tts_service import TooLongForShort
from app.services.youtube_service import YouTubeAuthRequired

h = html.escape
GREETINGS = {"hi", "hii", "hello", "hey", "start", "menu", "hola", "namaste"}
MAX_MESSAGE = 3800  # Telegram limit is 4096; leave room for headers
_FAILED = object()

# Which button actions are valid in which step (stale buttons are ignored).
ALLOWED_STEPS = {
    "lang": {Step.CHOOSE_LANGUAGE},
    "len": {Step.CHOOSE_LENGTH},
    "fix": {Step.FIX_LENGTH},
    "cat": {Step.CHOOSE_CATEGORY},
    "topic": {Step.CHOOSE_TOPIC},
    "topics": {Step.CHOOSE_TOPIC, Step.AWAIT_CUSTOM_TOPIC},
    "script": {Step.REVIEW_SCRIPT},
    "voices": {Step.CHOOSE_VOICE, Step.REVIEW_AUDIO},
    "voice": {Step.CHOOSE_VOICE},
    "audio": {Step.REVIEW_AUDIO},
    "video": {Step.REVIEW_VIDEO},
    "music": {Step.REVIEW_VIDEO},
}


# ════════════════════════════════════════════════════════════════════
#  Infrastructure
# ════════════════════════════════════════════════════════════════════

async def _authorized(update: Update) -> bool:
    user = update.effective_user
    allowed = get_settings().allowed_user_ids
    if user and user.id in allowed:
        return True
    logger.warning("Blocked Telegram user {} ({})", user.id if user else "?", user.username if user else "?")
    if update.effective_chat:
        await update.effective_chat.send_message(
            "🔒 This bot is private.\n\n"
            f"Your Telegram user ID is <code>{user.id if user else '?'}</code>. "
            "To get access, the owner adds it to <code>TELEGRAM_ALLOWED_USERS</code> in the .env file "
            "and restarts the bot.",
            parse_mode=ParseMode.HTML,
        )
    return False


async def _keep_chat_action(context: ContextTypes.DEFAULT_TYPE, chat_id: int, action: str) -> None:
    while True:
        with suppress(TelegramError):
            await context.bot.send_chat_action(chat_id, action)
        await asyncio.sleep(4.5)


def _friendly_error(exc: Exception) -> str:
    if isinstance(exc, YouTubeAuthRequired):
        return str(exc)
    text = str(exc).strip() or exc.__class__.__name__
    if isinstance(exc, LLMUnavailableError):
        return (
            "None of the AI providers could answer right now.\n\n" + text
            + "\n\nCheck the keys in .env (run `python -m app.llm_check`), or try again in a minute."
        )
    limit = 900 if isinstance(exc, LLMUnavailableError) else 350
    return text if len(text) < limit else text[:limit] + "…"


async def run_step(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    status: str,
    work: Callable[[Callable[[str], Awaitable[None]]], Awaitable[Any]],
    *,
    retry: Optional[str] = None,
    action: str = ChatAction.TYPING,
) -> Any:
    """
    Show a status message, run *work*, and turn failures into a friendly
    message with a "Try again" button. *work* receives a ``report(text)``
    callback to update the status message with progress.

    Returns the work result, or ``_FAILED``.
    """
    status_msg = await context.bot.send_message(chat_id, f"⏳ {status}", parse_mode=ParseMode.HTML)
    ticker = asyncio.create_task(_keep_chat_action(context, chat_id, action))

    async def report(text: str) -> None:
        with suppress(TelegramError):
            await status_msg.edit_text(f"⏳ {text}", parse_mode=ParseMode.HTML)

    try:
        result = await work(report)
    except Exception as exc:  # noqa: BLE001 — every failure must reach the user
        logger.exception("Bot step failed: {}", status)
        markup = kb.retry(retry) if retry else kb.only_menu()
        with suppress(TelegramError):
            await status_msg.edit_text(
                f"❌ <b>That didn't work.</b>\n\n{h(_friendly_error(exc))}",
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
        return _FAILED
    finally:
        ticker.cancel()

    with suppress(TelegramError):
        await status_msg.delete()
    return result


def _chunks(text: str, size: int = MAX_MESSAGE) -> list:
    """Split on paragraph/line boundaries so messages stay readable."""
    parts, current = [], ""
    for para in text.split("\n"):
        while len(para) > size:
            parts.append(para[:size])
            para = para[size:]
        if len(current) + len(para) + 1 > size:
            parts.append(current)
            current = para
        else:
            current = f"{current}\n{para}" if current else para
    if current:
        parts.append(current)
    return parts or [""]


async def _remove_buttons(message: Optional[Message]) -> None:
    if message:
        with suppress(TelegramError):
            await message.edit_reply_markup(reply_markup=None)


# ════════════════════════════════════════════════════════════════════
#  Entry points
# ════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update):
        return
    session = get_session(update.effective_chat.id)
    if session.lock.locked():
        await update.effective_chat.send_message("⏳ I'm still working on the previous step — one moment.")
        return
    await pipeline.cancel(session.video_id)
    session.reset()
    await send_main_menu(update.effective_chat.id, context, update.effective_user.first_name)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update):
        return
    session = get_session(update.effective_chat.id)
    if session.lock.locked():
        await update.effective_chat.send_message(
            "⏳ A step is running right now. You can cancel as soon as it finishes."
        )
        return
    await pipeline.cancel(session.video_id)
    session.reset()
    await update.effective_chat.send_message("✖️ Cancelled. Nothing was published.", reply_markup=kb.main_menu())


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update):
        return
    await update.effective_chat.send_message(
        "<b>How it works</b>\n\n"
        "Say <b>hi</b> (or /start) and choose:\n"
        "1️⃣ <b>Find a trending topic</b> — latest topics by category, AI script, voice, video\n"
        "2️⃣ <b>I have a script</b> — paste it, pick a voice, get the video\n"
        "3️⃣ <b>I have the audio</b> — send your voice-over, get the video\n\n"
        "You approve the script, voice-over and final video — nothing is published without your OK.\n\n"
        "/start — main menu\n/cancel — stop the current video\n/help — this message",
        parse_mode=ParseMode.HTML,
    )


async def send_main_menu(chat_id: int, context: ContextTypes.DEFAULT_TYPE, name: Optional[str] = None) -> None:
    greeting = f"👋 Hi {h(name)}!" if name else "👋 Hi!"
    await context.bot.send_message(
        chat_id,
        f"{greeting} I turn ideas into published YouTube videos.\n\n"
        "<b>What do you have today?</b>\n\n"
        "1️⃣ <b>Find a trending topic</b> — I'll show the latest topics by category, "
        "write the script, voice it and build the video.\n"
        "2️⃣ <b>I have a script</b> — paste it, pick a voice, and I'll do the rest.\n"
        "3️⃣ <b>I have the audio</b> — send your voice-over and I'll build the video.\n\n"
        "You approve every step before anything goes to YouTube.",
        parse_mode=ParseMode.HTML,
        reply_markup=kb.main_menu(),
    )


# ════════════════════════════════════════════════════════════════════
#  Button router
# ════════════════════════════════════════════════════════════════════

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await _authorized(update):
        await query.answer()
        return

    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    action, _, arg = (query.data or "").partition(":")

    if session.lock.locked():
        await query.answer("⏳ Still working on the previous step…", show_alert=False)
        return

    allowed = ALLOWED_STEPS.get(action)
    if allowed is not None and session.step not in allowed:
        await query.answer("This button has expired. Say hi to start again.", show_alert=False)
        await _remove_buttons(query.message)
        return

    async with session.lock:
        await query.answer()
        await _remove_buttons(query.message)
        try:
            await _dispatch(action, arg, chat_id, session, context, update)
        except Exception:  # noqa: BLE001
            logger.exception("Unhandled error for button {}", query.data)
            await context.bot.send_message(
                chat_id, "❌ Something unexpected happened. Say hi to start again.", reply_markup=kb.only_menu()
            )


async def _dispatch(action, arg, chat_id, session: Session, context, update) -> None:
    if action == "nav":
        await _navigate(arg, chat_id, session, context, update)
    elif action == "menu":
        await _choose_flow(arg, chat_id, session, context)
    elif action == "lang":
        await _language_chosen(arg, chat_id, session, context)
    elif action == "len":
        await _length_chosen(arg, chat_id, session, context)
    elif action == "fix":
        await _fix_length(arg, chat_id, session, context)
    elif action == "cat":
        session.category = arg
        session.shown_topics = []
        await show_topics(chat_id, session, context)
    elif action == "topics" and arg == "more":
        await show_topics(chat_id, session, context)
    elif action == "topics" and arg == "custom":
        session.step = Step.AWAIT_CUSTOM_TOPIC
        await context.bot.send_message(
            chat_id, "✍️ Type the topic you want a video about:", reply_markup=kb.InlineKeyboardMarkup([[kb.CANCEL]])
        )
    elif action == "topic":
        index = int(arg)
        if 0 <= index < len(session.topics):
            chosen = session.topics[index]
            await start_script(chat_id, session, context, chosen["title"], chosen.get("title_en"))
    elif action == "script":
        await _script_action(arg, chat_id, session, context)
    elif action == "voices":
        await show_voices(chat_id, session, context)
    elif action == "voice":
        await record_voiceover(chat_id, session, context, int(arg))
    elif action == "audio" and arg == "ok":
        await build_video(chat_id, session, context)
    elif action == "audio" and arg == "revoice":
        await show_voices(chat_id, session, context)
    elif action == "video" and arg == "ok":
        await publish(chat_id, session, context)
    elif action == "video" and arg == "regen":
        await build_video(chat_id, session, context, fresh_visuals=True)
    elif action == "music":
        await change_music(chat_id, session, context, arg)


async def _navigate(where, chat_id, session: Session, context, update) -> None:
    if where in ("cancel", "menu"):
        await pipeline.cancel(session.video_id)
        session.reset()
        if where == "cancel":
            await context.bot.send_message(chat_id, "✖️ Cancelled. Nothing was published.")
        await send_main_menu(chat_id, context)
    elif where == "language":
        session.step = Step.CHOOSE_LANGUAGE
        await context.bot.send_message(chat_id, "🌐 Choose the language:", reply_markup=kb.languages())
    elif where == "length":
        await _ask_length(chat_id, session, context)
    elif where == "categories":
        session.step = Step.CHOOSE_CATEGORY
        await _send_categories(chat_id, session, context)
    elif where == "topics":
        await pipeline.cancel(session.video_id)
        session.video_id = None
        if session.topics:
            session.step = Step.CHOOSE_TOPIC
            await _send_topic_list(chat_id, session, context)
        else:
            session.step = Step.CHOOSE_CATEGORY
            await _send_categories(chat_id, session, context)


# ════════════════════════════════════════════════════════════════════
#  Step implementations
# ════════════════════════════════════════════════════════════════════

async def _choose_flow(choice, chat_id, session: Session, context) -> None:
    await pipeline.cancel(session.video_id)
    session.reset()
    if choice == "audio":
        session.flow = Flow.AUDIO
        await _ask_length(chat_id, session, context)
        return

    session.flow = Flow.TOPIC if choice == "topic" else Flow.SCRIPT
    session.step = Step.CHOOSE_LANGUAGE
    what = "script and voice-over" if session.flow == Flow.TOPIC else "voice-over"
    await context.bot.send_message(
        chat_id, f"🌐 Which language should the {what} be in?", reply_markup=kb.languages()
    )


async def _language_chosen(code, chat_id, session: Session, context) -> None:
    session.language = get_language(code).code
    await _ask_length(chat_id, session, context)


async def _ask_length(chat_id, session: Session, context) -> None:
    session.step = Step.CHOOSE_LENGTH
    if session.flow == Flow.TOPIC:
        await context.bot.send_message(
            chat_id,
            "⏱ <b>How long should the video be?</b>\n\n"
            "📱 <b>Short</b>: vertical, under 1 minute, for YouTube Shorts.\n"
            "⏱ <b>Minutes</b>: a regular horizontal video; the script is written to that length.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb.lengths(),
        )
    else:
        what = "script" if session.flow == Flow.SCRIPT else "audio"
        await context.bot.send_message(
            chat_id,
            f"📐 <b>Short or long video?</b>\n\nYour {what} decides the exact length. "
            "A Short must be under 1 minute.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb.short_or_long(),
        )


def _length_label(target) -> str:
    if target == pipeline.SHORT:
        return "📱 Short (under 1 min, vertical)"
    if target:
        return f"⏱ ~{target // 60} min video"
    return "🎬 Long video"


async def _length_chosen(arg, chat_id, session: Session, context) -> None:
    session.target = pipeline.SHORT if arg == "short" else (int(arg) if arg.isdigit() else None)
    await context.bot.send_message(chat_id, f"👍 {_length_label(session.target)}")

    if session.flow == Flow.TOPIC:
        session.step = Step.CHOOSE_CATEGORY
        await _send_categories(chat_id, session, context)
    elif session.flow == Flow.SCRIPT:
        session.step = Step.AWAIT_SCRIPT_TEXT
        language = get_language(session.language)
        limit = ""
        if session.target == pipeline.SHORT:
            limit = (f"\n\n📱 For a Short, keep it under about "
                     f"<b>{language.units_for(55)} {language.unit_name}</b> (≈55 seconds).")
        await context.bot.send_message(
            chat_id,
            f"📄 Paste your <b>{h(language.name)}</b> script as a message.\n\n"
            "Only the words to be spoken — I'll generate the title, description, tags and visuals from it. "
            "Long scripts can be sent in one message (up to 4096 characters)." + limit,
            parse_mode=ParseMode.HTML,
            reply_markup=kb.InlineKeyboardMarkup([[kb.CANCEL]]),
        )
    else:
        session.step = Step.AWAIT_AUDIO_FILE
        limit = "\n\n📱 For a Short, the audio must be <b>under 60 seconds</b>." if session.target == pipeline.SHORT else ""
        await context.bot.send_message(
            chat_id,
            "🎧 <b>Send me your voice-over</b> as an audio file or voice message "
            "(mp3, m4a, wav, ogg — max 20 MB).\n\n"
            "I'll detect the language, write the title & description, add matching footage and subtitles." + limit,
            parse_mode=ParseMode.HTML,
            reply_markup=kb.InlineKeyboardMarkup([[kb.CANCEL]]),
        )


async def _fix_length(arg, chat_id, session: Session, context) -> None:
    """A Short's voice-over came out too long."""
    if arg == "shorter":
        session.step = Step.REVIEW_SCRIPT
        await _generate_script(chat_id, session, context, regenerate=True)
    elif arg == "resend":
        await pipeline.cancel(session.video_id)
        session.video_id = None
        session.step = Step.AWAIT_SCRIPT_TEXT
        language = get_language(session.language)
        await context.bot.send_message(
            chat_id,
            f"📄 Send a shorter script: about <b>{language.units_for(55)} {language.unit_name}</b> or less.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb.InlineKeyboardMarkup([[kb.CANCEL]]),
        )
    elif arg == "long":
        session.target = None
        if session.pending_text:  # pasted script that was too long for a Short
            text, session.pending_text = session.pending_text, None
            await context.bot.send_message(chat_id, "🎬 OK, making a regular (horizontal) video.")
            await _own_script_received(text, chat_id, session, context, None)
            return
        await pipeline.make_long(session.video_id)
        await context.bot.send_message(chat_id, "🎬 OK, it's now a regular (horizontal) video.")
        session.step = Step.CHOOSE_VOICE
        if session.voice_index is not None:
            await record_voiceover(chat_id, session, context, session.voice_index)
        else:
            await show_voices(chat_id, session, context)


async def _send_categories(chat_id, session: Session, context) -> None:
    lang = get_language(session.language)
    await context.bot.send_message(
        chat_id, f"📂 Pick a category ({lang.button_label}):", reply_markup=kb.categories()
    )


async def show_topics(chat_id, session: Session, context) -> None:
    settings = get_settings()
    category = trends_service.CATEGORIES.get(session.category or "")
    if category is None:
        session.step = Step.CHOOSE_CATEGORY
        await _send_categories(chat_id, session, context)
        return
    language = get_language(session.language)

    result = await run_step(
        context,
        chat_id,
        f"Finding the latest <b>{h(category.label)}</b> topics…",
        lambda report: trends_service.suggest_topics(
            category, language, exclude=session.shown_topics, count=settings.topics_per_page
        ),
        retry="topics:more" if session.step == Step.CHOOSE_TOPIC else f"cat:{category.key}",
    )
    if result is _FAILED:
        return

    session.topics = result
    session.shown_topics.extend(t["title"] for t in result)
    session.step = Step.CHOOSE_TOPIC
    await _send_topic_list(chat_id, session, context)


async def _send_topic_list(chat_id, session: Session, context) -> None:
    category = trends_service.CATEGORIES[session.category]
    lines = [f"🔥 <b>Trending in {h(category.label)}</b>\n"]
    for i, topic in enumerate(session.topics, start=1):
        lines.append(f"<b>{i}.</b> {h(topic['title'])}")
        if topic.get("why"):
            lines.append(f"    <i>{h(topic['why'])}</i>")
    lines.append("\nTap a number, ask for other topics, or just <b>type your own topic</b>.")
    await context.bot.send_message(
        chat_id, "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=kb.topics(len(session.topics))
    )


async def start_script(chat_id, session: Session, context, topic: str, topic_en: Optional[str] = None) -> None:
    category = trends_service.CATEGORIES.get(session.category or "")
    language = get_language(session.language)
    session.video_id = await pipeline.create_video(
        topic=topic,
        category=category.label if category else "People & Blogs",
        language=language,
        source=Flow.TOPIC.value,
        chat_id=chat_id,
        target=session.target,
        topic_en=topic_en,
    )
    session.step = Step.REVIEW_SCRIPT
    await context.bot.send_message(chat_id, f"👍 Topic: <b>{h(topic)}</b>", parse_mode=ParseMode.HTML)
    await _generate_script(chat_id, session, context, regenerate=False)


async def _generate_script(chat_id, session: Session, context, regenerate: bool) -> None:
    language = get_language(session.language)
    verb = "Rewriting" if regenerate else "Writing"
    video = await run_step(
        context,
        chat_id,
        f"Researching sources, {verb.lower()} and fact-checking your {h(language.name)} script… (1–2 minutes)",
        lambda report: pipeline.write_script(session.video_id, regenerate=regenerate),
        retry="script:regen",
    )
    if video is not _FAILED:
        await send_script(chat_id, video, context)


async def send_script(chat_id, video, context) -> None:
    language = get_language(video.language)
    units = language.count_units(video.script or "")
    seconds = script_service.estimated_seconds(video.script or "", language)
    kind = "📱 Short" if script_service.is_short(video) else "🎬 Video"
    length = f"~{seconds} s" if seconds < 90 else f"~{round(seconds / 60)} min"
    header = (
        f"📝 <b>{h(video.title or video.topic)}</b>\n"
        f"{kind} · 🌐 {h(language.name)} · {units} {language.unit_name} · {length} spoken\n\n"
    )
    for i, chunk in enumerate(_chunks(video.script or "")):
        await context.bot.send_message(chat_id, (header if i == 0 else "") + h(chunk), parse_mode=ParseMode.HTML)

    await context.bot.send_message(
        chat_id,
        fact_summary(video) + "\n\n👉 <b>Approve this script?</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb.script_review(),
        disable_web_page_preview=True,
    )


def _mmss(audio_path) -> str:
    try:
        from app.services.video_service import _get_audio_duration

        seconds = _get_audio_duration(Path(audio_path))
        return f"{int(seconds // 60)}:{int(seconds % 60):02d}"
    except Exception:  # noqa: BLE001
        return "?"


def fact_summary(video) -> str:
    """Fact-check result and sources, formatted for Telegram."""
    check = FactCheck.from_json(getattr(video, "fact_check", None))
    sources = json.loads(video.sources) if getattr(video, "sources", None) else []

    research = ""
    topic_en = getattr(video, "topic_en", None)
    if topic_en and topic_en != video.topic:
        research = f"🔎 Researched as: <i>{h(topic_en)}</i>\n"

    if check is None or not check.checked:
        reason = (check.error if check and check.error else "no sources could be found for this topic")
        lines = [f"⚠️ <b>Not fact-checked</b> — {h(reason)}."]
    elif not check.unsupported:
        lines = [f"✅ <b>Fact-checked</b> against {check.sources} sources — every fact is supported."]
        if check.corrected:
            lines.append(f"🛠 {check.corrected} unsupported statement(s) were corrected or removed automatically.")
    else:
        lines = [f"⚠️ <b>{len(check.unsupported)} statement(s) could not be verified</b> against {check.sources} sources:"]
        for issue in check.unsupported[:5]:
            quote = issue["quote"] if len(issue["quote"]) <= 160 else issue["quote"][:160] + "…"
            lines.append(f"• “{h(quote)}”" + (f"\n   <i>{h(issue.get('problem', ''))}</i>" if issue.get("problem") else ""))
        lines.append("You can regenerate, edit it, or approve anyway.")

    if research:
        lines.insert(0, research.rstrip())
    if sources:
        lines.append("\n📚 <b>Sources</b>")
        for src in sources[:6]:
            label = h(src.get("title", "")[:90])
            meta = ", ".join(x for x in (src.get("publisher"), src.get("published")) if x)
            link = f'<a href="{h(src["url"], quote=True)}">{label}</a>' if src.get("url") else label
            lines.append(f"• {link}" + (f" — {h(meta)}" if meta else ""))
        if len(sources) > 6:
            lines.append(f"…and {len(sources) - 6} more (all listed in the YouTube description).")
    return "\n".join(lines)


async def _script_action(arg, chat_id, session: Session, context) -> None:
    if arg == "ok":
        await show_voices(chat_id, session, context)
    elif arg == "regen":
        await _generate_script(chat_id, session, context, regenerate=True)
    elif arg == "edit":
        session.step = Step.AWAIT_SCRIPT_EDIT
        await context.bot.send_message(
            chat_id,
            "✏️ Copy the script above, edit it, and send the <b>full</b> new version as a message.",
            parse_mode=ParseMode.HTML,
        )


async def show_voices(chat_id, session: Session, context) -> None:
    session.step = Step.CHOOSE_VOICE
    language = get_language(session.language)
    samples = await run_step(
        context,
        chat_id,
        f"Preparing {h(language.name)} voice samples…",
        lambda report: pipeline.voice_samples(session.video_id),
        retry="voices:show",
        action=ChatAction.RECORD_VOICE,
    )
    if samples is _FAILED:
        return
    if not samples:
        await context.bot.send_message(
            chat_id, "❌ No voice could be generated right now.", reply_markup=kb.retry("voices:show")
        )
        return

    session.voices = samples
    await context.bot.send_message(
        chat_id,
        f"🎙 <b>Choose a voice</b> — each sample reads the opening of your script.\n"
        f"Tap <b>✅ Use …</b> under the one you like.",
        parse_mode=ParseMode.HTML,
    )
    for index, (voice, path) in enumerate(samples):
        with path.open("rb") as fh:
            await context.bot.send_audio(
                chat_id,
                audio=fh,
                title=voice.label,
                performer="Voice sample",
                filename=f"{voice.label.split(' · ')[0]}.mp3",
                caption=f"🔊 {voice.label}",
                reply_markup=kb.voice_pick(voice, index),
                write_timeout=120,
            )


async def record_voiceover(chat_id, session: Session, context, index: int) -> None:
    if not (0 <= index < len(session.voices)):
        return
    voice, _ = session.voices[index]
    session.voice_index = index

    async def work(report):
        try:
            return await pipeline.narrate(session.video_id, voice.id)
        except TooLongForShort as exc:
            return exc

    video = await run_step(
        context,
        chat_id,
        f"Recording the full voice-over with <b>{h(voice.label)}</b>…",
        work,
        retry=f"voice:{index}",
        action=ChatAction.RECORD_VOICE,
    )
    if video is _FAILED:
        return
    if isinstance(video, TooLongForShort):
        session.step = Step.FIX_LENGTH
        await context.bot.send_message(
            chat_id,
            f"⏱ <b>Too long for a Short.</b> The voice-over is <b>{video.seconds:.0f} seconds</b>; "
            f"Shorts must be under {video.limit:.0f}. Speeding it up enough would sound rushed.\n\n"
            "What would you like to do?",
            parse_mode=ParseMode.HTML,
            reply_markup=kb.fix_length(ai_script=session.flow == Flow.TOPIC),
        )
        return

    session.step = Step.REVIEW_AUDIO
    with open(video.audio_path, "rb") as fh:
        await context.bot.send_audio(
            chat_id,
            audio=fh,
            title=(video.title or video.topic)[:60],
            performer=voice.label,
            caption=f"🎧 <b>Your voice-over is ready</b> ({_mmss(video.audio_path)}). "
            "Listen and approve it to build the video.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb.audio_review(),
            write_timeout=300,
            read_timeout=120,
        )


async def build_video(chat_id, session: Session, context, fresh_visuals: bool = False) -> None:
    session.step = Step.REVIEW_VIDEO
    stages = iter([
        "🎞 Finding stock footage for each scene…",
        "💬 Creating subtitles…",
        "🎬 Rendering the video…",
        "🖼 Designing the thumbnail…",
    ])

    async def work(report):
        async def on_stage():
            # Rough progress: advance one stage every ~25 s while the build runs.
            for text in stages:
                await report(text + "\n<i>This usually takes 2–5 minutes.</i>")
                await asyncio.sleep(25)

        progress = asyncio.create_task(on_stage())
        try:
            video = await pipeline.build_video(session.video_id, fresh_visuals=fresh_visuals)
            await report("📦 Preparing the preview…")
            preview = await pipeline.telegram_preview(video)
            return video, preview
        finally:
            progress.cancel()

    result = await run_step(
        context,
        chat_id,
        "Building your video…",
        work,
        retry="video:regen",
        action=ChatAction.UPLOAD_VIDEO,
    )
    if result is _FAILED:
        return
    await send_video_for_review(chat_id, context, *result)


async def change_music(chat_id, session: Session, context, choice: str) -> None:
    status = {"none": "Removing the music…", "add": "Adding background music…"}.get(
        choice, "Trying a different track…")
    result = await run_step(
        context, chat_id, status + " (re-rendering, about a minute)",
        lambda report: pipeline.change_music(session.video_id, choice),
        retry=f"music:{choice}", action=ChatAction.UPLOAD_VIDEO,
    )
    if result is not _FAILED:
        await send_video_for_review(chat_id, context, *result, with_thumbnail=False)


async def send_video_for_review(chat_id, context, video, preview, with_thumbnail: bool = True) -> None:
    settings = get_settings()
    if video.thumbnail_path and with_thumbnail:
        with suppress(Exception), open(video.thumbnail_path, "rb") as fh:
            await context.bot.send_photo(
                chat_id, photo=fh, caption=f"🖼 Thumbnail\n<b>{h(video.title or video.topic)}</b>",
                parse_mode=ParseMode.HTML,
            )

    caption = (
        f"🎬 <b>{h((video.title or video.topic)[:200])}</b>\n\n"
        f"🎵 Music: {h(music_service.describe(video.music))}\n"
        f"Approve to publish on YouTube as <b>{h(settings.youtube_privacy)}</b>."
    )
    has_music = music_service.resolve(video.music) is not None
    if preview is None:
        await context.bot.send_message(
            chat_id,
            caption + "\n\n⚠️ The video is too large to preview in Telegram. "
            f"It's saved on the server at <code>{h(video.video_path)}</code>.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb.video_review(has_music),
        )
        return

    with open(preview, "rb") as fh:
        await context.bot.send_video(
            chat_id,
            video=fh,
            caption=caption,
            parse_mode=ParseMode.HTML,
            supports_streaming=True,
            reply_markup=kb.video_review(has_music),
            write_timeout=600,
            read_timeout=600,
        )


async def publish(chat_id, session: Session, context) -> None:
    url = await run_step(
        context,
        chat_id,
        "Publishing to YouTube… (large videos can take a few minutes)",
        lambda report: pipeline.publish(session.video_id),
        retry="video:ok",
        action=ChatAction.UPLOAD_VIDEO,
    )
    if url is _FAILED:
        return
    session.reset()
    await context.bot.send_message(
        chat_id,
        f"🎉 <b>Published!</b>\n\n{h(url)}\n\nSay hi anytime to make the next one.",
        parse_mode=ParseMode.HTML,
        reply_markup=kb.only_menu(),
    )


# ════════════════════════════════════════════════════════════════════
#  Free text & files
# ════════════════════════════════════════════════════════════════════

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update):
        return
    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    text = (update.message.text or "").strip()

    if session.lock.locked():
        await update.message.reply_text("⏳ I'm still working on the previous step — one moment.")
        return

    async with session.lock:
        try:
            await _handle_text(text, chat_id, session, context, update)
        except Exception:  # noqa: BLE001
            logger.exception("Unhandled error handling text")
            await context.bot.send_message(
                chat_id, "❌ Something unexpected happened. Say hi to start again.", reply_markup=kb.only_menu()
            )


async def _handle_text(text, chat_id, session: Session, context, update) -> None:
    if text.lower().strip("!.👋 ") in GREETINGS or session.step == Step.IDLE:
        await pipeline.cancel(session.video_id)
        session.reset()
        await send_main_menu(chat_id, context, update.effective_user.first_name)
        return

    if session.step in (Step.CHOOSE_TOPIC, Step.AWAIT_CUSTOM_TOPIC):
        if text.isdigit() and 1 <= int(text) <= len(session.topics) and session.step == Step.CHOOSE_TOPIC:
            text = session.topics[int(text) - 1]["title"]
        if len(text) < 4:
            await update.message.reply_text("Please type a topic with a few more words.")
            return
        await start_script(chat_id, session, context, text[:300])
        return

    if session.step == Step.AWAIT_SCRIPT_EDIT:
        if len(text.split()) < 20:
            await update.message.reply_text("That looks too short for a full script — please send the whole edited script.")
            return
        session.step = Step.REVIEW_SCRIPT
        video = await run_step(
            context, chat_id, "Fact-checking your edited script…",
            lambda report: pipeline.replace_script_text(session.video_id, text),
            retry="script:regen",
        )
        if video is not _FAILED:
            await send_script(chat_id, video, context)
        return

    if session.step == Step.AWAIT_SCRIPT_TEXT:
        await _own_script_received(text, chat_id, session, context, update)
        return

    hints = {
        Step.AWAIT_AUDIO_FILE: "🎧 Please send an audio file or voice message.",
        Step.CHOOSE_LANGUAGE: "👆 Please pick a language from the buttons above.",
        Step.CHOOSE_CATEGORY: "👆 Please pick a category from the buttons above.",
    }
    await update.message.reply_text(
        hints.get(session.step, "👆 Please use the buttons above, or say hi to start over."),
        reply_markup=kb.only_menu(),
    )


async def _own_script_received(text, chat_id, session: Session, context, update) -> None:
    language = get_language(session.language)
    if language.count_units(text) < (100 if language.counts_characters else 30):
        await context.bot.send_message(
            chat_id, "That's quite short — please send at least ~30 words of script (or say hi to start over)."
        )
        return

    if session.target == pipeline.SHORT:
        seconds = script_service.estimated_seconds(text, language)
        if seconds > script_service.SHORT_MAX_SECONDS * 1.2:
            session.pending_text = text
            session.step = Step.FIX_LENGTH
            await context.bot.send_message(
                chat_id,
                f"⏱ <b>That script is about {seconds} seconds</b> when spoken — too long for a Short "
                f"(under 60 s, roughly {language.units_for(55)} {language.unit_name}).",
                parse_mode=ParseMode.HTML,
                reply_markup=kb.fix_length(ai_script=False),
            )
            return

    session.video_id = await pipeline.create_video(
        topic="Custom script", category="People & Blogs", language=language,
        source=Flow.SCRIPT.value, chat_id=chat_id, target=session.target,
    )
    video = await run_step(
        context,
        chat_id,
        "Reading your script, preparing title and visuals, and fact-checking it… (about a minute)",
        lambda report: pipeline.use_own_script(session.video_id, text, language),
    )
    if video is _FAILED:
        await context.bot.send_message(chat_id, "Please send your script again to retry.")
        return

    await context.bot.send_message(
        chat_id,
        f"✅ Got it — <b>{h(video.title or '')}</b>\n{len(text.split())} words.\n\n{fact_summary(video)}"
        "\n\n<i>Your script is used exactly as written. To change it, say hi and paste a corrected version.</i>",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    await show_voices(chat_id, session, context)


_AUDIO_EXTENSIONS = (".mp3", ".m4a", ".wav", ".ogg", ".oga", ".opus", ".aac", ".flac", ".webm")


async def on_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update):
        return
    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    message = update.message

    if session.lock.locked():
        await message.reply_text("⏳ I'm still working on the previous step — one moment.")
        return
    if session.step != Step.AWAIT_AUDIO_FILE:
        await message.reply_text(
            "To make a video from your audio, say hi and choose 3️⃣ I have the audio.", reply_markup=kb.only_menu()
        )
        return

    media = message.audio or message.voice or message.document
    filename = getattr(media, "file_name", None) or "voice.ogg"
    if message.document and not (
        (message.document.mime_type or "").startswith("audio/") or filename.lower().endswith(_AUDIO_EXTENSIONS)
    ):
        await message.reply_text("That doesn't look like an audio file. Please send mp3, m4a, wav or ogg.")
        return
    if media.file_size and media.file_size > pipeline.TELEGRAM_DOWNLOAD_LIMIT:
        await message.reply_text(
            "That file is over Telegram's 20 MB bot limit. Please send a compressed mp3 (e.g. 96–128 kbps)."
        )
        return

    async with session.lock:
        session.video_id = await pipeline.create_video(
            topic="Uploaded audio", category="People & Blogs", language=get_language(None),
            source=Flow.AUDIO.value, chat_id=chat_id, target=session.target,
        )
        target = pipeline.save_upload_path(session.video_id, filename)

        async def work(report):
            tg_file = await context.bot.get_file(media.file_id)
            await tg_file.download_to_drive(custom_path=target)
            await report("Listening to your audio — transcribing and writing the title & description…")
            return await pipeline.use_own_audio(session.video_id, target)

        video = await run_step(context, chat_id, "Downloading your audio…", work)
        if video is _FAILED:
            await context.bot.send_message(chat_id, "Please send the audio again to retry.")
            return

        session.language = video.language
        note = ""
        if session.target == pipeline.SHORT and video.video_format != "vertical":
            note = (f"\n\n⏱ Your audio is {_mmss(video.audio_path)}, longer than a Short allows (under 1 min), "
                    "so I'm making a regular horizontal video instead.")
        await context.bot.send_message(
            chat_id,
            f"✅ Detected <b>{h(get_language(video.language).name)}</b> · "
            f"<b>{h(video.title or '')}</b>{note}",
            parse_mode=ParseMode.HTML,
        )
        await build_video(chat_id, session, context)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.opt(exception=context.error).error("Telegram update failed: {}", context.error)
