#!/usr/bin/env python3
"""Diagnostico de la flota distribuida de SuperServer.

Uso: python3 scripts/diag_dist_fleet.py [--json]
Muestra flags activos, tags requeridos, nodos excluidos y requisitos de recursos.
No consume cuota YouTube. No modifica nada.
"""
import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

def _flag(n, d=False):
    r = os.environ.get(n, "")
    return d if not r else r.strip().lower() in ("1","true","yes","on")

def _int(n, d=0):
    try: return int(os.environ.get(n,"") or d)
    except: return d

def check():
    out = {}
    out["flags"] = {
        "RENDER_V2": _flag("AUTOTUBE_DIST_RENDER_V2"),
        "CONCAT": _flag("AUTOTUBE_DIST_CONCAT"),
        "IMAGES": _flag("AUTOTUBE_DIST_IMAGES"),
        "YTDLP": _flag("AUTOTUBE_DIST_YTDLP"),
        "SHORTS": _flag("AUTOTUBE_DIST_SHORTS"),
    }
    out["tags"] = {
        "render": os.environ.get("AUTOTUBE_RENDER_TAGS","render"),
        "concat": os.environ.get("AUTOTUBE_ASSEMBLE_TAGS","render"),
        "images": os.environ.get("AUTOTUBE_SD_TAGS","local-sd"),
    }
    out["excluded_nodes"] = [n.strip() for n in os.environ.get("AUTOTUBE_DIST_EXCLUDED_NODES","").split(",") if n.strip()]
    out["resources"] = {
        "render": {"cores": _int("AUTOTUBE_DIST_THREADS",2), "memMb": _int("AUTOTUBE_DIST_MEM_MB",2048)},
        "concat": {"cores": _int("AUTOTUBE_DIST_CONCAT_CORES",2), "memMb": _int("AUTOTUBE_DIST_MEM_MB",2048)},
        "images": {"cores": _int("AUTOTUBE_DIST_SD_THREADS",2), "memMb": _int("AUTOTUBE_DIST_SD_MEM_MB",4600)},
    }
    out["repair_rounds"] = _int("AUTOTUBE_DIST_REPAIR_ROUNDS", 2)
    out["timeouts"] = {
        "dist_sec": _int("AUTOTUBE_DIST_TIMEOUT_SEC", 1800),
        "sd_sec": _int("AUTOTUBE_DIST_SD_TIMEOUT_SEC", 0),
    }

    # Spool status
    dist_dir = Path(__file__).resolve().parent.parent / "pipeline_dist"
    spool = dist_dir / "spool"
    results = dist_dir / "results"
    out["spool"] = {"path": str(spool), "exists": spool.exists(),
                    "pending": len(list(spool.glob("*.json"))) if spool.exists() else 0}
    out["results"] = {"path": str(results), "exists": results.exists(),
                      "count": len(list(results.glob("*.json"))) if results.exists() else 0}
    return out

def main():
    data = check()
    if "--json" in sys.argv:
        print(json.dumps(data, indent=2))
    else:
        print("=== Flota SuperServer: Estado de configuracion ===\n")
        print("Flags activos:")
        for k,v in data["flags"].items():
            print(f"  {k:15s} = {'ON' if v else 'off'}")
        print("\nTags requeridos:")
        for k,v in data["tags"].items():
            print(f"  {k:15s} = {v}")
        print(f"\nNodos excluidos: {data['excluded_nodes'] or '(ninguno)'}")
        print("\nRecursos por definicion:")
        for k,v in data["resources"].items():
            print(f"  {k:15s} cores={v['cores']} mem={v['memMb']}MB")
        print(f"\nRepair rounds: {data['repair_rounds']}")
        print(f"Timeouts: {data['timeouts']}")
        print(f"\nSpool: {data['spool']}")
        print(f"Results: {data['results']}")

if __name__ == "__main__":
    main()
