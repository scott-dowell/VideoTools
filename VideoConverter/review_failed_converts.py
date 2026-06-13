#!/usr/bin/env python3
"""Summarize recent failed conversions and artifact availability.

Usage:
  python review_failed_converts.py
  python review_failed_converts.py --limit 20 --since-hours 72
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


@dataclass
class FailedEntry:
    when: datetime
    title: str
    detail: str
    line: str


LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}) \|\s+FAILED\s+\|\s+"
    r"(?P<title>.+?)\s+\|\s+.+?\|\s+FAILED at:\s*(?P<detail>.+?)\s+\[\d+s\]$"
)


def load_settings(base_dir: Path) -> dict:
    settings_path = base_dir / "settings.json"
    if not settings_path.exists():
        return {}
    try:
        return json.loads(settings_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def parse_failed(history_path: Path, since_hours: int, limit: int) -> list[FailedEntry]:
    if not history_path.exists():
        return []

    cutoff = datetime.now() - timedelta(hours=since_hours)
    rows: list[FailedEntry] = []

    for raw in history_path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = LINE_RE.match(raw.strip())
        if not m:
            continue
        ts = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M")
        if ts < cutoff:
            continue
        rows.append(
            FailedEntry(
                when=ts,
                title=m.group("title").rstrip(),
                detail=m.group("detail").strip(),
                line=raw,
            )
        )

    rows.sort(key=lambda r: r.when, reverse=True)
    return rows[:limit]


def find_run_dirs(logs_dir: Path, title: str, max_items: int = 5) -> list[Path]:
    prefix = f"{title}_".lower()
    candidates = [p for p in logs_dir.iterdir() if p.is_dir() and p.name.lower().startswith(prefix)] if logs_dir.exists() else []
    candidates.sort(key=lambda p: p.name, reverse=True)
    return candidates[:max_items]


def main() -> int:
    parser = argparse.ArgumentParser(description="Review failed conversion runs and intermediate availability.")
    parser.add_argument("--limit", type=int, default=10, help="Maximum failed entries to report")
    parser.add_argument("--since-hours", type=int, default=24, help="Only include failures within this many hours")
    args = parser.parse_args()

    base_dir = Path(__file__).resolve().parent
    logs_dir = base_dir / "logs"
    history_path = logs_dir / "conversion_history.log"

    settings = load_settings(base_dir)
    temp_dir = Path(settings.get("local_temp_dir", r"C:\Temp\vc_working"))
    keep_failed = bool(settings.get("keep_failed_intermediates", False))

    failed = parse_failed(history_path, args.since_hours, args.limit)

    print("Failed Conversion Review")
    print("=" * 80)
    print(f"History file: {history_path}")
    print(f"Temp dir:     {temp_dir}")
    print(f"Keep failed intermediates: {keep_failed}")
    print()

    if not failed:
        print("No failed entries in the selected window.")
        return 0

    review_root = temp_dir / "_failed_intermediates"

    for i, row in enumerate(failed, start=1):
        print(f"[{i}] {row.when:%Y-%m-%d %H:%M}  {row.title}")
        print(f"    Reason: {row.detail}")

        runs = find_run_dirs(logs_dir, row.title)
        if runs:
            print("    Log runs:")
            for run in runs:
                c = run / "compress_qsv.log"
                r1 = run / "remux_attempt_1.log"
                r5 = run / "remux_attempt_5.log"
                print(
                    "      - "
                    f"{run.name} "
                    f"(compress={'Y' if c.exists() else 'N'}, "
                    f"remux1={'Y' if r1.exists() else 'N'}, "
                    f"remux5={'Y' if r5.exists() else 'N'})"
                )
        else:
            print("    Log runs: none found")

        staged_mp4 = temp_dir / f"{row.title}.mp4"
        staged_mkv = temp_dir / f"{row.title}.mkv"
        review_dirs = []
        if review_root.exists():
            prefix = f"{row.title}_".lower()
            review_dirs = sorted(
                [p for p in review_root.iterdir() if p.is_dir() and p.name.lower().startswith(prefix)],
                key=lambda p: p.name,
                reverse=True,
            )

        print(
            "    Intermediates: "
            f"staged_mp4={'Y' if staged_mp4.exists() else 'N'}, "
            f"staged_mkv={'Y' if staged_mkv.exists() else 'N'}, "
            f"preserved_bundles={len(review_dirs)}"
        )
        if review_dirs:
            print(f"      latest bundle: {review_dirs[0]}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
