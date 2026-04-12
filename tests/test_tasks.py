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


def test_process_alert_does_not_log_dvla_enrichment_without_key(tmp_path, monkeypatch, caplog):
    image_path = tmp_path / "alert.jpg"
    image_path.write_bytes(b"fake-image")

    config = {
        "id": "cfg1",
        "name": "Front",
        "request_id": "req12345",
        "telegram_token": "token",
        "chat_id": "chat",
        "prompt": "Describe motion.",
        "instant_notify": 0,
        "send_video": 0,
        "trigger_filename": "",
        "dvla_api_key": "",
        "verbose_logging": 0,
    }

    monkeypatch.setattr(tasks, "is_muted", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(tasks, "check_auto_mute", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(tasks, "build_prompt", lambda *_args, **_kwargs: "Describe motion.")
    monkeypatch.setattr(tasks, "optimize_image", lambda *_args, **_kwargs: "encoded")
    monkeypatch.setattr(tasks, "analyze_image_parallel", lambda *_args, **_kwargs: "Vehicle arrived")
    monkeypatch.setattr(tasks, "send_telegram", lambda cfg, *_args, **_kwargs: cfg.update({"last_msg_id": 321}))
    monkeypatch.setattr(tasks, "enrich_caption_with_dvla", lambda text, *_args, **_kwargs: text)

    with caplog.at_level(logging.INFO):
        tasks.process_alert(str(image_path), config)

    assert "phase=dvla_caption_enriched" not in caplog.text
