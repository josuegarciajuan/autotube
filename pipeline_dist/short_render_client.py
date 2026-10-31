"""Cliente de la casa para el render de SHORTS distribuido (flota SuperServer).

Prepara un staging con los insumos (assets + audio + srt) y un `spec.json`, envía
una ejecución `autotube-short-render` al motor, espera el resultado y copia el
`render.mp4` al destino local. Reutiliza el MISMO `render_short_hybrid` que la
casa (dentro del contenedor), así que la salida es equivalente por construcción.

Nunca se usa como ruta única: el llamador hace fallback local ante cualquier
error (fail-open). Kill-switch: `AUTOTUBE_DIST_SHORTS` (0/ausente = local).
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from . import dsl_client


def enabled() -> bool:
    return str(os.environ.get("AUTOTUBE_DIST_SHORTS", "")).strip().lower() in ("1", "true", "yes", "on")


def _excluded_nodes() -> list:
    raw = os.environ.get("AUTOTUBE_DIST_EXCLUDED_NODES", "")
    return [n.strip() for n in str(raw).split(",") if n.strip()]


def _staging_root() -> str:
    root = os.environ.get("SS_STAGING_DIR", "/var/lib/taildeck/staging")
    try:
        os.makedirs(root, exist_ok=True)
    except OSError:
        root = tempfile.gettempdir()
    return root


def render_short_dist(
    asset_items: list,
    audio_path,
    output_path,
    audio_duration: float | None = None,
    srt_path=None,
    scene_ranges: list | None = None,
    crossfade_dur: float = 1.0,
    bg_color_hex: str = "0a0a1a",
    timeout: float = 2400.0,
    max_duration_sec: float | None = None,
) -> Path:
    """Renderiza el short en la flota y devuelve el Path del mp4. Lanza ante fallo."""
    staging = Path(tempfile.mkdtemp(prefix="short-render-", dir=_staging_root()))
    # El staging es PLANO: el motor lo empuja a /work/in (dest 'in'), así que los
    # ficheros quedan en /work/in/<name> y el spec los referencia como "in/<name>".
    in_dir = staging

    # Copia assets/audio/srt al staging con nombres canónicos y construye el spec
    # con rutas relativas a /work (in/...).
    assets = []
    for idx, a in enumerate(asset_items):
        if not a:
            assets.append(None)
            continue
        src = Path(a.get("path"))
        if not src.exists():
            assets.append(None)
            continue
        dst_name = f"a{idx:03d}{src.suffix or '.bin'}"
        shutil.copy2(src, in_dir / dst_name)
        assets.append({"path": f"in/{dst_name}", "type": a.get("type", "image")})

    audio_dst = in_dir / f"audio{Path(audio_path).suffix or '.mp3'}"
    shutil.copy2(audio_path, audio_dst)

    srt_rel = None
    if srt_path and Path(srt_path).exists():
        srt_dst = in_dir / "sub.srt"
        shutil.copy2(srt_path, srt_dst)
        srt_rel = "in/sub.srt"

    spec = {
        "assets": assets,
        "audio": f"in/{audio_dst.name}",
        "srt": srt_rel,
        "audio_duration": audio_duration,
        "scene_ranges": scene_ranges or None,
        "crossfade_dur": crossfade_dur,
        "bg_color_hex": bg_color_hex,
        "max_duration_sec": max_duration_sec,
        "output": "out/render.mp4",
    }
    import json
    (staging / "spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")

    eid = dsl_client.submit(
        "autotube-short-render",
        {
            "stagingDir": str(staging),
            "image": os.environ.get("AUTOTUBE_SHORTS_IMAGE", "autotube-worker:1"),
            "worker": "pipeline_dist/short_render_worker.py",
            "codeDir": os.environ.get("AUTOTUBE_RUNTIME_DIR", "/opt/taildeck/autotube"),
            "req": {"cores": 4, "memMb": 2048},
        },
        label="autotube short render",
        max_attempts=2,
        deadline_sec=int(timeout),
        excluded_nodes=_excluded_nodes() or None,
        req={"cores": 4, "memMb": 2048},
    )
    summary = dsl_client.wait(eid, timeout=timeout, poll=3.0)
    if summary.get("status") != "done":
        raise RuntimeError(f"render distribuido {eid} no terminó ok: {summary.get('status')} {summary.get('error')}")
    res = (summary.get("result") or {}).get("render") or {}
    out_dir = res.get("outDir")
    filename = res.get("filename")
    if not out_dir or not filename:
        raise RuntimeError(f"render distribuido {eid} sin artefacto: {res}")
    src = Path(out_dir) / filename
    if not src.exists():
        raise RuntimeError(f"artefacto ausente: {src}")
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, output_path)
    # The worker validates too, but an older image on a node may not: verify the
    # artifact copied back to the house before it reaches the short queue.
    from pipeline.shorts_media import validate_short_render
    validate_short_render(
        output_path, max_duration_sec=max_duration_sec,
    )
    return Path(output_path)
