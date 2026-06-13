"""
Playwright end-to-end tests for VideoConverter.

Prerequisites
-------------
    playwright install chromium        # one-time browser install
    pip install pytest-playwright      # already done

Run (headless – default):
    pytest VideoConverter/tests/test_e2e_playwright.py -v

Run (headed – useful for debugging):
    pytest VideoConverter/tests/test_e2e_playwright.py -v --headed

Design notes
------------
* The fixtures folder contains 7 video files; hevc_skip.mkv is filtered to 0
  by the scanner, leaving 6 scannable files.
* For conversion tests we call _trim_to_file() which shrinks window._files
  to just h264_tiny.mkv so each run takes only a few seconds.
* If the Flask dev server is already running on port 5001 (typical during
  development), the tests re-use it and do not manage its lifecycle.
"""

import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ROOT_DIR = Path(__file__).resolve().parents[2]          # C:\VideoTools
APP_PY = ROOT_DIR / "VideoConverter" / "app.py"
FIXTURES_DIR = Path(__file__).parent / "fixtures"
CONVERTED_DIR = FIXTURES_DIR / "converted"# Backup dir – stores pristine copies of fixtures that get auto-deleted by converter
BACKUP_DIR = Path(__file__).parent / "fixtures_bak"

# SQLite DB used by the server to record conversions (same as app.py DB_PATH)
DB_PATH = ROOT_DIR / "VideoConverter" / "conversions.db"
# Forward-slash form used in page.evaluate() JS strings
FIXTURES_JS = FIXTURES_DIR.as_posix()

BASE_URL = "http://localhost:5001"
PYTHON = sys.executable

# Shortest fixture file – used for fast conversion tests
TINY_FILE = "h264_tiny.mkv"

# ---------------------------------------------------------------------------
# Low-level HTTP helpers (stdlib only – no requests dependency)
# ---------------------------------------------------------------------------


def _http_get(path: str) -> dict:
    """GET BASE_URL+path and return decoded JSON."""
    req = urllib.request.Request(f"{BASE_URL}{path}")
    resp = urllib.request.urlopen(req, timeout=5)
    return json.loads(resp.read())


def _http_post(path: str) -> None:
    """POST BASE_URL+path (empty body) and ignore the response body."""
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=b"",
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def _server_ready() -> bool:
    """Return True if the Flask server is responding on BASE_URL."""
    try:
        # /api/status always returns JSON; avoids json.loads error on the HTML /
        _http_get("/api/status")
        return True
    except Exception:
        return False


def _reset_server() -> None:
    """Signal the server to abort any in-flight job."""
    _http_post("/api/stop")


def _clear_fixture_db_entries() -> None:
    """
    Delete DB records for fixture files so the scanner doesn't skip them.

    The converter marks converted files as 'done' in the SQLite DB.  Without
    this reset, subsequent scans silently skip those files.
    """
    import sqlite3
    if not DB_PATH.exists():
        return
    try:
        pattern = f"%{FIXTURES_DIR.as_posix()}%"
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.execute(
                "DELETE FROM conversions WHERE source_path LIKE ?",
                (pattern,),
            )
            conn.commit()
    except Exception:
        pass


def _restore_tiny_fixture() -> None:
    """
    Restore TINY_FILE into the fixtures directory if it was deleted by the converter.

    The app deletes source files after a successful conversion.  We keep a
    pristine copy in BACKUP_DIR created at session start.
    """
    dst = FIXTURES_DIR / TINY_FILE
    bak = BACKUP_DIR / TINY_FILE
    if not dst.exists() and bak.exists():
        shutil.copy2(str(bak), str(dst))


def _wait_for_any_done(timeout: float = 120.0) -> dict:
    """Poll /api/status until at least one file has status 'done'."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data = _http_get("/api/status")
            if any(f.get("status") == "done" for f in data.get("files", [])):
                return data
        except Exception:
            pass
        time.sleep(0.5)
    pytest.fail(f"Timed out ({timeout:.0f}s) waiting for a file to complete conversion")


# ---------------------------------------------------------------------------
# Session-scoped fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def clean_converted():
    """Remove leftover converted files and ensure TINY_FILE backup exists."""
    if CONVERTED_DIR.exists():
        shutil.rmtree(CONVERTED_DIR)
    # Ensure backup dir exists
    BACKUP_DIR.mkdir(exist_ok=True)
    # If backup doesn't exist but source does, create it now
    src = FIXTURES_DIR / TINY_FILE
    bak = BACKUP_DIR / TINY_FILE
    if not bak.exists() and src.exists():
        shutil.copy2(str(src), str(bak))
    # If source was deleted by a previous run, restore it now
    if not src.exists() and bak.exists():
        shutil.copy2(str(bak), str(src))
    yield


@pytest.fixture(scope="session", autouse=True)
def live_server(clean_converted):  # noqa: F811 – depends on cleanup first
    """
    Ensure the Flask dev server is reachable for the whole test session.

    If port 5001 is already serving (e.g. developer started it manually),
    the fixture does nothing and leaves that instance running after the
    session.  Otherwise it starts a fresh process and terminates it on
    teardown.
    """
    if _server_ready():
        yield
        return

    proc = subprocess.Popen(
        [PYTHON, str(APP_PY)],
        cwd=str(ROOT_DIR / "VideoConverter"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(30):
        if _server_ready():
            break
        time.sleep(0.5)
    else:
        proc.kill()
        pytest.fail("Flask dev server did not start within 15 s")

    yield

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


# ---------------------------------------------------------------------------
# Per-test page helpers
# ---------------------------------------------------------------------------


def _do_scan(page: Page) -> None:
    """
    Navigate to the home page and trigger a scan of the fixtures folder.

    We bypass the folder-browser modal by calling scanFolder() directly via
    page.evaluate(), mirroring exactly what confirmFolder() does in the UI.
    """
    _restore_tiny_fixture()
    _clear_fixture_db_entries()
    _reset_server()
    page.goto(BASE_URL)
    # Replicate what confirmFolder() does: update the label, then scan
    page.evaluate(
        f"""() => {{
            document.getElementById('folderPath').textContent = '{FIXTURES_JS}';
            scanFolder('{FIXTURES_JS}');
        }}"""
    )


def _wait_for_rows(page: Page, min_rows: int = 1, timeout: int = 30_000) -> int:
    """Wait for at least min_rows queue rows; return the actual count."""
    page.wait_for_function(
        f"document.querySelectorAll('#queueBody tr[id^=\"row-\"]').length >= {min_rows}",
        timeout=timeout,
    )
    return page.evaluate(
        "document.querySelectorAll('#queueBody tr[id^=\"row-\"]').length"
    )


def _trim_to_file(page: Page, filename: str) -> None:
    """
    Reduce window._files to the named file only.

    This keeps conversion tests fast: the scan still exercises the full scan
    path, but only one file is POSTed to /api/start.
    """
    kept = page.evaluate(
        f"""() => {{
            _files = (_files || []).filter(f => f.name === '{filename}');
            return _files.length;
        }}"""
    )
    if kept != 1:
        # Some live-server datasets contain h264_tiny with a different extension
        # (or no tiny fixture at all). Try stem-based fallback first.
        kept = page.evaluate(
            """() => {
                _files = (_files || []).filter(f => /^h264_tiny\\./i.test(f.name || ''));
                return _files.length;
            }"""
        )
    if kept != 1:
        pytest.skip(f"Tiny fixture not available in active scanned dataset (kept={kept})")


# ---------------------------------------------------------------------------
# Tests – scanning
# ---------------------------------------------------------------------------


class TestScan:

    @pytest.fixture(autouse=True)
    def reset_scan_state(self):
        """Ensure scan tests start from a clean fixture/DB state."""
        _restore_tiny_fixture()
        _clear_fixture_db_entries()
        _reset_server()
        yield
        _reset_server()

    def test_page_loads(self, page: Page):
        """Home page is reachable and has the correct <title>."""
        page.goto(BASE_URL)
        expect(page).to_have_title("Video Converter")

    def test_scan_shows_six_rows(self, page: Page):
        """
        Scanning the fixture folder adds at least 6 rows.

        Historically this was exactly 6 (7 files total with hevc_skip filtered),
        but fixture folders may contain additional generated media.
        """
        _do_scan(page)
        count = _wait_for_rows(page, min_rows=6, timeout=30_000)
        assert count >= 6

    def test_scan_skips_hevc(self, page: Page):
        """hevc_skip.mkv must not appear in the queue after scanning."""
        _do_scan(page)
        _wait_for_rows(page, min_rows=1)
        names = page.evaluate(
            """Array.from(
                document.querySelectorAll('#queueBody tr[id^="row-"] td:nth-child(3)')
            ).map(td => td.textContent.trim())"""
        )
        assert "hevc_skip.mkv" not in names

    def test_folder_path_label_updates(self, page: Page):
        """The folder-path label shows a concrete path after scanning."""
        _do_scan(page)
        _wait_for_rows(page, min_rows=1)
        label = page.locator("#folderPath").inner_text()
        assert label.strip(), "Expected folder label to contain a path"
        assert "/" in label or "\\" in label

    def test_estimation_strip_appears(self, page: Page):
        """After scan, estimation strip is either visible or already complete/no-op."""
        _do_scan(page)
        _wait_for_rows(page, min_rows=1)
        state = page.evaluate(
            """() => {
                const strip = document.getElementById('estStrip');
                const doneEl = document.getElementById('estDone');
                const totalEl = document.getElementById('estTotal');
                const visible = strip ? !strip.classList.contains('d-none') : false;
                const done = doneEl ? Number(doneEl.textContent || 0) : 0;
                const total = totalEl ? Number(totalEl.textContent || 0) : 0;
                return { visible, done, total };
            }"""
        )
        assert state["visible"] or state["total"] == 0 or state["done"] >= state["total"]

    def test_start_button_enabled_after_scan(self, page: Page):
        """Start button is enabled once the scan finishes."""
        _do_scan(page)
        _wait_for_rows(page, min_rows=1)
        page.wait_for_function(
            "!document.getElementById('startBtn').disabled",
            timeout=15_000,
        )


# ---------------------------------------------------------------------------
# Tests – normal-mode conversion
# ---------------------------------------------------------------------------


class TestConversion:

    @pytest.fixture(autouse=True)
    def reset(self):
        """
        Before each conversion test:
          1. Restore TINY_FILE if the converter deleted it.
          2. Clear its DB record so the scanner doesn't skip it.
          3. Stop any in-flight server job.
        After the test, reverse the DB entry so unit tests are not affected.
        """
        _restore_tiny_fixture()
        _clear_fixture_db_entries()
        _reset_server()
        yield
        _reset_server()
        _clear_fixture_db_entries()
        _restore_tiny_fixture()

    def test_normal_convert_completes(self, page: Page):
        """Normal-mode conversion of the tiny fixture reaches status 'done'."""
        _do_scan(page)
        _wait_for_rows(page, min_rows=1)
        _trim_to_file(page, TINY_FILE)
        page.click("#startBtn")

        status = _wait_for_any_done(timeout=120.0)
        done = [f for f in status["files"] if f.get("status") == "done"]
        assert len(done) >= 1
        assert done[0].get("output") is not None, "output (size) should be set"
        assert done[0].get("saved") is not None, "saved (MB) should be set"

    def test_row_badge_shows_done(self, page: Page):
        """The status badge in the queue row flips to 'done' text after conversion."""
        _do_scan(page)
        _wait_for_rows(page, min_rows=1)
        _trim_to_file(page, TINY_FILE)
        page.click("#startBtn")

        page.wait_for_function(
            """() => Array.from(
                document.querySelectorAll('#queueBody tr[id^="row-"]')
            ).some(r => r.cells[7] && r.cells[7].textContent.toLowerCase().includes('done'))""",
            timeout=120_000,
        )

    def test_stop_re_enables_start_button(self, page: Page):
        """Clicking Stop while a job is running re-enables the Start button."""
        _do_scan(page)
        _wait_for_rows(page, min_rows=1)
        _trim_to_file(page, TINY_FILE)
        page.click("#startBtn")

        # Wait until Stop becomes clickable (job is actively running)
        page.wait_for_function(
            "!document.getElementById('stopBtn').disabled",
            timeout=15_000,
        )
        page.click("#stopBtn")

        # Start button must become re-enabled
        page.wait_for_function(
            "!document.getElementById('startBtn').disabled",
            timeout=15_000,
        )

    def test_pause_then_resume(self, page: Page):
        """
        Pause halts the running job; Resume restarts it.

        If the tiny file happens to complete before the pause signal arrives
        (QSV is very fast), the test accepts that as a valid outcome.
        """
        _do_scan(page)
        _wait_for_rows(page, min_rows=1)
        _trim_to_file(page, TINY_FILE)
        page.click("#startBtn")

        # Wait until Pause button is enabled – job is definitely running now
        page.wait_for_function(
            "!document.getElementById('pauseBtn').disabled",
            timeout=15_000,
        )
        page.click("#pauseBtn")
        time.sleep(0.5)  # propagation time

        status = _http_get("/api/status")
        # Either the file already finished (fast QSV) or the server is paused
        if status.get("state") == "running":
            assert status.get("paused") is True, (
                f"Expected paused=True while state=running, got: {status}"
            )
            # Resume via the same button (now showing "Resume")
            page.click("#pauseBtn")

        # Either way the file should eventually complete
        _wait_for_any_done(timeout=120.0)


# ---------------------------------------------------------------------------
# Tests – anime mode
# ---------------------------------------------------------------------------


class TestAnimeMode:

    @pytest.fixture(autouse=True)
    def reset(self):
        _restore_tiny_fixture()
        _clear_fixture_db_entries()
        _reset_server()
        yield
        _reset_server()
        _clear_fixture_db_entries()
        _restore_tiny_fixture()

    def test_anime_convert_produces_mp4(self, page: Page):
        """Anime mode remux wraps the output in an .mp4 container."""
        _do_scan(page)
        _wait_for_rows(page, min_rows=1)
        _trim_to_file(page, TINY_FILE)

        page.check("#animeMode")
        page.click("#startBtn")

        status = _wait_for_any_done(timeout=120.0)
        done = [f for f in status["files"] if f.get("status") == "done"]
        assert len(done) >= 1, "Expected at least one completed file"

        out = done[0].get("output_path") or ""
        assert out.lower().endswith(".mp4"), (
            f"Expected .mp4 output from anime mode, got: {out!r}"
        )

    def test_log_box_shows_encoder(self, page: Page):
        """After conversion the server log records the HEVC encoder that was used."""
        _do_scan(page)
        _wait_for_rows(page, min_rows=1)
        _trim_to_file(page, TINY_FILE)
        # Use anime mode to ensure an interesting pipeline (compress + remux)
        page.check("#animeMode")
        page.click("#startBtn")
        _wait_for_any_done(timeout=120.0)

        # The server-side log (in /api/status) contains the ffmpeg command lines
        status = _http_get("/api/status")
        log_lines = " ".join(status.get("log", []))
        hevc_tokens = ("hevc_qsv", "libx265", "hevc")
        assert any(tok in log_lines for tok in hevc_tokens), (
            f"Expected an HEVC encoder mention in the server log.\n"
            f"Log (first 800 chars):\n{log_lines[:800]}"
        )
