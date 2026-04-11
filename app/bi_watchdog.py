#!/usr/bin/env python3
"""
Watchdog for repairing stranded BI export and delivery jobs.
"""

import json
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler

from bi_export_shared import (
    ACTIVE_EXPORT_SET,
    DELIVERY_QUEUE_STALE_AGE,
    DOWNLOAD_REQUEST_QUEUE,
    DOWNLOAD_TIMEOUT,
    EXPORT_QUEUE_ACK_TIMEOUT,
    EXPORT_QUEUE_TIMEOUT,
    MAX_DELIVERY_ATTEMPTS,
    MAX_EXPORT_ATTEMPTS,
    RETRY_QUEUE_STALE_AGE,
    WATCHDOG_INTERVAL,
    WATCHDOG_STALE_BUFFER,
    finish_delivery,
    iter_job_ids,
    job_tag,
    load_job,
    mark_delivery_queued,
    queue_retry,
    r,
    save_job,
    write_result,
)


LOG_FILE = os.getenv("LOG_FILE", "/app/logs/bi_watchdog.log")

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


def _repair_job(job):
    now = time.time()
    tag = job_tag(job)
    status = job.get("status")
    delivery_status = job.get("delivery_status")
    request_id = job["request_id"]
    submitted_age = now - float(job.get("submitted_at", now))
    transition_age = now - float(job.get("last_transition_at", job.get("updated_at", now)))

    if status in {"submitted", "queued"} and not r.sismember(ACTIVE_EXPORT_SET, request_id):
        logging.warning(f"{tag} Watchdog re-attaching active export tracking for {status} job")
        r.sadd(ACTIVE_EXPORT_SET, request_id)
        job["next_poll_at"] = 0
        job["last_transition_at"] = now
        save_job(job)
        return

    if status == "submitted" and submitted_age >= (EXPORT_QUEUE_ACK_TIMEOUT + WATCHDOG_STALE_BUFFER):
        if job.get("export_attempts", 1) < MAX_EXPORT_ATTEMPTS:
            logging.warning(f"{tag} Watchdog retrying unacknowledged export")
            queue_retry(job, "watchdog: export acknowledgement stale")
        else:
            job["status"] = "failed"
            job["error"] = "watchdog: export acknowledgement stale"
            job["last_transition_at"] = now
            save_job(job)
            r.srem(ACTIVE_EXPORT_SET, request_id)
            write_result(request_id, job["output_path"], False, job["error"])
        return

    if status == "queued" and submitted_age >= (EXPORT_QUEUE_TIMEOUT + WATCHDOG_STALE_BUFFER):
        if job.get("export_attempts", 1) < MAX_EXPORT_ATTEMPTS:
            logging.warning(f"{tag} Watchdog retrying stale queued export")
            queue_retry(job, "watchdog: export queue stale")
        else:
            job["status"] = "failed"
            job["error"] = "watchdog: export queue stale"
            job["last_transition_at"] = now
            save_job(job)
            r.srem(ACTIVE_EXPORT_SET, request_id)
            write_result(request_id, job["output_path"], False, job["error"])
        return

    if status == "ready" and transition_age >= (DOWNLOAD_TIMEOUT + WATCHDOG_STALE_BUFFER):
        logging.warning(f"{tag} Watchdog requeueing stale ready job for download")
        job["last_transition_at"] = now
        save_job(job)
        r.rpush(DOWNLOAD_REQUEST_QUEUE, request_id)
        return

    if status == "retry_queued" and transition_age >= RETRY_QUEUE_STALE_AGE:
        logging.warning(f"{tag} Watchdog requeueing stalled retry job")
        job["last_transition_at"] = now
        save_job(job)
        r.rpush("bi:export:requests", json.dumps(job["request"]))
        return

    if status == "downloaded":
        if job.get("delivery_context") and delivery_status in {None, "queued", "retry_queued", "processing"}:
            if delivery_status in {None, "queued", "retry_queued"} and transition_age >= DELIVERY_QUEUE_STALE_AGE:
                logging.warning(f"{tag} Watchdog requeueing stale delivery job")
                mark_delivery_queued(job)
                return

            if delivery_status == "processing" and transition_age >= (DELIVERY_QUEUE_STALE_AGE * 2):
                if int(job.get("delivery_attempts", 0)) < MAX_DELIVERY_ATTEMPTS:
                    logging.warning(f"{tag} Watchdog requeueing stuck delivery processing job")
                    mark_delivery_queued(job)
                else:
                    finish_delivery(job, False, "watchdog: delivery processing stale")


def _run_once():
    for request_id in list(iter_job_ids()):
        job = load_job(request_id)
        if job:
            _repair_job(job)


def run_watchdog():
    logging.info("[bi_watchdog] Monitoring for stranded BI export and delivery jobs")
    while True:
        _run_once()
        time.sleep(WATCHDOG_INTERVAL)


if __name__ == "__main__":
    run_watchdog()
