"""Regresión: la limpieza de duplicados distingue un fallo REAL de un límite
transitorio de cuota.

Contexto (oct-2026): `cleanup_yt_duplicates.py` contaba cualquier fallo (incluido
un 403 quotaExceeded) como error y salía con rc=1, lo que marcaba el service
`autotube-dup-cleanup.service` como fallido casi cada día aunque el trabajo
fuese correcto. Ahora la cuota agotada es transitoria (no falla el servicio) y
`_delete_video` devuelve un código de estado explícito.
"""
from googleapiclient.errors import HttpError

from scripts.cleanup_yt_duplicates import _delete_video


class _Resp:
    def __init__(self, status, reason):
        self.status = status
        self.reason = reason


def _http_error(status, reason):
    import json
    body = json.dumps({"error": {"errors": [{"reason": reason}], "message": reason}}).encode()
    return HttpError(_Resp(status, reason), body)


class _Delete:
    def __init__(self, kind):
        self.kind = kind

    def delete(self, id=None):
        kind = self.kind

        class _Exec:
            def execute(self_inner):
                if kind == "ok":
                    return {}
                if kind == "quota":
                    raise _http_error(403, "quotaExceeded")
                if kind == "forbidden":
                    raise _http_error(403, "forbidden")
                if kind == "notfound":
                    raise _http_error(404, "videoNotFound")
                raise RuntimeError("boom")

        return _Exec()


class _Service:
    def __init__(self, kind):
        self.kind = kind

    def videos(self):
        return _Delete(self.kind)


def test_delete_video_status_codes():
    assert _delete_video(_Service("ok"), "abc", channel_slug="canal2") == "ok"
    assert _delete_video(_Service("quota"), "abc", channel_slug="canal2") == "quota"
    assert _delete_video(_Service("notfound"), "abc", channel_slug="canal2") == "gone"
    assert _delete_video(_Service("forbidden"), "abc", channel_slug="canal2") == "error"
    assert _delete_video(_Service("boom"), "abc", channel_slug="canal2") == "error"
