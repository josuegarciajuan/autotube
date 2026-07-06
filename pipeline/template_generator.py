"""Template Generator — creates reusable intro/CTA/outro mini-videos per channel.

Caches generated templates in output/templates/{channel_slug}/
Uses MoviePy for video generation with Ken Burns effects, text overlays,
and optionally AI-generated background images. Falls back to gradient + text
when AI image generation is unavailable.
"""

import os
import logging
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# ── MoviePy import (v2 preferred, v1 fallback) ──────────────────
try:
    from moviepy import (
        VideoClip,
        VideoFileClip,
        ImageClip,
        CompositeVideoClip,
        TextClip,
        concatenate_videoclips,
        vfx,
    )
    MOVIEPY_V2 = True
except ImportError:
    from moviepy.editor import (
        VideoClip,
        VideoFileClip,
        ImageClip,
        CompositeVideoClip,
        TextClip,
        concatenate_videoclips,
        vfx,
    )
    MOVIEPY_V2 = False


# ── Helpers ──────────────────────────────────────────────────────

def _resolve_font() -> str:
    """Return the first available font from a preferred list.
    
    Replicates the same logic as video_editor.VideoEditor._resolve_font().
    """
    candidates = [
        "DejaVu-Sans",
        "DejaVu Sans",
        "Arial",
        "Helvetica",
        "Liberation-Sans",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for name in candidates:
        if os.path.exists(name):
            return name
        if any(Path("/usr/share/fonts").rglob(f"*{name}*")):
            return name
    return "DejaVu-Sans"


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    """Convert (R, G, B) tuple to '#RRGGBB' hex string."""
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def _alpha_fade(
    clip: VideoClip,
    duration: float,
    fade_in: float = 0.0,
    fade_out: float = 0.0,
) -> VideoClip:
    """Apply fade-in/out by modulating the alpha channel of RGBA frames.

    Safe for the 4‑channel RGBA clips produced by ``_create_text_clip``,
    ``_build_logo_image`` and other custom ``VideoClip`` helpers — it scales
    the fourth channel directly instead of blending RGB toward black, which
    avoids a NumPy broadcasting error inside MoviePy's ``vfx.FadeIn``/``FadeOut``
    (those effects assume 3‑channel frames and crash on RGBA arrays).

    Falls back to ``clip.with_effects([vfx.FadeIn, vfx.FadeOut])`` when the
    frames it receives are not 4‑channel RGBA (e.g. a 3‑channel MoviePy
    ``ImageClip``).  Non‑v2 MoviePy is also tried as a last resort.
    """
    if fade_in <= 0 and fade_out <= 0:
        return clip

    def _filter(get_frame, t: float):
        frame = get_frame(t)
        # Only touch genuine RGBA overlays; leave everything else alone
        if isinstance(frame, np.ndarray) and frame.ndim == 3 and frame.shape[2] == 4:
            factor = 1.0
            if 0 < fade_in and t < fade_in:
                factor = min(factor, t / fade_in)
            if 0 < fade_out and t > duration - fade_out:
                factor = min(factor, max(0.0, (duration - t) / fade_out))
            if factor < 1.0:
                frame = frame.copy()
                frame[:, :, 3] = (frame[:, :, 3] * factor).astype(frame.dtype)
        return frame

    try:
        if MOVIEPY_V2:
            return clip.transform(_filter)
        else:
            # MoviePy v1 fallback — use transform if available, else try vfx
            try:
                return clip.fl(lambda gf, t: _filter(gf, t))
            except AttributeError:
                return clip.fadein(fade_in).fadeout(fade_out)
    except Exception:
        # If anything fails, return the un‑faded clip rather than crashing
        return clip


def _fade_in_out(
    clip: VideoClip, total_duration: float, hold_ratio: float = 0.5
) -> VideoClip:
    """Apply symmetric fade-in / fade-out leaving *hold_ratio* visible.

    Delegates to ``_alpha_fade`` so that RGBA overlays fade correctly.
    """
    fade_dur = total_duration * (1.0 - hold_ratio) / 2.0
    if fade_dur <= 0:
        return clip
    return _alpha_fade(clip, total_duration, fade_in=fade_dur, fade_out=fade_dur)


def _gradient_bg(
    vw: int, vh: int, center_color: tuple, edge_color: tuple
) -> np.ndarray:
    """Create a radial gradient background frame (RGB only)."""
    cx, cy = vw / 2, vh / 2
    max_r = np.sqrt(cx ** 2 + cy ** 2)
    yy, xx = np.mgrid[0:vh, 0:vw]
    dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2) / max_r
    dist = np.clip(dist, 0, 1)
    r = (center_color[0] * (1 - dist) + edge_color[0] * dist).astype(np.uint8)
    g = (center_color[1] * (1 - dist) + edge_color[1] * dist).astype(np.uint8)
    b = (center_color[2] * (1 - dist) + edge_color[2] * dist).astype(np.uint8)
    return np.stack([r, g, b], axis=2)


def _create_text_clip(
    text: str,
    font_size: int,
    color: tuple,
    duration: float,
    font: str = None,
    max_width: int = None,
    y: int = None,
    shadow_color: tuple = None,
) -> VideoClip:
    """Create a text clip using PIL rendering on a full-frame transparent canvas.

    Renders text onto a 1920×1080 RGBA canvas via PIL for precise glyph
    measurement that properly accounts for accent marks (e.g. 'í') and
    descenders.  This avoids MoviePy TextClip height miscalculations that
    silently clip the bottom third of text when using ``method="caption"``
    (which is unsupported in MoviePy v2 and falls back to ``label``).

    Args:
        text: Text to render (multi-paragraph OK).
        font_size: Point size.
        color: RGB tuple for the main text colour.
        duration: Clip duration in seconds.
        font: Font name / path (defaults to ``_resolve_font()``).
        max_width: Max width in px for word-wrap (default 85 % of frame).
        y: Vertical anchor (top of first line) in px.  When *None* the
           text block is vertically centred on the frame.
        shadow_color: Optional RGB tuple; a soft two-layer shadow is drawn
           behind each glyph when set.
    """
    vw, vh = 1920, 1080
    if max_width is None:
        max_width = int(vw * 0.85)

    # ── Font ──────────────────────────────────────────────────
    try:
        pil_font = ImageFont.truetype(font or _resolve_font(), font_size)
    except Exception:
        pil_font = ImageFont.load_default()

    # ── Word-wrap ─────────────────────────────────────────────
    lines: list[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        current = ""
        for word in words:
            test = f"{current} {word}".strip()
            bbox = pil_font.getbbox(test)
            if (bbox[2] - bbox[0]) <= max_width:
                current = test
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)

    if not lines:
        lines = [text]

    line_height = font_size + 8          # spacing between baselines
    total_h = line_height * len(lines)

    start_y = y if y is not None else (vh - total_h) // 2

    # ── Render onto full-frame RGBA canvas ────────────────────
    canvas = Image.new("RGBA", (vw, vh), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    shadow_off = max(2, font_size // 20)

    r, g, b = color
    for i, line in enumerate(lines):
        bbox = pil_font.getbbox(line)
        line_w = bbox[2] - bbox[0]
        x = (vw - line_w) // 2
        line_y = start_y + i * line_height

        # Optional shadow (offset + tight inner)
        if shadow_color:
            sr, sg, sb = shadow_color
            draw.text((x + shadow_off, line_y + shadow_off),
                      line, font=pil_font, fill=(sr, sg, sb, 255))
            draw.text((x - 1, line_y - 1),
                      line, font=pil_font, fill=(sr, sg, sb, 200))
        # Main text
        draw.text((x, line_y), line, font=pil_font, fill=(r, g, b, 255))

    frame = np.array(canvas)

    def _make_frame(t: float) -> np.ndarray:
        return frame

    return VideoClip(_make_frame, duration=duration)


def _build_logo_image(initials: str, size: int, color_palette: dict) -> np.ndarray:
    """Create a circular logo image with initials, returns RGBA numpy array."""
    primary = color_palette.get("primary", (160, 22, 22))
    accent = color_palette.get("accent", (161, 117, 55))
    text_clr = color_palette.get("text", (225, 220, 215))
    secondary = color_palette.get("secondary", (12, 10, 10))

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Outer ring
    ring_w = 4
    draw.ellipse([0, 0, size - 1, size - 1], outline=accent + (180,), width=ring_w)

    # Inner filled circle
    margin = ring_w + 2
    draw.ellipse(
        [margin, margin, size - margin, size - margin],
        fill=secondary + (230,),
    )

    # Inner accent ellipse
    inner_m = size // 5
    draw.ellipse(
        [inner_m, inner_m, size - inner_m, size - inner_m],
        fill=primary + (40,),
    )

    # Initials text
    try:
        font = ImageFont.truetype(_resolve_font(), int(size * 0.30))
    except Exception:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), initials, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    tx = (size - tw) // 2
    ty = (size - th) // 2 - bbox[1]

    # Shadow
    draw.text((tx + 2, ty + 2), initials, fill=primary + (180,), font=font)
    # Main text
    draw.text((tx, ty), initials, fill=text_clr + (240,), font=font)

    return np.array(img)


def _build_cta_icon(kind: str, size: int, palette: dict) -> np.ndarray:
    """Draw a vector CTA icon (like / subscribe / bell) as an RGBA numpy array.

    Args:
        kind: one of ``"like"``, ``"subscribe"``, ``"bell"``.
        size: square side length in pixels (recommended 80-160).
        palette: ``COLOR_PALETTE`` dict with ``accent``, ``text``, ``primary``,
                 ``secondary`` keys.

    Returns:
        (size, size, 4) RGBA uint8 numpy array.
    """
    accent = palette.get("accent", (200, 160, 40))
    text_c = palette.get("text", (230, 230, 230))
    primary = palette.get("primary", (160, 22, 22))
    secondary = palette.get("secondary", (12, 10, 10))

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    s = size       # shorthand
    m = s // 2     # midpoint

    if kind == "like":
        # ── thumbs-up icon ──────────────────────────────────────
        # Palm: rounded rectangle
        palm_w, palm_h = int(s * 0.32), int(s * 0.50)
        palm_x, palm_y = (s - palm_w) // 2, int(s * 0.40)
        draw.rounded_rectangle(
            [palm_x, palm_y, palm_x + palm_w, palm_y + palm_h],
            radius=int(palm_w * 0.40), fill=accent + (220,),
        )
        # Thumb: vertical pill
        thumb_w, thumb_h = int(s * 0.22), int(s * 0.50)
        thumb_x, thumb_y = (s - thumb_w) // 2, int(s * 0.08)
        draw.rounded_rectangle(
            [thumb_x, thumb_y, thumb_x + thumb_w, thumb_y + thumb_h],
            radius=thumb_w // 2, fill=accent + (240,),
        )
        # Highlight on thumb
        hl_x, hl_w = thumb_x + 3, thumb_w - 6
        hl_y = thumb_y + int(thumb_h * 0.15)
        hl_h = int(thumb_h * 0.25)
        draw.rounded_rectangle(
            [hl_x, hl_y, hl_x + hl_w, hl_y + hl_h],
            radius=hl_w // 2, fill=text_c + (120,),
        )

    elif kind == "subscribe":
        # ── person silhouette + badge ────────────────────────────
        # Head: circle
        head_r = int(s * 0.14)
        head_cx, head_cy = m, int(s * 0.24)
        draw.ellipse(
            [head_cx - head_r, head_cy - head_r,
             head_cx + head_r, head_cy + head_r],
            fill=accent + (230,),
        )
        # Body: arc / wide U shape
        body_w, body_h = int(s * 0.42), int(s * 0.40)
        body_x0 = (s - body_w) // 2
        body_y0 = head_cy + head_r + int(s * 0.02)
        body_x1 = body_x0 + body_w
        body_y1 = body_y0 + body_h
        # Draw as a rounded rectangle then erase the top half to make it a bowl
        body_img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        body_draw = ImageDraw.Draw(body_img)
        body_draw.arc(
            [body_x0, body_y0, body_x1, body_y0 + body_h],
            start=180, end=0, fill=accent + (230,), width=int(s * 0.12),
        )
        img = Image.alpha_composite(img, body_img)

        # "+" badge on bottom-right
        badge_r = int(s * 0.13)
        badge_cx = int(s * 0.70)
        badge_cy = int(s * 0.72)
        draw.ellipse(
            [badge_cx - badge_r, badge_cy - badge_r,
             badge_cx + badge_r, badge_cy + badge_r],
            fill=primary + (230,),
        )
        # Plus lines
        pw = int(badge_r * 0.65)
        draw.line(
            [badge_cx - pw, badge_cy, badge_cx + pw, badge_cy],
            fill=text_c + (255,), width=max(3, s // 28),
        )
        draw.line(
            [badge_cx, badge_cy - pw, badge_cx, badge_cy + pw],
            fill=text_c + (255,), width=max(3, s // 28),
        )

    elif kind == "bell":
        # ── notification bell ────────────────────────────────────
        accent_bright = tuple(min(c + 40, 255) for c in accent)
        # Bell body: taller rounded shape (egg-like)
        bell_w, bell_h = int(s * 0.46), int(s * 0.54)
        bell_x0 = (s - bell_w) // 2
        bell_y0 = int(s * 0.10)
        bell_x1 = bell_x0 + bell_w
        bell_y1 = bell_y0 + bell_h
        draw.ellipse(
            [bell_x0, bell_y0, bell_x1, bell_y1],
            fill=accent + (230,),
        )
        # Bell curves: use arcs for the narrowed top (negative space in shield)
        arc_r = int(bell_w * 0.48)
        left_arc_x0 = bell_x0 - arc_r
        right_arc_x0 = bell_x1 - arc_r
        draw.arc(
            [left_arc_x0, bell_y0 - int(s * 0.04),
             left_arc_x0 + 2 * arc_r, bell_y0 + 2 * arc_r],
            start=0, end=90, fill=accent + (230,), width=max(3, s // 16),
        )
        draw.arc(
            [right_arc_x0, bell_y0 - int(s * 0.04),
             right_arc_x0 + 2 * arc_r, bell_y0 + 2 * arc_r],
            start=90, end=180, fill=accent + (230,), width=max(3, s // 16),
        )
        # Top clip (horizontal rounded rect)
        clip_w, clip_h = int(bell_w * 0.50), int(s * 0.10)
        clip_x = (s - clip_w) // 2
        clip_y = bell_y0 - clip_h // 2 - 1
        draw.rounded_rectangle(
            [clip_x, clip_y, clip_x + clip_w, clip_y + clip_h],
            radius=int(clip_w * 0.30), fill=accent_bright + (240,),
        )
        # Clapper (small circle at bottom)
        clap_r = int(s * 0.07)
        clap_cx, clap_cy = m, bell_y1 + int(s * 0.02)
        draw.ellipse(
            [clap_cx - clap_r, clap_cy - clap_r,
             clap_cx + clap_r, clap_cy + clap_r],
            fill=accent_bright + (240,),
        )
        # Notification dot (red)
        dot_r = int(s * 0.10)
        dot_cx = int(s * 0.74)
        dot_cy = int(s * 0.40)
        # White ring
        draw.ellipse(
            [dot_cx - dot_r - 2, dot_cy - dot_r - 2,
             dot_cx + dot_r + 2, dot_cy + dot_r + 2],
            fill=(255, 255, 255, 200),
        )
        # Red dot
        draw.ellipse(
            [dot_cx - dot_r, dot_cy - dot_r,
             dot_cx + dot_r, dot_cy + dot_r],
            fill=primary + (240,),
        )

    return np.array(img)


# ── TemplateGenerator ────────────────────────────────────────────

class TemplateGenerator:
    """Generate and cache channel template mini-videos (intro, cta, outro)."""

    VIDEO_SIZE = (1920, 1080)
    VIDEO_FPS = 24

    def __init__(
        self,
        channel_slug: str,
        channel_config=None,
        output_dir: Path = None,
    ):
        self.slug = channel_slug
        self.config = channel_config
        self.output_dir = output_dir or Path("output/templates") / channel_slug
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # ── Public API ──────────────────────────────────────────────

    def generate_intro(self) -> Optional[Path]:
        """Generate intro template: channel logo + name with Ken Burns effect.

        Duration: INTRO_DURATION_SEC from config (default 3s).
        """
        duration = self._cfg("INTRO_DURATION_SEC", 3.0)
        display_name = self._cfg("CANAL_DISPLAY_NAME", self.slug)
        subtitle = self._cfg("INTRO_SUBTITLE", "")
        color_pal = self._cfg("COLOR_PALETTE", {"accent": (200, 160, 40), "text": (230, 230, 230)})
        initials = self._cfg("CANAL_INITIALS", display_name[:2].upper() if display_name else "AT")
        logo_size = self._cfg("LOGO_SIZE", 140)
        font_size = self._cfg("INTRO_FONT_SIZE", 68)
        sub_font_size = self._cfg("INTRO_SUBTITLE_FONT_SIZE", 28)

        output_path = self.output_dir / "intro.mp4"
        try:
            vw, vh = self.VIDEO_SIZE
            font = _resolve_font()
            accent_color = color_pal.get("accent", (200, 160, 40))
            text_color = color_pal.get("text", (230, 230, 230))
            bg_center = (8, 8, 12)
            bg_edge = (2, 2, 8)

            # Background with subtle Ken Burns zoom
            bg_frame = _gradient_bg(vw, vh, bg_center, bg_edge)

            def make_bg(t: float) -> np.ndarray:
                # Subtle zoom: 1.0 → 1.03 over duration
                if duration > 0:
                    scale = 1.0 + 0.03 * min(t / duration, 1.0)
                else:
                    scale = 1.0
                if scale == 1.0:
                    return bg_frame
                pil_img = Image.fromarray(bg_frame)
                new_w = int(vw * scale)
                new_h = int(vh * scale)
                pil_img = pil_img.resize((new_w, new_h), Image.LANCZOS)
                left = (new_w - vw) // 2
                top = (new_h - vh) // 2
                cropped = pil_img.crop((left, top, left + vw, top + vh))
                return np.array(cropped)

            bg = VideoClip(make_bg, duration=duration)

            clips = [bg]

            # Logo at top with scale animation
            try:
                logo_arr = _build_logo_image(initials, logo_size, color_pal)
                logo_clip = ImageClip(logo_arr).with_duration(duration)
                logo_y = int(vh * 0.18)
                logo_clip = logo_clip.with_position(("center", logo_y))
                if MOVIEPY_V2:
                    logo_clip = logo_clip.with_effects([
                        vfx.Resize(lambda t: 0.85 + 0.15 * min(t / (duration * 0.5), 1.0) if duration > 0 else 1.0)
                    ])
                logo_clip = _fade_in_out(logo_clip, duration, hold_ratio=0.25)
                clips.append(logo_clip)
            except Exception:
                logger.warning("Logo generation failed for intro — skipping", exc_info=True)

            # Decorative line
            line_y = int(vh * 0.36)
            line_w = int(vw * 0.15)
            x_start = (vw - line_w) // 2
            line_frame = np.zeros((vh, vw, 4), dtype=np.uint8)
            line_frame[line_y:line_y + 2, x_start:x_start + line_w] = (*accent_color, 180)

            def make_line(t: float) -> np.ndarray:
                return line_frame

            line_clip = VideoClip(make_line, duration=duration)
            line_clip = _fade_in_out(line_clip, duration, hold_ratio=0.3)
            clips.append(line_clip)

            # Channel name
            max_w = int(vw * 0.80)
            label = _create_text_clip(display_name, font_size, accent_color,
                                      duration, font, max_w, y=int(vh * 0.46))
            label = _fade_in_out(label, duration, hold_ratio=0.4)
            clips.append(label)

            # Subtitle
            if subtitle:
                sub = _create_text_clip(subtitle, sub_font_size, text_color,
                                        duration, font, max_w, y=int(vh * 0.56))
                sub = _fade_in_out(sub, duration, hold_ratio=0.4)
                clips.append(sub)

            composite = CompositeVideoClip(clips, size=self.VIDEO_SIZE)
            composite.write_videofile(
                str(output_path),
                fps=self.VIDEO_FPS,
                codec="libx264",
                bitrate="2000k",
                preset="medium",
                audio_codec="aac",
                threads=os.cpu_count() or 4,
                ffmpeg_params=["-movflags", "+faststart"],
            )
            composite.close()
            logger.info("Intro template generated → %s", output_path)
            return output_path

        except Exception:
            logger.exception("Intro template generation failed")
            return None

    def generate_cta(self) -> Optional[Path]:
        """Generate CTA template: like/subscribe/bell — clean branded minivideo.

        Duration: min(OUTRO_DURATION_SEC / 2, 5.0) from config (default 2.5s).
        Uses channel palette gradient background + vector PIL icons with
        labelled text – no AI images, no slug-in-prompt, no random backgrounds.
        """
        outro_total = self._cfg("OUTRO_DURATION_SEC", 5.0)
        duration = min(outro_total / 2.0, 5.0)
        cta_like = self._cfg("OUTRO_CTA_LIKE", "Like")
        cta_sub = self._cfg("OUTRO_CTA_SUBSCRIBE", "Suscríbete")
        cta_bell = self._cfg("OUTRO_CTA_BELL", "Activa la campana")
        color_pal = self._cfg(
            "COLOR_PALETTE",
            {"accent": (200, 160, 40), "text": (230, 230, 230),
             "primary": (160, 22, 22), "secondary": (12, 10, 10)},
        )
        font_size = self._cfg("OUTRO_FONT_SIZE", 46)

        output_path = self.output_dir / "cta.mp4"

        try:
            vw, vh = self.VIDEO_SIZE
            font = _resolve_font()
            accent_color = color_pal.get("accent", (200, 160, 40))
            text_color = color_pal.get("text", (230, 230, 230))

            # ── Background: single clean gradient with subtle zoom ──
            bg_frame = _gradient_bg(vw, vh, (10, 10, 20), (3, 3, 10))

            def _make_bg(t: float) -> np.ndarray:
                if duration <= 0:
                    return bg_frame
                scale = 1.0 + 0.03 * min(t / duration, 1.0)
                if scale == 1.0:
                    return bg_frame
                pil_img = Image.fromarray(bg_frame)
                new_w, new_h = int(vw * scale), int(vh * scale)
                pil_img = pil_img.resize((new_w, new_h), Image.LANCZOS)
                left = (new_w - vw) // 2
                top = (new_h - vh) // 2
                return np.array(pil_img.crop((left, top, left + vw, top + vh)))

            bg = VideoClip(_make_bg, duration=duration)
            clips = [bg]

            # ── Three stacked CTA cues (icon + label, all visible together) ──
            icon_size = 100
            gap = int(font_size * 2.8)
            row_spill = (icon_size + gap) * 3
            start_y = (vh - row_spill) // 2 + int(gap * 0.25)

            # PIL font for text labels (we side-render in compact strips)
            try:
                pil_font = ImageFont.truetype(font, font_size)
            except Exception:
                pil_font = ImageFont.load_default()

            cta_items = [
                ("like", cta_like),
                ("subscribe", cta_sub),
                ("bell", cta_bell),
            ]

            for idx, (icon_kind, label) in enumerate(cta_items):
                y_center = start_y + idx * (icon_size + gap)
                stagger = idx * 0.18  # staggered fade-in delay per row

                # ── Icon ──
                icon_arr = _build_cta_icon(icon_kind, icon_size, color_pal)
                icon_clip = ImageClip(icon_arr).with_duration(duration)
                icon_y = y_center + (icon_size - icon_size) // 2  # vertically centred
                icon_x = vw // 2 - int(icon_size * 1.6) - gap // 2
                icon_clip = icon_clip.with_position((icon_x, icon_y))
                icon_clip = _alpha_fade(icon_clip, duration, fade_in=0.35, fade_out=0.0)
                if stagger > 0:
                    # delay visibility
                    icon_clip = icon_clip.with_start(stagger)
                clips.append(icon_clip)

                # ── Pulse ring around icon ──
                pulse_cx = icon_x + icon_size // 2
                pulse_cy = y_center + icon_size // 2
                pulse_radius = icon_size // 2 + 6

                def _make_pulse(
                    t: float,
                    cx=pulse_cx, cy=pulse_cy, r=pulse_radius,
                    ac=accent_color, d=duration, st=stagger,
                ) -> np.ndarray:
                    local_t = t - st
                    if local_t < 0 or local_t >= d:
                        return np.zeros((vh, vw, 4), dtype=np.uint8)
                    progress = min(local_t / d, 1.0) if d > 0 else 1.0
                    alpha = int(90 * (1.0 - progress))
                    frame = np.zeros((vh, vw, 4), dtype=np.uint8)
                    try:
                        pil_img = Image.fromarray(frame)
                        draw2 = ImageDraw.Draw(pil_img)
                        rr = int(r * (1 + progress * 0.4))
                        draw2.ellipse(
                            [cx - rr, cy - rr, cx + rr, cy + rr],
                            outline=ac + (alpha,), width=3,
                        )
                        return np.array(pil_img)
                    except Exception:
                        return frame

                pulse = VideoClip(_make_pulse, duration=duration)
                clips.append(pulse)

                # ── Label text (rendered in a compact strip, positioned beside icon) ──
                label_strip_w = int(vw * 0.55)
                label_strip_h = font_size + 16
                label_canvas = Image.new("RGBA", (label_strip_w, label_strip_h), (0, 0, 0, 0))
                label_draw = ImageDraw.Draw(label_canvas)
                label_bbox = pil_font.getbbox(label)
                label_text_w = label_bbox[2] - label_bbox[0]
                label_x = 0
                label_y = (label_strip_h - (label_bbox[3] - label_bbox[1])) // 2 - label_bbox[1]
                label_draw.text((label_x, label_y), label, font=pil_font,
                                fill=text_color + (255,))

                label_arr = np.array(label_canvas)
                label_clip = ImageClip(label_arr).with_duration(duration)
                label_clip = label_clip.with_position(
                    (icon_x + icon_size + 20, y_center + (icon_size - label_strip_h) // 2)
                )
                label_clip = _alpha_fade(label_clip, duration, fade_in=0.35, fade_out=0.0)
                if stagger > 0:
                    label_clip = label_clip.with_start(stagger)
                clips.append(label_clip)

            composite = CompositeVideoClip(clips, size=self.VIDEO_SIZE)
            composite.write_videofile(
                str(output_path),
                fps=self.VIDEO_FPS,
                codec="libx264",
                bitrate="2000k",
                preset="medium",
                audio_codec="aac",
                threads=os.cpu_count() or 4,
                ffmpeg_params=["-movflags", "+faststart"],
            )
            composite.close()
            logger.info("CTA template generated → %s (%.1fs)", output_path, duration)
            return output_path

        except Exception:
            logger.exception("CTA template generation failed")
            return None

    def generate_outro(self) -> Optional[Path]:
        """Generate outro template: final branding with channel name + CTA.

        Duration: OUTRO_DURATION_SEC from config (default 5s for canal2/3, 6s for canal4).
        """
        outro_total = self._cfg("OUTRO_DURATION_SEC", 6.0)
        duration = outro_total
        display_name = self._cfg("CANAL_DISPLAY_NAME", self.slug)
        outro_tagline = self._cfg("CANAL_OUTRO_TAGLINE", "Suscríbete para más.")
        color_pal = self._cfg("COLOR_PALETTE", {"accent": (200, 160, 40), "text": (230, 230, 230)})
        initials = self._cfg("CANAL_INITIALS", display_name[:2].upper() if display_name else "AT")
        logo_size = self._cfg("LOGO_SIZE", 120)
        font_size = self._cfg("OUTRO_FONT_SIZE", 52)
        tagline_font_size = self._cfg("INTRO_SUBTITLE_FONT_SIZE", 28)

        output_path = self.output_dir / "outro.mp4"

        try:
            vw, vh = self.VIDEO_SIZE
            font = _resolve_font()
            accent_color = color_pal.get("accent", (200, 160, 40))
            text_color = color_pal.get("text", (230, 230, 230))

            # Dark cinematic background with subtle zoom
            bg_frame = _gradient_bg(vw, vh, (6, 6, 14), (0, 0, 4))

            def make_bg(t: float) -> np.ndarray:
                if duration > 0:
                    scale = 1.0 + 0.04 * min(t / duration, 1.0)
                else:
                    scale = 1.0
                if scale == 1.0:
                    return bg_frame
                pil_img = Image.fromarray(bg_frame)
                new_w = int(vw * scale)
                new_h = int(vh * scale)
                pil_img = pil_img.resize((new_w, new_h), Image.LANCZOS)
                left = (new_w - vw) // 2
                top = (new_h - vh) // 2
                cropped = pil_img.crop((left, top, left + vw, top + vh))
                return np.array(cropped)

            bg = VideoClip(make_bg, duration=duration)
            clips = [bg]

            # Logo
            try:
                logo_arr = _build_logo_image(initials, logo_size, color_pal)
                logo_clip = ImageClip(logo_arr).with_duration(duration)
                logo_y = int(vh * 0.12)
                logo_clip = logo_clip.with_position(("center", logo_y))
                clips.append(logo_clip)
            except Exception:
                logger.warning("Logo generation failed for outro — skipping", exc_info=True)

            # Channel name
            max_w = int(vw * 0.80)
            name = _create_text_clip(display_name, font_size, accent_color,
                                     duration, font, max_w, y=int(vh * 0.42))
            name = _fade_in_out(name, duration, hold_ratio=0.5)
            clips.append(name)

            # Tagline below
            tagline = _create_text_clip(outro_tagline, tagline_font_size, text_color,
                                        duration, font, max_w, y=int(vh * 0.52))
            tagline = _fade_in_out(tagline, duration, hold_ratio=0.4)
            clips.append(tagline)

            # Crossfade to black at end (overlay)
            fade_dur = duration * 0.3
            def _make_fade(t: float) -> np.ndarray:
                if t < duration - fade_dur:
                    return np.zeros((vh, vw, 4), dtype=np.uint8)
                alpha = int(255 * min((t - (duration - fade_dur)) / fade_dur, 1.0))
                frame = np.zeros((vh, vw, 4), dtype=np.uint8)
                frame[:, :, :3] = 0
                frame[:, :, 3] = alpha
                return frame

            fade_overlay = VideoClip(_make_fade, duration=duration)
            clips.append(fade_overlay)

            composite = CompositeVideoClip(clips, size=self.VIDEO_SIZE)
            composite.write_videofile(
                str(output_path),
                fps=self.VIDEO_FPS,
                codec="libx264",
                bitrate="2000k",
                preset="medium",
                audio_codec="aac",
                threads=os.cpu_count() or 4,
                ffmpeg_params=["-movflags", "+faststart"],
            )
            composite.close()
            logger.info("Outro template generated → %s", output_path)
            return output_path

        except Exception:
            logger.exception("Outro template generation failed")
            return None

    def generate_all(self) -> dict:
        """Generate all three templates. Returns dict with segment_type → path."""
        return {
            "intro": self.generate_intro(),
            "cta": self.generate_cta(),
            "outro": self.generate_outro(),
        }

    # ── Internal helpers ────────────────────────────────────────

    def _cfg(self, key: str, default=None):
        """Get a config value from the channel config object or dict."""
        if self.config is None:
            return default
        if isinstance(self.config, dict):
            return self.config.get(key, default)
        return getattr(self.config, key, default)
