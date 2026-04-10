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
        """run_monitor must pop and process a queued request then exit."""
        _push_request()
        processed = []
        
        # Kill switch logic
        should_run = True
        def keep_going():
            return should_run

        monkeypatch.setattr(
            bi_monitor,
            "_enqueue_export_request",
            lambda raw, active_jobs: processed.append(json.loads(raw)),
        )
        monkeypatch.setattr(bi_monitor, "_process_active_exports", lambda active_jobs: None)
        monkeypatch.setattr(bi_monitor, "BLPOP_BLOCK_TIMEOUT", 1)
        monkeypatch.setattr(bi_monitor, "MONITOR_LOOP_IDLE_TIMEOUT", 1)

        # Pass the kill switch to the monitor
        t = threading.Thread(target=bi_monitor.run_monitor, args=(keep_going,), daemon=True)
        t.start()
        
        # Wait a moment for it to process
        time.sleep(2)
        
        # Signal the thread to stop
        should_run = False
        t.join(timeout=2)

        assert len(processed) >= 1


class TestPreResolvedClip:
    def setup_method(self):
        bi_monitor._session_cache.clear()

    def test_pre_resolved_clip_skips_alertlist(self, monkeypatch):
        import unittest.mock as mock
        fake_sess = mock.MagicMock()
        def post_side_effect(_url, json=None, timeout=None):
            if json == {"cmd": "export", "session": "sid"}:
                return mock.MagicMock(
                    status_code=200,
                    json=lambda: {
                        "result": "success",
                        "data": [{"path": "@existing", "uri": "Clipboard\\existing.mp4"}],
                    },
                )
            if json and json.get("cmd") == "export" and json.get("path"):
                return mock.MagicMock(
                    status_code=200,
                    json=lambda: {
                        "result": "success",
                        "data": [
                            {"path": "@clip/foo", "uri": "Clipboard\\foo.mp4"},
                            {"path": "@existing", "uri": "Clipboard\\existing.mp4"},
                        ],
                    },
                )
            raise AssertionError(f"Unexpected POST payload: {json}")

        fake_sess.post.side_effect = post_side_effect
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
        assert err is None


class TestPersistent404FastFail:
    def setup_method(self):
        bi_monitor._session_cache.clear()

    def test_fast_fail_on_persistent_404(self, monkeypatch):
        import unittest.mock as mock
        fake_sess = mock.MagicMock()
        def post_side_effect(_url, json=None, timeout=None):
            if json == {"cmd": "export", "session": "sid"}:
                return mock.MagicMock(
                    status_code=200,
                    json=lambda: {"result": "success", "data": []},
                )
            if json and json.get("cmd") == "export" and json.get("path"):
                return mock.MagicMock(
                    status_code=200,
                    json=lambda: {
                        "result": "success",
                        "data": {"path": "@clip/foo", "uri": "Clipboard\\foo.mp4"},
                    },
                )
            raise AssertionError(f"Unexpected POST payload: {json}")

        fake_sess.post.side_effect = post_side_effect
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

        # Advance time across loop checks so the persistent 404 path exits.
        timeline = iter([1000, 1001, 1002, 1002, 1002, 1063])
        monkeypatch.setattr(time, "time", lambda: next(timeline, 1063))
        monkeypatch.setattr(time, "sleep", lambda _: None)

        req = _push_request()
        ok, err = bi_monitor._do_export(req, "[Test404]")

        assert ok is False
        assert err == "download failed (file not ready)"


class TestQueueResolution:
    def test_resolves_new_entry_from_queue_list(self):
        known_paths = {"@existing"}
        queue_data = [
            {"path": "@new", "uri": "Clipboard\\new.mp4"},
            {"path": "@existing", "uri": "Clipboard\\existing.mp4"},
        ]

        target_path, relative_uri = bi_monitor.bi_resolve_export_target(queue_data, known_paths, "[TestQueue]")

        assert target_path == "@new"
        assert relative_uri == "Clipboard/new.mp4"


class TestActiveExportProcessing:
    def test_completed_export_is_downloaded_and_result_written(self, monkeypatch):
        request_id = str(uuid.uuid4())
        active_jobs = {
            request_id: {
                "tag": "[TestCam][abcd1234]",
                "sess": object(),
                "sid": "sid",
                "bi_url": "http://192.168.1.1:81",
                "bi_user": "admin",
                "output_path": "/tmp/test_out.mp4",
                "target_path": "@done",
                "relative_uri": "Clipboard/done.mp4",
                "delete_after": False,
                "restart_url": "",
                "restart_token": "",
                "recovery_depth": 0,
                "monitor_started_at": time.time() - 12,
                "next_poll_at": 0,
                "last_progress_log": 0,
                "req": {"request_id": request_id},
            }
        }
        results = []

        monkeypatch.setattr(bi_monitor, "bi_get_export_queue", lambda *a, **kw: [])
        monkeypatch.setattr(bi_monitor, "_download_export", lambda job: (True, None))
        monkeypatch.setattr(
            bi_monitor,
            "_write_result",
            lambda rid, path, ok, err=None: results.append((rid, path, ok, err)),
        )

        bi_monitor._process_active_exports(active_jobs)

        assert request_id not in active_jobs
        assert results == [(request_id, "/tmp/test_out.mp4", True, None)]

    def test_handles_single_object_response(self):
        target_path, relative_uri = bi_monitor.bi_resolve_export_target(
            {"path": "@new", "uri": "Clipboard\\new.mp4"},
            set(),
            "[TestQueue]",
        )

        assert target_path == "@new"
        assert relative_uri == "Clipboard/new.mp4"
