#!/usr/bin/env python3
"""
Asynchronous Telegram video delivery for completed Blue Iris exports.
"""

import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler

from bi_export_shared import (
    MAX_DELIVERY_ATTEMPTS,
    VIDEO_DELIVERY_QUEUE,
    finish_delivery,
    job_tag,
    log_job_event,
    load_job,
    requeue_delivery,
    r,
    save_job,
)
from tasks import (
    analyze_video_gemini,
    deliver_video_to_telegram,
    enrich_caption_with_dvla,
    update_telegram_caption,
)


LOG_FILE = os.getenv("LOG_FILE", "/app/logs/video_delivery_worker.log")

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


def _optimised_path(raw_mp4):
    if raw_mp4.endswith("_raw.mp4"):
        return raw_mp4[:-8] + ".mp4"
    return raw_mp4 + ".optimised.mp4"


def _cleanup_paths(*paths):
    for path in paths:
        if path and os.path.exists(path):
            os.remove(path)


def _process_delivery_request(request_id):
    job = load_job(request_id)
    if not job:
        return

    delivery = job.get("delivery_context") or {}
    config = delivery.get("config") or {}
    if not config or "last_msg_id" not in config:
        finish_delivery(job, False, "missing Telegram delivery context")
        return

    tag = job_tag(job)
    raw_mp4 = job["output_path"]
    optimised_mp4 = _optimised_path(raw_mp4)

    job["delivery_attempts"] = int(job.get("delivery_attempts", 0)) + 1
    job["delivery_status"] = "processing"
    job["last_transition_at"] = time.time()
    save_job(job)
    log_job_event(logging.INFO, f"{tag} delivery processing started", job)

    if not os.path.exists(raw_mp4):
        finish_delivery(job, False, "downloaded video missing from disk")
        return

    _sent_path, media_ok = deliver_video_to_telegram(
        config,
        raw_mp4,
        optimised_mp4,
        delivery.get("still_caption", "Motion detected."),
        tag,
    )
    if not media_ok:
        if job["delivery_attempts"] < MAX_DELIVERY_ATTEMPTS:
            log_job_event(logging.WARNING, f"{tag} telegram media replace failed; retrying", job)
            requeue_delivery(job, "telegram media replace failed")
        else:
            finish_delivery(job, False, "telegram media replace failed")
            _cleanup_paths(optimised_mp4, raw_mp4)
        return

    video_caption = analyze_video_gemini(config, raw_mp4, delivery.get("prompt", "Describe the clip."))
    if video_caption:
        update_telegram_caption(
            config,
            enrich_caption_with_dvla(video_caption, config, tag),
        )

    _cleanup_paths(optimised_mp4, raw_mp4)
    finish_delivery(load_job(request_id) or job, True, None)
    log_job_event(logging.INFO, f"{tag} delivery completed", load_job(request_id) or job)


def run_video_delivery_worker():
    logging.info("[video_delivery_worker] Waiting for downloaded BI videos")
    while True:
        item = r.blpop(VIDEO_DELIVERY_QUEUE, timeout=5)
        if not item:
            continue
        request_id = item[1].decode() if isinstance(item[1], bytes) else item[1]
        _process_delivery_request(request_id)


if __name__ == "__main__":
    run_video_delivery_worker()
