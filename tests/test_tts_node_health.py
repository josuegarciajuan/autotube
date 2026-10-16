"""Registro de salud de nodos TTS (Fase 3)."""
from api.services import tts_node_health as H


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOTUBE_TTS_HEALTH_FILE", str(tmp_path / "health.json"))
    monkeypatch.delenv("AUTOTUBE_TTS_QUARANTINE_AFTER", raising=False)


def test_estado_inicial_healthy(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    assert H.state_of("mail") == H.HEALTHY
    assert H.is_available("mail") is True


def test_degrada_y_luego_cuarentena(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    assert H.record_failure("mail", "c1") == H.DEGRADED
    assert H.is_available("mail") is True  # degradado sigue disponible
    assert H.record_failure("mail", "c2") == H.QUARANTINED
    assert H.is_available("mail") is False


def test_umbral_configurable(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("AUTOTUBE_TTS_QUARANTINE_AFTER", "1")
    assert H.record_failure("vps649560", "x") == H.QUARANTINED


def test_exito_limpia_historial(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    H.record_failure("oficina", "x")
    H.record_success("oficina")
    assert H.state_of("oficina") == H.HEALTHY


def test_reconcile_limpia_solo_certificados(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    H.record_failure("mail", "x"); H.record_failure("mail", "x")
    H.record_failure("oficina", "x"); H.record_failure("oficina", "x")
    H.reconcile_with_certification({"mail"})
    assert H.state_of("mail") == H.HEALTHY
    assert H.state_of("oficina") == H.QUARANTINED


def test_registro_corrupto_no_rompe(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    (tmp_path / "health.json").write_text("{no json")
    assert H.state_of("mail") == H.HEALTHY
    assert H.record_failure("mail", "x") in (H.DEGRADED, H.QUARANTINED)
