"""
Bulk-fix records that are status='failed' with an error_tail containing
'Output not smaller' — these should be 'no_saving'.
"""
import sqlite3, os, argparse

DB = os.path.join(os.path.dirname(__file__), '..', 'conversions.db')

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT id, source_path FROM conversions "
        "WHERE status='failed' AND error_tail LIKE '%Output not smaller%'"
    ).fetchall()

    print(f"Found {len(rows)} failed records with 'Output not smaller' in error_tail")
    for r in rows:
        print(f"  id={r['id']}  {r['source_path']}")

    if not args.dry_run and rows:
        conn.execute(
            "UPDATE conversions SET status='no_saving', error_tail=NULL, completed_at=datetime('now') "
            "WHERE status='failed' AND error_tail LIKE '%Output not smaller%'"
        )
        conn.commit()
        print(f"Updated {len(rows)} records -> no_saving")
    elif args.dry_run:
        print("(dry run — no changes made)")

    conn.close()

if __name__ == '__main__':
    main()
