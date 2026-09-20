import sqlite3

db_path = r'c:\VideoTools\VideoConverter\conversions.db'
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

folder = r"C:/Users/scott/Downloads/Anime/The Pet Girl of Sakurasou"

# Get ALL records for this folder (not just latest by ID)
cursor.execute("""
    SELECT id, source_path, source_mtime, status, completed_at, 
           source_bitrate_kbps, output_size_mb, saved_mb
    FROM conversions 
    WHERE source_path LIKE ? 
    ORDER BY source_path, id
""", (folder.replace("\\", "/") + "%",))

rows = cursor.fetchall()
print(f"Total DB records for folder: {len(rows)}\n")
print(f"{'ID':<5} {'Status':<12} {'Mtime':<15} {'Completed':<20} {'Filename':<50}")
print("-" * 115)

for row in rows:
    path = row["source_path"]
    filename = path.split("/")[-1]
    mtime = row["source_mtime"]
    status = row["status"]
    completed = row["completed_at"]
    print(f"{row['id']:<5} {status:<12} {mtime:<15.1f} {str(completed):<20} {filename:<50}")

conn.close()
