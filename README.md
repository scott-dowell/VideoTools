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
