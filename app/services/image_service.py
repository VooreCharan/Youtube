"""
Image generation service.

Primary: Pexels stock photo API (free, no GPU).
Fallback: Gradient backgrounds with overlay text via Pillow.
"""

from __future__ import annotations

import asyncio
import json
import random
import subprocess
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from loguru import logger

from app.config import get_settings
from app.models.video import Video
from app.utils.helpers import ensure_dir, json_loads_safe
from sqlalchemy.ext.asyncio import AsyncSession

try:
    import imageio_ffmpeg
    _FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
except ImportError:
    _FFMPEG_EXE = "ffmpeg"


# ── Colour palettes for fallback gradients ───────────────────────────
_GRADIENT_PALETTES = [
    [(25, 25, 112), (70, 130, 180)],      # midnight blue → steel blue
    [(72, 61, 139), (147, 112, 219)],      # dark slate blue → medium purple
    [(0, 100, 0), (34, 139, 34)],          # dark green → forest green
    [(139, 0, 0), (220, 20, 60)],          # dark red → crimson
    [(0, 0, 139), (0, 191, 255)],          # dark blue → deep sky blue
    [(75, 0, 130), (238, 130, 238)],       # indigo → violet
    [(184, 134, 11), (255, 215, 0)],       # dark goldenrod → gold
    [(0, 128, 128), (0, 206, 209)],        # teal → dark turquoise
]


async def generate_images(db: AsyncSession, video_id: int) -> list[str]:
    """
    Generate images for each scene of the video.

    Returns a list of saved image file paths.
    """
    settings = get_settings()

    video = await db.get(Video, video_id)
    if video is None:
        raise ValueError(f"Video {video_id} not found")

    prompts: list[str] = json_loads_safe(video.image_prompts, [])
    if not prompts:
        raise ValueError(f"Video {video_id} has no image prompts — generate script first")

    # Determine target resolution
    if video.video_format == "vertical":
        width, height = 1080, 1920
    else:
        width, height = 1920, 1080

    output_dir = ensure_dir(settings.images_dir / str(video_id))
    saved_paths: list[str] = []

    for idx, prompt in enumerate(prompts):
        output_file_vid = output_dir / f"scene_{idx + 1}.mp4"
        output_file_img = output_dir / f"scene_{idx + 1}.jpg"

        # Try Google Veo Video first if configured
        if settings.media_provider == "veo" and settings.gemini_api_key:
            success = await _fetch_veo_video(
                prompt, width, height, output_file_vid, settings.gemini_api_key
            )
            if success:
                saved_paths.append(str(output_file_vid))
                continue

        # Try Pexels Video first
        if settings.pexels_api_key:
            success = await _fetch_pexels_video(
                prompt, width, height, output_file_vid, settings.pexels_api_key
            )
            if success:
                saved_paths.append(str(output_file_vid))
                continue

            # Fallback to Pexels Image
            success = await _fetch_pexels_image(
                prompt, width, height, output_file_img, settings.pexels_api_key
            )
            if success:
                saved_paths.append(str(output_file_img))
                continue

        # Ultimate fallback: generate a gradient image with text
        _create_gradient_image(prompt, width, height, output_file_img, idx)
        saved_paths.append(str(output_file_img))

    video.image_paths = json.dumps(saved_paths)
    await db.flush()

    logger.info("Generated {} media files for Video id={}", len(saved_paths), video_id)
    return saved_paths


async def _fetch_pexels_video(
    query: str,
    width: int,
    height: int,
    output_path: Path,
    api_key: str,
) -> bool:
    """Download a relevant video clip from Pexels and normalize it using FFmpeg."""
    search_query = " ".join(query.split()[:5])
    orientation = "landscape" if width > height else "portrait"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                "https://api.pexels.com/videos/search",
                params={
                    "query": search_query,
                    "per_page": 5,
                    "orientation": orientation,
                },
                headers={"Authorization": api_key},
            )
            resp.raise_for_status()
            data = resp.json()

        videos = data.get("videos", [])
        if not videos:
            logger.warning("Pexels Video returned no results for '{}'", search_query)
            return False

        # Pick a random video
        video_obj = random.choice(videos)
        
        # Get the highest quality video file that isn't excessively huge
        video_files = sorted(
            video_obj.get("video_files", []), 
            key=lambda x: (x.get("width") or 0) * (x.get("height") or 0), 
            reverse=True
        )
        
        if not video_files:
            return False
            
        video_url = video_files[0]["link"]

        # Download raw video
        raw_path = output_path.with_name(f"raw_{output_path.name}")
        async with httpx.AsyncClient(timeout=60) as client:
            vid_resp = await client.get(video_url)
            vid_resp.raise_for_status()

        raw_path.write_bytes(vid_resp.content)

        # Normalize via FFmpeg (crop to exact target size, 30fps, remove audio, reset SAR)
        def _normalize():
            vf = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1"
            cmd = [
                _FFMPEG_EXE, "-y",
                "-i", str(raw_path),
                "-vf", vf,
                "-r", "30",
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "23",
                "-an",  # Strip audio
                str(output_path)
            ]
            subprocess.run(cmd, capture_output=True, check=True)
            raw_path.unlink(missing_ok=True)

        await asyncio.to_thread(_normalize)

        logger.info("Pexels video saved and normalized: {}", output_path)
        return True

    except Exception as exc:
        logger.warning("Pexels video fetch failed for '{}': {}", search_query, exc)
        return False


async def _fetch_veo_video(
    prompt: str,
    width: int,
    height: int,
    output_path: Path,
    api_key: str,
) -> bool:
    """Generate a video clip using Google Veo via Gemini API."""
    try:
        from google import genai
        from google.genai import types
        import time

        client = genai.Client(api_key=api_key)
        aspect_ratio = "16:9" if width > height else "9:16"

        # Offload the blocking generate_videos call and polling to a thread
        def _generate():
            logger.info("Starting Veo generation for prompt: '{}'", prompt)
            operation = client.models.generate_videos(
                model="veo-2.0-generate-001",
                prompt=prompt,
                config=types.GenerateVideosConfig(
                    aspect_ratio=aspect_ratio,
                    person_generation="ALLOW_ADULT"
                )
            )

            while not operation.done:
                time.sleep(10)
                operation = client.operations.get(operation=operation)

            if getattr(operation, "error", None) and operation.error:
                raise RuntimeError(f"Veo generation failed: {operation.error.message}")

            if not getattr(operation, "response", None) or not getattr(operation.response, "generated_videos", None):
                raise RuntimeError("Veo generation returned no videos.")

            generated_video = operation.response.generated_videos[0]
            
            # Use the SDK to download the video file locally
            video_bytes = client.files.get(name=generated_video.video.name)
            
            import urllib.request
            urllib.request.urlretrieve(video_bytes.uri, str(output_path))
            
        await asyncio.to_thread(_generate)

        # Normalize via FFmpeg to match the rest of the pipeline
        raw_path = output_path.with_name(f"raw_veo_{output_path.name}")
        output_path.rename(raw_path)

        def _normalize():
            vf = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1"
            cmd = [
                _FFMPEG_EXE, "-y",
                "-i", str(raw_path),
                "-vf", vf,
                "-r", "30",
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "23",
                "-an",  # Strip audio
                str(output_path)
            ]
            subprocess.run(cmd, capture_output=True, check=True)
            raw_path.unlink(missing_ok=True)

        await asyncio.to_thread(_normalize)

        logger.info("Veo video saved and normalized: {}", output_path)
        return True

    except Exception as exc:
        logger.warning("Veo video generation failed for '{}': {}", prompt, exc)
        return False


async def _fetch_pexels_image(
    query: str,
    width: int,
    height: int,
    output_path: Path,
    api_key: str,
) -> bool:
    """Download a relevant image from Pexels. Returns True on success."""
    # Simplify the query to key terms for better search results
    search_query = " ".join(query.split()[:5])

    orientation = "landscape" if width > height else "portrait"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                "https://api.pexels.com/v1/search",
                params={
                    "query": search_query,
                    "per_page": 5,
                    "orientation": orientation,
                },
                headers={"Authorization": api_key},
            )
            resp.raise_for_status()
            data = resp.json()

        photos = data.get("photos", [])
        if not photos:
            logger.warning("Pexels returned no results for '{}'", search_query)
            return False

        # Pick a random photo from top results
        photo = random.choice(photos)
        img_url = photo["src"].get("large2x") or photo["src"].get("original")

        async with httpx.AsyncClient(timeout=60) as client:
            img_resp = await client.get(img_url)
            img_resp.raise_for_status()

        output_path.write_bytes(img_resp.content)

        # Resize to target dimensions
        img = Image.open(output_path).convert("RGB")
        img = img.resize((width, height), Image.LANCZOS)
        img.save(output_path, "JPEG", quality=90)

        logger.info("Pexels image saved: {}", output_path)
        return True

    except Exception as exc:
        logger.warning("Pexels fetch failed for '{}': {}", search_query, exc)
        return False


def _create_gradient_image(
    prompt: str,
    width: int,
    height: int,
    output_path: Path,
    scene_index: int,
) -> None:
    """Create a gradient background with overlaid scene text."""
    palette = _GRADIENT_PALETTES[scene_index % len(_GRADIENT_PALETTES)]
    color_top, color_bottom = palette

    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img)

    # Draw vertical gradient
    for y in range(height):
        ratio = y / height
        r = int(color_top[0] * (1 - ratio) + color_bottom[0] * ratio)
        g = int(color_top[1] * (1 - ratio) + color_bottom[1] * ratio)
        b = int(color_top[2] * (1 - ratio) + color_bottom[2] * ratio)
        draw.line([(0, y), (width, y)], fill=(r, g, b))

    # Add decorative circles
    for _ in range(6):
        cx = random.randint(0, width)
        cy = random.randint(0, height)
        radius = random.randint(50, 200)
        alpha_img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        alpha_draw = ImageDraw.Draw(alpha_img)
        alpha_draw.ellipse(
            [cx - radius, cy - radius, cx + radius, cy + radius],
            fill=(255, 255, 255, 25),
        )
        img = Image.alpha_composite(img.convert("RGBA"), alpha_img).convert("RGB")

    draw = ImageDraw.Draw(img)

    # Try to load a font, fall back to default
    font_size = width // 25
    try:
        font = ImageFont.truetype("arial.ttf", font_size)
    except (OSError, IOError):
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
        except (OSError, IOError):
            font = ImageFont.load_default()

    # Word-wrap the prompt text
    words = prompt.split()
    lines = []
    current_line = ""
    max_chars = width // (font_size // 2 + 2)

    for word in words:
        test_line = f"{current_line} {word}".strip()
        if len(test_line) <= max_chars:
            current_line = test_line
        else:
            if current_line:
                lines.append(current_line)
            current_line = word
    if current_line:
        lines.append(current_line)

    # Centre text vertically
    line_height = font_size + 10
    total_text_height = len(lines) * line_height
    y_start = (height - total_text_height) // 2

    for i, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=font)
        text_width = bbox[2] - bbox[0]
        x = (width - text_width) // 2
        y = y_start + i * line_height

        # Shadow
        draw.text((x + 2, y + 2), line, fill=(0, 0, 0, 180), font=font)
        # Main text
        draw.text((x, y), line, fill=(255, 255, 255), font=font)

    img.save(output_path, "JPEG", quality=90)
    logger.info("Gradient image saved: {}", output_path)
