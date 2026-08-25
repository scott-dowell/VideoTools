import json
import os
import sqlite3
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, r"c:/VideoTools/VideoConverter")
import converter
import config

DB_PATH = "c:/VideoTools/VideoConverter/conversions.db"


def fetch_candidates(limit_each: int = 2) -> tuple[list[str], list[str]]:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    anime_keywords = [
        "%anime%", "%one piece%", "%naruto%", "%bleach%", "%db_%", "%dxd%", "%kanokon%",
    ]
    anime_clause = " OR ".join(["lower(replace(source_path,'\\\\','/')) LIKE ?" for _ in anime_keywords])

    q_common = """
        source_duration_secs >= 180
        AND source_duration_secs <= 1800
        AND source_codec IS NOT NULL
    """

    anime_rows = cur.execute(
        f"""
        SELECT source_path
        FROM conversions
        WHERE ({anime_clause})
          AND {q_common}
        ORDER BY source_mtime DESC
        LIMIT ?
        """,
        [*anime_keywords, limit_each * 60],
    ).fetchall()

    non_anime_rows = cur.execute(
        f"""
        SELECT source_path
        FROM conversions
        WHERE NOT ({anime_clause})
          AND lower(replace(source_path,'\\\\','/')) LIKE 'n:/videos/%'
          AND {q_common}
        ORDER BY source_mtime DESC
        LIMIT ?
        """,
        [*anime_keywords, limit_each * 60],
    ).fetchall()

    con.close()

    anime_paths = []
    for (p,) in anime_rows:
        if p and os.path.exists(p) and p not in anime_paths:
            anime_paths.append(p)
        if len(anime_paths) >= limit_each:
            break

    non_paths = []
    for (p,) in non_anime_rows:
        if p and os.path.exists(p) and p not in non_paths:
            non_paths.append(p)
        if len(non_paths) >= limit_each:
            break

    return anime_paths, non_paths


def filter_viable(paths: list[str], limit: int, fractions: tuple[float, ...], clip_secs: float) -> list[str]:
    """Keep files where estimate runs successfully under the baseline policy."""
    viable = []
    for p in paths:
        with patch.object(config, "ESTIMATE_SAMPLE_FRACTIONS", fractions), \
             patch.object(config, "ESTIMATE_CLIP_SECS", clip_secs):
            r = converter.estimate(p, quality=30)
        if not r.get("error"):
            viable.append(p)
        if len(viable) >= limit:
            break
    return viable


def run_estimate_with_timing(path: str, fractions: tuple[float, ...], clip_secs: float) -> dict:
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
        result = converter.estimate(path, quality=30)
        total = time.perf_counter() - t0

    agg = defaultdict(float)
    for kind, dt in calls:
        agg[kind] += dt

    return {
        "path": path,
        "total_s": total,
        "result": result,
        "timing": {
            "encode_s": agg.get("encode", 0.0),
            "extract_s": agg.get("extract", 0.0),
            "ffprobe_s": agg.get("ffprobe", 0.0),
            "calls": len(calls),
        },
    }


def summarize(label: str, rows: list[dict]) -> dict:
    totals = [r["total_s"] for r in rows if not r["result"].get("error")]
    est_pct = [r["result"].get("estimated_saving_pct") for r in rows if not r["result"].get("error")]
    cv = [r["result"].get("sample_cv_pct") for r in rows if not r["result"].get("error")]

    if not totals:
        return {"label": label, "ok": 0}

    return {
        "label": label,
        "ok": len(totals),
        "avg_total_s": round(statistics.mean(totals), 3),
        "median_total_s": round(statistics.median(totals), 3),
        "p95_total_s": round(sorted(totals)[max(0, int(0.95 * len(totals)) - 1)], 3),
        "avg_est_pct": round(statistics.mean(est_pct), 2) if est_pct else None,
        "est_pct_stdev": round(statistics.pstdev(est_pct), 2) if len(est_pct) > 1 else 0.0,
        "avg_sample_cv_pct": round(statistics.mean(cv), 2) if cv else None,
    }


def main() -> None:
    anime_paths, non_paths = fetch_candidates(limit_each=2)
    baseline_fracs = (1 / 6, 5 / 6)
    baseline_secs = 15.0
    anime_viable = filter_viable(anime_paths, limit=2, fractions=baseline_fracs, clip_secs=baseline_secs)
    non_viable = filter_viable(non_paths, limit=2, fractions=baseline_fracs, clip_secs=baseline_secs)
    all_paths = [("anime", p) for p in anime_viable] + [("non_anime", p) for p in non_viable]

    if not all_paths:
        raise SystemExit("No viable candidate files found on disk for benchmark")

    policies = [
        ("2x15", (1 / 6, 5 / 6), 15.0),
        ("5x10", (1 / 6, 2 / 6, 3 / 6, 4 / 6, 5 / 6), 10.0),
    ]

    out = {"files": all_paths, "results": {}}

    for policy_name, fracs, secs in policies:
        policy_rows = []
        print(f"Running policy {policy_name} ...")
        for bucket, path in all_paths:
            row = run_estimate_with_timing(path, fracs, secs)
            row["bucket"] = bucket
            policy_rows.append(row)
            print(json.dumps({
                "policy": policy_name,
                "bucket": bucket,
                "file": Path(path).name,
                "total_s": round(row["total_s"], 3),
                "est_pct": row["result"].get("estimated_saving_pct"),
                "sample_count": row["result"].get("sample_count"),
                "sample_cv_pct": row["result"].get("sample_cv_pct"),
                "error": row["result"].get("error"),
            }, ensure_ascii=True))
        out["results"][policy_name] = policy_rows

    def by_bucket(rows: list[dict], bucket: str) -> list[dict]:
        return [r for r in rows if r.get("bucket") == bucket and not r["result"].get("error")]

    summary = {}
    for policy_name, rows in out["results"].items():
        summary[policy_name] = {
            "all": summarize(f"{policy_name}-all", rows),
            "anime": summarize(f"{policy_name}-anime", by_bucket(rows, "anime")),
            "non_anime": summarize(f"{policy_name}-non", by_bucket(rows, "non_anime")),
        }

    print("\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
