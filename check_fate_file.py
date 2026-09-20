import sqlite3
import os

conn = sqlite3.connect('VideoConverter/conversions.db')
cur = conn.cursor()

# Get schema
print("=== Database Schema ===")
rows = cur.execute("PRAGMA table_info(conversions)").fetchall()
for r in rows:
  print(f'{r[1]}: {r[2]}')

print("\n=== Checking Fate file ===")
path = r'C:/Users/scott/Downloads/Anime/_Fate kaleid liner Prisma Illya/7 - Season 2 - Specials/Fate kaleid liner Prisma Illya 2wei! - S00SP01.mkv'
# Try both forward and backslash versions
rows = cur.execute('''
  SELECT id, source_path, status, output_bitrate_kbps, error_tail
  FROM conversions 
  WHERE source_path LIKE ?
  ORDER BY id DESC LIMIT 5
''', ('%Fate kaleid liner Prisma Illya 2wei%',)).fetchall()

if rows:
  for r in rows:
    print(f'ID: {r[0]}, Status: {r[2]}, BitRate: {r[3]}')
    if r[4]:
      print(f'  Error tail: {r[4][:300]}')
else:
  print("No records found. Checking all _Fate files...")
  rows = cur.execute('''
    SELECT id, source_path, status, error_tail
    FROM conversions 
    WHERE source_path LIKE '%_Fate%'
    ORDER BY id DESC LIMIT 5
  ''').fetchall()
  for r in rows:
    print(f'ID: {r[0]}, Status: {r[2]}')
    print(f'  Path: {r[1][-80:]}')
    if r[3]:
      print(f'  Error: {r[3][:200]}')

conn.close()
