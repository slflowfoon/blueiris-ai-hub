#!/usr/bin/env python3
"""
Exporter service for staged Blue Iris clip processing.
"""

import json
import logging
import os
import time
from urllib.parse import urljoin

from bi_export_shared import (
    ACTIVE_EXPORT_SET,
    EXPORT_REQUEST_QUEUE,
    MAX_EXPORT_ATTEMPTS,
    STALE_REQUEST_AGE,
    bi_get_export_queue,
    bi_instance_label,
    bi_resolve_export_target,
    get_session,
    job_tag,
    log_job_event,
    log_terminal_diagnosis,
    r,
    safe_error_summary,
    save_job,
    setup_service_logger,
    write_result,
)


LOG_FILE = os.getenv("LOG_FILE", "/app/logs/bi_exporter.log")

if os.path.dirname(LOG_FILE):
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

logger = setup_service_logger("bi_exporter", LOG_FILE)


def _prepare_export(req, tag):
    sess, sid = get_session(req["bi_url"], req["bi_user"], req["bi_pass"], tag)
    if not sid:
        return None, "BI login failed"

    clip_path = req.get("clip_path")
    if not clip_path:
        return None, "missing clip_path for staged export"

    final_path = clip_path if clip_path.startswith("@") else f"@{clip_path}"
    if not final_path.endswith(".bvr"):
        final_path += ".bvr"

    export_url = urljoin(req["bi_url"].rstrip("/") + "/", "json?_export")
    payload = {
        "cmd": "export",
        "path": final_path,
        "startms": int(req.get("offset", 0) or 0),
        "msec": int(req.get("duration", 10000) or 10000),
        "format": 1,
        "audio": False,
        "session": sid,
    }

    current_queue = []
    known_paths = set()
    try:
        current_queue = bi_get_export_queue(sess, req["bi_url"], sid)
        known_paths = {item.get("path") for item in current_queue if item.get("path")}
    except Exception as exc:
        logger.warning(
            f"{tag} Failed to read export queue before enqueue | "
            f"bi_instance={bi_instance_label(req['bi_url'])} phase=export_snapshot "
            f"error_code=queue_snapshot_failed error={safe_error_summary(exc)}"
        )

    # On retry, if the original export is still in BI's queue, reattach to it
    # rather than submitting a new export and compounding the queue depth.
    previous_target = req.get("_previous_target_path")
    if previous_target and previous_target in known_paths:
        existing = next((item for item in current_queue if item.get("path") == previous_target), None)
        existing_uri = (existing.get("uri") or "").replace("\\", "/") if existing else ""
        if existing_uri:
            log_job_event(
                logging.INFO,
                f"{tag} reattaching to existing BI export",
                req,
                logger=logger,
                phase="export_reattach",
                target_path=previous_target,
            )
            now = time.time()
            job = {
                "request_id": req["request_id"],
                "alert_request_id": req.get("alert_request_id"),
                "config_name": req.get("config_name", "?"),
                "request": req,
                "bi_url": req["bi_url"],
                "bi_user": req["bi_user"],
                "bi_pass": req["bi_pass"],
                "output_path": req["output_path"],
                "target_path": previous_target,
                "relative_uri": existing_uri,
                "delete_after": req.get("delete_after", True),
                "restart_url": req.get("bi_restart_url", ""),
                "restart_token": req.get("bi_restart_token", ""),
                "delivery_context": req.get("delivery_context"),
                "delivery_status": "pending" if req.get("delivery_context") else None,
                "delivery_attempts": 0,
                "download_attempts": 0,
                "status": "queued",
                "export_attempts": int(req.get("_export_attempts", 0)) + 1,
                "recovery_attempts": int(req.get("_recovery_attempts", 0)),
                "submitted_at": now,
                "monitor_started_at": now,
                "last_transition_at": now,
                "last_progress_log": 0,
                "next_poll_at": now,
            }
            return job, None

    target_path = None
    relative_uri = None
    for export_attempt in range(2):
        er = sess.post(export_url, json=payload, timeout=10)
        res = er.json()
        if res.get("result") == "success":
            target_path, relative_uri = bi_resolve_export_target(res.get("data"), known_paths, tag)
            if not target_path or not relative_uri:
                try:
                    queue_data = bi_get_export_queue(sess, req["bi_url"], sid)
                    target_path, relative_uri = bi_resolve_export_target(queue_data, known_paths, tag)
                except Exception as exc:
                    logger.warning(
                        f"{tag} Failed to refresh export queue after enqueue | "
                        f"bi_instance={bi_instance_label(req['bi_url'])} phase=export_snapshot_refresh "
                        f"error_code=queue_refresh_failed error={safe_error_summary(exc)}"
                    )
            if target_path and relative_uri:
                break

        if "OpenBVR failed" in str(res.get("data", {})) and export_attempt == 0:
            logger.warning(
                f"{tag} BI reported OpenBVR failed. Retrying in 2s... | "
                f"bi_instance={bi_instance_label(req['bi_url'])} phase=export_submit "
                f"error_code=openbvr_failed"
            )
            time.sleep(2)
            continue

        return None, f"BI export command failed: {res.get('result')}"

    if not target_path or not relative_uri:
        return None, "missing path/uri in BI response"

    now = time.time()
    job = {
        "request_id": req["request_id"],
        "alert_request_id": req.get("alert_request_id"),
        "config_name": req.get("config_name", "?"),
        "request": req,
        "bi_url": req["bi_url"],
        "bi_user": req["bi_user"],
        "bi_pass": req["bi_pass"],
        "output_path": req["output_path"],
        "target_path": target_path,
        "relative_uri": relative_uri,
        "delete_after": req.get("delete_after", True),
        "restart_url": req.get("bi_restart_url", ""),
        "restart_token": req.get("bi_restart_token", ""),
        "delivery_context": req.get("delivery_context"),
        "delivery_status": "pending" if req.get("delivery_context") else None,
        "delivery_attempts": 0,
        "download_attempts": 0,
        "status": "submitted",
        "export_attempts": int(req.get("_export_attempts", 0)) + 1,
        "recovery_attempts": int(req.get("_recovery_attempts", 0)),
        "submitted_at": now,
        "monitor_started_at": now,
        "last_transition_at": now,
        "last_progress_log": 0,
        "next_poll_at": now,
    }
    return job, None


def _process_request(raw):
    try:
        req = json.loads(raw)
    except Exception:
        return

    request_id = req.get("request_id", "unknown")
    tag = job_tag(req)
    queued_at = req.get("queued_at", 0)
    if queued_at and (time.time() - queued_at) > STALE_REQUEST_AGE:
        log_terminal_diagnosis(
            logger,
            tag,
            req,
            "export_rejected",
            "stale_request",
            error="stale request",
        )
        write_result(request_id, req.get("output_path"), False, "stale request")
        return

    if int(req.get("_export_attempts", 0)) >= MAX_EXPORT_ATTEMPTS:
        log_terminal_diagnosis(
            logger,
            tag,
            req,
            "export_rejected",
            "retry_limit_reached",
            error="export retry limit reached",
        )
        write_result(request_id, req.get("output_path"), False, "export retry limit reached")
        return

    job, error_msg = _prepare_export(req, tag)
    if not job:
        error_code = "export_command_failed"
        if error_msg == "BI login failed":
            error_code = "bi_login_failed"
        elif error_msg == "missing clip_path for staged export":
            error_code = "missing_clip_path"
        elif error_msg == "missing path/uri in BI response":
            error_code = "missing_export_target"
        log_terminal_diagnosis(
            logger,
            tag,
            req,
            "export_submit_failed",
            error_code,
            error=error_msg,
        )
        write_result(request_id, req.get("output_path"), False, error_msg)
        return

    save_job(job)
    r.sadd(ACTIVE_EXPORT_SET, request_id)
    log_job_event(
        logging.INFO,
        f"{tag} export submitted",
        job,
        logger=logger,
        phase="export_submitted",
        target_path=job["target_path"],
        relative_uri=job["relative_uri"],
        queue="bi:export:requests",
    )


def run_exporter():
    logger.info("Waiting for requests on bi:export:requests")
    while True:
        item = r.blpop(EXPORT_REQUEST_QUEUE, timeout=5)
        if item:
            _process_request(item[1])


if __name__ == "__main__":
    run_exporter()
