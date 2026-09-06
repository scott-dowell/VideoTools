#!/usr/bin/env python3
"""Deep analysis of Fate kaleid liner files with audio/video duration mismatch."""

import json
import subprocess
from pathlib import Path

fate_files = [
    r"C:/Users/scott/Downloads/Anime/_Fate kaleid liner Prisma Illya/7 - Season 2 - Specials/Fate kaleid liner Prisma Illya 2wei! - S00SP01.mkv",
    r"C:/Users/scott/Downloads/Anime/_Fate kaleid liner Prisma Illya/7 - Season 2 - Specials/Fate kaleid liner Prisma Illya 2wei! - S00SP03.mkv",
    r"C:/Users/scott/Downloads/Anime/_Fate kaleid liner Prisma Illya/7 - Season 2 - Specials/Fate kaleid liner Prisma Illya 2wei! - S00SP05.mkv",
]

for fpath in fate_files:
    # Check if file exists (convert forward slashes)
    fpath_win = fpath.replace('/', '\\')
    if not Path(fpath_win).exists():
        print(f"❌ File not found: {fpath_win}\n")
        continue
    
    fname = Path(fpath_win).name
    print(f"\n{'='*70}")
    print(f"📁 {fname}")
    print(f"{'='*70}")
    
    # Run ffprobe to get detailed stream info
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        fpath_win,
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            print(f"❌ ffprobe failed: {result.stderr[:200]}")
            continue
        
        data = json.loads(result.stdout)
        
        # Format duration
        fmt = data.get("format", {})
        fmt_dur = float(fmt.get("duration", 0))
        print(f"\nContainer duration: {fmt_dur:.1f}s")
        
        # Analyze each stream
        print(f"\nStreams:")
        for i, stream in enumerate(data.get("streams", [])):
            idx = stream.get("index")
            codec_type = stream.get("codec_type")
            codec_name = stream.get("codec_name", "unknown")
            
            if codec_type == "video":
                dur = float(stream.get("duration", 0))
                fps = stream.get("r_frame_rate", "0")
                res = f"{stream.get('width')}x{stream.get('height')}"
                print(f"  [{idx}] VIDEO:  {codec_name:8s}  {res:9s}  dur={dur:6.1f}s  fps={fps}")
            
            elif codec_type == "audio":
                dur = float(stream.get("duration", 0))
                lang = stream.get("tags", {}).get("language", "?")
                ch = stream.get("channels", "?")
                sr = stream.get("sample_rate", "?")
                print(f"  [{idx}] AUDIO:  {codec_name:8s}  {lang:4s}  {ch}ch@{sr:5s}Hz  dur={dur:6.1f}s")
            
            elif codec_type == "subtitle":
                lang = stream.get("tags", {}).get("language", "?")
                print(f"  [{idx}] SUB:    {codec_name:8s}  {lang:4s}")
        
        # Calculate A/V mismatch
        video_dur = None
        audio_durs = []
        
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                video_dur = float(stream.get("duration", 0))
            elif stream.get("codec_type") == "audio":
                audio_durs.append(float(stream.get("duration", 0)))
        
        if video_dur and audio_durs:
            max_audio = max(audio_durs)
            av_diff = abs(video_dur - max_audio)
            av_diff_pct = av_diff / max(video_dur, max_audio) * 100
            print(f"\n⚠️  A/V Mismatch: video={video_dur:.1f}s, max_audio={max_audio:.1f}s, diff={av_diff_pct:.1f}%")
            
            if av_diff_pct > 5:
                print(f"    → Exceeds 5% threshold (CONVERSION WILL FAIL)")
                if video_dur > max_audio:
                    print(f"    → Video is {av_diff:.1f}s longer than audio")
                    print(f"    → Options: (1) Drop short audio, (2) Extend audio with silence, (3) Trim video to audio end")
                else:
                    print(f"    → Audio is {av_diff:.1f}s longer than video")
                    print(f"    → Options: (1) Drop long audio, (2) Trim audio to video, (3) Extend video")
    
    except json.JSONDecodeError as e:
        print(f"❌ JSON parse error: {e}")
    except subprocess.TimeoutExpired:
        print(f"❌ ffprobe timeout")
    except Exception as e:
        print(f"❌ Error: {e}")

print(f"\n{'='*70}")
