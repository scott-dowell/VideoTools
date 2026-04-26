"""
cleanup_db.py — cleans two categories of junk from conversions.db:

1. Orphaned done records: status='done' but neither source nor output file
   exists on disk anymore. Safe to remove — nothing to track.

2. Duplicate source_paths: multiple records for the same source_path.
   Keep the best record (done > no_saving > skipped > pending > failed),
   using highest id to break ties. Delete all others.

Usage:
    python VideoConverter/tests/cleanup_db.py [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import sqlite3

DB = r"C:\VideoTools\VideoConverter\conversions.db"

STATUS_PRIORITY = {"done": 0, "no_saving": 1, "skipped": 2, "pending": 3, "queued": 3, "failed": 4}


def _best_record(records: list[sqlite3.Row]) -> sqlite3.Row:
    """Return the record to keep from a group with the same source_path."""
    return min(records, key=lambda r: (STATUS_PRIORITY.get(r["status"], 99), -r["id"]))


def main(dry_run: bool = False) -> None:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    if dry_run:
        print("DRY RUN — no changes will be written.\n")

    # ── 1. Orphaned done records ─────────────────────────────────────────────
    done_rows = conn.execute(
        "SELECT id, source_path, output_path FROM conversions WHERE status = 'done'"
    ).fetchall()

    orphan_ids = []
    for r in done_rows:
        src_ok = r["source_path"] and os.path.isfile(r["source_path"])
        out_ok = r["output_path"] and os.path.isfile(r["output_path"])
        if not src_ok and not out_ok:
            orphan_ids.append(r["id"])

    print(f"Orphaned done records (neither file exists): {len(orphan_ids)}")
    if not dry_run and orphan_ids:
        placeholders = ",".join("?" * len(orphan_ids))
        cur = conn.execute(f"DELETE FROM conversions WHERE id IN ({placeholders})", orphan_ids)
        print(f"  Deleted: {cur.rowcount}")

    # ── 2. Duplicate source_paths ────────────────────────────────────────────
    dup_paths = conn.execute("""
        SELECT source_path
        FROM conversions
        GROUP BY source_path
        HAVING COUNT(*) > 1
    """).fetchall()

    print(f"\nSource paths with multiple records: {len(dup_paths)}")

    delete_ids: list[int] = []
    for row in dup_paths:
        path = row["source_path"]
        records = conn.execute(
            "SELECT id, status FROM conversions WHERE source_path = ? ORDER BY id",
            (path,),
        ).fetchall()
        keep = _best_record(records)
        for rec in records:
            if rec["id"] != keep["id"]:
                delete_ids.append(rec["id"])

    print(f"  Records to delete (keeping best per path): {len(delete_ids)}")
    if not dry_run and delete_ids:
        placeholders = ",".join("?" * len(delete_ids))
        cur = conn.execute(f"DELETE FROM conversions WHERE id IN ({placeholders})", delete_ids)
        print(f"  Deleted: {cur.rowcount}")

    if not dry_run:
        conn.commit()
        conn.isolation_level = None  # autocommit mode required for VACUUM
        conn.execute("VACUUM")
        print("\nVACUUM complete.")

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
