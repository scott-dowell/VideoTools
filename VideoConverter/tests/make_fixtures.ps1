<#
.SYNOPSIS
    Generates synthetic test video fixtures for VideoConverter using FFmpeg.

.DESCRIPTION
    All content is synthetic (colourbar test pattern + sine tone) — no copyright.
    Fixtures are written to VideoConverter/tests/fixtures/.

.USAGE
    From C:\VideoTools:
        .\.venv\Scripts\Activate.ps1
        .\VideoConverter\tests\make_fixtures.ps1
#>

$ffmpeg = 'ffmpeg'
if (-not (Get-Command $ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Error "ffmpeg not found in PATH.  Install FFmpeg and make sure it is on PATH."
    exit 1
}

$outDir = Join-Path $PSScriptRoot 'fixtures'
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
Write-Host "Output directory: $outDir" -ForegroundColor Cyan

# ---------------------------------------------------------------------------
# lavfi source strings
# ---------------------------------------------------------------------------

$vid30s  = 'testsrc=size=1280x720:rate=24:duration=30,format=yuv420p'
$vid5min = 'testsrc=size=1280x720:rate=24:duration=300,format=yuv420p'
$vid8s   = 'testsrc=size=1280x720:rate=24:duration=8,format=yuv420p'

$aud30s  = 'sine=frequency=440:sample_rate=48000:duration=30'
$aud5min = 'sine=frequency=440:sample_rate=48000:duration=300'
$aud8s   = 'sine=frequency=440:sample_rate=48000:duration=8'

# ---------------------------------------------------------------------------
# Temp SRT subtitle file
# ---------------------------------------------------------------------------

$tmpSrt = Join-Path $env:TEMP 'vc_fixture_sub.srt'
@"
1
00:00:01,000 --> 00:00:03,000
Test subtitle line 1

2
00:00:05,000 --> 00:00:08,000
Test subtitle line 2
"@ | Set-Content -Encoding UTF8 -Path $tmpSrt

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

function Invoke-FFmpeg {
    param(
        [string]   $Label,
        [string[]] $FFmpegArgs
    )
    Write-Host "  Building $Label ..." -NoNewline
    # Collect output (stdout + stderr) to avoid breaking on x265/x264 info lines.
    # $ErrorActionPreference is NOT 'Stop' at script level, so ErrorRecord objects
    # from external stderr are collected in $output without aborting.
    $output = & $ffmpeg @FFmpegArgs 2>&1
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        Write-Host " FAILED (exit $exitCode)" -ForegroundColor Red
        $output | Select-Object -Last 10 |
            ForEach-Object { Write-Host "    $_" -ForegroundColor DarkRed }
        throw "FFmpeg failed for: $Label"
    }
    $size = [math]::Round((Get-Item (Join-Path $outDir $Label)).Length / 1MB, 1)
    Write-Host " OK  ($size MB)" -ForegroundColor Green
}

# ---------------------------------------------------------------------------
# 1. h264_short.mkv  —  H264 8-bit, 30 s, AAC stereo, ASS eng sub
# ---------------------------------------------------------------------------

Invoke-FFmpeg -Label 'h264_short.mkv' -FFmpegArgs @(
    '-y', '-hide_banner', '-loglevel', 'error',
    '-f', 'lavfi', '-i', $vid30s,
    '-f', 'lavfi', '-i', $aud30s,
    '-i', $tmpSrt,
    '-map', '0:v', '-map', '1:a', '-map', '2:s',
    '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '28',
    '-c:a', 'aac_mf', '-b:a', '128k',
    '-c:s', 'ass',
    '-metadata:s:a:0', 'language=eng',
    '-metadata:s:s:0', 'language=eng',
    (Join-Path $outDir 'h264_short.mkv')
)

# ---------------------------------------------------------------------------
# 2. h264_long.mkv  —  H264 8-bit, 5 min, AAC stereo, no subs
# ---------------------------------------------------------------------------

Invoke-FFmpeg -Label 'h264_long.mkv' -FFmpegArgs @(
    '-y', '-hide_banner', '-loglevel', 'error',
    '-f', 'lavfi', '-i', $vid5min,
    '-f', 'lavfi', '-i', $aud5min,
    '-map', '0:v', '-map', '1:a',
    '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '28',
    '-c:a', 'aac_mf', '-b:a', '128k',
    '-metadata:s:a:0', 'language=eng',
    (Join-Path $outDir 'h264_long.mkv')
)

# ---------------------------------------------------------------------------
# 3. hevc_skip.mkv  —  HEVC, 30 s, AAC stereo, no subs  (must be skipped)
# ---------------------------------------------------------------------------

Invoke-FFmpeg -Label 'hevc_skip.mkv' -FFmpegArgs @(
    '-y', '-hide_banner', '-loglevel', 'error',
    '-f', 'lavfi', '-i', $vid30s,
    '-f', 'lavfi', '-i', $aud30s,
    '-map', '0:v', '-map', '1:a',
    '-c:v', 'libx265', '-preset', 'ultrafast', '-crf', '28',
    '-c:a', 'aac_mf', '-b:a', '128k',
    '-metadata:s:a:0', 'language=eng',
    (Join-Path $outDir 'hevc_skip.mkv')
)

# ---------------------------------------------------------------------------
# 4. h264_tiny.mkv  —  H264 8-bit, 8 s, no audio, no subs
# ---------------------------------------------------------------------------

Invoke-FFmpeg -Label 'h264_tiny.mkv' -FFmpegArgs @(
    '-y', '-hide_banner', '-loglevel', 'error',
    '-f', 'lavfi', '-i', $vid8s,
    '-map', '0:v',
    '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '28',
    '-an',
    (Join-Path $outDir 'h264_tiny.mkv')
)

# ---------------------------------------------------------------------------
# 5. h264_multitrack.mkv  —  H264 8-bit, 30 s, AC3 jpn + AAC eng, ASS eng sub
#    Note: real PGS bitmap sub fixture is added in Phase 3b.
# ---------------------------------------------------------------------------

Invoke-FFmpeg -Label 'h264_multitrack.mkv' -FFmpegArgs @(
    '-y', '-hide_banner', '-loglevel', 'error',
    '-f', 'lavfi', '-i', $vid30s,
    '-f', 'lavfi', '-i', $aud30s,   # audio track 0 → jpn AC3
    '-f', 'lavfi', '-i', $aud30s,   # audio track 1 → eng AAC
    '-i', $tmpSrt,
    '-map', '0:v', '-map', '1:a', '-map', '2:a', '-map', '3:s',
    '-c:v',   'libx264', '-preset', 'ultrafast', '-crf', '28',
    '-c:a:0', 'ac3',  '-ac:a:0', '2', '-b:a:0', '384k', '-metadata:s:a:0', 'language=jpn',
    '-c:a:1', 'aac_mf',  '-b:a:1', '128k', '-metadata:s:a:1', 'language=eng',
    '-c:s',   'ass',  '-metadata:s:s:0', 'language=eng',
    (Join-Path $outDir 'h264_multitrack.mkv')
)

# ---------------------------------------------------------------------------
# 6. h264_hi10.mkv  —  H264 10-bit, 30 s, FLAC jpn, ASS eng sub
# ---------------------------------------------------------------------------

Invoke-FFmpeg -Label 'h264_hi10.mkv' -FFmpegArgs @(
    '-y', '-hide_banner', '-loglevel', 'error',
    '-f', 'lavfi', '-i', "${vid30s},format=yuv420p10le",
    '-f', 'lavfi', '-i', $aud30s,
    '-i', $tmpSrt,
    '-map', '0:v', '-map', '1:a', '-map', '2:s',
    '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '28', '-profile:v', 'high10',
    '-c:a', 'flac',
    '-c:s', 'ass',
    '-metadata:s:a:0', 'language=jpn',
    '-metadata:s:s:0', 'language=eng',
    (Join-Path $outDir 'h264_hi10.mkv')
)

# ---------------------------------------------------------------------------
# 7. h264_mp4_aac.mp4  —  H264 8-bit, 30 s, AAC stereo, no subs  (MP4 fast-path)
# ---------------------------------------------------------------------------

Invoke-FFmpeg -Label 'h264_mp4_aac.mp4' -FFmpegArgs @(
    '-y', '-hide_banner', '-loglevel', 'error',
    '-f', 'lavfi', '-i', $vid30s,
    '-f', 'lavfi', '-i', $aud30s,
    '-map', '0:v', '-map', '1:a',
    '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '28',
    '-c:a', 'aac_mf', '-b:a', '128k',
    '-metadata:s:a:0', 'language=eng',
    '-movflags', '+faststart',
    (Join-Path $outDir 'h264_mp4_aac.mp4')
)

# ---------------------------------------------------------------------------
# Cleanup + summary
# ---------------------------------------------------------------------------

Remove-Item $tmpSrt -ErrorAction SilentlyContinue

# ---------------------------------------------------------------------------
# 8. h264_bitmap_sub.mkv  — HEVC, 60 s real clip, AAC jpn, ASS eng + PGS eng
#
#    This fixture is NOT generated synthetically — ffmpeg cannot encode PGS.
#    It is a 60-second extract from a real source file.
#    To regenerate:
#        ffmpeg -y -ss 120 -t 60 `
#          -i "C:\Users\scott\Downloads\Anime\Isuca\S01E01-Chance Meeting.mkv" `
#          -map 0:0 -map 0:1 -map 0:2 -map 0:3 `
#          -c:v copy -c:a copy -c:s copy `
#          "$outDir\h264_bitmap_sub.mkv"
#    Streams: 0=hevc, 1=aac/jpn, 2=ass/eng(Doki), 3=hdmv_pgs_subtitle/eng(USBD)
# ---------------------------------------------------------------------------
if (-not (Test-Path (Join-Path $outDir 'h264_bitmap_sub.mkv'))) {
    Write-Host "  h264_bitmap_sub.mkv not found — skipping (requires real source file)" -ForegroundColor Yellow
} else {
    $size = [math]::Round((Get-Item (Join-Path $outDir 'h264_bitmap_sub.mkv')).Length / 1MB, 1)
    Write-Host "  h264_bitmap_sub.mkv already present ($size MB)" -ForegroundColor Green
}

Write-Host ""
Write-Host "Synthetic fixtures generated successfully." -ForegroundColor Cyan
Get-ChildItem $outDir | Format-Table Name, @{L='Size (MB)'; E={[math]::Round($_.Length/1MB,1)}} -AutoSize
