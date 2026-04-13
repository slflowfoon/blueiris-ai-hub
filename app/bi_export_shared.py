#!/usr/bin/env python3
"""
Shared helpers for the staged Blue Iris export pipeline.
"""

import hashlib
import json
import logging
import os
import socket
import time
from urllib.parse import urljoin, urlparse
from logging.handlers import RotatingFileHandler

import redis
import requests
from db_utils import connect as sqlite_connect


REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
DATA_DIR = os.getenv("DATA_DIR", "/app/data")
DB_FILE = os.path.join(DATA_DIR, "configs.db")

EXPORT_REQUEST_QUEUE     = "bi:export:requests"
DOWNLOAD_REQUEST_QUEUE   = "bi:download:requests"
VIDEO_DELIVERY_QUEUE     = "bi:delivery:requests"
ACTIVE_EXPORT_SET        = "bi:exports:active"
JOB_KEY_PREFIX           = "bi:job:"
RESULT_KEY_PREFIX        = "bi:result:"
RESULT_KEY_TTL           = 300
STALE_REQUEST_AGE        = 600
EXPORT_QUEUE_TIMEOUT     = 180
EXPORT_QUEUE_ACK_TIMEOUT = 20
DOWNLOAD_TIMEOUT         = 60
RECOVERY_PAUSE           = 15
QUEUE_PROGRESS_LOG_INTERVAL = 15
MAX_EXPORT_ATTEMPTS      = 2
MAX_RECOVERY_ATTEMPTS    = 1
MAX_DELIVERY_ATTEMPTS    = 3
SESSION_KEY_PREFIX       = "bi:session:"
SESSION_TTL              = 3600
WATCHDOG_INTERVAL        = 15
WATCHDOG_STALE_BUFFER    = 10
RETRY_QUEUE_STALE_AGE    = 30
DELIVERY_QUEUE_STALE_AGE = 45

r = redis.from_url(REDIS_URL)
_session_cache = {}
_bi_instance_logging_cache = {"checked_at": 0.0, "enabled": True}
INSTANCE_ID = os.getenv("HOSTNAME") or socket.gethostname()


def setup_service_logger(name, log_file):
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    logger.propagate = False
    if os.path.dirname(log_file):
        os.makedirs(os.path.dirname(log_file), exist_ok=True)

    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    file_handler = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=1)
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger


def safe_error_summary(exc):
    """Summarise request-related failures without logging raw exception text."""
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None):
        return f"{type(exc).__name__} status={response.status_code}"
    return type(exc).__name__


def _bi_protocol_hash(value: str) -> str:
    """
    Blue Iris requires an MD5 digest of `user:session:password` for JSON API
    login. This is protocol interoperability, not password storage.
    """
    # Blue Iris mandates MD5 here; changing the algorithm would break auth.
    return hashlib.md5(value.encode("utf-8"), usedforsecurity=False).hexdigest()


def job_key(request_id):
    return f"{JOB_KEY_PREFIX}{request_id}"


def result_key(request_id):
    return f"{RESULT_KEY_PREFIX}{request_id}"


def job_tag(job):
    correlation_id = job.get("alert_request_id") or job.get("request_id") or "unknown"
    return f"[{job.get('config_name', '?')}][{correlation_id[:8]}]"


def bi_instance_label(bi_url):
    parsed = urlparse((bi_url or "").strip())
    return parsed.netloc or parsed.path or "unknown"


def should_log_bi_instance():
    now = time.time()
    cached = _bi_instance_logging_cache
    if (now - cached["checked_at"]) < 60:
        return cached["enabled"]

    enabled = True
    try:
        with sqlite_connect(DB_FILE) as conn:
            count = conn.execute(
                """
                SELECT COUNT(DISTINCT TRIM(bi_url))
                FROM configs
                WHERE bi_url IS NOT NULL AND TRIM(bi_url) <> ''
                """
            ).fetchone()[0]
            enabled = count > 1
    except Exception:
        # Fall back to logging the BI instance when config inspection is unavailable.
        enabled = True

    cached["checked_at"] = now
    cached["enabled"] = enabled
    return enabled


def _job_log_fields(job=None, **extra):
    fields = {}
    fields.update({
        "instance": INSTANCE_ID,
        "pid": os.getpid(),
    })
    if job:
        fields.update({
            "camera": job.get("config_name", "?"),
            "alert_id": (job.get("alert_request_id") or job.get("request_id") or "unknown")[:8],
            "job_id": (job.get("request_id") or "unknown")[:8],
            "state": job.get("status", ""),
            "delivery_state": job.get("delivery_status", ""),
            "export_attempt": job.get("export_attempts", 0),
            "recovery_attempt": job.get("recovery_attempts", 0),
            "download_attempt": job.get("download_attempts", 0),
            "delivery_attempt": job.get("delivery_attempts", 0),
        })
        if job.get("bi_url") and should_log_bi_instance():
            fields["bi_instance"] = bi_instance_label(job["bi_url"])
    fields.update({k: v for k, v in extra.items() if v is not None and v != ""})
    return fields


def format_log_fields(job=None, **extra):
    fields = _job_log_fields(job, **extra)
    ordered = []
    for key in sorted(fields):
        value = fields[key]
        ordered.append(f"{key}={value}")
    return " ".join(ordered)


def log_job_event(level, message, job=None, logger=None, **extra):
    logger = logger or logging.getLogger()
    line = message
    suffix = format_log_fields(job, **extra)
    if suffix:
        line = f"{line} | {suffix}"
    logger.log(level, line)


def recommended_action_for(error_code):
    actions = {
        "active_export_missing": "check_queue_monitor_and_redis_active_export_membership",
        "alert_not_found": "check_bi_alertlist_retention_and_trigger_filename_mapping",
        "bi_credentials_missing": "check_blue_iris_credentials_in_camera_config",
        "bi_login_failed": "check_bi_url_credentials_and_session_reuse",
        "delivery_processing_stale": "check_video_delivery_worker_health_and_ffmpeg_runtime",
        "delivery_queue_stale": "check_video_delivery_worker_queue_consumption_and_tmp_images_sharing",
        "download_attempt_failed": "check_bi_clip_endpoint_and_network_stability",
        "download_not_ready": "check_bi_export_completion_and_clip_readiness",
        "download_stale": "check_bi_downloader_health_and_download_queue_backlog",
        "downloaded_video_missing": "check_tmp_images_volume_sharing_and_cleanup_timing",
        "export_command_failed": "check_bi_export_api_response_and_clip_source_path",
        "lookup_failed": "check_bi_alertlist_access_and_shared_session_reuse",
        "missing_delivery_context": "check_telegram_message_context_persistence_before_queueing_export",
        "missing_export_target": "check_bi_export_queue_response_and_target_resolution",
        "missing_clip_path": "check_prequeue_lookup_and_bvr_clip_fallback",
        "offset_unparseable": "check_alert_filename_format_and_offset_parser",
        "openbvr_failed": "check_blue_iris_clip_integrity_and_source_bvr_path",
        "persistent_404": "check_bi_clip_endpoint_visibility_after_export_completion",
        "queue_ack_timeout": "check_bi_export_queue_acknowledgement_and_export_submission",
        "queue_poll_failed": "check_bi_queue_monitor_connectivity_and_session_validity",
        "queue_refresh_failed": "check_bi_export_queue_refresh_after_submit",
        "queue_snapshot_failed": "check_bi_export_queue_snapshot_before_submit",
        "queue_timeout": "check_bi_export_queue_progress_and_encoder_health",
        "retry_queue_stale": "check_export_retry_requeue_and_exporter_consumer_health",
        "stale_request": "check_queue_latency_and_alert_enqueue_timing",
        "task_exception": "check_system_log_for_preceding_task_failure_details",
        "telegram_replace_failed": "check_telegram_media_replace_api_and_message_permissions",
        "retry_limit_reached": "check_repeated_export_failures_and_prior_error_codes",
        "video_export_unavailable": "check_prequeue_lookup_result_and_export_request_enqueue",
    }
    return actions.get(error_code)


def log_terminal_diagnosis(logger, tag, job, phase, error_code, final_status="video_not_delivered", **extra):
    log_job_event(
        logging.ERROR,
        f"{tag} terminal diagnosis",
        job,
        logger=logger,
        phase=phase,
        error_code=error_code,
        final_status=final_status,
        recommended_action=recommended_action_for(error_code),
        **extra,
    )


def session_key(bi_url, bi_user):
    digest = hashlib.sha256(f"{bi_url}|{bi_user}".encode("utf-8")).hexdigest()
    return f"{SESSION_KEY_PREFIX}{digest}"


def _load_shared_session(bi_url, bi_user):
    raw = r.get(session_key(bi_url, bi_user))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    sid = data.get("sid")
    return sid or None


def _save_shared_session(bi_url, bi_user, sid):
    r.setex(
        session_key(bi_url, bi_user),
        SESSION_TTL,
        json.dumps({"sid": sid, "updated_at": time.time()}),
    )


def _invalidate_session(bi_url, bi_user):
    _session_cache.pop((bi_url, bi_user), None)
    r.delete(session_key(bi_url, bi_user))


def bi_login(sess, base_url, user, password, tag):
    try:
        json_url = urljoin(base_url.rstrip("/") + "/", "json")
        r1 = sess.post(json_url, json={"cmd": "login"}, timeout=10)
        r1.raise_for_status()
        sid = r1.json().get("session")
        resp = _bi_protocol_hash(f"{user}:{sid}:{password}")
        r2 = sess.post(json_url, json={"cmd": "login", "session": sid, "response": resp}, timeout=10)
        r2.raise_for_status()
        if r2.json().get("result") != "success":
            logging.error(f"{tag} BI login failed")
            return None
        return sid
    except Exception as exc:
        logging.error(f"{tag} BI login error: {safe_error_summary(exc)}")
        return None


def get_session(bi_url, bi_user, bi_pass, tag):
    key = (bi_url, bi_user)
    cached = _session_cache.get(key)
    if cached:
        sess, sid = cached
        try:
            json_url = urljoin(bi_url.rstrip("/") + "/", "json")
            chk = sess.post(json_url, json={"cmd": "status", "session": sid}, timeout=10)
            if chk.status_code == 200 and chk.json().get("result") == "success":
                return sess, sid
        except Exception:
            pass
        _invalidate_session(bi_url, bi_user)

    shared_sid = _load_shared_session(bi_url, bi_user)
    if shared_sid:
        sess = requests.Session()
        try:
            json_url = urljoin(bi_url.rstrip("/") + "/", "json")
            chk = sess.post(json_url, json={"cmd": "status", "session": shared_sid}, timeout=10)
            if chk.status_code == 200 and chk.json().get("result") == "success":
                _session_cache[key] = (sess, shared_sid)
                logging.info(f"{tag} Reused shared BI session for {bi_user}")
                return sess, shared_sid
        except Exception:
            pass
        _invalidate_session(bi_url, bi_user)

    sess = requests.Session()
    sid = bi_login(sess, bi_url, bi_user, bi_pass, tag)
    if sid:
        _session_cache[key] = (sess, sid)
        _save_shared_session(bi_url, bi_user, sid)
        logging.info(f"{tag} New BI session cached for {bi_user}")
    return sess, sid


def trigger_bi_recovery(restart_url, restart_token, tag):
    url = (restart_url or "").strip()
    if not url:
        return False
    try:
        logging.warning(f"{tag} Stuck encoder -- calling recovery endpoint: {url}")
        resp = requests.post(url, headers={"X-Recovery-Token": restart_token or ""}, timeout=60)
        if resp.status_code == 200:
            logging.info(f"{tag} BI recovery OK -- waiting {RECOVERY_PAUSE}s for BI to restart...")
            time.sleep(RECOVERY_PAUSE)
            return True
        logging.error(f"{tag} BI recovery returned {resp.status_code}")
    except Exception as exc:
        logging.error(f"{tag} BI recovery error: {safe_error_summary(exc)}")
    return False


def bi_get_export_queue(sess, base_url, sid):
    json_url = urljoin(base_url.rstrip("/") + "/", "json?_export")
    resp = sess.post(json_url, json={"cmd": "export", "session": sid}, timeout=10)
    resp.raise_for_status()
    data = resp.json().get("data", [])
    return data if isinstance(data, list) else []


def bi_resolve_export_target(export_data, known_paths, tag):
    if isinstance(export_data, dict):
        path = export_data.get("path")
        uri = (export_data.get("uri") or "").replace("\\", "/")
        if path and uri:
            return path, uri
        return None, None

    if not isinstance(export_data, list):
        return None, None

    new_entries = [item for item in export_data if item.get("path") and item.get("path") not in known_paths]
    if len(new_entries) == 1:
        target = new_entries[0]
        return target.get("path"), (target.get("uri") or "").replace("\\", "/")

    if len(new_entries) > 1:
        logging.warning(f"{tag} Multiple new BI exports detected; using the newest queued item")
        target = new_entries[0]
        return target.get("path"), (target.get("uri") or "").replace("\\", "/")

    if len(export_data) == 1:
        target = export_data[0]
        return target.get("path"), (target.get("uri") or "").replace("\\", "/")

    return None, None


def bi_delete_clip(sess, base_url, sid, clip_id, tag):
    try:
        clean = clip_id.replace("@", "")
        json_url = urljoin(base_url.rstrip("/") + "/", "json")
        resp = sess.post(json_url, json={"cmd": "delclip", "path": f"@{clean}", "session": sid}, timeout=10)
        if resp.json().get("result") == "success":
            logging.info(f"{tag} Deleted clip @{clean}")
            return True
    except Exception as exc:
        logging.error(f"{tag} Delete clip error: {safe_error_summary(exc)}")
    return False


def queue_poll_interval(elapsed):
    if elapsed < 20:
        return 8
    if elapsed < 50:
        return 4
    return 2


def load_job(request_id):
    raw = r.get(job_key(request_id))
    if not raw:
        return None
    return json.loads(raw)


def save_job(job):
    job["updated_at"] = time.time()
    r.set(job_key(job["request_id"]), json.dumps(job))


def write_result(request_id, output_path, ok, error_msg=None):
    result = {
        "ok": ok,
        "path": output_path if ok else None,
        "error": error_msg,
    }
    r.rpush(result_key(request_id), json.dumps(result))
    r.expire(result_key(request_id), RESULT_KEY_TTL)


def finish_job(job, ok, error_msg=None):
    request_id = job["request_id"]
    if ok:
        job["status"] = "downloaded"
    else:
        job["status"] = "failed"
        job["error"] = error_msg
    job["last_transition_at"] = time.time()
    save_job(job)
    r.srem(ACTIVE_EXPORT_SET, request_id)
    write_result(request_id, job.get("output_path"), ok, error_msg)


def mark_delivery_queued(job):
    job["delivery_status"] = "queued"
    job["delivery_queued_at"] = time.time()
    job["last_transition_at"] = job["delivery_queued_at"]
    save_job(job)
    r.rpush(VIDEO_DELIVERY_QUEUE, job["request_id"])


def finish_delivery(job, ok, error_msg=None):
    if ok:
        job["delivery_status"] = "completed"
        job["status"] = "completed"
    else:
        job["delivery_status"] = "failed"
        job["status"] = "delivery_failed"
        job["delivery_error"] = error_msg
    job["last_transition_at"] = time.time()
    save_job(job)


def requeue_delivery(job, reason):
    job["delivery_status"] = "retry_queued"
    job["delivery_error"] = reason
    job["delivery_attempts"] = int(job.get("delivery_attempts", 0))
    job["last_transition_at"] = time.time()
    save_job(job)
    r.rpush(VIDEO_DELIVERY_QUEUE, job["request_id"])


def queue_retry(job, reason):
    """Requeue an export submission attempt using the original request payload."""
    retry_request = dict(job["request"])
    retry_request["queued_at"] = time.time()
    retry_request["_export_attempts"] = job.get("export_attempts", 1)
    retry_request["_recovery_attempts"] = job.get("recovery_attempts", 0)

    job["status"] = "retry_queued"
    job["last_error"] = reason
    job["last_transition_at"] = time.time()
    save_job(job)
    r.srem(ACTIVE_EXPORT_SET, job["request_id"])
    r.rpush(EXPORT_REQUEST_QUEUE, json.dumps(retry_request))


def iter_job_ids():
    cursor = 0
    pattern = f"{JOB_KEY_PREFIX}*"
    while True:
        cursor, keys = r.scan(cursor=cursor, match=pattern, count=100)
        for key in keys:
            decoded = key.decode() if isinstance(key, bytes) else key
            yield decoded.removeprefix(JOB_KEY_PREFIX)
        if cursor == 0:
            break
