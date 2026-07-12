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
        "secondary_peaks": [0, 14, 17],  # madrugada (insomnes), sobremesa, tarde-noche
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


def calculate_target_public_time(
    slug: str,
    primary_keyword: str = "",
    secondary_keywords: list[str] = None,
    timezone_str: str = "Europe/Madrid",
    target_hour: Optional[int] = None,
    jitter_min: int = 20,
    warmup_min: int = 120,
    db=None,
    channel_id: Optional[int] = None,
) -> dict:
    """Calcula la hora objetivo de publicación para un vídeo.

    Args:
        slug: Canal slug
        primary_keyword: Keyword SEO primaria
        secondary_keywords: Keywords SEO secundarias
        timezone_str: Zona horaria del canal (ej: 'Europe/Madrid')
        target_hour: Hora semilla (de config). Si es None, se usa la heurística.
        jitter_min: ±N minutos de variación aleatoria
        warmup_min: Minutos mínimos en 'private' antes de publicar
        db: Database para consultar histórico
        channel_id: ID del canal en BD para consultar histórico

    Returns:
        {
            "target_public_at": str (ISO8601 UTC),
            "peak_hour_local": int,
            "peak_source": "heuristic" | "history",
            "niche": str,
            "jitter_applied": int (minutos, positivo o negativo),
            "warmup_until": str (ISO8601 UTC) — momento en que acaba el warmup,
        }
    """
    all_keywords = [primary_keyword] if primary_keyword else []
    if secondary_keywords:
        all_keywords.extend(secondary_keywords)

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

    # ── 3. Aplicar jitter aleatorio ──
    jitter = random.randint(-jitter_min, jitter_min)
    effective_hour = seed_hour + (jitter / 60.0)  # hora decimal

    # ── 4. Determinar la próxima ocurrencia de esa hora en la zona local ──
    try:
        tz = pytz.timezone(timezone_str)
    except pytz.UnknownTimeZoneError:
        logger.warning("[%s] Unknown timezone '%s', falling back to UTC", slug, timezone_str)
        tz = pytz.UTC

    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tz)

    # Siguiente ocurrencia de la hora objetivo HOY (en hora local)
    hour_int = int(effective_hour)
    minute_int = int((effective_hour - hour_int) * 60)

    target_local = now_local.replace(
        hour=hour_int, minute=minute_int, second=0, microsecond=0,
    )

    # Si ya pasó hoy, mover a mañana
    if target_local <= now_local:
        target_local += timedelta(days=1)

    # ── 5. Asegurar warmup mínimo ──
    # El warmup empieza DESDE AHORA (cuando se sube el vídeo).
    # La hora de publicación no puede ser antes de now + warmup_min.
    warmup_until_utc = now_utc + timedelta(minutes=warmup_min)
    target_utc = target_local.astimezone(pytz.UTC)

    if target_utc < warmup_until_utc:
        logger.info(
            "[%s] Target time (%s UTC) is before warmup end (%s UTC). "
            "Pushing to warmup end.",
            slug, target_utc.isoformat(), warmup_until_utc.isoformat(),
        )
        target_utc = warmup_until_utc

    logger.info(
        "[%s] Scheduled publish: niche=%s, peak=%02d:%02d (local), "
        "source=%s, jitter=%+dmin, target=%s UTC",
        slug, niche, hour_int, minute_int, peak_source, jitter,
        target_utc.isoformat(),
    )

    return {
        "target_public_at": target_utc.isoformat(),
        "peak_hour_local": seed_hour,
        "peak_source": peak_source,
        "niche": niche,
        "jitter_applied": jitter,
        "warmup_until": warmup_until_utc.isoformat(),
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
