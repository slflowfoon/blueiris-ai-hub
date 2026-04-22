import logging
import threading
from datetime import datetime

import tasks
import tv_delivery


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


def test_send_telegram_logs_photo_send_failure_reason(tmp_path, monkeypatch, caplog):
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
            ok=False,
            status_code=400,
            payload={"description": "Bad Request: chat not found"},
        ),
    )

    with caplog.at_level(logging.ERROR):
        tasks.send_telegram(config, str(image_path), "Motion detected.")

    assert "phase=telegram_photo_send_failed" in caplog.text
    assert "reason=status=400 detail=Bad Request: chat not found" in caplog.text


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


def test_process_alert_instant_notify_sends_before_ai_work(tmp_path, monkeypatch):
    image_path = tmp_path / "alert.jpg"
    image_path.write_bytes(b"fake-image")

    config = {
        "id": "cfg1",
        "name": "Front",
        "request_id": "req12345",
        "telegram_token": "token",
        "chat_id": "chat",
        "prompt": "Describe motion.",
        "instant_notify": 1,
        "send_video": 0,
        "trigger_filename": "",
        "dvla_api_key": "",
        "verbose_logging": 0,
    }

    order = []

    monkeypatch.setattr(tasks, "is_muted", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(tasks, "check_auto_mute", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(tasks, "build_prompt", lambda *_args, **_kwargs: "Describe motion.")

    def fake_send(cfg, *_args, **_kwargs):
        order.append("send")
        cfg["last_msg_id"] = 321

    def fake_optimize(*_args, **_kwargs):
        order.append("optimize")
        return "encoded"

    def fake_analyze(*_args, **_kwargs):
        order.append("analyze")
        return "Vehicle arrived"

    def fake_update(*_args, **_kwargs):
        order.append("update")
        return True

    monkeypatch.setattr(tasks, "send_telegram", fake_send)
    monkeypatch.setattr(tasks, "optimize_image", fake_optimize)
    monkeypatch.setattr(tasks, "analyze_image_parallel", fake_analyze)
    monkeypatch.setattr(tasks, "update_telegram_caption", fake_update)
    monkeypatch.setattr(tasks, "enrich_caption_with_dvla", lambda text, *_args, **_kwargs: text)

    tasks.process_alert(str(image_path), config)

    assert order == ["send", "optimize", "analyze", "update"]


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


class _FakeRedis:
    """Minimal Redis stub for build_prompt tests."""
    def __init__(self, caption_mode_value=None):
        self._value = caption_mode_value

    def get(self, _key):
        return self._value


def test_build_prompt_appends_no_vehicle_speculation_in_normal_mode(monkeypatch):
    monkeypatch.setattr(tasks, "r", _FakeRedis())
    monkeypatch.setattr(tasks, "load_known_plates", lambda: {})

    prompt = tasks.build_prompt({"chat_id": "123", "prompt": "Describe motion."})

    assert "Describe motion." in prompt
    assert tasks._NO_VEHICLE_SPECULATION in prompt


def test_build_prompt_appends_no_vehicle_speculation_in_caption_mode(monkeypatch):
    import json
    from datetime import datetime, timedelta

    expires = (datetime.now() + timedelta(hours=1)).isoformat()
    mode_data = json.dumps({"mode": "hilarious", "expires": expires}).encode()
    monkeypatch.setattr(tasks, "r", _FakeRedis(caption_mode_value=mode_data))
    monkeypatch.setattr(tasks, "load_known_plates", lambda: {})

    prompt = tasks.build_prompt({"chat_id": "123"})

    assert tasks.CAPTION_PROMPTS["hilarious"] in prompt
    assert tasks._NO_VEHICLE_SPECULATION in prompt


def test_build_prompt_includes_known_plates_before_speculation_note(monkeypatch):
    monkeypatch.setattr(tasks, "r", _FakeRedis())
    monkeypatch.setattr(tasks, "load_known_plates", lambda: {"AB12CDE": "Owner"})

    prompt = tasks.build_prompt({"chat_id": "123", "prompt": "Describe motion."})

    plate_pos = prompt.index("AB12CDE")
    specul_pos = prompt.index(tasks._NO_VEHICLE_SPECULATION)
    assert plate_pos < specul_pos


def test_process_alert_dispatches_tv_alert_when_enabled(tmp_path, monkeypatch):
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
        "tv_push_enabled": 1,
        "tv_rtsp_url": "rtsp://camera/live",
        "tv_duration_seconds": 25,
        "tv_group": "driveway",
    }

    dispatched = {}

    monkeypatch.setattr(tasks, "is_muted", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(tasks, "check_auto_mute", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(tasks, "build_prompt", lambda *_args, **_kwargs: "Describe motion.")
    monkeypatch.setattr(tasks, "optimize_image", lambda *_args, **_kwargs: "encoded")
    monkeypatch.setattr(tasks, "analyze_image_parallel", lambda *_args, **_kwargs: "Vehicle arrived")
    monkeypatch.setattr(tasks, "send_telegram", lambda cfg, *_args, **_kwargs: cfg.update({"last_msg_id": 321}))
    monkeypatch.setattr(
        tv_delivery,
        "should_dispatch_group_alert",
        lambda *_args, **_kwargs: (True, config),
    )
    monkeypatch.setattr(
        tv_delivery,
        "dispatch_tv_alert",
        lambda cfg, tag: dispatched.setdefault("call", {"config": cfg, "tag": tag}),
    )

    tasks.process_alert(str(image_path), config)

    assert dispatched["call"]["tag"] == "[Front][req12345]"
    assert dispatched["call"]["config"] == {
        "id": "cfg1",
        "name": "Front",
        "request_id": "req12345",
        "tv_rtsp_url": "rtsp://camera/live",
        "tv_duration_seconds": 25,
        "tv_group": "driveway",
        "tv_mute_audio": None,
        "tv_stream_type": "rtsp",
        "bi_url": None,
        "bi_user": None,
        "bi_pass": None,
    }


def test_process_alert_logs_mjpg_skip_reason_without_rtsp_message(tmp_path, monkeypatch, caplog):
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
        "tv_push_enabled": 1,
        "tv_stream_type": "mjpg",
        "bi_url": "http://blueiris.local",
        "dvla_api_key": "",
        "verbose_logging": 0,
    }

    monkeypatch.setattr(tasks, "is_muted", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(tasks, "check_auto_mute", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(tasks, "build_prompt", lambda *_args, **_kwargs: "Describe motion.")
    monkeypatch.setattr(tasks, "optimize_image", lambda *_args, **_kwargs: "encoded")
    monkeypatch.setattr(tasks, "analyze_image_parallel", lambda *_args, **_kwargs: "Vehicle arrived")
    monkeypatch.setattr(tasks, "send_telegram", lambda cfg, *_args, **_kwargs: cfg.update({"last_msg_id": 321}))
    monkeypatch.setattr(
        tv_delivery,
        "dispatch_tv_alert",
        lambda *_args, **_kwargs: {"skipped": True, "payload": {"mjpg_url": None, "rtsp_url": None}},
    )

    with caplog.at_level(logging.INFO):
        tasks.process_alert(str(image_path), config)

    assert "TV dispatch skipped (no MJPG proxy URL)" in caplog.text
    assert "no RTSP URL" not in caplog.text


def test_process_alert_skips_tv_dispatch_when_higher_priority_group_camera_is_active(tmp_path, monkeypatch, caplog):
    image_path = tmp_path / "alert.jpg"
    image_path.write_bytes(b"fake-image")

    config = {
        "id": "cfg-low",
        "name": "Lower Driveway",
        "request_id": "req12345",
        "telegram_token": "token",
        "chat_id": "chat",
        "prompt": "Describe motion.",
        "instant_notify": 0,
        "send_video": 0,
        "trigger_filename": "",
        "tv_push_enabled": 1,
        "tv_rtsp_url": "rtsp://camera/live",
        "tv_duration_seconds": 25,
        "tv_group": "driveway",
    }

    dispatch_calls = []

    monkeypatch.setattr(tasks, "is_muted", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(tasks, "check_auto_mute", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(tasks, "build_prompt", lambda *_args, **_kwargs: "Describe motion.")
    monkeypatch.setattr(tasks, "optimize_image", lambda *_args, **_kwargs: "encoded")
    monkeypatch.setattr(tasks, "analyze_image_parallel", lambda *_args, **_kwargs: "Vehicle arrived")
    monkeypatch.setattr(tasks, "send_telegram", lambda cfg, *_args, **_kwargs: cfg.update({"last_msg_id": 321}))
    monkeypatch.setattr(
        tv_delivery,
        "should_dispatch_group_alert",
        lambda *_args, **_kwargs: (
            False,
            {"id": "cfg-high", "name": "High Driveway"},
        ),
    )
    monkeypatch.setattr(
        tv_delivery,
        "dispatch_tv_alert",
        lambda *_args, **_kwargs: dispatch_calls.append(True),
    )

    with caplog.at_level(logging.INFO):
        tasks.process_alert(str(image_path), config)

    assert dispatch_calls == []
    assert "TV dispatch skipped (higher-priority camera active: 'High Driveway')" in caplog.text


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


def test_check_auto_mute_uses_global_settings(monkeypatch):
    stored = {}

    class DummyRedis:
        def lpush(self, key, value):
            stored.setdefault(key, []).insert(0, value)

        def ltrim(self, key, start, end):
            stored[key] = stored.get(key, [])[start : end + 1]

        def expire(self, key, _seconds):
            return True

        def lrange(self, key, _start, _end):
            return [item.encode() if isinstance(item, str) else item for item in stored.get(key, [])]

        def set(self, key, value, ex=None):
            stored[key] = {"value": value, "ex": ex}

        def delete(self, key):
            stored.pop(key, None)

    monkeypatch.setattr(
        tasks,
        "get_auto_mute_settings",
        lambda: {"threshold": 2, "window_minutes": 15, "duration_minutes": 45},
    )
    monkeypatch.setattr(tasks, "r", DummyRedis())

    config = {"id": "cfg-1", "name": "Driveway", "chat_id": "chat-1"}

    assert tasks.check_auto_mute(config) is False
    assert tasks.check_auto_mute(config) is True

    mute_key = "mute:driveway:chat-1"
    assert mute_key in stored
    assert stored[mute_key]["ex"] == 45 * 60 + 60
    assert datetime.fromisoformat(stored[mute_key]["value"]) > datetime.now()


def test_send_auto_mute_notification_uses_global_settings(monkeypatch):
    captured = {}

    def fake_post(url, data=None, timeout=None):
        captured.update({"url": url, "data": data, "timeout": timeout})
        return _DummyResponse()

    monkeypatch.setattr(
        tasks,
        "get_auto_mute_settings",
        lambda: {"threshold": 7, "window_minutes": 15, "duration_minutes": 45},
    )
    monkeypatch.setattr(tasks.requests, "post", fake_post)

    tasks.send_auto_mute_notification(
        {
            "name": "Driveway",
            "request_id": "abc12345",
            "telegram_token": "token",
            "chat_id": "chat-1",
        }
    )

    assert "sendMessage" in captured["url"]
    assert "45 min" in captured["data"]["text"]
    assert "7+ triggers in 15 min" in captured["data"]["text"]


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


# ---------------------------------------------------------------------------
# send_telegram — retry and degraded-mode tests (issue #135)
# ---------------------------------------------------------------------------

class _CountingResponse:
    """Fails for the first `fail_count` calls, then succeeds."""

    def __init__(self, fail_count=1, fail_status=500, success_payload=None):
        self._calls = 0
        self._fail_count = fail_count
        self._fail_status = fail_status
        self._success_payload = success_payload or {"result": {"message_id": 42}}

    def __call__(self, *args, **kwargs):
        self._calls += 1
        if self._calls <= self._fail_count:
            return _DummyResponse(ok=False, status_code=self._fail_status)
        return _DummyResponse(ok=True, payload=self._success_payload)

    @property
    def call_count(self):
        return self._calls


def test_send_telegram_retries_on_server_error_then_succeeds(tmp_path, monkeypatch, caplog):
    image_path = tmp_path / "alert.jpg"
    image_path.write_bytes(b"fake-image")

    config = {
        "name": "Driveway",
        "request_id": "abc12345",
        "telegram_token": "token",
        "chat_id": "chat",
    }

    responder = _CountingResponse(fail_count=1, fail_status=503)
    monkeypatch.setattr(tasks.requests, "post", responder)
    monkeypatch.setattr(tasks.time, "sleep", lambda _: None)

    with caplog.at_level(logging.INFO):
        tasks.send_telegram(config, str(image_path), "Motion detected.", max_attempts=3)

    assert config.get("last_msg_id") == 42
    assert not config.get("still_delivery_failed")
    assert responder.call_count == 2
    assert "phase=telegram_photo_send_failed" in caplog.text
    assert "error_category=server_error" in caplog.text
    assert "phase=telegram_photo_sent" in caplog.text


def test_send_telegram_no_retry_on_auth_error(tmp_path, monkeypatch, caplog):
    image_path = tmp_path / "alert.jpg"
    image_path.write_bytes(b"fake-image")

    config = {
        "name": "Driveway",
        "request_id": "abc12345",
        "telegram_token": "token",
        "chat_id": "chat",
    }

    call_count = {"n": 0}

    def fake_post(*args, **kwargs):
        call_count["n"] += 1
        return _DummyResponse(ok=False, status_code=401,
                               payload={"description": "Unauthorized"})

    monkeypatch.setattr(tasks.requests, "post", fake_post)
    monkeypatch.setattr(tasks.time, "sleep", lambda _: None)

    with caplog.at_level(logging.ERROR):
        tasks.send_telegram(config, str(image_path), "Motion detected.", max_attempts=3)

    assert call_count["n"] == 1
    assert config.get("still_delivery_failed") is True
    assert "error_category=auth" in caplog.text
    assert "attempt=1/3" in caplog.text


def test_send_telegram_all_retries_exhausted_sets_degraded_flag(tmp_path, monkeypatch, caplog):
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
        lambda *args, **kwargs: _DummyResponse(ok=False, status_code=503),
    )
    monkeypatch.setattr(tasks.time, "sleep", lambda _: None)

    with caplog.at_level(logging.WARNING):
        tasks.send_telegram(config, str(image_path), "Motion detected.", max_attempts=3)

    assert config.get("still_delivery_failed") is True
    assert not config.get("last_msg_id")
    assert "attempt=3/3" in caplog.text
    assert "error_category=server_error" in caplog.text


def test_send_telegram_transport_error_is_retriable(tmp_path, monkeypatch, caplog):
    import requests as req_lib

    image_path = tmp_path / "alert.jpg"
    image_path.write_bytes(b"fake-image")

    config = {
        "name": "Driveway",
        "request_id": "abc12345",
        "telegram_token": "token",
        "chat_id": "chat",
    }

    call_count = {"n": 0}

    def fake_post(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise req_lib.exceptions.ConnectionError("network down")
        return _DummyResponse(ok=True, payload={"result": {"message_id": 99}})

    monkeypatch.setattr(tasks.requests, "post", fake_post)
    monkeypatch.setattr(tasks.time, "sleep", lambda _: None)

    with caplog.at_level(logging.WARNING):
        tasks.send_telegram(config, str(image_path), "Motion detected.", max_attempts=3)

    assert config.get("last_msg_id") == 99
    assert not config.get("still_delivery_failed")
    assert call_count["n"] == 3
    assert "error_category=transport" in caplog.text


def test_send_telegram_success_after_prior_failure_clears_degraded_flag(tmp_path, monkeypatch):
    """A successful retry clears any still_delivery_failed flag left from a prior call."""
    image_path = tmp_path / "alert.jpg"
    image_path.write_bytes(b"fake-image")

    config = {
        "name": "Driveway",
        "request_id": "abc12345",
        "telegram_token": "token",
        "chat_id": "chat",
        "still_delivery_failed": True,
    }

    monkeypatch.setattr(
        tasks.requests,
        "post",
        lambda *args, **kwargs: _DummyResponse(ok=True, payload={"result": {"message_id": 7}}),
    )
    monkeypatch.setattr(tasks.time, "sleep", lambda _: None)

    tasks.send_telegram(config, str(image_path), "Motion detected.")

    assert config.get("last_msg_id") == 7
    assert not config.get("still_delivery_failed")
