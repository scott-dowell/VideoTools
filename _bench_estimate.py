import os
import time
import statistics
import subprocess
import sqlite3
import sys
from collections import defaultdict
from unittest.mock import patch

sys.path.insert(0, r"c:/VideoTools/VideoConverter")
import converter

DB_PATH = "c:/VideoTools/VideoConverter/conversions.db"


def pick_sample_path() -> str | None:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    row = cur.execute(
        """
        SELECT source_path
        FROM conversions
        WHERE lower(replace(source_path,'\\','/')) LIKE 'n:/videos/casting/%'
          AND source_duration_secs >= 120
          AND source_duration_secs <= 900
        ORDER BY source_mtime DESC
        LIMIT 1
        """
    ).fetchone()
    con.close()
    return row[0] if row else None


def main() -> None:
    path = pick_sample_path()
    print("sample_path=", path)
    if not path or not os.path.exists(path):
        raise SystemExit("Sample file not found")

    startup = []
    for _ in range(8):
        t0 = time.perf_counter()
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        startup.append(time.perf_counter() - t0)

    print(
        "ffmpeg_version_startup_avg_ms=",
        round(statistics.mean(startup) * 1000, 1),
        "min_ms=",
        round(min(startup) * 1000, 1),
        "max_ms=",
        round(max(startup) * 1000, 1),
    )

    original_run = converter.subprocess.run
    calls: list[tuple[str, float, int | None]] = []

    def timed_run(cmd, *args, **kwargs):
        t0 = time.perf_counter()
        result = original_run(cmd, *args, **kwargs)
        dt = time.perf_counter() - t0

        exe = cmd[0] if isinstance(cmd, list) and cmd else ""
        kind = "other"
        if exe == "ffprobe":
            kind = "ffprobe"
        elif exe == "ffmpeg":
            out = cmd[-1] if isinstance(cmd, list) and cmd else ""
            if isinstance(out, str) and "_est_src_" in out:
                kind = "extract"
            elif isinstance(out, str) and "_est_enc_" in out:
                kind = "encode"
            else:
                kind = "ffmpeg_other"

        calls.append((kind, dt, getattr(result, "returncode", None)))
        return result

    with patch.object(converter.subprocess, "run", side_effect=timed_run):
        t0 = time.perf_counter()
        estimate_result = converter.estimate(path, quality=30)
        total = time.perf_counter() - t0

    print("estimate_result=", estimate_result)
    print("estimate_total_s=", round(total, 3))

    agg: dict[str, list[float]] = defaultdict(list)
    for k, dt, _ in calls:
        agg[k].append(dt)

    for k in sorted(agg):
        vals = agg[k]
        print(
            k,
            "count=",
            len(vals),
            "sum_s=",
            round(sum(vals), 3),
            "avg_ms=",
            round((sum(vals) / len(vals)) * 1000, 1),
        )

    print("all_calls_count=", len(calls), "all_calls_sum_s=", round(sum(dt for _, dt, _ in calls), 3))


if __name__ == "__main__":
    main()
