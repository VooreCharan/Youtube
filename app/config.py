"""
Centralized configuration loaded from environment variables via pydantic-settings.
"""

from __future__ import annotations

import os
from pathlib import Path
from functools import lru_cache

from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    """Application-wide settings sourced from .env file."""

    # ── LLM chain ────────────────────────────────────────────────────
    # Providers are tried in this order; ones without a key are skipped.
    llm_chain: str = Field(
        default="groq,gemini,openrouter_free,github,openai,openrouter",
        description="Comma-separated provider priority order",
    )
    llm_timeout: float = Field(default=90, description="Seconds before a model is abandoned for the next one")
    llm_max_wait: float = Field(default=240, description="Total seconds one request may spend across the whole chain")
    llm_retry_rounds: int = Field(default=2, description="Extra passes over temporarily failing models")
    llm_retry_delay: float = Field(default=8, description="Seconds to wait before an extra pass")
    llm_provider: str = Field(default="openrouter", description="Legacy: 'ollama' puts Ollama first in the chain")

    # Keys (all have free tiers except paid OpenRouter models)
    gemini_api_key: str = Field(default="", description="Google AI Studio key — used for text and Veo")
    groq_api_key: str = Field(default="")
    github_token: str = Field(default="", description="GitHub token with models:read")
    openrouter_api_key: str = Field(default="", description="Used for free and paid OpenRouter models")
    openai_api_key: str = Field(default="", description="Paid OpenAI fallback")

    # Models per provider (comma-separated, tried in order)
    gemini_models: str = Field(
        default="gemini-flash-latest,gemini-3.5-flash,gemini-flash-lite-latest,gemini-3.1-flash-lite"
    )
    groq_models: str = Field(
        default="openai/gpt-oss-120b,qwen/qwen3.8-27b,openai/gpt-oss-20b",
        description="Each Groq model has its own free daily limit",
    )
    openrouter_free_models: str = Field(default="google/gemma-4-31b-it:free,nvidia/nemotron-3-ultra-550b-a55b:free")
    github_models: str = Field(default="openai/gpt-4.1-mini")
    openai_models: str = Field(default="gpt-5.4-mini,gpt-4.1-mini", description="Paid fallback models")
    openrouter_model: str = Field(default="google/gemini-2.5-pro", description="Paid last-resort model")
    ollama_model: str = Field(default="llama3")
    ollama_base_url: str = Field(default="http://localhost:11434")

    # ── YouTube ──────────────────────────────────────────────────────
    youtube_client_secrets_path: str = Field(default="client_secrets.json")

    # ── Telegram ─────────────────────────────────────────────────────
    telegram_bot_token: str = Field(default="")
    telegram_chat_id: str = Field(default="", description="Chat that receives scheduler notifications")
    telegram_allowed_users: str = Field(
        default="",
        description="Comma-separated Telegram user IDs allowed to use the bot",
    )
    topics_per_page: int = Field(default=8, description="Topic ideas shown per batch in the bot")

    # ── Publishing ───────────────────────────────────────────────────
    youtube_privacy: str = Field(
        default="public",
        description="Privacy used when the bot publishes an approved video: public | unlisted | private",
    )

    # ── Media Generation ─────────────────────────────────────────────
    media_provider: str = Field(default="pexels", description="pexels | veo")
    pexels_api_key: str = Field(default="")

    # ── Whisper ──────────────────────────────────────────────────────
    whisper_model_size: str = Field(default="base", description="Local fallback model")
    groq_whisper_model: str = Field(
        default="whisper-large-v3",
        description="Hosted speech-to-text on Groq (used first when GROQ_API_KEY is set; empty = local only)",
    )

    # ── TTS ──────────────────────────────────────────────────────────
    default_voice: str = Field(default="en-US-AvaNeural")

    # ── Video ────────────────────────────────────────────────────────
    default_video_format: str = Field(default="horizontal")

    # ── Database ─────────────────────────────────────────────────────
    db_path: str = Field(default="data/youtube_automation.db")

    # ── Logging ──────────────────────────────────────────────────────
    log_level: str = Field(default="INFO")

    # ── Scheduler ────────────────────────────────────────────────────
    enable_scheduler: bool = Field(
        default=False,
        description="Run the unattended daily generate/upload jobs (bypasses Telegram approval)",
    )
    generation_hour: int = Field(default=8)
    upload_hour: int = Field(default=10)

    # ── Server ───────────────────────────────────────────────────────
    api_host: str = Field(default="0.0.0.0")
    api_port: int = Field(default=8000)

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
        "extra": "ignore",  # unrelated variables in .env must not crash the app
    }

    # ── Derived paths ────────────────────────────────────────────────
    @property
    def base_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent

    @property
    def generated_dir(self) -> Path:
        return self.base_dir / "generated"

    @property
    def scripts_dir(self) -> Path:
        return self.generated_dir / "scripts"

    @property
    def audio_dir(self) -> Path:
        return self.generated_dir / "audio"

    @property
    def images_dir(self) -> Path:
        return self.generated_dir / "images"

    @property
    def videos_dir(self) -> Path:
        return self.generated_dir / "videos"

    @property
    def subtitles_dir(self) -> Path:
        return self.generated_dir / "subtitles"

    @property
    def thumbnails_dir(self) -> Path:
        return self.generated_dir / "thumbnails"

    @property
    def voice_samples_dir(self) -> Path:
        return self.generated_dir / "voice_samples"

    @property
    def uploads_dir(self) -> Path:
        return self.generated_dir / "uploads"

    @property
    def previews_dir(self) -> Path:
        return self.generated_dir / "previews"

    @property
    def allowed_user_ids(self) -> set[int]:
        ids: set[int] = set()
        for part in self.telegram_allowed_users.replace(";", ",").split(","):
            part = part.strip()
            if part.lstrip("-").isdigit():
                ids.add(int(part))
        return ids

    @property
    def logs_dir(self) -> Path:
        return self.base_dir / "logs"

    @property
    def data_dir(self) -> Path:
        return self.base_dir / "data"

    @property
    def db_url(self) -> str:
        db = self.base_dir / self.db_path
        return f"sqlite+aiosqlite:///{db}"

    def ensure_directories(self) -> None:
        """Create all required output directories if they don't exist."""
        for d in (
            self.scripts_dir,
            self.audio_dir,
            self.images_dir,
            self.videos_dir,
            self.subtitles_dir,
            self.thumbnails_dir,
            self.voice_samples_dir,
            self.uploads_dir,
            self.previews_dir,
            self.logs_dir,
            self.data_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings singleton."""
    settings = Settings()
    settings.ensure_directories()
    return settings
