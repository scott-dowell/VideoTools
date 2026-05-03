"""Quick test: probe and AAC-encode the audio of a specific file using native aac + P-core pinning."""
import subprocess, json, os, time, ctypes, sys

src = r'C:/Users/scott/Downloads/Anime/Ayakashi Triangle/Ayakashi Triangle - 08v2 [5F05F1CC].mkv'
out = r'C:\Temp\test_audio_aac.m4a'

# Probe audio streams
probe = subprocess.run(
    ['ffprobe', '-v', 'error', '-select_streams', 'a',
     '-show_entries', 'stream=index,codec_name,channels,sample_rate',
     '-of', 'json', src],
    capture_output=True, text=True, timeout=30
)
streams = json.loads(probe.stdout).get('streams', [])
print(f'Audio streams: {len(streams)}')
for s in streams:
    print(f'  index={s["index"]} codec={s["codec_name"]} ch={s["channels"]} sr={s["sample_rate"]}')

if not streams:
    print('No audio streams found — aborting.')
    sys.exit(1)

# P-core affinity mask
kernel32 = ctypes.windll.kernel32
cpu_count = os.cpu_count() or 1
e_core_count = 8
p_core_mask = (1 << (cpu_count - e_core_count)) - 1 if cpu_count > e_core_count else (1 << cpu_count) - 1
print(f'CPU count={cpu_count}, P-core mask=0x{p_core_mask:x}')

if os.path.exists(out):
    os.remove(out)

ai = streams[0]['index']
cmd = [
    'ffmpeg', '-y',
    '-fflags', '+genpts',
    '-avoid_negative_ts', 'make_zero',
    '-i', src,
    '-map', f'0:{ai}',
    '-c:a', 'aac',
    '-vn', '-sn',
    out,
]
print(f'Running: {" ".join(cmd)}')
t0 = time.monotonic()
proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
try:
    kernel32.SetProcessAffinityMask(proc._handle, p_core_mask)
except Exception as e:
    print(f'Affinity set failed (non-fatal): {e}')

output, _ = proc.communicate(timeout=300)
elapsed = time.monotonic() - t0
print(f'Exit code: {proc.returncode}  Time: {elapsed:.1f}s')

if os.path.exists(out):
    size = os.path.getsize(out)
    print(f'Output size: {size:,} bytes')
    if size > 10240:
        print('SUCCESS')
    else:
        print('FAILED (output too small)')
        print(output[-500:])
else:
    print('FAILED (no output file)')
    print(output[-500:])
