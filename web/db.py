"""Supabase Postgres user store and agent jobs."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from web.config import (
    DATABASE_URL,
    USERS_COL_CREATED,
    USERS_COL_EMAIL,
    USERS_COL_ID,
    USERS_COL_NAME,
    USERS_COL_PASSWORD,
    USERS_TABLE,
)

_pool: ConnectionPool | None = None


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL is not set. Add your Supabase Postgres pooler URI to .env"
            )
        _pool = ConnectionPool(
            DATABASE_URL,
            min_size=1,
            max_size=5,
            kwargs={"row_factory": dict_row},
        )
    return _pool


@contextmanager
def _conn() -> Iterator[Any]:
    with _get_pool().connection() as conn:
        yield conn


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


def _parse_user_id(user_id: str | int) -> int:
    if isinstance(user_id, int):
        return user_id
    return int(user_id)


def _user_select_sql() -> str:
    return (
        f"SELECT {USERS_COL_ID}, {USERS_COL_NAME} AS name, {USERS_COL_EMAIL}, "
        f"{USERS_COL_PASSWORD}, {USERS_COL_CREATED} FROM {USERS_TABLE}"
    )


def _normalize_user_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row[USERS_COL_ID]),
        "name": row.get("name") or "",
        "email": row[USERS_COL_EMAIL],
        "password_hash": row[USERS_COL_PASSWORD],
        "created_at": _iso(row.get(USERS_COL_CREATED)) or "",
    }


def init_db() -> None:
    with _conn() as conn:
        conn.execute("SELECT 1")


def create_user(name: str, email: str, password_hash: str) -> dict[str, Any]:
    email_lower = email.lower()
    with _conn() as conn:
        row = conn.execute(
            f"""
            INSERT INTO {USERS_TABLE} ({USERS_COL_EMAIL}, {USERS_COL_PASSWORD}, {USERS_COL_NAME})
            VALUES (%s, %s, %s)
            RETURNING {USERS_COL_ID}, {USERS_COL_NAME} AS name, {USERS_COL_EMAIL}, {USERS_COL_CREATED}
            """,
            (email_lower, password_hash, name),
        ).fetchone()
    if row is None:
        raise RuntimeError("Failed to create user")
    user = {
        "id": str(row[USERS_COL_ID]),
        "name": name,
        "email": row[USERS_COL_EMAIL],
        "created_at": _iso(row.get(USERS_COL_CREATED)) or "",
    }
    return user


def get_user_by_email(email: str) -> dict[str, Any] | None:
    with _conn() as conn:
        row = conn.execute(
            f"{_user_select_sql()} WHERE {USERS_COL_EMAIL} = %s",
            (email.lower(),),
        ).fetchone()
    if row is None:
        return None
    return _normalize_user_row(row)


def get_user_by_id(user_id: str | int) -> dict[str, Any] | None:
    with _conn() as conn:
        row = conn.execute(
            f"{_user_select_sql()} WHERE {USERS_COL_ID} = %s",
            (_parse_user_id(user_id),),
        ).fetchone()
    if row is None:
        return None
    return _normalize_user_row(row)


def update_user_password(user_id: str | int, password_hash: str) -> bool:
    with _conn() as conn:
        cur = conn.execute(
            f"""
            UPDATE {USERS_TABLE}
            SET {USERS_COL_PASSWORD} = %s, updated_at = now()
            WHERE {USERS_COL_ID} = %s
            """,
            (password_hash, _parse_user_id(user_id)),
        )
    return cur.rowcount > 0


def update_last_login(user_id: str | int) -> None:
    with _conn() as conn:
        conn.execute(
            f"""
            UPDATE {USERS_TABLE}
            SET last_login_at = now(), updated_at = now()
            WHERE {USERS_COL_ID} = %s
            """,
            (_parse_user_id(user_id),),
        )


def _job_row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    d = dict(row)
    d["user_id"] = str(d["user_id"])
    d["force"] = bool(d.get("force"))
    for key in ("created_at", "started_at", "finished_at"):
        if key in d:
            d[key] = _iso(d[key])
    return d


def create_job(
    *,
    user_id: str | int,
    run_mode: str,
    topic: str | None = None,
    category: str | None = None,
    market_id: str | None = None,
    parent_run_id: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    job_id = str(uuid.uuid4())
    uid = _parse_user_id(user_id)
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO agent_jobs (
              id, user_id, status, run_mode, topic, category, market_id,
              parent_run_id, force
            ) VALUES (%s, %s, 'queued', %s, %s, %s, %s, %s, %s)
            """,
            (
                job_id,
                uid,
                run_mode,
                topic,
                category,
                market_id,
                parent_run_id,
                force,
            ),
        )
    return get_job(job_id)  # type: ignore[return-value]


def get_job(job_id: str) -> dict[str, Any] | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM agent_jobs WHERE id = %s", (job_id,)
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
        fields.append("status = %s")
        values.append(status)
    if card_run_id is not None:
        fields.append("card_run_id = %s")
        values.append(card_run_id)
    if error is not None:
        fields.append("error = %s")
        values.append(error)
    if started_at is not None:
        fields.append("started_at = %s")
        values.append(started_at)
    if finished_at is not None:
        fields.append("finished_at = %s")
        values.append(finished_at)
    if not fields:
        return get_job(job_id)
    values.append(job_id)
    with _conn() as conn:
        conn.execute(
            f"UPDATE agent_jobs SET {', '.join(fields)} WHERE id = %s",
            values,
        )
    return get_job(job_id)


def list_jobs_for_user(user_id: str | int, limit: int = 20) -> list[dict[str, Any]]:
    uid = _parse_user_id(user_id)
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM agent_jobs
            WHERE user_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (uid, limit),
        ).fetchall()
    return [_job_row_to_dict(r) for r in rows]


def count_jobs_today_for_user(user_id: str | int) -> int:
    uid = _parse_user_id(user_id)
    today = datetime.now(timezone.utc).date().isoformat()
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM agent_jobs
            WHERE user_id = %s
              AND created_at >= %s::timestamptz
            """,
            (uid, f"{today}T00:00:00+00:00"),
        ).fetchone()
    return int(row["n"]) if row else 0


def user_has_active_job(user_id: str | int) -> bool:
    uid = _parse_user_id(user_id)
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM agent_jobs
            WHERE user_id = %s AND status IN ('queued', 'running')
            LIMIT 1
            """,
            (uid,),
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
