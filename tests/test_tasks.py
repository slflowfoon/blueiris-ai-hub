import logging
import threading

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


def test_process_alert_starts_bi_export_prep_before_still_send_finishes(tmp_path, monkeypatch):
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
        "send_video": 1,
        "trigger_filename": "Front.20260412_160001.2973364.3-1.jpg",
        "bi_url": "http://bi.local:81",
        "bi_user": "admin",
        "bi_pass": "pw",
        "dvla_api_key": "",
        "verbose_logging": 0,
    }

    build_started = threading.Event()
    allow_build_finish = threading.Event()
    enqueued = {}

    monkeypatch.setattr(tasks, "is_muted", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(tasks, "check_auto_mute", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(tasks, "build_prompt", lambda *_args, **_kwargs: "Describe motion.")
    monkeypatch.setattr(tasks, "optimize_image", lambda *_args, **_kwargs: "encoded")
    monkeypatch.setattr(tasks, "analyze_image_parallel", lambda *_args, **_kwargs: "Vehicle arrived")
    monkeypatch.setattr(tasks, "enrich_caption_with_dvla", lambda text, *_args, **_kwargs: text)

    def fake_build_payload(*_args, **_kwargs):
        build_started.set()
        assert allow_build_finish.wait(1)
        return {"request_id": "export-123"}

    def fake_send(cfg, *_args, **_kwargs):
        assert build_started.is_set()
        cfg["last_msg_id"] = 321
        allow_build_finish.set()

    monkeypatch.setattr(tasks, "build_bi_export_payload", fake_build_payload)
    monkeypatch.setattr(tasks, "send_telegram", fake_send)
    monkeypatch.setattr(
        tasks,
        "enqueue_bi_export_payload",
        lambda payload, _tag: enqueued.setdefault("payload", payload) or payload["request_id"],
    )

    tasks.process_alert(str(image_path), config)

    assert enqueued["payload"]["delivery_context"]["config"]["last_msg_id"] == 321


def test_enrich_caption_uses_correct_dvla_endpoint(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        return _DummyResponse(
            status_code=200,
            payload={
                "make": "Ford",
                "colour": "Blue",
                "yearOfManufacture": 2019,
            },
        )

    monkeypatch.setattr(tasks, "load_known_plates", lambda: {})
    monkeypatch.setattr(tasks, "_audit_plate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tasks.requests, "post", fake_post)

    caption = tasks.enrich_caption_with_dvla(
        "Vehicle AB12 CDE arrived",
        {"name": "Driveway", "dvla_api_key": "test-key"},
        tag="[Driveway][abc12345]",
    )

    assert captured["url"] == "https://driver-vehicle-licensing.api.gov.uk/vehicle-enquiry/v1/vehicles"
    assert captured["headers"]["x-api-key"] == "test-key"
    assert captured["json"] == {"registrationNumber": "AB12CDE"}
    assert captured["timeout"] == 10
    assert "(Ford, Blue, 2019)" in caption


def test_enrich_caption_without_plate_returns_original_text(monkeypatch):
    monkeypatch.setattr(tasks, "load_known_plates", lambda: {})

    caption = tasks.enrich_caption_with_dvla(
        "Vehicle arrived on driveway",
        {"name": "Driveway", "dvla_api_key": "test-key"},
        tag="[Driveway][abc12345]",
    )

    assert caption == "Vehicle arrived on driveway"


# ---------------------------------------------------------------------------
# replace_telegram_media — fallback to sendAnimation when last_msg_id is None
# ---------------------------------------------------------------------------

def _make_video_config(**overrides):
    cfg = {
        "name": "Driveway",
        "request_id": "de12f077",
        "telegram_token": "token",
        "chat_id": "123",
    }
    cfg.update(overrides)
    return cfg


def test_replace_telegram_media_falls_back_to_send_when_no_msg_id(tmp_path, monkeypatch):
    """When last_msg_id is None, sendAnimation is called instead of editMessageMedia."""
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake-video")

    captured = {}

    def fake_post(url, data=None, files=None, timeout=None):
        captured["url"] = url
        captured["files"] = list(files.keys()) if files else []
        return _DummyResponse(ok=True, payload={"result": {"message_id": 999}})

    config = _make_video_config(last_msg_id=None)
    monkeypatch.setattr(tasks.requests, "post", fake_post)

    result = tasks.replace_telegram_media(config, str(video), "Motion detected.")

    assert result is True
    assert "sendAnimation" in captured["url"]
    assert "editMessageMedia" not in captured["url"]
    assert "animation" in captured["files"]
    assert config["last_msg_id"] == 999


def test_replace_telegram_media_falls_back_when_key_missing(tmp_path, monkeypatch):
    """When last_msg_id key is absent entirely, sendAnimation is called."""
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake-video")

    captured = {}

    def fake_post(url, data=None, files=None, timeout=None):
        captured["url"] = url
        return _DummyResponse(ok=True, payload={"result": {"message_id": 42}})

    config = _make_video_config()  # no last_msg_id key at all
    monkeypatch.setattr(tasks.requests, "post", fake_post)

    result = tasks.replace_telegram_media(config, str(video), "Motion detected.")

    assert result is True
    assert "sendAnimation" in captured["url"]
    assert config["last_msg_id"] == 42


def test_replace_telegram_media_uses_edit_when_msg_id_present(tmp_path, monkeypatch):
    """Normal path: last_msg_id present → editMessageMedia, no sendAnimation."""
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake-video")

    captured = {}

    def fake_post(url, data=None, files=None, timeout=None):
        captured["url"] = url
        return _DummyResponse(ok=True, payload={})

    config = _make_video_config(last_msg_id=555)
    monkeypatch.setattr(tasks.requests, "post", fake_post)

    result = tasks.replace_telegram_media(config, str(video), "Motion detected.")

    assert result is True
    assert "editMessageMedia" in captured["url"]
    assert "sendAnimation" not in captured["url"]


def test_replace_telegram_media_fallback_sets_last_msg_id_for_caption_update(tmp_path, monkeypatch):
    """After fallback send, last_msg_id is updated so caption updates work."""
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake-video")

    calls = []

    def fake_post(url, data=None, files=None, timeout=None):
        calls.append(url)
        if "sendAnimation" in url:
            return _DummyResponse(ok=True, payload={"result": {"message_id": 777}})
        if "editMessageCaption" in url:
            assert data.get("message_id") == 777
            return _DummyResponse(ok=True, payload={})
        return _DummyResponse(ok=False)

    config = _make_video_config(last_msg_id=None)
    monkeypatch.setattr(tasks.requests, "post", fake_post)

    tasks.replace_telegram_media(config, str(video), "Motion detected.")
    tasks.update_telegram_caption(config, "Car in driveway.")

    assert any("sendAnimation" in c for c in calls)
    assert any("editMessageCaption" in c for c in calls)

