"""4-layer agentic orchestrator (layered sub-agents).

Each of the 4 layers (selection-verify, surface-research, deep-factor-
research, synthesis) runs as its own mini agent loop with its own
message history. That means:

- Layer N+1 never sees Layer N's raw tool dumps; it only sees Layer N's
  condensed structured JSON findings. The synthesis call (Layer 4) thus
  has a tiny input context (~2-3k tokens) and a generous output budget
  (~3500 tokens), so the final card no longer truncates.
- Each sub-agent gets only the tools it should be using (Layer 4 gets
  no tools at all). Constraining the `tools` payload prevents off-layer
  behaviour at the API level, not just in the prompt.
- A shared `backoff_state` and `client` are threaded through all four
  sub-agents so cross-layer rate limiting still works as a single run.

The orchestrator's responsibilities:
1. Wire up the 4 tools (gamma_api, tavily_search, exa_search, tavily_extract).
2. Run each sub-agent under a `layer_N.*` tracer span; nest llm/tool spans.
3. Parse each layer's final assistant message as JSON, hand the condensed
   finding to the next layer.
4. Assemble the final card payload from {L1, L3, L4} and hand off to
   `writer.py` to persist the card and the run-health report.

Same code path is used for fresh and re-analysis runs — only the per-layer
user message includes prior-card context for reanalysis.
"""

from __future__ import annotations

import json
import os
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import anthropic
from openai import OpenAI

from . import selector as selector_module
from . import writer as writer_module
from .prompt_hasher import hash_prompt, hash_text
from .tools import exa as exa_tool
from .tools import gamma as gamma_tool
from .tools import tavily as tavily_tool
from .tracer import RunTracer, Span

PROMPTS_DIR = Path(__file__).parent / "prompts"

# Anthropic published pricing for claude-sonnet-4-x as of writing
# (USD per 1M tokens). Used to convert per-run token usage to a $ estimate
# emitted on the root span so Discovery Agent can show $/run trends.
# Kept as plain floats so they're trivial to update when the rate card
# changes; nothing in the agent loop depends on the exact value.
ANTHROPIC_INPUT_USD_PER_MTOK = float(os.getenv("ANTHROPIC_INPUT_USD_PER_MTOK", "3.00"))
ANTHROPIC_OUTPUT_USD_PER_MTOK = float(os.getenv("ANTHROPIC_OUTPUT_USD_PER_MTOK", "15.00"))

# claude-sonnet-4-x has a 200k token context window. Used to compute
# `llm.context_window_pct` on each LLM span so Discovery Agent can flag
# runs that are creeping toward the limit (and thus 429-prone).
ANTHROPIC_CONTEXT_WINDOW_TOKENS = int(os.getenv("ANTHROPIC_CONTEXT_WINDOW_TOKENS", "200000"))

# --- OpenAI (when LLM_PROVIDER=openai) ---
# Pricing defaults are placeholders; set OPENAI_*_USD_PER_MTOK to match the
# current rate card for your model (e.g. gpt-5-mini).
OPENAI_INPUT_USD_PER_MTOK = float(os.getenv("OPENAI_INPUT_USD_PER_MTOK", "0.25"))
OPENAI_OUTPUT_USD_PER_MTOK = float(os.getenv("OPENAI_OUTPUT_USD_PER_MTOK", "2.00"))
OPENAI_CONTEXT_WINDOW_TOKENS = int(os.getenv("OPENAI_CONTEXT_WINDOW_TOKENS", "400000"))
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")
# gpt-5 / o-series reasoning models: "minimal" | "low" | "medium" | "high".
# Empty string disables the parameter (for non-reasoning OpenAI models).
OPENAI_REASONING_EFFORT = os.getenv("OPENAI_REASONING_EFFORT", "low").strip().lower()

# LLM_PROVIDER: "anthropic" (default) or "openai"
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic").strip().lower()


def _llm_provider() -> str:
    p = LLM_PROVIDER
    if p in ("anthropic", "openai"):
        return p
    return "anthropic"


def _active_model_id() -> str:
    return OPENAI_MODEL if _llm_provider() == "openai" else ANTHROPIC_MODEL


def _context_window_tokens() -> int:
    return (
        OPENAI_CONTEXT_WINDOW_TOKENS
        if _llm_provider() == "openai"
        else ANTHROPIC_CONTEXT_WINDOW_TOKENS
    )


def estimate_cost_usd(input_tokens: int, output_tokens: int) -> float:
    """Convert (input, output) token counts to a USD cost estimate."""
    if _llm_provider() == "openai":
        inp = OPENAI_INPUT_USD_PER_MTOK
        out = OPENAI_OUTPUT_USD_PER_MTOK
    else:
        inp = ANTHROPIC_INPUT_USD_PER_MTOK
        out = ANTHROPIC_OUTPUT_USD_PER_MTOK
    input_cost = (input_tokens or 0) * (inp / 1_000_000)
    output_cost = (output_tokens or 0) * (out / 1_000_000)
    return round(input_cost + output_cost, 6)

# Valid IDs follow Anthropic naming, e.g. claude-sonnet-4-6 (dateless) or
# claude-sonnet-4-5-20250929 (dated). The old spec default
# claude-sonnet-4-20250514 is not a real API id and returns HTTP 404.
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
MAX_TURNS = int(os.getenv("MAX_AGENT_TURNS", "24"))
# Final card JSON is ~600-900 tokens. 4096 was wildly oversized AND every
# `max_tokens` is reserved against your TPM budget — that's why the very first
# request 429s on a 10k TPM tier.
MAX_TOKENS = int(os.getenv("ANTHROPIC_MAX_TOKENS", "1500"))

# Per-layer max_tokens caps. The synthesis (layer 4) call is the one that
# emits the largest JSON payload (sub_prediction with 3+ basis entries,
# verdict with reasoning + watch_up/watch_down) so it gets a larger budget.
# Tool-using layers (1-3) emit short structured findings only, so they keep
# the small default. All values can be overridden via env vars.
MAX_TOKENS_LAYER1 = int(os.getenv("ANTHROPIC_MAX_TOKENS_LAYER1", str(MAX_TOKENS)))
MAX_TOKENS_LAYER2 = int(os.getenv("ANTHROPIC_MAX_TOKENS_LAYER2", str(MAX_TOKENS)))
MAX_TOKENS_LAYER3 = int(os.getenv("ANTHROPIC_MAX_TOKENS_LAYER3", "2500"))
MAX_TOKENS_LAYER4 = int(os.getenv("ANTHROPIC_MAX_TOKENS_LAYER4", "3500"))

# OpenAI reasoning models (gpt-5 / o-series) consume hidden reasoning
# tokens against `max_completion_tokens`. With the Anthropic-tuned caps
# above, the model runs out of budget before emitting any visible JSON.
# These defaults give reasoning room AND keep the JSON output budget.
OPENAI_MAX_TOKENS_LAYER1 = int(os.getenv("OPENAI_MAX_TOKENS_LAYER1", "4000"))
OPENAI_MAX_TOKENS_LAYER2 = int(os.getenv("OPENAI_MAX_TOKENS_LAYER2", "4000"))
OPENAI_MAX_TOKENS_LAYER3 = int(os.getenv("OPENAI_MAX_TOKENS_LAYER3", "8000"))
OPENAI_MAX_TOKENS_LAYER4 = int(os.getenv("OPENAI_MAX_TOKENS_LAYER4", "8000"))


def _layer_max_tokens(layer_num: int) -> int:
    if _llm_provider() == "openai":
        return {
            1: OPENAI_MAX_TOKENS_LAYER1,
            2: OPENAI_MAX_TOKENS_LAYER2,
            3: OPENAI_MAX_TOKENS_LAYER3,
            4: OPENAI_MAX_TOKENS_LAYER4,
        }.get(layer_num, OPENAI_MAX_TOKENS_LAYER1)
    return {
        1: MAX_TOKENS_LAYER1,
        2: MAX_TOKENS_LAYER2,
        3: MAX_TOKENS_LAYER3,
        4: MAX_TOKENS_LAYER4,
    }.get(layer_num, MAX_TOKENS_LAYER1)

# Per-layer iteration caps (max LLM turns including a final no-tool turn).
MAX_TURNS_LAYER1 = int(os.getenv("MAX_TURNS_LAYER1", "4"))
MAX_TURNS_LAYER2 = int(os.getenv("MAX_TURNS_LAYER2", "4"))
MAX_TURNS_LAYER3 = int(os.getenv("MAX_TURNS_LAYER3", "8"))
MAX_TURNS_LAYER4 = int(os.getenv("MAX_TURNS_LAYER4", "2"))

# Anthropic bills the **full** `system` string on every `messages.create` call,
# plus the entire conversation so far. Low TPM tiers (e.g. 10k input tokens/min)
# hit 429 quickly during multi-turn tool loops. Mitigations: wait and retry on
# 429, optional minimum spacing between API calls, and `LLM_INTER_TURN_SECONDS`.
#
# LLM_429_RETRY_MAX = total HTTP attempts per single `messages.create` call
# (not "extra" retries). Capped to avoid runaway loops if .env is wrong.
LLM_429_RETRY_MAX = max(1, min(int(os.getenv("LLM_429_RETRY_MAX", "6")), 2))
LLM_429_RETRY_WAIT_SECONDS = float(os.getenv("LLM_429_RETRY_WAIT_SECONDS", "60"))
LLM_INTER_TURN_SECONDS = float(os.getenv("LLM_INTER_TURN_SECONDS", "0"))
# Enforced after each successful call and after each 429 wait (spreads TPM).
LLM_MIN_SECONDS_BETWEEN_API = float(os.getenv("LLM_MIN_SECONDS_BETWEEN_API", "0"))


# ---------------------------------------------------------------------------
# prompt loading
# ---------------------------------------------------------------------------

def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def build_system_prompt(mode: str) -> str:
    """Per spec: only the identity prompt + output schema go in `system`.

    The layer-specific guidance files (layer1_scoring, layer2_impact,
    layer3_factors, layer4_synthesis) are NOT concatenated into `system` —
    they're already summarised inside `system_{mode}.md`. Re-baking all four
    here costs ~2,000 tokens **per turn** and is the main reason a single
    request can exceed a 10k TPM org budget on its own.
    """
    base = load_prompt(f"system_{mode}.md")
    schema = load_prompt("output_schema.md")
    return f"{base}\n\n---\n\n{schema}"


def _is_rate_limit_error(exc: BaseException) -> bool:
    if getattr(exc, "status_code", None) == 429:
        return True
    # Anthropic and OpenAI SDKs both use RateLimitError for HTTP 429.
    return type(exc).__name__ == "RateLimitError"


def _retry_after_from_exception(exc: BaseException) -> float:
    """Prefer API Retry-After header when present; else configured default."""
    resp = getattr(exc, "response", None)
    if resp is not None:
        headers = getattr(resp, "headers", None) or {}
        ra = headers.get("retry-after") or headers.get("Retry-After")
        if ra is not None:
            try:
                return max(float(ra), 1.0)
            except (TypeError, ValueError):
                pass
    return max(LLM_429_RETRY_WAIT_SECONDS, 1.0)


def _respect_anthropic_backoff(backoff_state: dict) -> None:
    """Sleep until per-run `not_before_mono` (set after 429 or successful calls)."""
    nb = float(backoff_state.get("not_before_mono", 0.0))
    now = time.monotonic()
    if now < nb:
        time.sleep(nb - now)


def _bump_anthropic_backoff(backoff_state: dict, seconds: float) -> None:
    """Ensure no API call happens until monotonic `now + seconds`."""
    now = time.monotonic()
    deadline = now + max(seconds, 0.0)
    prev = float(backoff_state.get("not_before_mono", 0.0))
    backoff_state["not_before_mono"] = max(prev, deadline)


def _messages_create_with_rate_limit_retry(
    client: anthropic.Anthropic,
    *,
    llm_span: Span,
    backoff_state: dict,
    rate_limit_stats: Optional[dict] = None,
    **kwargs: Any,
):
    """Call Messages API. On 429, sleep then retry up to LLM_429_RETRY_MAX attempts total.

    Uses `backoff_state` so spacing survives across turns in the same run (one agent
    loop can otherwise open a new LLM turn immediately and blow the same TPM window).

    `rate_limit_stats`, when provided, accumulates a per-run summary
    (`events`, `wait_seconds_total`) that's surfaced on the root span.
    Discovery Agent uses this to flag runs where most of the wall-clock
    time was spent waiting on Anthropic 429s.
    """
    _respect_anthropic_backoff(backoff_state)
    span_wait_total = 0.0
    span_event_count = 0

    for attempt in range(1, LLM_429_RETRY_MAX + 1):
        try:
            response = client.messages.create(**kwargs)
            _bump_anthropic_backoff(backoff_state, LLM_MIN_SECONDS_BETWEEN_API)
            if span_event_count:
                llm_span.set("llm.rate_limit_events", span_event_count)
                llm_span.set("llm.rate_limit_wait_seconds_total", round(span_wait_total, 1))
            return response
        except Exception as exc:  # noqa: BLE001
            if not _is_rate_limit_error(exc):
                raise
            wait_s = _retry_after_from_exception(exc)
            llm_span.set("llm.rate_limit_attempt", attempt)
            llm_span.set("llm.rate_limit_wait_seconds", round(wait_s, 1))
            span_event_count += 1
            span_wait_total += wait_s
            if rate_limit_stats is not None:
                rate_limit_stats["events"] = int(rate_limit_stats.get("events", 0)) + 1
                rate_limit_stats["wait_seconds_total"] = (
                    float(rate_limit_stats.get("wait_seconds_total", 0.0)) + wait_s
                )

            if attempt >= LLM_429_RETRY_MAX:
                raise RuntimeError(
                    f"LLM rate limit (429): gave up after {LLM_429_RETRY_MAX} attempt(s) "
                    f"on this request. Set LLM_429_RETRY_WAIT_SECONDS, LLM_INTER_TURN_SECONDS, "
                    f"or LLM_MIN_SECONDS_BETWEEN_API (e.g. 5-10), or raise your org TPM. "
                    f"Last error: {exc}"
                ) from exc

            print(
                f"LLM rate limit (429): attempt {attempt}/{LLM_429_RETRY_MAX} failed; "
                f"sleeping {wait_s:.0f}s, then attempt {attempt + 1}/{LLM_429_RETRY_MAX}.",
                flush=True,
            )
            time.sleep(wait_s)
            _bump_anthropic_backoff(backoff_state, LLM_MIN_SECONDS_BETWEEN_API)
            _respect_anthropic_backoff(backoff_state)


def _openai_tools_from_anthropic_schema(tools: list[dict]) -> list[dict]:
    """Map Claude-style tool defs to OpenAI Chat Completions `tools` format."""
    out: list[dict] = []
    for t in tools:
        out.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema")
                or {"type": "object", "properties": {}},
            },
        })
    return out


def _openai_chat_with_rate_limit_retry(
    client: OpenAI,
    *,
    llm_span: Span,
    backoff_state: dict,
    rate_limit_stats: Optional[dict] = None,
    **kwargs: Any,
):
    """OpenAI Chat Completions with the same 429 retry/backoff as Anthropic Messages."""
    _respect_anthropic_backoff(backoff_state)
    span_wait_total = 0.0
    span_event_count = 0

    for attempt in range(1, LLM_429_RETRY_MAX + 1):
        try:
            response = client.chat.completions.create(**kwargs)
            _bump_anthropic_backoff(backoff_state, LLM_MIN_SECONDS_BETWEEN_API)
            if span_event_count:
                llm_span.set("llm.rate_limit_events", span_event_count)
                llm_span.set("llm.rate_limit_wait_seconds_total", round(span_wait_total, 1))
            return response
        except Exception as exc:  # noqa: BLE001
            if not _is_rate_limit_error(exc):
                raise
            wait_s = _retry_after_from_exception(exc)
            llm_span.set("llm.rate_limit_attempt", attempt)
            llm_span.set("llm.rate_limit_wait_seconds", round(wait_s, 1))
            span_event_count += 1
            span_wait_total += wait_s
            if rate_limit_stats is not None:
                rate_limit_stats["events"] = int(rate_limit_stats.get("events", 0)) + 1
                rate_limit_stats["wait_seconds_total"] = (
                    float(rate_limit_stats.get("wait_seconds_total", 0.0)) + wait_s
                )

            if attempt >= LLM_429_RETRY_MAX:
                raise RuntimeError(
                    f"LLM rate limit (429): gave up after {LLM_429_RETRY_MAX} attempt(s) "
                    f"on this request. Set LLM_429_RETRY_WAIT_SECONDS, LLM_INTER_TURN_SECONDS, "
                    f"or LLM_MIN_SECONDS_BETWEEN_API (e.g. 5-10), or raise your org TPM. "
                    f"Last error: {exc}"
                ) from exc

            print(
                f"LLM rate limit (429): attempt {attempt}/{LLM_429_RETRY_MAX} failed; "
                f"sleeping {wait_s:.0f}s, then attempt {attempt + 1}/{LLM_429_RETRY_MAX}.",
                flush=True,
            )
            time.sleep(wait_s)
            _bump_anthropic_backoff(backoff_state, LLM_MIN_SECONDS_BETWEEN_API)
            _respect_anthropic_backoff(backoff_state)


# ---------------------------------------------------------------------------
# tool schemas exposed to Claude
# ---------------------------------------------------------------------------

TOOLS_SCHEMA: list[dict] = [
    {
        "name": "gamma_api",
        "description": (
            "Fetch the latest market data from Polymarket Gamma API. "
            "Use in Layer 1 to refresh the selected market's prices and "
            "metadata. Returns probability, volume, liquidity, end date."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "market_id": {
                    "type": "string",
                    "description": "The Polymarket market id or slug.",
                },
            },
            "required": ["market_id"],
        },
    },
    {
        "name": "tavily_search",
        "description": (
            "AI-optimized news/web search. Use for current events, breaking "
            "news, official statements, expert forecast consensus. Use in "
            "Layer 1 to verify news, Layer 2 for surface impact, Layer 3 "
            "for one expert-consensus query."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "default": 5},
                "days": {
                    "type": "integer",
                    "description": "Restrict to news from the last N days.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "exa_search",
        "description": (
            "Neural/semantic search returning full page text. Use in Layer 3 "
            "for deep factor research — one query per major component of the "
            "key event (shelter, energy, food, etc. for a CPI report)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "num_results": {"type": "integer", "default": 5},
                "start_published_date": {
                    "type": "string",
                    "description": "ISO8601 lower bound on publish date.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "tavily_extract",
        "description": (
            "Pull full article text from a specific URL. Use in Layer 3 when "
            "a search snippet is not enough detail — typically when exa or "
            "tavily returned a primary-source page with critical data."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "One or more URLs to extract.",
                },
            },
            "required": ["urls"],
        },
    },
]


# ---------------------------------------------------------------------------
# tool subsetting per layer
# ---------------------------------------------------------------------------

LAYER_NAMES = {
    1: "layer_1.market_selection",
    2: "layer_2.surface_research",
    3: "layer_3.deep_factor_research",
    4: "layer_4.prediction_synthesis",
}

# Tools each layer's sub-agent is allowed to call. Constraining the
# `tools` payload (not just the prompt) means the LLM physically cannot
# call the wrong tool for its layer.
LAYER_TOOL_NAMES: dict[int, list[str]] = {
    1: ["tavily_search", "gamma_api"],
    2: ["tavily_search"],
    3: ["exa_search", "tavily_search", "tavily_extract"],
    4: [],  # synthesis — no tools
}


def _tools_for_layer(layer_num: int) -> list[dict]:
    allowed = LAYER_TOOL_NAMES.get(layer_num, [])
    return [t for t in TOOLS_SCHEMA if t["name"] in allowed]


# ---------------------------------------------------------------------------
# tool execution
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _truncate_for_llm(payload: Any, max_chars: int = 6000) -> str:
    """Serialize a tool result to JSON, capping size to keep the context
    window — and per-minute token budget — manageable. Truncated results
    still include a marker so the LLM understands content was cut."""
    text = json.dumps(payload, default=str)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n\n... [truncated, total={len(text)} chars]"


def _set_search_output_preview(span: Span, results: list[dict]) -> None:
    """Populate `tool.output_preview` + `tool.output_raw_chars` for a search tool.

    Discovery Agent uses these to detect when the agent proceeds on weak
    or empty results (e.g. the `tavily_extract returning 0 items` case
    that motivated this whole instrumentation pass).
    """
    if results:
        first_title = str(results[0].get("title") or "")[:100]
        span.set("tool.output_preview", f"Top result: {first_title}")
        span.set(
            "tool.output_raw_chars",
            sum(len(str(r.get("content") or "")) for r in results),
        )
    else:
        span.set("tool.output_preview", "no results returned")
        span.set("tool.output_raw_chars", 0)


def execute_tool(
    *,
    name: str,
    arguments: dict,
    tracer: RunTracer,
    parent_span: Span,
) -> dict:
    """Run a tool, return the result dict, and emit a tool span."""
    span = tracer.tool_span(name, parent_span)
    span.set("tool.input", json.dumps(arguments)[:1000])
    # `success` here means "the *tool call* succeeded". For
    # `tavily_extract` we override this below to mean "the tool ACTUALLY
    # returned readable content" — see P0-3.
    success = True
    try:
        if name == "gamma_api":
            market_id = arguments.get("market_id")
            if not market_id:
                raise ValueError("market_id required")
            span.set("tool.query", str(market_id))
            result = gamma_tool.fetch_market(market_id) or {"error": "market not found"}
            found = isinstance(result, dict) and "error" not in result
            span.set("tool.output_items", 1 if found else 0)
            if found:
                q = str(result.get("question") or "")[:120]
                span.set("tool.output_preview", f"market: {q}")
                span.set("tool.output_raw_chars", len(str(result.get("description") or "")))
            else:
                span.set("tool.output_preview", "market not found")
                span.set("tool.output_raw_chars", 0)
        elif name == "tavily_search":
            query = arguments.get("query", "")
            span.set("tool.query", query)
            result = tavily_tool.search(
                query=query,
                max_results=int(arguments.get("max_results") or 5),
                days=arguments.get("days"),
            )
            results_list = result.get("results") or []
            span.set("tool.output_items", len(results_list))
            _set_search_output_preview(span, results_list)
        elif name == "exa_search":
            query = arguments.get("query", "")
            span.set("tool.query", query)
            result = exa_tool.search(
                query=query,
                num_results=int(arguments.get("num_results") or 5),
                start_published_date=arguments.get("start_published_date"),
            )
            results_list = result.get("results") or []
            span.set("tool.output_items", len(results_list))
            _set_search_output_preview(span, results_list)
        elif name == "tavily_extract":
            urls = arguments.get("urls") or []
            if isinstance(urls, str):
                urls = [urls]
            span.set("tool.query", ", ".join(urls)[:300])
            result = tavily_tool.extract(urls=urls)
            span.set("tool.output_items", len(result.get("results") or []))
            # P0-3: HTTP-success on extract is NOT the same as
            # content-success. The `tavily_tool.extract` wrapper now
            # surfaces a `content_retrieved` flag; we mirror it onto the
            # tool span and use it as the actual `tool.success` value.
            raw_content = str(result.get("raw_content") or "")
            content_retrieved = bool(result.get("content_retrieved"))
            success = content_retrieved
            span.set("tool.content_retrieved", content_retrieved)
            span.set("tool.output_raw_chars", len(raw_content))
            preview = raw_content[:150].replace("\n", " ") if raw_content else "empty"
            span.set("tool.output_preview", preview)
        else:
            raise ValueError(f"unknown tool: {name}")
    except Exception as exc:  # noqa: BLE001
        success = False
        result = {"error": str(exc), "traceback": traceback.format_exc(limit=2)}
        span.set("tool.error", str(exc)[:500])
        span.set("tool.output_preview", f"error: {str(exc)[:120]}")
        span.set("tool.output_raw_chars", 0)
    finally:
        span.set("tool.success", success)
        span.set("tool.latency_ms", span.elapsed_ms())
        span.end(status_code=1 if success else 2)

    return result


# ---------------------------------------------------------------------------
# main entry points
# ---------------------------------------------------------------------------

def _build_run_id() -> str:
    return "run_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _summarize_market_for_card(market: dict) -> dict:
    return {
        "id": market.get("id"),
        "question": market.get("question"),
        "category": market.get("category"),
        "probability_now": market.get("probability_now"),
        "probability_24hr_ago": None,
        "probability_delta": None,
        "volume_24hr": market.get("volume_24hr"),
        "end_date": market.get("end_date"),
        "polymarket_url": market.get("polymarket_url"),
    }


def _compact_market(market: dict, description_chars: int = 600) -> dict:
    """Strip large/noisy fields from a Gamma market dict before showing it
    to the LLM. The full `description` and the `raw` Gamma payload can each
    be many hundreds of tokens and aren't needed for reasoning."""
    desc = market.get("description") or ""
    if isinstance(desc, str) and len(desc) > description_chars:
        desc = desc[:description_chars] + "…"
    return {
        "id": market.get("id"),
        "slug": market.get("slug"),
        "question": market.get("question"),
        "category": market.get("category"),
        "description": desc,
        "probability_now": market.get("probability_now"),
        "outcome_prices": market.get("outcome_prices"),
        "volume": market.get("volume"),
        "volume_24hr": market.get("volume_24hr"),
        "liquidity": market.get("liquidity"),
        "start_date": market.get("start_date"),
        "end_date": market.get("end_date"),
        "polymarket_url": market.get("polymarket_url"),
    }


def _prior_card_compact(prior_card: dict) -> dict:
    return {
        "run_id": prior_card.get("id"),
        "timestamp": prior_card.get("timestamp"),
        "market_question": (prior_card.get("market") or {}).get("question"),
        "prior_sub_prediction": prior_card.get("sub_prediction"),
        "prior_verdict": prior_card.get("verdict"),
        "prior_layer3": prior_card.get("layer3"),
        "prior_probability": (prior_card.get("market") or {}).get("probability_now"),
    }


# ---------------------------------------------------------------------------
# permissive JSON extraction
# ---------------------------------------------------------------------------

def _try_close_truncated_json(text: str) -> Optional[str]:
    """Best-effort: if a JSON object got cut off mid-string / mid-array /
    mid-object (stop_reason='max_tokens'), append the right number of
    closing brackets/quotes so `json.loads` has a shot. Returns None if
    we can't even find an opening brace.
    """
    start = text.find("{")
    if start < 0:
        return None
    s = text[start:]

    # Walk the string and track stack of unmatched openers, plus whether
    # we're currently inside a string. Comments are not valid JSON so
    # we don't worry about them.
    stack: list[str] = []
    in_string = False
    escape = False
    for ch in s:
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack and ((ch == "}" and stack[-1] == "{") or (ch == "]" and stack[-1] == "[")):
                stack.pop()
            else:
                # mismatch; bail
                return None

    repaired = s
    if in_string:
        repaired += '"'
    # Trailing comma before close fails; strip any trailing comma/whitespace
    stripped = repaired.rstrip()
    if stripped.endswith(","):
        repaired = stripped[:-1]
    # Now close anything still open, innermost first.
    while stack:
        opener = stack.pop()
        repaired += "}" if opener == "{" else "]"
    return repaired


def _extract_json_from_text(text: str) -> dict:
    """Permissive JSON extraction.

    The sub-agent system prompts ask Claude to respond with only JSON.
    If it slips a fence or commentary in, or if a `max_tokens` truncation
    cut the JSON mid-flight, we try a few recovery strategies before
    giving up.
    """
    text = text.strip()
    if text.startswith("```"):
        # strip code fences
        text = text.strip("`")
        first_nl = text.find("\n")
        if first_nl > 0 and text[:first_nl].strip().lower() in ("json", ""):
            text = text[first_nl + 1:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Largest balanced { ... } block
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    # Truncated JSON repair (stop_reason='max_tokens')
    repaired = _try_close_truncated_json(text)
    if repaired:
        try:
            return json.loads(repaired)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Could not parse JSON from agent (even after truncation repair): {exc}"
            ) from exc

    raise ValueError("No JSON object found in agent's final message")


# ---------------------------------------------------------------------------
# layered sub-agent driver
# ---------------------------------------------------------------------------

def _load_subagent_prompt(layer_num: int) -> str:
    return load_prompt(f"subagent_layer{layer_num}.md")


def _annotate_llm_prompt_provenance(
    span: Span,
    *,
    layer_num: int,
    system_prompt_file: str,
    layer_prompt_file: Optional[str],
    messages_count: int,
    max_tokens: int,
    temperature: Optional[float] = None,
) -> None:
    """Stamp every LLM span with the prompt files + hashes that produced it.

    Discovery Agent uses the hashes (not the file names alone) to detect
    prompt drift across runs and to correlate a Git commit that changed
    a prompt with a behavioural change in the trace.
    """
    span.set("llm.system_prompt_file", system_prompt_file)
    span.set("llm.system_prompt_hash", hash_prompt(system_prompt_file))
    if layer_prompt_file:
        span.set("llm.layer_prompt_file", layer_prompt_file)
        span.set("llm.layer_prompt_hash", hash_prompt(layer_prompt_file))
    span.set("llm.messages_count", messages_count)
    span.set("llm.max_tokens", max_tokens)
    if temperature is not None:
        span.set("llm.temperature", float(temperature))


def _record_llm_response(
    span: Span,
    response: Any,
    *,
    context_stats: dict,
) -> tuple[int, int, str, list[str]]:
    """Pull usage + content from an Anthropic response and stamp the span.

    Returns ``(input_tokens, output_tokens, stop_reason, text_chunks)`` so
    the caller can keep accumulating run-level totals.

    `context_stats` is a per-run dict accumulating max input tokens + first
    input tokens, used by P2-3 to compute context-growth on the root span.
    """
    usage = getattr(response, "usage", None)
    in_tok = getattr(usage, "input_tokens", 0) or 0
    out_tok = getattr(usage, "output_tokens", 0) or 0
    stop_reason = str(response.stop_reason)

    span.set("llm.input_tokens", in_tok)
    span.set("llm.output_tokens", out_tok)
    span.set("llm.stop_reason", stop_reason)
    span.set("llm.latency_ms", span.elapsed_ms())

    # P2-3: per-call context window pressure. Surfacing this on every span
    # is what lets Discovery Agent say "this run drifted from 1,800 to
    # 15,000 tokens by turn 9" instead of just "you hit a rate limit".
    span.set("llm.context_window_used_tokens", in_tok)
    cw = _context_window_tokens()
    if cw > 0:
        span.set(
            "llm.context_window_pct",
            round(in_tok / cw * 100, 2),
        )

    if context_stats.get("first_input_tokens", 0) == 0 and in_tok:
        context_stats["first_input_tokens"] = in_tok
    context_stats["max_input_tokens"] = max(
        int(context_stats.get("max_input_tokens", 0)), int(in_tok)
    )

    text_chunks: list[str] = []
    tool_chosen: Optional[str] = None
    for block in response.content:
        btype = getattr(block, "type", None)
        if btype == "text":
            text_chunks.append(block.text)
        elif btype == "tool_use" and tool_chosen is None:
            tool_chosen = getattr(block, "name", None)

    # P1-2: short response summary on every LLM span. We deliberately cap
    # at 200 chars so the trace stays small; full text would balloon the
    # JSON output and is recoverable from the agent's own internal logs.
    summary_source = text_chunks[0] if text_chunks else ""
    if summary_source:
        span.set(
            "llm.response_summary",
            summary_source[:200].replace("\n", " ").strip(),
        )
    else:
        span.set("llm.response_summary", f"<no text — stop_reason={stop_reason}>")

    if stop_reason == "tool_use" and tool_chosen:
        span.set("llm.tool_chosen", tool_chosen)

    return in_tok, out_tok, stop_reason, text_chunks


def _record_openai_response(
    span: Span,
    response: Any,
    *,
    context_stats: dict,
) -> tuple[int, int, str, list[str]]:
    """Pull usage + content from an OpenAI Chat Completions response; stamp the span.

    Maps OpenAI ``finish_reason`` values onto the same vocabulary used for
    Anthropic traces (``tool_use``, ``max_tokens``, etc.) where possible.
    """
    usage = getattr(response, "usage", None)
    in_tok = int(getattr(usage, "prompt_tokens", None) or 0) if usage else 0
    out_tok = int(getattr(usage, "completion_tokens", None) or 0) if usage else 0

    choice = response.choices[0]
    raw_finish = choice.finish_reason or ""
    if raw_finish == "tool_calls":
        stop_reason = "tool_use"
    elif raw_finish == "length":
        stop_reason = "max_tokens"
    else:
        stop_reason = str(raw_finish) if raw_finish else "stop"

    span.set("llm.input_tokens", in_tok)
    span.set("llm.output_tokens", out_tok)
    span.set("llm.stop_reason", stop_reason)
    span.set("llm.latency_ms", span.elapsed_ms())

    span.set("llm.context_window_used_tokens", in_tok)
    cw = _context_window_tokens()
    if cw > 0:
        span.set(
            "llm.context_window_pct",
            round(in_tok / cw * 100, 2),
        )

    if context_stats.get("first_input_tokens", 0) == 0 and in_tok:
        context_stats["first_input_tokens"] = in_tok
    context_stats["max_input_tokens"] = max(
        int(context_stats.get("max_input_tokens", 0)), int(in_tok)
    )

    msg = choice.message
    text_chunks: list[str] = []
    if getattr(msg, "content", None):
        text_chunks.append(str(msg.content))

    tool_chosen: Optional[str] = None
    tcalls = getattr(msg, "tool_calls", None) or []
    for tc in tcalls:
        fn = getattr(tc, "function", None)
        if fn is not None and getattr(fn, "name", None):
            tool_chosen = str(fn.name)
            break

    summary_source = text_chunks[0] if text_chunks else ""
    if summary_source:
        span.set(
            "llm.response_summary",
            summary_source[:200].replace("\n", " ").strip(),
        )
    else:
        span.set("llm.response_summary", f"<no text — stop_reason={stop_reason}>")

    if stop_reason == "tool_use" and tool_chosen:
        span.set("llm.tool_chosen", tool_chosen)

    return in_tok, out_tok, stop_reason, text_chunks


def _run_layer_subagent(
    *,
    layer_num: int,
    system_prompt: str,
    system_prompt_file: str,
    layer_prompt_file: Optional[str],
    initial_user_message: str,
    tools: list[dict],
    max_iterations: int,
    max_tokens: int,
    client: Any,
    backoff_state: dict,
    tracer: RunTracer,
    layer_span: Span,
    context_stats: dict,
    rate_limit_stats: dict,
) -> tuple[str, dict]:
    """Dispatch to Anthropic Messages or OpenAI Chat Completions based on LLM_PROVIDER."""
    if _llm_provider() == "openai":
        return _run_layer_subagent_openai(
            layer_num=layer_num,
            system_prompt=system_prompt,
            system_prompt_file=system_prompt_file,
            layer_prompt_file=layer_prompt_file,
            initial_user_message=initial_user_message,
            tools=tools,
            max_iterations=max_iterations,
            max_tokens=max_tokens,
            client=client,
            backoff_state=backoff_state,
            tracer=tracer,
            layer_span=layer_span,
            context_stats=context_stats,
            rate_limit_stats=rate_limit_stats,
        )
    return _run_layer_subagent_anthropic(
        layer_num=layer_num,
        system_prompt=system_prompt,
        system_prompt_file=system_prompt_file,
        layer_prompt_file=layer_prompt_file,
        initial_user_message=initial_user_message,
        tools=tools,
        max_iterations=max_iterations,
        max_tokens=max_tokens,
        client=client,
        backoff_state=backoff_state,
        tracer=tracer,
        layer_span=layer_span,
        context_stats=context_stats,
        rate_limit_stats=rate_limit_stats,
    )


def _run_layer_subagent_openai(
    *,
    layer_num: int,
    system_prompt: str,
    system_prompt_file: str,
    layer_prompt_file: Optional[str],
    initial_user_message: str,
    tools: list[dict],
    max_iterations: int,
    max_tokens: int,
    client: OpenAI,
    backoff_state: dict,
    tracer: RunTracer,
    layer_span: Span,
    context_stats: dict,
    rate_limit_stats: dict,
) -> tuple[str, dict]:
    """OpenAI Chat Completions tool loop (parallel contract to Anthropic sub-agent)."""
    openai_tools = _openai_tools_from_anthropic_schema(tools) if tools else []
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": initial_user_message},
    ]

    total_input_tokens = 0
    total_output_tokens = 0
    tool_call_counts: dict[str, int] = {}
    tavily_queries: list[str] = []
    tools_called: list[str] = []
    iterations = 0
    final_text = ""
    last_stop: Optional[str] = None

    while iterations < max_iterations:
        iterations += 1
        if iterations > 1 and LLM_INTER_TURN_SECONDS > 0:
            time.sleep(LLM_INTER_TURN_SECONDS)

        llm_span = tracer.llm_span(layer_span)
        llm_span.set("llm.model", OPENAI_MODEL)
        llm_span.set("llm.provider", "openai")
        llm_span.set("llm.turn", iterations)
        llm_span.set("llm.layer", layer_num)
        _annotate_llm_prompt_provenance(
            llm_span,
            layer_num=layer_num,
            system_prompt_file=system_prompt_file,
            layer_prompt_file=layer_prompt_file,
            messages_count=len(messages),
            max_tokens=max_tokens,
        )

        api_kwargs: dict[str, Any] = {
            "model": OPENAI_MODEL,
            "messages": messages,
            # gpt-5 / o-series models reject `max_tokens` and require
            # `max_completion_tokens`. The newer name is also accepted by
            # legacy chat-completions models, so we use it unconditionally.
            "max_completion_tokens": max_tokens,
        }
        if OPENAI_REASONING_EFFORT:
            api_kwargs["reasoning_effort"] = OPENAI_REASONING_EFFORT
        if openai_tools:
            api_kwargs["tools"] = openai_tools
            api_kwargs["tool_choice"] = "auto"

        try:
            response = _openai_chat_with_rate_limit_retry(
                client,
                llm_span=llm_span,
                backoff_state=backoff_state,
                rate_limit_stats=rate_limit_stats,
                **api_kwargs,
            )
        except Exception as exc:  # noqa: BLE001
            llm_span.set("llm.error", str(exc)[:500])
            llm_span.end(status_code=2)
            # Re-raise the original OpenAI error untouched so the real
            # 400/404 body (which names the offending parameter) reaches
            # the caller's traceback. The previous generic RuntimeError
            # hid the underlying message.
            raise

        in_tok, out_tok, last_stop, text_chunks = _record_openai_response(
            llm_span, response, context_stats=context_stats
        )
        total_input_tokens += in_tok
        total_output_tokens += out_tok

        msg = response.choices[0].message
        assistant_msg: dict[str, Any] = {"role": "assistant", "content": msg.content}
        if msg.tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments or "{}",
                    },
                }
                for tc in msg.tool_calls
            ]
        messages.append(assistant_msg)

        if last_stop == "tool_use" and msg.tool_calls:
            llm_span.end()
            for tc in msg.tool_calls:
                tool_name = tc.function.name
                tools_called.append(tool_name)
                tool_call_counts[tool_name] = tool_call_counts.get(tool_name, 0) + 1
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                if tool_name == "tavily_search":
                    tavily_queries.append(str(args.get("query", "")))

                result = execute_tool(
                    name=tool_name,
                    arguments=args,
                    tracer=tracer,
                    parent_span=layer_span,
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": _truncate_for_llm(result),
                })
            continue

        if last_stop == "max_tokens":
            llm_span.set("llm.truncated", True)
        llm_span.end()
        final_text = "\n".join(text_chunks).strip()
        break

    if not final_text:
        llm_span = tracer.llm_span(layer_span)
        llm_span.set("llm.model", OPENAI_MODEL)
        llm_span.set("llm.provider", "openai")
        llm_span.set("llm.turn", iterations + 1)
        llm_span.set("llm.layer", layer_num)
        llm_span.set("llm.role", "force_final")
        _annotate_llm_prompt_provenance(
            llm_span,
            layer_num=layer_num,
            system_prompt_file=system_prompt_file,
            layer_prompt_file=layer_prompt_file,
            messages_count=len(messages),
            max_tokens=max_tokens,
        )
        try:
            forced_system = (
                system_prompt
                + "\n\nIMPORTANT: Do NOT call any more tools. Respond NOW "
                  "with ONLY the JSON the prior user message asked for."
            )
            forced_messages = [{"role": "system", "content": forced_system}] + messages[1:]
            forced_kwargs: dict[str, Any] = {
                "model": OPENAI_MODEL,
                "messages": forced_messages,
                "max_completion_tokens": max_tokens,
            }
            if OPENAI_REASONING_EFFORT:
                forced_kwargs["reasoning_effort"] = OPENAI_REASONING_EFFORT
            response = _openai_chat_with_rate_limit_retry(
                client,
                llm_span=llm_span,
                backoff_state=backoff_state,
                rate_limit_stats=rate_limit_stats,
                **forced_kwargs,
            )
        except Exception as exc:  # noqa: BLE001
            llm_span.set("llm.error", str(exc)[:500])
            llm_span.end(status_code=2)
            raise
        in_tok, out_tok, last_stop, text_chunks = _record_openai_response(
            llm_span, response, context_stats=context_stats
        )
        total_input_tokens += in_tok
        total_output_tokens += out_tok
        llm_span.end()
        final_text = "\n".join(text_chunks).strip()

    meta = {
        "iterations": iterations,
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
        "tool_call_counts": tool_call_counts,
        "tools_called": tools_called,
        "tavily_queries": tavily_queries,
        "stop_reason": str(last_stop) if last_stop else "",
    }
    return final_text, meta


def _run_layer_subagent_anthropic(
    *,
    layer_num: int,
    system_prompt: str,
    system_prompt_file: str,
    layer_prompt_file: Optional[str],
    initial_user_message: str,
    tools: list[dict],
    max_iterations: int,
    max_tokens: int,
    client: anthropic.Anthropic,
    backoff_state: dict,
    tracer: RunTracer,
    layer_span: Span,
    context_stats: dict,
    rate_limit_stats: dict,
) -> tuple[str, dict]:
    """Run a single layer's mini agent loop with its OWN message history.

    Returns (final_assistant_text, layer_meta). The caller parses the text
    as JSON and stores layer-level attributes on `layer_span`.

    Important property: this sub-agent's message history is local — it does
    NOT see prior layers' tool dumps. The user message contains only
    condensed prior findings. That is why the synthesis (layer 4) call
    can stay under a few thousand input tokens while the run as a whole
    accumulates plenty of research.
    """
    messages: list[dict] = [{"role": "user", "content": initial_user_message}]

    total_input_tokens = 0
    total_output_tokens = 0
    tool_call_counts: dict[str, int] = {}
    tavily_queries: list[str] = []
    tools_called: list[str] = []
    iterations = 0
    final_text = ""
    last_stop: Optional[str] = None

    # When tools is empty (e.g. Layer 4), Anthropic API rejects an empty
    # list — we must omit the kwarg entirely.
    api_kwargs_base: dict = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "system": system_prompt,
    }
    if tools:
        api_kwargs_base["tools"] = tools

    while iterations < max_iterations:
        iterations += 1
        if iterations > 1 and LLM_INTER_TURN_SECONDS > 0:
            time.sleep(LLM_INTER_TURN_SECONDS)

        llm_span = tracer.llm_span(layer_span)
        llm_span.set("llm.model", ANTHROPIC_MODEL)
        llm_span.set("llm.turn", iterations)
        llm_span.set("llm.layer", layer_num)
        _annotate_llm_prompt_provenance(
            llm_span,
            layer_num=layer_num,
            system_prompt_file=system_prompt_file,
            layer_prompt_file=layer_prompt_file,
            messages_count=len(messages),
            max_tokens=max_tokens,
        )

        try:
            response = _messages_create_with_rate_limit_retry(
                client,
                llm_span=llm_span,
                backoff_state=backoff_state,
                rate_limit_stats=rate_limit_stats,
                messages=messages,
                **api_kwargs_base,
            )
        except Exception as exc:  # noqa: BLE001
            llm_span.set("llm.error", str(exc)[:500])
            llm_span.end(status_code=2)
            # Surface the underlying Anthropic exception unchanged so the
            # exact API message (e.g. wrong model id, billing issue,
            # rate-limit detail) is visible in the traceback.
            raise

        in_tok, out_tok, last_stop, text_chunks = _record_llm_response(
            llm_span, response, context_stats=context_stats
        )
        total_input_tokens += in_tok
        total_output_tokens += out_tok

        assistant_content = []
        tool_uses: list[dict] = []
        for block in response.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                assistant_content.append({"type": "text", "text": block.text})
            elif btype == "tool_use":
                tu = {
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                }
                assistant_content.append(tu)
                tool_uses.append(tu)
        messages.append({"role": "assistant", "content": assistant_content})

        if last_stop == "tool_use" and tool_uses:
            llm_span.end()

            tool_results_content = []
            for tu in tool_uses:
                tool_name = tu["name"]
                tools_called.append(tool_name)
                tool_call_counts[tool_name] = tool_call_counts.get(tool_name, 0) + 1
                if tool_name == "tavily_search":
                    tavily_queries.append(str(tu["input"].get("query", "")))

                result = execute_tool(
                    name=tool_name,
                    arguments=tu["input"] or {},
                    tracer=tracer,
                    parent_span=layer_span,
                )
                tool_results_content.append({
                    "type": "tool_result",
                    "tool_use_id": tu["id"],
                    "content": _truncate_for_llm(result),
                    "is_error": bool(isinstance(result, dict) and result.get("error")),
                })
            messages.append({"role": "user", "content": tool_results_content})
            continue

        # end_turn / max_tokens / anything not "tool_use" — sub-agent is done.
        if last_stop == "max_tokens":
            llm_span.set("llm.truncated", True)
        llm_span.end()
        final_text = "\n".join(text_chunks).strip()
        break

    if not final_text:
        # Loop hit max_iterations without a final text turn. Force a no-tools
        # closing call so we don't lose the layer entirely.
        llm_span = tracer.llm_span(layer_span)
        llm_span.set("llm.model", ANTHROPIC_MODEL)
        llm_span.set("llm.turn", iterations + 1)
        llm_span.set("llm.layer", layer_num)
        llm_span.set("llm.role", "force_final")
        _annotate_llm_prompt_provenance(
            llm_span,
            layer_num=layer_num,
            system_prompt_file=system_prompt_file,
            layer_prompt_file=layer_prompt_file,
            messages_count=len(messages),
            max_tokens=max_tokens,
        )
        try:
            forced_system = (
                system_prompt
                + "\n\nIMPORTANT: Do NOT call any more tools. Respond NOW "
                  "with ONLY the JSON the prior user message asked for."
            )
            response = _messages_create_with_rate_limit_retry(
                client,
                llm_span=llm_span,
                backoff_state=backoff_state,
                rate_limit_stats=rate_limit_stats,
                model=ANTHROPIC_MODEL,
                max_tokens=max_tokens,
                system=forced_system,
                messages=messages,
            )
        except Exception as exc:  # noqa: BLE001
            llm_span.set("llm.error", str(exc)[:500])
            llm_span.end(status_code=2)
            raise
        in_tok, out_tok, last_stop, text_chunks = _record_llm_response(
            llm_span, response, context_stats=context_stats
        )
        total_input_tokens += in_tok
        total_output_tokens += out_tok
        llm_span.end()
        final_text = "\n".join(text_chunks).strip()

    meta = {
        "iterations": iterations,
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
        "tool_call_counts": tool_call_counts,
        "tools_called": tools_called,
        "tavily_queries": tavily_queries,
        "stop_reason": str(last_stop) if last_stop else "",
    }
    return final_text, meta


# ---------------------------------------------------------------------------
# per-layer user messages — these are deliberately COMPACT. Each layer only
# sees the data it needs, never raw tool dumps from earlier layers.
# ---------------------------------------------------------------------------

def _user_msg_layer1(
    market_compact: dict,
    selection_score: float,
    selection_reason: str,
    mode: str,
    prior_card: Optional[dict],
) -> str:
    parts = [
        "You are starting LAYER 1 of a 4-layer analysis. "
        "Verify with one targeted `tavily_search` whether real news is "
        "driving the selected market.",
        "",
        "selected_market:",
        json.dumps(market_compact, indent=2, default=str),
        "",
        f"selection_score: {selection_score:.2f}",
        f"selection_reason: {selection_reason}",
    ]
    if mode == "reanalysis" and prior_card is not None:
        parts += [
            "",
            "This is a RE-ANALYSIS. Prior context:",
            json.dumps(_prior_card_compact(prior_card), indent=2, default=str),
            "Note any probability delta vs. prior. Do NOT call gamma_api "
            "unless you actually need to refresh — the market data above is "
            "already current.",
        ]
    parts += [
        "",
        "After your tool call(s), respond with ONLY this JSON:",
        "{",
        '  "selection_reason": "<1-2 sentence why-this-market>",',
        '  "news_headline": "<headline or empty>",',
        '  "news_source": "<outlet or empty>",',
        '  "news_url": "<URL or empty>",',
        '  "news_verified": true|false,',
        '  "plain_english": "<2-3 sentences for a layperson>",',
        '  "key_event": "<precise upcoming event + date, or fallback string>"',
        "}",
    ]
    return "\n".join(parts)


def _user_msg_layer2(market_compact: dict, l1_findings: dict) -> str:
    return "\n".join([
        "You are doing LAYER 2 (surface impact + components for L3).",
        "",
        "selected_market:",
        json.dumps(market_compact, indent=2, default=str),
        "",
        "layer1_findings:",
        json.dumps(l1_findings, indent=2, default=str),
        "",
        "Issue 1-2 topic-specific `tavily_search` queries. Then identify "
        "3+ underlying components Layer 3 will research one-at-a-time.",
        "",
        "Respond with ONLY this JSON:",
        "{",
        '  "surface_impact": "<2-3 sentences>",',
        '  "key_event_refined": "<refined event name+date, or copy L1 verbatim>",',
        '  "components_to_research": ["component1", "component2", "component3"],',
        '  "evidence_snippets": [',
        '    {"headline": "...", "source": "...", "url": "...", "takeaway": "..."}',
        "  ]",
        "}",
    ])


def _user_msg_layer3(
    market_compact: dict,
    l1_findings: dict,
    l2_findings: dict,
) -> str:
    return "\n".join([
        "You are doing LAYER 3 (deep factor research).",
        "",
        "selected_market:",
        json.dumps(market_compact, indent=2, default=str),
        "",
        "layer1_findings:",
        json.dumps(l1_findings, indent=2, default=str),
        "",
        "layer2_findings:",
        json.dumps(l2_findings, indent=2, default=str),
        "",
        "For EACH item in `components_to_research`, issue ONE `exa_search`. "
        "Then issue ONE `tavily_search` for expert consensus on the key "
        "event outcome. Optionally `tavily_extract` one URL if a snippet "
        "lacks critical detail.",
        "",
        "Respond with ONLY this JSON:",
        "{",
        '  "components_researched": {',
        '    "<component_name>": {',
        '      "finding": "<1 sentence with a number when possible>",',
        '      "direction": "up|down|neutral",',
        '      "confidence": 0.0,',
        '      "source": "<URL>"',
        "    }",
        "  },",
        '  "expert_consensus": {',
        '    "median_forecast": "<concrete value>",',
        '    "range": "<low — high>",',
        '    "source": "<URL or outlet>"',
        "  },",
        '  "conflicting_signals": true|false',
        "}",
    ])


def _user_msg_layer4(
    market_compact: dict,
    l1_findings: dict,
    l2_findings: dict,
    l3_findings: dict,
    mode: str,
    prior_card: Optional[dict],
) -> str:
    parts = [
        "You are doing LAYER 4 (synthesis). NO tool calls. Use only the "
        "findings below to produce the final prediction + verdict.",
        "",
        "market:",
        json.dumps(market_compact, indent=2, default=str),
        "",
        "layer1_findings:",
        json.dumps(l1_findings, indent=2, default=str),
        "",
        "layer2_findings:",
        json.dumps(l2_findings, indent=2, default=str),
        "",
        "layer3_findings:",
        json.dumps(l3_findings, indent=2, default=str),
    ]
    if mode == "reanalysis" and prior_card is not None:
        parts += [
            "",
            "prior_card (for change tracking):",
            json.dumps(_prior_card_compact(prior_card), indent=2, default=str),
        ]

    parts += [
        "",
        "Respond with ONLY this JSON object:",
        "{",
        '  "sub_prediction": {',
        '    "predicted_value": "<concrete value>",',
        '    "predicted_range": "<low — high>",',
        '    "confidence": 0.0,',
        '    "basis": ["<evidence + inference 1>", "<2>", "<3>"],',
        '    "invalidation_condition": "<specific observable>",',
        '    "market_implication": "<what this means for market probability>"',
        "  },",
        '  "verdict": {',
        '    "call": "underpriced|overpriced|fair",',
        '    "confidence": 0.0,',
        '    "reasoning": "<base rate or analytic argument>",',
        '    "watch_up": "<what would push higher>",',
        '    "watch_down": "<what would push lower>"',
        "  }",
    ]
    if mode == "reanalysis":
        parts += [
            "  ,",
            '  "changes_since_prior": {',
            '    "probability_delta": "<signed string e.g. +0.08>",',
            '    "new_developments": ["...", "..."],',
            '    "prior_prediction_event_resolved": true|false,',
            '    "prior_prediction_correct": true|false|null,',
            '    "components_changed": ["..."],',
            '    "components_unchanged": ["..."]',
            "  }",
        ]
    parts.append("}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# whole-run driver — 4 sub-agents stitched together
# ---------------------------------------------------------------------------

def _safe_dict(value: Any, fallback: Optional[dict] = None) -> dict:
    if isinstance(value, dict):
        return value
    return dict(fallback) if fallback else {}


def _emit_gamma_selection_span(
    tracer: RunTracer,
    *,
    parent: Span,
    selected: "selector_module.SelectedMarket",
) -> None:
    """Synthesise a `tool.gamma_api` span describing the SELECTION-time
    Gamma fetch.

    Layer 1's tool framework already creates spans for any gamma_api call
    the LLM itself decides to make. But the markets-list fetch the
    *selector* makes (before the agent even starts) used to be invisible
    to the trace — the layer would show `tool_calls: 1` (a tavily_search)
    with no gamma_api evidence at all. This span captures it.
    """
    span = tracer.tool_span("gamma_api", parent)
    span.set("tool.name", "gamma_api")
    span.set("tool.phase", "selection")
    span.set("tool.input", "limit=100&active=true&closed=false")
    span.set("tool.output_items", int(selected.markets_fetched))
    span.set("tool.latency_ms", int(selected.gamma_fetch_ms))
    span.set("tool.fetch_calls", int(selected.gamma_fetch_calls))
    span.set("tool.success", selected.markets_fetched > 0)
    span.set(
        "tool.output_preview",
        f"selected market id={selected.market.get('id')} "
        f"out of {selected.markets_fetched} fetched",
    )
    span.set(
        "tool.output_raw_chars",
        len(str(selected.market.get("description") or "")),
    )
    # `Span.start_time_ns` defaulted to now(); back-date end to match
    # so the OTel duration reflects what we know.
    span.end()


def _run_layered_agent(
    *,
    mode: str,
    selected: "selector_module.SelectedMarket",
    prior_card: Optional[dict],
    run_id: str,
    tracer: RunTracer,
    root: Span,
) -> tuple[dict, dict]:
    """Run all 4 layers as separate sub-agents; assemble the final payload."""
    provider = _llm_provider()
    if provider == "openai":
        if not (os.getenv("OPENAI_API_KEY") or "").strip():
            raise RuntimeError(
                "LLM_PROVIDER=openai requires OPENAI_API_KEY in the environment (.env)."
            )
        client: Any = OpenAI()
    else:
        client = anthropic.Anthropic()  # picks up ANTHROPIC_API_KEY from env

    root.set("run.llm_provider", provider)

    # Shared rate-limit pacing across all 4 sub-agents.
    backoff_state: dict = {"not_before_mono": 0.0}

    # Per-run instrumentation aggregators.
    # `context_stats`     — used by P2-3 to surface context-window growth.
    # `rate_limit_stats`  — used by P2-4 to summarise 429 events at the root.
    # `components_set`    — used by P1-5 to record which components L3 hit.
    context_stats: dict = {"first_input_tokens": 0, "max_input_tokens": 0}
    rate_limit_stats: dict = {"events": 0, "wait_seconds_total": 0.0}

    market = selected.market
    selection_score = selected.score
    selection_reason = selected.reason

    market_compact = _compact_market(market)
    market_type = selector_module.classify_market_type(market.get("question") or "")
    framework_fit_label = selector_module.framework_fit(market_type)

    total_input_tokens = 0
    total_output_tokens = 0
    total_tool_calls = 0
    tools_called: list[str] = []
    layer_iterations: dict[int, int] = {}
    layer_stop_reasons: dict[int, str] = {}

    def _accumulate(meta: dict) -> None:
        nonlocal total_input_tokens, total_output_tokens, total_tool_calls
        total_input_tokens += int(meta.get("input_tokens", 0) or 0)
        total_output_tokens += int(meta.get("output_tokens", 0) or 0)
        tools_called.extend(meta.get("tools_called") or [])
        total_tool_calls += sum((meta.get("tool_call_counts") or {}).values())

    # ---- print a single estimated budget summary for the run ----
    sys1 = _load_subagent_prompt(1)
    sys4 = _load_subagent_prompt(4)
    cap_l1 = _layer_max_tokens(1)
    cap_l2 = _layer_max_tokens(2)
    cap_l3 = _layer_max_tokens(3)
    cap_l4 = _layer_max_tokens(4)
    approx_l1_call = (len(sys1) + 1200) // 4 + cap_l1
    approx_l4_call = (len(sys4) + 3500) // 4 + cap_l4
    root.set("llm.estimated_first_call_input_tokens", (len(sys1) + 1200) // 4)
    print(
        f"[budget] layered-subagents  L1 first-call~{approx_l1_call}t  "
        f"L4 synthesis-call~{approx_l4_call}t  "
        f"(per-layer caps L1={cap_l1}/L2={cap_l2}/L3={cap_l3}/L4={cap_l4}) "
        f"provider={provider}",
        flush=True,
    )

    # ===== Layer 1 =====
    l1_span = tracer.layer_span(LAYER_NAMES[1], root)
    l1_span.set("layer", 1)
    # P0-2: capture the SELECTION-time gamma_api fetch under Layer 1. This
    # has to happen BEFORE the sub-agent runs so the gamma span is
    # ordered first in the layer's child list.
    if selected.gamma_fetch_calls > 0:
        _emit_gamma_selection_span(tracer, parent=l1_span, selected=selected)
    try:
        l1_text, l1_meta = _run_layer_subagent(
            layer_num=1,
            system_prompt=_load_subagent_prompt(1),
            system_prompt_file="subagent_layer1.md",
            layer_prompt_file="layer1_scoring.md",
            initial_user_message=_user_msg_layer1(
                market_compact, selection_score, selection_reason, mode, prior_card,
            ),
            tools=_tools_for_layer(1),
            max_iterations=MAX_TURNS_LAYER1,
            max_tokens=_layer_max_tokens(1),
            client=client,
            backoff_state=backoff_state,
            tracer=tracer,
            layer_span=l1_span,
            context_stats=context_stats,
            rate_limit_stats=rate_limit_stats,
        )
        _accumulate(l1_meta)
        layer_iterations[1] = int(l1_meta.get("iterations", 0))
        layer_stop_reasons[1] = str(l1_meta.get("stop_reason", ""))
        l1_findings = _safe_dict(_extract_json_from_text(l1_text))
        l1_span.set("tool_calls", sum((l1_meta.get("tool_call_counts") or {}).values()))
        l1_span.set("news_search_done",
                    (l1_meta.get("tool_call_counts") or {}).get("tavily_search", 0) > 0)
        l1_span.set("news_verified", bool(l1_findings.get("news_verified")))
        # P1-3: selection-quality + market-evaluation metadata.
        l1_span.set("markets_fetched", int(selected.markets_fetched))
        l1_span.set("markets_scored", int(selected.markets_fetched))
        l1_span.set("market_selected_id", str(market.get("id") or ""))
        l1_span.set(
            "market_selected_question",
            str(market.get("question") or "")[:120],
        )
        l1_span.set("selection_score", round(float(selection_score), 3))
        l1_span.set("selection_reason", str(selection_reason)[:200])
        l1_span.set("retries", int(selected.retries))
        try:
            l1_span.set("probability_now", float(market.get("probability_now") or 0.0))
        except (TypeError, ValueError):
            pass
        try:
            l1_span.set("volume_24hr", float(market.get("volume_24hr") or 0.0))
        except (TypeError, ValueError):
            pass
        # P2-5: market-type classification + framework-fit flag.
        l1_span.set("market.type", market_type)
        l1_span.set("market.framework_fit", framework_fit_label)
        l1_span.end()
    except Exception as exc:  # noqa: BLE001
        l1_span.set("layer.error", str(exc)[:500])
        l1_span.end(status_code=2)
        raise

    # ===== Layer 2 =====
    l2_span = tracer.layer_span(LAYER_NAMES[2], root)
    l2_span.set("layer", 2)
    try:
        l2_text, l2_meta = _run_layer_subagent(
            layer_num=2,
            system_prompt=_load_subagent_prompt(2),
            system_prompt_file="subagent_layer2.md",
            layer_prompt_file="layer2_impact.md",
            initial_user_message=_user_msg_layer2(market_compact, l1_findings),
            tools=_tools_for_layer(2),
            max_iterations=MAX_TURNS_LAYER2,
            max_tokens=_layer_max_tokens(2),
            client=client,
            backoff_state=backoff_state,
            tracer=tracer,
            layer_span=l2_span,
            context_stats=context_stats,
            rate_limit_stats=rate_limit_stats,
        )
        _accumulate(l2_meta)
        layer_iterations[2] = int(l2_meta.get("iterations", 0))
        layer_stop_reasons[2] = str(l2_meta.get("stop_reason", ""))
        l2_findings = _safe_dict(_extract_json_from_text(l2_text))
        queries = l2_meta.get("tavily_queries") or []
        l2_span.set("tavily_queries_made",
                    (l2_meta.get("tool_call_counts") or {}).get("tavily_search", 0))
        if queries:
            l2_span.set("query_1", queries[0])
        if len(queries) > 1:
            l2_span.set("query_2", queries[1])
        # P1-4: surface the L2 outputs that drive L3 research.
        components_to_research = l2_findings.get("components_to_research") or []
        if not isinstance(components_to_research, list):
            components_to_research = []
        key_event = (
            l2_findings.get("key_event_refined")
            or l1_findings.get("key_event")
            or ""
        )
        l2_span.set("key_event_identified", str(key_event)[:200])
        # `impact_severity` isn't part of L2's JSON contract; we record a
        # placeholder so the field exists for Discovery Agent. If the
        # schema is extended later, this attribute will pick up the real
        # value without further code changes.
        l2_span.set(
            "impact_severity",
            str(l2_findings.get("impact_severity") or "unspecified"),
        )
        l2_span.set("market_topic_category", str(selected.category or "unknown"))
        evidence_snippets = l2_findings.get("evidence_snippets") or []
        l2_span.set(
            "sources_found",
            len(evidence_snippets) if isinstance(evidence_snippets, list) else 0,
        )
        l2_span.set(
            "components_to_research",
            ",".join(str(c) for c in components_to_research)[:500],
        )
        l2_span.set("components_to_research_count", len(components_to_research))
        l2_span.set(
            "surface_impact_summary",
            str(l2_findings.get("surface_impact") or "")[:200],
        )
        l2_span.end()
    except Exception as exc:  # noqa: BLE001
        l2_span.set("layer.error", str(exc)[:500])
        l2_span.end(status_code=2)
        raise

    # ===== Layer 3 =====
    l3_span = tracer.layer_span(LAYER_NAMES[3], root)
    l3_span.set("layer", 3)
    try:
        l3_text, l3_meta = _run_layer_subagent(
            layer_num=3,
            system_prompt=_load_subagent_prompt(3),
            system_prompt_file="subagent_layer3.md",
            layer_prompt_file="layer3_factors.md",
            initial_user_message=_user_msg_layer3(market_compact, l1_findings, l2_findings),
            tools=_tools_for_layer(3),
            max_iterations=MAX_TURNS_LAYER3,
            max_tokens=_layer_max_tokens(3),
            client=client,
            backoff_state=backoff_state,
            tracer=tracer,
            layer_span=l3_span,
            context_stats=context_stats,
            rate_limit_stats=rate_limit_stats,
        )
        _accumulate(l3_meta)
        layer_iterations[3] = int(l3_meta.get("iterations", 0))
        layer_stop_reasons[3] = str(l3_meta.get("stop_reason", ""))
        l3_findings = _safe_dict(_extract_json_from_text(l3_text))
        counts = l3_meta.get("tool_call_counts") or {}
        exa_count = int(counts.get("exa_search", 0))
        extract_count = int(counts.get("tavily_extract", 0))
        tavily_search_count = int(counts.get("tavily_search", 0))
        l3_span.set("exa_queries_made", exa_count)
        l3_span.set("tavily_extracts_made", extract_count)
        l3_span.set("tavily_expert_queries_made", tavily_search_count)
        l3_span.set("total_tool_calls", sum(counts.values()))
        # P1-5: the actual components L3 researched + the conflict flag.
        components_researched_obj = l3_findings.get("components_researched") or {}
        if isinstance(components_researched_obj, dict):
            component_names = list(components_researched_obj.keys())
        elif isinstance(components_researched_obj, list):
            component_names = [str(c) for c in components_researched_obj]
        else:
            component_names = []
        l3_span.set(
            "components_researched",
            ",".join(component_names)[:500],
        )
        l3_span.set("components_count", len(component_names))
        l3_span.set(
            "conflicting_signals_found",
            bool(l3_findings.get("conflicting_signals")),
        )
        # Total sources = unique URLs across all component findings + expert
        # consensus, when present. We do a best-effort count and never crash.
        urls: set[str] = set()
        if isinstance(components_researched_obj, dict):
            for entry in components_researched_obj.values():
                if isinstance(entry, dict) and entry.get("source"):
                    urls.add(str(entry["source"]))
        expert = l3_findings.get("expert_consensus") or {}
        if isinstance(expert, dict) and expert.get("source"):
            urls.add(str(expert["source"]))
        l3_span.set("total_sources", len(urls))
        l3_span.end()
    except Exception as exc:  # noqa: BLE001
        l3_span.set("layer.error", str(exc)[:500])
        l3_span.end(status_code=2)
        raise

    # ===== Layer 4 (synthesis, no tools) =====
    l4_span = tracer.layer_span(LAYER_NAMES[4], root)
    l4_span.set("layer", 4)
    try:
        l4_text, l4_meta = _run_layer_subagent(
            layer_num=4,
            system_prompt=_load_subagent_prompt(4),
            system_prompt_file="subagent_layer4.md",
            layer_prompt_file="layer4_synthesis.md",
            initial_user_message=_user_msg_layer4(
                market_compact, l1_findings, l2_findings, l3_findings, mode, prior_card,
            ),
            tools=_tools_for_layer(4),  # empty list -> no tools attached
            max_iterations=MAX_TURNS_LAYER4,
            max_tokens=_layer_max_tokens(4),
            client=client,
            backoff_state=backoff_state,
            tracer=tracer,
            layer_span=l4_span,
            context_stats=context_stats,
            rate_limit_stats=rate_limit_stats,
        )
        _accumulate(l4_meta)
        layer_iterations[4] = int(l4_meta.get("iterations", 0))
        layer_stop_reasons[4] = str(l4_meta.get("stop_reason", ""))
        try:
            l4_findings = _safe_dict(_extract_json_from_text(l4_text))
        except Exception:
            l4_span.set("synthesis.error", "could_not_parse_json")
            l4_span.set("synthesis.raw_text_tail", l4_text[-400:] if l4_text else "")
            l4_span.end(status_code=2)
            raise

        sub = _safe_dict(l4_findings.get("sub_prediction"))
        verdict = _safe_dict(l4_findings.get("verdict"))
        l4_span.set("sub_prediction_value", str(sub.get("predicted_value", "")))
        l4_span.set("sub_prediction_range", str(sub.get("predicted_range", "")))
        try:
            l4_span.set("sub_prediction_confidence", float(sub.get("confidence")))
        except (TypeError, ValueError):
            pass
        l4_span.set(
            "evidence_pieces_used",
            len(sub.get("basis") or []) if isinstance(sub.get("basis"), list) else 0,
        )
        l4_span.set("market_verdict", str(verdict.get("call", "")))
        try:
            l4_span.set("verdict_confidence", float(verdict.get("confidence")))
        except (TypeError, ValueError):
            pass
        l4_span.end()
    except Exception as exc:  # noqa: BLE001
        if l4_span.end_time_ns is None:
            l4_span.set("layer.error", str(exc)[:500])
            l4_span.end(status_code=2)
        raise

    # ===== assemble final payload =====
    payload: dict = {
        "market": l1_findings,
        "layer3": l3_findings,
        "sub_prediction": l4_findings.get("sub_prediction") or {},
        "verdict": l4_findings.get("verdict") or {},
    }
    if mode == "reanalysis":
        payload["changes_since_prior"] = l4_findings.get("changes_since_prior") or {}

    meta = {
        "trace_id": tracer.trace_id,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "tool_call_count": total_tool_calls,
        "tools_called": tools_called,
        "turns": sum(layer_iterations.values()),
        "stop_reason": layer_stop_reasons.get(4, ""),
        "model": _active_model_id(),
        "llm_provider": provider,
        "mode": mode,
        "layer_iterations": layer_iterations,
        "layer_stop_reasons": layer_stop_reasons,
        "architecture": "layered-subagents-v1",
        # Per-run instrumentation summaries surfaced to the root span by
        # the caller of `_run_layered_agent`.
        "context_stats": context_stats,
        "rate_limit_stats": rate_limit_stats,
        "market_type": market_type,
        "market_framework_fit": framework_fit_label,
    }
    return payload, meta


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def run_agent(
    selected: selector_module.SelectedMarket,
    *,
    mode: str = "fresh",
    prior_card: Optional[dict] = None,
    parent_run_id: Optional[str] = None,
    run_id: Optional[str] = None,
    retries_used: int = 0,
) -> dict:
    """Run the full 4-layer agent and persist a card. Returns the card."""
    run_id = run_id or _build_run_id()
    tracer = RunTracer(run_id=run_id, traces_dir=os.getenv("TRACES_DIR", "traces"))
    root = tracer.root_span()
    root.set("run.market_question", selected.market.get("question"))
    root.set("run.market_category", selected.category)
    root.set("run.mode", mode)
    root.set("run.retries", retries_used)

    run_started = time.time()
    payload: Optional[dict] = None
    meta: dict = {"trace_id": tracer.trace_id, "mode": mode}
    status = "ok"
    failed_at_layer: Optional[int] = None
    failure_reason: Optional[str] = None

    try:
        payload, llm_meta = _run_layered_agent(
            mode=mode,
            selected=selected,
            prior_card=prior_card,
            run_id=run_id,
            tracer=tracer,
            root=root,
        )
        meta.update(llm_meta)
    except Exception as exc:  # noqa: BLE001
        status = "failed"
        failure_reason = str(exc)[:500]
        # try to attribute failure to the most recent open layer
        for span in reversed(tracer.spans):
            if span.name.startswith("layer_"):
                try:
                    failed_at_layer = int(span.name.split("_")[1].split(".")[0])
                except (ValueError, IndexError):
                    failed_at_layer = None
                break
        root.set("run.error", failure_reason)
        traceback.print_exc()

    duration = time.time() - run_started
    meta["retries"] = retries_used
    meta["run_duration_seconds"] = round(duration, 2)

    market_summary = _summarize_market_for_card(selected.market)
    market_summary["selection_score"] = round(selected.score, 3)
    market_summary["selection_reason"] = selected.reason
    market_summary["selection_category"] = selected.category

    run_number = writer_module.next_run_number()

    prior_summary: Optional[dict] = None
    analysis_number = 1
    if mode == "reanalysis" and prior_card is not None:
        prior_summary = {
            "run_id": prior_card.get("id"),
            "timestamp": prior_card.get("timestamp"),
            "sub_prediction": prior_card.get("sub_prediction"),
            "verdict": prior_card.get("verdict"),
            "probability_at_time": (prior_card.get("market") or {}).get(
                "probability_now"
            ),
        }
        analysis_number = int(prior_card.get("analysis_number", 1)) + 1

    if payload is None:
        payload = {}
    validation_errors = writer_module.validate_card_payload(payload, mode)
    if validation_errors and status == "ok":
        status = "failed"
        failure_reason = "; ".join(validation_errors)
        failed_at_layer = 4

    card = writer_module.build_card(
        run_id=run_id,
        run_number=run_number,
        mode=mode,
        market_summary=market_summary,
        agent_payload=payload,
        meta=meta,
        parent_run_id=parent_run_id,
        analysis_number=analysis_number,
        prior_analysis=prior_summary,
        status=status,
        failed_at_layer=failed_at_layer,
        failure_reason=failure_reason,
    )

    # finalise root span with summary attributes
    root.set("run.status", status)
    root.set("run.layers_completed", _layers_completed(tracer))
    root.set("run.total_tool_calls", meta.get("tool_call_count", 0))
    total_input = int(meta.get("total_input_tokens", 0) or 0)
    total_output = int(meta.get("total_output_tokens", 0) or 0)
    root.set("run.total_input_tokens", total_input)
    root.set("run.total_output_tokens", total_output)
    # P2-1: replace the ambiguous `llm.estimated_first_call_tpm_cost` field
    # (it was an int with no clear unit) with clearly-named, correctly-typed
    # cost + token totals on the root span.
    root.set("llm.prompt_tokens_total", total_input)
    root.set("llm.completion_tokens_total", total_output)
    root.set("llm.estimated_cost_usd", estimate_cost_usd(total_input, total_output))
    # P2-3: surface peak context-window usage + how much it grew across the run.
    context_stats = meta.get("context_stats") or {}
    max_in = int(context_stats.get("max_input_tokens", 0) or 0)
    first_in = int(context_stats.get("first_input_tokens", 0) or 0)
    root.set("run.max_context_window_tokens", max_in)
    cw_tokens = _context_window_tokens()
    if cw_tokens > 0:
        root.set(
            "run.max_context_window_pct",
            round(max_in / cw_tokens * 100, 2),
        )
    if first_in > 0:
        root.set("run.context_growth_ratio", round(max_in / first_in, 2))
    # P2-4: rate-limit summary. Without this you'd have to scan every LLM
    # span individually to see that 42% of the run was spent waiting on 429s.
    rl_stats = meta.get("rate_limit_stats") or {}
    rl_events = int(rl_stats.get("events", 0) or 0)
    rl_wait_seconds = float(rl_stats.get("wait_seconds_total", 0.0) or 0.0)
    root.set("run.rate_limit_events", rl_events)
    root.set("run.rate_limit_wait_seconds_total", round(rl_wait_seconds, 1))
    if duration > 0:
        root.set(
            "run.rate_limit_pct_of_runtime",
            round(rl_wait_seconds / duration * 100, 1),
        )
    # P2-5: surface market-type classification on the root for quick filtering.
    if meta.get("market_type"):
        root.set("run.market_type", str(meta["market_type"]))
        root.set("run.market_framework_fit", str(meta.get("market_framework_fit", "")))
    try:
        root.set("run.verdict", str((payload.get("verdict") or {}).get("call", "")))
    except AttributeError:
        pass
    try:
        root.set(
            "run.verdict_confidence",
            float((payload.get("verdict") or {}).get("confidence")),
        )
    except (TypeError, ValueError, AttributeError):
        pass
    try:
        root.set(
            "run.sub_prediction",
            str((payload.get("sub_prediction") or {}).get("predicted_value", "")),
        )
    except AttributeError:
        pass
    root.set("run.duration_seconds", round(duration, 2))

    writer_module.append_card(card)

    # P2-6: mirror the key health-check booleans onto the root span. Doing
    # this BEFORE we close the root span and write the trace makes a trace
    # file self-contained for diagnosis (no cross-reference to
    # run_health.json needed).
    try:
        health = writer_module.compute_run_health(card, tracer)
        _mirror_health_onto_root(root, health)
        writer_module.write_run_health(health)
    except Exception as exc:  # noqa: BLE001
        print(f"warning: run-health report failed: {exc}")

    root.end(status_code=1 if status == "ok" else 2)

    trace_path = tracer.write()
    print(f"Trace written: {trace_path}")

    if status == "ok" and mode == "fresh":
        try:
            selector_module.record_analysis(
                market_id=str(selected.market.get("id")),
                question=str(selected.market.get("question")),
                category=selected.category,
                run_id=run_id,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"warning: cooldown registration failed: {exc}")

    return card


def _mirror_health_onto_root(root: Span, health: dict) -> None:
    """P2-6: copy the boolean run-health checks onto the root span.

    The same booleans are written to `output/run_health.json`, but
    embedding them on the root span means a single trace file is enough
    for Discovery Agent to make a pass/fail call — no cross-referencing
    a second file.
    """
    checks = (health or {}).get("checks") or {}
    summary = (health or {}).get("summary") or {}

    def _check(name: str) -> bool:
        return bool(checks.get(name, False))

    root.set("health.layer3_depth_ok", _check("layer3_exa_depth"))
    root.set("health.tool_variety_ok", _check("tool_variety"))
    root.set("health.confidence_varied", _check("confidence_not_flat"))
    root.set("health.extract_used", _check("tavily_extract_used"))
    root.set("health.no_empty_extracts", _check("no_empty_extracts"))
    root.set("health.layer2_query_relevant", _check("layer2_query_relevant"))
    root.set("health.retry_logic_recent", _check("retry_logic_fired_recent"))

    # Critical checks for an "overall: pass" verdict. Tool variety + depth
    # demonstrate the agent actually used its research toolkit; confidence
    # varying across runs guards against the model degenerating to a
    # single canned score.
    critical_pass = (
        _check("layer3_exa_depth")
        and _check("tool_variety")
        and _check("confidence_not_flat")
        and _check("no_empty_extracts")
    )
    root.set("health.overall", "pass" if critical_pass else "fail")
    root.set("health.checks_passed", int(summary.get("pass", 0) or 0))
    root.set("health.checks_failed", int(summary.get("fail", 0) or 0))


def _layers_completed(tracer: RunTracer) -> int:
    completed = 0
    for s in tracer.spans:
        if s.name.startswith("layer_") and s.end_time_ns is not None and s.status_code == 1:
            completed += 1
    return completed


# ---------------------------------------------------------------------------
# convenience wrapper used by main.py + test_run.py + reanalysis_run.py
# ---------------------------------------------------------------------------

def run_once(
    *,
    category_override: Optional[str] = None,
    keyword_filter: Optional[str] = None,
    market_id_override: Optional[str] = None,
    force: bool = False,
) -> Optional[dict]:
    """Pick a market via the selector, then run the fresh agent. Includes
    the spec's retry loop: if news verification (as decided by the model)
    fails, the selector is re-invoked with cooldown skipped to find the
    next-best market, up to MAX_RETRIES."""
    max_retries = int(os.getenv("MAX_RETRIES", "3"))
    skip_cooldown = force or bool(keyword_filter) or bool(market_id_override)

    attempt = 0
    last_market_id: Optional[str] = None
    while attempt <= max_retries:
        selected = selector_module.select_market(
            category_override=category_override,
            keyword_filter=keyword_filter,
            market_id_override=market_id_override,
            skip_cooldown=skip_cooldown,
        )
        if not selected:
            print("No market found by selector.")
            return None
        if selected.market.get("id") == last_market_id:
            # selector handed us the same market again — bail to avoid loop
            break

        card = run_agent(selected, mode="fresh", retries_used=attempt)
        # Layer 1 news-verification retry rule: only fresh runs, only when
        # the agent itself flagged news_verified=False AND a retry budget
        # remains AND we're in default scheduled flow (no overrides).
        news_verified = (card.get("analysis") or {}).get("news_verified")
        if (
            attempt < max_retries
            and news_verified is False
            and not (keyword_filter or market_id_override or category_override)
        ):
            attempt += 1
            last_market_id = selected.market.get("id")
            skip_cooldown = True  # avoid re-picking the just-cooled-down market
            print(f"News not verified - retrying ({attempt}/{max_retries})")
            continue
        return card
    return None


def run_reanalysis_for_run_id(run_id: str) -> Optional[dict]:
    prior = writer_module.get_card_by_run_id(run_id)
    if not prior:
        print(f"No card found for run_id={run_id}")
        return None
    return _reanalyse(prior)


def run_reanalysis_for_market(market_id: str) -> Optional[dict]:
    prior = writer_module.most_recent_card_for_market(market_id)
    if not prior:
        print(f"No prior card for market_id={market_id}")
        return None
    return _reanalyse(prior)


def _reanalyse(prior_card: dict) -> Optional[dict]:
    market_id = (prior_card.get("market") or {}).get("id")
    if not market_id:
        print("Prior card has no market id; cannot re-analyse.")
        return None
    market = gamma_tool.fetch_market(str(market_id))
    if not market:
        print(f"Could not re-fetch market {market_id}.")
        return None

    selected = selector_module.SelectedMarket(
        market=market,
        score=0.0,
        reason="re-analysis of prior card; selector bypassed",
        category=(prior_card.get("market") or {}).get("category")
                 or selector_module.categorize_market(market),
    )
    return run_agent(
        selected,
        mode="reanalysis",
        prior_card=prior_card,
        parent_run_id=prior_card.get("id"),
    )
