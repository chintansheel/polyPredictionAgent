"""Background execution of agent runs for web users."""

from __future__ import annotations

import os
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv()

# Ensure repo root is importable when worker thread loads agent package.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from agent.orchestrator import (  # noqa: E402
    run_once,
    run_reanalysis_for_market,
    run_reanalysis_for_run_id,
)
from agent.run_input import parse_keyword_or_polymarket_url  # noqa: E402
from agent.writer import OutputContext, get_card_by_run_id  # noqa: E402
from web import db  # noqa: E402
from web.config import WEB_MAX_CONCURRENT_RUNS  # noqa: E402

_executor = ThreadPoolExecutor(max_workers=WEB_MAX_CONCURRENT_RUNS)

_PROGRESS = {
    "queued": "Waiting in queue…",
    "running": "Running 4-layer analysis (this may take 1–2 minutes)…",
    "completed": "Done",
    "failed": "Failed",
}


def progress_message(status: str) -> str:
    return _PROGRESS.get(status, status)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _execute_job(job_id: str) -> None:
    job = db.get_job(job_id)
    if not job:
        return

    user_id = job["user_id"]
    output_ctx = OutputContext.for_user(user_id)

    db.update_job(job_id, status="running", started_at=_iso_now())

    card: Optional[dict] = None
    error_msg: Optional[str] = None

    try:
        mode = job["run_mode"]
        if mode == "fresh":
            topic = job.get("topic")
            market_id = job.get("market_id")
            if topic and not market_id and not job.get("category"):
                keyword, parsed_market = parse_keyword_or_polymarket_url(topic)
                if parsed_market:
                    market_id = parsed_market
                    topic = None
                elif keyword:
                    topic = keyword
            card = run_once(
                category_override=job.get("category"),
                keyword_filter=topic,
                market_id_override=market_id,
                force=bool(job.get("force")),
                output_ctx=output_ctx,
            )
            if card is None:
                error_msg = "No market found matching your input."
        elif mode == "reanalysis":
            parent_run_id = job.get("parent_run_id")
            market_id = job.get("market_id")
            if parent_run_id:
                card = run_reanalysis_for_run_id(
                    parent_run_id, output_ctx=output_ctx
                )
            elif market_id:
                card = run_reanalysis_for_market(
                    market_id, output_ctx=output_ctx
                )
            else:
                error_msg = "Re-analysis requires a prior run or market id."
            if card is None and not error_msg:
                error_msg = "Could not re-analyse: prior card or market not found."
        else:
            error_msg = f"Unknown run mode: {mode}"
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        error_msg = str(exc)[:500]

    finished_at = _iso_now()

    if card is not None:
        card_run_id = card.get("id")
        if card.get("status") == "ok":
            db.update_job(
                job_id,
                status="completed",
                card_run_id=card_run_id,
                finished_at=finished_at,
            )
        else:
            reason = card.get("failure_reason") or "Agent run failed"
            db.update_job(
                job_id,
                status="failed",
                card_run_id=card_run_id,
                error=reason[:500],
                finished_at=finished_at,
            )
    else:
        db.update_job(
            job_id,
            status="failed",
            error=error_msg or "Run produced no card",
            finished_at=finished_at,
        )


def enqueue_job(job_id: str) -> None:
    _executor.submit(_execute_job, job_id)


def job_response_for_user(job: dict[str, Any], user_id: str) -> dict[str, Any]:
    if job["user_id"] != user_id:
        raise PermissionError("Job does not belong to this user")

    status = job["status"]
    out: dict[str, Any] = {
        "id": job["id"],
        "status": status,
        "run_mode": job["run_mode"],
        "progress": progress_message(status),
        "topic": job.get("topic"),
        "category": job.get("category"),
        "market_id": job.get("market_id"),
        "parent_run_id": job.get("parent_run_id"),
        "force": job.get("force"),
        "card_run_id": job.get("card_run_id"),
        "error": job.get("error"),
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "card": None,
    }

    if status == "completed" and job.get("card_run_id"):
        ctx = OutputContext.for_user(user_id)
        out["card"] = get_card_by_run_id(job["card_run_id"], ctx)
    elif status == "failed" and job.get("card_run_id"):
        ctx = OutputContext.for_user(user_id)
        out["card"] = get_card_by_run_id(job["card_run_id"], ctx)

    return out
