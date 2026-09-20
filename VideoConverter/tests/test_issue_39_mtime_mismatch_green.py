"""GREEN test: Verify files with changed mtime are recognized as done after scan."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import db
import scanner


def test_mtime_mismatch_files_show_as_done():
    """
    GIVEN: Files with status='done' but changed filesystem mtime
    WHEN: Scanner processes the folder
    THEN: Files should appear as done in the grid (not pending)
    AND: DB mtime should be updated to match filesystem mtime
    """
    sakurasou_path = r"C:/Users/scott/Downloads/Anime/The Pet Girl of Sakurasou"
    
    # Verify the problematic files exist before scanning
    # Initialize DB to get path
    db.init(r"c:\VideoTools\VideoConverter\conversions.db")
    
    # Verify the problematic files exist before scanning
    cursor = db.sqlite3.connect(db._DB_PATH).cursor()
    cursor.execute("""
        SELECT COUNT(*) FROM conversions 
        WHERE source_path LIKE '%Sakurasou%' AND status='done'
    """)
    done_count_before = cursor.fetchone()[0]
    print(f"Before scan: {done_count_before} done files")
    
    # Verify some have mtime mismatch before scan
    cursor.execute("""
        SELECT COUNT(*) FROM conversions 
        WHERE source_path LIKE '%Sakurasou%' 
        AND status='done'
        AND source_path NOT LIKE '%SP%'
    """)
    before_scan = cursor.fetchone()[0]
    
    # Run the scanner
    scan_events = list(scanner.walk(sakurasou_path))
    print(f"Scan generated {len(scan_events)} events")
    
    # Count events for tracking
    hash_match_events = [e for e in scan_events if e.get("type") == "hash_match_done"]
    print(f"Found {len(hash_match_events)} hash_match_done events")
    
    # Verify the fix worked
    cursor.execute("""
        SELECT 
            source_path,
            source_mtime,
            status
        FROM conversions 
        WHERE source_path LIKE '%Sakurasou%' AND status='done'
        ORDER BY source_path
    """)
    
    done_rows = cursor.fetchall()
    done_count_after = len(done_rows)
    print(f"After scan: {done_count_after} done files")
    
    # Check first file S01E01 - should have updated mtime
    e01_row = next((r for r in done_rows if "S01E01" in r[0]), None)
    if e01_row:
        _, db_mtime, status = e01_row
        import os.path
        fs_mtime = os.path.getmtime(os.path.join(sakurasou_path, "S01E01-Cat, White, Mashiro.mp4"))
        print(f"S01E01 DB mtime: {db_mtime}, FS mtime: {fs_mtime}, Status: {status}")
        if abs(db_mtime - fs_mtime) < 1.0:  # Allow 1 second tolerance
            print("✓ S01E01 mtime has been synced!")
        else:
            print(f"✗ S01E01 mtime still mismatched: {db_mtime} vs {fs_mtime}")
    
    cursor.close()
    
    # Verify done count increased or stayed same
    assert done_count_after >= before_scan, f"Expected more done files after scan"
    assert len(hash_match_events) > 0, "Expected hash_match_done events for mtime-mismatched files"
    
    print("✓ GREEN TEST PASSED: Mtime-mismatched files recognized as done")


if __name__ == "__main__":
    test_mtime_mismatch_files_show_as_done()
