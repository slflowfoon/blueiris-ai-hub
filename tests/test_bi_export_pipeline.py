import json
import os
import threading
import time
import uuid

import bi_downloader
import bi_export_shared
import bi_exporter
import bi_queue_monitor
import bi_watchdog
import video_delivery_worker


_r = bi_export_shared.r


def _request_payload(**overrides):
    payload = {
        "request_id": str(uuid.uuid4()),
        "alert_request_id": "webhook42",
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
    active_ids = set(
        rid.decode() if isinstance(rid, bytes) else rid
        for rid in _r.smembers(bi_export_shared.ACTIVE_EXPORT_SET)
    )
    active_ids.update(bi_export_shared.iter_job_ids())
    keys = [
        bi_export_shared.ACTIVE_EXPORT_SET,
        bi_export_shared.EXPORT_REQUEST_QUEUE,
        bi_export_shared.DOWNLOAD_REQUEST_QUEUE,
        bi_export_shared.VIDEO_DELIVERY_QUEUE,
    ]
    for request_id in active_ids:
        keys.append(bi_export_shared.job_key(request_id))
        keys.append(bi_export_shared.result_key(request_id))
    _r.delete(*keys)


class TestExporter:
    def setup_method(self):
        _clear_pipeline_state()

    def test_stale_request_is_rejected(self):
        payload = _request_payload(queued_at=time.time() - (bi_export_shared.STALE_REQUEST_AGE + 60))

        bi_exporter._process_request(json.dumps(payload).encode())

        result = _r.blpop(bi_export_shared.result_key(payload["request_id"]), timeout=1)
        assert json.loads(result[1])["ok"] is False

    def test_process_request_stores_submitted_job(self, monkeypatch):
        payload = _request_payload()

        monkeypatch.setattr(
            bi_exporter,
            "_prepare_export",
            lambda req, _tag: (
                {
                    "request_id": req["request_id"],
                    "alert_request_id": req["alert_request_id"],
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
        assert stored["alert_request_id"] == payload["alert_request_id"]
        assert stored["target_path"] == "@queued"
        assert _r.sismember(bi_export_shared.ACTIVE_EXPORT_SET, payload["request_id"])

    def test_job_tag_prefers_alert_request_id(self):
        payload = _request_payload()

        assert bi_export_shared.job_tag(payload) == "[TestCam][webhook4]"

    def test_result_key_has_ttl_on_failure(self, monkeypatch):
        payload = _request_payload()
        monkeypatch.setattr(bi_exporter, "_prepare_export", lambda req, tag: (None, "export failed"))

        bi_exporter._process_request(json.dumps(payload).encode())

        ttl = _r.ttl(bi_export_shared.result_key(payload["request_id"]))
        assert 0 < ttl <= bi_export_shared.RESULT_KEY_TTL


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
        monkeypatch.setattr(bi_downloader, "_download_export", lambda current_job: (True, None, 1.2, 2048))

        bi_downloader._process_download_request(job["request_id"])

        stored = bi_export_shared.load_job(job["request_id"])
        result = _r.blpop(bi_export_shared.result_key(job["request_id"]), timeout=1)
        assert stored["status"] == "downloaded"
        assert json.loads(result[1])["ok"] is True

    def test_download_success_queues_async_delivery(self, monkeypatch):
        payload = _request_payload(output_path="/tmp/test_delivery_raw.mp4")
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
            "delivery_context": {"config": {"last_msg_id": 123}},
            "delivery_status": "pending",
            "delivery_attempts": 0,
        }
        bi_export_shared.save_job(job)
        monkeypatch.setattr(bi_downloader, "_download_export", lambda current_job: (True, None, 1.2, 2048))

        bi_downloader._process_download_request(job["request_id"])

        queued_delivery = _r.blpop(bi_export_shared.VIDEO_DELIVERY_QUEUE, timeout=1)
        stored = bi_export_shared.load_job(job["request_id"])
        assert queued_delivery[1].decode() == job["request_id"]
        assert stored["delivery_status"] == "queued"

    def test_download_failure_requeues_after_recovery(self, monkeypatch):
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
            "restart_url": "http://recovery.local/restart",
            "restart_token": "token",
            "status": "ready",
            "export_attempts": 1,
            "recovery_attempts": 0,
        }
        bi_export_shared.save_job(job)
        monkeypatch.setattr(
            bi_downloader,
            "_download_export",
            lambda current_job: (False, "download failed (file not ready)", None, None),
        )
        monkeypatch.setattr(bi_downloader, "trigger_bi_recovery", lambda *args, **kwargs: True)

        bi_downloader._process_download_request(job["request_id"])

        queued_retry = _r.blpop(bi_export_shared.EXPORT_REQUEST_QUEUE, timeout=1)
        stored = bi_export_shared.load_job(job["request_id"])
        assert queued_retry is not None
        assert stored["status"] == "retry_queued"
        assert stored["last_error"] == "download failed (file not ready)"


class TestSharedSessionCache:
    def setup_method(self):
        _clear_pipeline_state()
        bi_export_shared._session_cache.clear()
        _r.delete(bi_export_shared.session_key("http://bi:81", "admin"))

    def test_shared_session_reused_via_redis(self, monkeypatch):
        import unittest.mock as mock

        fake_sess = mock.MagicMock()
        fake_sess.post.return_value = mock.MagicMock(
            status_code=200,
            json=lambda: {"result": "success"},
        )

        monkeypatch.setattr(bi_export_shared.requests, "Session", lambda: fake_sess)
        monkeypatch.setattr(bi_export_shared, "bi_login", lambda *args, **kwargs: "shared-sid")

        _sess1, sid1 = bi_export_shared.get_session("http://bi:81", "admin", "pw", "[T]")
        bi_export_shared._session_cache.clear()
        _sess2, sid2 = bi_export_shared.get_session("http://bi:81", "admin", "pw", "[T]")

        assert sid1 == "shared-sid"
        assert sid2 == "shared-sid"


class TestWatchdog:
    def setup_method(self):
        _clear_pipeline_state()

    def test_watchdog_requeues_stale_ready_job_for_download(self):
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
            "last_transition_at": time.time() - (bi_export_shared.DOWNLOAD_TIMEOUT + 20),
        }
        bi_export_shared.save_job(job)

        bi_watchdog._run_once()

        queued_download = _r.blpop(bi_export_shared.DOWNLOAD_REQUEST_QUEUE, timeout=1)
        assert queued_download[1].decode() == job["request_id"]


class TestVideoDeliveryWorker:
    def setup_method(self):
        _clear_pipeline_state()

    def test_video_delivery_completes_downloaded_job(self, monkeypatch):
        raw_mp4 = "/tmp/video_delivery_worker_raw.mp4"
        with open(raw_mp4, "wb") as fh:
            fh.write(b"video-data")

        payload = _request_payload(output_path=raw_mp4)
        job = {
            "request_id": payload["request_id"],
            "config_name": payload["config_name"],
            "request": payload,
            "bi_url": payload["bi_url"],
            "bi_user": payload["bi_user"],
            "bi_pass": payload["bi_pass"],
            "output_path": raw_mp4,
            "target_path": "@queued",
            "relative_uri": "Clipboard/foo.mp4",
            "delete_after": False,
            "restart_url": "",
            "restart_token": "",
            "status": "downloaded",
            "delivery_context": {
                "config": {
                    "id": 1,
                    "name": "TestCam",
                    "request_id": "req",
                    "telegram_token": "token",
                    "chat_id": "chat",
                    "last_msg_id": 42,
                    "dvla_api_key": "",
                    "gemini_key": "gemini-key",
                },
                "prompt": "describe this",
                "still_caption": "Motion detected.",
            },
            "delivery_status": "queued",
            "delivery_attempts": 0,
        }
        bi_export_shared.save_job(job)
        monkeypatch.setattr(video_delivery_worker, "optimize_video_for_telegram", lambda *args, **kwargs: True)
        monkeypatch.setattr(video_delivery_worker, "replace_telegram_media", lambda *args, **kwargs: True)
        monkeypatch.setattr(video_delivery_worker, "analyze_video_gemini", lambda *args, **kwargs: "Car on drive")
        monkeypatch.setattr(video_delivery_worker, "enrich_caption_with_dvla", lambda text, *_args, **_kwargs: text)
        monkeypatch.setattr(video_delivery_worker, "update_telegram_caption", lambda *args, **kwargs: True)

        video_delivery_worker._process_delivery_request(job["request_id"])

        stored = bi_export_shared.load_job(job["request_id"])
        assert stored["status"] == "completed"
        assert stored["delivery_status"] == "completed"
        assert not os.path.exists(raw_mp4)

    def test_video_delivery_logs_missing_gemini_key_when_caption_unavailable(self, monkeypatch):
        raw_mp4 = "/tmp/video_delivery_worker_missing_key_raw.mp4"
        with open(raw_mp4, "wb") as fh:
            fh.write(b"video-data")

        payload = _request_payload(output_path=raw_mp4)
        job = {
            "request_id": payload["request_id"],
            "config_name": payload["config_name"],
            "request": payload,
            "bi_url": payload["bi_url"],
            "bi_user": payload["bi_user"],
            "bi_pass": payload["bi_pass"],
            "output_path": raw_mp4,
            "target_path": "@queued",
            "relative_uri": "Clipboard/foo.mp4",
            "delete_after": False,
            "restart_url": "",
            "restart_token": "",
            "status": "downloaded",
            "delivery_context": {
                "config": {
                    "id": 1,
                    "name": "TestCam",
                    "request_id": "req",
                    "telegram_token": "token",
                    "chat_id": "chat",
                    "last_msg_id": 42,
                    "dvla_api_key": "",
                    "gemini_key": "",
                },
                "prompt": "describe this",
                "still_caption": "Motion detected.",
            },
            "delivery_status": "queued",
            "delivery_attempts": 0,
        }
        bi_export_shared.save_job(job)
        monkeypatch.setattr(video_delivery_worker, "optimize_video_for_telegram", lambda *args, **kwargs: True)
        monkeypatch.setattr(video_delivery_worker, "replace_telegram_media", lambda *args, **kwargs: True)
        monkeypatch.setattr(video_delivery_worker, "analyze_video_gemini", lambda *args, **kwargs: None)
        logged = []
        monkeypatch.setattr(
            video_delivery_worker,
            "log_telegram_event",
            lambda level, tag, message, phase, config, **kwargs: logged.append(
                {
                    "level": level,
                    "tag": tag,
                    "message": message,
                    "phase": phase,
                    "kwargs": kwargs,
                }
            ),
        )

        video_delivery_worker._process_delivery_request(job["request_id"])

        assert any(item["phase"] == "video_caption_unavailable" for item in logged)
        assert any(item["kwargs"].get("error_code") == "missing_gemini_key" for item in logged)

    def test_video_delivery_logs_dvla_skip_when_no_key_configured(self, monkeypatch):
        raw_mp4 = "/tmp/video_delivery_worker_dvla_skip_raw.mp4"
        with open(raw_mp4, "wb") as fh:
            fh.write(b"video-data")

        payload = _request_payload(output_path=raw_mp4)
        job = {
            "request_id": payload["request_id"],
            "config_name": payload["config_name"],
            "request": payload,
            "bi_url": payload["bi_url"],
            "bi_user": payload["bi_user"],
            "bi_pass": payload["bi_pass"],
            "output_path": raw_mp4,
            "target_path": "@queued",
            "relative_uri": "Clipboard/foo.mp4",
            "delete_after": False,
            "restart_url": "",
            "restart_token": "",
            "status": "downloaded",
            "delivery_context": {
                "config": {
                    "id": 1,
                    "name": "TestCam",
                    "request_id": "req",
                    "telegram_token": "token",
                    "chat_id": "chat",
                    "last_msg_id": 42,
                    "dvla_api_key": "",
                    "gemini_key": "gemini-key",
                },
                "prompt": "describe this",
                "still_caption": "Motion detected.",
            },
            "delivery_status": "queued",
            "delivery_attempts": 0,
        }
        bi_export_shared.save_job(job)
        monkeypatch.setattr(video_delivery_worker, "optimize_video_for_telegram", lambda *args, **kwargs: True)
        monkeypatch.setattr(video_delivery_worker, "replace_telegram_media", lambda *args, **kwargs: True)
        monkeypatch.setattr(video_delivery_worker, "analyze_video_gemini", lambda *args, **kwargs: "Car on drive")
        monkeypatch.setattr(video_delivery_worker, "update_telegram_caption", lambda *args, **kwargs: True)
        logged = []
        monkeypatch.setattr(
            video_delivery_worker,
            "log_telegram_event",
            lambda level, tag, message, phase, config, **kwargs: logged.append(
                {
                    "level": level,
                    "tag": tag,
                    "message": message,
                    "phase": phase,
                    "kwargs": kwargs,
                }
            ),
        )

        video_delivery_worker._process_delivery_request(job["request_id"])

        assert any(item["phase"] == "dvla_caption_skipped" for item in logged)
        assert any(item["kwargs"].get("reason") == "no_api_key" for item in logged)
        assert not any(item["phase"] == "dvla_caption_enriched" for item in logged)

    def test_video_delivery_starts_caption_analysis_before_media_replace_finishes(self, monkeypatch):
        raw_mp4 = "/tmp/video_delivery_worker_parallel_raw.mp4"
        with open(raw_mp4, "wb") as fh:
            fh.write(b"video-data")

        payload = _request_payload(output_path=raw_mp4)
        job = {
            "request_id": payload["request_id"],
            "config_name": payload["config_name"],
            "request": payload,
            "bi_url": payload["bi_url"],
            "bi_user": payload["bi_user"],
            "bi_pass": payload["bi_pass"],
            "output_path": raw_mp4,
            "target_path": "@queued",
            "relative_uri": "Clipboard/foo.mp4",
            "delete_after": False,
            "restart_url": "",
            "restart_token": "",
            "status": "downloaded",
            "delivery_context": {
                "config": {
                    "id": 1,
                    "name": "TestCam",
                    "request_id": "req",
                    "telegram_token": "token",
                    "chat_id": "chat",
                    "last_msg_id": 42,
                    "dvla_api_key": "",
                    "gemini_key": "gemini-key",
                },
                "prompt": "describe this",
                "still_caption": "Motion detected.",
            },
            "delivery_status": "queued",
            "delivery_attempts": 0,
        }
        bi_export_shared.save_job(job)
        analysis_started = threading.Event()

        monkeypatch.setattr(video_delivery_worker, "optimize_video_for_telegram", lambda *args, **kwargs: True)

        def fake_replace(*_args, **_kwargs):
            assert analysis_started.wait(1)
            return True

        def fake_analyze(*_args, **_kwargs):
            analysis_started.set()
            return "Car on drive"

        monkeypatch.setattr(video_delivery_worker, "replace_telegram_media", fake_replace)
        monkeypatch.setattr(video_delivery_worker, "analyze_video_gemini", fake_analyze)
        monkeypatch.setattr(video_delivery_worker, "enrich_caption_with_dvla", lambda text, *_args, **_kwargs: text)
        monkeypatch.setattr(video_delivery_worker, "update_telegram_caption", lambda *args, **kwargs: True)

        video_delivery_worker._process_delivery_request(job["request_id"])

        assert analysis_started.is_set()
