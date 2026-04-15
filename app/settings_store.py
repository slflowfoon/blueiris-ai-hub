import os
import sqlite3

DATA_DIR = os.getenv("DATA_DIR", "/app/data")
DB_FILE = os.path.join(DATA_DIR, "configs.db")

SUPPORTED_CAPTION_STYLES = ("hilarious", "witty", "rude")

DEFAULT_SETTINGS = {
    "mute_bot_poll_interval_seconds": "3",
    "mute_bot_caption_default_minutes": "60",
    "mute_bot_enabled_caption_styles": ",".join(SUPPORTED_CAPTION_STYLES),
}


def _connect():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_global_settings(conn=None):
    owns_connection = conn is None
    if owns_connection:
        conn = _connect()

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS global_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    for key, value in DEFAULT_SETTINGS.items():
        conn.execute(
            "INSERT OR IGNORE INTO global_settings (key, value) VALUES (?, ?)",
            (key, value),
        )

    if owns_connection:
        conn.commit()
        conn.close()


def _clean_int(value, fallback, minimum, maximum):
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        numeric = fallback
    return str(max(minimum, min(maximum, numeric)))


def normalize_caption_styles(raw_value):
    if raw_value is None:
        raw_value = DEFAULT_SETTINGS["mute_bot_enabled_caption_styles"]
    raw_items = [item.strip().lower() for item in str(raw_value).split(",")]
    styles = []
    for item in raw_items:
        if item in SUPPORTED_CAPTION_STYLES and item not in styles:
            styles.append(item)
    return styles


def get_global_settings():
    with _connect() as conn:
        init_global_settings(conn)
        rows = conn.execute("SELECT key, value FROM global_settings").fetchall()

    data = DEFAULT_SETTINGS.copy()
    data.update({row["key"]: row["value"] for row in rows})
    data["mute_bot_poll_interval_seconds"] = _clean_int(
        data.get("mute_bot_poll_interval_seconds"),
        fallback=3,
        minimum=1,
        maximum=60,
    )
    data["mute_bot_caption_default_minutes"] = _clean_int(
        data.get("mute_bot_caption_default_minutes"),
        fallback=60,
        minimum=1,
        maximum=1440,
    )
    data["mute_bot_enabled_caption_styles"] = normalize_caption_styles(
        data.get("mute_bot_enabled_caption_styles")
    )
    return data


def save_global_settings(values):
    cleaned = {
        "mute_bot_poll_interval_seconds": _clean_int(
            values.get("mute_bot_poll_interval_seconds"),
            fallback=3,
            minimum=1,
            maximum=60,
        ),
        "mute_bot_caption_default_minutes": _clean_int(
            values.get("mute_bot_caption_default_minutes"),
            fallback=60,
            minimum=1,
            maximum=1440,
        ),
        "mute_bot_enabled_caption_styles": ",".join(
            normalize_caption_styles(values.get("mute_bot_enabled_caption_styles", ""))
        ),
    }

    with _connect() as conn:
        init_global_settings(conn)
        for key, value in cleaned.items():
            conn.execute(
                """
                INSERT INTO global_settings (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (key, value),
            )
        conn.commit()

    return get_global_settings()


def get_mute_bot_settings():
    settings = get_global_settings()
    return {
        "poll_interval_seconds": int(settings["mute_bot_poll_interval_seconds"]),
        "caption_default_minutes": int(settings["mute_bot_caption_default_minutes"]),
        "enabled_caption_styles": settings["mute_bot_enabled_caption_styles"],
    }
