"""
Loguru-based logging configuration.

Three log sinks:
  • logs/app.log      — all messages, rotated at 10 MB
  • logs/error.log    — ERROR and above, rotated at 5 MB
  • logs/upload.log   — upload-specific events (filtered by module name)
  • Console           — colourised, human-readable
"""

from __future__ import annotations

import sys
from loguru import logger

from app.config import get_settings


def _upload_filter(record) -> bool:
    """Only pass records from upload-related modules."""
    return "youtube" in record["name"].lower() or "upload" in record["name"].lower()


def setup_logging() -> None:
    """Remove default handlers and configure project sinks."""
    settings = get_settings()

    # Remove the default stderr handler
    logger.remove()

    log_dir = settings.logs_dir

    # Console
    logger.add(
        sys.stderr,
        level=settings.log_level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> — "
            "<level>{message}</level>"
        ),
        colorize=True,
    )

    # General application log
    logger.add(
        str(log_dir / "app.log"),
        level="DEBUG",
        rotation="10 MB",
        retention="30 days",
        compression="zip",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} — {message}",
    )

    # Error-only log
    logger.add(
        str(log_dir / "error.log"),
        level="ERROR",
        rotation="5 MB",
        retention="30 days",
        compression="zip",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} — {message}",
    )

    # Upload-specific log
    logger.add(
        str(log_dir / "upload.log"),
        level="INFO",
        rotation="5 MB",
        retention="30 days",
        compression="zip",
        filter=_upload_filter,
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} — {message}",
    )

    logger.info("Logging initialised (level={})", settings.log_level)
