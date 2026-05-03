import subprocess, json, os

files = [
    ("q00_original", r"C:\Users\scott\Downloads\Anime\Gushing Over Magical Girls\quality_compare\q00_original.mp4"),
    ("q26_medium",   r"C:\Users\scott\Downloads\Anime\Gushing Over Magical Girls\quality_compare\q26_medium.mp4"),
    ("q30_low",      r"C:\Users\scott\Downloads\Anime\Gushing Over Magical Girls\quality_compare\q30_low.mp4"),
    ("q34_very_low", r"C:\Users\scott\Downloads\Anime\Gushing Over Magical Girls\quality_compare\q34_very_low.mp4"),
]

for label, path in files:
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format", path],
        capture_output=True, text=True
    )
    d = json.loads(r.stdout)
    total_mb = int(d["format"]["size"]) / 1024 / 1024
    duration = float(d["format"]["duration"])
    print(f"{label}  ({total_mb:.1f} MB total, {duration/60:.1f} min)")
    for s in d["streams"]:
        br = int(s.get("bit_rate") or 0)
        lang = (s.get("tags") or {}).get("language", "?")
        ctype = s.get("codec_type", "?")
        cname = s.get("codec_name", "?")
        # Estimate stream size in MB from bitrate * duration
        est_mb = (br * duration) / 8 / 1024 / 1024 if br else 0
        print(f"  {ctype:10s} {cname:12s} {br//1000:5d} kbps  lang={lang}  ~{est_mb:.1f} MB")
    print()
