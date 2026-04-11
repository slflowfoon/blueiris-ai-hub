#!/usr/bin/env python3
"""
Telegram mute bot for Blue Iris AI Hub (Linux/Redis version).
Polls for commands in the alert thread and manages mute/caption state in Redis.
"""

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta

import redis
import requests
from service_health import start_heartbeat_thread

LOG_FILE = "/app/logs/mute_bot.log"
DB_FILE = "/app/data/configs.db"
REDIS_URL = os.getenv('REDIS_URL', 'redis://redis:6379/0')

CAPTION_STYLES = ["hilarious", "witty", "rude"]
CAPTION_DEFAULT_MINUTES = 60
POLL_INTERVAL = 3

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

r = redis.from_url(REDIS_URL)


# =============================================================================
# DB helpers
# =============================================================================

def get_configs():
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM configs").fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except Exception as e:
        logging.error(f"DB read error: {e}")
        return []


def get_camera_names():
    return [c['name'] for c in get_configs()]


def get_primary_session():
    """Return (token, chat_id, thread_id) from the first config."""
    configs = get_configs()
    if not configs:
        return None
    c = configs[0]
    return {
        'token': c['telegram_token'],
        'chat_id': c['chat_id'],
        'thread_id': str(c.get('message_thread_id') or ''),
    }


# =============================================================================
# Mute state (Redis)
# =============================================================================

def set_mute(chat_id, camera, minutes):
    expiry = (datetime.now() + timedelta(minutes=minutes)).isoformat(timespec='seconds')
    if camera == 'all':
        key = f'mute:all:{chat_id}'
    else:
        key = f'mute:{camera.lower()}:{chat_id}'
    r.set(key, expiry, ex=minutes * 60 + 60)
    logging.info(f"Muted '{camera}' for {minutes} min in chat {chat_id}")


def clear_mute(chat_id, camera):
    if camera == 'all':
        for cam in get_camera_names():
            r.delete(f'mute:{cam.lower()}:{chat_id}')
        r.delete(f'mute:all:{chat_id}')
        return True
    key = f'mute:{camera.lower()}:{chat_id}'
    existed = bool(r.exists(key))
    r.delete(key)
    return existed


def get_status_text(chat_id):
    lines = []
    any_mute = False
    for cam in get_camera_names():
        val = r.get(f'mute:{cam.lower()}:{chat_id}')
        if val:
            expiry = datetime.fromisoformat(val.decode())
            if expiry > datetime.now():
                remaining = int((expiry - datetime.now()).total_seconds() / 60)
                lines.append(f"• {cam}: muted for {remaining} more min(s)")
                any_mute = True
    val = r.get(f'mute:all:{chat_id}')
    if val:
        expiry = datetime.fromisoformat(val.decode())
        if expiry > datetime.now():
            remaining = int((expiry - datetime.now()).total_seconds() / 60)
            lines.append(f"• All cameras: muted for {remaining} more min(s)")
            any_mute = True
    if not any_mute:
        lines.append("No active mutes.")

    cm = r.get(f'caption_mode:{chat_id}')
    if cm:
        try:
            data = json.loads(cm)
            expires = datetime.fromisoformat(data['expires'])
            if expires > datetime.now():
                remaining = int((expires - datetime.now()).total_seconds() / 60)
                lines.append(f"\n🎭 Caption mode: {data['mode']} ({remaining} min remaining)")
        except Exception:
            pass

    return "\n".join(lines)


# =============================================================================
# Caption mode (Redis)
# =============================================================================

def set_caption_mode(chat_id, style, minutes):
    expiry = (datetime.now() + timedelta(minutes=minutes)).isoformat(timespec='seconds')
    r.set(f'caption_mode:{chat_id}', json.dumps({'mode': style, 'expires': expiry}), ex=minutes * 60 + 60)
    logging.info(f"Caption mode set to '{style}' for {minutes} min in chat {chat_id}")


def clear_caption_mode(chat_id):
    r.delete(f'caption_mode:{chat_id}')
    logging.info(f"Caption mode cleared for chat {chat_id}")


# =============================================================================
# Telegram
# =============================================================================

def send_message(token, chat_id, thread_id, text, reply_markup=None):
    data = {'chat_id': chat_id, 'text': text}
    if thread_id:
        data['message_thread_id'] = thread_id
    if reply_markup:
        data['reply_markup'] = json.dumps(reply_markup)
    try:
        resp = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=10)
        if not resp.ok:
            logging.warning(f"sendMessage error: {resp.text}")
    except Exception as e:
        logging.error(f"sendMessage error: {e}")


def answer_callback(token, callback_id, text=""):
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/answerCallbackQuery",
            data={'callback_query_id': callback_id, 'text': text},
            timeout=5
        )
    except Exception:
        pass


def get_updates(token, offset):
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={'offset': offset, 'timeout': 30, 'allowed_updates': ['message', 'callback_query']},
            timeout=35
        )
        if resp.ok:
            return resp.json().get('result', [])
    except Exception as e:
        logging.error(f"getUpdates error: {e}")
    return []


# =============================================================================
# Command handling
# =============================================================================

def handle_command(token, chat_id, thread_id, text):
    parts = text.strip().split()
    cmd = parts[0].lower().lstrip('/').split('@')[0]  # strip @botname suffix

    if cmd == 'mute':
        if len(parts) == 2 and parts[1].isdigit():
            minutes = int(parts[1])
            set_mute(chat_id, 'all', minutes)
            send_message(token, chat_id, thread_id, f"🔇 All cameras muted for {minutes} min.")
        elif len(parts) == 3 and parts[2].isdigit():
            cam, minutes = parts[1], int(parts[2])
            set_mute(chat_id, cam, minutes)
            send_message(token, chat_id, thread_id, f"🔇 {cam} muted for {minutes} min.")
        else:
            send_message(token, chat_id, thread_id, "Usage: /mute <minutes>  or  /mute <camera> <minutes>")

    elif cmd == 'unmute':
        if len(parts) == 1:
            clear_mute(chat_id, 'all')
            send_message(token, chat_id, thread_id, "🔔 All cameras unmuted.")
        else:
            cam = parts[1]
            clear_mute(chat_id, cam)
            send_message(token, chat_id, thread_id, f"🔔 {cam} unmuted.")

    elif cmd == 'status':
        send_message(token, chat_id, thread_id, get_status_text(chat_id))

    elif cmd == 'caption':
        if len(parts) >= 2:
            style = parts[1].lower()
            if style == 'off':
                clear_caption_mode(chat_id)
                send_message(token, chat_id, thread_id, "🎭 Caption mode reset to normal.")
            elif style in CAPTION_STYLES:
                minutes = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else CAPTION_DEFAULT_MINUTES
                set_caption_mode(chat_id, style, minutes)
                send_message(token, chat_id, thread_id, f"🎭 Caption mode: {style} for {minutes} min.")
            else:
                send_message(token, chat_id, thread_id,
                             f"Unknown style. Choose from: {', '.join(CAPTION_STYLES)}, or off")
        else:
            send_message(token, chat_id, thread_id,
                         f"Usage: /caption <{'|'.join(CAPTION_STYLES)}|off> [minutes]")

    elif cmd == 'help':
        cameras = ', '.join(get_camera_names()) or 'Driveway, Front, Garden'
        msg = (
            "📹 Blue Iris AI Hub — Commands\n\n"
            "/mute <minutes> — mute all cameras\n"
            "/mute <camera> <minutes> — mute one camera\n"
            "/unmute — unmute all cameras\n"
            "/unmute <camera> — unmute one camera\n"
            "/status — show active mutes and caption mode\n"
            f"/caption <{'|'.join(CAPTION_STYLES)}> [minutes] — set caption style\n"
            "/caption off — back to normal captions\n"
            "/help — show this message\n\n"
            f"Cameras: {cameras}"
        )
        send_message(token, chat_id, thread_id, msg)


def handle_callback(token, callback_query):
    data = callback_query.get('data', '')
    cid = callback_query['id']
    chat_id = str(callback_query['message']['chat']['id'])
    thread_id = str(callback_query['message'].get('message_thread_id', ''))

    if data.startswith('unmute:'):
        parts = data.split(':')
        # format: unmute:<camera>:<chat_id>
        cam = parts[1] if len(parts) > 1 else 'all'
        clear_mute(chat_id, cam)
        label = "All cameras" if cam == 'all' else cam.title()
        answer_callback(token, cid, f"✅ {label} unmuted")
        send_message(token, chat_id, thread_id, f"🔔 {label} unmuted.")
    else:
        answer_callback(token, cid)


# =============================================================================
# Main poll loop
# =============================================================================

def run_bot(session):
    token = session['token']
    chat_id = session['chat_id']
    thread_id = session['thread_id']

    offset_key = f'bot_offset:{chat_id}'
    offset = int(r.get(offset_key) or 0)

    logging.info(f"Mute bot started for chat {chat_id} (thread: {thread_id or 'none'})")

    while True:
        updates = get_updates(token, offset)
        for update in updates:
            offset = update['update_id'] + 1
            r.set(offset_key, offset)

            if 'message' in update:
                msg = update['message']
                msg_chat_id = str(msg['chat']['id'])
                text = msg.get('text', '')
                if msg_chat_id == chat_id and text.startswith('/'):
                    handle_command(token, chat_id, thread_id, text)

            elif 'callback_query' in update:
                handle_callback(token, update['callback_query'])

        time.sleep(POLL_INTERVAL)


def main():
    start_heartbeat_thread("mute_bot")
    logging.info("Mute bot starting...")
    while True:
        try:
            session = get_primary_session()
            if not session:
                logging.warning("No configs in DB yet. Retrying in 30s...")
                time.sleep(30)
                continue
            run_bot(session)
        except Exception as e:
            logging.error(f"Bot crashed: {e}. Restarting in 10s...")
            time.sleep(10)


if __name__ == '__main__':
    main()
