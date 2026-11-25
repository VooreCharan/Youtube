"""
Trending topic discovery.

1. Pull the latest headlines for a category from Google News RSS (free, no key),
   using the edition that matches the user's language.
2. Ask the LLM to turn those headlines into YouTube video ideas.

If the news fetch fails, the LLM still produces ideas from its own knowledge,
so the user always gets a list.
"""

from __future__ import annotations

import random
from datetime import date
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Dict, List, Optional
from urllib.parse import quote_plus

import httpx
from loguru import logger

from app.services.languages import Language
from app.services.llm_service import call_llm_json


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    emoji: str
    news_section: Optional[str] = None   # Google News topic section
    search_query: Optional[str] = None   # Google News search (for categories without a section)

    @property
    def button_label(self) -> str:
        return f"{self.emoji} {self.label}"


CATEGORIES: Dict[str, Category] = {
    c.key: c
    for c in [
        Category("tech", "Technology", "💻", news_section="TECHNOLOGY"),
        Category("ai", "AI", "🤖", search_query="artificial intelligence"),
        Category("business", "Business & Finance", "💰", news_section="BUSINESS"),
        Category("science", "Science", "🔬", news_section="SCIENCE"),
        Category("health", "Health", "🩺", news_section="HEALTH"),
        Category("entertainment", "Entertainment", "🎬", news_section="ENTERTAINMENT"),
        Category("sports", "Sports", "🏏", news_section="SPORTS"),
        Category("world", "World News", "🌍", news_section="WORLD"),
        Category("space", "Space", "🚀", search_query="NASA OR ISRO OR ESA OR space mission OR astronomers"),
        Category("history", "History", "🏛", search_query="archaeologists OR historians OR ancient discovery"),
        Category("nature", "Animals & Nature", "🐾", search_query="new species OR wildlife study OR scientists animals"),
        Category("body", "Human Body", "🧠", search_query="study finds human brain OR body OR sleep research"),
        Category("education", "Education", "📚", search_query="education OR exam OR students"),
    ]
}

_NEWS_BASE = "https://news.google.com/rss"
_ENGLISH_EDITION = ("en-US", "US", "US:en")


def _news_url(category: Category, edition: tuple) -> str:
    hl, gl, ceid = edition
    params = f"hl={hl}&gl={gl}&ceid={ceid}"
    if category.news_section:
        return f"{_NEWS_BASE}/headlines/section/topic/{category.news_section}?{params}"
    query = quote_plus(f"{category.search_query} when:7d")
    return f"{_NEWS_BASE}/search?q={query}&{params}"


async def fetch_headlines(category: Category, language: Language, limit: int = 40) -> List[str]:
    """Return up to *limit* recent headlines for the category (may be empty)."""
    editions = [language.news]
    if language.news != _ENGLISH_EDITION:
        editions.append(_ENGLISH_EDITION)  # regional editions can be thin

    headlines: List[str] = []
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for edition in editions:
            url = _news_url(category, edition)
            try:
                resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (YouTubeAutomation)"})
                resp.raise_for_status()
                root = ET.fromstring(resp.content)
            except Exception as exc:
                logger.warning("Google News fetch failed ({}): {}", url, exc)
                continue

            for item in root.iter("item"):
                title = (item.findtext("title") or "").strip()
                if " - " in title:
                    title = title.rsplit(" - ", 1)[0]  # drop " - Publisher"
                if title and title not in headlines:
                    headlines.append(title)
            if len(headlines) >= 15:
                break

    logger.info("Fetched {} headlines for {} / {}", len(headlines), category.label, language.name)
    return headlines[:limit]


_TOPIC_PROMPT = """You are a YouTube content strategist.

Today's date: {today} (topics must feel current as of this date; never refer to past years as upcoming)
Category: {category}
Audience language: {language}
Today's latest headlines in this category:
{headlines}

Suggest {count} fresh, specific YouTube video topics for a FACT-BASED channel, based on what is
trending right now{headline_hint}. Each topic must work as a 4-7 minute factual explainer.

Rules:
- Topics must be about verifiable facts, events, discoveries or explanations — no rumours, gossip,
  predictions presented as fact, or opinion pieces.
- Titles must be accurate: they may be intriguing, but must not promise anything the facts don't support.
- Write every "title" in {language}.
- Titles: punchy, curiosity-driven, max 90 characters.
- "why" is one short English sentence on why this will get views now.
- "title_en" is the exact English meaning of the title (same subject — e.g. the same planet, person, place).
- Do NOT repeat or rephrase any of these already-shown topics:
{exclude}

Respond as a JSON array:
[{{"title": "...", "title_en": "...", "why": "..."}}, ...]
"""


def _topic_list(data) -> list:
    """Accept either a bare JSON array or {"topics": [...]}."""
    if isinstance(data, dict):
        data = data.get("topics") or next((v for v in data.values() if isinstance(v, list)), [])
    return data if isinstance(data, list) else []


async def suggest_topics(
    category: Category,
    language: Language,
    exclude: Optional[List[str]] = None,
    count: int = 8,
) -> List[dict]:
    """Return a list of ``{"title", "why"}`` topic ideas."""
    headlines = await fetch_headlines(category, language)
    random.shuffle(headlines)  # different mix on every "More topics"

    prompt = _TOPIC_PROMPT.format(
        today=date.today().strftime("%d %B %Y"),
        category=category.label,
        language=language.name,
        headlines="\n".join(f"- {h}" for h in headlines[:30]) or "- (headlines unavailable)",
        headline_hint=" (use the headlines above)" if headlines else " (use your own knowledge of current trends)",
        count=count,
        exclude="\n".join(f"- {t}" for t in (exclude or [])) or "- (none)",
    )

    data = _topic_list(await call_llm_json(prompt, validate=lambda d: len(_topic_list(d)) >= 3))

    topics: List[dict] = []
    seen = {t.strip().lower() for t in (exclude or [])}
    for item in data or []:
        if isinstance(item, str):
            item = {"title": item, "why": ""}
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip().strip('"')
        if title and title.lower() not in seen:
            seen.add(title.lower())
            topics.append({
                "title": title[:200],
                "title_en": str(item.get("title_en", "") or (title if language.code == "en" else "")).strip()[:200],
                "why": str(item.get("why", "")).strip()[:200],
            })

    if not topics:
        raise RuntimeError("The AI did not return any topic ideas. Please try again.")
    return topics[:count]
