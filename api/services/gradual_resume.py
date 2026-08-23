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
SHORTS_NATIVE_PER_DAY = 1  # techo shorts nativos
SHORTS_CLIPS_PER_LONG = 0  # clips desactivados
VIDEOS_PER_DAY_MAX = 1     # techo long-form

_PLAN_KEY = "resume_plan_{channel_id}"
_PHASE_KEY = "resume_phase_{channel_id}"


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
            hour, minute = 12, 0
        new_dt = target_day.replace(hour=hour, minute=minute, tzinfo=timezone.utc)
        if new_dt <= now:
            new_dt = target_day.replace(hour=12, minute=0, tzinfo=timezone.utc)
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

        # Shorts: asegurar techo (native=1, clips=0)
        shorts_changed = False
        sc_list = db.get_shorts_planning_config(channel_id=cid) or []
        sc = sc_list[0] if sc_list else {}
        cur_native = int(sc.get("shorts_native_per_day", SHORTS_NATIVE_PER_DAY) or SHORTS_NATIVE_PER_DAY)
        cur_clips = int(sc.get("shorts_clips_per_long", SHORTS_CLIPS_PER_LONG) or SHORTS_CLIPS_PER_LONG)
        if cur_native != SHORTS_NATIVE_PER_DAY or cur_clips != SHORTS_CLIPS_PER_LONG:
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
                          f"shorts_native={SHORTS_NATIVE_PER_DAY} clips={SHORTS_CLIPS_PER_LONG}",
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
                    {"shorts_native_per_day": SHORTS_NATIVE_PER_DAY,
                     "shorts_clips_per_long": SHORTS_CLIPS_PER_LONG},
                )
            db.set_system_state(_PHASE_KEY.format(channel_id=cid), str(phase))
            action = (f"vpd={vpd} alt={alt_pattern} "
                      f"shorts_native={SHORTS_NATIVE_PER_DAY} clips={SHORTS_CLIPS_PER_LONG}")
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
        cfg = db.get_channel_planning_config(cid) or {}
        sc_list = db.get_shorts_planning_config(channel_id=cid) or []
        sc = sc_list[0] if sc_list else {}
        out.append({
            "slug": slug,
            "source": entry.get("source", "?"),
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
