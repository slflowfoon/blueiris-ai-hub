import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import sqlite3
import time
import uuid

import requests

from wsgi import get_db_connection, get_redis_client


PAIRING_TTL_SECONDS = 300


def _pairing_key(pairing_token):
    return f"tv_pairing:{pairing_token}"


def _pairing_code_key(manual_code):
    return f"tv_pairing_code:{manual_code}"


def _finalized_pairing_key(pairing_token):
    return f"tv_pairing_result:{pairing_token}"


def _paired_tv_lookup_key(tv_id):
    return f"tv_pairing_tv:{tv_id}"


def _load_pairing_lookup_entries(raw):
    if not raw:
        return []
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return [entry for entry in payload if isinstance(entry, dict)]
    return []


def _store_pairing_lookup_entries(redis_client, tv_id, entries):
    redis_client.setex(
        _paired_tv_lookup_key(tv_id),
        PAIRING_TTL_SECONDS,
        json.dumps(entries),
    )


def _invalidate_pairing_entries(redis_client, tv_id, entries):
    keys_to_delete = [_paired_tv_lookup_key(tv_id)]
    for entry in entries:
        pairing_token = entry.get("pairing_token")
        manual_code = entry.get("manual_code")
        if manual_code:
            keys_to_delete.append(_pairing_code_key(manual_code))
        if pairing_token:
            keys_to_delete.extend([
                _pairing_key(pairing_token),
                _finalized_pairing_key(pairing_token),
            ])
    for key in keys_to_delete:
        redis_client.delete(key)


def _existing_paired_tv_id(device_id):
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT id FROM paired_tvs WHERE device_token_id=?",
            (device_id,),
        ).fetchone()
    return row["id"] if row else None


def _safe_rtsp_url(device_info):
    return device_info.get("rtsp_url") or ""


def _normalize_private_ip_address(ip_address):
    candidate = (ip_address or "").strip()
    if not candidate:
        raise ValueError("ip_address is required")
    try:
        parsed = ipaddress.ip_address(candidate)
    except ValueError as exc:
        raise ValueError("ip_address must be a valid IP address") from exc
    if not (parsed.is_private or parsed.is_loopback):
        raise ValueError("ip_address must be a private or loopback IP address")
    return str(parsed)


def _normalize_tv_pairing_target(ip_address, port):
    normalized_ip = _normalize_private_ip_address(ip_address)
    try:
        normalized_port = int(port)
    except (TypeError, ValueError) as exc:
        raise ValueError("port must be a valid integer") from exc
    if normalized_port < 1 or normalized_port > 65535:
        raise ValueError("port must be between 1 and 65535")
    url_host = normalized_ip if ":" not in normalized_ip else f"[{normalized_ip}]"
    return normalized_ip, normalized_port, f"http://{url_host}:{normalized_port}/pair/complete"


def _resolve_camera_id_from_name(camera_name):
    if not camera_name:
        return None
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT id FROM configs WHERE name=? ORDER BY COALESCE(created_at, '') DESC, id DESC",
            (camera_name,),
        ).fetchall()
    if len(rows) == 1:
        return rows[0]["id"]
    return None


def _camera_group_priority_requires_camera_name(conn):
    for row in conn.execute("PRAGMA table_info(camera_group_priorities)").fetchall():
        if row["name"] == "camera_name":
            return bool(row["notnull"])
    return False


def _load_pairing_payload(raw):
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("pairing session payload is invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("pairing session payload is invalid")
    return payload


def _extract_device_info(payload):
    device_info = payload.get("device_info")
    if not isinstance(device_info, dict):
        raise ValueError("pairing session payload is missing device_info")
    tv_name = device_info.get("tv_name")
    if not tv_name:
        raise ValueError("pairing session payload is missing required field: tv_name")
    device_id = device_info.get("device_id")
    if not device_id:
        raise ValueError("pairing session payload is missing required field: device_id")
    ip_address = device_info.get("ip_address")
    if not ip_address:
        raise ValueError("pairing session payload is missing required field: ip_address")
    return device_info


def create_pairing_session(device_info):
    pairing_token = secrets.token_urlsafe(24)
    redis_client = get_redis_client()
    while True:
        manual_code = secrets.token_hex(3).upper()
        if redis_client.set(
            _pairing_code_key(manual_code),
            pairing_token,
            ex=PAIRING_TTL_SECONDS,
            nx=True,
        ):
            break
    expires_at = time.time() + PAIRING_TTL_SECONDS
    payload = {
        "pairing_token": pairing_token,
        "manual_code": manual_code,
        "expires_at": expires_at,
        "device_info": device_info,
    }
    redis_client.setex(_pairing_key(pairing_token), PAIRING_TTL_SECONDS, json.dumps(payload))
    return {
        "pairing_token": pairing_token,
        "manual_code": manual_code,
        "expires_at": expires_at,
    }


def _upsert_paired_tv(device_info, shared_secret):
    device_id = device_info["device_id"]
    existing_id = _existing_paired_tv_id(device_id)

    try:
        with get_db_connection() as conn:
            if existing_id:
                tv_id = existing_id
                conn.execute(
                    """
                    UPDATE paired_tvs
                    SET name=?, ip_address=?, port=?, rtsp_url=?, shared_secret=?,
                        device_token_id=?, last_ip_seen=?
                    WHERE id=?
                    """,
                    (
                        device_info.get("tv_name"),
                        device_info.get("ip_address"),
                        device_info.get("port", 7979),
                        _safe_rtsp_url(device_info),
                        shared_secret,
                        device_id,
                        device_info.get("ip_address"),
                        tv_id,
                    ),
                )
            else:
                tv_id = str(uuid.uuid4())
                conn.execute(
                    """
                    INSERT INTO paired_tvs (
                        id, name, ip_address, port, rtsp_url, shared_secret,
                        device_token_id, last_seen_at, last_ip_seen
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tv_id,
                        device_info.get("tv_name"),
                        device_info.get("ip_address"),
                        device_info.get("port", 7979),
                        _safe_rtsp_url(device_info),
                        shared_secret,
                        device_id,
                        None,
                        device_info.get("ip_address"),
                    ),
                )
    except sqlite3.IntegrityError:
        existing_id = _existing_paired_tv_id(device_id)
        if not existing_id:
            raise
        with get_db_connection() as conn:
            conn.execute(
                """
                UPDATE paired_tvs
                SET name=?, ip_address=?, port=?, rtsp_url=?, shared_secret=?,
                    device_token_id=?, last_ip_seen=?
                WHERE id=?
                """,
                (
                    device_info.get("tv_name"),
                    device_info.get("ip_address"),
                    device_info.get("port", 7979),
                    _safe_rtsp_url(device_info),
                    shared_secret,
                    device_id,
                    device_info.get("ip_address"),
                    existing_id,
                ),
            )
        tv_id = existing_id

    return tv_id


def pair_remote_tv_by_code(ip_address, manual_code, port=7979):
    normalized_ip, normalized_port, target_url = _normalize_tv_pairing_target(ip_address, port)
    shared_secret = secrets.token_urlsafe(32)
    response = requests.post(
        target_url,
        json={
            "manual_code": (manual_code or "").strip().upper(),
            "shared_secret": shared_secret,
        },
        timeout=5,
    )
    response.raise_for_status()
    device_info = response.json()
    if not isinstance(device_info, dict):
        raise ValueError("invalid pairing response from tv")
    if not device_info.get("ip_address"):
        device_info["ip_address"] = normalized_ip
    if not device_info.get("port"):
        device_info["port"] = normalized_port
    return _upsert_paired_tv(device_info, shared_secret)


def finalize_pairing(pairing_token):
    redis_client = get_redis_client()
    raw = redis_client.get(_pairing_key(pairing_token))
    if not raw:
        finalized_id = redis_client.get(_finalized_pairing_key(pairing_token))
        if finalized_id:
            return finalized_id.decode("utf-8") if isinstance(finalized_id, bytes) else finalized_id
        raise ValueError("pairing session expired")
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    payload = _load_pairing_payload(raw)
    device_info = _extract_device_info(payload)
    shared_secret = secrets.token_urlsafe(32)
    tv_id = _upsert_paired_tv(device_info, shared_secret)
    redis_client.delete(_pairing_key(pairing_token))
    redis_client.setex(_finalized_pairing_key(pairing_token), PAIRING_TTL_SECONDS, tv_id)
    lookup_key = _paired_tv_lookup_key(tv_id)
    existing_entries = _load_pairing_lookup_entries(redis_client.get(lookup_key))
    existing_entries.append({
        "pairing_token": pairing_token,
        "manual_code": payload["manual_code"],
    })
    _store_pairing_lookup_entries(redis_client, tv_id, existing_entries)
    return tv_id


def finalize_pairing_by_code(manual_code):
    redis_client = get_redis_client()
    pairing_token = redis_client.get(_pairing_code_key(manual_code))
    if not pairing_token:
        raise ValueError("pairing code not found")
    if isinstance(pairing_token, bytes):
        pairing_token = pairing_token.decode("utf-8")
    return finalize_pairing(pairing_token)


def delete_paired_tv(tv_id):
    redis_client = get_redis_client()
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT id FROM paired_tvs WHERE id=?",
            (tv_id,),
        ).fetchone()
        if not row:
            return False
        conn.execute("DELETE FROM camera_tv_targets WHERE tv_id=?", (tv_id,))
        conn.execute("DELETE FROM paired_tvs WHERE id=?", (tv_id,))

    lookup_raw = redis_client.get(_paired_tv_lookup_key(tv_id))
    lookup_entries = _load_pairing_lookup_entries(lookup_raw)
    if lookup_entries:
        _invalidate_pairing_entries(redis_client, tv_id, lookup_entries)
    else:
        redis_client.delete(_paired_tv_lookup_key(tv_id))

    return True


def set_group_priority(group_name, ordered_camera_ids):
    if not group_name:
        raise ValueError("group_name is required")

    ordered_camera_ids = [camera_id for camera_id in ordered_camera_ids if camera_id]
    with get_db_connection() as conn:
        requires_camera_name = _camera_group_priority_requires_camera_name(conn)
        conn.execute(
            "DELETE FROM camera_group_priorities WHERE group_name=?",
            (group_name,),
        )
        conn.executemany(
            """
            INSERT INTO camera_group_priorities (
                id, camera_id, camera_name, group_name, priority
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    str(uuid.uuid4()),
                    camera_id,
                    camera_id if requires_camera_name else None,
                    group_name,
                    priority,
                )
                for priority, camera_id in enumerate(ordered_camera_ids)
            ],
        )


def save_group_priority(group_name, ordered_camera_ids):
    return set_group_priority(group_name, ordered_camera_ids)


def get_group_priority_ids(group_name):
    if not group_name:
        return []

    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT camera_id
            FROM camera_group_priorities
            WHERE group_name=?
            ORDER BY priority ASC, COALESCE(created_at, '') ASC, id ASC
            """,
            (group_name,),
        ).fetchall()

    return [row["camera_id"] for row in rows if row["camera_id"]]


def resolve_group_winner(group_name, triggered_configs):
    priority_ids = get_group_priority_ids(group_name)
    if not priority_ids:
        return None

    eligible_configs = [
        config
        for config in triggered_configs
        if int(config.get("tv_push_enabled") or 0) == 1
        and (
            config.get("tv_rtsp_url")
            or ((config.get("tv_stream_type") or "rtsp") == "mjpg" and config.get("bi_url"))
        )
    ]
    if not eligible_configs:
        return None

    eligible_by_identifier = {}
    for config in eligible_configs:
        camera_id = config.get("id")
        if camera_id and camera_id not in eligible_by_identifier:
            eligible_by_identifier[camera_id] = config

    for camera_id in priority_ids:
        winner = eligible_by_identifier.get(camera_id)
        if winner:
            return winner

    return None


def get_paired_tv(tv_id):
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT id, name, ip_address, port, rtsp_url, shared_secret,
                   device_token_id, last_seen_at, last_ip_seen, created_at
            FROM paired_tvs
            WHERE id=?
            """,
            (tv_id,),
        ).fetchone()
    return dict(row) if row else None


def _load_target_tv_ids(camera_id, camera_name):
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


def _load_target_tvs(camera_id, camera_name):
    tv_ids = _load_target_tv_ids(camera_id, camera_name)
    return [tv for tv_id in tv_ids if (tv := get_paired_tv(tv_id))]

def dispatch_tv_alert(config, tag):
    from settings_store import get_global_settings
    stream_type = config.get("tv_stream_type") or "rtsp"

    if stream_type == "mjpg":
        hub_base_url = (get_global_settings().get("hub_base_url") or "").rstrip("/")
        mjpg_url = f"{hub_base_url}/bi-mjpg/{config['id']}" if hub_base_url else None
        rtsp_url = None
    else:
        mjpg_url = None
        rtsp_url = config.get("tv_rtsp_url")

    payload = {
        "camera_id": config.get("id"),
        "camera_name": config.get("name"),
        "rtsp_url": rtsp_url,
        "mjpg_url": mjpg_url,
        "duration": int(config.get("tv_duration_seconds") or 0),
        "tv_group": config.get("tv_group"),
        "mute_audio": bool(int(config.get("tv_mute_audio") or 0)),
        "request_id": config.get("request_id", "unknown"),
        "tag": tag,
    }

    try:
        if not rtsp_url and not mjpg_url:
            return {"delivered": [], "failed": [], "skipped": True, "payload": payload}

        targets = _load_target_tvs(config.get("id"), config.get("name"))
        if not targets:
            return {"delivered": [], "failed": [], "payload": payload}

        delivery_result = send_to_many_tvs(targets, payload)
        delivery_result["payload"] = payload
        return delivery_result
    except Exception as exc:
        logging.warning(f"{tag} TV dispatch failed: {exc}")
        return {"delivered": [], "failed": [], "error": str(exc), "payload": payload}


def sign_payload(shared_secret, payload):
    canonical_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload_hash = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
    signature = hmac.new(
        (shared_secret or "").encode("utf-8"),
        canonical_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "payload": payload,
        "signature": signature,
        "signing": {
            "algorithm": "hmac-sha256",
            "payload_hash": payload_hash,
            "payload_format": "json",
        },
    }


def send_to_tv_device(tv, payload, attempts=2):
    tv_id = tv.get("id")
    ip_address = tv.get("ip_address")
    port = tv.get("port", 7979)
    shared_secret = tv.get("shared_secret", "")
    url = f"http://{ip_address}:{port}/notify"
    body = sign_payload(shared_secret, payload)

    max_attempts = max(int(attempts), 1)
    last_status_code = None
    last_error = None

    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.post(url, json=body, timeout=5)
            last_status_code = response.status_code
            if 200 <= response.status_code < 300:
                return {
                    "tv_id": tv_id,
                    "ok": True,
                    "attempts": attempt,
                    "status_code": response.status_code,
                }
            last_error = response.text or f"HTTP {response.status_code}"
        except requests.RequestException as exc:
            last_error = str(exc)

        if attempt < max_attempts:
            continue

    return {
        "tv_id": tv_id,
        "ok": False,
        "attempts": max_attempts,
        "status_code": last_status_code,
        "error": last_error,
    }


def send_to_many_tvs(tvs, payload):
    delivered = []
    failed = []

    for tv in tvs:
        try:
            result = send_to_tv_device(tv, payload)
        except Exception:
            result = {"tv_id": tv.get("id"), "ok": False}

        if result.get("ok"):
            delivered.append(result.get("tv_id"))
        else:
            failed.append(result.get("tv_id"))

    return {"delivered": delivered, "failed": failed}
