"""
Recreate secret files from environment variables at startup.

Hosts like Railway can't receive file uploads, and these files must never be
committed to git. Instead:

  YOUTUBE_CLIENT_SECRETS_JSON  → client_secrets.json   (raw JSON or base64)
  YOUTUBE_TOKEN_B64            → token.pickle          (base64)

Both are written to the storage folder. The token is only overwritten when the
environment value changes, so a token refreshed by the app (saved on the
persistent volume) isn't replaced by an older copy on every restart.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json

from loguru import logger

from app.config import get_settings


def materialize_secret_files() -> None:
    settings = get_settings()
    settings.storage_root.mkdir(parents=True, exist_ok=True)

    if settings.youtube_client_secrets_json:
        raw = settings.youtube_client_secrets_json.strip()
        if not raw.startswith("{"):
            raw = _b64decode(raw, "YOUTUBE_CLIENT_SECRETS_JSON").decode("utf-8")
        json.loads(raw)  # fail loudly on a bad paste rather than at upload time
        path = settings.client_secrets_file
        path.write_text(raw, encoding="utf-8")
        path.chmod(0o600)
        logger.info("client_secrets.json written from environment")

    if settings.youtube_token_b64:
        token = _b64decode(settings.youtube_token_b64.strip(), "YOUTUBE_TOKEN_B64")
        digest = hashlib.sha256(token).hexdigest()
        marker = settings.storage_root / ".youtube_token_env.sha256"
        previous = marker.read_text().strip() if marker.exists() else None
        if digest != previous or not settings.token_path.exists():
            settings.token_path.write_bytes(token)
            settings.token_path.chmod(0o600)
            marker.write_text(digest)
            logger.info("token.pickle written from YOUTUBE_TOKEN_B64")


def _b64decode(value: str, name: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SystemExit(f"{name} is not valid base64 — regenerate it with `python -m app.export_railway_env`.") from exc
