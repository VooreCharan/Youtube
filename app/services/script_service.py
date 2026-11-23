"""
Script generation service.

- ``generate_script``      – LLM writes a full narrated script + metadata for a topic.
- ``apply_user_script``    – keep the user's own script/transcript verbatim and let
                             the LLM produce only the metadata (title, tags, visuals…).
"""

from __future__ import annotations

import json
from datetime import date
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.config import get_settings
from app.models.video import Video
from app.services.languages import get_language, language_from_voice
from app.services.llm_service import call_llm_json


_SCRIPT_PROMPT = """You are a professional YouTube scriptwriter. Create a complete, engaging video script for the following topic.

Today's date: {today} (write as of this date; do not present past years as the future)
Topic: {topic}
Category: {category}
Language: {language} (You MUST write the narration in this exact language!)

Requirements:
1. The script should be for a 5-8 minute narrated video: about 750-1000 words of narration in total,
   roughly 120-180 words per scene. Do not write a short summary.
2. Divide the script into 5-8 scenes/sections.
3. Each scene should have clear narration text.
4. Open with a strong hook in the first two sentences; end with a call to action.
5. Generate SEO-optimized metadata.

Respond with a JSON object in this EXACT format:
{{
    "title": "An engaging, click-worthy YouTube title (max 100 chars)",
    "description": "A compelling YouTube description (200-500 words) with relevant keywords. Include a brief summary, key points covered, and a call to action.",
    "tags": ["tag1", "tag2", "tag3", "tag4", "tag5", "tag6", "tag7", "tag8", "tag9", "tag10"],
    "hashtags": ["#hashtag1", "#hashtag2", "#hashtag3", "#hashtag4", "#hashtag5"],
    "chapters": "00:00 Introduction\\n00:45 Section Title\\n...",
    "script": "Scene 1: Introduction\\n[Narration text for scene 1]\\n\\nScene 2: ...\\n[Narration text for scene 2]\\n\\n...",
    "image_prompts": [
        "short generic keywords",
        "short generic keywords",
        "..."
    ]
}}

IMPORTANT:
- The "title", "description" and "script" fields MUST be written in {language}.
- The "script" field should contain ONLY the narration text (what will be spoken aloud).
- Each scene should start with "Scene N: Title" on its own line (the Scene header itself must be in English).
- image_prompts must be in ENGLISH and extremely short (1-3 words max) generic search terms suitable for a stock video API (e.g., "business meeting", "city skyline", "bmw driving"). Do NOT write long sentences.
- Generate one image_prompt per scene.
- Make the title attention-grabbing and curiosity-inducing.
- Include relevant timestamps in chapters.
{avoid}"""


_METADATA_PROMPT = """You are a YouTube SEO expert. Below is the narration of a video (written by the creator — do NOT rewrite it).

Narration language: {language} (the text below may be an English translation of it)
Narration:
\"\"\"
{script}
\"\"\"

Produce metadata for this video. Respond with a JSON object in this EXACT format:
{{
    "title": "Click-worthy YouTube title in {language} (max 100 chars)",
    "description": "YouTube description in {language} (150-400 words): summary, key points, call to action",
    "tags": ["10 relevant tags"],
    "hashtags": ["#five", "#relevant", "#hashtags"],
    "category": "one of: {categories}",
    "image_prompts": ["exactly {scenes} ENGLISH stock-footage search terms, 1-3 words each, in the order the narration covers them"]
}}
"""

_YT_CATEGORIES = [
    "Technology", "AI", "Business & Finance", "Science", "Health", "Entertainment",
    "Sports", "World News", "Education", "Self-Improvement", "People & Blogs",
]


def _language_for(video: Video):
    return get_language(video.language) if video.language else language_from_voice(video.voice)


async def generate_script(
    db: AsyncSession,
    video_id: int,
    voice: Optional[str] = None,
    avoid_previous: bool = False,
) -> Video:
    """Generate a full script + metadata for the given video and persist it.

    With *avoid_previous* the LLM is told to take a different angle than the
    current script (used by the bot's "Regenerate" button).
    """
    video = await db.get(Video, video_id)
    if video is None:
        raise ValueError(f"Video {video_id} not found")

    if voice:
        video.voice = voice
    language = _language_for(video)
    previous_title = video.title if avoid_previous else None

    video.upload_status = "generating"
    await db.flush()

    logger.info("Generating script for Video id={} topic='{}' language={}", video_id, video.topic, language.name)

    avoid = ""
    if previous_title:
        avoid = (
            f"- A previous draft was titled \"{previous_title}\". The user rejected it: "
            "take a clearly different angle, structure and hook.\n"
        )

    prompt = _SCRIPT_PROMPT.format(
        today=date.today().strftime("%d %B %Y"),
        topic=video.topic,
        category=video.category,
        language=language.name,
        avoid=avoid,
    )
    data = await call_llm_json(
        prompt,
        validate=lambda d: isinstance(d, dict) and len(str(d.get("script", "")).split()) >= 50 and bool(d.get("title")),
    )

    if not data or not isinstance(data, dict) or not data.get("script"):
        video.upload_status = "failed"
        video.error_message = "LLM returned invalid JSON for script generation"
        await db.flush()
        raise RuntimeError(video.error_message)

    _apply_metadata(video, data)
    video.script = str(data.get("script", ""))
    video.chapters = str(data.get("chapters", "") or "")
    _save_script_file(video)

    video.upload_status = "pending"
    video.error_message = None
    await db.flush()
    await db.refresh(video)

    logger.info("Script generated for Video id={}: title='{}'", video_id, video.title)
    return video


async def apply_user_script(
    db: AsyncSession,
    video_id: int,
    script_text: str,
    language_name: Optional[str] = None,
) -> Video:
    """Store a user-supplied script (or audio transcript) verbatim and generate metadata for it."""
    video = await db.get(Video, video_id)
    if video is None:
        raise ValueError(f"Video {video_id} not found")

    script_text = script_text.strip()
    language_name = language_name or _language_for(video).name
    words = len(script_text.split())
    scenes = max(4, min(12, words // 70 or 1))

    prompt = _METADATA_PROMPT.format(
        language=language_name,
        script=script_text[:12000],
        categories=", ".join(_YT_CATEGORIES),
        scenes=scenes,
    )
    data = await call_llm_json(prompt, validate=lambda d: isinstance(d, dict) and bool(d.get("title")))
    if not data or not isinstance(data, dict):
        raise RuntimeError("LLM returned invalid JSON while preparing video metadata")

    _apply_metadata(video, data)
    if not json.loads(video.image_prompts or "[]"):
        video.image_prompts = json.dumps(["abstract background"] * scenes)
    category = str(data.get("category", "")).strip()
    if category in _YT_CATEGORIES:
        video.category = category
    if video.topic in ("", "Custom script", "Uploaded audio") and video.title:
        video.topic = video.title

    video.script = script_text
    video.chapters = ""
    _save_script_file(video)
    video.error_message = None
    await db.flush()
    await db.refresh(video)

    logger.info("User script stored for Video id={} ({} words, {} scenes)", video_id, words, scenes)
    return video


def _apply_metadata(video: Video, data: dict) -> None:
    video.title = str(data.get("title") or video.topic)[:500]
    video.description = str(data.get("description", "") or "")
    video.tags = json.dumps([str(t) for t in data.get("tags", []) or []])
    video.hashtags = json.dumps([str(h) for h in data.get("hashtags", []) or []])
    prompts = [str(p) for p in data.get("image_prompts", []) or [] if str(p).strip()]
    video.image_prompts = json.dumps(prompts)


def _save_script_file(video: Video) -> None:
    settings = get_settings()
    (settings.scripts_dir / f"{video.id}.txt").write_text(video.script or "", encoding="utf-8")
