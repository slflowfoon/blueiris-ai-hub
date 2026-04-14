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
    MAX_OPENBVR_DEFER_ATTEMPTS,
    MAX_EXPORT_ATTEMPTS,
    OPENBVR_RETRY_QUEUE,
    STALE_REQUEST_AGE,
    bi_lookup_alert,
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
from service_health import start_heartbeat_thread


LOG_FILE = os.getenv("LOG_FILE", "/app/logs/bi_exporter.log")

if os.path.dirname(LOG_FILE):
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

logger = setup_service_logger("bi_exporter", LOG_FILE)


def _defer_openbvr_retry(req, tag, clip_path):
    active_exports = r.scard(ACTIVE_EXPORT_SET)
    if active_exports <= 0:
        return False

    deferred_attempts = int(req.get("_openbvr_deferred_attempts", 0))
    if deferred_attempts >= MAX_OPENBVR_DEFER_ATTEMPTS:
        return False

    req["_openbvr_deferred_attempts"] = deferred_attempts + 1
    req["queued_at"] = time.time()
    queue_depth_before = r.llen(OPENBVR_RETRY_QUEUE)
    r.rpush(OPENBVR_RETRY_QUEUE, json.dumps(req))
    logger.warning(
        f"{tag} Deferring OpenBVR retry until BI export queue is idle | "
        f"bi_instance={bi_instance_label(req['bi_url'])} phase=export_submit_deferred "
        f"retry_reason=openbvr_failed active_exports={active_exports} "
        f"deferred_attempt={req['_openbvr_deferred_attempts']} "
        f"deferred_queue_depth={queue_depth_before + 1} clip_path={clip_path}"
    )
    return True


def _refresh_export_request_after_openbvr(req, payload, tag):
    trigger_filename = req.get("trigger_filename")
    if not trigger_filename:
        return False, "missing trigger_filename for BI alert refresh"

    try:
        result = bi_lookup_alert(
            req["bi_url"],
            req["bi_user"],
            req["bi_pass"],
            trigger_filename,
            tag,
        )
    except Exception as exc:
        logger.warning(
            f"{tag} BI alert lookup refresh failed after OpenBVR error | "
            f"bi_instance={bi_instance_label(req['bi_url'])} phase=prequeue_lookup "
            f"error_code=openbvr_lookup_refresh_failed error={safe_error_summary(exc)}"
        )
        return False, "BI alert lookup refresh failed after OpenBVR error"

    if result is None:
        logger.warning(
            f"{tag} BI alert lookup refresh found no matching alert after OpenBVR error | "
            f"bi_instance={bi_instance_label(req['bi_url'])} phase=prequeue_lookup "
            f"error_code=openbvr_lookup_refresh_failed lookup_result=refresh_not_found_after_openbvr_failure "
            f"trigger_filename={trigger_filename}"
        )
        return False, "BI alert lookup refresh found no matching alert after OpenBVR error"

    clip_path, offset, duration = result
    req["clip_path"] = clip_path
    req["offset"] = offset
    req["duration"] = duration

    refreshed_path = clip_path if clip_path.startswith("@") else f"@{clip_path}"
    if not refreshed_path.endswith(".bvr"):
        refreshed_path += ".bvr"

    payload["path"] = refreshed_path
    payload["startms"] = int(offset or 0)
    payload["msec"] = int(duration or 10000)

    logger.info(
        f"{tag} BI alert lookup refreshed after OpenBVR error | "
        f"bi_instance={bi_instance_label(req['bi_url'])} phase=prequeue_lookup "
        f"lookup_result=refreshed_after_openbvr_failure clip_path={clip_path} "
        f"offset={offset} duration={duration}"
    )
    logger.info(
        f"{tag} Retrying BI export with refreshed alert metadata | "
        f"bi_instance={bi_instance_label(req['bi_url'])} phase=export_submit_retry "
        f"retry_reason=openbvr_failed clip_path={clip_path} offset={offset} duration={duration}"
    )
    return True, None


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

    if int(req.get("_openbvr_deferred_attempts", 0)) > 0:
        refreshed, refresh_error = _refresh_export_request_after_openbvr(req, payload, tag)
        if not refreshed:
            return None, refresh_error

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
                "export_attempts": int(req.get("_export_attempts", 0)),
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

        if "OpenBVR failed" in str(res.get("data", {})):
            if _defer_openbvr_retry(req, tag, payload.get("path")):
                return None, "openbvr deferred retry queued"

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
        elif error_msg and error_msg.startswith("BI alert lookup refresh"):
            error_code = "openbvr_lookup_refresh_failed"
        elif error_msg == "openbvr deferred retry queued":
            return
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
    start_heartbeat_thread("bi_exporter")
    logger.info("Waiting for requests on bi:export:requests")
    while True:
        item = r.blpop(EXPORT_REQUEST_QUEUE, timeout=5)
        if item:
            _process_request(item[1])
            continue

        if r.scard(ACTIVE_EXPORT_SET) != 0:
            continue

        deferred_item = r.blpop(OPENBVR_RETRY_QUEUE, timeout=1)
        if deferred_item:
            _process_request(deferred_item[1])


if __name__ == "__main__":
    run_exporter()
