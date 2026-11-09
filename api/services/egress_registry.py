"""Registro durable de canales con egress gestionado (fail-closed).

Fuentes de verdad
-----------------
1. **DB** ``channels.egress_managed``: marca durable. Si el JSON secreto se
   pierde, el canal SIGUE siendo gestionado y se bloquea (nunca cae a local).
2. **``config/egress_agents.json``** (gitignored): url + token + expected_ip.
   Es el único sitio donde vive el secreto del agente.

Reglas duras
------------
- ``managed = db_managed OR registry_entry``.
- Si un canal es gestionado y falta ``url``, ``token`` o ``expected_ip`` se
  lanza ``EgressConfigurationError``. Los callers NO deben caer al camino local.
- Un canal no gestionado devuelve ``None`` y conserva el comportamiento actual.

Este módulo no importa ``requests`` ni la DB a nivel de módulo: se puede usar
desde el servidor principal y desde tests sin efectos secundarios.
"""
from __future__ import annotations

import json
import logging
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Ruta del mapa secreto slug -> {url, token, expected_ip}. Gitignored.
_AGENTS_FILE = Path(__file__).resolve().parent.parent.parent / "config" / "egress_agents.json"

# Raíz del repo (para localizar credenciales locales que NO deben existir).
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class EgressError(RuntimeError):
    """Error base del subsistema de egress aislado."""


class EgressAgentUnavailableError(EgressError):
    """El agente de un canal gestionado no está accesible (fail-closed).

    Los callers NO deben reintentar cayendo al camino local directo; deben
    mantener el vídeo/job en espera hasta que el agente vuelva a estar activo.
    """


class EgressConfigurationError(EgressAgentUnavailableError):
    """El canal es gestionado pero su perfil está incompleto (fail-closed).

    Hereda de ``EgressAgentUnavailableError`` para que cualquier ``except``
    existente siga bloqueando la operación en lugar de continuar en local.
    """


@dataclass(frozen=True)
class ManagedProfile:
    """Perfil resuelto de un canal gestionado."""

    slug: str
    url: str
    token: str
    expected_ip: str
    mode: str = "gateway"
    label: str = ""
    status: str = "active"
    google_account: str = ""
    source: str = ""
    raw: dict = field(default_factory=dict)


def _load_agents() -> dict:
    """Lee el mapa secreto. Un JSON corrupto se registra como error crítico."""
    if not _AGENTS_FILE.exists():
        return {}
    try:
        data = json.loads(_AGENTS_FILE.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as exc:
        # No devolver silenciosamente {} sin dejar rastro: un canal gestionado
        # por DB quedará bloqueado por perfil incompleto, pero queremos la traza.
        logger.critical("egress_registry: no se pudo leer %s: %s", _AGENTS_FILE, exc)
        return {}


def _db_channel(slug: str) -> Optional[dict]:
    """Fila de ``channels`` para *slug* (o None). Aislado para poder testear."""
    try:
        from database.db_extended import ExtendedDatabase
        return ExtendedDatabase().get_channel_by_slug(slug)
    except Exception as exc:  # noqa: BLE001
        logger.warning("egress_registry: DB no disponible para %s: %s", slug, exc)
        return None


def _row_is_managed(row: Optional[dict]) -> bool:
    if not row:
        return False
    try:
        return int(row.get("egress_managed") or 0) == 1
    except (TypeError, ValueError):
        return False


def is_egress_managed(slug: str, db_row: Optional[dict] = None) -> bool:
    """True si el canal debe delegar su egress al agente.

    No lanza si el perfil está incompleto: devuelve True para que el caller
    aplique fail-closed. Usa ``get_profile``/``require_profile`` para validar.
    """
    if not slug:
        return False
    row = db_row if db_row is not None else _db_channel(slug)
    if _row_is_managed(row):
        return True
    return slug in _load_agents()


def get_profile(slug: str, db_row: Optional[dict] = None) -> Optional[ManagedProfile]:
    """Perfil resuelto, ``None`` si no es gestionado, o error si está incompleto."""
    if not slug:
        return None

    row = db_row if db_row is not None else _db_channel(slug)
    agents = _load_agents()
    cfg = agents.get(slug) or {}
    managed = _row_is_managed(row) or bool(cfg)
    if not managed:
        return None

    url = str(cfg.get("url") or "").rstrip("/")
    token = str(cfg.get("token") or "")
    expected = str(
        cfg.get("expected_ip")
        or (row or {}).get("egress_expected_ip")
        or ""
    ).strip()

    missing = []
    if not url:
        missing.append("url")
    if not token:
        missing.append("token")
    if not expected:
        missing.append("expected_ip")
    if missing:
        raise EgressConfigurationError(
            f"El canal '{slug}' es gestionado (DB o registro) pero le falta "
            f"{', '.join(missing)} en config/egress_agents.json. "
            f"Egress local BLOQUEADO (fail-closed). Completa el perfil del gateway."
        )

    return ManagedProfile(
        slug=slug,
        url=url,
        token=token,
        expected_ip=expected,
        mode=str((row or {}).get("egress_mode") or cfg.get("mode") or "gateway"),
        label=str((row or {}).get("egress_label") or cfg.get("egress_label") or ""),
        status=str((row or {}).get("egress_status") or "active"),
        google_account=str((row or {}).get("google_account") or cfg.get("google_account") or ""),
        source="db" if _row_is_managed(row) else "registry",
        raw=cfg,
    )


def require_profile(slug: str, db_row: Optional[dict] = None) -> Optional[ManagedProfile]:
    """Como ``get_profile`` pero deja explícito el contrato fail-closed."""
    return get_profile(slug, db_row=db_row)


def get_managed_slugs(db=None) -> set[str]:
    """Unión de canales gestionados en DB y en el registro secreto."""
    slugs: set[str] = set(_load_agents().keys())
    if db is None:
        try:
            from database.db_extended import ExtendedDatabase
            db = ExtendedDatabase()
        except Exception as exc:  # noqa: BLE001
            logger.warning("egress_registry: no se pudo abrir DB para listar: %s", exc)
            return slugs
    try:
        with db._connect() as conn:
            rows = conn.execute(
                "SELECT slug FROM channels WHERE egress_managed = 1"
            ).fetchall()
            for r in rows:
                try:
                    slug = r["slug"]
                except (TypeError, IndexError, KeyError):
                    slug = r[0] if r else None
                if slug:
                    slugs.add(str(slug))
    except Exception as exc:  # noqa: BLE001
        logger.debug("egress_registry: no se pudo listar egress_managed: %s", exc)
    return slugs


def find_local_credentials(slug: str, profile: Optional[ManagedProfile] = None) -> list[str]:
    """Rutas de credenciales de un canal gestionado que NO deben estar en el server.

    Devuelve una lista (vacía = correcto). No lanza.
    """
    found: list[str] = []
    tokens_dir = PROJECT_ROOT / "tokens"
    candidates = [
        tokens_dir / f"{slug}.pickle",
        PROJECT_ROOT / "config" / f"client_secret_{slug}.json",
    ]
    account = (profile.google_account if profile else "") or slug
    for acc in {account, slug}:
        if not acc:
            continue
        candidates.append(tokens_dir / f"{acc}_browser_profile")
        candidates.append(tokens_dir / f"{acc}_browser_session.json")
    for path in candidates:
        try:
            if path.exists():
                found.append(str(path))
        except OSError:
            continue
    return found


def reconcile_on_startup(db=None) -> dict:
    """Autochequeo de arranque: perfiles completos y sin credenciales locales.

    - Marca ``channels.egress_status='error'`` si el perfil está incompleto.
    - Registra ``system_state['egress_config_error_<slug>']`` y una alerta.
    - Nunca desactiva el canal automáticamente: el fail-closed de las
      operaciones ya lo bloquea; el operador decide.
    """
    if db is None:
        try:
            from database.db_extended import ExtendedDatabase
            db = ExtendedDatabase()
        except Exception as exc:  # noqa: BLE001
            logger.warning("egress_registry: reconcile sin DB: %s", exc)
            return {"managed": [], "errors": {}, "warnings": []}

    managed = sorted(get_managed_slugs(db))
    errors: dict[str, str] = {}
    warnings: list[str] = []

    for slug in managed:
        try:
            profile = require_profile(slug)
        except EgressConfigurationError as exc:
            errors[slug] = str(exc)
            _mark_error(db, slug, str(exc))
            continue
        if profile is None:
            continue
        local = find_local_credentials(slug, profile)
        if local:
            msg = (
                f"El canal gestionado '{slug}' tiene credenciales locales en el "
                f"servidor principal: {', '.join(local)}. Deben vivir solo en el "
                f"gateway. Egress local bloqueado (fail-closed)."
            )
            errors[slug] = msg
            _mark_error(db, slug, msg)
            continue
        try:
            db.set_system_state(f"egress_config_error_{slug}", "0")
        except Exception:  # noqa: BLE001
            pass

    # Slugs del registro sin canal en DB: aviso (config obsoleta).
    try:
        with db._connect() as conn:
            rows = conn.execute("SELECT slug FROM channels").fetchall()
            known = {str(r["slug"]) for r in rows if r and r["slug"]}
        for slug in sorted(set(_load_agents().keys()) - known):
            warnings.append(f"egress_agents.json contiene '{slug}' sin canal en DB")
    except Exception:  # noqa: BLE001
        pass

    result = {"managed": managed, "errors": errors, "warnings": warnings}
    if errors:
        logger.critical("egress_registry: %d canal(es) con error de configuración: %s",
                        len(errors), sorted(errors))
    else:
        logger.info("egress_registry: %d canal(es) gestionado(s) OK", len(managed))
    return result


def _mark_error(db, slug: str, message: str) -> None:
    try:
        with db._connect() as conn:
            conn.execute(
                "UPDATE channels SET egress_status = 'error', updated_at = CURRENT_TIMESTAMP "
                "WHERE slug = ?",
                (slug,),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.debug("[%s] no se pudo marcar egress_status=error: %s", slug, exc)
    try:
        db.set_system_state(f"egress_config_error_{slug}", "1")
    except Exception:  # noqa: BLE001
        pass
    try:
        from api.services.lifecycle_monitor import create_alert
        create_alert(
            db, entity_type="system", entity_id=abs(zlib.crc32(str(slug).encode())) % (10 ** 6),
            channel_id=None, alert_type="egress_config_error", severity="critical",
            title=f"Egress mal configurado: {slug}",
            message=message,
            metadata={"slug": slug},
        )
    except Exception:  # noqa: BLE001
        pass
