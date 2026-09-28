"""Single entry point to start the jira_manager web UI.

Run with:
    python main.py
(or ``.venv\\Scripts\\python.exe main.py`` if your shell's default ``python``
isn't the project's virtualenv). Then open http://127.0.0.1:5000/ in a browser.

``.env`` is loaded automatically (see webapp.py) — no need to export env vars
into the shell manually before running this.
"""

from __future__ import annotations

from webapp import app

if __name__ == "__main__":
    app.run(debug=True)
