"""
backfill_durations.py
---------------------
One-off script: probe all 'done' DB records that are missing
source_duration_secs, source_bitrate_kbps, or output_bitrate_kbps
and persist the values so the UI can display them.

Run from the repo root:
    python VideoConverter/tests/backfill_durations.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sqlite3


# ---------------------------------------------------------------------------
# ffprobe helpers
# ---------------------------------------------------------------------------

def _ffprobe(path: str) -> dict | None:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_format", "-show_streams",
                path,
            ],
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except Exception:
        return None


def _parse(probe: dict) -> tuple[float | None, int | None]:
    """Return (duration_secs, bitrate_kbps) from a probe result."""
    duration_secs: float | None = None
    bitrate_kbps: int | None = None

    fmt = probe.get("format", {})
    if fmt.get("duration"):
        try:
            duration_secs = float(fmt["duration"])
        except (ValueError, TypeError):
            pass

    # Prefer video stream bitrate; fall back to format bitrate
    for stream in probe.get("streams", []):
        if stream.get("codec_type") == "video":
            br = stream.get("bit_rate")
            if br:
                try:
                    bitrate_kbps = round(int(br) / 1000)
                except (ValueError, TypeError):
                    pass
            break

    if bitrate_kbps is None and fmt.get("bit_rate"):
        try:
            bitrate_kbps = round(int(fmt["bit_rate"]) / 1000)
        except (ValueError, TypeError):
            pass

    return duration_secs, bitrate_kbps


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(dry_run: bool = False) -> None:
    import sqlite3 as _sqlite3
    _db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "conversions.db")
    conn = _sqlite3.connect(_db_path)
    conn.row_factory = _sqlite3.Row

    rows = conn.execute("""
        SELECT id, source_path, output_path, source_size_mb,
               source_duration_secs, source_bitrate_kbps, output_bitrate_kbps,
               output_size_mb
          FROM conversions
         WHERE status = 'done'
           AND (source_duration_secs IS NULL
                OR source_bitrate_kbps IS NULL
                OR output_bitrate_kbps IS NULL)
         ORDER BY id
    """).fetchall()

    total = len(rows)
    print(f"Found {total} done records missing duration/bitrate data.")
    if dry_run:
        print("DRY RUN — no changes will be written.\n")

    updated = 0
    skipped_missing = 0
    skipped_probe_fail = 0

    for i, row in enumerate(rows, 1):
        record_id   = row["id"]
        source_path = row["source_path"]
        output_path = row["output_path"]
        source_size_mb = row["source_size_mb"]

        # Determine which file to probe: prefer output, fall back to source
        probe_path = None
        for candidate in (output_path, source_path):
            if candidate and os.path.isfile(candidate):
                probe_path = candidate
                break

        if probe_path is None:
            skipped_missing += 1
            if i <= 5 or i % 100 == 0:
                print(f"[{i}/{total}] SKIP (file not found) id={record_id}")
            continue

        probe = _ffprobe(probe_path)
        if probe is None:
            skipped_probe_fail += 1
            print(f"[{i}/{total}] SKIP (probe failed)  id={record_id} {os.path.basename(probe_path)}")
            continue

        duration_secs, out_bitrate_kbps = _parse(probe)

        # Compute source bitrate from source_size_mb + duration if we have them
        src_bitrate_kbps: int | None = row["source_bitrate_kbps"]
        if src_bitrate_kbps is None and source_size_mb and duration_secs and duration_secs > 0:
            src_bitrate_kbps = round(source_size_mb * 8192 / duration_secs)

        # If output_bitrate_kbps still missing but we have output_size_mb + duration
        if out_bitrate_kbps is None:
            out_size = row["output_size_mb"]
            if out_size and duration_secs and duration_secs > 0:
                out_bitrate_kbps = round(out_size * 8192 / duration_secs)

        if i % 50 == 0 or i <= 3:
            print(
                f"[{i}/{total}] id={record_id} dur={duration_secs:.1f}s "
                f"src_br={src_bitrate_kbps} out_br={out_bitrate_kbps} "
                f"— {os.path.basename(probe_path)}"
            )

        if not dry_run:
            updates: list[str] = []
            params: list = []
            if duration_secs is not None and row["source_duration_secs"] is None:
                updates.append("source_duration_secs = ?")
                params.append(duration_secs)
            if src_bitrate_kbps is not None and row["source_bitrate_kbps"] is None:
                updates.append("source_bitrate_kbps = ?")
                params.append(src_bitrate_kbps)
            if out_bitrate_kbps is not None and row["output_bitrate_kbps"] is None:
                updates.append("output_bitrate_kbps = ?")
                params.append(out_bitrate_kbps)
            if updates:
                params.append(record_id)
                conn.execute(
                    f"UPDATE conversions SET {', '.join(updates)} WHERE id = ?",
                    params,
                )
        updated += 1

    if not dry_run:
        conn.commit()
    conn.close()

    print(f"\nDone.")
    print(f"  Updated:             {updated}")
    print(f"  Skipped (no file):   {skipped_missing}")
    print(f"  Skipped (bad probe): {skipped_probe_fail}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill duration/bitrate on done DB records.")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be changed without writing.")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
