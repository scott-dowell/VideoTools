"""
Full end-to-end test against a real folder via the Flask API.

Usage:
    python VideoConverter/tests/run_e2e_real_folder.py [FOLDER_PATH] [--anime] [--dry-run]

Defaults:
    FOLDER_PATH  = C:\\Users\\scott\\Downloads\\Anime\\Rosario to Vampire\\Rosario to Vampire
    anime mode   = True
    dry-run      = False (set --dry-run to scan only, no conversion)

Run from C:\\VideoTools:
    .venv\\Scripts\\python.exe VideoConverter\\tests\\run_e2e_real_folder.py
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = "http://localhost:5001"

# ─────────────────────────────────────────────────────────────────────────────
# Colours (basic ANSI — suppressed if not a terminal)
# ─────────────────────────────────────────────────────────────────────────────
_TTY = sys.stdout.isatty()

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text

def ok(t):   return _c("32", t)
def warn(t): return _c("33", t)
def err(t):  return _c("31", t)
def bold(t): return _c("1",  t)
def dim(t):  return _c("2",  t)

# ─────────────────────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get(path: str) -> dict:
    req  = urllib.request.Request(f"{BASE_URL}{path}")
    resp = urllib.request.urlopen(req, timeout=10)
    return json.loads(resp.read())


def _post(path: str, body: dict | None = None) -> dict:
    data = json.dumps(body or {}).encode()
    req  = urllib.request.Request(
        f"{BASE_URL}{path}", data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    resp = urllib.request.urlopen(req, timeout=10)
    return json.loads(resp.read())


def _server_ok() -> bool:
    try:
        _get("/api/status")
        return True
    except Exception:
        return False


def _scan_sse(folder: str) -> tuple[list[dict], dict | None]:
    """Consume /api/scan SSE and return (all_files, done_event)."""
    import urllib.parse
    encoded = urllib.parse.urlencode({"path": folder})
    url  = f"{BASE_URL}/api/scan?{encoded}"
    req  = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
    all_files: list[dict] = []
    done_event = None
    folder_count = 0

    with urllib.request.urlopen(req, timeout=60) as resp:
        for raw in resp:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                ev = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue

            if ev.get("type") == "folder":
                folder_count += 1
                files = ev.get("files", [])
                all_files.extend(files)
                rel = ev.get("folder") or ev.get("path", "")
                print(f"  {dim('folder')} {rel or '.'} → {len(files)} file(s)")

            elif ev.get("type") == "warning":
                print(warn(f"  WARN  {ev.get('message', '')}  [{ev.get('path', '')}]"))

            elif ev.get("type") == "error":
                print(err(f"  ERR   {ev.get('message', '')}"))

            elif ev.get("type") == "done":
                done_event = ev

    return all_files, done_event


# ─────────────────────────────────────────────────────────────────────────────
# Progress polling
# ─────────────────────────────────────────────────────────────────────────────

STATUS_ICON = {
    "pending":    dim("·"),
    "converting": warn("▶"),
    "done":       ok("✓"),
    "failed":     err("✗"),
}

def _poll_until_done(num_files: int, timeout: float = 7200.0) -> dict:
    """Poll /api/status printing live progress; return final status dict."""
    deadline = time.monotonic() + timeout
    last_idx  = -1
    last_pct  = -1.0
    start_ts  = time.monotonic()

    print()
    print(bold("─── Conversion progress ───────────────────────────────────────"))

    while time.monotonic() < deadline:
        try:
            s = _get("/api/status")
        except Exception:
            time.sleep(1)
            continue

        state   = s.get("state", "idle")
        cur_idx = s.get("current_index", 0)
        pct     = s.get("progress_pct", 0.0)
        fps     = s.get("fps", 0.0)
        eta     = s.get("eta_secs", 0)

        # Print a new line whenever the active file changes
        if cur_idx != last_idx or int(pct) != int(last_pct):
            elapsed = time.monotonic() - start_ts
            cur_name = ""
            files = s.get("files", [])
            if 0 <= cur_idx < len(files):
                cur_name = files[cur_idx].get("name", "")

            line = (
                f"\r  [{cur_idx+1:2d}/{num_files}] "
                f"{cur_name:<48s} "
                f"{pct:5.1f}%  "
                f"fps={fps:5.1f}  "
                f"eta={_fmt_secs(eta):<8s} "
                f"elapsed={_fmt_secs(int(elapsed))}"
            )
            print(line, end="", flush=True)
            last_idx = cur_idx
            last_pct = pct

        if state in ("done", "stopped"):
            print()   # finish the progress line
            return s

        time.sleep(0.4)

    print()
    print(err("TIMEOUT waiting for conversion to complete"))
    return _get("/api/status")


def _fmt_secs(s: int) -> str:
    if s < 60:
        return f"{s}s"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m}m{sec:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


# ─────────────────────────────────────────────────────────────────────────────
# Report
# ─────────────────────────────────────────────────────────────────────────────

def _print_report(status: dict) -> None:
    files = status.get("files", [])
    done  = [f for f in files if f.get("status") == "done"]
    fails = [f for f in files if f.get("status") == "failed"]

    total_saved = sum(float(f.get("saved", 0) or 0) for f in done)

    print()
    print(bold("─── Results ────────────────────────────────────────────────────"))
    print(f"  State          : {bold(status.get('state', '?'))}")
    print(f"  Total files    : {len(files)}")
    print(f"  Completed      : {ok(str(len(done)))}")
    print(f"  Failed         : {(err if fails else ok)(str(len(fails)))}")
    total_saved_gb = total_saved / 1024
    print(f"  Space saved    : {ok(f'{total_saved:.1f} MB')}  ({total_saved_gb:.2f} GB)")
    print()

    # Per-file table
    col = [60, 10, 10, 8, 8]
    hdr = f"  {'File':<{col[0]}}  {'In (MB)':>{col[1]}}  {'Out (MB)':>{col[2]}}  {'Saved':>{col[3]}}  {'Status':>{col[4]}}"
    print(dim(hdr))
    print(dim("  " + "─" * (sum(col) + 8)))

    for f in files:
        name   = f.get("name", "?")[:col[0]]
        size   = f.get("size", "?")
        output = f.get("output", "—") or "—"
        pct    = f.get("pct", "—") or "—"
        stat   = f.get("status", "?")
        icon   = STATUS_ICON.get(stat, "?")

        row = f"  {name:<{col[0]}}  {size:>{col[1]}}  {output:>{col[2]}}  {pct+'%':>{col[3]}}  {stat:>{col[4]}}"
        if stat == "done":
            print(ok(row))
        elif stat == "failed":
            print(err(row))
            tail = f.get("error_tail", "")
            if tail:
                for line in tail.splitlines()[-5:]:
                    print(err(f"      {line}"))
        else:
            print(row)

    print()


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="VideoConverter full E2E test")
    parser.add_argument(
        "folder",
        nargs="?",
        default=r"C:\Users\scott\Downloads\Anime\Rosario to Vampire\Rosario to Vampire",
        help="Root folder to scan and convert",
    )
    parser.add_argument(
        "--no-anime", dest="anime", action="store_false", default=True,
        help="Disable anime mode (default: anime mode ON)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Scan only — do not start conversion",
    )
    args = parser.parse_args()

    folder = str(Path(args.folder).resolve())
    print()
    print(bold("═══ VideoConverter — Full E2E Test ════════════════════════════"))
    print(f"  Folder     : {folder}")
    print(f"  Anime mode : {ok('ON') if args.anime else warn('OFF')}")
    print(f"  Dry run    : {warn('YES — no conversion') if args.dry_run else ok('NO')}")
    print()

    # ── 1. Server check ────────────────────────────────────────────────────
    print(bold("─── 1 / 4  Server check ────────────────────────────────────────"))
    if not _server_ok():
        print(err("  Flask server is not reachable at " + BASE_URL))
        print(err("  Start it with: cd VideoConverter && python app.py"))
        return 1
    srv = _get("/api/status")
    if srv.get("state") == "running":
        print(warn("  Server already has a running job — aborting to avoid conflict"))
        return 1
    print(ok(f"  Server is up  (state={srv.get('state', '?')})"))

    # ── 2. Scan ────────────────────────────────────────────────────────────
    print()
    print(bold("─── 2 / 4  Scanning folder ─────────────────────────────────────"))
    print(f"  {folder}")
    t0 = time.monotonic()
    try:
        files, done_ev = _scan_sse(folder)
    except Exception as exc:
        print(err(f"  Scan failed: {exc}"))
        return 1
    scan_time = time.monotonic() - t0

    if not files:
        print(warn("  No eligible files found (all may already be HEVC or in DB)."))
        return 0

    total_mb = done_ev.get("total_mb", 0) if done_ev else 0
    print()
    print(f"  {ok(str(len(files)))} file(s) found in {scan_time:.1f}s   "
          f"({total_mb:.0f} MB  /  {total_mb/1024:.2f} GB)")
    print()

    # Codec breakdown
    from collections import Counter
    codecs = Counter(f.get("codec", "?") for f in files)
    print(f"  Codecs: " + "  ".join(f"{k} ×{v}" for k, v in codecs.items()))
    print()

    # File list
    print(dim(f"  {'#':>3}  {'Name':<52}  {'Size (MB)':>10}  {'Codec':<8}  Duration"))
    print(dim("  " + "─" * 92))
    for i, f in enumerate(files, 1):
        print(f"  {i:3d}  {f['name']:<52}  {f['size']:>10}  {f.get('codec','?'):<8}  {f.get('duration','?')}")

    if args.dry_run:
        print()
        print(warn("  --dry-run: stopping here (no conversion started)"))
        return 0

    # ── 3. Start conversion ────────────────────────────────────────────────
    print()
    print(bold("─── 3 / 4  Starting conversion ─────────────────────────────────"))
    try:
        resp = _post("/api/settings", {"anime_mode": args.anime})
        print(f"  Settings updated — anime_mode={'true' if args.anime else 'false'}  →  {resp}")

        resp = _post("/api/start", {"files": files, "anime_mode": args.anime})
        print(f"  /api/start → {resp}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        print(err(f"  Start failed ({exc.code}): {body}"))
        return 1

    # ── 4. Monitor and report ──────────────────────────────────────────────
    print()
    print(bold("─── 4 / 4  Monitoring ──────────────────────────────────────────"))
    final = _poll_until_done(len(files))

    _print_report(final)

    failures = sum(1 for f in final.get("files", []) if f.get("status") == "failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
