"""Single boundary for upload visibility and publication timing."""

from datetime import datetime, timedelta, timezone
from api.time_utils import youtube_rfc3339
from api.time_utils import parse_utc


def validate_upload_visibility(*, publish_mode: str, privacy: str,
                               publish_at: str | None,
                               content_type: str = "long") -> None:
    """Reject the only unsafe combination: scheduled content without publishAt."""
    if content_type == "short":
        if privacy != "public" or publish_at:
            raise ValueError("native shorts must be public and immediate")
        return
    if publish_mode == "scheduled" and not publish_at:
        raise ValueError("scheduled uploads require a future publish_at")
    if publish_at and privacy != "private":
        raise ValueError("publish_at uploads must use private visibility")


def validate_manual_publication(*, publish_mode: str, target_public_at: str | None,
                                now: datetime | None = None) -> None:
    """Prevent manual/direct public transitions before a scheduled target."""
    if publish_mode != "scheduled":
        return
    target = parse_utc(target_public_at)
    now = now or datetime.now(timezone.utc)
    if target is None or target > now:
        raise ValueError("scheduled content cannot be published before its target")


def assert_public_admission(db, *, channel_id: int, content_type: str) -> None:
    """Enforce the DB-owned daily public cap at the final public transition."""
    from api.services.channel_policy import resolve_channel_policy_values
    cap_key = "longform_publish_cap" if content_type == "long" else "native_shorts_per_day"
    policy = resolve_channel_policy_values(channel_id, db=db)
    cap = int(policy.get(cap_key, 0) or 0)
    if cap <= 0:
        raise ValueError("daily public cap is disabled")
    from api.time_utils import madrid_day_range
    start, end = madrid_day_range()
    table = "videos" if content_type == "long" else "shorts"
    date_col = "published_at" if content_type == "long" else "actual_published_at"
    with db._connect() as conn:
        count = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE channel_id=? AND status='published' AND {date_col}>=? AND {date_col}<?",
            (channel_id, start, end),
        ).fetchone()[0]
    if int(count or 0) >= cap:
        raise ValueError(f"daily public cap reached ({count}/{cap})")


def upload_publication_kwargs(*, publish_mode: str, target_public_at: str | None = None,
                              now: datetime | None = None, warmup_min: int = 120,
                              content_type: str = "long") -> dict:
    """Return safe uploader kwargs; scheduled content can never go public early."""
    if content_type == "short":
        return {"privacy": "public"}
    if publish_mode != "scheduled":
        return {"privacy": "public"}
    now = now or datetime.now(timezone.utc)
    target = target_public_at
    if not target:
        target = now + timedelta(minutes=max(60, int(warmup_min)))
    return {"privacy": "private", "publish_at": youtube_rfc3339(target)}
