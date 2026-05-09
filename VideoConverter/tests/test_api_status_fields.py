"""Confirm /api/status always includes the new structured fields."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import json
import app as _app

client = _app.app.test_client()


def test_idle_status_has_new_fields():
    # Reset to a clean idle state before checking
    with _app._job_lock:
        _app._job['phase']     = ''
        _app._job['steps']     = []
        _app._job['ocr_batch'] = {'total': 0, 'done': 0, 'current_file': '', 'files': []}
    r = client.get('/api/status')
    data = json.loads(r.data)
    assert 'phase'     in data, "missing 'phase'"
    assert 'ocr_batch' in data, "missing 'ocr_batch'"
    assert 'steps'     in data, "missing 'steps'"
    assert data['phase'] == ''
    assert data['steps'] == []
    assert isinstance(data['ocr_batch'], dict)
    assert data['ocr_batch']['total'] == 0


def test_ocr_batch_phase_structure():
    with _app._job_lock:
        _app._job['phase'] = 'ocr_batch'
        _app._job['ocr_batch'] = {
            'total': 3, 'done': 1, 'current_file': 'foo.mkv',
            'files': [
                {'name': 'a.mkv',   'state': 'done'},
                {'name': 'foo.mkv', 'state': 'running'},
                {'name': 'b.mkv',   'state': 'waiting'},
            ]
        }
    r = client.get('/api/status')
    data = json.loads(r.data)
    assert data['phase'] == 'ocr_batch'
    assert data['ocr_batch']['total'] == 3
    assert data['ocr_batch']['done']  == 1
    assert data['ocr_batch']['current_file'] == 'foo.mkv'
    assert len(data['ocr_batch']['files']) == 3
    # Reset
    with _app._job_lock:
        _app._job['phase']     = ''
        _app._job['ocr_batch'] = {'total': 0, 'done': 0, 'current_file': '', 'files': []}


def test_converting_phase_with_steps():
    sample_steps = [
        {"id": "compress", "label": "Compress", "state": "running",  "detail": "hevc_qsv", "attempt": 1},
        {"id": "remux",    "label": "Remux",    "state": "waiting",  "detail": "",          "attempt": 1},
        {"id": "verify",   "label": "Verify",   "state": "waiting",  "detail": "",          "attempt": 1},
    ]
    with _app._job_lock:
        _app._job['phase'] = 'converting'
        _app._job['steps'] = [dict(s) for s in sample_steps]
    r = client.get('/api/status')
    data = json.loads(r.data)
    assert data['phase'] == 'converting'
    assert len(data['steps']) == 3
    assert data['steps'][0]['id']    == 'compress'
    assert data['steps'][0]['state'] == 'running'
    # Reset
    with _app._job_lock:
        _app._job['phase'] = ''
        _app._job['steps'] = []
