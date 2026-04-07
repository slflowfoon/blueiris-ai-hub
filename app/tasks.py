import os
import time
import logging
import requests
import base64
import io
import json
import hashlib
import subprocess
import redis
from urllib.parse import urljoin
from logging.handlers import RotatingFileHandler
from PIL import Image
from datetime import datetime, timedelta

# --- LOGGING SETUP ---
LOG_FILE = "/app/logs/system.log"
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
handler = RotatingFileHandler(LOG_FILE, maxBytes=1000000, backupCount=1)
handler.setFormatter(formatter)
logger = logging.getLogger()
logger.setLevel(logging.INFO)
logger.addHandler(handler)

# --- REDIS ---
redis_url = os.getenv('REDIS_URL', 'redis://redis:6379/0')
r = redis.from_url(redis_url)

# --- CONSTANTS ---
DATA_DIR = "/app/data"
KNOWN_PLATES_FILE = f"{DATA_DIR}/known_plates.json"

GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.0-flash-lite"]
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"

AUTO_MUTE_WINDOW_MINUTES = 10
AUTO_MUTE_THRESHOLD = 5
AUTO_MUTE_DURATION_MINUTES = 30

CAPTION_PROMPTS = {
    "normal": None,  # uses config prompt
    "hilarious": (
        "The CCTV has detected motion. Describe what's happening in a single outrageously "
        "funny sentence (max 145 characters). Be dramatic and absurd — narrate it like a "
        "nature documentary gone completely wrong. Include vehicles, people, or deliveries you can see."
    ),
    "witty": (
        "The CCTV has detected motion. Describe what's happening in a single witty, sardonic "
        "sentence (max 145 characters). Dry wit and clever observations only — think bored "
        "private detective. Include vehicles, people, or deliveries visible."
    ),
    "rude": (
        "The CCTV has detected motion. Describe what's happening in a single cheeky, rude "
        "sentence (max 145 characters). British crude humour — sarcastic, irreverent, mildly "
        "offensive but not hateful. Include vehicles, people, or deliveries visible."
    ),
}


# =============================================================================
# Helpers
# =============================================================================

def get_api_keys(config):
    raw = config.get('gemini_key', '')
    return [k.strip() for k in raw.split(',') if k.strip()]


def load_known_plates():
    try:
        if os.path.exists(KNOWN_PLATES_FILE):
            return json.loads(open(KNOWN_PLATES_FILE).read())
    except Exception:
        pass
    return {}


def build_prompt(config):
    chat_id = config.get('chat_id', '')
    mode = 'normal'
    cm = r.get(f'caption_mode:{chat_id}')
    if cm:
        try:
            data = json.loads(cm)
            expires = datetime.fromisoformat(data['expires'])
            if expires > datetime.now():
                mode = data.get('mode', 'normal')
        except Exception:
            pass

    plates = load_known_plates()
    plate_hint = "; ".join(f"{p} = {n}" for p, n in plates.items()) if plates else ""
    plate_note = f" Known plates: {plate_hint}." if plate_hint else ""

    if mode != 'normal' and mode in CAPTION_PROMPTS:
        return CAPTION_PROMPTS[mode] + plate_note

    base = config.get('prompt', 'Describe any motion detected by this CCTV camera in one sentence.')
    return base + plate_note


def is_muted(config):
    chat_id = config.get('chat_id', '')
    cam_name = config.get('name', '').lower()
    for key in [f'mute:all:{chat_id}', f'mute:{cam_name}:{chat_id}']:
        val = r.get(key)
        if val:
            try:
                if datetime.fromisoformat(val.decode()) > datetime.now():
                    return True
                r.delete(key)
            except Exception:
                r.delete(key)
    return False


def check_auto_mute(config):
    """Track trigger frequency. Returns True if auto-mute was just triggered."""
    config_id = config['id']
    now = datetime.now()
    key = f'triggers:{config_id}'

    r.lpush(key, now.isoformat())
    r.ltrim(key, 0, AUTO_MUTE_THRESHOLD + 5)
    r.expire(key, (AUTO_MUTE_WINDOW_MINUTES + 1) * 60)

    entries = r.lrange(key, 0, -1)
    window_start = now - timedelta(minutes=AUTO_MUTE_WINDOW_MINUTES)
    recent = [e for e in entries if datetime.fromisoformat(e.decode()) > window_start]

    if len(recent) >= AUTO_MUTE_THRESHOLD:
        chat_id = config.get('chat_id', '')
        cam_name = config.get('name', '').lower()
        expiry = (now + timedelta(minutes=AUTO_MUTE_DURATION_MINUTES)).isoformat(timespec='seconds')
        r.set(f'mute:{cam_name}:{chat_id}', expiry, ex=AUTO_MUTE_DURATION_MINUTES * 60 + 60)
        r.delete(key)
        return True
    return False


def send_auto_mute_notification(config):
    cam_name = config.get('name', 'Camera')
    token = config['telegram_token']
    chat_id = config['chat_id']
    thread_id = config.get('message_thread_id') or ''

    text = (
        f"🔇 {cam_name} auto-muted for {AUTO_MUTE_DURATION_MINUTES} min "
        f"({AUTO_MUTE_THRESHOLD}+ triggers in {AUTO_MUTE_WINDOW_MINUTES} min)"
    )
    keyboard = {"inline_keyboard": [[{
        "text": "Remove Mute",
        "callback_data": f"unmute:{cam_name.lower()}:{chat_id}"
    }]]}
    data = {'chat_id': chat_id, 'text': text, 'reply_markup': json.dumps(keyboard)}
    if thread_id:
        data['message_thread_id'] = thread_id
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=10)
    except Exception as e:
        logging.error(f"[{cam_name}] Auto-mute notification error: {e}")


# =============================================================================
# Gemini — image
# =============================================================================

def analyze_image_gemini(config, encoded_image, prompt):
    keys = get_api_keys(config)
    if not keys:
        return None

    config_id = config['id']
    tag = f"[{config['name']}]"
    start_idx = int(r.get(f'gemini_key_idx:{config_id}') or 0)

    for attempt in range(len(keys) * len(GEMINI_MODELS)):
        key_i = (start_idx + attempt // len(GEMINI_MODELS)) % len(keys)
        model = GEMINI_MODELS[attempt % len(GEMINI_MODELS)]
        key = keys[key_i]

        url = f"{GEMINI_API_BASE}/models/{model}:generateContent?key={key}"
        payload = {"contents": [{"parts": [
            {"text": prompt},
            {"inline_data": {"mime_type": "image/jpeg", "data": encoded_image}}
        ]}]}
        try:
            logging.info(f"{tag} Gemini image: key {key_i + 1}/{len(keys)}, {model}")
            resp = requests.post(url, json=payload, timeout=30)
            if resp.status_code == 200:
                r.set(f'gemini_key_idx:{config_id}', (key_i + 1) % len(keys))
                return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            elif resp.status_code == 429:
                logging.warning(f"{tag} Rate limited: key {key_i + 1}, {model}")
                continue
            else:
                logging.warning(f"{tag} Gemini {model} error {resp.status_code}: {resp.text[:120]}")
                continue
        except Exception as e:
            logging.error(f"{tag} Gemini request error: {e}")
            continue
    return None


# =============================================================================
# Gemini — video via Files API
# =============================================================================

def analyze_video_gemini(config, video_path, prompt):
    keys = get_api_keys(config)
    if not keys:
        return None

    config_id = config['id']
    tag = f"[{config['name']}]"
    start_idx = int(r.get(f'gemini_key_idx:{config_id}') or 0)

    for ki in range(len(keys)):
        key_i = (start_idx + ki) % len(keys)
        key = keys[key_i]
        file_name = None

        try:
            file_size = os.path.getsize(video_path)
            logging.info(f"{tag} Uploading video to Gemini Files API (key {key_i + 1})...")

            upload_url = f"https://generativelanguage.googleapis.com/upload/v1beta/files?key={key}"
            with open(video_path, 'rb') as f:
                upload_resp = requests.post(
                    upload_url,
                    headers={
                        'X-Goog-Upload-Command': 'start, upload, finalize',
                        'X-Goog-Upload-Header-Content-Length': str(file_size),
                        'X-Goog-Upload-Header-Content-Type': 'video/mp4',
                        'Content-Type': 'video/mp4',
                    },
                    data=f,
                    timeout=120
                )

            if upload_resp.status_code not in (200, 201):
                logging.warning(f"{tag} Upload failed: {upload_resp.status_code} {upload_resp.text[:100]}")
                continue

            file_info = upload_resp.json().get('file', {})
            file_uri = file_info.get('uri')
            file_name = file_info.get('name')

            if not file_uri or not file_name:
                logging.warning(f"{tag} No file URI/name in upload response")
                continue

            # Poll until ACTIVE
            logging.info(f"{tag} Uploaded: {file_uri}. Polling for ACTIVE state...")
            active = False
            for _ in range(20):
                state_resp = requests.get(f"{GEMINI_API_BASE}/{file_name}?key={key}", timeout=10)
                if state_resp.status_code == 200:
                    state = state_resp.json().get('state')
                    if state == 'ACTIVE':
                        active = True
                        break
                    elif state == 'FAILED':
                        logging.warning(f"{tag} File processing failed")
                        break
                time.sleep(2)

            if not active:
                continue

            # Analyze
            for model in GEMINI_MODELS:
                url = f"{GEMINI_API_BASE}/models/{model}:generateContent?key={key}"
                payload = {"contents": [{"parts": [
                    {"text": prompt},
                    {"file_data": {"mime_type": "video/mp4", "file_uri": file_uri}}
                ]}]}
                try:
                    resp = requests.post(url, json=payload, timeout=60)
                    if resp.status_code == 200:
                        r.set(f'gemini_key_idx:{config_id}', (key_i + 1) % len(keys))
                        result = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                        logging.info(f"{tag} Video analysis result ({model}): {result}")
                        return result
                    elif resp.status_code == 429:
                        continue
                    else:
                        logging.warning(f"{tag} Video analysis {model} error {resp.status_code}")
                        continue
                except Exception as e:
                    logging.error(f"{tag} Video analysis error: {e}")
                    continue

        except Exception as e:
            logging.error(f"{tag} Gemini video error: {e}")
        finally:
            if file_name:
                try:
                    requests.delete(f"{GEMINI_API_BASE}/{file_name}?key={key}", timeout=10)
                except Exception:
                    pass

    return None


# =============================================================================
# Grok / Groq fallbacks
# =============================================================================

def analyze_image_grok(config, encoded_image, prompt):
    api_key = config.get('grok_api_key') or ''
    if not api_key:
        return None
    tag = f"[{config['name']}]"
    try:
        logging.info(f"{tag} Trying Grok fallback...")
        resp = requests.post(
            "https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": "grok-2-vision-1212", "max_tokens": 200, "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded_image}"}}
            ]}]},
            timeout=30
        )
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"].strip()
        logging.warning(f"{tag} Grok error {resp.status_code}: {resp.text[:100]}")
    except Exception as e:
        logging.error(f"{tag} Grok error: {e}")
    return None


def analyze_image_groq(config, encoded_image, prompt):
    api_key = config.get('groq_api_key') or ''
    if not api_key:
        return None
    tag = f"[{config['name']}]"
    try:
        logging.info(f"{tag} Trying Groq fallback...")
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": "llama-3.2-11b-vision-preview", "max_tokens": 200, "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded_image}"}}
            ]}]},
            timeout=30
        )
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"].strip()
        logging.warning(f"{tag} Groq error {resp.status_code}: {resp.text[:100]}")
    except Exception as e:
        logging.error(f"{tag} Groq error: {e}")
    return None


# =============================================================================
# Image / video processing
# =============================================================================

def optimize_image(image_path):
    try:
        with Image.open(image_path) as img:
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            img.thumbnail((1024, 1024))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            return base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception as e:
        logging.error(f"Image optimization error: {e}")
        return None


def optimize_video_for_telegram(input_path, output_path, tag):
    try:
        logging.info(f"{tag} Optimising video...")
        subprocess.run([
            'ffmpeg', '-y', '-i', input_path,
            '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '28',
            '-r', '10', '-vf', 'scale=854:-2', '-an', '-movflags', '+faststart',
            output_path
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.path.exists(output_path):
            size_mb = os.path.getsize(output_path) / (1024 * 1024)
            logging.info(f"{tag} Video optimised ({size_mb:.2f} MB)")
            return True
    except Exception as e:
        logging.error(f"{tag} Video optimisation error: {e}")
    return False


# =============================================================================
# Telegram
# =============================================================================

def _tg_thread(config):
    t = config.get('message_thread_id') or ''
    return str(t) if t else None


def send_telegram(config, img_path, caption):
    tag = f"[{config['name']}]"
    token = config['telegram_token']
    chat_id = config['chat_id']
    thread_id = _tg_thread(config)

    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    data = {'chat_id': chat_id, 'caption': caption}
    if thread_id:
        data['message_thread_id'] = thread_id
    try:
        with open(img_path, 'rb') as f:
            resp = requests.post(url, files={'photo': f}, data=data, timeout=15)
            if resp.ok:
                config['last_msg_id'] = resp.json()['result']['message_id']
            else:
                logging.error(f"{tag} Telegram send error: {resp.text}")
    except Exception as e:
        logging.error(f"{tag} Telegram error: {e}")


def update_telegram_caption(config, text):
    if 'last_msg_id' not in config:
        return
    token = config['telegram_token']
    chat_id = config['chat_id']
    data = {'chat_id': chat_id, 'message_id': config['last_msg_id'], 'caption': text}
    try:
        requests.post(f"https://api.telegram.org/bot{token}/editMessageCaption", data=data, timeout=10)
    except Exception as e:
        logging.error(f"[{config['name']}] Caption update error: {e}")


def replace_telegram_media(config, media_path, caption):
    tag = f"[{config['name']}]"
    if 'last_msg_id' not in config:
        return
    token = config['telegram_token']
    chat_id = config['chat_id']
    media_json = json.dumps({"type": "animation", "media": "attach://media_file", "caption": caption})
    data = {'chat_id': chat_id, 'message_id': config['last_msg_id'], 'media': media_json}
    try:
        with open(media_path, 'rb') as f:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/editMessageMedia",
                data=data, files={'media_file': f}, timeout=60
            )
            if resp.ok:
                logging.info(f"{tag} Replaced photo with video")
            else:
                logging.error(f"{tag} Replace media error: {resp.text}")
    except Exception as e:
        logging.error(f"{tag} Replace media error: {e}")


# =============================================================================
# Blue Iris helpers (unchanged from v1)
# =============================================================================

def md5_hex(s):
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def bi_login(sess, base_url, user, password, tag):
    try:
        json_url = urljoin(base_url.rstrip("/") + "/", "json")
        r1 = sess.post(json_url, json={"cmd": "login"}, timeout=10)
        r1.raise_for_status()
        sid = r1.json().get("session")
        resp = md5_hex(f"{user}:{sid}:{password}")
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
        r = sess.post(json_url, json={"cmd": "alertlist", "camera": "Index", "session": sid}, timeout=10)
        r.raise_for_status()
        data = r.json().get("data", [])
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
        r = sess.post(json_url, json={"cmd": "delclip", "path": f"@{clean}", "session": sid}, timeout=10)
        if r.json().get("result") == "success":
            logging.info(f"{tag} Deleted clip @{clean}")
            return True
    except Exception as e:
        logging.error(f"{tag} Delete clip error: {e}")
    return False


def bi_wait_for_export_ready(sess, base_url, sid, export_id, tag, timeout=180):
    json_url = urljoin(base_url.rstrip("/") + "/", "json")
    start = time.time()
    logging.info(f"{tag} Polling BI clipboard for export @{export_id}...")
    while time.time() - start < timeout:
        try:
            r = sess.post(json_url, json={"cmd": "cliplist", "camera": "Index", "view": "new.clipboard", "session": sid}, timeout=10)
            if r.status_code == 200:
                for clip in r.json().get("data", []):
                    if export_id in clip.get("path", ""):
                        return clip.get("file")
        except Exception:
            pass
        time.sleep(2)
    return None


def bi_export_and_download(base_url, user, password, trigger_filename, output_path, tag, verbose=False, delete_after=True, config=None):
    sess = requests.Session()
    sid = bi_login(sess, base_url, user, password, tag)
    if not sid:
        return False

    clip_path, offset, duration = bi_find_alert_details(sess, base_url, sid, trigger_filename, tag, verbose)
    if not clip_path:
        logging.error(f"{tag} Cannot export: alert not found")
        return False

    final_path = clip_path if clip_path.startswith("@") else f"@{clip_path}"
    if not final_path.endswith(".bvr"):
        final_path += ".bvr"

    try:
        export_url = urljoin(base_url.rstrip("/") + "/", "json?_export")
        payload = {"cmd": "export", "path": final_path, "startms": int(offset),
                   "msec": int(duration), "format": 1, "audio": False, "session": sid}
        if verbose:
            logging.info(f"{tag} VERBOSE export payload: {json.dumps(payload)}")
        r = sess.post(export_url, json=payload, timeout=10)
        if r.json().get("result") != "success":
            logging.error(f"{tag} Export failed: {r.json()}")
            return False

        export_id = r.json().get("data", {}).get("path", "").strip().replace("@", "").replace(".mp4", "")
        clipboard_path = bi_wait_for_export_ready(sess, base_url, sid, export_id, tag)
        if not clipboard_path:
            logging.error(f"{tag} Export timed out")
            bi_delete_clip(sess, base_url, sid, export_id, tag)
            return False

        mp4_url = f"{base_url.rstrip('/')}/clips/{clipboard_path.lstrip('/')}?dl=1&session={sid}"
        logging.info(f"{tag} [TIMING] Clipboard ready — attempting download immediately. URL={mp4_url}")

        # --- TEMPORARY DIAGNOSTIC LOGGING ---
        # Log clipprocess from status to see if BI signals encoding state
        try:
            json_url = urljoin(base_url.rstrip("/") + "/", "json")
            st = sess.post(json_url, json={"cmd": "status", "session": sid}, timeout=10)
            clipprocess = st.json().get("data", {}).get("clipprocess", "?")
            logging.info(f"{tag} [TIMING] BI status.clipprocess={clipprocess!r}")
        except Exception as e:
            logging.warning(f"{tag} [TIMING] status check error: {e}")

        # Try HEAD first to see what BI returns before we attempt a full download
        try:
            head = sess.head(mp4_url, timeout=10)
            logging.info(f"{tag} [TIMING] HEAD status={head.status_code} Content-Length={head.headers.get('Content-Length','?')} headers={dict(head.headers)}")
        except Exception as e:
            logging.warning(f"{tag} [TIMING] HEAD error: {e}")
        # --- END TEMPORARY DIAGNOSTIC LOGGING ---

        downloaded = False
        dl_start = time.time()
        attempt = 0
        consecutive_503s = 0
        recovery_attempted = False

        while time.time() - dl_start < 120:
            attempt += 1
            elapsed = time.time() - dl_start
            try:
                with sess.get(mp4_url, stream=True, timeout=60) as dl:
                    cl = int(dl.headers.get('Content-Length', '0') or '0')
                    logging.info(f"{tag} [TIMING] attempt={attempt} elapsed={elapsed:.1f}s status={dl.status_code} Content-Length={cl}")

                    if dl.status_code == 503 and cl == 0:
                        consecutive_503s += 1
                        # Stuck encoder: 30 consecutive empty 503s (~60s) with no progress
                        if consecutive_503s >= 30 and not recovery_attempted and config:
                            recovery_attempted = True
                            if trigger_bi_recovery(config, tag):
                                sess2 = requests.Session()
                                sid2 = bi_login(sess2, base_url, user, password, tag)
                                if sid2:
                                    sess = sess2
                                    sid = sid2
                                    er2 = sess.post(
                                        f"{base_url.rstrip('/')}/json?_export",
                                        json={'cmd': 'export', 'path': final_path,
                                              'startms': int(offset), 'msec': int(duration),
                                              'format': 1, 'audio': False, 'session': sid2},
                                        timeout=10
                                    )
                                    new_id = er2.json().get('data', {}).get('path', '').strip().replace('@', '').replace('.mp4', '')
                                    if new_id:
                                        new_clipboard = bi_wait_for_export_ready(sess, base_url, sid2, new_id, tag)
                                        if new_clipboard:
                                            mp4_url = f"{base_url.rstrip('/')}/clips/{new_clipboard.lstrip('/')}?dl=1&session={sid2}"
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
                    with open(output_path, 'wb') as f:
                        for chunk in dl.iter_content(8192):
                            f.write(chunk)
                    size = os.path.getsize(output_path)
                    logging.info(f"{tag} [TIMING] Download complete at elapsed={elapsed:.1f}s size={size}")
                    if size > 1024:
                        downloaded = True
                        break
                    time.sleep(2)
            except Exception as e:
                logging.warning(f"{tag} [TIMING] attempt={attempt} elapsed={elapsed:.1f}s error: {e}")
                time.sleep(2)

        if not downloaded:
            logging.error(f"{tag} Download failed after retries")
            bi_delete_clip(sess, base_url, sid, export_id, tag)
            return False

        if delete_after:
            bi_delete_clip(sess, base_url, sid, export_id, tag)
        return True

    except Exception as e:
        logging.error(f"{tag} Export/download error: {e}")
        return False


# =============================================================================
# BI encoder recovery
# =============================================================================

def trigger_bi_recovery(config, tag):
    """POST to bi_restart_url to recover a stuck Blue Iris encoder."""
    url = (config.get('bi_restart_url') or '').strip()
    token = (config.get('bi_restart_token') or '').strip()
    if not url:
        logging.warning(f"{tag} Stuck encoder detected but no bi_restart_url configured.")
        return False
    try:
        logging.warning(f"{tag} Stuck encoder detected — calling recovery endpoint: {url}")
        resp = requests.post(url, headers={'X-Recovery-Token': token}, timeout=60)
        if resp.status_code == 200:
            logging.info(f"{tag} BI recovery OK — waiting 15s for BI to restart...")
            import time as _t; _t.sleep(15)
            return True
        logging.error(f"{tag} BI recovery returned {resp.status_code}: {resp.text[:100]}")
    except Exception as e:
        logging.error(f"{tag} BI recovery error: {e}")
    return False


# =============================================================================
# Main entry point
# =============================================================================

def process_alert(image_path, config):
    tag = f"[{config['name']}]"
    try:
        logging.info(f"{tag} Processing alert...")

        # Check mute before doing anything
        if is_muted(config):
            logging.info(f"{tag} Muted — skipping.")
            return

        # Auto-mute burst detection
        if check_auto_mute(config):
            send_auto_mute_notification(config)
            return

        # Build prompt with caption mode + plates
        current_time = datetime.now().strftime("%I:%M %p")
        prompt = f"Current time: {current_time}. {build_prompt(config)}"

        # Analyse still image first, then send with real caption
        encoded = optimize_image(image_path)
        ai_text = None
        if encoded:
            ai_text = analyze_image_gemini(config, encoded, prompt)
            if not ai_text:
                ai_text = analyze_image_grok(config, encoded, prompt)
            if not ai_text:
                ai_text = analyze_image_groq(config, encoded, prompt)

        still_caption = ai_text or "Motion detected."

        # Send photo with real caption (no placeholder)
        if config.get('initial_msg_id'):
            config['last_msg_id'] = config['initial_msg_id']
        else:
            send_telegram(config, image_path, still_caption)

        # Video handling
        if config.get('send_video') == 1 and config.get('trigger_filename'):
            if config.get('bi_url') and config.get('bi_user') and config.get('bi_pass'):
                raw_mp4 = image_path.replace(".jpg", "_raw.mp4")
                optimised_mp4 = image_path.replace(".jpg", ".mp4")
                verbose = config.get('verbose_logging') == 1
                delete_after = config.get('delete_after_send') == 1

                success = bi_export_and_download(
                    config['bi_url'], config['bi_user'], config['bi_pass'],
                    config['trigger_filename'], raw_mp4, tag, verbose, delete_after,
                    config=config
                )

                if success:
                    # Replace photo with video immediately (using still caption for now)
                    if optimize_video_for_telegram(raw_mp4, optimised_mp4, tag):
                        replace_telegram_media(config, optimised_mp4, still_caption)
                    else:
                        logging.warning(f"{tag} Video optimisation failed, sending raw MP4.")
                        replace_telegram_media(config, raw_mp4, still_caption)

                    # Now analyse video with Gemini and update caption when ready
                    video_caption = analyze_video_gemini(config, raw_mp4, prompt)
                    if video_caption:
                        update_telegram_caption(config, video_caption)

                    if os.path.exists(optimised_mp4):
                        os.remove(optimised_mp4)
                    if os.path.exists(raw_mp4):
                        os.remove(raw_mp4)
                else:
                    logging.warning(f"{tag} Video export failed, keeping photo.")
            else:
                logging.warning(f"{tag} Send video enabled but BI credentials missing.")

    except Exception as e:
        logging.error(f"{tag} Task failed: {e}")
    finally:
        if os.path.exists(image_path):
            os.remove(image_path)
