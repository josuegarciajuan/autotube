"""Single boundary for upload visibility and publication timing."""

from datetime import datetime, timedelta, timezone


def upload_publication_kwargs(*, publish_mode: str, target_public_at: str | None = None,
                              now: datetime | None = None, warmup_min: int = 120) -> dict:
    """Return safe uploader kwargs; scheduled content can never go public early."""
    if publish_mode != "scheduled":
        return {"privacy": "public"}
    now = now or datetime.now(timezone.utc)
    target = target_public_at
    if not target:
        target = (now + timedelta(minutes=max(60, int(warmup_min)))).isoformat()
    return {"privacy": "private", "publish_at": target}
