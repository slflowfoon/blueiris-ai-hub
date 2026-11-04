"""Download/delivery ownership regressions using conftest's isolated Redis DB."""

import json
import time
from copy import deepcopy
from pathlib import Path

import pytest
import bi_export_shared as shared
import bi_downloader as downloader
import bi_watchdog as watchdog
import bi_queue_monitor as monitor
import video_delivery_worker as delivery


def job_at(tmp_path, status, delivery_status):
    path = tmp_path / "synthetic_raw.mp4"
    path.write_bytes(b"original-owned-media")
    return {
        "request_id": "synthetic-acceptance",
        "config_name": "Synthetic camera",
        "status": status,
        "delivery_status": delivery_status,
        "output_path": str(path),
        "submitted_at": time.time() - 900,
        "last_transition_at": time.time() - 900,
        "ready_at": time.time() - 900,
        "export_attempts": 3,
        "recovery_attempts": 1,
        "download_attempts": 4,
        "delivery_attempts": 1,
        "target_path": "@synthetic-export",
        "relative_uri": "synthetic.mp4",
        "bi_url": "http://invalid.test",
        "bi_user": "synthetic",
        "bi_pass": "synthetic",
        "request": {
            "request_id": "synthetic-acceptance",
            "_download_reexport_attempts": 2,
            "trigger_filename": "synthetic.jpg",
            "clip_path": "@original",
            "offset": 111,
            "duration": 22222,
            "output_path": str(path),
        },
        "delivery_context": {"config": {"telegram_token": "synthetic", "chat_id": "synthetic"}},
    }


@pytest.mark.parametrize(
    "status,delivery_status",
    [("downloaded", "processing"), ("completed", "completed"), ("failed", "processing"), ("failed", "completed")],
)
def test_duplicate_download_is_side_effect_free(tmp_path, monkeypatch, status, delivery_status):
    job = job_at(tmp_path, status, delivery_status)
    shared.save_job(job)
    # Model a retained pre-upgrade claim, including contradictory terminal pairs.
    shared.r.set(shared.delivery_claim_key(job["request_id"]), "existing-worker", ex=300)
    shared.touch_delivery_heartbeat(job["request_id"])
    before = shared.r.get(shared.job_key(job["request_id"]))
    claim = shared.r.get(shared.delivery_claim_key(job["request_id"]))
    heartbeat = shared.r.get(shared.delivery_heartbeat_key(job["request_id"]))
    calls = []

    def stale_io(attempt, *args, **kwargs):
        calls.append(True)
        return False, "download failed (file not ready)", None, None

    monkeypatch.setattr(downloader, "_download_export", stale_io)
    monkeypatch.setattr(downloader, "trigger_bi_recovery", lambda *args: pytest.fail("unexpected recovery"))
    downloader._process_download_request(job["request_id"])
    assert not calls
    assert shared.r.get(shared.job_key(job["request_id"])) == before
    assert shared.r.get(shared.delivery_claim_key(job["request_id"])) == claim
    assert shared.r.get(shared.delivery_heartbeat_key(job["request_id"])) == heartbeat
    assert shared.r.llen(shared.VIDEO_DELIVERY_QUEUE) == 0
    assert shared.r.llen(shared.EXPORT_REQUEST_QUEUE) == 0
    assert shared.r.llen(shared.result_key(job["request_id"])) == 0


@pytest.mark.parametrize(
    "status,delivery_status",
    [(s, d) for s in ["failed", "ready", "submitted", "queued", "retry_queued"] for d in ["processing", "completed"]],
)
def test_historical_contradiction_is_preserved_without_replay(tmp_path, monkeypatch, status, delivery_status):
    job = job_at(tmp_path, status, delivery_status)
    shared.save_job(job)
    before = shared.r.get(shared.job_key(job["request_id"]))
    shared.r.sadd(shared.ACTIVE_EXPORT_SET, job["request_id"])
    monkeypatch.setattr(monitor.time, "sleep", lambda *_a: None)
    monkeypatch.setattr(monitor, "get_session", lambda *a: pytest.fail("contradiction polled BI"))
    watchdog._run_once()
    monitor._poll_active_exports()
    watchdog._run_once()
    monitor._poll_active_exports()
    assert shared.r.sismember(shared.ACTIVE_EXPORT_SET, job["request_id"])
    assert shared.r.get(shared.job_key(job["request_id"])) == before
    for queue in [shared.VIDEO_DELIVERY_QUEUE, shared.EXPORT_REQUEST_QUEUE, shared.DOWNLOAD_REQUEST_QUEUE]:
        assert shared.r.llen(queue) == 0


@pytest.mark.parametrize("download_ok", [True, False])
def test_delivery_completion_wins_over_inflight_download(tmp_path, monkeypatch, download_ok):
    job = job_at(tmp_path, "ready", "pending")
    shared.save_job(job)
    winning = {}

    def late_io(attempt, *args, **kwargs):
        # Model another actor completing delivery before this I/O returns.
        current = shared.load_job(job["request_id"])
        current.update(status="completed", delivery_status="completed", output_path=job["output_path"])
        shared.save_job(current)
        shared.r.set(shared.delivery_claim_key(job["request_id"]), "winning-owner", ex=300)
        shared.touch_delivery_heartbeat(job["request_id"])
        winning["job"] = shared.r.get(shared.job_key(job["request_id"]))
        winning["heartbeat"] = shared.r.get(shared.delivery_heartbeat_key(job["request_id"]))
        Path(job["output_path"]).write_bytes(b"winning-media")
        Path(attempt["output_path"]).write_bytes(b"stale-media")
        return download_ok, None if download_ok else "download failed (file not ready)", 1.0, 2048

    monkeypatch.setattr(downloader, "_download_export", late_io)
    monkeypatch.setattr(downloader, "trigger_bi_recovery", lambda *args: pytest.fail("late recovery"))
    downloader._process_download_request(job["request_id"])
    assert shared.r.get(shared.job_key(job["request_id"])) == winning["job"]
    assert Path(job["output_path"]).read_bytes() == b"winning-media"
    assert shared.r.get(shared.delivery_claim_key(job["request_id"])) == b"winning-owner"
    assert shared.r.get(shared.delivery_heartbeat_key(job["request_id"])) == winning["heartbeat"]
    for queue in [shared.VIDEO_DELIVERY_QUEUE, shared.EXPORT_REQUEST_QUEUE, shared.result_key(job["request_id"])]:
        assert shared.r.llen(queue) == 0


def test_file_ownership_survives_export_attempt_counter_reset(tmp_path, monkeypatch):
    job = job_at(tmp_path, "ready", "pending")
    job["download_attempts"] = 0
    shared.save_job(job)
    calls = []
    winner = {}

    def interleaved_io(attempt, *args, **kwargs):
        calls.append(attempt["output_path"])
        if len(calls) == 1:
            # Re-export creates a fresh job and resets the download counter.
            replacement = dict(job, export_attempts=job["export_attempts"] + 1, download_attempts=0)
            shared.save_job(replacement)
            downloader._process_download_request(job["request_id"])
            winner["job"] = shared.r.get(shared.job_key(job["request_id"]))
            winner["path"] = shared.load_job(job["request_id"])["output_path"]
            Path(attempt["output_path"]).write_bytes(b"late-old-attempt")
        else:
            Path(attempt["output_path"]).write_bytes(b"new-export-media")
        return True, None, 1.0, 2048

    monkeypatch.setattr(downloader, "_download_export", interleaved_io)
    downloader._process_download_request(job["request_id"])
    assert len(calls) == 2 and calls[0] != calls[1]
    assert shared.r.get(shared.job_key(job["request_id"])) == winner["job"]
    assert Path(winner["path"]).read_bytes() == b"new-export-media"
    assert shared.r.llen(shared.VIDEO_DELIVERY_QUEUE) == 1
    assert shared.r.llen(shared.result_key(job["request_id"])) == 1


@pytest.mark.parametrize(
    "status,delivery_status",
    [("failed", "processing"), ("failed", "completed"), ("downloading", "pending"), ("ready", "pending")],
)
def test_old_delivery_queue_item_cannot_replay_ineligible_state(tmp_path, monkeypatch, status, delivery_status):
    job = job_at(tmp_path, status, delivery_status)
    shared.save_job(job)
    before = shared.r.get(shared.job_key(job["request_id"]))
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        raise RuntimeError("synthetic unexpected delivery attempt")

    for name in ["optimize_video_for_telegram", "replace_telegram_media", "analyze_video_gemini"]:
        monkeypatch.setattr(delivery, name, forbidden)
    delivery._process_delivery_request(job["request_id"])
    assert not calls
    assert shared.r.get(shared.job_key(job["request_id"])) == before
    assert shared.r.llen(shared.VIDEO_DELIVERY_QUEUE) == 0


def test_stale_watchdog_ready_snapshot_cannot_reopen_completed_delivery(tmp_path):
    job = job_at(tmp_path, "ready", "pending")
    shared.save_job(job)
    stale = shared.load_job(job["request_id"])
    current = dict(stale, status="completed", delivery_status="completed")
    shared.save_job(current)
    shared.r.set(shared.delivery_claim_key(job["request_id"]), "winner", ex=300)
    shared.touch_delivery_heartbeat(job["request_id"])
    before = shared.r.get(shared.job_key(job["request_id"]))
    heartbeat = shared.r.get(shared.delivery_heartbeat_key(job["request_id"]))
    watchdog._repair_job(stale)
    assert shared.r.get(shared.job_key(job["request_id"])) == before
    assert shared.r.get(shared.delivery_claim_key(job["request_id"])) == b"winner"
    assert shared.r.get(shared.delivery_heartbeat_key(job["request_id"])) == heartbeat
    assert shared.r.llen(shared.DOWNLOAD_REQUEST_QUEUE) == 0


def test_restart_recovers_downloading_with_fresh_owned_file(tmp_path, monkeypatch):
    job = job_at(tmp_path, "downloading", "pending")
    job["download_base_path"] = str(tmp_path / "base_raw.mp4")
    job["download_attempt_token"] = "synthetic-dead-process"
    shared.save_job(job)
    old_path = Path(job["output_path"])

    def download(attempt, *args, **kwargs):
        assert attempt["output_path"] != str(old_path)
        Path(attempt["output_path"]).write_bytes(b"recovered-media")
        return True, None, 1.0, 2048

    monkeypatch.setattr(downloader, "_download_export", download)
    watchdog._run_once()
    assert shared.r.lpop(shared.DOWNLOAD_REQUEST_QUEUE) == job["request_id"].encode()
    downloader._process_download_request(job["request_id"])
    current = shared.load_job(job["request_id"])
    assert current["status"] == "downloaded" and current["delivery_status"] == "queued"
    assert Path(current["output_path"]).read_bytes() == b"recovered-media"
    assert old_path.read_bytes() == b"original-owned-media"
    assert shared.r.llen(shared.VIDEO_DELIVERY_QUEUE) == 1


def _before_exec(monkeypatch, predicate, action):
    """Interleave a real Redis write after WATCH/read and before the real EXEC."""
    pipeline_type = type(shared.r.pipeline())
    original = pipeline_type.execute
    fired = []

    def execute(pipe, *args, **kwargs):
        if not fired and predicate(pipe):
            fired.append(True)
            action()
        return original(pipe, *args, **kwargs)

    monkeypatch.setattr(pipeline_type, "execute", execute)
    return fired


def _sets_job(pipe, job, status):
    return any(
        args[0] == "SET" and args[1] == shared.job_key(job["request_id"]) and json.loads(args[2])["status"] == status
        for args, _kwargs in pipe.command_stack
    )


def _set_delivery_key(job, kind):
    key = (shared.delivery_claim_key if kind == "claim" else shared.delivery_heartbeat_key)(job["request_id"])
    value = "competing-owner" if kind == "claim" else str(time.time())
    shared.r.set(key, value, ex=300)
    return key, value.encode()


@pytest.mark.parametrize("ok", [True, False])
@pytest.mark.parametrize("kind", ["claim", "heartbeat"])
def test_download_outcome_rejects_delivery_owner_appearing_during_watch(tmp_path, monkeypatch, ok, kind):
    job = job_at(tmp_path, "ready", "pending")
    shared.save_job(job)
    claimed = shared.claim_download_attempt(job["request_id"])
    before = shared.r.get(shared.job_key(job["request_id"]))
    shared.r.sadd(shared.ACTIVE_EXPORT_SET, job["request_id"])
    competing = []
    fired = _before_exec(
        monkeypatch,
        lambda pipe: _sets_job(pipe, job, "downloaded" if ok else "failed"),
        lambda: competing.append(_set_delivery_key(job, kind)),
    )
    assert shared.commit_download_outcome(claimed, ok, "synthetic error" if not ok else None) is None
    assert fired
    assert shared.r.get(shared.job_key(job["request_id"])) == before
    assert shared.r.get(competing[0][0]) == competing[0][1]
    assert shared.r.sismember(shared.ACTIVE_EXPORT_SET, job["request_id"])
    assert shared.r.llen(shared.result_key(job["request_id"])) == 0
    assert shared.r.llen(shared.VIDEO_DELIVERY_QUEUE) == 0


@pytest.mark.parametrize("retry", ["same_metadata", "refreshed_metadata", "recovery"])
@pytest.mark.parametrize("kind", ["claim", "heartbeat"])
def test_download_retry_rejects_delivery_owner_appearing_during_watch(tmp_path, monkeypatch, retry, kind):
    job = job_at(tmp_path, "ready", "pending")
    shared.save_job(job)
    claimed = shared.claim_download_attempt(job["request_id"])
    before = shared.r.get(shared.job_key(job["request_id"]))
    shared.r.sadd(shared.ACTIVE_EXPORT_SET, job["request_id"])
    competing = []
    monkeypatch.setattr(
        downloader,
        "bi_lookup_alert",
        lambda *a: {
            "export_source_path": "@refreshed",
            "offset": 123,
            "msec": 456,
        },
    )
    fired = _before_exec(
        monkeypatch,
        lambda pipe: _sets_job(pipe, job, "retry_queued"),
        lambda: competing.append(_set_delivery_key(job, kind)),
    )
    if retry == "recovery":
        candidate = deepcopy(claimed)
        candidate["request"]["_recovery_attempts"] = 1
        assert shared.queue_retry(candidate, "synthetic recovery", expected_job=claimed) is None
    else:
        outcome, _error = downloader._queue_download_reexport(
            claimed,
            "[Synthetic]",
            refresh_metadata=retry == "refreshed_metadata",
        )
        assert outcome == "stale"
    assert fired
    assert shared.r.get(shared.job_key(job["request_id"])) == before
    assert shared.r.get(competing[0][0]) == competing[0][1]
    assert shared.r.sismember(shared.ACTIVE_EXPORT_SET, job["request_id"])
    assert shared.r.llen(shared.EXPORT_REQUEST_QUEUE) == 0
    assert shared.r.llen(shared.result_key(job["request_id"])) == 0


@pytest.mark.parametrize("lookup", [None, {}, "raises"])
def test_failed_metadata_refresh_revalidates_before_recovery(tmp_path, monkeypatch, lookup):
    job = job_at(tmp_path, "ready", "pending")
    job["request"]["_download_reexport_attempts"] = 1
    job["recovery_attempts"] = 0
    shared.save_job(job)
    winner = []

    def refresh(*args):
        current = shared.load_job(job["request_id"])
        current.update(status="completed", delivery_status="completed")
        shared.save_job(current)
        winner.append(shared.r.get(shared.job_key(job["request_id"])))
        if lookup == "raises":
            raise RuntimeError("synthetic lookup failure")
        return lookup

    monkeypatch.setattr(
        downloader, "_download_export", lambda *a: (False, "download failed (file not ready)", None, None)
    )
    monkeypatch.setattr(downloader, "bi_lookup_alert", refresh)
    monkeypatch.setattr(downloader, "trigger_bi_recovery", lambda *a: pytest.fail("stale recovery"))
    downloader._process_download_request(job["request_id"])
    assert shared.r.get(shared.job_key(job["request_id"])) == winner[0]
    assert shared.r.llen(shared.EXPORT_REQUEST_QUEUE) == 0
    assert shared.r.llen(shared.result_key(job["request_id"])) == 0


@pytest.mark.parametrize("download_ok", [True, False])
def test_bi_delete_is_skipped_after_download_ownership_changes(tmp_path, monkeypatch, download_ok):
    job = job_at(tmp_path, "ready", "pending")
    shared.save_job(job)
    claimed = shared.claim_download_attempt(job["request_id"])

    def advance():
        shared.save_job(dict(claimed, status="completed", delivery_status="completed"))

    class Response:
        status_code = 200
        headers = {"Content-Length": "2048"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, size):
            advance()
            yield b"x" * 2048

    class Session:
        def get(self, *args, **kwargs):
            return Response()

    def session(*args):
        if not download_ok:
            advance()
        return Session(), "synthetic-session"

    monkeypatch.setattr(downloader, "get_session", session)
    # A zero timeout takes the failed-download cleanup branch without sleeping.
    monkeypatch.setattr(downloader, "DOWNLOAD_TIMEOUT", 120 if download_ok else 0)
    monkeypatch.setattr(downloader, "bi_delete_clip", lambda *a: pytest.fail("stale clip delete"))
    ok, *_rest = downloader._download_export(claimed)
    assert ok is download_ok


def _monitor_job(tmp_path):
    job = job_at(tmp_path, "queued", "pending")
    job["next_poll_at"] = 0
    shared.save_job(job)
    shared.r.sadd(shared.ACTIVE_EXPORT_SET, job["request_id"])
    return job


def test_monitor_ready_handoff_is_atomic(tmp_path, monkeypatch):
    job = _monitor_job(tmp_path)
    before = shared.r.get(shared.job_key(job["request_id"]))
    monkeypatch.setattr(monitor, "get_session", lambda *a: (object(), "synthetic-session"))
    monkeypatch.setattr(monitor, "bi_get_export_queue", lambda *a: [])

    def before_handoff():
        assert shared.r.get(shared.job_key(job["request_id"])) == before
        assert shared.r.sismember(shared.ACTIVE_EXPORT_SET, job["request_id"])
        assert shared.r.llen(shared.DOWNLOAD_REQUEST_QUEUE) == 0

    fired = _before_exec(monkeypatch, lambda pipe: _sets_job(pipe, job, "ready"), before_handoff)
    monitor._poll_active_exports()
    assert fired
    assert shared.load_job(job["request_id"])["status"] == "ready"
    assert not shared.r.sismember(shared.ACTIVE_EXPORT_SET, job["request_id"])
    assert shared.r.lrange(shared.DOWNLOAD_REQUEST_QUEUE, 0, -1) == [job["request_id"].encode()]


@pytest.mark.parametrize("branch", ["progress", "ready", "retry", "failure"])
def test_monitor_snapshot_race_does_not_overwrite_delivery(tmp_path, monkeypatch, branch):
    job = _monitor_job(tmp_path)
    if branch in {"retry", "failure"}:
        job["status"] = "submitted"
        job["export_attempts"] = 1 if branch == "retry" else shared.MAX_EXPORT_ATTEMPTS
        shared.save_job(job)
    winner = []

    def poll(*args):
        # Simulate progress while BI queue I/O is in flight.
        shared.save_job(dict(job, status="completed", delivery_status="completed"))
        winner.append(shared.r.get(shared.job_key(job["request_id"])))
        return [{"path": job["target_path"]}] if branch == "progress" else []

    monkeypatch.setattr(monitor, "get_session", lambda *a: (object(), "synthetic-session"))
    monkeypatch.setattr(monitor, "bi_get_export_queue", poll)
    monitor._poll_active_exports()
    assert shared.r.get(shared.job_key(job["request_id"])) == winner[0]
    for queue in (shared.EXPORT_REQUEST_QUEUE, shared.DOWNLOAD_REQUEST_QUEUE, shared.result_key(job["request_id"])):
        assert shared.r.llen(queue) == 0


def test_monitor_handoff_watch_conflict_leaves_no_download_item(tmp_path, monkeypatch):
    job = _monitor_job(tmp_path)
    monkeypatch.setattr(monitor, "get_session", lambda *a: (object(), "synthetic-session"))
    monkeypatch.setattr(monitor, "bi_get_export_queue", lambda *a: [])
    fired = _before_exec(
        monkeypatch,
        lambda pipe: _sets_job(pipe, job, "ready"),
        lambda: shared.save_job(dict(job, status="completed", delivery_status="completed")),
    )
    monitor._poll_active_exports()
    assert fired
    assert shared.load_job(job["request_id"])["status"] == "completed"
    assert shared.r.llen(shared.DOWNLOAD_REQUEST_QUEUE) == 0


@pytest.mark.parametrize("attempt", [0, 1, 2])
def test_download_reexports_keep_metadata_order_caps_and_original_ages(tmp_path, monkeypatch, attempt):
    job = job_at(tmp_path, "ready", "pending")
    job["request"]["_download_reexport_attempts"] = attempt
    job.update(submitted_at=1000, monitor_started_at=1005, queue_ack_at=1010)
    job["request"].update(
        _original_submitted_at=100,
        _original_monitor_started_at=105,
        _original_queue_ack_at=110,
    )
    shared.save_job(job)
    lookups = []

    def lookup(*args):
        lookups.append(True)
        return {"export_source_path": "@refreshed", "offset": 333, "msec": 444}

    monkeypatch.setattr(
        downloader, "_download_export", lambda *a: (False, "download failed (file not ready)", None, None)
    )
    monkeypatch.setattr(downloader, "bi_lookup_alert", lookup)
    monkeypatch.setattr(downloader, "trigger_bi_recovery", lambda *a: pytest.fail("recovery cap exceeded"))
    downloader._process_download_request(job["request_id"])
    current = shared.load_job(job["request_id"])
    assert [current[key] for key in ("submitted_at", "monitor_started_at", "queue_ack_at")] == [1000, 1005, 1010]
    if attempt == 2:
        assert current["status"] == "failed"
        assert not lookups
        assert shared.r.llen(shared.EXPORT_REQUEST_QUEUE) == 0
        assert shared.r.llen(shared.result_key(job["request_id"])) == 1
    else:
        retry = json.loads(shared.r.lpop(shared.EXPORT_REQUEST_QUEUE))
        assert retry == current["request"]
        assert [
            retry[key] for key in ("_original_submitted_at", "_original_monitor_started_at", "_original_queue_ack_at")
        ] == [100, 105, 110]
        assert retry["_previous_target_path"] == job["target_path"]
        assert retry["_export_attempts"] == job["export_attempts"]
        assert retry["_recovery_attempts"] == job["recovery_attempts"]
        assert retry["_download_reexport_attempts"] == attempt + 1
        assert retry["_refresh_before_export"] is (attempt == 1)
        assert (retry["clip_path"], retry["offset"], retry["duration"]) == (
            ("@original", 111, 22222) if attempt == 0 else ("@refreshed", 333, 444)
        )
        assert len(lookups) == attempt


def _mock_delivery_io(monkeypatch, *, media_ok=True, caption=None):
    calls = []

    def optimise(raw, optimised, *args, **kwargs):
        calls.append("optimise")
        Path(optimised).write_bytes(b"synthetic optimised media")
        return True

    def send(config, *args):
        calls.append("send")
        config["last_msg_id"] = 12345
        return media_ok

    def analyse(*args):
        calls.append("analyse")
        return caption

    monkeypatch.setattr(delivery, "optimize_video_for_telegram", optimise)
    monkeypatch.setattr(delivery, "replace_telegram_media", send)
    monkeypatch.setattr(delivery, "analyze_video_gemini", analyse)
    monkeypatch.setattr(delivery, "update_telegram_caption", lambda *a, **kw: True)
    return calls


def _sets_delivery_status(pipe, job, delivery_status):
    return any(
        args[0] == "SET"
        and args[1] == shared.job_key(job["request_id"])
        and json.loads(args[2]).get("delivery_status") == delivery_status
        for args, _kwargs in pipe.command_stack
    )


@pytest.mark.parametrize("outcome", ["completed", "retry_queued", "failed"])
def test_delivery_heartbeat_conflict_retries_commit_without_repeating_io(tmp_path, monkeypatch, outcome):
    job = job_at(tmp_path, "downloaded", "queued")
    job["delivery_attempts"] = shared.MAX_DELIVERY_ATTEMPTS - 1 if outcome == "failed" else 0
    shared.save_job(job)
    calls = _mock_delivery_io(monkeypatch, media_ok=outcome == "completed")

    def heartbeat():
        owner = shared.r.get(shared.delivery_claim_key(job["request_id"])).decode()
        assert shared.refresh_delivery_claim(job["request_id"], owner)

    fired = _before_exec(
        monkeypatch,
        lambda pipe: _sets_delivery_status(pipe, job, outcome),
        heartbeat,
    )
    delivery._process_delivery_request(job["request_id"])
    assert fired
    assert sorted(calls) == ["analyse", "optimise", "send"]
    current = shared.load_job(job["request_id"])
    assert current["delivery_status"] == outcome
    assert current["delivery_context"]["config"]["last_msg_id"] == 12345
    assert current["delivery_attempts"] == job["delivery_attempts"] + 1
    assert shared.r.get(shared.delivery_claim_key(job["request_id"])) is None
    assert shared.r.get(shared.delivery_heartbeat_key(job["request_id"])) is None
    assert shared.r.llen(shared.VIDEO_DELIVERY_QUEUE) == (1 if outcome == "retry_queued" else 0)
    assert Path(job["output_path"]).exists() is (outcome == "retry_queued")


@pytest.mark.parametrize("outcome", ["completed", "failed"])
@pytest.mark.parametrize("change", ["owner", "state"])
def test_delivery_rejected_settlement_preserves_current_state_and_files(tmp_path, monkeypatch, outcome, change):
    job = job_at(tmp_path, "downloaded", "queued")
    job["delivery_attempts"] = shared.MAX_DELIVERY_ATTEMPTS - 1
    shared.save_job(job)
    calls = _mock_delivery_io(monkeypatch, media_ok=outcome == "completed")
    winner = []

    def take_over():
        if change == "owner":
            shared.r.set(shared.delivery_claim_key(job["request_id"]), "winning-owner", ex=300)
            shared.touch_delivery_heartbeat(job["request_id"])
        else:
            current = shared.load_job(job["request_id"])
            # Same owner, changed generation: it must not be adopted as the expected snapshot.
            current["download_attempt_token"] = "new-generation"
            shared.save_job(current)
        Path(job["output_path"]).write_bytes(b"current owner's media")
        winner.append(shared.r.get(shared.job_key(job["request_id"])))

    fired = _before_exec(
        monkeypatch,
        lambda pipe: _sets_delivery_status(pipe, job, outcome),
        take_over,
    )
    delivery._process_delivery_request(job["request_id"])
    assert fired
    assert sorted(calls) == ["analyse", "optimise", "send"]
    assert shared.r.get(shared.job_key(job["request_id"])) == winner[0]
    assert Path(job["output_path"]).read_bytes() == b"current owner's media"
    assert Path(delivery._optimised_path(job["output_path"])).exists()
    if change == "owner":
        assert shared.r.get(shared.delivery_claim_key(job["request_id"])) == b"winning-owner"
        assert shared.r.get(shared.delivery_heartbeat_key(job["request_id"])) is not None
    assert shared.r.llen(shared.VIDEO_DELIVERY_QUEUE) == 0


def test_delivery_progress_after_send_cannot_overwrite_new_owner(tmp_path, monkeypatch):
    job = job_at(tmp_path, "downloaded", "queued")
    shared.save_job(job)
    _mock_delivery_io(monkeypatch)
    winner = []

    def send(config, *args):
        current = shared.load_job(job["request_id"])
        current.update(status="completed", delivery_status="completed")
        shared.save_job(current)
        shared.r.set(shared.delivery_claim_key(job["request_id"]), "new-owner", ex=300)
        shared.touch_delivery_heartbeat(job["request_id"])
        winner.append(shared.r.get(shared.job_key(job["request_id"])))
        config["last_msg_id"] = 999
        return True

    monkeypatch.setattr(delivery, "replace_telegram_media", send)
    delivery._process_delivery_request(job["request_id"])
    assert shared.r.get(shared.job_key(job["request_id"])) == winner[0]
    assert shared.r.get(shared.delivery_claim_key(job["request_id"])) == b"new-owner"
    assert Path(job["output_path"]).exists()
    assert Path(delivery._optimised_path(job["output_path"])).exists()


def test_delivery_admission_progress_race_rejects_io(tmp_path, monkeypatch):
    job = job_at(tmp_path, "downloaded", "queued")
    shared.save_job(job)
    calls = _mock_delivery_io(monkeypatch)
    fired = _before_exec(
        monkeypatch,
        lambda pipe: _sets_delivery_status(pipe, job, "processing"),
        lambda: shared.save_job(dict(job, status="completed", delivery_status="completed")),
    )
    delivery._process_delivery_request(job["request_id"])
    assert fired
    assert not calls
    assert shared.load_job(job["request_id"])["status"] == "completed"
    assert Path(job["output_path"]).exists()


@pytest.mark.parametrize("operation", ["refresh", "clear"])
def test_old_delivery_owner_cannot_touch_replaced_claim_during_watch(tmp_path, monkeypatch, operation):
    job = job_at(tmp_path, "downloaded", "processing")
    shared.save_job(job)
    assert shared.claim_delivery(job["request_id"], "old-owner")
    claim_key = shared.delivery_claim_key(job["request_id"])
    heartbeat_key = shared.delivery_heartbeat_key(job["request_id"])

    def take_over():
        shared.r.set(claim_key, "new-owner", ex=300)
        shared.r.set(heartbeat_key, "synthetic-new-heartbeat", ex=300)

    fired = _before_exec(
        monkeypatch,
        lambda pipe: any(args[0] in ("EXPIRE", "DEL") for args, _kw in pipe.command_stack),
        take_over,
    )
    if operation == "refresh":
        assert not shared.refresh_delivery_claim(job["request_id"], "old-owner")
    else:
        shared.clear_delivery_claim(job["request_id"], "old-owner")
    assert fired
    assert shared.r.get(claim_key) == b"new-owner"
    assert shared.r.get(heartbeat_key) == b"synthetic-new-heartbeat"


@pytest.mark.parametrize("ok", [True, False])
def test_repeated_terminal_download_items_do_not_grow_results_or_reset_ttl(tmp_path, monkeypatch, ok):
    job = job_at(tmp_path, "ready", "pending")
    shared.save_job(job)
    calls = []

    def download(attempt):
        calls.append(attempt["output_path"])
        Path(attempt["output_path"]).write_bytes(b"synthetic download")
        return ok, None if ok else "synthetic terminal failure", 1.0, 2048

    monkeypatch.setattr(downloader, "_download_export", download)
    downloader._process_download_request(job["request_id"])
    result_key = shared.result_key(job["request_id"])
    assert shared.r.llen(result_key) == 1
    assert 0 < shared.r.ttl(result_key) <= shared.RESULT_KEY_TTL
    shared.r.expire(result_key, 30)
    before = shared.r.get(shared.job_key(job["request_id"]))
    for _ in range(4):
        downloader._process_download_request(job["request_id"])
        watchdog._run_once()
    assert len(calls) == 1
    assert shared.r.get(shared.job_key(job["request_id"])) == before
    assert shared.r.llen(result_key) == 1
    assert 0 < shared.r.ttl(result_key) <= 30
    assert shared.r.llen(shared.VIDEO_DELIVERY_QUEUE) == (1 if ok else 0)
    assert shared.r.llen(shared.EXPORT_REQUEST_QUEUE) == 0
    # A terminal snapshot is not a new download attempt, even if read afresh.
    assert shared.commit_download_outcome(shared.load_job(job["request_id"]), ok) is None
    assert shared.r.llen(result_key) == 1


def test_delivery_does_not_adopt_fresh_snapshot_after_caption_io(tmp_path, monkeypatch):
    job = job_at(tmp_path, "downloaded", "queued")
    shared.save_job(job)
    _mock_delivery_io(monkeypatch, caption="Synthetic clip caption")
    winner = []

    def caption(*args, **kwargs):
        current = shared.load_job(job["request_id"])
        current["download_attempt_token"] = "superseding-generation"
        shared.save_job(current)
        winner.append(shared.r.get(shared.job_key(job["request_id"])))
        return True

    monkeypatch.setattr(delivery, "update_telegram_caption", caption)
    delivery._process_delivery_request(job["request_id"])
    assert len(winner) == 1
    assert shared.r.get(shared.job_key(job["request_id"])) == winner[0]
    assert shared.load_job(job["request_id"])["delivery_status"] == "processing"
    assert Path(job["output_path"]).exists()


def test_download_recovery_retry_retains_increment_and_is_bounded(tmp_path, monkeypatch):
    job = job_at(tmp_path, "ready", "pending")
    job["recovery_attempts"] = 0
    shared.save_job(job)
    recovery_calls = []
    monkeypatch.setattr(downloader, "_download_export", lambda *a: (False, "synthetic download failure", None, None))
    monkeypatch.setattr(downloader, "trigger_bi_recovery", lambda *a: recovery_calls.append(True) or True)
    downloader._process_download_request(job["request_id"])
    retry = json.loads(shared.r.lpop(shared.EXPORT_REQUEST_QUEUE))
    assert retry["_recovery_attempts"] == shared.MAX_RECOVERY_ATTEMPTS == 1
    current = shared.load_job(job["request_id"])
    assert current["recovery_attempts"] == 1
    assert retry["_original_submitted_at"] == job["submitted_at"]
    # Model exporter consumption of persisted retry metadata after a restart.
    current.update(status="ready", recovery_attempts=retry["_recovery_attempts"])
    shared.save_job(current)
    downloader._process_download_request(job["request_id"])
    assert recovery_calls == [True]
    assert shared.load_job(job["request_id"])["status"] == "failed"
    assert shared.r.llen(shared.EXPORT_REQUEST_QUEUE) == 0


@pytest.mark.parametrize("kind", ["claim", "heartbeat"])
def test_watchdog_download_restart_watch_conflict_preserves_delivery_owner(tmp_path, monkeypatch, kind):
    job = job_at(tmp_path, "downloading", "pending")
    shared.save_job(job)
    before = shared.r.get(shared.job_key(job["request_id"]))
    competing = []
    fired = _before_exec(
        monkeypatch,
        lambda pipe: _sets_job(pipe, job, "ready"),
        lambda: competing.append(_set_delivery_key(job, kind)),
    )
    watchdog._repair_job(job)
    assert fired
    assert shared.r.get(shared.job_key(job["request_id"])) == before
    assert shared.r.get(competing[0][0]) == competing[0][1]
    assert shared.r.llen(shared.DOWNLOAD_REQUEST_QUEUE) == 0


def test_watchdog_does_not_reclaim_download_before_claim_timeout(tmp_path):
    job = job_at(tmp_path, "downloading", "pending")
    job["last_transition_at"] = time.time() - shared.DOWNLOAD_CLAIM_STALE_AGE + 10
    shared.save_job(job)
    before = shared.r.get(shared.job_key(job["request_id"]))
    watchdog._run_once()
    assert shared.r.get(shared.job_key(job["request_id"])) == before
    assert shared.r.llen(shared.DOWNLOAD_REQUEST_QUEUE) == 0


def test_progressing_stream_outlives_130_seconds(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(shared.time, "time", lambda: clock[0])
    job = job_at(tmp_path, "ready", "pending")
    job["delete_after"] = False
    shared.save_job(job)

    class Response:
        status_code = 200
        headers = {"Content-Length": "12288"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def raise_for_status(self):
            pass

        def iter_content(self, _size):
            for _ in range(6):
                clock[0] += 40
                watchdog._repair_job(shared.load_job(job["request_id"]))
                yield b"x" * 2048

    class Session:
        def get(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(downloader, "get_session", lambda *_a: (Session(), "synthetic-session"))
    monkeypatch.setattr(downloader, "trigger_bi_recovery", lambda *_a: pytest.fail("healthy transfer restarted BI"))
    downloader._process_download_request(job["request_id"])
    current = shared.load_job(job["request_id"])
    assert clock[0] == 1240
    assert current["status"] == "downloaded" and current["delivery_status"] == "queued"
    assert Path(current["output_path"]).stat().st_size == 12288
    assert shared.r.llen(shared.VIDEO_DELIVERY_QUEUE) == 1
    assert shared.r.llen(shared.DOWNLOAD_REQUEST_QUEUE) == 0
    assert shared.r.llen(shared.result_key(job["request_id"])) == 1


@pytest.mark.parametrize("phase", ["login", "metadata", "recovery"])
def test_bounded_calls_renew_download_liveness(tmp_path, monkeypatch, phase):
    clock = [1000.0]
    monkeypatch.setattr(shared.time, "time", lambda: clock[0])
    job = job_at(tmp_path, "ready", "pending")
    job["recovery_attempts"] = 0
    job["request"]["_download_reexport_attempts"] = 1 if phase == "metadata" else 2
    shared.save_job(job)
    observed = []

    def bounded_call(*_args):
        clock[0] += 75 if phase == "recovery" else 50
        current = shared.load_job(job["request_id"])
        assert clock[0] - current["submitted_at"] > 130
        watchdog._repair_job(current)
        assert shared.load_job(job["request_id"])["status"] == "downloading"
        assert shared.r.llen(shared.DOWNLOAD_REQUEST_QUEUE) == 0
        observed.append(True)
        return (None, None) if phase == "login" else None

    if phase == "login":
        claimed = shared.claim_download_attempt(job["request_id"])
        clock[0] += shared.DOWNLOAD_HEARTBEAT_STALE_AGE - 20
        monkeypatch.setattr(downloader, "get_session", bounded_call)
        assert downloader._download_export(claimed)[0] is False
    else:
        def failure(_claimed):
            clock[0] += shared.DOWNLOAD_HEARTBEAT_STALE_AGE - 20
            return False, "download failed (file not ready)" if phase == "metadata" else "synthetic error", None, None

        monkeypatch.setattr(downloader, "_download_export", failure)
        monkeypatch.setattr(downloader, "bi_lookup_alert", bounded_call)
        monkeypatch.setattr(
            downloader, "trigger_bi_recovery", bounded_call if phase == "recovery" else lambda *_a: False,
        )
        downloader._process_download_request(job["request_id"])
    assert observed == [True]


def test_watchdog_heartbeat_renewal_during_reclaim_aborts_cas(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(shared.time, "time", lambda: clock[0])
    job = job_at(tmp_path, "ready", "pending")
    shared.save_job(job)
    claimed = shared.claim_download_attempt(job["request_id"])
    clock[0] += shared.DOWNLOAD_HEARTBEAT_STALE_AGE + 1
    before = shared.r.get(shared.job_key(job["request_id"]))
    fired = _before_exec(
        monkeypatch, lambda pipe: _sets_job(pipe, job, "ready"),
        lambda: shared.touch_download_heartbeat(job["request_id"], claimed["download_attempt_token"]),
    )
    watchdog._repair_job(claimed)
    assert fired
    assert shared.r.get(shared.job_key(job["request_id"])) == before
    assert shared.r.llen(shared.DOWNLOAD_REQUEST_QUEUE) == 0
    # The same attempt remains reclaimable once actual progress stops.
    clock[0] += shared.DOWNLOAD_HEARTBEAT_STALE_AGE + 1
    watchdog._repair_job(claimed)
    assert shared.load_job(job["request_id"])["status"] == "ready"
    assert shared.r.lrange(shared.DOWNLOAD_REQUEST_QUEUE, 0, -1) == [job["request_id"].encode()]


def test_old_token_heartbeat_cannot_mask_crashed_new_attempt(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(shared.time, "time", lambda: clock[0])
    job = job_at(tmp_path, "ready", "pending")
    shared.save_job(job)
    old = shared.claim_download_attempt(job["request_id"])
    clock[0] += shared.DOWNLOAD_HEARTBEAT_STALE_AGE + 1
    watchdog._repair_job(old)
    new = shared.claim_download_attempt(job["request_id"])
    assert new["download_attempt_token"] != old["download_attempt_token"]
    clock[0] += shared.DOWNLOAD_HEARTBEAT_STALE_AGE + 1
    shared.touch_download_heartbeat(job["request_id"], old["download_attempt_token"])
    watchdog._repair_job(new)
    assert shared.load_job(job["request_id"])["status"] == "ready"


def test_recovery_allowance_is_durable_before_external_call_crashes(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(shared.time, "time", lambda: clock[0])
    job = job_at(tmp_path, "ready", "pending")
    job["recovery_attempts"] = 0
    job["request"]["_recovery_attempts"] = 0
    job["next_poll_at"] = 0
    shared.save_job(job)
    calls = []

    class Crash(BaseException):
        pass

    def restart(*_args):
        current = shared.load_job(job["request_id"])
        assert current["recovery_attempts"] == current["request"]["_recovery_attempts"] == 1
        assert current["submitted_at"] == job["submitted_at"]
        calls.append(True)
        raise Crash()

    monkeypatch.setattr(downloader, "_download_export", lambda *_a: (False, "synthetic failure", None, None))
    monkeypatch.setattr(downloader, "trigger_bi_recovery", restart)
    with pytest.raises(Crash):
        downloader._process_download_request(job["request_id"])
    clock[0] += 1000
    watchdog._repair_job(shared.load_job(job["request_id"]))
    downloader._process_download_request(job["request_id"])
    assert calls == [True]
    assert shared.load_job(job["request_id"])["status"] == "failed"


def test_stale_delivery_item_overlapping_download_success_never_claims(tmp_path, monkeypatch):
    job = job_at(tmp_path, "ready", "pending")
    shared.save_job(job)
    touched = []
    pipeline_type = type(shared.r.pipeline())
    execute = pipeline_type.execute

    def record(pipe, *args, **kwargs):
        assert not any(
            command[0] in ("SET", "SETEX") and command[1] == shared.delivery_claim_key(job["request_id"])
            for command, _options in pipe.command_stack
        )
        return execute(pipe, *args, **kwargs)

    def download(claimed):
        Path(claimed["output_path"]).write_bytes(b"x" * 2048)
        return True, None, 1.0, 2048

    def stale_item():
        delivery._process_delivery_request(job["request_id"])
        assert not shared.has_delivery_claim(job["request_id"])
        touched.append(True)

    monkeypatch.setattr(pipeline_type, "execute", record)
    monkeypatch.setattr(downloader, "_download_export", download)
    fired = _before_exec(monkeypatch, lambda pipe: _sets_job(pipe, job, "downloaded"), stale_item)
    downloader._process_download_request(job["request_id"])
    assert fired and touched
    current = shared.load_job(job["request_id"])
    assert current["status"] == "downloaded" and current["delivery_status"] == "queued"
    assert Path(current["output_path"]).stat().st_size == 2048
    assert shared.r.llen(shared.VIDEO_DELIVERY_QUEUE) == 1


@pytest.mark.parametrize("operation", ["download", "delivery_claim", "recovery", "reexport"])
def test_harmless_watch_conflict_retries_same_snapshot(tmp_path, monkeypatch, operation):
    job = job_at(tmp_path, "downloaded" if operation == "delivery_claim" else "ready", "pending")
    job["recovery_attempts"] = 0
    shared.save_job(job)
    claimed = job if operation == "delivery_claim" else shared.claim_download_attempt(job["request_id"])
    key = shared.job_key(job["request_id"])
    before = shared.r.get(key)

    def identical_write():
        assert shared.r.get(key) == before
        shared.r.set(key, before)

    predicate = (
        (lambda pipe: any(args[0] == "SET" and args[1] == shared.delivery_claim_key(job["request_id"])
                          for args, _kw in pipe.command_stack))
        if operation == "delivery_claim" else
        (lambda pipe: _sets_job(pipe, job, {"download": "downloaded", "recovery": "downloading",
                                         "reexport": "retry_queued"}[operation]))
    )
    fired = _before_exec(monkeypatch, predicate, identical_write)
    if operation == "delivery_claim":
        assert shared.claim_delivery(job["request_id"], "synthetic-owner")
    elif operation == "recovery":
        reserved = shared.reserve_recovery_attempt(claimed)
        assert reserved["recovery_attempts"] == reserved["request"]["_recovery_attempts"] == 1
    elif operation == "reexport":
        assert downloader._queue_download_reexport(claimed, "[Synthetic]")[0] == "requeued"
        assert shared.r.llen(shared.EXPORT_REQUEST_QUEUE) == 1
    else:
        Path(claimed["output_path"]).write_bytes(b"x" * 2048)
        monkeypatch.setattr(downloader, "_download_export", lambda *_a: (True, None, 1.0, 2048))
        # Exercise the worker's cleanup branch as well as the commit helper.
        monkeypatch.setattr(downloader, "claim_download_attempt", lambda *_a: claimed)
        downloader._process_download_request(job["request_id"])
        current = shared.load_job(job["request_id"])
        assert current["status"] == "downloaded"
        assert Path(current["output_path"]).exists()
        assert shared.r.llen(shared.result_key(job["request_id"])) == 1
        assert shared.r.llen(shared.VIDEO_DELIVERY_QUEUE) == 1
    assert fired


@pytest.mark.parametrize("reference", ["job", "result", "none"])
def test_rejected_download_keeps_referenced_attempt_file(tmp_path, monkeypatch, reference):
    job = job_at(tmp_path, "ready", "pending")
    shared.save_job(job)
    paths = []

    def download(claimed):
        paths.append(claimed["output_path"])
        Path(claimed["output_path"]).write_bytes(b"x" * 2048)
        winner = dict(claimed, status="completed", delivery_status="completed")
        if reference != "job":
            winner["output_path"] = job["output_path"]
        shared.save_job(winner)
        if reference == "result":
            shared.write_result(job["request_id"], claimed["output_path"], True)
        return True, None, 1.0, 2048

    monkeypatch.setattr(downloader, "_download_export", download)
    downloader._process_download_request(job["request_id"])
    assert Path(paths[0]).exists() is (reference != "none")
    assert shared.r.llen(shared.VIDEO_DELIVERY_QUEUE) == 0


def test_reclaim_winning_before_clip_delete_rejects_external_io(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(shared.time, "time", lambda: clock[0])
    job = job_at(tmp_path, "ready", "pending")
    shared.save_job(job)
    claimed = shared.claim_download_attempt(job["request_id"])
    at_delete = []

    class Response:
        status_code = 200
        headers = {"Content-Length": "2048"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            # A process pause after transfer but before the external delete.
            clock[0] += shared.DOWNLOAD_HEARTBEAT_STALE_AGE + 1
            at_delete.append(True)

        def raise_for_status(self):
            pass

        def iter_content(self, _size):
            yield b"x" * 2048

    class Session:
        def get(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(downloader, "get_session", lambda *_a: (Session(), "synthetic-session"))
    monkeypatch.setattr(downloader, "bi_delete_clip", lambda *_a: pytest.fail("stale external delete"))
    fired = _before_exec(
        monkeypatch,
        lambda pipe: at_delete and any(
            args[0] == "SETEX" and args[1] == shared.download_heartbeat_key(
                job["request_id"], claimed["download_attempt_token"],
            ) for args, _kw in pipe.command_stack
        ),
        lambda: watchdog._repair_job(claimed),
    )
    assert downloader._download_export(claimed)[0] is True
    assert fired
    assert shared.load_job(job["request_id"])["status"] == "ready"
    assert shared.r.llen(shared.DOWNLOAD_REQUEST_QUEUE) == 1
    assert shared.commit_download_outcome(claimed, True) is None
    downloader._cleanup_attempt_file(claimed)
    assert Path(claimed["output_path"]).exists()


@pytest.mark.parametrize("status", ["submitted", "queued"])
def test_export_recovery_reservation_survives_watchdog_retry(tmp_path, status):
    job = job_at(tmp_path, status, "pending")
    job["export_attempts"] = 1
    job["recovery_attempts"] = 0
    shared.save_job(job)
    reserved = shared.reserve_recovery_attempt(job)
    assert reserved["recovery_attempts"] == reserved["request"]["_recovery_attempts"] == 1
    assert reserved["submitted_at"] == job["submitted_at"]
    assert shared.reserve_recovery_attempt(reserved) is None
    # Model a crash before/during the external call. Reattachment and timeout
    # repair must propagate the already-spent reservation in the next request.
    shared.r.sadd(shared.ACTIVE_EXPORT_SET, job["request_id"])
    watchdog._repair_job(reserved)
    request = json.loads(shared.r.lpop(shared.EXPORT_REQUEST_QUEUE))
    assert request["_recovery_attempts"] == shared.MAX_RECOVERY_ATTEMPTS
    assert request["_original_submitted_at"] == job["submitted_at"]
