# Conversion Progress UX — Design Options

Current date: 2026-05-09  
Author: design session

---

## The Problem

The right panel currently shows:
- A single progress bar (file %)
- A second bar (overall %)
- fps / ETA / saved (three numbers)
- File elapsed
- A raw log box that mixes scan noise, OCR output, ffmpeg progress lines,
  DTS retry warnings, and everything else into one undifferentiated stream

You cannot tell from the UI:
- Which *step* of the pipeline is currently executing
- Whether OCR is running or done, and for which file
- Whether the encoder is compressing or remuxing (two different phases in anime mode)
- Which attempt (1–6) the DTS retry loop is on
- What the *planned* steps are for the current file (so you can anticipate wait time)
- Whether a step succeeded or is being retried

---

## Pipeline Steps (per file, anime mode)

The steps vary by file type. Here's the full matrix:

| Step              | H.264 normal | Hi10 H.264 | HEVC    | AV1     | H.264 non-anime |
|-------------------|-------------|------------|---------|---------|-----------------|
| OCR (PGS subs)    | maybe       | maybe      | maybe   | maybe   | —               |
| Audio pre-encode  | maybe       | maybe      | maybe   | maybe   | —               |
| Compress (QSV)    | ✓           | —          | —       | —       | ✓               |
| Remux → MP4       | ✓           | ✓          | ✓       | ✓       | —               |
| Verify            | ✓           | ✓          | ✓       | ✓       | ✓               |

"maybe" = only if the file has those stream types.

Steps the backend already logs (in raw form):
- `[ocr]` lines from the OCR batch
- `"Anime mode: compressing then remuxing to MP4."`
- `"Remuxing compressed output to MP4..."` / `"Remuxing to MP4..."`
- `"AAC mux failed — pre-encoding audio tracks individually..."`
- `"DTS overflow detected — retrying with..."` / attempt 2, 3 labels
- `"Integrity check failed..."` / pass
- `"Done. Saved X MB →"`

So structured step state *can* be inferred from existing log lines without adding new
backend API fields, though a proper structured approach is better long-term.

---

## Option A — Upgraded "Current Job" Card (replace Progress card)

**Concept:** The Progress card in the right panel becomes a richer "Current Job" card.
The table stays completely unchanged. The log stays below as the scrolling history.

This is the least invasive option and fits naturally into the existing layout.

```
┌─────────────────────────────────────────────────────┐
│ ⚙  Current Job                                      │
├─────────────────────────────────────────────────────┤
│ Ebiten - 07 [F2F0F259].mkv           1 of 12        │
│ ████████████████████░░░░░░░░░░░░░  52%  ·  6m 12s  │
│                                                      │
│  Steps                                               │
│  ✓  OCR          PGS bitmap → SRT   [done]          │
│  ✓  Audio prep   2 tracks, AAC copy [done]          │
│  ▶  Compress     hevc_qsv · 52%     [running]       │
│  ○  Remux        MKV → MP4          [waiting]       │
│  ○  Verify       duration + tracks  [waiting]       │
│                                                      │
│  fps 147  ·  ETA 5m 44s  ·  saved 12.3 GB           │
│  ▸ frame=18340 fps=147 q=25.0 size=38MB time=...    │
└─────────────────────────────────────────────────────┘
```

Step states:
- `✓` green  — completed OK
- `▶` amber  — currently running (pulses/animates)
- `✗` red    — failed / retrying
- `↺` yellow — retrying (shows attempt number)
- `○` gray   — not yet started
- `—` dim    — not applicable for this file type

**Pros:**
- No table changes
- Fits exactly where the current Progress card sits
- Both phases of anime mode (compress + remux) are clearly separated
- OCR visible without digging through the log
- Retry state surfaced explicitly

**Cons:**
- Needs the backend to emit structured step events (or clever log parsing in JS)
- Card gets taller; may push the log box down

---

## Option B — Table Row Accordion (expand active row)

**Concept:** When a file starts converting, its table row expands inline to show
the task list and a mini progress bar. Completed rows collapse back to normal.

```
┌──┬──────────┬────────────────────────┬──────┬──────┬────────┐
│  │ Folder   │ Filename               │ Size │ Btr  │ Status │
├──┼──────────┼────────────────────────┼──────┼──────┼────────┤
│  │ _Ebiten  │ Ebiten - 01.mkv        │ 824  │ 4420 │ done   │
│  │ _Ebiten  │ Ebiten - 02.mkv        │ 811  │ 4350 │ done   │
├──┴──────────┴────────────────────────┴──────┴──────┴────────┤ ← expanded
│ ▶ Ebiten - 07 [F2F0F259].mkv                                │
│   ████████████████████░░░░░░░  52%                          │
│   ✓ OCR  ✓ Audio  ▶ Compress (hevc_qsv · 147fps)  ○ Remux  │
│                                                              │
├──┬──────────┬────────────────────────┬──────┬──────┬────────┤
│  │ _Ebiten  │ Ebiten - 08.mkv        │ 798  │ 4280 │ pending│
└──┴──────────┴────────────────────────┴──────┴──────┴────────┘
```

**Pros:**
- Context-in-place — you see the converting file exactly where it is in the queue
- After collapse, the row looks like all the others

**Cons:**
- Table colspan expansion is visually jarring; column alignment breaks
- With lots of columns (14 currently) the expanded row is wide and wasteful
- Hard to scroll — the expanded row may not be visible if the table is long
- More JS complexity for expand/collapse state during live updates

---

## Option C — Detached "Now Converting" Banner

**Concept:** A fixed-position strip between the stat cards and the table.
Hidden when idle; appears and sticks to the top of the queue card when running.

```
╔═══════════════════════════════════════════════════════════════════════╗
║  ▶  Ebiten - 07 [F2F0F259].mkv        [2 of 12]            52%  ↓  ║
║     ✓ OCR  ✓ Audio  ▶ Compress (hevc_qsv · 147fps · ETA 5m)  ○ ...║
╚═══════════════════════════════════════════════════════════════════════╝
```

The ↓ icon collapses it to a single-line slim bar if the user wants to reclaim
space.

**Pros:**
- Always visible regardless of scroll position in the table
- Table and right panel unchanged
- Natural place to expand step detail without redesigning cards

**Cons:**
- Another horizontal strip competes with scan strip and est strip
- Might feel busy if all three strips are showing simultaneously
- Less real estate for step details than Option A

---

## Option D — Per-File Log Accordion in the Log Panel

**Concept:** The log panel organises entries into collapsible groups, one per file.
The active file's group is auto-expanded; completed ones collapse to a summary line.

```
┌──────────────────────────────────────────────┐
│ ▸ Ebiten - 01.mkv   ✓ done  52% saved  3m2s │  ← collapsed
│ ▸ Ebiten - 02.mkv   ✓ done  49% saved  2m58s│  ← collapsed
│                                              │
│ ▼ Ebiten - 07.mkv   ▶ running…             │  ← expanded (auto)
│   [ocr]  Track 0: 434 lines extracted       │
│   [ocr]  Saved Ebiten - 07.pgs0.srt         │
│   Anime mode: compressing then remuxing...  │
│   Remuxing to MP4...                        │
│   ▸ frame=18340 fps=147 q=25.0 ...          │
│                                              │
│ ○ Ebiten - 08.mkv   pending                 │  ← collapsed/future
└──────────────────────────────────────────────┘
```

**Pros:**
- Log entries stay in context with the file they belong to
- Completed files have a persistent searchable record in the UI
- Very clear what happened to each file

**Cons:**
- Log panel is currently flex-grow — it would need virtualised rendering for
  large queues (60+ files × many log lines each)
- Doesn't solve the "what step is running" problem without extra backend work
- Mixing past (completed) and present (running) in one scrolling box is
  still potentially confusing

---

## Recommendation: Option A + structured log events (two-phase impl)

### Phase 1 (pure frontend, no backend changes needed)

Parse the existing raw log lines in `_pollStatus` to infer step state:

| Log substring matched                         | Step implied        | State    |
|-----------------------------------------------|---------------------|----------|
| `[ocr]` any line                              | OCR                 | running  |
| `[ocr]` … `Saved *.srt`                       | OCR                 | done ✓   |
| `[ocr]` … `no OCR output`                     | OCR                 | failed ✗ |
| `"No PGS"` badge already set                  | OCR                 | skipped — |
| `"Anime mode: compressing"`                   | Compress            | running  |
| `"Remuxing compressed output to MP4"`         | Compress → Remux    | done ✓   |
| `"Remuxing to MP4"` (first attempt)           | Remux               | running  |
| `"DTS overflow detected"` / `"retry"`         | Remux               | retrying ↺|
| `"pre-encoding audio"`                        | Audio pre-encode    | running  |
| `"Track N pre-encoded"`                       | Audio pre-encode    | done ✓   |
| `"pre-extracted text subs to SRT"`            | Sub extract         | running  |
| `"Integrity check failed"`                    | Verify              | failed ✗ |
| `"Done. Saved"` final line                    | Verify              | done ✓   |
| `frame=… fps=… speed=`                        | (current step fps)  | live     |

This lets the frontend display accurate step state with zero backend changes.

### Phase 2 (structured backend events, optional later)

Add a `_job["current_step"]` dict emitted on `/api/status`:
```json
{
  "steps": [
    {"id": "ocr",     "label": "OCR",      "state": "done",    "detail": "434 lines"},
    {"id": "audio",   "label": "Audio",    "state": "done",    "detail": "2 tracks copied"},
    {"id": "compress","label": "Compress", "state": "running", "detail": "hevc_qsv"},
    {"id": "remux",   "label": "Remux",    "state": "waiting", "detail": ""},
    {"id": "verify",  "label": "Verify",   "state": "waiting", "detail": ""}
  ]
}
```

The frontend reads this directly instead of parsing log lines.

---

## Current Job Card — detailed mockup (Option A)

### During OCR batch (before individual files start):

```
┌──────────────────────────────────────────────────────┐
│ ⚙  Current Job                                       │
├──────────────────────────────────────────────────────┤
│ OCR pre-pass  (8 files with PGS subtitles)           │
│ █████████░░░░░░░░░░░░░░░░░░  2 of 8                  │
│                                                      │
│  ▶  Ebiten - 07.mkv   extracting frame 2840 / 6120  │
│     Ebiten - 08.mkv   queued                        │
│     Ebiten - 09.mkv   queued                        │
│                                                      │
│  fps —  ·  ETA —  ·  saved —                        │
└──────────────────────────────────────────────────────┘
```

### During compression (anime mode, H.264 source):

```
┌──────────────────────────────────────────────────────┐
│ ⚙  Current Job                         [3 of 12]    │
├──────────────────────────────────────────────────────┤
│ Ebiten - 07 [F2F0F259].mkv                          │
│ ████████████████░░░░░░░░░░░░  48%  ·  ETA 6m 14s   │
│                                                      │
│  ✓  OCR          434 lines → Ebiten-07.pgs0.srt     │
│  ✓  Audio        2 tracks  · AAC copy               │
│  ▶  Compress     hevc_qsv  · 147 fps                │
│  ○  Remux        MKV → MP4                          │
│  ○  Verify                                          │
│                                                      │
│  ▸ frame=18340 fps=147 q=25.0 size=38MB …           │
└──────────────────────────────────────────────────────┘
```

### During remux (DTS retry):

```
┌──────────────────────────────────────────────────────┐
│ ⚙  Current Job                         [3 of 12]    │
├──────────────────────────────────────────────────────┤
│ Ebiten - 07 [F2F0F259].mkv                          │
│ ████████████████████████████  100% (remuxing)       │
│                                                      │
│  ✓  OCR          434 lines → Ebiten-07.pgs0.srt     │
│  ✓  Audio        2 tracks  · AAC copy               │
│  ✓  Compress     hevc_qsv  · done                   │
│  ↺  Remux        attempt 2/6 · -max_interleave 0    │
│  ○  Verify                                          │
│                                                      │
│  ▸ DTS overflow — retrying with -max_interleave_delta 0 │
└──────────────────────────────────────────────────────┘
```

### AV1 stream-copy (no compress step):

```
┌──────────────────────────────────────────────────────┐
│ ⚙  Current Job                         [1 of 5]     │
├──────────────────────────────────────────────────────┤
│ Master of Martial Hearts - 01.mkv                    │
│ ████████████░░░░░░░░░░░░░░░░  38%  ·  ETA 0m 22s   │
│                                                      │
│  ✓  OCR          No PGS tracks                      │
│  ▶  Remux        AV1 stream-copy · MKV → MP4        │
│  ○  Verify                                          │
│                                                      │
│  ▸ frame=7440 fps=2180 q=-1.0 size=292MB …          │
└──────────────────────────────────────────────────────┘
```

### Non-anime mode:

```
┌──────────────────────────────────────────────────────┐
│ ⚙  Current Job                         [7 of 22]    │
├──────────────────────────────────────────────────────┤
│ One.Piece.E1050.mp4                                  │
│ ██████████████░░░░░░░░░░░░░░  61%  ·  ETA 4m 02s   │
│                                                      │
│  ▶  Compress     hevc_qsv  · 94 fps                 │
│  ○  Verify                                          │
│                                                      │
│  ▸ frame=52200 fps=94 q=25.0 size=610MB …           │
└──────────────────────────────────────────────────────┘
```

---

## Implementation notes

### State object (frontend)

```js
let _currentSteps = [];
// [{ id, label, state, detail, attempt }]
// state: 'waiting' | 'running' | 'done' | 'failed' | 'retry' | 'skipped'
```

### Log parsing rules (Phase 1)

Parse each new log line in `_pollStatus` → update `_currentSteps`:

```js
function _inferStepFromLog(line) {
  if (/^\[ocr\]/.test(line)) _setStep('ocr', 'running', line.replace(/^\[ocr\]\s*/,''));
  if (/Saved .+\.srt/.test(line)) _setStep('ocr', 'done', '');
  if (/No PGS tracks|No active PGS/.test(line)) _setStep('ocr', 'skipped', 'No PGS');
  if (/pre-encoding audio/.test(line)) _setStep('audio', 'running', '');
  if (/Track \d+ pre-encoded/.test(line)) _setStep('audio', 'done', '');
  if (/compressing then remuxing/.test(line)) _setStep('compress', 'running', '');
  if (/Remuxing compressed output/.test(line)) { _setStep('compress','done',''); _setStep('remux','running',''); }
  if (/^Remuxing to MP4/.test(line)) _setStep('remux', 'running', 'attempt 1');
  if (/DTS overflow|retrying with/.test(line)) _setStep('remux', 'retry', line);
  if (/Integrity check failed/.test(line)) _setStep('verify', 'failed', line);
  if (/^Done\. Saved/.test(line)) _setStep('verify', 'done', '');
}
```

### CSS for step list

```css
.step-list { list-style:none; padding:0; margin:.5rem 0 0; font-size:.8rem; }
.step-list li { display:flex; gap:.5rem; align-items:baseline; padding:.15rem 0; }
.step-icon-done    { color:#3fb950; }
.step-icon-running { color:#d29922; animation: pulse 1s ease-in-out infinite; }
.step-icon-retry   { color:#f0883e; }
.step-icon-failed  { color:#f85149; }
.step-icon-waiting { color:#484f58; }
.step-icon-skipped { color:#484f58; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
```

---

## Open questions for review

1. **OCR phase**: The OCR batch runs *before* individual file processing. Should the
   "Current Job" card have a distinct "OCR batch" mode that shows all files being
   processed (with a mini per-file progress), or should it only show per-file state?

2. **Log panel**: Once we have step detail in the Current Job card, should the Log
   panel filter out OCR/ffmpeg-verbose lines and only show high-level messages?
   Or keep everything and add a "verbose" toggle?

3. **Phase 1 vs Phase 2**: Is log-line parsing (Phase 1) good enough to ship first,
   or should we go straight to structured backend events?

4. **Retry visibility**: Should a DTS retry attempt cause the Remux row to stay
   "running" (hiding the retry) or explicitly show "↺ attempt 2/6" to set
   expectations?
