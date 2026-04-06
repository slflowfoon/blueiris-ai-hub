import os
import uuid
import json
import sqlite3
import redis
import logging
import requests
import time
from datetime import datetime, timedelta
from rq import Queue
from flask import Flask, request, jsonify, render_template_string, redirect, url_for, flash
from tasks import process_alert
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "super_secret_key_change_this")

BASE_URL = os.getenv('BASE_URL')
if BASE_URL and BASE_URL.endswith('/'):
    BASE_URL = BASE_URL[:-1]

redis_url = os.getenv('REDIS_URL', 'redis://redis:6379/0')
r = redis.from_url(redis_url)
q = Queue(connection=r)

DATA_DIR = "/app/data"
DB_FILE = os.path.join(DATA_DIR, "configs.db")
KNOWN_PLATES_FILE = os.path.join(DATA_DIR, "known_plates.json")
TEMP_IMAGE_DIR = "/tmp_images"
LOG_FILE = "/app/logs/system.log"

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(TEMP_IMAGE_DIR, exist_ok=True)
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

app.logger.setLevel(logging.INFO)

# --- VERSION ---
try:
    with open('/app/VERSION') as _f:
        CURRENT_VERSION = _f.read().strip()
except Exception:
    CURRENT_VERSION = "unknown"

GITHUB_REPO = "slflowfoon/blueiris-ai-hub"
UPDATE_CHECK_CACHE_KEY = "update_check"
UPDATE_CHECK_TTL = 3600  # 1 hour


# --- DATABASE ---

def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with sqlite3.connect(DB_FILE) as conn:
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
                groq_api_key TEXT
            )
        """)
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
        ]:
            try:
                conn.execute(f"SELECT {col} FROM configs LIMIT 1")
            except sqlite3.OperationalError:
                conn.execute(f"ALTER TABLE configs ADD COLUMN {col} {definition}")

init_db()


# --- HELPERS ---

def get_log_content():
    if not os.path.exists(LOG_FILE):
        return "No logs yet."
    try:
        with open(LOG_FILE) as f:
            return "".join(f.readlines()[-200:])
    except Exception as e:
        return f"Error reading logs: {e}"


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

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en" data-bs-theme="light">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Blue Iris AI Hub</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        .card { box-shadow: 0 4px 6px rgba(0,0,0,0.1); border: 1px solid var(--bs-border-color); }
        .webhook-box { background: var(--bs-tertiary-bg); padding: 10px; border-radius: 5px; font-family: monospace; word-break: break-all; border: 1px solid var(--bs-border-color); color: var(--bs-body-color); }
        .log-viewer { background-color: #121212; color: #e0e0e0; font-family: monospace; padding: 15px; border-radius: 5px; height: 600px; overflow-y: scroll; white-space: pre-wrap; font-size: 0.85em; border: 1px solid var(--bs-border-color); line-height: 1.4; }
        .nav-tabs .nav-link { cursor: pointer; }
        .nav-tabs .nav-link.active { font-weight: bold; }
        .status-badge { font-size: 0.8em; }
        .last-seen { font-size: 0.85em; color: var(--bs-secondary-color); }
        body, .card, .webhook-box, .modal-content { transition: background-color 0.3s, color 0.3s; }
        .mute-badge { font-size: 0.75em; }
    </style>
</head>
<body>
<div class="container py-5">
    <div class="d-flex justify-content-between align-items-center mb-4">
        <div>
            <h1 class="h3 text-primary d-inline-block me-2">Blue Iris AI Hub</h1>
            <span class="badge bg-success status-badge align-middle">System Online</span>
            <span class="badge bg-info status-badge align-middle ms-1">v{{ current_version }}</span>
        </div>
        <div class="d-flex gap-2">
            <button class="btn btn-outline-secondary" onclick="toggleTheme()" id="themeBtn"><span id="themeIcon">🌙</span></button>
            <button class="btn btn-primary" data-bs-toggle="modal" data-bs-target="#addModal">+ New Configuration</button>
        </div>
    </div>

    <!-- Update banner — shown by JS if a newer version is available on GitHub -->
    <div id="update-banner" class="alert alert-warning alert-dismissible fade show d-none mb-3" role="alert">
        <strong>Update available!</strong>
        v<span id="update-version"></span> is available on
        <a id="update-link" href="#" target="_blank" rel="noopener">GitHub</a>.
        Run <code>docker compose pull && docker compose up -d</code> to update.
        <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
    </div>

    {% with messages = get_flashed_messages(with_categories=true) %}
      {% if messages %}{% for category, message in messages %}
        <div class="alert alert-{{ category }}">{{ message }}</div>
      {% endfor %}{% endif %}
    {% endwith %}

    <ul class="nav nav-tabs mb-4" id="mainTab">
        <li class="nav-item"><button class="nav-link active" data-bs-toggle="tab" data-bs-target="#configs-pane">Configurations</button></li>
        <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#global-pane">Global Settings</button></li>
        <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#logs-pane">Logs</button></li>
    </ul>

    <div class="tab-content">

        <!-- Configurations tab -->
        <div class="tab-pane fade show active" id="configs-pane">
            <div class="p-3 mb-3 bg-light border rounded">
                <strong>Blue Iris Setup — On Alert action (per camera):</strong>
                <ol class="mb-0 mt-2 ps-3">
                    <li>In Blue Iris, open camera settings and go to the <strong>Alerts</strong> tab.</li>
                    <li>Under <strong>On alert</strong>, add a <em>Run a program or write to a file</em> action.</li>
                    <li>Set <strong>Action</strong> to <code>Run program/script</code>.</li>
                    <li>Set <strong>File</strong> to <code>curl.exe</code>.</li>
                    <li>Set <strong>Parameters</strong> to the <em>Blue Iris Notification Parameter</em> for this config (with <code>&lt;AlertsFolder&gt;</code> replaced).</li>
                    <li>Set <strong>Window</strong> to <code>Hide</code>.</li>
                    <li>Set <strong>Camera</strong> to the camera you are configuring (e.g. <em>Driveway</em>).</li>
                    <li>Uncheck <strong>Also execute on Remote Management</strong> and <strong>Wait for process to complete</strong>.</li>
                </ol>
            </div>
            <div class="row">
                {% for config in configs %}
                <div class="col-md-6 mb-4">
                    <div class="card h-100">
                        <div class="card-body">
                            <div class="d-flex justify-content-between align-items-start">
                                <div>
                                    <h5 class="card-title mb-1">{{ config.name }}</h5>
                                    <span class="last-seen">{% if config.last_triggered %}Last triggered: {{ config.last_triggered }}{% else %}Never triggered{% endif %}</span>
                                </div>
                                <div class="btn-group">
                                    <button class="btn btn-sm btn-outline-secondary" onclick='openEditModal({{ config|tojson }})'>Edit</button>
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
                                {% if config.message_thread_id %}<span class="badge bg-secondary">🧵 Thread {{ config.message_thread_id }}</span>{% endif %}
                                {% if config.grok_api_key %}<span class="badge bg-warning text-dark">Grok ✓</span>{% endif %}
                                {% if config.groq_api_key %}<span class="badge bg-warning text-dark">Groq ✓</span>{% endif %}
                            </div>
                        </div>
                    </div>
                </div>
                {% else %}
                <div class="col-12 text-center py-5"><h4 class="text-muted">No configurations yet.</h4></div>
                {% endfor %}
            </div>
        </div>

        <!-- Global Settings tab -->
        <div class="tab-pane fade" id="global-pane">
            <div class="row">

                <!-- Mute Status -->
                <div class="col-md-6 mb-4">
                    <div class="card h-100">
                        <div class="card-body">
                            <h5 class="card-title">🔇 Mute Status</h5>
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
                </div>

                <!-- Known Plates -->
                <div class="col-md-6 mb-4">
                    <div class="card h-100">
                        <div class="card-body">
                            <h5 class="card-title">🚗 Known Plates</h5>
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
        </div>

        <!-- Logs tab -->
        <div class="tab-pane fade" id="logs-pane">
            <div class="d-flex justify-content-between mb-2">
                <h5>Live Logs (last 200 lines)</h5>
                <div>
                    <a href="{{ url_for('index') }}" class="btn btn-sm btn-outline-secondary">🔄 Refresh</a>
                    <form action="{{ url_for('clear_logs') }}" method="POST" style="display:inline;">
                        <button class="btn btn-sm btn-outline-danger">🗑️ Clear</button>
                    </form>
                </div>
            </div>
            <div class="log-viewer" id="logViewer">{{ logs }}</div>
        </div>

    </div>
</div>

<!-- Add Modal -->
<div class="modal fade" id="addModal" tabindex="-1">
    <div class="modal-dialog modal-lg">
        <div class="modal-content">
            <form action="{{ url_for('add_config') }}" method="POST">
                <div class="modal-header"><h5 class="modal-title">Add Configuration</h5><button type="button" class="btn-close" data-bs-dismiss="modal"></button></div>
                <div class="modal-body">
                    <div class="mb-3"><label class="form-label">Name</label><input type="text" name="name" class="form-control" placeholder="e.g. Driveway" required></div>
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
                    <h6 class="text-primary">Blue Iris Video Settings</h6>
                    <div class="row">
                        <div class="col-md-6 mb-3"><label class="form-label">Blue Iris URL</label><input type="text" name="bi_url" class="form-control" placeholder="http://192.168.0.11:81"></div>
                        <div class="col-md-6 mb-3">
                            <div class="form-check mt-4"><input class="form-check-input" type="checkbox" name="send_video" id="add_send_video"><label class="form-check-label" for="add_send_video">Fetch &amp; send video clip</label></div>
                            <div class="form-check"><input class="form-check-input" type="checkbox" name="delete_after_send" id="add_delete_after_send" checked><label class="form-check-label" for="add_delete_after_send">Delete clip from BI after send</label></div>
                            <div class="form-check"><input class="form-check-input" type="checkbox" name="verbose_logging" id="add_verbose_logging"><label class="form-check-label" for="add_verbose_logging">Verbose logging</label></div>
                        </div>
                    </div>
                    <div class="row">
                        <div class="col-md-6 mb-3"><label class="form-label">BI Username</label><input type="text" name="bi_user" class="form-control"></div>
                        <div class="col-md-6 mb-3"><label class="form-label">BI Password</label><div class="input-group"><input type="password" name="bi_pass" id="add_bi_pass" class="form-control"><button class="btn btn-outline-secondary" type="button" onclick="togglePassword('add_bi_pass')">👁️</button></div></div>
                    </div>
                    <hr>
                    <h6 class="text-primary">AI Fallback Keys <span class="text-muted fw-normal small">(optional)</span></h6>
                    <div class="row">
                        <div class="col-md-6 mb-3"><label class="form-label">Grok API Key</label><div class="input-group"><input type="password" name="grok_api_key" id="add_grok_key" class="form-control"><button class="btn btn-outline-secondary" type="button" onclick="togglePassword('add_grok_key')">👁️</button></div></div>
                        <div class="col-md-6 mb-3"><label class="form-label">Groq API Key</label><div class="input-group"><input type="password" name="groq_api_key" id="add_groq_key" class="form-control"><button class="btn btn-outline-secondary" type="button" onclick="togglePassword('add_groq_key')">👁️</button></div></div>
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
            <form id="editForm" method="POST">
                <div class="modal-header"><h5 class="modal-title">Edit Configuration</h5><button type="button" class="btn-close" data-bs-dismiss="modal"></button></div>
                <div class="modal-body">
                    <div class="mb-3"><label class="form-label">Name</label><input type="text" id="edit_name" name="name" class="form-control" required></div>
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
                    <h6 class="text-primary">Blue Iris Video Settings</h6>
                    <div class="row">
                        <div class="col-md-6 mb-3"><label class="form-label">Blue Iris URL</label><input type="text" id="edit_bi_url" name="bi_url" class="form-control"></div>
                        <div class="col-md-6 mb-3">
                            <div class="form-check mt-4"><input class="form-check-input" type="checkbox" name="send_video" id="edit_send_video"><label class="form-check-label" for="edit_send_video">Fetch &amp; send video clip</label></div>
                            <div class="form-check"><input class="form-check-input" type="checkbox" name="delete_after_send" id="edit_delete_after_send"><label class="form-check-label" for="edit_delete_after_send">Delete clip from BI after send</label></div>
                            <div class="form-check"><input class="form-check-input" type="checkbox" name="verbose_logging" id="edit_verbose_logging"><label class="form-check-label" for="edit_verbose_logging">Verbose logging</label></div>
                        </div>
                    </div>
                    <div class="row">
                        <div class="col-md-6 mb-3"><label class="form-label">BI Username</label><input type="text" id="edit_bi_user" name="bi_user" class="form-control"></div>
                        <div class="col-md-6 mb-3"><label class="form-label">BI Password</label><div class="input-group"><input type="password" id="edit_bi_pass" name="bi_pass" class="form-control"><button class="btn btn-outline-secondary" type="button" onclick="togglePassword('edit_bi_pass')">👁️</button></div></div>
                    </div>
                    <hr>
                    <h6 class="text-primary">AI Fallback Keys <span class="text-muted fw-normal small">(optional)</span></h6>
                    <div class="row">
                        <div class="col-md-6 mb-3"><label class="form-label">Grok API Key</label><div class="input-group"><input type="password" id="edit_grok_key" name="grok_api_key" class="form-control"><button class="btn btn-outline-secondary" type="button" onclick="togglePassword('edit_grok_key')">👁️</button></div></div>
                        <div class="col-md-6 mb-3"><label class="form-label">Groq API Key</label><div class="input-group"><input type="password" id="edit_groq_key" name="groq_api_key" class="form-control"><button class="btn btn-outline-secondary" type="button" onclick="togglePassword('edit_groq_key')">👁️</button></div></div>
                    </div>
                </div>
                <div class="modal-footer"><button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Close</button><button type="submit" class="btn btn-primary">Update</button></div>
            </form>
        </div>
    </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
<script>
const colors = ['#00ffff','#00ff00','#ff00ff','#ffff00','#ff8800','#ff4444','#8888ff'];
function stringToColor(s){let h=0;for(let i=0;i<s.length;i++){h=s.charCodeAt(i)+((h<<5)-h);}return colors[Math.abs(h)%colors.length];}
function colorizeLogs(){const v=document.getElementById('logViewer');if(!v)return;const lines=v.innerText.split('\\n');let html='';lines.forEach(l=>{if(!l.trim())return;const m=l.match(/\[(.*?)\]/);html+=`<div style="color:${m?stringToColor(m[1]):'#888'}">${l}</div>`;});v.innerHTML=html;v.scrollTop=v.scrollHeight;}
const html=document.documentElement;
function setTheme(t){html.setAttribute('data-bs-theme',t);localStorage.setItem('theme',t);document.getElementById('themeIcon').innerText=t==='dark'?'☀️':'🌙';}
function toggleTheme(){setTheme(html.getAttribute('data-bs-theme')==='dark'?'light':'dark');}
setTheme(localStorage.getItem('theme')||(window.matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light'));
function togglePassword(id){const el=document.getElementById(id);el.type=el.type==='password'?'text':'password';}
function copyToClipboard(id,btn){const text=document.getElementById(id).innerText.trim();const orig=btn.innerHTML;const origClass=btn.className;function success(){btn.innerHTML='✅ Copied!';btn.className='btn btn-sm btn-success';setTimeout(()=>{btn.innerHTML=orig;btn.className=origClass;},1500);}if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(text).then(success);}else{const ta=document.createElement('textarea');ta.value=text;ta.style.position='fixed';ta.style.opacity='0';document.body.appendChild(ta);ta.focus();ta.select();try{document.execCommand('copy');success();}finally{document.body.removeChild(ta);}}}
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
    document.getElementById('edit_verbose_logging').checked=c.verbose_logging===1;
    document.getElementById('edit_grok_key').value=c.grok_api_key||'';
    document.getElementById('edit_groq_key').value=c.groq_api_key||'';
    ['edit_gemini_key','edit_telegram_token','edit_bi_pass','edit_grok_key','edit_groq_key'].forEach(id=>document.getElementById(id).type='password');
    document.getElementById('editForm').action='/edit/'+c.id;
    new bootstrap.Modal(document.getElementById('editModal')).show();
}
document.addEventListener('DOMContentLoaded',()=>{
    const savedTab=localStorage.getItem('activeTab');
    if(savedTab){const t=document.querySelector(`[data-bs-target="${savedTab}"]`);if(t)new bootstrap.Tab(t).show();}
    document.querySelectorAll('[data-bs-toggle="tab"]').forEach(el=>el.addEventListener('click',e=>localStorage.setItem('activeTab',e.target.getAttribute('data-bs-target'))));
    document.querySelector('[data-bs-target="#logs-pane"]').addEventListener('shown.bs.tab',()=>{const v=document.getElementById('logViewer');if(v)v.scrollTop=v.scrollHeight;});
    colorizeLogs();
    // Check for updates (cached server-side for 1 hour)
    fetch('/api/check-update').then(r=>r.json()).then(d=>{
        if(d.update_available){
            document.getElementById('update-version').textContent=d.latest_version;
            document.getElementById('update-link').href=d.release_url||'https://github.com/slflowfoon/blueiris-ai-hub/releases';
            document.getElementById('update-banner').classList.remove('d-none');
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
    conn.close()

    primary_chat_id = configs[0]['chat_id'] if configs else ''
    mutes = get_mute_status(primary_chat_id) if primary_chat_id else []
    caption_mode = get_caption_mode(primary_chat_id) if primary_chat_id else None

    return render_template_string(
        HTML_TEMPLATE,
        configs=configs,
        logs=get_log_content(),
        base_url=BASE_URL,
        mutes=mutes,
        caption_mode=caption_mode,
        primary_chat_id=primary_chat_id,
        known_plates=load_known_plates(),
        current_version=CURRENT_VERSION,
    )


@app.route('/api/check-update')
def check_update():
    cached = r.get(UPDATE_CHECK_CACHE_KEY)
    if cached:
        return jsonify(json.loads(cached))
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
            headers={"Accept": "application/vnd.github+json"},
            timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()
            latest = data["tag_name"].lstrip("v")
            result = {
                "current_version": CURRENT_VERSION,
                "latest_version": latest,
                "update_available": latest != CURRENT_VERSION,
                "release_url": data.get("html_url", ""),
            }
        else:
            result = {"update_available": False}
    except Exception:
        result = {"update_available": False}
    r.set(UPDATE_CHECK_CACHE_KEY, json.dumps(result), ex=UPDATE_CHECK_TTL)
    return jsonify(result)


@app.route('/add', methods=['POST'])
def add_config():
    try:
        conn = get_db_connection()
        conn.execute(
            'INSERT INTO configs (id,name,gemini_key,telegram_token,chat_id,prompt,bi_url,bi_user,bi_pass,'
            'send_video,verbose_logging,delete_after_send,message_thread_id,grok_api_key,groq_api_key) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (str(uuid.uuid4()), request.form['name'], request.form['gemini_key'],
             request.form['telegram_token'], request.form['chat_id'], request.form['prompt'],
             request.form.get('bi_url'), request.form.get('bi_user'), request.form.get('bi_pass'),
             1 if 'send_video' in request.form else 0,
             1 if 'verbose_logging' in request.form else 0,
             1 if 'delete_after_send' in request.form else 0,
             request.form.get('message_thread_id') or None,
             request.form.get('grok_api_key') or None,
             request.form.get('groq_api_key') or None)
        )
        conn.commit()
        conn.close()
        flash('Configuration added!', 'success')
    except Exception as e:
        flash(f'Error: {e}', 'danger')
    return redirect(url_for('index'))


@app.route('/edit/<id>', methods=['POST'])
def edit_config(id):
    try:
        conn = get_db_connection()
        conn.execute(
            'UPDATE configs SET name=?,gemini_key=?,telegram_token=?,chat_id=?,prompt=?,bi_url=?,bi_user=?,'
            'bi_pass=?,send_video=?,verbose_logging=?,delete_after_send=?,message_thread_id=?,'
            'grok_api_key=?,groq_api_key=? WHERE id=?',
            (request.form['name'], request.form['gemini_key'], request.form['telegram_token'],
             request.form['chat_id'], request.form['prompt'],
             request.form.get('bi_url'), request.form.get('bi_user'), request.form.get('bi_pass'),
             1 if 'send_video' in request.form else 0,
             1 if 'verbose_logging' in request.form else 0,
             1 if 'delete_after_send' in request.form else 0,
             request.form.get('message_thread_id') or None,
             request.form.get('grok_api_key') or None,
             request.form.get('groq_api_key') or None,
             id)
        )
        conn.commit()
        conn.close()
        flash('Configuration updated!', 'success')
    except Exception as e:
        flash(f'Error: {e}', 'danger')
    return redirect(url_for('index'))


@app.route('/delete/<id>', methods=['POST'])
def delete_config(id):
    conn = get_db_connection()
    conn.execute('DELETE FROM configs WHERE id=?', (id,))
    conn.commit()
    conn.close()
    flash('Configuration deleted.', 'warning')
    return redirect(url_for('index'))


@app.route('/clear_logs', methods=['POST'])
def clear_logs():
    try:
        open(LOG_FILE, 'w').close()
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
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("UPDATE configs SET last_triggered=? WHERE id=?", (now_str, config_id))
    conn.commit()
    conn.close()

    filename = f"{TEMP_IMAGE_DIR}/{uuid.uuid4()}.jpg"
    original_filename = request.files['image'].filename
    config['trigger_filename'] = secure_filename(original_filename)

    try:
        request.files['image'].save(filename)
        app.logger.info(f"[{config['name']}] Webhook triggered. File: {original_filename}")

        q.enqueue(process_alert, filename, config)

    except Exception as e:
        app.logger.error(f"Error processing webhook: {e}")
        return jsonify({"error": str(e)}), 500

    return jsonify({"status": "queued", "camera": config['name']}), 200


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
