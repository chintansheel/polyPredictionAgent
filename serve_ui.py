"""Local web UI: run analysis from a keyword or Polymarket URL; view card + trace.

Run from the package root (this directory)::

    python serve_ui.py

Then open http://127.0.0.1:5050/ (port overridable with ``UI_PORT``).
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.chdir(ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from flask import Flask, jsonify, request, send_from_directory  # noqa: E402

from agent.run_input import parse_keyword_or_polymarket_url  # noqa: E402

app = Flask(__name__, static_folder="ui", static_url_path="/static")
_run_lock = threading.Lock()


@app.get("/")
def index() -> object:
    # Standalone local UI (no auth). Foretell web app uses ui/index.html at /app.
    return send_from_directory(ROOT / "ui", "local-dev.html")


@app.get("/api/feed")
def api_feed() -> object:
    path = ROOT / "output" / "feed.json"
    if not path.exists():
        return jsonify([])
    try:
        return jsonify(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return jsonify([])


@app.get("/api/scorecard")
def api_scorecard() -> object:
    path = ROOT / "output" / "scorecard.json"
    if not path.exists():
        return jsonify({}), 404
    try:
        return jsonify(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return jsonify({}), 404


@app.get("/api/trace/<run_id>")
def api_trace(run_id: str) -> object:
    traces_dir = ROOT / os.getenv("TRACES_DIR", "traces")
    path = traces_dir / f"trace_{run_id}.json"
    if not path.is_file():
        return jsonify({}), 404
    try:
        return jsonify(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return jsonify({}), 404


@app.post("/api/run")
def api_run() -> object:
    body = request.get_json(silent=True) or {}
    q = (body.get("query") or "").strip()
    if not q:
        return jsonify({"ok": False, "error": "Enter a search keyword or a Polymarket event URL."}), 400

    keyword, market_id = parse_keyword_or_polymarket_url(q)
    if not keyword and not market_id:
        return jsonify(
            {
                "ok": False,
                "error": (
                    "Could not read that Polymarket link. "
                    "Use an address like https://polymarket.com/event/your-event-slug "
                    "or paste a search keyword instead."
                ),
            }
        ), 400

    if not _run_lock.acquire(blocking=False):
        return jsonify({"ok": False, "error": "Another analysis is still running. Try again when it finishes."}), 429

    try:
        from agent.orchestrator import run_once  # noqa: E402

        card = run_once(
            keyword_filter=keyword,
            market_id_override=market_id,
            force=True,
        )
        if not card:
            return jsonify(
                {
                    "ok": False,
                    "error": "No market matched that input, or the run stopped before producing a card.",
                }
            )

        run_id = card.get("id")
        traces_dir = ROOT / os.getenv("TRACES_DIR", "traces")
        trace_path = traces_dir / f"trace_{run_id}.json"
        trace_obj = None
        if trace_path.is_file():
            trace_obj = json.loads(trace_path.read_text(encoding="utf-8"))

        return jsonify({"ok": True, "card": card, "trace": trace_obj})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        _run_lock.release()


def main() -> None:
    port = int(os.getenv("UI_PORT", "5050"))
    print(f"Serving UI at http://127.0.0.1:{port}/  (cwd={ROOT})")
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
