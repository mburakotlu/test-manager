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

from app_paths import APP_DIR

if getattr(sys, "frozen", False):
    os.chdir(APP_DIR)

from webapp import app

if __name__ == "__main__":
    if getattr(sys, "frozen", False):
        threading.Timer(1.5, webbrowser.open, args=("http://127.0.0.1:5000/",)).start()
    app.run(host="127.0.0.1", port=5000, debug=not getattr(sys, "frozen", False), use_reloader=False)
