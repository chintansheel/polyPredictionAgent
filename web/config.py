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
DATABASE_URL = os.getenv("DATABASE_URL", "")
WEB_PORT = int(os.getenv("WEB_PORT", "8970"))
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days
COOKIE_NAME = "session"
WEB_MAX_CONCURRENT_RUNS = int(os.getenv("WEB_MAX_CONCURRENT_RUNS", "1"))
WEB_RUNS_PER_USER_PER_DAY = int(os.getenv("WEB_RUNS_PER_USER_PER_DAY", "5"))

# Shared Supabase public.users column mapping (orchestrator app schema)
USERS_TABLE = os.getenv("USERS_TABLE", "users")
USERS_COL_ID = os.getenv("USERS_COL_ID", "id")
USERS_COL_EMAIL = os.getenv("USERS_COL_EMAIL", "email")
USERS_COL_NAME = os.getenv("USERS_COL_NAME", "username")
USERS_COL_PASSWORD = os.getenv("USERS_COL_PASSWORD", "password_hash")
USERS_COL_CREATED = os.getenv("USERS_COL_CREATED", "created_at")

# Password reset (Brevo transactional email)
BREVO_API_KEY = os.getenv("BREVO_API_KEY", "")
BREVO_FROM_EMAIL = os.getenv("BREVO_FROM_EMAIL", "")
BREVO_FROM_NAME = os.getenv("BREVO_FROM_NAME", "Foretell")
RESET_PASSWORD_BASE_URL = os.getenv("RESET_PASSWORD_BASE_URL", "").rstrip("/")
RESET_TOKEN_MAX_AGE = int(os.getenv("RESET_TOKEN_MAX_AGE", "3600"))
