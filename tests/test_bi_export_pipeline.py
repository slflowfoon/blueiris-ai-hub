import json
import os
import threading
import time
import uuid

import pytest

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


def _retry_queued_job(payload, **overrides):
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
        "status": "retry_queued",
        "export_attempts": 1,
        "recovery_attempts": 0,
        "last_error": "watchdog: export queue stale",
        "last_transition_at": time.time(),
    }
    job.update(overrides)
    return job


def _legacy_openbvr_retry(*, original_age=800, deferred_age=10, refreshed=False):
    """Seed the old persisted representation, without using new deferral code."""
    now = time.time()
    original = _request_payload(
        queued_at=now - original_age, _export_attempts=1, _recovery_attempts=0,
        _previous_target_path="@queued", _original_submitted_at=1000,
        _original_monitor_started_at=1005, _original_queue_ack_at=1010,
    )
    if refreshed:
        original.update(
            _openbvr_refreshed_attempts=1, _lookup_file=original["trigger_filename"],
            _lookup_path=original["clip_path"], _lookup_clip="synthetic-old-clip",
            _lookup_match_type="exact_file", _export_source_field="path",
        )
    deferred = dict(original, queued_at=now - deferred_age, _openbvr_deferred_attempts=1)
    if refreshed:
        deferred.update(
            clip_path="@clip/refreshed", offset=222, duration=33333,
            _lookup_path="@clip/refreshed", _lookup_clip="synthetic-new-clip",
            _lookup_match_type="exact_file_duplicate_first",
        )
    saved = _retry_queued_job(
        original, submitted_at=1000, monitor_started_at=1005, queue_ack_at=1010,
        last_transition_at=now - 90,
    )
    _r.set(bi_export_shared.job_key(original["request_id"]), json.dumps(saved))
    return original, deferred, saved


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

    def test_reattach_to_existing_export_when_previous_target_still_in_queue(self, monkeypatch):
        payload = _request_payload(
            _export_attempts=1,
            _previous_target_path="@22833",
        )

        monkeypatch.setattr(bi_exporter, "get_session", lambda *args, **kwargs: (object(), "sid"))
        monkeypatch.setattr(
            bi_exporter,
            "bi_get_export_queue",
            lambda *args, **kwargs: [{"path": "@22833", "uri": "Clipboard\\foo.mp4"}],
        )

        job, error = bi_exporter._prepare_export(payload, "[T]")

        assert error is None
        assert job["target_path"] == "@22833"
        assert job["relative_uri"] == "Clipboard/foo.mp4"
        assert job["status"] == "queued"
        assert job["export_attempts"] == 1  # reattach does not increment; no new export was submitted

    def test_submits_new_export_when_previous_target_left_queue(self, monkeypatch):
        payload = _request_payload(
            _export_attempts=1,
            _previous_target_path="@22833",
        )

        call_count = {"n": 0}

        def fake_get_queue(*args, **kwargs):
            call_count["n"] += 1
            return []  # previous target no longer present

        monkeypatch.setattr(bi_exporter, "get_session", lambda *args, **kwargs: (object(), "sid"))
        monkeypatch.setattr(bi_exporter, "bi_get_export_queue", fake_get_queue)

        posted = []

        class FakeSession:
            def post(self, url, json=None, timeout=None):
                posted.append(json)

                class R:
                    def json(self):
                        return {"result": "success", "data": {"path": "@22842", "uri": "Clipboard\\new.mp4"}}
                return R()

        monkeypatch.setattr(bi_exporter, "get_session", lambda *args, **kwargs: (FakeSession(), "sid"))
        monkeypatch.setattr(bi_exporter, "bi_get_export_queue", fake_get_queue)

        job, error = bi_exporter._prepare_export(payload, "[T]")

        assert error is None
        assert job["target_path"] == "@22842"
        assert job["status"] == "submitted"
        assert any(p.get("cmd") == "export" for p in posted)

    def test_result_key_has_ttl_on_failure(self, monkeypatch):
        payload = _request_payload()
        monkeypatch.setattr(bi_exporter, "_prepare_export", lambda req, tag: (None, "export failed"))

        bi_exporter._process_request(json.dumps(payload).encode())

        ttl = _r.ttl(bi_export_shared.result_key(payload["request_id"]))
        assert 0 < ttl <= bi_export_shared.RESULT_KEY_TTL

    @pytest.mark.parametrize("outcome", ["success", "deferral"])
    @pytest.mark.parametrize("mutation", ["touch", "unrelated_queue"])
    def test_enriched_retry_success_survives_harmless_settlement_conflict(self, monkeypatch, mutation, outcome):
        payload = _request_payload()
        bi_export_shared.queue_retry(_retry_queued_job(payload, status="queued"), "synthetic retry")
        raw = _r.lpop(bi_export_shared.EXPORT_REQUEST_QUEUE)
        request = json.loads(raw)
        request_id = request["request_id"]
        assert bi_export_shared._legacy_openbvr_base(bi_export_shared.load_job(request_id)) == request
        posted = []
        if outcome == "deferral":
            _r.sadd(bi_export_shared.ACTIVE_EXPORT_SET, "synthetic-other-export")
        monkeypatch.setattr(bi_exporter.time, "sleep", lambda *_args: None)

        class Session:
            def post(self, _url, json=None, timeout=None):
                posted.append(json)

                class Response:
                    def json(self):
                        if outcome == "deferral":
                            return {"result": "fail", "data": {"reason": "OpenBVR failed"}}
                        return {"result": "success", "data": {"path": "@new", "uri": "Clipboard/new.mp4"}}

                return Response()

        monkeypatch.setattr(bi_exporter, "get_session", lambda *_args: (Session(), "sid"))
        monkeypatch.setattr(bi_exporter, "bi_get_export_queue", lambda *_args: [])
        pipeline_class = type(_r.pipeline())
        execute = pipeline_class.execute
        interleaved = []
        unrelated = json.dumps(dict(request, request_id="synthetic-unrelated", _openbvr_deferred_attempts=1)).encode()

        def race(pipe, *args, **kwargs):
            if not interleaved:
                interleaved.append(True)
                assert len(posted) == (1 if outcome == "success" else 2)
                if mutation == "touch":
                    current = bi_export_shared.load_job(request_id)
                    current["last_transition_at"] += 1
                    bi_export_shared.save_job(current)
                else:
                    _r.rpush(bi_export_shared.OPENBVR_RETRY_QUEUE, unrelated)
            return execute(pipe, *args, **kwargs)

        monkeypatch.setattr(pipeline_class, "execute", race)
        bi_exporter._process_request(raw)
        assert interleaved
        stored = bi_export_shared.load_job(request_id)
        queued = [unrelated] if mutation == "unrelated_queue" else []
        if outcome == "success":
            assert stored["status"] == "submitted"
            assert stored["request"] == request
            assert stored["target_path"] == "@new"
            assert stored["relative_uri"] == "Clipboard/new.mp4"
            assert stored["export_attempts"] == 2
            assert _r.sismember(bi_export_shared.ACTIVE_EXPORT_SET, request_id)
        else:
            deferred = stored["request"]
            assert deferred == dict(request, queued_at=deferred["queued_at"], _openbvr_deferred_attempts=1)
            assert deferred["queued_at"] >= request["queued_at"]
            assert stored["status"] == "retry_queued"
            assert stored["retry_queue"] == bi_export_shared.OPENBVR_RETRY_QUEUE
            assert stored["target_path"] == "@queued"
            assert stored["export_attempts"] == 1
            assert not _r.sismember(bi_export_shared.ACTIVE_EXPORT_SET, request_id)
            queued.append(json.dumps(deferred).encode())
        assert _r.llen(bi_export_shared.result_key(request_id)) == 0
        assert _r.llen(bi_export_shared.EXPORT_REQUEST_QUEUE) == 0
        assert _r.lrange(bi_export_shared.OPENBVR_RETRY_QUEUE, 0, -1) == queued
        before = _r.get(bi_export_shared.job_key(request_id))
        bi_exporter._process_request(raw)
        assert _r.get(bi_export_shared.job_key(request_id)) == before
        assert len(posted) == (1 if outcome == "success" else 2)
        assert _r.lrange(bi_export_shared.OPENBVR_RETRY_QUEUE, 0, -1) == queued

    @pytest.mark.parametrize("outcome", ["success", "deferral"])
    @pytest.mark.parametrize("mutation", ["newer", "progress", "terminal", "deferral_push", "deferral_pop"])
    def test_enriched_retry_success_conflict_preserves_superseding_state(self, monkeypatch, mutation, outcome):
        payload = _request_payload()
        bi_export_shared.queue_retry(_retry_queued_job(payload, status="queued"), "synthetic retry")
        raw = _r.lpop(bi_export_shared.EXPORT_REQUEST_QUEUE)
        request = json.loads(raw)
        request_id = request["request_id"]
        key = bi_export_shared.job_key(request_id)
        deferred = dict(request, queued_at=time.time(), _openbvr_deferred_attempts=1)
        deferred_raw = json.dumps(deferred).encode()
        prepared = []

        def prepare(req, _tag):
            prepared.append(True)
            if mutation == "deferral_pop":
                _r.rpush(bi_export_shared.OPENBVR_RETRY_QUEUE, deferred_raw)
            if outcome == "deferral":
                req.update(deferred)
                return None, "openbvr deferred retry queued"
            return _retry_queued_job(req, status="submitted", target_path="@new", export_attempts=2), None

        monkeypatch.setattr(bi_exporter, "_prepare_export", prepare)
        pipeline_class = type(_r.pipeline())
        execute = pipeline_class.execute
        interleaved = []

        def race(pipe, *args, **kwargs):
            if not interleaved:
                assert len(prepared) == 1
                current = bi_export_shared.load_job(request_id)
                if mutation == "newer":
                    current["request"] = dict(request, queued_at=time.time(), _export_attempts=2)
                    current["export_attempts"] = 2
                    bi_export_shared.save_job(current)
                elif mutation in {"progress", "terminal"}:
                    current["status"] = "ready" if mutation == "progress" else "failed"
                    bi_export_shared.save_job(current)
                elif mutation == "deferral_push":
                    _r.rpush(bi_export_shared.OPENBVR_RETRY_QUEUE, deferred_raw)
                else:
                    assert _r.lpop(bi_export_shared.OPENBVR_RETRY_QUEUE) == deferred_raw
                interleaved.append(_r.get(key))
            return execute(pipe, *args, **kwargs)

        monkeypatch.setattr(pipeline_class, "execute", race)
        bi_exporter._process_request(raw)
        assert interleaved
        if mutation == "deferral_push":
            stored = bi_export_shared.load_job(request_id)
            assert stored["status"] == "retry_queued"
            assert stored["request"] == deferred
            assert stored["retry_queue"] == bi_export_shared.OPENBVR_RETRY_QUEUE
            assert stored["target_path"] == "@queued"
            assert stored["export_attempts"] == 1
        else:
            assert _r.get(key) == interleaved[0]
        assert len(prepared) == 1
        assert not _r.sismember(bi_export_shared.ACTIVE_EXPORT_SET, request_id)
        assert _r.llen(bi_export_shared.result_key(request_id)) == 0
        assert _r.llen(bi_export_shared.EXPORT_REQUEST_QUEUE) == 0
        assert _r.lrange(bi_export_shared.OPENBVR_RETRY_QUEUE, 0, -1) == (
            [deferred_raw] if mutation == "deferral_push" else []
        )
        if mutation == "deferral_pop":
            assert bi_export_shared.reconcile_legacy_export_request(deferred)
            assert bi_export_shared.load_job(request_id)["request"] == deferred


class TestTerminalExportRejection:
    @pytest.mark.parametrize("saved", [False, True])
    @pytest.mark.parametrize("rejection", ["stale", "limit", "submission", "refreshed", "legacy"])
    def test_rejection_settles_once_across_watchdog_and_duplicates(self, monkeypatch, saved, rejection):
        payload = _request_payload(_export_attempts=1, _previous_target_path="@queued")
        error = "BI export command failed: fail"
        if rejection == "stale":
            payload["queued_at"] -= bi_export_shared.STALE_REQUEST_AGE + 60
            error = "stale request"
        elif rejection == "limit":
            payload["_export_attempts"] = bi_export_shared.MAX_EXPORT_ATTEMPTS
            error = "export retry limit reached"
        elif rejection == "refreshed":
            error = "BI export failed after refreshed OpenBVR retry: fail"
        elif rejection == "legacy":
            for field in ("queued_at", "_export_attempts", "_previous_target_path"):
                payload.pop(field)

        class FailedSession:
            def post(self, *_args, **_kwargs):
                class Response:
                    def json(self):
                        return {"result": "fail"}
                return Response()

        monkeypatch.setattr(bi_exporter, "get_session", lambda *_args: (FailedSession(), "sid"))
        monkeypatch.setattr(bi_exporter, "bi_get_export_queue", lambda *_args: [])
        if rejection == "refreshed":
            def fail_after_refresh(req, _tag):
                req.update(clip_path="@refreshed", offset=222, _openbvr_refreshed_attempts=1)
                return None, error
            monkeypatch.setattr(bi_exporter, "_prepare_export", fail_after_refresh)
        request_id = payload["request_id"]
        key = bi_export_shared.job_key(request_id)
        results = bi_export_shared.result_key(request_id)
        if saved:
            bi_export_shared.save_job(_retry_queued_job(
                payload, last_transition_at=time.time() - 90,
                submitted_at=1000, monitor_started_at=1005, queue_ack_at=1010,
            ))
        snapshot = bi_export_shared.load_job(request_id)
        _r.sadd(bi_export_shared.ACTIVE_EXPORT_SET, request_id)
        raw = json.dumps(payload).encode()
        bi_exporter._process_request(raw)

        stored = bi_export_shared.load_job(request_id)
        assert stored["status"] == "failed"
        assert stored["error"] == error
        assert stored["request"] == payload  # immutable identity even after preparation refresh
        if saved:
            assert [stored[k] for k in ("submitted_at", "monitor_started_at", "queue_ack_at")] == [1000, 1005, 1010]
        assert not _r.sismember(bi_export_shared.ACTIVE_EXPORT_SET, request_id)
        assert [json.loads(value) for value in _r.lrange(results, 0, -1)] == [
            {"ok": False, "path": None, "error": error},
        ]
        assert 0 < _r.ttl(results) <= bi_export_shared.RESULT_KEY_TTL
        settled_raw = _r.get(key)
        # Make any unintended TTL renewal observable without sleeping five minutes.
        _r.pexpire(results, 50000)
        for _ in range(4):
            if snapshot:
                bi_watchdog._repair_job(snapshot)
            bi_watchdog._run_once()
            _r.rpush(bi_export_shared.EXPORT_REQUEST_QUEUE, raw, raw)
            while item := _r.lpop(bi_export_shared.EXPORT_REQUEST_QUEUE):
                bi_exporter._process_request(item)
        assert _r.get(key) == settled_raw
        assert _r.llen(results) == 1
        assert 0 < _r.pttl(results) <= 50000
        assert _r.llen(bi_export_shared.EXPORT_REQUEST_QUEUE) == 0
        # The persisted failure also suppresses duplicates after results are consumed/expired.
        _r.lpop(results)
        bi_exporter._process_request(raw)
        assert _r.llen(results) == 0
        assert _r.pttl(results) == -2
        assert _r.get(key) == settled_raw

    @pytest.mark.parametrize("status", [
        "submitted", "queued", "ready", "downloading", "downloaded", "completed",
        "failed", "delivery_failed", "retry_queued",
    ])
    @pytest.mark.parametrize("stale", [False, True])
    def test_old_request_cannot_touch_newer_or_progressed_job(self, monkeypatch, status, stale):
        payload = _request_payload(queued_at=time.time() - 700 if stale else time.time())
        # Same timestamp, different retry metadata: queued_at alone is insufficient identity.
        current_request = dict(payload, _export_attempts=2, _previous_target_path="@newer")
        bi_export_shared.save_job(_retry_queued_job(current_request, status=status))
        request_id = payload["request_id"]
        key = bi_export_shared.job_key(request_id)
        before = _r.get(key)
        _r.sadd(bi_export_shared.ACTIVE_EXPORT_SET, request_id)
        def unexpected_prepare(*_args):
            pytest.fail("obsolete queue item must be rejected before contacting BI")
        monkeypatch.setattr(bi_exporter, "_prepare_export", unexpected_prepare)

        bi_exporter._process_request(json.dumps(payload).encode())

        assert _r.get(key) == before
        assert _r.sismember(bi_export_shared.ACTIVE_EXPORT_SET, request_id)
        assert _r.llen(bi_export_shared.result_key(request_id)) == 0

    def test_progressed_identical_request_is_not_resubmitted(self, monkeypatch):
        payload = _request_payload()
        bi_export_shared.save_job(_retry_queued_job(payload, status="submitted"))
        def unexpected_prepare(*_args):
            pytest.fail("already submitted request must not be exported twice")
        monkeypatch.setattr(bi_exporter, "_prepare_export", unexpected_prepare)
        before = _r.get(bi_export_shared.job_key(payload["request_id"]))
        bi_exporter._process_request(json.dumps(payload).encode())
        assert _r.get(bi_export_shared.job_key(payload["request_id"])) == before
        assert _r.llen(bi_export_shared.result_key(payload["request_id"])) == 0

    def test_old_watchdog_snapshot_cannot_replace_new_retry(self):
        payload = _request_payload()
        bi_export_shared.save_job(_retry_queued_job(payload, last_transition_at=time.time() - 90))
        snapshot = bi_export_shared.load_job(payload["request_id"])
        newer = dict(snapshot, request=dict(payload, _export_attempts=2))
        # Retain the snapshot's transition timestamp to exercise more than a timestamp CAS.
        bi_export_shared.save_job(newer)
        before = _r.get(bi_export_shared.job_key(payload["request_id"]))
        bi_watchdog._repair_job(snapshot)
        assert _r.get(bi_export_shared.job_key(payload["request_id"])) == before
        assert _r.llen(bi_export_shared.EXPORT_REQUEST_QUEUE) == 0

    @pytest.mark.parametrize("status", ["submitted", "queued"])
    def test_old_active_watchdog_snapshot_cannot_revive_terminal_retry(self, status):
        payload = _request_payload(queued_at=time.time() - 700)
        bi_export_shared.save_job(_retry_queued_job(
            payload, status=status, submitted_at=time.time() - 500,
        ))
        snapshot = bi_export_shared.load_job(payload["request_id"])
        bi_export_shared.save_job(dict(snapshot, status="retry_queued"))
        _r.sadd(bi_export_shared.ACTIVE_EXPORT_SET, payload["request_id"])
        bi_exporter._process_request(json.dumps(payload).encode())
        key = bi_export_shared.job_key(payload["request_id"])
        before = _r.get(key)
        bi_watchdog._repair_job(snapshot)
        assert _r.get(key) == before
        assert not _r.sismember(bi_export_shared.ACTIVE_EXPORT_SET, payload["request_id"])
        assert _r.llen(bi_export_shared.EXPORT_REQUEST_QUEUE) == 0
        assert _r.llen(bi_export_shared.result_key(payload["request_id"])) == 1

    @pytest.mark.parametrize("status", ["submitted", "queued"])
    @pytest.mark.parametrize("attempts", [1, bi_export_shared.MAX_EXPORT_ATTEMPTS])
    def test_watchdog_export_timeout_retains_retry_budget_and_original_times(self, status, attempts):
        payload = _request_payload()
        bi_export_shared.save_job(_retry_queued_job(
            payload, status=status, submitted_at=1000, monitor_started_at=1005,
            queue_ack_at=1010, export_attempts=attempts,
        ))
        request_id = payload["request_id"]
        _r.sadd(bi_export_shared.ACTIVE_EXPORT_SET, request_id)
        snapshot = bi_export_shared.load_job(request_id)
        bi_watchdog._repair_job(snapshot)
        stored = bi_export_shared.load_job(request_id)
        assert stored["status"] == ("retry_queued" if attempts == 1 else "failed")
        assert not _r.sismember(bi_export_shared.ACTIVE_EXPORT_SET, request_id)
        if attempts == 1:
            retry = json.loads(_r.lpop(bi_export_shared.EXPORT_REQUEST_QUEUE))
            assert retry == stored["request"]
            assert retry["_export_attempts"] == 1
            assert retry["_previous_target_path"] == "@queued"
            assert [retry[k] for k in (
                "_original_submitted_at", "_original_monitor_started_at", "_original_queue_ack_at",
            )] == [1000, 1005, 1010]
            assert retry["queued_at"] >= payload["queued_at"]
        before = _r.get(bi_export_shared.job_key(request_id))
        # A second pass with the obsolete snapshot cannot enqueue or publish again.
        bi_watchdog._repair_job(dict(snapshot, status=status, request=payload))
        assert _r.get(bi_export_shared.job_key(request_id)) == before
        assert _r.llen(bi_export_shared.EXPORT_REQUEST_QUEUE) == 0
        assert _r.llen(bi_export_shared.result_key(request_id)) == (0 if attempts == 1 else 1)

    def test_terminal_transition_has_no_partial_writes_before_exec(self, monkeypatch):
        payload = _request_payload(queued_at=time.time() - 700)
        bi_export_shared.save_job(_retry_queued_job(payload))
        request_id = payload["request_id"]
        _r.sadd(bi_export_shared.ACTIVE_EXPORT_SET, request_id)
        before = _r.get(bi_export_shared.job_key(request_id))
        pipeline_class = type(_r.pipeline())
        execute = pipeline_class.execute
        def interrupt(pipe, *_args, **_kwargs):
            raise RuntimeError("simulated interruption before EXEC")
        monkeypatch.setattr(pipeline_class, "execute", interrupt)
        with pytest.raises(RuntimeError, match="before EXEC"):
            bi_exporter._process_request(json.dumps(payload).encode())
        assert _r.get(bi_export_shared.job_key(request_id)) == before
        assert _r.sismember(bi_export_shared.ACTIVE_EXPORT_SET, request_id)
        assert _r.llen(bi_export_shared.result_key(request_id)) == 0
        monkeypatch.setattr(pipeline_class, "execute", execute)
        bi_exporter._process_request(json.dumps(payload).encode())
        assert bi_export_shared.load_job(request_id)["status"] == "failed"
        assert not _r.sismember(bi_export_shared.ACTIVE_EXPORT_SET, request_id)
        assert _r.llen(bi_export_shared.result_key(request_id)) == 1

    @pytest.mark.parametrize("saved", [False, True])
    @pytest.mark.parametrize("mutation", ["touch", "progress", "newer"])
    def test_exporter_watch_conflict_preserves_live_state(self, monkeypatch, saved, mutation):
        payload = _request_payload(queued_at=time.time() - 700)
        if saved:
            bi_export_shared.save_job(_retry_queued_job(payload))
        request_id = payload["request_id"]
        pipeline_class = type(_r.pipeline())
        execute = pipeline_class.execute
        interleaved = []
        def race(pipe, *args, **kwargs):
            if not interleaved:
                interleaved.append(True)
                current = bi_export_shared.load_job(request_id) or _retry_queued_job(payload)
                if mutation == "progress":
                    current["status"] = "submitted"
                elif mutation == "newer":
                    current["request"] = dict(payload, queued_at=time.time(), _export_attempts=2)
                else:
                    # Watchdog timestamp update keeps the same request eligible for settlement.
                    current["last_transition_at"] = time.time()
                bi_export_shared.save_job(current)
            return execute(pipe, *args, **kwargs)
        monkeypatch.setattr(pipeline_class, "execute", race)
        bi_exporter._process_request(json.dumps(payload).encode())
        assert interleaved
        status = {"progress": "submitted", "newer": "retry_queued", "touch": "failed"}[mutation]
        assert bi_export_shared.load_job(request_id)["status"] == status
        assert _r.llen(bi_export_shared.result_key(request_id)) == (1 if mutation == "touch" else 0)

    def test_exporter_settlement_wins_during_watchdog_transaction(self, monkeypatch):
        payload = _request_payload(queued_at=time.time() - 700)
        bi_export_shared.save_job(_retry_queued_job(payload, last_transition_at=time.time() - 90))
        snapshot = bi_export_shared.load_job(payload["request_id"])
        pipeline_class = type(_r.pipeline())
        execute = pipeline_class.execute
        interleaved = []
        def race(pipe, *args, **kwargs):
            if not interleaved:
                interleaved.append(True)
                bi_exporter._process_request(json.dumps(payload).encode())
            return execute(pipe, *args, **kwargs)
        monkeypatch.setattr(pipeline_class, "execute", race)
        bi_watchdog._repair_job(snapshot)
        assert interleaved
        assert bi_export_shared.load_job(payload["request_id"])["status"] == "failed"
        assert _r.llen(bi_export_shared.EXPORT_REQUEST_QUEUE) == 0
        assert _r.llen(bi_export_shared.result_key(payload["request_id"])) == 1

    @pytest.mark.parametrize("reattach", [False, True])
    def test_watchdog_requeued_valid_retry_can_succeed(self, monkeypatch, reattach):
        payload = _request_payload()
        job = _retry_queued_job(payload, status="queued", submitted_at=1000, monitor_started_at=1005, queue_ack_at=1010)
        bi_export_shared.queue_retry(job, "synthetic retry")
        _r.lpop(bi_export_shared.EXPORT_REQUEST_QUEUE)  # simulate lost consumption before restart
        snapshot = bi_export_shared.load_job(payload["request_id"])
        snapshot["last_transition_at"] = time.time() - 90
        bi_export_shared.save_job(snapshot)
        bi_watchdog._run_once()
        retried = _r.lpop(bi_export_shared.EXPORT_REQUEST_QUEUE)
        assert retried is not None
        class Session:
            def post(self, *_args, **_kwargs):
                class Response:
                    def json(self):
                        return {"result": "success", "data": {"path": "@new", "uri": "Clipboard/new.mp4"}}
                return Response()
        monkeypatch.setattr(bi_exporter, "get_session", lambda *_args: (Session(), "sid"))
        monkeypatch.setattr(bi_exporter, "bi_get_export_queue", lambda *_args: (
            [{"path": "@queued", "uri": "Clipboard/old.mp4"}] if reattach else []
        ))
        bi_exporter._process_request(retried)
        stored = bi_export_shared.load_job(payload["request_id"])
        assert stored["status"] == ("queued" if reattach else "submitted")
        assert stored["export_attempts"] == (1 if reattach else 2)
        if reattach:
            assert [stored[k] for k in ("submitted_at", "monitor_started_at", "queue_ack_at")] == [1000, 1005, 1010]
        assert _r.sismember(bi_export_shared.ACTIVE_EXPORT_SET, payload["request_id"])
        assert _r.llen(bi_export_shared.result_key(payload["request_id"])) == 0


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

    def test_queue_retry_includes_previous_target_path(self):
        payload = _request_payload()
        job = {
            "request_id": payload["request_id"],
            "config_name": payload["config_name"],
            "request": payload,
            "bi_url": payload["bi_url"],
            "bi_user": payload["bi_user"],
            "bi_pass": payload["bi_pass"],
            "output_path": payload["output_path"],
            "target_path": "@22833",
            "relative_uri": "Clipboard/foo.mp4",
            "delete_after": False,
            "restart_url": "",
            "restart_token": "",
            "status": "queued",
            "export_attempts": 1,
            "recovery_attempts": 0,
            "submitted_at": time.time(),
            "last_transition_at": time.time(),
        }
        bi_export_shared.save_job(job)

        bi_export_shared.queue_retry(job, "watchdog: export queue stale")

        raw = _r.blpop(bi_export_shared.EXPORT_REQUEST_QUEUE, timeout=1)
        assert raw is not None
        retry_req = json.loads(raw[1])
        assert retry_req["_previous_target_path"] == "@22833"

    def test_queue_retry_preserves_original_queue_timing(self):
        payload = _request_payload()
        job = {
            "request_id": payload["request_id"],
            "config_name": payload["config_name"],
            "request": payload,
            "bi_url": payload["bi_url"],
            "bi_user": payload["bi_user"],
            "bi_pass": payload["bi_pass"],
            "output_path": payload["output_path"],
            "target_path": "@22833",
            "relative_uri": "Clipboard/foo.mp4",
            "delete_after": False,
            "restart_url": "",
            "restart_token": "",
            "status": "queued",
            "export_attempts": 1,
            "recovery_attempts": 0,
            "submitted_at": 1000,
            "monitor_started_at": 1005,
            "queue_ack_at": 1010,
            "last_transition_at": time.time(),
        }

        bi_export_shared.queue_retry(job, "watchdog: export queue stale")

        raw = _r.blpop(bi_export_shared.EXPORT_REQUEST_QUEUE, timeout=1)
        assert raw is not None
        retry_req = json.loads(raw[1])
        assert retry_req["_original_submitted_at"] == 1000
        assert retry_req["_original_monitor_started_at"] == 1005
        assert retry_req["_original_queue_ack_at"] == 1010

    def test_queue_retry_persists_retry_request_onto_saved_job(self):
        """Watchdog stall path: job["request"] must carry _previous_target_path so that
        bi_watchdog.py's json.dumps(job["request"]) requeue doesn't lose it on a second stall."""
        payload = _request_payload()
        job = {
            "request_id": payload["request_id"],
            "config_name": payload["config_name"],
            "request": payload,
            "bi_url": payload["bi_url"],
            "bi_user": payload["bi_user"],
            "bi_pass": payload["bi_pass"],
            "output_path": payload["output_path"],
            "target_path": "@22833",
            "relative_uri": "Clipboard/foo.mp4",
            "delete_after": False,
            "restart_url": "",
            "restart_token": "",
            "status": "queued",
            "export_attempts": 1,
            "recovery_attempts": 0,
            "submitted_at": time.time(),
            "last_transition_at": time.time(),
        }
        bi_export_shared.save_job(job)
        bi_export_shared.queue_retry(job, "watchdog: export queue stale")

        stored = bi_export_shared.load_job(job["request_id"])
        assert stored["request"]["_previous_target_path"] == "@22833"

    def test_reattach_preserves_original_queue_age(self, monkeypatch):
        payload = _request_payload(
            _export_attempts=1,
            _previous_target_path="@22833",
            _original_submitted_at=1000,
            _original_monitor_started_at=1005,
            _original_queue_ack_at=1010,
        )

        monkeypatch.setattr(bi_exporter, "get_session", lambda *args, **kwargs: (object(), "sid"))
        monkeypatch.setattr(
            bi_exporter,
            "bi_get_export_queue",
            lambda *args, **kwargs: [{"path": "@22833", "uri": "Clipboard\\foo.mp4"}],
        )

        job, error = bi_exporter._prepare_export(payload, "[T]")

        assert error is None
        assert job["status"] == "queued"
        assert job["submitted_at"] == 1000
        assert job["monitor_started_at"] == 1005
        assert job["queue_ack_at"] == 1010

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

    def test_download_failure_requeues_export_with_same_metadata_first(self, monkeypatch):
        payload = _request_payload(
            clip_path="@queued",
            offset=111,
            duration=22222,
            trigger_filename="Garden.20260418_170000.11995.3-1.jpg",
        )
        job = {
            "request_id": payload["request_id"],
            "config_name": payload["config_name"],
            "request": dict(payload),
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
            "download_attempts": 0,
        }
        bi_export_shared.save_job(job)
        monkeypatch.setattr(
            bi_downloader,
            "_download_export",
            lambda current_job: (False, "download failed (file not ready)", None, None),
        )
        monkeypatch.setattr(bi_downloader, "trigger_bi_recovery", lambda *args, **kwargs: False)

        bi_downloader._process_download_request(job["request_id"])

        raw = _r.blpop(bi_export_shared.EXPORT_REQUEST_QUEUE, timeout=1)
        assert raw is not None
        retry_req = json.loads(raw[1])
        assert retry_req["clip_path"] == "@queued"
        assert retry_req["offset"] == 111
        assert retry_req["duration"] == 22222
        assert retry_req.get("_download_reexport_attempts") == 1
        assert retry_req.get("_refresh_before_export") is not True

    def test_second_download_failure_refreshes_metadata_before_reexport(self, monkeypatch):
        payload = _request_payload(
            clip_path="@queued",
            offset=111,
            duration=22222,
            trigger_filename="Garden.20260418_170000.11995.3-1.jpg",
        )
        job = {
            "request_id": payload["request_id"],
            "config_name": payload["config_name"],
            "request": dict(payload, _download_reexport_attempts=1),
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
            "export_attempts": 2,
            "recovery_attempts": 0,
            "download_attempts": 1,
        }
        bi_export_shared.save_job(job)
        monkeypatch.setattr(
            bi_downloader,
            "_download_export",
            lambda current_job: (False, "download failed (file not ready)", None, None),
        )
        monkeypatch.setattr(bi_downloader, "trigger_bi_recovery", lambda *args, **kwargs: False)
        monkeypatch.setattr(
            bi_downloader,
            "bi_lookup_alert",
            lambda *args, **kwargs: {
                "camera": "Garden",
                "file": "Garden.20260418_170000.11995.3-1.jpg",
                "path": "@4192553408.bvr",
                "clip": "@4192491959.bvr",
                "offset": 11995,
                "msec": 42557,
                "lookup_match_type": "exact_file",
                "export_source_path": "@4192553408.bvr",
                "export_source_field": "path",
            },
        )

        bi_downloader._process_download_request(job["request_id"])

        raw = _r.blpop(bi_export_shared.EXPORT_REQUEST_QUEUE, timeout=1)
        assert raw is not None
        retry_req = json.loads(raw[1])
        assert retry_req["clip_path"] == "@4192553408.bvr"
        assert retry_req["offset"] == 11995
        assert retry_req["duration"] == 42557
        assert retry_req.get("_download_reexport_attempts") == 2
        assert retry_req.get("_refresh_before_export") is True

    def test_second_download_failure_with_no_export_source_path_does_not_queue_reexport(self, monkeypatch):
        payload = _request_payload(
            clip_path="@queued",
            offset=111,
            duration=22222,
            trigger_filename="Garden.20260418_170000.11995.3-1.jpg",
        )
        job = {
            "request_id": payload["request_id"],
            "config_name": payload["config_name"],
            "request": dict(payload, _download_reexport_attempts=1),
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
            "export_attempts": 2,
            "recovery_attempts": 0,
            "download_attempts": 1,
        }
        bi_export_shared.save_job(job)
        monkeypatch.setattr(
            bi_downloader,
            "_download_export",
            lambda current_job: (False, "download failed (file not ready)", None, None),
        )
        monkeypatch.setattr(bi_downloader, "trigger_bi_recovery", lambda *args, **kwargs: False)
        monkeypatch.setattr(
            bi_downloader,
            "bi_lookup_alert",
            lambda *args, **kwargs: {
                "camera": "Garden",
                "file": "Garden.20260418_170000.11995.3-1.jpg",
                "path": None,
                "clip": None,
                "offset": 11995,
                "msec": 42557,
                "lookup_match_type": "exact_file",
                "export_source_path": None,
                "export_source_field": None,
            },
        )

        bi_downloader._process_download_request(job["request_id"])

        assert _r.blpop(bi_export_shared.EXPORT_REQUEST_QUEUE, timeout=1) is None
        stored = bi_export_shared.load_job(job["request_id"])
        assert stored["status"] == "failed"
        assert stored["error"] == "download metadata refresh: alert has no exportable path"


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


def test_bi_lookup_alert_returns_structured_metadata(monkeypatch):
    alert_rows = [
        {
            "camera": "Garden",
            "path": "@wrong_path.bvr",
            "clip": "@wrong_clip.bvr",
            "file": "Garden.20260418_170030.99999.3-1.jpg",
            "offset": 99999,
            "msec": 32115,
        },
        {
            "camera": "Garden",
            "path": "@4192553408.bvr",
            "clip": "@4192491959.bvr",
            "file": "Garden.20260418_170000.11995.3-1.jpg",
            "offset": 11995,
            "msec": 42557,
        },
    ]

    class FakeSession:
        def post(self, url, json=None, timeout=None):
            class R:
                def raise_for_status(self):
                    return None

                def json(self):
                    return {"data": alert_rows}

            return R()

    monkeypatch.setattr(bi_export_shared, "get_session", lambda *args, **kwargs: (FakeSession(), "sid"))

    result = bi_export_shared.bi_lookup_alert(
        "http://192.168.0.11:81",
        "admin",
        "secret",
        "Garden.20260418_170000.11995.3-1.jpg",
        "[Garden][d81bbc11]",
    )

    assert result == {
        "camera": "Garden",
        "file": "Garden.20260418_170000.11995.3-1.jpg",
        "path": "@4192553408.bvr",
        "clip": "@4192491959.bvr",
        "offset": 11995,
        "msec": 42557,
        "lookup_match_type": "exact_file",
        "export_source_path": "@4192553408.bvr",
        "export_source_field": "path",
    }


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

    def test_watchdog_does_not_requeue_processing_delivery_with_fresh_heartbeat(self, monkeypatch):
        payload = _request_payload(output_path="/tmp/watchdog_processing.mp4")
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
            "status": "downloaded",
            "delivery_context": {"config": {"telegram_token": "token", "chat_id": "chat"}},
            "delivery_status": "processing",
            "delivery_attempts": 1,
            "last_transition_at": time.time() - ((bi_export_shared.DELIVERY_QUEUE_STALE_AGE * 2) + 5),
        }
        bi_export_shared.save_job(job)

        requeued = []
        monkeypatch.setattr(bi_watchdog, "delivery_heartbeat_age", lambda *_args, **_kwargs: 5)
        monkeypatch.setattr(bi_watchdog, "mark_delivery_queued", lambda job: requeued.append(job["request_id"]))

        bi_watchdog._run_once()

        assert requeued == []


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

    def test_fallback_send_persists_new_msg_id_to_redis(self, monkeypatch):
        """Crash/retry path: after a successful fallback sendAnimation, the new
        last_msg_id must be flushed to Redis so a restarted worker doesn't
        call sendAnimation again and create a duplicate Telegram message."""
        raw_mp4 = "/tmp/video_delivery_worker_fallback_raw.mp4"
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
                    "last_msg_id": None,
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

        def fake_replace(config, *_args, **_kwargs):
            # Simulate the fallback sendAnimation setting a new message id.
            config["last_msg_id"] = 888
            return True

        monkeypatch.setattr(video_delivery_worker, "optimize_video_for_telegram", lambda *args, **kwargs: True)
        monkeypatch.setattr(video_delivery_worker, "replace_telegram_media", fake_replace)
        monkeypatch.setattr(video_delivery_worker, "analyze_video_gemini", lambda *args, **kwargs: None)
        monkeypatch.setattr(video_delivery_worker, "enrich_caption_with_dvla", lambda text, *_args, **_kwargs: text)
        monkeypatch.setattr(video_delivery_worker, "update_telegram_caption", lambda *args, **kwargs: True)

        video_delivery_worker._process_delivery_request(job["request_id"])

        stored = bi_export_shared.load_job(job["request_id"])
        assert stored["delivery_context"]["config"]["last_msg_id"] == 888

    def test_duplicate_delivery_queue_item_is_skipped_when_claim_exists(self, monkeypatch):
        raw_mp4 = "/tmp/video_delivery_worker_claim_raw.mp4"
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
        _r.set(bi_export_shared.delivery_claim_key(job["request_id"]), "other-worker", ex=60)

        called = []
        monkeypatch.setattr(
            video_delivery_worker,
            "optimize_video_for_telegram",
            lambda *args, **kwargs: called.append("opt") or True,
        )

        video_delivery_worker._process_delivery_request(job["request_id"])

        stored = bi_export_shared.load_job(job["request_id"])
        assert called == []
        assert stored["delivery_status"] == "queued"


def test_deferred_openbvr_retry_refreshes_alert_lookup_before_retry(monkeypatch):
    payload = _request_payload(
        clip_path="@clip/original",
        offset=10,
        duration=10000,
        trigger_filename="alert.jpg",
        _openbvr_deferred_attempts=1,
    )

    monkeypatch.setattr(bi_exporter, "bi_get_export_queue", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        bi_exporter,
        "bi_lookup_alert",
        lambda *args, **kwargs: {
            "camera": "TestCam",
            "file": "alert.jpg",
            "path": "@clip/refreshed",
            "clip": "@clip/ignored",
            "offset": 222,
            "msec": 33333,
            "lookup_match_type": "exact_file",
            "export_source_path": "@clip/refreshed",
            "export_source_field": "path",
        },
    )

    posted = []

    class FakeSession:
        def post(self, url, json=None, timeout=None):
            posted.append(dict(json))

            class R:
                def __init__(self, body):
                    self._body = body

                def json(self):
                    return self._body

            if len(posted) == 1:
                return R({"result": "fail", "data": {"reason": "OpenBVR failed"}})
            return R({"result": "success", "data": {"path": "@22842", "uri": "Clipboard\\new.mp4"}})

    monkeypatch.setattr(bi_exporter, "get_session", lambda *args, **kwargs: (FakeSession(), "sid"))

    job, error = bi_exporter._prepare_export(payload, "[T]")

    assert error is None
    assert job["request"]["clip_path"] == "@clip/refreshed"
    assert job["request"]["offset"] == 222
    assert job["request"]["duration"] == 33333
    assert posted[0]["path"] == "@clip/refreshed.bvr"
    assert posted[0]["startms"] == 222
    assert posted[0]["msec"] == 33333


def test_deferred_openbvr_refresh_failure_returns_specific_error(monkeypatch):
    payload = _request_payload(
        clip_path="@clip/original",
        offset=10,
        duration=10000,
        trigger_filename="alert.jpg",
        _openbvr_deferred_attempts=1,
    )

    monkeypatch.setattr(bi_exporter, "bi_get_export_queue", lambda *args, **kwargs: [])
    monkeypatch.setattr(bi_exporter, "bi_lookup_alert", lambda *args, **kwargs: None)

    class FakeSession:
        def post(self, url, json=None, timeout=None):
            class R:
                def json(self):
                    return {"result": "fail", "data": {"reason": "OpenBVR failed"}}

            return R()

    monkeypatch.setattr(bi_exporter, "get_session", lambda *args, **kwargs: (FakeSession(), "sid"))

    job, error = bi_exporter._prepare_export(payload, "[T]")

    assert job is None
    assert error == "BI alert lookup refresh found no matching alert after OpenBVR error"


def test_openbvr_immediate_retry_keeps_original_metadata(monkeypatch):
    payload = _request_payload(
        clip_path="@clip/original",
        offset=10,
        duration=10000,
        trigger_filename="alert.jpg",
    )

    monkeypatch.setattr(bi_exporter, "bi_get_export_queue", lambda *args, **kwargs: [])

    posted = []

    class FakeSession:
        def post(self, url, json=None, timeout=None):
            posted.append(dict(json))

            class R:
                def json(self):
                    if len(posted) == 1:
                        return {"result": "fail", "data": {"reason": "OpenBVR failed"}}
                    return {"result": "success", "data": {"path": "@22842", "uri": "Clipboard\\new.mp4"}}

            return R()

    monkeypatch.setattr(bi_exporter, "get_session", lambda *args, **kwargs: (FakeSession(), "sid"))

    job, error = bi_exporter._prepare_export(payload, "[T]")

    assert error is None
    assert job["request"]["clip_path"] == "@clip/original"
    assert posted[0]["path"] == "@clip/original.bvr"
    assert posted[1]["path"] == "@clip/original.bvr"
    assert posted[1]["startms"] == 10
    assert posted[1]["msec"] == 10000


def test_openbvr_idle_second_failure_refreshes_and_retries(monkeypatch):
    payload = _request_payload(
        clip_path="@clip/original",
        offset=10,
        duration=10000,
        trigger_filename="alert.jpg",
    )

    monkeypatch.setattr(bi_exporter, "bi_get_export_queue", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        bi_exporter,
        "bi_lookup_alert",
        lambda *args, **kwargs: {
            "camera": "TestCam",
            "file": "alert.jpg",
            "path": "@clip/refreshed",
            "clip": "@clip/ignored",
            "offset": 222,
            "msec": 33333,
            "lookup_match_type": "exact_file",
            "export_source_path": "@clip/refreshed",
            "export_source_field": "path",
        },
    )

    class FakeRedis:
        def scard(self, key):
            assert key == bi_export_shared.ACTIVE_EXPORT_SET
            return 0

        def llen(self, key):
            raise AssertionError("deferred queue depth should not be checked when idle")

        def rpush(self, key, value):
            raise AssertionError("should not defer when already idle")

    posted = []

    class FakeSession:
        def post(self, url, json=None, timeout=None):
            posted.append(dict(json))

            class R:
                def __init__(self, body):
                    self._body = body

                def json(self):
                    return self._body

            if len(posted) < 3:
                return R({"result": "fail", "data": {"reason": "OpenBVR failed"}})
            return R({"result": "success", "data": {"path": "@22842", "uri": "Clipboard\\new.mp4"}})

    monkeypatch.setattr(bi_exporter, "r", FakeRedis())
    monkeypatch.setattr(bi_exporter, "get_session", lambda *args, **kwargs: (FakeSession(), "sid"))

    job, error = bi_exporter._prepare_export(payload, "[T]")

    assert error is None
    assert job["request"]["clip_path"] == "@clip/refreshed"
    assert posted[0]["path"] == "@clip/original.bvr"
    assert posted[1]["path"] == "@clip/original.bvr"
    assert posted[2]["path"] == "@clip/refreshed.bvr"
    assert posted[2]["startms"] == 222
    assert posted[2]["msec"] == 33333


def test_openbvr_refresh_then_export_failure_uses_specific_terminal_error(monkeypatch):
    payload = _request_payload(
        clip_path="@clip/original",
        offset=10,
        duration=10000,
        trigger_filename="Garden.20260418_170000.11995.3-1.jpg",
    )

    monkeypatch.setattr(bi_exporter, "bi_get_export_queue", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        bi_exporter,
        "bi_lookup_alert",
        lambda *args, **kwargs: {
            "camera": "Garden",
            "file": "Garden.20260418_170000.11995.3-1.jpg",
            "path": "@4192553408.bvr",
            "clip": "@4192491959.bvr",
            "offset": 11995,
            "msec": 42557,
            "lookup_match_type": "exact_file",
            "export_source_path": "@4192553408.bvr",
            "export_source_field": "path",
        },
    )

    class FakeRedis:
        def scard(self, key):
            assert key == bi_export_shared.ACTIVE_EXPORT_SET
            return 0

        def llen(self, key):
            raise AssertionError("deferred queue depth should not be checked when idle")

        def rpush(self, key, value):
            raise AssertionError("should not defer when already idle")

        def sadd(self, *args, **kwargs):
            raise AssertionError("job should not be marked active on terminal export failure")

    class FakeSession:
        def __init__(self):
            self.calls = 0

        def post(self, url, json=None, timeout=None):
            self.calls += 1

            class R:
                def __init__(self, body):
                    self._body = body

                def json(self):
                    return self._body

            if self.calls < 3:
                return R({"result": "fail", "data": {"reason": "OpenBVR failed"}})
            return R({"result": "fail", "data": {"reason": "fail"}})

    terminal = {}
    monkeypatch.setattr(bi_exporter, "r", FakeRedis())
    monkeypatch.setattr(bi_exporter, "get_session", lambda *args, **kwargs: (FakeSession(), "sid"))
    monkeypatch.setattr(
        bi_exporter,
        "log_terminal_diagnosis",
        lambda _logger, _tag, _job, _phase, error_code, **extra: terminal.update(
            {"error_code": error_code, **extra}
        ),
    )

    bi_exporter._process_request(json.dumps(payload).encode())

    assert terminal["error_code"] == "openbvr_failed_after_refresh"
    assert terminal["error"] == "BI export failed after refreshed OpenBVR retry: fail"


@pytest.mark.parametrize("already_queued", [False, True])
def test_openbvr_watchdog_preserves_queue_membership_priority_and_age(monkeypatch, already_queued):
    now = time.time()
    payload = _request_payload(
        queued_at=now, _openbvr_deferred_attempts=1,
    )
    queue = bi_export_shared.OPENBVR_RETRY_QUEUE
    job = _retry_queued_job(payload, retry_queue=queue, last_transition_at=now)
    bi_export_shared.save_job(job)
    raw = json.dumps(payload).encode()
    before = json.dumps(_request_payload(request_id="synthetic-before")).encode()
    after = json.dumps(_request_payload(request_id="synthetic-after")).encode()
    _r.rpush(queue, before)
    if already_queued:
        _r.rpush(queue, raw)
    _r.rpush(queue, after)
    _r.sadd(bi_export_shared.ACTIVE_EXPORT_SET, "synthetic-other-export")
    expected_queue = [before, raw, after] if already_queued else [before, after, raw]

    # Keep BI busy past the request-age limit: watchdog must neither append more
    # copies nor move an existing entry behind its neighbors or renew its age.
    for elapsed in (90, 180, bi_export_shared.STALE_REQUEST_AGE + 90):
        monkeypatch.setattr(bi_watchdog.time, "time", lambda elapsed=elapsed: now + elapsed)
        bi_watchdog._run_once()
        assert _r.lrange(queue, 0, -1) == expected_queue
        stored = bi_export_shared.load_job(payload["request_id"])
        assert stored["request"] == payload
        assert stored["last_transition_at"] == now + elapsed
        assert _r.llen(bi_export_shared.EXPORT_REQUEST_QUEUE) == 0
        assert _r.llen(bi_export_shared.result_key(payload["request_id"])) == 0

    # Once idle, normal consumption must still reject the aged request.
    _r.srem(bi_export_shared.ACTIVE_EXPORT_SET, "synthetic-other-export")
    assert _r.lrem(queue, 1, raw) == 1
    monkeypatch.setattr(bi_exporter, "_prepare_export", lambda *_args: pytest.fail("stale request submitted"))
    bi_exporter._process_request(raw)
    stored = bi_export_shared.load_job(payload["request_id"])
    assert stored["status"] == "failed"
    assert stored["request"] == payload
    assert _r.llen(bi_export_shared.result_key(payload["request_id"])) == 1
    bi_watchdog._run_once()
    assert _r.lrange(queue, 0, -1) == [before, after]


@pytest.mark.parametrize("openbvr_fails", [False, True])
@pytest.mark.parametrize("refreshed", [False, True])
def test_legacy_openbvr_deferral_consumed_directly(monkeypatch, openbvr_fails, refreshed):
    original, deferred, _saved = _legacy_openbvr_retry(refreshed=refreshed)
    queue = bi_export_shared.OPENBVR_RETRY_QUEUE
    _r.rpush(queue, json.dumps(deferred))
    # Other exports appearing after the idle pop must not grant a second deferral.
    _r.sadd(bi_export_shared.ACTIVE_EXPORT_SET, "synthetic-other-export")
    monkeypatch.setattr(bi_exporter, "bi_get_export_queue", lambda *_args: [])
    monkeypatch.setattr(bi_exporter, "bi_lookup_alert", lambda *_args: {
        "file": "synthetic-alert.jpg", "path": "@clip/refreshed", "offset": 222,
        "msec": 33333, "export_source_path": "@clip/refreshed", "export_source_field": "path",
    })
    monkeypatch.setattr(bi_exporter.time, "sleep", lambda *_args: None)
    posted = []

    class Session:
        def post(self, _url, json=None, **_kwargs):
            posted.append(dict(json))
            class Response:
                def json(self):
                    if openbvr_fails:
                        return {"result": "fail", "data": {"reason": "OpenBVR failed"}}
                    return {"result": "success", "data": {"path": "@new", "uri": "Clipboard/new.mp4"}}
            return Response()

    monkeypatch.setattr(bi_exporter, "get_session", lambda *_args: (Session(), "sid"))
    bi_exporter._process_request(_r.lpop(queue))
    stored = bi_export_shared.load_job(original["request_id"])
    assert stored["status"] == ("failed" if openbvr_fails else "submitted")
    assert stored["request"]["queued_at"] == deferred["queued_at"]
    assert stored["request"]["_openbvr_deferred_attempts"] == 1
    assert len(posted) == ((2 if refreshed else 3) if openbvr_fails else 1)
    assert all(payload["path"] == "@clip/refreshed.bvr" for payload in posted)
    before = _r.get(bi_export_shared.job_key(original["request_id"]))
    for payload in (original, deferred):
        bi_exporter._process_request(json.dumps(payload).encode())
    assert _r.get(bi_export_shared.job_key(original["request_id"])) == before
    assert _r.llen(queue) == 0
    assert _r.llen(bi_export_shared.EXPORT_REQUEST_QUEUE) == 0
    assert _r.llen(bi_export_shared.result_key(original["request_id"])) == int(openbvr_fails)


@pytest.mark.parametrize("watchdog_first", [False, True])
@pytest.mark.parametrize("deferred_age", [10, 700])
@pytest.mark.parametrize("refreshed", [False, True])
def test_legacy_openbvr_old_main_copy_cannot_fail_queued_deferral(monkeypatch, watchdog_first, deferred_age, refreshed):
    original, deferred, saved = _legacy_openbvr_retry(deferred_age=deferred_age, refreshed=refreshed)
    request_id = original["request_id"]
    queue = bi_export_shared.OPENBVR_RETRY_QUEUE
    raw = json.dumps(deferred).encode()
    before = json.dumps(_request_payload(request_id="synthetic-before")).encode()
    after = json.dumps(_request_payload(request_id="synthetic-after")).encode()
    _r.rpush(queue, before, raw, after)
    _r.rpush(bi_export_shared.EXPORT_REQUEST_QUEUE, json.dumps(original))
    _r.sadd(bi_export_shared.ACTIVE_EXPORT_SET, "synthetic-other-export")
    monkeypatch.setattr(bi_exporter, "_prepare_export", lambda *_args: pytest.fail("old copy submitted early"))
    if watchdog_first:
        bi_watchdog._repair_job(saved)
    bi_exporter._process_request(_r.lpop(bi_export_shared.EXPORT_REQUEST_QUEUE))
    stored = bi_export_shared.load_job(request_id)
    assert stored["status"] == "retry_queued"
    assert stored["request"] == deferred
    assert stored["retry_queue"] == queue
    assert [stored[key] for key in ("submitted_at", "monitor_started_at", "queue_ack_at")] == [1000, 1005, 1010]
    assert _r.llen(bi_export_shared.result_key(request_id)) == 0
    # Repair scans preserve ordering, membership and age while BI stays busy.
    now = time.time()
    for elapsed in (90, 180, 270):
        monkeypatch.setattr(bi_watchdog.time, "time", lambda elapsed=elapsed: now + elapsed)
        bi_watchdog._run_once()
        assert bi_export_shared.load_job(request_id)["request"] == deferred
    assert _r.lrange(queue, 0, -1) == [before, raw, after]
    assert _r.llen(bi_export_shared.EXPORT_REQUEST_QUEUE) == 0
    _r.srem(bi_export_shared.ACTIVE_EXPORT_SET, "synthetic-other-export")
    assert _r.lrem(queue, 1, raw) == 1
    if deferred_age < bi_export_shared.STALE_REQUEST_AGE:
        monkeypatch.setattr(bi_exporter, "_prepare_export", lambda req, _tag: (
            _retry_queued_job(req, status="submitted", export_attempts=2), None,
        ))
    bi_exporter._process_request(raw)
    stored = bi_export_shared.load_job(request_id)
    assert stored["status"] == ("submitted" if deferred_age == 10 else "failed")
    assert stored["request"] == deferred
    if deferred_age == 700:
        assert stored["error"] == "stale request"
        results = bi_export_shared.result_key(request_id)
        assert _r.llen(results) == 1
        _r.pexpire(results, 50000)
        for payload in (original, deferred, deferred):
            bi_exporter._process_request(json.dumps(payload).encode())
        bi_watchdog._run_once()
        assert _r.llen(results) == 1
        assert 0 < _r.pttl(results) <= 50000
    assert _r.lrange(queue, 0, -1) == [before, after]


@pytest.mark.parametrize("refreshed", [False, True])
def test_legacy_openbvr_stale_deferral_consumed_directly(monkeypatch, refreshed):
    original, deferred, saved = _legacy_openbvr_retry(deferred_age=700, refreshed=refreshed)
    request_id = original["request_id"]
    monkeypatch.setattr(bi_exporter, "_prepare_export", lambda *_args: pytest.fail("stale deferral submitted"))
    bi_exporter._process_request(json.dumps(deferred).encode())
    stored = bi_export_shared.load_job(request_id)
    assert stored["status"] == "failed"
    assert stored["error"] == "stale request"
    assert stored["request"] == deferred
    assert stored["submitted_at"] == saved["submitted_at"]
    before = _r.get(bi_export_shared.job_key(request_id))
    bi_watchdog._repair_job(saved)
    bi_exporter._process_request(json.dumps(deferred).encode())
    assert _r.get(bi_export_shared.job_key(request_id)) == before
    assert _r.llen(bi_export_shared.result_key(request_id)) == 1
    assert 0 < _r.ttl(bi_export_shared.result_key(request_id)) <= bi_export_shared.RESULT_KEY_TTL
    assert _r.llen(bi_export_shared.EXPORT_REQUEST_QUEUE) == 0
    assert _r.llen(bi_export_shared.OPENBVR_RETRY_QUEUE) == 0


def test_legacy_openbvr_fresh_old_main_copy_stays_idle_only(monkeypatch):
    original, deferred, _saved = _legacy_openbvr_retry(original_age=50)
    queue = bi_export_shared.OPENBVR_RETRY_QUEUE
    raw = json.dumps(deferred).encode()
    _r.rpush(queue, raw)
    _r.sadd(bi_export_shared.ACTIVE_EXPORT_SET, "synthetic-other-export")
    monkeypatch.setattr(bi_exporter, "_prepare_export", lambda *_args: pytest.fail("deferred work submitted early"))
    bi_exporter._process_request(json.dumps(original).encode())
    stored = bi_export_shared.load_job(original["request_id"])
    assert stored["request"] == deferred
    assert stored["status"] == "retry_queued"
    assert stored["retry_queue"] == queue
    assert _r.lrange(queue, 0, -1) == [raw]
    assert _r.llen(bi_export_shared.result_key(original["request_id"])) == 0


@pytest.mark.parametrize("mutation", ["timestamp_only", "source", "attempts"])
def test_legacy_openbvr_nonmatching_queued_item_cannot_rescue_stale_request(monkeypatch, mutation):
    original, deferred, _saved = _legacy_openbvr_retry()
    if mutation == "timestamp_only":
        deferred.pop("_openbvr_deferred_attempts")
    elif mutation == "source":
        deferred["clip_path"] = "@different-source"
    else:
        deferred["_export_attempts"] = 2
    raw = json.dumps(deferred).encode()
    queue = bi_export_shared.OPENBVR_RETRY_QUEUE
    _r.rpush(queue, raw)
    monkeypatch.setattr(bi_exporter, "_prepare_export", lambda *_args: pytest.fail("stale request submitted"))
    bi_exporter._process_request(json.dumps(original).encode())
    stored = bi_export_shared.load_job(original["request_id"])
    assert stored["status"] == "failed"
    assert stored["request"] == original
    assert stored["error"] == "stale request"
    before = _r.get(bi_export_shared.job_key(original["request_id"]))
    bi_exporter._process_request(_r.lpop(queue))
    assert _r.get(bi_export_shared.job_key(original["request_id"])) == before
    assert _r.llen(bi_export_shared.result_key(original["request_id"])) == 1


@pytest.mark.parametrize("mutation", [
    "timestamp_only", "older_timestamp", "future_timestamp", "source", "lookup", "attempts",
    "recovery", "target", "original_time", "delivery", "second_deferral", "already_deferred",
    "newer_generation", "newer_request", "queue_marker", "submitted", "ready", "completed", "failed",
])
@pytest.mark.parametrize("refreshed", [False, True])
def test_legacy_openbvr_adoption_requires_exact_old_relationship(monkeypatch, mutation, refreshed):
    original, deferred, saved = _legacy_openbvr_retry(refreshed=refreshed)
    changes = {
        "older_timestamp": {"queued_at": original["queued_at"] - 1},
        "future_timestamp": {"queued_at": time.time() + 100},
        "source": {"clip_path": "@different-source"},
        "lookup": {"_lookup_path": "@different-lookup"},
        "attempts": {"_export_attempts": 2},
        "recovery": {"_recovery_attempts": 1},
        "target": {"_previous_target_path": "@different-target"},
        "original_time": {"_original_submitted_at": 999},
        "delivery": {"delivery_context": {"prompt": "synthetic changed prompt"}},
        "second_deferral": {"_openbvr_deferred_attempts": 2},
    }
    deferred.update(changes.get(mutation, {}))
    if mutation == "timestamp_only":
        deferred.pop("_openbvr_deferred_attempts")
    elif mutation == "already_deferred":
        saved["request"] = dict(original, _openbvr_deferred_attempts=1)
    elif mutation == "newer_generation":
        saved["export_attempts"] = 2
    elif mutation == "newer_request":
        saved["request"] = dict(original, queued_at=time.time())
    elif mutation == "queue_marker":
        saved["retry_queue"] = bi_export_shared.EXPORT_REQUEST_QUEUE
    elif mutation in {"submitted", "ready", "completed", "failed"}:
        saved["status"] = mutation
    key = bi_export_shared.job_key(original["request_id"])
    _r.set(key, json.dumps(saved))
    before = _r.get(key)
    _r.sadd(bi_export_shared.ACTIVE_EXPORT_SET, original["request_id"])
    monkeypatch.setattr(bi_exporter, "_prepare_export", lambda *_args: pytest.fail("nonmatching legacy item submitted"))
    bi_exporter._process_request(json.dumps(deferred).encode())
    assert _r.get(key) == before
    assert _r.sismember(bi_export_shared.ACTIVE_EXPORT_SET, original["request_id"])
    assert _r.llen(bi_export_shared.result_key(original["request_id"])) == 0
    assert _r.llen(bi_export_shared.OPENBVR_RETRY_QUEUE) == 0
    assert _r.llen(bi_export_shared.EXPORT_REQUEST_QUEUE) == 0


@pytest.mark.parametrize("source_field", ["path", "clip"])
@pytest.mark.parametrize("mutation", [
    "valid", "no_flag", "source", "file", "match_type", "offset", "missing_field", "extra_field",
])
def test_legacy_openbvr_refreshed_deferral_matches_old_lookup_only(monkeypatch, source_field, mutation):
    original, _deferred, saved = _legacy_openbvr_retry(refreshed=True)
    if mutation == "no_flag":
        original.pop("_openbvr_refreshed_attempts")
    key = bi_export_shared.job_key(original["request_id"])
    _r.set(key, json.dumps(saved))
    before = _r.get(key)
    source = "@clip/new-lookup"
    lookup = {
        "file": original["trigger_filename"], "path": source if source_field == "path" else None,
        "clip": source, "offset": 444, "msec": 55555, "lookup_match_type": "exact_file",
        "export_source_path": source, "export_source_field": source_field,
    }
    lookups = []

    def lookup_alert(*_args):
        lookups.append(True)
        return lookup

    monkeypatch.setattr(bi_exporter, "bi_lookup_alert", lookup_alert)
    deferred = dict(original)
    assert bi_exporter._refresh_export_request_after_openbvr(deferred, {}, "synthetic") == (True, None)
    deferred.update(queued_at=time.time(), _openbvr_deferred_attempts=1)
    changes = {
        "source": {"clip_path": "@clip/inconsistent"},
        "file": {"_lookup_file": "synthetic-other-alert.jpg"},
        "match_type": {"_lookup_match_type": "unrelated"},
        "offset": {"offset": "invalid"},
        "extra_field": {"delivery_context": {"prompt": "synthetic changed prompt"}},
    }
    deferred.update(changes.get(mutation, {}))
    if mutation == "missing_field":
        deferred.pop("_lookup_clip")
    assert bi_export_shared.reconcile_legacy_export_request(deferred) == (mutation == "valid")
    if mutation == "valid":
        stored = bi_export_shared.load_job(original["request_id"])
        assert stored["request"] == deferred
        assert stored["retry_queue"] == bi_export_shared.OPENBVR_RETRY_QUEUE
    else:
        assert _r.get(key) == before
    assert lookups == [True]
    assert _r.llen(bi_export_shared.OPENBVR_RETRY_QUEUE) == 0
    assert _r.llen(bi_export_shared.result_key(original["request_id"])) == 0


@pytest.mark.parametrize("path", ["intake", "watchdog", "outcome"])
@pytest.mark.parametrize("mutation", ["pop", "settlement", "newer"])
def test_legacy_openbvr_queued_adoption_watches_job_and_queue(monkeypatch, path, mutation):
    original, deferred, saved = _legacy_openbvr_retry()
    request_id = original["request_id"]
    key = bi_export_shared.job_key(request_id)
    queue = bi_export_shared.OPENBVR_RETRY_QUEUE
    raw = json.dumps(deferred).encode()
    _r.rpush(queue, raw)
    pipeline_class = type(_r.pipeline())
    execute = pipeline_class.execute
    interleaved = []

    def race(pipe, *args, **kwargs):
        if not interleaved:
            interleaved.append(True)
            if mutation == "pop":
                assert _r.lpop(queue) == raw
            else:
                updated = dict(saved, status="failed") if mutation == "settlement" else dict(
                    saved, request=dict(original, _export_attempts=2), export_attempts=2,
                )
                _r.set(key, json.dumps(updated))
            interleaved.append(_r.get(key))
        return execute(pipe, *args, **kwargs)

    monkeypatch.setattr(pipeline_class, "execute", race)
    if path == "intake":
        assert not bi_export_shared.reconcile_legacy_export_request(original)
    elif path == "watchdog":
        assert bi_export_shared.requeue_export_retry(saved, time.time()) is None
    else:
        assert bi_export_shared.transition_export_request(original, error_msg="stale request") is None
    assert interleaved
    assert _r.get(key) == interleaved[1]
    assert _r.llen(bi_export_shared.result_key(request_id)) == 0
    assert _r.llen(bi_export_shared.EXPORT_REQUEST_QUEUE) == 0
    assert _r.lrange(queue, 0, -1) == ([] if mutation == "pop" else [raw])
    if mutation == "pop":
        # The consumer of D still owns a valid deferred attempt after P's abort.
        assert bi_export_shared.reconcile_legacy_export_request(deferred)
        assert bi_export_shared.load_job(request_id)["request"] == deferred


@pytest.mark.parametrize("mutation", ["touch", "settlement", "newer"])
def test_legacy_openbvr_popped_adoption_revalidates_job_after_conflict(monkeypatch, mutation):
    original, deferred, saved = _legacy_openbvr_retry()
    request_id = original["request_id"]
    key = bi_export_shared.job_key(request_id)
    pipeline_class = type(_r.pipeline())
    execute = pipeline_class.execute
    interleaved = []

    def race(pipe, *args, **kwargs):
        if not interleaved:
            interleaved.append(True)
            if mutation == "touch":
                updated = dict(saved, last_transition_at=time.time())
            elif mutation == "settlement":
                updated = dict(saved, status="failed")
            else:
                updated = dict(saved, request=dict(original, _export_attempts=2), export_attempts=2)
            _r.set(key, json.dumps(updated))
            interleaved.append(_r.get(key))
        return execute(pipe, *args, **kwargs)

    monkeypatch.setattr(pipeline_class, "execute", race)
    assert bi_export_shared.reconcile_legacy_export_request(deferred) == (mutation == "touch")
    assert interleaved
    if mutation == "touch":
        stored = bi_export_shared.load_job(request_id)
        assert stored["request"] == deferred
        assert stored["last_transition_at"] == json.loads(interleaved[1])["last_transition_at"]
    else:
        assert _r.get(key) == interleaved[1]
    assert _r.llen(bi_export_shared.result_key(request_id)) == 0
    assert _r.llen(bi_export_shared.EXPORT_REQUEST_QUEUE) == 0
    assert _r.llen(bi_export_shared.OPENBVR_RETRY_QUEUE) == 0


@pytest.mark.parametrize("path", ["popped", "intake", "watchdog", "outcome"])
def test_legacy_openbvr_adoption_has_no_partial_writes_before_exec(monkeypatch, path):
    original, deferred, saved = _legacy_openbvr_retry()
    request_id = original["request_id"]
    key = bi_export_shared.job_key(request_id)
    queue = bi_export_shared.OPENBVR_RETRY_QUEUE
    if path != "popped":
        _r.rpush(queue, json.dumps(deferred))
    before = _r.get(key)
    queued = _r.lrange(queue, 0, -1)
    _r.sadd(bi_export_shared.ACTIVE_EXPORT_SET, request_id)

    def interrupt(*_args, **_kwargs):
        raise RuntimeError("interrupted legacy adoption before EXEC")

    monkeypatch.setattr(type(_r.pipeline()), "execute", interrupt)
    with pytest.raises(RuntimeError, match="before EXEC"):
        if path == "popped":
            bi_export_shared.reconcile_legacy_export_request(deferred)
        elif path == "intake":
            bi_export_shared.reconcile_legacy_export_request(original)
        elif path == "watchdog":
            bi_export_shared.requeue_export_retry(saved, time.time())
        else:
            bi_export_shared.transition_export_request(original, error_msg="stale request")
    assert _r.get(key) == before
    assert _r.lrange(queue, 0, -1) == queued
    assert _r.sismember(bi_export_shared.ACTIVE_EXPORT_SET, request_id)
    assert _r.llen(bi_export_shared.EXPORT_REQUEST_QUEUE) == 0
    assert _r.llen(bi_export_shared.result_key(request_id)) == 0


def test_legacy_openbvr_deferral_arriving_after_intake_wins_over_old_rejection():
    original, deferred, _saved = _legacy_openbvr_retry()
    assert bi_export_shared.reconcile_legacy_export_request(original)
    raw = json.dumps(deferred).encode()
    _r.rpush(bi_export_shared.OPENBVR_RETRY_QUEUE, raw)
    assert bi_export_shared.transition_export_request(original, error_msg="stale request") is None
    stored = bi_export_shared.load_job(original["request_id"])
    assert stored["status"] == "retry_queued"
    assert stored["request"] == deferred
    assert stored["retry_queue"] == bi_export_shared.OPENBVR_RETRY_QUEUE
    assert _r.lrange(bi_export_shared.OPENBVR_RETRY_QUEUE, 0, -1) == [raw]
    assert _r.llen(bi_export_shared.result_key(original["request_id"])) == 0


def test_legacy_openbvr_queue_push_during_rejection_exec_aborts_failure(monkeypatch):
    original, deferred, saved = _legacy_openbvr_retry()
    key = bi_export_shared.job_key(original["request_id"])
    before = _r.get(key)
    queue = bi_export_shared.OPENBVR_RETRY_QUEUE
    raw = json.dumps(deferred).encode()
    pipeline_class = type(_r.pipeline())
    execute = pipeline_class.execute
    interleaved = []

    def race(pipe, *args, **kwargs):
        if not interleaved:
            interleaved.append(True)
            _r.rpush(queue, raw)
        return execute(pipe, *args, **kwargs)

    monkeypatch.setattr(pipeline_class, "execute", race)
    bi_exporter._process_request(json.dumps(original).encode())
    assert interleaved
    assert _r.get(key) == before
    assert _r.llen(bi_export_shared.result_key(original["request_id"])) == 0
    assert _r.lrange(queue, 0, -1) == [raw]
    bi_watchdog._repair_job(saved)
    assert bi_export_shared.load_job(original["request_id"])["request"] == deferred
    assert _r.lrange(queue, 0, -1) == [raw]
    assert _r.llen(bi_export_shared.EXPORT_REQUEST_QUEUE) == 0


@pytest.mark.parametrize("already_queued", [False, True])
@pytest.mark.parametrize("mutation", ["queue_only", "settlement"])
def test_openbvr_watchdog_queue_and_state_interleaving(monkeypatch, already_queued, mutation):
    payload = _request_payload(queued_at=time.time() - 700, _openbvr_deferred_attempts=1)
    queue = bi_export_shared.OPENBVR_RETRY_QUEUE
    bi_export_shared.save_job(_retry_queued_job(
        payload, retry_queue=queue, last_transition_at=time.time() - 90,
    ))
    snapshot = bi_export_shared.load_job(payload["request_id"])
    raw = json.dumps(payload).encode()
    if already_queued:
        _r.rpush(queue, raw)
    pipeline_class = type(_r.pipeline())
    execute = pipeline_class.execute
    interleaved = []

    def race(pipe, *args, **kwargs):
        if not interleaved:
            interleaved.append(True)
            if already_queued:
                assert _r.lpop(queue) == raw
            elif mutation == "queue_only":
                _r.rpush(queue, raw)
            if mutation == "settlement":
                bi_exporter._process_request(raw)
        return execute(pipe, *args, **kwargs)

    monkeypatch.setattr(pipeline_class, "execute", race)
    bi_watchdog._repair_job(snapshot)
    assert interleaved
    if mutation == "queue_only":
        # A queue-only mutation must abort the job timestamp write, even though
        # the saved job itself has not changed. A later scan rechecks membership.
        assert bi_export_shared.load_job(payload["request_id"]) == snapshot
        assert _r.lrange(queue, 0, -1) == ([] if already_queued else [raw])
        bi_watchdog._run_once()
        assert _r.lrange(queue, 0, -1) == [raw]
        assert bi_export_shared.load_job(payload["request_id"])["request"] == payload
        assert _r.llen(bi_export_shared.result_key(payload["request_id"])) == 0
    else:
        assert bi_export_shared.load_job(payload["request_id"])["status"] == "failed"
        bi_watchdog._run_once()
        assert _r.llen(queue) == 0
        assert _r.llen(bi_export_shared.result_key(payload["request_id"])) == 1
    assert _r.llen(bi_export_shared.EXPORT_REQUEST_QUEUE) == 0


@pytest.mark.parametrize("saved", [False, True])
@pytest.mark.parametrize("succeeds", [False, True])
def test_defers_openbvr_retry_when_active_exports_running(monkeypatch, saved, succeeds):
    payload = _request_payload(
        clip_path="@clip/original", offset=10, duration=10000,
        trigger_filename="alert.jpg", _export_attempts=1,
    )
    if saved:
        bi_export_shared.save_job(_retry_queued_job(payload))
    request_id = payload["request_id"]
    monkeypatch.setattr(bi_exporter, "bi_get_export_queue", lambda *_args: [])
    monkeypatch.setattr(bi_exporter, "bi_lookup_alert", lambda *_args: {
        "camera": "TestCam", "file": "alert.jpg", "path": "@clip/refreshed",
        "offset": 222, "msec": 33333, "lookup_match_type": "exact_file",
        "export_source_path": "@clip/refreshed", "export_source_field": "path",
    })
    _r.sadd(bi_export_shared.ACTIVE_EXPORT_SET, "synthetic-other-export")
    posted = []
    class Session:
        def post(self, _url, json=None, **_kwargs):
            posted.append(dict(json))
            class Response:
                def json(self):
                    if len(posted) <= 2:
                        return {"result": "fail", "data": {"reason": "OpenBVR failed"}}
                    if succeeds:
                        return {"result": "success", "data": {"path": "@new", "uri": "Clipboard/new.mp4"}}
                    return {"result": "fail"}
            return Response()
    monkeypatch.setattr(bi_exporter, "get_session", lambda *_args: (Session(), "sid"))
    monkeypatch.setattr(bi_exporter.time, "sleep", lambda *_args: None)

    bi_exporter._process_request(json.dumps(payload).encode())
    stored = bi_export_shared.load_job(request_id)
    assert stored["status"] == "retry_queued"
    deferred = stored["request"]
    assert deferred["clip_path"] == "@clip/original"
    assert deferred["_openbvr_deferred_attempts"] == 1
    assert deferred["queued_at"] >= payload["queued_at"]
    assert json.loads(_r.lindex(bi_export_shared.OPENBVR_RETRY_QUEUE, 0)) == deferred
    assert _r.llen(bi_export_shared.result_key(request_id)) == 0
    assert not _r.sismember(bi_export_shared.ACTIVE_EXPORT_SET, request_id)
    # Old intake duplicates cannot replace the deferred generation or submit early.
    bi_exporter._process_request(json.dumps(payload).encode())
    assert len(posted) == 2
    assert _r.llen(bi_export_shared.OPENBVR_RETRY_QUEUE) == 1
    # A watchdog repair retains the idle-only queue when a deferred item is lost.
    _r.lpop(bi_export_shared.OPENBVR_RETRY_QUEUE)
    stored["last_transition_at"] = time.time() - 90
    bi_export_shared.save_job(stored)
    bi_watchdog._run_once()
    assert _r.llen(bi_export_shared.EXPORT_REQUEST_QUEUE) == 0
    assert json.loads(_r.lindex(bi_export_shared.OPENBVR_RETRY_QUEUE, 0)) == deferred
    _r.srem(bi_export_shared.ACTIVE_EXPORT_SET, "synthetic-other-export")
    raw = _r.lpop(bi_export_shared.OPENBVR_RETRY_QUEUE)
    bi_exporter._process_request(raw)
    stored = bi_export_shared.load_job(request_id)
    assert stored["status"] == ("submitted" if succeeds else "failed")
    assert posted[-1]["path"] == "@clip/refreshed.bvr"
    assert posted[-1]["startms"] == 222
    assert posted[-1]["msec"] == 33333
    if succeeds:
        assert stored["request"]["clip_path"] == "@clip/refreshed"
        assert stored["export_attempts"] == 2
        assert _r.sismember(bi_export_shared.ACTIVE_EXPORT_SET, request_id)
        assert _r.llen(bi_export_shared.result_key(request_id)) == 0
    else:
        assert stored["request"] == deferred
        assert _r.llen(bi_export_shared.result_key(request_id)) == 1
    bi_exporter._process_request(raw)
    assert len(posted) == 3


@pytest.mark.parametrize("active_exports", [0, 1])
def test_run_exporter_only_drains_openbvr_retry_queue_when_idle(monkeypatch, active_exports):
    calls = []

    class FakeRedis:
        def __init__(self):
            self.export_blpop_calls = 0

        def blpop(self, key, timeout=0):
            calls.append(("blpop", key, timeout))
            if key == bi_export_shared.EXPORT_REQUEST_QUEUE:
                self.export_blpop_calls += 1
                if self.export_blpop_calls == 1:
                    return None
                raise RuntimeError("stop")
            if key == bi_export_shared.OPENBVR_RETRY_QUEUE:
                return (key, b'{"request_id":"deferred"}')
            raise AssertionError(f"unexpected queue {key}")

        def scard(self, key):
            calls.append(("scard", key))
            assert key == bi_export_shared.ACTIVE_EXPORT_SET
            return active_exports

    monkeypatch.setattr(bi_exporter, "r", FakeRedis())
    monkeypatch.setattr(bi_exporter, "start_heartbeat_thread", lambda *_args, **_kwargs: None)

    def fake_process(raw):
        calls.append(("process", raw))

    monkeypatch.setattr(bi_exporter, "_process_request", fake_process)

    try:
        bi_exporter.run_exporter()
    except RuntimeError as exc:
        assert str(exc) == "stop"

    assert ("scard", bi_export_shared.ACTIVE_EXPORT_SET) in calls
    assert (("blpop", bi_export_shared.OPENBVR_RETRY_QUEUE, 1) in calls) == (active_exports == 0)
    assert (("process", b'{"request_id":"deferred"}') in calls) == (active_exports == 0)
