"""Scheduled entry point.

Runs `orchestrator.run_once()` immediately on boot, then every
RUN_INTERVAL_HOURS hours via APScheduler. APScheduler is in-process so
there is no infra to manage.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

# Allow running this file as `python agent/main.py` from repo root.
if __package__ is None or __package__ == "":
    THIS_DIR = os.path.dirname(os.path.abspath(__file__))
    REPO_ROOT = os.path.dirname(THIS_DIR)
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)

load_dotenv()

from agent.orchestrator import run_once  # noqa: E402


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def scheduled_run() -> None:
    print(f"[{_now_str()}] scheduled run starting")
    try:
        card = run_once()
    except Exception as exc:  # noqa: BLE001
        print(f"[{_now_str()}] run failed at top level: {exc}")
        return
    if not card:
        print(f"[{_now_str()}] run produced no card")
        return
    sub = (card.get("sub_prediction") or {}).get("predicted_value")
    verdict = (card.get("verdict") or {}).get("call")
    print(
        f"[{_now_str()}] run complete: status={card.get('status')} "
        f"verdict={verdict} sub_prediction={sub}"
    )


def main() -> None:
    interval_hours = float(os.getenv("RUN_INTERVAL_HOURS", "2"))
    print(f"polymarket-agent starting. interval={interval_hours}h")

    # Run once immediately so a fresh deploy produces output without waiting.
    scheduled_run()

    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
    except ImportError:
        print("apscheduler not installed; remaining in single-run mode.")
        return

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        scheduled_run,
        trigger="interval",
        hours=interval_hours,
        next_run_time=None,  # we already did the initial run above
        max_instances=1,
        coalesce=True,
    )
    print(f"Scheduler armed. Next run in {interval_hours}h.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("Shutting down.")


if __name__ == "__main__":
    main()
