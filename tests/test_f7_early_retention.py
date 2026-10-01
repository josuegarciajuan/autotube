"""Tests de F7 — diagnóstico de retención por CAÍDA y gancho 30/60/90 s."""
from api.services import retention_feedback as rf


TIMELINE = [
    {"phase_id": "gancho", "start": 0, "end": 60},
    {"phase_id": "desarrollo", "start": 60, "end": 400},
    {"phase_id": "cierre", "start": 400, "end": 600},
]


def _curve():
    # 0.55 en gancho, cae a 0.20 al entrar en desarrollo, sube a 0.35 al final.
    out = []
    for i in range(0, 100, 5):
        t = (i / 100.0) * 600
        wr = 0.55 if t < 60 else (0.20 if t < 400 else 0.35)
        out.append({"elapsed": i / 100.0, "watch_ratio": wr})
    return out


def test_phase_drops_attributes_loss_to_hook_exit():
    drops = rf._phase_drops(_curve(), TIMELINE, 600)
    # La mayor pérdida (0.55->0.20) ocurre al salir del gancho.
    assert drops.get("gancho", 0) > 0.3
    assert drops.get("desarrollo", 0) == 0


def test_early_retention_reads_30_60_90s():
    early = rf._early_retention(_curve(), 600)
    assert set(early) == {"r30", "r60", "r90"}
    assert early["r30"] > 0.5           # aún en el gancho
    assert early["r60"] < 0.3           # ya en desarrollo


def test_directive_flags_weak_hook():
    directive = rf._build_directive(
        20.0, 40.0, "down", weak=[], phase_directives=None,
        early_retention={"r30": 80.0, "r60": 45.0, "r90": 30.0},
    )
    assert "GANCHO DEBIL" in directive
    assert "mapas" in directive  # visuales explicativos sugeridos


def test_directive_no_hook_flag_when_retention_ok():
    directive = rf._build_directive(
        45.0, 40.0, "up", weak=[], phase_directives=None,
        early_retention={"r60": 75.0},
    )
    assert "GANCHO DEBIL" not in directive
