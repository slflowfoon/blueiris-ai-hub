import sqlite3


SQLITE_BUSY_TIMEOUT_MS = 5000


def configure_connection(conn):
    conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def connect(db_file, row_factory=None):
    conn = sqlite3.connect(db_file)
    configure_connection(conn)
    if row_factory is not None:
        conn.row_factory = row_factory
    return conn
