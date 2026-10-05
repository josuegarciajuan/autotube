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

Alertas de error (independientes de ``OBS_LOG_*``)
--------------------------------------------------
``obs_alert()`` emite una alerta fail-open vía
``api.services.lifecycle_monitor.emit_alert``.
``obs_error()`` escribe un evento JSONL de nivel ``error`` Y crea/actualiza una
alerta crítica agregada por ``(entity_type, entity_id, alert_type)``.
``CriticalErrorAlertHandler`` reenvía records ``ERROR``+ (de todo el sistema) a
una cola acotada con hilo daemon, agregando por tipo/entidad con contador y
aplicando cooldown in-process.
``setup_error_alerts()`` instala ese handler en el root logger (idempotente) y
``install_exception_hooks()`` captura excepciones no tratadas
(``sys``/``threading``/asyncio). ``OBS_LOG_ENABLED=False`` NO silencia estas
alertas; el kill-switch total es ``OBS_ERROR_ALERTS_ENABLED=False``.
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
import queue
import random
import re
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

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

    Note: this does NOT install :class:`CriticalErrorAlertHandler`; error
    alerts are an independent mechanism (``setup_error_alerts()``) so that
    ``OBS_LOG_ENABLED=False`` / ``OBS_LOG_LEVEL="off"`` never silence errors.
    Callers should wire both explicitly.
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


# ═══════════════════════════════════════════════════════════════════
# Alertas de error agregadas (ERROR logs + excepciones no capturadas)
# ═══════════════════════════════════════════════════════════════════
#
# Objetivo: cualquier record de nivel ERROR (o superior) y cualquier
# excepción no capturada se convierte en UNA alerta CRÍTICA agregada por
# ``(entity_type, entity_id, alert_type)`` con contador y metadata, en vez de
# una alerta por incidente. Fail-open absoluto: nunca lanza, nunca bloquea al
# productor (cola acotada + hilo daemon) y nunca satura la DB (rate-limit
# in-process por clave + dedup de ``emit_alert``).
#
# Independiente de ``OBS_LOG_ENABLED`` / ``OBS_LOG_LEVEL``: apagar el detalle
# fino de observabilidad NO silencia los errores. El kill-switch total es
# ``OBS_ERROR_ALERTS_ENABLED=False``.

_ERROR_ALERT_LOGGER_PREFIX = "autotube.lifecycle"
_IGNORED_ALERT_LOGGERS = frozenset({
    "autotube.lifecycle",
    "autotube.obs",
    "pipeline.observability",
})

# Texto: enmascara pares ``clave=valor`` sensibles y cabeceras Bearer.
_SECRET_TEXT_RE = re.compile(
    r"(?i)(bearer\s+)[A-Za-z0-9._\-]+"
    r"|((?:access[_-]?token|api[_-]?key|apikey|token|secret|password|passwd"
    r"|cookie|authorization)\s*[=:]\s*)\S+"
)
_SLUG_RE = re.compile(r"[^a-z0-9]+", re.IGNORECASE)

_tls = threading.local()

# Estado del handler de errores (idempotencia de setup).
_error_state: Dict[str, Any] = {
    "configured": False,
    "enabled": False,
    "handler": None,
}


def _utc_iso() -> str:
    try:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
    except Exception:  # pragma: no cover - defensive
        return ""


def _slugify(value: Any, max_len: int = 80) -> str:
    """Sanitise *value* into a stable ``[a-z0-9_]`` slug (never raises)."""
    try:
        text = _SLUG_RE.sub("_", str(value or "")).strip("_").lower()
        return (text or "unknown")[: max(1, int(max_len))]
    except Exception:  # pragma: no cover - defensive
        return "unknown"


def _redact_text(text: Any, max_len: int = 500) -> str:
    """Redact sensitive ``key=value`` / Bearer patterns and cap the length."""
    try:
        out = str(text)
        out = _SECRET_TEXT_RE.sub(
            lambda m: (m.group(1) or m.group(2) or "") + _REDACTION_MARK
            if (m.group(1) or m.group(2))
            else _REDACTION_MARK,
            out,
        )
        if max_len and len(out) > max_len:
            out = out[:max_len] + "...<truncated>"
        return out
    except Exception:  # pragma: no cover - defensive
        return "<unredactable>"


def _to_int(value: Any) -> Optional[int]:
    try:
        if isinstance(value, bool) or value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_level(value: Any, default: int = logging.ERROR) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return _LEVELS.get(value.strip().lower(), default)
    return default


def _as_str_list(value: Any) -> List[str]:
    """Coerce a value to a list of lowercased, non-empty strings."""
    try:
        if value is None:
            return []
        if isinstance(value, str):
            parts = value.replace(";", ",").split(",")
            return [p.strip().lower() for p in parts if p.strip()]
        if isinstance(value, (list, tuple, set, frozenset)):
            out = []
            for item in value:
                text = str(item).strip().lower()
                if text:
                    out.append(text)
            return out
        text = str(value).strip().lower()
        return [text] if text else []
    except Exception:  # pragma: no cover - defensive
        return []


def _load_error_alert_settings() -> Dict[str, Any]:
    """Resolve error-alert settings (env > config.settings > default).

    Never raises; every malformed value falls back to a safe default.
    """
    try:
        enabled = _as_bool(
            _read_env_or_setting("OBS_ERROR_ALERTS_ENABLED", True), True
        )
    except Exception:  # pragma: no cover - defensive
        enabled = True
    try:
        min_level = _as_level(
            _read_env_or_setting("OBS_ERROR_ALERTS_MIN_LEVEL", "error"),
            logging.ERROR,
        )
    except Exception:  # pragma: no cover - defensive
        min_level = logging.ERROR
    try:
        cooldown = max(
            0,
            _as_int(_read_env_or_setting("OBS_ERROR_ALERT_COOLDOWN_MIN", 30), 30),
        )
    except Exception:  # pragma: no cover - defensive
        cooldown = 30
    try:
        ignore = _as_str_list(
            _read_env_or_setting("OBS_ERROR_ALERT_IGNORE", [])
        )
    except Exception:  # pragma: no cover - defensive
        ignore = []
    try:
        queue_size = max(
            1,
            _as_int(_read_env_or_setting("OBS_ERROR_ALERT_QUEUE_SIZE", 500), 500),
        )
    except Exception:  # pragma: no cover - defensive
        queue_size = 500
    return {
        "enabled": enabled,
        "min_level": min_level,
        "cooldown_min": cooldown,
        "ignore": ignore,
        "queue_size": queue_size,
    }


def _is_ignored(logger_name: Any, message: Any) -> bool:
    patterns = _load_error_alert_settings().get("ignore") or []
    if not patterns:
        return False
    haystack = (str(logger_name or "") + " " + str(message or "")).lower()
    return any(p in haystack for p in patterns)


def _entity_from_context(ctx: Dict[str, Any]):
    """Return ``(entity_type, entity_id, channel_id)`` inferred from context."""
    try:
        video_id = (ctx or {}).get("video_id")
        channel_id = _to_int((ctx or {}).get("channel"))
        if video_id not in (None, ""):
            return "video", _to_int(video_id), channel_id
    except Exception:  # pragma: no cover - defensive
        pass
    return "system", None, None


def _context_subset(ctx: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    try:
        for key in _CONTEXT_KEYS:
            value = (ctx or {}).get(key)
            if value not in (None, ""):
                out[key] = value
    except Exception:  # pragma: no cover - defensive
        pass
    return out


def obs_alert(
    alert_type: str,
    *,
    severity: str = "critical",
    title: str,
    message: Optional[str] = None,
    metadata: Optional[dict] = None,
    entity_type: str = "system",
    entity_id: Optional[int] = None,
    channel_id: Optional[int] = None,
) -> None:
    """Emit one alert via ``lifecycle_monitor.emit_alert`` — absolute fail-open.

    The import is lazy (inside the function) to avoid an import cycle between
    ``pipeline.observability`` and the API services. Any failure (DB down,
    import error, handler bug) is swallowed: this function NEVER raises and
    never lets alerting break the pipeline or an API request. ``emit_alert``
    already journals to ``logs/alerts_fallback.log`` when the DB is down.

    Also sets a per-thread re-entrancy flag so that any ERROR log produced
    while emitting the alert is not captured again by
    :class:`CriticalErrorAlertHandler` (anti-recursion).
    """
    try:
        prev = getattr(_tls, "active", False)
        _tls.active = True
        try:
            from api.services.lifecycle_monitor import emit_alert
            emit_alert(
                entity_type=entity_type,
                entity_id=entity_id,
                channel_id=channel_id,
                alert_type=alert_type,
                severity=severity,
                title=title,
                message=message,
                metadata=metadata,
            )
        finally:
            _tls.active = prev
    except Exception:
        pass


class _ErrorAlertSink:
    """In-process aggregator: one alert per key with count + rate-limit.

    Key = ``(entity_type, entity_id, alert_type)``. The first event of a key
    emits immediately; subsequent events inside ``OBS_ERROR_ALERT_COOLDOWN_MIN``
    only accumulate the counter (the next write refreshes the DB row with the
    updated ``count``/metadata via ``emit_alert``'s dedup update).
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: Dict[tuple, Dict[str, Any]] = {}

    def reset(self) -> None:
        try:
            with self._lock:
                self._states.clear()
        except Exception:  # pragma: no cover - defensive
            pass

    def submit(
        self,
        *,
        alert_type: str,
        title: str,
        message: Optional[str],
        severity: str,
        entity_type: str,
        entity_id: Optional[int],
        channel_id: Optional[int],
        logger_name: str = "",
        level_name: str = "",
        context: Optional[dict] = None,
        extra_metadata: Optional[dict] = None,
        now: Optional[float] = None,
    ) -> bool:
        """Aggregate one occurrence. Returns True if it emitted to the DB."""
        try:
            cfg = _load_error_alert_settings()
            timestamp = time.monotonic() if now is None else now
            key = (entity_type, entity_id, alert_type)
            with self._lock:
                state = self._states.get(key)
                if state is None:
                    state = {
                        "count": 0,
                        "first_seen": _utc_iso(),
                        "last_seen": "",
                        "sample_message": "",
                        "logger": "",
                        "level": "",
                        "context": {},
                        "extra": {},
                        "last_emit": None,
                    }
                    self._states[key] = state
                state["count"] += 1
                state["last_seen"] = _utc_iso()
                if message:
                    state["sample_message"] = _redact_text(message, 500)
                if logger_name:
                    state["logger"] = str(logger_name)
                if level_name:
                    state["level"] = str(level_name)
                if context:
                    state["context"] = redact(dict(context))
                if extra_metadata:
                    state["extra"] = redact(dict(extra_metadata))

                cooldown_sec = float(cfg["cooldown_min"]) * 60.0
                last_emit = state.get("last_emit")
                if last_emit is not None and (timestamp - last_emit) < cooldown_sec:
                    return False
                state["last_emit"] = timestamp

                count = state["count"]
                metadata = dict(state.get("extra") or {})
                metadata.update({
                    "count": count,
                    "first_seen": state["first_seen"],
                    "last_seen": state["last_seen"],
                    "logger": state["logger"],
                    "level": state["level"],
                    "sample_message": state["sample_message"],
                    "context": dict(state["context"]),
                })
                alert_message = message
                if not alert_message:
                    alert_message = state.get("sample_message")
                aggregated_message = f"[count={count}] {alert_message}" if alert_message else f"[count={count}]"

            obs_alert(
                alert_type,
                severity=severity,
                title=title,
                message=aggregated_message,
                metadata=metadata,
                entity_type=entity_type,
                entity_id=entity_id,
                channel_id=channel_id,
            )
            return True
        except Exception:
            return False


_error_sink = _ErrorAlertSink()


def _submit_error_alert(
    *,
    alert_type: str,
    title: str,
    message: Optional[str],
    logger_name: str = "",
    level_name: str = "error",
    context: Optional[dict] = None,
    entity_type: str = "system",
    entity_id: Optional[int] = None,
    channel_id: Optional[int] = None,
    extra_metadata: Optional[dict] = None,
    severity: str = "critical",
    now: Optional[float] = None,
) -> bool:
    """Shared entry point for the handler and :func:`obs_error`."""
    if not _load_error_alert_settings().get("enabled"):
        return False
    return _error_sink.submit(
        alert_type=alert_type,
        title=title,
        message=message,
        severity=severity,
        entity_type=entity_type,
        entity_id=entity_id,
        channel_id=channel_id,
        logger_name=logger_name,
        level_name=level_name,
        context=context,
        extra_metadata=extra_metadata,
        now=now,
    )


def obs_error(
    event: str,
    *,
    title: Optional[str] = None,
    message: Optional[str] = None,
    metadata: Optional[dict] = None,
    entity_type: str = "system",
    entity_id: Optional[int] = None,
    channel_id: Optional[int] = None,
    **fields: Any,
) -> None:
    """Write a JSONL ``error`` event AND create/update an aggregated alert.

    Always fail-open. The correlation context (``channel``/``video_id``/
    ``job_id``/``phase``/``scene_idx``) is used to infer the alert entity. An
    explicit ``alert_type`` may be passed through ``fields``; otherwise
    ``"error_" + slug(event)`` is used.
    """
    try:
        obs_event(event, level="error", **fields)
    except Exception:
        pass
    try:
        explicit_alert_type = fields.get("alert_type")
        alert_type = (
            str(explicit_alert_type)
            if explicit_alert_type
            else "error_" + _slugify(event)
        )
        ctx = get_context()
        inferred_type, inferred_id, inferred_channel = _entity_from_context(ctx)
        if entity_id is None and inferred_id is not None:
            entity_type, entity_id = inferred_type, inferred_id
        if channel_id is None and inferred_channel is not None:
            channel_id = inferred_channel
        context = _context_subset(ctx)
        raw_message = message if message is not None else event
        redacted_message = _redact_text(raw_message, 500)
        if _is_ignored(event, redacted_message):
            return
        _submit_error_alert(
            alert_type=alert_type,
            title=title or f"Error: {event}",
            message=redacted_message,
            logger_name=str(event),
            level_name="error",
            context=context,
            entity_type=entity_type,
            entity_id=entity_id,
            channel_id=channel_id,
            extra_metadata=metadata,
        )
    except Exception:
        pass


class CriticalErrorAlertHandler(logging.Handler):
    """Forward ``ERROR``+ records to a bounded background queue.

    A daemon worker thread aggregates by ``(entity_type, entity_id,
    alert_type)`` and emits ONE critical alert with a counter. The producer
    never blocks: if the bounded queue is full the record is dropped and
    counted (``dropped``). Re-entrant per-thread flag prevents recursion if
    ``emit_alert`` itself logs an error.
    """

    def __init__(
        self,
        level: Optional[int] = None,
        queue_size: Optional[int] = None,
    ) -> None:
        cfg = _load_error_alert_settings()
        super().__init__(level if level is not None else int(cfg["min_level"]))
        size = int(queue_size if queue_size is not None else cfg["queue_size"])
        self._queue: "queue.Queue" = queue.Queue(maxsize=max(1, size))
        self._dropped = 0
        self._drop_lock = threading.Lock()
        self._stop = threading.Event()
        self._worker = threading.Thread(
            target=self._run, name="autotube-error-alerts", daemon=True,
        )
        self._worker.start()
        try:
            setattr(self, "_autotube_error_alert_handler", True)
        except Exception:  # pragma: no cover - defensive
            pass

    @property
    def dropped(self) -> int:
        try:
            with self._drop_lock:
                return self._dropped
        except Exception:  # pragma: no cover - defensive
            return 0

    def emit(self, record: logging.LogRecord) -> None:  # noqa: A003
        try:
            if record.levelno < self.level:
                return
            # Anti-recursion: never capture logs produced while we emit alerts.
            if getattr(_tls, "active", False):
                return
            name = getattr(record, "name", "") or ""
            if name in _IGNORED_ALERT_LOGGERS or name.startswith(
                _ERROR_ALERT_LOGGER_PREFIX
            ):
                return
            try:
                raw_message = record.getMessage()
            except Exception:
                raw_message = str(getattr(record, "msg", ""))
            if _is_ignored(name, raw_message):
                return
            ctx = get_context()
            try:
                self._queue.put_nowait((record, ctx, time.monotonic()))
            except queue.Full:
                with self._drop_lock:
                    self._dropped += 1
        except Exception:
            # A broken handler must never break logging or the pipeline.
            pass

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                break
            _tls.active = True
            try:
                if isinstance(item, tuple) and len(item) == 3:
                    self._process(*item)
            except Exception:
                pass
            finally:
                _tls.active = False

    def _process(
        self,
        record: logging.LogRecord,
        ctx: Dict[str, Any],
        now: float,
    ) -> None:
        logger_name = getattr(record, "name", "") or ""
        explicit = getattr(record, "alert_type", None)
        alert_type = (
            str(explicit) if explicit else "error_" + _slugify(logger_name)
        )
        try:
            raw_message = record.getMessage()
        except Exception:
            raw_message = str(getattr(record, "msg", ""))
        redacted_message = _redact_text(raw_message, 500)
        explicit_title = getattr(record, "alert_title", None)
        title = str(explicit_title) if explicit_title else (
            f"Errores de {logger_name or 'sistema'}"
        )
        entity_type, entity_id, channel_id = _entity_from_context(ctx)
        level_name = str(getattr(record, "levelname", "error")).lower()
        _submit_error_alert(
            alert_type=alert_type,
            title=title,
            message=redacted_message,
            logger_name=logger_name,
            level_name=level_name,
            context=_context_subset(ctx),
            entity_type=entity_type,
            entity_id=entity_id,
            channel_id=channel_id,
            now=now,
        )

    def close(self) -> None:
        try:
            self._stop.set()
            try:
                self._queue.put_nowait(None)
            except Exception:
                pass
            if self._worker.is_alive():
                self._worker.join(timeout=1.0)
        except Exception:
            pass
        finally:
            try:
                super().close()
            except Exception:  # pragma: no cover - defensive
                pass


def setup_error_alerts(force: bool = False) -> Optional[CriticalErrorAlertHandler]:
    """Install :class:`CriticalErrorAlertHandler` on the root logger.

    Idempotent. Installs only when ``OBS_ERROR_ALERTS_ENABLED`` is True;
    independent from ``setup_obs_logging`` (disabling the fine-grained obs
    detail must NOT silence error alerts). Returns the installed handler (or
    the existing one), or ``None`` when disabled.

    ``setup_obs_logging`` intentionally does NOT call this automatically so the
    two switches stay independent; callers wire both explicitly.
    """
    try:
        with _lock:
            cfg = _load_error_alert_settings()
            root = logging.getLogger()
            if _error_state.get("configured") and not force:
                return _error_state.get("handler")
            for existing in list(root.handlers):
                if getattr(existing, "_autotube_error_alert_handler", False):
                    try:
                        root.removeHandler(existing)
                        existing.close()
                    except Exception:
                        pass
            _error_state.update({
                "configured": True,
                "enabled": bool(cfg["enabled"]),
                "handler": None,
            })
            if not cfg["enabled"]:
                return None
            try:
                handler = CriticalErrorAlertHandler(
                    level=int(cfg["min_level"]),
                    queue_size=int(cfg["queue_size"]),
                )
                root.addHandler(handler)
                _error_state["handler"] = handler
                return handler
            except Exception:
                _error_state["enabled"] = False
                return None
    except Exception:
        return None


# ── Captura de excepciones no tratadas ───────────────────────────────

_exc_hooks_lock = threading.RLock()
_exc_hooks_installed = False
_exc_hooks_labels: List[str] = []
_ORIGINAL_SYS_EXCEPTHOOK = None
_ORIGINAL_THREAD_EXCEPTHOOK = None


def _bounded_traceback(
    exc_type: Any,
    exc_value: Any,
    exc_tb: Any,
    max_chars: int = 1500,
) -> str:
    try:
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    except Exception:
        try:
            text = f"{exc_type}: {exc_value}"
        except Exception:  # pragma: no cover - defensive
            text = "<unprintable exception>"
    return _redact_text(text, max_chars)


def _emit_uncaught(label: str, exc_type: Any, exc_value: Any, exc_tb: Any) -> None:
    try:
        obs_error(
            "uncaught_exception",
            title=f"Excepción no capturada ({label})",
            message=_bounded_traceback(exc_type, exc_value, exc_tb),
            metadata={
                "process": label,
                "exception_type": getattr(exc_type, "__name__", str(exc_type)),
            },
            alert_type="uncaught_exception_" + _slugify(label, 40),
        )
    except Exception:
        pass


def install_exception_hooks(process_label: str) -> None:
    """Install fail-open hooks for uncaught exceptions (idempotent).

    Covers ``sys.excepthook``, ``threading.excepthook`` (when available) and the
    running asyncio event loop's exception handler (best-effort). Each
    uncaught exception emits a critical aggregated alert of type
    ``uncaught_exception_<process_label>`` with a bounded, redacted traceback.

    Safe to call multiple times and from multiple call sites: the hooks are
    installed only once per process.
    """
    global _exc_hooks_installed, _ORIGINAL_SYS_EXCEPTHOOK
    global _ORIGINAL_THREAD_EXCEPTHOOK
    label = _slugify(process_label or "process", 40)
    try:
        with _exc_hooks_lock:
            if label not in _exc_hooks_labels:
                _exc_hooks_labels.append(label)
            if _exc_hooks_installed:
                return

            # sys.excepthook
            try:
                _ORIGINAL_SYS_EXCEPTHOOK = sys.excepthook

                def _sys_hook(exc_type, exc_value, exc_tb):
                    try:
                        _emit_uncaught(label, exc_type, exc_value, exc_tb)
                    except Exception:
                        pass
                    try:
                        prev = _ORIGINAL_SYS_EXCEPTHOOK
                        if prev is not None and prev is not _sys_hook:
                            prev(exc_type, exc_value, exc_tb)
                    except Exception:
                        pass

                sys.excepthook = _sys_hook
            except Exception:
                pass

            # threading.excepthook (Python 3.8+)
            try:
                if hasattr(threading, "excepthook"):
                    _ORIGINAL_THREAD_EXCEPTHOOK = threading.excepthook

                    def _thread_hook(args):
                        try:
                            _emit_uncaught(
                                label,
                                getattr(args, "exc_type", None),
                                getattr(args, "exc_value", None),
                                getattr(args, "exc_traceback", None),
                            )
                        except Exception:
                            pass
                        try:
                            prev = _ORIGINAL_THREAD_EXCEPTHOOK
                            if prev is not None and prev is not _thread_hook:
                                prev(args)
                        except Exception:
                            pass

                    threading.excepthook = _thread_hook
            except Exception:
                pass

            # asyncio event loop (only when a loop is already running).
            try:
                import asyncio

                loop = asyncio.get_running_loop()
                previous_handler = loop.get_exception_handler()

                def _loop_handler(active_loop, context):
                    try:
                        exc = (context or {}).get("exception")
                        if exc is not None:
                            _emit_uncaught(
                                label, type(exc), exc, getattr(exc, "__traceback__", None)
                            )
                        else:
                            obs_error(
                                "uncaught_exception",
                                title=f"Excepción no capturada ({label})",
                                message=_redact_text(
                                    (context or {}).get("message", ""), 500
                                ),
                                metadata={"process": label},
                                alert_type="uncaught_exception_" + label,
                            )
                    except Exception:
                        pass
                    try:
                        if previous_handler is not None:
                            previous_handler(active_loop, context)
                        else:
                            active_loop.default_exception_handler(context)
                    except Exception:
                        pass

                loop.set_exception_handler(_loop_handler)
            except Exception:
                pass

            _exc_hooks_installed = True
    except Exception:
        pass


def _reset_error_alert_state() -> None:
    """Test helper: clear aggregation state and remove our root handlers."""
    try:
        with _lock:
            _error_state.update({
                "configured": False, "enabled": False, "handler": None,
            })
            _error_sink.reset()
        root = logging.getLogger()
        for handler in list(root.handlers):
            if getattr(handler, "_autotube_error_alert_handler", False):
                try:
                    root.removeHandler(handler)
                    handler.close()
                except Exception:
                    pass
    except Exception:
        pass


def _reset_exception_hooks_for_tests() -> None:
    """Test helper: restore original exception hooks and allow reinstall."""
    global _exc_hooks_installed
    try:
        with _exc_hooks_lock:
            if _ORIGINAL_SYS_EXCEPTHOOK is not None:
                try:
                    sys.excepthook = _ORIGINAL_SYS_EXCEPTHOOK
                except Exception:
                    pass
            try:
                if (
                    hasattr(threading, "excepthook")
                    and _ORIGINAL_THREAD_EXCEPTHOOK is not None
                ):
                    threading.excepthook = _ORIGINAL_THREAD_EXCEPTHOOK
            except Exception:
                pass
            _exc_hooks_installed = False
            _exc_hooks_labels.clear()
    except Exception:
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
    "obs_alert",
    "obs_error",
    "CriticalErrorAlertHandler",
    "setup_error_alerts",
    "install_exception_hooks",
]
