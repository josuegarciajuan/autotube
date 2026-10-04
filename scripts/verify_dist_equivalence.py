#!/usr/bin/env python3
"""Harness de equivalencia distribuida (F2/F3/golden test).

Verifica que el contenedor hermético ``autotube-worker:1`` reproduce **bit a bit**
lo que produce el control plane para las operaciones que se van a distribuir:

  1. Pre-transcode 4K→1080p (ffmpeg puro: concat/transcode).
  2. Render de segmento vía MoviePy (imageio-ffmpeg).

Por defecto compara ``local`` vs ``docker run autotube-worker:1``.
Con ``--node <nombre>`` compara además contra ese nodo (si tiene la imagen),
vía SSH ``docker run``; sirve de *qualification* de nodos.

Salida: tabla de hashes + veredicto. Exit 0 si todo coincide; 1 si no.

Uso:
    python3 scripts/verify_dist_equivalence.py
    python3 scripts/verify_dist_equivalence.py --node mail --node oficina
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

IMAGE = os.environ.get("AUTOTUBE_WORKER_IMAGE", "autotube-worker:1")
PRETRANSCODE_VF = ("scale=1920:1080:force_original_aspect_ratio=decrease,"
                   "pad=1920:1080:(ow-iw)/2:(oh-ih)/2")
MP_RENDER_SNIPPET = (
    "import sys\n"
    "from moviepy import ColorClip\n"
    "c = ColorClip(size=(320,180), color=(10,20,30), duration=1).with_fps(24)\n"
    "c.write_videofile(sys.argv[1], fps=24, codec='libx264', preset='fast',\n"
    "    ffmpeg_params=['-crf','16','-pix_fmt','yuv420p','-an','-threads','2'], logger=None)\n"
)


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def _make_4k_input(tmp: Path) -> Path:
    src = tmp / "in4k.mp4"
    _run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
          "-i", "testsrc=size=4096x2160:rate=30:duration=2",
          "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", str(src)], timeout=120)
    return src


def _local_pretranscode(tmp: Path, src: Path) -> Path:
    out = tmp / "pre_local.mp4"
    _run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vf", PRETRANSCODE_VF,
          "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-threads", "4",
          "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)], timeout=180)
    return out


def _docker_pretranscode(tmp: Path, src: Path) -> Path:
    out = tmp / "pre_docker.mp4"
    _run(["docker", "run", "--rm", "--security-opt", "seccomp=unconfined", "-v", f"{tmp}:/work", IMAGE, "sh", "-c",
          f'ffmpeg -y -v error -i /work/{src.name} -vf "{PRETRANSCODE_VF}" '
          f'-c:v libx264 -preset ultrafast -crf 23 -threads 4 -c:a aac -b:a 128k '
          f'-movflags +faststart /work/{out.name}'], timeout=180)
    return out


def _local_mp_render(tmp: Path) -> Path:
    out = tmp / "mp_local.mp4"
    r = _run(["python3", "-c", MP_RENDER_SNIPPET, str(out)], timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"moviepy local falló: {r.stderr[-400:]}")
    return out


def _docker_mp_render(tmp: Path) -> Path:
    out = "mp_docker.mp4"
    script = tmp / "mp_render.py"
    script.write_text("import sys\n" + MP_RENDER_SNIPPET, encoding="utf-8")
    r = _run(["docker", "run", "--rm", "--security-opt", "seccomp=unconfined", "-v", f"{tmp}:/work", IMAGE,
              "python3", f"/work/{script.name}", f"/work/{out}"], timeout=180)
    if r.returncode != 0:
        raise RuntimeError(f"moviepy docker falló: {r.stderr[-400:]}")
    return tmp / out


def _node_docker_sha(node: str, state: dict, tmp: Path) -> dict:
    """Corre el golden test en el nodo vía ssh + docker (usa la imagen del nodo)."""
    n = state["nodes"][node]
    dst = f"{n.get('user') or 'root'}@{n['ip']}"
    remote = f"/tmp/dist_equiv_{os.getpid()}"
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
           "-o", "StrictHostKeyChecking=no", dst]
    _run(ssh + [f"mkdir -p {remote}"])
    # Crear input y script en el nodo con el mismo contenido exacto.
    _run(["scp", "-q", str(tmp / "in4k.mp4"), f"{dst}:{remote}/in4k.mp4"])
    script_local = tmp / "mp_render.py"
    _run(["scp", "-q", str(script_local), f"{dst}:{remote}/mp_render.py"])
    cmd = (
        f"cd {remote} && "
        f"docker run --rm --security-opt seccomp=unconfined -v {remote}:/work {IMAGE} sh -c "
        f"'ffmpeg -y -v error -i /work/in4k.mp4 -vf \"{PRETRANSCODE_VF}\" "
        f"-c:v libx264 -preset ultrafast -crf 23 -threads 4 -c:a aac -b:a 128k "
        f"-movflags +faststart /work/pre_node.mp4' && "
        f"docker run --rm --security-opt seccomp=unconfined -v {remote}:/work {IMAGE} python3 /work/mp_render.py /work/mp_node.mp4 && "
        f"sha256sum pre_node.mp4 mp_node.mp4"
    )
    r = _run(ssh + [cmd], timeout=300)
    out = {}
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) == 2:
            out[parts[1]] = parts[0]
    _run(ssh + [f"rm -rf {remote}"])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Golden test de equivalencia distribuida")
    ap.add_argument("--node", action="append", default=[],
                    help="Nodo(s) a verificar además del contenedor local")
    ap.add_argument("--state", default="/root/superserver/data/state.json")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="dist_equiv_"))
    try:
        src = _make_4k_input(tmp)
        results: dict[str, dict[str, str]] = {}

        pre_local = _sha(_local_pretranscode(tmp, src))
        mp_local = _sha(_local_mp_render(tmp))
        pre_docker = _sha(_docker_pretranscode(tmp, src))
        mp_docker = _sha(_docker_mp_render(tmp))
        results["local"] = {"pre_transcode": pre_local, "moviepy_render": mp_local}
        results["docker(local)"] = {"pre_transcode": pre_docker, "moviepy_render": mp_docker}

        state = {}
        if args.node:
            state = json.load(open(args.state))
            for node in args.node:
                try:
                    ns = _node_docker_sha(node, state, tmp)
                    results[f"node:{node}"] = {
                        "pre_transcode": ns.get("pre_node.mp4", "MISSING"),
                        "moviepy_render": ns.get("mp_node.mp4", "MISSING"),
                    }
                except Exception as exc:  # noqa: BLE001
                    results[f"node:{node}"] = {"error": str(exc)}

        print(f"{'target':22} {'pre_transcode':16} {'moviepy_render':16}")
        for name, r in results.items():
            if "error" in r:
                print(f"{name:22} ERROR {r['error'][:80]}")
                continue
            print(f"{name:22} {r['pre_transcode'][:16]} {r['moviepy_render'][:16]}")

        ref = results["local"]
        ok = all(
            r.get("pre_transcode") == ref["pre_transcode"]
            and r.get("moviepy_render") == ref["moviepy_render"]
            for r in results.values() if "error" not in r
        )
        errors = [n for n, r in results.items() if "error" in r]
        print("\nVEREDICTO:", "✅ IDÉNTICO" if ok and not errors else "❌ MISMATCH")
        if errors:
            print("  Nodos con error (excluir):", ", ".join(errors))
        return 0 if (ok and not errors) else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
