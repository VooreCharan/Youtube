"""
Check every AI provider in the chain.

    python -m app.llm_check

Sends a tiny prompt to each configured model and reports which ones work,
so you can fix keys / model names before relying on the fallback chain.
"""

import asyncio
import time

from app.config import get_settings
from app.services.llm_service import _complete, _cooldown_for, _providers, build_chain, parse_json

_PROMPT = 'Reply with exactly this JSON and nothing else: {"ok": true, "telugu": "నమస్కారం"}'


async def main() -> None:
    settings = get_settings()
    chain = build_chain()
    configured = {t.provider.name for t in chain}

    print("Priority order:", settings.llm_chain, "\n")
    for name, provider in _providers().items():
        if name not in [n.strip() for n in settings.llm_chain.split(",")]:
            continue
        if name not in configured:
            print(f"⚪ {name:16} skipped — no key set")

    working = 0
    for target in chain:
        started = time.monotonic()
        try:
            text = await _complete(target, _PROMPT, min(settings.llm_timeout, 60))
            data = parse_json(text)
            ok = isinstance(data, dict) and data.get("ok") is True
            status = "✅ works" if ok else f"⚠️  answered but not valid JSON: {text[:60]!r}"
            working += ok
        except Exception as exc:  # noqa: BLE001
            status = f"❌ {_cooldown_for(exc)[1][:70]}"
        print(f"{status:45}  {target.key}  ({time.monotonic() - started:.1f}s)")

    print(f"\n{working}/{len(chain)} models working.", "" if working else "⚠️  The bot cannot write scripts until at least one works.")


if __name__ == "__main__":
    asyncio.run(main())
