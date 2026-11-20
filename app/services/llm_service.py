"""
LLM abstraction layer – supports OpenAI and Ollama (local).
"""

from __future__ import annotations

import json
from typing import Optional

import httpx
from loguru import logger

from app.config import get_settings
from app.utils.helpers import extract_json_from_text


async def call_llm(prompt: str, *, json_mode: bool = False) -> str:
    """
    Send *prompt* to the configured LLM provider and return the response text.

    When *json_mode* is True the prompt is amended with an instruction to
    reply with valid JSON only.
    """
    settings = get_settings()

    if json_mode:
        prompt += (
            "\n\nIMPORTANT: Respond with ONLY valid JSON. "
            "No markdown fences, no explanation, just the JSON object."
        )

    if settings.llm_provider == "openrouter":
        return await _call_openrouter(prompt, settings)
    elif settings.llm_provider == "ollama":
        return await _call_ollama(prompt, settings)
    else:
        raise ValueError(f"Unsupported LLM provider: {settings.llm_provider}")


async def call_llm_json(prompt: str) -> dict | list | None:
    """Call the LLM and parse the response as JSON, with fallback extraction."""
    raw = await call_llm(prompt, json_mode=True)

    # Try direct parse first
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        pass

    # Try extracting JSON from surrounding text
    extracted = extract_json_from_text(raw)
    if extracted:
        try:
            return json.loads(extracted)
        except (json.JSONDecodeError, TypeError):
            pass

    logger.warning("Failed to parse LLM JSON response: {}", raw[:500])
    return None


# ── OpenRouter ───────────────────────────────────────────────────────

async def _call_openrouter(prompt: str, settings) -> str:
    """Call OpenRouter via the official openai Python SDK."""
    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=settings.openrouter_api_key,
        )
        response = await client.chat.completions.create(
            model=settings.openrouter_model,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content or ""
    except Exception as exc:
        logger.error("OpenRouter API call failed: {}", exc)
        raise


# ── Ollama ───────────────────────────────────────────────────────────

async def _call_ollama(prompt: str, settings) -> str:
    """Call a local Ollama instance."""
    url = f"{settings.ollama_base_url}/api/generate"
    payload = {
        "model": settings.ollama_model,
        "prompt": prompt,
        "stream": False,
    }
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data.get("response", "")
    except Exception as exc:
        logger.error("Ollama API call failed: {}", exc)
        raise
