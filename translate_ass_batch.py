import argparse
import html
import os
import re
import subprocess
import tempfile
from typing import Dict, List, Tuple

from deep_translator import MyMemoryTranslator

TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2},\d{3}\s+-->\s+\d{2}:\d{2}:\d{2},\d{3}$")
HTML_TAG_RE = re.compile(r"<[^>]+>")
ASS_TAG_RE = re.compile(r"\{[^}]*\}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create .en.srt sidecars from MKV subtitles")
    parser.add_argument(
        "folder",
        nargs="?",
        default=r"C:\Users\scott\Downloads\Anime\_Kuttsukiboshi",
        help="Folder containing MKV files",
    )
    return parser.parse_args()


def list_source_mkvs(folder: str) -> List[str]:
    mkvs = []
    for name in sorted(os.listdir(folder)):
        low = name.lower()
        if not low.endswith(".mkv"):
            continue
        if ".with-eng" in low:
            continue
        mkvs.append(os.path.join(folder, name))
    return mkvs


def extract_first_subtitle_to_srt(mkv_path: str, out_srt: str) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-i",
        mkv_path,
        "-map",
        "0:s:0",
        out_srt,
    ]
    subprocess.run(cmd, check=True)


def normalize_text(text: str) -> str:
    t = html.unescape(text)
    t = HTML_TAG_RE.sub("", t)
    t = ASS_TAG_RE.sub("", t)
    t = t.replace(r"\N", "\n").replace(r"\n", "\n")
    t = t.replace("\\", "")
    t = t.replace("\r", "")
    t = t.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    t = t.replace("…", "...").replace("–", "-").replace("—", "-")
    lines = [ln.rstrip() for ln in t.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines).strip()


def parse_srt(path: str) -> List[Tuple[str, str]]:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        data = f.read().replace("\r\n", "\n").replace("\r", "\n")

    blocks = [b for b in data.split("\n\n") if b.strip()]
    cues: List[Tuple[str, str]] = []
    for block in blocks:
        lines = block.split("\n")
        if len(lines) < 2:
            continue
        if lines[0].strip().isdigit() and TIME_RE.match(lines[1].strip()):
            ts = lines[1].strip()
            txt = "\n".join(lines[2:])
            cues.append((ts, txt))
    return cues


def write_srt(path: str, cues: List[Tuple[str, str]]) -> None:
    out_blocks = []
    idx = 1
    for ts, txt in cues:
        if not txt.strip():
            continue
        out_blocks.append(f"{idx}\n{ts}\n{txt}")
        idx += 1
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n\n".join(out_blocks) + "\n")


def translate_cues(cues: List[Tuple[str, str]], translator: MyMemoryTranslator) -> List[Tuple[str, str]]:
    unique: List[str] = []
    seen = set()
    for _, txt in cues:
        cleaned = normalize_text(txt)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            unique.append(cleaned)

    translations: Dict[str, str] = {}
    batch_size = 20
    total = len(unique)
    for i in range(0, total, batch_size):
        chunk = unique[i : i + batch_size]
        try:
            result = translator.translate_batch(chunk)
            if not isinstance(result, list):
                result = [result]
        except Exception:
            result = chunk
        for src, dst in zip(chunk, result):
            translations[src] = normalize_text((dst or src).strip())
        print(f"  translated {min(i + batch_size, total)}/{total}", flush=True)

    out: List[Tuple[str, str]] = []
    for ts, txt in cues:
        cleaned = normalize_text(txt)
        if not cleaned:
            continue
        out.append((ts, translations.get(cleaned, cleaned)))
    return out


def main() -> int:
    args = parse_args()
    folder = os.path.abspath(args.folder)
    if not os.path.isdir(folder):
        print(f"Folder not found: {folder}")
        return 1

    mkvs = list_source_mkvs(folder)
    if not mkvs:
        print("No source MKV files found.")
        return 1

    translator = MyMemoryTranslator(source="fr-FR", target="en-GB")

    for mkv_path in mkvs:
        base = os.path.splitext(os.path.basename(mkv_path))[0]
        out_srt = os.path.join(folder, base + ".en.srt")

        print(f"Processing {base}", flush=True)
        with tempfile.NamedTemporaryFile(suffix=".srt", delete=False) as tmp:
            temp_srt = tmp.name
        try:
            extract_first_subtitle_to_srt(mkv_path, temp_srt)
            cues = parse_srt(temp_srt)
            translated = translate_cues(cues, translator)
            write_srt(out_srt, translated)
            print(f"Saved {out_srt}", flush=True)
        finally:
            if os.path.exists(temp_srt):
                os.remove(temp_srt)

    print("Done. Created only .en.srt sidecars.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
