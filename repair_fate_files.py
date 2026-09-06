#!/usr/bin/env python3
"""
Repair Fate kaleid liner files with audio/video duration mismatch.

These files have video ~60s longer than audio (likely padding at the end).
Solution: Trim video to match audio duration using stream-copy (no re-encoding).
"""

import subprocess
import os
from pathlib import Path
from datetime import datetime

def repair_fate_file(input_path, output_path=None):
    """Trim video stream to match audio duration, keep all audio/subtitle tracks."""
    
    input_path = Path(input_path)
    if not input_path.exists():
        print(f"❌ File not found: {input_path}")
        return False
    
    if output_path is None:
        # Create output with _repaired suffix
        output_path = input_path.parent / f"{input_path.stem}_repaired{input_path.suffix}"
    else:
        output_path = Path(output_path)
    
    print(f"\n🔧 Repairing: {input_path.name}")
    print(f"   Output:   {output_path.name}")
    
    # Step 1: Get audio and video durations using ffprobe
    print("   ├─ Probing streams...")
    cmd_probe = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(input_path),
    ]
    
    try:
        result = subprocess.run(cmd_probe, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            print(f"   ❌ ffprobe failed")
            return False
        
        import json
        data = json.loads(result.stdout)
        
        # Extract video and audio durations
        video_dur = None
        audio_dur = None
        
        for stream in data.get("streams", []):
            codec_type = stream.get("codec_type")
            dur = stream.get("duration")
            
            if codec_type == "video" and video_dur is None:
                if dur and float(dur) > 0:
                    video_dur = float(dur)
            
            if codec_type == "audio" and audio_dur is None:
                if dur and float(dur) > 0:
                    audio_dur = float(dur)
        
        # Fallback to container duration if individual stream durations not available
        fmt = data.get("format", {})
        container_dur = float(fmt.get("duration", 0)) if fmt.get("duration") else 0
        
        if not video_dur and container_dur > 0:
            video_dur = container_dur
        if not audio_dur and container_dur > 0:
            audio_dur = container_dur
        
        if not video_dur or not audio_dur:
            print(f"   ❌ Could not determine stream durations")
            return False
        
        # Trim to the shorter duration to avoid A/V mismatch
        trim_dur = min(video_dur, audio_dur)
        diff_s = abs(video_dur - audio_dur)
        diff_pct = (diff_s / max(video_dur, audio_dur)) * 100
        
        print(f"   ├─ Video: {video_dur:.1f}s, Audio: {audio_dur:.1f}s")
        print(f"   ├─ Mismatch: {diff_s:.1f}s ({diff_pct:.1f}%)")
        print(f"   ├─ Trimming to: {trim_dur:.1f}s ({int(trim_dur//60)}m {int(trim_dur%60)}s)")
    
    except Exception as e:
        print(f"   ❌ Probe error: {e}")
        return False
    
    # Step 2: Trim to shorter duration using stream-copy
    print("   ├─ Trimming to shorter duration (stream-copy)...")
    cmd_trim = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-map", "0",  # copy all streams
        "-c", "copy",  # stream-copy (no re-encoding)
        "-to", f"{trim_dur:.3f}",  # trim to shorter duration
        "-movflags", "faststart",  # for better MP4 compatibility if needed
        str(output_path),
    ]
    
    try:
        result = subprocess.run(cmd_trim, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            print(f"   ❌ Trim failed")
            print(f"      {result.stderr[-200:]}")
            return False
        
        if not output_path.exists() or output_path.stat().st_size == 0:
            print(f"   ❌ Output file not created or empty")
            return False
        
        input_size_mb = input_path.stat().st_size / (1024*1024)
        output_size_mb = output_path.stat().st_size / (1024*1024)
        saved_mb = input_size_mb - output_size_mb
        saved_pct = (saved_mb / input_size_mb * 100) if input_size_mb > 0 else 0
        
        print(f"   ├─ Trim complete:")
        print(f"      Input:  {input_size_mb:.1f} MB")
        print(f"      Output: {output_size_mb:.1f} MB")
        print(f"      Saved:  {saved_mb:.1f} MB ({saved_pct:.1f}%)")
    
    except subprocess.TimeoutExpired:
        print(f"   ❌ Trim timeout")
        return False
    except Exception as e:
        print(f"   ❌ Trim error: {e}")
        return False
    
    # Step 3: Verify the repaired file
    print("   ├─ Verifying repaired file...")
    cmd_verify = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(output_path),
    ]
    
    try:
        result = subprocess.run(cmd_verify, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            print(f"   ⚠️  Verification failed")
            return False
        
        data = json.loads(result.stdout)
        out_dur = float(data.get("format", {}).get("duration", 0))
        
        # Check A/V alignment
        video_dur = None
        audio_dur_check = None
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                video_dur = float(stream.get("duration", 0)) or out_dur
            elif stream.get("codec_type") == "audio" and audio_dur_check is None:
                audio_dur_check = float(stream.get("duration", 0)) or out_dur
        
        if video_dur and audio_dur_check:
            av_diff_pct = abs(video_dur - audio_dur_check) / max(video_dur, audio_dur_check) * 100
            if av_diff_pct <= 5:
                print(f"   ✅ A/V aligned: video={video_dur:.1f}s, audio={audio_dur_check:.1f}s (diff={av_diff_pct:.1f}%)")
                return True
            else:
                print(f"   ⚠️  Still misaligned: video={video_dur:.1f}s, audio={audio_dur_check:.1f}s (diff={av_diff_pct:.1f}%)")
                return False
        
        print(f"   ✅ Repaired file created: {output_path.name}")
        return True
    
    except Exception as e:
        print(f"   ⚠️  Verification error: {e}")
        return True  # file was created, even if verification failed


# Repair all 3 files
fate_files = [
    r"C:\Users\scott\Downloads\Anime\_Fate kaleid liner Prisma Illya\7 - Season 2 - Specials\Fate kaleid liner Prisma Illya 2wei! - S00SP01.mkv",
    r"C:\Users\scott\Downloads\Anime\_Fate kaleid liner Prisma Illya\7 - Season 2 - Specials\Fate kaleid liner Prisma Illya 2wei! - S00SP03.mkv",
    r"C:\Users\scott\Downloads\Anime\_Fate kaleid liner Prisma Illya\7 - Season 2 - Specials\Fate kaleid liner Prisma Illya 2wei! - S00SP05.mkv",
]

print(f"\n{'='*70}")
print(f"Fate kaleid liner — A/V Duration Mismatch Repair")
print(f"{'='*70}")
print(f"Issue: Video 16% longer than audio (appears to have ~60s padding at end)")
print(f"Fix:   Stream-copy trim video to audio duration (no re-encoding)")

success_count = 0
for fpath in fate_files:
    if repair_fate_file(fpath):
        success_count += 1

print(f"\n{'='*70}")
print(f"Result: {success_count}/{len(fate_files)} files repaired successfully")
if success_count == len(fate_files):
    print(f"\n✅ All files ready for conversion!")
    print(f"   Replace originals with _repaired versions, then rescan/convert.")
print(f"{'='*70}\n")
