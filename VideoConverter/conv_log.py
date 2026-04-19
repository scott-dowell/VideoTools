"""
conv_log.py  —  Per-conversion structured logging for VideoConverter.

Directory layout  (LOG_DIR = VideoConverter/logs/):

    logs/
        conversion_history.log          # one line per conversion, always appended
        <safe_stem>_<YYYYMMDD_HHMMSS>/  # per-conversion dir — deleted on success
            compress_qsv.log            # full ffmpeg output for QSV compress phase
            compress_sw.log             # full ffmpeg output for SW compress phase
            remux_attempt_1.log         # full ffmpeg output for each remux try
            remux_attempt_2.log
            ...
            audio_track_1_aac_mf.log    # raw stderr for each audio pre-encode attempt
            audio_track_1_native_aac.log
            ...

The history line format is:
    2026-04-19 10:23 | DONE     | <stem>                                            | compress_qsv -> remux_attempt_1 | 67% saved  [42s]
    2026-04-19 10:45 | FAILED   | <stem>                                            | compress_qsv -> remux_attempt_3 | FAILED at: audio track 2 pre-encode  [183s]

Usage in converter.py:
    clog = ConversionLogger(input_path)

    # Inline logging (compress — uses _run_ffmpeg which takes a log callable):
    qsv_log = clog.tee(log, "compress_qsv")
    _run_ffmpeg(cmd, qsv_log, ...)

    # Batch logging (remux — output_lines are accumulated then dumped):
    clog.write_phase("remux_attempt_1", "".join(output_lines))

    # Sub-step raw stderr (no entry in history phases list):
    clog.write_step("audio_track_1_aac_mf", r.stderr or "")

    # Mark the failure point for the history line:
    clog.mark_fail_at("audio track 2 pre-encode (aac_mf rc=3221225477, native aac also failed)")

    # Finalize from app.py after the final outcome is known:
    clog.success("hevc_qsv", saved_pct=67)   # writes history + deletes run dir
    clog.failure()                            # writes history + keeps run dir
"""
from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Callable

LOG_DIR: str = os.path.join(os.path.dirname(__file__), "logs")

LogFn = Callable[[str], None]


class ConversionLogger:
    def __init__(self, video_path: str, logs_dir: str = LOG_DIR) -> None:
        self.logs_dir = logs_dir
        os.makedirs(logs_dir, exist_ok=True)

        stem = Path(video_path).stem
        # Sanitize for a safe directory name, keep it recognisable
        safe = "".join(
            c if c.isalnum() or c in " _-." else "_" for c in stem
        )[:60].rstrip("_. ")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        self.run_dir      = Path(logs_dir) / f"{safe}_{ts}"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.history_path = Path(logs_dir) / "conversion_history.log"
        self.video_stem   = stem
        self._start       = datetime.now()
        self._fail_at     = ""
        self._phases: list[str] = []   # major phases in order (for history line)
        self._finalized   = False       # prevents double history writes

    # ------------------------------------------------------------------
    # Logging helpers
    # ------------------------------------------------------------------

    def tee(self, base_log: LogFn, phase_name: str) -> LogFn:
        """
        Register *phase_name* as a major phase and return a LogFn that both
        calls *base_log* AND appends each message to <phase_name>.log.

        Use for inline logging phases (compress) where the log fn is passed
        into _run_ffmpeg.
        """
        if phase_name not in self._phases:
            self._phases.append(phase_name)
        log_path = self.run_dir / f"{phase_name}.log"

        def _log(msg: str) -> None:
            base_log(msg)
            with open(log_path, "a", encoding="utf-8", errors="replace") as fh:
                fh.write(msg + "\n")

        return _log

    def write_phase(self, phase_name: str, content: str) -> None:
        """
        Register *phase_name* as a major phase and write *content* to its log.

        Use for batch-output phases (remux) where ffmpeg output is collected
        into a list then dumped at once.
        """
        if phase_name not in self._phases:
            self._phases.append(phase_name)
        log_path = self.run_dir / f"{phase_name}.log"
        with open(log_path, "a", encoding="utf-8", errors="replace") as fh:
            fh.write(content)
            if content and not content.endswith("\n"):
                fh.write("\n")

    def write_step(self, step_name: str, content: str) -> None:
        """
        Write *content* to a step log file WITHOUT registering it as a major
        phase.

        Use for sub-step raw output (audio pre-encode stderr) that should be
        available for diagnosis but not cluttering the history line.
        """
        log_path = self.run_dir / f"{step_name}.log"
        with open(log_path, "a", encoding="utf-8", errors="replace") as fh:
            fh.write(content)
            if content and not content.endswith("\n"):
                fh.write("\n")

    def mark_fail_at(self, location: str) -> None:
        """Record where the failure occurred (written into the history line)."""
        if not self._fail_at:   # keep the first (innermost) location
            self._fail_at = location

    # ------------------------------------------------------------------
    # Finalise — called from app.py after the final outcome is known
    # ------------------------------------------------------------------

    def _write_history(self, status: str, detail: str) -> None:
        elapsed = int((datetime.now() - self._start).total_seconds())
        ts      = datetime.now().strftime("%Y-%m-%d %H:%M")
        phases  = " -> ".join(self._phases) if self._phases else "?"
        line    = (
            f"{ts} | {status:<8} | {self.video_stem[:50]:<50} | "
            f"{phases} | {detail}  [{elapsed}s]\n"
        )
        with open(self.history_path, "a", encoding="utf-8", errors="replace") as fh:
            fh.write(line)

    def success(self, encoder: str, saved_pct: int) -> None:
        """Write DONE history entry and delete the per-conversion log dir."""
        if self._finalized:
            return
        self._finalized = True
        self._write_history("DONE", f"{saved_pct}% saved")
        shutil.rmtree(self.run_dir, ignore_errors=True)

    def failure(self, reason: str = "") -> None:
        """Write FAILED history entry and keep the per-conversion log dir."""
        if self._finalized:
            return
        self._finalized = True
        at = self._fail_at or reason or "unknown"
        self._write_history("FAILED", f"FAILED at: {at}")
        # run_dir is kept intentionally — it holds the diagnosis logs
