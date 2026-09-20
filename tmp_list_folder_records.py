import sqlite3

DB = r"c:\VideoTools\VideoConverter\conversions.db"
FOLDER = "C:/Users/scott/Downloads/Anime/_Ah! My Buddha"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
cur = conn.cursor()
cur.execute(
    """
    SELECT source_path, status, source_bitrate_kbps, output_bitrate_kbps, source_duration_secs, completed_at
    FROM conversions
    WHERE source_path LIKE ?
    ORDER BY source_path
    """,
    (FOLDER + "/%",),
)
rows = cur.fetchall()
print("rows", len(rows))
for r in rows[:20]:
    name = r["source_path"].split("/")[-1]
    print(name, "|", r["status"], "| src_br", r["source_bitrate_kbps"], "| out_br", r["output_bitrate_kbps"], "| dur", r["source_duration_secs"], "| completed", r["completed_at"])

conn.close()
