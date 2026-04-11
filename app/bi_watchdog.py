#!/usr/bin/env python3
"""
Watchdog for repairing stranded BI export and delivery jobs.
"""

import json
import logging
import os
import time

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
    VIDEO_DELIVERY_QUEUE,
    WATCHDOG_INTERVAL,
    WATCHDOG_STALE_BUFFER,
    finish_delivery,
    iter_job_ids,
    job_tag,
    log_job_event,
    load_job,
    mark_delivery_queued,
    queue_retry,
    r,
    save_job,
    setup_service_logger,
    write_result,
)


LOG_FILE = os.getenv("LOG_FILE", "/app/logs/bi_watchdog.log")

if os.path.dirname(LOG_FILE):
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

logger = setup_service_logger("bi_watchdog", LOG_FILE)


def _repair_job(job):
    now = time.time()
    tag = job_tag(job)
    status = job.get("status")
    delivery_status = job.get("delivery_status")
    request_id = job["request_id"]
    submitted_age = now - float(job.get("submitted_at", now))
    transition_age = now - float(job.get("last_transition_at", job.get("updated_at", now)))

    if status in {"submitted", "queued"} and not r.sismember(ACTIVE_EXPORT_SET, request_id):
        log_job_event(logging.WARNING, f"{tag} watchdog reattaching active export", job, logger=logger)
        r.sadd(ACTIVE_EXPORT_SET, request_id)
        job["next_poll_at"] = 0
        job["last_transition_at"] = now
        save_job(job)
        return

    if status == "submitted" and submitted_age >= (EXPORT_QUEUE_ACK_TIMEOUT + WATCHDOG_STALE_BUFFER):
        if job.get("export_attempts", 1) < MAX_EXPORT_ATTEMPTS:
            log_job_event(
                logging.WARNING,
                f"{tag} watchdog retrying unacknowledged export",
                job,
                logger=logger,
                age=f"{submitted_age:.1f}s",
            )
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
            log_job_event(
                logging.WARNING,
                f"{tag} watchdog retrying stale queued export",
                job,
                logger=logger,
                age=f"{submitted_age:.1f}s",
            )
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
        log_job_event(
            logging.WARNING,
            f"{tag} watchdog requeueing stale ready download",
            job,
            logger=logger,
            age=f"{transition_age:.1f}s",
            download_queue_depth=r.llen(DOWNLOAD_REQUEST_QUEUE),
        )
        job["last_transition_at"] = now
        save_job(job)
        r.rpush(DOWNLOAD_REQUEST_QUEUE, request_id)
        return

    if status == "retry_queued" and transition_age >= RETRY_QUEUE_STALE_AGE:
        log_job_event(
            logging.WARNING,
            f"{tag} watchdog requeueing stalled export retry",
            job,
            logger=logger,
            age=f"{transition_age:.1f}s",
        )
        job["last_transition_at"] = now
        save_job(job)
        r.rpush("bi:export:requests", json.dumps(job["request"]))
        return

    if status == "downloaded":
        if job.get("delivery_context") and delivery_status in {None, "queued", "retry_queued", "processing"}:
            if delivery_status in {None, "queued", "retry_queued"} and transition_age >= DELIVERY_QUEUE_STALE_AGE:
                log_job_event(
                    logging.WARNING,
                    f"{tag} watchdog requeueing stale delivery job",
                    job,
                    logger=logger,
                    age=f"{transition_age:.1f}s",
                    delivery_queue_depth=r.llen(VIDEO_DELIVERY_QUEUE),
                )
                mark_delivery_queued(job)
                return

            if delivery_status == "processing" and transition_age >= (DELIVERY_QUEUE_STALE_AGE * 2):
                if int(job.get("delivery_attempts", 0)) < MAX_DELIVERY_ATTEMPTS:
                    log_job_event(
                        logging.WARNING,
                        f"{tag} watchdog requeueing stuck delivery processing",
                        job,
                        logger=logger,
                        age=f"{transition_age:.1f}s",
                    )
                    mark_delivery_queued(job)
                else:
                    finish_delivery(job, False, "watchdog: delivery processing stale")


def _run_once():
    for request_id in list(iter_job_ids()):
        job = load_job(request_id)
        if job:
            _repair_job(job)


def run_watchdog():
    logger.info("[bi_watchdog] Monitoring for stranded BI export and delivery jobs")
    while True:
        _run_once()
        time.sleep(WATCHDOG_INTERVAL)


if __name__ == "__main__":
    run_watchdog()
