#!/usr/bin/env python3
"""Genera banner y avatar con Pollo AI para los canales nuevos 2026.

- 1 banner 16:9 y 1 avatar 1:1 por canal.
- Banner: arte IA + texto nítido superpuesto con Pillow (nombre/tagline).
- Avatar: arte IA + variante con iniciales superpuestas.
- Guarda variantes en output/thumbnails/<slug>/ai/ y un manifest.json.
- NO sobrescribe banner.jpg/avatar.jpg: quedan para elegir desde la UI.

Uso:
  OUTPUT_DIR=/root/autotube/output DATABASE_PATH=/root/autotube/autotube.db \
  python3 scripts/generate_channel_assets_pollo.py --all --force
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("OUTPUT_DIR", str(ROOT / "output"))
sys.path.insert(0, str(ROOT))

from config.config_bridge import get_channel_config  # noqa: E402
from pipeline.ai_image_generator import AIImageGenerator, PolloAIError  # noqa: E402
from pipeline.channel_profile_generator import _find_font, _get_rgb  # noqa: E402

CHANNELS = ["canal6", "canal7", "canal8", "canal9"]
MODEL = os.getenv("POLLO_IMAGE_MODEL", "pollo-image-v2")


def _out_dir(slug: str) -> Path:
    d = Path(os.environ["OUTPUT_DIR"]) / "thumbnails" / slug / "ai"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _url(slug: str, name: str) -> str:
    return f"/api/static/output/thumbnails/{slug}/ai/{name}"


def _generate(cfg, kind: str, out_dir: Path, force: bool) -> Path | None:
    raw = out_dir / f"{kind}_pollo.jpg"
    if raw.exists() and raw.stat().st_size > 0 and not force:
        print(f"   · {kind}: ya existe {raw.name} (skip)", flush=True)
        return raw
    prompt = getattr(cfg, f"AI_{kind.upper()}_PROMPT", "") or ""
    if not prompt:
        print(f"   ⚠️ {kind}: falta AI_{kind.upper()}_PROMPT", flush=True)
        return None
    aspect = "16:9" if kind == "banner" else "1:1"
    print(f"   · {kind}: generando con Pollo ({aspect})…", flush=True)
    t0 = time.time()
    try:
        gen = AIImageGenerator(model=MODEL, db=None)
        path = gen.generate(prompt, raw, aspect_ratio=aspect)
        print(f"   ✅ {kind}: {path} ({time.time()-t0:.0f}s)", flush=True)
        return Path(path)
    except PolloAIError as exc:
        print(f"   ❌ {kind}: PolloAIError: {exc}", flush=True)
        return None
    except Exception as exc:  # noqa: BLE001
        print(f"   ❌ {kind}: error: {exc}", flush=True)
        return None


def _fit(raw: Path, size: tuple[int, int]):
    from PIL import Image, ImageOps
    img = Image.open(raw).convert("RGB")
    return ImageOps.fit(img, size, method=Image.LANCZOS, centering=(0.5, 0.5))


def _process_banner(cfg, raw: Path, out_dir: Path) -> dict:
    from PIL import Image, ImageDraw
    img = _fit(raw, (2560, 1440))
    raw_path = out_dir / "banner_ai_raw.jpg"
    img.save(raw_path, "JPEG", quality=95)

    final = img.convert("RGBA")
    overlay = Image.new("RGBA", final.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    # Banda central semi-transparente para legibilidad (zona segura aprox.)
    draw.rounded_rectangle(
        [280, 560, 2280, 1000], radius=40, fill=(0, 0, 0, 120),
    )
    final = Image.alpha_composite(final, overlay)
    draw = ImageDraw.Draw(final)

    palette = getattr(cfg, "COLOR_PALETTE", {})
    text_col = _get_rgb(palette, "text", (245, 240, 230))
    name = str(getattr(cfg, "CANAL_DISPLAY_NAME", "") or "").upper()
    tagline = str(getattr(cfg, "CANAL_TAGLINE", "") or "")
    if len(tagline) > 90:
        tagline = tagline[:87] + "..."
    try:
        f_name = _find_font(118)
        bbox = draw.textbbox((0, 0), name, font=f_name)
        tw = bbox[2] - bbox[0]
        tx, ty = (2560 - tw) // 2, 650
        draw.text((tx + 3, ty + 3), name, fill=(0, 0, 0, 180), font=f_name)
        draw.text((tx, ty), name, fill=text_col, font=f_name)
    except Exception:
        pass
    if tagline:
        try:
            f_tag = _find_font(36)
            bbox = draw.textbbox((0, 0), tagline, font=f_tag)
            tw = bbox[2] - bbox[0]
            tx, ty = (2560 - tw) // 2, 830
            draw.text((tx + 2, ty + 2), tagline, fill=(0, 0, 0, 150), font=f_tag)
            draw.text((tx, ty), tagline, fill=text_col, font=f_tag)
        except Exception:
            pass

    final_path = out_dir / "banner_ai.jpg"
    final.convert("RGB").save(final_path, "JPEG", quality=95)
    return [
        {
            "kind": "banner", "file": final_path.name,
            "raw": raw_path.name, "label": "IA + texto",
            "width": 2560, "height": 1440,
        },
        {
            "kind": "banner", "file": raw_path.name,
            "raw": None, "label": "IA sin texto",
            "width": 2560, "height": 1440,
        },
    ]


def _process_avatar(cfg, raw: Path, out_dir: Path) -> list[dict]:
    from PIL import Image, ImageDraw
    img = _fit(raw, (800, 800))
    art_path = out_dir / "avatar_ai_art.jpg"
    img.save(art_path, "JPEG", quality=95)

    final = img.convert("RGBA")
    overlay = Image.new("RGBA", final.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.ellipse([70, 70, 730, 730], fill=(0, 0, 0, 105))
    final = Image.alpha_composite(final, overlay)
    draw = ImageDraw.Draw(final)

    palette = getattr(cfg, "COLOR_PALETTE", {})
    text_col = _get_rgb(palette, "text", (245, 240, 230))
    initials = str(getattr(cfg, "CANAL_INITIALS", "AI") or "AI")[:2].upper()
    try:
        font = _find_font(280)
        bbox = draw.textbbox((0, 0), initials, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text(((800 - tw) / 2 + 4, (800 - th) / 2 + 4), initials,
                  fill=(0, 0, 0, 160), font=font)
        draw.text(((800 - tw) / 2, (800 - th) / 2), initials,
                  fill=text_col, font=font)
    except Exception:
        pass

    final_path = out_dir / "avatar_ai.jpg"
    final.convert("RGB").save(final_path, "JPEG", quality=95)
    return [
        {"kind": "avatar", "file": final_path.name, "raw": art_path.name,
         "label": "IA + iniciales", "width": 800, "height": 800},
        {"kind": "avatar", "file": art_path.name, "raw": None,
         "label": "IA sola", "width": 800, "height": 800},
    ]


def _pollo_meta(out_dir: Path) -> dict:
    meta = {}
    for name in ("banner_pollo.pollo.json", "avatar_pollo.pollo.json"):
        p = out_dir / name
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            # Nunca exponer cookies ni rutas internas sensibles.
            for k in ("cookie", "cookie_used", "session", "token", "path"):
                data.pop(k, None)
            meta[name] = data
        except Exception:
            continue
    return meta


def _write_manifest(slug: str, cfg, out_dir: Path, variants: list[dict]) -> Path:
    manifest = {
        "slug": slug,
        "name": getattr(cfg, "CANAL_DISPLAY_NAME", slug),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": MODEL,
        "banner_prompt": getattr(cfg, "AI_BANNER_PROMPT", ""),
        "avatar_prompt": getattr(cfg, "AI_AVATAR_PROMPT", ""),
        "negative_prompt": getattr(cfg, "AI_NEGATIVE_PROMPT", ""),
        "pollo_meta": _pollo_meta(out_dir),
        "variants": [
            {**v, "url": _url(slug, v["file"])}
            for v in variants
        ],
    }
    p = out_dir / "manifest.json"
    p.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return p


def _existing_variants(out_dir: Path) -> list[dict]:
    """Variantes ya presentes en disco (para reconstruir manifests sin Pollo)."""
    specs = [
        ("banner_ai.jpg", "banner", "IA + texto"),
        ("banner_ai_raw.jpg", "banner", "IA sin texto"),
        ("avatar_ai.jpg", "avatar", "IA + iniciales"),
        ("avatar_ai_art.jpg", "avatar", "IA sola"),
    ]
    out = []
    for fname, kind, label in specs:
        f = out_dir / fname
        if f.exists() and f.stat().st_size > 0:
            out.append({
                "kind": kind, "file": fname,
                "label": label,
                "width": 2560 if kind == "banner" else 800,
                "height": 1440 if kind == "banner" else 800,
            })
    return out


def run_channel(slug: str, force: bool) -> dict:
    print(f"\n=== {slug} ===", flush=True)
    cfg = get_channel_config(slug)
    out_dir = _out_dir(slug)
    variants: list[dict] = []

    banner_raw = _generate(cfg, "banner", out_dir, force)
    if banner_raw:
        variants.extend(_process_banner(cfg, banner_raw, out_dir))
    avatar_raw = _generate(cfg, "avatar", out_dir, force)
    if avatar_raw:
        variants.extend(_process_avatar(cfg, avatar_raw, out_dir))

    variants = _existing_variants(out_dir)
    manifest = _write_manifest(slug, cfg, out_dir, variants)
    print(f"   manifest: {manifest} ({len(variants)} variantes)", flush=True)
    return {"slug": slug, "variants": variants, "manifest": str(manifest)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--slug", action="append", default=[], help="canal (repetible)")
    ap.add_argument("--all", action="store_true", help="los 4 canales")
    ap.add_argument("--force", action="store_true", help="regenerar aunque exista")
    ap.add_argument("--rebuild-manifests", action="store_true",
                    help="solo reconstruir manifest.json con los archivos existentes")
    args = ap.parse_args()

    slugs = CHANNELS if args.all else args.slug
    if not slugs:
        print("Indica --all o --slug canal6", file=sys.stderr)
        return 2
    if args.rebuild_manifests:
        for slug in slugs:
            cfg = get_channel_config(slug)
            out_dir = _out_dir(slug)
            variants = _existing_variants(out_dir)
            manifest = _write_manifest(slug, cfg, out_dir, variants)
            print(f"{slug}: {manifest} ({len(variants)} variantes)")
        return 0
    print("OUTPUT_DIR =", os.environ.get("OUTPUT_DIR"))
    results = []
    for slug in slugs:
        try:
            results.append(run_channel(slug, args.force))
        except Exception as exc:  # noqa: BLE001
            print(f"❌ {slug}: {exc}", flush=True)
            results.append({"slug": slug, "error": str(exc)})
    print("\n=== RESUMEN ===")
    for r in results:
        print(json.dumps(r, ensure_ascii=False))
    return 0 if all("error" not in r for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
