"""Regresión: transporte OAuth compatible con google-auth 2.54.

Contexto (oct 2026): las credenciales google-auth modernas NO exponen
``credentials.authorize()`` (eso era oauth2client) y ``googleapiclient``
rechaza recibir ``http`` y ``credentials`` a la vez. El wrapper válido es
``google_auth_httplib2.AuthorizedHttp``.

Estas rutas no se ejercitan en el flujo normal (proxy desactivado / ningún
canal egress), pero fallarían al activarse si se dejara el patrón antiguo.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import pipeline.youtube_uploader as up


# ── Uploader: rama con proxy ────────────────────────────────

class _NoAuthorizeCreds:
    """Credencial google-auth moderna: valid=True y SIN authorize()."""

    valid = True

    def authorize(self, http):  # noqa: D401 — debe ser inalcanzable
        raise AssertionError("credentials.authorize() no existe en google-auth 2.54")


def test_uploader_proxy_usa_authorized_http(monkeypatch):
    uploader = up.YouTubeUploader.__new__(up.YouTubeUploader)
    uploader._service = None
    uploader._credentials = _NoAuthorizeCreds()

    monkeypatch.setattr(up.YouTubeUploader, "_should_use_proxy", lambda self: True)
    monkeypatch.setattr(up.YouTubeUploader, "_configure_proxy", lambda self, http: None)

    captured = {}

    class FakeAuthorizedHttp:
        def __init__(self, creds, http=None):
            assert http is not None, "debe recibir transporte httplib2"
            assert creds is uploader._credentials
            captured["timeout"] = http.timeout
            self.timeout = http.timeout

    def fake_build(svc, ver, **kwargs):
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(up, "AuthorizedHttp", FakeAuthorizedHttp)
    monkeypatch.setattr(up, "build", fake_build)

    uploader._get_service()

    kw = captured["kwargs"]
    assert "http" in kw and isinstance(kw["http"], FakeAuthorizedHttp)
    assert "credentials" not in kw, "no se puede pasar http y credentials a la vez"
    assert captured["timeout"] == up.UPLOAD_HTTP_TIMEOUT_S


def test_uploader_sin_proxy_mantiene_credentials(monkeypatch):
    """Sin proxy el comportamiento no cambia (build con credentials)."""
    uploader = up.YouTubeUploader.__new__(up.YouTubeUploader)
    uploader._service = None
    uploader._credentials = _NoAuthorizeCreds()

    monkeypatch.setattr(up.YouTubeUploader, "_should_use_proxy", lambda self: False)

    captured = {}

    def fake_build(svc, ver, **kwargs):
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(up, "build", fake_build)
    uploader._get_service()

    kw = captured["kwargs"]
    assert kw["credentials"] is uploader._credentials
    assert "http" not in kw


# ── Agente egress: rama youtubeAnalytics ────────────────────

def test_egress_analytics_usa_authorized_http(monkeypatch):
    import egress_agent.youtube_api as eg

    # El agente reutiliza las credenciales refrescadas del uploader.
    class FakeUploader:
        def __init__(self, *a, **k):
            self._credentials = _NoAuthorizeCreds()

    monkeypatch.setattr(
        "pipeline.youtube_uploader.YouTubeUploader", FakeUploader
    )

    captured = {}

    class FakeAuthorizedHttp:
        def __init__(self, creds, http=None):
            assert http is not None
            captured["timeout"] = http.timeout

    def fake_build(api, ver, **kwargs):
        captured["kwargs"] = kwargs
        captured["api"] = (api, ver)
        return object()

    monkeypatch.setattr("google_auth_httplib2.AuthorizedHttp", FakeAuthorizedHttp)
    monkeypatch.setattr("googleapiclient.discovery.build", fake_build)

    class Cfg:
        slug = "canalx"

    eg._service(Cfg(), api="youtubeAnalytics", version="v2")

    assert captured["api"] == ("youtubeAnalytics", "v2")
    kw = captured["kwargs"]
    assert "http" in kw and isinstance(kw["http"], FakeAuthorizedHttp)
    assert "credentials" not in kw, "googleapiclient rechaza http + credentials"
    assert captured["timeout"] == eg._GOOGLE_HTTP_TIMEOUT_S
