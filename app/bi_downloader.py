#!/usr/bin/env python3
"""
Downloader service for staged Blue Iris exports.
"""

import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler

from bi_export_shared import (
    DOWNLOAD_REQUEST_QUEUE,
    DOWNLOAD_TIMEOUT,
    MAX_RECOVERY_ATTEMPTS,
    bi_delete_clip,
    finish_job,
    get_session,
    job_tag,
    load_job,
    mark_delivery_queued,
    r,
    safe_error_summary,
    save_job,
    trigger_bi_recovery,
    queue_retry,
)


LOG_FILE = os.getenv("LOG_FILE", "/app/logs/bi_downloader.log")

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


def _download_export(job):
    tag = job_tag(job)
    sess, sid = get_session(job["bi_url"], job["bi_user"], job["bi_pass"], tag)
    if not sid:
        return False, "BI login failed"

    mp4_url = f"{job['bi_url'].rstrip('/')}/clips/{job['relative_uri']}?dl=1&session={sid}"
    downloaded = False
    dl_start = time.time()

    while time.time() - dl_start < DOWNLOAD_TIMEOUT:
        attempt_elapsed = time.time() - dl_start
        try:
            with sess.get(mp4_url, stream=True, timeout=60) as dl:
                if dl.status_code == 404:
                    if attempt_elapsed >= 50:
                        logging.error(f"{tag} Persistent 404 after {attempt_elapsed:.1f}s")
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
                    logging.info(f"{tag} Download complete elapsed={attempt_elapsed:.1f}s size={final_size}")
                    downloaded = True
                    break
                time.sleep(2)
        except Exception as exc:
            logging.warning(f"{tag} Download error: {safe_error_summary(exc)}")
            time.sleep(2)

    if not downloaded:
        bi_delete_clip(sess, job["bi_url"], sid, job["target_path"], tag)
        return False, "download failed (file not ready)"

    if job.get("delete_after", True):
        bi_delete_clip(sess, job["bi_url"], sid, job["target_path"], tag)

    return True, None


def _process_download_request(request_id):
    job = load_job(request_id)
    if not job:
        return

    job["download_attempts"] = int(job.get("download_attempts", 0)) + 1
    job["last_transition_at"] = time.time()
    save_job(job)

    ok, error_msg = _download_export(job)
    if ok:
        finish_job(job, True, None)
        if job.get("delivery_context"):
            mark_delivery_queued(load_job(job["request_id"]) or job)
        return

    tag = job_tag(job)
    if job.get("recovery_attempts", 0) < MAX_RECOVERY_ATTEMPTS and trigger_bi_recovery(
        job.get("restart_url", ""),
        job.get("restart_token", ""),
        tag,
    ):
        logging.warning(f"{tag} Download failed; retrying export after BI recovery")
        job["request"]["_recovery_attempts"] = job.get("recovery_attempts", 0) + 1
        queue_retry(job, error_msg or "download failed")
        return

    finish_job(job, False, error_msg)


def run_downloader():
    logging.info("[bi_downloader] Waiting for completed exports")
    while True:
        item = r.blpop(DOWNLOAD_REQUEST_QUEUE, timeout=5)
        if not item:
            continue
        request_id = item[1].decode() if isinstance(item[1], bytes) else item[1]
        _process_download_request(request_id)


if __name__ == "__main__":
    run_downloader()
