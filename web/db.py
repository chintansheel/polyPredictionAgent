"""SQLite user store."""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from web.config import USERS_DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  email TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_jobs (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  status TEXT NOT NULL,
  run_mode TEXT NOT NULL,
  topic TEXT,
  category TEXT,
  market_id TEXT,
  parent_run_id TEXT,
  force INTEGER DEFAULT 0,
  card_run_id TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_agent_jobs_user ON agent_jobs(user_id, created_at DESC);
"""


def _ensure_parent() -> None:
    USERS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def _conn():
    _ensure_parent()
    conn = sqlite3.connect(USERS_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _conn() as conn:
        conn.executescript(_SCHEMA)


def create_user(name: str, email: str, password_hash: str) -> dict[str, Any]:
    user_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO users (id, name, email, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, name, email.lower(), password_hash, created_at),
        )
    return {
        "id": user_id,
        "name": name,
        "email": email.lower(),
        "created_at": created_at,
    }


def get_user_by_email(email: str) -> dict[str, Any] | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT id, name, email, password_hash, created_at FROM users WHERE email = ?",
            (email.lower(),),
        ).fetchone()
    if row is None:
        return None
    return dict(row)


def get_user_by_id(user_id: str) -> dict[str, Any] | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT id, name, email, password_hash, created_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    if row is None:
        return None
    return dict(row)


def update_user_password(user_id: str, password_hash: str) -> bool:
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (password_hash, user_id),
        )
    return cur.rowcount > 0


def _job_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["force"] = bool(d.get("force"))
    return d


def create_job(
    *,
    user_id: str,
    run_mode: str,
    topic: str | None = None,
    category: str | None = None,
    market_id: str | None = None,
    parent_run_id: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    job_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO agent_jobs (
              id, user_id, status, run_mode, topic, category, market_id,
              parent_run_id, force, created_at
            ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                user_id,
                run_mode,
                topic,
                category,
                market_id,
                parent_run_id,
                1 if force else 0,
                created_at,
            ),
        )
    return get_job(job_id)  # type: ignore[return-value]


def get_job(job_id: str) -> dict[str, Any] | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM agent_jobs WHERE id = ?", (job_id,)
        ).fetchone()
    if row is None:
        return None
    return _job_row_to_dict(row)


def update_job(
    job_id: str,
    *,
    status: str | None = None,
    card_run_id: str | None = None,
    error: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> dict[str, Any] | None:
    fields: list[str] = []
    values: list[Any] = []
    if status is not None:
        fields.append("status = ?")
        values.append(status)
    if card_run_id is not None:
        fields.append("card_run_id = ?")
        values.append(card_run_id)
    if error is not None:
        fields.append("error = ?")
        values.append(error)
    if started_at is not None:
        fields.append("started_at = ?")
        values.append(started_at)
    if finished_at is not None:
        fields.append("finished_at = ?")
        values.append(finished_at)
    if not fields:
        return get_job(job_id)
    values.append(job_id)
    with _conn() as conn:
        conn.execute(
            f"UPDATE agent_jobs SET {', '.join(fields)} WHERE id = ?",
            values,
        )
    return get_job(job_id)


def list_jobs_for_user(user_id: str, limit: int = 20) -> list[dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM agent_jobs
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    return [_job_row_to_dict(r) for r in rows]


def count_jobs_today_for_user(user_id: str) -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM agent_jobs
            WHERE user_id = ?
              AND created_at >= ?
            """,
            (user_id, f"{today}T00:00:00"),
        ).fetchone()
    return int(row["n"]) if row else 0


def user_has_active_job(user_id: str) -> bool:
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM agent_jobs
            WHERE user_id = ? AND status IN ('queued', 'running')
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
    return row is not None


def count_active_jobs_global() -> int:
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM agent_jobs
            WHERE status IN ('queued', 'running')
            """
        ).fetchone()
    return int(row["n"]) if row else 0
