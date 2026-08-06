"""LLM Credit Checker — monitors DeepSeek balance, OpenAI quota errors, and YouTube API quota.

Usage:
    from api.services.llm_credit_checker import check_all_llm_credits, get_llm_credit_status
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

import requests

from config.settings import (
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_CREDIT_CHECK_INTERVAL_HOURS,
    LLM_CREDIT_LOW_THRESHOLD_USD,
    OPENAI_API_KEY,
)

logger = logging.getLogger("autotube.llm_credits")


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _upsert_credit_status(db, provider: str, status: str, balance_usd=None,
                           error_count_7d=0, last_error=None, metadata=None):
    """Insert or update a credit status row."""
    try:
        with db._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM llm_credit_status WHERE provider = ?", (provider,),
            ).fetchone()
            meta_json = json.dumps(metadata) if metadata else None
            if existing:
                conn.execute(
                    """UPDATE llm_credit_status
                       SET status = ?, balance_usd = ?, error_count_7d = ?,
                           last_error = ?, metadata_json = ?, checked_at = ?
                       WHERE provider = ?""",
                    (status, balance_usd, error_count_7d, last_error,
                     meta_json, _utcnow(), provider),
                )
            else:
                conn.execute(
                    """INSERT INTO llm_credit_status
                       (provider, status, balance_usd, error_count_7d,
                        last_error, metadata_json, checked_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (provider, status, balance_usd, error_count_7d,
                     last_error, meta_json, _utcnow()),
                )
            conn.commit()
    except Exception as exc:
        logger.warning("Failed to upsert credit status for %s: %s", provider, exc)


# ═══════════════════════════════════════════════════════════════
# DeepSeek Balance Check
# ═══════════════════════════════════════════════════════════════

def _parse_deepseek_balance(data: dict) -> dict:
    """Parse DeepSeek /user/balance response into a normalized dict."""
    is_available = data.get("is_available", False)
    balance_infos = data.get("balance_infos", [])
    usd_balance = 0.0
    cny_balance = None
    currency = "USD"

    for bi in balance_infos:
        cur = bi.get("currency", "")
        total = float(bi.get("total_balance", "0"))
        if cur == "USD":
            usd_balance = total
            currency = "USD"
        elif cur == "CNY":
            cny_balance = total
            if usd_balance == 0.0:
                # Rough approximation if only CNY available
                usd_balance = round(total * 0.14, 2)
                currency = "CNY"

    return {
        "is_available": is_available,
        "balance_usd": usd_balance,
        "currency": currency,
        "cny_balance": cny_balance,
    }


def check_deepseek_balance(api_key: str = None) -> dict:
    """Call DeepSeek balance API and return parsed status.

    Returns:
        {
            "status": "healthy" | "low" | "exhausted" | "error",
            "balance_usd": float,
            "currency": str,
            "metadata": {...},
            "error": str | None,
        }
    """
    key = api_key or LLM_API_KEY
    if not key:
        return {"status": "error", "balance_usd": 0.0, "currency": "USD",
                "metadata": None, "error": "No API key configured"}

    try:
        resp = requests.get(
            "https://api.deepseek.com/user/balance",
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            timeout=15,
        )
        if resp.status_code != 200:
            return {"status": "error", "balance_usd": 0.0, "currency": "USD",
                    "metadata": {"http_status": resp.status_code, "body": resp.text[:300]},
                    "error": f"HTTP {resp.status_code}"}

        data = resp.json()
        parsed = _parse_deepseek_balance(data)

        if not parsed["is_available"]:
            status = "exhausted"
        elif parsed["balance_usd"] <= 0:
            status = "exhausted"
        elif parsed["balance_usd"] < LLM_CREDIT_LOW_THRESHOLD_USD:
            status = "low"
        else:
            status = "healthy"

        return {
            "status": status,
            "balance_usd": parsed["balance_usd"],
            "currency": parsed["currency"],
            "metadata": {
                "cny_balance": parsed.get("cny_balance"),
                "topped_up_balance": next(
                    (float(bi.get("topped_up_balance", "0")) for bi in data.get("balance_infos", [])
                     if bi.get("currency") == parsed["currency"]), None),
                "granted_balance": next(
                    (float(bi.get("granted_balance", "0")) for bi in data.get("balance_infos", [])
                     if bi.get("currency") == parsed["currency"]), None),
            },
            "error": None,
        }
    except requests.RequestException as exc:
        logger.warning("DeepSeek balance check failed: %s", exc)
        return {"status": "error", "balance_usd": 0.0, "currency": "USD",
                "metadata": None, "error": str(exc)[:200]}
    except Exception as exc:
        logger.warning("DeepSeek balance check unexpected error: %s", exc)
        return {"status": "error", "balance_usd": 0.0, "currency": "USD",
                "metadata": None, "error": str(exc)[:200]}


# ═══════════════════════════════════════════════════════════════
# OpenAI Quota Detection (error-based, no test API call)
# ═══════════════════════════════════════════════════════════════

OPENAI_QUOTA_PATTERNS = [
    "insufficient_quota",
    "You exceeded your current quota",
    "exceeded your current quota",
    "billing_not_active",
    "quota exceeded",
    "out of credits",
    "no credits",
    "insufficient funds",
    "rate_limit_exceeded",
    "429",
    "authentication",
]


def check_openai_from_errors(db) -> dict:
    """Detect OpenAI quota exhaustion from existing pipeline errors.

    Scans pipeline_alerts and script_generation_attempts for patterns
    matching quota/credit exhaustion. No test API call is made.

    Returns:
        {
            "status": "healthy" | "exhausted" | "unknown",
            "error_count_7d": int,
            "last_error": str | None,
            "has_quota": bool,
        }
    """
    try:
        with db._connect() as conn:
            # Check pipeline_alerts for any openai-related error
            alert_rows = conn.execute(
                """SELECT message, title, created_at FROM pipeline_alerts
                   WHERE resolved = 0
                     AND (
                        title LIKE '%OpenAI%' OR title LIKE '%openai%'
                        OR message LIKE '%OpenAI%' OR message LIKE '%openai%'
                        OR message LIKE '%insufficient_quota%'
                        OR message LIKE '%quota%exceed%'
                        OR message LIKE '%credits%'
                        OR title LIKE '%quota%' OR title LIKE '%crédito%'
                     )
                   ORDER BY created_at DESC LIMIT 10"""
            ).fetchall()

            # Check script_generation_attempts for quota errors
            attempt_rows = conn.execute(
                """SELECT error, created_at FROM script_generation_attempts
                   WHERE error IS NOT NULL
                     AND created_at > datetime('now', '-7 days')
                   ORDER BY created_at DESC LIMIT 50"""
            ).fetchall()

            # Detect quota patterns
            quota_errors = []
            for row in alert_rows:
                msg = (row["message"] or "") + (row["title"] or "")
                for pattern in OPENAI_QUOTA_PATTERNS:
                    if pattern.lower() in msg.lower():
                        quota_errors.append({
                            "error": msg[:300],
                            "created_at": row["created_at"],
                            "source": "alert",
                        })
                        break

            for row in attempt_rows:
                err = row["error"] or ""
                for pattern in OPENAI_QUOTA_PATTERNS:
                    if pattern.lower() in err.lower():
                        quota_errors.append({
                            "error": err[:300],
                            "created_at": row["created_at"],
                            "source": "attempt",
                        })
                        break

            error_count = len(quota_errors)
            last_error = quota_errors[0]["error"] if quota_errors else None

            if error_count > 0:
                status = "exhausted"
            else:
                status = "healthy"  # no errors = assume healthy

            return {
                "status": status,
                "error_count_7d": error_count,
                "last_error": last_error,
                "has_quota": (status == "healthy"),
            }
    except Exception as exc:
        logger.warning("OpenAI error check failed: %s", exc)
        return {"status": "error", "error_count_7d": 0,
                "last_error": str(exc)[:200], "has_quota": True}


# ═══════════════════════════════════════════════════════════════
# YouTube API Quota Status
# ═══════════════════════════════════════════════════════════════

def get_youtube_quota_status(db) -> dict:
    """Read YouTube API quota exhaustion state from system_state table.

    Returns:
        {
            "exhausted": bool,
            "exhausted_at": str | None,
            "elapsed_hours": float | None,
            "estimated_reset_hours": float | None,
        }
    """
    try:
        paused = db.get_system_state("scheduler_paused") == "true"
        exhausted_at_str = db.get_system_state("quota_exhausted_at")

        if not (paused and exhausted_at_str):
            return {
                "exhausted": False,
                "exhausted_at": None,
                "elapsed_hours": None,
                "estimated_reset_hours": None,
            }

        # Parse exhausted_at timestamp
        try:
            exhausted_at = datetime.fromisoformat(exhausted_at_str)
        except (ValueError, TypeError):
            return {
                "exhausted": True,
                "exhausted_at": exhausted_at_str,
                "elapsed_hours": None,
                "estimated_reset_hours": None,
            }

        if exhausted_at.tzinfo is None:
            exhausted_at = exhausted_at.replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)
        elapsed = (now - exhausted_at).total_seconds() / 3600
        remaining = max(0, 6 - elapsed)  # quota resets after ~6 hours

        return {
            "exhausted": True,
            "exhausted_at": exhausted_at_str,
            "elapsed_hours": round(elapsed, 1),
            "estimated_reset_hours": round(remaining, 1),
        }
    except Exception as exc:
        logger.warning("YouTube quota status check failed: %s", exc)
        return {"exhausted": False, "exhausted_at": None,
                "elapsed_hours": None, "estimated_reset_hours": None}


# ═══════════════════════════════════════════════════════════════
# Getter: returns filtered status from DB
# ═══════════════════════════════════════════════════════════════

def get_llm_credit_status(db) -> dict:
    """Read current credit status for all providers from the database.

    Returns:
        {
            "deepseek": {status, balance_usd, ...} | null,
            "openai": {status, error_count_7d, ...} | null,
            "youtube": {exhausted, exhausted_at, ...} | null,
            "checked_at": str | null,
        }
    """
    result = {"deepseek": None, "openai": None, "youtube": None, "checked_at": None}
    try:
        with db._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM llm_credit_status ORDER BY checked_at DESC"
            ).fetchall()

            for row in rows:
                provider = row["provider"]
                data = {
                    "status": row["status"],
                    "checked_at": row["checked_at"],
                }
                if provider == "deepseek":
                    data["balance_usd"] = row["balance_usd"]
                elif provider == "openai":
                    data["error_count_7d"] = row["error_count_7d"]
                    data["last_error"] = row["last_error"]
                    data["has_quota"] = row["status"] == "healthy"

                if row["metadata_json"]:
                    try:
                        data["metadata"] = json.loads(row["metadata_json"])
                    except json.JSONDecodeError:
                        pass

                result[provider] = data
                if result["checked_at"] is None:
                    result["checked_at"] = row["checked_at"]

            # Add YouTube quota info
            result["youtube"] = get_youtube_quota_status(db)

        return result
    except Exception as exc:
        logger.warning("Failed to read LLM credit status: %s", exc)
        result["youtube"] = get_youtube_quota_status(db)
        return result


# ═══════════════════════════════════════════════════════════════
# Master check: orchestrates all checks (called by health monitor)
# ═══════════════════════════════════════════════════════════════

def check_all_llm_credits(db, force: bool = False) -> dict:
    """Run all credit checks and upsert results into the database.

    Args:
        db: ExtendedDatabase instance
        force: If True, run checks even if within the interval window

    Returns:
        {
            "deepseek": {...},
            "openai": {...},
            "youtube": {...},
        }
    """
    # Check if we should run based on interval
    if not force:
        try:
            with db._connect() as conn:
                last = conn.execute(
                    "SELECT checked_at FROM llm_credit_status ORDER BY checked_at DESC LIMIT 1"
                ).fetchone()
                if last and last["checked_at"]:
                    try:
                        last_at = datetime.fromisoformat(last["checked_at"])
                        if last_at.tzinfo is None:
                            last_at = last_at.replace(tzinfo=timezone.utc)
                        elapsed = (datetime.now(timezone.utc) - last_at).total_seconds() / 3600
                        if elapsed < LLM_CREDIT_CHECK_INTERVAL_HOURS:
                            logger.debug(
                                "LLM credit check skipped: last check %.1f hours ago (interval: %dh)",
                                elapsed, LLM_CREDIT_CHECK_INTERVAL_HOURS,
                            )
                            return get_llm_credit_status(db)
                    except (ValueError, TypeError):
                        pass  # Invalid timestamp — run check anyway
        except Exception:
            pass  # Table might not exist yet — run check anyway

    logger.info("Running LLM credit checks...")

    # ── DeepSeek balance ──
    ds = check_deepseek_balance()
    _upsert_credit_status(
        db,
        provider="deepseek",
        status=ds["status"],
        balance_usd=ds["balance_usd"],
        metadata=ds.get("metadata"),
    )
    logger.info("DeepSeek: status=%s balance=%.2f %s", ds["status"], ds["balance_usd"], ds["currency"])

    # ── OpenAI from errors ──
    oa = check_openai_from_errors(db)
    _upsert_credit_status(
        db,
        provider="openai",
        status=oa["status"],
        error_count_7d=oa.get("error_count_7d", 0),
        last_error=oa.get("last_error"),
        metadata=None,
    )
    logger.info("OpenAI: status=%s errors_7d=%d", oa["status"], oa.get("error_count_7d", 0))

    return get_llm_credit_status(db)
