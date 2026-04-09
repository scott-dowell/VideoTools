from __future__ import annotations

import os


# Note: has_problematic_filename is kept in convert_videos.py as it handles
# more complex Unicode/special character checking specific to that GUI context.


def should_skip_folder(dirpath: str, skip_folders: list[str]) -> bool:
    """Check if folder should be skipped based on skip list."""
    return any(skip in dirpath for skip in skip_folders)


def passes_size_filter(size_mb: float, min_size: float, max_size: float) -> tuple[bool, str | None]:
    """Check if file size is within acceptable range.

    Returns:
        (passes, reason_if_skipped)
    """
    if size_mb < min_size:
        return False, f"below minimum size ({size_mb:.1f} MB < {min_size} MB)"
    if max_size > 0 and size_mb > max_size:
        return False, f"above maximum size ({size_mb:.1f} MB > {max_size} MB)"
    return True, None


def passes_duration_filter(duration: float | None, min_duration: float) -> tuple[bool, str | None]:
    """Check if video duration meets minimum requirement.

    Returns:
        (passes, reason_if_skipped)
    """
    if duration is None or duration < min_duration:
        dur_str = "unknown" if duration is None else f"{duration:.1f}s"
        return False, f"duration too short ({dur_str} < {min_duration}s)"
    return True, None


def handle_existing_conversion(full_path: str, output_path: str, summary: dict) -> tuple[str, dict | None]:
    """Handle case where converted file already exists.

    Returns:
        (action, stats_update_or_none)
        action: "skip", "replace_with_original", "remove_original", "error"
        stats_update: dict with size info if applicable
    """
    if not os.path.exists(output_path):
        return "none", None

    input_size = os.path.getsize(full_path)
    conv_size = os.path.getsize(output_path)
    saved_mb = (input_size - conv_size) / (1024**2)

    stats = {
        "input_size": input_size,
        "conv_size": conv_size,
        "saved_mb": saved_mb
    }

    if conv_size >= input_size:
        return "replace_with_original", stats
    else:
        return "remove_original", stats


def calculate_conversion_rate(size_mb: float, duration: float) -> float:
    """Calculate file bitrate in KB/s for prioritisation."""
    if duration <= 0:
        return 0.0
    size_kb = size_mb * 1024
    return size_kb / duration


def format_duration_hms(duration: float) -> str:
    """Format duration in seconds as HH:MM:SS."""
    hours = int(duration // 3600)
    minutes = int((duration % 3600) // 60)
    seconds = int(duration % 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"
