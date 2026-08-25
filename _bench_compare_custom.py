import json
import csv
import os
import sqlite3
import statistics
import sys
import time
import argparse
from collections import defaultdict
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, r"c:/VideoTools/VideoConverter")
import converter
import config

DB_PATH = "c:/VideoTools/VideoConverter/conversions.db"
ANIME_ROOT = r"C:/Users/scott/Downloads/Anime"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare estimate policies across anime and non-anime samples.")
    parser.add_argument("--anime-root", default=ANIME_ROOT, help="Root folder containing anime samples")
    parser.add_argument("--anime-count", type=int, default=5, help="Number of viable anime files to benchmark")
    parser.add_argument("--non-anime-count", type=int, default=5, help="Number of viable non-anime files to benchmark")
    parser.add_argument("--scan-limit", type=int, default=120, help="How many candidate files to scan per bucket before filtering")
    parser.add_argument("--csv", default="", help="Optional CSV path to write per-file results")
    return parser.parse_args()


def anime_candidates(root: str, limit_scan: int = 80) -> list[str]:
    exts = {".mp4", ".mkv", ".avi", ".mov"}
    out = []
    for folder, _, files in os.walk(root):
        for name in files:
            if Path(name).suffix.lower() in exts:
                p = os.path.join(folder, name)
                out.append(p)
                if len(out) >= limit_scan:
                    return out
    return out


def non_anime_candidates(limit_scan: int = 80) -> list[str]:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    rows = cur.execute(
        """
        SELECT source_path
        FROM conversions
        WHERE lower(replace(source_path,'\\','/')) LIKE 'n:/videos/%'
          AND source_duration_secs >= 180
          AND source_duration_secs <= 1800
          AND source_codec IS NOT NULL
        ORDER BY source_mtime DESC
        LIMIT ?
        """,
        (limit_scan,),
    ).fetchall()
    con.close()
    return [p for (p,) in rows if p and os.path.exists(p)]


def filter_viable(paths: list[str], limit: int) -> list[str]:
    viable = []
    for p in paths:
        with patch.object(config, "ESTIMATE_SAMPLE_FRACTIONS", (1 / 6, 5 / 6)), \
             patch.object(config, "ESTIMATE_CLIP_SECS", 15.0):
            r = converter.estimate(p, quality=30)
        if not r.get("error"):
            viable.append(p)
        if len(viable) >= limit:
            break
    return viable


def run_estimate(path: str, fractions: tuple[float, ...], clip_secs: float) -> dict:
    original_run = converter.subprocess.run
    calls = []

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
        calls.append((kind, dt))
        return result

    with patch.object(config, "ESTIMATE_SAMPLE_FRACTIONS", fractions), \
         patch.object(config, "ESTIMATE_CLIP_SECS", clip_secs), \
         patch.object(converter.subprocess, "run", side_effect=timed_run):
        t0 = time.perf_counter()
        res = converter.estimate(path, quality=30)
        total = time.perf_counter() - t0

    agg = defaultdict(float)
    for k, dt in calls:
        agg[k] += dt

    return {
        "path": path,
        "total_s": total,
        "result": res,
        "timing": {
            "encode_s": agg.get("encode", 0.0),
            "extract_s": agg.get("extract", 0.0),
            "ffprobe_s": agg.get("ffprobe", 0.0),
            "calls": len(calls),
        },
    }


def summarize(rows: list[dict]) -> dict:
    rows = [r for r in rows if not r["result"].get("error")]
    if not rows:
        return {"ok": 0}
    totals = [r["total_s"] for r in rows]
    est = [r["result"].get("estimated_saving_pct") for r in rows]
    cv = [r["result"].get("sample_cv_pct") for r in rows]
    return {
        "ok": len(rows),
        "avg_total_s": round(statistics.mean(totals), 3),
        "median_total_s": round(statistics.median(totals), 3),
        "avg_est_pct": round(statistics.mean(est), 2) if est else None,
        "avg_sample_cv_pct": round(statistics.mean(cv), 2) if cv else None,
    }


def write_csv_rows(csv_path: str, rows: list[dict]) -> None:
    if not csv_path:
        return
    fieldnames = [
        "policy",
        "bucket",
        "file",
        "path",
        "total_s",
        "sample_count",
        "estimated_saving_pct",
        "sample_cv_pct",
        "error",
        "encode_s",
        "extract_s",
        "ffprobe_s",
        "calls",
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "policy": row["policy"],
                "bucket": row["bucket"],
                "file": Path(row["path"]).name,
                "path": row["path"],
                "total_s": round(row["total_s"], 3),
                "sample_count": row["result"].get("sample_count"),
                "estimated_saving_pct": row["result"].get("estimated_saving_pct"),
                "sample_cv_pct": row["result"].get("sample_cv_pct"),
                "error": row["result"].get("error"),
                "encode_s": round(row["timing"].get("encode_s", 0.0), 3),
                "extract_s": round(row["timing"].get("extract_s", 0.0), 3),
                "ffprobe_s": round(row["timing"].get("ffprobe_s", 0.0), 3),
                "calls": row["timing"].get("calls"),
            })


def main() -> None:
    args = parse_args()
    anime = filter_viable(anime_candidates(args.anime_root, args.scan_limit), limit=args.anime_count)
    non = filter_viable(non_anime_candidates(args.scan_limit), limit=args.non_anime_count)
    files = [("anime", p) for p in anime] + [("non_anime", p) for p in non]
    if not files:
        raise SystemExit("No viable files found")

    policies = [
        ("2x15", (1 / 6, 5 / 6), 15.0),
        ("5x10", (1 / 6, 2 / 6, 3 / 6, 4 / 6, 5 / 6), 10.0),
    ]

    all_results = {}
    flat_rows: list[dict] = []
    for name, fracs, secs in policies:
        rows = []
        print(f"Running {name}")
        for bucket, path in files:
            r = run_estimate(path, fracs, secs)
            r["bucket"] = bucket
            r["policy"] = name
            rows.append(r)
            flat_rows.append(r)
            print(json.dumps({
                "policy": name,
                "bucket": bucket,
                "file": Path(path).name,
                "total_s": round(r["total_s"], 3),
                "est_pct": r["result"].get("estimated_saving_pct"),
                "sample_cv_pct": r["result"].get("sample_cv_pct"),
                "sample_count": r["result"].get("sample_count"),
                "error": r["result"].get("error"),
            }, ensure_ascii=True))
        all_results[name] = rows

    summary = {}
    for name, rows in all_results.items():
        summary[name] = {
            "all": summarize(rows),
            "anime": summarize([r for r in rows if r["bucket"] == "anime"]),
            "non_anime": summarize([r for r in rows if r["bucket"] == "non_anime"]),
        }

    print("\n=== FILES USED ===")
    print(json.dumps(files, ensure_ascii=True, indent=2))
    print("\n=== SUMMARY ===")
    print(json.dumps(summary, ensure_ascii=True, indent=2))

    if args.csv:
        write_csv_rows(args.csv, flat_rows)
        print(f"\nCSV written to: {args.csv}")


if __name__ == "__main__":
    main()
