import logging

import tasks


class _DummyResponse:
    def __init__(self, ok=True, status_code=200, payload=None):
        self.ok = ok
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def test_send_telegram_logs_photo_sent(tmp_path, monkeypatch, caplog):
    image_path = tmp_path / "alert.jpg"
    image_path.write_bytes(b"fake-image")

    config = {
        "name": "Driveway",
        "request_id": "abc12345",
        "telegram_token": "token",
        "chat_id": "chat",
    }

    monkeypatch.setattr(
        tasks.requests,
        "post",
        lambda *args, **kwargs: _DummyResponse(
            ok=True,
            payload={"result": {"message_id": 321}},
        ),
    )

    with caplog.at_level(logging.INFO):
        tasks.send_telegram(config, str(image_path), "Motion detected.")

    assert config["last_msg_id"] == 321
    assert "phase=telegram_photo_sent" in caplog.text
    assert "caption_source=still" in caplog.text
    assert "message_id=321" in caplog.text


def test_update_telegram_caption_logs_source_and_change(monkeypatch, caplog):
    config = {
        "name": "Driveway",
        "request_id": "abc12345",
        "telegram_token": "token",
        "chat_id": "chat",
        "last_msg_id": 654,
    }

    monkeypatch.setattr(tasks.requests, "post", lambda *args, **kwargs: _DummyResponse(ok=True))

    with caplog.at_level(logging.INFO):
        ok = tasks.update_telegram_caption(
            config,
            "Vehicle AB12 CDE arrived",
            caption_source="dvla",
            previous_text="Vehicle arrived",
        )

    assert ok is True
    assert "phase=telegram_caption_update_started" in caplog.text
    assert "phase=telegram_caption_updated" in caplog.text
    assert "caption_source=dvla" in caplog.text
    assert "caption_changed=true" in caplog.text
    assert "message_id=654" in caplog.text
