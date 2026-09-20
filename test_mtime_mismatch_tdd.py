#!/usr/bin/env python3
"""
TEST: Files with changed mtime should not appear as pending on next scan

Issue: When a file has status='done' in DB but the filesystem mtime has changed,
the file should NOT appear as "pending" in the grid. Instead:
1. It should be hash-checked to verify it's the same file
2. If hash matches, mtime should be updated in DB
3. It should appear as "done", not "pending"

This test reproduces the Sakurasou issue where S01E01-E18 show as pending
despite being marked as done in the database.
"""
import sys
import os
import tempfile
import shutil
import sqlite3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'VideoConverter'))

import db
import scanner

def test_mtime_changed_file_appears_as_done():
    """Test that a done file with changed mtime doesn't show as pending."""
    
    # Use a temporary database for this test
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        test_db_path = f.name
    
    try:
        # Initialize test DB
        db.init_db(test_db_path)
        
        # Create a dummy video file
        with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as f:
            test_video_path = f.name
            # Write minimal MP4 header so file isn't empty
            f.write(b'\x00\x00\x00\x20ftypisom')
        
        try:
            # Record the original mtime
            original_mtime = os.path.getmtime(test_video_path)
            
            # Simulate: insert a done record with old mtime
            old_mtime = original_mtime - 100000  # 100k seconds in past
            fp_normalized = test_video_path.replace('\\', '/')
            
            conn = sqlite3.connect(test_db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("""
                INSERT INTO conversions 
                (source_path, source_mtime, status, source_codec, source_bitrate_kbps,
                 source_duration_secs, source_video_track_count, source_audio_track_count,
                 source_subtitle_track_count, completed_at, output_size_mb, saved_mb, saved_pct)
                VALUES (?, ?, 'done', 'h264', 5000, 3600, 1, 2, 0, ?, 100, 50, 33)
            """, (fp_normalized, old_mtime, '2026-08-24T12:00:00+00:00'))
            conn.commit()
            conn.close()
            
            print(f"Setup: Created done record with old mtime")
            print(f"  File: {os.path.basename(test_video_path)}")
            print(f"  DB mtime: {old_mtime}")
            print(f"  Filesystem mtime: {original_mtime}")
            print(f"  Mtime mismatch: {abs(original_mtime - old_mtime):.1f} seconds")
            print()
            
            # Now simulate: scan the folder and collect folder events
            # This simulates what the /api/scan endpoint does
            folder = os.path.dirname(test_video_path)
            
            folder_events = []
            pending_files = []
            done_files = []
            
            for event in scanner.walk(folder):
                if event.get('type') == 'folder':
                    folder_events.append(event)
                    # Collect the files in this folder event
                    for f in event.get('files', []):
                        if f.get('status') == 'pending':
                            pending_files.append(f)
                        elif f.get('status') == 'done':
                            done_files.append(f)
            
            # The test: verify the file appears as done, not pending
            test_file_found = False
            for f in done_files:
                if os.path.basename(test_video_path) in f.get('full_path', ''):
                    test_file_found = True
                    print(f"✓ PASS: File appears in done list")
                    break
            
            for f in pending_files:
                if os.path.basename(test_video_path) in f.get('full_path', ''):
                    print(f"✗ FAIL: File appears in pending list (should be done)")
                    test_file_found = True  # Found it, but in wrong place
                    return False
            
            if not test_file_found:
                print(f"✗ FAIL: File not found in either list")
                print(f"  Done files: {[os.path.basename(f.get('full_path', '')) for f in done_files]}")
                print(f"  Pending files: {[os.path.basename(f.get('full_path', '')) for f in pending_files]}")
                return False
            
            return True
            
        finally:
            if os.path.exists(test_video_path):
                os.unlink(test_video_path)
    finally:
        if os.path.exists(test_db_path):
            os.unlink(test_db_path)


if __name__ == '__main__':
    success = test_mtime_changed_file_appears_as_done()
    sys.exit(0 if success else 1)
