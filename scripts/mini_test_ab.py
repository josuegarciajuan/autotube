#!/usr/bin/env python3
"""Mini test A/B del pipeline distribuido identity-preserving.

Secuencia desatendida:
  1) freeze-only: genera un bundle de inputs (script/audio/media/scene_ranges).
  2) dist: render+concat en la flota desde el bundle.
  3) local: render+concat local (-no-dist) desde el MISMO bundle.
  4) compara sha256, participación por nodo y gate; escribe report.txt.

Pensado para lanzarse DESAPEGADO de opencode (setsid nohup ... &).
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

WT = Path("/root/.opencode-worktrees/autotube/20261003-dist-pipeline")
OUT = Path(os.environ.get("MINI_TEST_OUT", "/root/autotube_mini_test"))
RUNNER = WT / "scripts" / "run_distributed_video.py"
DIST_RESULTS = Path("/var/lib/taildeck/distributed/results")
ENV = dict(os.environ)
ENV["AUTOTUBE_CONCAT_BATCH_SIZE"] = os.environ.get("AUTOTUBE_CONCAT_BATCH_SIZE", "3")
ENV["AUTOTUBE_PRODUCTION_DB"] = os.environ.get("AUTOTUBE_PRODUCTION_DB", "/root/autotube/autotube.db")
ENV["PYTHONUNBUFFERED"] = "1"


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _run(args: list[str], log: Path):
    t0 = time.time()
    with open(log, "w", encoding="utf-8") as fh:
        rc = subprocess.run(
            [sys.executable, str(RUNNER)] + args,
            cwd=str(WT), env=ENV, stdout=fh, stderr=subprocess.STDOUT,
        ).returncode
    return rc, time.time() - t0


def _video_of(run_id: str):
    tj = WT / "output" / "dist_test" / run_id / "timings.json"
    if not tj.exists():
        return None, None
    d = json.loads(tj.read_text(encoding="utf-8"))
    return d.get("video", {}).get("path"), d


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    rep: list[str] = [f"mini test @ {time.strftime('%Y-%m-%d %H:%M:%S')}"]
    bundle = OUT / "bundle"

    rc, dt = _run(["--canal", "canal2", "--mode", "quick", "--media-fast",
                   "--freeze", str(bundle), "--freeze-only", "--run-id", "mini-freeze"],
                  OUT / "freeze.log")
    rep.append(f"[1] freeze rc={rc} t={dt:.0f}s")
    if rc != 0:
        (OUT / "report.txt").write_text("\n".join(rep) + "\n", encoding="utf-8")
        return 1

    rc_d, t_d = _run(["--canal", "canal2", "--mode", "quick", "--media-fast",
                      "--from-freeze", str(bundle), "--run-id", "mini-dist"],
                     OUT / "dist.log")
    vp_d, tim_d = _video_of("mini-dist")
    rep.append(f"[2] dist rc={rc_d} t={t_d:.0f}s video={vp_d}")

    rc_l, t_l = _run(["--canal", "canal2", "--mode", "quick", "--media-fast",
                      "--from-freeze", str(bundle), "--no-dist", "--run-id", "mini-local"],
                     OUT / "local.log")
    vp_l, tim_l = _video_of("mini-local")
    rep.append(f"[3] local rc={rc_l} t={t_l:.0f}s video={vp_l}")

    if vp_d and vp_l and Path(vp_d).exists() and Path(vp_l).exists():
        hd, hl = _sha(Path(vp_d)), _sha(Path(vp_l))
        rep.append(f"[4] sha256 dist ={hd}")
        rep.append(f"[4] sha256 local={hl}")
        rep.append("[4] IDENTIDAD: " + ("OK ✅" if hd == hl else "DIFERENTE ❌"))
        rep.append(f"[4] size dist={Path(vp_d).stat().st_size} local={Path(vp_l).stat().st_size}")
    else:
        rep.append("[4] falta alguno de los vídeos → sin comparación")

    for kind in ("atube-render2", "atube-concat"):
        for f in sorted(glob.glob(str(DIST_RESULTS / f"{kind}-*mini-dist*.json"))):
            try:
                d = json.loads(Path(f).read_text(encoding="utf-8"))
                rep.append(f"[nodeas] {kind}: status={d.get('status')} "
                           f"perNode={d.get('perNode')} stats={d.get('stats')}")
            except Exception as exc:  # noqa: BLE001
                rep.append(f"[nodeas] {kind}: error {exc}")

    try:
        sys.path.insert(0, str(WT))
        os.environ.setdefault("DATABASE_PATH", "/tmp/autotube_worker.db")
        import importlib
        from pipeline.render_gate import evaluate_render_gate
        cfg = importlib.import_module("config.canal2_config")
        for label, p in (("dist", vp_d), ("local", vp_l)):
            if p and Path(p).exists():
                g = evaluate_render_gate(p, channel_config=cfg)
                rep.append(f"[gate:{label}] passed={g['passed']} black={g['black_sec']} "
                           f"silence={g['silence_sec']} blocking={g['blocking']}")
    except Exception as exc:  # noqa: BLE001
        rep.append(f"[gate] error: {exc}")

    for label, tim in (("dist", tim_d), ("local", tim_l)):
        if tim:
            rep.append(f"[timings:{label}] {json.dumps(tim.get('phases_sec'))} "
                       f"total={tim.get('total_sec')}")

    text = "\n".join(rep) + "\n"
    (OUT / "report.txt").write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
