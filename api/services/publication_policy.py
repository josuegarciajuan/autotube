"""Single boundary for upload visibility and publication timing."""

from datetime import datetime, timedelta, timezone
from api.time_utils import youtube_rfc3339


def validate_upload_visibility(*, publish_mode: str, privacy: str,
                               publish_at: str | None) -> None:
    """Reject the only unsafe combination: scheduled content without publishAt."""
    if publish_mode == "scheduled" and not publish_at:
        raise ValueError("scheduled uploads require a future publish_at")
    if publish_at and privacy != "private":
        raise ValueError("publish_at uploads must use private visibility")


def upload_publication_kwargs(*, publish_mode: str, target_public_at: str | None = None,
                              now: datetime | None = None, warmup_min: int = 120) -> dict:
    """Return safe uploader kwargs; scheduled content can never go public early."""
    if publish_mode != "scheduled":
        return {"privacy": "public"}
    now = now or datetime.now(timezone.utc)
    target = target_public_at
    if not target:
        target = now + timedelta(minutes=max(60, int(warmup_min)))
    return {"privacy": "private", "publish_at": youtube_rfc3339(target)}
