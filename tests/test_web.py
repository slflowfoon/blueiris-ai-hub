import io
import sqlite3
import uuid
from unittest.mock import MagicMock, patch
import wsgi


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


def test_sqlite_wal_mode_enabled(client):
    """Test that the application database is configured for WAL mode."""
    with sqlite3.connect(wsgi.DB_FILE) as conn:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(journal_mode).lower() == "wal"
