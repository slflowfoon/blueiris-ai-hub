#!/usr/bin/env python3
"""
Shared helpers for the staged Blue Iris export pipeline.
"""

import hashlib
import json
import logging
import os
import time
from urllib.parse import urljoin

import redis
import requests


REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

EXPORT_REQUEST_QUEUE     = "bi:export:requests"
DOWNLOAD_REQUEST_QUEUE   = "bi:download:requests"
ACTIVE_EXPORT_SET        = "bi:exports:active"
JOB_KEY_PREFIX           = "bi:job:"
RESULT_KEY_PREFIX        = "bi:result:"
RESULT_KEY_TTL           = 60
STALE_REQUEST_AGE        = 600
EXPORT_QUEUE_TIMEOUT     = 180
EXPORT_QUEUE_ACK_TIMEOUT = 20
DOWNLOAD_TIMEOUT         = 60
RECOVERY_PAUSE           = 15
QUEUE_PROGRESS_LOG_INTERVAL = 15
MAX_EXPORT_ATTEMPTS      = 2
MAX_RECOVERY_ATTEMPTS    = 1

r = redis.from_url(REDIS_URL)
_session_cache = {}


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
    return f"[{job.get('config_name', '?')}][{job.get('request_id', 'unknown')[:8]}]"


def _invalidate_session(bi_url, bi_user):
    _session_cache.pop((bi_url, bi_user), None)


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
        logging.error(f"{tag} BI login error: {exc}")
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

    sess = requests.Session()
    sid = bi_login(sess, bi_url, bi_user, bi_pass, tag)
    if sid:
        _session_cache[key] = (sess, sid)
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
        logging.error(f"{tag} BI recovery error: {exc}")
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
        logging.error(f"{tag} Delete clip error: {exc}")
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
    save_job(job)
    r.srem(ACTIVE_EXPORT_SET, request_id)
    write_result(request_id, job.get("output_path"), ok, error_msg)


def queue_retry(job, reason):
    """Requeue an export submission attempt using the original request payload."""
    retry_request = dict(job["request"])
    retry_request["queued_at"] = time.time()
    retry_request["_export_attempts"] = job.get("export_attempts", 1)
    retry_request["_recovery_attempts"] = job.get("recovery_attempts", 0)

    job["status"] = "retry_queued"
    job["last_error"] = reason
    save_job(job)
    r.srem(ACTIVE_EXPORT_SET, job["request_id"])
    r.rpush(EXPORT_REQUEST_QUEUE, json.dumps(retry_request))
