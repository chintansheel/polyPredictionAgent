"""Market selection with category rotation + cooldown.

Without this the agent picks the same 3-5 high-liquidity markets every
run (Fed rates, BTC, US election). The selector forces breadth via a
category rotation and prevents repeats via a 48-hour cooldown.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .tools import gamma

CATEGORY_ROTATION = [
    "economics",
    "politics",
    "crypto",
    "science_tech",
    "sports",
    "geopolitics",
    "business",
    "health",
]

# Market-type classification used by trace instrumentation. Different from
# CATEGORY_ROTATION: rotation is about topic breadth, market_type is about
# whether the 4-layer research framework actually fits the market.
#
#   fundamental — driven by a measurable indicator (CPI, GDP, jobs). 4-layer
#                 framework fits very well.
#   event       — resolved by a one-shot event (ceasefire, vote, ruling).
#                 4-layer framework fits well.
#   behavioral  — tracks someone's behaviour (tweet count, posting volume).
#                 4-layer framework fits POORLY; bad signal for the agent.
#   price       — price/level prediction (BTC at $X). Mediocre fit.
#
# Order matters: we check `behavioral` BEFORE `event` because event-keywords
# like "deal" can leak into behavioural markets ("Musk deal-tweet count").
MARKET_TYPE_KEYWORDS: dict[str, list[str]] = {
    "behavioral": ["tweets", "tweet", "posts", "followers", "activity",
                   "tracker", "post count"],
    "fundamental": ["inflation", "gdp", "fed", "fomc", "rate", "rates",
                    "jobs", "cpi", "ppi", "election", "unemployment"],
    "event": ["war", "ceasefire", "deal", "vote", "ruling", "launch",
              "approval", "indictment", "verdict"],
    "price": ["bitcoin", "btc", "price", "stock", "ipo", "market cap",
              "ether", "eth"],
}

# Which market types the 4-layer factor-research framework is a good fit for.
# Used to flag selector quality issues (e.g. "3 of last 5 runs picked
# behavioural markets — selector scoring may need reweighting").
_FRAMEWORK_GOOD_FIT = frozenset({"fundamental", "event"})


def classify_market_type(question: str) -> str:
    """Classify a market question into one of fundamental/event/behavioral/price.

    Returns ``"unknown"`` if no keyword matches. Lowercases the question once
    up front and does a simple substring sweep — fast enough to call inline
    when emitting the Layer 1 span.
    """
    if not question:
        return "unknown"
    q = question.lower()
    for market_type, keywords in MARKET_TYPE_KEYWORDS.items():
        if any(kw in q for kw in keywords):
            return market_type
    return "unknown"


def framework_fit(market_type: str) -> str:
    """Return ``"good"`` if the 4-layer framework fits this market type."""
    return "good" if market_type in _FRAMEWORK_GOOD_FIT else "poor"

# Loose keyword bags used to bucket markets when the Gamma `category` field
# is missing or unhelpful. Order matters — first match wins.
CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "economics": ["fed", "rate", "inflation", "cpi", "ppi", "recession", "jobs",
                  "unemployment", "gdp", "earnings", "fomc"],
    "politics":  ["election", "president", "senate", "congress", "primary",
                  "trump", "biden", "harris", "vance", "speaker", "vote"],
    "crypto":    ["bitcoin", "btc", "ether", "eth", "crypto", "solana", "doge",
                  "stablecoin", "etf"],
    "science_tech": ["ai", "openai", "gpt", "claude", "spacex", "nasa", "tesla",
                     "climate", "vaccine", "rocket"],
    "sports":    ["nba", "nfl", "mlb", "world cup", "champions league", "uefa",
                  "playoffs", "super bowl"],
    "geopolitics": ["ukraine", "russia", "china", "taiwan", "israel", "gaza",
                    "iran", "nato", "north korea", "war"],
    "business":  ["ipo", "merger", "acquisition", "ceo", "stock", "nvidia",
                  "apple", "microsoft", "amazon"],
    "health":    ["fda", "outbreak", "covid", "flu", "pharma", "approval"],
}

DEFAULT_COOLDOWN_HOURS = int(os.getenv("COOLDOWN_HOURS", "48"))
STATE_DIR = Path(os.getenv("STATE_DIR", "state"))
COOLDOWN_FILE = STATE_DIR / "analysed_topics.json"
ROTATION_FILE = STATE_DIR / "category_rotation.json"


@dataclass
class SelectedMarket:
    market: dict
    score: float
    reason: str
    category: str
    retries: int = 0
    # Stats captured during selection so the orchestrator can emit a
    # `tool.gamma_api` span representing the *selection* fetch (which
    # happens before the agent loop even starts). Without this, Discovery
    # Agent has no visibility into how many markets the selector
    # considered or how long the Gamma call took.
    markets_fetched: int = 0
    gamma_fetch_ms: int = 0
    gamma_fetch_calls: int = 0

    def to_dict(self) -> dict:
        return {
            "market": self.market,
            "score": self.score,
            "reason": self.reason,
            "category": self.category,
            "retries": self.retries,
            "markets_fetched": self.markets_fetched,
            "gamma_fetch_ms": self.gamma_fetch_ms,
            "gamma_fetch_calls": self.gamma_fetch_calls,
        }


# ---------------------------------------------------------------------------
# state helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def _load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return dict(default)
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return dict(default)


def _save_json(path: Path, payload: dict) -> None:
    _ensure_state_dir()
    path.write_text(json.dumps(payload, indent=2))


def load_cooldown() -> dict:
    return _load_json(COOLDOWN_FILE, {"markets": {}, "stats": {}})


def load_rotation() -> dict:
    return _load_json(
        ROTATION_FILE,
        {"last_category": None, "last_run": None, "rotation_index": -1},
    )


def save_cooldown(payload: dict) -> None:
    _save_json(COOLDOWN_FILE, payload)


def save_rotation(payload: dict) -> None:
    _save_json(ROTATION_FILE, payload)


# ---------------------------------------------------------------------------
# scoring + categorization
# ---------------------------------------------------------------------------

def categorize_market(market: dict) -> str:
    """Map a market to one of CATEGORY_ROTATION buckets."""
    raw_cat = (market.get("category") or "").lower()
    for canonical in CATEGORY_ROTATION:
        if canonical in raw_cat:
            return canonical
        if canonical == "science_tech" and ("science" in raw_cat or "tech" in raw_cat):
            return canonical
    haystack = f"{market.get('question', '')} {market.get('description', '')}".lower()
    for canonical, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in haystack for kw in keywords):
            return canonical
    return "business"  # neutral default bucket


def _days_to_close(market: dict) -> float:
    end = market.get("end_date")
    if not end:
        return 30.0
    try:
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return 30.0
    delta = (end_dt - _now()).total_seconds() / 86400.0
    return max(delta, 0.5)


def _normalize(value: float, scale: float) -> float:
    if scale <= 0:
        return 0.0
    return min(value / scale, 1.0)


def score_market(market: dict) -> tuple[float, str]:
    """Return (score, reason). Score is in [0, 1]."""
    prob = market.get("probability_now") or 0.0
    # Treat distance from 0.5 (uncertain band) as more interesting — markets
    # at 0.05 or 0.95 are pricing certainty and have low information value.
    uncertainty = 1.0 - abs(prob - 0.5) * 2.0
    volume_24hr = float(market.get("volume_24hr") or 0.0)
    liquidity = float(market.get("liquidity") or 0.0)
    days = _days_to_close(market)
    urgency = 1.0 / max(days, 1.0)

    score = (
        0.40 * uncertainty
        + 0.30 * _normalize(volume_24hr, 250_000)
        + 0.20 * urgency
        + 0.10 * _normalize(liquidity, 100_000)
    )
    reason = (
        f"24hr vol ${int(volume_24hr):,}, "
        f"probability {prob:.2f} (uncertainty {uncertainty:.2f}), "
        f"closes in {days:.1f}d, liquidity ${int(liquidity):,}"
    )
    return score, reason


def _is_in_cooldown(market_id: str, cooldown_state: dict) -> bool:
    entry = cooldown_state.get("markets", {}).get(market_id)
    if not entry:
        return False
    until = entry.get("cooldown_until")
    if not until:
        return False
    try:
        until_dt = datetime.fromisoformat(until.replace("Z", "+00:00"))
    except ValueError:
        return False
    return until_dt > _now()


def _keyword_match_score(market: dict, keyword: str) -> float:
    kw = keyword.lower().strip()
    if not kw:
        return 0.0
    haystack = f"{market.get('question', '')} {market.get('description', '')}".lower()
    tokens = [t for t in kw.split() if t]
    if not tokens:
        return 0.0
    hits = sum(1 for t in tokens if t in haystack)
    return hits / len(tokens)


# ---------------------------------------------------------------------------
# rotation
# ---------------------------------------------------------------------------

def next_category() -> tuple[str, int]:
    state = load_rotation()
    idx = (state.get("rotation_index", -1) + 1) % len(CATEGORY_ROTATION)
    return CATEGORY_ROTATION[idx], idx


def advance_rotation(category: str, index: int) -> None:
    save_rotation({
        "last_category": category,
        "last_run": _iso(_now()),
        "rotation_index": index,
    })


# ---------------------------------------------------------------------------
# cooldown registration
# ---------------------------------------------------------------------------

def record_analysis(
    market_id: str,
    question: str,
    category: str,
    run_id: str,
    cooldown_hours: int = DEFAULT_COOLDOWN_HOURS,
) -> None:
    state = load_cooldown()
    markets = state.setdefault("markets", {})
    now = _now()
    until = now + timedelta(hours=cooldown_hours)

    entry = markets.get(market_id) or {
        "question": question,
        "category": category,
        "first_analysed": _iso(now),
        "analysis_count": 0,
        "run_ids": [],
    }
    entry["last_analysed"] = _iso(now)
    entry["analysis_count"] = int(entry.get("analysis_count", 0)) + 1
    entry["cooldown_until"] = _iso(until)
    entry["category"] = category
    entry["question"] = question
    entry["run_ids"] = list(entry.get("run_ids", [])) + [run_id]
    markets[market_id] = entry

    stats = state.setdefault("stats", {})
    stats["total_unique_markets"] = len(markets)
    cats = set(stats.get("categories_covered", []))
    cats.add(category)
    stats["categories_covered"] = sorted(cats)
    stats["last_updated"] = _iso(now)

    save_cooldown(state)


# ---------------------------------------------------------------------------
# main selection API
# ---------------------------------------------------------------------------

def _fetch_active_with_stats(stats: dict, *, limit: int = 100) -> list[dict]:
    """`gamma.fetch_active_markets` with timing + count accumulation.

    The orchestrator pulls these numbers off `SelectedMarket` to emit a
    `tool.gamma_api` span representing the selection-time Gamma call(s).
    """
    started = time.monotonic()
    markets = gamma.fetch_active_markets(limit=limit)
    stats["gamma_fetch_ms"] += int((time.monotonic() - started) * 1000)
    stats["gamma_fetch_calls"] += 1
    stats["markets_fetched"] += len(markets)
    return markets


def _fetch_market_with_stats(stats: dict, market_id: str) -> Optional[dict]:
    started = time.monotonic()
    market = gamma.fetch_market(market_id)
    stats["gamma_fetch_ms"] += int((time.monotonic() - started) * 1000)
    stats["gamma_fetch_calls"] += 1
    if market is not None:
        stats["markets_fetched"] += 1
    return market


def _search_markets_with_stats(
    stats: dict, query: str, *, limit: int = 50
) -> list[dict]:
    """Wrap ``gamma.search_markets`` with the same stats accumulation as the
    other selection-time Gamma calls so the orchestrator's `tool.gamma_api`
    span captures search latency / hit-count too."""
    started = time.monotonic()
    try:
        markets = gamma.search_markets(query, limit_per_type=limit)
    except Exception:
        # Search is a discovery aid; if Gamma's /public-search misbehaves
        # we degrade to the top-100 fallback rather than failing the run.
        markets = []
    stats["gamma_fetch_ms"] += int((time.monotonic() - started) * 1000)
    stats["gamma_fetch_calls"] += 1
    stats["markets_fetched"] += len(markets)
    return markets


def _apply_stats(selected: SelectedMarket, stats: dict) -> SelectedMarket:
    selected.markets_fetched = int(stats.get("markets_fetched", 0))
    selected.gamma_fetch_ms = int(stats.get("gamma_fetch_ms", 0))
    selected.gamma_fetch_calls = int(stats.get("gamma_fetch_calls", 0))
    return selected


def select_market(
    *,
    category_override: Optional[str] = None,
    keyword_filter: Optional[str] = None,
    market_id_override: Optional[str] = None,
    skip_cooldown: bool = False,
    max_category_skips: int = 4,
) -> Optional[SelectedMarket]:
    """Pick the best market subject to rotation + cooldown.

    Resolution order:
    1. `market_id_override` — direct lookup, no scoring.
    2. `keyword_filter`     — call Gamma /public-search; fall back to
                              top-100-active + local substring match if
                              search returns nothing.
    3. `category_override`  — restrict to one category.
    4. Default              — advance rotation, retry up to `max_category_skips`
       categories until a valid market is found.
    """
    cooldown_state = load_cooldown()
    # Per-selection Gamma fetch stats. Survives across all `_select_*` calls
    # we make below so the resulting `SelectedMarket` reflects the TOTAL
    # Gamma activity that went into picking this market.
    fetch_stats: dict = {"markets_fetched": 0, "gamma_fetch_ms": 0, "gamma_fetch_calls": 0}

    if market_id_override:
        market = _fetch_market_with_stats(fetch_stats, market_id_override)
        if not market:
            return None
        score, reason = score_market(market)
        return _apply_stats(
            SelectedMarket(
                market=market,
                score=score,
                reason=f"explicit market-id override; {reason}",
                category=categorize_market(market),
            ),
            fetch_stats,
        )

    if keyword_filter:
        # Primary path: Gamma's /public-search hits the actual search index,
        # so long-tail topics ("silver", "gold", specific commodities) that
        # never crack the top-100-by-24hr-volume list are findable.
        search_hits = _search_markets_with_stats(
            fetch_stats, keyword_filter, limit=50
        )
        source = "gamma search"
        candidates: list[dict] = search_hits

        # Fallback: if search returned nothing (rate-limited, indexing lag,
        # truly no match) re-use the old "top-100 active + local substring"
        # path so we still surface popular markets that mention the keyword.
        if not candidates:
            top_active = _fetch_active_with_stats(fetch_stats, limit=100)
            candidates = [
                m for m in top_active
                if _keyword_match_score(m, keyword_filter) > 0
            ]
            source = "top-100 local match"

        if not candidates:
            return None

        ranked: list[tuple[float, float, dict]] = []
        for m in candidates:
            kscore = _keyword_match_score(m, keyword_filter)
            base, _ = score_market(m)
            ranked.append((kscore, base, m))
        # Sort by substring relevance first, then by intrinsic market score.
        # When all hits come from /public-search, kscore is often the same
        # across candidates so the secondary sort (volume/uncertainty/urgency)
        # picks the most tradeable one.
        ranked.sort(key=lambda x: (x[0], x[1]), reverse=True)
        kscore, base, market = ranked[0]
        _, reason = score_market(market)
        return _apply_stats(
            SelectedMarket(
                market=market,
                score=base,
                reason=(
                    f"{source} for '{keyword_filter}' "
                    f"(keyword score {kscore:.2f}); {reason}"
                ),
                category=categorize_market(market),
            ),
            fetch_stats,
        )

    if category_override:
        selected = _select_in_category(
            category=category_override,
            cooldown_state=cooldown_state,
            skip_cooldown=skip_cooldown,
            fetch_stats=fetch_stats,
        )
        return _apply_stats(selected, fetch_stats) if selected else None

    # default scheduled flow — rotation
    skips = 0
    while skips < max_category_skips:
        category, idx = next_category()
        selected = _select_in_category(
            category=category,
            cooldown_state=cooldown_state,
            skip_cooldown=skip_cooldown,
            fetch_stats=fetch_stats,
        )
        if selected:
            advance_rotation(category, idx)
            return _apply_stats(selected, fetch_stats)
        advance_rotation(category, idx)  # advance even on miss so next run progresses
        skips += 1

    # final fallback — best market overall regardless of category
    fallback = _select_any(
        cooldown_state=cooldown_state,
        skip_cooldown=skip_cooldown,
        fetch_stats=fetch_stats,
    )
    return _apply_stats(fallback, fetch_stats) if fallback else None


def _select_in_category(
    *,
    category: str,
    cooldown_state: dict,
    skip_cooldown: bool,
    fetch_stats: dict,
) -> Optional[SelectedMarket]:
    # Gamma's category filter is unreliable — pull broadly, then filter locally.
    candidates = _fetch_active_with_stats(fetch_stats, limit=100)
    in_cat = [m for m in candidates if categorize_market(m) == category]
    return _rank_and_pick(
        candidates=in_cat,
        category_hint=category,
        cooldown_state=cooldown_state,
        skip_cooldown=skip_cooldown,
    )


def _select_any(
    *,
    cooldown_state: dict,
    skip_cooldown: bool,
    fetch_stats: dict,
) -> Optional[SelectedMarket]:
    candidates = _fetch_active_with_stats(fetch_stats, limit=100)
    return _rank_and_pick(
        candidates=candidates,
        category_hint=None,
        cooldown_state=cooldown_state,
        skip_cooldown=skip_cooldown,
    )


def _rank_and_pick(
    *,
    candidates: list[dict],
    category_hint: Optional[str],
    cooldown_state: dict,
    skip_cooldown: bool,
) -> Optional[SelectedMarket]:
    scored: list[tuple[float, str, dict]] = []
    for m in candidates:
        if not skip_cooldown and _is_in_cooldown(m["id"], cooldown_state):
            continue
        score, reason = score_market(m)
        scored.append((score, reason, m))
    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    score, reason, market = scored[0]
    return SelectedMarket(
        market=market,
        score=score,
        reason=reason,
        category=category_hint or categorize_market(market),
    )
