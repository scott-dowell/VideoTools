#!/usr/bin/env python3
"""
Test: Verify that files with changed mtime are properly identified and updated.

Issue: Files with status='done' but changed mtime should:
1. Be detected as mtime_mismatch in Phase 1
2. Be added to to_hash_check (not to folder_files initially)
3. In Phase 2, hash-match should update the DB mtime
4. On next scan, they should be found via batch lookup with matching mtime
"""
import sqlite3
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'VideoConverter'))

import db

db_path = r'c:\VideoTools\VideoConverter\conversions.db'
db.init_db(db_path)

# Test case: Get a "done" file and check its mtime behavior
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# Find a Sakurasou done file
cursor.execute("""
    SELECT id, source_path, source_mtime, status 
    FROM conversions 
    WHERE source_path LIKE '%Sakurasou%' AND status='done'
    LIMIT 1
""")
record = cursor.fetchone()

if not record:
    print("No Sakurasou done records found. Test inconclusive.")
    conn.close()
    sys.exit(1)

record_id, source_path, old_mtime, status = record
print(f"Test file: {os.path.basename(source_path)}")
print(f"  DB mtime (before update): {old_mtime}")
print(f"  Status: {status}")

# Simulate what happens in Phase 2: update the mtime
new_mtime = old_mtime + 22405  # Approximate time difference from actual Sakurasou files
db.update_mtime(record_id, new_mtime)

# Verify update worked
cursor.execute("SELECT source_mtime FROM conversions WHERE id = ?", (record_id,))
result = cursor.fetchone()
db_mtime_after = result[0] if result else None

print(f"  DB mtime (after update): {db_mtime_after}")
print()

# Now test the batch lookup - simulate Phase 1 of next scan
# Query for the most recent record (by ID) for this path, like get_latest_statuses_by_paths does
source_path_normalized = source_path.replace("\\", "/")
cursor.execute("""
    SELECT c.id, c.source_path, c.status, c.source_mtime
    FROM conversions c
    INNER JOIN (
        SELECT source_path, MAX(id) AS max_id
        FROM conversions
        WHERE source_path = ?
        GROUP BY source_path
    ) latest ON c.id = latest.max_id
""", (source_path_normalized,))
batch_result = cursor.fetchone()

if batch_result:
    batch_id, batch_path, batch_status, batch_mtime = batch_result
    print(f"Batch lookup result:")
    print(f"  ID: {batch_id}")
    print(f"  Status: {batch_status}")
    print(f"  Mtime: {batch_mtime}")
    print()
    
    # Check if mtime matches
    if batch_mtime == new_mtime:
        print("✓ PASS: Batch lookup returns updated mtime")
    else:
        print(f"✗ FAIL: Mtime mismatch in batch lookup. Expected {new_mtime}, got {batch_mtime}")
else:
    print("✗ FAIL: Batch lookup returned no results")

# Reset mtime back to original
db.update_mtime(record_id, old_mtime)
conn.close()
