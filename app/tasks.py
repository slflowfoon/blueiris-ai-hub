import os
import re
import time
import logging
import requests
import base64
import io
import json
import hashlib
import subprocess
import sqlite3
import redis
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin
from logging.handlers import RotatingFileHandler
from PIL import Image
from datetime import datetime, timedelta

# --- LOGGING SETUP ---
LOG_FILE = os.getenv("LOG_FILE", "/app/logs/system.log")
if os.path.dirname(LOG_FILE):
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
DATA_DIR = os.getenv("DATA_DIR", "/app/data")
DB_FILE = os.path.join(DATA_DIR, "configs.db")
KNOWN_PLATES_FILE = f"{DATA_DIR}/known_plates.json"
PLATE_IMAGES_DIR = os.path.join(DATA_DIR, "plate_images")

GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.5-flash-lite"]
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"

AUTO_MUTE_WINDOW_MINUTES = 10
AUTO_MUTE_THRESHOLD = 5
AUTO_MUTE_DURATION_MINUTES = 30

CAPTION_PROMPTS = {
    "normal": None,
    "hilarious": (
        "The CCTV has detected motion. Describe what's happening in a single outrageously "
        "funny sentence (max 145 characters). Be dramatic and absurd — narrate it like a "
        "nature documentary gone completely wrong. Include vehicles, people, or deliveries."
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

def _safe_request_error(exc):
    """
    Summarise request failures without logging secrets from URLs, headers, or
    provider error messages.
    """
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None):
        return f"{type(exc).__name__} (status {response.status_code})"
    return type(exc).__name__


def get_api_keys(config):
    raw = config.get('gemini_key', '')
    return [k.strip() for k in raw.split(',') if k.strip()]


def load_known_plates():
    try:
        if os.path.exists(KNOWN_PLATES_FILE):
            with open(KNOWN_PLATES_FILE, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return {}


_PLATE_RE = re.compile(
    r'\b([A-Z]{2}[0-9]{2}\s?[A-Z]{3}'  # Current (2001+):  AB12 ABC
    r'|[A-Z][0-9]{1,3}\s?[A-Z]{3}'     # Prefix (1983-01): A123 BCD
    r'|[A-Z]{3}\s?[0-9]{1,3}[A-Z])\b'  # Suffix (1963-83): ABC 123D
)
_DVLA_URL = "https://driver-vehicle-licensing.api.gov.uk/vehicle-enquiries/v1/vehicles"


def _save_plate_thumbnail(image_path, plate):
    try:
        os.makedirs(PLATE_IMAGES_DIR, exist_ok=True)
        filename = f"{plate}_{uuid.uuid4().hex[:8]}.jpg"
        with Image.open(image_path) as img:
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            img.thumbnail((400, 400))
            img.save(os.path.join(PLATE_IMAGES_DIR, filename), format="JPEG", quality=80)
        return filename
    except Exception as e:
        logging.warning(f"Plate thumbnail save failed for {plate}: {e}")
        return None


def _audit_plate(plate, dvla_data, camera_name, image_path, tag):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    image_filename = _save_plate_thumbnail(image_path, plate) if image_path else None
    try:
        with sqlite3.connect(DB_FILE) as conn:
            existing = conn.execute(
                "SELECT id, image_filename FROM plate_audit WHERE plate=?", (plate,)
            ).fetchone()
            if existing:
                old_filename = existing[1]
                img_to_save = image_filename or old_filename
                if image_filename and old_filename and image_filename != old_filename:
                    old_path = os.path.join(PLATE_IMAGES_DIR, old_filename)
                    if os.path.exists(old_path):
                        os.remove(old_path)
                conn.execute(
                    "UPDATE plate_audit SET last_seen=?, seen_count=seen_count+1, "
                    "image_filename=?, dvla_make=?, dvla_colour=?, dvla_year=?, "
                    "dvla_tax_status=?, dvla_tax_due=?, dvla_mot_status=?, "
                    "dvla_mot_expiry=?, dvla_checked_at=? WHERE plate=?",
                    (now, img_to_save,
                     dvla_data.get('make', '').title(), dvla_data.get('colour', '').title(),
                     dvla_data.get('yearOfManufacture'), dvla_data.get('taxStatus'),
                     dvla_data.get('taxDueDate'), dvla_data.get('motStatus'),
                     dvla_data.get('motExpiryDate'), now, plate)
                )
            else:
                conn.execute(
                    "INSERT INTO plate_audit (id, plate, first_seen, last_seen, seen_count, "
                    "camera_name, image_filename, dvla_make, dvla_colour, dvla_year, "
                    "dvla_tax_status, dvla_tax_due, dvla_mot_status, dvla_mot_expiry, dvla_checked_at) "
                    "VALUES (?,?,?,?,1,?,?,?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), plate, now, now, camera_name, image_filename,
                     dvla_data.get('make', '').title(), dvla_data.get('colour', '').title(),
                     dvla_data.get('yearOfManufacture'), dvla_data.get('taxStatus'),
                     dvla_data.get('taxDueDate'), dvla_data.get('motStatus'),
                     dvla_data.get('motExpiryDate'), now)
                )
    except Exception as e:
        if tag:
            logging.warning(f"{tag} Plate audit write failed for {plate}: {e}")


def enrich_caption_with_dvla(caption, config, tag="", image_path=None):
    dvla_key = (config.get('dvla_api_key') or '').strip()
    if not dvla_key or not caption:
        return caption
    known = load_known_plates()
    camera_name = config.get('name', '')
    for match in _PLATE_RE.finditer(caption.upper()):
        plate_raw = match.group(1)
        plate = plate_raw.replace(' ', '')
        if plate in known:
            continue
        try:
            resp = requests.post(
                _DVLA_URL,
                headers={'x-api-key': dvla_key, 'Content-Type': 'application/json'},
                json={'registrationNumber': plate},
                timeout=10,
            )
            if resp.status_code == 200:
                d = resp.json()
                make   = d.get('make', '').title()
                colour = d.get('colour', '').title()
                year   = d.get('yearOfManufacture', '')
                suffix = f" ({make}, {colour}, {year})"
                _audit_plate(plate, d, camera_name, image_path, tag)
            elif resp.status_code == 404:
                suffix = " (unverified)"
                _audit_plate(plate, {'taxStatus': 'unverified'}, camera_name, image_path, tag)
            else:
                if tag:
                    logging.warning(f"{tag} DVLA lookup returned {resp.status_code} for {plate}")
                continue
            caption = caption.replace(plate_raw, plate_raw + suffix, 1)
        except Exception as e:
            if tag:
                logging.warning(f"{tag} DVLA lookup error for {plate}: {e}")
    return caption


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
    req_id = config.get('request_id', 'legacy')
    tag = f"[{cam_name}][{req_id}]"

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
        logging.error(f"{tag} Auto-mute notification error: {e}")


# =============================================================================
# Gemini — image
# =============================================================================

def analyze_image_gemini(config, encoded_image, prompt):
    keys = get_api_keys(config)
    if not keys:
        return None

    config_id = config['id']
    req_id = config.get('request_id', 'legacy')
    tag = f"[{config['name']}][{req_id}]"

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
                logging.warning(f"{tag} Gemini {model} error {resp.status_code}")
                continue
        except Exception as e:
            logging.error(f"{tag} Gemini request error: {_safe_request_error(e)}")
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
    req_id = config.get('request_id', 'legacy')
    tag = f"[{config['name']}][{req_id}]"

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
                logging.warning(f"{tag} Upload failed with status {upload_resp.status_code}")
                continue

            file_info = upload_resp.json().get('file', {})
            file_uri = file_info.get('uri')
            file_name = file_info.get('name')

            if not file_uri or not file_name:
                logging.warning(f"{tag} No file URI/name in upload response")
                continue

            # Poll until ACTIVE
            logging.info(f"{tag} Gemini upload accepted. Polling for ACTIVE state...")
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
                        logging.info(f"{tag} Video analysis succeeded with {model}")
                        return result
                    elif resp.status_code == 429:
                        continue
                    else:
                        logging.warning(f"{tag} Video analysis {model} error {resp.status_code}")
                        continue
                except Exception as e:
                    logging.error(f"{tag} Video analysis error: {_safe_request_error(e)}")
                    continue

        except Exception as e:
            logging.error(f"{tag} Gemini video error: {_safe_request_error(e)}")
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

    req_id = config.get('request_id', 'legacy')
    tag = f"[{config['name']}][{req_id}]"

    try:
        logging.info(f"{tag} Trying Grok fallback...")
        resp = requests.post(
            "https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": "grok-4-0709", "max_tokens": 200, "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded_image}"}}
            ]}]},
            timeout=30
        )
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"].strip()
        logging.warning(f"{tag} Grok error {resp.status_code}: {resp.text[:100]}")
    except Exception as e:
        logging.error(f"{tag} Grok error: {_safe_request_error(e)}")
    return None


def analyze_image_groq(config, encoded_image, prompt):
    api_key = config.get('groq_api_key') or ''
    if not api_key:
        return None

    req_id = config.get('request_id', 'legacy')
    tag = f"[{config['name']}][{req_id}]"

    try:
        logging.info(f"{tag} Trying Groq fallback...")
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": "meta-llama/llama-4-scout-17b-16e-instruct", "max_tokens": 200, "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded_image}"}}
            ]}]},
            timeout=30
        )
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"].strip()
        logging.warning(f"{tag} Groq error {resp.status_code}: {resp.text[:100]}")
    except Exception as e:
        logging.error(f"{tag} Groq error: {_safe_request_error(e)}")
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
    req_id = config.get('request_id', 'legacy')
    tag = f"[{config['name']}][{req_id}]"

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

    req_id = config.get('request_id', 'legacy')
    tag = f"[{config['name']}][{req_id}]"

    token = config['telegram_token']
    chat_id = config['chat_id']
    data = {'chat_id': chat_id, 'message_id': config['last_msg_id'], 'caption': text}
    try:
        requests.post(f"https://api.telegram.org/bot{token}/editMessageCaption", data=data, timeout=10)
    except Exception as e:
        logging.error(f"{tag} Caption update error: {e}")


def replace_telegram_media(config, media_path, caption):
    req_id = config.get('request_id', 'legacy')
    tag = f"[{config['name']}][{req_id}]"

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


def deliver_video_to_telegram(config, raw_mp4, optimised_mp4, caption, tag):
    """
    Prepare Telegram-friendly media while Gemini analyzes the raw export.
    """
    if optimize_video_for_telegram(raw_mp4, optimised_mp4, tag):
        replace_telegram_media(config, optimised_mp4, caption)
        return optimised_mp4

    logging.warning(f"{tag} Video optimisation failed, sending raw MP4.")
    replace_telegram_media(config, raw_mp4, caption)
    return raw_mp4


# =============================================================================
# Blue Iris helpers
# =============================================================================


def _bi_protocol_hash(s: str) -> str:
    """MD5 hex digest required by the Blue Iris JSON API (protocol interop only)."""
    return hashlib.md5(s.encode("utf-8"), usedforsecurity=False).hexdigest()


def _parse_offset_ms(filename):
    """Extract ms offset from a BI alert filename."""
    m = re.match(r'^.+\.\d{8}_\d{6}\.(\d+)\.\d+-\d+\.\w+$', filename)
    return int(m.group(1)) if m else None


def _bi_lookup_alert(bi_url, bi_user, bi_pass, trigger_filename, tag):
    """
    Login to BI and look up clip details for trigger_filename immediately,
    while the alert is guaranteed fresh in the alertlist.
    """
    json_url = urljoin(bi_url.rstrip("/") + "/", "json")
    sess = requests.Session()
    r1 = sess.post(json_url, json={"cmd": "login"}, timeout=10)
    r1.raise_for_status()
    sid = r1.json().get("session")
    response = _bi_protocol_hash(f"{bi_user}:{sid}:{bi_pass}")
    r2 = sess.post(json_url, json={"cmd": "login", "session": sid, "response": response}, timeout=10)
    r2.raise_for_status()
    if r2.json().get("result") != "success":
        raise RuntimeError("BI login failed")
    al = sess.post(json_url, json={"cmd": "alertlist", "camera": "Index", "session": sid}, timeout=10)
    al.raise_for_status()
    for alert in al.json().get("data", []):
        if alert.get("file") == trigger_filename:
            return alert.get("clip"), alert.get("offset", 0), alert.get("msec", 10000)
    return None


def request_bi_export(config, output_path, tag, timeout=300):
    """
    Queue a BI export request to the bi_monitor service and block until done.
    Returns True on success, False on failure.
    """
    request_id = str(uuid.uuid4())

    # Pre-queue alertlist lookup: resolve clip details while the alert is fresh.
    clip_path = offset = duration = None
    trigger_filename = config.get("trigger_filename", "")
    bvr_clip = config.get("bvr_clip", "")
    if trigger_filename and config.get("bi_url") and config.get("bi_user"):
        try:
            result = _bi_lookup_alert(
                config["bi_url"], config["bi_user"], config["bi_pass"],
                trigger_filename, tag,
            )
            if result is not None:
                clip_path, offset, duration = result
                logging.info(f"{tag} Pre-queue alert resolved: clip={clip_path}")
            elif bvr_clip:
                clip_path = bvr_clip
                offset = _parse_offset_ms(trigger_filename)
                duration = 30000
                if offset is not None:
                    logging.info(f"{tag} Alert not in alertlist — bvr fallback: clip={clip_path} offset={offset}")
                else:
                    logging.warning(f"{tag} Alert not in alertlist and offset unparseable — skipping export")
                    return False
            else:
                logging.warning(f"{tag} Alert not in BI alertlist at queue time -- skipping export")
                return False
        except Exception as e:
            logging.warning(f"{tag} Pre-queue BI lookup failed ({e})")
            if bvr_clip:
                clip_path = bvr_clip
                offset = _parse_offset_ms(trigger_filename)
                duration = 30000
                if offset is not None:
                    logging.info(f"{tag} Using bvr fallback after lookup error: clip={clip_path} offset={offset}")
                else:
                    logging.warning(f"{tag} bvr fallback unavailable (offset unparseable) -- queuing anyway")

    payload = json.dumps({
        "request_id":       request_id,
        "config_name":      config.get("name", "?"),
        "bi_url":           config["bi_url"],
        "bi_user":          config["bi_user"],
        "bi_pass":          config["bi_pass"],
        "trigger_filename": trigger_filename,
        "clip_path":        clip_path,
        "offset":           offset,
        "duration":         duration,
        "output_path":      output_path,
        "bi_restart_url":   config.get("bi_restart_url", ""),
        "bi_restart_token": config.get("bi_restart_token", ""),
        "verbose":          config.get("verbose_logging") == 1,
        "delete_after":     config.get("delete_after_send") == 1,
        "queued_at":        time.time(),
    })
    r.rpush("bi:requests", payload)
    logging.info(f"{tag} BI export request queued (id={request_id})")

    result_key = f"bi:result:{request_id}"
    item = r.blpop(result_key, timeout=timeout)
    if item is None:
        logging.error(f"{tag} BI monitor timed out after {timeout}s")
        return False

    result = json.loads(item[1])
    if result.get("ok"):
        return True

    error_msg = result.get("error", "unknown monitor error")
    logging.error(f"{tag} BI monitor returned failure: {error_msg}")
    return False


# =============================================================================
# Image / Still fallback analysis
# =============================================================================

def analyze_still_image_fallback(image_path, prompt, config):
    """PLAN C: Fallback to Still image AI models if Gemini Video fails."""
    encoded = optimize_image(image_path)
    if not encoded:
        return None
    ai_text = analyze_image_gemini(config, encoded, prompt)
    if not ai_text:
        ai_text = analyze_image_grok(config, encoded, prompt)
    if not ai_text:
        ai_text = analyze_image_groq(config, encoded, prompt)
    return ai_text


# =============================================================================
# Main entry point
# =============================================================================

def process_alert(image_path, config):
    req_id = config.get('request_id', 'legacy')
    tag = f"[{config['name']}][{req_id}]"
    try:
        logging.info(f"{tag} Processing alert...")

        if is_muted(config):
            logging.info(f"{tag} Muted — skipping.")
            return

        if check_auto_mute(config):
            send_auto_mute_notification(config)
            return

        current_time = datetime.now().strftime("%I:%M %p")
        prompt = f"Current time: {current_time}. {build_prompt(config)}"

        encoded = optimize_image(image_path)
        instant_notify = config.get('instant_notify') == 1

        ai_text = None
        if encoded:
            ai_text = analyze_image_gemini(config, encoded, prompt)
            if not ai_text:
                ai_text = analyze_image_grok(config, encoded, prompt)
            if not ai_text:
                ai_text = analyze_image_groq(config, encoded, prompt)

        still_caption = ai_text or "Motion detected."

        if instant_notify:
            if config.get('initial_msg_id'):
                config['last_msg_id'] = config['initial_msg_id']
            else:
                send_telegram(config, image_path, "Motion detected.")
            if ai_text:
                update_telegram_caption(config, still_caption)
        else:
            if config.get('initial_msg_id'):
                config['last_msg_id'] = config['initial_msg_id']
            else:
                send_telegram(config, image_path, still_caption)

        # DVLA enrichment after Telegram send — edits the caption if plates are found (#66)
        enriched_still = enrich_caption_with_dvla(still_caption, config, tag, image_path=image_path)
        if enriched_still != still_caption:
            update_telegram_caption(config, enriched_still)
        still_caption = enriched_still

        # Video handling
        if config.get('send_video') == 1 and config.get('trigger_filename'):
            if config.get('bi_url') and config.get('bi_user') and config.get('bi_pass'):
                raw_mp4 = image_path.replace(".jpg", "_raw.mp4")
                optimised_mp4 = image_path.replace(".jpg", ".mp4")
                success = request_bi_export(config, raw_mp4, tag)

                if success:
                    with ThreadPoolExecutor(max_workers=2) as executor:
                        delivery_future = executor.submit(
                            deliver_video_to_telegram,
                            config,
                            raw_mp4,
                            optimised_mp4,
                            still_caption,
                            tag,
                        )
                        video_caption_future = executor.submit(
                            analyze_video_gemini,
                            config,
                            raw_mp4,
                            prompt,
                        )

                        delivery_future.result()
                        video_caption = video_caption_future.result()

                    if video_caption:
                        update_telegram_caption(config, enrich_caption_with_dvla(video_caption, config, tag, image_path=image_path))
                    else:
                        logging.info(f"{tag} Video analysis failed, falling back to image caption.")
                        fallback_text = analyze_still_image_fallback(image_path, prompt, config)
                        if fallback_text:
                            logging.info(f"{tag} Successfully recovered using Still Image fallback.")
                            update_telegram_caption(config, enrich_caption_with_dvla(fallback_text, config, tag, image_path=image_path))

                    for f_path in [optimised_mp4, raw_mp4]:
                        if os.path.exists(f_path):
                            os.remove(f_path)
                else:
                    logging.warning(f"{tag} Video export failed, keeping photo.")
            else:
                logging.warning(f"{tag} Send video enabled but BI credentials missing.")

    except Exception as e:
        logging.error(f"{tag} Task failed: {e}")
    finally:
        if os.path.exists(image_path):
            os.remove(image_path)
