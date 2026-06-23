"""Channel profile generator: description, banner, and avatar.

Creates the visual identity for a new channel using Pollo AI for
image generation and channel config for the description text.

Usage:
    from pipeline.channel_profile_generator import generate_channel_profile
    profile = generate_channel_profile("canal2")
    # profile = {"description": "...", "banner_url": "...", "avatar_url": "..."}
"""

import logging
from pathlib import Path
from typing import Optional

from config.config_bridge import get_channel_config
from config.settings import OUTPUT_DIR

logger = logging.getLogger(__name__)

THUMBNAILS_DIR = OUTPUT_DIR / "thumbnails"


def generate_channel_profile(slug: str) -> dict:
    """Generate profile elements for a channel.

    Reads the description from the channel config and generates
    banner + avatar images via Pollo AI.

    Args:
        slug: Channel slug (e.g. "canal2").

    Returns:
        Dict with keys: description, banner_url, avatar_url.
        Image URLs are paths served by the API static endpoint.
    """
    cfg = get_channel_config(slug)

    # ── Description ──────────────────────────────────────────
    description = _get_description(cfg)

    # ── Images ───────────────────────────────────────────────
    out_dir = THUMBNAILS_DIR / slug
    out_dir.mkdir(parents=True, exist_ok=True)

    banner_path = out_dir / "banner.jpg"
    avatar_path = out_dir / "avatar.jpg"

    banner_url = _generate_banner(slug, cfg, banner_path)
    avatar_url = _generate_avatar(slug, cfg, avatar_path)

    return {
        "description": description,
        "banner_url": banner_url,
        "avatar_url": avatar_url,
    }


def _get_description(cfg) -> str:
    """Extract the channel description from config."""
    desc = getattr(cfg, "CHANNEL_ABOUT_SECTION", None)
    if desc:
        return desc.strip()

    # Fallback: build from display name + tagline
    name = getattr(cfg, "CANAL_DISPLAY_NAME", "Canal")
    tagline = getattr(cfg, "CANAL_TAGLINE", "")
    if tagline:
        return f"Bienvenido a {name}. {tagline}"
    return f"Bienvenido a {name}."


def _generate_banner(slug: str, cfg, output_path: Path) -> str:
    """Generate YouTube channel banner using Pillow (16:9, 2560×1440).

    Creates an elegant gradient + light-ray banner from the channel palette.
    Returns the URL path accessible via the API static endpoint.
    """
    if output_path.exists() and output_path.stat().st_size > 0:
        logger.info("Channel %s banner already cached", slug)
        return _build_static_url(slug, output_path.name)

    try:
        _render_banner_pillow(cfg, output_path)
        url = _build_static_url(slug, output_path.name)
        logger.info("Channel %s banner rendered: %s", slug, url)
        return url
    except Exception as exc:
        logger.error("Banner render failed for %s: %s", slug, exc)
        return ""


def _generate_avatar(slug: str, cfg, output_path: Path) -> str:
    """Generate YouTube channel avatar using Pillow (1:1, 800×800).

    Creates a circular emblem with initials from the channel palette.
    Returns the URL path accessible via the API static endpoint.
    """
    if output_path.exists() and output_path.stat().st_size > 0:
        logger.info("Channel %s avatar already cached", slug)
        return _build_static_url(slug, output_path.name)

    try:
        _render_avatar_pillow(cfg, output_path)
        url = _build_static_url(slug, output_path.name)
        logger.info("Channel %s avatar rendered: %s", slug, url)
        return url
    except Exception as exc:
        logger.error("Avatar render failed for %s: %s", slug, exc)
        return ""


# ═══════════════════════════════════════════════════════════════════
# Pillow-based rendering (no external API dependency)
# ═══════════════════════════════════════════════════════════════════

def _render_banner_pillow(cfg, output_path: Path) -> None:
    """Render a 2560×1440 banner: constellation + typography + gradient."""
    from PIL import Image, ImageDraw, ImageFont
    import math, random as _random

    palette = getattr(cfg, "COLOR_PALETTE", {})
    primary = _get_rgb(palette, "primary", (212, 175, 55))
    secondary = _get_rgb(palette, "secondary", (15, 32, 62))
    accent = _get_rgb(palette, "accent", (200, 120, 80))
    text_col = _get_rgb(palette, "text", (245, 240, 230))

    W, H = 2560, 1440
    img = Image.new("RGB", (W, H))
    pixels = img.load()

    # ═══ Layer 1: Gradient background ═══════════════════════
    for y in range(H):
        ratio = y / H
        bright_ratio = min(ratio * 1.6, 1.0)
        r = int(secondary[0] + (primary[0] - secondary[0]) * bright_ratio)
        g = int(secondary[1] + (primary[1] - secondary[1]) * bright_ratio)
        b = int(secondary[2] + (primary[2] - secondary[2]) * bright_ratio)
        for x in range(W):
            pixels[x, y] = (r, g, b)

    draw = ImageDraw.Draw(img, "RGBA")

    # ═══ Layer 2: Central glow ══════════════════════════════
    cx, cy = W // 2, int(H * 0.35)

    for r_outer in range(900, 20, -10):
        alpha = int(30 * (1.0 - r_outer / 900))
        if alpha <= 0:
            break
        draw.ellipse(
            [cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer],
            fill=(255, 220, 140, alpha),
        )

    for r_inner in range(250, 10, -5):
        alpha = int(50 * (1.0 - r_inner / 250))
        if alpha <= 0:
            break
        draw.ellipse(
            [cx - r_inner, cy - r_inner, cx + r_inner, cy + r_inner],
            fill=(255, 240, 190, alpha),
        )

    # ═══ Layer 3: Constellation ═════════════════════════════
    _random.seed(42)
    margin = 200
    nodes = []
    for _ in range(55):
        nx = _random.randint(margin, W - margin)
        ny = _random.randint(int(H * 0.05), int(H * 0.75))
        nodes.append((nx, ny))
        # Vary dot sizes — some bigger, some smaller
        dot_r = _random.randint(2, 7)
        dot_alpha = _random.randint(140, 230)
        draw.ellipse(
            [nx - dot_r, ny - dot_r, nx + dot_r, ny + dot_r],
            fill=(primary[0], primary[1], primary[2], dot_alpha),
        )

    # Connect nearby nodes with thin golden lines
    for i, (x1, y1) in enumerate(nodes):
        for j in range(i + 1, len(nodes)):
            x2, y2 = nodes[j]
            dist = math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)
            max_dist = 350
            if dist < max_dist:
                alpha = int(55 * (1.0 - dist / max_dist))
                if alpha > 0:
                    draw.line(
                        [(x1, y1), (x2, y2)],
                        fill=(primary[0], primary[1], primary[2], alpha),
                        width=1,
                    )

    # ═══ Layer 4: Light rays (subtle, behind text) ═════════
    for i in range(4):
        ray_y = int(H * 0.25 + i * H * 0.14)
        ray_width = int(W * (0.30 + i * 0.10))
        ray_x1 = (W - ray_width) // 2
        for rx in range(ray_x1, ray_x1 + ray_width):
            dist_center = abs(rx - W // 2) / (ray_width / 2)
            alpha = int(40 * (1.0 - dist_center))
            if alpha > 0:
                for dy in range(-2, 3):
                    yy = ray_y + dy
                    if 0 <= yy < H:
                        cur = pixels[rx, yy]
                        pixels[rx, yy] = (
                            min(255, cur[0] + alpha),
                            min(255, cur[1] + alpha),
                            min(255, cur[2] + alpha),
                        )

    # ═══ Layer 5: Channel name "SINCRONÍAS" ════════════════
    display_name = getattr(cfg, "CANAL_DISPLAY_NAME", "Sincronías").upper()
    try:
        name_font = _find_font(130)
        # Shadow
        bbox = draw.textbbox((0, 0), display_name, font=name_font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        tx = (W - tw) // 2
        ty = int(H * 0.48)
        draw.text((tx + 3, ty + 3), display_name, fill=(0, 0, 0, 140), font=name_font)
        # Main text
        draw.text((tx, ty), display_name, fill=text_col, font=name_font)
    except Exception:
        pass

    # ═══ Layer 6: Tagline ══════════════════════════════════
    tagline = getattr(cfg, "CANAL_TAGLINE", "")
    if tagline:
        # Truncate for display
        if len(tagline) > 80:
            tagline = tagline[:77] + "..."
        try:
            tag_font = _find_font(32)
            bbox2 = draw.textbbox((0, 0), tagline, font=tag_font)
            tw2, th2 = bbox2[2] - bbox2[0], bbox2[3] - bbox2[1]
            tx2 = (W - tw2) // 2
            ty2 = int(H * 0.48) + 140
            draw.text((tx2 + 2, ty2 + 2), tagline, fill=(0, 0, 0, 100), font=tag_font)
            draw.text((tx2, ty2), tagline, fill=(text_col[0], text_col[1], text_col[2], 200), font=tag_font)
        except Exception:
            pass

    # ═══ Layer 7: Sparkle particles ════════════════════════
    _random.seed(99)
    for _ in range(80):
        px_x = _random.randint(W // 5, 4 * W // 5)
        px_y = _random.randint(int(H * 0.05), int(H * 0.70))
        sparkle_r = _random.randint(2, 5)
        alpha = _random.randint(40, 100)
        draw.ellipse(
            [px_x - sparkle_r, px_y - sparkle_r,
             px_x + sparkle_r, px_y + sparkle_r],
            fill=(255, 245, 200, alpha),
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path, "JPEG", quality=95)


def _render_avatar_pillow(cfg, output_path: Path) -> None:
    """Render an 800×800 circular avatar with bright visible emblem."""
    from PIL import Image, ImageDraw, ImageFont
    import math

    palette = getattr(cfg, "COLOR_PALETTE", {})
    primary = _get_rgb(palette, "primary", (212, 175, 55))
    secondary = _get_rgb(palette, "secondary", (15, 32, 62))
    accent = _get_rgb(palette, "accent", (200, 120, 80))
    text_colour = _get_rgb(palette, "text", (245, 240, 230))

    SIZE = 800
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    radius = SIZE // 2

    # Solid circle with brighter gradient (indigo → gold, reaching gold faster)
    for r in range(radius, 0, -1):
        ratio = r / radius
        # Brighter gradient: starts dark indigo, reaches gold by r=~50% radius
        bright_ratio = min((1.0 - ratio) * 2.0, 1.0)  # gold by 50% inward
        rr = int(secondary[0] + (primary[0] - secondary[0]) * bright_ratio)
        gg = int(secondary[1] + (primary[1] - secondary[1]) * bright_ratio)
        bb = int(secondary[2] + (primary[2] - secondary[2]) * bright_ratio)
        draw.ellipse(
            [radius - r, radius - r, radius + r, radius + r],
            fill=(rr, gg, bb, 255),
        )

    # Thick golden ring at edge
    ring_width = 16
    draw.ellipse(
        [ring_width, ring_width, SIZE - ring_width, SIZE - ring_width],
        outline=(primary[0], primary[1], primary[2], 220),
        width=4,
    )

    # Inner thin accent ring
    inner_ring = radius - 45
    draw.ellipse(
        [radius - inner_ring, radius - inner_ring,
         radius + inner_ring, radius + inner_ring],
        outline=(accent[0], accent[1], accent[2], 140),
        width=2,
    )

    # Bright glow behind initials
    glow_r = 200
    for gr in range(glow_r, 10, -5):
        alpha = int(40 * (1.0 - gr / glow_r))
        draw.ellipse(
            [radius - gr, radius - gr, radius + gr, radius + gr],
            fill=(255, 245, 200, alpha),
        )

    # Channel initials — large and bright
    initials = getattr(cfg, "CANAL_INITIALS", "SX")
    initials = initials[:2].upper()

    try:
        font_size = 280
        font = _find_font(font_size)
        bbox = draw.textbbox((0, 0), initials, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        # Draw shadow
        draw.text(
            ((SIZE - tw) / 2 + 3, (SIZE - th) / 2 - font_size * 0.05 + 3),
            initials,
            fill=(0, 0, 0, 120),
            font=font,
        )
        # Draw text
        draw.text(
            ((SIZE - tw) / 2, (SIZE - th) / 2 - font_size * 0.05),
            initials,
            fill=text_colour,
            font=font,
        )
    except Exception:
        font_size = 200
        font = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), initials, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text(
            ((SIZE - tw) / 2, (SIZE - th) / 2),
            initials,
            fill=text_colour,
            font=font,
        )

    # Merge onto RGB background
    from PIL import Image as PILImage
    bg = PILImage.new("RGB", (SIZE, SIZE), secondary)
    bg.paste(img, (0, 0), img)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    bg.save(output_path, "JPEG", quality=95)


def _find_font(size: int):
    """Find a suitable TrueType font for the given size."""
    from PIL import ImageFont
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    ]
    for fp in font_paths:
        if Path(fp).exists():
            return ImageFont.truetype(fp, size)
    return ImageFont.load_default()


def _get_rgb(palette: dict, key: str, fallback: tuple) -> tuple:
    """Extract an RGB tuple from the channel colour palette."""
    val = palette.get(key, fallback)
    if isinstance(val, (list, tuple)) and len(val) == 3:
        return tuple(int(c) for c in val)
    return fallback


def _build_static_url(slug: str, filename: str) -> str:
    """Build the public URL for a channel asset via nginx proxy.

    nginx routes /autotube/thumbnails/* → localhost:8000/api/static/thumbnails/*
    Returns e.g. "/autotube/thumbnails/canal2/banner.jpg"
    """
    return f"/autotube/thumbnails/{slug}/{filename}"
