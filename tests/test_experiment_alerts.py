"""Tests de las alertas críticas del experimento.

Cubre:
  1. ``reminder_severity``: severidad desde metadata_json (default warning,
     inválidas/malformadas → warning).
  2. ``evaluate_experiment_progress``: no dispara antes del mínimo de días,
     detecta estancamiento y deja de estar estancado si algún KPI leading mejora.
"""
import json
from datetime import datetime, timedelta, timezone

from api.services.lifecycle_monitor import reminder_severity
from api.services.experiment_report import evaluate_experiment_progress


def test_reminder_severity_default_warning():
    assert reminder_severity(None) == "warning"
    assert reminder_severity("") == "warning"
    assert reminder_severity(json.dumps({})) == "warning"


def test_reminder_severity_critical():
    assert reminder_severity(json.dumps({"severity": "critical"})) == "critical"


def test_reminder_severity_invalid_or_malformed():
    assert reminder_severity(json.dumps({"severity": "catastrophic"})) == "warning"
    assert reminder_severity("{not json") == "warning"


def _report(days_ago: int, channels: list) -> dict:
    started = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    return {"experiment_started_at": started, "channels": channels}


def _ch(slug, ctr_delta, ipv_delta):
    return {
        "slug": slug,
        "leading": {
            "longform_ctr_pct_delta": ctr_delta,
            "longform_impr_per_video_delta": ipv_delta,
            "longform_ctr_pct_now": 1.8,
            "longform_ctr_pct_pre": 2.4,
            "longform_impr_per_video_now": 60.0,
            "longform_impr_per_video_pre": 90.0,
        },
    }


def test_progress_too_early_not_stagnant():
    rep = _report(3, [_ch("canal3", -0.6, -30.0)])
    ev = evaluate_experiment_progress(rep, min_days=10)
    assert ev["stagnant"] is False
    assert "solo 3d" in ev["reason"]


def test_progress_stagnant_when_no_kpi_improves():
    rep = _report(12, [_ch("canal3", -0.6, -30.0), _ch("canal5", -0.2, None)])
    ev = evaluate_experiment_progress(rep, min_days=10)
    assert ev["stagnant"] is True
    assert ev["days"] >= 10


def test_progress_not_stagnant_when_any_kpi_improves():
    rep = _report(12, [_ch("canal3", -0.6, -30.0), _ch("canal5", 0.3, -5.0)])
    ev = evaluate_experiment_progress(rep, min_days=10)
    assert ev["stagnant"] is False
    improving = [c["slug"] for c in ev["channels"] if c["improving"]]
    assert improving == ["canal5"]
