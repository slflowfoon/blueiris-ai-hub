import io
import importlib
import sqlite3
import uuid
from unittest.mock import MagicMock, patch
import wsgi
from settings_store import get_global_settings


class FakeRedis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    def setex(self, key, ex, value):
        self.store[key] = value
        return True

    def delete(self, key):
        self.store.pop(key, None)
        return 1


def _insert_config(config_id):
    """Insert a minimal camera config row for webhook tests."""
    with sqlite3.connect(wsgi.DB_FILE) as conn:
        conn.execute(
            "INSERT INTO configs (id, name, gemini_key, telegram_token, chat_id, prompt) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (config_id, "TestCam", "gk", "ttoken", "123", "describe"),
        )


def _fake_image():
    return (io.BytesIO(b"\xff\xd8\xff\xe0" + b"\x00" * 16), "alert_20240101_120000.jpg")


def test_webhook_dedup_same_alert(client):
    """Second request with same bvr+trigger_filename returns duplicate and does not enqueue."""
    config_id = uuid.uuid4().hex
    _insert_config(config_id)

    fake_redis = MagicMock()
    fake_redis.set.side_effect = [1, None]  # first succeeds, second blocked
    fake_queue = MagicMock()

    with patch.object(wsgi, "r", fake_redis), patch.object(wsgi, "q", fake_queue):
        img1, name1 = _fake_image()
        r1 = client.post(
            f"/webhook/{config_id}",
            data={"image": (img1, name1), "bvr": "20240101_clip.bvr"},
            content_type="multipart/form-data",
        )
        assert r1.status_code == 200
        assert r1.get_json()["status"] == "queued"

        img2, name2 = _fake_image()
        r2 = client.post(
            f"/webhook/{config_id}",
            data={"image": (img2, name2), "bvr": "20240101_clip.bvr"},
            content_type="multipart/form-data",
        )
        assert r2.status_code == 200
        assert r2.get_json()["status"] == "duplicate"

    assert fake_queue.enqueue.call_count == 1


def test_webhook_dedup_different_trigger_on_same_bvr(client):
    """Two alerts sharing the same .bvr but different trigger filenames both queue normally."""
    config_id = uuid.uuid4().hex
    _insert_config(config_id)

    fake_redis = MagicMock()
    fake_redis.set.return_value = 1  # always succeeds — different keys
    fake_queue = MagicMock()

    with patch.object(wsgi, "r", fake_redis), patch.object(wsgi, "q", fake_queue):
        img1, _ = _fake_image()
        r1 = client.post(
            f"/webhook/{config_id}",
            data={"image": (img1, "alert_20240101_120000.jpg"), "bvr": "20240101_clip.bvr"},
            content_type="multipart/form-data",
        )
        assert r1.status_code == 200
        assert r1.get_json()["status"] == "queued"

        img2, _ = _fake_image()
        r2 = client.post(
            f"/webhook/{config_id}",
            data={"image": (img2, "alert_20240101_120040.jpg"), "bvr": "20240101_clip.bvr"},
            content_type="multipart/form-data",
        )
        assert r2.status_code == 200
        assert r2.get_json()["status"] == "queued"

    assert fake_queue.enqueue.call_count == 2


def test_dashboard_loads(client):
    """Test that the main dashboard loads successfully."""
    response = client.get('/')
    assert response.status_code == 200
    assert b"Blue Iris AI Hub" in response.data
    assert b"logo-mark.svg" in response.data
    assert b"Auto-mute Policy" in response.data
    assert b"Pair TV" in response.data
    assert b"Push stream to TV overlay" in response.data
    assert b"Alert Controls" in response.data
    assert b"TV Settings" in response.data
    assert b"Copy Trace" in response.data
    assert b"copyWebhookTrace(this)" in response.data


def test_api_check_update(client, monkeypatch):
    """Test that the update API endpoint returns JSON."""
    monkeypatch.setattr(wsgi.r, "get", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(wsgi.r, "set", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        wsgi,
        "get_update_status",
        lambda: {"update_available": False, "latest_version": None, "current_version": "test"},
    )
    response = client.get('/api/check-update')
    assert response.status_code == 200
    data = response.get_json()
    assert "update_available" in data


def test_init_db_adds_tv_columns_and_tables(tmp_path, monkeypatch):
    original_state = {
        "DATA_DIR": wsgi.DATA_DIR,
        "DB_FILE": wsgi.DB_FILE,
        "KNOWN_PLATES_FILE": wsgi.KNOWN_PLATES_FILE,
        "TEMP_IMAGE_DIR": wsgi.TEMP_IMAGE_DIR,
        "LOG_FILE": wsgi.LOG_FILE,
    }
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    try:
        importlib.reload(wsgi)

        with sqlite3.connect(wsgi.DB_FILE) as conn:
            config_columns = {row[1] for row in conn.execute("PRAGMA table_info(configs)")}
            assert "tv_push_enabled" in config_columns
            assert "tv_rtsp_url" in config_columns
            assert "tv_duration_seconds" in config_columns
            assert "tv_group" in config_columns

            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert "paired_tvs" in tables
            assert "camera_tv_targets" in tables
            assert "camera_group_priorities" in tables
    finally:
        wsgi.DATA_DIR = original_state["DATA_DIR"]
        wsgi.DB_FILE = original_state["DB_FILE"]
        wsgi.KNOWN_PLATES_FILE = original_state["KNOWN_PLATES_FILE"]
        wsgi.TEMP_IMAGE_DIR = original_state["TEMP_IMAGE_DIR"]
        wsgi.LOG_FILE = original_state["LOG_FILE"]


def test_add_config_persists_tv_settings(client, monkeypatch):
    monkeypatch.setattr(wsgi, "get_mute_status", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(wsgi, "get_caption_mode", lambda *_args, **_kwargs: None)

    response = client.post("/add", data={
        "name": "Driveway",
        "gemini_key": "g",
        "telegram_token": "t",
        "chat_id": "1",
        "prompt": "Describe motion.",
        "tv_push_enabled": "on",
        "tv_rtsp_base_url": "rtsp://192.168.1.50:554/stream1",
        "tv_rtsp_username": "camuser",
        "tv_rtsp_password": "secret",
        "tv_duration_seconds": "20",
        "tv_group": "driveway",
    }, follow_redirects=True)

    assert response.status_code == 200

    conn = wsgi.get_db_connection()
    row = conn.execute(
        "SELECT tv_push_enabled, tv_rtsp_url, tv_duration_seconds, tv_group FROM configs WHERE name=?",
        ("Driveway",),
    ).fetchone()
    conn.close()

    assert row["tv_push_enabled"] == 1
    assert row["tv_rtsp_url"] == "rtsp://camuser:secret@192.168.1.50:554/stream1"
    assert row["tv_duration_seconds"] == 20
    assert row["tv_group"] == "driveway"


def test_edit_config_rtsp_password_with_at_sign(client, monkeypatch):
    """Passwords containing @ must be stored with literal @ (not %40) so media3 authenticates correctly."""
    monkeypatch.setattr(wsgi, "get_mute_status", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(wsgi, "get_caption_mode", lambda *_args, **_kwargs: None)

    response = client.post("/add", data={
        "name": "Driveway",
        "gemini_key": "g",
        "telegram_token": "t",
        "chat_id": "1",
        "prompt": "Describe motion.",
        "tv_push_enabled": "on",
        "tv_rtsp_base_url": "rtsp://192.168.90.2:554/h264Preview_01_main",
        "tv_rtsp_username": "monkeyrush",
        "tv_rtsp_password": ".RoKff@wgYDfvFV4@JdE",
        "tv_duration_seconds": "20",
        "tv_group": "",
    }, follow_redirects=True)
    assert response.status_code == 200

    conn = wsgi.get_db_connection()
    row = conn.execute("SELECT tv_rtsp_url FROM configs WHERE name=?", ("Driveway",)).fetchone()
    conn.close()

    # @ in password must not be percent-encoded — media3 uses Uri.getUserInfo() which
    # returns the raw (still-encoded) string, so %40 would be sent literally to the camera
    assert row["tv_rtsp_url"] == "rtsp://monkeyrush:.RoKff@wgYDfvFV4@JdE@192.168.90.2:554/h264Preview_01_main"


def test_edit_config_preserves_existing_rtsp_password_when_blank(client, monkeypatch):
    monkeypatch.setattr(wsgi, "get_mute_status", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(wsgi, "get_caption_mode", lambda *_args, **_kwargs: None)

    response = client.post("/add", data={
        "name": "Driveway",
        "gemini_key": "g",
        "telegram_token": "t",
        "chat_id": "1",
        "prompt": "Describe motion.",
        "tv_push_enabled": "on",
        "tv_rtsp_base_url": "rtsp://192.168.1.50:554/stream1",
        "tv_rtsp_username": "camuser",
        "tv_rtsp_password": "secret",
        "tv_duration_seconds": "20",
        "tv_group": "driveway",
    }, follow_redirects=True)
    assert response.status_code == 200

    conn = wsgi.get_db_connection()
    row = conn.execute("SELECT id FROM configs WHERE name=?", ("Driveway",)).fetchone()
    conn.close()

    response = client.post(f"/edit/{row['id']}", data={
        "name": "Driveway",
        "gemini_key": "g",
        "telegram_token": "t",
        "chat_id": "1",
        "prompt": "Describe motion.",
        "tv_push_enabled": "on",
        "tv_rtsp_base_url": "rtsp://192.168.1.50:554/stream2",
        "tv_rtsp_username": "camuser",
        "tv_rtsp_password": "",
        "tv_duration_seconds": "20",
        "tv_group": "driveway",
    }, follow_redirects=True)
    assert response.status_code == 200

    conn = wsgi.get_db_connection()
    updated = conn.execute(
        "SELECT tv_rtsp_url FROM configs WHERE id=?",
        (row["id"],),
    ).fetchone()
    conn.close()

    assert updated["tv_rtsp_url"] == "rtsp://camuser:secret@192.168.1.50:554/stream2"


def test_pair_tv_by_manual_code_with_ip_pairs_remote_tv(client, monkeypatch):
    import tv_delivery

    captured = {}

    def fake_pair_remote_tv_by_code(ip_address, manual_code, port=7979):
        captured["ip_address"] = ip_address
        captured["manual_code"] = manual_code
        captured["port"] = port
        return "tv-remote-1"

    monkeypatch.setattr(tv_delivery, "pair_remote_tv_by_code", fake_pair_remote_tv_by_code)

    response = client.post(
        "/tv/pair/code",
        data={"manual_code": "ABC123", "ip_address": "192.168.10.6", "port": "7979"},
    )

    assert response.status_code == 200
    assert response.get_json() == {"status": "paired", "tv_id": "tv-remote-1"}
    assert captured == {
        "ip_address": "192.168.10.6",
        "manual_code": "ABC123",
        "port": 7979,
    }


def test_pair_tv_by_manual_code(client, monkeypatch):
    import tv_delivery

    fake_redis = FakeRedis()
    monkeypatch.setattr(tv_delivery, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(wsgi, "get_redis_client", lambda: fake_redis)

    session = tv_delivery.create_pairing_session({
        "tv_name": "Lounge TV",
        "ip_address": "192.168.1.88",
        "port": 7979,
        "device_id": "tv-device-web-1",
    })

    response = client.post("/tv/pair/code", data={"manual_code": session["manual_code"]})
    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "paired"
    assert data["tv_id"]


def test_pair_tv_by_manual_code_returns_generic_server_error(client, monkeypatch):
    import tv_delivery

    def fail_pair_remote_tv_by_code(_ip_address, _manual_code, port=7979):
        raise RuntimeError(f"boom on port {port}")

    monkeypatch.setattr(tv_delivery, "pair_remote_tv_by_code", fail_pair_remote_tv_by_code)

    response = client.post(
        "/tv/pair/code",
        data={"manual_code": "ABC123", "ip_address": "192.168.10.6", "port": "7979"},
    )

    assert response.status_code == 500
    assert response.get_json() == {"error": "tv pairing failed"}


def test_pair_tv_by_manual_code_sanitizes_unexpected_value_error(client, monkeypatch):
    import tv_delivery

    def fail_pair_remote_tv_by_code(_ip_address, _manual_code, port=7979):
        raise ValueError(f"unexpected failure on port {port}")

    monkeypatch.setattr(tv_delivery, "pair_remote_tv_by_code", fail_pair_remote_tv_by_code)

    response = client.post(
        "/tv/pair/code",
        data={"manual_code": "ABC123", "ip_address": "192.168.10.6", "port": "7979"},
    )

    assert response.status_code == 400
    assert response.get_json() == {"error": "invalid tv pairing request"}


def test_test_tv_route_sanitizes_dispatch_result(client, monkeypatch):
    import tv_delivery

    config_id = uuid.uuid4().hex
    _insert_config(config_id)

    with sqlite3.connect(wsgi.DB_FILE) as conn:
        conn.execute(
            """
            UPDATE configs
            SET tv_push_enabled=1,
                tv_rtsp_url='rtsp://camera/stream'
            WHERE id=?
            """,
            (config_id,),
        )

    monkeypatch.setattr(
        tv_delivery,
        "dispatch_tv_alert",
        lambda *_args, **_kwargs: {
            "delivered": ["tv-1"],
            "failed": ["tv-2"],
            "error": "boom with internal details",
            "payload": {"shared_secret": "s3cr3t"},
        },
    )

    response = client.post(f"/test-tv/{config_id}")

    assert response.status_code == 502
    assert response.get_json() == {"error": "dispatch failed"}


def test_test_tv_route_returns_sent_status_on_success(client, monkeypatch):
    import tv_delivery

    config_id = uuid.uuid4().hex
    _insert_config(config_id)

    with sqlite3.connect(wsgi.DB_FILE) as conn:
        conn.execute(
            """
            UPDATE configs
            SET tv_push_enabled=1,
                tv_rtsp_url='rtsp://camera/stream'
            WHERE id=?
            """,
            (config_id,),
        )

    monkeypatch.setattr(
        tv_delivery,
        "dispatch_tv_alert",
        lambda *_args, **_kwargs: {"delivered": ["tv-1"], "failed": []},
    )

    response = client.post(f"/test-tv/{config_id}")

    assert response.status_code == 200
    assert response.get_json() == {"status": "sent"}


def test_test_tv_route_uses_configured_tv_duration(client, monkeypatch):
    import tv_delivery

    config_id = uuid.uuid4().hex
    _insert_config(config_id)

    with sqlite3.connect(wsgi.DB_FILE) as conn:
        conn.execute(
            """
            UPDATE configs
            SET tv_push_enabled=1,
                tv_rtsp_url='rtsp://camera/stream',
                tv_duration_seconds=27
            WHERE id=?
            """,
            (config_id,),
        )

    captured = {}

    def fake_dispatch_tv_alert(dispatch_config, _tag):
        captured["duration"] = dispatch_config["tv_duration_seconds"]
        return {"delivered": ["tv-1"], "failed": []}

    monkeypatch.setattr(tv_delivery, "dispatch_tv_alert", fake_dispatch_tv_alert)

    response = client.post(f"/test-tv/{config_id}")

    assert response.status_code == 200
    assert response.get_json() == {"status": "sent"}
    assert captured["duration"] == 27


def test_test_tv_route_logs_failed_targets(client, monkeypatch, caplog):
    import logging
    import tv_delivery

    config_id = uuid.uuid4().hex
    _insert_config(config_id)

    with sqlite3.connect(wsgi.DB_FILE) as conn:
        conn.execute(
            """
            UPDATE configs
            SET tv_push_enabled=1,
                tv_rtsp_url='rtsp://camera/stream'
            WHERE id=?
            """,
            (config_id,),
        )

    monkeypatch.setattr(
        tv_delivery,
        "dispatch_tv_alert",
        lambda *_args, **_kwargs: {"delivered": [], "failed": ["tv-1", "tv-2"]},
    )

    with caplog.at_level(logging.WARNING):
        response = client.post(f"/test-tv/{config_id}")

    assert response.status_code == 502
    assert response.get_json() == {"error": "dispatch failed"}
    assert "failed_targets=tv-1,tv-2" in caplog.text
    assert "reason=delivery_failed" in caplog.text


def test_test_tv_route_logs_no_target_reason(client, monkeypatch, caplog):
    import logging
    import tv_delivery

    config_id = uuid.uuid4().hex
    _insert_config(config_id)

    with sqlite3.connect(wsgi.DB_FILE) as conn:
        conn.execute(
            """
            UPDATE configs
            SET tv_push_enabled=1,
                tv_rtsp_url='rtsp://camera/stream'
            WHERE id=?
            """,
            (config_id,),
        )

    monkeypatch.setattr(
        tv_delivery,
        "dispatch_tv_alert",
        lambda *_args, **_kwargs: {"delivered": [], "failed": []},
    )

    with caplog.at_level(logging.WARNING):
        response = client.post(f"/test-tv/{config_id}")

    assert response.status_code == 502
    assert response.get_json() == {"error": "dispatch failed"}
    assert "reason=no_target_tvs" in caplog.text


def test_test_tv_route_logs_missing_base_url_reason_for_mjpg(client, monkeypatch, caplog):
    import logging
    import tv_delivery

    config_id = uuid.uuid4().hex
    _insert_config(config_id)

    with sqlite3.connect(wsgi.DB_FILE) as conn:
        conn.execute(
            """
            UPDATE configs
            SET tv_push_enabled=1,
                tv_stream_type='mjpg',
                bi_url='http://blueiris.local'
            WHERE id=?
            """,
            (config_id,),
        )

    monkeypatch.setattr(tv_delivery, "BASE_URL", "")

    with caplog.at_level(logging.WARNING):
        response = client.post(f"/test-tv/{config_id}")

    assert response.status_code == 502
    assert response.get_json() == {"error": "dispatch failed"}
    assert "reason=missing_base_url" in caplog.text


def test_dashboard_shows_tv_apk_downloader_url(client):
    response = client.get("/")

    assert response.status_code == 200
    assert b"/downloads/android-tv-overlay.apk" in response.data
    assert b"TV App Downloader URL" in response.data


def test_download_tv_overlay_apk_redirects_to_override_url(client, monkeypatch):
    monkeypatch.setattr(wsgi, "TV_OVERLAY_APK_URL", "https://example.com/pr-133/app-debug.apk")

    response = client.get("/downloads/android-tv-overlay.apk")

    assert response.status_code == 302
    assert response.headers["Location"] == "https://example.com/pr-133/app-debug.apk"


def test_download_tv_overlay_apk_redirects_to_latest_github_release(client, monkeypatch):
    monkeypatch.setattr(wsgi, "TV_OVERLAY_APK_URL", "")
    monkeypatch.setattr(
        wsgi.requests,
        "get",
        lambda *a, **kw: type(
            "Resp",
            (),
            {
                "ok": True,
                "json": lambda self: {
                    "assets": [
                        {"name": "pipup-v1.2.3.apk", "browser_download_url": "https://github.com/example/pipup-v1.2.3.apk"}
                    ]
                },
            },
        )(),
    )

    response = client.get("/downloads/android-tv-overlay.apk")

    assert response.status_code == 302
    assert response.headers["Location"] == "https://github.com/example/pipup-v1.2.3.apk"


def test_download_tv_overlay_apk_returns_404_when_missing(client, tmp_path, monkeypatch):
    monkeypatch.setattr(wsgi, "TV_OVERLAY_APK_URL", "")
    monkeypatch.setattr(wsgi.requests, "get", lambda *a, **kw: (_ for _ in ()).throw(ConnectionError("offline")))

    response = client.get("/downloads/android-tv-overlay.apk")
    expected = {
        "error": (
            "android tv overlay apk not found — no PR override URL configured and no GitHub release APK found"
        )
    }

    assert response.status_code == 404
    assert response.get_json() == expected


def test_delete_paired_tv_redirects_back_to_tv_settings(client, tmp_path, monkeypatch):
    import tv_delivery

    db_path = tmp_path / "paired_tv_delete.sqlite"
    original_db_file = wsgi.DB_FILE

    try:
        wsgi.DB_FILE = str(db_path)
        wsgi.init_db()
        fake_redis = FakeRedis()
        monkeypatch.setattr(tv_delivery, "get_redis_client", lambda: fake_redis)

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO paired_tvs (id, name, ip_address, port, shared_secret)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("tv-1", "Living Room", "192.168.1.88", 7979, "secret"),
            )

        response = client.post("/tv/devices/tv-1/delete", follow_redirects=False)

        assert response.status_code == 302
        assert response.headers["Location"].endswith("/#tv-groups-pane")

        with sqlite3.connect(db_path) as conn:
            remaining = conn.execute("SELECT COUNT(*) FROM paired_tvs").fetchone()[0]
        assert remaining == 0
    finally:
        wsgi.DB_FILE = original_db_file


def test_tv_group_priorities_render_saved_camera_order(client, tmp_path):
    db_path = tmp_path / "tv_group_priority_index.sqlite"
    original_db_file = wsgi.DB_FILE

    try:
        wsgi.DB_FILE = str(db_path)
        wsgi.init_db()
        wsgi.get_mute_status = lambda *_args, **_kwargs: []
        wsgi.get_caption_mode = lambda *_args, **_kwargs: None

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO configs (
                    id, name, gemini_key, telegram_token, chat_id, prompt,
                    tv_push_enabled, tv_rtsp_url, tv_duration_seconds, tv_group
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("cam-high", "High Driveway", "g", "t", "1", "p", 1, "rtsp://high", 20, "driveway"),
            )
            conn.execute(
                """
                INSERT INTO configs (
                    id, name, gemini_key, telegram_token, chat_id, prompt,
                    tv_push_enabled, tv_rtsp_url, tv_duration_seconds, tv_group
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("cam-low", "Lower Driveway", "g", "t", "1", "p", 1, "rtsp://low", 20, "driveway"),
            )
            conn.execute(
                """
                INSERT INTO camera_group_priorities (id, camera_id, group_name, priority)
                VALUES (?, ?, ?, ?)
                """,
                (str(uuid.uuid4()), "cam-high", "driveway", 0),
            )
            conn.execute(
                """
                INSERT INTO camera_group_priorities (id, camera_id, group_name, priority)
                VALUES (?, ?, ?, ?)
                """,
                (str(uuid.uuid4()), "cam-low", "driveway", 1),
            )

        response = client.get("/")

        assert response.status_code == 200
        html = response.data.decode("utf-8")
        assert 'data-group-name="driveway"' in html
        tv_priority_section = html.split('data-group-name="driveway"', 1)[1]
        assert tv_priority_section.index("High Driveway") < tv_priority_section.index("Lower Driveway")
    finally:
        wsgi.DB_FILE = original_db_file


def test_save_tv_group_priority_persists_order(client, tmp_path):
    db_path = tmp_path / "tv_group_priority_save.sqlite"
    original_db_file = wsgi.DB_FILE

    try:
        wsgi.DB_FILE = str(db_path)
        wsgi.init_db()

        response = client.post(
            "/tv/groups/driveway/priority",
            json={"camera_ids": ["cam-high", "cam-low"]},
        )

        assert response.status_code == 200

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT camera_id, group_name, priority
                FROM camera_group_priorities
                ORDER BY priority ASC, created_at ASC, id ASC
                """
            ).fetchall()

        assert [row["camera_id"] for row in rows] == ["cam-high", "cam-low"]
        assert [row["priority"] for row in rows] == [0, 1]
        assert all(row["group_name"] == "driveway" for row in rows)
    finally:
        wsgi.DB_FILE = original_db_file


def test_get_log_entries_marks_webhook_trigger_and_alert_tag(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log_file = log_dir / "system.log"
    log_file.write_text(
        "\n".join(
            [
                (
                    "2026-04-12 12:42:08,592 - INFO - [Driveway][0382523c] "
                    "Webhook triggered. File: "
                    "Driveway.20260412_130000.2517804.3-1.jpg"
                ),
                (
                    "2026-04-12 12:42:08,700 - INFO - [Driveway][0382523c] "
                    "Processing alert... | phase=alert_processing_started"
                ),
            ]
        )
    )

    monkeypatch.setattr(wsgi, "LOG_DIR", str(log_dir))

    entries = wsgi.get_log_entries()

    assert len(entries) == 2
    assert entries[0]["source"] == "system.log"
    assert entries[0]["alert_tag"] == "[Driveway][0382523c]"
    assert entries[0]["is_trigger"] is True
    assert entries[1]["alert_tag"] == "[Driveway][0382523c]"
    assert entries[1]["is_trigger"] is False


def test_get_log_entries_includes_test_tv_tags(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log_file = log_dir / "system.log"
    log_file.write_text(
        "\n".join(
            [
                (
                    "2026-04-17 21:21:05,476 - WARNING - [test-tv:Driveway] "
                    "test dispatch failed reason=no_target_tvs failed_targets=none"
                ),
                "2026-04-17 21:21:06,000 - INFO - [test-tv:Driveway] Follow-up diagnostic line",
            ]
        )
    )

    monkeypatch.setattr(wsgi, "LOG_DIR", str(log_dir))

    entries = wsgi.get_log_entries()

    assert len(entries) == 2
    assert entries[0]["alert_tag"] == "[test-tv:Driveway]"
    assert entries[0]["is_trigger"] is False
    assert entries[1]["alert_tag"] == "[test-tv:Driveway]"


def test_save_global_settings_route_updates_auto_mute_defaults(client):
    response = client.post(
        "/settings/global",
        data={
            "auto_mute_threshold": "7",
            "auto_mute_window_minutes": "15",
            "auto_mute_duration_minutes": "45",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302

    settings = get_global_settings()
    assert settings["auto_mute_threshold"] == "7"
    assert settings["auto_mute_window_minutes"] == "15"
    assert settings["auto_mute_duration_minutes"] == "45"


def test_get_log_entries_keeps_last_100_trigger_groups(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    system_lines = []
    worker_lines = []
    for idx in range(105):
        tag = f"[Driveway][tag{idx:04d}]"
        second = idx % 60
        minute = idx // 60
        timestamp = f"2026-04-13 12:{minute:02d}:{second:02d},000"
        system_lines.append(f"{timestamp} - INFO - {tag} Webhook triggered. File: test-{idx}.jpg")
        worker_lines.append(f"{timestamp} - INFO - {tag} delivery completed | phase=delivery_completed")

    (log_dir / "system.log").write_text("\n".join(system_lines))
    (log_dir / "video_delivery_worker.log").write_text("\n".join(worker_lines))

    monkeypatch.setattr(wsgi, "LOG_DIR", str(log_dir))

    entries = wsgi.get_log_entries()
    tags = {entry["alert_tag"] for entry in entries}

    assert "[Driveway][tag0000]" not in tags
    assert "[Driveway][tag0004]" not in tags
    assert "[Driveway][tag0005]" in tags
    assert "[Driveway][tag0104]" in tags
    assert len({entry["alert_tag"] for entry in entries if entry["is_trigger"]}) == 100
    assert any(entry["source"] == "video_delivery_worker.log" for entry in entries)


def test_sqlite_wal_mode_enabled(client):
    """Test that the application database is configured for WAL mode."""
    with sqlite3.connect(wsgi.DB_FILE) as conn:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(journal_mode).lower() == "wal"


def test_health_endpoint(client, monkeypatch):
    """Test that the health endpoint returns JSON with healthy Redis state."""
    monkeypatch.setattr(wsgi, "get_redis_health", lambda: {"status": "ok"})
    response = client.get('/health')
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok", "redis": {"status": "ok"}}


def test_health_endpoint_degraded(client, monkeypatch):
    """Test that the health endpoint returns 503 when Redis is degraded."""
    monkeypatch.setattr(
        wsgi,
        "get_redis_health",
        lambda: {"status": "error", "error": "ConnectionError"},
    )
    response = client.get('/health')
    assert response.status_code == 503
    assert response.get_json() == {
        "status": "degraded",
        "redis": {"status": "error", "error": "ConnectionError"},
    }


def test_status_endpoint(client, monkeypatch):
    """Test that the status endpoint returns operator-facing pipeline details."""
    monkeypatch.setattr(wsgi, "get_redis_health", lambda: {"status": "ok"})
    monkeypatch.setattr(
        wsgi,
        "get_pipeline_status",
        lambda: {
            "queue_depths": {
                "export_requests": 1,
                "download_requests": 0,
                "video_delivery_requests": 0,
                "active_exports": 1,
            },
            "stale_jobs": {
                "submitted": 0,
                "queued": 0,
                "ready": 0,
                "retry_queued": 0,
                "delivery_processing": 0,
            },
            "services": {"worker": {"status": "ok", "age_seconds": 1.0}},
        },
    )
    response = client.get('/status')
    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "ok"
    assert data["redis"]["status"] == "ok"
    assert data["pipeline"]["queue_depths"]["export_requests"] == 1
