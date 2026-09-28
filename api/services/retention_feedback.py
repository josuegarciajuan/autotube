"""Bucle de retención (Fase 3 del experimento de recuperación de alcance).

Histórico → señal → directiva → prompt. Lee las curvas de retención que recoge
la recolección profunda (``audienceWatchRatio`` por ``elapsedVideoTimeRatio``),
las mapea a las fases narrativas del guion (``phase_id`` de ``scene_ranges``) y
deriva una directiva compacta que se inyecta al generar el siguiente long-form.

- Solo lectura de la DB + caché en ``system_state`` (cuota 0, sin red).
- Fail-open: sin datos suficientes devuelve ``None`` y el prompt usa la config.
- Kill-switch: ``RETENTION_FEEDBACK_ENABLED=False`` o
  ``system_state["retention_feedback_disabled"]="true"``.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger("autotube.retention_feedback")

STATE_PREFIX = "retention_feedback_"
STARTED_KEY = "retention_feedback_started_at"
DISABLED_KEY = "retention_feedback_disabled"

_DISABLED_VALUES = {"1", "true", "yes", "on"}
_TREND_EPS = 0.02  # 2 puntos porcentuales de watch-ratio para considerar tendencia


# ─────────────────────────────────────────────────────────────────────
# Config / kill-switch
# ─────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_cfg(slug: str):
    try:
        from config.config_bridge import get_channel_config
        return get_channel_config(slug)
    except Exception as exc:  # noqa: BLE001
        logger.debug("retention_feedback: config load failed for %s: %s", slug, exc)
        return None


def _is_disabled(db, cfg) -> bool:
    if cfg is not None and not getattr(cfg, "RETENTION_FEEDBACK_ENABLED", True):
        return True
    try:
        return str(db.get_system_state(DISABLED_KEY) or "").strip().lower() in _DISABLED_VALUES
    except Exception:  # noqa: BLE001
        return False


def _age_hours(iso_ts: str | None) -> float | None:
    if not iso_ts:
        return None
    try:
        ts = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0
    except (ValueError, TypeError):
        return None


# ─────────────────────────────────────────────────────────────────────
# Beat timeline (curva → fase narrativa)
# ─────────────────────────────────────────────────────────────────────

def _timeline_from_scene_ranges(scene_ranges) -> list[dict] | None:
    tl = []
    for r in scene_ranges or []:
        if not isinstance(r, dict):
            continue
        try:
            start = float(r.get("start", 0))
            end = float(r.get("end", 0))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        tl.append({
            "phase_id": r.get("phase_id") or "default",
            "start": start,
            "end": end,
        })
    return tl or None


def _timeline_from_blocks(bloques, duration: float) -> list[dict] | None:
    """Fallback: timeline proporcional al reparto de palabras por bloque."""
    phases = []
    for b in bloques or []:
        if not isinstance(b, dict):
            continue
        pid = b.get("phase_id") or "default"
        words = len(str(b.get("texto", "")).split()) or 1
        phases.append((pid, words))
    total = sum(w for _, w in phases)
    if total <= 0 or not phases:
        return None
    tl = []
    cursor = 0.0
    for pid, words in phases:
        seg = duration * (words / total)
        tl.append({"phase_id": pid, "start": cursor, "end": cursor + seg})
        cursor += seg
    return tl


def build_beat_timeline(db, video: dict) -> list[dict] | None:
    """Return ``[{phase_id, start, end}]`` for a video, or None.

    Prefers the real ``scene_ranges`` persisted in the media checkpoint; falls
    back to a proportional reconstruction from ``scripts.bloques_json``.
    """
    duration = float(video.get("duracion_seg") or 0)
    if duration <= 0:
        return None

    cp = video.get("checkpoint_data")
    if isinstance(cp, str):
        try:
            cp = json.loads(cp)
        except (json.JSONDecodeError, TypeError):
            cp = None
    scene_ranges = None
    if isinstance(cp, dict):
        media = cp.get("media")
        if isinstance(media, dict):
            scene_ranges = media.get("scene_ranges")

    tl = _timeline_from_scene_ranges(scene_ranges)
    if tl:
        return tl

    sid = video.get("script_id")
    if not sid:
        return None
    try:
        script = db.get_script(sid)
    except Exception:  # noqa: BLE001
        script = None
    if not script:
        return None
    bloques = script.get("bloques_json")
    if isinstance(bloques, str):
        try:
            bloques = json.loads(bloques)
        except (json.JSONDecodeError, TypeError):
            bloques = None
    return _timeline_from_blocks(bloques, duration)


def _phase_at(timeline: list[dict], t: float) -> str:
    for r in timeline:
        if r["start"] <= t < r["end"]:
            return r["phase_id"]
    return timeline[-1]["phase_id"] if timeline else "default"


def _curve_by_phase(curve: list[dict], timeline: list[dict],
                    duration: float) -> dict[str, float]:
    acc: dict[str, dict] = {}
    for p in curve:
        try:
            t = float(p["elapsed"]) * duration
            val = float(p["watch_ratio"])
        except (KeyError, TypeError, ValueError):
            continue
        pid = _phase_at(timeline, t)
        d = acc.setdefault(pid, {"sum": 0.0, "n": 0})
        d["sum"] += val
        d["n"] += 1
    return {pid: d["sum"] / d["n"] for pid, d in acc.items() if d["n"] > 0}


# ─────────────────────────────────────────────────────────────────────
# Queries
# ─────────────────────────────────────────────────────────────────────

def _videos_with_curves(db, channel_id: int, since_days: int,
                        limit: int) -> list[dict]:
    conn = None
    try:
        conn = db._connect()
    except Exception:  # noqa: BLE001
        return []
    try:
        rows = conn.execute(
            """SELECT v.id, v.script_id, v.duracion_seg,
                      v.published_at, v.uploaded_at, v.checkpoint_data
               FROM videos v
               WHERE v.channel_id = ?
                 AND EXISTS (SELECT 1 FROM video_analytics_detailed d
                             WHERE d.video_id = v.id
                               AND d.report_type = 'audience_retention')
                 AND COALESCE(v.published_at, v.uploaded_at)
                     >= datetime('now', ?)
               ORDER BY COALESCE(v.published_at, v.uploaded_at) DESC
               LIMIT ?""",
            (channel_id, f"-{int(since_days)} days", int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        logger.debug("retention_feedback: video query failed: %s", exc)
        return []
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _phase_labels(cfg, phase_ids) -> dict[str, str]:
    labels = {}
    for p in (getattr(cfg, "SCRIPT_STRUCTURE", []) or []):
        if isinstance(p, dict) and p.get("id"):
            labels[p["id"]] = p.get("step") or p["id"]
    return {pid: labels.get(pid, pid) for pid in phase_ids}


def _build_directive(overall_pct: float, target: float, trend: str,
                     weak: list[dict]) -> str:
    if overall_pct >= target:
        return (
            f"Retencion media reciente {overall_pct}% (objetivo {target}%). "
            "Manten la estructura actual y no relajes el ritmo ni alargues los "
            "bloques sin un hecho nuevo."
        )
    parts = [f"RETENCION MEDIA RECIENTE {overall_pct}% < OBJETIVO {target}%."]
    if trend == "down":
        parts.append("La tendencia de los ultimos videos es a la baja.")
    elif trend == "up":
        parts.append("La tendencia es al alza: consolida lo que funciona.")
    if weak:
        labels = ", ".join(
            f"{w['label']} ({w['watch_ratio_pct']}%)" for w in weak
        )
        parts.append(f"Las fases que mas audiencia pierden son: {labels}.")
        parts.append(
            "Refuerza esas fases: cliffhanger antes de su ecuador, sin relleno, "
            "y acortalas si no aportan un hecho nuevo."
        )
    parts.append(
        "Manten la promesa explicita del payoff en los primeros 90 segundos y "
        "un reset (recapitulacion corta) hacia la mitad."
    )
    return " ".join(parts)


# ─────────────────────────────────────────────────────────────────────
# Señal por canal
# ─────────────────────────────────────────────────────────────────────

def compute_channel_signal(db, channel: dict, cfg=None,
                           persist: bool = True) -> dict | None:
    """Compute the retention signal for one channel.

    ``channel`` must contain ``id`` and ``slug``. Returns a dict or None when
    disabled / insufficient data (fail-open).
    """
    channel_id = channel.get("id")
    slug = channel.get("slug")
    if not channel_id or not slug:
        return None
    if cfg is None:
        cfg = _load_cfg(slug)
    if _is_disabled(db, cfg):
        return None

    max_v = int(getattr(cfg, "RETENTION_CURVE_MAX_VIDEOS", 20) or 20)
    lookback = int(getattr(cfg, "RETENTION_LOOKBACK_DAYS", 90) or 90)
    min_v = int(getattr(cfg, "RETENTION_CURVE_MIN_VIDEOS", 3) or 3)
    max_phases = int(getattr(cfg, "RETENTION_FOCUS_MAX_PHASES", 2) or 2)
    target = float(getattr(cfg, "RETENTION_TARGET_PCT", 40.0) or 40.0)

    videos = _videos_with_curves(db, channel_id, lookback, max_v)
    if len(videos) < min_v:
        return None

    phase_acc: dict[str, list[float]] = {}
    all_points: list[float] = []
    video_areas: list[tuple[str, float]] = []
    used = 0

    for v in videos:
        tl = build_beat_timeline(db, v)
        if not tl:
            continue
        try:
            curve = db.get_video_retention_curve(v["id"])
        except Exception:  # noqa: BLE001
            curve = []
        if not curve:
            continue

        duration = float(v.get("duracion_seg") or 0)
        used += 1
        for pid, val in _curve_by_phase(curve, tl, duration).items():
            phase_acc.setdefault(pid, []).append(val)
        area = sum(float(p["watch_ratio"]) for p in curve) / len(curve)
        all_points.extend(float(p["watch_ratio"]) for p in curve)
        video_areas.append(
            (str(v.get("published_at") or v.get("uploaded_at") or ""), area)
        )

    if used < min_v or not phase_acc:
        return None

    phase_avg = {
        pid: sum(vals) / len(vals) for pid, vals in phase_acc.items() if vals
    }
    if not phase_avg:
        return None

    overall = sum(all_points) / len(all_points) if all_points else 0.0
    overall_pct = round(overall * 100, 1)

    # Trend: media de la curva (area) reciente vs antigua.
    trend = "flat"
    video_areas.sort(key=lambda x: x[0])
    if len(video_areas) >= 4:
        half = len(video_areas) // 2
        older = sum(a for _, a in video_areas[:half]) / half
        recent = sum(a for _, a in video_areas[half:]) / (len(video_areas) - half)
        if recent - older > _TREND_EPS:
            trend = "up"
        elif older - recent > _TREND_EPS:
            trend = "down"

    # Prioriza fases REALES del canal (SCRIPT_STRUCTURE). La pseudo-fase
    # "default" solo aparece en vídeos anteriores sin phase_id; citarla en la
    # directiva sería confuso, así que se omite salvo que no haya ninguna real.
    known = {
        p.get("id") for p in (getattr(cfg, "SCRIPT_STRUCTURE", []) or [])
        if isinstance(p, dict) and p.get("id")
    }
    ranked = sorted(phase_avg.items(), key=lambda kv: kv[1])
    ranked_known = [(pid, val) for pid, val in ranked if pid in known]
    ranked_sel = (ranked_known if ranked_known else ranked)[:max_phases]

    labels = _phase_labels(cfg, [pid for pid, _ in ranked_sel])
    weak = [
        {
            "phase_id": pid,
            "label": labels.get(pid, pid),
            "watch_ratio_pct": round(val * 100, 1),
        }
        for pid, val in ranked_sel
    ]

    signal = {
        "slug": slug,
        "channel_id": channel_id,
        "generated_at": _now_iso(),
        "videos_analyzed": used,
        "retention_pct": overall_pct,
        "target_pct": target,
        "gap_pp": round(target - overall_pct, 1),
        "trend": trend,
        "phase_watch_ratio_pct": {
            pid: round(val * 100, 1) for pid, val in phase_avg.items()
        },
        "weak_phases": weak,
        "directive": _build_directive(overall_pct, target, trend, weak),
    }

    if persist:
        try:
            db.set_system_state(
                STATE_PREFIX + slug, json.dumps(signal, ensure_ascii=False)
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("retention_feedback: cache save failed for %s: %s", slug, exc)
    return signal


def load_cached_signal(db, slug: str) -> dict | None:
    try:
        raw = db.get_system_state(STATE_PREFIX + slug)
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def get_directive_for_generation(db, slug: str, channel_id: int | None = None,
                                 cfg=None) -> str | None:
    """Return the retention directive to inject when writing a script.

    Uses the cached signal when fresh; otherwise recomputes on demand (local
    DB only, no network). Returns None when disabled or without data.
    """
    if cfg is None:
        cfg = _load_cfg(slug)
    if _is_disabled(db, cfg):
        return None

    max_age = float(getattr(cfg, "RETENTION_FEEDBACK_MAX_AGE_HOURS", 48) or 48)
    signal = load_cached_signal(db, slug)
    age = _age_hours(signal.get("generated_at")) if signal else None
    if signal is None or age is None or age > max_age:
        if channel_id is None:
            return signal.get("directive") if signal else None
        signal = compute_channel_signal(
            db, {"id": channel_id, "slug": slug}, cfg=cfg
        )
    if not signal:
        return None
    directive = signal.get("directive")
    return str(directive).strip() or None


def compute_and_cache_all(db, persist: bool = True) -> list[dict]:
    """Recompute the retention signal for every active channel."""
    results = []
    try:
        channels = db.get_channels(active_only=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("retention_feedback: cannot list channels: %s", exc)
        return results
    for ch in channels:
        slug = ch.get("slug")
        if not slug:
            continue
        try:
            sig = compute_channel_signal(
                db, {"id": ch.get("id"), "slug": slug}, persist=persist
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("retention_feedback failed for %s: %s", slug, exc)
            sig = None
        if sig:
            results.append(sig)
    return results


def mark_started(db) -> None:
    """Seal the Fase 3 start timestamp once (for pre/post attribution)."""
    try:
        if not db.get_system_state(STARTED_KEY):
            db.set_system_state(STARTED_KEY, _now_iso())
    except Exception:  # noqa: BLE001
        pass
