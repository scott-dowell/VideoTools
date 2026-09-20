import sys
import os

sys.path.insert(0, r"c:\VideoTools\VideoConverter")
import scanner
import db

ROOT = r"C:/Users/scott/Downloads/Anime/_Ah! My Buddha"
TARGET = "Ah! My Buddha 01 (CB8C790A).mp4"
DB = r"c:\VideoTools\VideoConverter\conversions.db"

db.init_db(DB)

found = False
for ev in scanner.walk(ROOT):
    t = ev.get('type')
    if t == 'folder':
        for f in ev.get('files', []):
            if f.get('name') == TARGET:
                found = True
                print('folder event status:', f.get('status'))
                print('full_path:', f.get('full_path'))
                print('codec:', f.get('codec'))
                print('duration:', f.get('duration'))
    elif t in ('hash_match_done','probe','remove') and TARGET in (ev.get('full_path') or ''):
        print('event', t, ev)

print('found_in_folder_event:', found)
