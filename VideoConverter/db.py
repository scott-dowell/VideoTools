"""
db.py
=====
SQLite persistence layer for VideoConverter.

All access goes through this module — the rest of the app never imports sqlite3
directly.  Uses raw sqlite3 (no ORM) for simplicity.

DB file: VideoConverter/conversions.db  (next to settings.json)

Schema
------
Table: conversions
  id              INTEGER PRIMARY KEY
  source_path     TEXT    NOT NULL
  source_mtime    REAL    NOT NULL   -- os.path.getmtime(); re-processing key
  source_size_mb  REAL
  source_codec    TEXT
  output_path     TEXT
  output_size_mb  REAL
  saved_mb        REAL
  saved_pct       INTEGER
  status          TEXT    NOT NULL DEFAULT 'pending'
                          -- pending | running | done | failed | skipped
  anime_mode      INTEGER DEFAULT 0
  encoder_used    TEXT
  started_at      TEXT               -- ISO-8601 UTC
  completed_at    TEXT
  error_tail      TEXT               -- last ~2 KB of ffmpeg stderr on failure

Unique index on (source_path, source_mtime) — same path with a NEW mtime gets a
fresh pending row, allowing re-processing when a file is replaced in-place.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

_DB_PATH: str | None = None


def init_db(db_path: str) -> None:
    """
    Create the database file and tables if they do not already exist.
    Must be called once at application startup before any other db function.
    """
    global _DB_PATH
    _DB_PATH = db_path

    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS conversions (
                id              INTEGER PRIMARY KEY,
                source_path     TEXT    NOT NULL,
                source_mtime    REAL    NOT NULL,
                source_size_mb  REAL,
                source_codec    TEXT,
                output_path     TEXT,
                output_size_mb  REAL,
                saved_mb        REAL,
                saved_pct       INTEGER,
                status          TEXT    NOT NULL DEFAULT 'pending',
                anime_mode      INTEGER DEFAULT 0,
                encoder_used    TEXT,
                started_at      TEXT,
                completed_at    TEXT,
                error_tail      TEXT
            );

            CREATE UNIQUE INDEX IF NOT EXISTS ux_conversions_path_mtime
                ON conversions (source_path, source_mtime);
        """)


@contextmanager
def _connect():
    """Yield a sqlite3 connection with WAL mode and foreign-key support."""
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_record(source_path: str, source_mtime: float) -> dict | None:
    """
    Return the conversion record for this (source_path, source_mtime) pair,
    or None if no such record exists.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM conversions WHERE source_path = ? AND source_mtime = ?",
            (source_path, source_mtime),
        ).fetchone()
    return dict(row) if row else None


def upsert_pending(
    source_path: str,
    source_mtime: float,
    source_size_mb: float | None = None,
    source_codec: str | None = None,
) -> int:
    """
    Insert a new pending row or return the existing row's id if it already exists.
    Returns the record id.
    """
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO conversions (source_path, source_mtime, source_size_mb, source_codec, status)
            VALUES (?, ?, ?, ?, 'pending')
            ON CONFLICT (source_path, source_mtime) DO NOTHING
            """,
            (source_path, source_mtime, source_size_mb, source_codec),
        )
        row = conn.execute(
            "SELECT id FROM conversions WHERE source_path = ? AND source_mtime = ?",
            (source_path, source_mtime),
        ).fetchone()
    return row["id"]


def mark_running(record_id: int, started_at: str) -> None:
    """Flip a record to status='running'."""
    with _connect() as conn:
        conn.execute(
            "UPDATE conversions SET status = 'running', started_at = ? WHERE id = ?",
            (started_at, record_id),
        )


def mark_done(
    record_id: int,
    output_path: str,
    output_size_mb: float,
    saved_mb: float,
    saved_pct: int,
    completed_at: str,
    encoder_used: str | None = None,
) -> None:
    """Flip a record to status='done' and fill in output metrics."""
    with _connect() as conn:
        conn.execute(
            """
            UPDATE conversions
               SET status         = 'done',
                   output_path    = ?,
                   output_size_mb = ?,
                   saved_mb       = ?,
                   saved_pct      = ?,
                   completed_at   = ?,
                   encoder_used   = ?
             WHERE id = ?
            """,
            (output_path, output_size_mb, saved_mb, saved_pct,
             completed_at, encoder_used, record_id),
        )


def mark_failed(record_id: int, error_tail: str | None, completed_at: str) -> None:
    """Flip a record to status='failed' and store the last stderr lines."""
    with _connect() as conn:
        conn.execute(
            """
            UPDATE conversions
               SET status       = 'failed',
                   error_tail   = ?,
                   completed_at = ?
             WHERE id = ?
            """,
            (error_tail, completed_at, record_id),
        )
