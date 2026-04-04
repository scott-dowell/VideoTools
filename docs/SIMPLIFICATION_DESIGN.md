# convert_videos.py — Simplification Design

## Problem Statement

The current code has grown into a single monolithic pipeline that attempts to handle anime  
(multiple audio tracks, multiple subtitle tracks, bitmap PGS subs, TrueHD/DTS audio, MKV  
containers) and normal videos (single audio track, no subs, already MP4 or simple MKV) with  
the same code path. The result is ~3700 lines of complexity that is almost entirely driven by  
anime edge-cases, applied to every video regardless of whether it matters.

---

## Two Clear Use Cases

### Normal mode (default, checkbox OFF)

Goal: **compress the video stream to HEVC, touch nothing else.**

- Stream-copy all audio tracks as-is
- Stream-copy all subtitle tracks as-is
- Output container: MP4 if the source is already MP4/M4V, otherwise MKV — ensures audio  
  copy always works (MKV accepts any codec)
- Size check: if the output is not smaller than the source, discard it and keep the original
- Single FFmpeg call, no probing beyond is-already-HEVC check

### Anime mode (checkbox ON)

Goal: **produce a clean, universally-compatible MP4 with a single English audio track and  
readable subtitles.**

- Remux to MP4 container (always)
- Re-encode audio to AAC (MP4 container requires it for DTS/TrueHD/EAC3 sources)
- Convert text subs (ASS/SRT) to mov_text; OCR bitmap subs (PGS/VOBSUB) to mov_text  
- Drop duplicate or non-English tracks (using the existing stream-filtering logic)
- QSV compress video after the remux step (existing `_attempt_qsv_compress`)
- All existing retry, DTS-crash-recovery, and subtitle-safety logic stays

---

## Proposed Code Structure

### Functions to KEEP unchanged
| Function | Used by |
|---|---|
| `_local_temp_convert` | Both modes — staging to local SSD |
| `_run_remux_cmd` | Anime mode and QSV compress |
| `_build_qsv_cmd` / `_build_sw_hevc_cmd` | Both modes |
| `_run_compress_with_fallback` | Both modes |
| `_attempt_qsv_compress` | Anime mode post-remux |
| `is_video_hevc` | Both modes |
| `is_video_10bit_h264` | Anime mode |
| `remux_to_mp4` | Anime mode only |
| `convert_video` | Anime mode only |
| `validate_audio_subtitle_preservation` | Anime mode only |
| `_force_mp4_output_filename` / `_run_ocr_prepass` | Anime mode only |

### New function: `compress_simple(input_path, output_path)`

Replaces the entire normal-mode path. Single responsibility: compress video to HEVC,  
copy everything else, keep the smaller file.

```
1. ffprobe: check if already HEVC → skip if so
2. Decide output extension:
   - source is .mp4 or .m4v  → output is .mp4
   - anything else (mkv etc) → output is .mkv  (guarantees -c:a copy always works)
3. Build FFmpeg command:
     ffmpeg -y -i <input>
       -c:v hevc_qsv -global_quality 30 -preset medium -tag:v hvc1
       -c:a copy
       -c:s copy
       -map 0:v:0 -map 0:a? -map 0:s?
       -max_muxing_queue_size 9999
       <output>
4. If QSV fails → software fallback: libx265 -crf 28
5. Size check: if output >= source → delete output, log "kept original", return False
6. If smaller → delete source, return True
```

### Processing loop changes

Currently the loop has two branches: HEVC files (→ `remux_to_mp4`) and non-HEVC files  
(→ `convert_video`). With the new design:

```
if anime_mode:
    # existing behaviour — remux_to_mp4, convert_video, full pipeline
else:
    # simple path — compress_simple for everything
    # already-HEVC files still need to be checked and skipped
```

The HEVC-already check in normal mode just logs and skips (no remux, no QSV, leave as-is).

---

## UI Change

Remove the current checkbox:
> "Keep all audio/subtitle tracks (convert video only)"

Replace with:
> "Anime mode (normalise tracks, remux to MP4)"  — default OFF

The variable rename: `enable_keep_all_streams` → `anime_mode`

---

## What Gets Deleted

Once the normal path uses `compress_simple`, the following are only reached when `anime_mode`  
is True and can be clearly labelled as such (or deleted if they have no callers outside that  
path after the refactor):

- `validate_audio_subtitle_preservation` — only meaningful when tracks are being filtered
- The full `remux_to_mp4` fast-path (currently duplicates compress_simple logic)
- The skip-remux fast-path block inside `remux_to_mp4` (superseded by compress_simple)
- `_force_mp4_output_filename` / `has_bitmap_subtitles` / OCR prepass — anime only
- `enable_keep_all_streams` references in `convert_video` and conservative path

Rough estimate: **400–600 lines** removed from the hot path.

---

## Migration / Risk

- Normal mode is a new code path — no existing behaviour changes for anime mode
- `_local_temp_convert` still wraps `compress_simple`, so cloud-drive staging is preserved
- The size check in `compress_simple` means there is zero risk of making a file larger
- Already-HEVC files in normal mode: just skip (log "already HEVC, skipping") — no remux,  
  no QSV (they're already compressed). If you want to recontainer an already-HEVC MKV to MP4  
  for normal videos, that's a separate decision and can be added later.

---

## Questions to Resolve Before Implementation

1. **Already-HEVC in normal mode**: ✅ Always run QSV compression — many HEVC files are  
   poorly compressed and will shrink significantly. Size check still applies: discard output  
   if not smaller than source.

2. **Already-HEVC MKV in normal mode**: ✅ No remux — QSV compress in-place, output stays  
   MKV. Remux to MP4 is anime mode only.

3. **`validate_audio_subtitle_preservation`**: ✅ Keep, gated behind anime mode only. Not  
   called in normal mode (tracks are copied unchanged, nothing to validate).

---

## Job State Database (decided during UI build — April 2026)

Use **Flask-SQLAlchemy with SQLite** (same pattern as SimpleMoney) to persist job state.

Why:
- In-memory state is lost if the server restarts mid-queue
- A persistent DB gives free job history, restartability, and makes `/api/status` trivial
- SQLAlchemy removes all raw SQL; route handlers stay 5–15 lines each
- SQLite needs zero infrastructure — single file on disk

Proposed models:

```
Job
  id          Integer PK
  root_path   String        — the folder that was scanned
  anime_mode  Boolean
  created_at  DateTime
  status      String        — idle | running | paused | done

FileEntry
  id            Integer PK
  job_id        Integer FK → Job
  folder        String        — relative subfolder (empty = root)
  filename      String
  full_path     String
  size_bytes    Integer
  codec         String
  duration_secs Integer
  status        String        — pending | converting | done | skipped | failed
  output_bytes  Integer nullable
  exit_code     Integer nullable
  ffmpeg_cmd    Text nullable  — exact command run (for reproduction)
  error_tail    Text nullable  — last 50 lines of stderr, set on failure only
  started_at    DateTime nullable
  finished_at   DateTime nullable
```

Add `Flask-SQLAlchemy` to `requirements.txt` when implementing the backend.

---

## Failure Diagnostics / Logging Strategy (decided April 2026)

**Problem:** FFmpeg failures are hard to diagnose after the fact — the Flask log scrolls away
and once the server restarts the error is gone entirely.

**Decision: hybrid approach — structured summary in DB, full output on disk.**

### What the DB stores (on `FileEntry`)

| Column | Type | Notes |
|---|---|---|
| `ffmpeg_cmd` | Text | Exact command string — allows manual reproduction |
| `error_tail` | Text | Last 50 lines of FFmpeg stderr, captured **only on failure** |
| `exit_code` | Integer | FFmpeg process exit code |

Storing only the tail keeps the DB lightweight. The actual error is always in the last few
lines — the thousands of frame-counter lines before it are not useful to store.

### Full log file on disk

Every encode writes its complete FFmpeg stderr to:

```
C:\Temp\vc_working\logs\<filename_stem>_<YYYYMMDD_HHMMSS>.log
```

This file is not cleaned up automatically and is available for deep diagnosis (frame-by-frame
bitrate, QSV init messages, etc.). If the temp dir is wiped, the DB `error_tail` still has
the critical lines.

### UI surface

- Failed rows in the queue table get a **"View Log"** button (small, in the Status cell or
  as a row action)
- Clicking opens a modal showing:
  - The FFmpeg command
  - The `error_tail` (monospace, scrollable)
  - A note with the full log file path for reference
- No digging through files needed for 99% of failure diagnosis
