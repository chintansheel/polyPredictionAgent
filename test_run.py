"""Manual trigger.

Bypasses the cron scheduler and runs the full agent pipeline once with
an optional topic / category / market-id override. Outputs a card and
trace identical to a scheduled run.

Examples:
  python test_run.py --category politics
  python test_run.py --topic "iran war"
  python test_run.py --market-id "fed-rate-july-2026"
  python test_run.py --force
"""

from __future__ import annotations

import argparse
import sys

# Windows consoles default to cp1252; some agent prints / market questions
# contain Unicode (em-dash, etc.) which would crash stdout.encode otherwise.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

from dotenv import load_dotenv

load_dotenv()

from agent.orchestrator import run_once  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Manual polymarket-agent trigger")
    parser.add_argument("--category", type=str, default=None,
                        help="Force a category from CATEGORY_ROTATION.")
    parser.add_argument("--topic", type=str, default=None,
                        help="Keyword to match against market question/description.")
    parser.add_argument("--market-id", dest="market_id", type=str, default=None,
                        help="Run on a specific Polymarket market id or slug.")
    parser.add_argument("--force", action="store_true",
                        help="Ignore cooldowns and pick the best overall market.")
    args = parser.parse_args()

    card = run_once(
        category_override=args.category,
        keyword_filter=args.topic,
        market_id_override=args.market_id,
        force=args.force,
    )
    if not card:
        print("Run produced no card.")
        return 1

    sub = card.get("sub_prediction") or {}
    verdict = card.get("verdict") or {}
    market = card.get("market") or {}
    print()
    print(f"Selected:       {market.get('question')}")
    print(f"Verdict:        {verdict.get('call')} "
          f"({float(verdict.get('confidence') or 0):.0%} confidence)")
    print(f"Sub-prediction: {sub.get('predicted_value')} "
          f"(confidence {float(sub.get('confidence') or 0):.0%})")
    print(f"Status:         {card.get('status')}")
    print(f"Run id:         {card.get('id')}")
    return 0 if card.get("status") == "ok" else 2


if __name__ == "__main__":
    sys.exit(main())
