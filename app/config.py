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

    # ── LLM ──────────────────────────────────────────────────────────
    llm_provider: str = Field(default="openrouter", description="openrouter | ollama")
    openrouter_api_key: str = Field(default="", description="OpenRouter API key")
    openrouter_model: str = Field(default="google/gemini-2.5-pro", description="OpenRouter model name")
    ollama_model: str = Field(default="llama3")
    ollama_base_url: str = Field(default="http://localhost:11434")

    # ── YouTube ──────────────────────────────────────────────────────
    youtube_client_secrets_path: str = Field(default="client_secrets.json")

    # ── Telegram ─────────────────────────────────────────────────────
    telegram_bot_token: str = Field(default="")
    telegram_chat_id: str = Field(default="")

    # ── Media Generation ─────────────────────────────────────────────
    media_provider: str = Field(default="pexels", description="pexels | veo")
    gemini_api_key: str = Field(default="", description="Gemini API Key for Veo")
    pexels_api_key: str = Field(default="")

    # ── Whisper ──────────────────────────────────────────────────────
    whisper_model_size: str = Field(default="base")

    # ── TTS ──────────────────────────────────────────────────────────
    default_voice: str = Field(default="en-US")

    # ── Video ────────────────────────────────────────────────────────
    default_video_format: str = Field(default="horizontal")

    # ── Database ─────────────────────────────────────────────────────
    db_path: str = Field(default="data/youtube_automation.db")

    # ── Logging ──────────────────────────────────────────────────────
    log_level: str = Field(default="INFO")

    # ── Scheduler ────────────────────────────────────────────────────
    generation_hour: int = Field(default=8)
    upload_hour: int = Field(default=10)

    # ── Server ───────────────────────────────────────────────────────
    api_host: str = Field(default="0.0.0.0")
    api_port: int = Field(default=8000)

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
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
