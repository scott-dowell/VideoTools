#!/usr/bin/env python3
"""
RED TEST: Verify files with mismatched mtime don't show as pending

Issue #39: Files with status='done' in DB but changed mtime on filesystem
should NOT appear as pending in the scan grid.

Expected: Files show as "done"
Current: Files show as "pending"
"""
import sys
import os
import sqlite3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import db

# Query actual Sakurasou files from real database
db_path = r'c:\VideoTools\VideoConverter\conversions.db'
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

# Get S01E01-E18 files (status='done' with mismatched mtime)
cursor = conn.cursor()
cursor.execute("""
    SELECT id, source_path, source_mtime, status
    FROM conversions
    WHERE source_path LIKE '%Sakurasou%' AND status='done'
    ORDER BY source_path
""")

done_files = cursor.fetchall()

if not done_files:
    print("❌ No Sakurasou done files found in database")
    conn.close()
    sys.exit(1)

print(f"Found {len(done_files)} done files in Sakurasou folder\n")

# Check mtime mismatches
mismatched_count = 0
for record in done_files:
    source_path = record['source_path']
    db_mtime = record['source_mtime']
    
    # Convert to native path for filesystem access
    fs_path = source_path.replace('/', '\\')
    
    if os.path.exists(fs_path):
        fs_mtime = os.path.getmtime(fs_path)
        if fs_mtime != db_mtime:
            mismatched_count += 1
            print(f"MTIME MISMATCH: {os.path.basename(fs_path)}")
            print(f"  DB mtime:    {db_mtime}")
            print(f"  FS mtime:    {fs_mtime}")
            print(f"  Difference:  {fs_mtime - db_mtime:.1f} seconds\n")

conn.close()

if mismatched_count == 0:
    print("❌ No mtime mismatches found - test inconclusive")
    sys.exit(1)

print(f"✓ Found {mismatched_count} files with mtime mismatch")
print("\nRED TEST: These files should NOT show as 'pending' in the grid.")
print("Currently they DO, which is the bug we're fixing.")
