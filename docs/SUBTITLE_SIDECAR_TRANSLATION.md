# Subtitle Sidecar Translation

Create English subtitle sidecars (`.en.srt`) from MKV subtitle tracks.

## What It Does

- Scans a folder for source MKV files.
- Extracts the first subtitle stream from each source file.
- Translates subtitle text from French to English.
- Writes only sidecar subtitle files named:
  - `<video name>.en.srt`

## What It Does Not Do

- No `.ass` outputs
- No remuxed `.mkv` outputs
- No permanent intermediate files

## Prerequisites

- `ffmpeg` available on `PATH`
- Python virtual environment in this repo
- Python packages:
  - `deep-translator`

Install package if needed:

```powershell
c:/VideoTools/.venv/Scripts/python.exe -m pip install deep-translator
```

## Run

```powershell
c:/VideoTools/.venv/Scripts/python.exe c:/VideoTools/translate_ass_batch.py "C:\Path\To\Folder"
```

If folder is omitted, the script uses its built-in default folder.

## Notes

- The script ignores files with `.with-eng` in the name when choosing source MKVs.
- If your player has issues with embedded subtitles, use the generated `.en.srt` sidecar directly.
