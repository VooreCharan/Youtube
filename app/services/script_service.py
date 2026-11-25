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
from app.services import research_service
from app.services.llm_service import call_llm_json


_SCRIPT_PROMPT = """You are a scriptwriter for a FACT-BASED YouTube channel. Viewers trust this channel
because every fact in it is accurate and verifiable.

Today's date: {today} (write as of this date; do not present past years as the future)
Topic: {topic}
Category: {category}
Language: {language} (You MUST write the narration in this exact language!)
Stay strictly on this topic; ignore any source that is about something else.

SOURCES (the ONLY facts you may use):
{sources}

FACT RULES (most important):
- Every specific fact (numbers, dates, names, places, events, quotes, records, "first/largest" claims)
  MUST come from the SOURCES above. Never invent or guess facts, statistics or quotes.
- If the sources don't cover something, leave it out. A shorter accurate script beats a longer invented one.
- Mention when something happened (e.g. "In March 2026, …") and who reported it when it matters.
- Clearly separate facts from interpretation ("experts say", "this could mean") and never present
  speculation or rumours as fact.
- General, uncontroversial background knowledge (what a satellite is, how rain forms) is allowed.

Requirements:
1. {length_rules}
2. Each scene should have clear narration text.
3. Open with a strong, accurate hook (a surprising TRUE fact); end with a call to action.
4. Generate SEO-optimized metadata. Title and description must be accurate — no clickbait claims
   the video does not back up.

Respond with a JSON object in this EXACT format:
{{
    "title": "Engaging but accurate YouTube title (max 100 chars)",
    "description": "YouTube description (150-400 words): summary of the facts covered and a call to action. Do not include links.",
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
- The "script" field should contain ONLY the narration text (what will be spoken aloud) — no source
  numbers, brackets or URLs.
- Each scene should start with "Scene N: Title" on its own line (the Scene header itself must be in English).
- image_prompts must be in ENGLISH and extremely short (1-3 words max) generic search terms suitable for a stock video API (e.g., "business meeting", "city skyline", "bmw driving"). Do NOT write long sentences.
- {image_rule}
{avoid}"""


_OUTLINE_PROMPT = """You are planning a FACT-BASED YouTube video of about {minutes} minutes, narrated in {language}.

Today's date: {today}
Topic: {topic}
Category: {category}

SOURCES (the ONLY facts you may use):
{sources}

Plan exactly {scenes} scenes that together tell the story in a logical order (hook first, wrap-up and
call to action last). Spread the facts across scenes so each scene has enough real material; give each
scene background, context and "why it matters" explanation so it can fill about {scene_seconds} seconds
without inventing anything. Every specific fact must come from the SOURCES.

Respond with JSON:
{{
  "title": "Engaging but accurate YouTube title in {language} (max 100 chars, no clickbait)",
  "description": "YouTube description in {language} (150-400 words), no links",
  "tags": ["10 tags"],
  "hashtags": ["#five", "#hashtags"],
  "scenes": [
    {{"title": "short ENGLISH scene title",
      "facts": ["2-4 short facts this scene covers, in English, each from the sources"],
      "source_ids": [1, 2],
      "image_prompt": "1-3 word ENGLISH stock footage search term"}}
  ]
}}
{avoid}"""


_SCENE_PROMPT = """You are writing one scene of a FACT-BASED YouTube video narrated in {language}.

Video title: {title}
Scene {number} of {total}: {scene_title}
Facts to cover in this scene:
{facts}
{previous}
SOURCES for this scene (the ONLY specific facts you may use):
{sources}

Write the narration for this scene only:
- Length: about {units} {unit_name} of {language} narration (≈{seconds} seconds spoken). Reach that length
  by explaining context, background and why it matters — never by inventing numbers, dates, names or quotes.
- {position_rule}
- Spoken text only: no scene header, no source numbers, brackets or URLs.

Respond with JSON: {{"narration": "the narration text in {language}"}}
"""


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
    "Sports", "World News", "Space", "History", "Animals & Nature", "Human Body", "Education",
    "Self-Improvement", "People & Blogs",
]


def _language_for(video: Video):
    return get_language(video.language) if video.language else language_from_voice(video.voice)


SHORT_MAX_SECONDS = 59        # YouTube Shorts here are kept under one minute
_SHORT_TARGET_SECONDS = 50    # aim a little lower: the voice-over is fitted afterwards
_SINGLE_PASS_MAX_SECONDS = 240


def _topic_line(video: Video) -> str:
    if video.topic_en and video.topic_en != video.topic:
        return f"{video.topic}  (in English: {video.topic_en})"
    return video.topic


def is_short(video: Video) -> bool:
    return video.video_format == "vertical" or (video.target_seconds or 999) <= 60


def estimated_seconds(text: str, language) -> int:
    """How long *text* takes to narrate, from the language's measured pace."""
    return int(language.count_units(text) / language.words_per_minute * 60)


async def generate_script(
    db: AsyncSession,
    video_id: int,
    voice: Optional[str] = None,
    avoid_previous: bool = False,
) -> Video:
    """Generate a fact-checked script + metadata sized to the video's target length.

    Shorts and videos up to 4 minutes are written in one pass; longer videos
    are planned first and written scene by scene (keeps each request inside
    free-tier limits and hits the target length). With *avoid_previous* the
    AI takes a different angle (the bot's "Regenerate" button).
    """
    video = await db.get(Video, video_id)
    if video is None:
        raise ValueError(f"Video {video_id} not found")

    if voice:
        video.voice = voice
    language = _language_for(video)
    short = is_short(video)
    target = _SHORT_TARGET_SECONDS if short else (video.target_seconds or 300)
    previous_title = video.title if avoid_previous else None

    video.upload_status = "generating"
    await db.flush()

    logger.info("Generating {} script ({}s) for Video id={} topic='{}' language={}",
                "Short" if short else "long", target, video_id, video.topic, language.name)

    avoid = ""
    if previous_title:
        avoid = (
            f"- A previous draft was titled \"{previous_title}\". The user rejected it: "
            "take a clearly different angle, structure and hook.\n"
        )

    # Research once per video; "Regenerate" reuses the same sources.
    sources = research_service.load_sources(video_id)
    if sources is None:
        if not video.topic_en:
            video.topic_en = await research_service.english_topic(video.topic)
        sources = await research_service.gather_sources(video.topic_en, video.category, language, target)
        research_service.save_sources(video_id, sources)

    if short or target <= _SINGLE_PASS_MAX_SECONDS:
        data = await _single_pass(video, language, sources, target, short, avoid)
    else:
        data = await _scene_by_scene(video, language, sources, target, avoid)

    _apply_metadata(video, data)
    script, check = await research_service.verify_script(
        str(data.get("script", "")), sources, language.name, fix=True
    )
    video.script = script
    video.chapters = ""  # computed from the real voice-over timing (long videos)
    video.sources = json.dumps(research_service.public_sources(sources), ensure_ascii=False)
    video.fact_check = check.to_json()
    _save_script_file(video)

    video.upload_status = "pending"
    video.error_message = None
    await db.flush()
    await db.refresh(video)

    logger.info("Script generated for Video id={}: '{}' (~{}s narration)",
                video_id, video.title, estimated_seconds(video.script, language))
    return video


async def _single_pass(video, language, sources, target: int, short: bool, avoid: str) -> dict:
    # Long scripts come out ~25% short, so ask for more; Shorts are kept tight (hard 60 s limit).
    units = language.units_for(target) if short else int(language.units_for(target) * 1.25)
    if short:
        length_rules = (
            f"This is a YouTube SHORT (vertical, under 60 seconds). Write about {units} {language.unit_name} "
            f"of narration (≈{target} seconds) — never more than {language.units_for(SHORT_MAX_SECONDS - 4)}. "
            "Use 1-3 scenes. The very first sentence must be the most surprising TRUE fact (no intro, no "
            "\"welcome back\"). End with a one-line call to follow for more facts."
        )
        image_rule = "Generate 4-6 image_prompts (the visuals change every few seconds)."
    else:
        scenes = max(3, min(8, round(target / 60)))
        length_rules = (
            f"Write about {units} {language.unit_name} of narration (≈{round(target / 60)} minutes spoken) "
            f"in {scenes} scenes. Reach that length by explaining context and why it matters — never by "
            "inventing facts."
        )
        image_rule = "Generate one image_prompt per scene."

    prompt = _SCRIPT_PROMPT.format(
        today=date.today().strftime("%d %B %Y"),
        topic=_topic_line(video),
        category=video.category,
        language=language.name,
        sources=research_service.format_sources(sources),
        length_rules=length_rules,
        image_rule=image_rule,
        avoid=avoid,
    )
    minimum = max(20, int(units * 0.4))
    data = await call_llm_json(
        prompt,
        validate=lambda d: isinstance(d, dict) and bool(d.get("title"))
        and language.count_units(str(d.get("script", ""))) >= minimum,
    )
    if not data or not isinstance(data, dict) or not data.get("script"):
        raise RuntimeError("The AI did not return a usable script")
    return data


async def _scene_by_scene(video, language, sources, target: int, avoid: str) -> dict:
    """Plan the video, then write each scene separately to its word budget."""
    scene_count = max(4, min(14, round(target / 75)))
    scene_seconds = target / scene_count
    outline = await call_llm_json(
        _OUTLINE_PROMPT.format(
            minutes=round(target / 60),
            language=language.name,
            today=date.today().strftime("%d %B %Y"),
            topic=_topic_line(video),
            category=video.category,
            sources=research_service.format_sources(sources),
            scenes=scene_count,
            scene_seconds=int(scene_seconds),
            avoid=avoid,
        ),
        validate=lambda d: isinstance(d, dict) and bool(d.get("title"))
        and isinstance(d.get("scenes"), list) and len(d["scenes"]) >= 3,
    )
    scenes = [sc for sc in outline["scenes"] if isinstance(sc, dict)]
    by_id = {src.id: src for src in sources}
    target_units = language.units_for(scene_seconds)
    units = int(target_units * 1.3)  # models reliably write ~25% under the requested length

    parts, previous_text = [], ""
    for number, scene in enumerate(scenes, start=1):
        ids = [i for i in scene.get("source_ids", []) if isinstance(i, int) and i in by_id]
        scene_sources = [by_id[i] for i in ids] or sources
        if number == 1:
            position = "This is the opening scene: start with the most surprising TRUE fact as the hook."
        elif number == len(scenes):
            position = "This is the final scene: wrap up the key facts and end with a call to subscribe."
        else:
            position = "Continue naturally from the previous scene; do not re-introduce the video."
        data = await call_llm_json(
            _SCENE_PROMPT.format(
                language=language.name,
                title=outline["title"],
                number=number,
                total=len(scenes),
                scene_title=scene.get("title", f"Part {number}"),
                facts="\n".join(f"- {f}" for f in scene.get("facts", [])) or "- (use the sources)",
                previous=f"The previous scene ended with: \"{previous_text[-300:]}\"\n" if previous_text else "",
                sources=research_service.format_sources(scene_sources),
                units=units,
                unit_name=language.unit_name,
                seconds=int(scene_seconds),
                position_rule=position,
            ),
            validate=lambda d: isinstance(d, dict)
            and language.count_units(str(d.get("narration", ""))) >= max(15, int(target_units * 0.4)),
        )
        narration = str(data["narration"]).strip()
        parts.append(f"Scene {number}: {scene.get('title', f'Part {number}')}\n{narration}")
        previous_text = narration
        logger.info("Scene {}/{} written ({} {})", number, len(scenes), language.count_units(narration),
                    language.unit_name)

    return {
        "title": outline["title"],
        "description": outline.get("description", ""),
        "tags": outline.get("tags", []),
        "hashtags": outline.get("hashtags", []),
        "image_prompts": [str(sc.get("image_prompt", "")) for sc in scenes],
        "script": "\n\n".join(parts),
    }


def compute_chapters(script: str, duration: float) -> str:
    """YouTube chapters from scene lengths, scaled to the real voice-over duration."""
    import re

    scenes = re.split(r"\n\s*\n(?=\s*Scene\s+\d+)", (script or "").strip())
    titled = []
    for scene in scenes:
        header, _, body = scene.partition("\n")
        m = re.match(r"\s*Scene\s+\d+\s*[:\-–]\s*(.*)", header)
        titled.append(((m.group(1).strip() if m else header.strip()) or "Part", max(1, len(body))))
    if len(titled) < 3:
        return ""
    total = sum(n for _, n in titled)
    lines, start = [], 0.0
    for title, n in titled:
        lines.append(f"{int(start // 60):02d}:{int(start % 60):02d} {title}")
        start += duration * n / total
    # YouTube needs ≥3 chapters, each ≥10 s.
    if any(duration * n / total < 10 for _, n in titled):
        return ""
    return "\n".join(lines)


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


async def check_script(db: AsyncSession, video_id: int) -> Video:
    """
    Fact-check a script the user wrote or edited. Their words are never
    changed; unsupported statements are only reported back.
    """
    video = await db.get(Video, video_id)
    if video is None:
        raise ValueError(f"Video {video_id} not found")
    language = _language_for(video)

    sources = research_service.load_sources(video_id)
    if sources is None:
        if not video.topic_en:
            video.topic_en = await research_service.english_topic(video.title or video.topic)
        sources = await research_service.gather_sources(video.topic_en, video.category, language,
                                                        video.target_seconds)
        research_service.save_sources(video_id, sources)

    _, check = await research_service.verify_script(video.script or "", sources, language.name, fix=False)
    video.sources = json.dumps(research_service.public_sources(sources), ensure_ascii=False)
    video.fact_check = check.to_json()
    await db.flush()
    await db.refresh(video)
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
