#!/usr/bin/env python3
"""
Asynchronous Telegram video delivery for completed Blue Iris exports.
"""

import logging
import os
import time

from bi_export_shared import (
    MAX_DELIVERY_ATTEMPTS,
    VIDEO_DELIVERY_QUEUE,
    finish_delivery,
    job_tag,
    log_job_event,
    log_terminal_diagnosis,
    load_job,
    requeue_delivery,
    r,
    save_job,
    setup_service_logger,
)
from tasks import (
    analyze_video_gemini,
    deliver_video_to_telegram,
    enrich_caption_with_dvla,
    log_telegram_event,
    update_telegram_caption,
)


LOG_FILE = os.getenv("LOG_FILE", "/app/logs/video_delivery_worker.log")

if os.path.dirname(LOG_FILE):
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

logger = setup_service_logger("video_delivery_worker", LOG_FILE)


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
        log_terminal_diagnosis(
            logger,
            job_tag(job),
            job,
            "delivery_failed",
            "missing_delivery_context",
            error="missing Telegram delivery context",
        )
        finish_delivery(job, False, "missing Telegram delivery context")
        return

    tag = job_tag(job)
    raw_mp4 = job["output_path"]
    optimised_mp4 = _optimised_path(raw_mp4)

    job["delivery_attempts"] = int(job.get("delivery_attempts", 0)) + 1
    job["delivery_status"] = "processing"
    job["last_transition_at"] = time.time()
    save_job(job)
    log_job_event(
        logging.INFO,
        f"{tag} delivery processing started",
        job,
        logger=logger,
        phase="delivery_started",
    )

    if not os.path.exists(raw_mp4):
        log_job_event(
            logging.ERROR,
            f"{tag} delivery failed; downloaded video missing from disk",
            job,
            logger=logger,
            phase="delivery_failed",
            error_code="downloaded_video_missing",
        )
        log_terminal_diagnosis(
            logger,
            tag,
            job,
            "delivery_failed",
            "downloaded_video_missing",
            error="downloaded video missing from disk",
        )
        finish_delivery(job, False, "downloaded video missing from disk")
        return

    _sent_path, media_ok = deliver_video_to_telegram(
        config,
        raw_mp4,
        optimised_mp4,
        delivery.get("still_caption", "Motion detected."),
        tag,
        service_logger=logger,
    )
    if not media_ok:
        if job["delivery_attempts"] < MAX_DELIVERY_ATTEMPTS:
            log_job_event(
                logging.WARNING,
                f"{tag} telegram media replace failed; retrying",
                job,
                logger=logger,
                phase="delivery_retry",
                error_code="telegram_replace_failed",
            )
            requeue_delivery(job, "telegram media replace failed")
        else:
            log_job_event(
                logging.ERROR,
                f"{tag} telegram media replace failed",
                job,
                logger=logger,
                phase="delivery_failed",
                error_code="telegram_replace_failed",
            )
            log_terminal_diagnosis(
                logger,
                tag,
                job,
                "delivery_failed",
                "telegram_replace_failed",
                error="telegram media replace failed",
            )
            finish_delivery(job, False, "telegram media replace failed")
            _cleanup_paths(optimised_mp4, raw_mp4)
        return

    video_caption = analyze_video_gemini(config, raw_mp4, delivery.get("prompt", "Describe the clip."))
    if video_caption:
        log_telegram_event(
            logging.INFO,
            tag,
            "Video caption generated",
            "video_caption_generated",
            config,
            service_logger=logger,
            text=video_caption,
            caption_source="video",
            message_id=config.get("last_msg_id"),
        )
        dvla_key = (config.get("dvla_api_key") or "").strip()
        if dvla_key:
            enriched_caption = enrich_caption_with_dvla(video_caption, config, tag)
            log_telegram_event(
                logging.INFO,
                tag,
                "DVLA video-caption enrichment complete",
                "dvla_caption_enriched",
                config,
                service_logger=logger,
                text=enriched_caption,
                caption_source="dvla",
                caption_changed=(enriched_caption != video_caption),
                message_id=config.get("last_msg_id"),
            )
        else:
            enriched_caption = video_caption
            log_telegram_event(
                logging.INFO,
                tag,
                "DVLA video-caption enrichment skipped",
                "dvla_caption_skipped",
                config,
                service_logger=logger,
                caption_source="dvla",
                caption_changed=False,
                message_id=config.get("last_msg_id"),
                reason="no_api_key",
            )
        update_telegram_caption(
            config,
            enriched_caption,
            service_logger=logger,
            caption_source="video",
            previous_text=delivery.get("still_caption"),
        )
    else:
        error_code = "video_caption_unavailable"
        if not (config.get("gemini_key") or "").strip():
            error_code = "missing_gemini_key"
        log_telegram_event(
            logging.WARNING,
            tag,
            "Video caption unavailable; keeping still caption",
            "video_caption_unavailable",
            config,
            service_logger=logger,
            error_code=error_code,
            caption_source="video",
            message_id=config.get("last_msg_id"),
        )

    _cleanup_paths(optimised_mp4, raw_mp4)
    finish_delivery(load_job(request_id) or job, True, None)
    log_job_event(
        logging.INFO,
        f"{tag} delivery completed",
        load_job(request_id) or job,
        logger=logger,
        phase="delivery_completed",
        final_status="video_delivered",
    )


def run_video_delivery_worker():
    logger.info("Waiting for downloaded BI videos")
    while True:
        item = r.blpop(VIDEO_DELIVERY_QUEUE, timeout=5)
        if not item:
            continue
        request_id = item[1].decode() if isinstance(item[1], bytes) else item[1]
        _process_delivery_request(request_id)


if __name__ == "__main__":
    run_video_delivery_worker()
