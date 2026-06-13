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
  id                INTEGER PRIMARY KEY
  source_path       TEXT    NOT NULL
  source_mtime      REAL    NOT NULL   -- os.path.getmtime(); re-processing key
  source_size_bytes INTEGER            -- exact file size; used for move/rename detection
  source_size_mb    REAL
  source_codec      TEXT
  source_hash       TEXT               -- SHA-256 of first 2 MB of source file
  output_path       TEXT
  output_size_mb    REAL
  output_hash       TEXT               -- SHA-256 of first 2 MB of output file
  saved_mb          REAL
  saved_pct         INTEGER
  status            TEXT    NOT NULL DEFAULT 'pending'
                            -- pending | running | done | failed | skipped
  anime_mode        INTEGER DEFAULT 0
  encoder_used      TEXT
  started_at        TEXT               -- ISO-8601 UTC
  completed_at      TEXT
  error_tail        TEXT               -- last ~2 KB of ffmpeg stderr on failure
  dropped_streams   TEXT               -- JSON array of ffprobe stream indices to skip, e.g. [5,7]

Unique index on (source_path, source_mtime) — same path with a NEW mtime gets a
fresh pending row, allowing re-processing when a file is replaced in-place.

Fingerprint lookup on (source_mtime, source_size_bytes) — survives folder renames
and moves because mtime and exact size are preserved by the filesystem.

Hash lookup on (source_hash | output_hash) — survives cross-drive copies that
reset mtime. Hashes only the first 2 MB so the cost is negligible (~4 ms/SSD).
"""

from __future__ import annotations

import hashlib
import json as _json
import sqlite3
from contextlib import contextmanager


def _json_loads_safe(value: str | None) -> list:
    """Parse a JSON string to a list, returning [] on any error or None input."""
    if not value:
        return []
    try:
        return _json.loads(value)
    except Exception:
        return []


def _norm(path: str | None) -> str | None:
    """Normalise a filesystem path to forward slashes for consistent DB storage."""
    return path.replace("\\", "/") if path else path


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
                id                INTEGER PRIMARY KEY,
                source_path       TEXT    NOT NULL,
                source_mtime      REAL    NOT NULL,
                source_size_bytes INTEGER,
                source_size_mb    REAL,
                source_codec      TEXT,
                source_hash          TEXT,
                source_bitrate_kbps  INTEGER,
                source_duration_secs REAL,
                source_video_track_count INTEGER,
                source_audio_track_count INTEGER,
                output_path          TEXT,
                output_size_mb       REAL,
                output_hash          TEXT,
                saved_mb             REAL,
                saved_pct            INTEGER,
                status               TEXT    NOT NULL DEFAULT 'pending',
                anime_mode        INTEGER DEFAULT 0,
                force_sw          INTEGER DEFAULT 0,
                force_convert     INTEGER DEFAULT 0,
                encoder_used      TEXT,
                started_at        TEXT,
                completed_at      TEXT,
                error_tail        TEXT
            );

            CREATE UNIQUE INDEX IF NOT EXISTS ux_conversions_path_mtime
                ON conversions (source_path, source_mtime);
        """)
        # Migration: add columns to existing databases that pre-date them.
        existing = {row[1] for row in conn.execute("PRAGMA table_info(conversions)")}
        for col, typedef in [
            ("source_size_bytes",    "INTEGER"),
            ("source_hash",          "TEXT"),
            ("output_hash",          "TEXT"),
            ("source_bitrate_kbps",  "INTEGER"),
            ("source_duration_secs", "REAL"),
            ("source_video_track_count", "INTEGER"),
            ("source_audio_track_count", "INTEGER"),
            ("output_bitrate_kbps",  "INTEGER"),
            ("force_sw",             "INTEGER DEFAULT 0"),
            ("force_convert",        "INTEGER DEFAULT 0"),
            ("dropped_streams",       "TEXT"),
            ("est_saving_pct",        "INTEGER"),
            ("est_saving_mb",         "REAL"),
            ("est_sample_cv_pct",     "REAL"),
            ("est_high_variance",     "INTEGER DEFAULT 0"),
            ("est_aggregation",       "TEXT"),
            ("est_quality",           "INTEGER"),
            ("est_version",           "INTEGER"),
        ]:
            if col not in existing:
                conn.execute(f"ALTER TABLE conversions ADD COLUMN {col} {typedef}")

        # Reset any records left in 'running' state from a previous session —
        # they can never complete now and would block re-queuing on the next scan.
        conn.execute("UPDATE conversions SET status='queued', started_at=NULL WHERE status='running'")


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
# File hashing
# ---------------------------------------------------------------------------

def hash_file_head(path: str, head_bytes: int = 2 * 1024 * 1024) -> str | None:
    """Return SHA-256 hex digest of the first `head_bytes` of a file, or None on error."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            h.update(f.read(head_bytes))
        return h.hexdigest()
    except OSError:
        return None


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
            (_norm(source_path), source_mtime),
        ).fetchone()
    return dict(row) if row else None


def get_record_by_output(output_path: str) -> dict | None:
    """
    Return a done record whose output_path matches, normalised to forward slashes.
    Used by the scanner to recognise already-converted files that changed extension.
    """
    normalised = _norm(output_path)
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM conversions WHERE REPLACE(output_path, '\\', '/') = ? AND status = 'done'",
            (normalised,),
        ).fetchone()
    return dict(row) if row else None


def get_record_by_fingerprint(source_mtime: float, source_size_bytes: int) -> dict | None:
    """
    Return a done record matching (source_mtime, source_size_bytes).
    Survives folder renames/moves because mtime and exact size are preserved.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM conversions WHERE source_mtime = ? AND source_size_bytes = ? AND status = 'done'",
            (source_mtime, source_size_bytes),
        ).fetchone()
    return dict(row) if row else None


def get_record_by_hash(content_hash: str) -> dict | None:
    """
    Return a done record whose source_hash OR output_hash matches.
    Used as a last-resort fallback when path and fingerprint lookups both miss
    (e.g. cross-drive copy that reset mtime).
    """
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM conversions
             WHERE (source_hash = ? OR output_hash = ?) AND status = 'done'
            """,
            (content_hash, content_hash),
        ).fetchone()
    return dict(row) if row else None


def update_source_hash(record_id: int, source_hash: str) -> None:
    """Store the source file hash on an existing record."""
    with _connect() as conn:
        conn.execute(
            "UPDATE conversions SET source_hash = ? WHERE id = ?",
            (source_hash, record_id),
        )


def get_latest_statuses_by_paths(paths: list) -> dict:
    """
    Return {source_path: {"id", "status", "bitrate_kbps", "codec", "duration_secs", "dropped_streams"}} for
    the most recent DB record per path.  Only paths with an existing record are included.

    For done records, bitrate_kbps is the output (post-conversion) bitrate:
      1. output_bitrate_kbps if explicitly stored
      2. Computed from output_size_mb / source_duration_secs as fallback
      3. source_bitrate_kbps as last resort
    For all other statuses, source_bitrate_kbps is returned.
    """
    if not paths:
        return {}
    normed = [_norm(p) for p in paths]
    with _connect() as conn:
        placeholders = ",".join("?" * len(normed))
        rows = conn.execute(
            f"""
            SELECT c.id, c.source_path, c.status,
                   c.source_bitrate_kbps, c.source_codec, c.source_duration_secs,
                   c.source_video_track_count, c.source_audio_track_count,
                   c.output_bitrate_kbps, c.output_size_mb, c.saved_mb, c.saved_pct,
                                     c.force_sw, c.force_convert, c.dropped_streams,
                     c.est_saving_pct, c.est_saving_mb,
                    c.est_sample_cv_pct, c.est_high_variance, c.est_aggregation,
                    c.est_quality, c.est_version
              FROM conversions c
             INNER JOIN (
                 SELECT source_path, MAX(id) AS max_id
                   FROM conversions
                  WHERE source_path IN ({placeholders})
                  GROUP BY source_path
             ) latest ON c.id = latest.max_id
            """,
            normed,
        ).fetchall()
    result = {}
    for row in rows:
        status = row["status"]
        if status == "done":
            bitrate = row["output_bitrate_kbps"]
            if not bitrate:
                out_mb  = row["output_size_mb"]
                dur_s   = row["source_duration_secs"]
                if out_mb and dur_s and dur_s > 0:
                    bitrate = round(out_mb * 8192 / dur_s)
            if not bitrate:
                bitrate = row["source_bitrate_kbps"]
        else:
            bitrate = row["source_bitrate_kbps"]
        result[row["source_path"]] = {
            "id":             row["id"],
            "status":         status,
            "bitrate_kbps":   bitrate,
            "codec":          row["source_codec"],
            "duration_secs":  row["source_duration_secs"],
            "video_track_count": row["source_video_track_count"],
            "audio_track_count": row["source_audio_track_count"],
            "output_size_mb": row["output_size_mb"],
            "saved_mb":       row["saved_mb"],
            "saved_pct":      row["saved_pct"],
            "force_sw":       bool(row["force_sw"]),
            "force_convert":  bool(row["force_convert"]),
            "dropped_streams": _json_loads_safe(row["dropped_streams"]),
            "est_saving_pct": row["est_saving_pct"],
            "est_saving_mb":  row["est_saving_mb"],
            "est_sample_cv_pct": row["est_sample_cv_pct"],
            "est_high_variance": bool(row["est_high_variance"]),
            "est_aggregation": row["est_aggregation"],
            "est_quality": row["est_quality"],
            "est_version": row["est_version"],
        }
    return result


def upsert_pending(
    source_path: str,
    source_mtime: float,
    source_size_bytes: int | None = None,
    source_size_mb: float | None = None,
    source_codec: str | None = None,
    anime_mode: bool = False,
) -> int:
    """
    Insert a new pending row or return the existing row's id if it already exists.
    Returns the record id.
    """
    source_path = _norm(source_path)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO conversions (source_path, source_mtime, source_size_bytes, source_size_mb, source_codec, anime_mode, status)
            VALUES (?, ?, ?, ?, ?, ?, 'pending')
            ON CONFLICT (source_path, source_mtime) DO UPDATE SET
                anime_mode        = excluded.anime_mode,
                source_size_bytes = COALESCE(conversions.source_size_bytes, excluded.source_size_bytes),
                source_size_mb    = COALESCE(conversions.source_size_mb,    excluded.source_size_mb),
                source_codec      = COALESCE(conversions.source_codec,      excluded.source_codec)
                -- force_sw is intentionally NOT reset here; it persists across retries
            """,
            (source_path, source_mtime, source_size_bytes, source_size_mb, source_codec, int(anime_mode)),
        )
        row = conn.execute(
            "SELECT id FROM conversions WHERE source_path = ? AND source_mtime = ?",
            (source_path, source_mtime),
        ).fetchone()
    return row["id"]


def update_source_path(record_id: int, new_source_path: str) -> None:
    """Update source_path on a record whose file has been moved/renamed."""
    with _connect() as conn:
        conn.execute(
            "UPDATE conversions SET source_path = ? WHERE id = ?",
            (_norm(new_source_path), record_id),
        )


def move_path(old_path: str, new_path: str) -> dict:
    """Update any source_path/output_path references from old_path to new_path."""
    old_norm = _norm(old_path)
    new_norm = _norm(new_path)
    with _connect() as conn:
        cur_src = conn.execute(
            "UPDATE conversions SET source_path = ? WHERE source_path = ?",
            (new_norm, old_norm),
        )
        cur_out = conn.execute(
            "UPDATE conversions SET output_path = ? WHERE output_path = ?",
            (new_norm, old_norm),
        )
        return {"source_updated": cur_src.rowcount, "output_updated": cur_out.rowcount}


def set_force_sw(source_path: str, value: bool) -> int:
    """Set or clear the force_sw flag on all records for a given source path."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE conversions SET force_sw = ? WHERE source_path = ?",
            (int(value), _norm(source_path)),
        )
        return cur.rowcount


def mark_running(record_id: int, started_at: str) -> None:
    """Flip a record to status='running'."""
    with _connect() as conn:
        conn.execute(
            "UPDATE conversions SET status = 'running', started_at = ? WHERE id = ?",
            (started_at, record_id),
        )


def reset_stale_running() -> int:
    """Reset any 'running' records to 'pending' on startup (handles crash recovery)."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE conversions SET status = 'pending' WHERE status = 'running'"
        )
        return cur.rowcount


def reset_done_to_pending(record_id: int) -> None:
    """Reset a 'done' record back to 'pending' so it is re-queued on next scan."""
    with _connect() as conn:
        conn.execute(
            "UPDATE conversions SET status = 'pending' WHERE id = ?",
            (record_id,),
        )


def mark_done(
    record_id: int,
    output_path: str,
    output_size_mb: float,
    saved_mb: float,
    saved_pct: int,
    completed_at: str,
    encoder_used: str | None = None,
    output_hash: str | None = None,
    output_bitrate_kbps: int | None = None,
) -> None:
    """Flip a record to status='done' and fill in output metrics."""
    with _connect() as conn:
        conn.execute(
            """
            UPDATE conversions
               SET status              = 'done',
                   output_path         = ?,
                   output_size_mb      = ?,
                   saved_mb            = ?,
                   saved_pct           = ?,
                   completed_at        = ?,
                   encoder_used        = ?,
                   output_hash         = ?,
                   output_bitrate_kbps = ?
             WHERE id = ?
            """,
            (_norm(output_path), output_size_mb, saved_mb, saved_pct,
             completed_at, encoder_used, output_hash, output_bitrate_kbps, record_id),
        )


def update_output_bitrate(record_id: int, bitrate_kbps: int) -> None:
    """Store the output file bitrate for a done record (used when probing old records)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE conversions SET output_bitrate_kbps = ? WHERE id = ?",
            (bitrate_kbps, record_id),
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


def mark_no_saving(record_id: int, completed_at: str) -> None:
    """Flip a record to status='no_saving' — encode succeeded but output was not smaller."""
    with _connect() as conn:
        conn.execute(
            """
            UPDATE conversions
               SET status       = 'no_saving',
                   error_tail   = NULL,
                   completed_at = ?
             WHERE id = ?
            """,
            (completed_at, record_id),
        )


def mark_low_savings(record_id: int, est_pct: int, threshold_pct: int, completed_at: str) -> None:
    """Flip a record to status='low_savings' — pre-encode estimate predicted insufficient savings."""
    with _connect() as conn:
        conn.execute(
            """
            UPDATE conversions
               SET status       = 'low_savings',
                   error_tail   = ?,
                   completed_at = ?
             WHERE id = ?
            """,
            (f"Estimated savings: {est_pct}% (below {threshold_pct}% threshold)", completed_at, record_id),
        )


def save_estimate(
    record_id: int,
    est_pct: int,
    est_mb: float,
    est_sample_cv_pct: float | None = None,
    est_high_variance: bool = False,
    est_aggregation: str | None = None,
    est_quality: int | None = None,
    est_version: int | None = None,
) -> None:
    """Persist the estimate result so it is not re-computed on subsequent runs."""
    with _connect() as conn:
        conn.execute(
            """
            UPDATE conversions
               SET est_saving_pct = ?,
                   est_saving_mb = ?,
                   est_sample_cv_pct = ?,
                   est_high_variance = ?,
                   est_aggregation = ?,
                   est_quality = ?,
                   est_version = ?
             WHERE id = ?
            """,
            (
                est_pct,
                est_mb,
                est_sample_cv_pct,
                1 if est_high_variance else 0,
                est_aggregation,
                est_quality,
                est_version,
                record_id,
            ),
        )


def delete_records_by_path(source_path: str) -> int:
    """Delete all DB records whose source_path matches.  Returns row count deleted."""
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM conversions WHERE source_path = ?",
            (_norm(source_path),),
        )
        return cur.rowcount


def get_dropped_streams(source_path: str) -> list[int]:
    """Return the list of ffprobe stream indices marked as dropped for this path."""
    source_path = _norm(source_path)
    with _connect() as conn:
        row = conn.execute(
            "SELECT dropped_streams FROM conversions WHERE source_path = ? "
            "ORDER BY id DESC LIMIT 1",
            (source_path,),
        ).fetchone()
    if not row:
        return []
    return _json_loads_safe(row["dropped_streams"])


def set_dropped_streams(source_path: str, indices: list[int]) -> None:
    """Persist the list of dropped ffprobe stream indices for all records of this path."""
    source_path = _norm(source_path)
    with _connect() as conn:
        conn.execute(
            "UPDATE conversions SET dropped_streams = ? WHERE source_path = ?",
            (_json.dumps(sorted(set(indices))), source_path),
        )


def sync_after_stream_edit(
    source_path: str,
    source_mtime: float,
    source_size_bytes: int,
    source_size_mb: float,
    source_codec: str | None,
    source_bitrate_kbps: int | None,
    source_duration_secs: float | None,
    source_video_track_count: int | None,
    source_audio_track_count: int | None,
    content_hash: str | None,
    completed_at: str,
) -> str:
    """Persist file metadata after in-place stream-edit replacement.

    If the file has already been converted before, keep the replacement as
    'done' so it stays out of the queue. Otherwise leave it 'pending' so the
    user can edit streams first and convert later.
    """
    source_path = _norm(source_path)
    with _connect() as conn:
        prev = conn.execute(
            "SELECT status, anime_mode, force_sw, force_convert FROM conversions "
            "WHERE source_path = ? ORDER BY id DESC LIMIT 1",
            (source_path,),
        ).fetchone()
        done_exists = conn.execute(
            "SELECT 1 FROM conversions WHERE source_path = ? AND status = 'done' LIMIT 1",
            (source_path,),
        ).fetchone() is not None
        new_status = "done" if done_exists else "pending"
        anime_mode = int(prev["anime_mode"]) if prev else 0
        force_sw = int(prev["force_sw"]) if prev else 0
        force_convert = int(prev["force_convert"]) if prev else 0

        conn.execute(
            """
            INSERT INTO conversions (
                source_path, source_mtime, source_size_bytes, source_size_mb,
                source_codec, source_hash, source_bitrate_kbps, source_duration_secs,
                source_video_track_count, source_audio_track_count,
                status, anime_mode, force_sw, force_convert,
                output_path, output_size_mb, output_hash, output_bitrate_kbps,
                encoder_used, completed_at, dropped_streams, error_tail, started_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
            ON CONFLICT(source_path, source_mtime) DO UPDATE SET
                source_size_bytes    = excluded.source_size_bytes,
                source_size_mb       = excluded.source_size_mb,
                source_codec         = excluded.source_codec,
                source_hash          = excluded.source_hash,
                source_bitrate_kbps  = excluded.source_bitrate_kbps,
                source_duration_secs = excluded.source_duration_secs,
                source_video_track_count = excluded.source_video_track_count,
                source_audio_track_count = excluded.source_audio_track_count,
                status               = excluded.status,
                anime_mode           = excluded.anime_mode,
                force_sw             = excluded.force_sw,
                force_convert        = excluded.force_convert,
                output_path          = excluded.output_path,
                output_size_mb       = excluded.output_size_mb,
                output_hash          = excluded.output_hash,
                output_bitrate_kbps  = excluded.output_bitrate_kbps,
                encoder_used         = excluded.encoder_used,
                completed_at         = excluded.completed_at,
                dropped_streams      = excluded.dropped_streams,
                error_tail           = NULL,
                started_at           = NULL
            """,
            (
                source_path,
                source_mtime,
                source_size_bytes,
                source_size_mb,
                source_codec,
                content_hash,
                source_bitrate_kbps,
                source_duration_secs,
                source_video_track_count,
                source_audio_track_count,
                new_status,
                anime_mode,
                force_sw,
                force_convert,
                source_path,
                source_size_mb,
                content_hash,
                source_bitrate_kbps,
                "stream_edit_copy",
                completed_at,
                _json.dumps([]),
            ),
        )
    return new_status


def save_probe_result(
    source_path: str,
    source_mtime: float,
    codec: str | None,
    bitrate_kbps: int | None,
    duration_secs: float | None,
    video_track_count: int | None = None,
    audio_track_count: int | None = None,
    source_size_bytes: int | None = None,
    source_size_mb: float | None = None,
) -> None:
    """
    Upsert probe data for a file so subsequent scans can skip ffprobe.
    Creates a minimal pending record if none exists; otherwise updates
    bitrate/duration/size and preserves existing codec if already set.
    """
    source_path = _norm(source_path)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO conversions
                (source_path, source_mtime, source_codec,
                 source_bitrate_kbps, source_duration_secs,
                 source_video_track_count, source_audio_track_count,
                 source_size_bytes, source_size_mb, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            ON CONFLICT (source_path, source_mtime) DO UPDATE SET
                source_codec         = COALESCE(conversions.source_codec,         excluded.source_codec),
                source_bitrate_kbps  = excluded.source_bitrate_kbps,
                source_duration_secs = excluded.source_duration_secs,
                source_video_track_count = excluded.source_video_track_count,
                source_audio_track_count = excluded.source_audio_track_count,
                source_size_bytes    = COALESCE(conversions.source_size_bytes,    excluded.source_size_bytes),
                source_size_mb       = COALESCE(conversions.source_size_mb,       excluded.source_size_mb)
            """,
            (source_path, source_mtime, codec, bitrate_kbps, duration_secs,
             video_track_count, audio_track_count,
             source_size_bytes, source_size_mb),
        )


def batch_update_sizes(items: list) -> None:
    """
    Fill in missing source_size_bytes / source_size_mb for a list of records.
    items: [(record_id, size_bytes, size_mb), ...]
    Only updates rows where the value is currently NULL.
    """
    if not items:
        return
    with _connect() as conn:
        conn.executemany(
            """
            UPDATE conversions
               SET source_size_bytes = COALESCE(source_size_bytes, ?),
                   source_size_mb    = COALESCE(source_size_mb,    ?)
             WHERE id = ?
            """,
            [(size_bytes, size_mb, record_id) for record_id, size_bytes, size_mb in items],
        )
