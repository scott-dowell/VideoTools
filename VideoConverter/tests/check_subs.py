"""
Non-destructive subtitle classification test.
Usage: python tests/check_subs.py <path_to_mkv>
"""
import sys, os, json, subprocess
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import converter

TEXT_SUB_CODECS    = {"ass", "subrip", "srt", "webvtt", "mov_text"}
PGS_SUB_CODECS     = {"hdmv_pgs_subtitle", "pgssub"}
COPY_BITMAP_CODECS = {"dvd_subtitle", "vobsub"}
BITMAP_SUB_CODECS  = PGS_SUB_CODECS | COPY_BITMAP_CODECS

src = sys.argv[1]
r = subprocess.run(
    ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", src],
    capture_output=True, text=True, timeout=60,
)
if not r.stdout.strip():
    print("ERROR: ffprobe returned no output:", r.stderr[:200])
    sys.exit(1)
streams = json.loads(r.stdout)["streams"]
sub_streams = [s for s in streams if s.get("codec_type") == "subtitle"]

if not sub_streams:
    print("No subtitle streams found.")
    sys.exit(0)

english_text   = []
pgs_ocr        = []
copy_bitmap    = []
dropped        = []

for s in sub_streams:
    tags  = s.get("tags", {})
    lang  = tags.get("language", "")
    title = tags.get("title", "")
    codec = s.get("codec_name", "")
    is_eng = converter._is_potentially_english(lang, title)

    if codec in TEXT_SUB_CODECS:
        if is_eng:
            english_text.append(s)
        else:
            dropped.append((s, "non-English text sub"))
    elif codec in PGS_SUB_CODECS:
        if is_eng:
            pgs_ocr.append(s)
        else:
            dropped.append((s, "non-English PGS sub"))
    elif codec in COPY_BITMAP_CODECS:
        if is_eng:
            copy_bitmap.append(s)
        else:
            dropped.append((s, f"non-English {codec}"))
    else:
        dropped.append((s, f"unknown codec {codec!r}"))

# Sole-sub fallback
if not english_text and not pgs_ocr and not copy_bitmap and len(sub_streams) == 1:
    s = sub_streams[0]
    codec = s.get("codec_name", "")
    if codec in TEXT_SUB_CODECS:
        english_text.append(s); dropped.clear()
        print("  (sole-sub rule: keeping sole text sub regardless of language)")
    elif codec in PGS_SUB_CODECS:
        pgs_ocr.append(s); dropped.clear()
        print("  (sole-sub rule: keeping sole PGS sub for OCR)")
    elif codec in COPY_BITMAP_CODECS:
        copy_bitmap.append(s); dropped.clear()
        print("  (sole-sub rule: keeping sole dvd_subtitle/vobsub for copy)")

print(f"{'':=<60}")
print(f"File: {os.path.basename(src)}")
print(f"{'':=<60}")
print(f"\nKept as English text (-> mov_text in MP4):")
for s in english_text:
    tags = s.get("tags", {})
    print(f"  stream {s['index']:2d}: {s['codec_name']:<20} lang={tags.get('language','')!r:6} title={tags.get('title','')!r}")

print(f"\nSent to PGS OCR (hdmv_pgs -> SRT -> mov_text):")
for s in pgs_ocr:
    tags = s.get("tags", {})
    print(f"  stream {s['index']:2d}: {s['codec_name']:<20} lang={tags.get('language','')!r:6} title={tags.get('title','')!r}")

print(f"\nCopied directly (dvd_subtitle/vobsub -> bin_data in MP4):")
for s in copy_bitmap:
    tags = s.get("tags", {})
    print(f"  stream {s['index']:2d}: {s['codec_name']:<20} lang={tags.get('language','')!r:6} title={tags.get('title','')!r}")

print(f"\nDropped:")
for s, reason in dropped:
    tags = s.get("tags", {})
    print(f"  stream {s['index']:2d}: {s['codec_name']:<20} lang={tags.get('language','')!r:6} title={tags.get('title','')!r}  ({reason})")

print()
