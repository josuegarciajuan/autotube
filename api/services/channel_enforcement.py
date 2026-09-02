"""Channel-scoped delivery enforcement.

This is the only boundary that turns an external delivery observation into a
strike.  Observations are retained as audit records, but only an explicit
classification and non-empty evidence can mutate enforcement state.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

INFORMATIONAL = {"unavailable", "unknown", "error", "private", "scheduled"}
STRIKE = "confirmed_strike"
EXPLICIT_STRIKE_SOURCES = frozenset({"studio", "email", "operator"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _save_event(db, event: dict) -> None:
    events = getattr(db, "enforcement_events", None)
    if events is not None:
        events.append(event)
        return
    try:
        with db._connect() as conn:
            conn.execute(
                """INSERT INTO channel_enforcement_events
                   (channel_id, classification, evidence_json, source, scope,
                    occurred_at, enforced) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (event["channel_id"], event["classification"],
                 json.dumps(event["evidence"]), event["source"], event["scope"],
                 event["occurred_at"], int(event["enforced"])),
            )
            conn.commit()
    except Exception as exc:
        # Pre-v52 test/legacy adapters may not have the ledger yet. Enforcement
        # state must still be applied; migration will make the audit durable.
        if "no such table" not in str(exc).lower():
            raise


def _alert(db, event: dict, *, severity: str, alert_type: str) -> None:
    from api.services.lifecycle_monitor import create_alert
    create_alert(
        db, entity_type="channel", entity_id=event["channel_id"],
        channel_id=event["channel_id"], alert_type=alert_type,
        severity=severity,
        title=f"Canal {event['channel_id']}: {alert_type}",
        message=(f"Clasificación: {event['classification']}; fuente: {event['source']}"),
        metadata={k: event[k] for k in ("classification", "evidence", "source",
                                        "occurred_at", "scope", "enforced")},
    )


def record_delivery_event(db, *, channel_id: int, classification: str | None,
                          evidence: dict | None, source: str | None,
                          occurred_at: str | None = None) -> dict:
    """Record an observation and enforce it only when evidence is explicit."""
    scope = f"channel_id:{int(channel_id)}"
    timestamp = occurred_at or _now()
    if not classification or not isinstance(evidence, dict) or not evidence or not source:
        return {"enforced": False, "reason": "explicit_classification_and_evidence_required",
                "scope": scope, "occurred_at": timestamp}

    is_strike = classification == STRIKE
    if is_strike and source not in EXPLICIT_STRIKE_SOURCES:
        return {"enforced": False, "reason": "confirmed_strike_source_not_allowed",
                "scope": scope, "occurred_at": timestamp}
    event = {"channel_id": int(channel_id), "classification": classification,
             "evidence": evidence, "source": source, "scope": scope,
             "occurred_at": timestamp, "enforced": is_strike}
    _save_event(db, event)
    if not is_strike:
        informational_alert = (classification if classification in {
            "video_removed_unconfirmed", "removal_confirmed"
        } else "channel_delivery_unavailable")
        _alert(db, event, severity="info", alert_type=informational_alert)
        return {**event, "alert_type": informational_alert}

    from api.services.channel_policy import get_historical_strikes, set_channel_delivery_state
    strikes = get_historical_strikes(channel_id, db) + 1
    db.set_system_state(f"shorts_spam_strikes_{channel_id}", str(strikes))
    block_until = time.time() + (12 if strikes == 1 else 24) * 3600
    db.set_system_state(f"shorts_spam_blocked_until_{channel_id}", str(block_until))
    try:
        set_channel_delivery_state("strike", channel_id, db)
    except Exception as exc:
        # Compatibility for pre-v51 adapters; production uses the atomic table.
        if "no such table" not in str(exc).lower():
            raise
        db.set_system_state(f"channel_delivery_state_{channel_id}", "strike")
    _alert(db, event, severity="critical", alert_type="spam_strike")
    return {**event, "strike_count": strikes, "blocked_until": block_until,
            "alert_type": "spam_strike"}


def record_watch_page_observation(db, *, channel_id: int, video_id: str,
                                  visibility: str, confirmations: int,
                                  source: str = "watch_page") -> dict:
    """Record watch-page state; it can never create enforcement."""
    confirmed = visibility == "removed" and int(confirmations or 0) >= 2
    classification = "removal_confirmed" if confirmed else "video_removed_unconfirmed"
    result = record_delivery_event(
        db, channel_id=channel_id, classification=classification,
        evidence={"video_id": video_id, "visibility": visibility,
                  "confirmations": int(confirmations or 0)}, source=source,
    )
    return result


def record_confirmed_strike(db, *, channel_id: int, source: str,
                            evidence: dict) -> dict:
    """Explicit operator/Studio/email entry point for a real strike."""
    if source not in EXPLICIT_STRIKE_SOURCES:
        return {"enforced": False, "reason": "confirmed_strike_source_not_allowed",
                "scope": f"channel_id:{int(channel_id)}"}
    if not isinstance(evidence, dict) or not evidence:
        return {"enforced": False, "reason": "explicit_classification_and_evidence_required",
                "scope": f"channel_id:{int(channel_id)}"}
    return record_delivery_event(
        db, channel_id=channel_id, classification=STRIKE,
        evidence=evidence, source=source,
    )


def get_channel_enforcement(db, channel_id: int) -> dict:
    from api.services.channel_policy import get_channel_delivery_state
    latest = None
    events = getattr(db, "enforcement_events", None)
    if events is not None:
        candidates = [e for e in events if e["channel_id"] == int(channel_id)]
        latest = candidates[-1] if candidates else None
    else:
        with db._connect() as conn:
            row = conn.execute(
                "SELECT * FROM channel_enforcement_events WHERE channel_id=? ORDER BY id DESC LIMIT 1",
                (channel_id,),).fetchone()
        if row:
            latest = dict(row)
            latest["evidence"] = json.loads(latest.pop("evidence_json") or "{}")
    return {"state": get_channel_delivery_state(channel_id, db),
            "cause": (latest or {}).get("classification"),
            "source": (latest or {}).get("source"),
            "occurred_at": (latest or {}).get("occurred_at"),
            "updated_at": (latest or {}).get("occurred_at"),
            "scope": f"channel_id:{int(channel_id)}"}


def _events(db, channel_id: int) -> list[dict]:
    value = getattr(db, "enforcement_events", None)
    if value is not None:
        return [e for e in value if e["channel_id"] == int(channel_id)]
    with db._connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM channel_enforcement_events WHERE channel_id=?", (channel_id,))]


def auto_transition_channels(db) -> dict:
    """Advance each channel independently; global spacing remains untouched."""
    from api.services.channel_policy import get_channel_delivery_state, set_channel_delivery_state
    from api.services.pacing_profile import (auto_transition_enabled,
                                             _auto_transition_normal_days,
                                             _auto_transition_recovery_days)
    result = {"scope": "per_channel", "channels": {}}
    if not auto_transition_enabled(db):
        for channel in db.get_channels(active_only=False) or []:
            state = get_channel_delivery_state(int(channel["id"]), db)
            result["channels"][str(channel["id"])] = {
                "from": state, "to": state, "clean_days": None,
                "reason": "kill-switch",
            }
        return result
    now = datetime.now(timezone.utc)
    for channel in db.get_channels(active_only=False) or []:
        cid = int(channel["id"])
        state = get_channel_delivery_state(cid, db)
        strikes = [e for e in _events(db, cid) if e.get("classification") == STRIKE and e.get("enforced")]
        clean = float("inf")
        if strikes:
            when = datetime.fromisoformat(strikes[-1]["occurred_at"].replace("Z", "+00:00"))
            clean = max(0.0, (now - when).total_seconds() / 86400)
        target = "recovery" if state == "strike" and clean >= _auto_transition_recovery_days() else state
        if state == "recovery" and clean >= _auto_transition_normal_days():
            target = "normal"
        if target != state:
            set_channel_delivery_state(target, cid, db)
        result["channels"][str(cid)] = {"from": state, "to": target,
                                         "clean_days": None if clean == float("inf") else round(clean, 1)}
    return result
