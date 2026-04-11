import wsgi


def test_dashboard_loads(client):
    """Test that the main dashboard loads successfully."""
    response = client.get('/')
    assert response.status_code == 200
    assert b"Blue Iris AI Hub" in response.data


def test_api_check_update(client, monkeypatch):
    """Test that the update API endpoint returns JSON."""
    monkeypatch.setattr(wsgi.r, "get", lambda _key: None)
    monkeypatch.setattr(wsgi.r, "set", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        wsgi,
        "get_update_status",
        lambda: {"update_available": False, "latest_version": None},
    )
    response = client.get('/api/check-update')
    assert response.status_code == 200
    data = response.get_json()
    assert "update_available" in data


def test_health_endpoint(client, monkeypatch):
    """Test that the health endpoint returns JSON with healthy Redis state."""
    monkeypatch.setattr(wsgi, "get_redis_health", lambda: {"status": "ok"})
    response = client.get('/health')
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok", "redis": {"status": "ok"}}


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
