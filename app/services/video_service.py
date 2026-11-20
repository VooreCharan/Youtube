"""
Video assembly service.
Combines images, audio, and subtitles into a final MP4.
Uses raw FFmpeg for extreme speed and stability (no MoviePy deadlocks).
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.video import Video
from app.utils.helpers import json_loads_safe

# Try to use the bundled FFmpeg from imageio-ffmpeg if available, else system ffmpeg
try:
    import imageio_ffmpeg
    _FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
except ImportError:
    _FFMPEG_EXE = "ffmpeg"


def _get_audio_duration(audio_path: Path) -> float:
    """Extract audio duration in seconds using FFmpeg."""
    cmd = [_FFMPEG_EXE, "-i", str(audio_path)]
    # ffmpeg prints duration to stderr
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    match = re.search(r"Duration:\s+(\d+):(\d+):(\d+\.\d+)", res.stderr)
    if not match:
        # Fallback if parsing fails (very rare)
        logger.warning("Could not parse audio duration, using 60s fallback")
        return 60.0
    h, m, s = match.groups()
    return float(h) * 3600 + float(m) * 60 + float(s)


def _build_concat_file(image_paths: list[str], duration_per_image: float, concat_path: Path) -> None:
    """Create a concat demuxer text file for FFmpeg."""
    lines = []
    # Replace backslashes with forward slashes for FFmpeg
    for img in image_paths:
        safe_path = Path(img).absolute().as_posix()
        lines.append(f"file '{safe_path}'")
        if str(safe_path).lower().endswith(".mp4"):
            # For videos, trim to the exact required duration
            lines.append(f"inpoint 0")
            lines.append(f"outpoint {duration_per_image:.3f}")
        else:
            # For images, specify duration
            lines.append(f"duration {duration_per_image:.3f}")
    
    # FFmpeg concat quirk: repeat the last file entry to prevent early termination
    if image_paths:
        safe_path = Path(image_paths[-1]).absolute().as_posix()
        lines.append(f"file '{safe_path}'")

    concat_path.write_text("\n".join(lines), encoding="utf-8")


def _run_ffmpeg_pipeline(
    video_id: int,
    concat_path: Path,
    audio_path: Path,
    output_path: Path,
    target_w: int,
    target_h: int,
    subtitle_path: str | None,
    base_dir: Path,
) -> None:
    """Execute the final FFmpeg render command."""
    
    # ── Video filters ──
    # 1. Scale and pad to fit exactly (target_w x target_h)
    vf = f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2"

    # 2. Burn subtitles if available
    if subtitle_path and Path(subtitle_path).exists():
        # Use a relative path to avoid Windows drive-letter colon parsing bugs in the subtitles filter
        try:
            rel_sub = Path(subtitle_path).relative_to(base_dir).as_posix()
        except ValueError:
            # Fallback if not inside base_dir: we'd need to manually escape the colon
            rel_sub = Path(subtitle_path).absolute().as_posix().replace(":", r"\:")
        
        # Subtitle styling: very small bold white text, translucent outline, anchored to bottom
        font_size = 14
        style = f"FontSize={font_size},PrimaryColour=&H00FFFFFF,OutlineColour=&H99000000,BorderStyle=1,Outline=2.5,Shadow=0,Bold=-1,MarginV=10,Alignment=2"
        vf += f",subtitles='{rel_sub}':force_style='{style}'"

    cmd = [
        _FFMPEG_EXE,
        "-y",                                   # Overwrite output
        "-f", "concat", "-safe", "0",           # Use concat demuxer for images
        "-i", str(concat_path),
        "-i", str(audio_path),                  # Audio input
        "-vf", vf,                              # Video filters (scale/pad/subtitles)
        "-c:v", "libx264",                      # H.264 video codec
        "-preset", "fast",                      # Fast encoding
        "-crf", "23",                           # Constant rate factor (quality)
        "-c:a", "aac",                          # AAC audio codec
        "-b:a", "128k",                         # Audio bitrate
        "-shortest",                            # Stop when the shortest stream (audio) ends
        "-threads", "4",
        str(output_path)
    ]

    logger.info("Executing FFmpeg for video {}:\n{}", video_id, " ".join(cmd))
    
    # Run synchronously (will be wrapped in to_thread)
    res = subprocess.run(cmd, capture_output=True, text=True, cwd=str(base_dir))
    if res.returncode != 0:
        logger.error("FFmpeg failed with exit code {}:\n{}", res.returncode, res.stderr)
        raise RuntimeError(f"FFmpeg assembly failed: {res.stderr[-500:]}")


async def create_video(
    db: AsyncSession,
    video_id: int,
    video_format: str | None = None,
) -> str:
    """
    Assemble a final MP4 video from generated images, audio, and subtitles.
    Uses an extremely fast pure-FFmpeg pipeline.
    """
    settings = get_settings()

    video = await db.get(Video, video_id)
    if video is None:
        raise ValueError(f"Video {video_id} not found")
    if not video.audio_path:
        raise ValueError(f"Video {video_id} has no audio — generate audio first")

    fmt = video_format or video.video_format or settings.default_video_format
    if fmt == "vertical":
        target_w, target_h = 1080, 1920
    else:
        target_w, target_h = 1920, 1080

    video.upload_status = "generating"
    await db.flush()

    output_path = settings.videos_dir / f"{video_id}.mp4"
    audio_path = Path(video.audio_path)
    concat_path = settings.videos_dir / f"concat_{video_id}.txt"

    image_paths: list[str] = json_loads_safe(video.image_paths, [])
    
    if not audio_path.exists():
        video.upload_status = "failed"
        await db.flush()
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    logger.info(
        "Assembling video id={} ({}x{}, {} images) using FFmpeg",
        video_id,
        target_w,
        target_h,
        len(image_paths),
    )

    valid_paths = [p for p in image_paths if Path(p).exists()]
    if not valid_paths:
        logger.warning("No valid images found for video {}!", video_id)
        # To avoid failure, we could generate a black dummy frame, but let's just error
        raise ValueError("No valid image paths found to assemble.")

    try:
        # Offload the heavy work to a thread
        def _render():
            duration = _get_audio_duration(audio_path)
            duration_per_image = duration / len(valid_paths)
            
            _build_concat_file(valid_paths, duration_per_image, concat_path)
            
            _run_ffmpeg_pipeline(
                video_id=video_id,
                concat_path=concat_path,
                audio_path=audio_path,
                output_path=output_path,
                target_w=target_w,
                target_h=target_h,
                subtitle_path=video.subtitle_path,
                base_dir=settings.base_dir,
            )
            
            # Cleanup temp concat file
            if concat_path.exists():
                concat_path.unlink(missing_ok=True)

        await asyncio.to_thread(_render)

    except Exception as exc:
        video.upload_status = "failed"
        video.error_message = f"Video assembly failed: {exc}"
        await db.flush()
        raise

    video.video_path = str(output_path)
    video.video_format = fmt
    video.upload_status = "ready"
    await db.flush()

    file_size_mb = output_path.stat().st_size / (1024 * 1024)
    logger.info("Video saved: {} ({:.1f} MB)", output_path, file_size_mb)
    
    return str(output_path)
