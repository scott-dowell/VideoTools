"""
Central configuration for VideoConverter.
All tunable constants live here.
"""

# Application version shown in the UI and used for release tracking.
APP_VERSION = "0.0.001"

# FFmpeg encoding quality
QSV_QUALITY = 30        # hevc_qsv global_quality (lower = better quality)
SW_HEVC_CRF = 28        # libx265 CRF fallback (lower = better quality)

# Local staging directory — keeps FFmpeg off OneDrive/network paths during encode
LOCAL_TEMP_DIR = r"C:\Temp\vc_working"

# Keep failed intermediate artifacts for post-mortem review.
# When enabled, failed remux/compress temp outputs are moved into
# LOCAL_TEMP_DIR\_failed_intermediates\<file>_<timestamp>\
KEEP_FAILED_INTERMEDIATES = False

# AV1 policy (anime mode): re-encode for meaningful size savings instead of
# stream-copying the AV1 video into MP4.
REENCODE_AV1 = True
AV1_QSV_QUALITY = 27

# Estimate sampling policy.
# Fractions are clip centers in [0,1] of timeline position.
# Default: middle of first third (1/6) and middle of last third (5/6).
ESTIMATE_SAMPLE_FRACTIONS = (1 / 6, 5 / 6)
ESTIMATE_CLIP_SECS = 15.0

# Flask
FLASK_PORT = 5001
FLASK_DEBUG = False
