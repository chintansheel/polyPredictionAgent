"""Web app configuration from environment."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
UI_DIR = ROOT / "ui"
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "./output")).resolve()
AUTH_SECRET = os.getenv("AUTH_SECRET", "dev-secret-change-in-production")
USERS_DB_PATH = Path(os.getenv("USERS_DB_PATH", "./state/users.db")).resolve()
WEB_PORT = int(os.getenv("WEB_PORT", "8000"))
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days
COOKIE_NAME = "session"
WEB_MAX_CONCURRENT_RUNS = int(os.getenv("WEB_MAX_CONCURRENT_RUNS", "1"))
WEB_RUNS_PER_USER_PER_DAY = int(os.getenv("WEB_RUNS_PER_USER_PER_DAY", "5"))

# Password reset (Brevo transactional email)
BREVO_API_KEY = os.getenv("BREVO_API_KEY", "")
BREVO_FROM_EMAIL = os.getenv("BREVO_FROM_EMAIL", "")
BREVO_FROM_NAME = os.getenv("BREVO_FROM_NAME", "Foretell")
RESET_PASSWORD_BASE_URL = os.getenv("RESET_PASSWORD_BASE_URL", "").rstrip("/")
RESET_TOKEN_MAX_AGE = int(os.getenv("RESET_TOKEN_MAX_AGE", "3600"))
