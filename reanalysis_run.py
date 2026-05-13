"""Re-analysis trigger.

Examples:
  python reanalysis_run.py --run-id run_20260511_140000
  python reanalysis_run.py --market-id fed-rate-july-2026
  python reanalysis_run.py --stale-days 3
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

from agent.orchestrator import (  # noqa: E402
    run_reanalysis_for_market,
    run_reanalysis_for_run_id,
)
from agent.writer import load_feed  # noqa: E402


def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _print_card_summary(card: dict) -> None:
    sub = card.get("sub_prediction") or {}
    verdict = card.get("verdict") or {}
    market = card.get("market") or {}
    print(f"  → re-analysis of: {market.get('question')}")
    print(f"     verdict        : {verdict.get('call')} "
          f"({float(verdict.get('confidence') or 0):.0%})")
    print(f"     sub-prediction : {sub.get('predicted_value')}")
    print(f"     run_id         : {card.get('id')}  "
          f"(parent={card.get('parent_run_id')})")


def _process_stale(days: int) -> int:
    feed = load_feed()
    if not feed:
        print("Feed is empty.")
        return 1

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    latest_by_market: dict[str, dict] = {}
    for c in feed:
        m_id = (c.get("market") or {}).get("id")
        if not m_id:
            continue
        prev = latest_by_market.get(m_id)
        if prev is None:
            latest_by_market[m_id] = c
            continue
        prev_ts = _parse_iso(prev.get("timestamp", "")) or datetime.min.replace(tzinfo=timezone.utc)
        cur_ts = _parse_iso(c.get("timestamp", "")) or datetime.min.replace(tzinfo=timezone.utc)
        if cur_ts > prev_ts:
            latest_by_market[m_id] = c

    stale = []
    for m_id, c in latest_by_market.items():
        ts = _parse_iso(c.get("timestamp", ""))
        if ts is None or ts < cutoff:
            stale.append(m_id)

    if not stale:
        print(f"No markets older than {days} days. Nothing to do.")
        return 0

    print(f"Re-analysing {len(stale)} stale market(s):")
    failures = 0
    for m_id in stale:
        print(f"\n→ {m_id}")
        card = run_reanalysis_for_market(m_id)
        if not card:
            failures += 1
            print(f"   failed: no card produced for {m_id}")
            continue
        _print_card_summary(card)
    return 0 if failures == 0 else 2


def main() -> int:
    parser = argparse.ArgumentParser(description="Polymarket-agent re-analysis trigger")
    parser.add_argument("--run-id", dest="run_id", type=str, default=None)
    parser.add_argument("--market-id", dest="market_id", type=str, default=None)
    parser.add_argument("--stale-days", dest="stale_days", type=int, default=None,
                        help="Re-analyse every market whose latest card is "
                             "older than this many days.")
    args = parser.parse_args()

    if not any([args.run_id, args.market_id, args.stale_days]):
        parser.error("provide --run-id, --market-id, or --stale-days")

    if args.stale_days is not None:
        return _process_stale(args.stale_days)

    if args.run_id:
        card = run_reanalysis_for_run_id(args.run_id)
    else:
        card = run_reanalysis_for_market(args.market_id)

    if not card:
        return 1
    _print_card_summary(card)
    return 0 if card.get("status") == "ok" else 2


if __name__ == "__main__":
    sys.exit(main())
