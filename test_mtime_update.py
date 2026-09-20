#!/usr/bin/env python3
"""
Test the mtime update fix for hash-based file re-identification.
"""
import sqlite3
import sys
import os

# Add VideoConverter dir to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'VideoConverter'))

import db

# Test the new update_mtime function
db_path = r'c:\VideoTools\VideoConverter\conversions.db'
db.init_db(db_path)

# Get an existing done record
conn = sqlite3.connect(db_path)
cursor = conn.cursor()
cursor.execute("SELECT id, source_path, source_mtime, status FROM conversions WHERE status='done' LIMIT 1")
record = cursor.fetchone()
conn.close()

if not record:
    print("No done records found to test with")
    sys.exit(1)

record_id, source_path, old_mtime, status = record
print(f"Testing with record: id={record_id}, path={source_path}")
print(f"  Old mtime: {old_mtime}")

# Test update_mtime
new_mtime = old_mtime + 1000  # Add 1000 seconds
db.update_mtime(record_id, new_mtime)

# Verify it was updated
conn = sqlite3.connect(db_path)
cursor = conn.cursor()
cursor.execute("SELECT source_mtime FROM conversions WHERE id = ?", (record_id,))
result = cursor.fetchone()
conn.close()

if result and result[0] == new_mtime:
    print(f"  New mtime: {new_mtime}")
    print("✓ update_mtime() works correctly!")
else:
    print(f"✗ Failed! Expected {new_mtime}, got {result[0] if result else 'None'}")
    sys.exit(1)

# Reset it back
db.update_mtime(record_id, old_mtime)
print("✓ Mtime reset successfully")
