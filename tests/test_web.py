import sqlite3

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


def test_sqlite_wal_mode_enabled(client):
    """Test that the application database is configured for WAL mode."""
    with sqlite3.connect(wsgi.DB_FILE) as conn:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(journal_mode).lower() == "wal"
