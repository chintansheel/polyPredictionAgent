"""Card writer + automated run-health checks.

`writer.py` is the boundary between the orchestrator and disk. It:
- Validates the agent's JSON against the card schema (minimal checks
  matching `prompts/output_schema.md`).
- Writes the card to `output/feed.json` (append) and `output/latest.json`.
- Computes per-run health checks from the in-memory trace + card and
  writes to `output/run_health.json`.
- Updates / initialises `output/scorecard.json`.

A failed run still writes a card with `status: "failed"` and the trace
is preserved. Discovery Agent treats failed traces as signal.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .tracer import RunTracer

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "output"))
FEED_FILE = OUTPUT_DIR / "feed.json"
LATEST_FILE = OUTPUT_DIR / "latest.json"
RUN_HEALTH_FILE = OUTPUT_DIR / "run_health.json"
SCORECARD_FILE = OUTPUT_DIR / "scorecard.json"


def _ensure_dir() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _now_iso() -> str:
    return _iso(datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

class CardValidationError(ValueError):
    pass


_REQUIRED_PATHS_FRESH = [
    ("market", "selection_reason"),
    ("market", "key_event"),
    ("market", "plain_english"),
    ("sub_prediction", "predicted_value"),
    ("sub_prediction", "confidence"),
    ("sub_prediction", "basis"),
    ("sub_prediction", "invalidation_condition"),
    ("verdict", "call"),
    ("verdict", "confidence"),
]

_REQUIRED_PATHS_REANALYSIS = _REQUIRED_PATHS_FRESH + [
    ("changes_since_prior",),
]


def _dig(payload: dict, path: tuple[str, ...]) -> Any:
    cur: Any = payload
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def validate_card_payload(payload: dict, mode: str) -> list[str]:
    """Return a list of human-readable validation errors. Empty = OK."""
    errors: list[str] = []
    required = _REQUIRED_PATHS_REANALYSIS if mode == "reanalysis" else _REQUIRED_PATHS_FRESH
    for path in required:
        if _dig(payload, path) in (None, "", [], {}):
            errors.append(f"missing or empty field: {'.'.join(path)}")

    verdict_call = _dig(payload, ("verdict", "call"))
    if verdict_call and verdict_call not in ("underpriced", "overpriced", "fair"):
        errors.append(f"verdict.call must be one of underpriced/overpriced/fair, got {verdict_call!r}")

    for cfield in (("sub_prediction", "confidence"), ("verdict", "confidence")):
        v = _dig(payload, cfield)
        if v is not None:
            try:
                vv = float(v)
                if not 0.0 <= vv <= 1.0:
                    errors.append(f"{'.'.join(cfield)} out of range: {v}")
            except (TypeError, ValueError):
                errors.append(f"{'.'.join(cfield)} not a number: {v}")

    basis = _dig(payload, ("sub_prediction", "basis"))
    if isinstance(basis, list) and len(basis) < 2:
        errors.append("sub_prediction.basis should have at least 2 entries")

    return errors


# ---------------------------------------------------------------------------
# card assembly
# ---------------------------------------------------------------------------

def build_card(
    *,
    run_id: str,
    run_number: int,
    mode: str,
    market_summary: dict,
    agent_payload: dict,
    meta: dict,
    parent_run_id: Optional[str] = None,
    analysis_number: int = 1,
    prior_analysis: Optional[dict] = None,
    status: str = "ok",
    failed_at_layer: Optional[int] = None,
    failure_reason: Optional[str] = None,
) -> dict:
    """Merge the agent's JSON with run metadata into a full card."""
    card: dict[str, Any] = {
        "id": run_id,
        "timestamp": _now_iso(),
        "run_number": run_number,
        "trace_id": meta.get("trace_id"),
        "mode": mode,
        "status": status,
        "market": {
            "id": market_summary.get("id"),
            "question": market_summary.get("question"),
            "category": market_summary.get("category"),
            "probability_now": market_summary.get("probability_now"),
            "probability_24hr_ago": market_summary.get("probability_24hr_ago"),
            "probability_delta": market_summary.get("probability_delta"),
            "volume_24hr": market_summary.get("volume_24hr"),
            "closes": market_summary.get("end_date"),
            "polymarket_url": market_summary.get("polymarket_url"),
        },
        "analysis": {
            "selection_reason": _dig(agent_payload, ("market", "selection_reason")),
            "news_headline": _dig(agent_payload, ("market", "news_headline")),
            "news_source": _dig(agent_payload, ("market", "news_source")),
            "news_url": _dig(agent_payload, ("market", "news_url")),
            "news_verified": _dig(agent_payload, ("market", "news_verified")),
            "plain_english": _dig(agent_payload, ("market", "plain_english")),
            "key_event": _dig(agent_payload, ("market", "key_event")),
        },
        "layer3": agent_payload.get("layer3", {}),
        "sub_prediction": {
            **(agent_payload.get("sub_prediction") or {}),
            "actual_value": None,
            "prediction_correct": None,
        },
        "verdict": agent_payload.get("verdict") or {},
        "meta": meta,
    }

    if mode == "reanalysis":
        card["parent_run_id"] = parent_run_id
        card["analysis_number"] = analysis_number
        card["prior_analysis"] = prior_analysis or {}
        card["changes_since_prior"] = agent_payload.get("changes_since_prior") or {}

    if status != "ok":
        card["failed_at_layer"] = failed_at_layer
        card["failure_reason"] = failure_reason

    return card


# ---------------------------------------------------------------------------
# file writers
# ---------------------------------------------------------------------------

def _load_feed() -> list[dict]:
    if not FEED_FILE.exists():
        return []
    try:
        return json.loads(FEED_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def append_card(card: dict) -> None:
    _ensure_dir()
    feed = _load_feed()
    feed.append(card)
    FEED_FILE.write_text(json.dumps(feed, indent=2))
    LATEST_FILE.write_text(json.dumps(card, indent=2))


def get_card_by_run_id(run_id: str) -> Optional[dict]:
    for c in _load_feed():
        if c.get("id") == run_id:
            return c
    return None


def most_recent_card_for_market(market_id: str) -> Optional[dict]:
    feed = _load_feed()
    matches = [c for c in feed if (c.get("market") or {}).get("id") == market_id]
    if not matches:
        return None
    return matches[-1]


def next_run_number() -> int:
    return len(_load_feed()) + 1


# ---------------------------------------------------------------------------
# run health (Level 1 validation)
# ---------------------------------------------------------------------------

def _previous_confidence() -> Optional[float]:
    feed = _load_feed()
    if len(feed) < 1:
        return None
    last = feed[-1]
    try:
        return float((last.get("sub_prediction") or {}).get("confidence"))
    except (TypeError, ValueError):
        return None


def _recent_retries(window: int = 10) -> int:
    feed = _load_feed()[-window:]
    total = 0
    for c in feed:
        try:
            total += int((c.get("meta") or {}).get("retries", 0))
        except (TypeError, ValueError):
            continue
    return total


def compute_run_health(card: dict, tracer: RunTracer) -> dict:
    spans = tracer.spans
    tool_spans = [s for s in spans if s.name.startswith("tool.")]
    tools_called = [
        (s.attributes.get("tool.name") or s.name.replace("tool.", ""))
        for s in tool_spans
    ]
    tool_raw_chars = [
        int(s.attributes.get("tool.output_raw_chars") or 0)
        for s in tool_spans
    ]
    # exa queries inside layer 3 specifically
    layer3 = next((s for s in spans if s.name == "layer_3.deep_factor_research"), None)
    exa_count = 0
    extract_used = False
    any_empty_extract = False
    if layer3:
        for s in spans:
            if s.parent_span_id == layer3.span_id and s.name == "tool.exa_search":
                exa_count += 1
            if s.parent_span_id == layer3.span_id and s.name == "tool.tavily_extract":
                extract_used = True
                # P0-3 ties in here: an extract is "empty" if it returned
                # no content or the content-retrieved flag is false.
                if not bool(s.attributes.get("tool.content_retrieved", True)):
                    any_empty_extract = True
                elif int(s.attributes.get("tool.output_raw_chars") or 0) == 0:
                    any_empty_extract = True

    layer2_query = ""
    for s in spans:
        if s.parent_span_id and s.name == "tool.tavily_search":
            layer2 = next((p for p in spans if p.span_id == s.parent_span_id), None)
            if layer2 and layer2.name == "layer_2.surface_research":
                layer2_query = str(s.attributes.get("tool.query") or "")
                break

    question_lower = ((card.get("market") or {}).get("question") or "").lower()
    layer2_relevant = any(
        token in layer2_query.lower()
        for token in question_lower.split()
        if len(token) > 3
    ) if layer2_query else False

    confidence = (card.get("sub_prediction") or {}).get("confidence")
    prev_conf = _previous_confidence()
    if confidence is None or prev_conf is None:
        confidence_not_flat = True  # first run gets a pass
    else:
        try:
            confidence_not_flat = abs(float(confidence) - prev_conf) > 0.05
        except (TypeError, ValueError):
            confidence_not_flat = False

    checks = {
        "layer3_exa_depth": exa_count >= 3,
        "tool_variety": len(set(tools_called)) >= 3,
        "layer2_query_relevant": bool(layer2_relevant),
        "confidence_not_flat": bool(confidence_not_flat),
        "tavily_extract_used": bool(extract_used),
        "no_empty_extracts": not any_empty_extract,
        "retry_logic_fired_recent": _recent_retries(window=10) > 0,
    }

    return {
        "run_id": card.get("id"),
        "timestamp": card.get("timestamp"),
        "checks": checks,
        "summary": {
            "pass": sum(1 for v in checks.values() if v),
            "fail": sum(1 for v in checks.values() if not v),
            "tools_called": tools_called,
            "tool_raw_chars": tool_raw_chars,
            "exa_queries_in_layer3": exa_count,
            "layer2_query": layer2_query,
            "any_empty_extract": any_empty_extract,
        },
    }


def write_run_health(report: dict) -> None:
    _ensure_dir()
    history: list[dict] = []
    if RUN_HEALTH_FILE.exists():
        try:
            existing = json.loads(RUN_HEALTH_FILE.read_text())
            if isinstance(existing, dict) and isinstance(existing.get("history"), list):
                history = existing["history"]
            elif isinstance(existing, list):
                history = existing
        except (json.JSONDecodeError, OSError):
            history = []
    history.append(report)
    payload = {
        "latest": report,
        "history": history[-50:],
    }
    RUN_HEALTH_FILE.write_text(json.dumps(payload, indent=2))


# ---------------------------------------------------------------------------
# scorecard
# ---------------------------------------------------------------------------

def init_scorecard() -> dict:
    return {
        "updated_at": _now_iso(),
        "sub_predictions": {
            "total": 0, "resolved": 0,
            "correct_within_range": 0, "accuracy": 0.0,
        },
        "market_verdicts": {
            "total": 0, "resolved": 0,
            "correct": 0, "accuracy": 0.0,
        },
    }


def load_scorecard() -> dict:
    if not SCORECARD_FILE.exists():
        return init_scorecard()
    try:
        return json.loads(SCORECARD_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return init_scorecard()


def save_scorecard(payload: dict) -> None:
    _ensure_dir()
    payload["updated_at"] = _now_iso()
    SCORECARD_FILE.write_text(json.dumps(payload, indent=2))


def save_feed(feed: list[dict]) -> None:
    _ensure_dir()
    FEED_FILE.write_text(json.dumps(feed, indent=2))


def load_feed() -> list[dict]:
    return _load_feed()
