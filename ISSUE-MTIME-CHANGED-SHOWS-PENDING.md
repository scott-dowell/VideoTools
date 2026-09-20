# Issue: Files with mtime changes show as pending despite being done

## Problem
When a file has `status='done'` in the database but its filesystem mtime has changed (e.g., re-downloaded), the file appears as **"pending"** in the grid instead of **"done"**.

Additionally, these files are hashed on every scan, indicating the database mtime is never updated.

## Example
Sakurasou folder:
- S01E01-E18: `status='done'`, DB mtime ≠ filesystem mtime → **show as pending** (wrong!)
- S01E19-E24: `status='pending'`, DB mtime = filesystem mtime → **show as pending** (correct)

Expected behavior:
- S01E01-E18 should show as **done** and NOT require hashing on subsequent scans
- Once hash-verified as same file, DB mtime should sync to filesystem mtime

## Root Cause
The scanner's Phase 1 batch lookup (`get_latest_statuses_by_paths`) returns records by MAX(id) per path. When a file's mtime changes without creating a new record, the query still returns the old "done" record, but with the old mtime. The Phase 1 code then displays these as "done", which is correct. However, there's a disconnect somewhere that's causing them to appear as "pending" in the grid.

## Solution
Need to implement mtime-based file identity verification:
1. In Phase 1, detect when done/low_savings/no_saving records have mismatched mtime
2. Queue these for hash verification in Phase 2
3. In Phase 2, when hash matches, update the DB mtime
4. Subsequent scans will find the file directly via batch lookup with matching mtime
