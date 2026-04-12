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
