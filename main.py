"""Single entry point to start the jira_manager web UI.

Run with:
    python main.py
(or ``.venv\\Scripts\\python.exe main.py`` if your shell's default ``python``
isn't the project's virtualenv). Then open http://127.0.0.1:5000/ in a browser.

``.env`` is loaded automatically (see webapp.py) — no need to export env vars
into the shell manually before running this.
"""

from __future__ import annotations

import os
import sys
import threading
import webbrowser
from getpass import getpass
import secrets

from app_paths import APP_DIR

if getattr(sys, "frozen", False):
    os.chdir(APP_DIR)

from dotenv import load_dotenv

load_dotenv(APP_DIR / ".env")


def run_setup_command() -> bool:
    if len(sys.argv) < 2:
        return False
    command = sys.argv[1]
    if command == "--generate-secret":
        print(secrets.token_urlsafe(48))
        return True
    if command == "--bootstrap-admin":
        from planner_store import create_first_admin, initialize_database

        initialize_database()
        username = input("Admin username: ").strip()
        password = getpass("Admin password (minimum 12 characters): ")
        if password != getpass("Confirm password: "):
            raise SystemExit("Passwords do not match.")
        try:
            create_first_admin(username, password)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        print(f"Administrator {username!r} created.")
        return True
    if command == "--import-workbook":
        from import_planner_workbook import main as import_workbook

        sys.argv = [sys.argv[0], *sys.argv[2:]]
        import_workbook()
        return True
    return False


if __name__ == "__main__":
    if run_setup_command():
        raise SystemExit(0)
    if len(sys.argv) > 1:
        raise SystemExit("Usage: JiraManager.exe [--generate-secret | --bootstrap-admin | --import-workbook [--confirm]]")
    from webapp import app

    if getattr(sys, "frozen", False):
        threading.Timer(1.5, webbrowser.open, args=("http://127.0.0.1:5000/",)).start()
    app.run(
        host=os.environ.get("APP_HOST", "127.0.0.1"),
        port=int(os.environ.get("APP_PORT", "5000")),
        debug=os.environ.get("APP_DEBUG", "").strip().lower() in ("1", "true", "yes"),
        use_reloader=False,
    )
