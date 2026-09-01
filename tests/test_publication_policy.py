from datetime import datetime, timezone

import pytest
from api.services.publication_policy import upload_publication_kwargs, validate_upload_visibility


def test_scheduled_upload_is_private_and_has_future_publication():
    result = upload_publication_kwargs(
        publish_mode="scheduled", now=datetime(2030, 1, 1, tzinfo=timezone.utc), warmup_min=120
    )
    assert result["privacy"] == "private"
    assert result["publish_at"] > "2030-01-01T00:00:00"


def test_immediate_upload_retains_explicit_public_policy():
    assert upload_publication_kwargs(publish_mode="immediate") == {"privacy": "public"}


def test_scheduled_upload_without_publish_at_is_rejected():
    with pytest.raises(ValueError):
        validate_upload_visibility(publish_mode="scheduled", privacy="public", publish_at=None)
