"""
Central configuration for VideoConverter.
All tunable constants live here.
"""

# FFmpeg encoding quality
QSV_QUALITY = 30        # hevc_qsv global_quality (lower = better quality)
SW_HEVC_CRF = 28        # libx265 CRF fallback (lower = better quality)

# Local staging directory — keeps FFmpeg off OneDrive/network paths during encode
LOCAL_TEMP_DIR = r"C:\Temp\vc_working"

# Flask
FLASK_PORT = 5001
FLASK_DEBUG = False
