# Folder Analysis Script — Spec

## Purpose

Identify the best **conversion targets** within a top-level folder.

A "good target" is a subfolder where:
- Files already converted show **high savings %**
- There are **many files still pending** conversion
- Conversions run **quickly** relative to source duration (good throughput)

The idea: videos in the same subfolder are usually encoded the same way (same
source, same codec, similar bitrate), so one converted file is a reliable
predictor for the rest.

---

## Script

**File:** `VideoConverter/analyse_folders.py`  
**Usage:**

```
python analyse_folders.py <root_folder> [options]
```

**Arguments:**

| Argument | Default | Description |
|----------|---------|-------------|
| `root_folder` | *(required)* | Top-level path to analyse (e.g. `F:\Videos`) — acts as a filter only |
| `--min-done N` | `1` | Minimum converted (`done`) files required to include a folder |
| `--min-pending N` | `1` | Minimum pending files required to include a folder |
| `--top N` | `25` | Show top N results |
| `--sort [score|savings|speed|pending]` | `score` | Sort column |

---

## Data Source

SQLite DB at `VideoConverter/conversions.db`, table `conversions`.

Relevant columns:

| Column | Used for |
|--------|----------|
| `source_path` | Grouping by subfolder |
| `status` | `done` = converted; `pending` = remaining opportunity |
| `saved_mb` | Actual MB saved (done files) |
| `saved_pct` | Actual savings % (done files) |
| `source_size_mb` | For estimating pending savings |
| `source_duration_secs` | For encode speed calculation |
| `started_at` / `completed_at` | Wall-clock encode time (ISO timestamp strings) |
| `source_codec` | Informational |
| `est_saving_pct` / `est_saving_mb` | Estimated savings (from Estimate All pass) |

---

## Grouping Logic

Each unique **directory** that contains video files is its own analysis unit.
Group by `dirname(source_path)` — every folder is examined in isolation.

The `root_folder` argument is a **filter only**: include any folder whose path
starts with the normalised root. No merging of subfolders.

Example structure:
```
F:/Videos/Action/Die Hard/
F:/Videos/Action/Terminator/
F:/Videos/Comedy/Airplane/
```
→ Three rows, one per folder. No "Action" aggregate row.

- Normalise all paths to forward slashes, lowercase for comparison on Windows.
- `root_folder` with no trailing slash: match any path that starts with `root/`.
- The output "Folder" column shows the path **relative to root_folder**, truncated
  if needed.

---

## Per-Subfolder Metrics

For each subfolder group:

### From `done` records (where `saved_pct IS NOT NULL`)

| Metric | Calculation |
|--------|-------------|
| `done_count` | `COUNT(*)` where `status = 'done'` |
| `avg_savings_pct` | `AVG(saved_pct)` |
| `total_saved_mb` | `SUM(saved_mb)` |
| `avg_encode_speed` | See below |

**Encode speed** (×realtime ratio):  
Only for done records where `source_duration_secs IS NOT NULL` and both
`started_at` and `completed_at` are non-null.

```
wall_secs = (completed_at - started_at) in seconds
speed_ratio = source_duration_secs / wall_secs
```

A speed of `3.0` means ffmpeg encoded 3 seconds of video per second of wall
time. Average across all qualifying done records in the group.

### From `pending` records

| Metric | Calculation |
|--------|-------------|
| `pending_count` | `COUNT(*)` where `status IN ('pending', 'failed')` |
| `pending_source_mb` | `SUM(source_size_mb)` for pending records |

### Derived

| Metric | Calculation | Notes |
|--------|-------------|-------|
| `est_additional_mb` | `avg_savings_pct / 100 × pending_source_mb` | Estimated MB recoverable if all pending converted |
| `priority_score` | `est_additional_mb × clamp(avg_encode_speed / 2.0, 0.5, 3.0)` | Weighted by encode throughput. Speed factor capped so outliers don't dominate. If no speed data, factor = 1.0 |

---

## Output Format

Rich table (via `rich` library, already available in venv), printed to stdout.

Columns (in order):

```
Subfolder | Done | Pending | Avg Save% | Encode Speed | Saved So Far | Est. Additional | Score
```

- `Subfolder`: truncated to 40 chars if needed, relative to root
- `Done`: integer
- `Pending`: integer, highlighted green if > 50
- `Avg Save%`: integer %, colour-coded (≥40% green, 20-39% yellow, <20% dim)
- `Encode Speed`: `2.3×` format, or `—` if no data
- `Saved So Far`: `1.2 GB` or `450 MB`
- `Est. Additional`: `3.4 GB` or `850 MB`, bold for top entries
- `Score`: float, used for sorting

Sorted by `priority_score` descending by default.

Footer line: total pending files, total est. additional savings across all shown subfolders.

---

## Inclusion Criteria (Filters)

A subfolder is **included** only if:
1. `done_count >= --min-done` (default 1) — at least one completed conversion
2. `pending_count >= --min-pending` (default 1) — at least one file left to convert
3. The subfolder path starts with the given `root_folder`

Subfolders where all files are `done`, `skipped`, `low_savings`, or `no_saving`
are excluded (no remaining opportunity).

---

## Example Output

```
Folder Analysis — F:\Videos  (342 folders analysed, 89 with remaining opportunity)

 Folder                                    Done  Pending  Avg Save%  Speed   Saved    Est. Add.   Score
 ────────────────────────────────────────────────────────────────────────────────────────────────────
 Sweetie Fox {Social, Teen, Blonde}/          1      47      44%     2.1×    0.8 GB    16.2 GB    34.0
 Naughty America - SiteRip 2024/              8      312      38%     3.2×    9.4 GB   101.3 GB   322.2
 Action/Die Hard Collection/                  3       22      51%     2.9×    2.1 GB     8.7 GB    25.2
 Comedy/Airplane/                             2        5      29%     1.8×    0.4 GB     1.1 GB     2.0
 ...

 Total: 386 pending files  |  Est. 127.3 GB additional savings
```

---

## Notes / Assumptions

- `low_savings` and `no_saving` files are **excluded** from pending counts — they
  have already been assessed and won't yield savings.
- `failed` files are **included** in pending (they may succeed on retry).
- If `avg_savings_pct` cannot be computed (no done files with `saved_pct` data),
  the subfolder is excluded.
- If `est_saving_pct` is available on pending records (from Estimate All pass),
  use `AVG(est_saving_pct)` for those files instead of propagating the done-file
  average — this gives a more accurate per-subfolder estimate.
- Path comparison is case-insensitive on Windows.

---

## Implementation Steps

1. Parse args with `argparse`
2. Open DB with `sqlite3` (direct, no import of `db.py` needed for a standalone script)
3. `SELECT source_path, status, saved_mb, saved_pct, source_size_mb, source_duration_secs, started_at, completed_at, est_saving_pct FROM conversions`
4. Filter to paths under root; group in Python by `posixpath.dirname(source_path)`
5. Compute metrics per group, filter by min-done / min-pending
6. Sort, render with `rich.table`
