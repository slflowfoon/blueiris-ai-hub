import os
import sqlite3

DATA_DIR = os.getenv("DATA_DIR", "/app/data")
DB_FILE = os.path.join(DATA_DIR, "configs.db")

DEFAULT_SETTINGS = {
    "auto_mute_threshold": "5",
    "auto_mute_window_minutes": "10",
    "auto_mute_duration_minutes": "30",
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


def get_global_settings():
    with _connect() as conn:
        init_global_settings(conn)
        rows = conn.execute("SELECT key, value FROM global_settings").fetchall()

    data = DEFAULT_SETTINGS.copy()
    data.update({row["key"]: row["value"] for row in rows})
    data["auto_mute_threshold"] = _clean_int(
        data.get("auto_mute_threshold"),
        fallback=5,
        minimum=1,
        maximum=100,
    )
    data["auto_mute_window_minutes"] = _clean_int(
        data.get("auto_mute_window_minutes"),
        fallback=10,
        minimum=1,
        maximum=1440,
    )
    data["auto_mute_duration_minutes"] = _clean_int(
        data.get("auto_mute_duration_minutes"),
        fallback=30,
        minimum=1,
        maximum=1440,
    )
    return data


def save_global_settings(values):
    cleaned = {
        "auto_mute_threshold": _clean_int(
            values.get("auto_mute_threshold"),
            fallback=5,
            minimum=1,
            maximum=100,
        ),
        "auto_mute_window_minutes": _clean_int(
            values.get("auto_mute_window_minutes"),
            fallback=10,
            minimum=1,
            maximum=1440,
        ),
        "auto_mute_duration_minutes": _clean_int(
            values.get("auto_mute_duration_minutes"),
            fallback=30,
            minimum=1,
            maximum=1440,
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


def get_auto_mute_settings():
    settings = get_global_settings()
    return {
        "threshold": int(settings["auto_mute_threshold"]),
        "window_minutes": int(settings["auto_mute_window_minutes"]),
        "duration_minutes": int(settings["auto_mute_duration_minutes"]),
    }
