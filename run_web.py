#!/usr/bin/env python3
"""Start the Foretell web app (landing, auth, protected feed).

Usage:
    python3 run_web.py

Do not use `python -m http.server` - that only serves static files and breaks
/signup, /login, /app, and the auth API.
"""

from __future__ import annotations

import os
import sys

if sys.version_info < (3, 10):
    print(
        f"Python 3.10+ is required (you are running {sys.version}).\n"
        "Use: python3 run_web.py",
        file=sys.stderr,
    )
    sys.exit(1)

from dotenv import load_dotenv

if __name__ == "__main__":
    load_dotenv()
    if not os.getenv("AUTH_SECRET"):
        print(
            "Warning: AUTH_SECRET is not set in .env — using a dev default. "
            "Set AUTH_SECRET for production.",
            file=sys.stderr,
        )
    if not os.getenv("BREVO_API_KEY"):
        print(
            "Warning: BREVO_API_KEY is not set — password reset emails will not send.",
            file=sys.stderr,
        )
    if not os.getenv("RESET_PASSWORD_BASE_URL"):
        print(
            "Warning: RESET_PASSWORD_BASE_URL is not set — reset links in emails will not work.",
            file=sys.stderr,
        )

    import socket

    import uvicorn

    from web.config import WEB_PORT

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(("127.0.0.1", WEB_PORT)) == 0:
            print(f"Error: port {WEB_PORT} is already in use.", file=sys.stderr)
            print(
                f"If Foretell is already running, open http://localhost:{WEB_PORT}/",
                file=sys.stderr,
            )
            print(
                f"To stop whatever is using the port:\n"
                f"  fuser -k {WEB_PORT}/tcp\n"
                f"  # or: pkill -f 'python run_web.py'",
                file=sys.stderr,
            )
            sys.exit(1)

    print(f"Starting Foretell at http://localhost:{WEB_PORT}/")
    uvicorn.run(
        "web.app:app",
        host="0.0.0.0",
        port=WEB_PORT,
        reload=True,
    )
