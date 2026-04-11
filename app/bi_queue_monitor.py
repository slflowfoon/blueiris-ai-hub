#!/usr/bin/env python3
"""
Central queue monitor for staged Blue Iris exports.
"""

import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler

from bi_export_shared import (
    ACTIVE_EXPORT_SET,
    DOWNLOAD_REQUEST_QUEUE,
    EXPORT_QUEUE_ACK_TIMEOUT,
    EXPORT_QUEUE_TIMEOUT,
    MAX_EXPORT_ATTEMPTS,
    MAX_RECOVERY_ATTEMPTS,
    QUEUE_PROGRESS_LOG_INTERVAL,
    bi_get_export_queue,
    get_session,
    job_tag,
    load_job,
    queue_poll_interval,
    r,
    safe_error_summary,
    save_job,
    trigger_bi_recovery,
    queue_retry,
    write_result,
)


LOG_FILE = os.getenv("LOG_FILE", "/app/logs/bi_queue_monitor.log")

if os.path.dirname(LOG_FILE):
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

logging.basicConfig(
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=1),
        logging.StreamHandler(sys.stdout),
    ],
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


def _poll_active_exports():
    request_ids = [rid.decode() if isinstance(rid, bytes) else rid for rid in r.smembers(ACTIVE_EXPORT_SET)]
    if not request_ids:
        time.sleep(1)
        return

    now = time.time()
    jobs = []
    nearest_poll_at = None
    for request_id in request_ids:
        job = load_job(request_id)
        if not job or job.get("status") not in {"submitted", "queued"}:
            continue
        next_poll_at = job.get("next_poll_at", 0)
        if nearest_poll_at is None or next_poll_at < nearest_poll_at:
            nearest_poll_at = next_poll_at
        if next_poll_at <= now:
            jobs.append(job)

    if not jobs:
        sleep_for = 1
        if nearest_poll_at is not None:
            sleep_for = max(0.5, min(1.0, nearest_poll_at - now))
        time.sleep(sleep_for)
        return

    jobs_by_bi = {}
    for job in jobs:
        key = (job["bi_url"], job["bi_user"])
        jobs_by_bi.setdefault(key, []).append(job)

    for (bi_url, bi_user), grouped_jobs in jobs_by_bi.items():
        tag = job_tag(grouped_jobs[0])
        sess, sid = get_session(bi_url, bi_user, grouped_jobs[0]["bi_pass"], tag)
        if not sid:
            for job in grouped_jobs:
                if (now - job["submitted_at"]) >= EXPORT_QUEUE_TIMEOUT:
                    job["status"] = "failed"
                    job["error"] = "BI login failed"
                    save_job(job)
                    r.srem(ACTIVE_EXPORT_SET, job["request_id"])
                    write_result(job["request_id"], job["output_path"], False, "BI login failed")
            continue

        try:
            active_exports = bi_get_export_queue(sess, bi_url, sid)
        except Exception as exc:
            logging.warning(f"{tag} Error polling export queue: {safe_error_summary(exc)}")
            continue

        active_paths = {item.get("path") for item in active_exports if item.get("path")}
        queue_size = len(active_exports)

        for job in grouped_jobs:
            tag = job_tag(job)
            elapsed = now - job["submitted_at"]
            if job["target_path"] in active_paths:
                if job["status"] == "submitted":
                    job["status"] = "queued"
                    job["queue_ack_at"] = now
                    job["last_transition_at"] = now
                    logging.info(f"{tag} Export {job['target_path']} acknowledged in BI queue")

                if (elapsed - job.get("last_progress_log", 0)) >= QUEUE_PROGRESS_LOG_INTERVAL:
                    logging.info(f"{tag} Export still in progress after {elapsed:.1f}s (queue size: {queue_size})")
                    job["last_progress_log"] = elapsed

                job["next_poll_at"] = now + queue_poll_interval(elapsed)
                save_job(job)
                continue

            if job["status"] == "queued":
                job["status"] = "ready"
                job["ready_at"] = now
                job["last_transition_at"] = now
                save_job(job)
                r.srem(ACTIVE_EXPORT_SET, job["request_id"])
                r.rpush(DOWNLOAD_REQUEST_QUEUE, job["request_id"])
                logging.info(f"{tag} Export {job['target_path']} left queue after {elapsed:.1f}s; queued for download")
                continue

            if job["status"] == "submitted" and elapsed >= EXPORT_QUEUE_ACK_TIMEOUT:
                if job.get("export_attempts", 1) < MAX_EXPORT_ATTEMPTS:
                    logging.warning(f"{tag} Export not acknowledged in queue; retrying submission")
                    queue_retry(job, "export not acknowledged by queue monitor")
                else:
                    job["status"] = "failed"
                    job["error"] = "export not acknowledged by queue monitor"
                    save_job(job)
                    r.srem(ACTIVE_EXPORT_SET, job["request_id"])
                    write_result(
                        job["request_id"],
                        job["output_path"],
                        False,
                        "export not acknowledged by queue monitor",
                    )
                continue

            if elapsed >= EXPORT_QUEUE_TIMEOUT:
                if job.get("recovery_attempts", 0) < MAX_RECOVERY_ATTEMPTS and trigger_bi_recovery(
                    job.get("restart_url", ""),
                    job.get("restart_token", ""),
                    tag,
                ):
                    logging.warning(f"{tag} Export queue timeout; retrying after BI recovery")
                    job["request"]["_recovery_attempts"] = job.get("recovery_attempts", 0) + 1
                    queue_retry(job, "timed out waiting for BI queue")
                else:
                    job["status"] = "failed"
                    job["error"] = "timed out waiting for BI queue"
                    save_job(job)
                    r.srem(ACTIVE_EXPORT_SET, job["request_id"])
                    write_result(job["request_id"], job["output_path"], False, "timed out waiting for BI queue")
                    logging.error(f"{tag} Export timed out waiting for BI queue")


def run_monitor():
    logging.info("[bi_queue_monitor] Monitoring staged BI export queue")
    while True:
        _poll_active_exports()


if __name__ == "__main__":
    run_monitor()
