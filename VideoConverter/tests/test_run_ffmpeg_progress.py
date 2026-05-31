"""Regression tests for ffmpeg progress parsing in _run_ffmpeg()."""
import os
import sys
import threading
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import converter


class _FakeStdout:
    def __init__(self, text: str):
        self._text = text
        self._idx = 0

    def read(self, n: int = 1) -> str:
        if self._idx >= len(self._text):
            return ""
        chunk = self._text[self._idx:self._idx + n]
        self._idx += len(chunk)
        return chunk


class _FakeProc:
    def __init__(self, stdout_text: str, returncode: int = 0):
        self.stdout = _FakeStdout(stdout_text)
        self.returncode = returncode
        self.pid = 12345

    def poll(self):
        if self.stdout._idx >= len(self.stdout._text):
            return self.returncode
        return None

    def wait(self):
        return self.returncode

    def kill(self):
        self.returncode = -9


def test_run_ffmpeg_progress_from_carriage_return_lines():
    callbacks = []
    logs = []
    stop = threading.Event()

    out = (
        "frame=  100 fps=50.0 q=30.0 size=100kB time=00:00:10.00 bitrate=80.0kbits/s speed=1.00x\r"
        "frame=  200 fps=48.0 q=30.0 size=220kB time=00:00:20.00 bitrate=90.0kbits/s speed=0.95x\r"
    )

    with patch("converter.subprocess.Popen", return_value=_FakeProc(out, returncode=0)):
        ok = converter._run_ffmpeg(
            ["ffmpeg", "-y", "-i", "in.mkv", "out.mkv"],
            logs.append,
            stop,
            duration_secs=100.0,
            progress_cb=lambda pct, fps, eta: callbacks.append((pct, fps, eta)),
        )

    assert ok is True
    assert len(callbacks) >= 2
    assert callbacks[0][0] > 0
    assert callbacks[-1][0] > callbacks[0][0]


def test_run_ffmpeg_progress_from_key_value_events():
    callbacks = []
    logs = []
    stop = threading.Event()

    out = (
        "out_time_ms=10000000\n"
        "fps=50.0\n"
        "speed=1.25x\n"
        "progress=continue\n"
        "out_time_ms=60000000\n"
        "fps=52.0\n"
        "speed=1.30x\n"
        "progress=continue\n"
        "progress=end\n"
    )

    with patch("converter.subprocess.Popen", return_value=_FakeProc(out, returncode=0)):
        ok = converter._run_ffmpeg(
            ["ffmpeg", "-y", "-i", "in.mkv", "out.mkv"],
            logs.append,
            stop,
            duration_secs=100.0,
            progress_cb=lambda pct, fps, eta: callbacks.append((pct, fps, eta)),
        )

    assert ok is True
    assert len(callbacks) >= 2
    assert callbacks[0][0] > 0
    assert callbacks[-1][0] >= callbacks[0][0]
