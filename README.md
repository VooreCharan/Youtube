# 🎬 AI-Powered YouTube Video Upload Automation System

Complete automation system that generates content, creates videos, and uploads them to YouTube — built entirely on free/open-source tools.

![Python](https://img.shields.io/badge/Python-3.11+-blue?style=flat-square&logo=python)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green?style=flat-square&logo=fastapi)
![Streamlit](https://img.shields.io/badge/Streamlit-1.38-red?style=flat-square&logo=streamlit)
![License](https://img.shields.io/badge/License-MIT-yellow?style=flat-square)

---

## ✨ Features

- 🤖 **AI Script Generation** — fallback chain over 6 providers (Groq, Gemini, OpenRouter free, GitHub Models, then paid OpenAI and OpenRouter); if one fails, the next takes over
- 🎙️ **Text-to-Speech** — High-quality narration via Microsoft Edge-TTS (free, no API key)
- 🖼️ **Image Generation** — Pexels stock photos with gradient fallback (no GPU needed)
- 🎥 **Video Assembly** — MoviePy-based composition with transitions and subtitle burn-in
- 📝 **Auto-Subtitles** — Faster-Whisper transcription with SRT export
- 🖼️ **Thumbnail Generation** — Professional 1280×720 thumbnails with Pillow
- 📤 **YouTube Upload** — OAuth 2.0 with resumable uploads and exponential backoff
- 🤖 **Telegram Bot** — approve topic → script → voice → video → publish, in 15 languages
- 📅 **Scheduler** (optional, `ENABLE_SCHEDULER=true`) — unattended daily generation and upload
- 📱 **Telegram Notifications** — Real-time alerts on upload success/failure
- 🎨 **Streamlit Dashboard** — Visual pipeline control with history and log viewing

---

## 📁 Project Structure

```
youtube-automation/
├── app/
│   ├── __init__.py
│   ├── main.py               # FastAPI entry point
│   ├── config.py              # Pydantic settings
│   ├── database.py            # Async SQLAlchemy setup
│   ├── models/
│   │   ├── __init__.py
│   │   ├── video.py           # Video ORM model
│   │   └── schemas.py         # Pydantic request/response schemas
│   ├── services/
│   │   ├── __init__.py
│   │   ├── llm_service.py     # LLM abstraction (Gemini/Ollama)
│   │   ├── topic_service.py   # Topic generation
│   │   ├── trends_service.py  # Latest topics from Google News + LLM
│   │   ├── languages.py       # Languages & voices catalog
│   │   ├── script_service.py  # Script generation
│   │   ├── tts_service.py     # Text-to-Speech
│   │   ├── image_service.py   # Image generation
│   │   ├── subtitle_service.py # Subtitle generation
│   │   ├── video_service.py   # Video assembly
│   │   ├── thumbnail_service.py # Thumbnail generation
│   │   ├── youtube_service.py  # YouTube upload
│   │   └── notification_service.py # Telegram notifications
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── generation.py      # Generation endpoints
│   │   └── upload.py          # Upload & history endpoints
│   ├── bot/                   # Telegram bot (python -m app.bot)
│   │   ├── app.py             # Application setup & handler registration
│   │   ├── handlers.py        # Conversation flow for the 3 options
│   │   ├── keyboards.py       # Inline buttons
│   │   ├── pipeline.py        # Bot → services facade (DB sessions, previews)
│   │   └── session.py         # Per-chat state machine
│   ├── youtube_auth.py        # One-time YouTube login
│   ├── scheduler/
│   │   ├── __init__.py
│   │   └── jobs.py            # APScheduler job definitions
│   └── utils/
│       ├── __init__.py
│       ├── helpers.py         # Utility functions
│       └── logger.py          # Loguru configuration
├── dashboard/
│   └── streamlit_app.py       # Streamlit dashboard
├── generated/                 # All generated artifacts
│   ├── scripts/
│   ├── audio/
│   ├── images/
│   ├── videos/
│   ├── subtitles/
│   └── thumbnails/
├── logs/                      # Application logs
├── data/                      # SQLite database
├── .env.example               # Environment template
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── README.md
```

---

## 🤖 Telegram Bot (main way to use it)

Say **hi** to your bot and pick one of three options. Nothing is published until you approve it.

```
hi ─► 1️⃣ Find a trending topic
      │   language ─► category ─► latest topics (🔄 show others / ✍️ type your own)
      │   ─► AI script  [✅ approve · 🔄 regenerate · ✏️ edit]
      │   ─► voice samples (listen, tap one) ─► voice-over  [✅ approve · 🎙 other voice]
      │   ─► video preview  [🚀 approve & publish · 🔄 new visuals]  ─► YouTube ✅
      │
      ├► 2️⃣ I have a script  ─► language ─► paste script ─► voices ─► voice-over ─► video ─► YouTube
      │
      └► 3️⃣ I have the audio ─► upload file (language auto-detected) ─► video ─► YouTube
```

- **Latest topics** come from Google News headlines for the category and language, which the AI turns into video ideas.
- **15 languages:** English, Hindi, Telugu, Tamil, Kannada, Malayalam, Bengali, Marathi, Gujarati, Spanish, French, German, Portuguese, Arabic and Japanese. Each has 2–8 neural voices (Edge-TTS) plus a basic Google voice.
- Each voice sample reads the opening of **your** script, so you hear exactly how it will sound.
- Commands: `/start` (menu) · `/cancel` · `/help`

### Bot setup

1. **Create the bot:** message [@BotFather](https://t.me/BotFather), run `/newbot`, and put the token in `TELEGRAM_BOT_TOKEN`.
2. **Lock it to you:** start the bot and say hi. It replies with your Telegram user ID. Put that in `TELEGRAM_ALLOWED_USERS` (comma-separate several IDs) and restart. Anyone else is refused, because the bot can publish to your channel.
3. **Connect YouTube once** on a machine with a browser (needs `client_secrets.json`, see [YouTube Setup](#-youtube-api-setup)):
   ```bash
   python -m app.youtube_auth      # saves token.pickle
   ```
   If the bot runs on a server, copy `token.pickle` next to it.
4. **Run the bot:**
   ```bash
   python -m app.bot
   ```
   It uses long polling, so no public URL or webhook is needed.

Approved videos are published with `YOUTUBE_PRIVACY` (default `public`). Telegram limits: uploads to the bot up to **20 MB**; previews larger than 50 MB are re-encoded at a lower resolution for Telegram only. YouTube gets the full-quality file.

---

## 🧠 AI provider fallback chain

Every AI call (topics, scripts, titles) walks this chain and uses the first good answer:

```
Groq → Gemini (4 models) → OpenRouter free → GitHub Models → OpenAI PAID → OpenRouter PAID
```

The order follows each provider's free allowance, most generous first, so the daily limits last as long as possible:

| # | Provider | Free allowance (approx.) |
|---|---|---|
| 1 | Groq (gpt-oss-120b, qwen3.8-27b, gpt-oss-20b) | separate free daily limit per model; very fast |
| 2 | Gemini | daily request caps per model; often overloaded at peak times |
| 3 | OpenRouter free models | ~50 requests/day (1,000/day after a one-time $10 top-up) |
| 4 | GitHub Models | ~50–150 requests/day, short answers only |
| 5 | OpenAI (gpt-5.4-mini, gpt-4.1-mini) | paid, but cheap mini models |
| 6 | OpenRouter paid (Gemini 2.5 Pro) | most expensive, so the very last resort |

Free limits change often; check each provider's dashboard. To change the order, set `LLM_CHAIN` in `.env`.

- Providers without a key are skipped, so add as many free keys as you can (see `.env.example`).
- A model that is overloaded (503), rate-limited (429), times out, or returns broken JSON is skipped. It rests briefly so later calls don't wait on it.
- A bad key or missing credits (401/402/403) rests that whole provider for 30 minutes. An unknown model name (404) rests that model for 6 hours.
- If everything fails, overloaded models get up to 2 more tries, within a 4-minute total limit. The bot then reports exactly which provider failed and why.
- Check your setup at any time:
  ```bash
  python -m app.llm_check
  ```

---

## 🚀 Quick Start

### Prerequisites

- **Python 3.11+**
- **FFmpeg** — [Download](https://ffmpeg.org/download.html) and add to PATH
- **Gemini API Key** — Free from [Google AI Studio](https://aistudio.google.com/)
- **YouTube OAuth Credentials** — See [YouTube Setup](#youtube-api-setup) below

### 1. Clone & Install

```bash
cd youtube-automation
python -m venv venv

# Windows
venv\Scripts\activate
# Linux/Mac
source venv/bin/activate

pip install -r requirements.txt
```

### 2. Configure Environment

```bash
copy .env.example .env
# Edit .env with your API keys
```

### 3. Run the API Server

```bash
uvicorn app.main:app --reload --port 8000
```

### 4. Run the Dashboard (separate terminal)

```bash
streamlit run dashboard/streamlit_app.py
```

### 5. Open in Browser

- **API Docs**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **Dashboard**: [http://localhost:8501](http://localhost:8501)

---

## 🐳 Docker

```bash
# create these first, otherwise Docker mounts them as empty directories
python -m app.youtube_auth            # → token.pickle (client_secrets.json must exist)
docker-compose up --build -d          # api + bot + dashboard
docker-compose logs -f bot
```

---

## 🔑 YouTube API Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or select existing)
3. Enable **YouTube Data API v3**
4. Go to **APIs & Services → Credentials**
5. Create **OAuth 2.0 Client ID** (Desktop application)
6. Download the JSON file as `client_secrets.json`
7. Place it in the project root directory

> **Note:** First upload will open a browser for OAuth consent. After that, credentials are cached in `token.pickle`.

---

## 📡 API Endpoints

### Generation

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/generate-topic` | Create a new video with a topic |
| `POST` | `/generate-script` | Generate script & metadata |
| `POST` | `/generate-audio` | Generate TTS narration |
| `POST` | `/generate-images` | Generate scene images |
| `POST` | `/generate-subtitles` | Generate SRT subtitles |
| `POST` | `/generate-video` | Assemble final MP4 |
| `POST` | `/generate-thumbnail` | Generate YouTube thumbnail |
| `POST` | `/generate-all` | Run full pipeline |
| `GET`  | `/voices` | List available TTS voices |

### Upload & History

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/upload-video` | Upload to YouTube |
| `GET`  | `/history` | List all videos |
| `GET`  | `/status/{id}` | Get video details |

---

## ⚡ Pipeline Flow

```
Topic → Script → Audio → Images → Subtitles → Video → Thumbnail → Upload
```

Each step can be run individually via the API or dashboard, or all at once with `/generate-all`.

---

## 📊 YouTube API Quotas

| Operation | Cost | Daily Limit |
|-----------|------|-------------|
| `videos.insert` | 1,600 units | ~6 uploads/day |
| `thumbnails.set` | 50 units | ~200/day |
| **Daily quota** | — | **10,000 units** |

---

## 🛠️ Configuration

All settings are in `.env`. Key options:

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_PROVIDER` | `openrouter` | `openrouter` or `ollama` |
| `OPENROUTER_API_KEY` | — | OpenRouter API key |
| `TELEGRAM_BOT_TOKEN` | — | From @BotFather |
| `TELEGRAM_ALLOWED_USERS` | — | Telegram user IDs allowed to use the bot |
| `YOUTUBE_PRIVACY` | `public` | Privacy for videos approved in Telegram |
| `ENABLE_SCHEDULER` | `false` | Unattended daily jobs (skip approval) |
| `MEDIA_PROVIDER` | `pexels` | `pexels` (free stock footage) or `veo` (AI video, paid & slow) |
| `WHISPER_MODEL_SIZE` | `base` | `base`, `medium`, or `large-v3` |
| `DEFAULT_VOICE` | `en-US-AvaNeural` | Edge-TTS voice |
| `DEFAULT_VIDEO_FORMAT` | `horizontal` | `horizontal` or `vertical` |
| `GENERATION_HOUR` | `8` | Hour (UTC) for auto-generation |
| `UPLOAD_HOUR` | `10` | Hour (UTC) for auto-upload |

---

## 📝 License

MIT License — free for personal and commercial use.
