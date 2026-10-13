"""Paths for editable application data in source and packaged runs."""

from pathlib import Path
import sys

# Single source of truth for the app version — shown in the web UI and baked
# into JiraManager.exe's file properties by build_exe.py.
APP_VERSION = "1.1.0"

APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
