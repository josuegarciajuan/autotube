"""Observabilidad estructurada y correlacionada (oct 2026).

Objetivo: poder analizar fallos del pipeline con un rastro JSON por evento,
correlacionado por ``channel`` / ``video_id`` / ``job_id`` / ``phase`` /
``scene_idx``, sin llenar el disco (rotación diaria + tope de tamaño) y sin
filtrar secretos.

Principios de diseño
--------------------
* **Fail-open absoluto.** Cualquier error de logging (disco lleno, dir de solo
  lectura, handler roto, logger no configurado) se traga y NUNCA propaga ni
  interrumpe el pipeline. ``obs_event`` es siempre seguro de llamar.
* **No-op limpio.** ``OBS_LOG_ENABLED=False`` o ``OBS_LOG_LEVEL="off"`` no
  escriben nada ni lanzan errores.
* **Sin secretos.** ``redact`` oscurece claves sensibles de forma recursiva.
  El texto completo de prompts/respuestas LLM solo se guarda en nivel
  ``trace``; en ``detail`` se guarda ``sha1`` + longitud.
* **Salida JSONL.** Una línea JSON por evento en ``logs/obs/obs.log`` con
  rotación diaria (``TimedRotatingFileHandler``) y tope de tamaño por archivo.
  La retención total la acota ``scripts/purge_logs_and_backups.py
  --max-total-mb``.
* **Idempotente.** ``setup_obs_logging()`` no duplica handlers si se llama
  varias veces.

API pública
-----------
``set_context`` / ``clear_context`` / ``bind_context`` / ``obs_context_scope``
gestionan el contexto correlacionado (``contextvars``).
``obs_event(event, level="info", **fields)`` escribe un evento.
``setup_obs_logging()`` instala el handler del logger ``autotube.obs``.
``redact`` / ``digest_text`` sanean datos.
``build_api_log_handler`` construye el handler rotativo de ``logs/api.log``.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import hashlib
import json
import logging
import logging.handlers
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

# ── Constantes ───────────────────────────────────────────────────────
OBS_LOGGER_NAME = "autotube.obs"
_CONTEXT_KEYS = ("channel", "video_id", "job_id", "phase", "scene_idx")
_MODES = ("off", "summary", "detail", "trace")
_MODE_VERBOSITY = {"off": 0, "summary": 1, "detail": 2, "trace": 3}
_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}
_REDACTION_MARK = "***REDACTED***"

# Keys whose full text is only allowed in ``trace`` mode. In detail/summary
# they are replaced by ``{"sha1": ..., "length": ...}``.
_LLM_TEXT_KEYS = frozenset({
    "prompt", "response", "system_prompt", "user_prompt", "completion",
    "raw_response", "raw_content", "llm_text", "prompt_text",
    "response_text", "messages",
})

# Sensitive key matcher. Deliberately bounded to avoid clobbering unrelated
# keys like "keywords"/"monkey": matches a whole word/segment, not a substring.
_SENSITIVE_RE = re.compile(
    r"(^|[_\-.])"
    r"(token|secret|password|passwd|cookie|authorization|apikey|api[_-]?key|key)"
    r"($|[_\-.])",
    re.IGNORECASE,
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ── Contexto correlacionado (contextvars) ────────────────────────────
_context: contextvars.ContextVar[Dict[str, Any]] = contextvars.ContextVar(
    "autotube_obs_context", default={}
)


def set_context(**kwargs: Any) -> None:
    """Merge fields into the current correlation context (never raises)."""
    try:
        current = dict(_context.get() or {})
        for key, value in kwargs.items():
            if value is not None:
                current[key] = value
        _context.set(current)
    except Exception:  # pragma: no cover - defensive
        pass


def clear_context() -> None:
    """Reset the correlation context (never raises)."""
    try:
        _context.set({})
    except Exception:  # pragma: no cover - defensive
        pass


def get_context() -> Dict[str, Any]:
    """Return a copy of the current correlation context (never raises)."""
    try:
        return dict(_context.get() or {})
    except Exception:  # pragma: no cover - defensive
        return {}


@contextlib.contextmanager
def bind_context(**kwargs: Any):
    """Context manager: set context fields, restore them on exit."""
    marker = None
    try:
        marker = _context.set({**(_context.get() or {}), **{
            k: v for k, v in kwargs.items() if v is not None
        }})
    except Exception:
        marker = None
    try:
        yield
    finally:
        if marker is not None:
            try:
                _context.reset(marker)
            except Exception:  # pragma: no cover - defensive
                pass


class _ContextScope(contextlib.ContextDecorator):
    """Context scope usable as decorator or context manager."""

    def __init__(self, **kwargs: Any) -> None:
        self._kwargs = kwargs

    def _recreate_cm(self):  # thread/task-safe for decorator reuse
        return type(self)(**self._kwargs)

    def __enter__(self):
        self._marker = _context.set({**(_context.get() or {}), **{
            k: v for k, v in self._kwargs.items() if v is not None
        }})
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            _context.reset(self._marker)
        except Exception:  # pragma: no cover - defensive
            pass
        return False


def obs_context_scope(**kwargs: Any) -> _ContextScope:
    """Return a context manager/decorator binding correlation fields.

    Usage::

        with obs_context_scope(phase="media", scene_idx=3):
            ...

        @obs_context_scope(phase="script")
        def generate(): ...
    """
    return _ContextScope(**kwargs)


# ── Filtro de contexto ───────────────────────────────────────────────
class ContextFilter(logging.Filter):
    """Inyecta el contexto correlacionado en cada ``LogRecord``.

    Así el contexto aparece tanto en handlers de texto como JSON, sin que el
    código de cada llamada tenga que pasar los campos.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            ctx = _context.get() or {}
            for key in _CONTEXT_KEYS:
                current = getattr(record, key, None)
                if current in (None, ""):
                    setattr(record, key, ctx.get(key, ""))
        except Exception:  # pragma: no cover - defensive
            pass
        return True


# ── Redacción ────────────────────────────────────────────────────────
def _is_sensitive_key(key: Any) -> bool:
    try:
        return bool(_SENSITIVE_RE.search(str(key)))
    except Exception:  # pragma: no cover - defensive
        return False


def redact(obj: Any, _depth: int = 0) -> Any:
    """Return a copy of *obj* with sensitive keys masked (recursive).

    Matches keys containing ``token|secret|password|cookie|authorization|
    api_key|key`` (as a word/segment). Never raises; depth-bounded to avoid
    cycles/huge structures.
    """
    try:
        if _depth > 12:
            return "<max-depth>"
        if isinstance(obj, dict):
            out: Dict[Any, Any] = {}
            for key, value in obj.items():
                if _is_sensitive_key(key):
                    out[key] = _REDACTION_MARK
                else:
                    out[key] = redact(value, _depth + 1)
            return out
        if isinstance(obj, (list, tuple, set)):
            return [redact(v, _depth + 1) for v in obj]
        return obj
    except Exception:  # pragma: no cover - defensive
        return "<redact-error>"


def digest_text(text: Any) -> Dict[str, Any]:
    """Return ``{"sha1": ..., "length": ...}`` for a text (never raises)."""
    try:
        raw = text if isinstance(text, str) else json.dumps(text, default=str)
        encoded = raw.encode("utf-8", errors="replace")
        return {"sha1": hashlib.sha1(encoded).hexdigest(), "length": len(raw)}
    except Exception:  # pragma: no cover - defensive
        return {"sha1": "", "length": 0}


def llm_text(text: Any) -> Any:
    """Full text only in ``trace`` mode; otherwise sha1 + length digest."""
    try:
        if _state.get("mode") == "trace":
            return text
    except Exception:  # pragma: no cover - defensive
        pass
    return digest_text(text)


def _sanitize_llm_fields(fields: Dict[str, Any], mode: str) -> Dict[str, Any]:
    if mode == "trace":
        return fields
    out = dict(fields)
    for key in list(out.keys()):
        if key.lower() in _LLM_TEXT_KEYS and isinstance(out[key], str):
            out[key] = digest_text(out[key])
    return out


# ── Resolución de configuración ──────────────────────────────────────
def _read_env_or_setting(name: str, default: Any) -> Any:
    """Resolve a setting: environment var > config.settings > default."""
    try:
        env_val = os.environ.get(name)
        if env_val is not None and env_val != "":
            return env_val
    except Exception:  # pragma: no cover - defensive
        pass
    try:
        from config import settings as _settings  # local import: avoid cycles
        val = getattr(_settings, name, None)
        if val is not None:
            return val
    except Exception:  # pragma: no cover - defensive
        pass
    return default


def _as_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _as_int(value: Any, default: int) -> int:
    try:
        if isinstance(value, bool):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        if isinstance(value, bool):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _load_settings() -> Dict[str, Any]:
    """Resolve and clamp the observability settings (never raises)."""
    enabled = _as_bool(_read_env_or_setting("OBS_LOG_ENABLED", True), True)
    mode_raw = _read_env_or_setting("OBS_LOG_LEVEL", "detail")
    mode = str(mode_raw).strip().lower() if isinstance(mode_raw, str) else "detail"
    if mode not in _MODES:
        mode = "detail"

    obs_dir_raw = _read_env_or_setting("OBS_LOG_DIR", "logs/obs")
    max_mb = max(0, _as_int(_read_env_or_setting("OBS_LOG_MAX_MB", 50), 50))
    backups = max(0, _as_int(_read_env_or_setting("OBS_LOG_BACKUPS", 10), 10))
    retention = max(0, _as_int(_read_env_or_setting("OBS_LOG_RETENTION_DAYS", 14), 14))
    sample = _as_float(_read_env_or_setting("OBS_LOG_SAMPLE_RATE", 1.0), 1.0)
    sample = max(0.0, min(1.0, sample))

    obs_dir = Path(str(obs_dir_raw))
    if not obs_dir.is_absolute():
        obs_dir = _PROJECT_ROOT / obs_dir

    return {
        "enabled": enabled,
        "mode": mode,
        "dir": obs_dir,
        "max_mb": max_mb,
        "backups": backups,
        "retention_days": retention,
        "sample_rate": sample,
    }


# ── Handlers ─────────────────────────────────────────────────────────
class _JsonFormatter(logging.Formatter):
    """Serializa un LogRecord a una única línea JSON."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            payload = dict(getattr(record, "obs_payload", None) or {})
            payload.setdefault("event", record.getMessage())
            payload.setdefault("level", record.levelname.lower())
            payload.setdefault(
                "ts", datetime.now(timezone.utc).isoformat()
            )
            for key in _CONTEXT_KEYS:
                if key not in payload:
                    value = getattr(record, key, "")
                    if value not in (None, ""):
                        payload[key] = value
            return json.dumps(payload, ensure_ascii=False, default=str)
        except Exception:  # pragma: no cover - defensive
            try:
                return json.dumps({"event": "obs_format_error"})
            except Exception:
                return '{"event": "obs_format_error"}'


class _TimedSizeRotatingFileHandler(logging.handlers.TimedRotatingFileHandler):
    """Rotación diaria + tope de tamaño por archivo.

    ``TimedRotatingFileHandler`` es el modo preferido (un fichero por día,
    ``backupCount`` backups). Se le añade un tope de tamaño opcional
    (``OBS_LOG_MAX_MB``) para que un día anómalamente verboso no llene el
    disco: al superar el tope se rota con un sufijo horario único, de modo que
    nunca se sobrescribe el backup del día.
    """

    def __init__(self, filename, max_bytes: int = 0, **kwargs: Any) -> None:
        kwargs.pop("maxBytes", None)
        super().__init__(filename, **kwargs)
        self.max_bytes = int(max_bytes or 0)
        self._size_suffix = ""

    def shouldRollover(self, record: logging.LogRecord) -> bool:  # type: ignore[override]
        try:
            if super().shouldRollover(record):
                self._size_suffix = ""
                return True
            if self.max_bytes > 0:
                if self.stream is None:
                    self.stream = self._open()
                self.stream.seek(0, 2)
                msg_len = len(self.format(record)) + 1
                if self.stream.tell() + msg_len > self.max_bytes:
                    self._size_suffix = "-" + time.strftime("%H%M%S")
                    return True
        except Exception:
            return False
        return False

    def rotation_filename(self, default_name: str) -> str:
        try:
            if self._size_suffix:
                return default_name + self._size_suffix
        except Exception:  # pragma: no cover - defensive
            pass
        return super().rotation_filename(default_name)


def _build_obs_handler(
    obs_dir: Path,
    max_mb: int,
    backups: int,
) -> logging.Handler:
    """Build the rotating JSONL handler for ``logs/obs/obs.log``.

    Prefers daily ``TimedRotatingFileHandler`` (see
    ``_TimedSizeRotatingFileHandler``). Falls back to a plain
    ``RotatingFileHandler`` if the timed handler is unavailable.
    """
    obs_dir.mkdir(parents=True, exist_ok=True)
    log_path = obs_dir / "obs.log"
    formatter = _JsonFormatter()
    max_bytes = int(max_mb) * 1024 * 1024 if max_mb and max_mb > 0 else 0

    try:
        handler: logging.Handler = _TimedSizeRotatingFileHandler(
            str(log_path),
            max_bytes=max_bytes,
            when="midnight",
            interval=1,
            backupCount=max(0, int(backups)),
            encoding="utf-8",
            utc=True,
            delay=True,
        )
    except Exception:
        handler = logging.handlers.RotatingFileHandler(
            str(log_path),
            maxBytes=max_bytes or (10 * 1024 * 1024),
            backupCount=max(0, int(backups)),
            encoding="utf-8",
            delay=True,
        )
    handler.setFormatter(formatter)
    handler.addFilter(ContextFilter())
    try:
        setattr(handler, "_autotube_obs_handler", True)
    except Exception:  # pragma: no cover - defensive
        pass
    return handler


def build_api_log_handler(
    path: Path | str,
    max_bytes: int = 50 * 1024 * 1024,
    backup_count: int = 10,
    formatter: Optional[logging.Formatter] = None,
) -> logging.Handler:
    """Build the size-rotating handler for ``logs/api.log``.

    The API log used to be a plain ``FileHandler`` that grew unbounded (193 MB
    observed). This helper centralises the rotation parameters so production
    and tests agree.
    """
    handler = logging.handlers.RotatingFileHandler(
        str(path), maxBytes=int(max_bytes), backupCount=int(backup_count),
        encoding="utf-8", delay=True,
    )
    if formatter is None:
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        )
        formatter.converter = time.gmtime  # UTC — same as DB CURRENT_TIMESTAMP
    handler.setFormatter(formatter)
    handler.addFilter(ContextFilter())
    return handler


# ── Estado del módulo ────────────────────────────────────────────────
_state: Dict[str, Any] = {
    "configured": False,
    "enabled": False,
    "mode": "off",
    "sample_rate": 1.0,
    "dir": None,
    "max_mb": 50,
    "backups": 10,
    "handler": None,
}
import threading as _threading

_lock = _threading.RLock()


def setup_obs_logging(force: bool = False) -> logging.Logger:
    """Install the rotating handler on the dedicated ``autotube.obs`` logger.

    Idempotent: calling it twice does not duplicate handlers. Fail-open: any
    error leaves ``obs_event`` as a safe no-op. ``force=True`` rebuilds the
    handler (used by tests to switch settings).
    """
    logger = logging.getLogger(OBS_LOGGER_NAME)
    try:
        with _lock:
            if _state.get("configured") and not force:
                return logger

            # Remove only handlers we own (idempotency / reconfiguration).
            for existing in list(logger.handlers):
                if getattr(existing, "_autotube_obs_handler", False):
                    try:
                        logger.removeHandler(existing)
                        existing.close()
                    except Exception:  # pragma: no cover - defensive
                        pass

            cfg = _load_settings()
            _state.update({
                "configured": True,
                "enabled": bool(cfg["enabled"]) and cfg["mode"] != "off",
                "mode": cfg["mode"] if cfg["enabled"] else "off",
                "sample_rate": cfg["sample_rate"],
                "dir": str(cfg["dir"]),
                "max_mb": cfg["max_mb"],
                "backups": cfg["backups"],
                "handler": None,
            })
            logger.propagate = False

            if not _state["enabled"]:
                null = logging.NullHandler()
                setattr(null, "_autotube_obs_handler", True)
                logger.addHandler(null)
                logger.setLevel(logging.CRITICAL)
                return logger

            try:
                handler = _build_obs_handler(
                    Path(cfg["dir"]), cfg["max_mb"], cfg["backups"],
                )
                logger.addHandler(handler)
                logger.setLevel(logging.DEBUG)
                _state["handler"] = handler
            except Exception:
                # Disk full / read-only dir / etc. — degrade to no-op.
                _state["enabled"] = False
                null = logging.NullHandler()
                setattr(null, "_autotube_obs_handler", True)
                logger.addHandler(null)
    except Exception:  # pragma: no cover - final safety net
        pass
    return logger


# ── Emisión de eventos ───────────────────────────────────────────────
def _required_verbosity(event: str, fields: Dict[str, Any]) -> int:
    try:
        ev = (event or "").lower()
        if ("prompt" in ev or "response" in ev or "completion" in ev) and any(
            k.lower() in _LLM_TEXT_KEYS for k in fields
        ):
            return 3  # trace
        if "scene_idx" in fields or ev.startswith("scene") or ev.endswith("_scene"):
            return 2  # detail
    except Exception:  # pragma: no cover - defensive
        pass
    return 1  # summary


def _should_emit(event: str, fields: Dict[str, Any]) -> bool:
    mode = _state.get("mode", "off")
    if mode == "off" or not _state.get("enabled"):
        return False
    if _MODE_VERBOSITY.get(mode, 0) < _required_verbosity(event, fields):
        return False
    # Scene-level sampling (only when rate < 1.0).
    rate = _state.get("sample_rate", 1.0)
    if rate >= 1.0:
        return True
    if rate <= 0.0:
        return "scene_idx" not in fields and not (event or "").startswith("scene")
    if "scene_idx" in fields or (event or "").startswith("scene"):
        return random.random() < rate
    return True


def _build_payload(event: str, level: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    mode = _state.get("mode", "detail")
    payload: Dict[str, Any] = {}
    try:
        for key, value in get_context().items():
            if value is not None:
                payload[key] = value
    except Exception:  # pragma: no cover - defensive
        pass
    try:
        payload.update(fields or {})
    except Exception:
        pass
    payload = _sanitize_llm_fields(payload, mode)
    payload = redact(payload)
    payload["event"] = event
    payload["level"] = str(level).lower()
    payload["ts"] = datetime.now(timezone.utc).isoformat()
    return payload


def obs_event(event: str, level: str = "info", **fields: Any) -> None:
    """Write ONE JSON event to ``logs/obs/obs.log``.

    Absolute fail-open: any error is swallowed and never propagates. When the
    logger is disabled/``off``, or the event's verbosity is below the active
    mode, it is a silent no-op.
    """
    try:
        if not event:
            return
        if not _state.get("enabled") or _state.get("mode") == "off":
            return
        if not _should_emit(event, fields):
            return
        payload = _build_payload(event, level, fields)
        logger = logging.getLogger(OBS_LOGGER_NAME)
        log_level = _LEVELS.get(str(level).lower(), logging.INFO)
        logger.log(log_level, event, extra={"obs_payload": payload})
    except Exception:
        # Never let observability break the pipeline.
        pass


__all__ = [
    "OBS_LOGGER_NAME",
    "ContextFilter",
    "set_context",
    "clear_context",
    "get_context",
    "bind_context",
    "obs_context_scope",
    "obs_event",
    "setup_obs_logging",
    "redact",
    "digest_text",
    "llm_text",
    "build_api_log_handler",
]
