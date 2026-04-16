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
import redis
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin
from logging.handlers import RotatingFileHandler
from PIL import Image
from datetime import datetime, timedelta
from bi_export_shared import EXPORT_REQUEST_QUEUE, get_session, recommended_action_for
from db_utils import connect as sqlite_connect
from settings_store import get_auto_mute_settings

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


def _resolve_logger(service_logger=None):
    return service_logger or logging


def _format_log_fields(**fields):
    ordered = []
    for key in sorted(fields):
        value = fields[key]
        if value is None or value == "":
            continue
        ordered.append(f"{key}={value}")
    return " ".join(ordered)


def log_alert_event(level, tag, message, phase, error_code=None, **extra):
    suffix = _format_log_fields(phase=phase, error_code=error_code, **extra)
    line = f"{tag} {message}"
    if suffix:
        line = f"{line} | {suffix}"
    logging.log(level, line)


def _telegram_log_fields(config, text=None, caption_source=None, caption_changed=None, message_id=None):
    fields = {}
    if caption_source:
        fields["caption_source"] = caption_source
    if caption_changed is not None:
        fields["caption_changed"] = str(bool(caption_changed)).lower()
    if text is not None:
        fields["caption_length"] = len(text)
        if config.get("verbose_logging") == 1:
            fields["caption_text"] = text
    if message_id is not None:
        fields["message_id"] = message_id
    return fields


def log_telegram_event(
    level,
    tag,
    message,
    phase,
    config,
    service_logger=None,
    error_code=None,
    text=None,
    caption_source=None,
    caption_changed=None,
    message_id=None,
    reason=None,
):
    log = _resolve_logger(service_logger)
    suffix = _format_log_fields(
        phase=phase,
        error_code=error_code,
        reason=reason,
        **_telegram_log_fields(
            config,
            text=text,
            caption_source=caption_source,
            caption_changed=caption_changed,
            message_id=message_id,
        ),
    )
    line = f"{tag} {message}"
    if suffix:
        line = f"{line} | {suffix}"
    log.log(level, line)

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
    raw = config.get('gemini_key') or ''
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
_DVLA_URL = "https://driver-vehicle-licensing.api.gov.uk/vehicle-enquiry/v1/vehicles"


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
        with sqlite_connect(DB_FILE) as conn:
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
    settings = get_auto_mute_settings()
    threshold = settings["threshold"]
    window_minutes = settings["window_minutes"]
    duration_minutes = settings["duration_minutes"]
    config_id = config['id']
    now = datetime.now()
    key = f'triggers:{config_id}'

    r.lpush(key, now.isoformat())
    r.ltrim(key, 0, threshold + 5)
    r.expire(key, (window_minutes + 1) * 60)

    entries = r.lrange(key, 0, -1)
    window_start = now - timedelta(minutes=window_minutes)
    recent = [e for e in entries if datetime.fromisoformat(e.decode()) > window_start]

    if len(recent) >= threshold:
        chat_id = config.get('chat_id', '')
        cam_name = config.get('name', '').lower()
        expiry = (now + timedelta(minutes=duration_minutes)).isoformat(timespec='seconds')
        r.set(f'mute:{cam_name}:{chat_id}', expiry, ex=duration_minutes * 60 + 60)
        r.delete(key)
        return True
    return False


def send_auto_mute_notification(config):
    settings = get_auto_mute_settings()
    cam_name = config.get('name', 'Camera')
    req_id = config.get('request_id', 'unknown')
    tag = f"[{cam_name}][{req_id}]"

    token = config['telegram_token']
    chat_id = config['chat_id']
    thread_id = config.get('message_thread_id') or ''

    text = (
        f"🔇 {cam_name} auto-muted for {settings['duration_minutes']} min "
        f"({settings['threshold']}+ triggers in {settings['window_minutes']} min)"
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
    req_id = config.get('request_id', 'unknown')
    tag = f"[{config['name']}][{req_id}]"

    start_idx = r.incr(f'gemini_key_idx:{config_id}') % len(keys)

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
    req_id = config.get('request_id', 'unknown')
    tag = f"[{config['name']}][{req_id}]"

    start_idx = r.incr(f'gemini_key_idx:{config_id}') % len(keys)

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

    req_id = config.get('request_id', 'unknown')
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

    req_id = config.get('request_id', 'unknown')
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


def optimize_video_for_telegram(input_path, output_path, tag, service_logger=None):
    log = _resolve_logger(service_logger)
    try:
        log.info(f"{tag} Optimising video...")
        subprocess.run([
            'ffmpeg', '-y', '-i', input_path,
            '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '28',
            '-r', '10', '-vf', 'scale=640:-2', '-an', '-movflags', '+faststart',
            output_path
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.path.exists(output_path):
            size_mb = os.path.getsize(output_path) / (1024 * 1024)
            log.info(f"{tag} Video optimised ({size_mb:.2f} MB)")
            return True
    except Exception as e:
        log.error(f"{tag} Video optimisation error: {e}")
    return False


# =============================================================================
# Telegram
# =============================================================================

def _tg_thread(config):
    t = config.get('message_thread_id') or ''
    return str(t) if t else None


def send_telegram(config, img_path, caption, service_logger=None):
    req_id = config.get('request_id', 'unknown')
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
                message_id = resp.json()['result']['message_id']
                config['last_msg_id'] = message_id
                log_telegram_event(
                    logging.INFO,
                    tag,
                    "Telegram photo sent",
                    "telegram_photo_sent",
                    config,
                    service_logger=service_logger,
                    text=caption,
                    caption_source="still",
                    message_id=message_id,
                )
            else:
                log_telegram_event(
                    logging.ERROR,
                    tag,
                    "Telegram photo send failed",
                    "telegram_photo_send_failed",
                    config,
                    service_logger=service_logger,
                    error_code="telegram_photo_send_failed",
                )
    except Exception:
        log_telegram_event(
            logging.ERROR,
            tag,
            "Telegram photo send error",
            "telegram_photo_send_failed",
            config,
            service_logger=service_logger,
            error_code="telegram_photo_send_failed",
        )


def update_telegram_caption(config, text, service_logger=None, caption_source="unknown", previous_text=None):
    if not config.get('last_msg_id'):
        req_id = config.get('request_id', 'unknown')
        tag = f"[{config['name']}][{req_id}]"
        log_telegram_event(
            logging.WARNING,
            tag,
            "Telegram caption update skipped; message id missing",
            "telegram_caption_update_skipped",
            config,
            service_logger=service_logger,
            error_code="telegram_message_id_missing",
            text=text,
            caption_source=caption_source,
        )
        return False

    req_id = config.get('request_id', 'unknown')
    tag = f"[{config['name']}][{req_id}]"

    token = config['telegram_token']
    chat_id = config['chat_id']
    data = {'chat_id': chat_id, 'message_id': config['last_msg_id'], 'caption': text}
    try:
        log_telegram_event(
            logging.INFO,
            tag,
            "Telegram caption update started",
            "telegram_caption_update_started",
            config,
            service_logger=service_logger,
            text=text,
            caption_source=caption_source,
            caption_changed=(previous_text != text) if previous_text is not None else None,
            message_id=config['last_msg_id'],
        )
        resp = requests.post(f"https://api.telegram.org/bot{token}/editMessageCaption", data=data, timeout=10)
        if resp.ok:
            log_telegram_event(
                logging.INFO,
                tag,
                "Telegram caption updated",
                "telegram_caption_updated",
                config,
                service_logger=service_logger,
                text=text,
                caption_source=caption_source,
                caption_changed=(previous_text != text) if previous_text is not None else None,
                message_id=config['last_msg_id'],
            )
            return True
        log_telegram_event(
            logging.ERROR,
            tag,
            "Telegram caption update failed",
            "telegram_caption_update_failed",
            config,
            service_logger=service_logger,
            error_code="telegram_caption_update_failed",
            text=text,
            caption_source=caption_source,
            caption_changed=(previous_text != text) if previous_text is not None else None,
            message_id=config['last_msg_id'],
        )
        return False
    except Exception:
        log_telegram_event(
            logging.ERROR,
            tag,
            "Telegram caption update error",
            "telegram_caption_update_failed",
            config,
            service_logger=service_logger,
            error_code="telegram_caption_update_failed",
            text=text,
            caption_source=caption_source,
            caption_changed=(previous_text != text) if previous_text is not None else None,
            message_id=config['last_msg_id'],
        )
        return False


def replace_telegram_media(config, media_path, caption, service_logger=None):
    req_id = config.get('request_id', 'unknown')
    tag = f"[{config['name']}][{req_id}]"
    token = config['telegram_token']
    chat_id = config['chat_id']
    thread_id = _tg_thread(config)
    last_msg_id = config.get('last_msg_id')

    if not last_msg_id:
        # The initial still photo send failed — no message to edit.
        # Fall back to sending the video as a new message.
        log_telegram_event(
            logging.WARNING,
            tag,
            "No still message to replace; sending video as new message",
            "telegram_video_fallback_send",
            config,
            service_logger=service_logger,
            text=caption,
            caption_source="video",
        )
        data = {'chat_id': chat_id, 'caption': caption}
        if thread_id:
            data['message_thread_id'] = thread_id
        try:
            with open(media_path, 'rb') as f:
                resp = requests.post(
                    f"https://api.telegram.org/bot{token}/sendAnimation",
                    data=data, files={'animation': f}, timeout=60
                )
                if resp.ok:
                    config['last_msg_id'] = resp.json()['result']['message_id']
                    log_telegram_event(
                        logging.INFO,
                        tag,
                        "Video sent as new message",
                        "telegram_video_sent",
                        config,
                        service_logger=service_logger,
                        text=caption,
                        caption_source="video",
                        message_id=config['last_msg_id'],
                    )
                    return True
                else:
                    log_telegram_event(
                        logging.ERROR,
                        tag,
                        "Telegram video fallback send failed",
                        "telegram_video_fallback_failed",
                        config,
                        service_logger=service_logger,
                        error_code="telegram_video_fallback_failed",
                        text=caption,
                        caption_source="video",
                    )
        except Exception:
            log_telegram_event(
                logging.ERROR,
                tag,
                "Telegram video fallback send error",
                "telegram_video_fallback_failed",
                config,
                service_logger=service_logger,
                error_code="telegram_video_fallback_failed",
                text=caption,
                caption_source="video",
            )
        return False

    # Normal path: edit the existing still photo message.
    media_json = json.dumps({"type": "animation", "media": "attach://media_file", "caption": caption})
    data = {'chat_id': chat_id, 'message_id': last_msg_id, 'media': media_json}
    try:
        log_telegram_event(
            logging.INFO,
            tag,
            "Telegram media replace started",
            "telegram_media_replace_started",
            config,
            service_logger=service_logger,
            text=caption,
            caption_source="video",
            message_id=last_msg_id,
        )
        with open(media_path, 'rb') as f:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/editMessageMedia",
                data=data, files={'media_file': f}, timeout=60
            )
            if resp.ok:
                log_telegram_event(
                    logging.INFO,
                    tag,
                    "Replaced photo with video",
                    "telegram_media_replaced",
                    config,
                    service_logger=service_logger,
                    text=caption,
                    caption_source="video",
                    message_id=last_msg_id,
                )
                return True
            else:
                log_telegram_event(
                    logging.ERROR,
                    tag,
                    "Telegram media replace failed",
                    "telegram_media_replace_failed",
                    config,
                    service_logger=service_logger,
                    error_code="telegram_media_replace_failed",
                    text=caption,
                    caption_source="video",
                    message_id=last_msg_id,
                )
    except Exception:
        log_telegram_event(
            logging.ERROR,
            tag,
            "Telegram media replace error",
            "telegram_media_replace_failed",
            config,
            service_logger=service_logger,
            error_code="telegram_media_replace_failed",
            text=caption,
            caption_source="video",
            message_id=last_msg_id,
        )
    return False


def deliver_video_to_telegram(config, raw_mp4, optimised_mp4, caption, tag, service_logger=None):
    log = _resolve_logger(service_logger)
    """
    Prepare Telegram-friendly media while Gemini analyzes the raw export.
    """
    if optimize_video_for_telegram(raw_mp4, optimised_mp4, tag, service_logger=service_logger):
        return optimised_mp4, replace_telegram_media(config, optimised_mp4, caption, service_logger=service_logger)

    log.warning(f"{tag} Video optimisation failed, sending raw MP4.")
    return raw_mp4, replace_telegram_media(config, raw_mp4, caption, service_logger=service_logger)


# =============================================================================
# Blue Iris helpers
# =============================================================================


def _bi_protocol_hash(s: str) -> str:
    """
    Blue Iris requires an MD5 digest of `user:session:password` for JSON API
    login. This is protocol interoperability, not password storage.
    """
    # Blue Iris mandates MD5 here; changing the algorithm would break auth.
    return hashlib.md5(s.encode("utf-8"), usedforsecurity=False).hexdigest()


def _parse_offset_ms(filename):
    """Extract ms offset from a BI alert filename."""
    m = re.match(r'^.+\.\d{8}_\d{6}\.(\d+)\.\d+-\d+\.\w+$', filename)
    return int(m.group(1)) if m else None


def _bi_lookup_alert(bi_url, bi_user, bi_pass, trigger_filename, tag):
    """
    Reuse the shared BI session and look up clip details for trigger_filename
    immediately, while the alert is guaranteed fresh in the alertlist.
    """
    json_url = urljoin(bi_url.rstrip("/") + "/", "json")
    sess, sid = get_session(bi_url, bi_user, bi_pass, tag)
    if not sid:
        raise RuntimeError("BI login failed")
    al = sess.post(json_url, json={"cmd": "alertlist", "camera": "Index", "session": sid}, timeout=10)
    al.raise_for_status()
    for alert in al.json().get("data", []):
        if alert.get("file") == trigger_filename:
            return alert.get("clip"), alert.get("offset", 0), alert.get("msec", 10000)
    return None


def build_bi_export_payload(config, output_path, tag, delivery_context=None):
    """Build the staged BI export request payload after resolving clip metadata."""
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
                log_alert_event(
                    logging.INFO,
                    tag,
                    "Pre-queue alert resolved",
                    "prequeue_lookup",
                    bi_instance=config["bi_url"],
                    lookup_result="resolved",
                    clip_path=clip_path,
                    offset=offset,
                    duration=duration,
                )
            elif bvr_clip:
                clip_path = bvr_clip
                offset = _parse_offset_ms(trigger_filename)
                duration = 30000
                if offset is not None:
                    log_alert_event(
                        logging.INFO,
                        tag,
                        "Alert not in alertlist; using bvr fallback",
                        "prequeue_lookup",
                        bi_instance=config["bi_url"],
                        lookup_result="bvr_fallback",
                        clip_path=clip_path,
                        offset=offset,
                        duration=duration,
                    )
                else:
                    log_alert_event(
                        logging.WARNING,
                        tag,
                        "Alert not in alertlist and offset unparseable; skipping export",
                        "prequeue_lookup",
                        error_code="offset_unparseable",
                        bi_instance=config["bi_url"],
                        lookup_result="skipped",
                        clip_path=clip_path,
                    )
                    return None
            else:
                log_alert_event(
                    logging.WARNING,
                    tag,
                    "Alert not in BI alertlist at queue time; skipping export",
                    "prequeue_lookup",
                    error_code="alert_not_found",
                    bi_instance=config["bi_url"],
                    lookup_result="skipped",
                )
                return None
        except Exception as e:
            log_alert_event(
                logging.WARNING,
                tag,
                "Pre-queue BI lookup failed",
                "prequeue_lookup",
                error_code="lookup_failed",
                bi_instance=config.get("bi_url"),
                error=_safe_request_error(e),
            )
            if bvr_clip:
                clip_path = bvr_clip
                offset = _parse_offset_ms(trigger_filename)
                duration = 30000
                if offset is not None:
                    log_alert_event(
                        logging.INFO,
                        tag,
                        "Using bvr fallback after lookup error",
                        "prequeue_lookup",
                        bi_instance=config["bi_url"],
                        lookup_result="bvr_fallback_after_error",
                        clip_path=clip_path,
                        offset=offset,
                        duration=duration,
                    )
                else:
                    log_alert_event(
                        logging.WARNING,
                        tag,
                        "bvr fallback unavailable (offset unparseable); queuing anyway",
                        "prequeue_lookup",
                        error_code="offset_unparseable",
                        bi_instance=config.get("bi_url"),
                        lookup_result="queue_without_clip",
                    )

    return {
        "request_id":       request_id,
        "alert_request_id": config.get("request_id", "unknown"),
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
        "delivery_context": delivery_context,
    }


def queue_bi_export(config, output_path, tag, delivery_context=None):
    payload = build_bi_export_payload(config, output_path, tag, delivery_context=delivery_context)
    if not payload:
        return None

    return enqueue_bi_export_payload(payload, tag)


def enqueue_bi_export_payload(payload, tag):
    request_id = payload["request_id"]
    r.rpush(EXPORT_REQUEST_QUEUE, json.dumps(payload))
    log_alert_event(
        logging.INFO,
        tag,
        "BI export request queued",
        "export_request_queued",
        queue=EXPORT_REQUEST_QUEUE,
    )
    return request_id


def analyze_image_parallel(config, encoded_image, prompt):
    """Run all configured AI providers concurrently; return the first successful result."""
    providers = []
    if (config.get('gemini_key') or '').strip():
        providers.append(lambda: analyze_image_gemini(config, encoded_image, prompt))
    if (config.get('grok_api_key') or '').strip():
        providers.append(lambda: analyze_image_grok(config, encoded_image, prompt))
    if (config.get('groq_api_key') or '').strip():
        providers.append(lambda: analyze_image_groq(config, encoded_image, prompt))
    if not providers:
        return None
    ex = ThreadPoolExecutor(max_workers=len(providers))
    try:
        futures = {ex.submit(fn): fn for fn in providers}
        for future in as_completed(futures):
            try:
                result = future.result()
                if result:
                    return result
            except Exception as e:
                logging.warning(f"[parallel AI] provider error: {e}")
    finally:
        ex.shutdown(wait=False)  # Don't block on remaining providers once we have a result
    return None


# =============================================================================
# Main entry point
# =============================================================================

def process_alert(image_path, config):
    req_id = config.get('request_id', 'unknown')
    tag = f"[{config['name']}][{req_id}]"
    raw_mp4 = None
    optimised_mp4 = None
    final_status = "unknown"
    summary_error_code = None
    try:
        log_alert_event(logging.INFO, tag, "Processing alert...", "alert_processing_started")

        if is_muted(config):
            final_status = "muted"
            log_alert_event(logging.INFO, tag, "Muted; skipping.", "alert_skipped", final_status=final_status)
            return

        if check_auto_mute(config):
            final_status = "auto_muted"
            send_auto_mute_notification(config)
            return

        current_time = datetime.now().strftime("%I:%M %p")
        prompt = f"Current time: {current_time}. {build_prompt(config)}"

        encoded = optimize_image(image_path)
        instant_notify = config.get('instant_notify') == 1

        ai_text = analyze_image_parallel(config, encoded, prompt) if encoded else None

        still_caption = ai_text or "Motion detected."
        export_payload_future = None
        if (
            config.get('send_video') == 1
            and config.get('trigger_filename')
            and config.get('bi_url')
            and config.get('bi_user')
            and config.get('bi_pass')
        ):
            raw_mp4 = image_path.replace(".jpg", "_raw.mp4")
            export_prepare_executor = ThreadPoolExecutor(max_workers=1)
            export_payload_future = export_prepare_executor.submit(
                build_bi_export_payload,
                config,
                raw_mp4,
                tag,
            )
        else:
            export_prepare_executor = None

        if instant_notify:
            send_telegram(config, image_path, "Motion detected.")
            if ai_text:
                update_telegram_caption(
                    config,
                    still_caption,
                    caption_source="still",
                    previous_text="Motion detected.",
                )
        else:
            send_telegram(config, image_path, still_caption)

        # DVLA enrichment after Telegram send — edits the caption if plates are found (#66)
        enriched_still = enrich_caption_with_dvla(still_caption, config, tag, image_path=image_path)
        if (config.get("dvla_api_key") or "").strip():
            log_telegram_event(
                logging.INFO,
                tag,
                "DVLA still-caption enrichment complete",
                "dvla_caption_enriched",
                config,
                text=enriched_still,
                caption_source="dvla",
                caption_changed=(enriched_still != still_caption),
                message_id=config.get("last_msg_id"),
            )
        if enriched_still != still_caption:
            update_telegram_caption(
                config,
                enriched_still,
                caption_source="dvla",
                previous_text=still_caption,
            )
        still_caption = enriched_still

        if config.get('tv_push_enabled') == 1 and config.get('tv_rtsp_url'):
            import tv_delivery

            tv_delivery.dispatch_tv_alert(
                {
                    "id": config.get("id"),
                    "name": config.get("name"),
                    "request_id": config.get("request_id"),
                    "tv_rtsp_url": config.get("tv_rtsp_url"),
                    "tv_duration_seconds": config.get("tv_duration_seconds"),
                    "tv_group": config.get("tv_group"),
                },
                tag,
            )

        # Video handling
        if config.get('send_video') == 1 and config.get('trigger_filename'):
            if config.get('bi_url') and config.get('bi_user') and config.get('bi_pass'):
                delivery_context = {
                    "config": {
                        "id": config["id"],
                        "name": config["name"],
                        "request_id": config.get("request_id", "unknown"),
                        "telegram_token": config["telegram_token"],
                        "chat_id": config["chat_id"],
                        "message_thread_id": config.get("message_thread_id"),
                        "last_msg_id": config.get("last_msg_id"),
                        "dvla_api_key": config.get("dvla_api_key", ""),
                        "gemini_key": config.get("gemini_key", ""),
                        "verbose_logging": config.get("verbose_logging", 0),
                    },
                    "prompt": prompt,
                    "still_caption": still_caption,
                }
                payload = export_payload_future.result() if export_payload_future else build_bi_export_payload(
                    config,
                    raw_mp4,
                    tag,
                )
                if export_prepare_executor is not None:
                    export_prepare_executor.shutdown(wait=False)

                request_id = None
                if payload:
                    payload["delivery_context"] = delivery_context
                    request_id = enqueue_bi_export_payload(payload, tag)

                if not request_id:
                    final_status = "photo_only"
                    summary_error_code = "video_export_unavailable"
                    log_alert_event(
                        logging.WARNING,
                        tag,
                        "Video export failed, keeping photo.",
                        "video_queue_result",
                        error_code=summary_error_code,
                        final_status=final_status,
                    )
                else:
                    final_status = "still_sent_video_queued"
                    log_alert_event(
                        logging.INFO,
                        tag,
                        "Video export queued for asynchronous delivery",
                        "video_queue_result",
                        final_status=final_status,
                    )
                    raw_mp4 = None
            else:
                if export_prepare_executor is not None:
                    export_prepare_executor.shutdown(wait=False)
                final_status = "photo_only"
                summary_error_code = "bi_credentials_missing"
                log_alert_event(
                    logging.WARNING,
                    tag,
                    "Send video enabled but BI credentials missing.",
                    "video_queue_result",
                    error_code=summary_error_code,
                    final_status=final_status,
                )
        elif final_status == "unknown":
            if export_prepare_executor is not None:
                export_prepare_executor.shutdown(wait=False)
            final_status = "photo_only"

    except Exception as e:
        final_status = "task_failed"
        summary_error_code = "task_exception"
        logging.error(f"{tag} Task failed: {_safe_request_error(e)}")
    finally:
        log_alert_event(
            logging.INFO,
            tag,
            "Alert processing summary",
            "alert_summary",
            error_code=summary_error_code,
            final_status=final_status,
            recommended_action=recommended_action_for(summary_error_code),
            send_video=config.get('send_video') == 1,
            trigger_filename=bool(config.get('trigger_filename')),
        )
        for p in [image_path, raw_mp4, optimised_mp4]:
            if p and os.path.exists(p):
                os.remove(p)
