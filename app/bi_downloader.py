#!/usr/bin/env python3
"""
Downloader service for staged Blue Iris exports.
"""

import io
import json
import logging
import os
import time

import urllib3

from bi_export_shared import (
    DOWNLOAD_HEARTBEAT_INTERVAL,
    EXPORT_REQUEST_QUEUE,
    VIDEO_DELIVERY_QUEUE,
    DOWNLOAD_REQUEST_QUEUE,
    DOWNLOAD_TIMEOUT,
    MAX_RECOVERY_ATTEMPTS,
    bi_lookup_alert,
    bi_delete_clip,
    claim_download_attempt,
    commit_download_outcome,
    get_session,
    has_delivery_claim,
    is_download_claim_current,
    job_tag,
    log_job_event,
    log_terminal_diagnosis,
    load_job,
    r,
    replace_export_job,
    reserve_recovery_attempt,
    result_key,
    safe_error_summary,
    setup_service_logger,
    touch_download_heartbeat,
    trigger_bi_recovery,
    queue_retry,
)
from service_health import start_heartbeat_thread


LOG_FILE = os.getenv("LOG_FILE", "/app/logs/bi_downloader.log")

if os.path.dirname(LOG_FILE):
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

logger = setup_service_logger("bi_downloader", LOG_FILE)

MAX_DOWNLOAD_REEXPORT_ATTEMPTS = 2

# Matches the prior iter_content chunk size; read1 never blocks to fill it, so
# this only bounds one transport read's maximum size, not its latency.
_DOWNLOAD_READ_CHUNK_SIZE = 8192


class _StaleDownloadAttempt(Exception):
    """Stop decoder input as soon as a transport heartbeat loses ownership."""


class _TransportProgressReader(io.RawIOBase):
    """Feeds a shadow, decode-only HTTPResponse from the real transport.

    Each read pulls one raw, undecoded chunk directly off the wire via the real
    response's read1(decode_content=False), which returns as soon as any bytes
    arrive instead of blocking to fill a chunk or to let a decoder produce
    output. `renew` is called for every non-empty transport read -- including
    ones the decoder consumes without yet emitting anything -- so heartbeat
    renewal tracks actual wire progress, not decoded output.
    """

    def __init__(self, raw, renew):
        super().__init__()
        self._raw = raw
        self._renew = renew

    def readable(self):
        return True

    def read(self, size=-1):
        amt = size if size and size > 0 else _DOWNLOAD_READ_CHUNK_SIZE
        chunk = self._raw.read1(amt, decode_content=False)
        if chunk and not self._renew():
            raise _StaleDownloadAttempt()
        return chunk

    # HTTPResponse.read1 needs the body to expose read1 as well as read.
    def read1(self, size=-1):
        return self.read(size)


def _touch_heartbeat(job):
    """Renew this attempt's liveness signal from real progress or right before a
    bounded external call -- never on an unconditional timer, so a genuinely
    hung transfer's heartbeat goes stale and stays reclaimable."""
    return touch_download_heartbeat(
        job["request_id"], job.get("download_attempt_token"), expected=job,
    )


def _download_export(job):
    tag = job_tag(job)
    if not is_download_claim_current(job):
        return False, "stale download attempt", None, None
    if not _touch_heartbeat(job):
        return False, "stale download attempt", None, None
    sess, sid = get_session(job["bi_url"], job["bi_user"], job["bi_pass"], tag)
    if not sid:
        return False, "BI login failed", None, None

    mp4_url = f"{job['bi_url'].rstrip('/')}/clips/{job['relative_uri']}?dl=1&session={sid}"
    downloaded = False
    dl_start = time.time()
    final_size = None
    success_elapsed = None

    while time.time() - dl_start < DOWNLOAD_TIMEOUT:
        if not is_download_claim_current(job):
            return False, "stale download attempt", None, None
        attempt_elapsed = time.time() - dl_start
        if not _touch_heartbeat(job):
            return False, "stale download attempt", None, None
        try:
            with sess.get(mp4_url, stream=True, timeout=60) as dl:
                if dl.status_code == 404:
                    if attempt_elapsed >= 50:
                        log_job_event(
                            logging.ERROR,
                            f"{tag} persistent 404 while waiting for download",
                            job,
                            logger=logger,
                            phase="download_wait",
                            error_code="persistent_404",
                            elapsed=f"{attempt_elapsed:.1f}s",
                        )
                        break
                    time.sleep(2)
                    continue

                cl = int(dl.headers.get("Content-Length", "0") or "0")
                if dl.status_code == 503 or (dl.status_code == 200 and cl < 1000):
                    time.sleep(2)
                    continue

                dl.raise_for_status()
                if not _touch_heartbeat(job):
                    return False, "stale download attempt", None, None
                last_heartbeat = time.time()

                def _renew():
                    nonlocal last_heartbeat
                    now_chunk = time.time()
                    if now_chunk - last_heartbeat < DOWNLOAD_HEARTBEAT_INTERVAL:
                        return True
                    if not _touch_heartbeat(job):
                        return False
                    last_heartbeat = now_chunk
                    return True

                # Decode through a second, synthetic HTTPResponse fed only the
                # real transport's undecoded bytes (read1(decode_content=False)).
                # This keeps heartbeat renewal tied to real wire progress even
                # while the decoder itself is still buffering input (e.g. a long
                # gzip header) with no decoded output yet to return -- renewing
                # via decode_content=True's own read1 would only check ownership
                # once decoded output is ready, which a slow/stalled decode input
                # could delay well past the heartbeat staleness window.
                content_encoding = dl.raw.headers.get("Content-Encoding")
                decode_headers = urllib3.HTTPHeaderDict()
                if content_encoding:
                    decode_headers["Content-Encoding"] = content_encoding
                decoding_response = urllib3.HTTPResponse(
                    body=_TransportProgressReader(dl.raw, _renew),
                    headers=decode_headers,
                    preload_content=False,
                    decode_content=True,
                )
                with decoding_response, open(job["output_path"], "wb") as fh:
                    while True:
                        chunk = decoding_response.read1(_DOWNLOAD_READ_CHUNK_SIZE, decode_content=True)
                        if not chunk:
                            break
                        fh.write(chunk)

                final_size = os.path.getsize(job["output_path"])
                if final_size > 1024:
                    downloaded = True
                    success_elapsed = attempt_elapsed
                    break
                time.sleep(2)
        except _StaleDownloadAttempt:
            return False, "stale download attempt", None, None
        except Exception as exc:
            log_job_event(
                logging.WARNING,
                f"{tag} download attempt failed",
                job,
                logger=logger,
                phase="download_wait",
                elapsed=f"{attempt_elapsed:.1f}s",
                error_code="download_attempt_failed",
                error=safe_error_summary(exc),
            )
            time.sleep(2)

    if not downloaded:
        if _touch_heartbeat(job):
            bi_delete_clip(sess, job["bi_url"], sid, job["target_path"], tag)
        return False, "download failed (file not ready)", None, None

    if job.get("delete_after", True) and _touch_heartbeat(job):
        bi_delete_clip(sess, job["bi_url"], sid, job["target_path"], tag)

    return True, None, success_elapsed, final_size


# These transitions retain output_path, but cannot deliver that attempt. A new
# claim always mints a different token/path; other references still prevent removal.
_CLEANUP_SAME_TOKEN_EXCEPTION_STATUSES = frozenset({"failed", "retry_queued", "ready"})

# Absent/pending: no delivery has been queued/claimed against this record yet.
_CLEANUP_SAFE_DELIVERY_STATUSES = (None, "pending")


def _cleanup_attempt_file(claimed):
    """Discard only this token's own file, and only once the saved record proves
    it is unambiguously non-deliverable.

    Unique attempt paths are never reused. Only a same-token non-deliverable
    record without delivery ownership can disregard its copied output_path.
    Base/request/result references always retain the file. An older token's
    unreferenced path can still be removed after a newer attempt has claimed
    a different file. Unknown records retain media conservatively.
    """
    path = claimed.get("output_path")
    token = claimed.get("download_attempt_token")
    if not path or not token or f".dl-{token}" not in path:
        return
    request_id = claimed["request_id"]
    try:
        current = load_job(request_id)
    except (ValueError, TypeError):
        return
    if not isinstance(current, dict) or current.get("status") not in {
        "submitted", "queued", "retry_queued", "ready", "downloading", "downloaded", "completed", "failed",
    }:
        return

    same_token_exception = (
        current.get("download_attempt_token") == token
        and current.get("status") in _CLEANUP_SAME_TOKEN_EXCEPTION_STATUSES
        and current.get("delivery_status") in _CLEANUP_SAFE_DELIVERY_STATUSES
        and not has_delivery_claim(request_id)
    )
    if not same_token_exception and current.get("output_path") == path:
        return
    if current.get("download_base_path") == path:
        return
    request = current.get("request") or {}
    if not isinstance(request, dict) or request.get("output_path") == path:
        return
    for raw in r.lrange(result_key(request_id), 0, -1):
        try:
            entry = json.loads(raw)
            matches = entry.get("path") == path
        except (ValueError, TypeError, AttributeError):
            return
        if matches:
            return
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _queue_download_reexport(claimed, tag, refresh_metadata=False):
    """Requeue a claimed download attempt as a fresh export, iff the claim is
    still current. Returns ("requeued"|"stale"|"error", message_or_none)."""
    if not is_download_claim_current(claimed):
        return "stale", None
    retry_request = dict(claimed["request"])
    retry_request["queued_at"] = time.time()
    retry_request["_export_attempts"] = claimed.get("export_attempts", 1)
    retry_request["_recovery_attempts"] = claimed.get("recovery_attempts", 0)
    retry_request["_previous_target_path"] = claimed.get("target_path")
    retry_request["_original_submitted_at"] = retry_request.get(
        "_original_submitted_at",
        claimed.get("submitted_at"),
    )
    retry_request["_original_monitor_started_at"] = retry_request.get(
        "_original_monitor_started_at",
        claimed.get("monitor_started_at", claimed.get("submitted_at")),
    )
    retry_request["_original_queue_ack_at"] = retry_request.get(
        "_original_queue_ack_at",
        claimed.get("queue_ack_at"),
    )
    retry_request["_download_reexport_attempts"] = int(retry_request.get("_download_reexport_attempts", 0)) + 1
    retry_request["_refresh_before_export"] = bool(refresh_metadata)

    if refresh_metadata:
        if not is_download_claim_current(claimed):
            return "stale", None
        trigger_filename = retry_request.get("trigger_filename")
        if not trigger_filename:
            return "error", "missing trigger_filename for download metadata refresh"
        if not _touch_heartbeat(claimed):
            return "stale", None
        lookup = bi_lookup_alert(
            claimed["bi_url"],
            claimed["bi_user"],
            claimed["bi_pass"],
            trigger_filename,
            tag,
        )
        if lookup is None:
            return "error", "download metadata refresh found no matching alert"
        if not lookup.get("export_source_path"):
            return "error", "download metadata refresh: alert has no exportable path"
        retry_request["clip_path"] = lookup.get("export_source_path")
        retry_request["offset"] = lookup.get("offset", 0)
        retry_request["duration"] = lookup.get("msec", 10000)

    updated = dict(claimed)
    updated["status"] = "retry_queued"
    updated["last_error"] = "download failed (file not ready)"
    updated["last_transition_at"] = time.time()
    updated["request"] = retry_request
    if has_delivery_claim(claimed["request_id"]):
        return "stale", None
    if replace_export_job(claimed, updated, request_queue=EXPORT_REQUEST_QUEUE) is None:
        return "stale", None
    return "requeued", None


def _process_download_request(request_id):
    claimed = claim_download_attempt(request_id)
    if claimed is None:
        job = load_job(request_id)
        tag = job_tag(job) if job else f"[?][{request_id[:8]}]"
        log_job_event(
            logging.INFO,
            f"{tag} stale or duplicate download item skipped",
            job,
            logger=logger,
            phase="download_stale_skip",
            error_code="download_stale_duplicate",
        )
        return

    tag = job_tag(claimed)
    ok, error_msg, wait_elapsed, final_size = _download_export(claimed)

    if ok:
        updated = commit_download_outcome(claimed, True, None)
        if updated is None:
            log_job_event(
                logging.WARNING,
                f"{tag} download succeeded but claim is stale; retaining referenced media",
                claimed,
                logger=logger,
                phase="download_stale_discard",
                error_code="download_stale_duplicate",
            )
            _cleanup_attempt_file(claimed)
            return
        log_job_event(
            logging.INFO,
            f"{tag} download complete",
            updated,
            logger=logger,
            phase="downloaded",
            wait_elapsed=f"{wait_elapsed:.1f}s" if wait_elapsed is not None else None,
            size=final_size,
        )
        if updated.get("delivery_status") == "queued":
            log_job_event(
                logging.INFO,
                f"{tag} delivery queued after download",
                updated,
                logger=logger,
                phase="delivery_queued",
                delivery_queue_depth=r.llen(VIDEO_DELIVERY_QUEUE),
            )
        return

    if not is_download_claim_current(claimed):
        log_job_event(
            logging.INFO,
            f"{tag} stale download claim superseded while download was in flight",
            claimed,
            logger=logger,
            phase="download_stale_discard",
            error_code="download_stale_duplicate",
        )
        _cleanup_attempt_file(claimed)
        return

    if error_msg == "download failed (file not ready)":
        reexport_attempts = int(claimed.get("request", {}).get("_download_reexport_attempts", 0))
        if reexport_attempts < MAX_DOWNLOAD_REEXPORT_ATTEMPTS:
            refresh_metadata = reexport_attempts >= 1
            try:
                outcome, requeue_error = _queue_download_reexport(claimed, tag, refresh_metadata=refresh_metadata)
            except Exception as exc:
                outcome, requeue_error = "error", safe_error_summary(exc)
            if outcome == "requeued":
                log_job_event(
                    logging.WARNING,
                    f"{tag} download unreadable; re-requesting BI export",
                    load_job(claimed["request_id"]) or claimed,
                    logger=logger,
                    phase="download_retry",
                    error=error_msg,
                    error_code="download_not_ready",
                    retry_reason="same_metadata" if not refresh_metadata else "refreshed_metadata",
                )
                _cleanup_attempt_file(claimed)
                return
            if outcome == "stale":
                log_job_event(
                    logging.INFO,
                    f"{tag} stale download claim superseded during re-export requeue",
                    claimed,
                    logger=logger,
                    phase="download_stale_discard",
                    error_code="download_stale_duplicate",
                )
                _cleanup_attempt_file(claimed)
                return
            error_msg = requeue_error or error_msg

    if claimed.get("recovery_attempts", 0) < MAX_RECOVERY_ATTEMPTS and is_download_claim_current(claimed):
        # Persist the recovery-attempt reservation BEFORE the external BI restart
        # call: a crash or watchdog reclaim mid-call must not be able to repeat
        # the restart past MAX_RECOVERY_ATTEMPTS, which a local-only increment
        # (lost on crash) cannot guarantee.
        reserved = reserve_recovery_attempt(claimed)
        if reserved is not None:
            claimed = reserved
            if trigger_bi_recovery(
                claimed.get("restart_url", ""),
                claimed.get("restart_token", ""),
                tag,
            ):
                candidate = dict(claimed)
                candidate["request"] = dict(claimed["request"])
                candidate["request"]["_recovery_attempts"] = claimed["recovery_attempts"]
                if has_delivery_claim(claimed["request_id"]):
                    result = None
                else:
                    result = queue_retry(candidate, error_msg or "download failed", expected_job=claimed)
                _cleanup_attempt_file(claimed)
                if result is None:
                    log_job_event(
                        logging.INFO,
                        f"{tag} stale download claim superseded during recovery requeue",
                        claimed,
                        logger=logger,
                        phase="download_stale_discard",
                        error_code="download_stale_duplicate",
                    )
                    return
                log_job_event(
                    logging.WARNING,
                    f"{tag} download failed; retrying export after recovery",
                    result,
                    logger=logger,
                    phase="download_retry",
                    error=error_msg,
                    error_code="download_not_ready",
                )
                return
            # The restart call itself failed; the already-persisted reservation
            # stays spent (not refunded) and `claimed` now reflects it, so the
            # terminal commit below is consistent with what is in Redis.

    updated = commit_download_outcome(claimed, False, error_msg)
    _cleanup_attempt_file(claimed)
    if updated is None:
        log_job_event(
            logging.INFO,
            f"{tag} stale download claim superseded before terminal failure commit",
            claimed,
            logger=logger,
            phase="download_stale_discard",
            error_code="download_stale_duplicate",
        )
        return

    log_job_event(
        logging.ERROR,
        f"{tag} download failed",
        updated,
        logger=logger,
        phase="download_failed",
        error=error_msg,
        error_code="download_not_ready",
    )
    log_terminal_diagnosis(
        logger,
        tag,
        updated,
        "download_failed",
        "download_not_ready",
        error=error_msg,
    )


def run_downloader():
    start_heartbeat_thread("bi_downloader")
    logger.info("Waiting for completed exports")
    while True:
        item = r.blpop(DOWNLOAD_REQUEST_QUEUE, timeout=5)
        if not item:
            continue
        request_id = item[1].decode() if isinstance(item[1], bytes) else item[1]
        _process_download_request(request_id)


if __name__ == "__main__":
    run_downloader()
