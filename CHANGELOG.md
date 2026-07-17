# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog.

## [Unreleased]

- #4 Normalize changelog format to one bullet line per issue (remove Added/Changed/Fixed grouping).
- #6 Improve savings accuracy by using per-segment source-vs-encoded estimator samples, lower HEVC fast-skip to 500 kbps (normalized), update OCR prepass row chips per-file as each OCR result completes, mark rows as OCR-done at scan time when prior .pgs*.srt sidecars already exist, and remove OCR-generated .pgsN.srt sidecars after successful remux.

## [0.0.001] - 2026-06-13

- #1 Add root changelog with [Unreleased] section and initial project change tracking.
- #2 Adopt versioned release structure and cut first release tag 0.0.001.
- #3 Start in-app versioning at 0.0.001 and show current version in the navbar UI.
- Baseline release bundle (pre-issue breakdown): queue filtered x/y header count, backend/frontend savings status field alignment, subtitle preview support, failed-conversion review tooling, and related test coverage updates.

[Unreleased]: https://github.com/scott-dowell/VideoTools/compare/0.0.001...HEAD
[0.0.001]: https://github.com/scott-dowell/VideoTools/releases/tag/0.0.001
