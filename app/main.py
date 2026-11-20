"""
FastAPI application entry point.

- Registers all routers
- Configures CORS
- Initialises the database and scheduler on startup
- Sets up Loguru logging
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from app.database import init_db
from app.routers import generation, upload
from app.scheduler.jobs import start_scheduler, shutdown_scheduler
from app.utils.logger import setup_logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle manager."""
    # ── Startup ──
    setup_logging()
    logger.info("Starting YouTube Automation API …")

    await init_db()
    logger.info("Database initialised")

    start_scheduler()
    logger.info("Scheduler started")

    yield

    # ── Shutdown ──
    shutdown_scheduler()
    logger.info("Scheduler stopped")
    logger.info("YouTube Automation API shutting down")


app = FastAPI(
    title="YouTube Automation API",
    description="AI-powered YouTube video generation & upload system",
    version="1.0.0",
    lifespan=lifespan,
)

# ── CORS ─────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ──────────────────────────────────────────────────────────
app.include_router(generation.router)
app.include_router(upload.router)


@app.get("/", tags=["health"])
async def root():
    return {
        "service": "YouTube Automation API",
        "status": "running",
        "docs": "/docs",
    }


@app.get("/health", tags=["health"])
async def health():
    return {"status": "healthy"}
