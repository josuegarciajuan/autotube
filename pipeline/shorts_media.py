"""Portrait image fetching and FFmpeg slideshow rendering for Shorts.

Provides shared functions used by the scheduler (planning_service), the API
endpoint (api/routers/shorts.py), and the standalone NativeShortsPipeline.
"""

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Module-level image provider cache ──────────────────────────────────────
# Creating new providers per call accumulates HTTP sessions and memory.
# Singleton pattern ensures one instance per provider for the process lifetime.
_providers_cache = {
    "unsplash": None,
    "pexels": None,
}


def _get_image_providers():
    """Return (unsplash, pexels) provider instances, creating them once."""
    from config import settings

    unsplash = _providers_cache["unsplash"]
    pexels = _providers_cache["pexels"]

    if unsplash is None and settings.UNSPLASH_ACCESS_KEY:
        from pipeline.image_fetcher import UnsplashProvider
        unsplash = UnsplashProvider(settings.UNSPLASH_ACCESS_KEY)
        _providers_cache["unsplash"] = unsplash

    if pexels is None and settings.PEXELS_API_KEY:
        from pipeline.image_fetcher import PexelsProvider
        pexels = PexelsProvider(settings.PEXELS_API_KEY)
        _providers_cache["pexels"] = pexels

    return unsplash, pexels


def _esc_ffmpeg(t: str) -> str:
    """Escape single-quotes / colons / percent for FFmpeg drawtext."""
    return (
        t.replace("'", "'\\\\\\''")
        .replace(":", "\\\\:")
        .replace("%", "\\\\%")
    )


# ─── image fetching ──────────────────────────────────────────────────────


def fetch_portrait_images(
    queries: list[str],
    ch_config,
    count: int = 4,
) -> list[Path]:
    """Fetch portrait (vertical) images from Unsplash / Pexels for a Short.

    Args:
        queries: Text queries to search for (one per block of the script).
        ch_config: Channel config module (has IMAGE_STYLE_MODIFIERS etc.).
        count: Max number of images to download.

    Returns:
        List of local Paths to downloaded images (may be shorter than count
        on errors).
    """
    from config import settings
    import requests, hashlib, re

    unsplash, pexels = _get_image_providers()

    if unsplash is None and pexels is None:
        logger.warning("No image providers configured — Short will have solid background")
        return []

    images_dir = settings.IMAGES_DIR
    images_dir.mkdir(parents=True, exist_ok=True)

    downloaded: list[Path] = []

    for query in queries[:count + 2]:  # slight overfetch
        if len(downloaded) >= count:
            break
        if not query or not query.strip():
            continue

        results = []
        for provider in (unsplash, pexels):
            if provider is None:
                continue
            try:
                results = provider.search(query, n=2, orientation="portrait")
            except Exception as exc:
                logger.debug("Provider search failed for %r: %s", query[:40], exc)
            if results:
                break
        if not results:
            continue

        for photo in results:
            download_url = photo.get("download_url", "")
            if not download_url:
                continue
            try:
                resp = requests.get(download_url, timeout=30)
                resp.raise_for_status()
            except requests.RequestException as exc:
                logger.debug("Download failed: %s", exc)
                continue

            url_hash = hashlib.md5(download_url.encode()).hexdigest()[:12]
            safe_query = re.sub(r"[^a-zA-Z0-9_]", "_", query[:30])
            filename = f"short_portrait_{safe_query}_{url_hash}.jpg"
            filepath = images_dir / filename
            filepath.write_bytes(resp.content)
            downloaded.append(filepath)
            break

    logger.info("Fetched %d portrait images for Short (wanted %d)", len(downloaded), count)
    return downloaded


def _build_solid_bg_filter(
    bg_color_hex: str,
    srt_path: Path | None = None,
) -> str:
    """Build filter_complex for solid-colour background (no images).

    If an SRT subtitle file is available it is burned in via the
    ``subtitles`` filter; otherwise the background is passed through as-is.
    """
    if srt_path and srt_path.exists():
        return (
            f"[0:v]subtitles='{_esc_ffmpeg(str(srt_path))}':"
            f"force_style='FontSize=18,Alignment=2,"
            f"PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
            f"BorderStyle=1,Outline=2'[v]"
        )
    return "[0:v]null[v]"


# ─── main render function ────────────────────────────────────────────────


def render_slideshow_with_images(
    image_paths: list[Path],
    audio_path: Path,
    hook_text: str,
    output_path: Path,
    audio_duration: float | None = None,
    bg_color_hex: str = "0a0a1a",
    crossfade_dur: float = 1.0,
    srt_path: Path | None = None,
) -> Path:
    """Render a vertical Short video as a slideshow of portrait images.

    Each image is scaled+cropped to 1080x1920, sequenced with crossfade
    transitions.  Optional SRT subtitle file is burned in via the
    ``subtitles`` filter, and TTS audio is added.

    Falls back to solid-colour background if no images are provided.

    Args:
        image_paths: Local paths to portrait images (empty = solid bg).
        audio_path: Path to MP3 audio file (TTS output).
        hook_text: Hook text to burn at the bottom.
        output_path: Where to write the output MP4.
        audio_duration: Duration of the audio in seconds (auto-detected).
        bg_color_hex: Fallback background color (hex without #).
        crossfade_dur: Crossfade duration between images in seconds.
        srt_path: Optional SRT/VTT subtitle file path to burn with
            FFmpeg's ``subtitles`` filter.

    Returns:
        The output Path on success.
    """
    # Get audio duration if not provided
    if audio_duration is None:
        dur_out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(audio_path)],
            capture_output=True, text=True, timeout=10,
        )
        try:
            audio_duration = float(dur_out.stdout.strip()) + 1.5
        except (ValueError, AttributeError):
            audio_duration = 20.0

    # ── No images → solid background fallback ────────────────
    if not image_paths:
        filter_str = _build_solid_bg_filter(bg_color_hex, srt_path)
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i",
            f"color=c=0x{bg_color_hex}:s=1080x1920:d={audio_duration}:r=30",
            "-i", str(audio_path),
            "-filter_complex", filter_str,
            "-map", "[v]", "-map", "1:a",
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-pix_fmt", "yuv420p", "-r", "30",
            "-c:a", "aac", "-b:a", "128k",
            "-shortest", "-movflags", "+faststart",
            str(output_path),
        ]
        subprocess.run(cmd, capture_output=True, timeout=180)
        return output_path

    # ── Images → slideshow with crossfade ────────────────────
    n_images = len(image_paths)
    fade = crossfade_dur
    segment_dur = (audio_duration + fade * (n_images - 1)) / n_images

    inputs = []
    filter_parts = []

    # Input each image as a loop
    for i, img in enumerate(image_paths):
        inputs.extend(["-loop", "1", "-t", f"{segment_dur + fade:.2f}", "-i", str(img)])

    # Add audio last
    inputs.extend(["-i", str(audio_path)])
    audio_idx = n_images

    # Scale + crop each image to 9:16
    for i in range(n_images):
        filter_parts.append(
            f"[{i}:v]scale=1080:1920:force_original_aspect_ratio=increase,"
            f"crop=1080:1920,setsar=1,fps=30[v{i}]"
        )

    # Crossfade chain
    if n_images == 1:
        final_label = "[v0]"
    else:
        for i in range(1, n_images):
            offset = segment_dur - fade
            if i == 1:
                filter_parts.append(
                    f"[v0][v1]xfade=transition=fade:duration={fade}:offset={offset:.2f}[vf1]"
                )
            else:
                filter_parts.append(
                    f"[vf{i-1}][v{i}]xfade=transition=fade:"
                    f"duration={fade}:offset={offset:.2f}[vf{i}]"
                )
        final_label = f"[vf{n_images - 1}]"

    # Burn SRT subtitles if available; otherwise passthrough video as-is
    if srt_path and srt_path.exists():
        filter_parts.append(
            f"{final_label}subtitles='{_esc_ffmpeg(str(srt_path))}':"
            f"force_style='FontSize=18,Alignment=2,"
            f"PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
            f"BorderStyle=1,Outline=2'[v]"
        )
    else:
        filter_parts.append(f"{final_label}null[v]")

    filter_graph = ";".join(filter_parts)
    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_graph,
        "-map", "[v]", "-map", f"{audio_idx}:a",
        "-c:v", "libx264", "-preset", "fast", "-crf", "22",
        "-pix_fmt", "yuv420p", "-r", "30",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest", "-movflags", "+faststart",
        str(output_path),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        logger.error("FFmpeg slideshow failed: %s", result.stderr[-400:])
        raise RuntimeError(f"FFmpeg slideshow render failed: {result.stderr[-300:]}")
    return output_path
