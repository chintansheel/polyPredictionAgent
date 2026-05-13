"""Polymarket Gamma API client.

Public, no auth. Used by the selector to fetch and score active markets,
and by the orchestrator to re-fetch current prices in re-analysis mode.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import httpx

BASE_URL = "https://gamma-api.polymarket.com"


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_outcome_prices(raw: Any) -> list[float]:
    """`outcomePrices` is sometimes a JSON-encoded string, sometimes a list."""
    if raw is None:
        return []
    if isinstance(raw, list):
        out = []
        for v in raw:
            f = _safe_float(v)
            if f is not None:
                out.append(f)
        return out
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return _parse_outcome_prices(parsed)
        except json.JSONDecodeError:
            return []
    return []


def _normalize_market(raw: dict) -> dict:
    prices = _parse_outcome_prices(raw.get("outcomePrices"))
    prob_now = prices[0] if prices else None

    return {
        "id": str(raw.get("id") or raw.get("conditionId") or raw.get("slug") or ""),
        "slug": raw.get("slug"),
        "question": raw.get("question") or "",
        "description": raw.get("description") or "",
        "category": (raw.get("category") or raw.get("category_label") or "").lower(),
        "outcome_prices": prices,
        "probability_now": prob_now,
        "volume": _safe_float(raw.get("volume")) or 0.0,
        "volume_24hr": _safe_float(raw.get("volumeClob24hr"))
                       or _safe_float(raw.get("volume24hr"))
                       or 0.0,
        "liquidity": _safe_float(raw.get("liquidity")) or 0.0,
        "start_date": raw.get("startDate"),
        "end_date": raw.get("endDate"),
        "active": bool(raw.get("active", True)),
        "closed": bool(raw.get("closed", False)),
        "polymarket_url": (
            f"https://polymarket.com/event/{raw.get('slug')}"
            if raw.get("slug")
            else None
        ),
        "raw": raw,
    }


class GammaClient:
    """Thin wrapper around the Gamma REST API."""

    def __init__(self, timeout_seconds: float = 15.0) -> None:
        self._client = httpx.Client(
            base_url=BASE_URL,
            timeout=timeout_seconds,
            headers={"User-Agent": "polymarket-research-agent/1.0"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GammaClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def fetch_active_markets(
        self,
        limit: int = 50,
        category: Optional[str] = None,
    ) -> list[dict]:
        """Fetch active, non-closed markets, optionally filtered by category.

        Gamma returns dicts in JSON. We normalize the fields we care about.
        """
        params: dict[str, Any] = {
            "limit": limit,
            "active": "true",
            "closed": "false",
            "order": "volume24hr",
            "ascending": "false",
        }
        if category:
            params["category"] = category

        response = self._client.get("/markets", params=params)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            return []

        markets = [_normalize_market(m) for m in data]
        return [m for m in markets if m["question"]]

    def fetch_market(self, market_id: str) -> Optional[dict]:
        """Fetch a single market by Gamma id OR slug."""
        for params in ({"id": market_id}, {"slug": market_id}):
            response = self._client.get("/markets", params=params)
            if response.status_code != 200:
                continue
            data = response.json()
            if isinstance(data, list) and data:
                return _normalize_market(data[0])
            if isinstance(data, dict) and data:
                return _normalize_market(data)
        return None

    def search_markets(
        self,
        query: str,
        *,
        limit_per_type: int = 50,
        include_closed: bool = False,
    ) -> list[dict]:
        """Server-side search via Gamma's ``/public-search`` endpoint.

        Unlike ``fetch_active_markets`` (which only returns the top-N by 24hr
        volume and is useless for long-tail topics like "silver" or "gold"),
        this hits Gamma's actual search index with ``q=<query>`` and walks
        every returned event to extract its child markets.

        Search results come back as a list of ``events``, each with a nested
        ``markets`` array. Individual markets in this payload often lack
        rolled-up fields (volume24hr, endDate, liquidity) that the selector's
        scoring relies on, so we merge the parent event's values down onto
        each market before normalization.
        """
        if not query or not query.strip():
            return []
        params: dict[str, Any] = {
            "q": query.strip(),
            "limit_per_type": limit_per_type,
            "events_status": "active",
            "keep_closed_markets": 1 if include_closed else 0,
        }
        response = self._client.get("/public-search", params=params)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            return []

        events = data.get("events") or []
        out: list[dict] = []
        for event in events:
            if not isinstance(event, dict):
                continue
            if not include_closed and (
                event.get("closed") or event.get("archived") or event.get("active") is False
            ):
                continue
            for raw_market in event.get("markets") or []:
                if not isinstance(raw_market, dict):
                    continue
                if not include_closed and (
                    raw_market.get("closed") or raw_market.get("active") is False
                ):
                    continue
                merged = _merge_event_into_market(event, raw_market)
                normalized = _normalize_market(merged)
                if normalized.get("question"):
                    out.append(normalized)
        return out


def _merge_event_into_market(event: dict, market: dict) -> dict:
    """Copy event-level fields onto a search-result market when missing.

    Search-result markets often have null ``volume24hr`` / ``endDate`` /
    ``liquidity`` because those metrics live on the parent event. Pull them
    down so ``_normalize_market`` and the selector's scoring see real values.
    The event's ``slug`` is also preferred for URL construction since
    polymarket.com routes by event slug.
    """
    merged = dict(market)
    event_slug = event.get("slug")
    if event_slug and not merged.get("slug"):
        merged["slug"] = event_slug
    for src_key, dst_keys in (
        ("volume24hr", ("volume24hr", "volumeClob24hr")),
        ("liquidity", ("liquidity",)),
        ("liquidityClob", ("liquidity",)),
        ("endDate", ("endDate",)),
        ("startDate", ("startDate",)),
        ("category", ("category",)),
        ("description", ("description",)),
    ):
        src_val = event.get(src_key)
        if src_val in (None, "", 0):
            continue
        for dst_key in dst_keys:
            if not merged.get(dst_key):
                merged[dst_key] = src_val
    return merged


def fetch_active_markets(limit: int = 50, category: Optional[str] = None) -> list[dict]:
    """Module-level convenience."""
    with GammaClient() as client:
        return client.fetch_active_markets(limit=limit, category=category)


def fetch_market(market_id: str) -> Optional[dict]:
    with GammaClient() as client:
        return client.fetch_market(market_id)


def search_markets(
    query: str,
    *,
    limit_per_type: int = 50,
    include_closed: bool = False,
) -> list[dict]:
    """Module-level convenience for keyword search."""
    with GammaClient() as client:
        return client.search_markets(
            query,
            limit_per_type=limit_per_type,
            include_closed=include_closed,
        )
