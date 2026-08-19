"""Publish Scheduler — Calcula la hora pico óptima para publicar cada vídeo.

Algoritmo en dos fases:
1. Heurística por nicho + país: tabla base de horas recomendadas según el tema y zona.
2. Auto-ajuste con histórico: si hay datos de rendimiento (video_stats_history),
   desplaza la hora hacia la franja que históricamente ha dado mejor rendimiento
   para ese canal.
"""

import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Optional
import pytz

logger = logging.getLogger(__name__)

# ── Heurística por nicho ──────────────────────────────────────────
# Fuente: benchmarks de YouTube (Social Blade, TubeBuddy, Metricool).
# Las horas se refieren a la hora LOCAL del público objetivo.
# Claves: match del SEO_PRIMARY_KEYWORD contra palabras clave de cada nicho.

NICHO_PEAK_HOURS = {
    "misterio_paranormal": {
        "keywords": [
            "milagro", "misterio", "inexplicable", "paranormal", "fantasma",
            "fenómeno", "casualidad", "coincidencia", "sincronía", "destino",
            "conspiración", "ovni", "extraterrestre", "profecía", "premonición",
            "secreto", "oculto", "enigma", "leyenda", "maldición",
        ],
        "peak_hour": 21,       # 9 PM — consumo nocturno de misterio
        "secondary_peaks": [14, 17, 20],  # sobremesa, media tarde, pre-prime (sin madrugada)
        "peak_day": "daily",
        "note": "El contenido de misterio tiene pico nocturno (20-23h)."
               " Las notificaciones previas al prime time (21h) maximizan CTR.",
    },
    "historia_documental": {
        "keywords": [
            "historia", "civilización", "documental", "antiguo", "arqueología",
            "expedición", "explorador", "descubrimiento", "ruinas", "imperio",
            "culturas", "perdida", "olvidada", "tumba", "pirámide",
        ],
        "peak_hour": 20,       # 8 PM — franja documental vespertina
        "secondary_peaks": [11, 14, 17],  # mañana-tarde, sobremesa, media tarde
        "peak_day": "weekend",
        "note": "Documentales funcionan mejor en fin de semana tarde (18-21h).",
    },
    "noticias_actualidad": {
        "keywords": [
            "noticia", "actualidad", "última hora", "política", "economía",
            "breaking", "urgente", "análisis",
        ],
        "peak_hour": 12,       # Mediodía
        "secondary_peaks": [7, 19, 22],  # mañana, tarde-noche, noche
        "peak_day": "weekday",
        "note": "Noticias/actualidad pico al mediodía y a las 19h.",
    },
    "educacion_ciencia": {
        "keywords": [
            "ciencia", "física", "matemáticas", "biología", "química",
            "experimento", "explicación", "teoría", "aprender", "curiosidad",
            "dato curioso", "sabías que", "educación",
        ],
        "peak_hour": 18,       # 6 PM — después del trabajo/escuela
        "secondary_peaks": [10, 14, 21],  # media mañana, sobremesa, noche
        "peak_day": "daily",
        "note": "Contenido educativo pico 17-19h y fines de semana.",
    },
    "entretenimiento_general": {
        "keywords": [],
        "peak_hour": 20,       # 8 PM — default prime time
        "secondary_peaks": [12, 15, 22],  # mediodía, media tarde, noche
        "peak_day": "daily",
        "note": "Entretenimiento general: prime time 20-22h.",
    },
}

# ── Por país/región: offset UTC para convertir hora local → UTC ──
# NOTE: This table is currently unused by active code. All timezone
# conversions use pytz.timezone(channel_config.PUBLISH_TIMEZONE) instead.
COUNTRY_UTC_OFFSETS = {
    # Latinoamérica
    "mx": -6, "ar": -3, "co": -5, "pe": -5, "cl": -4, "ve": -4,
    "ec": -5, "bo": -4, "py": -4, "uy": -3, "pa": -5, "cr": -6,
    "sv": -6, "gt": -6, "hn": -6, "ni": -6, "do": -4, "pr": -4,
    "cu": -5,
    # España
    "es": +2,  # CEST en verano, +1 en invierno
    # US
    "us": -5,  # ET; considerar -8 para PT
}

# ── Variación permitida respecto al pico detectado por histórico ──
HISTORY_SHIFT_MAX_HOURS = 2.5  # Máximo desplazamiento respecto a la heurística
HISTORY_MIN_DATA_POINTS = 3    # Mínimos puntos de datos para confiar en histórico
SAME_CHANNEL_PUBLISH_GAP_HOURS = 3  # v10.1: Mínimo de horas entre publicaciones del mismo canal

# ── Antelación máxima de publicación (clamp "no tan lejano") ──
# (ago 2026): los vídeos subidos como private con publishAt heredan el
# target_public_at del slot planificado, que puede estar a días vista
# (horizonte de planning 7d + lead boost). Sin clamp, un vídeo subido hoy
# se queda "calentando" (uploaded_private) hasta su día de publicación.
# Con este límite, si al SUBIR el target_public_at está más allá de
# MAX_PUBLISH_AT_AHEAD_HOURS, se recalcula al siguiente pico (o ahora+warmup),
# de modo que un vídeo nunca espera más de este margen en privado.
MAX_PUBLISH_AT_AHEAD_HOURS = 24

# Seguridad anti-patología para el clamp: el resultado NUNCA supera este margen
# (5 días), aunque la resolución de colisiones haya empujado más allá. No es un
# objetivo: el espaciado real lo garantiza la resolución de colisiones / repack.
MAX_PUBLISH_AHEAD_SAFETY_HOURS = 120


def detect_niche(keywords: list[str]) -> str:
    """Detecta el nicho del canal a partir de sus keywords SEO.

    Devuelve la clave del nicho más probable ('misterio_paranormal', etc.)
    o 'entretenimiento_general' como fallback.
    """
    if not keywords:
        return "entretenimiento_general"

    keyword_lower = " ".join(keywords).lower()
    best_niche = "entretenimiento_general"
    best_score = 0

    for niche_id, niche_data in NICHO_PEAK_HOURS.items():
        score = sum(1 for kw in niche_data["keywords"] if kw.lower() in keyword_lower)
        if score > best_score:
            best_score = score
            best_niche = niche_id

    if best_score == 0:
        logger.debug("No niche match — using entertainment general")
    else:
        logger.debug("Detected niche '%s' (score=%d)", best_niche, best_score)

    return best_niche


def get_channel_peak_info(config_or_dict) -> dict:
    """Obtener la(s) franja(s) pico efectiva(s) de un canal sin agendar nada.

    Prioridad: config.PUBLISH_TARGET_HOUR > heurística del nicho > historial > default.
    Usable desde el planning, la UI y el uploader.

    Args:
        config_or_dict: objeto de config (SimpleNamespace) o dict con keys:
            SEO_PRIMARY_KEYWORD, SEO_SECONDARY_KEYWORDS, PUBLISH_TARGET_HOUR,
            PUBLISH_TIMEZONE, PUBLISH_JITTER_MIN, PUBLISH_WARMUP_MIN

    Returns:
        {
            "peak_hour": int,           # hora principal (local)
            "secondary_peaks": [int],   # picos secundarios por orden
            "jitter_min": int,
            "timezone": str,
            "warmup_min": int,
            "source": "config"|"heuristic",
            "niche": str,
        }
    """
    # Normalize to dict
    cfg = config_or_dict
    if not isinstance(cfg, dict):
        cfg = {k: v for k, v in vars(config_or_dict).items() if not k.startswith("_")}

    primary_kw = cfg.get("SEO_PRIMARY_KEYWORD", "") or cfg.get("seo_primary_keyword", "")
    secondary_kws = cfg.get("SEO_SECONDARY_KEYWORDS", []) or cfg.get("seo_secondary_keywords", [])
    target_hour = cfg.get("PUBLISH_TARGET_HOUR") or cfg.get("publish_target_hour")
    jitter = cfg.get("PUBLISH_JITTER_MIN", 20) or cfg.get("publish_jitter_min", 20)
    warmup = cfg.get("PUBLISH_WARMUP_MIN", 120) or cfg.get("publish_warmup_min", 120)
    tz_str = cfg.get("PUBLISH_TIMEZONE", "Europe/Madrid") or cfg.get("publish_timezone", "Europe/Madrid")

    all_keywords = [primary_kw] if primary_kw else []
    if secondary_kws:
        all_keywords.extend(secondary_kws)

    niche = detect_niche(all_keywords)
    niche_data = NICHO_PEAK_HOURS.get(niche, NICHO_PEAK_HOURS["entretenimiento_general"])

    if target_hour is not None:
        peak = int(target_hour)
        source = "config"
        secondary = niche_data.get("secondary_peaks", [])
    else:
        peak = niche_data["peak_hour"]
        source = "heuristic"
        secondary = niche_data.get("secondary_peaks", [])

    # Ensure peak is not duplicated in secondary
    secondary = [h for h in secondary if h != peak]

    return {
        "peak_hour": peak,
        "secondary_peaks": secondary,
        "jitter_min": jitter,
        "timezone": tz_str,
        "warmup_min": warmup,
        "source": source,
        "niche": niche,
    }


def get_peak_windows(config_or_dict, n: int = 1) -> list[tuple]:
    """Devuelve las n mejores franjas (hora, peso, nombre) para distribuir n vídeos/día.

    Usa el pico principal primero, luego picos secundarios. Si necesita más,
    reutiliza el principal con offsets (±1h, ±2h).

    Returns:
        Lista de (start_hour, end_hour, weight, name) al estilo de SPAIN_UPLOAD_WINDOWS.
    """
    info = get_channel_peak_info(config_or_dict)
    peak = info["peak_hour"]
    secondary = info["secondary_peaks"]

    windows = []
    priority_hours = [peak] + [h for h in secondary if h != peak]

    for i in range(n):
        if i < len(priority_hours):
            h = priority_hours[i]
            name = "pico principal" if h == peak else f"pico secundario {i}"
            weight = 3 if h == peak else (2 if i < 3 else 1)
        else:
            # Overflow: offset from peak (±1h, ±2h...)
            offset_idx = i - len(priority_hours) + 1
            h = (peak + offset_idx) % 24
            name = f"pico+{offset_idx}h"
            weight = 1

        start_h = max(0, h - 1)
        end_h = min(23, h + 1)
        windows.append((start_h, end_h, weight, name))

    return windows


def _avoid_channel_collision(
    channel_id: int,
    proposed_utc: datetime,
    db=None,
    slug: str = "",
    cross_channel: bool = False,
) -> datetime:
    """Check and resolve same-channel publish time collisions.

    Queries the DB for ANY video or planned slot whose target_public_at
    is within SAME_CHANNEL_PUBLISH_GAP_HOURS of the proposed time.

    v10.3 (Aug 2026): Rewritten to cover ALL gap scenarios:
      - No status filter on videos — any video with target_public_at is checked.
      - Also queries planned_slots table (pending/running slots not yet dispatched).
      - Optional cross-channel collision with MIN_CROSS_CHANNEL_GAP_MINUTES.

    If collision detected, shifts the proposed time forward to the next
    available window (minimum gap enforced).

    Returns:
        Adjusted publish datetime (UTC), or the original if no collision.
    """
    if db is None or channel_id is None:
        if db is None:
            logger.warning(
                "[%s] _avoid_channel_collision called without db — collision check SKIPPED. "
                "This may cause duplicate publish times.", slug,
            )
        return proposed_utc

    try:
        import sqlite3
        from datetime import timedelta as _timedelta
        min_gap = _timedelta(hours=SAME_CHANNEL_PUBLISH_GAP_HOURS)
        # Cross-channel: minimum 30 min gap between different channels
        MIN_CROSS_CHANNEL_GAP_MINUTES = 30
        cross_gap = _timedelta(minutes=MIN_CROSS_CHANNEL_GAP_MINUTES)
        
        conflict_found = True
        adjusted = proposed_utc
        max_iterations = 8  # safety: don't loop forever

        for iteration in range(max_iterations):
            # ── Determine effective window (wider for cross-channel) ──
            if cross_channel and iteration == 0:
                # For cross-channel: check smaller window (±30 min)
                check_gap = cross_gap
            else:
                check_gap = min_gap

            # ── v10.4: ISO8601-bound comparison. Use 'T' separator so stored
            # ISO8601 timestamps (e.g. "2026-08-03T09:00:00+00:00") compare
            # correctly with the bound strings. REPLACE(...,' ','T') handles
            # legacy space-separated targets stored before this fix.
            # Without this, 'T' > ' ' in lexicographic order, making ISO8601
            # strings always > space-separated bounds — collision was blind.
            window_start_iso = (adjusted - check_gap).strftime("%Y-%m-%dT%H:%M:%S")
            window_end_iso = (adjusted + check_gap).strftime("%Y-%m-%dT%H:%M:%S")

            # ── Check 1: videos table — ALL statuses, any video with target_public_at ──
            # v10.3: Removed status filter. ANY video (published, generating, draft, etc.)
            # with a target_public_at in the window is a collision.
            video_collisions = []
            conn = None
            try:
                if hasattr(db, '_connect'):
                    conn = db._connect()
                    if cross_channel:
                        # Cross-channel: check ALL channels, not just this one
                        video_rows = conn.execute("""
                            SELECT v.id, v.target_public_at, v.titulo_final, v.channel_id
                            FROM videos v
                            WHERE v.target_public_at IS NOT NULL
                              AND REPLACE(v.target_public_at, ' ', 'T') >= ?
                              AND REPLACE(v.target_public_at, ' ', 'T') <= ?
                            ORDER BY v.target_public_at
                            LIMIT 10
                        """, (window_start_iso, window_end_iso)).fetchall()
                    else:
                        video_rows = conn.execute("""
                            SELECT v.id, v.target_public_at, v.titulo_final
                            FROM videos v
                            WHERE v.channel_id = ?
                              AND v.target_public_at IS NOT NULL
                              AND REPLACE(v.target_public_at, ' ', 'T') >= ?
                              AND REPLACE(v.target_public_at, ' ', 'T') <= ?
                            ORDER BY v.target_public_at
                            LIMIT 10
                        """, (channel_id, window_start_iso, window_end_iso)).fetchall()
                    for row in video_rows:
                        video_collisions.append({
                            "source": "video",
                            "id": row["id"],
                            "datetime_str": row["target_public_at"],
                            "label": (row["titulo_final"] or "?")[:40],
                            "channel_id": dict(row).get("channel_id"),
                        })
            except Exception:
                pass
            finally:
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass

            # ── Check 2: planned_slots table (pending/running, not yet dispatched as video) ──
            # v10.3: Catches slots that were planned but haven't been converted to videos yet.
            # A slot with video_id is already covered by the videos check above.
            planned_collisions = []
            conn_ps = None
            try:
                if hasattr(db, '_connect'):
                    conn_ps = db._connect()
                    if cross_channel:
                        planned_rows = conn_ps.execute("""
                            SELECT ps.id, ps.target_public_at, ps.channel_id,
                                   ps.video_id
                            FROM planned_slots ps
                            WHERE ps.target_public_at IS NOT NULL
                              AND ps.status IN ('pending', 'running')
                              AND REPLACE(ps.target_public_at, ' ', 'T') >= ?
                              AND REPLACE(ps.target_public_at, ' ', 'T') <= ?
                            ORDER BY ps.target_public_at
                            LIMIT 10
                        """, (window_start_iso, window_end_iso)).fetchall()
                    else:
                        planned_rows = conn_ps.execute("""
                            SELECT ps.id, ps.target_public_at, ps.video_id
                            FROM planned_slots ps
                            WHERE ps.channel_id = ?
                              AND ps.target_public_at IS NOT NULL
                              AND ps.status IN ('pending', 'running')
                              AND REPLACE(ps.target_public_at, ' ', 'T') >= ?
                              AND REPLACE(ps.target_public_at, ' ', 'T') <= ?
                            ORDER BY ps.target_public_at
                            LIMIT 10
                        """, (channel_id, window_start_iso, window_end_iso)).fetchall()
                    for row in planned_rows:
                        dict_row = dict(row)
                        # Skip if video_id is already set (covered by video check)
                        if dict_row.get("video_id"):
                            continue
                        planned_collisions.append({
                            "source": "planned_slot",
                            "id": row["id"],
                            "datetime_str": row["target_public_at"],
                            "label": f"slot #{row['id']}",
                            "channel_id": dict_row.get("channel_id"),
                        })
            except Exception:
                pass
            finally:
                if conn_ps:
                    try:
                        conn_ps.close()
                    except Exception:
                        pass

            # ── Check 3: lifecycle go_public actions (scheduled_for) ──
            # Catches desyncs where lifecycle collision guards adjusted
            # scheduled_for but videos.target_public_at was not updated.
            lifecycle_collisions = []
            conn_lc = None
            try:
                if hasattr(db, '_connect'):
                    conn_lc = db._connect()
                    if cross_channel:
                        lifecycle_rows = conn_lc.execute("""
                            SELECT vla.id as action_id, vla.video_id,
                                   vla.scheduled_for, vla.channel_id
                            FROM video_lifecycle_actions vla
                            WHERE vla.action_type = 'go_public'
                              AND vla.status = 'pending'
                              AND vla.scheduled_for IS NOT NULL
                              AND REPLACE(vla.scheduled_for, ' ', 'T') >= ?
                              AND REPLACE(vla.scheduled_for, ' ', 'T') <= ?
                            ORDER BY vla.scheduled_for
                            LIMIT 10
                        """, (window_start_iso, window_end_iso)).fetchall()
                    else:
                        lifecycle_rows = conn_lc.execute("""
                            SELECT vla.id as action_id, vla.video_id,
                                   vla.scheduled_for
                            FROM video_lifecycle_actions vla
                            WHERE vla.channel_id = ?
                              AND vla.action_type = 'go_public'
                              AND vla.status = 'pending'
                              AND vla.scheduled_for IS NOT NULL
                              AND REPLACE(vla.scheduled_for, ' ', 'T') >= ?
                              AND REPLACE(vla.scheduled_for, ' ', 'T') <= ?
                            ORDER BY vla.scheduled_for
                            LIMIT 10
                        """, (channel_id, window_start_iso, window_end_iso)).fetchall()
                    for row in lifecycle_rows:
                        dr = dict(row)
                        lifecycle_collisions.append({
                            "source": "lifecycle",
                            "id": row["video_id"],
                            "datetime_str": row["scheduled_for"],
                            "label": f"go_public #{row['action_id']}",
                            "channel_id": dr.get("channel_id"),
                        })
            except Exception:
                pass
            finally:
                if conn_lc:
                    try:
                        conn_lc.close()
                    except Exception:
                        pass

            # ── Merge collisions from all sources ──
            all_collisions = video_collisions + planned_collisions + lifecycle_collisions
            
            # ── Cross-channel: filter out own channel collisions ──
            if cross_channel:
                own_collisions = [c for c in all_collisions
                                  if c.get("channel_id") is None or c["channel_id"] == channel_id]
                other_collisions = [c for c in all_collisions
                                    if c.get("channel_id") is not None and c["channel_id"] != channel_id]
                
                # Own-channel collisions => use full 3h gap
                # Other-channel collisions => use 30 min gap
                if own_collisions:
                    # Process own-channel first (full gap)
                    latest_own = None
                    latest_own_dt = None
                    for c in own_collisions:
                        try:
                            ts = c["datetime_str"]
                            if isinstance(ts, str):
                                dt = datetime.fromisoformat(ts.replace("Z", "+00:00").replace(" ", "T"))
                            else:
                                dt = ts
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=timezone.utc)
                            if latest_own_dt is None or dt > latest_own_dt:
                                latest_own_dt = dt
                                latest_own = c
                        except (ValueError, TypeError):
                            continue
                    if latest_own_dt is not None:
                        adjusted = latest_own_dt + min_gap
                        logger.info(
                            "[%s] Same-channel collision [%s]: proposed %s conflicts with "
                            "#%s (%s) at %s. Pushing to %s.",
                            slug, latest_own["source"],
                            proposed_utc.strftime("%H:%M"),
                            latest_own["id"], latest_own.get("label", "?")[:40],
                            latest_own_dt.strftime("%H:%M"),
                            adjusted.strftime("%H:%M"),
                        )
                        continue  # re-check with new adjusted time

                if other_collisions:
                    # Process cross-channel (smaller gap)
                    latest_other = max(other_collisions,
                                       key=lambda c: c["datetime_str"] if isinstance(c["datetime_str"], datetime)
                                       else datetime.fromisoformat(str(c["datetime_str"]).replace("Z", "+00:00").replace(" ", "T")))
                    try:
                        ts = latest_other["datetime_str"]
                        if isinstance(ts, str):
                            other_dt = datetime.fromisoformat(ts.replace("Z", "+00:00").replace(" ", "T"))
                        else:
                            other_dt = ts
                        if other_dt.tzinfo is None:
                            other_dt = other_dt.replace(tzinfo=timezone.utc)
                        adjusted = other_dt + cross_gap
                        logger.info(
                            "[%s] Cross-channel collision: proposed %s conflicts with "
                            "channel %s #%s at %s. Pushing to %s.",
                            slug, proposed_utc.strftime("%H:%M"),
                            latest_other.get("channel_id", "?"), latest_other["id"],
                            other_dt.strftime("%H:%M"),
                            adjusted.strftime("%H:%M"),
                        )
                        continue  # re-check with new adjusted time
                    except (ValueError, TypeError):
                        pass
            else:
                # ── Same-channel only: standard collision resolution ──
                if not all_collisions:
                    conflict_found = False
                    break

                # Find the latest collision time across all sources
                latest_collision = None
                latest_dt = None
                for c in all_collisions:
                    try:
                        ts = c["datetime_str"]
                        if isinstance(ts, str):
                            dt = datetime.fromisoformat(ts.replace("Z", "+00:00").replace(" ", "T"))
                        else:
                            dt = ts
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        if latest_dt is None or dt > latest_dt:
                            latest_dt = dt
                            latest_collision = c
                    except (ValueError, TypeError):
                        continue

                if latest_dt is None:
                    latest_dt = adjusted
                    latest_collision = {"id": "?", "label": "?"}

                adjusted = latest_dt + min_gap

                logger.info(
                    "[%s] Collision detected [%s]: proposed %s conflicts with "
                    "#%s (%s) at %s. Pushing to %s.",
                    slug,
                    latest_collision["source"],
                    proposed_utc.strftime("%H:%M"),
                    latest_collision["id"],
                    latest_collision.get("label", "?")[:40],
                    latest_dt.strftime("%H:%M"),
                    adjusted.strftime("%H:%M"),
                )

        if conflict_found and iteration >= max_iterations - 1:
            logger.warning(
                "[%s] Max collision iterations reached — using last adjusted time.",
                slug,
            )

        return adjusted

    except Exception as e:
        logger.debug("[%s] Collision check skipped: %s", slug, e)
        return proposed_utc


def _pick_optimal_slot(db, channel_id: int, slug: str = None) -> Optional[dict]:
    """Pick the best optimal publish slot using epsilon-greedy strategy.

    Returns:
        dict with {slot_rank, target_hour, target_minute, confidence, audience_focus}
        or None if no fresh optimal slots exist.

    Uses get_optimal_slot_assignment from ExtendedDatabase which implements:
    - 70% exploration (round-robin across 3 slots)
    - 30% exploitation (best-performing slot by avg views)
    """
    try:
        slot = db.get_optimal_slot_assignment(channel_id, "long")
        if slot:
            return {
                "slot_rank": slot["slot_rank"],
                "target_hour": slot["target_hour"],
                "target_minute": slot.get("target_minute", 0),
                "confidence": slot.get("confidence", 0.0),
                "audience_focus": slot.get("audience_focus", "blend"),
            }
    except Exception:
        pass
    return None


def _snap_to_next_target_hour(
    target_hour: int,
    timezone_str: str,
    warmup_min: int,
    slug: str,
    spread_min: int = 0,
) -> tuple[datetime, datetime]:
    """Snap to HH:00 in the given timezone, respecting warmup.

    If now + warmup_min is past target_hour today, schedules for tomorrow.

    Args:
        spread_min: If >0, add random ±spread_min minutes to avoid all videos
                    landing exactly on the same minute (v23).

    Returns:
        (target_utc: datetime (UTC), target_local: datetime (local tz))
    """
    import random

    try:
        tz = pytz.timezone(timezone_str)
    except pytz.UnknownTimeZoneError:
        logger.warning("[%s] Unknown timezone '%s', falling back to UTC", slug, timezone_str)
        tz = pytz.UTC

    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tz)

    # Build target at HH:00:00 today (local time)
    target_local = now_local.replace(
        hour=target_hour, minute=0, second=0, microsecond=0,
    )

    # If now + warmup is already past the target, push to tomorrow
    warmup_deadline = now_local + timedelta(minutes=warmup_min)
    if target_local <= warmup_deadline:
        target_local += timedelta(days=1)
        logger.info(
            "[%s] Warmup deadline (%s) >= target %02d:00 today → scheduling for tomorrow %s",
            slug,
            warmup_deadline.strftime("%H:%M"),
            target_hour,
            target_local.strftime("%Y-%m-%d %H:%M"),
        )
    else:
        logger.info(
            "[%s] Warmup OK: now=%s + %dmin=%s < target=%02d:00 → scheduling for today",
            slug,
            now_local.strftime("%H:%M"),
            warmup_min,
            warmup_deadline.strftime("%H:%M"),
            target_hour,
        )

    # ── v23: Apply random spread to avoid exact-snapping collisions ──
    jitter_applied = 0
    if spread_min > 0:
        jitter_applied = random.randint(-spread_min, spread_min)
        target_local += timedelta(minutes=jitter_applied)
        logger.info(
            "[%s] Applying spread jitter: %+d min → %s",
            slug, jitter_applied, target_local.strftime("%H:%M"),
        )

    target_utc = target_local.astimezone(pytz.UTC)
    return target_utc, target_local


def calculate_target_public_time(
    slug: str,
    primary_keyword: str = "",
    secondary_keywords: list[str] = None,
    timezone_str: str = "Europe/Madrid",
    target_hour: Optional[int] = None,
    jitter_min: int = 0,
    jitter_after: int = 0,
    warmup_min: int = 60,
    publish_window_spread_min: int = 0,
    db=None,
    channel_id: Optional[int] = None,
) -> dict:
    """Calcula la hora objetivo de publicación para un vídeo.

    Lógica determinista con spread opcional (v23): se ajusta a HH:00 en la zona
    horaria del canal, más un spread aleatorio de ±publish_window_spread_min
    minutos para evitar colisiones. Si no queda suficiente warmup (warmup_min)
    hasta esa hora hoy, se programa para la misma hora del día siguiente.

    Args:
        slug: Canal slug
        primary_keyword: Keyword SEO primaria
        secondary_keywords: Keywords SEO secundarias
        timezone_str: Zona horaria del canal (ej: 'Europe/Madrid')
        target_hour: Hora semilla (de config). Si es None, se usa la heurística.
        jitter_min: Ignorado (mantenido por compatibilidad de firma).
        jitter_after: Ignorado (mantenido por compatibilidad de firma).
        warmup_min: Minutos mínimos entre la subida y la publicación (default 60).
        publish_window_spread_min: ±minutos de spread aleatorio alrededor de HH:00 (default 0).
        db: Database para consultar histórico y optimal slots.
        channel_id: ID del canal en BD.

    Returns: {target_public_at, target_public_at_local, peak_hour_local, peak_source, niche,
              jitter_applied, warmup_min, ...}
    """
    all_keywords = [primary_keyword] if primary_keyword else []
    if secondary_keywords:
        all_keywords.extend(secondary_keywords)

    # ── 0. Intentar usar franjas óptimas calculadas (v10) ──
    optimal_slot_rank = None
    if db is not None and channel_id is not None:
        try:
            optimal_slot = _pick_optimal_slot(db, channel_id, slug)
            if optimal_slot is not None:
                seed_hour = optimal_slot["target_hour"]
                peak_source = "optimal_slots"
                niche = "data_driven"
                optimal_slot_rank = optimal_slot["slot_rank"]
                logger.info(
                    "[%s] Using optimal slot #%d: %02d:%02d (confidence=%.2f, focus=%s)",
                    slug, optimal_slot_rank, optimal_slot["target_hour"],
                    optimal_slot.get("target_minute", 0),
                    optimal_slot.get("confidence", 0),
                    optimal_slot.get("audience_focus", "blend"),
                )
                # Record usage for epsilon-greedy strategy
                try:
                    db.record_slot_usage(
                        channel_id, "long", optimal_slot_rank,
                    )
                except Exception:
                    pass

                # ── Snap to HH:00 ± spread_min, respecting warmup ──
                target_utc, target_local = _snap_to_next_target_hour(
                    seed_hour, timezone_str, warmup_min, slug,
                    spread_min=publish_window_spread_min,
                )

                # ── Avoid same-channel publish time collisions ──
                target_utc = _avoid_channel_collision(
                    channel_id, target_utc, db=db, slug=slug,
                )
                # ── v10.3: Cross-channel collision (30 min gap) ──
                target_utc = _avoid_channel_collision(
                    channel_id, target_utc, db=db, slug=slug, cross_channel=True,
                )

                logger.info(
                    "[%s] Scheduled publish via optimal slots: slot_rank=%d, "
                    "peak=%02d:00 (local), target=%s UTC",
                    slug, optimal_slot_rank, seed_hour,
                    target_utc.isoformat(),
                )
                return {
                    "target_public_at": target_utc.isoformat(),
                    "target_public_at_local": target_local.isoformat(),
                    "peak_hour_local": seed_hour,
                    "peak_source": peak_source,
                    "niche": niche,
                    "jitter_applied": 0,
                    "warmup_min": warmup_min,
                    "optimal_slot_rank": optimal_slot_rank,
                }
        except Exception as e:
            logger.debug("[%s] Optimal slots lookup skipped: %s", slug, e)

    # ── 1. Detectar nicho y obtener hora semilla ──
    niche = detect_niche(all_keywords)
    niche_data = NICHO_PEAK_HOURS.get(niche, NICHO_PEAK_HOURS["entretenimiento_general"])

    if target_hour is None:
        seed_hour = niche_data["peak_hour"]
    else:
        seed_hour = target_hour

    peak_source = "heuristic"

    # ── 2. Auto-ajuste con histórico si hay datos ──
    if db is not None and channel_id is not None:
        try:
            adjusted_hour = _adjust_from_history(
                db, channel_id, seed_hour, timezone_str,
            )
            if adjusted_hour is not None and adjusted_hour != seed_hour:
                logger.info(
                    "[%s] Peak hour adjusted from %02d:00 → %02d:00 (history)",
                    slug, seed_hour, adjusted_hour,
                )
                seed_hour = adjusted_hour
                peak_source = "history"
        except Exception as e:
            logger.debug("[%s] History adjustment skipped: %s", slug, e)

    # ── 3. Snap to HH:00 ± spread_min, respecting warmup ──
    target_utc, target_local = _snap_to_next_target_hour(
        seed_hour, timezone_str, warmup_min, slug,
        spread_min=publish_window_spread_min,
    )

    # ── 4. Avoid same-channel publish time collisions ──
    target_utc = _avoid_channel_collision(
        channel_id, target_utc, db=db, slug=slug,
    )
    # ── v10.3: Cross-channel collision (30 min gap) ──
    target_utc = _avoid_channel_collision(
        channel_id, target_utc, db=db, slug=slug, cross_channel=True,
    )

    logger.info(
        "[%s] Scheduled publish: niche=%s, peak=%02d:00 (local), "
        "source=%s, target=%s UTC",
        slug, niche, seed_hour, peak_source,
        target_utc.isoformat(),
    )

    return {
        "target_public_at": target_utc.isoformat(),
        "target_public_at_local": target_local.isoformat(),
        "peak_hour_local": seed_hour,
        "peak_source": peak_source,
        "niche": niche,
        "jitter_applied": 0,
        "warmup_min": warmup_min,
    }


def _adjust_from_history(db, channel_id: int, seed_hour: int,
                          timezone_str: str) -> Optional[int]:
    """Ajusta la hora semilla usando el rendimiento histórico del canal.

    Analiza los stats del canal para encontrar la franja horaria con mejor
    rendimiento relativo (CTR, views en primeras 24h) respecto a la media.

    Returns:
        Hora ajustada (int) o None si no hay suficientes datos.
    """
    try:
        tz = pytz.timezone(timezone_str)
    except pytz.UnknownTimeZoneError:
        tz = pytz.UTC

    # Obtener videos publicados en los últimos 90 días con stats
    import sqlite3
    conn = None
    try:
        # Get videos for this channel with their upload times and stats
        if hasattr(db, '_connect'):
            conn = db._connect()
        elif hasattr(db, 'get_videos'):
            # Use the public API
            videos = db.get_videos(channel_id=channel_id, limit=200)
        else:
            return None

        if conn:
            rows = conn.execute("""
                SELECT v.id, v.uploaded_at, v.published_at, v.status,
                       vs.views, vs.likes, vs.comments
                FROM videos v
                LEFT JOIN video_stats_history vs ON vs.video_id = v.id
                    AND vs.fetched_at = (
                        SELECT MIN(fetched_at)
                        FROM video_stats_history vs2
                        WHERE vs2.video_id = v.id
                        AND vs2.fetched_at > datetime(v.uploaded_at, '+18 hours')
                    )
                WHERE v.channel_id = ?
                  AND (v.status IN ('uploaded', 'published'))
                  AND v.uploaded_at IS NOT NULL
                  AND v.uploaded_at > datetime('now', '-90 days')
                ORDER BY v.uploaded_at DESC
                LIMIT 100
            """, (channel_id,)).fetchall()
        else:
            return None

        if not rows or len(rows) < HISTORY_MIN_DATA_POINTS:
            return None

        # Agrupar por franja de 3 horas y calcular CTR medio
        hour_buckets = {}  # {hour_bucket: [ctr_values]}
        for row in rows:
            if not row["uploaded_at"]:
                continue

            # Parse upload time
            try:
                # uploaded_at could be a string or datetime
                if isinstance(row["uploaded_at"], str):
                    upload_dt = datetime.fromisoformat(row["uploaded_at"].replace("Z", "+00:00"))
                else:
                    upload_dt = row["uploaded_at"]

                if upload_dt.tzinfo is None:
                    upload_dt = upload_dt.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue

            local_dt = upload_dt.astimezone(tz)
            hour = local_dt.hour

            # Bucket: 3-hour windows
            bucket = (hour // 3) * 3

            views = row["views"] or 0
            likes = row["likes"] or 0
            if views > 0:
                # Engagement proxy: likes per 1000 views
                ctr_proxy = (likes / views) * 1000
                if bucket not in hour_buckets:
                    hour_buckets[bucket] = []
                hour_buckets[bucket].append(ctr_proxy)

        if not hour_buckets:
            return None

        # Encontrar la franja con mejor engagement medio
        best_bucket = None
        best_engagement = 0
        bucket_avgs = {}
        for bucket, values in hour_buckets.items():
            avg = sum(values) / len(values)
            bucket_avgs[bucket] = avg
            if avg > best_engagement:
                best_engagement = avg
                best_bucket = bucket

        if best_bucket is None:
            return None

        # El mejor bucket representa 3 horas. Tomamos el punto medio.
        adjusted_hour = best_bucket + 1  # +1 = punto medio de la franja de 3h

        # Limitar el desplazamiento respecto a la heurística
        if abs(adjusted_hour - seed_hour) > HISTORY_SHIFT_MAX_HOURS:
            logger.debug(
                "History adjustment %d→%d exceeds max shift %.1fh — clamping",
                seed_hour, adjusted_hour, HISTORY_SHIFT_MAX_HOURS,
            )
            if adjusted_hour > seed_hour:
                adjusted_hour = seed_hour + int(HISTORY_SHIFT_MAX_HOURS)
            else:
                adjusted_hour = seed_hour - int(HISTORY_SHIFT_MAX_HOURS)

        logger.debug(
            "History adjustment: best bucket %dh-%.1f engagement, → adjusted_hour=%d",
            best_bucket, best_engagement, adjusted_hour,
        )

        return int(adjusted_hour)

    except Exception as e:
        logger.debug("History adjustment failed: %s", e)
        return None
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _parse_target_public_at(target_public_at: str, timezone_str: str = "Europe/Madrid") -> Optional[datetime]:
    """Parse a target_public_at string into a timezone-aware UTC datetime.

    Handles multiple formats:
      - ISO8601 with TZ: '2026-07-24T19:07:00+00:00'
      - ISO8601 with Z:   '2026-07-24T19:07:00Z'
      - Naive local:      '2026-07-24 21:07:00' (interpreted as the channel's timezone)
      - Naive T-separated: '2026-07-24T21:07:00' (interpreted as the channel's timezone)

    Returns None if parsing fails.
    """
    if not target_public_at:
        return None

    raw = str(target_public_at).strip()

    # Try ISO8601 with timezone info (contains +, Z, or explicit offset)
    for fmt, is_utc in [
        ("%Y-%m-%dT%H:%M:%S%z", False),
        ("%Y-%m-%dT%H:%M:%S+00:00", False),
    ]:
        try:
            from datetime import datetime as _dt, timezone as _tz
            if raw.endswith("Z") or raw.endswith("z"):
                dt = _dt.fromisoformat(raw.replace("z", "Z").replace("Z", "+00:00"))
            elif "+" in raw[10:] or raw.count("-") > 2:
                # Contains timezone offset — parse directly
                dt = _dt.fromisoformat(raw.replace(" ", "T"))
            else:
                # Naive: interpret as channel's timezone
                dt = _dt.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
                try:
                    tz = pytz.timezone(timezone_str)
                except pytz.UnknownTimeZoneError:
                    tz = pytz.UTC
                dt = tz.localize(dt)

            if dt.tzinfo is None:
                try:
                    tz = pytz.timezone(timezone_str)
                except pytz.UnknownTimeZoneError:
                    tz = pytz.UTC
                dt = tz.localize(dt)

            return dt.astimezone(timezone.utc)
        except (ValueError, TypeError):
            continue

    # Last resort: try fromisoformat
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00").replace(" ", "T"))
        if dt.tzinfo is None:
            try:
                tz = pytz.timezone(timezone_str)
            except pytz.UnknownTimeZoneError:
                tz = pytz.UTC
            dt = tz.localize(dt)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        pass

    return None


def _target_is_stale(target_public_at: Optional[str],
                     timezone_str: str = "Europe/Madrid",
                     warmup_min: int = 120) -> bool:
    """Check if a target_public_at is already in the past (stale).

    Returns True if the target is before now_utc (+ warmup buffer).
    Returns False if the target is sufficiently in the future, or None.

    v (Aug 2026): warmup_min is now honored. Previously it was accepted but
    ignored, so a target only minutes away was treated as "not stale" and the
    video uploaded with a publishAt with no YouTube processing margin. Now a
    target within warmup_min of now triggers recalculation.
    """
    if not target_public_at:
        return True  # No target at all → needs recalculation

    parsed = _parse_target_public_at(target_public_at, timezone_str)
    if parsed is None:
        return True  # Can't parse → treat as stale

    now_utc = datetime.now(timezone.utc)
    if warmup_min and warmup_min > 0:
        return parsed < now_utc + timedelta(minutes=warmup_min)
    return parsed < now_utc


def ensure_future_target_public_at(
    target_public_at: Optional[str],
    slug: str,
    timezone_str: str = "Europe/Madrid",
    primary_keyword: str = "",
    secondary_keywords: list[str] = None,
    target_hour: Optional[int] = None,
    jitter_min: int = 0,
    warmup_min: int = 60,
    publish_window_spread_min: int = 0,
    db=None,
    channel_id: Optional[int] = None,
) -> str:
    """Validate that target_public_at is in the future and collision-free.

    This is the single guard function that should be called before any upload
    or scheduling operation to ensure the target_public_at is never in the past
    and never collides with another video on the same channel (v23).

    Args:
        target_public_at: Current target (can be None, ISO8601 UTC, or naive local).
        slug: Channel slug for logging + recalculation.
        timezone_str: Channel timezone (for parsing naive strings).
        primary_keyword, secondary_keywords, target_hour, jitter_min, warmup_min,
            publish_window_spread_min:
            Parameters forwarded to calculate_target_public_time() if recalc needed.
        db: Database instance for history-based recalculation.
        channel_id: Channel DB ID for history lookup.

    Returns:
        ISO8601 UTC string guaranteed to be in the future and collision-free.
    """
    needs_recalc = _target_is_stale(target_public_at, timezone_str, warmup_min)

    if not needs_recalc:
        # Already valid — parse and return as ISO8601 UTC
        parsed = _parse_target_public_at(target_public_at, timezone_str)
        if parsed is not None:
            result_iso = parsed.isoformat()

            # ── v23: Check for collisions even when target is in the future ──
            if db is not None and channel_id is not None:
                try:
                    adjusted_utc = _avoid_channel_collision(
                        channel_id, parsed, db=db, slug=slug,
                    )
                    adjusted_iso = adjusted_utc.isoformat()
                    if adjusted_iso != result_iso:
                        logger.info(
                            "[%s] Collision detected even though target is in the future. "
                            "Adjusted: %s → %s",
                            slug, str(target_public_at)[:19] if target_public_at else "None",
                            str(adjusted_iso)[:19],
                        )
                        return adjusted_iso
                except Exception as e:
                    logger.debug("[%s] Collision check skipped: %s", slug, e)

            return result_iso

    # ── Stale or None → recalculate ──
    logger.info(
        "[%s] Target_public_at is stale or missing (%s). Recalculating...",
        slug, (str(target_public_at)[:19] if target_public_at else "None"),
    )

    result = calculate_target_public_time(
        slug=slug,
        primary_keyword=primary_keyword,
        secondary_keywords=secondary_keywords or [],
        timezone_str=timezone_str,
        target_hour=target_hour,
        jitter_min=jitter_min,
        warmup_min=warmup_min,
        publish_window_spread_min=publish_window_spread_min,
        db=db,
        channel_id=channel_id,
    )

    new_target = result["target_public_at"]
    logger.info(
        "[%s] Recalculated target_public_at: %s → %s",
        slug,
        (str(target_public_at)[:19] if target_public_at else "None"),
        new_target,
    )
    return new_target


def planned_target_is_off_peak(
    target_public_at: str,
    channel_id: int,
    db,
    timezone_str: str = "Europe/Madrid",
    tolerance_hours: int = 1,
) -> bool:
    """Return True if target_public_at's local hour is far from all optimal slots.

    Belt-and-suspenders for the uploader: a planning-provided target seeded by
    the niche heuristic (before data-driven optimal slots existed, or when they
    were stale) can land on off-peak hours (e.g. midnight). If optimal slots
    exist and the target hour is more than ``tolerance_hours`` from every
    optimal hour, the caller should recalculate via calculate_target_public_time
    (which prefers optimal slots). Returns False when there is nothing to
    compare against (no db / no optimal slots / parse ok), so behaviour is
    unchanged in those cases.
    """
    if db is None or channel_id is None:
        return False
    try:
        slots = db.get_optimal_slots(channel_id, "long")
    except Exception:
        return False
    if not slots:
        return False

    parsed = _parse_target_public_at(target_public_at, timezone_str)
    if parsed is None:
        return True  # unparseable → let caller recalculate

    try:
        tz = pytz.timezone(timezone_str)
    except pytz.UnknownTimeZoneError:
        tz = pytz.UTC
    local_h = parsed.astimezone(tz).hour

    for s in slots:
        opt_h = int(s.get("target_hour", 0) or 0)
        if abs(local_h - opt_h) <= tolerance_hours:
            return False
    return True


def clamp_max_ahead_target_public_at(
    target_public_at: Optional[str],
    slug: str,
    timezone_str: str = "Europe/Madrid",
    warmup_min: int = 60,
    max_ahead_hours: int = MAX_PUBLISH_AT_AHEAD_HOURS,
    db=None,
    channel_id: Optional[int] = None,
    primary_keyword: str = "",
    secondary_keywords: list[str] = None,
    target_hour: Optional[int] = None,
    publish_window_spread_min: int = 0,
) -> str:
    """Clamp a far-future target_public_at to at most max_ahead_hours ahead.

    Espejo del guard de "staleness" (`ensure_future_target_public_at`) pero para
    el otro extremo: si el target está demasiado lejano (p. ej. un slot del
    horizonte de planning a días vista), se recalcula al siguiente pico de
    publicación vía `calculate_target_public_time`, de modo que el vídeo nunca
    espera más de `max_ahead_hours` en "calentando" (uploaded_private).

    Reglas:
    - None o inparseable → recalc (seguro).
    - target <= now + max_ahead_hours → se devuelve tal cual.
    - target > now + max_ahead_hours → recalc al siguiente pico (respeta warmup
      y evita colisiones 3h mismo canal / 30min cross-channel).

    Returns:
        ISO8601 UTC string del target_public_at efectivo (clampado o no).
    """
    if not target_public_at:
        logger.info("[%s] target_public_at is empty — nothing to clamp", slug)
        return target_public_at

    parsed = _parse_target_public_at(target_public_at, timezone_str)
    if parsed is None:
        logger.info("[%s] target_public_at unparseable (%s) — recalculating",
                    slug, str(target_public_at)[:19])
        return _recalc_clamped_target(
            slug=slug, timezone_str=timezone_str, warmup_min=warmup_min,
            max_ahead_hours=max_ahead_hours, db=db, channel_id=channel_id,
            primary_keyword=primary_keyword,
            secondary_keywords=secondary_keywords or [],
            target_hour=target_hour,
            publish_window_spread_min=publish_window_spread_min,
            old_target=str(target_public_at),
        )

    now_utc = datetime.now(timezone.utc)
    horizon = now_utc + timedelta(hours=max_ahead_hours)

    if parsed <= horizon:
        logger.debug(
            "[%s] target_public_at %s within %dh — no clamp needed",
            slug, parsed.isoformat(), max_ahead_hours,
        )
        return target_public_at

    logger.info(
        "[%s] target_public_at %s is >%dh ahead — clamping to next peak",
        slug, parsed.isoformat(), max_ahead_hours,
    )
    return _recalc_clamped_target(
        slug=slug, timezone_str=timezone_str, warmup_min=warmup_min,
        max_ahead_hours=max_ahead_hours, db=db, channel_id=channel_id,
        primary_keyword=primary_keyword,
        secondary_keywords=secondary_keywords or [],
        target_hour=target_hour,
        publish_window_spread_min=publish_window_spread_min,
        old_target=str(target_public_at),
    )


def _recalc_clamped_target(
    slug: str,
    timezone_str: str,
    warmup_min: int,
    max_ahead_hours: int,
    db,
    channel_id: Optional[int],
    primary_keyword: str,
    secondary_keywords: list[str],
    target_hour: Optional[int],
    publish_window_spread_min: int,
    old_target: str,
) -> str:
    """Recalcula el target al siguiente pico (usado por clamp_max_ahead_...).

    Con CAP duro: si la resolución de colisiones empuja el resultado más allá
    de now + max_ahead_hours (backlog denso), se recorta a now + max_ahead_hours.
    Así el clamp CONVERGE siempre: el resultado nunca supera el margen y un
    vídeo ya clampado no vuelve a entrar en el scan. El posible solapamiento
    residual en momentos de backlog extremo es el precio de no esperar días;
    los guards de overlap del sistema lo suavizan cuando el backlog se drena.
    """
    result = calculate_target_public_time(
        slug=slug,
        primary_keyword=primary_keyword,
        secondary_keywords=secondary_keywords,
        timezone_str=timezone_str,
        target_hour=target_hour,
        warmup_min=warmup_min,
        publish_window_spread_min=publish_window_spread_min,
        db=db,
        channel_id=channel_id,
    )
    new_target = result["target_public_at"]
    parsed = _parse_target_public_at(new_target, timezone_str)
    if parsed is not None:
        now_utc = datetime.now(timezone.utc)
        # ── Seguridad: cap amplio (5 días) SOLO para evitar resultados patológicos.
        # (ago 2026) Se eliminó el cap duro de 24h: apilaba muchos vídeos del
        # mismo canal en el mismo instante cuando el backlog era denso. Ahora la
        # resolución de colisiones estira la publicación con gaps de 3h, aunque
        # supere el umbral de 24h (trigger). El repack dedicado
        # (repack_channel_publish_times) garantiza el espaciado >= gap_hours.
        safety_cap = now_utc + timedelta(hours=MAX_PUBLISH_AHEAD_SAFETY_HOURS)
        if parsed > safety_cap:
            capped = safety_cap.isoformat()
            logger.warning(
                "[%s] Clamp safety cap: collision resolution pushed beyond %dh (%s) — "
                "recortando a %s",
                slug, MAX_PUBLISH_AHEAD_SAFETY_HOURS, new_target[:19], capped[:19],
            )
            new_target = capped
    logger.info(
        "[%s] Clamp far-future target_public_at: %s → %s (peak=%02d:%02d, src=%s)",
        slug, old_target[:19], new_target[:19],
        result["peak_hour_local"], 0, result["peak_source"],
    )
    return new_target


def repack_channel_publish_times(
    db,
    channel_id: int,
    slug: str,
    timezone_str: str = "Europe/Madrid",
    warmup_min: int = 60,
    gap_hours: int = SAME_CHANNEL_PUBLISH_GAP_HOURS,
    safety_ahead_hours: int = MAX_PUBLISH_AHEAD_SAFETY_HOURS,
) -> list[dict]:
    """Repack ALL pending publish times of a channel with >= gap_hours spacing.

    (ago 2026) Sustituye al enfoque incremental (clamp por vídeo): el clamp con
    cap duro apilaba muchos vídeos del mismo canal en el mismo instante cuando el
    backlog era denso (p. ej. 8 vídeos a las 10:48). Este repack recalcula TODAS
    las publicaciones pendientes del canal en una sola pasada:

    - Candidatos: vídeos con compromiso de publicación:
      * uploaded_private / warming / scheduled → ya subidos a YouTube (private).
      * awaiting_upload / ready → generados, listos para subir.
    - Orden: cronológico por (uploaded_at, scheduled_upload_at, created_at) —
      los ya subidos/listos primero, "después de los que hay subidos y listos".
    - Walk: cursor arranca en el siguiente pico (>= now + warmup) y avanza en
      pasos de gap_hours. Cada vídeo recibe el cursor; para awaiting_upload se
      garantiza publish >= scheduled_upload_at + warmup + buffer 60min.
    - NUNCA escribe en DB: devuelve la lista de cambios propuestos.

    Returns:
        list[dict] con {video_id, status, yt_video_id, old_target, new_target,
        requires_yt_update, adjusted_upload_at}
    """
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    # ── 1. Candidatos ──
    try:
        with db._connect() as conn:
            rows = conn.execute(
                """SELECT v.id, v.status, v.yt_video_id, v.target_public_at,
                          v.scheduled_upload_at, v.uploaded_at, v.created_at
                   FROM videos v
                   WHERE v.channel_id = ?
                     AND v.status IN ('uploaded_private','warming','scheduled',
                                      'awaiting_upload','ready')
                     AND v.publish_mode = 'scheduled'
                   ORDER BY COALESCE(v.uploaded_at, v.scheduled_upload_at, v.created_at)
                """, (channel_id,),
            ).fetchall()
    except Exception as exc:
        logger.debug("[%s] repack scan skipped: %s", slug, exc)
        return []

    if not rows:
        logger.info("[%s] repack: no hay publicaciones pendientes", slug)
        return []

    # ── 2. Config de pico para el primer slot ──
    try:
        ch = db.get_channel(channel_id)
        cfg = {}
        if ch and ch.get("config_json"):
            import json as _json
            cfg = _json.loads(ch["config_json"] or "{}")
    except Exception:
        cfg = {}
    tz_str = cfg.get("PUBLISH_TIMEZONE", timezone_str)
    warmup = int(cfg.get("PUBLISH_WARMUP_MIN", warmup_min) or warmup_min)
    target_hour = cfg.get("PUBLISH_TARGET_HOUR")

    peak_info = get_channel_peak_info(
        {k: v for k, v in cfg.items()} if cfg else {"SEO_PRIMARY_KEYWORD": ""}
    )
    peak_hour = int(target_hour) if target_hour is not None else peak_info["peak_hour"]

    # ── 3. Walk ──
    now_utc = _dt.now(_tz.utc)
    floor = now_utc + _td(minutes=warmup)
    try:
        tz = pytz.timezone(tz_str)
    except pytz.UnknownTimeZoneError:
        tz = pytz.UTC
    # Primer slot: siguiente pico HH:00 (>= floor)
    local_floor = floor.astimezone(tz)
    cursor_local = local_floor.replace(hour=peak_hour, minute=0, second=0, microsecond=0)
    if cursor_local < local_floor:
        cursor_local += _td(days=1)
    cursor_utc = cursor_local.astimezone(_tz.utc)

    safety_limit = now_utc + _td(hours=safety_ahead_hours)
    plan = []
    for row in rows:
        video_id = row["id"]
        status = row["status"]
        yt_id = row["yt_video_id"] or ""

        slot = cursor_utc
        # awaiting_upload/ready: publish despues de upload + warmup + buffer 60min
        adjusted_upload_at = None
        up_raw = row["scheduled_upload_at"]
        if status in ("awaiting_upload", "ready") and up_raw:
            up_dt = _parse_target_public_at(str(up_raw), tz_str)
            if up_dt is not None:
                min_public = up_dt + _td(minutes=warmup + 60)
                if slot < min_public:
                    slot = min_public
            # La subida debe caber: ajustar scheduled_upload_at si se pasa
            if slot > now_utc + _td(minutes=warmup):
                candidate_up = slot - _td(minutes=warmup + 60)
                if candidate_up > now_utc and (up_dt is None or candidate_up < up_dt):
                    adjusted_upload_at = candidate_up.strftime("%Y-%m-%d %H:%M:%S")

        if slot > safety_limit:
            logger.warning(
                "[%s] repack: safety bound %dh alcanzado para #%d — se recorta",
                slug, safety_ahead_hours, video_id,
            )
            slot = safety_limit

        old_target = str(row["target_public_at"]) if row["target_public_at"] else ""
        requires_yt = status in ("uploaded_private", "warming", "scheduled") and bool(yt_id)

        plan.append({
            "video_id": video_id,
            "status": status,
            "yt_video_id": yt_id,
            "old_target": old_target,
            "new_target": slot.isoformat(),
            "requires_yt_update": requires_yt,
            "adjusted_upload_at": adjusted_upload_at,
        })
        cursor_utc = slot + _td(hours=gap_hours)

    logger.info(
        "[%s] repack: %d vídeo(s) planificados (gap=%dh, pico=%02d:00 %s)",
        slug, len(plan), gap_hours, peak_hour, tz_str,
    )
    return plan
