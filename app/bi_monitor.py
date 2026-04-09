#!/usr/bin/env python3
"""
Centralised Blue Iris API monitor for blueiris-ai-hub.

Single-process service that serialises all BI API calls, eliminating
clipboard contention when multiple workers attempt concurrent video exports.

Protocol:
  Worker  -> RPUSH bi:requests <json payload>
  Monitor -> BLPOP bi:requests (blocking)
  Monitor -> RPUSH bi:result:<request_id> <json result>
  Worker  -> BLPOP bi:result:<request_id> (blocking, 300s timeout)
"""

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
RESULT_KEY_TTL         = 60    # seconds — cleanup if worker died before reading
STALE_REQUEST_AGE      = 600   # seconds — skip requests queued >10 min ago
CLIPBOARD_POLL_TIMEOUT = 180   # seconds — bi_wait_for_export_ready
DOWNLOAD_TIMEOUT       = 120   # seconds — download retry loop
RECOVERY_PAUSE         = 15    # seconds — wait after BI service restart
BLPOP_BLOCK_TIMEOUT    = 5     # seconds — allows periodic keepalive between requests

# =============================================================================
# Logging
# =============================================================================

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    handlers=[RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=1)],
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

# =============================================================================
# Redis
# =============================================================================

r = redis.from_url(REDIS_URL)

# =============================================================================
# BI protocol helpers  (moved verbatim from tasks.py)
# =============================================================================

def _bi_protocol_hash(s: str) -> str:
    """MD5 hex digest required by the Blue Iris JSON API (protocol interop only)."""
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
    try:
        json_url = urljoin(base_url.rstrip("/") + "/", "json")
        resp = sess.post(json_url, json={"cmd": "alertlist", "camera": "Index", "session": sid}, timeout=10)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        if verbose:
            logging.info(f"{tag} VERBOSE alert list: {json.dumps(data)}")
        for alert in data:
            if alert.get("file") == trigger_filename:
                logging.info(f"{tag} Alert match: {alert.get('clip')}")
                return alert.get("clip"), alert.get("offset", 0), alert.get("msec", 10000)
        logging.warning(f"{tag} No alert found for: {trigger_filename}")
    except Exception as e:
        logging.error(f"{tag} BI alert list error: {e}")
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
    """POST to the Windows bi_recovery.py endpoint to restart the BI service."""
    url = (restart_url or "").strip()
    if not url:
        logging.warning(f"{tag} Stuck encoder detected but no bi_restart_url configured.")
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
# Session cache  { (bi_url, bi_user): (requests.Session, sid) }
# =============================================================================

_session_cache: dict = {}


def _get_session(bi_url, bi_user, bi_pass, tag):
    """Return a cached (sess, sid) pair, logging in fresh if not cached or expired."""
    key = (bi_url, bi_user)
    cached = _session_cache.get(key)
    if cached:
        sess, sid = cached
        try:
            json_url = urljoin(bi_url.rstrip("/") + "/", "json")
            chk = sess.post(json_url, json={"cmd": "status", "session": sid}, timeout=10)
            if chk.status_code == 200 and chk.json().get("result") == "success":
                logging.info(f"{tag} Reusing cached BI session for {bi_user}@{bi_url}")
                return sess, sid
        except Exception:
            pass
        logging.info(f"{tag} Cached BI session expired -- re-logging in")
        _invalidate_session(bi_url, bi_user)

    sess = requests.Session()
    sid = bi_login(sess, bi_url, bi_user, bi_pass, tag)
    if sid:
        _session_cache[key] = (sess, sid)
        logging.info(f"{tag} New BI session cached for {bi_user}@{bi_url}")
    return sess, sid


def _invalidate_session(bi_url, bi_user):
    _session_cache.pop((bi_url, bi_user), None)


# =============================================================================
# Core export logic
# =============================================================================

def _do_export(req, tag):
    """
    Execute a single BI export request end-to-end.
    Returns True on success, False on failure.
    """
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
        return False

    clip_path = req.get("clip_path")
    offset    = req.get("offset", 0)
    duration  = req.get("duration", 10000)
    if clip_path:
        logging.info(f"{tag} Using pre-resolved clip: {clip_path}")
    else:
        clip_path, offset, duration = bi_find_alert_details(
            sess, bi_url, sid, trigger_file, tag, verbose
        )
        if not clip_path:
            logging.error(f"{tag} Cannot export: alert not found")
            return False

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
        if verbose:
            logging.info(f"{tag} VERBOSE export payload: {json.dumps(payload)}")

        er = sess.post(export_url, json=payload, timeout=10)
        if er.json().get("result") != "success":
            logging.error(f"{tag} Export command failed: {er.json()}")
            return False

        export_id = er.json().get("data", {}).get("path", "").strip().replace("@", "").replace(".mp4", "")
        clipboard_path = bi_wait_for_export_ready(sess, bi_url, sid, export_id, tag)
        if not clipboard_path:
            logging.error(f"{tag} Export timed out waiting for clipboard")
            if export_id:
                bi_delete_clip(sess, bi_url, sid, export_id, tag)
            return False

        mp4_url = f"{bi_url.rstrip('/')}/clips/{clipboard_path.lstrip('/')}?dl=1&session={sid}"
        logging.info(f"{tag} Clipboard ready -- beginning download. URL={mp4_url}")

        downloaded = False
        dl_start = time.time()
        attempt = 0
        consecutive_503s = 0
        recovery_attempted = False

        while time.time() - dl_start < DOWNLOAD_TIMEOUT:
            attempt += 1
            elapsed = time.time() - dl_start
            try:
                with sess.get(mp4_url, stream=True, timeout=60) as dl:
                    cl = int(dl.headers.get("Content-Length", "0") or "0")
                    logging.info(
                        f"{tag} attempt={attempt} elapsed={elapsed:.1f}s"
                        f" status={dl.status_code} Content-Length={cl}"
                    )

                    if dl.status_code == 503 and cl == 0:
                        consecutive_503s += 1
                        if consecutive_503s >= 30 and not recovery_attempted:
                            recovery_attempted = True
                            if trigger_bi_recovery(restart_url, restart_token, tag):
                                _invalidate_session(bi_url, bi_user)
                                sess2 = requests.Session()
                                sid2 = bi_login(sess2, bi_url, bi_user, bi_pass, tag)
                                if sid2:
                                    _session_cache[(bi_url, bi_user)] = (sess2, sid2)
                                    sess, sid = sess2, sid2
                                    er2 = sess.post(
                                        f"{bi_url.rstrip('/')}/json?_export",
                                        json={"cmd": "export", "path": final_path,
                                              "startms": int(offset), "msec": int(duration),
                                              "format": 1, "audio": False, "session": sid2},
                                        timeout=10,
                                    )
                                    _raw = er2.json().get("data", {}).get("path", "")
                                    new_id = _raw.strip().replace("@", "").replace(".mp4", "")
                                    if new_id:
                                        new_clipboard = bi_wait_for_export_ready(sess, bi_url, sid2, new_id, tag)
                                        if new_clipboard:
                                            mp4_url = (
                                                f"{bi_url.rstrip('/')}/clips/"
                                                f"{new_clipboard.lstrip('/')}?dl=1&session={sid2}"
                                            )
                                            export_id = new_id
                                            consecutive_503s = 0
                                            logging.info(f"{tag} Re-export after recovery ready: {mp4_url}")
                        time.sleep(2)
                        continue

                    consecutive_503s = 0
                    if dl.status_code == 404:
                        time.sleep(2)
                        continue
                    dl.raise_for_status()
                    with open(output_path, "wb") as f:
                        for chunk in dl.iter_content(8192):
                            f.write(chunk)
                    size = os.path.getsize(output_path)
                    logging.info(f"{tag} Download complete elapsed={elapsed:.1f}s size={size}")
                    if size > 1024:
                        downloaded = True
                        break
                    time.sleep(2)
            except Exception as e:
                logging.warning(f"{tag} attempt={attempt} elapsed={elapsed:.1f}s error: {e}")
                time.sleep(2)

        if not downloaded:
            logging.error(f"{tag} Download failed after retries")
            if export_id:
                bi_delete_clip(sess, bi_url, sid, export_id, tag)
            return False

        if delete_after and export_id:
            bi_delete_clip(sess, bi_url, sid, export_id, tag)
        return True

    except Exception as e:
        logging.error(f"{tag} Export/download error: {e}")
        if export_id:
            try:
                bi_delete_clip(sess, bi_url, sid, export_id, tag)
            except Exception:
                pass
        return False


# =============================================================================
# Request handler
# =============================================================================

def _process_request(raw: bytes):
    try:
        req = json.loads(raw)
    except Exception as e:
        logging.error(f"[bi_monitor] Malformed request: {e}")
        return

    request_id  = req.get("request_id", "unknown")
    config_name = req.get("config_name", "?")
    tag         = f"[{config_name}][{request_id[:8]}]"
    result_key  = f"bi:result:{request_id}"

    # Stale request guard: worker has already timed out, no point processing
    queued_at = req.get("queued_at", 0)
    if queued_at and (time.time() - queued_at) > STALE_REQUEST_AGE:
        age = int(time.time() - queued_at)
        logging.warning(f"{tag} Skipping stale request (age={age}s > {STALE_REQUEST_AGE}s)")
        r.rpush(result_key, json.dumps({"ok": False, "error": f"stale ({age}s old)"}))
        r.expire(result_key, RESULT_KEY_TTL)
        return

    logging.info(f"{tag} Processing BI export request")
    try:
        ok = _do_export(req, tag)
        result = {
            "ok": ok,
            "path": req.get("output_path") if ok else None,
            "error": None if ok else "export failed",
        }
    except Exception as e:
        logging.error(f"{tag} Unhandled error in _do_export: {e}")
        result = {"ok": False, "error": str(e)}

    r.rpush(result_key, json.dumps(result))
    r.expire(result_key, RESULT_KEY_TTL)
    logging.info(f"{tag} Result pushed: ok={result['ok']}")


# =============================================================================
# Main loop
# =============================================================================

def run_monitor():
    logging.info("[bi_monitor] Waiting for requests on bi:requests")
    while True:
        item = r.blpop(REQUEST_QUEUE, timeout=BLPOP_BLOCK_TIMEOUT)
        if item is not None:
            _, raw = item
            _process_request(raw)


def main():
    logging.info("[bi_monitor] Service starting...")
    while True:
        try:
            run_monitor()
        except Exception as e:
            logging.error(f"[bi_monitor] Crashed: {e}. Restarting in 10s...")
            time.sleep(10)


if __name__ == "__main__":
    main()
