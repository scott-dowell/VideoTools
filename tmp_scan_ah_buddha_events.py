import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "VideoConverter"))

import scanner
import db

ROOT = r"C:/Users/scott/Downloads/Anime/_Ah! My Buddha"
DB = r"c:\VideoTools\VideoConverter\conversions.db"
TARGETS = {
    "Ah! My Buddha 01 (CB8C790A).mp4",
    "Ah! My Buddha 02 (53E3FCD6).mp4",
    "Ah! My Buddha 03 (6333BECD).mp4",
}

db.init_db(DB)
found = {}
for ev in scanner.walk(ROOT):
    t = ev.get("type")
    if t == "folder":
        for f in ev.get("files", []):
            if f.get("name") in TARGETS:
                found[f["name"]] = {
                    "status": f.get("status"),
                    "bitrate_kbps": f.get("bitrate_kbps"),
                    "duration": f.get("duration"),
                    "codec": f.get("codec"),
                }
    elif t == "hash_match_done":
        n = ev.get("full_path", "").split("/")[-1]
        if n in TARGETS:
            found[n] = {
                "status": "done(hash_match)",
                "bitrate_kbps": ev.get("bitrate_kbps"),
                "duration": ev.get("duration"),
                "codec": ev.get("codec"),
            }

print(found)
