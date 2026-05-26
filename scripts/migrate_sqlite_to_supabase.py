#!/usr/bin/env python3
"""One-off: migrate Foretell SQLite users/jobs to shared Supabase Postgres.

Usage (from repo root, with DATABASE_URL in .env):
  python3 scripts/migrate_sqlite_to_supabase.py
  python3 scripts/migrate_sqlite_to_supabase.py --sqlite ./state/users.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

if sys.version_info < (3, 10):
    print(
        f"Python 3.10+ is required (you are running {sys.version}).\n"
        "Use: python3 scripts/migrate_sqlite_to_supabase.py",
        file=sys.stderr,
    )
    sys.exit(1)
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv

load_dotenv(_ROOT / ".env")

from web import db  # noqa: E402
from web.config import DATABASE_URL  # noqa: E402


def _sqlite_users(path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return list(
            conn.execute(
                "SELECT id, name, email, password_hash, created_at FROM users"
            ).fetchall()
        )
    finally:
        conn.close()


def _sqlite_jobs(path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute("SELECT * FROM agent_jobs").fetchall())
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate SQLite web DB to Supabase")
    parser.add_argument(
        "--sqlite",
        type=Path,
        default=_ROOT / "state" / "users.db",
        help="Path to Foretell SQLite file (default: ./state/users.db)",
    )
    args = parser.parse_args()

    if not DATABASE_URL:
        print("ERROR: DATABASE_URL is not set in .env", file=sys.stderr)
        sys.exit(1)
    if not args.sqlite.is_file():
        print(f"No SQLite file at {args.sqlite}; nothing to migrate.")
        return

    db.init_db()
    email_to_id: dict[str, str] = {}

    for row in _sqlite_users(args.sqlite):
        email = row["email"].lower()
        existing = db.get_user_by_email(email)
        if existing:
            email_to_id[email] = existing["id"]
            print(f"  user {email}: already in Supabase as id={existing['id']}")
            continue
        created = db.create_user(row["name"], email, row["password_hash"])
        email_to_id[email] = created["id"]
        print(f"  user {email}: inserted as id={created['id']}")

    jobs = _sqlite_jobs(args.sqlite)
    migrated = 0
    skipped = 0
    for job in jobs:
        old_uid = job["user_id"]
        email_for_uid = None
        for row in _sqlite_users(args.sqlite):
            if row["id"] == old_uid:
                email_for_uid = row["email"].lower()
                break
        if not email_for_uid:
            print(f"  job {job['id']}: skip (unknown user_id {old_uid})")
            skipped += 1
            continue
        new_uid = email_to_id.get(email_for_uid)
        if not new_uid:
            print(f"  job {job['id']}: skip (no mapped user for {email_for_uid})")
            skipped += 1
            continue
        if db.get_job(job["id"]):
            print(f"  job {job['id']}: already exists")
            skipped += 1
            continue
        from web.db import _conn  # noqa: PLC0415

        with _conn() as conn:
            conn.execute(
                """
                INSERT INTO agent_jobs (
                  id, user_id, status, run_mode, topic, category, market_id,
                  parent_run_id, force, card_run_id, error, created_at, started_at, finished_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    job["id"],
                    int(new_uid),
                    job["status"],
                    job["run_mode"],
                    job["topic"],
                    job["category"],
                    job["market_id"],
                    job["parent_run_id"],
                    bool(job["force"]),
                    job["card_run_id"],
                    job["error"],
                    job["created_at"],
                    job["started_at"],
                    job["finished_at"],
                ),
            )
        migrated += 1
        print(f"  job {job['id']}: migrated for user_id={new_uid}")

    print(f"Done. jobs migrated={migrated} skipped={skipped}")


if __name__ == "__main__":
    main()
