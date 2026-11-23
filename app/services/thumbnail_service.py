"""
Thumbnail generation service.
Creates eye-catching 1280×720 thumbnails using Pillow.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.video import Video
from app.utils.helpers import json_loads_safe


def _load_background(path: Path) -> Image.Image:
    """Open an image, or grab a frame from a video clip (Pexels/Veo scenes are MP4s)."""
    if path.suffix.lower() not in (".mp4", ".mov", ".webm", ".mkv"):
        return Image.open(path).convert("RGB")

    import subprocess
    import tempfile

    from app.services.video_service import _FFMPEG_EXE

    with tempfile.TemporaryDirectory() as tmp:
        frame = Path(tmp) / "frame.jpg"
        subprocess.run(
            [_FFMPEG_EXE, "-y", "-ss", "1", "-i", str(path), "-frames:v", "1", str(frame)],
            capture_output=True,
            check=True,
            timeout=60,
        )
        return Image.open(frame).convert("RGB")


async def generate_thumbnail(db: AsyncSession, video_id: int) -> str:
    """
    Create a YouTube thumbnail for the given video.

    Returns the path to the saved thumbnail image.
    """
    settings = get_settings()

    video = await db.get(Video, video_id)
    if video is None:
        raise ValueError(f"Video {video_id} not found")

    width, height = 1280, 720
    output_path = settings.thumbnails_dir / f"{video_id}.jpg"

    logger.info("Generating thumbnail for Video id={}", video_id)

    # Try to use the first scene image as background
    image_paths: list[str] = json_loads_safe(video.image_paths, [])
    bg_image = None

    for img_path in image_paths:
        if Path(img_path).exists():
            try:
                bg_image = _load_background(Path(img_path)).resize((width, height), Image.LANCZOS)
                break
            except Exception:
                continue

    if bg_image is None:
        # Create a gradient background
        bg_image = _create_gradient_bg(width, height)

    # Apply cinematic overlay
    bg_image = _apply_overlay(bg_image, width, height)

    # Add title text
    title = video.title or video.topic
    bg_image = _add_title_text(bg_image, title, width, height, video.language)

    # Add subtle border
    bg_image = _add_border(bg_image, width, height)

    bg_image.save(output_path, "JPEG", quality=95)

    video.thumbnail_path = str(output_path)
    await db.flush()

    logger.info("Thumbnail saved: {}", output_path)
    return str(output_path)


def _create_gradient_bg(width: int, height: int) -> Image.Image:
    """Create a dark gradient background."""
    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img)

    # Dark cinematic gradient
    colors = [
        [(15, 12, 41), (48, 43, 99), (36, 36, 62)],
        [(20, 20, 40), (40, 80, 120), (20, 40, 80)],
        [(30, 10, 40), (80, 20, 80), (40, 10, 60)],
    ]
    palette = random.choice(colors)

    for y in range(height):
        ratio = y / height
        if ratio < 0.5:
            r = ratio * 2
            c1, c2 = palette[0], palette[1]
        else:
            r = (ratio - 0.5) * 2
            c1, c2 = palette[1], palette[2]

        red = int(c1[0] * (1 - r) + c2[0] * r)
        green = int(c1[1] * (1 - r) + c2[1] * r)
        blue = int(c1[2] * (1 - r) + c2[2] * r)
        draw.line([(0, y), (width, y)], fill=(red, green, blue))

    return img


def _apply_overlay(img: Image.Image, width: int, height: int) -> Image.Image:
    """Apply a dark gradient overlay for text readability."""
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Bottom-heavy dark gradient overlay
    for y in range(height):
        alpha = int(180 * (y / height) ** 1.5)
        draw.line([(0, y), (width, y)], fill=(0, 0, 0, alpha))

    # Additional darkening at bottom third
    for y in range(height * 2 // 3, height):
        ratio = (y - height * 2 // 3) / (height // 3)
        alpha = int(60 + 100 * ratio)
        draw.line([(0, y), (width, y)], fill=(0, 0, 0, alpha))

    img_rgba = img.convert("RGBA")
    composited = Image.alpha_composite(img_rgba, overlay)
    return composited.convert("RGB")


def _add_title_text(
    img: Image.Image, title: str, width: int, height: int, language: str | None = None
) -> Image.Image:
    """Render the title text on the thumbnail."""
    from app.utils.fonts import load_font

    draw = ImageDraw.Draw(img)
    font_size = width // 16  # ~80px for 1280px width
    font = load_font(language, font_size)

    # Word wrap
    max_width = width - 120  # padding on both sides
    lines = _wrap_text(draw, title.upper(), font, max_width)

    # Position text in the lower-center area
    line_height = font_size + 8
    total_text_height = len(lines) * line_height
    y_start = height - total_text_height - 80  # 80px from bottom

    for i, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=font)
        text_w = bbox[2] - bbox[0]
        x = (width - text_w) // 2
        y = y_start + i * line_height

        # Glow / shadow layers
        for offset in [4, 3, 2]:
            draw.text(
                (x + offset, y + offset),
                line,
                fill=(0, 0, 0),
                font=font,
            )

        # Main text (white with slight yellow tint for pop)
        draw.text((x, y), line, fill=(255, 255, 240), font=font)

    return img


def _add_border(img: Image.Image, width: int, height: int) -> Image.Image:
    """Add a subtle glowing border."""
    draw = ImageDraw.Draw(img)
    border_color = (255, 200, 50, 180)  # gold-ish

    # Inner border rectangle
    border_width = 4
    for i in range(border_width):
        alpha_factor = 1 - (i / border_width) * 0.5
        color = tuple(int(c * alpha_factor) for c in border_color[:3])
        draw.rectangle(
            [8 + i, 8 + i, width - 9 - i, height - 9 - i],
            outline=color,
        )

    return img


def _wrap_text(draw: ImageDraw.Draw, text: str, font, max_width: int) -> list[str]:
    """Split text into lines that fit within max_width."""
    words = text.split()
    lines = []
    current = ""

    for word in words:
        test = f"{current} {word}".strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)

    return lines[:4]  # max 4 lines
