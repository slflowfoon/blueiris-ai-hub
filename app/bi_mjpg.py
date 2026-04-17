import logging
import os
import sqlite3
from urllib.parse import quote

from flask import Blueprint, Response, stream_with_context

from bi_export_shared import get_session
from db_utils import connect as sqlite_connect

DATA_DIR = os.getenv("DATA_DIR", "/app/data")
DB_FILE = os.path.join(DATA_DIR, "configs.db")

bi_mjpg_bp = Blueprint("bi_mjpg", __name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/147.0.0.0 Safari/537.36"
)


def _get_config(config_id):
    conn = sqlite_connect(DB_FILE, row_factory=sqlite3.Row)
    try:
        row = conn.execute(
            "SELECT name, bi_url, bi_user, bi_pass FROM configs WHERE id=?",
            (config_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


@bi_mjpg_bp.route("/bi-mjpg/<config_id>")
def stream_mjpg(config_id):
    config = _get_config(config_id)
    if not config:
        return "config not found", 404

    bi_url = (config.get("bi_url") or "").strip()
    bi_user = (config.get("bi_user") or "").strip()
    bi_pass = config.get("bi_pass") or ""
    camera = (config.get("name") or "").strip()

    if not bi_url or not camera:
        return "bi_url or camera name not configured", 400

    tag = f"[bi_mjpg:{camera}]"
    sess, sid = get_session(bi_url, bi_user, bi_pass, tag)
    if not sid:
        return "BI authentication failed", 502

    camera_q = quote(camera)
    url = f"{bi_url.rstrip('/')}/mjpg/{camera_q}/video.mjpg"

    upstream = sess.get(
        url,
        params={"decode": "-1", "session": sid},
        headers={"User-Agent": _UA},
        stream=True,
        timeout=(5, 30),
        allow_redirects=False,
    )

    if upstream.status_code != 200:
        upstream.close()
        logging.warning(f"{tag} upstream MJPG returned {upstream.status_code}")
        return f"upstream error: {upstream.status_code}", 502

    content_type = upstream.headers.get("Content-Type", "multipart/x-mixed-replace; boundary=myboundary")

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk
        except (GeneratorExit, Exception):
            pass
        finally:
            upstream.close()

    return Response(
        stream_with_context(generate()),
        content_type=content_type,
        headers={"Cache-Control": "no-cache"},
    )


@bi_mjpg_bp.route("/bi-image/<config_id>")
def single_image(config_id):
    config = _get_config(config_id)
    if not config:
        return "config not found", 404

    bi_url = (config.get("bi_url") or "").strip()
    bi_user = (config.get("bi_user") or "").strip()
    bi_pass = config.get("bi_pass") or ""
    camera = (config.get("name") or "").strip()

    if not bi_url or not camera:
        return "bi_url or camera name not configured", 400

    tag = f"[bi_image:{camera}]"
    sess, sid = get_session(bi_url, bi_user, bi_pass, tag)
    if not sid:
        return "BI authentication failed", 502

    camera_q = quote(camera)
    r = sess.get(
        f"{bi_url.rstrip('/')}/image/{camera_q}",
        params={"session": sid},
        headers={"User-Agent": _UA},
        timeout=10,
        allow_redirects=False,
    )

    if r.status_code != 200:
        return f"upstream error: {r.status_code}", 502

    return Response(
        r.content,
        content_type=r.headers.get("Content-Type", "image/jpeg"),
    )
