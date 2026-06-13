# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog.

## [Unreleased]

- Tracking issue: #2

- No changes yet.

## [0.0.001] - 2026-06-13

### Added
- Started app versioning at 0.0.001 and surfaced the current version in the navbar UI.
- Added queue filtered count display in the Video Queue header (x/y when filters are active).
- Added backend status savings fields for session and current file values:
  - session_realized_mb
  - session_in_progress_est_mb
  - current_file_saved_mb
  - current_file_est_mb
- Added subtitle preview API support and subtitle language/payload helper logic.
- Added failed conversion review utility script: VideoConverter/review_failed_converts.py.
- Added prompt file for failed conversion review workflows under .github/prompts.

### Changed
- Updated frontend status card calculations to consume backend-provided savings fields.
- Improved conversion fallback/failure reporting and status messaging in conversion flow.
- Updated scanner, routes, and API-related behavior to align with new status and subtitle handling.
- Expanded and updated automated tests across API, scanner, converter, OCR, cleanup, routes, and e2e coverage.

### Fixed
- Improved consistency of live savings numbers shown during active conversion.

[Unreleased]: https://github.com/scott-dowell/VideoTools/compare/0.0.001...HEAD
[0.0.001]: https://github.com/scott-dowell/VideoTools/releases/tag/0.0.001
