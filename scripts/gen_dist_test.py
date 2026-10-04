#!/usr/bin/env python3
"""Prueba de GENERACIÓN DISTRIBUIDA end-to-end (mini vídeo).

Lanza UN run distribuido (render v2 + concat en la flota) y conserva el vídeo
resultante para validación manual, con su id interno, nodos participantes y gate.
Siempre escribe el informe (incluso si algo falla).

Artefactos:
  /root/autotube_mini_test/final/<video>.mp4   (vídeo generado, SE CONSERVA)
  /root/autotube_mini_test/final_video.txt      (id, path, sha256, nodos, gate)
  /root/autotube_mini_test/run.log             (log completo del piloto)

Lanzar desapegado:  setsid nohup python3 scripts/gen_dist_test.py > runner.log 2>&1 &
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

WT = Path("/root/.opencode-worktrees/autotube/20261003-dist-pipeline")
OUT = Path(os.environ.get("GEN_TEST_OUT", "/root/autotube_mini_test"))
FINAL = OUT / "final"
RUNNER = WT / "scripts" / "run_distributed_video.py"
RES = Path("/var/lib/taildeck/distributed/results")
RUN_ID = os.environ.get("GEN_RUN_ID") or ("gen-dist-" + time.strftime("%Y%m%d-%H%M%S"))

ENV = dict(os.environ)
ENV["AUTOTUBE_CONCAT_BATCH_SIZE"] = os.environ.get("AUTOTUBE_CONCAT_BATCH_SIZE", "6")
ENV["AUTOTUBE_PRODUCTION_DB"] = os.environ.get("AUTOTUBE_PRODUCTION_DB", "/root/autotube/autotube.db")
ENV["AUTOTUBE_DIST_TIMEOUT_SEC"] = os.environ.get("AUTOTUBE_DIST_TIMEOUT_SEC", "900")
ENV["PYTHONUNBUFFERED"] = "1"


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _collect(rep: list[str], rc, dt: float) -> None:
    rep.append(f"rc={rc}")
    rep.append(f"duracion_run_sec={dt:.0f}")

    vp = None
    tj = WT / "output" / "dist_test" / RUN_ID / "timings.json"
    if tj.exists():
        d = json.loads(tj.read_text(encoding="utf-8"))
        vp = d.get("video", {}).get("path")
        rep.append(f"phases={json.dumps(d.get('phases_sec'))}")
        rep.append(f"total_sec={d.get('total_sec')}")
    rep.append(f"video_path={vp}")

    if vp and Path(vp).exists():
        dst = FINAL / Path(vp).name
        shutil.copy2(vp, dst)
        rep.append(f"VIDEO_CONSERVADO={dst}")
        rep.append(f"sha256={_sha(dst)}")
        rep.append(f"size_mb={dst.stat().st_size / 1024 / 1024:.1f}")
        try:
            pr = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries",
                 "format=duration:stream=codec_type,width,height", "-of", "json", str(dst)],
                capture_output=True, text=True, timeout=60,
            )
            rep.append("ffprobe=" + (pr.stdout or pr.stderr).strip().replace("\n", " "))
        except Exception as exc:  # noqa: BLE001
            rep.append(f"ffprobe_error={exc}")

    db = WT / "output" / "dist_test" / RUN_ID / "autotube_test.db"
    if db.exists():
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            row = c.execute(
                "SELECT id,status,yt_video_id,video_path FROM videos ORDER BY id DESC LIMIT 1"
            ).fetchone()
            rep.append(f"clone_video(id,status,yt_id,path)={row}")
        except Exception as exc:  # noqa: BLE001
            rep.append(f"db_error={exc}")

    for kind in ("atube-render2", "atube-concat"):
        for f in sorted(glob.glob(str(RES / f"{kind}-*{RUN_ID}*.json"))):
            try:
                d = json.loads(Path(f).read_text(encoding="utf-8"))
                rep.append(f"{kind}: status={d.get('status')} perNode={d.get('perNode')} "
                           f"stats={d.get('stats')} error={str(d.get('error'))[:160]}")
            except Exception as exc:  # noqa: BLE001
                rep.append(f"{kind}_err={exc}")

    try:
        sys.path.insert(0, str(WT))
        os.environ.setdefault("DATABASE_PATH", "/tmp/autotube_worker.db")
        import importlib
        from pipeline.render_gate import evaluate_render_gate
        cfg = importlib.import_module("config.canal2_config")
        if vp and Path(vp).exists():
            g = evaluate_render_gate(vp, channel_config=cfg)
            rep.append(f"gate passed={g['passed']} black={g['black_sec']} "
                       f"silence={g['silence_sec']} blocking={g['blocking']}")
    except Exception as exc:  # noqa: BLE001
        rep.append(f"gate_error={exc}")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    FINAL.mkdir(parents=True, exist_ok=True)
    rep = [f"mini test distribuido @ {time.strftime('%Y-%m-%d %H:%M:%S')}", f"RUN_ID={RUN_ID}"]
    rc = 99
    dt = 0.0
    try:
        args = [sys.executable, str(RUNNER), "--canal", "canal2", "--mode", "quarter",
                "--skip-scrape", "--media-fast", "--run-id", RUN_ID]
        t0 = time.time()
        with open(OUT / "run.log", "w", encoding="utf-8") as fh:
            rc = subprocess.run(args, cwd=str(WT), env=ENV,
                                stdout=fh, stderr=subprocess.STDOUT).returncode
        dt = time.time() - t0
        _collect(rep, rc, dt)
    except Exception as exc:  # noqa: BLE001
        rep.append(f"EXCEPTION_runner={exc!r}")
    finally:
        text = "\n".join(rep) + "\n"
        (OUT / "final_video.txt").write_text(text, encoding="utf-8")
        print(text)
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
