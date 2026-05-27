"""Prediction accuracy checker.

Run nightly. Walks the feed:
- For markets that have resolved (Gamma reports `closed: true` with a
  binary outcome) → fill in `actual_value` and `prediction_correct` on
  the verdict.
- Roll the result into `output/scorecard.json`.

Sub-predictions are partially supported: we can only auto-verify them
when the predicted_value matches the market's binary outcome. Numeric
sub-predictions (e.g. CPI = 2.8%) need a manual `actual_value` to be
filled in via the UI or by editing the card directly — the scorecard
respects whatever is already there.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from agent.tools import gamma  # noqa: E402
from agent.writer import (  # noqa: E402
    compute_scorecard_stats,
    init_scorecard,
    load_feed,
    load_scorecard,
    save_feed,
    save_scorecard,
)


def _maybe_int_to_str(v) -> str:
    if isinstance(v, (int, float)):
        return f"{v}"
    return str(v) if v is not None else ""


def _resolved_outcome(market: dict | None) -> str | None:
    """Return 'YES' / 'NO' / numeric string if Gamma reports resolved, else None."""
    if not market:
        return None
    raw = market.get("raw") or {}
    if not raw.get("closed"):
        return None
    # Some markets report a `resolvedOutcome` (string) or `outcomes` + the
    # final price = 1.0 / 0.0 in `outcomePrices`. Handle both.
    if raw.get("resolvedOutcome"):
        return str(raw["resolvedOutcome"]).upper()
    prices = market.get("outcome_prices") or []
    outcomes = raw.get("outcomes")
    if isinstance(outcomes, str):
        try:
            import json as _json
            outcomes = _json.loads(outcomes)
        except Exception:  # noqa: BLE001
            outcomes = None
    if prices and outcomes and len(prices) == len(outcomes):
        for price, label in zip(prices, outcomes):
            if abs(float(price) - 1.0) < 1e-3:
                return str(label).upper()
    return None


def _predict_matches_actual(predicted: str, actual: str) -> bool:
    if not predicted or not actual:
        return False
    p = predicted.strip().lower()
    a = actual.strip().lower()
    if p in {"yes", "y", "true"} and a in {"yes", "y", "true"}:
        return True
    if p in {"no", "n", "false"} and a in {"no", "n", "false"}:
        return True
    return p == a


def main() -> int:
    feed = load_feed()
    if not feed:
        print("Feed is empty.")
        return 0

    scorecard = load_scorecard() or init_scorecard()

    updates = 0
    for card in feed:
        sub = card.get("sub_prediction") or {}
        verdict = card.get("verdict") or {}
        market_summary = card.get("market") or {}
        market_id = market_summary.get("id")

        if not market_id:
            continue

        # Has the user (or a previous run) already recorded an actual value?
        already_resolved = sub.get("actual_value") is not None
        if not already_resolved:
            market = gamma.fetch_market(str(market_id))
            actual = _resolved_outcome(market)
            if actual:
                sub["actual_value"] = actual
                sub["prediction_correct"] = _predict_matches_actual(
                    _maybe_int_to_str(sub.get("predicted_value")), actual
                )
                # The market verdict resolves when the actual outcome is known.
                prob_now = float(market.get("probability_now") or 0.0) if market else 0.0
                call = verdict.get("call")
                if call == "underpriced":
                    verdict["correct"] = actual.lower() in {"yes", "y", "true"}
                elif call == "overpriced":
                    verdict["correct"] = actual.lower() in {"no", "n", "false"}
                elif call == "fair":
                    verdict["correct"] = abs(prob_now - 0.5) < 0.1
                updates += 1

        # write back into card object
        card["sub_prediction"] = sub
        card["verdict"] = verdict

    stats = compute_scorecard_stats(feed)
    scorecard.update(stats)
    sub = stats["sub_predictions"]
    mv = stats["market_verdicts"]
    scorecard["last_run"] = datetime.now(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )

    save_feed(feed)
    save_scorecard(scorecard)

    print(
        f"Scorecard updated. Updates={updates}. "
        f"Sub-predictions {sub['correct_within_range']}/{sub['resolved']} resolved correctly "
        f"(of {sub['total']} total). "
        f"Market verdicts {mv['correct']}/{mv['resolved']}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
