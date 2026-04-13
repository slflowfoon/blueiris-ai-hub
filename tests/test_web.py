import sqlite3

import wsgi


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
