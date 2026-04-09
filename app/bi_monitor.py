#!/usr/bin/env python3
"""
Centralised Blue Iris API monitor for blueiris-ai-hub.
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
CLIPBOARD_POLL_TIMEOUT = 180
DOWNLOAD_TIMEOUT       = 120
RECOVERY_PAUSE         = 15
BLPOP_BLOCK_TIMEOUT    = 5

# =============================================================================
# Logging
# =============================================================================

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
    """Try to find alert in BI list with retries to account for indexing delays."""
    for attempt in range(3):
        try:
            json_url = urljoin(base_url.rstrip("/") + "/", "json")
            resp = sess.post(json_url, json={"cmd": "alertlist", "camera": "Index", "session": sid}, timeout=10)
            resp.raise_for_status()
            data = resp.json().get("data", [])
            for alert in data:
                if alert.get("file") == trigger_filename:
                    logging.info(f"{tag} Alert match found on attempt {attempt+1}")
                    return alert.get("clip"), alert.get("offset", 0), alert.get("msec", 10000)
            
            if attempt < 2:
                logging.warning(f"{tag} Alert not found (attempt {attempt+1}/3). Waiting 2s...")
                time.sleep(2)
        except Exception as e:
            logging.error(f"{tag} BI alert list error: {e}")
            time.sleep(1)
    return None, 0, 0


def bi_delete_clip(sess, base_url, sid, clip_id, tag):
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


def bi_wait_for_export_ready(sess, base_url, sid, export_id, tag, timeout=CLIPBOARD_POLL_TIMEOUT):
    json_url = urljoin(base_url.rstrip("/") + "/", "json")
    start = time.time()
    logging.info(f"{tag} Polling BI clipboard for export @{export_id}...")
    while time.time() - start < timeout:
        try:
            resp = sess.post(
                json_url,
                json={"cmd": "cliplist", "camera": "Index", "view": "new.clipboard", "session": sid},
                timeout=10,
            )
            if resp.status_code == 200:
                for clip in resp.json().get("data", []):
                    if export_id in clip.get("path", ""):
                        return clip.get("file")
        except Exception:
            pass
        time.sleep(2)
    return None


def trigger_bi_recovery(restart_url, restart_token, tag):
    url = (restart_url or "").strip()
    if not url:
        return False
    try:
        logging.warning(f"{tag} Stuck encoder -- calling recovery endpoint: {url}")
        resp = requests.post(url, headers={"X-Recovery-Token": restart_token or ""}, timeout=60)
        if resp.status_code == 200:
            time.sleep(RECOVERY_PAUSE)
            return True
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
    return sess, sid


def _invalidate_session(bi_url, bi_user):
    _session_cache.pop((bi_url, bi_user), None)


# =============================================================================
# Core export logic
# =============================================================================

def _do_export(req, tag):
    """Execute a single BI export request end-to-end."""
    bi_url        = req["bi_url"]
    bi_user       = req["bi_user"]
    bi_pass       = req["bi_pass"]
    trigger_file  = req["trigger_filename"]
    output_path   = req["output_path"]
    verbose       = req.get("verbose", False)
    delete_after  = req.get("delete_after", True)
    restart_url   = req.get("bi_restart_url", "")
    restart_token = req.get("bi_restart_token", "")

    sess, sid = _get_session(bi_url, bi_user, bi_pass, tag)
    if not sid:
        return False, "BI login failed"

    clip_path = req.get("clip_path")
    offset    = req.get("offset", 0)
    duration  = req.get("duration", 10000)
    
    # Resolve clip details if not provided in payload
    if clip_path:
        logging.info(f"{tag} Using pre-resolved clip: {clip_path}")
    else:
        clip_path, offset, duration = bi_find_alert_details(sess, bi_url, sid, trigger_file, tag, verbose)
        if not clip_path:
            return False, "alert not found in BI list"

    final_path = clip_path if clip_path.startswith("@") else f"@{clip_path}"
    if not final_path.endswith(".bvr"):
        final_path += ".bvr"

    export_id = None
    try:
        export_url = urljoin(bi_url.rstrip("/") + "/", "json?_export")
        payload = {
            "cmd": "export", "path": final_path,
            "startms": int(offset), "msec": int(duration),
            "format": 1, "audio": False, "session": sid,
        }

        # --- EXPORT COMMAND WITH RETRY (Handles 'OpenBVR failed') ---
        for export_attempt in range(2):
            er = sess.post(export_url, json=payload, timeout=10)
            res = er.json()
            if res.get("result") == "success":
                export_id = res.get("data", {}).get("path", "").strip().replace("@", "").replace(".mp4", "")
                break
            
            error_detail = str(res.get("data", {}))
            if "OpenBVR failed" in error_detail and export_attempt == 0:
                logging.warning(f"{tag} BI reported OpenBVR failed. Retrying in 2s...")
                time.sleep(2)
                continue
            
            return False, f"BI export command failed: {res.get('result')}"

        clipboard_path = bi_wait_for_export_ready(sess, bi_url, sid, export_id, tag)
        if not clipboard_path:
            if export_id:
                bi_delete_clip(sess, bi_url, sid, export_id, tag)
            return False, "timed out waiting for clipboard"

        mp4_url = f"{bi_url.rstrip('/')}/clips/{clipboard_path.lstrip('/')}?dl=1&session={sid}"
        logging.info(f"{tag} Clipboard ready -- beginning download.")

        downloaded = False
        dl_start = time.time()
        consecutive_503s = 0
        consecutive_404s = 0
        recovery_attempted = False

        while time.time() - dl_start < DOWNLOAD_TIMEOUT:
            try:
                with sess.get(mp4_url, stream=True, timeout=60) as dl:
                    cl = int(dl.headers.get("Content-Length", "0") or "0")
                    if dl.status_code == 503 and cl == 0:
                        consecutive_503s += 1
                        if consecutive_503s >= 30 and not recovery_attempted:
                            recovery_attempted = True
                            if trigger_bi_recovery(restart_url, restart_token, tag):
                                _invalidate_session(bi_url, bi_user)
                                return _do_export(req, tag)
                        time.sleep(2)
                        continue

                    if dl.status_code == 404:
                        consecutive_404s += 1
                        if consecutive_404s >= 20:
                            logging.error(f"{tag} Persistent 404 after 20 attempts -- failing fast")
                            break
                        time.sleep(2)
                        continue
                    
                    consecutive_404s = 0
                    dl.raise_for_status()
                    with open(output_path, "wb") as f:
                        for chunk in dl.iter_content(8192):
                            f.write(chunk)
                    if os.path.getsize(output_path) > 1024:
                        downloaded = True
                        break
            except Exception:
                time.sleep(2)

        if not downloaded:
            if export_id:
                bi_delete_clip(sess, bi_url, sid, export_id, tag)
            return False, "download failed after retries"

        if delete_after and export_id:
            bi_delete_clip(sess, bi_url, sid, export_id, tag)
        return True, None

    except Exception as e:
        return False, f"internal monitor error: {str(e)}"

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
        result = {"ok": ok, "path": req.get("output_path") if ok else None, "error": error_msg}
    except Exception as e:
        result = {"ok": False, "error": str(e)}

    r.rpush(result_key, json.dumps(result))
    r.expire(result_key, RESULT_KEY_TTL)

def run_monitor():
    while True:
        item = r.blpop(REQUEST_QUEUE, timeout=BLPOP_BLOCK_TIMEOUT)
        if item:
            _process_request(item[1])

def main():
    logging.info("[bi_monitor] Service starting...")
    while True:
        try:
            run_monitor()
        except Exception as e:
            logging.error(f"Crashed: {e}. Restarting in 10s...")
            time.sleep(10)

if __name__ == "__main__":
    main()
