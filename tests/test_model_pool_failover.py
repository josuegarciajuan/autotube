"""Regresión: el 402 "Insufficient Balance" de DeepSeek debe marcar el proveedor
como no-retryable para que el failover a OpenAI salte al primer fallo.

Contexto (oct-2026): DeepSeek se quedó sin saldo ($0). Sin estos patrones el
pool reintentaba 3 veces el proveedor caído por cada llamada y el failover nunca
ocurría, disparando una cascada de alertas ``error_*``.
"""
from config.model_pool import (
    is_non_retryable_error,
    mark_provider_unavailable,
    provider_is_available,
    reset_provider_circuit,
)


class _FakeError(Exception):
    def __init__(self, msg: str, status_code=None):
        super().__init__(msg)
        if status_code is not None:
            self.status_code = status_code


def test_deepseek_insufficient_balance_is_non_retryable():
    err = _FakeError(
        "Error code: 402 - {'error': {'message': 'Insufficient Balance', "
        "'code': 'invalid_request_error'}}"
    )
    assert is_non_retryable_error(err) is True


def test_status_402_is_non_retryable():
    assert is_non_retryable_error(_FakeError("boom", status_code=402)) is True
    assert is_non_retryable_error(_FakeError("boom", status_code=401)) is True
    assert is_non_retryable_error(_FakeError("boom", status_code=403)) is True


def test_transient_errors_stay_retryable():
    assert is_non_retryable_error(_FakeError("socket timeout")) is False
    assert is_non_retryable_error(_FakeError("HTTP 500 server error")) is False


def test_circuit_breaker_marks_and_clears():
    reset_provider_circuit()
    try:
        assert provider_is_available("deepseek") is True
        mark_provider_unavailable("deepseek", ttl_seconds=60)
        assert provider_is_available("deepseek") is False
        reset_provider_circuit()
        assert provider_is_available("deepseek") is True
    finally:
        reset_provider_circuit()
