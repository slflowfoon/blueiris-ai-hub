import json
import time
import uuid

import bi_downloader
import bi_export_shared
import bi_exporter
import bi_queue_monitor


_r = bi_export_shared.r


def _request_payload(**overrides):
    payload = {
        "request_id": str(uuid.uuid4()),
        "config_name": "TestCam",
        "bi_url": "http://192.168.1.1:81",
        "bi_user": "admin",
        "bi_pass": "secret",
        "trigger_filename": "20240101_120000.jpg",
        "clip_path": "@clip/foo.mp4",
        "offset": 0,
        "duration": 10000,
        "output_path": "/tmp/test_out.mp4",
        "bi_restart_url": "",
        "bi_restart_token": "",
        "delete_after": False,
        "queued_at": time.time(),
    }
    payload.update(overrides)
    return payload


def _clear_pipeline_state():
    active_ids = [
        rid.decode() if isinstance(rid, bytes) else rid
        for rid in _r.smembers(bi_export_shared.ACTIVE_EXPORT_SET)
    ]
    keys = [
        bi_export_shared.ACTIVE_EXPORT_SET,
        bi_export_shared.EXPORT_REQUEST_QUEUE,
        bi_export_shared.DOWNLOAD_REQUEST_QUEUE,
    ]
    for request_id in active_ids:
        keys.append(bi_export_shared.job_key(request_id))
        keys.append(bi_export_shared.result_key(request_id))
    _r.delete(*keys)


class TestExporter:
    def setup_method(self):
        _clear_pipeline_state()

    def test_process_request_stores_submitted_job(self, monkeypatch):
        payload = _request_payload()

        monkeypatch.setattr(
            bi_exporter,
            "_prepare_export",
            lambda req, _tag: (
                {
                    "request_id": req["request_id"],
                    "config_name": req["config_name"],
                    "request": req,
                    "bi_url": req["bi_url"],
                    "bi_user": req["bi_user"],
                    "bi_pass": req["bi_pass"],
                    "output_path": req["output_path"],
                    "target_path": "@queued",
                    "relative_uri": "Clipboard/foo.mp4",
                    "delete_after": False,
                    "restart_url": "",
                    "restart_token": "",
                    "status": "submitted",
                    "export_attempts": 1,
                    "recovery_attempts": 0,
                    "submitted_at": 1000,
                    "monitor_started_at": 1000,
                    "last_progress_log": 0,
                    "next_poll_at": 1000,
                },
                None,
            ),
        )

        bi_exporter._process_request(json.dumps(payload).encode())

        stored = bi_export_shared.load_job(payload["request_id"])
        assert stored["status"] == "submitted"
        assert stored["target_path"] == "@queued"
        assert _r.sismember(bi_export_shared.ACTIVE_EXPORT_SET, payload["request_id"])


class TestQueueMonitor:
    def setup_method(self):
        _clear_pipeline_state()

    def test_acknowledged_export_transitions_to_queued(self, monkeypatch):
        payload = _request_payload()
        job = {
            "request_id": payload["request_id"],
            "config_name": payload["config_name"],
            "request": payload,
            "bi_url": payload["bi_url"],
            "bi_user": payload["bi_user"],
            "bi_pass": payload["bi_pass"],
            "output_path": payload["output_path"],
            "target_path": "@queued",
            "relative_uri": "Clipboard/foo.mp4",
            "delete_after": False,
            "restart_url": "",
            "restart_token": "",
            "status": "submitted",
            "export_attempts": 1,
            "recovery_attempts": 0,
            "submitted_at": time.time(),
            "monitor_started_at": time.time(),
            "last_progress_log": 0,
            "next_poll_at": 0,
        }
        bi_export_shared.save_job(job)
        _r.sadd(bi_export_shared.ACTIVE_EXPORT_SET, job["request_id"])

        monkeypatch.setattr(bi_queue_monitor, "get_session", lambda *args, **kwargs: (object(), "sid"))
        monkeypatch.setattr(
            bi_queue_monitor,
            "bi_get_export_queue",
            lambda *args, **kwargs: [{"path": "@queued", "uri": "Clipboard\\foo.mp4"}],
        )

        bi_queue_monitor._poll_active_exports()

        stored = bi_export_shared.load_job(job["request_id"])
        assert stored["status"] == "queued"
        assert stored.get("queue_ack_at") is not None

    def test_ready_export_is_sent_to_downloader(self, monkeypatch):
        payload = _request_payload()
        job = {
            "request_id": payload["request_id"],
            "config_name": payload["config_name"],
            "request": payload,
            "bi_url": payload["bi_url"],
            "bi_user": payload["bi_user"],
            "bi_pass": payload["bi_pass"],
            "output_path": payload["output_path"],
            "target_path": "@queued",
            "relative_uri": "Clipboard/foo.mp4",
            "delete_after": False,
            "restart_url": "",
            "restart_token": "",
            "status": "queued",
            "export_attempts": 1,
            "recovery_attempts": 0,
            "submitted_at": time.time() - 30,
            "monitor_started_at": time.time() - 30,
            "last_progress_log": 0,
            "next_poll_at": 0,
        }
        bi_export_shared.save_job(job)
        _r.sadd(bi_export_shared.ACTIVE_EXPORT_SET, job["request_id"])

        monkeypatch.setattr(bi_queue_monitor, "get_session", lambda *args, **kwargs: (object(), "sid"))
        monkeypatch.setattr(bi_queue_monitor, "bi_get_export_queue", lambda *args, **kwargs: [])

        bi_queue_monitor._poll_active_exports()

        stored = bi_export_shared.load_job(job["request_id"])
        queued_download = _r.blpop(bi_export_shared.DOWNLOAD_REQUEST_QUEUE, timeout=1)
        assert stored["status"] == "ready"
        assert queued_download[1].decode() == job["request_id"]
        assert not _r.sismember(bi_export_shared.ACTIVE_EXPORT_SET, job["request_id"])

    def test_unacknowledged_export_is_retried(self, monkeypatch):
        payload = _request_payload()
        job = {
            "request_id": payload["request_id"],
            "config_name": payload["config_name"],
            "request": payload,
            "bi_url": payload["bi_url"],
            "bi_user": payload["bi_user"],
            "bi_pass": payload["bi_pass"],
            "output_path": payload["output_path"],
            "target_path": "@queued",
            "relative_uri": "Clipboard/foo.mp4",
            "delete_after": False,
            "restart_url": "",
            "restart_token": "",
            "status": "submitted",
            "export_attempts": 1,
            "recovery_attempts": 0,
            "submitted_at": time.time() - (bi_export_shared.EXPORT_QUEUE_ACK_TIMEOUT + 1),
            "monitor_started_at": time.time(),
            "last_progress_log": 0,
            "next_poll_at": 0,
        }
        bi_export_shared.save_job(job)
        _r.sadd(bi_export_shared.ACTIVE_EXPORT_SET, job["request_id"])

        monkeypatch.setattr(bi_queue_monitor, "get_session", lambda *args, **kwargs: (object(), "sid"))
        monkeypatch.setattr(bi_queue_monitor, "bi_get_export_queue", lambda *args, **kwargs: [])

        bi_queue_monitor._poll_active_exports()

        queued_retry = _r.blpop(bi_export_shared.EXPORT_REQUEST_QUEUE, timeout=1)
        stored = bi_export_shared.load_job(job["request_id"])
        assert queued_retry is not None
        assert stored["status"] == "retry_queued"


class TestDownloader:
    def setup_method(self):
        _clear_pipeline_state()

    def test_download_success_finishes_job(self, monkeypatch):
        payload = _request_payload()
        job = {
            "request_id": payload["request_id"],
            "config_name": payload["config_name"],
            "request": payload,
            "bi_url": payload["bi_url"],
            "bi_user": payload["bi_user"],
            "bi_pass": payload["bi_pass"],
            "output_path": payload["output_path"],
            "target_path": "@queued",
            "relative_uri": "Clipboard/foo.mp4",
            "delete_after": False,
            "restart_url": "",
            "restart_token": "",
            "status": "ready",
            "export_attempts": 1,
            "recovery_attempts": 0,
        }
        bi_export_shared.save_job(job)
        monkeypatch.setattr(bi_downloader, "_download_export", lambda current_job: (True, None))

        bi_downloader._process_download_request(job["request_id"])

        stored = bi_export_shared.load_job(job["request_id"])
        result = _r.blpop(bi_export_shared.result_key(job["request_id"]), timeout=1)
        assert stored["status"] == "downloaded"
        assert json.loads(result[1])["ok"] is True
