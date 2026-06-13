# VideoTools

Video processing tools for Windows.

## Projects

- **VideoConverter** — Compress video files to HEVC using Intel QSV (with libx265 fallback). Flask-based UI.

## Setup

```bash
cd VideoConverter
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
python app.py
```

Then open http://localhost:5001

## Failed Conversion Review Workflow

To retain failed intermediate artifacts for manual review, enable this in
`VideoConverter/settings.json`:

```json
"keep_failed_intermediates": true
```

When enabled, failed temp artifacts are moved under:

`C:\\Temp\\vc_working\\_failed_intermediates\\<file>_<timestamp>\\`

You can run a quick review report of recent failures with:

```bash
python VideoConverter/review_failed_converts.py --since-hours 72 --limit 20
```

There is also a reusable Copilot prompt for this workflow:

`.github/prompts/failed-convert-review.prompt.md`
