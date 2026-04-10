import shutil, os
src = r"C:\Users\scott\Downloads\Anime\Rosario to Vampire\Rosario to Vampire\[Anime Time] Rosario to Vampire - 01.mkv"
bak = src + ".bak"
if not os.path.exists(bak):
    print(f"Backing up to {bak} ...")
    shutil.copy2(src, bak)
src_mb = os.path.getsize(src) / 1024 / 1024
bak_mb = os.path.getsize(bak) / 1024 / 1024
print(f"Source: {src_mb:.2f} MB")
print(f"Backup: {bak_mb:.2f} MB")
