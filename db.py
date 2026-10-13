#!/usr/bin/env python3
"""SQLite store for cairn-meter. stdlib only.

One store per user, outside any project: `~/.claude/cairn-meter/meter.db`.
Every session records the project directory it ran in (`sessions.project`),
so rollups can be cut per project without a database per project.

Usage:
    from db import connect, insert_call, save_session
    conn = connect(store_dir)   # opens/creates store_dir/meter.db with tables
"""
import os
import sqlite3
import threading
from pathlib import Path

DB_FILENAME = "meter.db"
DEFAULT_STORE_DIR = Path.home() / ".claude" / "cairn-meter"
DEFAULT_CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Stamped into `PRAGMA user_version` on every open so a future migration can
# tell an older `meter.db` apart from the current shape. Every statement
# below is `CREATE TABLE IF NOT EXISTS` (additive-only).
# 1: calls, usage_limit_events, tool_uses, session_labels, sessions.
SCHEMA_VERSION = 1

CALLS_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    request_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    agent TEXT,
    model TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_5m_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_1h_tokens INTEGER NOT NULL DEFAULT 0
)
"""

USAGE_LIMIT_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_limit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    raw_entry TEXT NOT NULL
)
"""

TOOL_USES_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_uses (
    tool_use_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    agent TEXT,
    tool_name TEXT NOT NULL,
    detail TEXT,
    timestamp TEXT NOT NULL
)
"""

SESSION_LABELS_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_labels (
    session_id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

SESSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    project TEXT,
    transcript_path TEXT,
    recorded_at TEXT NOT NULL
)
"""

CALLS_SESSION_INDEX = "CREATE INDEX IF NOT EXISTS idx_calls_session_id ON calls (session_id)"
CALLS_TIMESTAMP_INDEX = "CREATE INDEX IF NOT EXISTS idx_calls_timestamp_trunc ON calls (substr(timestamp, 1, 19))"
TOOL_USES_SESSION_INDEX = "CREATE INDEX IF NOT EXISTS idx_tool_uses_session_id ON tool_uses (session_id)"
USAGE_LIMIT_EVENTS_SESSION_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_usage_limit_events_session_id ON usage_limit_events (session_id)"
)


# Every table `connect` creates; readers treat the store as "no data yet"
# until all of them exist.
REQUIRED_TABLES = ("calls", "usage_limit_events", "tool_uses", "session_labels", "sessions")


def db_path(store_dir: Path) -> Path:
    return Path(store_dir) / DB_FILENAME


def _initialise(conn: sqlite3.Connection) -> None:
    # WAL mode lets a writer (the parser) and a reader (the server) touch
    # the same file at once without contending destructively - set this
    # before any table exists so every subsequent connection sees it.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(CALLS_SCHEMA)
    conn.execute(USAGE_LIMIT_EVENTS_SCHEMA)
    conn.execute(TOOL_USES_SCHEMA)
    conn.execute(SESSION_LABELS_SCHEMA)
    conn.execute(SESSIONS_SCHEMA)
    conn.execute(CALLS_SESSION_INDEX)
    conn.execute(CALLS_TIMESTAMP_INDEX)
    conn.execute(TOOL_USES_SESSION_INDEX)
    conn.execute(USAGE_LIMIT_EVENTS_SESSION_INDEX)
    current_version = conn.execute("PRAGMA user_version").fetchone()[0]
    if current_version < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def _create_store(path: Path) -> None:
    """Build a fully initialised WAL database beside `path`, then publish it
    under `path` atomically (hard link: never clobbers a store another
    process just created). A concurrent reader therefore sees either no file
    or a complete one, never a database mid WAL-conversion or half-schema'd.
    """
    staging = path.with_name(f"{path.name}.init-{os.getpid()}-{threading.get_ident()}")
    conn = sqlite3.connect(staging)
    try:
        _initialise(conn)
    finally:
        conn.close()
    try:
        os.link(staging, path)
    except FileExistsError:
        pass
    finally:
        for leftover in (staging, Path(f"{staging}-wal"), Path(f"{staging}-shm")):
            leftover.unlink(missing_ok=True)


def connect(store_dir: Path) -> sqlite3.Connection:
    store_dir = Path(store_dir)
    store_dir.mkdir(parents=True, exist_ok=True)
    path = db_path(store_dir)
    if not path.exists():
        _create_store(path)
    conn = sqlite3.connect(path)
    _initialise(conn)  # idempotent: upgrades an older store's schema
    return conn


def insert_call(conn, row: dict) -> None:
    """Insert one `calls`-shaped row. Idempotent on `request_id`.

    `row` carries the same keys as the `calls` table's columns; `agent`
    is optional (column is nullable), and the three cache-token fields
    default to 0 when absent from `row`.
    """
    conn.execute(
        "INSERT OR IGNORE INTO calls "
        "(request_id, session_id, agent, model, timestamp, input_tokens, output_tokens, "
        "cache_read_tokens, cache_write_5m_tokens, cache_write_1h_tokens) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            row["request_id"],
            row["session_id"],
            row.get("agent"),
            row["model"],
            row["timestamp"],
            row["input_tokens"],
            row["output_tokens"],
            row.get("cache_read_tokens", 0),
            row.get("cache_write_5m_tokens", 0),
            row.get("cache_write_1h_tokens", 0),
        ),
    )


def has_session(conn, session_id: str) -> bool:
    """True if any row for `session_id` is already recorded in `calls`."""
    row = conn.execute("SELECT 1 FROM calls WHERE session_id = ? LIMIT 1", (session_id,)).fetchone()
    return row is not None


def insert_usage_limit_event(conn, *, session_id, timestamp, raw_entry):
    conn.execute(
        "INSERT INTO usage_limit_events (session_id, timestamp, raw_entry) VALUES (?, ?, ?)",
        (session_id, timestamp, raw_entry),
    )


def insert_tool_use(conn, *, tool_use_id, request_id, session_id, agent, tool_name, timestamp, detail=None):
    conn.execute(
        "INSERT OR IGNORE INTO tool_uses "
        "(tool_use_id, request_id, session_id, agent, tool_name, detail, timestamp) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (tool_use_id, request_id, session_id, agent, tool_name, detail, timestamp),
    )


def save_session_label(conn, *, session_id, label):
    """Upserts `label` for `session_id`, replacing whatever was stored
    before (a session's title can be revised - see parser.py's
    `_extract_ai_title`). Callers only invoke this when a title was
    actually found, so a session with no title this pass keeps its
    previously saved label rather than having it wiped.
    """
    conn.execute(
        "INSERT INTO session_labels (session_id, label, updated_at) "
        "VALUES (?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(session_id) DO UPDATE SET label = excluded.label, updated_at = excluded.updated_at",
        (session_id, label),
    )


def save_session(conn, *, session_id, project=None, transcript_path=None):
    """Records which project directory (and transcript file) `session_id`
    belongs to. The first non-null project wins: a later call for the same
    session fills a missing project but never moves an already-set one.
    """
    conn.execute(
        "INSERT INTO sessions (session_id, project, transcript_path, recorded_at) "
        "VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(session_id) DO UPDATE SET "
        "project = COALESCE(sessions.project, excluded.project), "
        "transcript_path = COALESCE(excluded.transcript_path, sessions.transcript_path)",
        (session_id, project, transcript_path),
    )
