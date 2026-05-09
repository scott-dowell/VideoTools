# Current Job Card — Implementation Plan

Status: approved for implementation  
Date: 2026-05-09

---

## Overview

Replace the right-panel "Progress" card with a "Current Job" card that shows:
- **OCR batch mode**: which files are queued / running / done during the pre-pass
- **Per-file mode**: a live step checklist with state icons and detail text
- Retry attempt count on the Remux step
- The ffmpeg live status line inline (not a separate status bar above the log)
- The Overall progress bar stays (moved inside the new card)

No table changes. Log box unchanged (verbose lines stay).

---

## Architecture Decision

Step state is maintained **server-side** in `_job["steps"]` and `_job["ocr_batch"]`.
The frontend reads them from `/api/status` at the normal 500ms poll interval.
This is Phase 2 (structured events) — no log-line parsing in JS.

The backend updates step state:
1. **Explicitly** at known code points in `_queue_worker` (OCR batch transitions,
   file start, compress/remux/verify transitions)
2. **Via `_capture_log` hook** which intercepts all log messages from `converter.py`
   and matches known strings to advance step state

---

## Data Structures Added to `_job`

```python
_job = {
    # ... existing fields unchanged ...

    # New: current phase
    "phase": "",          # "" | "ocr_batch" | "converting"

    # New: OCR batch summary (populated only during phase == "ocr_batch")
    "ocr_batch": {
        "total":        0,   # total files in batch
        "done":         0,   # files with sidecar produced or confirmed no-PGS
        "current_file": "",  # basename of file being OCR'd right now
        "files": [],         # [{"name": str, "state": "waiting"|"running"|"done"|"failed"}]
    },

    # New: per-file step list (populated on each file start, cleared on idle)
    "steps": [],
    # Each step:
    # {
    #   "id":      "ocr" | "audio" | "compress" | "remux" | "verify",
    #   "label":   str,        e.g. "Compress"
    #   "state":   "waiting" | "running" | "done" | "failed" | "retry" | "skipped",
    #   "detail":  str,        e.g. "hevc_qsv · 147 fps"  or  "attempt 2/6 · DTS fix"
    #   "attempt": int,        1-6 for remux; 1 for all others
    # }
}
```

---

## Stage 1 — Backend: Step Infrastructure

### 1a. Extend `_job` initial dict

File: `app.py`  
In the `_job` dict declaration (line ~50) and in the `api_start` reset block (line ~785):

```python
"phase":     "",
"ocr_batch": {"total": 0, "done": 0, "current_file": "", "files": []},
"steps":     [],
```

### 1b. Add `_step()` and `_set_phase()` helpers

File: `app.py`, near `_job_log()`:

```python
def _step(step_id: str, state: str, detail: str = "", attempt: int = 1) -> None:
    """Update a single step's state in the current job under the lock."""
    with _job_lock:
        for s in _job["steps"]:
            if s["id"] == step_id:
                s["state"]   = state
                s["detail"]  = detail
                s["attempt"] = attempt
                return

def _set_phase(phase: str) -> None:
    with _job_lock:
        _job["phase"] = phase
```

### 1c. Build the step list at file-start

File: `app.py`, inside `_queue_worker`, just before `convert_video()` is called.

The step list depends on anime mode, video codec, and stream types from `file_info`.

```python
def _build_steps(file_info: dict, anime_mode: bool) -> list[dict]:
    streams   = file_info.get("streams") or {}
    vid       = streams.get("video") or {}
    v_codec   = (vid.get("codec") or "").lower()
    subs      = streams.get("subs") or []
    has_pgs   = any(
        (s.get("codec") or "").upper() in ("PGS", "HDMV_PGS_SUBTITLE", "PGSSUB")
        for s in subs
    )
    audio     = streams.get("audio") or []
    has_audio = len(audio) > 0

    steps = []

    if anime_mode:
        # OCR step — only if PGS subs exist; otherwise shown as skipped
        ocr_state = "waiting" if has_pgs else "skipped"
        ocr_detail = "" if has_pgs else "No PGS tracks"
        steps.append({"id": "ocr", "label": "OCR", "state": ocr_state,
                       "detail": ocr_detail, "attempt": 1})

        # Audio pre-encode — shown only when non-AAC audio present
        # (if all audio is already AAC it will be stream-copied; still show it)
        steps.append({"id": "audio", "label": "Audio", "state": "waiting",
                       "detail": "", "attempt": 1})

        if v_codec in ("av1", "av1_cuvid"):
            # AV1: stream-copy video → goes straight to remux (no compress step)
            steps.append({"id": "remux", "label": "Remux", "state": "waiting",
                           "detail": "AV1 stream-copy → MP4", "attempt": 1})
        elif v_codec == "hevc" or v_codec.startswith("hevc"):
            # HEVC: already optimal — no compress, straight remux
            steps.append({"id": "remux", "label": "Remux", "state": "waiting",
                           "detail": "HEVC stream-copy → MP4", "attempt": 1})
        else:
            # H.264 (normal or Hi10): compress then remux
            steps.append({"id": "compress", "label": "Compress", "state": "waiting",
                           "detail": "", "attempt": 1})
            steps.append({"id": "remux", "label": "Remux", "state": "waiting",
                           "detail": "MKV → MP4", "attempt": 1})

        steps.append({"id": "verify", "label": "Verify", "state": "waiting",
                       "detail": "", "attempt": 1})
    else:
        # Non-anime: single compress step
        steps.append({"id": "compress", "label": "Compress", "state": "waiting",
                       "detail": "", "attempt": 1})
        steps.append({"id": "verify", "label": "Verify", "state": "waiting",
                       "detail": "", "attempt": 1})

    return steps
```

Set at file start:
```python
with _job_lock:
    _job["steps"] = _build_steps(file_info, anime_mode)
    _job["phase"] = "converting"
```

### 1d. Wire OCR batch state

File: `app.py`, inside the OCR batch loop.

At batch start (before the `while _remaining` loop):
```python
with _job_lock:
    _job["phase"] = "ocr_batch"
    _job["ocr_batch"] = {
        "total":        len(_ocr_paths),
        "done":         0,
        "current_file": os.path.basename(_ocr_paths[0]) if _ocr_paths else "",
        "files": [
            {"name": os.path.basename(p), "state": "waiting"}
            for p in _ocr_paths
        ],
    }
```

Inside the OCR output-reading loop, when `_current_ocr_path` changes (a new file
starts), update the batch state:
```python
# Existing line that detects file transition:
if _prev_was_sep and stripped.startswith("  ") and stripped.strip() in _ocr_bn_map:
    _prev_path = _current_ocr_path
    _current_ocr_path = _ocr_bn_map[stripped.strip()]
    # NEW: mark previous as done, current as running
    with _job_lock:
        b = _job["ocr_batch"]
        b["current_file"] = os.path.basename(_current_ocr_path)
        for fi in b["files"]:
            if fi["name"] == os.path.basename(_prev_path):
                fi["state"] = "done"
                b["done"] = b.get("done", 0) + 1
            if fi["name"] == os.path.basename(_current_ocr_path):
                fi["state"] = "running"
```

At batch end (before the `for idx, file_info` loop):
```python
# Mark all remaining as done (or failed for ocr_failed_paths)
with _job_lock:
    b = _job["ocr_batch"]
    for fi in b["files"]:
        fp = _ocr_bn_map_final.get(fi["name"])   # need to keep full map accessible
        if fp in ocr_failed_paths:
            fi["state"] = "failed"
        elif fi["state"] != "failed":
            fi["state"] = "done"
    b["done"] = sum(1 for fi in b["files"] if fi["state"] == "done")
    b["current_file"] = ""
```

### 1e. Update steps from `_capture_log`

File: `app.py`, `_capture_log` closure inside `_queue_worker`.

The log messages from `converter.py` are the hook points:

```python
_REMUX_ATTEMPT = [0]   # closure-captured counter for current file

def _capture_log(msg: str) -> None:
    _file_log.append(msg)
    _job_log(msg)

    # --- Step state machine ---
    m = msg.strip()

    # Compress phase (anime mode H.264)
    if m == "Anime mode: compressing then remuxing to MP4.":
        _step("compress", "running", "hevc_qsv")

    elif m.startswith("Compressing with "):
        # non-anime: "Compressing with hevc_qsv..."
        enc = m.split("Compressing with ", 1)[1].rstrip(".")
        _step("compress", "running", enc)

    # Compress done (anime): "Remuxing compressed output to MP4..."
    elif m == "Remuxing compressed output to MP4...":
        _step("compress", "done", "")
        _REMUX_ATTEMPT[0] = 1
        _step("remux", "running", f"attempt {_REMUX_ATTEMPT[0]}/6")

    # Direct remux (Hi10, HEVC, AV1, or single-pass anime)
    elif m == "Remuxing to MP4...":
        _REMUX_ATTEMPT[0] = (_REMUX_ATTEMPT[0] or 0) + 1
        attempt = _REMUX_ATTEMPT[0]
        _step("remux", "running", f"attempt {attempt}/6")

    # DTS retry messages → remux retry
    elif m.startswith("DTS overflow detected"):
        _REMUX_ATTEMPT[0] += 1
        _step("remux", "retry", f"attempt {_REMUX_ATTEMPT[0]}/6 · DTS fix")

    elif m.startswith("DTS fix retry"):
        _REMUX_ATTEMPT[0] += 1
        _step("remux", "retry", f"attempt {_REMUX_ATTEMPT[0]}/6 · genpts fix")

    elif m.startswith("AAC mux failed"):
        _REMUX_ATTEMPT[0] += 1
        _step("audio", "running", "pre-encoding individually")
        _step("remux", "retry", f"attempt {_REMUX_ATTEMPT[0]}/6 · audio pre-enc")

    elif m.startswith("Subtitle DTS fix"):
        _REMUX_ATTEMPT[0] += 1
        _step("remux", "retry", f"attempt {_REMUX_ATTEMPT[0]}/6 · SRT pre-extract")

    elif m.startswith("Retrying with pre-extracted SRT"):
        _step("remux", "running", f"attempt {_REMUX_ATTEMPT[0]}/6 · SRT subs")

    elif m.startswith("Retrying without subtitle"):
        _REMUX_ATTEMPT[0] += 1
        _step("remux", "retry", f"attempt {_REMUX_ATTEMPT[0]}/6 · no subs")

    # Audio pre-encode track progress
    elif "Pre-encoding audio track" in m:
        _step("audio", "running", m.split("Pre-encoding audio track", 1)[1].strip().rstrip("."))

    # Verify
    elif m.startswith("Integrity check failed"):
        _step("verify", "failed", m.split("Integrity check failed:", 1)[-1].strip())

    elif m.startswith("Done. Saved"):
        _step("remux", "done", "")
        _step("verify", "done", "")

    elif m.startswith("Skipped – output was not smaller"):
        _step("compress", "failed", "no savings")
        _step("verify", "skipped", "")
```

Also: before `convert_video()` is called, set the OCR step for the current file
based on whether OCR was already done in the batch:

```python
with _job_lock:
    for s in _job["steps"]:
        if s["id"] == "ocr":
            ocr_st = file_info.get("ocr_status", "")
            if ocr_st == "done":
                s["state"]  = "done"
                s["detail"] = "SRT extracted"
            elif ocr_st == "skipped":
                s["state"]  = "skipped"
                s["detail"] = "No PGS tracks"
            else:
                s["state"]  = "done"   # batch ran before we got here
            break
```

---

## Stage 2 — Backend: Clean up on idle

File: `app.py`, in the final `with _job_lock` block at the end of `_queue_worker`:

```python
_job["phase"]     = ""
_job["ocr_batch"] = {"total": 0, "done": 0, "current_file": "", "files": []}
_job["steps"]     = []
```

Also reset in `api_start` alongside the existing `_job.update({...})`.

---

## Stage 3 — HTML: Replace Progress Card

File: `templates/index.html`

Remove the existing `<!-- Progress -->` card (the one with fileBar, overallBar,
fpsVal, etaVal, savedVal, elapsedVal).

Replace with:

```html
<!-- Current Job -->
<div class="card" id="currentJobCard">
  <div class="card-header d-flex align-items-center justify-content-between">
    <span><i class="bi bi-activity me-2"></i>Current Job</span>
    <span class="text-secondary" style="font-size:.75rem" id="jobFileCounter"></span>
  </div>
  <div class="card-body pb-2" id="jobCardBody">

    <!-- Idle state -->
    <div id="jobIdle" class="text-secondary text-center py-3" style="font-size:.85rem">
      —
    </div>

    <!-- OCR batch mode -->
    <div id="jobOcrBatch" class="d-none">
      <div class="fw-semibold mb-1" style="font-size:.85rem">
        <i class="bi bi-cpu me-1 text-purple"></i>OCR pre-pass
      </div>
      <div class="progress mb-2" style="height:5px">
        <div class="progress-bar" id="ocrBatchBar" style="width:0%;background:#7c3aed"></div>
      </div>
      <div id="ocrBatchFileList" style="font-size:.75rem;max-height:120px;overflow-y:auto"></div>
    </div>

    <!-- Per-file converting mode -->
    <div id="jobConverting" class="d-none">
      <div class="text-truncate fw-semibold mb-1" id="jobFilename"
           style="font-size:.82rem" title=""></div>

      <!-- File progress bar -->
      <div class="d-flex justify-content-between mb-1">
        <small class="text-secondary">File progress</small>
        <small id="filePct">0%</small>
      </div>
      <div class="progress mb-1">
        <div class="progress-bar" id="fileBar" style="width:0%"></div>
      </div>

      <!-- Step checklist -->
      <ul class="step-list" id="stepList"></ul>

      <!-- Stats row: fps / ETA / elapsed -->
      <div class="d-flex justify-content-between align-items-center mt-2 pt-1 border-top"
           style="font-size:.75rem">
        <span>
          <span class="fw-bold text-primary" id="fpsVal">—</span>
          <span class="text-secondary ms-1">fps</span>
        </span>
        <span>
          <span class="fw-bold text-warning" id="etaVal">—</span>
          <span class="text-secondary ms-1">ETA</span>
        </span>
        <span>
          <span class="fw-bold text-success" id="savedVal">—</span>
          <span class="text-secondary ms-1">saved</span>
        </span>
        <span>
          <span class="fw-bold text-info" id="elapsedVal">—</span>
          <span class="text-secondary ms-1">elapsed</span>
        </span>
      </div>

      <!-- ffmpeg live status line -->
      <div id="ffmpegStatus" class="ffmpeg-status mt-1 d-none"
           style="font-size:.65rem;border-top:none;padding-top:4px"></div>
    </div>

    <!-- Overall progress (always shown when running) -->
    <div id="jobOverall" class="mt-2 d-none">
      <div class="d-flex justify-content-between mb-1">
        <small class="text-secondary">Overall</small>
        <small id="overallPct">0%</small>
      </div>
      <div class="progress" style="height:4px">
        <div class="progress-bar overall" id="overallBar" style="width:0%"></div>
      </div>
    </div>

  </div>
</div>
```

Note: `fileBar`, `filePct`, `fpsVal`, `etaVal`, `savedVal`, `elapsedVal`,
`overallBar`, `overallPct`, `ffmpegStatus` all keep their existing IDs so the
rest of `_pollStatus` still works without changes to those lines.

---

## Stage 4 — CSS

File: `static/style.css`

```css
/* Step checklist */
.step-list {
  list-style: none;
  padding: 0;
  margin: .5rem 0 0;
}
.step-list li {
  display: flex;
  align-items: baseline;
  gap: .4rem;
  padding: .18rem 0;
  font-size: .78rem;
  line-height: 1.3;
}
.step-icon { width: 1rem; text-align: center; flex-shrink: 0; }
.step-label { font-weight: 600; flex-shrink: 0; min-width: 5.5rem; }
.step-detail { color: #8b949e; font-size: .72rem; }

.step-done    .step-icon { color: #3fb950; }
.step-running .step-icon { color: #d29922; }
.step-retry   .step-icon { color: #f0883e; }
.step-failed  .step-icon { color: #f85149; }
.step-waiting .step-icon { color: #484f58; }
.step-skipped .step-icon { color: #484f58; opacity: .5; }

.step-running .step-label { color: #d29922; }
.step-retry   .step-label { color: #f0883e; }
.step-failed  .step-label { color: #f85149; }
.step-done    .step-label { color: #8b949e; }  /* de-emphasise completed */
.step-skipped .step-label { color: #484f58; opacity: .5; }

@keyframes step-pulse {
  0%, 100% { opacity: 1; }
  50%       { opacity: .35; }
}
.step-running .step-icon,
.step-retry   .step-icon { animation: step-pulse 1.2s ease-in-out infinite; }

/* OCR batch file list */
.ocr-batch-file {
  display: flex;
  gap: .4rem;
  align-items: center;
  padding: .1rem 0;
  font-size: .73rem;
}
.ocr-file-done    { color: #3fb950; }
.ocr-file-running { color: #d29922; }
.ocr-file-failed  { color: #f85149; }
.ocr-file-waiting { color: #484f58; }
```

Light-theme overrides (append to the `[data-bs-theme="light"]` block):
```css
[data-bs-theme="light"] .step-detail     { color: #57606a; }
[data-bs-theme="light"] .step-done .step-label { color: #57606a; }
[data-bs-theme="light"] .step-waiting .step-icon { color: #adb5bd; }
[data-bs-theme="light"] .step-skipped .step-icon { color: #adb5bd; }
[data-bs-theme="light"] .ocr-file-waiting { color: #adb5bd; }
```

---

## Stage 5 — JavaScript

File: `static/app.js`

### 5a. Step icon lookup

```js
function _stepIcon(state) {
  if (state === 'done')    return '<i class="bi bi-check-circle-fill"></i>';
  if (state === 'running') return '<i class="bi bi-arrow-repeat"></i>';
  if (state === 'retry')   return '<i class="bi bi-arrow-clockwise"></i>';
  if (state === 'failed')  return '<i class="bi bi-x-circle-fill"></i>';
  if (state === 'skipped') return '<i class="bi bi-dash-circle"></i>';
  return '<i class="bi bi-circle"></i>';  // waiting
}
```

### 5b. `_renderCurrentJob(s)` — called from `_pollStatus`

```js
function _renderCurrentJob(s) {
  const idle       = document.getElementById('jobIdle');
  const ocrDiv     = document.getElementById('jobOcrBatch');
  const convDiv    = document.getElementById('jobConverting');
  const overallDiv = document.getElementById('jobOverall');
  const counter    = document.getElementById('jobFileCounter');

  const phase = s.phase || '';
  const isRunning = s.state === 'running';

  // File counter (top-right of card header)
  if (counter) {
    counter.textContent = isRunning && s.total
      ? (s.current_index + 1) + ' of ' + s.total
      : '';
  }

  // Show/hide sections
  idle.classList.toggle('d-none',       phase !== '');
  ocrDiv.classList.toggle('d-none',     phase !== 'ocr_batch');
  convDiv.classList.toggle('d-none',    phase !== 'converting');
  overallDiv.classList.toggle('d-none', !isRunning);

  // --- OCR batch mode ---
  if (phase === 'ocr_batch' && s.ocr_batch) {
    const b = s.ocr_batch;
    const pct = b.total > 0 ? Math.round(b.done / b.total * 100) : 0;
    document.getElementById('ocrBatchBar').style.width = pct + '%';

    const listEl = document.getElementById('ocrBatchFileList');
    if (listEl) {
      listEl.innerHTML = (b.files || []).map(f => {
        const cls  = 'ocr-file-' + (f.state || 'waiting');
        const icon = f.state === 'done'    ? '<i class="bi bi-check-circle-fill"></i>'
                   : f.state === 'running' ? '<i class="bi bi-cpu-fill" style="animation:step-pulse 1.2s ease infinite"></i>'
                   : f.state === 'failed'  ? '<i class="bi bi-x-circle-fill"></i>'
                   :                         '<i class="bi bi-circle"></i>';
        return '<div class="ocr-batch-file ' + cls + '">' + icon +
               '<span class="text-truncate">' + _esc(f.name) + '</span></div>';
      }).join('');
    }
  }

  // --- Per-file converting mode ---
  if (phase === 'converting') {
    const filename = s.current_file ? s.current_file.split(/[\\/]/).pop() : '—';
    const fnEl = document.getElementById('jobFilename');
    if (fnEl) { fnEl.textContent = filename; fnEl.title = s.current_file || ''; }

    const listEl = document.getElementById('stepList');
    if (listEl && s.steps) {
      listEl.innerHTML = s.steps.map(st => {
        return '<li class="step-' + st.state + '">' +
          '<span class="step-icon">' + _stepIcon(st.state) + '</span>' +
          '<span class="step-label">' + _esc(st.label) + '</span>' +
          (st.detail ? '<span class="step-detail">' + _esc(st.detail) + '</span>' : '') +
          '</li>';
      }).join('');
    }
  }

  // currentFilename still updated by existing _pollStatus code for
  // non-card uses — that element no longer exists in the HTML but the
  // reference is now the jobFilename element above (same ID not reused;
  // the old id="currentFilename" element is removed from the template).
}

// Simple HTML escape
function _esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
```

### 5c. Wire into `_pollStatus`

At the top of the `.then(s => {` block in `_pollStatus`, add:
```js
_renderCurrentJob(s);
```

Remove the lines that previously wrote to `currentFilename` (the old progress card
element, which no longer exists after the HTML change).

---

## Stage 6 — Tests

### Test 1: Backend step dict shape

File: `VideoConverter/tests/test_current_job_steps.py`

```python
"""Unit tests for _build_steps() step list construction."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Import only the helper, not the full Flask app
from app import _build_steps   # will need to extract to a module-level function

H264_FILE = {
    "streams": {
        "video": {"codec": "h264"},
        "audio": [{"codec": "aac"}],
        "subs":  [{"codec": "PGS", "index": 2}],
    }
}
AV1_FILE = {
    "streams": {
        "video": {"codec": "av1"},
        "audio": [{"codec": "opus"}],
        "subs":  [{"codec": "ass", "index": 1}],
    }
}
HEVC_FILE = {
    "streams": {
        "video": {"codec": "hevc"},
        "audio": [{"codec": "aac"}],
        "subs":  [],
    }
}

def test_h264_anime_with_pgs():
    steps = _build_steps(H264_FILE, anime_mode=True)
    ids = [s["id"] for s in steps]
    assert ids == ["ocr", "audio", "compress", "remux", "verify"]
    ocr = next(s for s in steps if s["id"] == "ocr")
    assert ocr["state"] == "waiting"

def test_av1_anime():
    steps = _build_steps(AV1_FILE, anime_mode=True)
    ids = [s["id"] for s in steps]
    assert "compress" not in ids
    assert "remux" in ids
    remux = next(s for s in steps if s["id"] == "remux")
    assert "AV1" in remux["detail"]

def test_hevc_anime_no_pgs():
    steps = _build_steps(HEVC_FILE, anime_mode=True)
    ids = [s["id"] for s in steps]
    assert "compress" not in ids
    ocr = next(s for s in steps if s["id"] == "ocr")
    assert ocr["state"] == "skipped"

def test_non_anime():
    steps = _build_steps(H264_FILE, anime_mode=False)
    ids = [s["id"] for s in steps]
    assert ids == ["compress", "verify"]
    assert all(s["state"] == "waiting" for s in steps)
```

**Pass criterion**: all 4 tests green with `python -m pytest tests/test_current_job_steps.py`

### Test 2: Step state transitions via log parsing

File: `VideoConverter/tests/test_step_state_machine.py`

This test simulates the `_capture_log` state machine by feeding log lines and
checking `_job["steps"]`:

```python
"""Test that log lines correctly advance step states."""
# Patch _job and _job_lock so we can run the state machine in isolation
# without starting a Flask app.

import threading
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import app as _app

def _reset_steps(steps):
    with _app._job_lock:
        _app._job["steps"] = [dict(s) for s in steps]

def _get_step(step_id):
    with _app._job_lock:
        for s in _app._job["steps"]:
            if s["id"] == step_id:
                return dict(s)
    return None

BASE_STEPS = [
    {"id": "ocr",      "label": "OCR",      "state": "done",    "detail": "", "attempt": 1},
    {"id": "audio",    "label": "Audio",    "state": "waiting", "detail": "", "attempt": 1},
    {"id": "compress", "label": "Compress", "state": "waiting", "detail": "", "attempt": 1},
    {"id": "remux",    "label": "Remux",    "state": "waiting", "detail": "", "attempt": 1},
    {"id": "verify",   "label": "Verify",   "state": "waiting", "detail": "", "attempt": 1},
]

def _fire(line, attempt_counter):
    """Simulate one log line through the state machine logic."""
    # Call the same logic as _capture_log's state machine section.
    # This requires extracting the state machine to a testable function — see
    # Stage 1e note on refactoring _capture_log.
    _app._process_step_log(line, attempt_counter)

def test_compress_start():
    _reset_steps(BASE_STEPS)
    cnt = [0]
    _fire("Anime mode: compressing then remuxing to MP4.", cnt)
    assert _get_step("compress")["state"] == "running"

def test_remux_after_compress():
    _reset_steps(BASE_STEPS)
    cnt = [0]
    _fire("Anime mode: compressing then remuxing to MP4.", cnt)
    _fire("Remuxing compressed output to MP4...", cnt)
    assert _get_step("compress")["state"] == "done"
    assert _get_step("remux")["state"] == "running"
    assert _get_step("remux")["attempt"] == 1

def test_dts_retry():
    _reset_steps(BASE_STEPS)
    cnt = [1]   # remux already at attempt 1
    _fire("Remuxing to MP4...", cnt)
    _fire("DTS overflow detected — retrying with -max_interleave_delta 0", cnt)
    assert _get_step("remux")["state"] == "retry"
    assert _get_step("remux")["attempt"] == 2

def test_done():
    _reset_steps(BASE_STEPS)
    cnt = [1]
    _fire("Done. Saved 59.0 MB → /some/path.mp4", cnt)
    assert _get_step("remux")["state"] == "done"
    assert _get_step("verify")["state"] == "done"
```

**Requires**: extracting the step-state-machine logic from inside `_capture_log`
into a module-level function `_process_step_log(line, attempt_counter)` so it
can be called from tests without running a full worker thread.

**Pass criterion**: all 4 tests green.

### Test 3: `/api/status` includes new fields

File: `VideoConverter/tests/test_api_status_fields.py`

```python
"""Confirm /api/status always includes the new structured fields."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import app as _app
import json

client = _app.app.test_client()

def test_idle_status_has_phase():
    r = client.get('/api/status')
    data = json.loads(r.data)
    assert 'phase'     in data
    assert 'ocr_batch' in data
    assert 'steps'     in data
    assert data['phase'] == ''
    assert data['steps'] == []

def test_ocr_batch_structure():
    with _app._job_lock:
        _app._job['phase'] = 'ocr_batch'
        _app._job['ocr_batch'] = {
            'total': 3, 'done': 1, 'current_file': 'foo.mkv',
            'files': [
                {'name': 'a.mkv', 'state': 'done'},
                {'name': 'foo.mkv', 'state': 'running'},
                {'name': 'b.mkv', 'state': 'waiting'},
            ]
        }
    r = client.get('/api/status')
    data = json.loads(r.data)
    assert data['phase'] == 'ocr_batch'
    assert data['ocr_batch']['total'] == 3
    assert len(data['ocr_batch']['files']) == 3
    # Reset
    with _app._job_lock:
        _app._job['phase'] = ''
        _app._job['ocr_batch'] = {'total':0,'done':0,'current_file':'','files':[]}
```

**Pass criterion**: both assertions pass.

### Test 4: Visual smoke test (manual)

With the server running:
1. Scan the `_Ebiten` folder (12 episodes, all H.264 Hi10, ASS subs, no PGS)
2. Start conversion
3. **Expected**: OCR batch section shows 12 files as "waiting"; immediately all
   transition to "No PGS" / skipped since no PGS tracks
4. First file begins: step list shows OCR (skipped), Audio, Remux, Verify
5. Remux running: `▶ Remux · attempt 1/6` animates
6. File done: all steps show ✓, next file loads
7. Scan the `_Master of Martial Hearts` folder (AV1 + Opus + ASS)
8. Start conversion; step list shows OCR (skipped/No PGS), Audio, Remux (AV1 stream-copy)
9. Remux completes very fast (stream copy); ✓ across the board

---

## Implementation Order

```
Stage 1a  →  1b  →  1c  →  1d  →  1e
       ↓                         ↓
    Test 3                    Test 1 + 2
       ↓
Stage 2  →  Stage 3  →  Stage 4  →  Stage 5
                                         ↓
                                      Test 4
```

Stages 1 and 2 can be committed and tested independently (the old Progress card
still works; the new fields are just additive to `/api/status`).

Stages 3–5 are one atomic commit (HTML + CSS + JS must land together or the page
breaks).

---

## Refactoring note for testability

The step-state-machine logic inside `_capture_log` (Stage 1e) must be extracted
to a standalone function:

```python
def _process_step_log(msg: str, attempt_counter: list) -> None:
    """
    Given a log line from converter.py and a mutable single-element list
    [attempt_number], update _job["steps"] under the lock.
    Called from _capture_log; also callable from tests.
    """
    # ... all the if/elif logic ...
```

`_capture_log` then becomes:
```python
def _capture_log(msg: str) -> None:
    _file_log.append(msg)
    _job_log(msg)
    _process_step_log(msg, _REMUX_ATTEMPT)
```

---

## Files changed summary

| File | Changes |
|------|---------|
| `app.py` | `_job` dict (3 new keys), `_step()`, `_set_phase()`, `_build_steps()`, `_process_step_log()`, OCR batch state wiring, file-start step init, reset on idle/start |
| `templates/index.html` | Replace Progress card HTML |
| `static/style.css` | `.step-list`, `.step-*`, `.ocr-batch-file` rules + light-theme overrides |
| `static/app.js` | `_stepIcon()`, `_esc()`, `_renderCurrentJob()`, wire into `_pollStatus`, remove old `currentFilename` ref |
| `tests/test_current_job_steps.py` | new |
| `tests/test_step_state_machine.py` | new |
| `tests/test_api_status_fields.py` | new |
