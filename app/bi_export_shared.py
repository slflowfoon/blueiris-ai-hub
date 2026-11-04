#!/usr/bin/env python3
"""
Shared helpers for the staged Blue Iris export pipeline.
"""

import hashlib
import json
import logging
import math
import os
import socket
import time
import uuid
from urllib.parse import urljoin, urlparse
from logging.handlers import RotatingFileHandler

import redis
import requests
from db_utils import connect as sqlite_connect


REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
DATA_DIR = os.getenv("DATA_DIR", "/app/data")
DB_FILE = os.path.join(DATA_DIR, "configs.db")

EXPORT_REQUEST_QUEUE     = "bi:export:requests"
OPENBVR_RETRY_QUEUE      = "bi:export:openbvr:retry"
DOWNLOAD_REQUEST_QUEUE   = "bi:download:requests"
VIDEO_DELIVERY_QUEUE     = "bi:delivery:requests"
ACTIVE_EXPORT_SET        = "bi:exports:active"
JOB_KEY_PREFIX           = "bi:job:"
RESULT_KEY_PREFIX        = "bi:result:"
DELIVERY_CLAIM_KEY_PREFIX = "bi:delivery:claim:"
DELIVERY_HEARTBEAT_KEY_PREFIX = "bi:delivery:heartbeat:"
DOWNLOAD_HEARTBEAT_KEY_PREFIX = "bi:download:heartbeat:"
RESULT_KEY_TTL           = 300
STALE_REQUEST_AGE        = 600
EXPORT_QUEUE_TIMEOUT     = 180
EXPORT_QUEUE_ACK_TIMEOUT = 20
DOWNLOAD_TIMEOUT         = 60
RECOVERY_PAUSE           = 15
QUEUE_PROGRESS_LOG_INTERVAL = 15
MAX_EXPORT_ATTEMPTS      = 3
MAX_OPENBVR_DEFER_ATTEMPTS = 1
MAX_RECOVERY_ATTEMPTS    = 1
MAX_DELIVERY_ATTEMPTS    = 3
DELIVERY_CLAIM_TTL       = 180
SESSION_KEY_PREFIX       = "bi:session:"
SESSION_TTL              = 3600
WATCHDOG_INTERVAL        = 15
WATCHDOG_STALE_BUFFER    = 10
RETRY_QUEUE_STALE_AGE    = 30
DELIVERY_QUEUE_STALE_AGE = 45
# Grace period for legacy downloading records that predate attempt heartbeats.
# New attempts use progress heartbeats; total transfer duration is not a deadline.
DOWNLOAD_CLAIM_STALE_AGE = DOWNLOAD_TIMEOUT * 2 + WATCHDOG_STALE_BUFFER
# Renewed from the owning attempt's own progress (chunk processing) and right before
# each bounded external call (BI login, metadata lookup, recovery restart) -- never on
# an unconditional timer, so a genuinely hung transfer's heartbeat goes stale and is
# reclaimable. The stale age must comfortably exceed the slowest single bounded
# external call on this path: requests' 60s timeout applies independently to
# connect/read inactivity; recovery also includes a 15s RECOVERY_PAUSE.
DOWNLOAD_HEARTBEAT_INTERVAL = 10
DOWNLOAD_HEARTBEAT_STALE_AGE = 180

# Must exceed the longest blocking pop (blpop timeout=5): redis-py 8 defaults socket_timeout
# to 5s, which makes an idle blpop raise TimeoutError and crash the service.
REDIS_SOCKET_TIMEOUT = 30
r = redis.from_url(REDIS_URL, socket_timeout=REDIS_SOCKET_TIMEOUT, health_check_interval=30)
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


def delivery_claim_key(request_id):
    return f"{DELIVERY_CLAIM_KEY_PREFIX}{request_id}"


def delivery_heartbeat_key(request_id):
    return f"{DELIVERY_HEARTBEAT_KEY_PREFIX}{request_id}"


def download_heartbeat_key(request_id, token):
    """Scoped per attempt token: a paused/zombie attempt resuming and blindly
    renewing its own (superseded) token can never collide with, or shadow, a
    live newer attempt's heartbeat -- they are different keys entirely."""
    return f"{DOWNLOAD_HEARTBEAT_KEY_PREFIX}{request_id}:{token}"


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
        "openbvr_failed_after_refresh": "check_bi_alert_lookup_row_selection_and_export_source_field",
        "openbvr_lookup_refresh_failed": "check_bi_alert_lookup_freshness_and_source_clip_path_after_openbvr_failure",
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


def bi_lookup_alert(bi_url, bi_user, bi_pass, trigger_filename, tag):
    """
    Look up an alert in BI's alert list using the shared session cache.
    Returns structured metadata for the selected row or None if the alert is not present.
    """
    json_url = urljoin(bi_url.rstrip("/") + "/", "json")
    sess, sid = get_session(bi_url, bi_user, bi_pass, tag)
    if not sid:
        raise RuntimeError("BI login failed")

    alert_list = sess.post(
        json_url,
        json={"cmd": "alertlist", "camera": "Index", "session": sid},
        timeout=10,
    )
    alert_list.raise_for_status()
    exact_matches = [alert for alert in alert_list.json().get("data", []) if alert.get("file") == trigger_filename]
    if exact_matches:
        alert = exact_matches[0]
        export_source_path = alert.get("path") or alert.get("clip")
        export_source_field = "path" if alert.get("path") else "clip"
        lookup_match_type = "exact_file"
        if len(exact_matches) > 1:
            lookup_match_type = "exact_file_duplicate_first"
            logging.warning(
                f"{tag} BI alert lookup found duplicate exact file matches; using first row | "
                f"phase=prequeue_lookup lookup_result=duplicate_exact_file "
                f"trigger_filename={trigger_filename} duplicate_count={len(exact_matches)}"
            )
        return {
            "camera": alert.get("camera"),
            "file": alert.get("file"),
            "path": alert.get("path"),
            "clip": alert.get("clip"),
            "offset": alert.get("offset", 0),
            "msec": alert.get("msec", 10000),
            "lookup_match_type": lookup_match_type,
            "export_source_path": export_source_path,
            "export_source_field": export_source_field,
        }
    return None


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


# A stale/duplicate delivery queue item racing a legitimate owner's own claim
# (e.g. right as the job becomes actionable) is a harmless WATCH conflict: retry
# against the same eligible snapshot rather than giving up on the first bounce.
_DELIVERY_CLAIM_RETRIES = 5


def claim_delivery(request_id, owner, ttl=DELIVERY_CLAIM_TTL):
    """Admit a delivery claim atomically with the job's own eligibility.

    A stale/duplicate queue item must never acquire ownership of a job that is
    not currently downloaded-and-undelivered (e.g. still downloading) even for
    the brief window before a later eligibility re-check would reject it --
    that window is enough for the real owner to misread the job as superseded
    and discard a valid in-flight download."""
    key = job_key(request_id)
    claim_key = delivery_claim_key(request_id)
    heartbeat_key = delivery_heartbeat_key(request_id)
    admission_snapshot = None
    for _attempt in range(_DELIVERY_CLAIM_RETRIES):
        with r.pipeline() as pipe:
            try:
                pipe.watch(key, claim_key, heartbeat_key)
                raw = pipe.get(key)
                current = json.loads(raw) if raw else None
                if current is None or not is_actionable_for_delivery(current):
                    return False
                if admission_snapshot is None:
                    admission_snapshot = current
                elif current != admission_snapshot:
                    return False
                if pipe.exists(claim_key) or pipe.exists(heartbeat_key):
                    return False
                now = time.time()
                pipe.multi()
                pipe.set(claim_key, owner, ex=ttl)
                pipe.setex(heartbeat_key, ttl, str(now))
                pipe.execute()
                return True
            except redis.WatchError:
                continue
    return False


def refresh_delivery_claim(request_id, owner, ttl=DELIVERY_CLAIM_TTL):
    key = delivery_claim_key(request_id)
    with r.pipeline() as pipe:
        try:
            pipe.watch(key)
            current = pipe.get(key)
            if isinstance(current, bytes):
                current = current.decode()
            if current != owner:
                return False
            pipe.multi()
            pipe.expire(key, ttl)
            pipe.setex(delivery_heartbeat_key(request_id), ttl, str(time.time()))
            pipe.execute()
            return True
        except redis.WatchError:
            return False


def touch_delivery_heartbeat(request_id, ttl=DELIVERY_CLAIM_TTL):
    now = time.time()
    r.setex(delivery_heartbeat_key(request_id), ttl, str(now))
    return now


def delivery_heartbeat_age(request_id, now=None):
    raw = r.get(delivery_heartbeat_key(request_id))
    if raw is None:
        return None
    try:
        if isinstance(raw, bytes):
            raw = raw.decode()
        ts = float(raw)
    except (TypeError, ValueError):
        return None
    return (now or time.time()) - ts


# Real Redis-side TTL only bounds leaked keys from a crashed attempt that never
# reclaims; it is deliberately much larger than DOWNLOAD_HEARTBEAT_STALE_AGE so the
# actual liveness decision is always the explicit `ts` field below, not key
# expiry -- keeping it driven by the same (mockable) clock callers use.
_DOWNLOAD_HEARTBEAT_KEY_TTL = DOWNLOAD_HEARTBEAT_STALE_AGE * 5


def touch_download_heartbeat(request_id, token, *, expected=None):
    """Renew progress for one token, optionally validating its exact ownership.

    Before external I/O, validate and renew together: a watchdog either wins the
    job transition first (rejecting I/O) or sees the fresh heartbeat. A zombie's
    token can never renew a different attempt's heartbeat.
    """
    if not token:
        return False
    heartbeat_key = download_heartbeat_key(request_id, token)
    if expected is None:
        r.setex(heartbeat_key, _DOWNLOAD_HEARTBEAT_KEY_TTL, str(time.time()))
        return True
    key = job_key(request_id)
    claim_key = delivery_claim_key(request_id)
    delivery_hb_key = delivery_heartbeat_key(request_id)
    while True:
        with r.pipeline() as pipe:
            try:
                pipe.watch(key, claim_key, delivery_hb_key)
                raw = pipe.get(key)
                if not raw or json.loads(raw) != expected:
                    return False
                if pipe.exists(claim_key) or pipe.exists(delivery_hb_key):
                    return False
                pipe.multi()
                pipe.setex(heartbeat_key, _DOWNLOAD_HEARTBEAT_KEY_TTL, str(time.time()))
                pipe.execute()
                return True
            except redis.WatchError:
                continue


def is_download_attempt_live(request_id, token, now=None, *, started_at=None):
    """Use attempt progress, with a bounded grace for pre-heartbeat records."""
    now = time.time() if now is None else now
    raw = r.get(download_heartbeat_key(request_id, token)) if token else None
    try:
        if raw is not None:
            ts = float(raw)
            return math.isfinite(ts) and now - ts < DOWNLOAD_HEARTBEAT_STALE_AGE
        # A newly claimed attempt always has a heartbeat. Missing keys on legacy
        # records get only the original grace; they cannot stay live indefinitely.
        return started_at is not None and now - float(started_at) < DOWNLOAD_CLAIM_STALE_AGE
    except (TypeError, ValueError):
        return False


def clear_delivery_claim(request_id, owner=None):
    key = delivery_claim_key(request_id)
    if owner is None:
        r.delete(key, delivery_heartbeat_key(request_id))
        return

    with r.pipeline() as pipe:
        try:
            pipe.watch(key)
            current = pipe.get(key)
            if isinstance(current, bytes):
                current = current.decode()
            if current != owner:
                return
            pipe.multi()
            pipe.delete(key, delivery_heartbeat_key(request_id))
            pipe.execute()
        except redis.WatchError:
            return


def _download_attempt_output_path(base_path, token):
    """Use a unique file per attempt, including across export counter resets."""
    root, ext = os.path.splitext(base_path)
    return f"{root}.dl-{token}{ext}"


def claim_download_attempt(request_id):
    """Claim an unchanged ready job with no delivery owner, using a unique file."""
    key = job_key(request_id)
    claim_key = delivery_claim_key(request_id)
    heartbeat_key = delivery_heartbeat_key(request_id)
    with r.pipeline() as pipe:
        try:
            pipe.watch(key, claim_key, heartbeat_key)
            raw = pipe.get(key)
            current = json.loads(raw) if raw else None
            if not current or current.get("status") != "ready":
                return None
            if current.get("delivery_status") not in (None, "pending"):
                return None
            if pipe.exists(claim_key) or pipe.exists(heartbeat_key):
                return None
            attempt = int(current.get("download_attempts", 0)) + 1
            base_path = current.get("download_base_path") or current.get("output_path")
            token = uuid.uuid4().hex
            claimed = dict(current)
            claimed["download_base_path"] = base_path
            claimed["download_attempts"] = attempt
            claimed["download_attempt_token"] = token
            claimed["output_path"] = _download_attempt_output_path(base_path, token)
            claimed["status"] = "downloading"
            claimed["last_transition_at"] = time.time()
            pipe.multi()
            pipe.set(key, json.dumps(claimed))
            pipe.setex(
                download_heartbeat_key(request_id, token),
                _DOWNLOAD_HEARTBEAT_KEY_TTL,
                str(claimed["last_transition_at"]),
            )
            pipe.execute()
            return claimed
        except redis.WatchError:
            return None


def is_download_claim_current(claimed):
    """Revalidate state and delivery ownership before external download actions."""
    return load_job(claimed["request_id"]) == claimed and not has_delivery_claim(claimed["request_id"])


def has_delivery_claim(request_id):
    """Best-effort precheck; commits must also WATCH claim and heartbeat keys."""
    return bool(r.exists(delivery_claim_key(request_id)) or r.exists(delivery_heartbeat_key(request_id)))


def commit_download_outcome(expected, ok, error_msg=None):
    """Atomically commit the exact download snapshot, result and delivery handoff."""
    if expected.get("status") != "downloading" or expected.get("delivery_status") not in (None, "pending"):
        return None
    request_id = expected["request_id"]
    key = job_key(request_id)
    claim_key = delivery_claim_key(request_id)
    heartbeat_key = delivery_heartbeat_key(request_id)
    while True:
        with r.pipeline() as pipe:
            try:
                pipe.watch(key, claim_key, heartbeat_key)
                raw = pipe.get(key)
                current = json.loads(raw) if raw else None
                if current != expected:
                    return None
                if pipe.exists(claim_key) or pipe.exists(heartbeat_key):
                    return None
                updated = dict(current)
                now = time.time()
                if ok:
                    updated["status"] = "downloaded"
                else:
                    updated["status"] = "failed"
                    updated["error"] = error_msg
                updated["last_transition_at"] = now
                updated["updated_at"] = now
                queue_delivery = ok and bool(updated.get("delivery_context"))
                if queue_delivery:
                    updated["delivery_status"] = "queued"
                    updated["delivery_queued_at"] = now
                pipe.multi()
                pipe.set(key, json.dumps(updated))
                pipe.srem(ACTIVE_EXPORT_SET, request_id)
                pipe.rpush(result_key(request_id), json.dumps({
                    "ok": ok,
                    "path": updated.get("output_path") if ok else None,
                    "error": error_msg,
                }))
                pipe.expire(result_key(request_id), RESULT_KEY_TTL)
                if queue_delivery:
                    pipe.rpush(VIDEO_DELIVERY_QUEUE, request_id)
                pipe.execute()
                return updated
            except redis.WatchError:
                continue


def reserve_recovery_attempt(expected):
    """Atomically persist the recovery-attempt increment BEFORE the external BI
    restart call, so a crash or watchdog reclaim mid-call cannot repeat the
    restart past MAX_RECOVERY_ATTEMPTS. Also renews the download heartbeat for
    the bounded external call about to follow."""
    if expected.get("status") not in {"submitted", "queued", "downloading"}:
        return None
    if is_contradictory_delivery_pair(expected):
        return None
    if int(expected.get("recovery_attempts", 0)) >= MAX_RECOVERY_ATTEMPTS:
        return None
    request_id = expected["request_id"]
    key = job_key(request_id)
    claim_key = delivery_claim_key(request_id)
    heartbeat_key = delivery_heartbeat_key(request_id)
    while True:
        with r.pipeline() as pipe:
            try:
                pipe.watch(key, claim_key, heartbeat_key)
                raw = pipe.get(key)
                current = json.loads(raw) if raw else None
                if current != expected:
                    return None
                if pipe.exists(claim_key) or pipe.exists(heartbeat_key):
                    return None
                now = time.time()
                updated = dict(current)
                updated["recovery_attempts"] = int(current.get("recovery_attempts", 0)) + 1
                updated["request"] = dict(current["request"], _recovery_attempts=updated["recovery_attempts"])
                updated["last_transition_at"] = now
                token = updated.get("download_attempt_token")
                pipe.multi()
                pipe.set(key, json.dumps(updated))
                if token:
                    pipe.setex(download_heartbeat_key(request_id, token), _DOWNLOAD_HEARTBEAT_KEY_TTL, str(now))
                pipe.execute()
                return updated
            except redis.WatchError:
                continue


def requeue_stale_download(snapshot, now):
    """Requeue an unchanged ready/crashed download only without a delivery owner.

    For an in-flight "downloading" attempt, reclamation is gated on the owning
    attempt's own heartbeat rather than elapsed time: a healthy streaming transfer
    must not be discarded just because it has been running a while. The download
    heartbeat key is WATCHed so a last-instant renewal by the live owner aborts
    this reclaim instead of racing past it."""
    is_downloading = snapshot.get("status") == "downloading"
    if snapshot.get("status") not in {"ready", "downloading"} or is_contradictory_delivery_pair(snapshot):
        return None
    request_id = snapshot["request_id"]
    token = snapshot.get("download_attempt_token")
    if is_downloading and is_download_attempt_live(
        request_id, token, now=now, started_at=snapshot.get("last_transition_at", snapshot.get("updated_at")),
    ):
        return None
    replacement = dict(snapshot, status="ready", last_transition_at=now)
    key = job_key(request_id)
    claim_key = delivery_claim_key(request_id)
    heartbeat_key = delivery_heartbeat_key(request_id)
    download_hb_key = download_heartbeat_key(request_id, token) if is_downloading else None
    with r.pipeline() as pipe:
        try:
            watch_keys = [key, claim_key, heartbeat_key]
            if download_hb_key:
                watch_keys.append(download_hb_key)
            pipe.watch(*watch_keys)
            raw = pipe.get(key)
            current = json.loads(raw) if raw else None
            if current != snapshot:
                return None
            if pipe.exists(claim_key) or pipe.exists(heartbeat_key):
                return None
            if is_downloading and is_download_attempt_live(
                request_id, token, now=now, started_at=snapshot.get("last_transition_at", snapshot.get("updated_at")),
            ):
                return None
            pipe.multi()
            pipe.set(key, json.dumps(replacement))
            if is_downloading:
                pipe.delete(download_hb_key)
            pipe.rpush(DOWNLOAD_REQUEST_QUEUE, request_id)
            pipe.execute()
            return replacement
        except redis.WatchError:
            return None


# A live owner's own heartbeat thread touches the claim/heartbeat keys (EXPIRE /
# SETEX) roughly every 15s while a commit below is in flight, which is a benign
# self-conflict, not a real ownership/state change. Bound the retry so a handful
# of re-validated attempts absorb that race without masking a genuine one.
_DELIVERY_COMMIT_RETRIES = 5


def _commit_delivery_transition(expected, owner, build_replacement, *, enqueue=False, release_claim=True):
    """Commit an exact snapshot only for its delivery owner (or absent ownership).

    Retry heartbeat WATCH conflicts with fresh ownership checks, without repeating
    external I/O. A real state/owner change rejects the transition."""
    if not is_actionable_for_delivery(expected):
        return None
    request_id = expected["request_id"]
    key = job_key(request_id)
    claim_key = delivery_claim_key(request_id)
    heartbeat_key = delivery_heartbeat_key(request_id)
    for _attempt in range(_DELIVERY_COMMIT_RETRIES):
        with r.pipeline() as pipe:
            try:
                pipe.watch(key, claim_key, heartbeat_key)
                raw = pipe.get(key)
                current = json.loads(raw) if raw else None
                if current != expected:
                    return None
                claim_value = pipe.get(claim_key)
                if isinstance(claim_value, bytes):
                    claim_value = claim_value.decode()
                if owner is None:
                    if claim_value is not None or pipe.exists(heartbeat_key):
                        return None
                elif claim_value != owner:
                    return None
                replacement = build_replacement(current)
                pipe.multi()
                pipe.set(key, json.dumps(replacement))
                if release_claim:
                    pipe.delete(claim_key, heartbeat_key)
                if enqueue:
                    pipe.rpush(VIDEO_DELIVERY_QUEUE, request_id)
                pipe.execute()
                return replacement
            except redis.WatchError:
                continue
    return None


def update_delivery_progress(expected, replacement, owner):
    """Persist admitted progress/message ID without releasing the owner's claim."""
    return _commit_delivery_transition(
        expected, owner, lambda _current: dict(replacement), release_claim=False,
    )


def mark_delivery_queued(expected):
    """Requeue an unchanged downloaded job only without a live delivery owner."""
    def _build(current):
        now = time.time()
        updated = dict(current)
        updated["delivery_status"] = "queued"
        updated["delivery_queued_at"] = now
        updated["last_transition_at"] = now
        return updated
    return _commit_delivery_transition(expected, None, _build, enqueue=True)


def finish_delivery(expected, ok, error_msg=None, *, owner=None):
    """Settle the exact delivery snapshot only while ownership still matches."""
    def _build(current):
        updated = dict(current)
        now = time.time()
        if ok:
            updated["delivery_status"] = "completed"
            updated["status"] = "completed"
        else:
            updated["delivery_status"] = "failed"
            updated["status"] = "delivery_failed"
            updated["delivery_error"] = error_msg
        updated["last_transition_at"] = now
        return updated
    return _commit_delivery_transition(expected, owner, _build)


def requeue_delivery(expected, reason, *, owner=None):
    """Retry the exact delivery snapshot only while ownership still matches."""
    def _build(current):
        updated = dict(current)
        now = time.time()
        updated["delivery_status"] = "retry_queued"
        updated["delivery_error"] = reason
        updated["delivery_attempts"] = int(current.get("delivery_attempts", 0))
        updated["last_transition_at"] = now
        return updated
    return _commit_delivery_transition(expected, owner, _build, enqueue=True)


# A job with no delivery yet (export in progress/retry, or download failed) must
# carry one of these delivery_status values; anything else is a contradictory
# persisted pair (e.g. a delivery claim/status against a never-downloaded export).
NO_DELIVERY_YET_STATUSES = (None, "pending")
# A `downloaded` job's delivery_status while delivery legitimately has not
# finished yet -- covers a fresh handoff, an explicit requeue, and a watchdog- or
# worker-restart recovery of a stalled `processing` attempt.
DOWNLOADED_DELIVERY_IN_PROGRESS_STATUSES = (None, "pending", "queued", "processing", "retry_queued")


def is_actionable_for_delivery(job):
    """Whether the persisted pair can legitimately begin or resume delivery."""
    return (
        job.get("status") == "downloaded"
        and job.get("delivery_status") in DOWNLOADED_DELIVERY_IN_PROGRESS_STATUSES
    )


def is_contradictory_delivery_pair(job):
    """Report inconsistent persisted state pairs without repairing or replaying them."""
    status = job.get("status")
    delivery_status = job.get("delivery_status")
    if status in ("completed", "delivery_failed"):
        return delivery_status != ("completed" if status == "completed" else "failed")
    if status == "downloaded":
        return delivery_status not in DOWNLOADED_DELIVERY_IN_PROGRESS_STATUSES
    return delivery_status not in NO_DELIVERY_YET_STATUSES


def count_actionable_active_exports():
    """Count ACTIVE_EXPORT_SET members that are still real in-flight exports.

    A contradictory persisted record (#189) is left untouched as a historical
    record and never repaired/removed from the set, but it also must not
    permanently read as "an export is active" and starve OpenBVR scheduling."""
    count = 0
    for raw_id in r.smembers(ACTIVE_EXPORT_SET):
        request_id = raw_id.decode() if isinstance(raw_id, bytes) else raw_id
        job = load_job(request_id)
        if job and job.get("status") in {"submitted", "queued"} and not is_contradictory_delivery_pair(job):
            count += 1
    return count


EXPORT_RETRYABLE_STATUSES = {"submitted", "queued", "ready", "downloading", "retry_queued"}


def queue_retry(job, reason, *, expected_job=None):
    """Requeue using the original payload; watchdog callers supply their snapshot."""
    if is_contradictory_delivery_pair(job) or job.get("status") not in EXPORT_RETRYABLE_STATUSES:
        return None
    if expected_job is not None and (
        is_contradictory_delivery_pair(expected_job) or expected_job.get("status") not in EXPORT_RETRYABLE_STATUSES
    ):
        return None
    snapshot = dict(expected_job if expected_job is not None else job)
    job = dict(job)
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

    job["status"] = "retry_queued"
    job["last_error"] = reason
    job["last_transition_at"] = time.time()
    job["request"] = retry_request
    return replace_export_job(
        snapshot, job, request_queue=EXPORT_REQUEST_QUEUE, allow_missing=expected_job is None,
    )


def is_pending_export_request(job, request):
    """Only an absent job or its exact persisted retry may be consumed.

    Compare the whole original payload, including legacy retry metadata. Preparation
    can refresh clip metadata or defer a request, so callers keep an unmodified copy.
    A contradictory persisted pair (#189) is a historical record, never a pending
    request to replay: a `retry_queued` status with a delivery_status that implies
    delivery already started/finished must not be consumed again.
    """
    return job is None or (
        job.get("status") == "retry_queued"
        and job.get("request") == request
        and not is_contradictory_delivery_pair(job)
    )


def _legacy_openbvr_base(job):
    """Old deferral left an enriched retry unchanged, without a queue marker."""
    if not job or job.get("status") != "retry_queued" or "retry_queue" in job:
        return None
    if is_contradictory_delivery_pair(job):
        return None
    request = job.get("request")
    if not isinstance(request, dict) or request.get("request_id") != job.get("request_id"):
        return None
    if request.get("_openbvr_deferred_attempts", 0) != 0:
        return None
    # These fields identify the saved export generation, not just its enqueue time.
    if (
        request.get("_export_attempts") != job.get("export_attempts", 1)
        or request.get("_recovery_attempts") != job.get("recovery_attempts", 0)
        or "_previous_target_path" not in request
        or request["_previous_target_path"] != job.get("target_path")
    ):
        return None
    return request


def _is_legacy_openbvr_deferral(job, deferred):
    request = _legacy_openbvr_base(job)
    if request is None or not isinstance(deferred, dict):
        return False
    if type(deferred.get("_openbvr_deferred_attempts")) is not int or deferred["_openbvr_deferred_attempts"] != 1:
        return False
    old_time, new_time = request.get("queued_at"), deferred.get("queued_at")
    if not all(type(value) in (int, float) and math.isfinite(value) for value in (old_time, new_time)):
        return False
    if not 0 < old_time <= new_time <= time.time():
        return False
    expected = dict(request, queued_at=new_time, _openbvr_deferred_attempts=1)
    if deferred == expected:
        return True
    # A retry already refreshed while idle refreshed metadata again before its
    # first deferral. Only that old lookup transformation may differ from saved P.
    refreshed = request.get("_openbvr_refreshed_attempts", 0)
    if type(refreshed) is not int or refreshed <= 0:
        return False
    fields = (
        "clip_path", "offset", "duration", "_lookup_file", "_lookup_path",
        "_lookup_clip", "_lookup_match_type", "_export_source_field",
    )
    if any(field not in deferred for field in fields):
        return False
    source = deferred.get("_lookup_path") or deferred.get("_lookup_clip")
    source_field = "path" if deferred.get("_lookup_path") else "clip"
    if (
        not isinstance(source, str) or not source
        or deferred["clip_path"] != source
        or deferred["_export_source_field"] != source_field
        or deferred["_lookup_file"] != request.get("trigger_filename")
        or deferred["_lookup_match_type"] not in ("exact_file", "exact_file_duplicate_first")
    ):
        return False
    try:
        int(deferred["offset"] or 0)
        int(deferred["duration"] or 10000)
    except (TypeError, ValueError, OverflowError):
        return False
    expected.update({field: deferred[field] for field in fields})
    return deferred == expected


def _queued_legacy_openbvr_deferral(pipe, job):
    if _legacy_openbvr_base(job) is None:
        return None
    pipe.watch(OPENBVR_RETRY_QUEUE)
    for raw in pipe.lrange(OPENBVR_RETRY_QUEUE, 0, -1):
        try:
            deferred = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if _is_legacy_openbvr_deferral(job, deferred):
            return deferred
    return None


def _adopt_legacy_openbvr_deferral(job, deferred):
    return dict(job, request=deferred, retry_queue=OPENBVR_RETRY_QUEUE, updated_at=time.time())


def reconcile_legacy_export_request(request):
    """Adopt an old deferral before intake can reject its older main-queue copy.

    A popped deferral is validated against the watched job. An older request also
    watches queue membership; a concurrent pop aborts intake rather than failing it.
    Neither path submits early, enqueues another copy or refreshes request age.
    """
    key = job_key(request["request_id"])
    while True:
        with r.pipeline() as pipe:
            direct_deferral = False
            try:
                pipe.watch(key)
                raw = pipe.get(key)
                current = json.loads(raw) if raw else None
                direct_deferral = _is_legacy_openbvr_deferral(current, request)
                if direct_deferral:
                    deferred = request
                elif is_pending_export_request(current, request):
                    deferred = _queued_legacy_openbvr_deferral(pipe, current)
                else:
                    return False
                if deferred is None:
                    return True
                updated = _adopt_legacy_openbvr_deferral(current, deferred)
                pipe.multi()
                pipe.set(key, json.dumps(updated))
                pipe.execute()
                return direct_deferral
            except redis.WatchError:
                if not direct_deferral:
                    return False
                # A popped deferral has no queue copy to recover. Revalidate the
                # live job after a conflict; never overwrite settlement/new progress.
                continue


def transition_export_request(request, job=None, *, error_msg=None, deferred_request=None):
    """Commit a matching export outcome and its queue/result effects together.

    A failed job is retained even for an initial rejection without a saved job:
    result consumers/expiry must not erase the duplicate-suppression record.
    """
    request_id = request["request_id"]
    key = job_key(request_id)
    while True:
        with r.pipeline() as pipe:
            legacy_pending = False
            legacy_deferred = None
            try:
                pipe.watch(key)
                raw = pipe.get(key)
                current = json.loads(raw) if raw else None
                if not is_pending_export_request(current, request):
                    return None
                legacy_pending = _legacy_openbvr_base(current) is not None
                legacy_deferred = _queued_legacy_openbvr_deferral(pipe, current)
                if legacy_deferred is not None:
                    # Close the gap between intake and outcome: a legacy deferral
                    # queued meanwhile must win over rejection of the old request.
                    pipe.multi()
                    pipe.set(key, json.dumps(_adopt_legacy_openbvr_deferral(current, legacy_deferred)))
                    pipe.execute()
                    return None
                updated = job if job is not None else current or {
                    "request_id": request_id,
                    "alert_request_id": request.get("alert_request_id"),
                    "config_name": request.get("config_name", "?"),
                    "request": request,
                    "output_path": request.get("output_path"),
                }
                now = time.time()
                updated["updated_at"] = now
                if job is None:
                    updated["last_transition_at"] = now
                    if deferred_request is not None:
                        updated["status"] = "retry_queued"
                        updated["request"] = deferred_request
                        updated["retry_queue"] = OPENBVR_RETRY_QUEUE
                    else:
                        updated["status"] = "failed"
                        updated["error"] = error_msg
                pipe.multi()
                pipe.set(key, json.dumps(updated))
                if job is not None:
                    pipe.sadd(ACTIVE_EXPORT_SET, request_id)
                else:
                    pipe.srem(ACTIVE_EXPORT_SET, request_id)
                    if deferred_request is not None:
                        pipe.rpush(OPENBVR_RETRY_QUEUE, json.dumps(deferred_request))
                    else:
                        pipe.rpush(result_key(request_id), json.dumps({
                            "ok": False, "path": None, "error": error_msg,
                        }))
                        pipe.expire(result_key(request_id), RESULT_KEY_TTL)
                pipe.execute()
                return updated
            except redis.WatchError:
                if legacy_pending and (
                    (job is None and deferred_request is None) or legacy_deferred is not None
                ):
                    # A queue pop may now own the deferral. Drop this obsolete
                    # intake instead of retrying rejection against its older payload.
                    return None
                # Keep an already prepared success or new deferral across harmless conflicts.
                # Recheck the live request and deferral queue without calling BI again.
                continue


def replace_export_job(
    snapshot, replacement, *, active=False, request_queue=None, download_queue=False, error_msg=None,
    allow_missing=False,
):
    """Commit a watchdog/queue-monitor export transition only while its snapshot
    is current. `download_queue=True` atomically promotes the job to the download
    stage: it is mutually exclusive with `request_queue` (there is no caller that
    needs both)."""
    if is_contradictory_delivery_pair(snapshot) or snapshot.get("status") not in EXPORT_RETRYABLE_STATUSES:
        return None
    request_id = snapshot["request_id"]
    key = job_key(request_id)
    proposed = dict(replacement)
    proposed_queue = request_queue
    while True:
        replacement = dict(proposed)
        request_queue = proposed_queue
        with r.pipeline() as pipe:
            legacy_deferred = None
            deferred_queue_watched = False
            try:
                pipe.watch(key)
                if snapshot.get("status") in {"ready", "downloading"}:
                    claim_key = delivery_claim_key(request_id)
                    heartbeat_key = delivery_heartbeat_key(request_id)
                    pipe.watch(claim_key, heartbeat_key)
                    if pipe.exists(claim_key) or pipe.exists(heartbeat_key):
                        return None
                raw = pipe.get(key)
                current = json.loads(raw) if raw else None
                if current != snapshot and not (allow_missing and current is None):
                    return None
                request_payload = None
                if request_queue is not None:
                    deferred_queue_watched = _legacy_openbvr_base(current) is not None
                    legacy_deferred = _queued_legacy_openbvr_deferral(pipe, current)
                    if legacy_deferred is not None:
                        replacement = _adopt_legacy_openbvr_deferral(replacement, legacy_deferred)
                        request_queue = OPENBVR_RETRY_QUEUE
                    request_payload = json.dumps(replacement["request"])
                    if request_queue == OPENBVR_RETRY_QUEUE:
                        # Busy BI leaves deferrals queued. Watch membership with the job
                        # so a concurrent pop/push cannot invalidate the enqueue decision.
                        deferred_queue_watched = True
                        pipe.watch(request_queue)
                        if pipe.lpos(request_queue, request_payload) is not None:
                            request_payload = None
                replacement["updated_at"] = time.time()
                pipe.multi()
                pipe.set(key, json.dumps(replacement))
                if active:
                    pipe.sadd(ACTIVE_EXPORT_SET, request_id)
                else:
                    pipe.srem(ACTIVE_EXPORT_SET, request_id)
                if request_payload is not None:
                    pipe.rpush(request_queue, request_payload)
                if download_queue:
                    pipe.rpush(DOWNLOAD_REQUEST_QUEUE, request_id)
                if error_msg is not None:
                    pipe.rpush(result_key(request_id), json.dumps({
                        "ok": False, "path": None, "error": error_msg,
                    }))
                    pipe.expire(result_key(request_id), RESULT_KEY_TTL)
                pipe.execute()
                return replacement
            except redis.WatchError:
                if deferred_queue_watched:
                    # A queue pop/push or legacy adoption invalidates this decision.
                    # Preserve #190: a later scan must recheck queue ownership before
                    # changing timestamps or replaying a possibly consumed request.
                    return None
                # Retry only the same snapshot; no external call is repeated.
                continue


def requeue_export_retry(snapshot, now):
    """Requeue only an unchanged retry snapshot, atomically with its timestamp."""
    if snapshot.get("status") != "retry_queued":
        return None
    replacement = dict(snapshot, last_transition_at=now)
    queue = (
        OPENBVR_RETRY_QUEUE if snapshot.get("retry_queue") == OPENBVR_RETRY_QUEUE
        else EXPORT_REQUEST_QUEUE
    )
    return replace_export_job(snapshot, replacement, request_queue=queue)


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
