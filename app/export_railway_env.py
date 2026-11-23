"""
Build the variables to paste into Railway.

    python -m app.export_railway_env

Reads your local .env, token.pickle and client_secrets.json and writes
``railway.env`` (git-ignored): clean KEY=value lines with the YouTube files
encoded as variables and STORAGE_DIR pointing at the Railway volume.
Paste it into Railway → your service → Variables → Raw Editor.
Values are never printed.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "railway.env"

# Local-only settings that must not be copied to the server.
_SKIP = {"STORAGE_DIR", "DB_PATH", "YOUTUBE_CLIENT_SECRETS_PATH", "OLLAMA_BASE_URL", "API_HOST", "API_PORT"}


def main() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        sys.exit("No .env file found in the project folder.")

    values = {k: (v or "").strip() for k, v in dotenv_values(env_file).items()}
    out = {k: v for k, v in values.items() if v and k.upper() not in _SKIP}
    out["STORAGE_DIR"] = "/data"

    token = ROOT / "token.pickle"
    secrets = ROOT / "client_secrets.json"
    missing = [p.name for p in (token, secrets) if not p.exists()]
    if missing:
        sys.exit(f"Missing {', '.join(missing)} — run `python -m app.youtube_auth` first.")
    out["YOUTUBE_TOKEN_B64"] = base64.b64encode(token.read_bytes()).decode()
    out["YOUTUBE_CLIENT_SECRETS_JSON"] = base64.b64encode(secrets.read_bytes()).decode()

    for key, value in out.items():
        if any(c in value for c in "\n\r\"'"):
            sys.exit(f"{key} contains quotes or line breaks — fix it in .env first.")

    OUT.write_text("".join(f"{k}={v}\n" for k, v in out.items()), encoding="utf-8")
    OUT.chmod(0o600)

    print(f"✅ Wrote {OUT.name} with {len(out)} variables:")
    for key in out:
        print(f"   • {key}")
    print("\nOpen it, copy everything, and paste into Railway → Variables → Raw Editor.")
    print("It contains secrets: don't share it. It's git-ignored.")


if __name__ == "__main__":
    main()
