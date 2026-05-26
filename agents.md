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
- `state/`            Runtime state. Category rotation, topic cooldown, `users.db`. Git-ignored.
- `output/`           Agent output cards. Git-ignored.
- `traces/`           OTel trace files. Kept locally for Discovery Agent ingestion.
- `ui/`               Landing, auth pages, and feed UI (static HTML).
- `web/`              FastAPI app: signup/login (SQLite), protected `/api/feed`.
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
If you are writing `tool_1(); tool_2(); tool_3()` — stop. That is a pipeline.

### Mode Flag
`orchestrator.py` accepts `mode`: `"fresh"` or `"reanalysis"`.
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
