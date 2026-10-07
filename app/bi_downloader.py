#!/usr/bin/env python3
"""
Downloader service for staged Blue Iris exports.
"""

import json
import logging
import os
import time

from bi_export_shared import (
    EXPORT_REQUEST_QUEUE,
    VIDEO_DELIVERY_QUEUE,
    DOWNLOAD_REQUEST_QUEUE,
    DOWNLOAD_TIMEOUT,
    MAX_RECOVERY_ATTEMPTS,
    bi_lookup_alert,
    bi_delete_clip,
    finish_job,
    get_session,
    job_tag,
    log_job_event,
    log_terminal_diagnosis,
    load_job,
    mark_delivery_queued,
    r,
    safe_error_summary,
    save_job,
    setup_service_logger,
    trigger_bi_recovery,
    queue_retry,
)
from service_health import start_heartbeat_thread


LOG_FILE = os.getenv("LOG_FILE", "/app/logs/bi_downloader.log")

if os.path.dirname(LOG_FILE):
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

logger = setup_service_logger("bi_downloader", LOG_FILE)

MAX_DOWNLOAD_REEXPORT_ATTEMPTS = 2


def _download_export(job):
    tag = job_tag(job)
    sess, sid = get_session(job["bi_url"], job["bi_user"], job["bi_pass"], tag)
    if not sid:
        return False, "BI login failed", None, None

    mp4_url = f"{job['bi_url'].rstrip('/')}/clips/{job['relative_uri']}?dl=1&session={sid}"
    downloaded = False
    dl_start = time.time()
    final_size = None
    success_elapsed = None

    while time.time() - dl_start < DOWNLOAD_TIMEOUT:
        attempt_elapsed = time.time() - dl_start
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
                with open(job["output_path"], "wb") as fh:
                    for chunk in dl.iter_content(8192):
                        fh.write(chunk)

                final_size = os.path.getsize(job["output_path"])
                if final_size > 1024:
                    downloaded = True
                    success_elapsed = attempt_elapsed
                    break
                time.sleep(2)
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
        bi_delete_clip(sess, job["bi_url"], sid, job["target_path"], tag)
        return False, "download failed (file not ready)", None, None

    if job.get("delete_after", True):
        bi_delete_clip(sess, job["bi_url"], sid, job["target_path"], tag)

    return True, None, success_elapsed, final_size


def _queue_download_reexport(job, tag, refresh_metadata=False):
    retry_request = dict(job["request"])
    retry_request["queued_at"] = time.time()
    retry_request["_export_attempts"] = job.get("export_attempts", 1)
    retry_request["_recovery_attempts"] = job.get("recovery_attempts", 0)
    retry_request["_previous_target_path"] = job.get("target_path")
    retry_request["_original_submitted_at"] = retry_request.get(
        "_original_submitted_at",
        job.get("submitted_at"),
    )
    retry_request["_original_monitor_started_at"] = retry_request.get(
        "_original_monitor_started_at",
        job.get("monitor_started_at", job.get("submitted_at")),
    )
    retry_request["_original_queue_ack_at"] = retry_request.get(
        "_original_queue_ack_at",
        job.get("queue_ack_at"),
    )
    retry_request["_download_reexport_attempts"] = int(retry_request.get("_download_reexport_attempts", 0)) + 1
    retry_request["_refresh_before_export"] = bool(refresh_metadata)

    if refresh_metadata:
        trigger_filename = retry_request.get("trigger_filename")
        if not trigger_filename:
            return False, "missing trigger_filename for download metadata refresh"
        lookup = bi_lookup_alert(
            job["bi_url"],
            job["bi_user"],
            job["bi_pass"],
            trigger_filename,
            tag,
        )
        if lookup is None:
            return False, "download metadata refresh found no matching alert"
        if not lookup.get("export_source_path"):
            return False, "download metadata refresh: alert has no exportable path"
        retry_request["clip_path"] = lookup.get("export_source_path")
        retry_request["offset"] = lookup.get("offset", 0)
        retry_request["duration"] = lookup.get("msec", 10000)

    job["status"] = "retry_queued"
    job["last_error"] = "download failed (file not ready)"
    job["last_transition_at"] = time.time()
    job["request"] = retry_request
    save_job(job)
    r.rpush(EXPORT_REQUEST_QUEUE, json.dumps(retry_request))
    return True, None


def _process_download_request(request_id):
    job = load_job(request_id)
    if not job:
        return

    job["download_attempts"] = int(job.get("download_attempts", 0)) + 1
    job["last_transition_at"] = time.time()
    save_job(job)
    tag = job_tag(job)

    ok, error_msg, wait_elapsed, final_size = _download_export(job)
    if ok:
        finish_job(job, True, None)
        completed_job = load_job(job["request_id"]) or job
        log_job_event(
            logging.INFO,
            f"{tag} download complete",
            completed_job,
            logger=logger,
            phase="downloaded",
            wait_elapsed=f"{wait_elapsed:.1f}s" if wait_elapsed is not None else None,
            size=final_size,
        )
        if job.get("delivery_context"):
            queue_depth_before = r.llen(VIDEO_DELIVERY_QUEUE)
            mark_delivery_queued(completed_job)
            log_job_event(
                logging.INFO,
                f"{tag} delivery queued after download",
                load_job(job["request_id"]) or job,
                logger=logger,
                phase="delivery_queued",
                delivery_queue_depth=queue_depth_before + 1,
            )
        return

    if error_msg == "download failed (file not ready)":
        reexport_attempts = int(job.get("request", {}).get("_download_reexport_attempts", 0))
        if reexport_attempts < MAX_DOWNLOAD_REEXPORT_ATTEMPTS:
            refresh_metadata = reexport_attempts >= 1
            try:
                requeued, requeue_error = _queue_download_reexport(job, tag, refresh_metadata=refresh_metadata)
            except Exception as exc:
                requeued, requeue_error = False, safe_error_summary(exc)
            if requeued:
                log_job_event(
                    logging.WARNING,
                    f"{tag} download unreadable; re-requesting BI export",
                    load_job(job["request_id"]) or job,
                    logger=logger,
                    phase="download_retry",
                    error=error_msg,
                    error_code="download_not_ready",
                    retry_reason="same_metadata" if not refresh_metadata else "refreshed_metadata",
                )
                return
            error_msg = requeue_error or error_msg

    if job.get("recovery_attempts", 0) < MAX_RECOVERY_ATTEMPTS and trigger_bi_recovery(
        job.get("restart_url", ""),
        job.get("restart_token", ""),
        tag,
    ):
        log_job_event(
            logging.WARNING,
            f"{tag} download failed; retrying export after recovery",
            job,
            logger=logger,
            phase="download_retry",
            error=error_msg,
            error_code="download_not_ready",
        )
        job["request"]["_recovery_attempts"] = job.get("recovery_attempts", 0) + 1
        queue_retry(job, error_msg or "download failed")
        return

    log_job_event(
        logging.ERROR,
        f"{tag} download failed",
        job,
        logger=logger,
        phase="download_failed",
        error=error_msg,
        error_code="download_not_ready",
    )
    log_terminal_diagnosis(
        logger,
        tag,
        job,
        "download_failed",
        "download_not_ready",
        error=error_msg,
    )
    finish_job(job, False, error_msg)


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
