"""Tests del cliente del motor distribuido (spool + lectura de resultados)."""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _reload_client(tmp_dir):
    os.environ["TAILDECK_DIST_DIR"] = tmp_dir
    import importlib
    import pipeline_dist.dsl_client as mod
    importlib.reload(mod)
    return mod


def test_submit_escribe_spool_y_wait_lee_resultado(tmp_path):
    client = _reload_client(str(tmp_path))
    eid = client.submit("synth-sum", {"start": 1, "end": 10}, exec_id="test-exec-1",
                        max_inflight=2, deadline_sec=1234,
                        req={"cores": 1, "memMb": 128})
    assert eid == "test-exec-1"
    spool = os.path.join(str(tmp_path), "spool", "test-exec-1.json")
    assert os.path.exists(spool)
    payload = json.load(open(spool, encoding="utf-8"))
    assert payload["definition"] == "synth-sum"
    assert payload["maxInflight"] == 2
    assert payload["deadlineSec"] == 1234
    assert payload["req"] == {"cores": 1, "memMb": 128}

    # El motor publicaría el resumen aquí; lo simulamos.
    os.makedirs(os.path.join(str(tmp_path), "results"), exist_ok=True)
    with open(os.path.join(str(tmp_path), "results", "test-exec-1.json"), "w") as fh:
        json.dump({"id": "test-exec-1", "status": "done", "result": {"scenes": []}}, fh)
    summary = client.wait("test-exec-1", timeout=5, poll=0.05)
    assert summary["status"] == "done"
    assert client.accepted_scenes(summary) == []


def test_sanitiza_ids_peligrosos(tmp_path):
    client = _reload_client(str(tmp_path))
    eid = client.submit("synth-sum", {}, exec_id="../../etc/passwd")
    assert "/" not in eid and eid.startswith("etc-passwd")
