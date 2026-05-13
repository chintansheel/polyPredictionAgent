# Polymarket Research Agent — Implementation Spec v2

## What This Is

An autonomous agent that wakes every 2 hours, picks the most interesting
prediction market on Polymarket, investigates it across 4 reasoning layers,
makes a concrete sub-prediction about the underlying data event driving
the market, and publishes a plain-English intelligence card to a local
JSON feed and a minimal web UI.

No trading. No wallet. No auth. Pure research and reasoning.

Every agent action is traced in OpenTelemetry-compatible JSON so the
Discovery Agent can monitor tool selection, token usage, reasoning quality,
and prompt regressions in real time.

---

## Goal

Build this agent primarily to stress-test the Discovery Agent monitoring
system. The agent must:

- Use **4 distinct tools** with different purposes so tool selection
  can be monitored and evaluated
- Perform **4-layer reasoning** where each layer's output shapes the
  next layer's direction — not a linear pipeline
- Make a **concrete sub-prediction** (e.g. "CPI will print at 2.8%")
  that can be validated within days, not months
- Emit **OpenTelemetry-format traces** to JSON files on every run so
  Discovery Agent can ingest and analyze them without any custom adapter
- Run **fully autonomously** on a cron schedule with zero human input

---

## API — No Auth Required

**Gamma API** (all we need for market data):
```
Base URL: https://gamma-api.polymarket.com
Auth:     None — fully public, no keys, no wallet
```

Key endpoints:
```
GET /markets?limit=20&active=true&closed=false   # active markets
GET /markets?id={id}                              # single market
GET /events?limit=10&active=true                  # events feed
```

Key fields per market:
- `question`        — the bet question
- `outcomePrices`   — ["0.62", "0.38"] means 62% YES
- `volume`          — total all-time volume
- `volumeClob24hr`  — 24hr volume — use this to detect movement
- `liquidity`       — current liquidity depth
- `startDate`       — market open date
- `endDate`         — resolution date
- `description`     — full context paragraph

No keys. No wallet. Just HTTP GET.

---

## Tools — 4 Distinct Tools

Each tool has a different purpose. The agent must choose the right tool
for each reasoning step. This variety is what makes Discovery Agent's
tool-selection monitoring meaningful — it can detect if the agent is
misusing tools or skipping tools after a prompt change.

```
tool_1: gamma_api       — Polymarket Gamma REST API
                          Purpose: fetch and score active markets
                          When to use: Layer 1 only

tool_2: tavily_search   — AI-optimized web search
                          Returns structured JSON with extracted
                          content, ready for LLM context injection
                          Purpose: breaking news, current events,
                          official statements, recent price moves
                          When to use: Layer 1 verification,
                          Layer 2 surface research, Layer 3 expert
                          consensus

tool_3: exa_search      — Neural/semantic search
                          Finds conceptually related content, not
                          just keyword matches. Returns full content.
                          Purpose: deep factor research, historical
                          patterns, academic analysis, underlying
                          component data
                          When to use: Layer 3 factor research
                          (majority of calls should be here)

tool_4: tavily_extract  — Full article extraction by URL
                          Purpose: when a search snippet is not
                          enough and the full article is needed
                          When to use: Layer 3, when exa or tavily
                          returns a highly relevant URL worth reading
                          in full
```

Discovery Agent watches: is the agent using exa for deep research and
tavily for news? Or are they being used interchangeably? Does
tavily_extract disappear from traces after a prompt change? These are
real regression signals.

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                    CRON (every 2 hrs)                    │
└─────────────────────────┬────────────────────────────────┘
                          │
                          ▼
┌──────────────────────────────────────────────────────────┐
│               TRACER (wraps entire run)                  │
│  Creates root span on start. Every tool call and LLM     │
│  call gets a child span. Writes OTel JSON on completion. │
└─────────────────────────┬────────────────────────────────┘
                          │
                          ▼
┌──────────────────────────────────────────────────────────┐
│              ORCHESTRATOR (agentic loop)                 │
│                                                          │
│  Passes tool results back to LLM after every call.       │
│  LLM decides next tool based on what it found.           │
│  Not a pipeline — a reasoning loop with branches.        │
└──────────────────────────────────────────────────────────┘
         │              │               │              │
         ▼              ▼               ▼              ▼
    [Layer 1]      [Layer 2]       [Layer 3]      [Layer 4]
    gamma_api      tavily_search   exa_search     No new tools
    Market select  News verify     Deep factor    Pure synthesis
    & scoring      Impact          research       Sub-prediction
                   Key event ID    (3-4 calls)    + verdict
         │              │               │              │
         └──────────────┴───────────────┴──────────────┘
                                   │
                                   ▼
                    ┌──────────────────────────┐
                    │       OUTPUT WRITER       │
                    │  output/feed.json         │
                    │  output/latest.json       │
                    │  output/scorecard.json    │
                    │  traces/trace_{id}.json   │
                    └──────────────┬───────────┘
                                   │
                                   ▼
                    ┌──────────────────────────┐
                    │     UI (index.html)       │
                    │  Reads feed.json          │
                    └──────────────────────────┘
```

---

## The 4-Layer Reasoning Loop

The orchestrator must be implemented as a proper agentic loop — not a
fixed sequence of function calls. After every tool result, the LLM
decides what to do next. The layers describe the intended reasoning
flow, but the agent controls execution.

```python
# Correct pattern — agentic loop
messages = [system_prompt]
while not done:
    response = claude.call(messages, tools=available_tools)
    if response.stop_reason == "tool_use":
        result = execute_tool(response.tool_call)
        messages.append(response)
        messages.append(tool_result_message(result))
        # loop — LLM sees result and decides next step
    else:
        done = True
        write_card(response.content)
```

---

### Layer 1 — Market Selection & Verification

**Primary tool:** `gamma_api`
**Secondary tool:** `tavily_search` (to verify news exists)

**Agent task:**
- Fetch top 20 active markets from Gamma API
- Score each market on: 24hr price movement, volume spike, days
  until resolution, liquidity depth
- Select the market with the highest interesting signal score
- Immediately verify with tavily_search that real-world news is
  driving the price move

**Decision branch — this makes it non-linear:**
- News found → proceed to Layer 2
- No news found → reject, pick next highest scored market, retry
  (max 3 retries before taking highest volume market regardless)

**Span logged:** `layer_1.market_selection`

**Output passed to Layer 2:**
```json
{
  "market": {
    "id": "...",
    "question": "Will the Fed cut rates before July 2026?",
    "probability_now": 0.62,
    "probability_24hr_ago": 0.48,
    "probability_delta": 0.14,
    "volume_24hr": 284000,
    "closes": "2026-06-30"
  },
  "selection_score": 0.84,
  "selection_reason": "14pp move in 24hrs, $284k volume spike",
  "news_headline": "Fed minutes suggest rate cut discussions began",
  "news_url": "https://reuters.com/...",
  "news_verified": true,
  "retries": 1
}
```

---

### Layer 2 — Surface Research & Impact

**Tool:** `tavily_search` (1-2 calls, topic-specific queries)

**Agent task:**
- Construct a targeted search query based on the market TOPIC
  (not generic — derived from what Layer 1 found)
- Examples:
  - Fed rate market → `"fed rate cut July 2026 mortgage savings impact"`
  - Recession market → `"US recession probability 2026 jobs layoffs"`
  - Election market → `"[candidate] swing state polling June 2026"`
  - Geopolitical market → `"[event] ceasefire negotiations latest"`
- Synthesize: who does this affect, how, and how concretely?
- **Critically: identify the specific upcoming data event or
  announcement that will resolve the market uncertainty.** This
  becomes the input to Layer 3.

**Span logged:** `layer_2.surface_research`

**Output passed to Layer 3:**
```json
{
  "impact_summary": "62% of bettors think Fed cuts rates by July.
    This would lower mortgage rates ~0.5%, reduce savings yields,
    boost stocks short-term.",
  "affected_groups": ["homeowners", "savers", "equity investors"],
  "impact_severity": "high",
  "key_upcoming_event": {
    "name": "April CPI Report",
    "date": "2026-05-13",
    "why_it_matters": "Below 3% CPI gives Fed cover to cut.
      Above 3.2% pushes cut past July.",
    "threshold_up": "CPI below 3.0%",
    "threshold_down": "CPI above 3.2%"
  }
}
```

---

### Layer 3 — Deep Factor Research

**THIS IS THE KEY LAYER. This is what separates this agent from
a news summarizer.**

**Primary tool:** `exa_search` (3-4 calls)
**Secondary tool:** `tavily_extract` (0-2 calls, when a specific URL
needs full content)
**Also:** `tavily_search` (1 call, for expert consensus/forecasts)

**Agent task:**
The agent digs into the UNDERLYING COMPONENTS of the key event
identified in Layer 2. It does not wait to see what happens — it
researches the factors that will determine the outcome right now.

**CPI example — the agent must:**
1. Identify major CPI components: shelter, energy, food, core
2. Search each component's current trend separately using exa_search:
   - `exa_search`: `"shelter inflation rent index April 2026 trend"`
   - `exa_search`: `"WTI crude oil energy prices April 2026 CPI"`
   - `exa_search`: `"grocery food prices April 2026 BLS data"`
3. Search for expert forecasts using tavily_search:
   - `tavily_search`: `"Wall Street CPI forecast May 13 2026 consensus"`
4. If exa returns a highly relevant URL, call tavily_extract to get
   full article content for deeper data

**The queries are dynamically constructed based on Layer 2's output.**
A jobs report would trigger: ADP payrolls, unemployment claims, sector
hiring, wage growth. A geopolitical event would trigger: diplomatic
signals, historical precedent, involved parties' stated positions, UN
statements.

The agent cannot know what to research in Layer 3 until Layer 2
identifies the key event. This is the chain that forces genuine
multi-hop reasoning.

**Span logged:** `layer_3.deep_factor_research`

**Output passed to Layer 4:**
```json
{
  "event": "April CPI Report — May 13 2026",
  "components_researched": {
    "shelter": {
      "finding": "Zillow Rent Index fell 0.3% in April",
      "direction": "down",
      "confidence": 0.8,
      "source": "zillow.com/research/..."
    },
    "energy": {
      "finding": "WTI crude down 8.2% in April, gasoline -6%",
      "direction": "down",
      "confidence": 0.9,
      "source": "eia.gov/..."
    },
    "food": {
      "finding": "Grocery prices flat, USDA weekly data stable",
      "direction": "neutral",
      "confidence": 0.7,
      "source": "usda.gov/..."
    }
  },
  "expert_consensus": {
    "median_forecast": "2.9%",
    "range": "2.6% — 3.1%",
    "source": "Bloomberg economist survey"
  },
  "community_signal": {
    "summary": "Economists citing energy as main downward driver",
    "sentiment": "slightly below consensus"
  }
}
```

---

### Layer 4 — Sub-Prediction Synthesis

**No new tool calls.** Pure LLM reasoning over Layers 1-3 output.

**Agent task:**
- Synthesize all evidence gathered across Layers 1-3
- Make a **concrete numerical prediction** about the key event
  (a specific number or binary, not "likely lower")
- State the explicit basis — which evidence supports each part
- State what would break the prediction (invalidation condition)
- State what the prediction implies for the market probability
- Give a final market verdict: overpriced / underpriced / fair

**This is fast-validatable:** when CPI prints on May 13, compare
the agent's 2.8% prediction against the actual number immediately.
You don't wait months for a Fed decision.

**Span logged:** `layer_4.prediction_synthesis`

**Layer 4 output — the card content:**
```json
{
  "sub_prediction": {
    "event": "April CPI Report",
    "event_date": "2026-05-13",
    "predicted_value": "2.8%",
    "predicted_range": "2.6% — 3.0%",
    "confidence": 0.66,
    "basis": [
      "Energy: WTI -8.2% in April → ~0.3pp downward pressure on CPI",
      "Shelter: Zillow rent index -0.3% → modest downward pressure",
      "Food: Flat → neutral",
      "Expert consensus median 2.9% — our estimate slightly below"
    ],
    "invalidation_condition": "If energy spiked after April 22 BLS
      data cutoff, this breaks. Check EIA data on May 12.",
    "market_implication": "If CPI prints 2.8%, Fed cut probability
      should move from 62% to ~78-82%. Current 62% underpriced
      assuming our CPI prediction is correct."
  },
  "market_verdict": "underpriced",
  "verdict_confidence": 0.64,
  "verdict_reasoning": "Historical base rate: Fed cuts within 60 days
    when CPI sub-3% for 2+ consecutive months is 71%. Current 62%
    is conservative if our CPI call is right."
}
```

---

## Final Output Card Schema

Written to `output/feed.json` after all 4 layers complete:

```json
{
  "id": "run_20260511_1400",
  "timestamp": "2026-05-11T14:00:00Z",
  "run_number": 42,
  "trace_id": "abc123def456",

  "market": {
    "question": "Will the Fed cut rates before July 2026?",
    "probability_now": 0.62,
    "probability_24hr_ago": 0.48,
    "probability_delta": "+14pp",
    "volume_24hr": 284000,
    "closes": "2026-06-30",
    "polymarket_url": "https://polymarket.com/event/..."
  },

  "analysis": {
    "selection_reason": "Largest 24hr move in active markets",
    "news_headline": "Fed minutes suggest rate cut discussions began",
    "news_source": "Reuters",
    "plain_english": "62% of bettors think the Fed cuts rates by July.
      This would lower your mortgage rate ~0.5% and reduce savings
      yields. Stocks would likely rally short term.",
    "key_event": "April CPI Report — May 13 2026"
  },

  "sub_prediction": {
    "predicted_value": "2.8%",
    "predicted_range": "2.6% — 3.0%",
    "confidence": 0.66,
    "basis": [
      "Energy prices down 8.2% in April",
      "Shelter costs easing per Zillow rent index",
      "Expert consensus at 2.9% — slightly below"
    ],
    "invalidation_condition": "Post-April-22 energy spike would break this",
    "actual_value": null,
    "prediction_correct": null
  },

  "verdict": {
    "call": "underpriced",
    "confidence": 0.64,
    "reasoning": "71% historical base rate when CPI sub-3% for 2+ months.",
    "watch_up": "CPI prints below 2.9% on May 13",
    "watch_down": "Fed Chair signals caution in May 7 speech"
  },

  "meta": {
    "layers_completed": 4,
    "tools_called": [
      "gamma_api", "tavily_search", "tavily_search",
      "exa_search", "exa_search", "exa_search",
      "tavily_extract", "tavily_search"
    ],
    "tool_call_count": 8,
    "total_input_tokens": 18400,
    "total_output_tokens": 2840,
    "retries": 1,
    "run_duration_seconds": 47,
    "trace_id": "abc123def456"
  }
}
```

---

## OpenTelemetry Tracing

### Why OTel Format

Discovery Agent ingests these trace files to monitor:
- Did the agent call the right tool at each layer?
- Did token usage spike after a prompt change?
- Did Layer 3 depth (exa call count) regress?
- Are confidence scores drifting or always the same value?
- Which Git commit correlates with accuracy changes?

Standard OTel JSON means Discovery Agent needs no custom adapter.
It is also compatible with any future observability tooling
(Jaeger, Tempo, etc.) if you ever want to go beyond flat files.

---

### Trace File Location

```
traces/
├── trace_run_20260511_1400.json    # one file per run
├── trace_run_20260511_1200.json
└── trace_run_20260511_1000.json
```

Discovery Agent watches the `traces/` directory. Every file is a
complete self-contained OTel trace export.

---

### OTel Trace Schema

One file per run. Root span wraps the entire run. Child spans for
each layer. Grandchild spans for every tool call and every LLM call.
Nothing skips the tracer.

```json
{
  "resourceSpans": [
    {
      "resource": {
        "attributes": [
          { "key": "service.name",    "value": { "stringValue": "polymarket-agent" } },
          { "key": "service.version", "value": { "stringValue": "1.0.0" } },
          { "key": "agent.run_id",    "value": { "stringValue": "run_20260511_1400" } }
        ]
      },
      "scopeSpans": [
        {
          "scope": { "name": "polymarket_agent.orchestrator" },
          "spans": [

            {
              "traceId": "abc123def456abc123def456abc12345",
              "spanId": "span_root_001",
              "parentSpanId": null,
              "name": "agent.run",
              "kind": 1,
              "startTimeUnixNano": "1747000800000000000",
              "endTimeUnixNano": "1747000847000000000",
              "status": { "code": 1 },
              "attributes": [
                { "key": "run.number",            "value": { "intValue": 42 } },
                { "key": "run.market_question",   "value": { "stringValue": "Will the Fed cut rates before July 2026?" } },
                { "key": "run.layers_completed",  "value": { "intValue": 4 } },
                { "key": "run.total_tool_calls",  "value": { "intValue": 8 } },
                { "key": "run.total_input_tokens","value": { "intValue": 18400 } },
                { "key": "run.total_output_tokens","value": { "intValue": 2840 } },
                { "key": "run.verdict",           "value": { "stringValue": "underpriced" } },
                { "key": "run.verdict_confidence","value": { "doubleValue": 0.64 } },
                { "key": "run.sub_prediction",    "value": { "stringValue": "2.8%" } },
                { "key": "run.retries",           "value": { "intValue": 1 } },
                { "key": "run.duration_seconds",  "value": { "doubleValue": 47.2 } }
              ]
            },

            {
              "traceId": "abc123def456abc123def456abc12345",
              "spanId": "span_l1_001",
              "parentSpanId": "span_root_001",
              "name": "layer_1.market_selection",
              "kind": 1,
              "startTimeUnixNano": "1747000800000000000",
              "endTimeUnixNano": "1747000808000000000",
              "status": { "code": 1 },
              "attributes": [
                { "key": "layer",                   "value": { "intValue": 1 } },
                { "key": "markets_fetched",          "value": { "intValue": 20 } },
                { "key": "market_selected_id",       "value": { "stringValue": "fed-rate-july-2026" } },
                { "key": "selection_score",          "value": { "doubleValue": 0.84 } },
                { "key": "selection_reason",         "value": { "stringValue": "14pp 24hr move, $284k volume" } },
                { "key": "news_verified",            "value": { "boolValue": true } },
                { "key": "retries",                  "value": { "intValue": 1 } }
              ]
            },

            {
              "traceId": "abc123def456abc123def456abc12345",
              "spanId": "span_tool_gamma_001",
              "parentSpanId": "span_l1_001",
              "name": "tool.gamma_api",
              "kind": 3,
              "startTimeUnixNano": "1747000800200000000",
              "endTimeUnixNano": "1747000800512000000",
              "status": { "code": 1 },
              "attributes": [
                { "key": "tool.name",         "value": { "stringValue": "gamma_api" } },
                { "key": "tool.input",        "value": { "stringValue": "limit=20&active=true&closed=false" } },
                { "key": "tool.output_items", "value": { "intValue": 20 } },
                { "key": "tool.latency_ms",   "value": { "intValue": 312 } },
                { "key": "tool.success",      "value": { "boolValue": true } }
              ]
            },

            {
              "traceId": "abc123def456abc123def456abc12345",
              "spanId": "span_llm_001",
              "parentSpanId": "span_l1_001",
              "name": "llm.call",
              "kind": 3,
              "startTimeUnixNano": "1747000800600000000",
              "endTimeUnixNano": "1747000802400000000",
              "status": { "code": 1 },
              "attributes": [
                { "key": "llm.model",         "value": { "stringValue": "claude-sonnet-4-20250514" } },
                { "key": "llm.layer",         "value": { "intValue": 1 } },
                { "key": "llm.purpose",       "value": { "stringValue": "market_scoring_and_selection" } },
                { "key": "llm.input_tokens",  "value": { "intValue": 3200 } },
                { "key": "llm.output_tokens", "value": { "intValue": 480 } },
                { "key": "llm.stop_reason",   "value": { "stringValue": "tool_use" } },
                { "key": "llm.tool_chosen",   "value": { "stringValue": "tavily_search" } },
                { "key": "llm.latency_ms",    "value": { "intValue": 1840 } }
              ]
            },

            {
              "traceId": "abc123def456abc123def456abc12345",
              "spanId": "span_l2_001",
              "parentSpanId": "span_root_001",
              "name": "layer_2.surface_research",
              "kind": 1,
              "startTimeUnixNano": "1747000808000000000",
              "endTimeUnixNano": "1747000818000000000",
              "status": { "code": 1 },
              "attributes": [
                { "key": "layer",                  "value": { "intValue": 2 } },
                { "key": "market_topic_category",  "value": { "stringValue": "monetary_policy" } },
                { "key": "tavily_queries_made",    "value": { "intValue": 2 } },
                { "key": "query_1",                "value": { "stringValue": "fed rate cut July 2026 mortgage impact" } },
                { "key": "sources_found",          "value": { "intValue": 7 } },
                { "key": "key_event_identified",   "value": { "stringValue": "April CPI Report — May 13 2026" } },
                { "key": "impact_severity",        "value": { "stringValue": "high" } }
              ]
            },

            {
              "traceId": "abc123def456abc123def456abc12345",
              "spanId": "span_l3_001",
              "parentSpanId": "span_root_001",
              "name": "layer_3.deep_factor_research",
              "kind": 1,
              "startTimeUnixNano": "1747000818000000000",
              "endTimeUnixNano": "1747000838000000000",
              "status": { "code": 1 },
              "attributes": [
                { "key": "layer",                      "value": { "intValue": 3 } },
                { "key": "event_researched",           "value": { "stringValue": "April CPI Report" } },
                { "key": "components_identified",      "value": { "intValue": 3 } },
                { "key": "exa_queries_made",           "value": { "intValue": 3 } },
                { "key": "tavily_extracts_made",       "value": { "intValue": 1 } },
                { "key": "tavily_expert_queries_made", "value": { "intValue": 1 } },
                { "key": "total_sources",              "value": { "intValue": 11 } },
                { "key": "conflicting_signals",        "value": { "boolValue": false } },
                { "key": "components_researched",      "value": { "stringValue": "shelter,energy,food" } }
              ]
            },

            {
              "traceId": "abc123def456abc123def456abc12345",
              "spanId": "span_l4_001",
              "parentSpanId": "span_root_001",
              "name": "layer_4.prediction_synthesis",
              "kind": 1,
              "startTimeUnixNano": "1747000838000000000",
              "endTimeUnixNano": "1747000844000000000",
              "status": { "code": 1 },
              "attributes": [
                { "key": "layer",                       "value": { "intValue": 4 } },
                { "key": "sub_prediction_value",        "value": { "stringValue": "2.8%" } },
                { "key": "sub_prediction_range",        "value": { "stringValue": "2.6-3.0%" } },
                { "key": "sub_prediction_confidence",   "value": { "doubleValue": 0.66 } },
                { "key": "evidence_pieces_used",        "value": { "intValue": 5 } },
                { "key": "invalidation_conditions",     "value": { "intValue": 1 } },
                { "key": "market_verdict",              "value": { "stringValue": "underpriced" } },
                { "key": "verdict_confidence",          "value": { "doubleValue": 0.64 } }
              ]
            }

          ]
        }
      ]
    }
  ]
}
```

---

### Tracer Implementation

Create `agent/tracer.py`. Import and use it in the orchestrator.
**Every tool call and every LLM call must go through the tracer.**
Nothing is logged after the fact — spans are opened before the call
and closed after.

```python
# agent/tracer.py

import time
import uuid
import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Optional

@dataclass
class Span:
    name: str
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    parent_span_id: Optional[str] = None
    start_time_ns: int = field(default_factory=lambda: time.time_ns())
    end_time_ns: Optional[int] = None
    attributes: dict = field(default_factory=dict)
    status_code: int = 1  # 1=OK, 2=ERROR
    kind: int = 1         # 1=INTERNAL, 3=CLIENT

    def end(self, status_code: int = 1):
        self.end_time_ns = time.time_ns()
        self.status_code = status_code

    def set(self, key: str, value: Any):
        self.attributes[key] = value

    def to_otel(self, trace_id: str) -> dict:
        return {
            "traceId": trace_id,
            "spanId": self.span_id,
            "parentSpanId": self.parent_span_id,
            "name": self.name,
            "kind": self.kind,
            "startTimeUnixNano": str(self.start_time_ns),
            "endTimeUnixNano": str(self.end_time_ns),
            "status": {"code": self.status_code},
            "attributes": [_attr(k, v) for k, v in self.attributes.items()]
        }


class RunTracer:
    def __init__(self, run_id: str, traces_dir: str = "traces"):
        self.run_id = run_id
        self.trace_id = uuid.uuid4().hex + uuid.uuid4().hex[:16]
        self.spans: list[Span] = []
        self.traces_dir = Path(traces_dir)
        self.traces_dir.mkdir(exist_ok=True)

    def root_span(self) -> Span:
        span = Span(name="agent.run")
        span.set("run.id", self.run_id)
        self.spans.append(span)
        return span

    def layer_span(self, name: str, parent: Span) -> Span:
        span = Span(name=name, parent_span_id=parent.span_id, kind=1)
        self.spans.append(span)
        return span

    def tool_span(self, tool_name: str, parent: Span) -> Span:
        span = Span(name=f"tool.{tool_name}",
                    parent_span_id=parent.span_id, kind=3)
        self.spans.append(span)
        return span

    def llm_span(self, parent: Span) -> Span:
        span = Span(name="llm.call",
                    parent_span_id=parent.span_id, kind=3)
        self.spans.append(span)
        return span

    def write(self) -> str:
        trace = {
            "resourceSpans": [{
                "resource": {
                    "attributes": [
                        _attr("service.name", "polymarket-agent"),
                        _attr("service.version", "1.0.0"),
                        _attr("agent.run_id", self.run_id)
                    ]
                },
                "scopeSpans": [{
                    "scope": {"name": "polymarket_agent.orchestrator"},
                    "spans": [s.to_otel(self.trace_id) for s in self.spans]
                }]
            }]
        }
        path = self.traces_dir / f"trace_{self.run_id}.json"
        path.write_text(json.dumps(trace, indent=2))
        return str(path)


def _attr(key: str, value: Any) -> dict:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    elif isinstance(value, int):
        return {"key": key, "value": {"intValue": value}}
    elif isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    else:
        return {"key": key, "value": {"stringValue": str(value)}}
```

**Usage pattern in orchestrator.py:**

```python
tracer = RunTracer(run_id=run_id)
root = tracer.root_span()

# --- Layer 1 ---
l1 = tracer.layer_span("layer_1.market_selection", root)

tool_s = tracer.tool_span("gamma_api", l1)
markets = gamma_api.fetch()
tool_s.set("tool.output_items", len(markets))
tool_s.set("tool.latency_ms", elapsed_ms)
tool_s.end()

llm_s = tracer.llm_span(l1)
response = claude.call(messages, tools=tools)
llm_s.set("llm.input_tokens", response.usage.input_tokens)
llm_s.set("llm.tool_chosen", response.tool_name)
llm_s.end()

l1.set("market_selected_id", selected_market.id)
l1.set("retries", retry_count)
l1.end()

# --- Layers 2, 3, 4 follow same pattern ---

root.set("run.verdict", verdict)
root.set("run.total_tool_calls", total_calls)
root.end()

path = tracer.write()
print(f"Trace written: {path}")
```

---

## File Structure

```
polymarket-agent/
├── agent/
│   ├── main.py              # entry point + APScheduler
│   ├── orchestrator.py      # 4-layer agentic loop (fresh + reanalysis modes)
│   ├── selector.py          # topic selection + cooldown + rotation logic
│   ├── tracer.py            # OTel trace writer
│   ├── tools/
│   │   ├── gamma.py         # Polymarket Gamma API
│   │   ├── tavily.py        # Tavily search + extract
│   │   └── exa.py           # Exa semantic search
│   ├── prompts/
│   │   ├── system_fresh.md      # system prompt for fresh analysis
│   │   └── system_reanalysis.md # system prompt for re-analysis mode
│   └── writer.py            # writes cards + scorecard
├── state/
│   ├── analysed_topics.json # cooldown tracker — markets seen recently
│   └── category_rotation.json # tracks last-used category per rotation
├── output/
│   ├── feed.json            # append-only card history
│   ├── latest.json          # most recent card only
│   ├── run_health.json      # automated quality checks per run
│   └── scorecard.json       # prediction accuracy over time
├── traces/
│   └── trace_{run_id}.json  # one OTel trace per run
├── ui/
│   └── index.html           # single-file feed + verdict UI
├── test_run.py              # manual trigger with topic/category override
├── reanalysis_run.py        # trigger re-analysis for a specific past card
├── scorecard.py             # nightly prediction accuracy checker
├── .env.example
├── requirements.txt
└── README.md
```

---

## Tech Stack

- **Language:** Python 3.11+
- **LLM:** Anthropic Claude API (`claude-sonnet-4-20250514`)
- **News search:** Tavily API — free tier, 1,000 calls/month
- **Deep/semantic search:** Exa API — free tier, 1,000 calls/month
- **Scheduler:** APScheduler (in-process, no infra needed)
- **Tracing:** `tracer.py` — custom OTel JSON writer, no collector needed
- **Storage:** Flat JSON files — no DB for MVP
- **UI:** Single HTML file reading feed.json — no framework needed

---

## Environment Variables

```env
ANTHROPIC_API_KEY=sk-ant-...
TAVILY_API_KEY=tvly-...
EXA_API_KEY=exa-...
RUN_INTERVAL_HOURS=2
MAX_RETRIES=3
OUTPUT_DIR=./output
TRACES_DIR=./traces
```

---

## Agent System Prompt (agent/prompts/system.md)

```
You are a prediction market research analyst. Your job is to investigate
a Polymarket prediction market and make a concrete prediction about the
underlying data event driving it — before that event happens.

You have 4 tools with distinct purposes:
- gamma_api:      fetch Polymarket market data and prices
- tavily_search:  search for breaking news, current events, official
                  statements. Use for Layer 1 verification, Layer 2
                  impact research, and expert forecast consensus.
- exa_search:     deep semantic search for factor research, historical
                  data, underlying component analysis. Use primarily
                  in Layer 3 for each component of the key event.
- tavily_extract: extract full content from a specific URL. Use when
                  a search snippet is not enough detail.

You MUST reason across 4 layers. After every tool result, decide what
to do next based on what you found. Do not follow a fixed sequence.

Layer 1: Select the most interesting market. Use gamma_api to fetch,
         tavily_search to verify news is driving it. Retry if stale.

Layer 2: Research surface impact using tavily_search. Identify the
         specific upcoming data event that will resolve uncertainty.
         Name it precisely: "April CPI Report — May 13", not "upcoming
         economic data".

Layer 3: Research the UNDERLYING FACTORS of that event using exa_search.
         Search each major component separately. For CPI: search shelter,
         energy, food individually. For jobs report: search ADP payrolls,
         claims, sector hiring individually. Use tavily_extract on the
         most relevant URL if the snippet is insufficient.

Layer 4: Synthesize everything into a concrete sub-prediction with a
         specific number or binary outcome, a confidence score (be honest
         — if uncertain say 0.55, not 0.80), explicit basis points, and
         one invalidation condition.

Rules:
1. Never leave Layer 3 without researching at least 3 components
2. Never state "watch for X" without also predicting what X will be
3. Confidence must vary — if every run is 0.65-0.70 you are not
   actually uncertain, you are filling a field
4. exa_search for deep factor research, tavily_search for current news
5. Every claim must cite the source you found it from

Output: structured JSON matching the card schema exactly.
```

---

## Validation — 3 Levels

### Level 1 — Automated, every run (run_health.json)

Parse the trace file after each run and check:

```python
checks = {
  "layer3_exa_depth":      trace.layer3.exa_queries_made >= 3,
  "tool_variety":          len(set(trace.root.tools_called)) >= 3,
  "layer2_query_relevant": layer2_query contains market topic keywords,
  "confidence_not_flat":   abs(confidence - prev_confidence) > 0.05,
  "tavily_extract_used":   "tavily_extract" in trace.root.tools_called,
  "retry_logic_fired":     at least 1 run in last 10 had retries > 0
}
```

Written to `output/run_health.json`. Green = pass, Red = fail.
Discovery Agent watches this file for regressions.

### Level 2 — Fast validation (days, automated)

Sub-predictions resolve quickly. `scorecard.py` runs nightly:
- Fetches resolved Polymarket markets
- Matches resolved cards in feed.json
- Checks `sub_prediction.predicted_value` vs `actual_value`
- Updates `prediction_correct` in the card and scorecard.json

```json
{
  "sub_predictions": {
    "total": 18, "resolved": 11,
    "correct_within_range": 7, "accuracy": 0.64
  },
  "market_verdicts": {
    "total": 18, "resolved": 4,
    "correct": 3, "accuracy": 0.75
  }
}
```

### Level 3 — Slow ground truth (weeks, auto)

Market resolutions (YES/NO) update cards in feed.json and roll up
into scorecard.json. Discovery Agent correlates accuracy shifts with
the Git commit that changed the system prompt at that time.

---

## Topic Selection Logic (agent/selector.py)

### The Problem Without This

Gamma API returns markets sorted by volume by default. Without
intervention the agent picks the same 3-5 high-liquidity markets
every run: Fed rates, Bitcoin price, US election outcomes. You get
12 identical cards a day and the agent never explores the full
breadth of Polymarket.

### Two-Layer Fix

**Layer A — Category Rotation**

Polymarket markets carry category tags. The selector maintains a
fixed rotation order across categories and forces each run to pick
from the next category in the sequence.

```python
CATEGORY_ROTATION = [
    "economics",      # Fed, inflation, recession, jobs
    "politics",       # elections, legislation, geopolitics
    "crypto",         # BTC, ETH, DeFi protocols
    "science_tech",   # AI announcements, space, climate
    "sports",         # championships, player contracts
    "geopolitics",    # wars, sanctions, treaties
    "business",       # IPOs, M&A, earnings
    "health",         # FDA approvals, outbreaks, pharma
]
```

State stored in `state/category_rotation.json`:
```json
{
  "last_category": "politics",
  "last_run": "2026-05-11T14:00:00Z",
  "rotation_index": 1
}
```

On each run the selector increments rotation_index and fetches
markets filtered to that category. If no good markets exist in
that category (low volume, no news), it skips to the next.

**Layer B — Topic Cooldown**

Every analysed market is logged with a cooldown expiry. Any market
whose cooldown has not expired is excluded from selection entirely.

State stored in `state/analysed_topics.json`:
```json
{
  "markets": {
    "fed-rate-july-2026": {
      "question": "Will the Fed cut rates before July 2026?",
      "category": "economics",
      "first_analysed": "2026-05-11T14:00:00Z",
      "last_analysed": "2026-05-11T14:00:00Z",
      "analysis_count": 1,
      "cooldown_until": "2026-05-13T14:00:00Z",
      "run_ids": ["run_20260511_1400"]
    },
    "bitcoin-100k-june": {
      "question": "Will Bitcoin hit $100k before June 2026?",
      "category": "crypto",
      "first_analysed": "2026-05-11T10:00:00Z",
      "last_analysed": "2026-05-11T10:00:00Z",
      "analysis_count": 1,
      "cooldown_until": "2026-05-13T10:00:00Z",
      "run_ids": ["run_20260511_1000"]
    }
  },
  "stats": {
    "total_unique_markets": 24,
    "categories_covered": ["economics", "politics", "crypto"],
    "last_updated": "2026-05-11T14:00:00Z"
  }
}
```

Default cooldown: **48 hours** per market.
Re-analysis runs (see below) do NOT reset the cooldown — they use a
separate `reanalysis_count` field and are explicitly triggered.

**Combined selection flow:**

```
1. Read category_rotation.json → get next category
2. Fetch markets for that category from Gamma API
3. Filter out any market in analysed_topics.json with active cooldown
4. Score remaining markets by: volume spike, price movement, liquidity
5. Pick top scorer that also has verifiable news (tavily verify)
6. If no valid market found in this category → advance to next category
7. Log selected market to analysed_topics.json with cooldown
8. Update category_rotation.json
```

**selector.py interface:**

```python
def select_market(
    category_override: str = None,   # for test_run.py
    keyword_filter: str = None,       # for test_run.py topic search
    skip_cooldown: bool = False        # for reanalysis_run.py
) -> SelectedMarket
```

---

## Test Script (test_run.py)

Allows manual triggering with a category or keyword override.
Bypasses the cron scheduler and runs the full agent pipeline once.
Outputs a card and trace identical to a scheduled run.

**Usage:**

```bash
# Run with a specific category
python test_run.py --category politics

# Run with a keyword — agent picks best matching market
python test_run.py --topic "iran war"
python test_run.py --topic "fed rate"
python test_run.py --topic "bitcoin"

# Run completely fresh — ignore cooldowns, pick best overall market
python test_run.py --force

# Run on a specific market ID (skips selection entirely)
python test_run.py --market-id "fed-rate-july-2026"
```

**How keyword matching works:**

When `--topic` is provided, the selector fetches all active markets,
scores them by semantic similarity to the keyword (simple: check if
keyword words appear in the market question or description), and picks
the highest scored match. No cooldown check in test mode.

**test_run.py implementation sketch:**

```python
import argparse
from agent.selector import select_market
from agent.orchestrator import run_agent

parser = argparse.ArgumentParser()
parser.add_argument("--category", type=str)
parser.add_argument("--topic", type=str)
parser.add_argument("--market-id", type=str)
parser.add_argument("--force", action="store_true")
args = parser.parse_args()

market = select_market(
    category_override=args.category,
    keyword_filter=args.topic,
    market_id_override=args.market_id,
    skip_cooldown=args.force or bool(args.topic) or bool(args.market_id)
)

print(f"Selected: {market.question}")
print(f"Running full 4-layer analysis...")

card = run_agent(market, mode="fresh")

print(f"Card written: output/feed.json")
print(f"Trace written: traces/trace_{card.run_id}.json")
print(f"Verdict: {card.verdict.call} ({card.verdict.confidence:.0%} confidence)")
print(f"Sub-prediction: {card.sub_prediction.predicted_value}")
```

---

## Re-Analysis Mode

### Design Decision: Same Agent, Different Mode

Re-analysis uses the same orchestrator, same 4-layer structure, same
tools, same trace format. What changes is the context injected into
the agent and the system prompt.

This is the right design because:
- Discovery Agent sees the same trace schema — no special handling needed
- The reasoning is richer: agent must reconcile prior beliefs with new
  evidence, which creates more interesting tool-selection patterns
- Cards are linked via `parent_run_id` so accuracy can be tracked across
  the full analysis chain for a single market

### Re-Analysis Trigger

```bash
# Trigger re-analysis for a specific past run
python reanalysis_run.py --run-id run_20260511_1400

# Trigger re-analysis for a market question (finds most recent card)
python reanalysis_run.py --market-id "fed-rate-july-2026"

# Re-analyse all markets that haven't been updated in 3+ days
python reanalysis_run.py --stale-days 3
```

### How Re-Analysis Differs From Fresh Run

**Context injected at start:**
The orchestrator is given the previous card as context before any
tool calls. The system prompt instructs it to treat this as a prior
belief to update, not a conclusion to confirm.

**Layer 1 (re-analysis mode):**
- Skip market selection entirely — market is already known
- Fetch current market data from Gamma API (prices may have moved)
- Compare current probability to previous probability
- Span logs: `probability_delta_since_last` — did the market move?

**Layer 2 (re-analysis mode):**
- Search for what has changed since the last analysis date
- Query: `"{market topic}" news after:{last_analysis_date}`
- Identify: did the key event the prior analysis predicted already happen?
  If yes, was the prediction correct?

**Layer 3 (re-analysis mode):**
- Re-research the same underlying factors, but looking for changes
- For each component from the prior analysis: has the data updated?
- exa_search queries include date context: "shelter inflation May 2026"
  vs prior "shelter inflation April 2026"
- Agent must explicitly state: "component X changed / unchanged since
  prior analysis"

**Layer 4 (re-analysis mode):**
- Produce an updated sub-prediction
- Explicitly compare to prior prediction:
  - "Prior prediction: 2.8%. New prediction: 2.6%. Revised down because
    energy prices fell further than expected."
- Mark prior prediction as validated/invalidated if the event has resolved
- Issue updated market verdict with change flag

### Re-Analysis Card Schema (additions to base card)

```json
{
  "id": "run_20260514_1000",
  "mode": "reanalysis",
  "parent_run_id": "run_20260511_1400",
  "analysis_number": 2,

  "market": { "...same fields..." },

  "prior_analysis": {
    "run_id": "run_20260511_1400",
    "timestamp": "2026-05-11T14:00:00Z",
    "sub_prediction": { "predicted_value": "2.8%", "confidence": 0.66 },
    "verdict": "underpriced",
    "probability_at_time": 0.62
  },

  "changes_since_prior": {
    "probability_delta": "+0.08",
    "probability_now": 0.70,
    "new_developments": [
      "Fed Chair Powell speech May 12 confirmed data-dependent stance",
      "Oil prices fell further -3% in past 3 days"
    ],
    "prior_prediction_event_resolved": false,
    "components_changed": ["energy"],
    "components_unchanged": ["shelter", "food"]
  },

  "sub_prediction": {
    "predicted_value": "2.6%",
    "predicted_range": "2.4% — 2.9%",
    "confidence": 0.71,
    "change_from_prior": "revised down from 2.8% — energy fell more",
    "basis": ["...updated basis..."],
    "actual_value": null,
    "prediction_correct": null
  },

  "verdict": {
    "call": "underpriced",
    "confidence": 0.72,
    "change_from_prior": "confidence increased — more evidence accumulated",
    "reasoning": "..."
  },

  "meta": { "...same fields, mode: reanalysis..." }
}
```

### Re-Analysis System Prompt (agent/prompts/system_reanalysis.md)

```
You are a prediction market research analyst reviewing your own prior
analysis. You made a prediction 3 days ago. Your job is to update it
based on new information.

You have the same 4 tools. Use them the same way.

You are given your prior analysis card as context. Treat it as a prior
belief — not a conclusion. New evidence should update it, even if that
means reversing your verdict.

Layer 1: Fetch current market data. Note probability change since prior.
         Has the market moved in the direction you predicted?

Layer 2: Search for developments SINCE your last analysis date.
         Focus on: did the key event you identified already happen?
         If yes — was your prediction correct? If not — what changed?

Layer 3: Re-research each component you identified before. Look for
         updates. For each one: state "changed" or "unchanged" and why.
         Only search components where you have reason to believe
         something may have shifted.

Layer 4: Issue an updated prediction. You MUST explicitly state:
         - What changed vs your prior analysis
         - Whether your confidence increased or decreased and why
         - If the prior prediction event has resolved, state if you
           were correct

Rules:
1. Intellectual honesty — if new evidence contradicts your prior,
   update the verdict. Do not anchor to your previous call.
2. Never repeat the prior analysis — only state what changed
3. Confidence should increase if evidence accumulated, decrease if
   contradicting evidence appeared

Output: structured JSON matching the re-analysis card schema.
```

---

## Build Order

1. `agent/tracer.py` — build and test tracing first; nothing skips it
2. `tools/gamma.py` — verify Gamma API returns expected fields
3. `tools/tavily.py` — test both search and extract endpoints
4. `tools/exa.py` — test semantic search with a sample query
5. `agent/selector.py` — topic selection, cooldown, category rotation
6. `agent/orchestrator.py` — 4-layer loop (fresh mode), wire tracer
7. `agent/writer.py` — write cards and traces to disk
8. `agent/main.py` — run once end-to-end manually, check all outputs
9. `test_run.py` — verify --topic and --category flags work
10. `ui/index.html` — display card feed
11. APScheduler in `main.py` — 2-hour cron
12. `orchestrator.py` reanalysis mode — add mode flag + prior context
13. `reanalysis_run.py` — trigger re-analysis by run-id or market-id
14. `scorecard.py` — add after first predictions can be validated

---

## MVP Success Criteria

- [ ] Agent completes all 4 layers without human input
- [ ] Every run produces a valid card in `output/feed.json`
- [ ] Every run produces a valid OTel trace in `traces/`
- [ ] Trace shows Layer 3 with 3+ exa_search spans
- [ ] Layer 2 query text visibly changes based on market topic
      across different runs (check traces manually)
- [ ] At least one run in first 10 shows retries > 0 in trace
- [ ] Sub-prediction has a specific number and confidence score
- [ ] `run_health.json` auto-updates after every run
- [ ] UI shows cards with prediction + verdict + confidence bar
- [ ] `state/analysed_topics.json` grows with each run — no repeats
      within 48-hour cooldown window
- [ ] `state/category_rotation.json` shows category advancing each run
- [ ] `python test_run.py --topic "iran war"` picks a relevant market
- [ ] `python test_run.py --category politics` picks a politics market
- [ ] `python reanalysis_run.py --run-id {id}` produces a re-analysis
      card linked to the parent via `parent_run_id`
- [ ] Re-analysis card explicitly states what changed vs prior
- [ ] `scorecard.py` can process a manually resolved test card

---

## agents.md — Root Convention File

Create this at the repo root. Cursor Code reads it automatically on
every session. It is the single source of truth for how this repo works.

```markdown
# Polymarket Research Agent

## What This Repo Is
An autonomous prediction market research agent. It wakes every 2 hours,
picks a Polymarket market, investigates it across 4 reasoning layers,
makes a concrete sub-prediction, and publishes an intelligence card.

This agent exists primarily to stress-test the Discovery Agent monitoring
system. Every design decision prioritises observable, traceable, non-linear
reasoning over simplicity.

## Repo Structure
- `agent/`            Core agent code. Orchestrator, selector, tracer, tools.
- `agent/prompts/`    All LLM prompts as .md files. Never inline prompts in code.
- `agent/tools/`      One file per external tool. No tool logic outside these files.
- `state/`            Runtime state. Category rotation + topic cooldown. Git-ignored.
- `output/`           Agent output cards. Git-ignored.
- `traces/`           OTel trace files. Kept locally for Discovery Agent ingestion.
- `ui/`               Single HTML file feed UI. No framework.
- `test_run.py`       Manual trigger with topic/category override.
- `reanalysis_run.py` Trigger re-analysis for a past card.
- `scorecard.py`      Nightly prediction accuracy checker.

## Key Conventions

### Never Inline Prompts
All prompts live in `agent/prompts/` as .md files.
Load them at runtime: `Path("agent/prompts/system_fresh.md").read_text()`
Never write prompt strings directly in Python files.

### Every Tool Call Goes Through the Tracer
Open a span before the call. Set attributes. Close after. No exceptions.
```python
span = tracer.tool_span("tavily_search", parent_span)
result = tavily.search(query)
span.set("tool.query", query)
span.set("tool.results_count", len(result))
span.set("tool.latency_ms", elapsed)
span.end()
```

### Agentic Loop — Not a Pipeline
The orchestrator passes tool results back to Claude after every call.
Claude decides the next tool. Do not hardcode a sequence of tool calls.
If you are writing tool_1(); tool_2(); tool_3() — stop. That is a pipeline.

### Mode Flag
orchestrator.py accepts `mode`: "fresh" or "reanalysis".
Same tools. Same trace format. Different system prompt and context.

### State Files
`state/analysed_topics.json` — cooldown tracker. Never delete entries.
`state/category_rotation.json` — rotation_index. Increment each scheduled run.

### Output Schema Is The Contract
Never change the card schema without updating BOTH:
- `agent/prompts/output_schema.md` — what the agent targets
- `agent/writer.py` — what gets validated before writing to feed.json
Silent schema drift is the hardest bug to catch.

### Trace Files
One file per run: `traces/trace_{run_id}.json`
Format: OpenTelemetry JSON export spec exactly. Do not invent fields.
Discovery Agent ingests these. Schema deviation breaks ingestion silently.

### Error Handling
On any layer failure: write partial card with `status: "failed"` and
`failed_at_layer: N`. Still write the trace. A failed trace is signal.
Never silently swallow exceptions.

### Adding a New Tool
1. Create `agent/tools/newtool.py` — single class, clear docstring
2. Add tool definition to `agent/orchestrator.py` tools list
3. Add tool name + purpose to agents.md Tools section below
4. Wrap every call: `tracer.tool_span("newtool", parent)`
5. Update `agent/prompts/system_fresh.md` — when should agent use it?

## Tools
| Tool            | Purpose                              | Primary Layers |
|-----------------|--------------------------------------|----------------|
| gamma_api       | Fetch and score Polymarket markets   | 1              |
| tavily_search   | Breaking news, current events        | 1, 2, 3        |
| exa_search      | Deep semantic/factor research        | 3              |
| tavily_extract  | Full article content from a URL      | 3              |

## Run Modes
```
python agent/main.py                              # scheduled, every 2hrs
python test_run.py --topic "iran war"             # keyword match
python test_run.py --category politics            # category override
python test_run.py --market-id fed-rate-july-2026 # specific market
python test_run.py --force                        # ignore cooldowns
python reanalysis_run.py --run-id run_20260511_1400
python reanalysis_run.py --stale-days 3           # all stale markets
python scorecard.py                               # update accuracy
```

## Environment
Copy `.env.example` to `.env`. Required:
- ANTHROPIC_API_KEY
- TAVILY_API_KEY
- EXA_API_KEY

## Hard Rules — Do Not Break These
- No database. Flat JSON files only.
- No web framework. UI is one static HTML file.
- No LangChain or agent framework. Raw Anthropic SDK only.
  Discovery Agent monitors raw tool calls. A framework hides them.
- No cross-run caching of tool results. Every run is a fresh investigation.
  Cached results produce fake traces that mislead Discovery Agent.
```

---

## Prompts Directory Structure

All prompts live in `agent/prompts/` as standalone `.md` files.
This enables clean Git diffs per prompt change — which is how Discovery
Agent correlates a specific commit to an accuracy shift.

```
agent/prompts/
├── system_fresh.md        # Agent identity, tools, global rules (fresh mode)
├── system_reanalysis.md   # Same but framed as "update your prior analysis"
├── layer1_scoring.md      # Market scoring rubric and retry logic
├── layer2_impact.md       # Impact research and key event identification
├── layer3_factors.md      # Factor decomposition and component research
├── layer4_synthesis.md    # Sub-prediction format and confidence rules
└── output_schema.md       # Exact JSON card schema with field descriptions
```

### Loading Pattern

```python
# agent/orchestrator.py
from pathlib import Path

def load_prompt(name: str) -> str:
    return (Path(__file__).parent / "prompts" / name).read_text()

def build_system_prompt(mode: str) -> str:
    base = load_prompt(f"system_{mode}.md")
    schema = load_prompt("output_schema.md")
    return f"{base}\n\n---\n\n{schema}"
```

System prompt is assembled at runtime from files.
Layer prompts are injected as user-turn context at each layer boundary —
not baked into the system prompt. This keeps the system prompt stable
and makes per-layer tuning isolated and testable.

### What Each File Contains

**`system_fresh.md`** (~200 words)
Agent identity. Full tools list with when-to-use for each. The 4-layer
structure overview. Global rules: always cite sources, confidence must
vary, never say "watch for X" without predicting X.

**`system_reanalysis.md`** (~200 words)
Identical structure to system_fresh but framed as belief updating.
Key addition: "if new evidence contradicts your prior verdict, update it.
Intellectual honesty over consistency."

**`layer1_scoring.md`** (~150 words)
Scoring formula: weight 24hr price delta (40%), volume spike (30%),
days-to-close urgency (20%), liquidity depth (10%). News verification
threshold: must find a named source headline, not just a price move.
Max 3 retries before fallback to highest-volume market.

**`layer2_impact.md`** (~150 words)
How to derive a topic-specific query — not generic. "fed rate cut July
2026 mortgage savings impact" not "economic news". Precision rule for
key event naming: "April CPI Report — May 13 2026" not "upcoming data".
Impact severity criteria (high/medium/low definitions).

**`layer3_factors.md`** (~250 words — most critical file)
How to decompose an event into components. Minimum 3 components before
proceeding. Tool selection rule: exa_search for factor data, tavily_search
for expert consensus, tavily_extract when a specific URL has critical data.
How to handle conflicting component signals. What "confidence per component"
means and how to set it honestly.

**`layer4_synthesis.md`** (~200 words)
Sub-prediction must be: a specific number or binary — not "likely lower".
Confidence rules: 0.50 = coin flip, 0.65 = lean, 0.80 = strong — be honest.
Invalidation condition must name a specific observable thing that would
break the prediction. Market implication must derive logically from the
sub-prediction, not be stated independently.

**`output_schema.md`** (~300 words)
Complete JSON card schema. Every field with type, description, and
example value. This is the contract between the agent and writer.py.
Any field the agent omits or misnames will fail writer.py validation.
Keeping it here means the schema is always in the agent's context window.