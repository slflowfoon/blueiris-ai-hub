"""
Tests for bi_monitor.py synchronized for Queue Monitor logic.
"""

import json
import threading
import time
import uuid
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
        req = _push_request(queued_at=time.time() - (bi_monitor.STALE_REQUEST_AGE + 60))
        bi_monitor._process_request(_r.rpop("bi:requests"))
        result = _pop_result(req["request_id"])
        assert result["ok"] is False

    def test_fresh_request_passes_guard(self, monkeypatch):
        req = _push_request()
        monkeypatch.setattr(bi_monitor, "_do_export", lambda r, tag: (True, None))
        bi_monitor._process_request(json.dumps(req).encode())
        result = _pop_result(req["request_id"])
        assert result["ok"] is True


class TestResultProtocol:
    def setup_method(self):
        _r.delete("bi:requests")

    def test_result_key_has_ttl(self, monkeypatch):
        req = _push_request()
        monkeypatch.setattr(bi_monitor, "_do_export", lambda r, t: (True, None))
        bi_monitor._process_request(json.dumps(req).encode())
        ttl = _r.ttl(f"bi:result:{req['request_id']}")
        assert 0 < ttl <= bi_monitor.RESULT_KEY_TTL

    def test_failed_export_returns_ok_false(self, monkeypatch):
        req = _push_request()
        monkeypatch.setattr(bi_monitor, "_do_export", lambda r, t: (False, "export failed"))
        bi_monitor._process_request(json.dumps(req).encode())
        result = _pop_result(req["request_id"])
        assert result["ok"] is False
        assert result["error"] == "export failed"


class TestSessionCache:
    def setup_method(self):
        bi_monitor._session_cache.clear()

    def test_session_stored_after_login(self, monkeypatch):
        monkeypatch.setattr(bi_monitor, "bi_login", lambda *a: "fake-sid")
        sess, sid = bi_monitor._get_session("http://bi:81", "admin", "pw", "[T]")
        assert sid == "fake-sid"


class TestRunMonitorLoop:
    def test_processes_queued_request(self, monkeypatch):
        _push_request()
        processed = []
        monkeypatch.setattr(bi_monitor, "_process_request", lambda raw: processed.append(json.loads(raw)))
        monkeypatch.setattr(bi_monitor, "BLPOP_BLOCK_TIMEOUT", 1)
        t = threading.Thread(target=bi_monitor.run_monitor, daemon=True)
        t.start()
        t.join(timeout=2)
        assert len(processed) >= 1


class TestPreResolvedClip:
    def setup_method(self):
        bi_monitor._session_cache.clear()

    def test_pre_resolved_clip_skips_alertlist(self, monkeypatch):
        import unittest.mock as mock
        fake_sess = mock.MagicMock()
        mock_resp = {
            "result": "success",
            "data": {"path": "@clip/foo", "uri": "Clipboard\\foo.mp4"}
        }
        fake_sess.post.return_value = mock.MagicMock(status_code=200, json=lambda: mock_resp)
        fake_dl = mock.MagicMock(status_code=200)
        fake_dl.headers = {"Content-Length": "2000"}
        fake_dl.iter_content = lambda chunk_size=0: [b"x" * 2000]
        fake_dl.__enter__ = lambda s: fake_dl
        fake_dl.__exit__ = mock.MagicMock(return_value=False)
        fake_sess.get.return_value = fake_dl

        monkeypatch.setattr(bi_monitor, "_get_session", lambda *a, **kw: (fake_sess, "sid"))
        # UPDATED: Patch new queue monitoring function
        monkeypatch.setattr(bi_monitor, "bi_wait_for_queue_completion", lambda *a, **kw: True)
        monkeypatch.setattr(bi_monitor, "bi_delete_clip", lambda *a, **kw: None)

        req = _push_request(clip_path="@clip/20240101_120000.mp4")
        ok, err = bi_monitor._do_export(req, "[TestPreResolved]")
        assert ok is True


class TestPersistent404FastFail:
    def setup_method(self):
        bi_monitor._session_cache.clear()

    def test_fast_fail_on_persistent_404(self, monkeypatch):
        import unittest.mock as mock
        fake_sess = mock.MagicMock()
        mock_resp = {
            "result": "success",
            "data": {"path": "@clip/foo", "uri": "Clipboard\\foo.mp4"}
        }
        fake_sess.post.return_value = mock.MagicMock(status_code=200, json=lambda: mock_resp)
        not_found = mock.MagicMock(status_code=404)
        not_found.headers = {"Content-Length": "0"}
        not_found.__enter__ = lambda s: not_found
        not_found.__exit__ = mock.MagicMock(return_value=False)
        fake_sess.get.return_value = not_found

        monkeypatch.setattr(bi_monitor, "_get_session", lambda *a, **kw: (fake_sess, "sid"))
        monkeypatch.setattr(bi_monitor, "bi_find_alert_details", lambda *a, **kw: ("@clip/foo.mp4", 0, 10000))
        # UPDATED: Patch new queue monitoring function
        monkeypatch.setattr(bi_monitor, "bi_wait_for_queue_completion", lambda *a, **kw: True)
        monkeypatch.setattr(bi_monitor, "bi_delete_clip", lambda *a, **kw: None)

        # Mock time.time() to jump forward to simulate timeout immediately
        start_time = time.time()
        monkeypatch.setattr(time, "time", lambda: start_time + 61)
        monkeypatch.setattr(time, "sleep", lambda _: None)

        req = _push_request()
        ok, err = bi_monitor._do_export(req, "[Test404]")

        assert ok is False
        assert err == "download failed (file not ready)"
