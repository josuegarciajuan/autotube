from pipeline.youtube_uploader import build_upload_status


def test_short_upload_payload_cannot_be_scheduled():
    assert build_upload_status(
        privacy="private", publish_at="2099-01-01T12:00:00Z", content_type="short"
    ) == {"privacyStatus": "public"}


def test_long_form_scheduled_payload_remains_private_with_publish_at():
    assert build_upload_status(
        privacy="private", publish_at="2099-01-01T12:00:00Z", content_type="long"
    ) == {"privacyStatus": "private", "publishAt": "2099-01-01T12:00:00Z"}
