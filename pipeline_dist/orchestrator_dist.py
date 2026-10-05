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
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
import zlib
from pathlib import Path
from typing import Optional

from config import settings as settings_mod
from orchestrator import PipelineOrchestrator
from . import dsl_client
from .ai_image_cache import CachedLocalSDProvider
from .model import atomic_write_json, sanitize_id
from .render_plan import build_scene_plans
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


AI_IMAGES_CACHE = "_dist_cache"


class DistributedOrchestrator(PipelineOrchestrator):
    """Pipeline clásico + render de escenas repartido en la flota."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Parches de estabilidad del LLM: opt-in (por defecto OFF en producción
        # para no alterar la generación de guion; el piloto los activa).
        if _env_flag("AUTOTUBE_DIST_LLM_PATCH", False):
            try:
                from .llm_patch import apply_llm_stability_patches
                apply_llm_stability_patches()
            except Exception as exc:  # nunca impedir la construcción
                logger.warning("[%s] No se pudieron aplicar parches LLM: %s", self.canal, exc)

    # ── Fase 4 sustituida: render distribuido con fallback local ────────────
    def phase_video(self, script: dict, audio_data: dict,
                    media_assets: list, job_id: int = None) -> Optional[dict]:
        if _env_flag("AUTOTUBE_DIST_RENDER_V2", False):
            try:
                if self._pre_render_scenes_v2(script, audio_data, media_assets, job_id):
                    logger.info("[%s] Render v2 distribuido completo; ensamblado local.",
                                self.canal)
            except Exception as exc:  # noqa: BLE001 — all-or-nothing => local
                logger.warning(
                    "[%s] Render v2 distribuido no aplicable (%s: %s). "
                    "Render local completo (identidad garantizada).",
                    self.canal, type(exc).__name__, exc,
                )
        elif _env_flag("AUTOTUBE_DIST_RENDER", False):
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
        if _env_flag("AUTOTUBE_DIST_CONCAT", False):
            self._install_dist_concat()
        return super().phase_video(script, audio_data, media_assets, job_id=job_id)

    # ── Imágenes IA: pre-generación distribuida (opt-in, all-or-nothing) ─────
    def _prefetch_ai_images(self, bloques, scene_ranges, script=None):
        """Pre-genera las imágenes IA en la flota y sirve a `local_sd` de caché.

        Sin `AUTOTUBE_DIST_IMAGES=1` es no-op (ruta local intacta). Cualquier
        fallo se registra y deja el pipeline local sin cambios.
        """
        if not _env_flag("AUTOTUBE_DIST_IMAGES", False):
            return None
        if not scene_ranges:
            return None
        if getattr(self.media_fetcher, "_local_sd", None) is None:
            logger.info("[%s] ai-image: sin proveedor local_sd; ruta local.", self.canal)
            return None
        # Acotar la pre-generación a las escenas cuyo PRIMER tier es IA. Las
        # `video_priority`/`stock_image_priority` intentan stock primero y solo
        # llegan a local_sd como fallback tardío: pre-generarlas es coste puro
        # (antes se generaba 1 imagen por CADA escena → ~2 h de flota por vídeo).
        # Reutilizamos la clasificación real del media_fetcher para no divergir.
        only_indices = None
        cap = int(os.environ.get("AUTOTUBE_DIST_SD_MAX_SCENES", "0") or 0)
        try:
            _, _, scene_types = self.media_fetcher._classify_scenes(scene_ranges)
            only_indices = {i for i, t in scene_types.items() if t == "ai_image"}
        except Exception as exc:  # noqa: BLE001 — si no se puede clasificar, no acotamos
            logger.warning(
                "[%s] ai-image: clasificación de tiers no disponible (%s); sin acotar.",
                self.canal, exc,
            )
        if only_indices is not None:
            if cap > 0 and len(only_indices) > cap:
                # Cap de seguridad (AUTOTUBE_DIST_SD_MAX_SCENES): prioriza las
                # primeras escenas AI-tier por orden de narración.
                only_indices = set(sorted(only_indices)[:cap])
            if not only_indices:
                logger.info(
                    "[%s] ai-image: ninguna escena AI-tier; la flota no pre-genera.",
                    self.canal,
                )
                return None
        try:
            return self._prefetch_ai_images_dist(
                scene_ranges, script, only_indices=only_indices,
            )
        except Exception as exc:  # noqa: BLE001 — cualquier fallo => local
            logger.warning(
                "[%s] Pre-generación IA distribuida no aplicable (%s: %s); local.",
                self.canal, type(exc).__name__, exc,
            )
            return None

    def _prefetch_ai_images_dist(self, scene_ranges: list, script: Optional[dict],
                                 only_indices: Optional[set[int]] = None):
        mf = self.media_fetcher
        provider = mf._local_sd
        n_scenes = len(scene_ranges)  # total real (para prompts/semillas 1:1)
        # Escenas a pre-generar: por defecto todas; con `only_indices`, solo las
        # AI-tier. Se preserva el índice GLOBAL para que `_build_ai_request` y el
        # hash de prompt coincidan exactamente con `_try_ai_image_chain`.
        selected = [(i, s) for i, s in enumerate(scene_ranges)
                    if only_indices is None or i in only_indices]
        n_expected = len(selected)
        if n_expected == 0:
            return None
        video_id = int(self.db_video_id or 0)
        script_id = str((script or {}).get("id") or "")
        seed_salt = f"{self.canal}:{video_id}:{script_id}"

        # Parámetros congelados del proveedor local (mismos que usaría la casa).
        steps = int(getattr(provider, "num_inference_steps", 8) or 8)
        width = int(getattr(provider, "width", 768) or 768)
        height = int(getattr(provider, "height", 432) or 432)
        model_id = str(getattr(provider, "model_id", "") or "")
        upscale_min = getattr(provider, "upscale_min", None)
        upscale_model = getattr(provider, "upscale_model", None)
        upscale_sharpen = bool(getattr(provider, "upscale_sharpen", True))
        upscale_amount = float(getattr(provider, "upscale_sharpen_amount", 0.4))
        upscale_sigma = float(getattr(provider, "upscale_sharpen_sigma", 2.0))

        # Staging ABSOLUTO: el engine hace `existsSync` desde su cwd.
        cache_root = (Path(settings_mod.OUTPUT_DIR) / "ai_images" /
                      AI_IMAGES_CACHE / str(video_id or "adhoc")).resolve()
        cache_root.mkdir(parents=True, exist_ok=True)
        staging = (cache_root / "_staging").resolve()
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)

        images_params = []
        prompt_by_key: dict[str, str] = {}
        for i, scene in selected:
            prompt, negative, _seed = mf._build_ai_request(scene, i, n_scenes)
            digest8 = hashlib.sha1(
                f"{seed_salt}:{i}:{prompt}".encode("utf-8")
            ).hexdigest()[:8]
            seed = int(digest8, 16) % (2 ** 31)
            key = f"{i:04d}"
            d = staging / f"scene_{i:04d}"
            d.mkdir(parents=True, exist_ok=True)
            request = {
                "key": key,
                "index": i,
                "prompt": prompt,
                "negative_prompt": negative,
                "seed": seed,
                "width": width,
                "height": height,
                "steps": steps,
                "output": f"scene_{i:04d}.jpg",
                "model_id": model_id or None,
                "upscale_min": list(upscale_min) if upscale_min else None,
                "upscale_model": upscale_model,
                "upscale_sharpen": upscale_sharpen,
                "upscale_sharpen_amount": upscale_amount,
                "upscale_sharpen_sigma": upscale_sigma,
            }
            atomic_write_json(str(d / "request.json"), request)
            images_params.append({"key": key, "dir": d.name, "request": "request.json"})
            prompt_by_key[key] = prompt

        nonce = os.environ.get("AUTOTUBE_DIST_EXEC_NONCE", "").strip()
        base = f"atube-img-{self.canal}-{video_id or 'x'}"
        exec_id = sanitize_id(
            f"{base}-{nonce}" if nonce else f"{base}-{uuid.uuid4().hex[:8]}"
        )
        tags = [t.strip() for t in
                os.environ.get("AUTOTUBE_SD_TAGS", "local-sd").split(",") if t.strip()]
        params = {
            "stagingDir": str(staging),
            "images": images_params,
            "image": os.environ.get("AUTOTUBE_SD_IMAGE", "autotube-sd:1"),
            "worker": "pipeline_dist/image_worker.py",
            "codeDir": os.environ.get("AUTOTUBE_RUNTIME_DIR", "/opt/taildeck/autotube"),
            "modelDir": os.environ.get(
                "AUTOTUBE_SD_MODELS", "/opt/taildeck/autotube/.hfcache"
            ),
            "threads": int(os.environ.get("AUTOTUBE_DIST_SD_THREADS", "3") or 3),
            "memMb": int(os.environ.get("AUTOTUBE_DIST_SD_MEM_MB", "4600") or 4600),
            "requiresTags": tags,
        }
        self._emit_progress(43, "images", f"Imágenes IA en la flota: 0/{n_expected}")
        eid = dsl_client.submit(
            "autotube-ai-image", params, exec_id=exec_id,
            label=f"autotube ai-image {self.canal}",
            max_inflight=int(os.environ.get("AUTOTUBE_DIST_SD_MAX_INFLIGHT", "6") or 6),
            max_attempts=3, max_units=len(images_params) + 16,
            req={"cores": params["threads"], "memMb": params["memMb"]},
        )
        logger.info("[%s] ai-image exec=%s (%d escenas AI-tier de %d)",
                    self.canal, eid, n_expected, n_scenes)
        # La espera debe cubrir TODAS las imágenes (se reparten de forma
        # concurrente). Ceiling generoso; `wait` retorna al acabar. Se puede
        # fijar explícitamente con AUTOTUBE_DIST_SD_TIMEOUT_SEC.
        _ceiling = float(os.environ.get("AUTOTUBE_DIST_SD_TIMEOUT_SEC", "0") or 0)
        if _ceiling <= 0:
            _per_img = float(os.environ.get("AUTOTUBE_DIST_SD_PER_IMAGE_SEC", "600") or 600)
            _ceiling = max(1800.0, n_expected * _per_img)
        timeout = _ceiling

        def _progress(summary: dict) -> None:
            p = (summary or {}).get("progress") or {}
            acc = int(p.get("accepted") or 0)
            self._emit_progress(
                43 + int(8 * acc / max(1, n_expected)), "images",
                f"Imágenes IA en la flota: {acc}/{n_expected}",
            )

        summary = self._wait_dist_stall_aware(
            eid, timeout, _progress,
            stall_sec=float(os.environ.get("AUTOTUBE_DIST_SD_STALL_SEC", "900") or 900),
        )
        if summary.get("status") != "done":
            raise ScenePlanError(
                f"ai-image {summary.get('status')}: {summary.get('error')}"
            )
        entries = (summary.get("result") or {}).get("images") or []
        if len(entries) != n_expected:
            raise ScenePlanError(
                f"ai-image devolvió {len(entries)} imágenes, esperaba {n_expected}"
            )

        cache_by_hash: dict[str, str] = {}
        for entry in entries:
            key = str(entry.get("key"))
            out_dir, fn = entry.get("outDir"), entry.get("filename")
            if not out_dir or not fn:
                raise ScenePlanError(f"imagen {key} sin outDir/filename")
            src = os.path.join(str(out_dir), str(fn))
            if not os.path.exists(src) or os.path.getsize(src) < 1024:
                raise ScenePlanError(f"imagen {key} inválida o vacía")
            digest = hashlib.md5(
                (prompt_by_key.get(key) or "").encode("utf-8")
            ).hexdigest()[:10]
            dst = cache_root / f"dist_{video_id or 'x'}_{key}.jpg"
            shutil.copy2(src, dst)
            cache_by_hash[digest] = str(dst)

        # Instala la caché: `_try_ai_image_chain` la consulta por hash de prompt.
        mf._local_sd = CachedLocalSDProvider(mf._local_sd, cache_by_hash)
        logger.info("[%s] ai-image OK: %d imágenes en caché (%s)",
                    self.canal, len(cache_by_hash), cache_root)
        return cache_by_hash

    def _wait_dist_stall_aware(self, eid: str, timeout: float,
                               progress_cb=None, stall_sec: float = 900.0) -> dict:
        """Espera una ejecución distribuida con guardia de estancamiento.

        El latido de progreso es la **salida real** (``accepted`` o ``generated``
        creciendo), no el mero hecho de que haya unidades ``inflight``: un worker
        colgado deja ``inflight>0`` indefinidamente y antes se interpretaba como
        "vivo", lo que podía colgar el vídeo hasta el timeout completo. Si no se
        acepta ninguna imagen nueva durante ``stall_sec``, se cancela y se lanza
        para caer al render local.
        """
        deadline = time.time() + float(timeout)
        last_progress = time.time()
        last_accepted = -1
        last_generated = -1
        last = None
        while time.time() < deadline:
            st = dsl_client.status(eid)
            accepted = last_accepted
            generated = last_generated
            inflight = 0
            pending = 0
            if st is not None:
                if progress_cb is not None:
                    try:
                        progress_cb(st)
                    except Exception:  # noqa: BLE001 — el progreso nunca rompe
                        pass
                last = st
                if st.get("status") in dsl_client.TERMINAL:
                    return st
                p = st.get("progress") or {}
                inflight = int(p.get("inflight") or 0)
                accepted = int(p.get("accepted") or 0)
                generated = int(p.get("generated") or 0)
                pending = int(p.get("pending") or 0)
                if accepted > last_accepted or generated > last_generated:
                    last_progress = time.time()
                    last_accepted = accepted
                    last_generated = generated
            # Guardia de estancamiento FUERA de `if st is not None`: si el motor
            # nunca publica `results/<eid>.json` (nodo no elegible, engine caído),
            # `status()` devuelve None y antes solo aplicaba el ceiling completo
            # (hasta 25 h con 150 escenas). Ahora se cancela y cae a local.
            if (time.time() - last_progress) > stall_sec:
                dsl_client.cancel(eid)
                raise ScenePlanError(
                    f"sin progreso {stall_sec:.0f}s "
                    f"(accepted={accepted}, generated={generated}, "
                    f"inflight={inflight}, pending={pending}); cancelado"
                )
            time.sleep(2.0)
        dsl_client.cancel(eid)
        raise ScenePlanError(f"ai-image timeout {timeout:.0f}s; cancelado")

    # ── Render v2: mismo código, estado congelado, todo-o-nada ──────────────
    @staticmethod
    def _json_safe_config(config) -> Optional[dict]:
        try:
            raw = dict(vars(config)) if hasattr(config, "__dict__") else dict(config)
        except Exception:  # noqa: BLE001
            return None
        out = {}
        for k, v in raw.items():
            if k.startswith("_") or callable(v):
                continue
            try:
                json.dumps(v)
                out[k] = v
            except TypeError:
                if isinstance(v, (tuple, set)):
                    out[k] = list(v)
        return out

    @staticmethod
    def _json_safe(obj):
        """Convierte recursivamente a tipos JSON-serializables (Path→str, etc.)."""
        if isinstance(obj, dict):
            return {str(k): DistributedOrchestrator._json_safe(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple, set)):
            return [DistributedOrchestrator._json_safe(v) for v in obj]
        if isinstance(obj, Path):
            return str(obj)
        if isinstance(obj, (str, int, float, bool)) or obj is None:
            return obj
        return str(obj)

    def _pre_render_scenes_v2(self, script, audio_data, media_assets, job_id) -> bool:
        scene_ranges = getattr(self, "_last_scene_ranges", None)
        if not scene_ranges or not media_assets or len(scene_ranges) != len(media_assets):
            raise ScenePlanError("scene_ranges/media_assets no alineados")
        ve = self.video_editor
        seed_base = int(self.db_video_id or job_id or 0)
        ve._video_seed = seed_base
        plans = build_scene_plans(ve, scene_ranges, media_assets)
        if not all(p.get("reproducible") for p in plans):
            raise ScenePlanError("alguna escena no reproducible → render local completo")

        seg_dir = (Path(settings_mod.VIDEOS_DIR) / "segments" /
                   str(self.db_video_id or job_id or "adhoc")).resolve()
        seg_dir.mkdir(parents=True, exist_ok=True)
        staging = seg_dir / "_dist_staging_v2"
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)

        cfg_json = self._json_safe_config(self.config)
        scenes_params = []
        for i, (br, asset, plan) in enumerate(zip(scene_ranges, media_assets, plans)):
            ap = str(asset.get("path") or "")
            if not ap or not Path(ap).exists():
                raise ScenePlanError(f"asset inexistente en escena {i}")
            d = staging / f"scene_{i:04d}"
            d.mkdir(parents=True, exist_ok=True)
            base = Path(ap).name
            try:
                os.link(ap, d / base)
            except OSError:
                shutil.copy2(ap, d / base)
            manifest = {
                "config": cfg_json, "seed_base": seed_base, "index": i,
                "block_range": self._json_safe(br),
                "asset": self._json_safe(asset),
                "plan": self._json_safe(plan),
            }
            atomic_write_json(str(d / "manifest.json"), manifest)
            scenes_params.append({"key": f"{i:04d}", "dir": d.name,
                                  "manifest": "manifest.json", "index": i})

        nonce = os.environ.get("AUTOTUBE_DIST_EXEC_NONCE", "").strip()
        base_id = f"atube-render2-{self.canal}-{self.db_video_id or 'x'}"
        exec_id = sanitize_id(f"{base_id}-{nonce}" if nonce else f"{base_id}-{uuid.uuid4().hex[:8]}")
        tags = [t.strip() for t in
                os.environ.get("AUTOTUBE_RENDER_TAGS", "render").split(",") if t.strip()]
        params = {
            "stagingDir": str(staging),
            "scenes": scenes_params,
            "image": os.environ.get("AUTOTUBE_WORKER_IMAGE", "autotube-worker:1"),
            "worker": "pipeline_dist/render_worker.py",
            "threads": int(os.environ.get("AUTOTUBE_DIST_THREADS", "2") or 2),
            "memMb": int(os.environ.get("AUTOTUBE_DIST_MEM_MB", "2048") or 2048),
            "requiresTags": tags,
        }
        self._emit_progress(38, "video", f"Render v2 distribuido: {len(scenes_params)} escenas")
        eid = dsl_client.submit(
            "autotube-render-scene", params, exec_id=exec_id,
            label=f"autotube render v2 {self.canal}",
            max_inflight=int(os.environ.get("AUTOTUBE_DIST_MAX_INFLIGHT", "0") or 0) or None,
            max_attempts=3, max_units=len(scenes_params) + 16,
            req={"cores": 2, "memMb": 2048},
        )
        logger.info("[%s] Render v2 exec=%s (%d escenas)",
                    self.canal, eid, len(scenes_params))
        summary = dsl_client.wait(
            eid,
            timeout=float(os.environ.get("AUTOTUBE_DIST_TIMEOUT_SEC", "1800")) + 120,
            poll=2.0,
        )
        if summary.get("status") != "done":
            raise ScenePlanError(
                f"render v2 {summary.get('status')}: {summary.get('error')}"
            )
        scenes = dsl_client.accepted_scenes(summary)
        if len(scenes) != len(scenes_params):
            raise ScenePlanError(
                f"render v2 devolvió {len(scenes)} escenas, esperaba {len(scenes_params)}"
            )
        placed = []
        try:
            for entry in scenes:
                key = str(entry.get("key"))
                out_dir, fn = entry.get("outDir"), entry.get("filename") or "scene.mp4"
                if not out_dir:
                    raise ScenePlanError(f"escena {key} sin outDir")
                src = os.path.join(str(out_dir), str(fn))
                if not os.path.exists(src) or os.path.getsize(src) < 1024:
                    raise ScenePlanError(f"escena {key} inválida o vacía")
                dest = seg_dir / f"scene_{int(key):04d}.mp4"
                tmp = dest.with_suffix(".mp4.part")
                shutil.copy2(src, tmp)
                os.replace(tmp, dest)
                placed.append(dest)
        except Exception:
            for p in placed:
                p.unlink(missing_ok=True)
            raise
        logger.info("[%s] Render v2 OK: %d segmentos en %s",
                    self.canal, len(placed), seg_dir)
        return True

    # ── Concat de batches distribuido con fallback local ────────────────────
    def _install_dist_concat(self) -> None:
        """Envuelve ``video_editor._concat_body_batched`` para repartir batches.

        La salida local se mantiene idéntica: el nodo ejecuta EXACTAMENTE el
        mismo comando ffmpeg (mismo filter_complex/encoding) en el contenedor
        hermético; ante cualquier fallo, cae al método local original.
        """
        ve = self.video_editor
        if getattr(ve, "_dist_concat_installed", False):
            return
        orig = ve._concat_body_batched

        def _wrapped(segment_paths, block_ranges, output_path, batch_size=25):
            try:
                if self._dist_concat_body_batched(
                    segment_paths, block_ranges, output_path, batch_size,
                ):
                    return output_path
            except Exception as exc:  # noqa: BLE001 — fallback local
                logger.warning(
                    "[%s] Concat distribuido no aplicable (%s: %s); local.",
                    self.canal, type(exc).__name__, exc,
                )
            return orig(segment_paths, block_ranges, output_path, batch_size)

        ve._concat_body_batched = _wrapped
        ve._dist_concat_installed = True

    # ── Implementación ──────────────────────────────────────────────────────
    def _dist_concat_body_batched(self, segment_paths, block_ranges,
                                  output_path, batch_size=25) -> bool:
        """Reparte el concat por batches a la flota. True si lo hizo todo."""
        n = len(segment_paths)
        if n <= batch_size:
            return False  # listas pequeñas: local (no merece repartir)
        ve = self.video_editor
        canal = ve.canal
        film = float(canal.get("FILM_GRAIN_OPACITY", 0) or 0)
        vig = float(canal.get("VIGNETTE_INTENSITY", 0) or 0)
        preset = str(canal.get("FFMPEG_PRESET", settings_mod.FFMPEG_PRESET_DEFAULT))

        staging = Path(tempfile.mkdtemp(prefix="autotube_concat_dist_"))
        batches = []
        for bi, start in enumerate(range(0, n, batch_size)):
            segs = segment_paths[start:start + batch_size]
            bdir = staging / f"batch_{start:04d}"
            bdir.mkdir(parents=True, exist_ok=True)
            names = []
            for j, sp in enumerate(segs):
                name = f"seg_{j:04d}{Path(sp).suffix or '.mp4'}"
                dst = bdir / name
                try:
                    os.link(sp, dst)
                except OSError:
                    shutil.copy2(sp, dst)
                names.append(name)
            filt, label = ve._build_duration_preserving_concat_filter(
                len(names), film_grain_opacity=film, vignette_intensity=vig,
            )
            spec = {
                "inputs": names, "filter_complex": filt, "map_label": label,
                "codec": settings_mod.VIDEO_CODEC, "preset": preset,
                "bitrate": settings_mod.VIDEO_BITRATE, "pix_fmt": "yuv420p",
                "threads": int(getattr(settings_mod, "FFMPEG_THREADS", 4) or 4),
                "output": f"batch_{start:04d}.mp4",
                "timeout": max(900, len(names) * 15),
            }
            atomic_write_json(str(bdir / "spec.json"), spec)
            batches.append({"key": f"b{bi:04d}", "dir": bdir.name,
                            "output": spec["output"]})

        nonce = os.environ.get("AUTOTUBE_DIST_EXEC_NONCE", "").strip()
        base = f"atube-concat-{self.canal}-{self.db_video_id or 'x'}"
        exec_id = sanitize_id(
            f"{base}-{nonce}" if nonce else f"{base}-{uuid.uuid4().hex[:8]}"
        )
        tags = [t.strip() for t in
                os.environ.get("AUTOTUBE_ASSEMBLE_TAGS", "render").split(",")
                if t.strip()]
        params = {
            "stagingDir": str(staging),
            "batches": batches,
            "image": os.environ.get("AUTOTUBE_WORKER_IMAGE", "autotube-worker:1"),
            "worker": "pipeline_dist/concat_worker.py",
            "requiresTags": tags,
            "memMb": int(os.environ.get("AUTOTUBE_DIST_MEM_MB", "2048") or 2048),
        }
        self._emit_progress(70, "video", f"Concat distribuido: {len(batches)} batches")
        eid = dsl_client.submit(
            "autotube-concat-batch", params, exec_id=exec_id,
            label=f"autotube concat {self.canal}",
            max_inflight=min(4, len(batches)), max_attempts=2,
            max_units=len(batches) + 8, req={"cores": 4, "memMb": 2048},
        )
        logger.info("[%s] Concat distribuido exec=%s (%d batches)",
                    self.canal, eid, len(batches))
        summary = dsl_client.wait(
            eid,
            timeout=float(os.environ.get("AUTOTUBE_DIST_TIMEOUT_SEC", "1800")) + 120,
            poll=2.0,
        )
        if summary.get("status") != "done":
            raise ScenePlanError(
                f"concat distribuido {summary.get('status')}: {summary.get('error')}"
            )
        items = (summary.get("result") or {}).get("batches") or []
        if len(items) != len(batches):
            raise ScenePlanError(
                f"concat devolvió {len(items)} batches, esperaba {len(batches)}"
            )
        merged = staging / "merged"
        merged.mkdir(parents=True, exist_ok=True)
        order = []
        for it in items:
            out_dir, fn = it.get("outDir"), it.get("filename")
            if not out_dir or not fn:
                raise ScenePlanError("batch sin outDir/filename")
            dst = merged / str(fn)
            shutil.copy2(os.path.join(str(out_dir), str(fn)), dst)
            order.append(dst)

        # Unión final LOCAL con demuxer (-c copy): idéntica a la local.
        concat_list = staging / "concat_list.txt"
        with open(concat_list, "w", encoding="utf-8") as fh:
            for p in order:
                fh.write(f"file '{os.path.abspath(p)}'\n")
        r = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
             "-i", str(concat_list), "-c", "copy", "-movflags", "+faststart",
             str(output_path)],
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode != 0:
            raise ScenePlanError(
                f"unión de batches falló (rc={r.returncode}): {(r.stderr or '')[-300:]}"
            )
        logger.info("[%s] Concat distribuido OK: %d batches → %s",
                    self.canal, len(batches), output_path)
        return True

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
