# Backend Wiring — Implementation Plan

> Status: **AWAITING SIGN-OFF**  
> Created: 2026-04-06  
> Updated: 2026-04-10 — Q1–Q6 answered; SQLite-first approach adopted

---

## Decisions (Q&A resolved)

### Q1 — Output directory → `converted/` subfolder alongside source

Output lands in a `converted/` subfolder at the same level as the source file:
```
D:/Anime/Folder/episode.mkv          ← source (deleted after success)
D:/Anime/Folder/converted/episode.mp4  ← output
```

**Re-scan / reprocessing protection:** Handled by SQLite DB (see Phase 0c). The scanner queries `conversions` before yielding a file — if `status='done'` and `source_mtime` matches, the file is silently skipped. This replaces all folder-based heuristics (`converted/` subfolder scan, HEVC codec skip). The `converted/` subfolder is still used for on-disk organisation, but it is **not** the source of truth.

**Re-processing detection:** `source_mtime` stores `os.path.getmtime(source_path)` as a float. If a file is replaced at the same path (different mtime), its DB record gets a new `status='pending'` row and it is re-queued automatically.

### Q2 — Source deletion → Yes, delete after integrity check passes

`os.remove(source)` is called only after `_verify_output()` returns `(True, "")`.

### Q3 — Scan depth → Recursive, all subdirectories

`os.walk()` with no depth limit. The SSE streaming approach means the user sees folders populate incrementally — depth is not a UX problem.

### Q4 — Codec filter → All video files

Any file with a video stream that is **not already HEVC or AV1** is a candidate. Covers H264, MPEG-2, VC-1, VP9, MPEG-4, etc. The scanner yields the codec name so the UI can display it and the converter can apply the right path.

### Q5 — Anime mode → In scope now

The existing `convert_videos.py` has battle-tested solutions for:
- **Hi10 H.264** detection → remux path instead of re-encode (QSV can't decode 10-bit)
- **Bitmap subtitles (PGS/VOBSUB)** → OCR via EasyOCR → embed as SRT in MP4
- **AAC encoder crash** (`aac_mf` on Windows instead of native `aac` — E-core FP overflow fix)
- **MP4 container restrictions** — AAC transcode, mov_text subs, hvc1 tag only for HEVC
- **DTS overflow recovery** — flood detection, per-stream retry, last-resort no-sub fallback
- **Power throttling** — `powercfg /powerthrottling disable /path ffmpeg.exe` at startup
- **Local temp staging** — all FFmpeg work to `C:\Temp\vc_working`, single copy to destination

The anime-specific modules to port/adapt from `C:\Users\scott\OneDrive\Documents\Python\VideoConversion\`:
- `convert_bitmap_subs.py` → copy verbatim as `VideoConverter/bitmap_subs.py` (strip only CLI arg parsing)
- `candidate_filtering.py` → copy verbatim as `VideoConverter/candidate_filtering.py`

**Normal mode** remains: QSV → sw fallback, copy audio/subs, no container change, no AAC transcode.  
**Anime mode** adds: remux to MP4, AAC transcode, OCR bitmap subs, Hi10 detection, all the edge cases above.

The UI toggle (Anime mode switch in the navbar) already exists — this just wires it to the right code path.

### Q6 — Pause → True process suspension via `NtSuspendProcess`

**How FFmpeg "pause" works on Windows:** The Windows NT kernel exposes `NtSuspendProcess` and `NtResumeProcess` in `ntdll.dll`. These freeze/unfreeze **all threads** of a process instantaneously — the process has no idea it was suspended, and resumes exactly mid-instruction. This is what Task Manager uses when you right-click → Suspend.

We've confirmed on this machine that `ctypes.windll.ntdll.NtSuspendProcess` is callable.

```python
import ctypes

PROCESS_ALL_ACCESS = 0x1F0FFF

def _suspend_ffmpeg(pid: int):
    h = ctypes.windll.kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
    ctypes.windll.ntdll.NtSuspendProcess(h)
    ctypes.windll.kernel32.CloseHandle(h)

def _resume_ffmpeg(pid: int):
    h = ctypes.windll.kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
    ctypes.windll.ntdll.NtResumeProcess(h)
    ctypes.windll.kernel32.CloseHandle(h)
```

**Why this is superior to alternatives:**
- Instantaneous — no waiting for a file boundary
- GPU encoder context is preserved (resumes mid-frame, no wasted work)
- Temp file integrity maintained
- No FFmpeg involvement — works on any subprocess

**One caveat:** While suspended, the stdout readline loop in Python will block waiting for the next line. This is fine — the thread just sleeps. When FFmpeg resumes it unblocks immediately.

---

## Architecture Summary (after wiring)

```
Browser                         Flask (port 5001)              converter.py / bitmap_subs.py
-------                         -----------------              -----------------------------
EventSource /api/scan  ──SSE──▶  scanner.walk()  ──ffprobe──▶  file metadata + stream info
fetch /api/estimate    ─────────▶  estimate()     ──ffmpeg──▶   10s QSV sample
fetch /api/start       ─────────▶  _queue thread  ──ffmpeg──▶   full encode (normal or anime)
fetch /api/status  ◀─ poll 1s──   _status dict    ←─ progress_cb updates
fetch /api/stop        ─────────▶  stop_event.set() → proc.kill()
fetch /api/pause       ─────────▶  NtSuspendProcess(ffmpeg_pid)
fetch /api/resume      ─────────▶  NtResumeProcess(ffmpeg_pid)
```

**New / modified modules:**
- `db.py` — SQLite init, schema migration, CRUD helpers (`upsert_conversion`, `get_status`, `mark_done`, `mark_failed`)
- `scanner.py` — filesystem walk + `ffprobe` per file, DB-aware skip, yields SSE events per folder
- `bitmap_subs.py` — ported from `convert_bitmap_subs.py` (OCR engine, PGS parser)
- `candidate_filtering.py` — ported verbatim (size/duration filter helpers)
- `converter.py` — extended: anime path, NtSuspend/Resume, progress_cb, integrity check
- `app.py` — new routes: `/api/scan`, `/api/start`, `/api/stop`, `/api/pause`, `/api/resume`; DB init on startup

---

## Phase 0 — Test Fixtures + Helper Modules + Database

**Goal:** Real dummy video files, ported helper modules, and SQLite DB wired up — so every subsequent phase can test against real FFmpeg output with full persistence.

### 0a — Fixture videos

`C:\VideoTools\VideoConverter\tests\fixtures\` created by `tests/make_fixtures.ps1`

| File | Codec | Duration | Audio | Subs | Purpose |
|------|-------|----------|-------|------|---------|
| `h264_short.mkv` | H264 8-bit | 30 s | AAC stereo | ASS (eng) | Normal convert path |
| `h264_long.mkv` | H264 8-bit | 5 min | AAC stereo | none | Progress bar / ETA test |
| `hevc_skip.mkv` | HEVC | 30 s | AAC stereo | none | Must be skipped by scanner |
| `h264_tiny.mkv` | H264 8-bit | 8 s | none | none | estimate() "too short" edge case |
| `h264_multitrack.mkv` | H264 8-bit | 30 s | AC3 5.1 (jpn) + AAC stereo (eng) | PGS bitmap (eng) + ASS (eng) | Full anime pipeline: bitmap sub OCR, multi audio |
| `h264_hi10.mkv` | H264 10-bit | 30 s | FLAC (jpn) | ASS (eng) | Hi10 → remux path (no QSV decode) |
| `h264_mp4_aac.mp4` | H264 8-bit | 30 s | AAC stereo | none | MP4 fast-path (no remux needed) |

All generated from synthetic colourbar + sine tone — no copyright content.

### 0b — Port helper modules

| Source | Destination | Changes |
|--------|-------------|---------|
| `VideoConversion/candidate_filtering.py` | `VideoConverter/candidate_filtering.py` | None — copy verbatim |
| `VideoConversion/convert_bitmap_subs.py` | `VideoConverter/bitmap_subs.py` | Strip CLI `argparse` block; keep `ocr_bitmap_subs_to_srt()` and all PGS/VOBSUB parsing |

**Tests (run after this phase):**
```
pytest tests/test_fixtures.py tests/test_candidate_filtering.py
```
| Test | Assert |
|------|--------|
| `test_fixtures_exist` | all 7 fixture files present with size > 0 |
| `test_fixture_codecs` | ffprobe confirms h264/hevc codecs as expected |
| `test_fixture_hevc_is_hevc` | `hevc_skip.mkv` probe returns `codec_name == hevc` |
| `test_fixture_hi10` | `h264_hi10.mkv` bits_per_raw_sample >= 10 |
| `test_passes_size_filter` | files above min pass, below min fail |
| `test_passes_duration_filter` | files above min pass, below min fail |

**Agent usage:** Agent can write `make_fixtures.ps1` independently.

---

### 0c — `db.py` + SQLite schema

**New file:** `VideoConverter/db.py`

Uses raw `sqlite3` (no ORM). DB path: `VideoConverter/conversions.db` (next to `settings.json`).

```sql
CREATE TABLE IF NOT EXISTS conversions (
    id              INTEGER PRIMARY KEY,
    source_path     TEXT    NOT NULL,
    source_mtime    REAL    NOT NULL,   -- os.path.getmtime(); re-processing key
    source_size_mb  REAL,
    source_codec    TEXT,
    output_path     TEXT,
    output_size_mb  REAL,
    saved_mb        REAL,
    saved_pct       INTEGER,
    status          TEXT    NOT NULL DEFAULT 'pending',  -- pending|running|done|failed|skipped
    anime_mode      INTEGER DEFAULT 0,
    encoder_used    TEXT,
    started_at      TEXT,              -- ISO-8601 UTC
    completed_at    TEXT,
    error_tail      TEXT               -- last ~2 KB of ffmpeg stderr on failure
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_conversions_path_mtime
    ON conversions (source_path, source_mtime);
```

**Public API in `db.py`:**
```python
def init_db(db_path: str) -> None: ...          # called once at app startup
def get_record(source_path, source_mtime) -> dict | None: ...
def upsert_pending(source_path, source_mtime, source_size_mb, source_codec) -> int: ...
def mark_running(record_id, started_at) -> None: ...
def mark_done(record_id, output_path, output_size_mb, saved_mb, saved_pct,
              completed_at, encoder_used) -> None: ...
def mark_failed(record_id, error_tail, completed_at) -> None: ...
```

**`app.py` startup:**
```python
DB_PATH = os.path.join(BASE_DIR, 'conversions.db')
with app.app_context():
    db.init_db(DB_PATH)
```

**Tests (run after this phase):**
```
pytest tests/test_fixtures.py tests/test_candidate_filtering.py tests/test_db.py
```
| Test | Assert |
|------|--------|
| `test_db_init_creates_table` | `init_db()` creates `conversions` table |
| `test_upsert_pending` | new row inserted with `status='pending'` |
| `test_upsert_idempotent` | same `(source_path, source_mtime)` → same row |
| `test_mark_running` | record flips to `status='running'` |
| `test_mark_done` | record flips to `status='done'`, output fields populated |
| `test_mark_failed` | `status='failed'`, `error_tail` set |
| `test_get_record_miss` | unknown path returns `None` |
| `test_mtime_change_requeues` | same path, different mtime → new pending row |

**Commit:** `Test: fixture videos + port candidate_filtering + bitmap_subs; Feat: db.py SQLite schema`

---

## Phase 1 — `scanner.py` + `/api/scan` SSE

**Goal:** Replace the `DEMO_FILES` stub in `scanFolder()` with a real streaming scan.

**New file:** `VideoConverter/scanner.py`

```python
# Public API:
def walk(root: str) -> Generator[dict, None, None]:
    """
    Yields one dict per folder encountered (sorted by highest total bitrate first):
    {
      "type": "folder",
      "folder": "relative/path/from/root",
      "files": [
        {
          "full_path": "D:/Anime/Folder/ep01.mkv",
          "name": "ep01.mkv",
          "folder": "Folder",             # relative to scan root
          "size": "1,234",               # MB, comma-string (existing JS expects this)
          "codec": "H264",               # normalised display label
          "duration": "23:15",           # HH:MM:SS or MM:SS
          "is_hi10": False,              # True for 10-bit H264 → remux path
          "streams": {
            "video": { codec, profile, resolution, fps, bitrate, hdr },
            "audio": [ { track, codec, channels, language, bitrate, title }, ... ],
            "subs":  [ { track, codec, language, title }, ... ],
          },
          "status": "pending",
        },
        ...
      ]
    }
    # Final:
    { "type": "done", "total_files": N, "total_mb": X }
    # Per-file warning (corrupt/unreadable):
    { "type": "warning", "path": "...", "message": "..." }
    # Hard error (can't read root):
    { "type": "error", "message": "..." }
    """
```

**Extension filter:** `.mkv .mp4 .avi .m4v .mov .wmv .ts .m2ts`  
**Skip rules (DB-first):**
- First video stream is `hevc` / `av1` / `hevc_cuvid` — already encoded (codec filter, no DB write)
- `db.get_record(full_path, mtime)` returns `status='done'` — already converted (silently skipped, `total_files` count excludes these)
- `db.get_record(full_path, mtime)` returns `status='running'` — in progress in another session (emit `warning` event, skip)

> **No `converted/` subfolder skip rule.** Files inside `converted/` are excluded by the codec filter (they are HEVC) or by the DB (status='done'). Scanner does not check the directory name.

**ffprobe call:** `ffprobe -v quiet -print_format json -show_streams -show_format` — one call per file.

**Flask route:**
```python
@app.route("/api/scan")
def api_scan():
    path = request.args.get("path", "").strip()
    if not path or not os.path.isdir(path):
        return jsonify({"error": "Invalid path"}), 400
    def generate():
        for event in scanner.walk(path):
            yield f"data: {json.dumps(event)}\n\n"
    return Response(generate(), mimetype="text/event-stream",
                    headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})
```

**Tests:**
```
pytest tests/test_scanner.py
```
| Test | Assert |
|------|--------|
| `test_walk_finds_h264_files` | all h264 fixtures appear |
| `test_walk_skips_hevc` | `hevc_skip.mkv` not in any yielded files |
| `test_walk_skips_db_done` | file with `status='done'` in DB not yielded |
| `test_walk_requeues_replaced_file` | same path, new mtime → yielded even though path was `done` before |
| `test_walk_finds_all_video_extensions` | `.mp4` fixture found alongside `.mkv` |
| `test_walk_streams_populated` | `streams.video.codec == "H264"` on h264_short |
| `test_walk_hi10_flagged` | `h264_hi10.mkv` has `is_hi10 == True` |
| `test_walk_folder_events` | each folder yields one event with correct relative folder name |
| `test_walk_done_event` | final event `type == "done"` with correct `total_files` count |
| `test_walk_corrupt_file_emits_warning` | a zero-byte `.mkv` yields a `warning` event, walk continues |
| `test_api_scan_invalid_path` | returns 400 |
| `test_api_scan_sse_content_type` | response Content-Type is `text/event-stream` |
| `test_api_scan_db_skip_done` | scan a folder with one done record in DB → 0 files yielded |

**Commit:** `Feat: scanner.py - recursive walk, ffprobe stream info, DB-aware skip, SSE /api/scan`

---

## Phase 2 — JS Scan Wiring

**Goal:** Replace `setTimeout(DEMO_FILES…)` in `scanFolder()` with a live `EventSource`.

**Changes to `app.js` only** — no template changes, hard-refresh only.

**Key change — incremental table append:** `populateTable()` currently re-renders the full tbody on every call. With SSE streaming, each folder event appends new rows without re-rendering existing ones. This preserves `est-*` cell IDs for the estimation strip. Implementation: new `_appendRows(files)` that `appendChild`es only the new rows; `populateTable()` is kept for full re-render (sort, filter changes).

**New `scanFolder()` logic:**
```javascript
let _scanEs = null;  // module-level, so new scan closes previous

function scanFolder(path) {
  if (_scanEs) { _scanEs.close(); _scanEs = null; }
  // ... reset state, show spinner ...
  _scanEs = new EventSource('/api/scan?path=' + encodeURIComponent(path));
  _scanEs.onmessage = e => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'folder') {
      const startIdx = _files.length;
      msg.files.forEach(f => _files.push(f));
      _appendRows(msg.files, startIdx);  // incremental append
      updateStats(_files);
    } else if (msg.type === 'done') {
      _scanEs.close(); _scanEs = null;
      setButtonStates('ready');
      runEstimation(_files);
      addLog('Found ' + _files.length + ' files — ' + msg.total_mb.toFixed(1) + ' GB', 'ok');
    } else if (msg.type === 'warning') {
      addLog('Skipped: ' + msg.message, 'warn');
    } else if (msg.type === 'error') {
      _scanEs.close(); _scanEs = null;
      addLog('Scan error: ' + msg.message, 'err');
      setButtonStates('idle');
    }
  };
  _scanEs.onerror = () => {
    if (_scanEs) { _scanEs.close(); _scanEs = null; }
    addLog('Scan connection lost.', 'err');
    setButtonStates('idle');
  };
}
```

**Manual tests:**
1. Select fixture folder → rows appear per-folder in real time; estimation strip appears after
2. Start a scan, immediately select a different folder → first SSE stream closes cleanly, no duplicate rows
3. Select an inaccessible path → error in log, buttons idle
4. Sort dropdown changes order correctly on a live-populated table
5. Filter chips show correct counts as rows arrive

**Commit:** `Feat: wire scanFolder() to /api/scan SSE with incremental row append`

---

## Phase 3a — Normal Mode Conversion Backend

**Goal:** Background thread processes queue via `compress_simple()` with real progress, stop, pause/resume.

**New global state in `app.py`:**
```python
import threading, ctypes

_job_lock   = threading.Lock()
_stop_event = threading.Event()
_job = { "state": "idle", "current_index": 0, "total": 0,
         "current_file": "", "progress_pct": 0.0, "fps": 0.0,
         "eta_secs": 0, "saved_mb": 0.0, "encoder": "",
         "files": [], "log": [] }
_ffmpeg_pid: int | None = None   # set while encode is running — used for pause/resume
# DB path inherited from startup init_db() call

PROCESS_ALL_ACCESS = 0x1F0FFF

def _suspend_ffmpeg():
    if _ffmpeg_pid:
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, _ffmpeg_pid)
        ctypes.windll.ntdll.NtSuspendProcess(h)
        ctypes.windll.kernel32.CloseHandle(h)

def _resume_ffmpeg():
    if _ffmpeg_pid:
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, _ffmpeg_pid)
        ctypes.windll.ntdll.NtResumeProcess(h)
        ctypes.windll.kernel32.CloseHandle(h)
```

**`converter.py` `_run_ffmpeg()` extended:**
- `stderr` merged into stdout (`stderr=subprocess.STDOUT`)
- Parse `frame=N fps=F` lines → call `progress_cb(pct, fps, eta_secs)`
- Store `proc.pid` in caller-accessible variable for NtSuspend
- Check `stop_event.is_set()` each line → `proc.kill()`
- Accept `quality` param per-call (not hardcoded from `config`)

**DB writes in the queue worker thread:**
1. Before encode starts: `db.mark_running(record_id, started_at=utcnow())`
2. After `_verify_output()` passes: `db.mark_done(record_id, output_path, output_size_mb, saved_mb, saved_pct, completed_at, encoder_used)`
3. On any failure / stop_event: `db.mark_failed(record_id, error_tail, completed_at)`

> `record_id` comes from `db.upsert_pending()` called during `/api/start` when the file list is received.

**New routes:**
| Route | Method | Body | Returns |
|-------|--------|------|---------|
| `/api/start` | POST | `{files:[{full_path,...}], anime_mode: bool}` | `{"ok":true}` / 409 if running |
| `/api/status` | GET | — | full `_job` dict |
| `/api/stop` | POST | — | `{"ok":true}` |
| `/api/pause` | POST | — | `{"ok":true}` |
| `/api/resume` | POST | — | `{"ok":true}` |

**Tests:**
```
pytest tests/test_converter.py tests/test_routes_conversion.py
```
| Test | Assert |
|------|--------|
| `test_compress_simple_qsv_success` | output in `converted/`, smaller than source |
| `test_compress_simple_sw_fallback` | QSV stubbed to fail → libx265 output produced |
| `test_compress_simple_no_savings` | returns False, no file in `converted/` |
| `test_compress_simple_stop` | `stop_event.set()` mid-encode → False, temp cleaned |
| `test_progress_cb_called` | `progress_cb` called with increasing pct values |
| `test_api_start_returns_ok` | status thread starts, `state == running` |
| `test_api_double_start_409` | second POST `/api/start` returns 409 |
| `test_api_stop` | state transitions to `idle` |
| `test_api_pause_resume` | `_ffmpeg_pid` is NtSuspended/Resumed; progress stops then continues |
| `test_db_running_on_start` | DB record flips to `running` when encode begins |
| `test_db_done_on_success` | DB record flips to `done` with output path + savings after verify passes |
| `test_db_failed_on_stop` | DB record flips to `failed` with `error_tail` when stopped mid-encode |

**Commit:** `Feat: normal mode conversion backend - threading, NtSuspend pause, progress_cb`

---

## 🎬 Real Video Test — Normal Mode

**When:** after Phase 3a commit, before writing Phase 3b code.

**Provide:** any H.264, VC-1, or ProRes file (`.mov`, `.mp4`, `.mkv`, `.avi`). Example from prior session: `C:\Users\scott\Downloads\Rachel Cook\Rac703ruepwe.mov`.

**Run directly (no UI needed):**
```powershell
& "C:\VideoTools\.venv\Scripts\python.exe" -c "
import sys; sys.path.insert(0, 'C:/VideoTools/VideoConverter')
import threading, converter
stop = threading.Event()
result = converter.convert_video(
    input_path=r'C:\path\to\your_file.ext',
    output_dir=r'C:\Temp\vc_test_out',
    anime_mode=False,
    quality=30,
    progress_cb=lambda p,f,e: print(f'{p:.0f}% fps={f:.0f} eta={e}s'),
    stop_event=stop)
print('Result:', result)
"
```

**Verify:**
- Output `.mkv` in `C:\Temp\vc_test_out\`
- File size < source
- Log line shows `hevc_qsv` or `libx265`
- Run `probe_output.py` (in `tests/`) to confirm duration within 5% and video stream is HEVC

---

## Phase 3b — Anime Mode Conversion Backend

**Goal:** Port the battle-tested anime pipeline from `convert_videos.py` into `converter.py`.

**What is ported (adapted from `convert_videos.py`):**

| Feature | Source function | Destination |
|---------|----------------|-------------|
| Hi10 H.264 detection | `is_video_10bit_h264()` | `converter.is_hi10()` |
| Remux to MP4 (container change, AAC transcode, PGS→mov_text) | `remux_to_mp4()` | `converter.remux_to_mp4()` |
| Bitmap sub detection + OCR | `_ocr_srt_map`, `convert_bitmap_subs.py` | `bitmap_subs.ocr_bitmap_subs_to_srt()` |
| AAC encoder selection (`aac_mf` on Windows) | `_AAC_ENCODER` | `converter._aac_encoder()` |
| English subtitle protection | `_is_potentially_english()`, sole-sub rule | `converter._is_potentially_english()` |
| DTS overflow detection + retry | lines 1100–1200 of convert_videos.py | `converter.remux_to_mp4()` |
| Power throttling fix | `_disable_power_throttling()` | `app.py` startup |
| Local temp staging | `_local_temp_convert()` | Already in `config.LOCAL_TEMP_DIR`; staging already done in `compress_simple()` |
| MP4 fast-path (already MP4 + AAC + no bitmap subs) | `remux_to_mp4()` early return | `converter.remux_to_mp4()` |

**`convert_video()` dispatch logic:**
```python
def convert_video(input_path, output_dir, anime_mode, quality, progress_cb, stop_event):
    if anime_mode:
        # Hi10 H264 → remux (no QSV decode)
        if is_hi10(input_path):
            return remux_to_mp4(input_path, output_dir, ...)
        # Normal H264 → compress (QSV/SW) then remux result to MP4
        return compress_and_remux(input_path, output_dir, ...)
    else:
        # Normal mode: compress in-place, keep container
        return compress_simple(input_path, output_dir, ...)
```

**What is NOT ported:**
- Tkinter GUI code (all UI is now Flask/JS)
- `log_to_output_box()` → replaced by `log_fn` callable (same pattern as existing `converter.py`)
- OpenCV dependency for frame counting → ffprobe only (already sufficient)
- `enable_conservative_mode` / `enable_keep_all_streams` toggles → simplified to always keep-all-streams (the newer design decision)

**Tests:**
```
pytest tests/test_converter_anime.py
```
| Test | Assert |
|------|--------|
| `test_remux_mp4_h264_aac` | `h264_mp4_aac.mp4` fast-path: no remux, goes straight to QSV compress |
| `test_remux_mkv_multitrack` | `h264_multitrack.mkv`: output is `.mp4`, audio is AAC, ASS subs become mov_text |
| `test_remux_hi10` | `h264_hi10.mkv`: video stream copied (no re-encode), audio re-encoded to AAC |
| `test_bitmap_sub_ocr` | `h264_multitrack.mkv` bitmap PGS → SRT embedded in output MP4 |
| `test_aac_encoder_windows` | `_aac_encoder()` returns `aac_mf` on win32 |
| `test_english_sub_protection` | file with mixed-lang subs: English sub kept, foreign dropped |
| `test_dts_overflow_recovery` | synthetic DTS-corrupt file → falls back to no-sub output |
| `test_stop_during_remux` | `stop_event.set()` mid-remux → False, output cleaned |
| `test_db_done_anime_path` | anime convert of `h264_multitrack.mkv` → DB record `status='done'`, `output_path` is `.mp4` |

**Commit:** `Feat: anime mode conversion - remux to MP4, AAC transcode, bitmap sub OCR, Hi10 path`

---

## 🎬 Real Video Test — Anime Mode

**When:** after Phase 3b commit, before writing Phase 4 code.

**Provide:** an MKV anime file with at least one subtitle track — ideally a Hi10 H.264 file (10-bit) to exercise the remux path. Any `.mkv` with AC3/DTS audio and ASS or PGS subs works.

**Two test cases:**

*Case A — normal H.264 anime (compress + remux):*
```powershell
& "C:\VideoTools\.venv\Scripts\python.exe" -c "
import sys; sys.path.insert(0, 'C:/VideoTools/VideoConverter')
import threading, converter
stop = threading.Event()
result = converter.convert_video(
    input_path=r'C:\path\to\anime_ep01.mkv',
    output_dir=r'C:\Temp\vc_test_out',
    anime_mode=True,
    quality=30,
    progress_cb=lambda p,f,e: print(f'{p:.0f}%'),
    stop_event=stop)
print('Result:', result)
"
```

*Case B — Hi10 H.264 anime (remux only, no re-encode):*
Same command but with a known Hi10 source. Verify the output video stream is still H.264 (not HEVC) — the video is copied, not re-encoded.

**Verify:**
- Output is `.mp4` (not `.mkv`)
- Audio track is AAC
- At least one subtitle track present (text-based, not bitmap)
- For Hi10: video stream codec is still H.264, duration within 5%
- DB record `status='done'`, `output_path` ends in `.mp4`

---

## Phase 4 — JS Conversion Wiring

**Goal:** Replace the `_sim*` simulation with real polling of `/api/status`.

**Changes to `app.js`:**
- `startConversion()` → POST `/api/start` with `{files: _files, anime_mode: animeChecked}` then `_startPolling()`
- `_pollInterval` (1 s) → GET `/api/status`, update all progress widgets + row badges
- `pauseConversion()` → POST `/api/pause` or `/api/resume` based on current state
- Stop → POST `/api/stop`; Hard Stop → POST `/api/stop` (same — distinction is at Flask level: hard stop does `proc.kill()` immediately vs graceful)
- When `state == done`: stop poll, `setButtonStates('ready')`, resume estimation strip

**Cell update on poll:** for each file in `status.files`, if `status` changed → update badge; if `output_mb`/`saved_mb` filled → update Output/Saved/% cells.

**Manual tests:**
1. Normal mode: `h264_short.mkv` — progress fills, flips to done, stat cards update
2. Anime mode: `h264_multitrack.mkv` — log shows "Remuxing…" then "QSV compress…"
3. Pause mid-encode — progress bar stops incrementing within 1s
4. Resume — progress continues from exact same percentage
5. Stop — temp file gone, state `ready`, estimation resumes
6. Hard Stop — same as Stop from JS perspective

**Commit:** `Feat: wire JS conversion to /api/start + /api/status polling`

---

## 🎬 Real Video Test — Full UI Flow

**When:** after Phase 4 commit, before writing Phase 5 code.

**Setup:**
1. Start the Flask server: `& "C:\VideoTools\.venv\Scripts\python.exe" VideoConverter/app.py`
2. Open `http://localhost:5001` in browser

**Test sequence:**
1. Browse to a folder containing at least one H.264 MKV or MOV file
2. Click **Scan** → verify rows appear in the table with file names, sizes, and codec badges
3. Estimation strip should populate automatically after scan completes
4. Click **Start** → watch progress bar fill in real time, per-row status badges flip to "Converting" then "Done"
5. **Pause mid-encode** → progress bar stops incrementing within ~1 s
6. **Resume** → progress continues from same percentage
7. Let it complete → stat cards show total saved MB, Done count
8. Re-scan the same folder → files that completed should **not** reappear (DB skip)

**Verify:**
- Output files in `converted/` subfolder of the scanned folder
- Each output smaller than its source
- Log panel shows encoder used (QSV or SW fallback)
- DB records all show `status='done'`

---

## Phase 5 — Source Integrity Check + Source Deletion

**Goal:** Verify encode output before deleting the source. Integrates Feature 1 from backlog.

**In `converter.py`:**
```python
def _verify_output(output_path: str, src_duration: float) -> tuple[bool, str]:
    """
    Checks via ffprobe:
    - File exists and size > 0
    - At least one video stream present
    - Duration within 5% of src_duration
    Returns (True, "") on pass, (False, reason) on fail.
    """
```

**Sequence after successful encode:**
1. `_verify_output(output_path, src_duration)` → if fail: mark `failed`, log reason, skip deletion
2. If pass: `os.remove(source)` → log "Deleted source"
3. Update job status with `output_mb`, `saved_mb`, `pct`

**Tests:**
```
pytest tests/test_integrity.py
```
| Test | Assert |
|------|--------|
| `test_verify_ok` | freshly encoded `h264_short.mkv` output passes |
| `test_verify_missing_file` | `(False, "file not found")` |
| `test_verify_zero_bytes` | `(False, "empty file")` |
| `test_verify_wrong_duration` | file with 1s truncated beyond 5% → `(False, "duration mismatch …")` |
| `test_verify_no_video_stream` | audio-only file → `(False, "no video stream")` |
| `test_source_deleted_on_pass` | source file removed after verify passes |
| `test_source_kept_on_fail` | source file present when verify fails |

**Commit:** `Feat: output integrity check (ffprobe duration/stream verify) + source deletion`

---

## 🎬 Real Video Test — Source Deletion & Integrity

**When:** after Phase 5 commit, before writing Phase 6 E2E tests.

**Provide:** a disposable copy of any H.264 file (make a copy so original is safe).

**Happy path:**
```powershell
# Make a test copy
Copy-Item "C:\path\to\source.mkv" "C:\Temp\vc_delete_test\source.mkv"

# Convert via CLI (no UI needed)
& "C:\VideoTools\.venv\Scripts\python.exe" -c "
import sys; sys.path.insert(0, 'C:/VideoTools/VideoConverter')
import threading, converter
stop = threading.Event()
result = converter.convert_video(
    input_path=r'C:\Temp\vc_delete_test\source.mkv',
    output_dir=r'C:\Temp\vc_delete_test\converted',
    anime_mode=False, quality=30,
    progress_cb=lambda p,f,e: print(f'{p:.0f}%'),
    stop_event=stop)
print('Result:', result)
"
```

**Verify:**
- **Source file is gone** (deleted after successful integrity check)
- Output file exists in `converted/` and is smaller
- DB record has `status='done'`

**Failure path (manual):**
- Temporarily corrupt `_verify_output()` to always return `(False, 'test')`
- Re-run → source file must **not** be deleted; DB record `status='failed'`
- Revert the corruption

---

## Phase 6 — End-to-End Test Pass

**Goal:** Complete realistic flow with no mocks — real FFmpeg, real files.

```
pytest tests/test_e2e.py -v
```

| Test | Assert |
|------|--------|
| `test_e2e_scan_fixtures` | 6 files found (hevc skipped), all streams populated |
| `test_e2e_scan_skips_db_done` | after converting `h264_short.mkv`, re-scan → it does not appear again |
| `test_e2e_estimate_h264_short` | estimate returns `estimated_saving_pct > 0` |
| `test_e2e_normal_convert` | `h264_short.mkv` → `converted/h264_short.mkv`, smaller, source deleted, DB `status='done'` |
| `test_e2e_anime_convert_mkv` | `h264_multitrack.mkv` → `converted/h264_multitrack.mp4`, AAC audio, text subs, DB `status='done'` |
| `test_e2e_anime_hi10` | `h264_hi10.mkv` → `converted/h264_hi10.mp4`, video stream copied (H264, not HEVC) |
| `test_e2e_stop_mid_convert` | stop during `h264_long.mkv` → temp cleaned, source intact, DB `status='failed'` |
| `test_e2e_pause_resume` | pause + resume on `h264_long.mkv` → eventually completes, DB `status='done'` |
| `test_e2e_full_queue` | all 6 fixtures convert sequentially, stat cards match final counts |
| `test_e2e_mtime_requeue` | replace a `done` fixture with a fresh colourbar (different mtime) → re-scan yields it again |

**Commit:** `Test: end-to-end tests - scan, estimate, normal + anime convert, stop, pause/resume`

---

## Agent Usage

| Phase | Task | Agent suitable? |
|-------|------|----------------|
| 0a | Write `make_fixtures.ps1` (FFmpeg colourbar + sine commands) | ✅ Yes — self-contained script |
| 0b | Port `candidate_filtering.py` (copy verbatim) | ✅ Yes — trivial |
| 0c | Write `db.py` + `tests/test_db.py` | ✅ Yes — pure Python, no Flask coupling |
| 1 | Write `scanner.py` + `tests/test_scanner.py` | ✅ Yes — no UI coupling |
| 3a | Conversion backend threading + DB writes | ❌ Main agent — tightly coupled to `app.py` globals |
| 3b | Port anime pipeline + DB writes | ❌ Main agent — needs careful adaptation, not mechanical copy |
| 5 | `_verify_output()` + tests | ✅ Yes — isolated function |

---

## Commit Timeline

```
Phase 0:   Test: fixture videos + port candidate_filtering + bitmap_subs; Feat: db.py SQLite schema
Phase 1:   Feat: scanner.py - recursive walk, ffprobe stream info, DB-aware skip, SSE /api/scan
Phase 2:   Feat: wire scanFolder() to /api/scan SSE with incremental row append
Phase 3a:  Feat: normal mode conversion backend - threading, NtSuspend pause, progress_cb, DB writes
Phase 3b:  Feat: anime mode - remux to MP4, AAC transcode, bitmap sub OCR, Hi10 path, DB writes
Phase 4:   Feat: wire JS conversion to /api/start + /api/status polling
Phase 5:   Feat: output integrity check + source deletion
Phase 6:   Test: end-to-end tests with DB assertions
```

---

## Things Deliberately OUT of Scope for this Plan

- SQLite job history DB — follow-on (reprocessing protection handled by source deletion + HEVC skip filter now)
- Windows toast notification (Feature 9) — easy add after Phase 4
- Per-file output path overrides (Feature 8) — on hold
- Multi-folder queue (scan multiple roots) — future
- Non-Windows support — `NtSuspendProcess` and `aac_mf` are Windows-only; Linux/Mac paths can be added later

---

*Status: awaiting sign-off to begin Phase 0.*
