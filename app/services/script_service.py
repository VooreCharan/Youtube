"""
Script generation service.
Uses the LLM to produce a structured script with metadata from a video topic.
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.config import get_settings
from app.models.video import Video
from app.services.llm_service import call_llm_json


_SCRIPT_PROMPT = """You are a professional YouTube scriptwriter. Create a complete, engaging video script for the following topic.

Topic: {topic}
Category: {category}
Language: {language} (You MUST write the narration in this exact language!)

Requirements:
1. The script should be for a 5-8 minute narrated video.
2. Divide the script into 5-8 scenes/sections.
3. Each scene should have clear narration text.
4. Generate SEO-optimized metadata.

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
- The "script" field MUST be written in the requested Language: {language}.
- The "script" field should contain ONLY the narration text (what will be spoken aloud).
- Each scene should start with "Scene N: Title" on its own line (the Scene header itself can be in English).
- image_prompts should be extremely short (1-3 words max) generic search terms suitable for a stock video API (e.g., "business meeting", "city skyline", "bmw driving"). Do NOT write long sentences.
- Generate one image_prompt per scene.
- Make the title attention-grabbing and curiosity-inducing.
- Include relevant timestamps in chapters.
"""


async def generate_script(db: AsyncSession, video_id: int, voice: str | None = None) -> Video:
    """Generate a full script + metadata for the given video and persist it."""
    settings = get_settings()

    video = await db.get(Video, video_id)
    if video is None:
        raise ValueError(f"Video {video_id} not found")

    video.upload_status = "generating"
    if voice:
        video.voice = voice
    await db.flush()

    logger.info("Generating script for Video id={} topic='{}' in voice='{}'", video_id, video.topic, video.voice)

    # Derive the language name from the voice locale prefix
    from app.services.tts_service import _voice_to_lang
    lang_code = _voice_to_lang(video.voice) if video.voice else "en"
    
    # Map code to full name for the LLM prompt
    LANG_MAP = {
        "en": "English", "te": "Telugu", "ta": "Tamil", "hi": "Hindi", 
        "bn": "Bengali", "mr": "Marathi", "gu": "Gujarati", "es": "Spanish",
        "fr": "French", "de": "German", "it": "Italian", "pt": "Portuguese"
    }
    language_name = LANG_MAP.get(lang_code, "English")

    prompt = _SCRIPT_PROMPT.format(
        topic=video.topic, 
        category=video.category,
        language=language_name,
    )
    data = await call_llm_json(prompt)

    if not data or not isinstance(data, dict):
        video.upload_status = "failed"
        video.error_message = "LLM returned invalid JSON for script generation"
        await db.flush()
        raise RuntimeError(video.error_message)

    # Persist the generated content
    video.title = data.get("title", video.topic)[:500]
    video.description = data.get("description", "")
    video.tags = json.dumps(data.get("tags", []))
    video.hashtags = json.dumps(data.get("hashtags", []))
    video.chapters = data.get("chapters", "")
    video.script = data.get("script", "")
    video.image_prompts = json.dumps(data.get("image_prompts", []))

    # Save script to file
    script_path = settings.scripts_dir / f"{video_id}.txt"
    script_path.write_text(video.script, encoding="utf-8")

    video.upload_status = "pending"
    await db.flush()
    await db.refresh(video)

    logger.info(
        "Script generated for Video id={}: title='{}'",
        video_id,
        video.title,
    )
    return video
