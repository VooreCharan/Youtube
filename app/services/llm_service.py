"""
LLM layer with automatic failover across providers.

Every provider below exposes an OpenAI-compatible API, so one client
implementation covers all of them. A call walks the chain in priority order
(free, high-quality providers first; paid OpenRouter last) and returns the
first good answer:

  • providers without an API key are skipped
  • a failing model is put on a cooldown (rate limit → ~1 min, bad key → 30 min,
    unknown model → 6 h) so later calls don't waste time on it
  • for JSON calls, an unparseable / incomplete answer also moves on to the next model

Order and models are configurable in .env (LLM_CHAIN, <PROVIDER>_MODELS).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from loguru import logger

from app.config import get_settings
from app.utils.helpers import extract_json_from_text


class LLMUnavailableError(RuntimeError):
    """Raised when every provider in the chain failed."""


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    api_key: str
    models: Tuple[str, ...]
    headers: Tuple[Tuple[str, str], ...] = ()


@dataclass
class Target:
    provider: Provider
    model: str

    @property
    def key(self) -> str:
        return f"{self.provider.name}/{self.model}"


@dataclass
class _Health:
    cooldown_until: Dict[str, float] = field(default_factory=dict)
    last_error: Dict[str, str] = field(default_factory=dict)
    transient: Dict[str, bool] = field(default_factory=dict)  # last failure was temporary


_health = _Health()
_clients: Dict[str, Any] = {}


def _split(value: str) -> Tuple[str, ...]:
    return tuple(m.strip() for m in (value or "").split(",") if m.strip())


def _providers() -> Dict[str, Provider]:
    s = get_settings()
    return {
        "gemini": Provider(
            "gemini", "https://generativelanguage.googleapis.com/v1beta/openai/",
            s.gemini_api_key, _split(s.gemini_models),
        ),
        "groq": Provider("groq", "https://api.groq.com/openai/v1", s.groq_api_key, _split(s.groq_models)),
        "openrouter_free": Provider(
            "openrouter_free", "https://openrouter.ai/api/v1", s.openrouter_api_key,
            _split(s.openrouter_free_models), (("X-Title", "YouTube Automation"),),
        ),
        "github": Provider("github", "https://models.github.ai/inference", s.github_token, _split(s.github_models)),
        "openai": Provider("openai", "https://api.openai.com/v1", s.openai_api_key, _split(s.openai_models)),
        "openrouter": Provider(
            "openrouter", "https://openrouter.ai/api/v1", s.openrouter_api_key,
            _split(s.openrouter_model), (("X-Title", "YouTube Automation"),),
        ),
        "ollama": Provider(
            "ollama", s.ollama_base_url.rstrip("/") + "/v1", "ollama", _split(s.ollama_model),
        ),
    }


def build_chain() -> List[Target]:
    """Ordered list of (provider, model) targets that have credentials."""
    s = get_settings()
    providers = _providers()
    order = list(_split(s.llm_chain))
    if s.llm_provider == "ollama" and "ollama" not in order:
        order.insert(0, "ollama")  # backwards compatibility with LLM_PROVIDER=ollama

    targets: List[Target] = []
    for name in order:
        provider = providers.get(name)
        if provider is None:
            logger.warning("Unknown provider '{}' in LLM_CHAIN — ignored", name)
            continue
        if not provider.api_key or not provider.models:
            continue
        targets.extend(Target(provider, model) for model in provider.models)
    return targets


def _client(provider: Provider):
    if provider.name not in _clients:
        from openai import AsyncOpenAI

        _clients[provider.name] = AsyncOpenAI(
            base_url=provider.base_url,
            api_key=provider.api_key,
            max_retries=0,  # the chain is the retry strategy
            default_headers=dict(provider.headers) or None,
        )
    return _clients[provider.name]


@dataclass
class _Failure:
    cooldown: float      # seconds to rest this target (0 = none)
    reason: str
    whole_provider: bool  # bad key / no credits → skip all of the provider's models
    transient: bool       # worth retrying after a short pause (overload, rate limit, timeout)


# Phrases that mean "this account has no money". Deliberately specific: ordinary
# rate-limit messages often link to billing pages (e.g. Groq's "upgrade" hint).
_NO_CREDIT_MARKERS = ("insufficient_quota", "credit_balance", "no credits remaining", "exceeded your current quota")


def _is_out_of_credits(exc: Exception) -> bool:
    """Distinguish 'account has no money' 429s from ordinary rate limits."""
    body = getattr(exc, "body", None)
    text = (json.dumps(body) if isinstance(body, (dict, list)) else str(body or "")) + str(exc)
    return any(marker in text.lower() for marker in _NO_CREDIT_MARKERS)


def _classify(exc: Exception) -> _Failure:
    status = getattr(exc, "status_code", None)
    name = exc.__class__.__name__
    if name == "_PacingSkip":
        return _Failure(0, "waiting for per-minute token budget", False, True)
    if status in (401, 403) or name in ("AuthenticationError", "PermissionDeniedError"):
        return _Failure(30 * 60, f"bad API key / no access ({status})", True, False)
    if status == 402:
        return _Failure(30 * 60, "out of credits (402)", True, False)
    if status == 429 and _is_out_of_credits(exc):
        return _Failure(30 * 60, "no credits left on this account (429)", True, False)
    if status == 429 or name == "RateLimitError":
        retry_after = 60.0
        response = getattr(exc, "response", None)
        if response is not None:
            try:
                retry_after = min(float(response.headers.get("retry-after", 60)), 15 * 60)
            except (TypeError, ValueError):
                pass
        return _Failure(retry_after, "rate limited / quota (429)", False, True)
    if status == 404 or name == "NotFoundError":
        return _Failure(6 * 3600, "model not found (404)", False, False)
    if status == 400:
        return _Failure(0, f"rejected request (400): {str(exc)[:120]}", False, False)
    if status in (500, 502, 503, 504):
        return _Failure(60, f"provider overloaded / down ({status})", False, True)
    if isinstance(exc, asyncio.TimeoutError) or "Timeout" in name:
        return _Failure(60, "timed out", False, True)
    if "Connection" in name:
        return _Failure(30, "connection failed", False, True)
    return _Failure(30, f"{name}: {str(exc)[:120]}", False, True)


def _cooldown_for(exc: Exception) -> Tuple[float, str, bool]:
    f = _classify(exc)
    return f.cooldown, f.reason, f.whole_provider


def _on_cooldown(target: Target, now: float) -> bool:
    return max(
        _health.cooldown_until.get(target.key, 0),
        _health.cooldown_until.get(target.provider.name, 0),
    ) > now


class _TokenPacer:
    """
    Client-side tokens-per-minute budget per Groq model (free tier: 8k TPM).

    Bursts of requests (e.g. writing 8 scenes) would otherwise hit 429s and
    push work onto slower, often-overloaded backups. When a model needs a few
    seconds to refill, it's cheaper to wait than to fall through.
    """

    def __init__(self):
        self._tokens: Dict[str, float] = {}
        self._stamp: Dict[str, float] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    def _refill(self, key: str, capacity: float) -> float:
        now = time.monotonic()
        tokens = self._tokens.get(key, capacity)
        tokens = min(capacity, tokens + (now - self._stamp.get(key, now)) * capacity / 60)
        self._tokens[key], self._stamp[key] = tokens, now
        return tokens

    async def acquire(self, key: str, need: float, capacity: float, max_wait: float) -> bool:
        need = min(need, capacity)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            tokens = self._refill(key, capacity)
            if tokens < need:
                wait = (need - tokens) * 60 / capacity
                if wait > max_wait:
                    return False
                logger.info("Pacing {}: waiting {:.0f}s for its per-minute token budget", key, wait)
                await asyncio.sleep(wait)
                self._refill(key, capacity)
            self._tokens[key] -= need
            return True

    def settle(self, key: str, estimated: float, used: float, capacity: float) -> None:
        self._refill(key, capacity)
        self._tokens[key] = min(capacity, self._tokens[key] + estimated - used)

    def ready(self, key: str, need: float, capacity: float) -> bool:
        """Would *need* tokens be available right now (no waiting)?"""
        return self._refill(key, capacity) >= min(need, capacity)

    def exhaust(self, key: str) -> None:
        self._tokens[key] = 0.0
        self._stamp[key] = time.monotonic()


_pacer = _TokenPacer()


def _estimate_tokens(prompt: str) -> float:
    """Conservative token estimate (non-Latin scripts use more tokens per character)."""
    non_ascii = sum(1 for ch in prompt if ord(ch) > 127)
    return (len(prompt) - non_ascii) / 3.5 + non_ascii / 1.5 + 1200  # + typical answer


def _request_options(target: Target) -> dict:
    """Provider-specific options.

    Groq's default answer length (~3k tokens) truncates long JSON (outlines,
    scripts), and its gpt-oss models spend part of that budget "thinking" —
    so ask for longer answers with low reasoning effort. The limit stays
    under Groq's free 8k-tokens-per-minute so a request can't be rejected
    as too large.
    """
    options: dict = {}
    if target.provider.name == "groq":
        options["max_tokens"] = get_settings().groq_max_tokens
        if "gpt-oss" in target.model:
            options["reasoning_effort"] = "low"
    return options


async def _complete(target: Target, prompt: str, timeout: float) -> str:
    paced = target.provider.name == "groq"
    settings = get_settings()
    estimate = _estimate_tokens(prompt)
    if paced and not await _pacer.acquire(target.key, estimate, settings.groq_tpm, settings.groq_max_pace_wait):
        raise _PacingSkip(f"{target.key} needs more time to refill its per-minute token budget")
    try:
        response = await _create(target, prompt, timeout)
    except Exception as exc:
        if paced and getattr(exc, "status_code", None) == 429:
            _pacer.exhaust(target.key)
        raise
    if paced and getattr(response, "usage", None):
        _pacer.settle(target.key, estimate, response.usage.total_tokens or estimate, settings.groq_tpm)
    if not response.choices:
        return ""
    choice = response.choices[0]
    if getattr(choice, "finish_reason", None) == "length":
        logger.warning("LLM {} hit its answer-length limit; the answer may be cut off", target.key)
    return (choice.message.content or "").strip()


class _PacingSkip(RuntimeError):
    """A paced model would need too long to refill; try the next one."""


async def _create(target: Target, prompt: str, timeout: float):
    return await asyncio.wait_for(
        _client(target.provider).chat.completions.create(
            model=target.model,
            messages=[{"role": "user", "content": prompt}],
            **_request_options(target),
        ),
        timeout=timeout,
    )


async def _run_chain(prompt: str, accept: Callable[[str], Any], prefer: Optional[List[str]] = None) -> Any:
    """
    Try each target until *accept(text)* returns a non-None value.
    *accept* returns None to reject an answer (e.g. invalid JSON) without cooldown.
    """
    settings = get_settings()
    chain = build_chain()
    if prefer:
        # Task-specific order (e.g. translation): preferred providers first, the rest after.
        rank = {name: i for i, name in enumerate(prefer)}
        chain = sorted(chain, key=lambda t: rank.get(t.provider.name, len(prefer)))
    if not chain:
        raise LLMUnavailableError(
            "No AI provider is configured. Add at least one key to .env "
            "(GROQ_API_KEY, GEMINI_API_KEY, OPENROUTER_API_KEY, GITHUB_TOKEN or OPENAI_API_KEY)."
        )

    now = time.monotonic()
    first_round = [t for t in chain if not _on_cooldown(t, now)]
    if not first_round:
        # Everything is cooling down: try them anyway rather than failing outright.
        logger.warning("All LLM targets are on cooldown — retrying the full chain")
        first_round = chain

    # Models resting after a *temporary* failure (overload, rate limit) are skipped
    # at first, but get another chance if everything else fails too.
    rested = [t for t in chain if t not in first_round and _health.transient.get(t.key)]

    # Among Groq models, ones that can answer right now go before ones that
    # would have to wait for their per-minute budget (spreads bursts across
    # all Groq models instead of queueing on the first).
    estimate = _estimate_tokens(prompt)
    tpm = settings.groq_tpm
    first_round = sorted(
        first_round,
        key=lambda t: (
            chain.index(t) if t.provider.name != "groq"
            else min(chain.index(x) for x in chain if x.provider.name == "groq")
            + (0 if _pacer.ready(t.key, estimate, tpm) else 0.5) + chain.index(t) * 1e-3
        ),
    )

    failures: List[str] = []
    candidates = first_round
    deadline = time.monotonic() + settings.llm_max_wait
    for round_no in range(1 + max(0, settings.llm_retry_rounds)):
        if round_no:
            if not candidates or time.monotonic() + settings.llm_retry_delay >= deadline:
                break
            logger.warning(
                "All AI providers failed — waiting {:.0f}s, then retrying {} temporarily failing model(s)",
                settings.llm_retry_delay, len(candidates),
            )
            await asyncio.sleep(settings.llm_retry_delay)

        retry_next: List[Target] = []
        dead_providers: set = set()  # bad key / no credits → skip its other models this round
        for target in candidates:
            if target.provider.name in dead_providers:
                continue
            remaining = deadline - time.monotonic()
            if remaining < 5:
                failures.append(f"{target.key}: not tried (time limit reached)")
                break
            started = time.monotonic()
            try:
                text = await _complete(target, prompt, min(settings.llm_timeout, remaining))
            except Exception as exc:  # noqa: BLE001 — any failure moves to the next target
                failure = _classify(exc)
                key = target.provider.name if failure.whole_provider else target.key
                if failure.cooldown:
                    _health.cooldown_until[key] = time.monotonic() + failure.cooldown
                _health.last_error[target.key] = failure.reason
                _health.transient[target.key] = failure.transient
                if failure.whole_provider:
                    dead_providers.add(target.provider.name)
                if failure.transient:
                    retry_next.append(target)
                failures.append(f"{target.key}: {failure.reason}")
                logger.warning("LLM {} failed ({}) → trying next", target.key, failure.reason)
                continue

            if not text:
                failures.append(f"{target.key}: empty answer")
                retry_next.append(target)
                logger.warning("LLM {} returned an empty answer → trying next", target.key)
                continue

            result = accept(text)
            if result is None:
                failures.append(f"{target.key}: unusable answer")
                logger.warning("LLM {} answer was not usable → trying next", target.key)
                continue

            _health.last_error.pop(target.key, None)
            _health.transient.pop(target.key, None)
            _health.cooldown_until.pop(target.key, None)
            logger.info("LLM answer from {} in {:.1f}s", target.key, time.monotonic() - started)
            return result

        candidates = retry_next + rested
        rested = []

    # Show each target's latest failure once.
    latest: Dict[str, str] = {}
    for entry in failures:
        target_key, _, reason = entry.partition(": ")
        latest[target_key] = reason
    summary = "; ".join(f"{k}: {v}" for k, v in latest.items())
    raise LLMUnavailableError(f"All AI providers failed — {summary}")


# ── Public API (signatures unchanged for existing callers) ──────────

async def call_llm(prompt: str, *, json_mode: bool = False) -> str:
    """Send *prompt* through the provider chain and return the response text."""
    if json_mode:
        prompt += _JSON_SUFFIX
    return await _run_chain(prompt, lambda text: text)


async def call_llm_json(
    prompt: str,
    validate: Optional[Callable[[Any], bool]] = None,
    prefer: Optional[List[str]] = None,
) -> dict | list | None:
    """
    Call the chain and parse JSON. If a model returns invalid JSON, or JSON
    that fails *validate*, the next model is tried.
    """

    def accept(text: str):
        data = parse_json(text)
        if data is None:
            return None
        if validate is not None:
            try:
                if not validate(data):
                    return None
            except Exception:  # noqa: BLE001
                return None
        return data

    return await _run_chain(prompt + _JSON_SUFFIX, accept, prefer)


_JSON_SUFFIX = (
    "\n\nIMPORTANT: Respond with ONLY valid JSON. "
    "No markdown fences, no explanation, just the JSON."
)


def parse_json(text: str):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    extracted = extract_json_from_text(text or "")
    if extracted:
        try:
            return json.loads(extracted)
        except (json.JSONDecodeError, TypeError):
            pass
    return None


def chain_status() -> List[dict]:
    """Configured targets with their cooldown / last error (for diagnostics)."""
    now = time.monotonic()
    rows = []
    for t in build_chain():
        rows.append({
            "target": t.key,
            "cooling_down": _on_cooldown(t, now),
            "last_error": _health.last_error.get(t.key),
        })
    return rows
