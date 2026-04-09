"""
Tests for bi_monitor.py.

Requires a real Redis instance — CI provides one via the services: redis: block.
Run locally with:
  REDIS_URL=redis://localhost:6379/0 PYTHONPATH=app pytest tests/test_bi_monitor.py -v
"""

import json
import threading
import time
import uuid

# bi_monitor lives in app/ — PYTHONPATH=app is set in CI
import bi_monitor

# Use the same Redis the module uses so helpers and the module share state
_r = bi_monitor.r


# =============================================================================
# Helpers
# =============================================================================

def _push_request(**kwargs):
    req = {
        "request_id": str(uuid.uuid4()),
        "config_name": "TestCam",
        "bi_url": "http://192.168.1.1:81",
        "bi_user": "admin",
        "bi_pass": "secret",
        "trigger_filename": "20240101_120000.jpg",
        "output_path": "/tmp/test_out.mp4",
        "verbose": False,
        "delete_after": False,
        "bi_restart_url": "",
        "bi_restart_token": "",
        "queued_at": time.time(),
    }
    req.update(kwargs)
    _r.rpush("bi:requests", json.dumps(req))
    return req


def _pop_result(request_id, timeout=2):
    item = _r.blpop(f"bi:result:{request_id}", timeout=timeout)
    if item is None:
        return None
    return json.loads(item[1])


# =============================================================================
# Tests
# =============================================================================

class TestStaleRequestGuard:
    def setup_method(self):
        _r.delete("bi:requests")

    def test_stale_request_is_skipped(self):
        """A request older than STALE_REQUEST_AGE should be rejected immediately."""
        req = _push_request(queued_at=time.time() - (bi_monitor.STALE_REQUEST_AGE + 60))
        bi_monitor._process_request(_r.rpop("bi:requests"))
        result = _pop_result(req["request_id"])
        assert result is not None
        assert result["ok"] is False
        assert "stale" in result["error"]

    def test_fresh_request_passes_guard(self, monkeypatch):
        """A fresh request should proceed to _do_export (which we stub)."""
        req = _push_request()
        # FIXED: Return tuple (bool, str)
        monkeypatch.setattr(bi_monitor, "_do_export", lambda r, tag: (True, None))
        bi_monitor._process_request(json.dumps(req).encode())
        result = _pop_result(req["request_id"])
        assert result is not None
        assert result["ok"] is True


class TestResultProtocol:
    def setup_method(self):
        _r.delete("bi:requests")

    def test_result_key_has_ttl(self, monkeypatch):
        """Result key must have a TTL so it self-cleans if worker dies."""
        req = _push_request()
        # FIXED: Return tuple (bool, str)
        monkeypatch.setattr(bi_monitor, "_do_export", lambda r, t: (True, None))
        bi_monitor._process_request(json.dumps(req).encode())
        ttl = _r.ttl(f"bi:result:{req['request_id']}")
        assert 0 < ttl <= bi_monitor.RESULT_KEY_TTL

    def test_failed_export_returns_ok_false(self, monkeypatch):
        req = _push_request()
        # FIXED: Return tuple (bool, str)
        monkeypatch.setattr(bi_monitor, "_do_export", lambda r, t: (False, "export failed"))
        bi_monitor._process_request(json.dumps(req).encode())
        result = _pop_result(req["request_id"])
        assert result["ok"] is False
        assert result["error"] == "export failed"

    def test_exception_in_do_export_returns_error(self, monkeypatch):
        req = _push_request()

        def _boom(r, t):
            raise RuntimeError("connection refused")

        monkeypatch.setattr(bi_monitor, "_do_export", _boom)
        bi_monitor._process_request(json.dumps(req).encode())
        result = _pop_result(req["request_id"])
        assert result["ok"] is False
        assert "connection refused" in result["error"]

    def test_malformed_json_does_not_crash(self):
        """Malformed payload must be silently dropped with no result key."""
        bi_monitor._process_request(b"not json {{{")


class TestSessionCache:
    def setup_method(self):
        bi_monitor._session_cache.clear()

    def test_session_stored_after_login(self, monkeypatch):
        monkeypatch.setattr(bi_monitor, "bi_login", lambda sess, base_url, user, password, tag: "fake-sid-123")
        sess, sid = bi_monitor._get_session("http://bi:81", "admin", "pw", "[T]")
        assert sid == "fake-sid-123"
        assert ("http://bi:81", "admin") in bi_monitor._session_cache

    def test_session_reused_on_second_call(self, monkeypatch):
        """Second call must reuse cached session without calling bi_login again."""
        import unittest.mock as mock

        fake_sess = mock.MagicMock()
        fake_sess.post.return_value = mock.MagicMock(
            status_code=200,
            json=lambda: {"result": "success"},
        )
        bi_monitor._session_cache[("http://bi:81", "admin")] = (fake_sess, "cached-sid")

        login_calls = []
        monkeypatch.setattr(bi_monitor, "bi_login", lambda *a, **kw: login_calls.append(1) or "new-sid")

        sess, sid = bi_monitor._get_session("http://bi:81", "admin", "pw", "[T]")
        assert sid == "cached-sid"
        assert login_calls == []

    def test_invalidate_removes_entry(self):
        bi_monitor._session_cache[("http://bi:81", "admin")] = ("sess", "sid")
        bi_monitor._invalidate_session("http://bi:81", "admin")
        assert ("http://bi:81", "admin") not in bi_monitor._session_cache


class TestRunMonitorLoop:
    def setup_method(self):
        _r.delete("bi:requests")
        bi_monitor._session_cache.clear()

    def test_processes_queued_request(self, monkeypatch):
        """run_monitor must pop and process a queued request then return on next empty poll."""
        req = _push_request()
        processed = []

        def _fake_process(raw):
            processed.append(json.loads(raw))

        monkeypatch.setattr(bi_monitor, "_process_request", _fake_process)
        monkeypatch.setattr(bi_monitor, "BLPOP_BLOCK_TIMEOUT", 1)

        t = threading.Thread(target=bi_monitor.run_monitor, daemon=True)
        t.start()
        t.join(timeout=4)

        assert len(processed) >= 1
        assert processed[0]["request_id"] == req["request_id"]


class TestPreResolvedClip:
    """#45 -- monitor must use pre-resolved clip_path from payload and skip alertlist lookup."""

    def setup_method(self):
        _r.delete("bi:requests")
        bi_monitor._session_cache.clear()

    def test_pre_resolved_clip_skips_alertlist(self, monkeypatch):
        """When clip_path is in payload, bi_find_alert_details must not be called."""
        alertlist_calls = []
        monkeypatch.setattr(
            bi_monitor, "bi_find_alert_details",
            lambda *a, **kw: alertlist_calls.append(1) or ("@clip/foo.mp4", 0, 10000),
        )
        import unittest.mock as mock

        fake_sess = mock.MagicMock()
        fake_sess.post.return_value = mock.MagicMock(
            status_code=200,
            json=lambda: {"result": "success", "data": {"path": "@clip/foo"}},
        )
        fake_dl = mock.MagicMock(status_code=200)
        fake_dl.headers = {"Content-Length": "2000"}  # Must be > 1000
        fake_dl.iter_content = lambda chunk_size=0: [b"x" * 2000]
        fake_dl.__enter__ = lambda s: fake_dl
        fake_dl.__exit__ = mock.MagicMock(return_value=False)
        fake_sess.get.return_value = fake_dl

        monkeypatch.setattr(bi_monitor, "_get_session", lambda *a, **kw: (fake_sess, "sid"))
        monkeypatch.setattr(bi_monitor, "bi_wait_for_export_ready", lambda *a, **kw: "clips/foo.mp4")
        monkeypatch.setattr(bi_monitor, "bi_delete_clip", lambda *a, **kw: None)

        req = {
            "request_id": str(uuid.uuid4()),
            "config_name": "TestCam",
            "bi_url": "http://192.168.1.1:81",
            "bi_user": "admin",
            "bi_pass": "secret",
            "trigger_filename": "20240101_120000.jpg",
            "clip_path": "@clip/20240101_120000.mp4",
            "offset": 0,
            "duration": 10000,
            "output_path": "/tmp/test_pre_resolved.mp4",
            "verbose": False,
            "delete_after": False,
            "bi_restart_url": "",
            "bi_restart_token": "",
            "queued_at": time.time(),
        }
        # FIXED: Unpack tuple
        ok, err = bi_monitor._do_export(req, "[TestPreResolved]")
        assert ok is True
        assert alertlist_calls == [], "bi_find_alert_details must not be called when clip_path is pre-resolved"


class TestPersistent404FastFail:
    """#46 -- 50+ consecutive 404s must break out of download loop early."""

    def setup_method(self):
        _r.delete("bi:requests")
        bi_monitor._session_cache.clear()

    def test_fast_fail_on_50_consecutive_404s(self, monkeypatch):
        """Download loop must give up after 50 consecutive 404s instead of running full timeout."""
        import unittest.mock as mock

        fake_sess = mock.MagicMock()
        fake_sess.post.return_value = mock.MagicMock(
            status_code=200,
            json=lambda: {"result": "success", "data": {"path": "@clip/foo"}},
        )
        not_found = mock.MagicMock(status_code=404)
        not_found.headers = {}
        not_found.__enter__ = lambda s: not_found
        not_found.__exit__ = mock.MagicMock(return_value=False)
        fake_sess.get.return_value = not_found

        monkeypatch.setattr(bi_monitor, "_get_session", lambda *a, **kw: (fake_sess, "sid"))
        monkeypatch.setattr(bi_monitor, "bi_find_alert_details",
                            lambda *a, **kw: ("@clip/foo.mp4", 0, 10000))
        monkeypatch.setattr(bi_monitor, "bi_wait_for_export_ready", lambda *a, **kw: "clips/foo.mp4")
        monkeypatch.setattr(bi_monitor, "bi_delete_clip", lambda *a, **kw: None)
        monkeypatch.setattr(bi_monitor.time, "sleep", lambda _: None)

        req = {
            "request_id": str(uuid.uuid4()),
            "config_name": "TestCam",
            "bi_url": "http://192.168.1.1:81",
            "bi_user": "admin",
            "bi_pass": "secret",
            "trigger_filename": "20240101_120000.jpg",
            "output_path": "/tmp/test_404_fastfail.mp4",
            "verbose": False,
            "delete_after": False,
            "bi_restart_url": "",
            "bi_restart_token": "",
            "queued_at": time.time(),
        }
        start = time.monotonic()
        # FIXED: Unpack tuple and check the boolean status
        ok, err = bi_monitor._do_export(req, "[Test404]")
        elapsed = time.monotonic() - start

        assert ok is False, "Should return False after persistent 404s"
        assert elapsed < 10, f"Fast-fail took too long: {elapsed:.1f}s"
        # Note: Your current bi_monitor.py uses 50 attempts
        assert fake_sess.get.call_count >= 50, "Should attempt at least 50 times before giving up"
