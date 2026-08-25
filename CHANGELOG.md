# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog.

## [Unreleased]

- #29 Fix repeated probing of unchanged pending files by skipping hash/probe when complete probe metadata is already cached for the latest DB record.
- #28 Batch hash-phase DB lookups and coalesce hash progress/row UI updates so hash matching remains accurate without per-file DB round-trips and constant queue repaint churn.
- #27 Add per-folder scan timing telemetry, remove phase-1 per-file output/fingerprint DB fallback lookups, and switch scan-strip indeterminate animation to a smooth shimmer to avoid bouncing during large-folder scans.
- #26 Smooth phase-1 scan-strip updates by showing the currently scanned folder from scan-progress events and throttling label/animation resets to reduce jerky UI motion.
- #25 Speed up phase-1 scanning by deferring hash reads to the hash phase, caching per-directory sidecar lookups, and emitting scan-progress heartbeat events so large-folder scans no longer appear to hang.
- #24 Remove the duplicate inline "Use This Folder" action in the browse list, keep a single footer confirm action, and auto-select the current folder on browse load.
- #23 Keep Controls-card folder actions enabled for remembered valid paths and disable them only when the selected path is missing or invalid.
- #22 Make the folder browser modal selection-only and move Scan/Load/Clean up/Analyse/Prep actions into the Controls card so folder selection and execution are separated.
- #21 Retry anime-mode conversions without subtitle streams when muxing produces a truncated output, and add a regression test for the subtitle-drop fallback.
- #20 Fix sidecar subtitle translation reliability by adding provider fallback and fail-fast checks so `.en.srt` generation no longer silently writes untranslated French output.
- #19 Simplify subtitle translation workflow to produce only `.en.srt` sidecars from source MKV subtitle tracks, plus a quick usage doc for repeatable runs.
- #18 Wire batch accept/discard endpoints into the Details modal UI with collapsible results panel, per-file action controls, and client handlers for single-file and batch-wide operations.
- #17 Add batch preview discard/accept-all actions with shared single-file commit/discard helpers, persisted replace-state transitions, and mixed-result failure reporting for batch replacements.
- #16 Add phase-1 batch preview orchestration with per-plan background build/status APIs, persisted preview state transitions, selector-resolution-based per-file apply behavior, and Details modal batch workflow wiring.
- #15 Implement phase-1 batch workflow foundation: persisted batch plan/file-state tables, stream-signature matching helpers, and initial create/get batch plan APIs with unit and route coverage.
- #14 Require phase-by-phase unit test gates in the batch workflow plan, blocking progression to the next phase until current phase tests pass.
- #13 Add a reviewed implementation checklist for multi-file Video Details batch workflow with preview-first apply-same-edits, persistent batch states, and explicit replacement safety constraints.
- #12 Move the Video Details preview actions into a dedicated workflow section and make English stereo test copies keep the other audio tracks while replacing the first English track with a stereo test track.
- #11 Recognize hash-matched done records during scans, clean up shadow pending rows for that path, and update done-row probe metadata in place so converted files do not reappear as pending.
- #4 Normalize changelog format to one bullet line per issue (remove Added/Changed/Fixed grouping).
- #5 Refresh DB/UI metadata immediately after stream-edit accepts (including Output/Saved/%), fail fast on metadata-sync errors, and add subtitle track count as an S column in the queue table.
- #7 Compact the Video Details modal using two-column Video Stream/File sections and auto-probe stream metadata on modal open for faster track editing.
- #8 Make Video Details top fields single-line label/value rows and tighten card spacing so the Video Stream/File sections use less vertical space while staying readable.
- #9 Backfill missing track metadata during folder-browser scans by probing files with incomplete track counts and persisting subtitle track counts so queue rows populate without manual Probe.
- #10 Add queue sorting by V/A/S track counts, including toolbar sort options and Settings default-sort support for those keys.
- #6 Improve savings accuracy by using per-segment source-vs-encoded estimator samples, lower HEVC fast-skip to 500 kbps (normalized), update OCR prepass row chips per-file as each OCR result completes, mark rows as OCR-done at scan time when prior .pgs*.srt sidecars already exist, and remove OCR-generated .pgsN.srt sidecars after successful remux.

## [0.0.001] - 2026-06-13

- #1 Add root changelog with [Unreleased] section and initial project change tracking.
- #2 Adopt versioned release structure and cut first release tag 0.0.001.
- #3 Start in-app versioning at 0.0.001 and show current version in the navbar UI.
- Baseline release bundle (pre-issue breakdown): queue filtered x/y header count, backend/frontend savings status field alignment, subtitle preview support, failed-conversion review tooling, and related test coverage updates.

[Unreleased]: https://github.com/scott-dowell/VideoTools/compare/0.0.001...HEAD
[0.0.001]: https://github.com/scott-dowell/VideoTools/releases/tag/0.0.001
