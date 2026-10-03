"""Orquestador distribuido: envuelve el pipeline clásico SIN modificarlo.

Hereda `PipelineOrchestrator` y solo sustituye `phase_video` por una versión que
pre-renderiza las escenas en los nodos de SuperServer y las deposita en la caché
de segmentos que el editor legacy ya sabe reutilizar
(`output/videos/segments/<video_id>/scene_NNNN.mp4`).

Garantías:
  - Si algo falla o no es seguro (asset repetido, escena sin asset, nodo caído),
    se cae al render local: `super().phase_video(...)`.
  - Todo-o-nada: no se deja un conjunto parcial de segmentos que rompería el
    dedup/offsets del editor si este tuviera que completar escenas en local.
  - El nodo nunca ve la DB ni secretos: solo manifiesto + asset.
"""
from __future__ import annotations

import logging
import os
import shutil
import zlib
from pathlib import Path
from typing import Optional

from config import settings as settings_mod
from orchestrator import PipelineOrchestrator
from . import dsl_client
from .model import sanitize_id
from .scene_plan import (
    DuplicateAssetError,
    MissingAssetError,
    ScenePlanError,
    build_manifests,
    stage_scene,
)
from .validate import validate_scene_result

logger = logging.getLogger(__name__)

SEGMENT_NAME = "scene_{:04d}.mp4"


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class DistributedOrchestrator(PipelineOrchestrator):
    """Pipeline clásico + render de escenas repartido en la flota."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Parches de estabilidad del LLM, aditivos y solo en este proceso.
        try:
            from .llm_patch import apply_llm_stability_patches
            apply_llm_stability_patches()
        except Exception as exc:  # nunca impedir la construcción
            logger.warning("[%s] No se pudieron aplicar parches LLM: %s", self.canal, exc)

    # ── Fase 4 sustituida: render distribuido con fallback local ────────────
    def phase_video(self, script: dict, audio_data: dict,
                    media_assets: list, job_id: int = None) -> Optional[dict]:
        if _env_flag("AUTOTUBE_DIST_RENDER", False):
            try:
                if self._pre_render_scenes(script, audio_data, media_assets, job_id):
                    logger.info("[%s] Render distribuido completo; ensamblado local.",
                                self.canal)
            except Exception as exc:  # noqa: BLE001 — cualquier fallo => local
                logger.warning(
                    "[%s] Render distribuido no aplicable (%s: %s). "
                    "Se renderiza en local como hasta ahora.",
                    self.canal, type(exc).__name__, exc,
                )
        return super().phase_video(script, audio_data, media_assets, job_id=job_id)

    # ── Implementación ──────────────────────────────────────────────────────
    def _pre_render_scenes(self, script: dict, audio_data: dict,
                           media_assets: list, job_id: Optional[int]) -> bool:
        scene_ranges = getattr(self, "_last_scene_ranges", None)
        if not scene_ranges:
            raise ScenePlanError("sin _last_scene_ranges (fase media no reconcilió)")
        if len(scene_ranges) < 2:
            raise ScenePlanError("demasiado pocas escenas para distribuir")

        cfg = self.config
        fps = float(getattr(cfg, "FPS", 30) or 30)
        res = getattr(cfg, "VIDEO_RESOLUTION", None)
        if res and len(res) == 2:
            width, height = int(res[0]), int(res[1])
        else:
            width, height = 1920, 1080
        preset = str(getattr(cfg, "FFMPEG_PRESET", "veryfast") or "veryfast")

        seed = zlib.crc32(
            f"{self.canal}:{self.db_video_id}:{script.get('id')}".encode("utf-8")
        )

        manifests = build_manifests(
            scene_ranges, media_assets,
            fps=fps, width=width, height=height, seed=seed,
            zoom_min=float(getattr(cfg, "KEN_BURNS_ZOOM_MIN", 1.0) or 1.0),
            zoom_max=float(getattr(cfg, "KEN_BURNS_ZOOM_MAX", 1.08) or 1.08),
            encoding={"codec": "libx264", "crf": 16, "preset": preset, "pix_fmt": "yuv420p"},
        )

        seg_dir = Path(settings_mod.VIDEOS_DIR) / "segments" / str(
            self.db_video_id if self.db_video_id else job_id or "adhoc"
        )
        # ABSOLUTA obligatoria: el engine hace `fs.existsSync(p.src)` desde SU
        # cwd (control plane) y omite el push EN SILENCIO si el staging es
        # relativo → /work/in queda vacío y el worker falla con "manifest
        # ilegible". Resolver aquí garantiza que el push encuentra la escena.
        seg_dir = seg_dir.resolve()
        seg_dir.mkdir(parents=True, exist_ok=True)
        staging = seg_dir / "_dist_staging"
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)

        scene_refs = []
        for i, manifest in enumerate(manifests):
            asset_path = str(media_assets[i].get("path") or "")
            scene_refs.append(stage_scene(str(staging), manifest, asset_path))

        run_id = sanitize_id(
            f"atube-render-{self.canal}-{self.db_video_id or job_id or 'x'}-{zlib.crc32(str(seed).encode()):x}"
        )
        # El exec-id determinista hace que el engine REUTILICE el resultado de una
        # corrida anterior (incluido un `failed`), sobre todo en pruebas. Un nonce
        # por corrida (AUTOTUBE_DIST_EXEC_NONCE) fuerza ejecución fresca.
        _nonce = os.environ.get("AUTOTUBE_DIST_EXEC_NONCE", "").strip()
        if _nonce:
            run_id = sanitize_id(f"{run_id}-{_nonce}")
        max_inflight = int(os.environ.get("AUTOTUBE_DIST_MAX_INFLIGHT", "0") or 0)
        if max_inflight <= 0:
            max_inflight = max(1, min(8, len(scene_refs)))
        threads = int(os.environ.get("AUTOTUBE_DIST_THREADS", "2") or 2)
        mem_mb = int(os.environ.get("AUTOTUBE_DIST_MEM_MB", "2048") or 2048)
        timeout_s = int(os.environ.get("AUTOTUBE_DIST_TIMEOUT_SEC", "600") or 600)

        params = {
            "stagingDir": str(staging),
            "scenes": scene_refs,
            "image": os.environ.get("AUTOTUBE_RENDER_IMAGE", "autotube-render:1"),
            "worker": os.environ.get(
                "AUTOTUBE_RENDER_WORKER", "pipeline_dist/scene_worker.py"
            ),
            "threads": threads,
            "memMb": mem_mb,
            "requiresTags": [
                t.strip() for t in os.environ.get("AUTOTUBE_RENDER_TAGS", "render").split(",")
                if t.strip()
            ],
        }

        self._emit_progress(62, "video", f"Render distribuido: {len(scene_refs)} escenas")
        logger.info("[%s] Dist render: %d escenas, inflight=%d, %dx%d@%gfps",
                    self.canal, len(scene_refs), max_inflight, width, height, fps)

        exec_id = dsl_client.submit(
            "autotube-render-scene", params,
            exec_id=run_id,
            label=f"autotube render {self.canal}",
            max_inflight=max_inflight,
            max_attempts=int(os.environ.get("AUTOTUBE_DIST_MAX_ATTEMPTS", "3") or 3),
            deadline_sec=int(os.environ.get("AUTOTUBE_DIST_DEADLINE_SEC", "0") or 0) or None,
            max_units=len(scene_refs) + 16,
            req={"cores": threads, "memMb": mem_mb},
        )
        logger.info("[%s] Dist render exec=%s (spool)", self.canal, exec_id)

        def _progress(summary: dict) -> None:
            p = summary.get("progress") or {}
            acc = int(p.get("accepted") or 0)
            tot = len(scene_refs)
            if tot:
                self._emit_progress(62 + int(18 * acc / tot), "video",
                                    f"Escenas remotas {acc}/{tot}")

        summary = dsl_client.wait(
            exec_id,
            timeout=float(timeout_s) + 120.0,
            poll=2.0,
            progress_cb=_progress,
        )
        if summary.get("status") != "done":
            raise ScenePlanError(
                f"ejecución distribuida {summary.get('status')}: {summary.get('error')}"
            )

        scenes = dsl_client.accepted_scenes(summary)
        if len(scenes) != len(manifests):
            raise ScenePlanError(
                f"el motor devolvió {len(scenes)} escenas, esperaba {len(manifests)}"
            )

        by_key = {str(m["scene_key"]): m for m in manifests}
        placed: list[Path] = []
        try:
            for entry in scenes:
                key = str(entry.get("key"))
                manifest = by_key.get(key)
                if manifest is None:
                    raise ScenePlanError(f"escena inesperada en el resultado: {key}")
                out_dir = entry.get("outDir")
                filename = entry.get("filename") or "scene.mp4"
                if not out_dir:
                    raise ScenePlanError(f"escena {key} sin outDir")
                artifact = os.path.join(str(out_dir), str(filename))
                # El resumen del motor no trae el result.json completo; reconstruimos
                # los campos verificables que sí publicó `combine`.
                result = {
                    "ok": True,
                    "sha256": entry.get("sha256"),
                    "frames": entry.get("frames"),
                    "duration": entry.get("duration"),
                    "fps": entry.get("fps"),
                }
                ok, problems = validate_scene_result(manifest, artifact, result)
                if not ok:
                    raise ScenePlanError(f"escena {key} inválida: {'; '.join(problems)}")

                dest = seg_dir / SEGMENT_NAME.format(int(manifest["index"]))
                tmp = dest.with_suffix(".mp4.part")
                shutil.copy2(artifact, tmp)
                os.replace(tmp, dest)
                placed.append(dest)

            logger.info("[%s] Dist render OK: %d segmentos en %s",
                        self.canal, len(placed), seg_dir)
            return True
        except Exception:
            # Todo-o-nada: si algo falla, no dejamos segmentos parciales que
            # romperían el dedup del render local.
            for p in placed:
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
            raise
        finally:
            if not _env_flag("AUTOTUBE_DIST_KEEP_STAGING", False):
                shutil.rmtree(staging, ignore_errors=True)
