"""Build a Windows folder containing JiraManager.exe and editable app data.

Run from the repository root with ``python build_exe.py``. The project .venv
must already have the packages from requirements.txt installed, and PyInstaller
must be available to the Python used for this build script.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "dist" / "JiraManager"
VENV_PACKAGES = ROOT / ".venv" / "Lib" / "site-packages"


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("Build this Windows EXE on Windows.")
    if not VENV_PACKAGES.is_dir():
        raise SystemExit("Install requirements.txt into .venv before building.")
    if OUTPUT.exists():
        raise SystemExit(f"Move or remove the existing output first: {OUTPUT}")

    subprocess.run(
        [
            sys.executable,
            "-m", "PyInstaller",
            "--onedir",
            "--name", "JiraManager",
            "--distpath", str(ROOT / "dist"),
            "--workpath", str(ROOT / "build"),
            "--specpath", str(ROOT / "build"),
            "--paths", str(VENV_PACKAGES),
            "--collect-all", "playwright",
            "--hidden-import", "playwright.sync_api",
            str(ROOT / "main.py"),
        ],
        cwd=ROOT,
        check=True,
    )

    for directory in ("vehicles", "templates"):
        shutil.copytree(ROOT / directory, OUTPUT / directory)
    for filename in ("DLCM_FZG_Planung.xlsx", "register.yaml", ".env.example"):
        shutil.copy2(ROOT / filename, OUTPUT / filename)
    (OUTPUT / "START_HERE.txt").write_text(
        "Jira Manager\n\n"
        "1. Copy .env.example to .env in this folder.\n"
        "2. Edit .env with your Jira URL, email and personal access token.\n"
        "3. Double-click JiraManager.exe. The local web app opens in your browser.\n\n"
        "Keep the whole JiraManager folder together. Do not share .env or .auth.\n"
        "For browser checklist features, install Chromium once with:\n"
        "    python -m playwright install chromium\n"
        "Close the console window to stop the app.\n",
        encoding="utf-8",
    )
    print(f"Built {OUTPUT / 'JiraManager.exe'}")


if __name__ == "__main__":
    main()
