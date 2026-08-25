"""Reanudación gradual post-strike (antiban, ago 2026).

Programa la frecuencia de publicación respetando fases por canal según su
fecha de desbloqueo:

  Fase 1 (días 0-5 desde el desbloqueo, SOLO canales con strike propio):
      1 long-form cada 2 días (alternate_pattern=[1,0]) + 1 short nativo/día.
      Clips desactivados. → ~3 long-form/semana + 7 shorts/semana máx.
  Fase 2 (días 6+ / hermanos sin strike / permanente):
      1 long-form/día + 1 short nativo/día (techo antiban permanente).

Reglas:
  - Solo AVANZA fases (nunca retrocede) y respeta el techo antiban
    (videos_per_day<=1, shorts_native<=1, clips=0).
  - Canales aún bloqueados se saltan (fase 0) hasta que expire su bloqueo.
  - Canales sin strike pero con hermano de cuenta bloqueado (ej. canal2 →
    canal3) esperan a que el hermano lleve SIBLING_BUFFER_DAYS días estable.
  - Idempotente: se llama en cada arranque de la API (ensure_spam_holds,
    ANTES del planning engine) y/o a diario vía scripts/gradual_resume.py.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone, date

logger = logging.getLogger("autotube.gradual_resume")

# ── Constantes del plan (antiban) ────────────────────────────────
PHASE1_DAYS = 5            # duración de "1 long cada 2 días"
SIBLING_BUFFER_DAYS = 3    # hermano sin strike: +3 días de estabilidad del hermano
PHASE1_SHORTS_NATIVE_PER_DAY = 1
PHASE2_SHORTS_NATIVE_PER_DAY = 2
SHORTS_CLIPS_PER_LONG = 0  # clips desactivados
VIDEOS_PER_DAY_MAX = 1     # techo long-form

_PLAN_KEY = "resume_plan_{channel_id}"
_PHASE_KEY = "resume_phase_{channel_id}"
_POLICY_KEY = "channel_delivery_policy_{channel_id}"


def get_explicit_delivery_policy(channel_id: int, db) -> dict | None:
    """Política explícita de entrega por canal (aprobada por el operador).

    Formato:
        {"mode": "explicit", "longs_per_day": 1, "native_shorts_per_day": 2,
         "shorts_enabled": true, "clips_enabled": false}

    Es la fuente autoritativa: gana sobre las fases automáticas derivadas de
    fechas (gradual_resume). Devuelve None si no hay política explícita.
    """
    try:
        raw = db.get_system_state(_POLICY_KEY.format(channel_id=channel_id))
        if not raw:
            return None
        data = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(data, dict) or data.get("mode") != "explicit":
            return None
        return {
            "mode": "explicit",
            "longs_per_day": max(0, int(data.get("longs_per_day", 1) or 0)),
            "native_shorts_per_day": max(0, int(data.get("native_shorts_per_day", 1) or 0)),
            "shorts_enabled": bool(data.get("shorts_enabled", True)),
            "clips_enabled": bool(data.get("clips_enabled", False)),
        }
    except Exception:
        return None


def effective_delivery_policy(channel_id: int, db) -> dict | None:
    """Política efectiva de entrega: la explícita si existe, si no None."""
    return get_explicit_delivery_policy(channel_id, db)


def _iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _get_blocked_until(db, cid: int):
    raw = db.get_system_state(f"shorts_spam_blocked_until_{cid}")
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _sibling_blocked(db, ch: dict, now_ts: float):
    """Si el canal no tiene strike propio pero un hermano de su cuenta SÍ,
    devuelve (blocked_until_hermano, slug_hermano)."""
    try:
        from api.services.spam_mitigation import _project_sibling_channels
        siblings = _project_sibling_channels(ch.get("slug", ""), db) or []
    except Exception:
        return None
    cid = int(ch.get("id") or 0)
    for sib in siblings:
        sid = int(sib.get("id") or 0)
        if not sid or sid == cid:
            continue
        b = _get_blocked_until(db, sid)
        if b and b > now_ts:
            return (b, sib.get("slug", ""))
    return None


def build_plan(db, persist: bool = True) -> dict:
    """Construye el plan de reanudación desde el estado actual de bloques."""
    plan: dict[int, dict] = {}
    now_ts = time.time()
    for ch in (db.get_channels(active_only=True) or []):
        cid = int(ch.get("id") or 0)
        slug = ch.get("slug", "")
        if not cid or not slug:
            continue
        entry = None
        b = _get_blocked_until(db, cid)
        if b and b > now_ts:
            entry = {"start_iso": _iso_utc(b), "source": "unblock", "slug": slug}
        else:
            # ¿Tuvo strike PROPIO alguna vez (contador > 0)? → rampa fase 1 desde
            # SU desbloqueo, aunque la fecha ya haya pasado (ej. canal5).
            try:
                own_strikes = int(db.get_system_state(f"shorts_spam_strikes_{cid}") or 0)
            except (TypeError, ValueError):
                own_strikes = 0
            if own_strikes > 0 and b:
                entry = {"start_iso": _iso_utc(b), "source": "unblock", "slug": slug}
            else:
                sib = _sibling_blocked(db, ch, now_ts)
                if sib:
                    sib_until, sib_slug = sib
                    entry = {
                        "start_iso": _iso_utc(sib_until + SIBLING_BUFFER_DAYS * 86400),
                        "source": "sibling",
                        "sibling_of": sib_slug,
                        "slug": slug,
                    }
                else:
                    entry = {"start_iso": _iso_utc(now_ts), "source": "permanent", "slug": slug}
        plan[cid] = entry
        if persist:
            try:
                db.set_system_state(_PLAN_KEY.format(channel_id=cid), json.dumps(entry))
            except Exception as exc:
                logger.warning("plan persist failed for %s: %s", slug, exc)
    return plan


def load_plan(db) -> dict:
    plan: dict[int, dict] = {}
    for ch in (db.get_channels(active_only=True) or []):
        cid = int(ch.get("id") or 0)
        if not cid:
            continue
        raw = db.get_system_state(_PLAN_KEY.format(channel_id=cid))
        if raw:
            try:
                plan[cid] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                pass
    return plan


def _phase_for(entry: dict, today: date) -> int:
    try:
        start = datetime.fromisoformat(entry["start_iso"]).date()
    except (ValueError, TypeError, KeyError):
        return 0
    days = (today - start).days
    if days < 0:
        return 0  # aún bloqueado / no iniciado
    if entry.get("source") != "unblock":
        return 2  # hermano o permanente: directo a 1/día
    if days <= PHASE1_DAYS:
        return 1
    return 2


def effective_native_shorts_per_day(channel_id: int, db, today: date | None = None) -> int:
    """Cap de shorts nativos/día aprobado para un canal.

    Precedencia: política explícita > fase automática de reanudación.
    """
    policy = get_explicit_delivery_policy(channel_id, db)
    if policy is not None:
        return policy["native_shorts_per_day"]
    today = today or datetime.now(timezone.utc).date()
    try:
        raw = db.get_system_state(_PLAN_KEY.format(channel_id=channel_id))
        entry = json.loads(raw) if raw else None
        if not isinstance(entry, dict):
            return PHASE1_SHORTS_NATIVE_PER_DAY
        return (
            PHASE2_SHORTS_NATIVE_PER_DAY
            if _phase_for(entry, today) >= 2
            else PHASE1_SHORTS_NATIVE_PER_DAY
        )
    except Exception:
        return PHASE1_SHORTS_NATIVE_PER_DAY


def _spread_phase1_pending(db, cid: int, slug: str, start_dt: datetime) -> int:
    """Reprograma las publicaciones pendientes de un canal en Fase 1.

    Máximo 1 publicación por día de publicación (días 0, 2, 4 desde el
    desbloqueo; el exceso va a días consecutivos de Fase 2). Evita que un
    canal recién desbloqueado despache una ráfaga de N vídeos el mismo día
    (la señal de spam exacta que causó los strikes). DB siempre + YouTube
    nativo best-effort (50 ud/vídeo). Devuelve nº de vídeos reprogramados.
    """
    try:
        from api.services.spam_mitigation import _pending_publish_all
        pending = _pending_publish_all(cid, db) or []
    except Exception:
        return 0
    if len(pending) <= 1:
        return 0
    now = datetime.now(timezone.utc)
    phase1_days = [start_dt + timedelta(days=i) for i in range(0, PHASE1_DAYS + 1, 2)]  # 0, 2, 4
    spill_start = start_dt + timedelta(days=PHASE1_DAYS + 1)  # 6, 7, 8…
    uploader = None
    moved = 0
    for idx, v in enumerate(pending):
        target_day = phase1_days[idx] if idx < len(phase1_days) \
            else spill_start + timedelta(days=idx - len(phase1_days))
        try:
            orig_dt = datetime.fromisoformat(
                str(v.get("target_public_at", "")).replace("Z", "+00:00").replace(" ", "T")
            )
            hour, minute = orig_dt.hour, orig_dt.minute
        except (ValueError, TypeError):
            orig_dt = None
            hour, minute = 12, 0
        new_dt = target_day.replace(hour=hour, minute=minute, tzinfo=timezone.utc)
        if new_dt <= now:
            new_dt = target_day.replace(hour=12, minute=0, tzinfo=timezone.utc)
        # Ya está en el día correcto → no tocar (evita 50 ud/vídeo de
        # set_publish_at en cada arranque de la API).
        if orig_dt is not None:
            try:
                orig_utc = orig_dt.replace(tzinfo=timezone.utc) if orig_dt.tzinfo is None else orig_dt.astimezone(timezone.utc)
                if orig_utc == new_dt:
                    continue
            except (ValueError, TypeError):
                pass
        new_iso = new_dt.isoformat()
        # 1. DB (siempre).
        try:
            with db._connect() as conn:
                conn.execute(
                    "UPDATE videos SET target_public_at = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = ?",
                    (new_iso, v["id"]),
                )
                conn.commit()
        except Exception as exc:
            logger.warning("spread publish: DB update failed for video %s: %s", v["id"], exc)
            continue
        # 2. YouTube nativo (best-effort).
        yt_id = v.get("video_id") or v.get("yt_video_id")
        if yt_id:
            try:
                if uploader is None:
                    from pipeline.youtube_uploader import YouTubeUploader
                    uploader = YouTubeUploader(account_name=slug, channel_slug=slug)
                uploader.set_publish_at(yt_id, new_iso)
            except Exception as exc:
                logger.warning(
                    "spread publish: set_publish_at failed para %s (%s): %s",
                    yt_id, slug, exc,
                )
        moved += 1
    if moved:
        logger.warning(
            "⚠️ Gradual resume [%s]: %d publicación(es) pendiente(s) reprogramadas a "
            "Fase 1 (1 por día de publicación)",
            slug, moved,
        )
    return moved


def spread_pending_publishes(
    db,
    channel_id: int,
    slug: str,
    max_per_day: int = 1,
    start_dt: datetime | None = None,
    dry_run: bool = False,
) -> int:
    """Reprograma las publicaciones pendientes de un canal a máx ``max_per_day``/día.

    Generalización de ``_spread_phase1_pending`` para CUALQUIER canal (strike
    propio, hermano o sin strike): un backlog de vídeos ya subidos como private
    con publishAt a 3h de separación = 8 publicaciones/día del mismo canal, la
    ráfaga que alimenta los strikes de spam (ago 2026). Este esparcido reparte
    los pendientes a 1 por día natural (por defecto), conservando la hora
    original de cada vídeo, empezando por ``start_dt`` (default: hoy).

    Idempotente: si el vídeo ya está en el día/hora correctos se salta (evita
    50 ud/vídeo de set_publish_at en cada arranque de la API).

    Args:
        db: ExtendedDatabase.
        channel_id: ID del canal.
        slug: slug del canal (log + uploader).
        max_per_day: máx publicaciones por día natural (default 1 = techo antiban).
        start_dt: primer día a partir del cual repartir (UTC). Default: hoy.
        dry_run: si True, solo loguea el plan sin escribir nada.

    Returns:
        Nº de vídeos reprogramados.
    """
    try:
        from api.services.spam_mitigation import _pending_publish_all
        pending = _pending_publish_all(channel_id, db) or []
    except Exception:
        return 0
    if len(pending) <= max_per_day:
        return 0

    now = datetime.now(timezone.utc)
    if start_dt is None:
        start_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=timezone.utc)

    uploader = None
    moved = 0
    day_idx = 0
    used_in_day = 0

    for v in pending:
        # Avanzar de día hasta un día >= hoy con hueco (máx max_per_day/día).
        guard = 0
        while guard < 365:
            target_day = start_dt + timedelta(days=day_idx)
            if target_day.date() >= now.date() and used_in_day < max_per_day:
                break
            day_idx += 1
            used_in_day = 0
            guard += 1
        used_in_day += 1

        try:
            orig_dt = datetime.fromisoformat(
                str(v.get("target_public_at", "")).replace("Z", "+00:00").replace(" ", "T")
            )
            if orig_dt.tzinfo is None:
                orig_dt = orig_dt.replace(tzinfo=timezone.utc)
            hour, minute = orig_dt.hour, orig_dt.minute
        except (ValueError, TypeError):
            orig_dt = None
            hour, minute = 12, 0

        new_dt = target_day.replace(hour=hour, minute=minute, tzinfo=timezone.utc)
        if new_dt <= now:
            new_dt = target_day.replace(hour=12, minute=0, tzinfo=timezone.utc)

        # Ya en el día/hora correctos → no tocar (evita 50 ud/vídeo en cada arranque).
        if orig_dt is not None:
            try:
                if (orig_dt.date(), orig_dt.hour, orig_dt.minute) == (
                    new_dt.date(), new_dt.hour, new_dt.minute
                ):
                    continue
            except (ValueError, TypeError):
                pass

        new_iso = new_dt.isoformat()
        if dry_run:
            logger.info(
                "[%s] spread[%d] DRY: #%s %s → %s",
                slug, channel_id, v.get("id"), str(v.get("target_public_at", ""))[:19], new_iso[:19],
            )
            moved += 1
            continue

        # 1. DB (siempre) + tablas auxiliares.
        try:
            with db._connect() as conn:
                conn.execute(
                    "UPDATE videos SET target_public_at = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = ?",
                    (new_iso, v["id"]),
                )
                conn.execute(
                    "UPDATE planned_slots SET target_public_at = ? WHERE video_id = ?",
                    (new_iso, v["id"]),
                )
                conn.execute(
                    """UPDATE video_lifecycle_actions SET scheduled_for = ?
                       WHERE video_id = ? AND action_type = 'go_public'
                         AND status = 'pending'""",
                    (new_iso, v["id"]),
                )
                conn.commit()
        except Exception as exc:
            logger.warning("spread publish: DB update failed for video %s: %s", v["id"], exc)
            continue

        # 2. YouTube nativo (best-effort, solo si hay yt_video_id).
        yt_id = v.get("video_id") or v.get("yt_video_id")
        if yt_id:
            try:
                if uploader is None:
                    from pipeline.youtube_uploader import YouTubeUploader
                    uploader = YouTubeUploader(account_name=slug, channel_slug=slug)
                uploader.set_publish_at(yt_id, new_iso)
            except Exception as exc:
                logger.warning(
                    "spread publish: set_publish_at failed para %s (%s): %s",
                    yt_id, slug, exc,
                )
        moved += 1

    if moved and not dry_run:
        logger.warning(
            "⚠️ Publish spread [%s]: %d publicación(es) reprogramada(s) a máx %d/día",
            slug, moved, max_per_day,
        )
    return moved


def _apply_explicit_policy(db, cid: int, slug: str, policy: dict, dry_run: bool = False) -> str:
    """Materializa la política explícita en las configs de planning y shorts.

    Idempotente. Devuelve la acción registrada (para el informe del endpoint).
    """
    longs = policy["longs_per_day"]
    natives = policy["native_shorts_per_day"]
    shorts_on = policy["shorts_enabled"]
    action = (f"explicit: vpd={longs} gen={longs} alt=None "
              f"shorts_native={natives} shorts_on={int(shorts_on)} clips=0")
    if dry_run:
        return "DRY " + action
    db.update_channel_planning_config(
        cid,
        videos_per_day=longs,
        longform_generation_per_day=longs,
        alternate_pattern=None,
        alternate_offset=0,
        videos_day_boost_weight=0.0,
    )
    db.update_shorts_planning_config(
        cid,
        {
            "shorts_enabled": shorts_on,
            "shorts_native_per_day": natives,
            "shorts_clip_per_day": 0,
            "shorts_clips_per_long": 0,
        },
    )
    logger.warning("⚠️ Gradual resume [%s]: política explícita aplicada (%s)", slug, action)
    return action


def apply_resume_phases(db=None, replan: bool = True, dry_run: bool = False) -> dict:
    """Aplica la fase vigente de cada canal (config de planning + shorts).

    ``replan=False`` (arranque de API): no toca el horizonte — el planning
    engine corre justo después y recoge la config. ``replan=True`` (CLI):
    regenera el horizonte de 7 días para que el cambio sea inmediato.
    Idempotente y solo avanza fases.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    plan = load_plan(db)
    if not plan:
        plan = build_plan(db)
    if not plan:
        return {"ok": True, "applied": [], "message": "sin canales activos"}

    today = datetime.now(timezone.utc).date()
    applied: list[dict] = []
    changed = False

    for cid, entry in sorted(plan.items(), key=lambda kv: kv[1].get("slug", "")):
        ch = db.get_channel(cid)
        if not ch:
            continue
        slug = ch.get("slug", "")

        # ── Política explícita (aprobada por el operador): gana sobre fases ──
        # Los canales con política explícita se materializan y se saltan las
        # fases derivadas de fechas (la rampa automática queda inerte para ellos).
        policy = get_explicit_delivery_policy(cid, db)
        if policy is not None:
            _action = _apply_explicit_policy(db, cid, slug, policy, dry_run=dry_run)
            applied.append({"slug": slug, "phase": None, "action": _action})
            changed = True
            continue

        phase = _phase_for(entry, today)
        if phase == 0:
            applied.append({"slug": slug, "phase": 0, "action": "skip (bloqueado/no iniciado)"})
            continue

        if phase == 1:
            try:
                start = datetime.fromisoformat(entry["start_iso"]).date()
                alt_offset = (-start.toordinal()) % 2  # día 1 = día de publicación
            except (ValueError, TypeError):
                alt_offset = 0
            vpd = VIDEOS_PER_DAY_MAX
            alt_pattern = [1, 0]
        else:
            vpd = VIDEOS_PER_DAY_MAX
            alt_pattern = None
            alt_offset = 0

        # Fase 1 conserva 1 native/día; fase 2 habilita 2. Los clips continúan
        # desactivados tras los strikes.
        native_per_day = (
            PHASE2_SHORTS_NATIVE_PER_DAY if phase >= 2 else PHASE1_SHORTS_NATIVE_PER_DAY
        )
        shorts_changed = False
        sc_list = db.get_shorts_planning_config(channel_id=cid) or []
        sc = sc_list[0] if sc_list else {}
        cur_native = int(sc.get("shorts_native_per_day", native_per_day) or native_per_day)
        cur_clips = int(sc.get("shorts_clips_per_long", SHORTS_CLIPS_PER_LONG) or SHORTS_CLIPS_PER_LONG)
        if cur_native != native_per_day or cur_clips != SHORTS_CLIPS_PER_LONG:
            shorts_changed = True

        cfg = db.get_channel_planning_config(cid) or {}
        cur_vpd = int(cfg.get("videos_per_day", 1) or 1)
        cur_alt = cfg.get("alternate_pattern")
        cur_off = int(cfg.get("alternate_offset", 0) or 0)
        long_changed = (cur_vpd != vpd or cur_alt != alt_pattern or cur_off != alt_offset)

        if not long_changed and not shorts_changed:
            # Config ya correcta. En Fase 1 igual hay que mantener el esparcido
            # de publicaciones pendientes (máx 1 por día de publicación).
            note = ""
            if phase == 1 and entry.get("source") == "unblock":
                try:
                    start_dt = datetime.fromisoformat(entry["start_iso"])
                    moved = _spread_phase1_pending(db, cid, slug, start_dt)
                    if moved:
                        note = f" + {moved} pendientes reprogramadas"
                except Exception as exc:
                    logger.warning("spread phase1 pending failed for %s: %s", slug, exc)
            applied.append({"slug": slug, "phase": phase, "action": "sin cambios" + note})
            continue

        if dry_run:
            applied.append({
                "slug": slug, "phase": phase,
                "action": f"DRY: vpd={vpd} alt={alt_pattern} "
                           f"shorts_native={native_per_day} clips={SHORTS_CLIPS_PER_LONG}",
            })
            changed = True
            continue

        try:
            db.update_channel_planning_config(
                cid,
                videos_per_day=vpd,
                alternate_pattern=alt_pattern,
                alternate_offset=alt_offset,
                videos_day_boost_weight=0.0,
            )
            if shorts_changed:
                db.update_shorts_planning_config(
                    cid,
                    {"shorts_native_per_day": native_per_day,
                     "shorts_clips_per_long": SHORTS_CLIPS_PER_LONG},
                )
            db.set_system_state(_PHASE_KEY.format(channel_id=cid), str(phase))
            action = (f"vpd={vpd} alt={alt_pattern} "
                       f"shorts_native={native_per_day} clips={SHORTS_CLIPS_PER_LONG}")
            # Fase 1: esparcir publicaciones pendientes (máx 1/día de publicación)
            if phase == 1 and entry.get("source") == "unblock":
                try:
                    start_dt = datetime.fromisoformat(entry["start_iso"])
                    moved = _spread_phase1_pending(db, cid, slug, start_dt)
                    if moved:
                        action += f" + {moved} pendientes reprogramadas"
                except Exception as exc:
                    logger.warning("spread phase1 pending failed for %s: %s", slug, exc)
            applied.append({"slug": slug, "phase": phase, "action": action})
            changed = True
            logger.warning("⚠️ Gradual resume [%s]: fase %d aplicada (%s)", slug, phase, action)
        except Exception as exc:
            logger.warning("gradual resume apply failed for %s: %s", slug, exc)
            applied.append({"slug": slug, "phase": phase, "action": f"ERROR: {exc}"})

    replan_result = None
    if changed and not dry_run and replan:
        try:
            from api.services.planning_service import compute_and_store_horizon
            replan_result = compute_and_store_horizon(horizon_days=7, db=db)
        except Exception as exc:
            replan_result = {"error": str(exc), "note": "el planning engine diario lo recogerá"}

    return {"ok": True, "applied": applied, "replan": replan_result, "dry_run": dry_run}


def resume_status(db=None) -> list[dict]:
    """Resumen del plan por canal (para CLI/panel)."""
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
    plan = load_plan(db)
    if not plan:
        plan = build_plan(db, persist=True)
    today = datetime.now(timezone.utc).date()
    out = []
    for cid, entry in sorted(plan.items(), key=lambda kv: kv[1].get("slug", "")):
        ch = db.get_channel(cid)
        slug = ch.get("slug", "") if ch else entry.get("slug", str(cid))
        name = ch.get("name", "") if ch else slug
        cfg = db.get_channel_planning_config(cid) or {}
        sc_list = db.get_shorts_planning_config(channel_id=cid) or []
        sc = sc_list[0] if sc_list else {}
        out.append({
            "channel_id": cid,
            "slug": slug,
            "name": name,
            "source": entry.get("source", "?"),
            "sibling_of": entry.get("sibling_of", ""),
            "start_iso": entry.get("start_iso", ""),
            "phase_today": _phase_for(entry, today),
            "freq": {
                "videos_per_day": cfg.get("videos_per_day"),
                "alternate_pattern": cfg.get("alternate_pattern"),
                "shorts_native_per_day": sc.get("shorts_native_per_day"),
                "shorts_clips_per_long": sc.get("shorts_clips_per_long"),
            },
        })
    return out


def _phase_label(phase: int, source: str = "") -> str:
    """Etiqueta humana de la fase."""
    if phase == 0:
        return "Bloqueado / no iniciado"
    if phase == 1:
        return "Fase 1 — 1 long cada 2 días"
    return "Fase 2 — 1 long/día"


def resume_status_detailed(db=None) -> list[dict]:
    """Estado detallado de reanudación por canal, para la UI.

    Combina resume_status() + build_spam_situation() (strikes/bloqueo/frecuencia
    rebajada) + _pending_publish_all() (publicaciones pendientes), y calcula el
    countdown a la siguiente fase. Lectura ligera (0 cuota de YouTube API).
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
    from api.services.spam_mitigation import (
        build_spam_situation, _pending_publish_all,
    )
    base = resume_status(db)
    today = datetime.now(timezone.utc).date()
    out = []
    for r in base:
        cid = int(r["channel_id"])
        phase = int(r["phase_today"])
        r["phase_label"] = _phase_label(phase, r.get("source", ""))

        # Timeline: días transcurridos / restantes / siguiente transición
        try:
            start_dt = datetime.fromisoformat(r["start_iso"]).date()
        except (ValueError, TypeError):
            start_dt = today
        days_elapsed = (today - start_dt).days
        r["days_elapsed"] = max(0, days_elapsed)
        r["next_transition_iso"] = None
        if phase == 0 and days_elapsed < 0:
            r["days_remaining_in_phase"] = -days_elapsed
            r["next_transition_iso"] = start_dt.isoformat()
        elif phase == 1:
            remaining = PHASE1_DAYS - days_elapsed
            r["days_remaining_in_phase"] = max(0, remaining)
            r["next_transition_iso"] = (start_dt + timedelta(days=PHASE1_DAYS + 1)).isoformat()
        else:
            r["days_remaining_in_phase"] = None
            r["next_transition_iso"] = None

        # Strikes / bloqueo / frecuencia (reutiliza el módulo de spam)
        try:
            sit = build_spam_situation(cid, db) or {}
            r["strikes"] = sit.get("strikes", 0)
            r["blocked"] = bool(sit.get("blocked", False))
            r["restan_h"] = sit.get("restan_h", 0.0)
            r["freq_reduced"] = bool(sit.get("freq_reduced", False))
        except Exception:
            r["strikes"] = 0
            r["blocked"] = False
            r["restan_h"] = 0.0
            r["freq_reduced"] = False

        # Publicaciones pendientes (esparcido de Fase 1 visible en la UI)
        try:
            pending = _pending_publish_all(cid, db) or []
            r["pending_publish"] = {
                "total": len(pending),
                "upcoming": [
                    {"video_id": p.get("video_id") or p.get("yt_video_id") or "",
                     "target_public_at": p.get("target_public_at", "")}
                    for p in pending
                ],
            }
        except Exception:
            r["pending_publish"] = {"total": 0, "upcoming": []}
        out.append(r)
    return out
