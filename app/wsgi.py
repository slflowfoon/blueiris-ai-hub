import os
import uuid
import json
import re
import sqlite3
from urllib.parse import quote, unquote, urlsplit, urlunsplit
import redis
import logging
import requests
from datetime import datetime
from collections import deque
from rq import Queue
from flask import Flask, request, jsonify, render_template_string, redirect, url_for, flash, send_file, abort
from db_utils import connect as sqlite_connect
from bi_export_shared import ACTIVE_EXPORT_SET, DOWNLOAD_REQUEST_QUEUE, EXPORT_REQUEST_QUEUE, VIDEO_DELIVERY_QUEUE, iter_job_ids, load_job
from service_health import HEARTBEAT_STALE_AFTER, heartbeat_status
from settings_store import (
    get_global_settings,
    init_global_settings,
    save_global_settings,
)
from tasks import process_alert
from werkzeug.utils import secure_filename
from bi_mjpg import bi_mjpg_bp

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "super_secret_key_change_this")
app.register_blueprint(bi_mjpg_bp)

BASE_URL = os.getenv('BASE_URL')
if BASE_URL and BASE_URL.endswith('/'):
    BASE_URL = BASE_URL[:-1]

redis_url = os.getenv('REDIS_URL', 'redis://redis:6379/0')
r = redis.from_url(redis_url)
q = Queue(connection=r)

DATA_DIR = os.getenv("DATA_DIR", "/app/data")
DB_FILE = os.path.join(DATA_DIR, "configs.db")
KNOWN_PLATES_FILE = os.path.join(DATA_DIR, "known_plates.json")
PLATE_IMAGES_DIR = os.path.join(DATA_DIR, "plate_images")
TEMP_IMAGE_DIR = os.getenv("TEMP_IMAGE_DIR", "/tmp_images")
LOG_FILE = os.getenv("LOG_FILE", "/app/logs/system.log")
LOG_DIR = os.path.dirname(LOG_FILE) or "/app/logs"
TV_OVERLAY_APK_URL = (os.getenv("TV_OVERLAY_APK_URL") or "").strip()

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(PLATE_IMAGES_DIR, exist_ok=True)
os.makedirs(TEMP_IMAGE_DIR, exist_ok=True)
if os.path.dirname(LOG_FILE):
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

app.logger.setLevel(logging.INFO)

# --- VERSION ---
try:
    with open('/app/VERSION') as _f:
        CURRENT_VERSION = _f.read().strip()
except Exception:
    CURRENT_VERSION = "unknown"

GITHUB_REPO = "slflowfoon/blueiris-ai-hub"
UPDATE_CHECK_CACHE_KEY = "hub_update_check"
UPDATE_CHECK_TTL = 900 

def get_update_status():
    """Logic to fetch latest release from GitHub and compare versions."""
    latest = None
    release_url = ""
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
            headers={"Accept": "application/vnd.github+json"},
            timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()
            latest = data["tag_name"].lstrip("v")
            release_url = data.get("html_url", "")
    except Exception as e:
        app.logger.error(f"Update check failed: {e}")
        return {"update_available": False, "latest_version": None}

    if not latest or CURRENT_VERSION in ["main", "dev", "unknown"]:
        return {
            "update_available": False, 
            "latest_version": latest, 
            "current_version": CURRENT_VERSION,
            "release_url": release_url
        }

    try:
        def parse_version(v):
            return tuple(map(int, v.lstrip('v').split('.')))
        local_v = parse_version(CURRENT_VERSION)
        remote_v = parse_version(latest)
        update_needed = remote_v > local_v
    except Exception:
        update_needed = latest.lstrip('v') != CURRENT_VERSION.lstrip('v')

    return {
        "update_available": update_needed,
        "latest_version": latest,
        "current_version": CURRENT_VERSION,
        "release_url": release_url
    }

# --- DATABASE ---

def get_db_connection():
    return sqlite_connect(DB_FILE, row_factory=sqlite3.Row)


def get_redis_client():
    return r


def _parse_tv_duration_seconds(value):
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if 5 <= parsed <= 120:
        return parsed
    return None


def init_db():
    with sqlite_connect(DB_FILE) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS configs (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                gemini_key TEXT NOT NULL,
                telegram_token TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                prompt TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_triggered TIMESTAMP,
                bi_url TEXT,
                bi_user TEXT,
                bi_pass TEXT,
                send_video INTEGER DEFAULT 0,
                verbose_logging INTEGER DEFAULT 0,
                delete_after_send INTEGER DEFAULT 1,
                message_thread_id TEXT,
                grok_api_key TEXT,
                groq_api_key TEXT,
                bi_restart_url TEXT,
                bi_restart_token TEXT,
                instant_notify INTEGER DEFAULT 0,
                dvla_api_key TEXT,
                tv_push_enabled INTEGER DEFAULT 0,
                tv_rtsp_url TEXT,
                tv_duration_seconds INTEGER,
                tv_group TEXT,
                tv_mute_audio INTEGER DEFAULT 0
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_configs_chat_id ON configs(chat_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_configs_created_at ON configs(created_at)")
        # Migrations for existing installs
        for col, definition in [
            ("last_triggered", "TIMESTAMP"),
            ("bi_url", "TEXT"), ("bi_user", "TEXT"), ("bi_pass", "TEXT"),
            ("send_video", "INTEGER DEFAULT 0"),
            ("verbose_logging", "INTEGER DEFAULT 0"),
            ("delete_after_send", "INTEGER DEFAULT 1"),
            ("message_thread_id", "TEXT"),
            ("grok_api_key", "TEXT"),
            ("groq_api_key", "TEXT"),
            ("bi_restart_url", "TEXT"),
            ("bi_restart_token", "TEXT"),
            ("instant_notify", "INTEGER DEFAULT 0"),
            ("dvla_api_key", "TEXT"),
            ("tv_push_enabled", "INTEGER DEFAULT 0"),
            ("tv_rtsp_url", "TEXT"),
            ("tv_duration_seconds", "INTEGER"),
            ("tv_group", "TEXT"),
            ("tv_mute_audio", "INTEGER DEFAULT 0"),
            ("tv_stream_type", "TEXT DEFAULT 'rtsp'"),
        ]:
            try:
                conn.execute(f"SELECT {col} FROM configs LIMIT 1")
            except sqlite3.OperationalError:
                try:
                    conn.execute(f"ALTER TABLE configs ADD COLUMN {col} {definition}")
                except sqlite3.OperationalError:
                    pass  # Another worker added the column concurrently
        conn.execute("""
            CREATE TABLE IF NOT EXISTS plate_audit (
                id TEXT PRIMARY KEY,
                plate TEXT NOT NULL,
                first_seen TIMESTAMP NOT NULL,
                last_seen TIMESTAMP NOT NULL,
                seen_count INTEGER DEFAULT 1,
                camera_name TEXT,
                image_filename TEXT,
                dvla_make TEXT,
                dvla_colour TEXT,
                dvla_year INTEGER,
                dvla_tax_status TEXT,
                dvla_tax_due TEXT,
                dvla_mot_status TEXT,
                dvla_mot_expiry TEXT,
                dvla_checked_at TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_plate_audit_plate ON plate_audit(plate)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS paired_tvs (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                ip_address TEXT,
                port INTEGER DEFAULT 7979,
                rtsp_url TEXT,
                shared_secret TEXT,
                device_token_id TEXT,
                last_seen_at TIMESTAMP,
                last_ip_seen TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        paired_tv_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(paired_tvs)").fetchall()
        }
        for col, definition in [
            ("ip_address", "TEXT"),
            ("port", "INTEGER DEFAULT 7979"),
            ("rtsp_url", "TEXT"),
            ("shared_secret", "TEXT"),
            ("device_token_id", "TEXT"),
            ("last_seen_at", "TIMESTAMP"),
            ("last_ip_seen", "TEXT"),
        ]:
            if col not in paired_tv_columns:
                conn.execute(f"ALTER TABLE paired_tvs ADD COLUMN {col} {definition}")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS camera_tv_targets (
                id TEXT PRIMARY KEY,
                camera_id TEXT,
                camera_name TEXT NOT NULL,
                tv_id TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        camera_tv_target_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(camera_tv_targets)").fetchall()
        }
        if "camera_id" not in camera_tv_target_columns:
            try:
                conn.execute("ALTER TABLE camera_tv_targets ADD COLUMN camera_id TEXT")
            except sqlite3.OperationalError:
                pass
        conn.execute("""
            CREATE TABLE IF NOT EXISTS camera_group_priorities (
                id TEXT PRIMARY KEY,
                camera_id TEXT,
                camera_name TEXT,
                group_name TEXT NOT NULL,
                priority INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        group_priority_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(camera_group_priorities)").fetchall()
        }
        for col, definition in [
            ("camera_id", "TEXT"),
            ("camera_name", "TEXT"),
        ]:
            if col not in group_priority_columns:
                try:
                    conn.execute(f"ALTER TABLE camera_group_priorities ADD COLUMN {col} {definition}")
                except sqlite3.OperationalError:
                    pass

        resolved_camera_ids_by_name = {
            row[0]: row[1]
            for row in conn.execute(
                """
                SELECT name, id
                FROM configs
                WHERE name IS NOT NULL
                GROUP BY name
                HAVING COUNT(*) = 1
                """
            ).fetchall()
        }
        legacy_targets = conn.execute(
            """
            SELECT id, camera_name
            FROM camera_tv_targets
            WHERE camera_id IS NULL
            """
        ).fetchall()
        camera_id_updates = [
            (resolved_camera_ids_by_name[row[1]], row[0])
            for row in legacy_targets
            if row[1] in resolved_camera_ids_by_name
        ]
        if camera_id_updates:
            conn.executemany(
                "UPDATE camera_tv_targets SET camera_id=? WHERE id=?",
                camera_id_updates,
            )
        legacy_priorities = conn.execute(
            """
            SELECT id, camera_name
            FROM camera_group_priorities
            WHERE camera_id IS NULL
            """
        ).fetchall()
        priority_updates = [
            (resolved_camera_ids_by_name[row[1]], row[0])
            for row in legacy_priorities
            if row[1] in resolved_camera_ids_by_name
        ]
        if priority_updates:
            conn.executemany(
                "UPDATE camera_group_priorities SET camera_id=? WHERE id=?",
                priority_updates,
            )

        rows = conn.execute(
            """
            SELECT id, device_token_id
            FROM paired_tvs
            WHERE device_token_id IS NOT NULL
            ORDER BY device_token_id, COALESCE(created_at, '') DESC, id DESC
            """
        ).fetchall()
        seen_tokens = set()
        duplicate_ids = []
        keep_id_by_token = {}
        replacement_by_id = {}
        for row in rows:
            tv_id = row[0]
            token = row[1]
            if token in seen_tokens:
                duplicate_ids.append(tv_id)
                replacement_by_id[tv_id] = keep_id_by_token[token]
            else:
                seen_tokens.add(token)
                keep_id_by_token[token] = tv_id
        if replacement_by_id:
            conn.executemany(
                "UPDATE camera_tv_targets SET tv_id=? WHERE tv_id=?",
                [(keep_id, discard_id) for discard_id, keep_id in replacement_by_id.items()],
            )
        target_rows = conn.execute(
            """
            SELECT id, camera_id, tv_id
            FROM camera_tv_targets
            WHERE camera_id IS NOT NULL
            ORDER BY camera_id, tv_id, COALESCE(created_at, ''), id
            """
        ).fetchall()
        seen_target_pairs = set()
        duplicate_target_ids = []
        for row in target_rows:
            target_key = (row[1], row[2])
            if target_key in seen_target_pairs:
                duplicate_target_ids.append(row[0])
            else:
                seen_target_pairs.add(target_key)
        if duplicate_target_ids:
            conn.executemany(
                "DELETE FROM camera_tv_targets WHERE id=?",
                [(row_id,) for row_id in duplicate_target_ids],
            )
        if duplicate_ids:
            conn.executemany(
                "DELETE FROM paired_tvs WHERE id=?",
                [(row_id,) for row_id in duplicate_ids],
            )

        priority_rows = conn.execute(
            """
            SELECT id, group_name, camera_id
            FROM camera_group_priorities
            WHERE camera_id IS NOT NULL
            ORDER BY group_name, camera_id, priority, COALESCE(created_at, ''), id
            """
        ).fetchall()
        seen_priority_pairs = set()
        duplicate_priority_ids = []
        for row in priority_rows:
            priority_key = (row[1], row[2])
            if priority_key in seen_priority_pairs:
                duplicate_priority_ids.append(row[0])
            else:
                seen_priority_pairs.add(priority_key)
        if duplicate_priority_ids:
            conn.executemany(
                "DELETE FROM camera_group_priorities WHERE id=?",
                [(row_id,) for row_id in duplicate_priority_ids],
            )

        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_paired_tvs_device_token_id "
            "ON paired_tvs(device_token_id)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_camera_tv_targets_camera_id_tv_id "
            "ON camera_tv_targets(camera_id, tv_id)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_camera_group_priorities_group_camera_id "
            "ON camera_group_priorities(group_name, camera_id)"
        )
        init_global_settings(conn)

init_db()


# --- HELPERS ---

_LOG_TAG_RE = re.compile(r'(\[[^\]]+\]\[[^\]]+\]|\[test-tv:[^\]]+\])')
_WEBHOOK_TRIGGER_LIMIT = 100


def _parse_log_line(source_name, line):
    line = line.rstrip("\n")
    if not line:
        return None

    timestamp = line[:23] if len(line) >= 23 else ""
    message = line[33:] if " - " in line else line
    tag_match = _LOG_TAG_RE.search(message)
    alert_tag = tag_match.group(1) if tag_match else None

    return {
        "source": source_name,
        "timestamp": timestamp,
        "line": line,
        "display": f"[{source_name}] {line}",
        "alert_tag": alert_tag,
        "is_trigger": source_name == "system.log" and "Webhook triggered." in message,
    }


def _get_recent_trigger_tags(system_log_path):
    recent = deque(maxlen=_WEBHOOK_TRIGGER_LIMIT)

    with open(system_log_path) as f:
        for line in f:
            parsed = _parse_log_line("system.log", line)
            if not parsed or not parsed["alert_tag"]:
                continue
            if parsed["is_trigger"] or parsed["alert_tag"].startswith("[test-tv:"):
                recent.append(parsed["alert_tag"])

    return list(recent)


def _load_tv_target_ids(camera_id, camera_name):
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT tv_id
            FROM camera_tv_targets
            WHERE (camera_id IS NOT NULL AND camera_id=?)
               OR (camera_id IS NULL AND camera_name=?)
            ORDER BY tv_id ASC
            """,
            (camera_id, camera_name),
        ).fetchall()
    return [row["tv_id"] for row in rows if row["tv_id"]]


def _save_tv_targets(camera_id, camera_name, tv_ids):
    with get_db_connection() as conn:
        conn.execute(
            """
            DELETE FROM camera_tv_targets
            WHERE (camera_id IS NOT NULL AND camera_id=?)
               OR (camera_id IS NULL AND camera_name=?)
            """,
            (camera_id, camera_name),
        )
        for tv_id in sorted({tv_id for tv_id in tv_ids if tv_id}):
            conn.execute(
                """
                INSERT INTO camera_tv_targets (id, camera_id, camera_name, tv_id)
                VALUES (?, ?, ?, ?)
                """,
                (str(uuid.uuid4()), camera_id, camera_name, tv_id),
            )


def _split_rtsp_url(rtsp_url):
    raw = (rtsp_url or "").strip()
    if not raw:
        return {"base_url": "", "username": "", "password": ""}

    parts = urlsplit(raw)
    if not parts.scheme or not parts.netloc:
        return {"base_url": raw, "username": "", "password": ""}

    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"

    return {
        "base_url": urlunsplit((parts.scheme, netloc, parts.path or "", parts.query or "", parts.fragment or "")),
        "username": unquote(parts.username or ""),
        "password": unquote(parts.password or ""),
    }


def _compose_rtsp_url(base_url, username="", password="", existing_url=None):
    base = (base_url or "").strip()
    if not base:
        return None

    parts = urlsplit(base)
    if not parts.scheme or not parts.netloc:
        return base

    username = (username or "").strip()
    password = password or ""
    if username and not password and existing_url:
        existing_parts = _split_rtsp_url(existing_url)
        if existing_parts["username"] == username:
            password = existing_parts["password"]

    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    if username:
        auth = quote(username, safe="")
        if password:
            auth = f"{auth}:{quote(password, safe='@')}"
        netloc = f"{auth}@{netloc}"

    return urlunsplit((parts.scheme, netloc, parts.path or "", parts.query or "", parts.fragment or ""))

def get_log_entries():
    if not os.path.isdir(LOG_DIR):
        return []

    try:
        log_files = sorted(
            name for name in os.listdir(LOG_DIR)
            if name.endswith(".log") and os.path.isfile(os.path.join(LOG_DIR, name))
        )
        if not log_files:
            return []

        system_log_path = os.path.join(LOG_DIR, "system.log")
        if not os.path.isfile(system_log_path):
            return []

        trigger_tags = _get_recent_trigger_tags(system_log_path)
        if not trigger_tags:
            return []

        selected_tags = set(trigger_tags)
        entries = []
        for name in log_files:
            path = os.path.join(LOG_DIR, name)
            with open(path) as f:
                for line in f:
                    parsed = _parse_log_line(name, line)
                    if parsed and parsed["alert_tag"] in selected_tags:
                        entries.append(parsed)

        if not entries:
            return []

        entries.sort(key=lambda item: item["timestamp"])
        return entries
    except Exception as e:
        return [{
            "source": "system",
            "timestamp": "",
            "line": f"Error reading logs: {e}",
            "display": f"[system] Error reading logs: {e}",
            "alert_tag": None,
            "is_trigger": False,
        }]


def get_redis_health():
    try:
        r.ping()
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "error": type(e).__name__}


def get_service_health():
    service_names = [
        "worker",
        "mute_bot",
        "bi_exporter",
        "bi_queue_monitor",
        "bi_downloader",
        "bi_watchdog",
        "video_delivery_worker",
    ]
    return {name: heartbeat_status(name) for name in service_names}


def get_pipeline_status():
    services = get_service_health()
    try:
        queue_depths = {
            "export_requests": r.llen(EXPORT_REQUEST_QUEUE),
            "download_requests": r.llen(DOWNLOAD_REQUEST_QUEUE),
            "video_delivery_requests": r.llen(VIDEO_DELIVERY_QUEUE),
            "active_exports": r.scard(ACTIVE_EXPORT_SET),
        }
    except Exception:
        return {
            "queue_depths": None,
            "stale_jobs": None,
            "services": services,
        }

    stale_jobs = {
        "submitted": 0,
        "queued": 0,
        "ready": 0,
        "retry_queued": 0,
        "delivery_processing": 0,
    }
    now = datetime.now().timestamp()
    for request_id in iter_job_ids():
        job = load_job(request_id)
        if not job:
            continue
        age = now - float(job.get("last_transition_at", job.get("updated_at", now)))
        status = job.get("status")
        delivery_status = job.get("delivery_status")
        if status == "submitted" and age > 30:
            stale_jobs["submitted"] += 1
        elif status == "queued" and age > 60:
            stale_jobs["queued"] += 1
        elif status == "ready" and age > 60:
            stale_jobs["ready"] += 1
        elif status == "retry_queued" and age > 30:
            stale_jobs["retry_queued"] += 1
        if delivery_status == "processing" and age > (HEARTBEAT_STALE_AFTER * 2):
            stale_jobs["delivery_processing"] += 1

    return {
        "queue_depths": queue_depths,
        "stale_jobs": stale_jobs,
        "services": services,
    }


def load_known_plates():
    try:
        if os.path.exists(KNOWN_PLATES_FILE):
            return json.loads(open(KNOWN_PLATES_FILE).read())
    except Exception:
        pass
    return {}


def save_known_plates(plates):
    with open(KNOWN_PLATES_FILE, 'w') as f:
        json.dump(plates, f, indent=2)


def get_mute_status(chat_id):
    """Return list of active mute dicts for display."""
    conn = get_db_connection()
    configs = [dict(r) for r in conn.execute("SELECT id, name, chat_id FROM configs WHERE chat_id=?", (chat_id,)).fetchall()]
    conn.close()
    mutes = []
    for c in configs:
        key = f"mute:{c['name'].lower()}:{chat_id}"
        val = r.get(key)
        if val:
            expiry = datetime.fromisoformat(val.decode())
            if expiry > datetime.now():
                remaining = int((expiry - datetime.now()).total_seconds() / 60)
                mutes.append({"camera": c['name'], "remaining_min": remaining, "key": key})
    all_key = f"mute:all:{chat_id}"
    val = r.get(all_key)
    if val:
        expiry = datetime.fromisoformat(val.decode())
        if expiry > datetime.now():
            remaining = int((expiry - datetime.now()).total_seconds() / 60)
            mutes.append({"camera": "All cameras", "remaining_min": remaining, "key": all_key})
    return mutes


def get_caption_mode(chat_id):
    cm = r.get(f'caption_mode:{chat_id}')
    if cm:
        try:
            data = json.loads(cm)
            expires = datetime.fromisoformat(data['expires'])
            if expires > datetime.now():
                remaining = int((expires - datetime.now()).total_seconds() / 60)
                return {"mode": data['mode'], "remaining_min": remaining}
        except Exception:
            pass
    return None


def send_telegram_blocking(token, chat_id, thread_id, img_path, caption):
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    data = {'chat_id': chat_id, 'caption': caption}
    if thread_id:
        data['message_thread_id'] = thread_id
    try:
        with open(img_path, 'rb') as f:
            resp = requests.post(url, files={'photo': f}, data=data, timeout=5)
            if resp.status_code == 200:
                return resp.json()['result']['message_id']
            app.logger.error(f"Telegram upload failed: {resp.text}")
    except Exception as e:
        app.logger.error(f"Telegram connection error: {e}")
    return None


# --- HTML TEMPLATE ---

HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en" data-bs-theme="light">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Blue Iris AI Hub</title>
    <link rel="icon" href="{{ url_for('static', filename='logo-mark.svg') }}" type="image/svg+xml">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="{{ url_for('static', filename='dashboard.css') }}" rel="stylesheet">
</head>
<body>
<div class="container py-4 py-lg-5 app-shell">
    <section class="mb-3">
        <div class="hero-grid">
            <div class="hero-topbar">
                <div class="hero-main">
                    <div class="brand-lockup">
                        <img src="{{ url_for('static', filename='logo-mark.svg') }}" alt="Blue Iris AI Hub logo">
                        <div>
                            <h1 class="hero-title">Blue Iris AI Hub</h1>
                        </div>
                    </div>
                </div>
                <div class="hero-actions">
                    <span class="badge text-bg-secondary">v{{ current_version }}</span>
                    <button class="btn btn-outline-secondary" onclick="toggleTheme()" id="themeBtn"><span id="themeIcon">🌙</span></button>
                    <button class="btn btn-primary" data-bs-toggle="modal" data-bs-target="#addModal">+ New Configuration</button>
                </div>
            </div>
        </div>
    </section>

    <!-- Update banner — shown by JS if a newer version is available on GitHub -->
    <div id="update-banner" class="alert alert-warning alert-dismissible fade show d-none mb-3" role="alert">
        <strong>Update available!</strong>
        v<span id="update-version"></span> is available on
        <a id="update-link" href="#" target="_blank" rel="noopener">GitHub</a>.
        Run <code>docker compose pull && docker compose up -d</code> to update.
        <button type="button" id="dismiss-update" class="btn-close" data-bs-dismiss="alert"></button>
    </div>

    {% with messages = get_flashed_messages(with_categories=true) %}
      {% if messages %}{% for category, message in messages %}
        <div class="alert alert-{{ category }}">{{ message }}</div>
      {% endfor %}{% endif %}
    {% endwith %}

    <ul class="nav nav-tabs mb-4" id="mainTab">
        <li class="nav-item"><button class="nav-link active" data-bs-toggle="tab" data-bs-target="#configs-pane">Configurations</button></li>
        <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#global-pane">Alert Controls</button></li>
        <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tv-groups-pane">TV Settings</button></li>
        <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#logs-pane">Logs</button></li>
        <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#plate-audit-pane">Plate Audit{% if plate_audit %} <span class="badge bg-secondary ms-1">{{ plate_audit|length }}</span>{% endif %}</button></li>
    </ul>

    <div class="tab-content">

        <!-- Configurations tab -->
        <div class="tab-pane fade show active" id="configs-pane">
            <div class="section-header">
                <div class="section-title">
                    <div>
                        <h2 class="h4 mb-1">Camera Configurations</h2>
                        <p class="section-subtitle">Each configuration owns one camera webhook, AI prompt chain, and Telegram delivery target.</p>
                    </div>
                </div>
            </div>
            <details class="subtle-panel mb-3">
                <summary class="d-flex justify-content-between align-items-center">
                    <span><strong>Blue Iris Setup</strong> <span class="text-muted">On Alert action (per camera)</span></span>
                    <span class="text-muted small">Expand</span>
                </summary>
                <ol class="mb-0 mt-3 ps-3">
                    <li>In Blue Iris, open camera settings and go to the <strong>Alerts</strong> tab.</li>
                    <li>Under <strong>On alert</strong>, add a <em>Run a program or write to a file</em> action.</li>
                    <li>Set <strong>Action</strong> to <code>Run program/script</code>.</li>
                    <li>Set <strong>File</strong> to <code>curl.exe</code>.</li>
                    <li>Set <strong>Parameters</strong> to the <em>Blue Iris Notification Parameter</em> for this config (with <code>&lt;AlertsFolder&gt;</code> replaced).</li>
                    <li>Set <strong>Window</strong> to <code>Hide</code>.</li>
                    <li>Set <strong>Camera</strong> to the camera you are configuring (e.g. <em>Driveway</em>).</li>
                    <li>Uncheck <strong>Also execute on Remote Management</strong> and <strong>Wait for process to complete</strong>.</li>
                </ol>
            </details>
            <div class="config-grid">
                {% for config in configs %}
                    <div class="card h-100">
                        <div class="card-body">
                            <div class="d-flex justify-content-between align-items-start">
                                <div>
                                    <h5 class="card-title mb-1">{{ config.name }}</h5>
                                    <span class="last-seen" {% if config.last_triggered %}data-ts="{{ config.last_triggered }}"{% endif %}>{% if config.last_triggered %}Last triggered: {{ config.last_triggered }}{% else %}Never triggered{% endif %}</span>
                                </div>
                                <div class="btn-group">
                                    <button class="btn btn-sm btn-outline-secondary" onclick='openEditModal({{ config|tojson }})'>Edit</button>
                                    {% if config.tv_push_enabled %}
                                    <button class="btn btn-sm btn-outline-info ms-1" onclick="testTvAlert('{{ config.id }}', this)">📺 Test</button>
                                    {% endif %}
                                    <form action="{{ url_for('delete_config', id=config.id) }}" method="POST" onsubmit="return confirm('Delete this config?');" style="display:inline;">
                                        <button class="btn btn-sm btn-outline-danger ms-1">Delete</button>
                                    </form>
                                </div>
                            </div>
                            <hr>
                            <label class="form-label fw-bold">Blue Iris Notification Parameter:</label>
                            <div class="webhook-box mb-2" id="webhook-{{config.id}}">-X POST -F "image=@&lt;AlertsFolder&gt;\&ALERT_PATH" --form-string "bvr=&ALERT_CLIP" {% if base_url %}{{ base_url }}/webhook/{{ config.id }}{% else %}{{ request.host_url }}webhook/{{ config.id }}{% endif %}</div>
                            <button class="btn btn-sm btn-outline-primary" onclick="copyToClipboard('webhook-{{config.id}}', this)">📋 Copy</button>
                            <div class="form-text mt-1">Replace <code>&lt;AlertsFolder&gt;</code> with your Blue Iris alerts path (e.g. <code>D:\Alerts</code> — found in Blue Iris &rarr; Global settings &rarr; Storage tab).</div>
                            <div class="mt-3 d-flex gap-2 flex-wrap">
                                {% if config.send_video %}<span class="badge bg-info text-dark">🎞️ Video enabled</span>{% endif %}
                                {% if config.instant_notify %}<span class="badge bg-success">⚡ Instant notify</span>{% endif %}
                                {% if config.tv_push_enabled %}<span class="badge bg-primary">📺 TV overlay{% if config.tv_mute_audio %} 🔇{% endif %}</span>{% endif %}
                                {% if config.tv_group %}<span class="badge bg-secondary">📺 TV group {{ config.tv_group }}</span>{% endif %}
                                {% if config.message_thread_id %}<span class="badge bg-secondary">🧵 Thread {{ config.message_thread_id }}</span>{% endif %}
                                {% if config.grok_api_key %}<span class="badge bg-warning text-dark">Grok ✓</span>{% endif %}
                                {% if config.groq_api_key %}<span class="badge bg-warning text-dark">Groq ✓</span>{% endif %}
                            </div>
                        </div>
                    </div>
                {% else %}
                <div class="card h-100">
                    <div class="card-body text-center py-5">
                        <h4 class="text-muted">No configurations yet.</h4>
                    </div>
                </div>
                {% endfor %}
            </div>
        </div>

        <!-- Alert Controls tab -->
        <div class="tab-pane fade" id="global-pane">
            <div class="section-header">
                <div class="section-title">
                    <div>
                        <h2 class="h4 mb-1">Alert Controls</h2>
                        <p class="section-subtitle">Operator-facing Telegram state, auto-mute policy, and known-plate metadata.</p>
                    </div>
                </div>
            </div>
            <div class="global-grid">
                <div class="card h-100">
                    <div class="card-body">
                        <h5 class="card-title">Live Mute State</h5>
                        <p class="text-muted small">Current mute state and temporary caption overrides coming from Telegram operators.</p>
                        {% if mutes %}
                            <ul class="list-group list-group-flush mb-3">
                            {% for m in mutes %}
                                <li class="list-group-item d-flex justify-content-between align-items-center">
                                    {{ m.camera }} — {{ m.remaining_min }} min remaining
                                    <form action="{{ url_for('clear_mute') }}" method="POST" style="display:inline;">
                                        <input type="hidden" name="redis_key" value="{{ m.key }}">
                                        <button class="btn btn-sm btn-outline-danger">Unmute</button>
                                    </form>
                                </li>
                            {% endfor %}
                            </ul>
                        {% else %}
                            <p class="text-muted">No active mutes.</p>
                        {% endif %}
                        <hr>
                        <h6>Caption Mode</h6>
                        {% if caption_mode %}
                            <p>Active: <strong>{{ caption_mode.mode }}</strong> — {{ caption_mode.remaining_min }} min remaining</p>
                            <form action="{{ url_for('clear_caption') }}" method="POST">
                                <input type="hidden" name="chat_id" value="{{ primary_chat_id }}">
                                <button class="btn btn-sm btn-outline-secondary">Reset to Normal</button>
                            </form>
                        {% else %}
                            <p class="text-muted">Normal (no override active)</p>
                        {% endif %}
                    </div>
                </div>

                <div class="card h-100">
                    <div class="card-body">
                        <h5 class="card-title">Auto-mute Policy</h5>
                        <p class="text-muted small">Use these values to control when noisy cameras are auto-muted after repeated triggers.</p>
                        <form action="{{ url_for('save_global_settings_route') }}" method="POST">
                            <div class="row">
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">Trigger threshold</label>
                                    <div class="input-group">
                                        <input type="number" min="1" max="100" name="auto_mute_threshold" class="form-control" value="{{ global_settings.auto_mute_threshold }}" required>
                                        <span class="input-group-text">triggers</span>
                                    </div>
                                </div>
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">Window</label>
                                    <div class="input-group">
                                        <input type="number" min="1" max="1440" name="auto_mute_window_minutes" class="form-control" value="{{ global_settings.auto_mute_window_minutes }}" required>
                                        <span class="input-group-text">min</span>
                                    </div>
                                </div>
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">Mute duration</label>
                                    <div class="input-group">
                                        <input type="number" min="1" max="1440" name="auto_mute_duration_minutes" class="form-control" value="{{ global_settings.auto_mute_duration_minutes }}" required>
                                        <span class="input-group-text">min</span>
                                    </div>
                                </div>
                            </div>
                            <button class="btn btn-primary">Save Global Settings</button>
                        </form>
                    </div>
                </div>

                <div class="card h-100">
                    <div class="card-body">
                        <h5 class="card-title">Known Plates</h5>
                        <p class="text-muted small">Plates the AI will recognise and label in captions.</p>
                        {% if known_plates %}
                        <table class="table table-sm mb-3">
                            <thead><tr><th>Plate</th><th>Label</th><th></th></tr></thead>
                            <tbody>
                            {% for plate, label in known_plates.items() %}
                            <tr>
                                <td><code>{{ plate }}</code></td>
                                <td>{{ label }}</td>
                                <td>
                                    <form action="{{ url_for('delete_plate') }}" method="POST" style="display:inline;">
                                        <input type="hidden" name="plate" value="{{ plate }}">
                                        <button class="btn btn-sm btn-outline-danger py-0">✕</button>
                                    </form>
                                </td>
                            </tr>
                            {% endfor %}
                            </tbody>
                        </table>
                        {% else %}
                        <p class="text-muted small">No plates configured.</p>
                        {% endif %}
                        <form action="{{ url_for('add_plate') }}" method="POST" class="d-flex gap-2 mt-2">
                            <input type="text" name="plate" class="form-control form-control-sm" placeholder="AB12 CDE" required style="max-width:130px;">
                            <input type="text" name="label" class="form-control form-control-sm" placeholder="Alice's Car" required>
                            <button class="btn btn-sm btn-primary">Add</button>
                        </form>
                    </div>
                </div>
            </div>
        </div>

        <div class="tab-pane fade" id="tv-groups-pane">
            <div class="section-header">
                <div class="section-title">
                    <div>
                        <h2 class="h4 mb-1">TV Settings</h2>
                        <p class="section-subtitle">TV app install, pairing, and grouped camera priority controls for overlay delivery.</p>
                    </div>
                </div>
            </div>
            <details class="subtle-panel mb-3">
                <summary class="d-flex justify-content-between align-items-center">
                    <span><strong>Android TV App Setup</strong> <span class="text-muted">One-time setup per TV</span></span>
                    <span class="text-muted small">Expand</span>
                </summary>
                <ol class="mb-0 mt-3 ps-3">
                    <li>Copy the <strong>TV App Downloader URL</strong> below, open <code>Downloader</code> on the TV, and enter the URL to install the PiPup APK.</li>
                    <li>Open the <strong>PiPup</strong> app on the TV and note the <strong>server address</strong> and <strong>pairing code</strong> shown on screen.</li>
                    <li>On the TV, go to <strong>Settings &rarr; Apps &rarr; Special app access &rarr; Display over other apps</strong> (or <em>Appear on top</em>).</li>
                    <li>Find <strong>PiPup / nl.rogro82.pipup</strong> in the list and <strong>enable</strong> the permission. Without this the overlay will not appear.</li>
                    <li>Use the <strong>Pair TV</strong> form below, entering the TV&rsquo;s IP and the pairing code from step 2.</li>
                    <li>In each camera&rsquo;s config, enable <strong>Push stream to TV overlay</strong> and set the RTSP URL, duration, and TV group.</li>
                </ol>
            </details>
            <div class="global-grid">
                <div class="card h-100">
                    <div class="card-body">
                        <h5 class="card-title">Pair TV</h5>
                        <p class="text-muted small">Enter the TV listener address and pairing code shown by the Android TV app.</p>
                        <div class="mb-3">
                            <label class="form-label">TV App Downloader URL</label>
                            <div class="webhook-box mb-2" id="tv-apk-download-url">{{ tv_apk_download_url }}</div>
                            <button class="btn btn-sm btn-outline-primary" type="button" onclick="copyToClipboard('tv-apk-download-url', this)">📋 Copy</button>
                            <div class="form-text mt-1">Open <code>Downloader</code> on the TV and enter this URL to install the current debug APK hosted by the hub.</div>
                        </div>
                        <hr>
                        <form onsubmit="return submitTvPairing(event)">
                            <div class="row">
                                <div class="col-md-5 mb-3"><label class="form-label">TV IP</label><input type="text" name="ip_address" class="form-control" placeholder="192.168.1.50"></div>
                                <div class="col-md-3 mb-3"><label class="form-label">Port</label><input type="number" min="1" max="65535" name="port" class="form-control" value="7979"></div>
                                <div class="col-md-4 mb-3"><label class="form-label">Pairing code</label><input type="text" name="manual_code" class="form-control" placeholder="ABC123" required></div>
                            </div>
                            <div class="d-flex align-items-center gap-3">
                                <button class="btn btn-primary" type="submit">Pair TV</button>
                                <span class="text-muted small" id="pairTvStatus"></span>
                            </div>
                        </form>
                    </div>
                </div>

                <div class="card h-100">
                    <div class="card-body">
                        <h5 class="card-title">Paired TVs</h5>
                        <p class="text-muted small">Registered TVs available as camera targets for overlay delivery.</p>
                        {% if paired_tvs %}
                            <ul class="list-group list-group-flush">
                            {% for tv in paired_tvs %}
                                <li class="list-group-item d-flex justify-content-between align-items-center">
                                    <div>
                                        <div><strong>{{ tv.name }}</strong></div>
                                        <small class="text-muted">{{ tv.ip_address or 'unknown ip' }}:{{ tv.port or 7979 }}</small>
                                    </div>
                                    <form action="{{ url_for('delete_paired_tv', tv_id=tv.id) }}" method="POST">
                                        <button class="btn btn-sm btn-outline-danger">Delete</button>
                                    </form>
                                </li>
                            {% endfor %}
                            </ul>
                        {% else %}
                            <p class="text-muted small mb-0">No TVs paired yet.</p>
                        {% endif %}
                    </div>
                </div>

                <div class="card h-100">
                    <div class="card-body">
                        <h5 class="card-title">TV Group Priorities</h5>
                        <p class="text-muted small">Grouped cameras can share a TV target. Drag-and-drop ordering is still pending; this release includes the persistence API and placeholder panel.</p>
                        <div id="tv-group-priority-root" class="text-muted">Priority management UI is available in this feature branch, but the drag-and-drop editor is not finalized yet.</div>
                    </div>
                </div>
            </div>
        </div>

        <!-- Logs tab -->
        <div class="tab-pane fade" id="logs-pane">
            <div class="section-header">
                <div class="section-title mb-0">
                    <div>
                        <h2 class="h4 mb-1">Live Alert Traces</h2>
                        <p class="section-subtitle">Grouped service logs for the most recent alert activity, with source filters for debugging one subsystem at a time.</p>
                    </div>
                </div>
            </div>
            <div class="log-shell">
                <div class="log-toolbar">
                    <div class="log-toolbar-title">
                        <h5>Grouped logs</h5>
                        <p>Live service output grouped by webhook trace when all sources are selected.</p>
                        <div class="log-summary-pill">Most recent alert traces and supporting worker output</div>
                    </div>
                    <div class="log-toolbar-actions">
                        <select id="logSourceFilter" class="form-select form-select-sm" style="width:auto;">
                            <option value="all">All sources</option>
                        </select>
                        <a href="{{ url_for('index') }}" class="btn btn-sm btn-outline-secondary">Refresh</a>
                        <form action="{{ url_for('clear_logs') }}" method="POST" style="display:inline;">
                            <button class="btn btn-sm btn-outline-danger">Clear</button>
                        </form>
                    </div>
                </div>
                <div class="log-console">
                    <div class="log-viewer" id="logViewer" data-log-entries='{{ log_entries|tojson|safe }}'></div>
                </div>
            </div>
        </div>

        <!-- Plate Audit tab -->
        <div class="tab-pane fade" id="plate-audit-pane">
            <div class="section-header">
                <div class="section-title mb-0">
                    <div>
                        <h2 class="h4 mb-1">Plate Audit</h2>
                        <p class="section-subtitle">Registrations seen by the hub, with DVLA enrichment and a saved thumbnail for fast review.</p>
                    </div>
                </div>
            </div>
            {% if plate_audit %}
            <div class="table-responsive">
            <table class="table table-hover align-middle">
                <thead><tr>
                    <th>Image</th><th>Plate</th><th>Vehicle</th><th>Tax</th><th>MOT</th><th>Camera</th><th>Last seen</th><th>Seen</th><th></th>
                </tr></thead>
                <tbody>
                {% for entry in plate_audit %}
                <tr>
                    <td style="width:80px">
                        {% if entry.image_filename %}
                        <img src="{{ url_for('plate_audit_image', filename=entry.image_filename) }}" style="width:72px;height:54px;object-fit:cover;border-radius:4px;">
                        {% else %}<span class="text-muted">—</span>{% endif %}
                    </td>
                    <td><strong>{{ entry.plate }}</strong></td>
                    <td>
                        {% if entry.dvla_make %}{{ entry.dvla_make }}{% endif %}
                        {% if entry.dvla_colour %}<br><small class="text-muted">{{ entry.dvla_colour }}{% if entry.dvla_year %}, {{ entry.dvla_year }}{% endif %}</small>{% endif %}
                    </td>
                    <td>
                        {% set ts = entry.dvla_tax_status or '' %}
                        {% if ts == 'Taxed' %}<span class="badge bg-success">Taxed</span>
                        {% elif ts in ['Untaxed', 'SORN'] %}<span class="badge bg-danger">{{ ts }}</span>
                        {% elif ts == 'unverified' %}<span class="badge bg-secondary">Unverified</span>
                        {% else %}<span class="text-muted">—</span>{% endif %}
                    </td>
                    <td>
                        {% set ms = entry.dvla_mot_status or '' %}
                        {% if ms == 'Valid' %}<span class="badge bg-success">Valid</span>
                        {% elif ms == 'Not valid' %}<span class="badge bg-danger">Not valid</span>
                        {% elif ms %}<span class="badge bg-secondary">{{ ms }}</span>
                        {% else %}<span class="text-muted">—</span>{% endif %}
                    </td>
                    <td><small>{{ entry.camera_name or '—' }}</small></td>
                    <td><small class="last-seen" {% if entry.last_seen %}data-ts="{{ entry.last_seen }}"{% endif %}>{{ entry.last_seen or '—' }}</small></td>
                    <td><span class="badge bg-secondary">{{ entry.seen_count }}</span></td>
                    <td>
                        <form action="{{ url_for('delete_plate_audit') }}" method="POST" onsubmit="return confirm('Remove {{ entry.plate }} from audit log?');">
                            <input type="hidden" name="id" value="{{ entry.id }}">
                            <button class="btn btn-sm btn-outline-danger">Delete</button>
                        </form>
                    </td>
                </tr>
                {% endfor %}
                </tbody>
            </table>
            </div>
            {% else %}
            <p class="text-muted mt-3">No unknown plates recorded yet. Plates seen by cameras with a DVLA API key configured will appear here.</p>
            {% endif %}
        </div>

    </div>
</div>

<!-- Add Modal -->
<div class="modal fade" id="addModal" tabindex="-1">
    <div class="modal-dialog modal-lg">
        <div class="modal-content">
            <form action="{{ url_for('add_config') }}" method="POST" onsubmit="return validateTvForm(this)">
                <div class="modal-header"><h5 class="modal-title">Add Configuration</h5><button type="button" class="btn-close" data-bs-dismiss="modal"></button></div>
                <div class="modal-body">
                    <div class="mb-3"><label class="form-label">Name</label><input type="text" name="name" class="form-control" placeholder="e.g. Driveway" required></div>
                    <div class="card mb-3 border-secondary">
                        <div class="card-header py-2"><h6 class="mb-0 text-secondary">Blue Iris Connection <span class="text-muted fw-normal small">(used for video export &amp; TV streaming)</span></h6></div>
                        <div class="card-body py-2">
                            <div class="row">
                                <div class="col-12 mb-2"><label class="form-label mb-1">Blue Iris URL</label><input type="text" name="bi_url" class="form-control" placeholder="http://192.168.0.11:81"></div>
                                <div class="col-md-6 mb-2"><label class="form-label mb-1">BI Username</label><input type="text" name="bi_user" class="form-control"></div>
                                <div class="col-md-6 mb-2"><label class="form-label mb-1">BI Password</label><div class="input-group"><input type="password" name="bi_pass" id="add_bi_pass" class="form-control"><button class="btn btn-outline-secondary btn-sm" type="button" onclick="togglePassword('add_bi_pass')">👁️</button></div></div>
                            </div>
                        </div>
                    </div>
                    <ul class="nav nav-tabs mb-3" id="addConfigTabs" role="tablist">
                        <li class="nav-item" role="presentation"><button class="nav-link active" id="add-telegram-tab" data-bs-toggle="tab" data-bs-target="#add-telegram-pane" type="button" role="tab">Telegram Alert</button></li>
                        <li class="nav-item" role="presentation"><button class="nav-link" id="add-tv-tab" data-bs-toggle="tab" data-bs-target="#add-tv-pane" type="button" role="tab">TV Overlay</button></li>
                    </ul>
                    <div class="tab-content">
                        <div class="tab-pane fade show active" id="add-telegram-pane" role="tabpanel" aria-labelledby="add-telegram-tab">
                            <div class="row">
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">Gemini API Key(s) <span class="text-muted small">(comma-separated for rotation)</span></label>
                                    <div class="input-group"><input type="password" name="gemini_key" id="add_gemini_key" class="form-control" required><button class="btn btn-outline-secondary" type="button" onclick="togglePassword('add_gemini_key')">👁️</button></div>
                                </div>
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">Telegram Bot Token</label>
                                    <div class="input-group"><input type="password" name="telegram_token" id="add_telegram_token" class="form-control" required><button class="btn btn-outline-secondary" type="button" onclick="togglePassword('add_telegram_token')">👁️</button></div>
                                </div>
                            </div>
                            <div class="row">
                                <div class="col-md-6 mb-3"><label class="form-label">Telegram Chat ID</label><input type="text" name="chat_id" class="form-control" required></div>
                                <div class="col-md-6 mb-3"><label class="form-label">Message Thread ID <span class="text-muted small">(optional)</span></label><input type="text" name="message_thread_id" class="form-control" placeholder="Topic/thread ID"></div>
                            </div>
                            <div class="mb-3"><label class="form-label">AI Prompt</label><textarea name="prompt" class="form-control" rows="3" required>The CCTV has detected motion. Describe any motion in a single sentence (max 145 characters) — vehicles (colour, make, plate), people, or deliveries. Do not describe static objects.</textarea></div>
                            <hr>
                            <h6 class="text-primary">Video Export</h6>
                            <div class="row">
                                <div class="col-md-6 mb-3">
                                    <div class="form-check"><input class="form-check-input" type="checkbox" name="send_video" id="add_send_video"><label class="form-check-label" for="add_send_video">Fetch &amp; send video clip</label></div>
                                    <div class="form-check"><input class="form-check-input" type="checkbox" name="delete_after_send" id="add_delete_after_send" checked><label class="form-check-label" for="add_delete_after_send">Delete clip from BI after send</label></div>
                                    <div class="form-check"><input class="form-check-input" type="checkbox" name="instant_notify" id="add_instant_notify"><label class="form-check-label" for="add_instant_notify">Instant notify <span class="text-muted small">(send immediately, caption follows)</span></label></div>
                                    <div class="form-check"><input class="form-check-input" type="checkbox" name="verbose_logging" id="add_verbose_logging"><label class="form-check-label" for="add_verbose_logging">Verbose logging</label></div>
                                </div>
                            </div>
                            <hr>
                            <h6 class="text-primary">AI Fallback Keys <span class="text-muted fw-normal small">(optional)</span></h6>
                            <div class="row">
                                <div class="col-md-6 mb-3"><label class="form-label">Grok API Key</label><div class="input-group"><input type="password" name="grok_api_key" id="add_grok_key" class="form-control"><button class="btn btn-outline-secondary" type="button" onclick="togglePassword('add_grok_key')">👁️</button></div></div>
                                <div class="col-md-6 mb-3"><label class="form-label">Groq API Key</label><div class="input-group"><input type="password" name="groq_api_key" id="add_groq_key" class="form-control"><button class="btn btn-outline-secondary" type="button" onclick="togglePassword('add_groq_key')">👁️</button></div></div>
                                <div class="col-md-6 mb-3"><label class="form-label">DVLA API Key</label><div class="input-group"><input type="password" name="dvla_api_key" id="add_dvla_key" class="form-control" placeholder="Optional — enables UK plate enrichment"><button class="btn btn-outline-secondary" type="button" onclick="togglePassword('add_dvla_key')">👁️</button></div><div class="form-text">Register free at <a href="https://developer-portal.driver-vehicle-licensing.api.gov.uk/" target="_blank" rel="noopener">DVLA developer portal</a> to get a key.</div></div>
                            </div>
                            <hr>
                            <h6 class="text-primary">BI Encoder Recovery <span class="text-muted fw-normal small">(optional)</span></h6>
                            <div class="row">
                                <div class="col-md-6 mb-3"><label class="form-label">Recovery URL</label><input type="text" name="bi_restart_url" class="form-control" placeholder="http://192.168.1.250:9090/restart-bi"></div>
                                <div class="col-md-6 mb-3"><label class="form-label">Recovery Token</label><div class="input-group"><input type="password" name="bi_restart_token" id="add_bi_restart_token" class="form-control"><button class="btn btn-outline-secondary" type="button" onclick="togglePassword('add_bi_restart_token')">👁️</button></div></div>
                            </div>
                        </div>
                        <div class="tab-pane fade" id="add-tv-pane" role="tabpanel" aria-labelledby="add-tv-tab">
                            <h6 class="text-primary">TV Overlay Settings</h6>
                            <div class="row">
                                <div class="col-md-3 mb-3">
                                    <div class="form-check mt-2">
                                        <input class="form-check-input" type="checkbox" name="tv_push_enabled" id="add_tv_push_enabled">
                                        <label class="form-check-label" for="add_tv_push_enabled">Push stream to TV overlay</label>
                                    </div>
                                </div>
                                <div class="col-md-3 mb-3">
                                    <div class="form-check mt-2">
                                        <input class="form-check-input" type="checkbox" name="tv_mute_audio" id="add_tv_mute_audio">
                                        <label class="form-check-label" for="add_tv_mute_audio">Mute audio</label>
                                    </div>
                                </div>
                                <div class="col-md-3 mb-3"><label class="form-label">TV Group</label><input type="text" name="tv_group" class="form-control" placeholder="driveway"></div>
                                <div class="col-md-3 mb-3"><label class="form-label">Duration (seconds)</label><input type="number" min="5" max="120" name="tv_duration_seconds" class="form-control" value="20"></div>
                                <div class="col-12 mb-3">
                                    <label class="form-label">Stream Source</label>
                                    <div>
                                        <div class="form-check form-check-inline"><input class="form-check-input" type="radio" name="tv_stream_type" id="add_stream_rtsp" value="rtsp" checked onchange="toggleAddStreamFields()"><label class="form-check-label" for="add_stream_rtsp">RTSP (manual URL)</label></div>
                                        <div class="form-check form-check-inline"><input class="form-check-input" type="radio" name="tv_stream_type" id="add_stream_mjpg" value="mjpg" onchange="toggleAddStreamFields()"><label class="form-check-label" for="add_stream_mjpg">Blue Iris MJPG (via proxy)</label></div>
                                    </div>
                                </div>
                                <div id="add_rtsp_fields" class="col-12">
                                    <div class="row">
                                        <div class="col-12 mb-3"><label class="form-label">RTSP Base URL</label><input type="text" name="tv_rtsp_base_url" class="form-control" placeholder="rtsp://192.168.1.50:554/stream1"></div>
                                        <div class="col-md-6 mb-3"><label class="form-label">RTSP Username</label><input type="text" name="tv_rtsp_username" class="form-control" placeholder="camera-user"></div>
                                        <div class="col-md-6 mb-3"><label class="form-label">RTSP Password</label><input type="password" name="tv_rtsp_password" id="add_tv_rtsp_password" class="form-control" placeholder="Optional"><div class="form-text">Stored server-side and merged into the saved RTSP URL.</div></div>
                                    </div>
                                </div>
                                <div class="col-12 mb-3">
                                    <label class="form-label">Target TVs</label>
                                    <select name="tv_target_ids" class="form-select" multiple size="4">
                                        {% for tv in paired_tvs %}
                                        <option value="{{ tv.id }}">{{ tv.name }}{% if tv.ip_address %} ({{ tv.ip_address }}){% endif %}</option>
                                        {% endfor %}
                                    </select>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
                <div class="modal-footer"><button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Close</button><button type="submit" class="btn btn-primary">Save</button></div>
            </form>
        </div>
    </div>
</div>

<!-- Edit Modal -->
<div class="modal fade" id="editModal" tabindex="-1">
    <div class="modal-dialog modal-lg">
        <div class="modal-content">
            <form id="editForm" method="POST" onsubmit="return validateTvForm(this)">
                <div class="modal-header"><h5 class="modal-title">Edit Configuration</h5><button type="button" class="btn-close" data-bs-dismiss="modal"></button></div>
                <div class="modal-body">
                    <div class="mb-3"><label class="form-label">Name</label><input type="text" id="edit_name" name="name" class="form-control" required></div>
                    <div class="card mb-3 border-secondary">
                        <div class="card-header py-2"><h6 class="mb-0 text-secondary">Blue Iris Connection <span class="text-muted fw-normal small">(used for video export &amp; TV streaming)</span></h6></div>
                        <div class="card-body py-2">
                            <div class="row">
                                <div class="col-12 mb-2"><label class="form-label mb-1">Blue Iris URL</label><input type="text" id="edit_bi_url" name="bi_url" class="form-control" placeholder="http://192.168.0.11:81"></div>
                                <div class="col-md-6 mb-2"><label class="form-label mb-1">BI Username</label><input type="text" id="edit_bi_user" name="bi_user" class="form-control"></div>
                                <div class="col-md-6 mb-2"><label class="form-label mb-1">BI Password</label><div class="input-group"><input type="password" id="edit_bi_pass" name="bi_pass" class="form-control"><button class="btn btn-outline-secondary btn-sm" type="button" onclick="togglePassword('edit_bi_pass')">👁️</button></div></div>
                            </div>
                        </div>
                    </div>
                    <ul class="nav nav-tabs mb-3" id="editConfigTabs" role="tablist">
                        <li class="nav-item" role="presentation"><button class="nav-link active" id="edit-telegram-tab" data-bs-toggle="tab" data-bs-target="#edit-telegram-pane" type="button" role="tab">Telegram Alert</button></li>
                        <li class="nav-item" role="presentation"><button class="nav-link" id="edit-tv-tab" data-bs-toggle="tab" data-bs-target="#edit-tv-pane" type="button" role="tab">TV Overlay</button></li>
                    </ul>
                    <div class="tab-content">
                        <div class="tab-pane fade show active" id="edit-telegram-pane" role="tabpanel" aria-labelledby="edit-telegram-tab">
                            <div class="row">
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">Gemini API Key(s) <span class="text-muted small">(comma-separated)</span></label>
                                    <div class="input-group"><input type="password" id="edit_gemini_key" name="gemini_key" class="form-control" required><button class="btn btn-outline-secondary" type="button" onclick="togglePassword('edit_gemini_key')">👁️</button></div>
                                </div>
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">Telegram Bot Token</label>
                                    <div class="input-group"><input type="password" id="edit_telegram_token" name="telegram_token" class="form-control" required><button class="btn btn-outline-secondary" type="button" onclick="togglePassword('edit_telegram_token')">👁️</button></div>
                                </div>
                            </div>
                            <div class="row">
                                <div class="col-md-6 mb-3"><label class="form-label">Telegram Chat ID</label><input type="text" id="edit_chat_id" name="chat_id" class="form-control" required></div>
                                <div class="col-md-6 mb-3"><label class="form-label">Message Thread ID <span class="text-muted small">(optional)</span></label><input type="text" id="edit_message_thread_id" name="message_thread_id" class="form-control"></div>
                            </div>
                            <div class="mb-3"><label class="form-label">AI Prompt</label><textarea id="edit_prompt" name="prompt" class="form-control" rows="3" required></textarea></div>
                            <hr>
                            <h6 class="text-primary">Video Export</h6>
                            <div class="row">
                                <div class="col-md-6 mb-3">
                                    <div class="form-check"><input class="form-check-input" type="checkbox" name="send_video" id="edit_send_video"><label class="form-check-label" for="edit_send_video">Fetch &amp; send video clip</label></div>
                                    <div class="form-check"><input class="form-check-input" type="checkbox" name="delete_after_send" id="edit_delete_after_send"><label class="form-check-label" for="edit_delete_after_send">Delete clip from BI after send</label></div>
                                    <div class="form-check"><input class="form-check-input" type="checkbox" name="instant_notify" id="edit_instant_notify"><label class="form-check-label" for="edit_instant_notify">Instant notify <span class="text-muted small">(send immediately, caption follows)</span></label></div>
                                    <div class="form-check"><input class="form-check-input" type="checkbox" name="verbose_logging" id="edit_verbose_logging"><label class="form-check-label" for="edit_verbose_logging">Verbose logging</label></div>
                                </div>
                            </div>
                            <hr>
                            <h6 class="text-primary">AI Fallback Keys <span class="text-muted fw-normal small">(optional)</span></h6>
                            <div class="row">
                                <div class="col-md-6 mb-3"><label class="form-label">Grok API Key</label><div class="input-group"><input type="password" id="edit_grok_key" name="grok_api_key" class="form-control"><button class="btn btn-outline-secondary" type="button" onclick="togglePassword('edit_grok_key')">👁️</button></div></div>
                                <div class="col-md-6 mb-3"><label class="form-label">Groq API Key</label><div class="input-group"><input type="password" id="edit_groq_key" name="groq_api_key" class="form-control"><button class="btn btn-outline-secondary" type="button" onclick="togglePassword('edit_groq_key')">👁️</button></div></div>
                                <div class="col-md-6 mb-3"><label class="form-label">DVLA API Key</label><div class="input-group"><input type="password" id="edit_dvla_key" name="dvla_api_key" class="form-control" placeholder="Optional — enables UK plate enrichment"><button class="btn btn-outline-secondary" type="button" onclick="togglePassword('edit_dvla_key')">👁️</button></div><div class="form-text">Register free at <a href="https://developer-portal.driver-vehicle-licensing.api.gov.uk/" target="_blank" rel="noopener">DVLA developer portal</a> to get a key.</div></div>
                            </div>
                            <hr>
                            <h6 class="text-primary">BI Encoder Recovery <span class="text-muted fw-normal small">(optional)</span></h6>
                            <div class="row">
                                <div class="col-md-6 mb-3"><label class="form-label">Recovery URL</label><input type="text" id="edit_bi_restart_url" name="bi_restart_url" class="form-control" placeholder="http://192.168.1.250:9090/restart-bi"></div>
                                <div class="col-md-6 mb-3"><label class="form-label">Recovery Token</label><div class="input-group"><input type="password" id="edit_bi_restart_token" name="bi_restart_token" class="form-control"><button class="btn btn-outline-secondary" type="button" onclick="togglePassword('edit_bi_restart_token')">&#128065;&#65039;</button></div></div>
                            </div>
                        </div>
                        <div class="tab-pane fade" id="edit-tv-pane" role="tabpanel" aria-labelledby="edit-tv-tab">
                            <h6 class="text-primary">TV Overlay Settings</h6>
                            <div class="row">
                                <div class="col-md-3 mb-3">
                                    <div class="form-check mt-2">
                                        <input class="form-check-input" type="checkbox" name="tv_push_enabled" id="edit_tv_push_enabled">
                                        <label class="form-check-label" for="edit_tv_push_enabled">Push stream to TV overlay</label>
                                    </div>
                                </div>
                                <div class="col-md-3 mb-3">
                                    <div class="form-check mt-2">
                                        <input class="form-check-input" type="checkbox" name="tv_mute_audio" id="edit_tv_mute_audio">
                                        <label class="form-check-label" for="edit_tv_mute_audio">Mute audio</label>
                                    </div>
                                </div>
                                <div class="col-md-3 mb-3"><label class="form-label">TV Group</label><input type="text" id="edit_tv_group" name="tv_group" class="form-control"></div>
                                <div class="col-md-3 mb-3"><label class="form-label">Duration (seconds)</label><input type="number" min="5" max="120" id="edit_tv_duration_seconds" name="tv_duration_seconds" class="form-control" value="20"></div>
                                <div class="col-12 mb-3">
                                    <label class="form-label">Stream Source</label>
                                    <div>
                                        <div class="form-check form-check-inline"><input class="form-check-input" type="radio" name="tv_stream_type" id="edit_stream_rtsp" value="rtsp" checked onchange="toggleEditStreamFields()"><label class="form-check-label" for="edit_stream_rtsp">RTSP (manual URL)</label></div>
                                        <div class="form-check form-check-inline"><input class="form-check-input" type="radio" name="tv_stream_type" id="edit_stream_mjpg" value="mjpg" onchange="toggleEditStreamFields()"><label class="form-check-label" for="edit_stream_mjpg">Blue Iris MJPG (via proxy)</label></div>
                                    </div>
                                </div>
                                <div id="edit_rtsp_fields" class="col-12">
                                    <div class="row">
                                        <div class="col-12 mb-3"><label class="form-label">RTSP Base URL</label><input type="text" id="edit_tv_rtsp_base_url" name="tv_rtsp_base_url" class="form-control"></div>
                                        <div class="col-md-6 mb-3"><label class="form-label">RTSP Username</label><input type="text" id="edit_tv_rtsp_username" name="tv_rtsp_username" class="form-control"></div>
                                        <div class="col-md-6 mb-3"><label class="form-label">RTSP Password</label><input type="password" id="edit_tv_rtsp_password" name="tv_rtsp_password" class="form-control"><div class="form-text">Leave blank to keep the currently saved password.</div></div>
                                    </div>
                                </div>
                                <div class="col-12 mb-3">
                                    <label class="form-label">Target TVs</label>
                                    <select id="edit_tv_target_ids" name="tv_target_ids" class="form-select" multiple size="4">
                                        {% for tv in paired_tvs %}
                                        <option value="{{ tv.id }}">{{ tv.name }}{% if tv.ip_address %} ({{ tv.ip_address }}){% endif %}</option>
                                        {% endfor %}
                                    </select>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
                <div class="modal-footer"><button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Close</button><button type="submit" class="btn btn-primary">Update</button></div>
            </form>
        </div>
    </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
<script>
const colors = [
    '#00ffff', '#00ff00', '#ff00ff', '#ffff00', '#ff8800', '#ff4444', '#8888ff',
    '#32cd32', '#1e90ff', '#ff69b4', '#ffa500', '#adff2f', '#00ced1', '#ffdab9',
    '#00ff7f', '#40e0d0', '#da70d6', '#fafad2', '#7fffd4', '#b0c4de', '#f08080',
    '#afeeee', '#ee82ee', '#98fb98'
];
function stringToColor(s){let h=0;for(let i=0;i<s.length;i++){h=s.charCodeAt(i)+((h<<5)-h);}return colors[Math.abs(h)%colors.length];}
function escapeHtml(s){return String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');}
function logSource(entry){return entry&&entry.source?entry.source:'unknown';}
function logTag(entry){return entry&&entry.alert_tag?entry.alert_tag:'';}
function colorKey(entry){
    const tag=logTag(entry);
    if(tag)return tag;
    return entry&&entry.source?entry.source:'';
}
function populateLogSourceFilter(entries){
    const select=document.getElementById('logSourceFilter');
    if(!select)return;
    const current=select.value||'all';
    const sources=[...new Set(entries.map(logSource).filter(Boolean))].sort();
    select.innerHTML='<option value="all">All sources</option>'+sources.map(s=>`<option value="${s}">${s}</option>`).join('');
    select.value=sources.includes(current)||current==='all'?current:'all';
}
function renderLogLine(entry){
    const key=colorKey(entry);
    return `<div class="log-entry-line" data-source="${escapeHtml(entry.source)}" style="color:${key?stringToColor(key):'#888'}">${escapeHtml(entry.display)}</div>`;
}
function buildGroupedLogs(entries){
    const triggerTags=new Set(entries.filter(e=>e.is_trigger&&e.alert_tag).map(e=>e.alert_tag));
    const grouped=new Map();
    const ungrouped=[];

    entries.forEach(entry=>{
        if(entry.alert_tag && triggerTags.has(entry.alert_tag)){
            if(!grouped.has(entry.alert_tag))grouped.set(entry.alert_tag, []);
            grouped.get(entry.alert_tag).push(entry);
        }else{
            ungrouped.push(entry);
        }
    });

    const blocks=[];
    const seenGroups=new Set();
    entries.forEach(entry=>{
        if(entry.alert_tag && triggerTags.has(entry.alert_tag)){
            if(seenGroups.has(entry.alert_tag))return;
            seenGroups.add(entry.alert_tag);
            const group=grouped.get(entry.alert_tag)||[];
            const trigger=group.find(e=>e.is_trigger)||group[0];
            const body=group.map(renderLogLine).join('');
            const copyTrace=encodeURIComponent(group.map(e=>e.display).join('\n'));
            blocks.push(
                `<details class="log-group" data-copy-trace="${copyTrace}"><summary><div class="log-group-summary"><span class="log-group-caret">▸</span><span class="log-group-summary-text" style="color:${stringToColor(colorKey(trigger))}">${escapeHtml(trigger.display)}</span><button type="button" class="btn btn-sm btn-outline-secondary log-group-summary-action" onclick="event.preventDefault();event.stopPropagation();copyWebhookTrace(this)">Copy Trace</button></div></summary><div class="log-group-body">${body}</div></details>`
            );
        }else{
            blocks.push(renderLogLine(entry));
        }
    });
    return blocks.join('');
}
function renderLogs(){
    const v=document.getElementById('logViewer');
    if(!v)return;
    let entries=[];
    try{
        entries=JSON.parse(v.dataset.logEntries||'[]');
    }catch(_e){
        entries=[];
    }
    populateLogSourceFilter(entries);
    const selected=(document.getElementById('logSourceFilter')||{}).value||'all';
    const filtered=selected==='all'?entries:entries.filter(e=>e.source===selected);
    let html='';
    if(!filtered.length){
        html='<div class="text-muted">No logs yet.</div>';
    }else if(selected==='all'){
        html=buildGroupedLogs(filtered);
    }else{
        html=filtered.map(renderLogLine).join('');
    }
    v.innerHTML=html;
    v.scrollTop=v.scrollHeight;
}
const html=document.documentElement;
function setTheme(t){html.setAttribute('data-bs-theme',t);localStorage.setItem('theme',t);document.getElementById('themeIcon').innerText=t==='dark'?'☀️':'🌙';}
function toggleTheme(){setTheme(html.getAttribute('data-bs-theme')==='dark'?'light':'dark');}
setTheme(localStorage.getItem('theme')||(window.matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light'));
function togglePassword(id){const el=document.getElementById(id);el.type=el.type==='password'?'text':'password';}
function toggleAddStreamFields(){const mjpg=document.getElementById('add_stream_mjpg').checked;document.getElementById('add_rtsp_fields').style.display=mjpg?'none':'';}
function toggleEditStreamFields(){const mjpg=document.getElementById('edit_stream_mjpg').checked;document.getElementById('edit_rtsp_fields').style.display=mjpg?'none':'';}
function validateTvForm(form){
    const tvEnabled=form.querySelector('[name=tv_push_enabled]')?.checked;
    if(!tvEnabled)return true;
    const biUrl=(form.querySelector('[name=bi_url]')?.value||'').trim();
    const streamType=form.querySelector('[name=tv_stream_type]:checked')?.value||'rtsp';
    const rtspUrl=(form.querySelector('[name=tv_rtsp_base_url]')?.value||'').trim();
    if(streamType==='mjpg'&&!biUrl){alert('Blue Iris URL is required when using MJPG stream source.');return false;}
    if(streamType==='rtsp'&&!rtspUrl){alert('RTSP Base URL is required when using RTSP stream source.');return false;}
    return true;
}
function withCopySuccess(btn){
    const orig=btn.innerHTML;
    const origClass=btn.className;
    btn.innerHTML='✅ Copied!';
    btn.className='btn btn-sm btn-success';
    setTimeout(()=>{btn.innerHTML=orig;btn.className=origClass;},1500);
}
function copyTextToClipboard(text,btn){
    function success(){if(btn)withCopySuccess(btn);}
    if(navigator.clipboard&&window.isSecureContext){
        navigator.clipboard.writeText(text).then(success);
    }else{
        const ta=document.createElement('textarea');
        ta.value=text;
        ta.style.position='fixed';
        ta.style.opacity='0';
        document.body.appendChild(ta);
        ta.focus();
        ta.select();
        try{
            document.execCommand('copy');
            success();
        }finally{
            document.body.removeChild(ta);
        }
    }
}
function copyWebhookTrace(btn){
    const group=btn.closest('.log-group');
    if(!group||!group.dataset.copyTrace)return;
    copyTextToClipboard(decodeURIComponent(group.dataset.copyTrace),btn);
}
function copyToClipboard(id,btn){copyTextToClipboard(document.getElementById(id).innerText.trim(),btn);}
async function testTvAlert(configId, btn){
    btn.disabled=true; btn.textContent='Sending…';
    try{
        const r=await fetch('/test-tv/'+configId, {method:'POST'});
        const d=await r.json();
        if(r.ok && d.status==='sent'){
            btn.textContent='✓ Sent';
            btn.classList.replace('btn-outline-info','btn-info');
        } else {
            btn.textContent='✗ Failed';
            btn.classList.replace('btn-outline-info','btn-danger');
        }
    } catch(e){
        btn.textContent='✗ Error';
        btn.classList.replace('btn-outline-info','btn-danger');
    }
    setTimeout(()=>{btn.disabled=false;btn.textContent='📺 Test';btn.className=btn.className.replace('btn-info','btn-outline-info').replace('btn-danger','btn-outline-info');},3000);
}
async function submitTvPairing(event){
    event.preventDefault();
    const statusEl=document.getElementById('pairTvStatus');
    statusEl.textContent='Pairing...';
    try{
        const response=await fetch('/tv/pair/code',{method:'POST',body:new FormData(event.target)});
        const data=await response.json();
        if(!response.ok) throw new Error(data.error||'Pairing failed');
        statusEl.textContent='Paired';
        window.location.reload();
    }catch(err){
        statusEl.textContent=err.message||'Pairing failed';
    }
    return false;
}
function openEditModal(c){
    document.getElementById('edit_name').value=c.name;
    document.getElementById('edit_gemini_key').value=c.gemini_key;
    document.getElementById('edit_telegram_token').value=c.telegram_token;
    document.getElementById('edit_chat_id').value=c.chat_id;
    document.getElementById('edit_message_thread_id').value=c.message_thread_id||'';
    document.getElementById('edit_prompt').value=c.prompt;
    document.getElementById('edit_bi_url').value=c.bi_url||'';
    document.getElementById('edit_bi_user').value=c.bi_user||'';
    document.getElementById('edit_bi_pass').value=c.bi_pass||'';
    document.getElementById('edit_send_video').checked=c.send_video===1;
    document.getElementById('edit_delete_after_send').checked=c.delete_after_send===1;
    document.getElementById('edit_instant_notify').checked=c.instant_notify===1;
    document.getElementById('edit_verbose_logging').checked=c.verbose_logging===1;
    document.getElementById('edit_tv_push_enabled').checked=c.tv_push_enabled===1;
    document.getElementById('edit_tv_mute_audio').checked=c.tv_mute_audio===1;
    document.getElementById('edit_tv_group').value=c.tv_group||'';
    const streamType=c.tv_stream_type||'rtsp';
    document.getElementById(streamType==='mjpg'?'edit_stream_mjpg':'edit_stream_rtsp').checked=true;
    toggleEditStreamFields();
    document.getElementById('edit_tv_rtsp_base_url').value=c.tv_rtsp_base_url||'';
    document.getElementById('edit_tv_rtsp_username').value=c.tv_rtsp_username||'';
    document.getElementById('edit_tv_rtsp_password').value='';
    document.getElementById('edit_tv_duration_seconds').value=c.tv_duration_seconds||20;
    Array.from(document.getElementById('edit_tv_target_ids').options).forEach(opt=>{opt.selected=(c.tv_target_ids||[]).includes(opt.value);});
    document.getElementById('edit_grok_key').value=c.grok_api_key||'';
    document.getElementById('edit_groq_key').value=c.groq_api_key||'';
    document.getElementById('edit_dvla_key').value=c.dvla_api_key||'';
    document.getElementById('edit_bi_restart_url').value=c.bi_restart_url||'';
    document.getElementById('edit_bi_restart_token').value=c.bi_restart_token||'';
    ['edit_gemini_key','edit_telegram_token','edit_bi_pass','edit_tv_rtsp_password','edit_grok_key','edit_groq_key','edit_dvla_key','edit_bi_restart_token'].forEach(id=>document.getElementById(id).type='password');
    document.getElementById('editForm').action='/edit/'+c.id;
    new bootstrap.Modal(document.getElementById('editModal')).show();
}
document.addEventListener('DOMContentLoaded',()=>{
    const savedTab=localStorage.getItem('activeTab');
    if(savedTab){const t=document.querySelector(`[data-bs-target="${savedTab}"]`);if(t)new bootstrap.Tab(t).show();}
    document.querySelectorAll('[data-bs-toggle="tab"]').forEach(el=>el.addEventListener('click',e=>localStorage.setItem('activeTab',e.target.getAttribute('data-bs-target'))));
    document.querySelector('[data-bs-target="#logs-pane"]').addEventListener('shown.bs.tab',()=>{const v=document.getElementById('logViewer');if(v)v.scrollTop=v.scrollHeight;});
    const logSourceFilter=document.getElementById('logSourceFilter');
    if(logSourceFilter)logSourceFilter.addEventListener('change',renderLogs);
    renderLogs();
    function relativeTime(ts){const diff=Math.floor((Date.now()-new Date(ts.replace(' ','T')))/1000);if(diff<60)return diff+'s ago';if(diff<3600)return Math.floor(diff/60)+'m ago';if(diff<86400)return Math.floor(diff/3600)+'h ago';return Math.floor(diff/86400)+'d ago';}
    document.querySelectorAll('.last-seen[data-ts]').forEach(el=>{el.textContent='Last triggered: '+relativeTime(el.dataset.ts);el.title=el.dataset.ts;});
    fetch('/api/check-update').then(r=>r.json()).then(d=>{
        const dismissedVersion = localStorage.getItem('dismissedUpdate');
        if(d.update_available && dismissedVersion !== d.latest_version){
            document.getElementById('update-version').textContent = d.latest_version;
            document.getElementById('update-link').href = d.release_url || 'https://github.com/slflowfoon/blueiris-ai-hub/releases';
            document.getElementById('update-banner').classList.remove('d-none');
            document.getElementById('dismiss-update').addEventListener('click', () => {
                localStorage.setItem('dismissedUpdate', d.latest_version);
            });
        }
    }).catch(()=>{});
});
</script>
</body>
</html>
"""


# --- ROUTES ---

@app.route('/')
def index():
    conn = get_db_connection()
    configs = [dict(r) for r in conn.execute('SELECT * FROM configs ORDER BY created_at DESC').fetchall()]
    plate_audit = [dict(r) for r in conn.execute('SELECT * FROM plate_audit ORDER BY last_seen DESC').fetchall()]
    paired_tvs = [dict(r) for r in conn.execute(
        """
        SELECT id, name, ip_address, port, rtsp_url, device_token_id,
               last_seen_at, last_ip_seen, created_at
        FROM paired_tvs
        ORDER BY COALESCE(created_at, '') DESC, id DESC
        """
    ).fetchall()]
    conn.close()
    for config in configs:
        config["tv_target_ids"] = _load_tv_target_ids(config["id"], config["name"])
        rtsp_parts = _split_rtsp_url(config.get("tv_rtsp_url"))
        config["tv_rtsp_base_url"] = rtsp_parts["base_url"]
        config["tv_rtsp_username"] = rtsp_parts["username"]

    primary_chat_id = configs[0]['chat_id'] if configs else ''
    mutes = get_mute_status(primary_chat_id) if primary_chat_id else []
    caption_mode = get_caption_mode(primary_chat_id) if primary_chat_id else None
    known_plates = load_known_plates()
    global_settings = get_global_settings()
    tv_apk_download_path = url_for('download_tv_overlay_apk')
    tv_apk_download_url = (
        f"{BASE_URL}{tv_apk_download_path}"
        if BASE_URL
        else f"{request.host_url.rstrip('/')}{tv_apk_download_path}"
    )

    return render_template_string(
        HTML_TEMPLATE,
        configs=configs,
        plate_audit=plate_audit,
        paired_tvs=paired_tvs,
        log_entries=get_log_entries(),
        base_url=BASE_URL,
        mutes=mutes,
        caption_mode=caption_mode,
        primary_chat_id=primary_chat_id,
        known_plates=known_plates,
        global_settings=global_settings,
        tv_apk_download_url=tv_apk_download_url,
        current_version=CURRENT_VERSION,
    )


@app.route('/health')
def health():
    redis_health = get_redis_health()
    status_code = 200 if redis_health["status"] == "ok" else 503
    return jsonify({"status": "ok" if status_code == 200 else "degraded", "redis": redis_health}), status_code


@app.route('/status')
def status():
    redis_health = get_redis_health()
    pipeline = get_pipeline_status()
    return jsonify({
        "status": "ok" if redis_health["status"] == "ok" else "degraded",
        "version": CURRENT_VERSION,
        "redis": redis_health,
        "pipeline": pipeline,
    }), 200 if redis_health["status"] == "ok" else 503


@app.route('/api/check-update')
def check_update_api():
    """Flask route that uses Redis caching and calls get_update_status."""
    cached = r.get(UPDATE_CHECK_CACHE_KEY)
    if cached:
        try:
            data = json.loads(cached)
            if data.get("current_version") == CURRENT_VERSION:
                return jsonify(data)
        except Exception:
            pass
            
    result = get_update_status()
    
    r.set(UPDATE_CHECK_CACHE_KEY, json.dumps(result), ex=UPDATE_CHECK_TTL)
    return jsonify(result)


@app.route('/add', methods=['POST'])
def add_config():
    try:
        camera_id = str(uuid.uuid4())
        tv_push_enabled = 1 if 'tv_push_enabled' in request.form else 0
        tv_mute_audio = 1 if 'tv_mute_audio' in request.form else 0
        tv_rtsp_url = _compose_rtsp_url(
            request.form.get('tv_rtsp_base_url'),
            request.form.get('tv_rtsp_username'),
            request.form.get('tv_rtsp_password'),
        )
        tv_duration_seconds = _parse_tv_duration_seconds(request.form.get('tv_duration_seconds'))
        tv_group = request.form.get('tv_group') or None
        conn = get_db_connection()
        conn.execute(
            'INSERT INTO configs (id,name,gemini_key,telegram_token,chat_id,prompt,bi_url,bi_user,bi_pass,'
            'send_video,verbose_logging,delete_after_send,message_thread_id,grok_api_key,groq_api_key,'
            'bi_restart_url,bi_restart_token,instant_notify,dvla_api_key,tv_push_enabled,tv_rtsp_url,'
            'tv_duration_seconds,tv_group,tv_mute_audio,tv_stream_type) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (camera_id, request.form['name'], request.form['gemini_key'],
             request.form['telegram_token'], request.form['chat_id'], request.form['prompt'],
             request.form.get('bi_url'), request.form.get('bi_user'), request.form.get('bi_pass'),
             1 if 'send_video' in request.form else 0,
             1 if 'verbose_logging' in request.form else 0,
             1 if 'delete_after_send' in request.form else 0,
             request.form.get('message_thread_id') or None,
             request.form.get('grok_api_key') or None,
             request.form.get('groq_api_key') or None,
             request.form.get('bi_restart_url') or None,
             request.form.get('bi_restart_token') or None,
             1 if 'instant_notify' in request.form else 0,
             request.form.get('dvla_api_key') or None,
             tv_push_enabled,
             tv_rtsp_url,
             tv_duration_seconds,
             tv_group,
             tv_mute_audio,
             request.form.get('tv_stream_type', 'rtsp'))
        )
        conn.commit()
        conn.close()
        _save_tv_targets(camera_id, request.form['name'], request.form.getlist('tv_target_ids'))
        flash('Configuration added!', 'success')
    except Exception as e:
        flash(f'Error: {e}', 'danger')
    return redirect(url_for('index'))


@app.route('/edit/<id>', methods=['POST'])
def edit_config(id):
    try:
        conn = get_db_connection()
        existing = conn.execute(
            """
            SELECT name, tv_push_enabled, tv_rtsp_url, tv_duration_seconds, tv_group, tv_mute_audio, tv_stream_type
            FROM configs
            WHERE id=?
            """,
            (id,),
        ).fetchone()
        tv_section_present = any(key.startswith("tv_") for key in request.form)
        if tv_section_present:
            tv_push_enabled = 1 if 'tv_push_enabled' in request.form else 0
            tv_mute_audio = 1 if 'tv_mute_audio' in request.form else 0
            tv_rtsp_url = _compose_rtsp_url(
                request.form.get('tv_rtsp_base_url'),
                request.form.get('tv_rtsp_username'),
                request.form.get('tv_rtsp_password'),
                existing_url=existing['tv_rtsp_url'] if existing else None,
            )
            tv_duration_seconds = _parse_tv_duration_seconds(request.form.get('tv_duration_seconds'))
            tv_group = request.form.get('tv_group') or None
            tv_stream_type = request.form.get('tv_stream_type', 'rtsp')
        else:
            tv_push_enabled = existing['tv_push_enabled'] if existing else 0
            tv_mute_audio = existing['tv_mute_audio'] if existing else 0
            tv_rtsp_url = existing['tv_rtsp_url'] if existing else None
            tv_duration_seconds = existing['tv_duration_seconds'] if existing else None
            tv_group = existing['tv_group'] if existing else None
            tv_stream_type = existing['tv_stream_type'] if existing else 'rtsp'
        conn.execute(
            'UPDATE configs SET name=?,gemini_key=?,telegram_token=?,chat_id=?,prompt=?,bi_url=?,bi_user=?,'
            'bi_pass=?,send_video=?,verbose_logging=?,delete_after_send=?,message_thread_id=?,'
            'grok_api_key=?,groq_api_key=?,bi_restart_url=?,bi_restart_token=?,instant_notify=?,'
            'dvla_api_key=?,tv_push_enabled=?,tv_rtsp_url=?,tv_duration_seconds=?,tv_group=?,'
            'tv_mute_audio=?,tv_stream_type=? WHERE id=?',
            (request.form['name'], request.form['gemini_key'], request.form['telegram_token'],
             request.form['chat_id'], request.form['prompt'],
             request.form.get('bi_url'), request.form.get('bi_user'), request.form.get('bi_pass'),
             1 if 'send_video' in request.form else 0,
             1 if 'verbose_logging' in request.form else 0,
             1 if 'delete_after_send' in request.form else 0,
             request.form.get('message_thread_id') or None,
             request.form.get('grok_api_key') or None,
             request.form.get('groq_api_key') or None,
             request.form.get('bi_restart_url') or None,
             request.form.get('bi_restart_token') or None,
             1 if 'instant_notify' in request.form else 0,
             request.form.get('dvla_api_key') or None,
             tv_push_enabled,
             tv_rtsp_url,
             tv_duration_seconds,
             tv_group,
             tv_mute_audio,
             tv_stream_type,
             id)
        )
        conn.commit()
        conn.close()
        _save_tv_targets(id, request.form['name'], request.form.getlist('tv_target_ids'))
        flash('Configuration updated!', 'success')
    except Exception as e:
        flash(f'Error: {e}', 'danger')
    return redirect(url_for('index'))


@app.route('/delete/<id>', methods=['POST'])
def delete_config(id):
    conn = get_db_connection()
    row = conn.execute('SELECT name FROM configs WHERE id=?', (id,)).fetchone()
    if row:
        conn.execute(
            """
            DELETE FROM camera_tv_targets
            WHERE (camera_id IS NOT NULL AND camera_id=?)
               OR (camera_id IS NULL AND camera_name=?)
            """,
            (id, row['name']),
        )
    conn.execute('DELETE FROM configs WHERE id=?', (id,))
    conn.commit()
    conn.close()
    flash('Configuration deleted.', 'warning')
    return redirect(url_for('index'))


@app.route('/test-tv/<id>', methods=['POST'])
def test_tv_alert(id):
    import tv_delivery
    conn = get_db_connection()
    row = conn.execute(
        "SELECT id, name, tv_push_enabled, tv_rtsp_url, tv_duration_seconds, tv_group, tv_mute_audio, tv_stream_type, bi_url, bi_user, bi_pass FROM configs WHERE id=?",
        (id,),
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "config not found"}), 404

    config = dict(row)
    stream_type = config.get("tv_stream_type") or "rtsp"
    bi_url = (config.get("bi_url") or "").strip()
    rtsp_url = config.get("tv_rtsp_url") or ""

    if stream_type == "mjpg" and not bi_url:
        return jsonify({"error": "MJPG selected but bi_url not configured"}), 400
    if stream_type == "rtsp" and not rtsp_url:
        return jsonify({"error": "RTSP selected but tv_rtsp_url not configured"}), 400

    dispatch_config = {
        **config,
        "tv_duration_seconds": config.get("tv_duration_seconds"),
        "request_id": "test",
    }
    tag = f"[test-tv:{config['name']}]"
    result = tv_delivery.dispatch_tv_alert(dispatch_config, tag)
    if result.get("error") or not result.get("delivered"):
        failed_targets = ",".join(result.get("failed") or [])
        if result.get("error"):
            reason = "dispatch_error"
        elif stream_type == "mjpg" and not tv_delivery.BASE_URL:
            reason = "missing_base_url"
        elif result.get("skipped"):
            reason = "skipped_no_stream_url"
        elif failed_targets:
            reason = "delivery_failed"
        else:
            reason = "no_target_tvs"
        logging.warning(
            "%s test dispatch failed reason=%s failed_targets=%s",
            tag,
            reason,
            failed_targets or "none",
        )
        return jsonify({"error": "dispatch failed"}), 502
    return jsonify({"status": "sent"}), 200


@app.route('/tv/pair/code', methods=['POST'])
def pair_tv_by_code():
    manual_code = request.form.get('manual_code', '').strip().upper()
    if not manual_code:
        return jsonify({"error": "manual_code is required"}), 400

    ip_address = request.form.get('ip_address', '').strip()
    port_raw = request.form.get('port', '').strip()
    port = int(port_raw) if port_raw.isdigit() else 7979

    try:
        if ip_address:
            from tv_delivery import pair_remote_tv_by_code
            tv_id = pair_remote_tv_by_code(ip_address, manual_code, port=port)
        else:
            from tv_delivery import finalize_pairing_by_code
            tv_id = finalize_pairing_by_code(manual_code)
    except ValueError:
        return jsonify({"error": "invalid tv pairing request"}), 400
    except Exception:
        app.logger.exception("TV pairing failed")
        return jsonify({"error": "tv pairing failed"}), 500

    return jsonify({"status": "paired", "tv_id": tv_id}), 200


@app.route('/tv/devices', methods=['GET'])
def list_paired_tvs():
    conn = get_db_connection()
    rows = conn.execute(
        """
        SELECT id, name, ip_address, port, rtsp_url, device_token_id,
               last_seen_at, last_ip_seen, created_at
        FROM paired_tvs
        ORDER BY COALESCE(created_at, '') DESC, id DESC
        """
    ).fetchall()
    conn.close()
    return jsonify({"devices": [dict(row) for row in rows]}), 200


@app.route('/tv/devices/<tv_id>/delete', methods=['POST'])
def delete_paired_tv(tv_id):
    from tv_delivery import delete_paired_tv as delete_paired_tv_helper

    deleted = delete_paired_tv_helper(tv_id)
    if not deleted:
        flash('TV not found.', 'warning')
        return redirect(url_for('index') + '#tv-groups-pane')
    flash('TV deleted.', 'warning')
    return redirect(url_for('index') + '#tv-groups-pane')


@app.route('/tv/groups/<group_name>/priority', methods=['POST'])
def save_tv_group_priority(group_name):
    payload = request.get_json(silent=True) or {}
    camera_ids = payload.get('camera_ids')
    if not isinstance(camera_ids, list):
        return jsonify({"error": "camera_ids must be a list"}), 400
    from tv_delivery import save_group_priority
    save_group_priority(group_name, camera_ids)
    return jsonify({"status": "ok"}), 200


@app.route('/clear_logs', methods=['POST'])
def clear_logs():
    try:
        for name in os.listdir(LOG_DIR):
            if name.endswith(".log"):
                open(os.path.join(LOG_DIR, name), 'w').close()
        flash('Logs cleared.', 'success')
    except Exception as e:
        flash(f'Error: {e}', 'danger')
    return redirect(url_for('index'))


# --- Global settings routes ---

@app.route('/plates/add', methods=['POST'])
def add_plate():
    plate = request.form.get('plate', '').strip().upper()
    label = request.form.get('label', '').strip()
    if plate and label:
        plates = load_known_plates()
        plates[plate] = label
        save_known_plates(plates)
        flash(f'Added plate {plate}.', 'success')
    return redirect(url_for('index') + '#global-pane')


@app.route('/settings/global', methods=['POST'])
def save_global_settings_route():
    save_global_settings(
        {
            "auto_mute_threshold": request.form.get("auto_mute_threshold"),
            "auto_mute_window_minutes": request.form.get("auto_mute_window_minutes"),
            "auto_mute_duration_minutes": request.form.get("auto_mute_duration_minutes"),
        }
    )
    flash('Global settings updated.', 'success')
    return redirect(url_for('index') + '#global-pane')


@app.route('/plates/delete', methods=['POST'])
def delete_plate():
    plate = request.form.get('plate', '').strip()
    if plate:
        plates = load_known_plates()
        plates.pop(plate, None)
        save_known_plates(plates)
        flash(f'Removed plate {plate}.', 'warning')
    return redirect(url_for('index') + '#global-pane')


@app.route('/mute/clear', methods=['POST'])
def clear_mute():
    key = request.form.get('redis_key', '')
    if key and key.startswith('mute:'):
        r.delete(key)
        flash('Mute removed.', 'success')
    return redirect(url_for('index'))


@app.route('/caption/clear', methods=['POST'])
def clear_caption():
    chat_id = request.form.get('chat_id', '')
    if chat_id:
        r.delete(f'caption_mode:{chat_id}')
        flash('Caption mode reset to normal.', 'success')
    return redirect(url_for('index'))


# --- Plate Audit ---

@app.route('/plate-audit/image/<filename>')
def plate_audit_image(filename):
    safe = secure_filename(filename)
    if not safe:
        abort(404)
    return send_file(os.path.join(PLATE_IMAGES_DIR, safe), mimetype='image/jpeg')


@app.route('/downloads/android-tv-overlay-debug.apk')
def download_tv_overlay_apk():
    if TV_OVERLAY_APK_URL:
        return redirect(TV_OVERLAY_APK_URL, code=302)

    try:
        r = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
            headers={"Accept": "application/vnd.github+json"},
            timeout=5,
        )
        if r.ok:
            for asset in r.json().get("assets", []):
                if asset["name"].endswith(".apk"):
                    return redirect(asset["browser_download_url"], code=302)
    except Exception:
        pass
    return jsonify({
        "error": "android tv overlay apk not found — no PR override URL configured and no GitHub release APK found"
    }), 404


@app.route('/plate-audit/delete', methods=['POST'])
def delete_plate_audit():
    entry_id = request.form.get('id')
    conn = get_db_connection()
    row = conn.execute("SELECT image_filename FROM plate_audit WHERE id=?", (entry_id,)).fetchone()
    if row and row['image_filename']:
        img_path = os.path.join(PLATE_IMAGES_DIR, row['image_filename'])
        if os.path.exists(img_path):
            os.remove(img_path)
    conn.execute("DELETE FROM plate_audit WHERE id=?", (entry_id,))
    conn.commit()
    conn.close()
    return redirect(url_for('index'))


# --- Webhook ---

@app.route('/webhook/<config_id>', methods=['POST'])
def webhook(config_id):
    if 'image' not in request.files:
        app.logger.error("Webhook received but no image provided.")
        return jsonify({"error": "No image provided"}), 400

    conn = get_db_connection()
    row = conn.execute("SELECT * FROM configs WHERE id=?", (config_id,)).fetchone()
    if not row:
        conn.close()
        app.logger.warning(f"Invalid webhook ID: {config_id}")
        return jsonify({"error": "Invalid webhook ID"}), 404

    config = dict(row)
    request_id = uuid.uuid4().hex[:8]
    tag = f"[{config['name']}][{request_id}]"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("UPDATE configs SET last_triggered=? WHERE id=?", (now_str, config_id))
    conn.commit()
    conn.close()

    filename = f"{TEMP_IMAGE_DIR}/{uuid.uuid4()}.jpg"
    original_filename = request.files['image'].filename
    config['trigger_filename'] = secure_filename(original_filename)
    config['request_id'] = request_id

    bvr = request.form.get('bvr', '').strip()
    if bvr:
        dedup_key = f"clip_dedup:{bvr}:{config['trigger_filename']}"
        if not r.set(dedup_key, 1, nx=True, ex=30):
            app.logger.info(f"{tag} Duplicate webhook for clip {bvr} — skipping.")
            return jsonify({"status": "duplicate", "camera": config['name']}), 200
        config['bvr_clip'] = bvr

    try:
        request.files['image'].save(filename)
        app.logger.info(f"{tag} Webhook triggered. File: {original_filename}")
        q.enqueue(process_alert, filename, config, job_timeout=600)

    except Exception:
        app.logger.exception("Error processing webhook")
        return jsonify({"error": "Internal server error"}), 500

    return jsonify({"status": "queued", "camera": config['name']}), 200


if __name__ == '__main__':
    debug_mode = os.getenv("FLASK_DEBUG", "False").lower() == "true"
    app.run(host='0.0.0.0', port=5000, debug=debug_mode)
