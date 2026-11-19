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

from app_paths import APP_VERSION

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "dist" / "JiraManager"
VENV_PACKAGES = ROOT / ".venv" / "Lib" / "site-packages"
VERSION_FILE = ROOT / "build" / "version_info.txt"


def _version_tuple() -> tuple[int, int, int, int]:
    parts = [int(p) for p in APP_VERSION.split(".")]
    parts += [0] * (4 - len(parts))
    return tuple(parts[:4])  # type: ignore[return-value]


def _write_version_file() -> None:
    """Write a PyInstaller version-info file so JiraManager.exe's Windows
    file properties (Details tab) show APP_VERSION."""
    v = _version_tuple()
    VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    VERSION_FILE.write_text(
        f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={v}, prodvers={v}),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('FileVersion', '{APP_VERSION}'),
      StringStruct('ProductVersion', '{APP_VERSION}'),
      StringStruct('ProductName', 'Jira Manager'),
      StringStruct('FileDescription', 'Jira Manager'),
    ])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])]),
  ]
)""",
        encoding="utf-8",
    )


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("Build this Windows EXE on Windows.")
    if not VENV_PACKAGES.is_dir():
        raise SystemExit("Install requirements.txt into .venv before building.")
    if OUTPUT.exists():
        raise SystemExit(f"Move or remove the existing output first: {OUTPUT}")

    _write_version_file()
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
            "--version-file", str(VERSION_FILE),
            str(ROOT / "main.py"),
        ],
        cwd=ROOT,
        check=True,
    )

    for directory in ("templates",):
        shutil.copytree(ROOT / directory, OUTPUT / directory)
    for filename in ("DLCM_FZG_Planung.xlsx", "register.yaml", ".env.example"):
        shutil.copy2(ROOT / filename, OUTPUT / filename)
    (OUTPUT / "START_HERE.txt").write_text(
        f"Jira Manager v{APP_VERSION}\n\n"
        "1. Copy .env.example to .env in this folder.\n"
        "2. Run JiraManager.exe --generate-secret and put the output in APP_SECRET_KEY in .env.\n"
        "3. Add the automation Jira credentials to .env; keep the token private.\n"
        "4. Run JiraManager.exe --import-workbook to preview, then add --confirm for a one-time import.\n"
        "5. Run JiraManager.exe --bootstrap-admin to create the first app administrator.\n"
        "6. Double-click JiraManager.exe. The local web app opens in your browser.\n\n"
        "Keep the whole JiraManager folder together. Do not share .env or .auth.\n"
        "For team use, deploy behind approved HTTPS on a persistent host; do not expose the built-in server.\n"
        "For browser checklist features, install Chromium once with:\n"
        "    python -m playwright install chromium\n"
        "Close the console window to stop the app.\n",
        encoding="utf-8",
    )
    print(f"Built {OUTPUT / 'JiraManager.exe'}")


if __name__ == "__main__":
    main()
