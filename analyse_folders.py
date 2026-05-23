#!/usr/bin/env python3
"""
analyse_folders.py — identify best conversion targets in a folder tree.

Usage:
    python analyse_folders.py <root_folder> [options]

Examples:
    python analyse_folders.py "F:\\Videos"
    python analyse_folders.py "F:\\Videos" --min-done 3 --top 10 --sort savings
"""

import argparse
import os
import posixpath
import sqlite3
from datetime import datetime
from pathlib import Path

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

DB_PATH = Path(__file__).parent / "VideoConverter" / "conversions.db"

_PENDING_STATUSES = {"pending", "failed"}
_DONE_STATUS = "done"
_EXCLUDE_STATUSES = {"low_savings", "no_saving", "skipped"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _norm(path: str) -> str:
    """Forward slashes, lowercase — for consistent path comparison."""
    return path.replace("\\", "/").lower()


def _folder(source_path: str) -> str:
    """Return normalised dirname of source_path."""
    return posixpath.dirname(_norm(source_path))


def _mb_str(mb: float) -> str:
    if mb >= 1024:
        return f"{mb / 1024:.1f} GB"
    return f"{mb:.0f} MB"


def _savings_style(pct: float) -> str:
    if pct >= 40:
        return "green"
    if pct >= 20:
        return "yellow"
    return "dim"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_rows():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT source_path, status, saved_mb, saved_pct, source_size_mb, "
        "source_duration_secs, started_at, completed_at, est_saving_pct "
        "FROM conversions"
    ).fetchall()
    con.close()
    return rows


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def _analyse(rows, root_norm: str, min_done: int, min_pending: int):
    prefix = root_norm.rstrip("/") + "/"

    # Accumulate rows per folder
    groups: dict[str, dict] = {}

    for row in rows:
        path = row["source_path"]
        if not path:
            continue
        if not _norm(path).startswith(prefix):
            continue

        fld = _folder(path)
        if fld not in groups:
            groups[fld] = {"done": [], "pending": []}

        status = (row["status"] or "").lower()
        if status == _DONE_STATUS:
            groups[fld]["done"].append(row)
        elif status in _PENDING_STATUSES:
            groups[fld]["pending"].append(row)
        # low_savings / no_saving / skipped — ignored

    results = []
    for fld, data in groups.items():
        done_rows = data["done"]
        pending_rows = data["pending"]

        if len(done_rows) < min_done or len(pending_rows) < min_pending:
            continue

        # --- Done metrics ---
        done_with_pct = [r for r in done_rows if r["saved_pct"] is not None]
        if not done_with_pct:
            continue  # can't compute avg_savings_pct

        avg_savings_pct = sum(r["saved_pct"] for r in done_with_pct) / len(done_with_pct)
        total_saved_mb = sum(r["saved_mb"] or 0.0 for r in done_rows)

        # Encode speed (×realtime)
        speed_vals = []
        for r in done_rows:
            dur = r["source_duration_secs"]
            sa = r["started_at"]
            ca = r["completed_at"]
            if dur and sa and ca:
                try:
                    wall = (
                        datetime.fromisoformat(ca) - datetime.fromisoformat(sa)
                    ).total_seconds()
                    if wall > 0:
                        speed_vals.append(dur / wall)
                except (ValueError, TypeError):
                    pass
        avg_speed = sum(speed_vals) / len(speed_vals) if speed_vals else None

        # --- Pending metrics ---
        pending_source_mb = sum(r["source_size_mb"] or 0.0 for r in pending_rows)

        # Est. additional MB — use per-file est_saving_pct where available
        est_add = 0.0
        for r in pending_rows:
            size_mb = r["source_size_mb"] or 0.0
            est_pct = r["est_saving_pct"]
            if est_pct is not None:
                est_add += (est_pct / 100.0) * size_mb
            else:
                est_add += (avg_savings_pct / 100.0) * size_mb

        # Priority score
        speed_factor = 1.0
        if avg_speed is not None:
            speed_factor = max(0.5, min(3.0, avg_speed / 2.0))
        priority_score = est_add * speed_factor

        results.append({
            "folder": fld,
            "done_count": len(done_rows),
            "pending_count": len(pending_rows),
            "avg_savings_pct": avg_savings_pct,
            "avg_speed": avg_speed,
            "total_saved_mb": total_saved_mb,
            "pending_source_mb": pending_source_mb,
            "est_additional_mb": est_add,
            "priority_score": priority_score,
        })

    return results


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _sort_key(item, sort_by: str):
    if sort_by == "savings":
        return item["avg_savings_pct"]
    if sort_by == "speed":
        return item["avg_speed"] or 0.0
    if sort_by == "pending":
        return item["pending_count"]
    return item["priority_score"]


def _render(results, root: str, root_norm: str, top: int, sort_by: str, total_analysed: int):
    console = Console()

    sorted_results = sorted(results, key=lambda x: _sort_key(x, sort_by), reverse=True)
    shown = sorted_results[:top]

    total_pending = sum(r["pending_count"] for r in results)
    total_est = sum(r["est_additional_mb"] for r in results)
    opportunity_count = len(results)

    table = Table(
        title=(
            f"Folder Analysis — {root}  "
            f"({total_analysed} folders analysed, {opportunity_count} with remaining opportunity)"
        ),
        box=box.SIMPLE_HEAD,
        pad_edge=True,
        show_footer=True,
    )

    table.add_column("Folder", no_wrap=True, max_width=66, footer="[dim]Total[/dim]")
    table.add_column("Done",    justify="right", style="dim", min_width=4)
    table.add_column("Pending", justify="right", min_width=7, footer=str(total_pending))
    table.add_column("Avg Save%", justify="right", min_width=9)
    table.add_column("Speed",   justify="right", min_width=5)
    table.add_column("Saved So Far",  justify="right", min_width=8)
    table.add_column("Est. Additional", justify="right", min_width=10,
                     footer=f"[bold]{_mb_str(total_est)}[/bold]")
    table.add_column("Score",   justify="right", min_width=5)

    prefix = root_norm.rstrip("/") + "/"

    for rank, r in enumerate(shown):
        # Full path, restore original casing as best we can (folder key is lowercased)
        rel = r["folder"]
        if len(rel) > 65:
            rel = "…" + rel[-64:]

        pending_text = (
            f"[green]{r['pending_count']}[/green]"
            if r["pending_count"] > 50
            else str(r["pending_count"])
        )

        pct = r["avg_savings_pct"]
        pct_text = Text(f"{pct:.0f}%", style=_savings_style(pct))

        speed = r["avg_speed"]
        speed_str = f"{speed:.1f}×" if speed is not None else "—"

        est_str = _mb_str(r["est_additional_mb"])
        if rank < 5:
            est_str = f"[bold]{est_str}[/bold]"

        table.add_row(
            rel,
            str(r["done_count"]),
            pending_text,
            pct_text,
            speed_str,
            _mb_str(r["total_saved_mb"]),
            est_str,
            f"{r['priority_score']:.1f}",
        )

    console.print(table)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Analyse conversion opportunity by folder.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "root_folder",
        help="Top-level path to filter by (e.g. F:\\Videos)",
    )
    parser.add_argument(
        "--min-done", type=int, default=1, metavar="N",
        help="Min completed conversions required per folder (default: 1)",
    )
    parser.add_argument(
        "--min-pending", type=int, default=1, metavar="N",
        help="Min pending files required per folder (default: 1)",
    )
    parser.add_argument(
        "--top", type=int, default=25, metavar="N",
        help="Show top N folders (default: 25)",
    )
    parser.add_argument(
        "--sort", choices=["score", "savings", "speed", "pending"],
        default="score",
        help="Sort column (default: score)",
    )
    args = parser.parse_args()

    root = args.root_folder.rstrip("\\/")
    root_norm = _norm(root)

    rows = _load_rows()

    # Count all distinct folders under root (any status)
    all_folders = {
        _folder(row["source_path"])
        for row in rows
        if row["source_path"] and _norm(row["source_path"]).startswith(root_norm + "/")
    }
    total_analysed = len(all_folders)

    results = _analyse(rows, root_norm, args.min_done, args.min_pending)

    if not results:
        print(f"No folders matched the criteria under: {root}")
        return

    _render(results, args.root_folder, root_norm, args.top, args.sort, total_analysed)


if __name__ == "__main__":
    main()
