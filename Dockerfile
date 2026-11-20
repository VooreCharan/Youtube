# ─── Build Stage ───────────────────────────────────────────────────
FROM python:3.11-slim AS base

# System dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY . .

# Create output directories
RUN mkdir -p generated/scripts generated/audio generated/images \
    generated/videos generated/subtitles generated/thumbnails \
    logs data

# Expose ports
EXPOSE 8000 8501

# Default: run FastAPI
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
