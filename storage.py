import sqlite3
import os
from contextlib import contextmanager

DB_PATH = os.getenv("DB_PATH", "errors_bot.db")


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS alerted_events (
                game_pk INTEGER,
                play_id INTEGER,
                event_type TEXT,
                alerted_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (game_pk, play_id, event_type)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS pending_video_lookups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_pk INTEGER,
                play_id INTEGER,
                message_id INTEGER,
                channel_id INTEGER,
                description TEXT,
                play_end_time TEXT,
                attempts INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)


def already_alerted(game_pk: int, play_id, event_type: str) -> bool:
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM alerted_events WHERE game_pk = ? AND play_id = ? AND event_type = ?",
            (game_pk, play_id, event_type),
        ).fetchone()
        return row is not None


def mark_alerted(game_pk: int, play_id, event_type: str):
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO alerted_events (game_pk, play_id, event_type) VALUES (?,?,?)",
            (game_pk, play_id, event_type),
        )


def set_config(key: str, value: str):
    with _conn() as c:
        c.execute(
            "INSERT INTO config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_config(key: str):
    with _conn() as c:
        row = c.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def add_pending_video_lookup(game_pk: int, play_id, message_id: int, channel_id: int,
                              description: str, play_end_time):
    with _conn() as c:
        c.execute("""
            INSERT INTO pending_video_lookups
            (game_pk, play_id, message_id, channel_id, description, play_end_time)
            VALUES (?,?,?,?,?,?)
        """, (game_pk, play_id, message_id, channel_id, description, play_end_time))


def get_pending_video_lookups():
    with _conn() as c:
        rows = c.execute("SELECT * FROM pending_video_lookups").fetchall()
        return [dict(r) for r in rows]


def increment_video_attempts(row_id: int):
    with _conn() as c:
        c.execute("UPDATE pending_video_lookups SET attempts = attempts + 1 WHERE id = ?", (row_id,))


def delete_pending_video_lookup(row_id: int):
    with _conn() as c:
        c.execute("DELETE FROM pending_video_lookups WHERE id = ?", (row_id,))
