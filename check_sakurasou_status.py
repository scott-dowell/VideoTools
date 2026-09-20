#!/usr/bin/env python3
"""
Diagnose the status of The Pet Girl of Sakurasou files in the database.
"""
import sqlite3
import os
from pathlib import Path

# The folder in question
folder = r"C:/Users/scott/Downloads/Anime/The Pet Girl of Sakurasou"
# Normalize to forward slashes as the DB uses
folder_normalized = folder.replace("\\", "/")

# Connect to the database
db_path = r"c:\VideoTools\VideoConverter\conversions.db"
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

print(f"Checking folder: {folder}")
print(f"Normalized: {folder_normalized}")
print("-" * 100)

# List files in the folder
files = sorted(os.listdir(folder))
print(f"Total files in folder: {len(files)}\n")

# Check each file
print(f"{'Filename':<50} {'Status':<12} {'Mtime':<15} {'DB Mtime':<15}")
print("-" * 100)

for filename in files:
    filepath = os.path.join(folder, filename)
    filepath_normalized = filepath.replace("\\", "/")
    mtime = os.path.getmtime(filepath)
    
    # Query the database
    cursor.execute(
        "SELECT id, status, source_mtime, completed_at FROM conversions WHERE source_path = ?",
        (filepath_normalized,)
    )
    row = cursor.fetchone()
    
    if row:
        status = row["status"]
        db_mtime = row["source_mtime"]
        completed_at = row["completed_at"]
        print(f"{filename:<50} {status:<12} {mtime:<15.1f} {db_mtime:<15.1f} (completed: {completed_at})")
    else:
        print(f"{filename:<50} {'NOT FOUND':<12} {mtime:<15.1f} {'---':<15}")

print("-" * 100)
print("\nSummary:")
cursor.execute(
    "SELECT status, COUNT(*) as count FROM conversions WHERE source_path LIKE ? GROUP BY status",
    (folder_normalized.replace("\\", "/") + "%",)
)
for row in cursor.fetchall():
    print(f"  {row['status']}: {row['count']}")

conn.close()
