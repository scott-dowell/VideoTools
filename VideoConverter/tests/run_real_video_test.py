import sys
sys.path.insert(0, r'C:\VideoTools\VideoConverter')
import threading, converter

stop = threading.Event()
src  = r'C:\Users\scott\Downloads\Rachel Cook\Rachel Cook Red Room.mov'
out  = r'C:\Temp\vc_test_out'

result = converter.convert_video(
    input_path  = src,
    output_dir  = out,
    anime_mode  = False,
    quality     = 30,
    progress_cb = lambda p, f, e: print(f'\r{p:.0f}%  fps={f:.0f}  eta={e}s    ', end='', flush=True),
    stop_event  = stop,
    log         = print,
)
print()
print('=== Result ===')
for k, v in result.items():
    print(f'  {k}: {v}')
