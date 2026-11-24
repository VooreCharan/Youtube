"""
Research & fact-checking for a facts-based channel.

  gather_sources(topic)        → Wikipedia passages + recent news headlines (free, no keys)
  fact_check(script, sources)  → statements in the script that the sources don't support
  revise(script, issues, ...)  → corrected script that only states supported facts

Scripts are written *from* these sources (see script_service) and checked
against them before the user ever sees the script.
"""

from __future__ import annotations

import asyncio
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import date
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote_plus

import httpx
from loguru import logger

from app.config import get_settings
from app.services.languages import Language
from app.services.llm_service import call_llm_json

_HEADERS = {"User-Agent": "YouTubeFactsBot/1.0 (educational video research)"}
_WIKI_API = "https://en.wikipedia.org/w/api.php"
_PASSAGE_BUDGET = 1800      # characters kept per Wikipedia article
_TOTAL_BUDGET = 6000        # characters of source text sent to the AI (free tiers allow ~8k tokens/min)
_MAX_WIKI_ARTICLES = 3
_MAX_HEADLINES = 10
_CHECK_CHUNK = 2200         # script characters fact-checked per request


@dataclass
class Source:
    id: int
    kind: str            # "wikipedia" | "news"
    title: str
    url: str
    publisher: str = ""
    published: str = ""  # YYYY-MM-DD for news
    text: str = ""

    def citation(self) -> str:
        bits = [self.title]
        if self.publisher:
            bits.append(self.publisher)
        if self.published:
            bits.append(self.published)
        return " — ".join(bits)


@dataclass
class FactCheck:
    sources: int = 0
    unsupported: List[dict] = field(default_factory=list)   # [{"quote", "problem"}] still in the script
    corrected: int = 0                                     # statements fixed automatically
    checked: bool = False
    error: str = ""                                        # why the check could not run

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: Optional[str]) -> Optional["FactCheck"]:
        if not raw:
            return None
        try:
            return cls(**json.loads(raw))
        except (ValueError, TypeError):
            return None


# ── Source collection ───────────────────────────────────────────────

_QUERY_PROMPT = """You prepare research for a fact-based YouTube video.
Topic: {topic}
Category: {category}

Return JSON:
{{"search_queries": ["2-3 short ENGLISH web search queries to find current news on this topic"],
  "wikipedia_titles": ["2-3 likely ENGLISH Wikipedia article titles with background facts"]}}
"""


async def gather_sources(topic: str, category: str, language: Language) -> List[Source]:
    """Collect Wikipedia passages and recent headlines relevant to *topic*."""
    plan = await call_llm_json(
        _QUERY_PROMPT.format(topic=topic, category=category),
        validate=lambda d: isinstance(d, dict) and d.get("search_queries"),
    )
    queries = [str(q) for q in plan.get("search_queries", [])][:3]
    wiki_titles = [str(t) for t in plan.get("wikipedia_titles", [])][:3]
    keywords = _keywords(" ".join(queries + wiki_titles + [topic]))
    news_keywords = _keywords(" ".join(queries + wiki_titles))

    async with httpx.AsyncClient(timeout=20, headers=_HEADERS, follow_redirects=True) as client:
        wiki, news = await asyncio.gather(
            _wikipedia(client, wiki_titles, queries, keywords),
            _news(client, queries, language, news_keywords),
        )

    sources: List[Source] = []
    used = 0
    for src in wiki + news:
        if used + len(src.text) > _TOTAL_BUDGET and sources:
            continue
        used += len(src.text)
        src.id = len(sources) + 1
        sources.append(src)

    logger.info(
        "Research for '{}': {} Wikipedia articles, {} news headlines",
        topic[:60], sum(s.kind == "wikipedia" for s in sources), sum(s.kind == "news" for s in sources),
    )
    return sources


async def _wikipedia(client, titles: List[str], queries: List[str], keywords: set) -> List[Source]:
    found: List[str] = []
    for query in titles + queries:
        if len(found) >= _MAX_WIKI_ARTICLES:
            break
        try:
            r = await client.get(_WIKI_API, params={
                "action": "query", "list": "search", "srsearch": query, "srlimit": 2, "format": "json",
            })
            for hit in r.json().get("query", {}).get("search", []):
                if hit["title"] not in found:
                    found.append(hit["title"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("Wikipedia search failed for '{}': {}", query, exc)

    results: List[Source] = []
    for title in found[:_MAX_WIKI_ARTICLES]:
        try:
            r = await client.get(_WIKI_API, params={
                "action": "query", "prop": "extracts|info", "inprop": "url", "explaintext": 1,
                "exsectionformat": "plain", "titles": title, "redirects": 1, "format": "json",
            })
            page = next(iter(r.json()["query"]["pages"].values()))
            text = _relevant_passages(page.get("extract", ""), keywords, _PASSAGE_BUDGET)
            if text:
                results.append(Source(0, "wikipedia", page.get("title", title), page.get("fullurl", ""),
                                      publisher="Wikipedia", text=text))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Wikipedia fetch failed for '{}': {}", title, exc)
    return results


async def _news(client, queries: List[str], language: Language, keywords: set) -> List[Source]:
    editions = [("en-US", "US", "US:en")]
    if language.news not in editions:
        editions.append(language.news)

    items: List[Source] = []
    seen = set()
    for query in queries[:2]:
        for hl, gl, ceid in editions:
            url = (f"https://news.google.com/rss/search?q={quote_plus(query + ' when:30d')}"
                   f"&hl={hl}&gl={gl}&ceid={ceid}")
            try:
                root = ET.fromstring((await client.get(url)).content)
            except Exception as exc:  # noqa: BLE001
                logger.warning("News search failed for '{}': {}", query, exc)
                continue
            for item in root.iter("item"):
                title = (item.findtext("title") or "").strip()
                publisher = (item.findtext("source") or "").strip()
                if publisher and title.endswith(f" - {publisher}"):
                    title = title[: -len(publisher) - 3]
                title = re.sub(r"\s*\|\s*Tap to know more.*$", "", title, flags=re.I).strip()
                if not title or title.lower() in seen:
                    continue
                # Loose searches return unrelated stories: keep headlines that
                # actually mention the topic.
                if keywords and not any(k in title.lower() for k in keywords):
                    continue
                seen.add(title.lower())
                published = ""
                try:
                    published = parsedate_to_datetime(item.findtext("pubDate") or "").date().isoformat()
                except (TypeError, ValueError):
                    pass
                items.append(Source(0, "news", title, (item.findtext("link") or "").strip(),
                                    publisher=publisher, published=published,
                                    text=f"Headline ({publisher}, {published}): {title}"))
    items.sort(key=lambda s: s.published, reverse=True)
    return items[:_MAX_HEADLINES]


def _keywords(text: str) -> set:
    stop = {"what", "with", "from", "that", "this", "have", "will", "about", "into", "their", "news", "latest"}
    return {w for w in re.findall(r"[a-z0-9]{4,}", text.lower()) if w not in stop}


def _relevant_passages(extract: str, keywords: set, budget: int) -> str:
    """Keep the intro plus the paragraphs that mention the most topic keywords."""
    paragraphs = [p.strip() for p in extract.split("\n") if len(p.strip()) > 80]
    if not paragraphs:
        return ""
    intro, rest = paragraphs[0], paragraphs[1:]
    scored = sorted(rest, key=lambda p: -sum(k in p.lower() for k in keywords))
    chosen, used = [intro[:budget]], len(intro)
    for p in scored:
        if used + len(p) > budget:
            continue
        if sum(k in p.lower() for k in keywords) == 0:
            break
        chosen.append(p)
        used += len(p)
    return "\n".join(chosen)


def format_sources(sources: List[Source]) -> str:
    """Numbered source block for prompts."""
    return "\n\n".join(f"[{s.id}] {s.citation()}\n{s.text}" for s in sources) or "(no sources found)"


# ── Persistence (so "Regenerate" doesn't redo the research) ──────────

def _cache_path(video_id: int) -> Path:
    return get_settings().scripts_dir / f"{video_id}.sources.json"


def save_sources(video_id: int, sources: List[Source]) -> None:
    _cache_path(video_id).write_text(json.dumps([asdict(s) for s in sources], ensure_ascii=False), encoding="utf-8")


def load_sources(video_id: int) -> Optional[List[Source]]:
    path = _cache_path(video_id)
    if not path.exists():
        return None
    try:
        return [Source(**s) for s in json.loads(path.read_text(encoding="utf-8"))]
    except (ValueError, TypeError):
        return None


def public_sources(sources: List[Source]) -> List[dict]:
    """What is stored on the video / shown to viewers (no source text)."""
    return [{"title": s.title, "url": s.url, "publisher": s.publisher, "published": s.published, "kind": s.kind}
            for s in sources]


# ── Fact-checking ───────────────────────────────────────────────────

_CHECK_PROMPT = """You are a strict fact-checker for a fact-based YouTube channel.

Today's date: {today}
SOURCES:
{sources}

SCRIPT (in {language}):
\"\"\"
{script}
\"\"\"

Find every sentence in the script that states a specific fact (number, date, name, event, quote,
cause/effect, ranking, "first/largest/only" claim) that is NOT supported by the sources above.
General, uncontroversial background (e.g. "drones are flying machines") is fine.
Opinions, questions and calls to action are fine.

Return JSON: {{"unsupported": [{{"quote": "exact sentence copied from the script", "problem": "short English reason"}}]}}
Return {{"unsupported": []}} if everything is supported.
"""

_REVISE_PROMPT = """You are editing a script for a fact-based YouTube channel.

SOURCES:
{sources}

These statements are NOT supported by the sources:
{issues}

SCRIPT (in {language}):
\"\"\"
{script}
\"\"\"

Rewrite the script so every factual statement is supported by the sources:
- Correct each listed statement using the sources, or remove it if the sources don't cover it.
- Do not add any new facts that are not in the sources.
- Keep the language ({language}), the tone, and the "Scene N: Title" structure.

Return JSON: {{"script": "the full corrected script"}}
"""


async def fact_check(script: str, sources: List[Source], language_name: str) -> List[dict]:
    """Return statements in *script* that the sources do not support."""
    data = await call_llm_json(
        _CHECK_PROMPT.format(
            today=date.today().strftime("%d %B %Y"),
            sources=format_sources(sources),
            language=language_name,
            script=script,
        ),
        validate=lambda d: isinstance(d, dict) and isinstance(d.get("unsupported"), list),
    )
    issues = []
    for item in data.get("unsupported", []):
        if isinstance(item, dict) and str(item.get("quote", "")).strip():
            issues.append({"quote": str(item["quote"]).strip()[:400], "problem": str(item.get("problem", ""))[:200]})
    return issues


async def revise(script: str, issues: List[dict], sources: List[Source], language_name: str) -> str:
    data = await call_llm_json(
        _REVISE_PROMPT.format(
            sources=format_sources(sources),
            issues="\n".join(f"- \"{i['quote']}\" ({i['problem']})" for i in issues),
            language=language_name,
            script=script,
        ),
        validate=lambda d: isinstance(d, dict) and len(str(d.get("script", "")).split()) >= 40,
    )
    return str(data["script"])


def _split_for_checking(script: str) -> List[str]:
    """Group whole scenes into chunks small enough for free-tier token limits."""
    scenes = re.split(r"\n\s*\n(?=\s*Scene\s+\d+)", script.strip())
    chunks: List[str] = []
    current = ""
    for scene in scenes:
        if current and len(current) + len(scene) > _CHECK_CHUNK:
            chunks.append(current)
            current = scene
        else:
            current = f"{current}\n\n{scene}" if current else scene
    if current:
        chunks.append(current)
    return chunks


async def verify_script(script: str, sources: List[Source], language_name: str, fix: bool) -> tuple:
    """
    Fact-check *script* section by section. With *fix*, sections containing
    unsupported statements are corrected (or those statements removed) and
    re-checked. Never raises: if the AI is unavailable the original script is
    returned with ``checked=False``. Returns ``(script, FactCheck)``.
    """
    result = FactCheck(sources=len(sources))
    if not sources:
        result.error = "no sources found for this topic"
        return script, result

    chunks = _split_for_checking(script)
    new_chunks: List[str] = []
    try:
        for chunk in chunks:
            issues = await fact_check(chunk, sources, language_name)
            if issues and fix:
                try:
                    revised = await revise(chunk, issues, sources, language_name)
                    remaining = await fact_check(revised, sources, language_name)
                    result.corrected += max(0, len(issues) - len(remaining))
                    chunk, issues = revised, remaining
                except Exception as exc:  # noqa: BLE001 — keep the section, report its issues
                    logger.warning("Could not revise a script section: {}", exc)
            result.unsupported.extend(issues)
            new_chunks.append(chunk)
    except Exception as exc:  # noqa: BLE001 — a failed check must never lose the script
        logger.warning("Fact-check could not run: {}", exc)
        result.error = "the AI fact-checker was busy — regenerate or edit to try again"
        result.unsupported = []
        result.corrected = 0
        return script, result

    result.checked = True
    logger.info("Fact-check done: {} sources, {} sections, {} corrected, {} still unsupported",
                len(sources), len(chunks), result.corrected, len(result.unsupported))
    return "\n\n".join(new_chunks), result
