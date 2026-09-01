from datetime import datetime, timezone

from api.services.publication_policy import upload_publication_kwargs


def test_scheduled_upload_is_private_and_has_future_publication():
    result = upload_publication_kwargs(
        publish_mode="scheduled", now=datetime(2030, 1, 1, tzinfo=timezone.utc), warmup_min=120
    )
    assert result["privacy"] == "private"
    assert result["publish_at"] > "2030-01-01T00:00:00"


def test_immediate_upload_retains_explicit_public_policy():
    assert upload_publication_kwargs(publish_mode="immediate") == {"privacy": "public"}
