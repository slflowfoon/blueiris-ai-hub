#!/usr/bin/env python3
"""
Centralised Blue Iris API monitor - Queue Monitor Edition.

Serialises all BI API calls and monitors the active export queue for 
completion, eliminating clipboard contention and polling overhead.
"""

import sys
import hashlib
import json
import logging
import os
import time
from urllib.parse import urljoin

import redis
import requests
from logging.handlers import RotatingFileHandler

# =============================================================================
# Configuration
# =============================================================================

LOG_FILE  = os.getenv("LOG_FILE", "/app/logs/bi_monitor.log")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

REQUEST_QUEUE          = "bi:requests"
RESULT_KEY_TTL         = 60
STALE_REQUEST_AGE      = 600
EXPORT_QUEUE_TIMEOUT   = 180   # Max seconds to wait for item to leave queue
DOWNLOAD_TIMEOUT       = 60    # Max seconds for the final file-ready check
RECOVERY_PAUSE         = 15
BLPOP_BLOCK_TIMEOUT    = 5

# =============================================================================
# Logging
# =============================================================================

if os.path.dirname(LOG_FILE):
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

logging.basicConfig(
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=1),
        logging.StreamHandler(sys.stdout)
    ],
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

r = redis.from_url(REDIS_URL)

# =============================================================================
# BI protocol helpers
# =============================================================================

def _bi_protocol_hash(s: str) -> str:
    """MD5 hex digest required by the Blue Iris JSON API."""
    return hashlib.md5(s.encode("utf-8"), usedforsecurity=False).hexdigest()


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
    except Exception as e:
        logging.error(f"{tag} BI login error: {e}")
        return None


def bi_find_alert_details(sess, base_url, sid, trigger_filename, tag, verbose=False):
    """Find alert in BI list with retries to account for indexing delays."""
    for attempt in range(3):
        try:
            json_url = urljoin(base_url.rstrip("/") + "/", "json")
            resp = sess.post(json_url, json={"cmd": "alertlist", "camera": "Index", "session": sid}, timeout=10)
            resp.raise_for_status()
            data = resp.json().get("data", [])
            
            for alert in data:
                if alert.get("file") == trigger_filename:
                    return alert.get("clip"), alert.get("offset", 0), alert.get("msec", 10000)
            
            if attempt < 2:
                logging.warning(f"{tag} Alert not in list yet (attempt {attempt+1}/3). Waiting 2s...")
                time.sleep(2)
        except Exception as e:
            logging.error(f"{tag} BI alert list error: {e}")
            time.sleep(1)
            
    return None, 0, 0


def bi_wait_for_queue_completion(sess, base_url, sid, target_path, tag):
    """Polls the export queue. Returns True when target_path is no longer present."""
    json_url = urljoin(base_url.rstrip("/") + "/", "json?_export")
    start = time.time()
    logging.info(f"{tag} Monitoring export queue for completion of {target_path}...")
    
    while time.time() - start < EXPORT_QUEUE_TIMEOUT:
        try:
            resp = sess.post(json_url, json={"cmd": "export", "session": sid}, timeout=10)
            active_exports = resp.json().get("data", [])
            
            if not any(item.get("path") == target_path for item in active_exports):
                logging.info(f"{tag} Export {target_path} completed (left queue).")
                return True
                
            logging.info(f"{tag} Export still in progress (queue size: {len(active_exports)})")
        except Exception as e:
            logging.warning(f"{tag} Error polling export queue: {e}")
            
        time.sleep(2)
    return False


def bi_get_export_queue(sess, base_url, sid):
    """Returns the active BI export queue as a list."""
    json_url = urljoin(base_url.rstrip("/") + "/", "json?_export")
    resp = sess.post(json_url, json={"cmd": "export", "session": sid}, timeout=10)
    resp.raise_for_status()
    data = resp.json().get("data", [])
    return data if isinstance(data, list) else []


def bi_resolve_export_target(export_data, known_paths, tag):
    """
    Resolve the queued export entry from BI's response payload.

    Blue Iris may return either a single object for the new export or the full
    active queue. Prefer newly-seen queue entries over positional guesses.
    """
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
    """Deletes a clip from the BI clipboard using its ID."""
    try:
        clean = clip_id.replace("@", "")
        json_url = urljoin(base_url.rstrip("/") + "/", "json")
        resp = sess.post(json_url, json={"cmd": "delclip", "path": f"@{clean}", "session": sid}, timeout=10)
        if resp.json().get("result") == "success":
            logging.info(f"{tag} Deleted clip @{clean}")
            return True
    except Exception as e:
        logging.error(f"{tag} Delete clip error: {e}")
    return False


def trigger_bi_recovery(restart_url, restart_token, tag):
    """POST to the Windows bi_recovery.py endpoint to restart the BI service."""
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
        logging.error(f"{tag} BI recovery returned {resp.status_code}: {resp.text[:100]}")
    except Exception as e:
        logging.error(f"{tag} BI recovery error: {e}")
    return False


# =============================================================================
# Session cache
# =============================================================================

_session_cache: dict = {}


def _get_session(bi_url, bi_user, bi_pass, tag):
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


def _invalidate_session(bi_url, bi_user):
    _session_cache.pop((bi_url, bi_user), None)


# =============================================================================
# Core export logic
# =============================================================================

def _do_export(req, tag):
    """
    Execute a single BI export request end-to-end.
    Returns (bool, str): (Success status, Error message or None)
    """
    bi_url         = req["bi_url"]
    bi_user        = req["bi_user"]
    bi_pass        = req["bi_pass"]
    trigger_file   = req["trigger_filename"]
    output_path    = req["output_path"]
    verbose        = req.get("verbose", False)
    delete_after   = req.get("delete_after", True)
    restart_url    = req.get("bi_restart_url", "")
    restart_token  = req.get("bi_restart_token", "")
    recovery_depth = req.get("_recovery_depth", 0)

    def _try_recovery(reason):
        if recovery_depth >= 1:
            logging.warning(f"{tag} Recovery already attempted -- not retrying again")
            return None
        if trigger_bi_recovery(restart_url, restart_token, tag):
            _invalidate_session(bi_url, bi_user)
            return _do_export({**req, "_recovery_depth": recovery_depth + 1}, tag)
        return None

    sess, sid = _get_session(bi_url, bi_user, bi_pass, tag)
    if not sid:
        return False, "BI login failed"

    # 1. Resolve clip details
    clip_path = req.get("clip_path")
    offset    = req.get("offset", 0)
    duration  = req.get("duration", 10000)
    
    if not clip_path:
        clip_path, offset, duration = bi_find_alert_details(sess, bi_url, sid, trigger_file, tag, verbose)
        if not clip_path:
            return False, "alert not found in BI list"

    final_path = clip_path if clip_path.startswith("@") else f"@{clip_path}"
    if not final_path.endswith(".bvr"):
        final_path += ".bvr"

    try:
        export_url = urljoin(bi_url.rstrip("/") + "/", "json?_export")
        payload = {
            "cmd": "export", "path": final_path,
            "startms": int(offset), "msec": int(duration),
            "format": 1, "audio": False, "session": sid,
        }

        # Snapshot the queue first so we can identify the newly-created export
        # even when BI returns the full active queue instead of a single object.
        known_paths = set()
        try:
            known_paths = {
                item.get("path") for item in bi_get_export_queue(sess, bi_url, sid) if item.get("path")
            }
        except Exception as e:
            logging.warning(f"{tag} Failed to read export queue before enqueue: {e}")

        # --- EXPORT COMMAND WITH RETRY ---
        target_path = None
        relative_uri = None
        for export_attempt in range(2):
            er = sess.post(export_url, json=payload, timeout=10)
            res = er.json()
            if res.get("result") == "success":
                target_path, relative_uri = bi_resolve_export_target(res.get("data"), known_paths, tag)
                if not target_path or not relative_uri:
                    try:
                        queue_data = bi_get_export_queue(sess, bi_url, sid)
                        target_path, relative_uri = bi_resolve_export_target(queue_data, known_paths, tag)
                    except Exception as e:
                        logging.warning(f"{tag} Failed to refresh export queue after enqueue: {e}")
                if target_path and relative_uri:
                    break
            
            if "OpenBVR failed" in str(res.get("data", {})) and export_attempt == 0:
                logging.warning(f"{tag} BI reported OpenBVR failed. Retrying in 2s...")
                time.sleep(2)
                continue
            
            return False, f"BI export command failed: {res.get('result')}"

        if not target_path or not relative_uri:
            return False, "missing path/uri in BI response"

        # 3. Wait for disappearance from active queue
        if not bi_wait_for_queue_completion(sess, bi_url, sid, target_path, tag):
            result = _try_recovery("queue timeout")
            if result is not None:
                return result
            return False, "timed out waiting for BI queue"

        # 4. Download with final readiness check
        mp4_url = f"{bi_url.rstrip('/')}/clips/{relative_uri}?dl=1&session={sid}"
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
                    if (dl.status_code == 503) or (dl.status_code == 200 and cl < 1000):
                        time.sleep(2)
                        continue
                    
                    dl.raise_for_status()
                    with open(output_path, "wb") as f:
                        for chunk in dl.iter_content(8192):
                            f.write(chunk)
                    
                    final_size = os.path.getsize(output_path)
                    if final_size > 1024:
                        logging.info(
                            f"{tag} Download complete elapsed={attempt_elapsed:.1f}s size={final_size}"
                        )
                        downloaded = True
                        break
                    time.sleep(2)
            except Exception as e:
                logging.warning(f"{tag} Download error: {e}")
                time.sleep(2)

        if not downloaded:
            if target_path:
                bi_delete_clip(sess, bi_url, sid, target_path, tag)
            return False, "download failed (file not ready)"

        # 5. Cleanup
        if delete_after:
            bi_delete_clip(sess, bi_url, sid, target_path, tag)

        return True, None

    except Exception as e:
        logging.error(f"{tag} Internal monitor error: {e}")
        return False, str(e)


# =============================================================================
# Request handler
# =============================================================================

def _process_request(raw: bytes):
    try:
        req = json.loads(raw)
    except Exception:
        return
    request_id  = req.get("request_id", "unknown")
    config_name = req.get("config_name", "?")
    tag         = f"[{config_name}][{request_id[:8]}]"
    result_key  = f"bi:result:{request_id}"
    queued_at = req.get("queued_at", 0)
    if queued_at and (time.time() - queued_at) > STALE_REQUEST_AGE:
        r.rpush(result_key, json.dumps({"ok": False, "error": "stale request"}))
        r.expire(result_key, RESULT_KEY_TTL)
        return
    logging.info(f"{tag} Processing BI export request")
    try:
        ok, error_msg = _do_export(req, tag)
        result = {
            "ok": ok,
            "path": req.get("output_path") if ok else None,
            "error": error_msg
        }
    except Exception as e:
        logging.error(f"{tag} Unhandled error: {e}")
        result = {"ok": False, "error": str(e)}

    r.rpush(result_key, json.dumps(result))
    r.expire(result_key, RESULT_KEY_TTL)


# =============================================================================
# Main loop
# =============================================================================

def run_monitor(keep_running=None):
    """Blocking loop that waits for and processes requests."""
    logging.info("[bi_monitor] Waiting for requests on bi:requests")
    while keep_running is None or keep_running():
        item = r.blpop(REQUEST_QUEUE, timeout=BLPOP_BLOCK_TIMEOUT)
        if item:
            _process_request(item[1])


def main():
    logging.info("[bi_monitor] Service starting (Queue Monitor Mode)...")
    while True:
        try:
            run_monitor()
        except Exception as e:
            logging.error(f"Crashed: {e}. Restarting in 10s...")
            time.sleep(10)


if __name__ == "__main__":
    main()
