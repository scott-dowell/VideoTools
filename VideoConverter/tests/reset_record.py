"""Reset a single conversion record back to pending by id."""
import argparse, sqlite3, os

DB = os.path.join(os.path.dirname(__file__), '..', 'conversions.db')

def main():
    p = argparse.ArgumentParser()
    p.add_argument('id', type=int)
    args = p.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute('SELECT id, source_path, status FROM conversions WHERE id=?', (args.id,)).fetchone()
    if not row:
        print(f'No record with id={args.id}')
        conn.close()
        return
    print(f'  id={row["id"]}  status={row["status"]}  path={row["source_path"]}')
    conn.execute(
        "UPDATE conversions SET status='pending', error_tail=NULL, started_at=NULL, completed_at=NULL WHERE id=?",
        (args.id,)
    )
    conn.commit()
    print(f'Reset to pending.')
    conn.close()

if __name__ == '__main__':
    main()
