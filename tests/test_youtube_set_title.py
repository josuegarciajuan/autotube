"""Regresión: set_title preserva snippet y actualiza solo el título."""

from unittest.mock import MagicMock

from pipeline.youtube_uploader import YouTubeUploader


def test_set_title_preserves_description_tags_and_category(monkeypatch):
    uploader = YouTubeUploader("canal2")
    service = MagicMock()
    item = {
        "snippet": {
            "title": "Viejo",
            "description": "Descripción intacta",
            "categoryId": "24",
            "tags": ["a", "b"],
        }
    }
    service.videos.return_value.list.return_value.execute.return_value = {"items": [item]}

    monkeypatch.setattr(uploader, "_get_service", lambda: service)
    monkeypatch.setattr("pipeline.youtube_uploader.track_quota", lambda *a, **k: None)

    result = uploader.set_title("yt123", "  Nuevo   título  ")

    assert result == {"updated": True, "yt_video_id": "yt123", "title": "Nuevo título"}
    body = service.videos.return_value.update.call_args.kwargs["body"]
    assert body["snippet"]["title"] == "Nuevo título"
    assert body["snippet"]["description"] == "Descripción intacta"
    assert body["snippet"]["categoryId"] == "24"
    assert body["snippet"]["tags"] == ["a", "b"]
    assert service.videos.return_value.update.call_args.kwargs["part"] == "snippet"


def test_set_title_rejects_empty(monkeypatch):
    uploader = YouTubeUploader("canal2")
    monkeypatch.setattr(uploader, "_get_service", lambda: MagicMock())
    try:
        uploader.set_title("yt123", "   ")
    except ValueError as exc:
        assert "empty" in str(exc)
    else:
        raise AssertionError("expected ValueError")
