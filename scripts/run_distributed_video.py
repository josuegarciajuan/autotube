#!/usr/bin/env python3
"""Piloto del pipeline de vídeo distribuido (paralelo al pipeline clásico).

No sube nada a YouTube y no toca la DB de producción: trabaja sobre una COPIA
de `autotube.db` y deposita el vídeo final en `output/videos/`.

Uso:
    python3 scripts/run_distributed_video.py --canal canal2 --mode quarter
    python3 scripts/run_distributed_video.py --canal canal2 --skip-scrape --no-dist

El render de escenas se reparte en los nodos de SuperServer; el ensamblado lo
hace el editor legacy sin cambios (reutiliza los segmentos pre-renderizados).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _resolve_production_db() -> Path:
    """Localiza la DB de producción (los datos viven solo en el árbol principal).

    Cuando el piloto corre desde un worktree, `PROJECT_ROOT/autotube.db` no
    existe; se resuelve el árbol principal vía `git worktree list`. Se puede
    forzar con `AUTOTUBE_PRODUCTION_DB`.
    """
    env = os.environ.get("AUTOTUBE_PRODUCTION_DB")
    if env:
        return Path(env)
    try:
        out = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        for line in out.splitlines():
            if line.startswith("worktree "):
                cand = Path(line.split(" ", 1)[1]) / "autotube.db"
                if cand.exists():
                    return cand
    except Exception:
        pass
    return PROJECT_ROOT / "autotube.db"


PRODUCTION_DB = _resolve_production_db()


def _log(msg: str) -> None:
    print(f"[dist-run] {msg}", flush=True)


def _clone_db(src: Path, dst: Path) -> None:
    """Snapshot consistente (WAL-safe) de la DB de producción a la de trabajo."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=10)
    try:
        target = sqlite3.connect(str(dst))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def _running_longform(db_path: Path) -> int:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            # Los centinelas de "pausa de generación" (phase='hold') NO son
            # trabajo real: no deben impedir el arranque del piloto.
            row = conn.execute(
                "SELECT COUNT(*) FROM generation_jobs "
                "WHERE status='running' AND COALESCE(phase,'') <> 'hold'"
            ).fetchone()
            return int(row[0] if row else 0)
        finally:
            conn.close()
    except Exception:
        return -1  # desconocido: no bloquear, solo avisar


def _apply_media_fast(cfg) -> list[str]:
    """Endurece la media para el piloto: sin IA lenta ni dependencias caídas.

    Motivo: Pollinations devuelve 402 (Payment Required) desde oct 2026 y el
    proveedor de respaldo (Stable Diffusion local en CPU) tarda ~13 min/imagen,
    dejando el piloto atascado horas en la fase de media. En modo *fast*:
      - ``ai_image_primary = False``  → no se intenta IA en la cadena de tiers
        (las escenas 'ai_image' caen directas a stock image/video).
      - ``ai_image_fallback = False`` → se desactiva el rescate Pollo AI (créditos,
        también lento) para que no bloquee.
    Devuelve la lista de cambios aplicados (para el log/reporte).
    """
    ms = getattr(cfg, "MEDIA_STRATEGY", None)
    if not isinstance(ms, dict):
        return []
    changes = []
    for key, val in (
        ("ai_image_primary", False),
        ("ai_image_fallback", False),
        ("ai_image_providers", ["pollinations"]),
    ):
        if ms.get(key) != val:
            ms[key] = val
            changes.append(f"{key}={val}")
    return changes


def main() -> int:
    ap = argparse.ArgumentParser(description="Piloto de vídeo distribuido")
    ap.add_argument("--canal", default="canal2")
    ap.add_argument("--db", default=None, help="DB de trabajo (por defecto, clon temporal)")
    ap.add_argument("--mode", choices=["quick", "quarter", "default", "prod"], default="quarter")
    ap.add_argument("--skip-scrape", action="store_true")
    ap.add_argument("--skip-metadata", action="store_true")
    ap.add_argument("--no-dist", action="store_true", help="Render 100%% local (control A/B)")
    ap.add_argument("--force", action="store_true", help="Ignorar generación activa en producción")
    ap.add_argument("--wait-for-idle", action="store_true",
                    help="Esperar a que no haya jobs activos en producción antes de arrancar")
    ap.add_argument("--max-wait-min", type=int, default=720,
                    help="Tiempo máximo de espera con --wait-for-idle (min)")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--freeze", default=None,
                    help="Guardar bundle (script/audio/media/scene_ranges) en DIR para A/B")
    ap.add_argument("--from-freeze", dest="from_freeze", default=None,
                    help="Cargar bundle de DIR (omite scrape/script/tts/media) → inputs idénticos")
    ap.add_argument("--freeze-only", action="store_true",
                    help="Con --freeze: guarda el bundle y termina (no renderiza)")
    ap.add_argument("--media-fast", action="store_true",
                    help="Media rápida para el piloto: sin IA lenta (local SD ~13 min/img) "
                         "ni Pollo AI; si la IA no está disponible, usa stock images/video.")
    ap.add_argument("--images-only", action="store_true",
                    help="Fuerza media solo-imágenes (sin vídeos de stock). Evita el "
                         "pre-transcode 4K→1080p local (~2 min/vídeo) para validar el "
                         "render distribuido end-to-end rápido.")
    args = ap.parse_args()

    if args.wait_for_idle and not args.force:
        waited = 0
        while True:
            n = _running_longform(PRODUCTION_DB)
            if n == 0:
                _log("Producción idle: arrancando piloto.")
                break
            if n < 0:
                _log("No pude leer producción; continúo (modo best-effort).")
                break
            if waited >= args.max_wait_min * 60:
                _log(f"Espera máxima alcanzada ({args.max_wait_min} min); abandono.")
                return 3
            _log(f"Producción con {n} job(s) activo(s); reintento en 2 min "
                 f"(esperado {waited//60} min).")
            time.sleep(120)
            waited += 120

    run_id = args.run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
    if args.db:
        db_path = Path(args.db).resolve()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        if not db_path.exists():
            _log(f"DB indicada no existe; clonando desde producción: {db_path}")
            _clone_db(PRODUCTION_DB, db_path)
    else:
        db_path = PROJECT_ROOT / "output" / "dist_test" / run_id / "autotube_test.db"
        _log(f"Clonando DB de producción (WAL-safe) → {db_path}")
        _clone_db(PRODUCTION_DB, db_path)

    # La DB de trabajo debe quedar fijada ANTES de importar config/orchestrator.
    os.environ["DATABASE_PATH"] = str(db_path)
    os.environ.setdefault(
        "AUTOTUBE_DIST_KEEP_STAGING", "1" if os.environ.get("KEEP_STAGING") else "0"
    )

    running = _running_longform(PRODUCTION_DB)
    if running > 0 and not args.force:
        _log(f"ABORTADO: hay {running} generación(es) long-form activas en producción. "
             f"Usa --force solo si sabes lo que haces.")
        return 2
    if running < 0:
        _log("Aviso: no pude comprobar generaciones activas en producción (DB ocupada).")

    if args.no_dist:
        os.environ["AUTOTUBE_DIST_RENDER"] = "0"
        os.environ["AUTOTUBE_DIST_RENDER_V2"] = "0"
        os.environ["AUTOTUBE_DIST_CONCAT"] = "0"
        _log("Distribución DESACTIVADA (--no-dist): control A/B local.")
    else:
        os.environ["AUTOTUBE_DIST_RENDER"] = "0"      # v1 (reimplementado) OFF
        os.environ["AUTOTUBE_DIST_RENDER_V2"] = "1"   # render identity-preserving
        os.environ["AUTOTUBE_DIST_CONCAT"] = "1"      # concat por batches

    # Imports tardíos (dependen de DATABASE_PATH).
    from config.settings import LOGS_DIR
    logging_info = None
    import logging
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOGS_DIR / f"dist_run_{run_id}.log", encoding="utf-8"),
        ],
    )
    logging_info = logging.getLogger("dist_run")

    # `config.settings` usa `load_dotenv(override=True)`, así que el
    # `DATABASE_PATH=autotube.db` del .env pisa la variable de entorno. Hay que
    # corregir la constante ANTES de importar el resto (que copian el valor con
    # `from config.settings import DATABASE_PATH`).
    import config.settings as _settings
    _settings.DATABASE_PATH = str(db_path)
    os.environ["DATABASE_PATH"] = str(db_path)

    # ── Aislamiento total de la salida (F7) ───────────────────────────────
    # TODA la salida del piloto va bajo output/dist_test/<run_id>/output, nunca
    # al output/ de producción, aunque se lance desde el árbol principal. Se fija
    # ANTES de importar orchestrator/VideoEditor (que copian los valores).
    _out_root = Path(db_path).parent / "output"
    for _name, _sub in (("OUTPUT_DIR", ""), ("VIDEOS_DIR", "videos"),
                        ("AUDIO_DIR", "audio"), ("IMAGES_DIR", "images"),
                        ("THUMBNAILS_DIR", "thumbnails")):
        _val = _out_root if not _sub else _out_root / _sub
        try:
            _val.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        setattr(_settings, _name, _val)
    _settings.OUTPUT_DIR = _out_root
    logging_info.info("Salida aislada del piloto: %s", _out_root)

    # Guard anti-silencio: sin claves LLM el generador cae a un fallback en
    # inglés y aborta más tarde con un error confuso. Mejor fallar claro aquí.
    if not (os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_API_KEY")):
        _log("ABORTADO: no hay claves LLM (OPENAI_API_KEY/LLM_API_KEY). "
             "¿Falta el .env en el worktree? (ln -s /root/autotube/.env .env)")
        return 2

    from config.test_profile import apply_test_profile
    from database.db import init_db
    from database.db_extended import ExtendedDatabase, migrate_v2
    from pipeline_dist.orchestrator_dist import DistributedOrchestrator

    import importlib
    cfg = importlib.import_module(f"config.{args.canal}_config")
    apply_test_profile(cfg, mode=args.mode)
    logging_info.info("Modo de test aplicado: %s (canal=%s db=%s)",
                      args.mode, args.canal, db_path)

    init_db(str(db_path))
    migrate_v2(str(db_path))
    ext_db = ExtendedDatabase(str(db_path))

    channel_id = None
    try:
        ch = ext_db.get_channel_by_slug(args.canal)
        channel_id = ch["id"] if ch else None
    except Exception:
        pass
    if not channel_id:
        _log(f"ABORTADO: canal '{args.canal}' no existe en la DB clonada.")
        return 2

    job_id = None
    try:
        job_id = ext_db.create_job(channel_id, "generate_only", None)
    except Exception as exc:
        logging_info.warning("No pude registrar job de panel: %s", exc)

    def _update_panel(progress: int, phase: str, msg: str, **kwargs):
        if job_id:
            try:
                ext_db.update_job(job_id, progress=progress, phase=phase, status="running")
            except Exception:
                pass

    orch = DistributedOrchestrator(
        canal=args.canal, db_path=str(db_path), progress_callback=_update_panel,
    )
    # El bridge crea un objeto nuevo: replicamos el perfil de test sobre él.
    try:
        apply_test_profile(orch.config, mode=args.mode)
    except Exception:
        pass

    # ── Fix media (piloto): sin IA lenta ni proveedores caídos ──────────────
    if args.media_fast:
        try:
            _chg = _apply_media_fast(orch.config)
            if args.images_only:
                _ms = getattr(orch.config, "MEDIA_STRATEGY", None)
                if isinstance(_ms, dict):
                    _ms["prefer_video"] = False
                    _ms["video_scene_hard_cap"] = 0
                    _chg += ["prefer_video=False", "video_scene_hard_cap=0"]
            # Estrategia/fetcher cacheados: invalidar para que el fix tome efecto.
            if hasattr(orch, "_cached_media_strategy"):
                delattr(orch, "_cached_media_strategy")
            if getattr(orch, "_media_fetcher", None) is not None:
                orch._media_fetcher = None
            logging_info.info("media-fast aplicado: %s", ", ".join(_chg) or "sin cambios")
        except Exception as exc:
            logging_info.warning("media-fast no aplicable: %s", exc)
    else:
        logging_info.info("media-fast desactivado (media con IA normal)")

    # Nonce de ejecución: evita que el engine reutilice un resultado previo
    # (p. ej. un `failed` cacheado) con el exec-id determinista del render.
    os.environ["AUTOTUBE_DIST_EXEC_NONCE"] = run_id

    # Override opcional del tamaño de batch de concat (para que un mini vídeo con
    # pocas escenas ejercite el concat distribuido). Mismo valor local y nodo.
    _xb = os.environ.get("AUTOTUBE_CONCAT_BATCH_SIZE")
    if _xb:
        try:
            setattr(orch.config, "XFADE_BATCH_SIZE", int(_xb))
            logging_info.info("XFADE_BATCH_SIZE=%s (override)", _xb)
        except (TypeError, ValueError):
            logging_info.warning("AUTOTUBE_CONCAT_BATCH_SIZE inválido: %r", _xb)

    t_start = time.time()
    timings: dict[str, float] = {}

    def _timed(name: str, fn, *fa, **fk):
        t0 = time.time()
        try:
            return fn(*fa, **fk)
        finally:
            timings[name] = round(time.time() - t0, 2)

    freeze_dir = Path(args.freeze).resolve() if args.freeze else None
    load_dir = Path(args.from_freeze).resolve() if args.from_freeze else None

    if load_dir:
        # Inputs congelados → A/B justo (mismo script/audio/media en local y dist).
        logging_info.info("Bundle congelado cargado: %s (se omiten scrape/script/tts/media)", load_dir)
        script = json.loads((load_dir / "script.json").read_text(encoding="utf-8"))
        audio_data = json.loads((load_dir / "audio.json").read_text(encoding="utf-8"))
        media_assets = json.loads((load_dir / "media.json").read_text(encoding="utf-8"))
        orch._last_scene_ranges = json.loads((load_dir / "scene_ranges.json").read_text(encoding="utf-8"))
        orch._last_media_assets = media_assets
    else:
        if not args.skip_scrape:
            logging_info.info("Fase scrape...")
            _timed("scrape", orch.phase_scrape)
        else:
            logging_info.info("Fase scrape omitida (--skip-scrape)")

        items = ext_db.get_unused_content(canal=args.canal, limit=10)
        if not items:
            _log("ABORTADO: no hay contenido unused en la DB clonada. Ejecuta sin --skip-scrape.")
            return 1
        script = _timed("script", orch.script_gen.generate, items[0])
        if not script:
            _log("ABORTADO: no se generó guion.")
            return 1
        _timed("pre_validate", orch.phase_pre_validate, script)

        audio_data = _timed("tts", orch.phase_tts, script)
        if not audio_data:
            _log("ABORTADO: TTS falló.")
            return 1

        media_assets = _timed("media", orch.phase_media, script, audio_data)
        if not media_assets:
            _log("ABORTADO: media falló.")
            return 1

        if freeze_dir:
            freeze_dir.mkdir(parents=True, exist_ok=True)
            (freeze_dir / "script.json").write_text(
                json.dumps(script, ensure_ascii=False), encoding="utf-8")
            (freeze_dir / "audio.json").write_text(
                json.dumps(audio_data, ensure_ascii=False), encoding="utf-8")
            (freeze_dir / "media.json").write_text(
                json.dumps(media_assets, ensure_ascii=False), encoding="utf-8")
            (freeze_dir / "scene_ranges.json").write_text(
                json.dumps(getattr(orch, "_last_scene_ranges", []) or [], ensure_ascii=False),
                encoding="utf-8")
            logging_info.info("Bundle congelado guardado en %s", freeze_dir)
            if args.freeze_only:
                _log("Bundle congelado guardado; fin (--freeze-only).")
                return 0

    video_data = _timed("video", orch.phase_video, script, audio_data, media_assets, job_id=job_id)
    if not video_data:
        _log("ABORTADO: ensamblado de vídeo falló.")
        return 1

    metadata = None
    if not args.skip_metadata:
        try:
            metadata = _timed("metadata", orch.phase_metadata, script, video_data)
        except Exception as exc:
            logging_info.warning("Metadata falló (no fatal): %s", exc)

    try:
        _timed("post_validate", orch.phase_post_validate, video_data, metadata, script)
    except Exception as exc:
        logging_info.warning("Post-validación falló (no fatal en piloto): %s", exc)

    elapsed = time.time() - t_start
    video_path = video_data.get("video_path")
    size_mb = (Path(video_path).stat().st_size / 1024 / 1024) if video_path else 0

    # ── Reporte de tiempos por fase (comparativa A/B local vs distribuido) ──
    report = {
        "run_id": run_id,
        "canal": args.canal,
        "mode": args.mode,
        "distributed_render": not args.no_dist,
        "media_fast": bool(args.media_fast),
        "phases_sec": timings,
        "total_sec": round(elapsed, 2),
        "video": {"path": str(video_path), "size_mb": round(size_mb, 1)},
        "orchestrator_timing": getattr(orch, "_timing", {}),
    }
    report_path = None
    try:
        report_path = Path(db_path).parent / "timings.json"
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    except Exception as exc:
        logging_info.warning("No pude escribir timings.json: %s", exc)

    if job_id:
        try:
            ext_db.update_job(job_id, status="completed", progress=100, phase="done")
        except Exception:
            pass

    _log("=" * 60)
    _log(f"VÍDEO PILOTO LISTO: {video_path}")
    _log(f"Tamaño: {size_mb:.1f} MB | Tiempo total: {elapsed/60:.1f} min")
    _log("Tiempos por fase (s): " + ", ".join(f"{k}={v}" for k, v in timings.items()))
    if report_path:
        _log(f"Reporte de tiempos: {report_path}")
    _log(f"DB de trabajo: {db_path}")
    _log("Subida a YouTube: OMITIDA (revisión manual antes de publicar).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
