# Polymarket Research Agent

An autonomous agent that wakes every 2 hours, picks the most interesting
Polymarket prediction market, investigates it across **4 reasoning layers**
using **4 distinct tools**, makes a concrete sub-prediction about the
underlying data event driving the market, and publishes a plain-English
intelligence card to a local JSON feed and a minimal web UI.

No trading. No wallet. Pure research and reasoning.

A **Foretell** web app (`web/`) provides a landing page, email/password signup and login, and a protected feed UI at `/app`.

Every agent action is traced in **OpenTelemetry-compatible JSON** so
external monitoring (e.g. the Discovery Agent) can ingest the traces
with no custom adapter.

---

## Quickstart

```bash
# 1. Install dependencies
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. Configure API keys
cp .env.example .env
# Edit .env and set:
#   ANTHROPIC_API_KEY=sk-ant-...
#   TAVILY_API_KEY=tvly-...
#   EXA_API_KEY=...
#   ANTHROPIC_MODEL=claude-sonnet-4-6   # must be a real model id from Anthropic docs
#   AUTH_SECRET=...                     # long random string for session cookies

# 3. Run once (manual trigger)
python test_run.py                        # default — uses category rotation
python test_run.py --topic "iran war"     # keyword match
python test_run.py --category politics    # force a category
python test_run.py --force                # ignore cooldowns

# 4. Run on a 2-hour cron
python -m agent.main

# 5. Run the web app (landing, auth, protected feed)
#    Do NOT use `python -m http.server` — /signup, /login, and auth will 404.
python run_web.py
# → http://localhost:8000/          landing page
# → http://localhost:8000/signup    create account
# → http://localhost:8000/login     sign in
# → http://localhost:8000/app       feed (requires login)
# → http://localhost:8000/app/run   start a new analysis (topic / category / market)
```

### Web UI: run your own analysis

After signing in:

1. Open **http://localhost:8000/app/run**
2. Choose **Fresh analysis** (topic keyword, category, or market id/slug) or **Re-analysis** (prior run or market from your feed)
3. Submit — the run executes in the background (~1–2 minutes). The page polls until complete.
4. Results appear on **/app** (your personal feed only)

User runs are stored under `output/users/{user_id}/` (feed, traces, scorecard). The scheduled agent (`python -m agent.main`) still writes to the global `output/feed.json` and is separate from per-user web feeds.

Rate limits (`.env`): `WEB_MAX_CONCURRENT_RUNS` (default 1), `WEB_RUNS_PER_USER_PER_DAY` (default 5).

### Anthropic rate limits (HTTP 429)

Each `messages` call sends the **full system prompt** (all layer markdown + schema)
**again**, plus the growing tool/assistant history. A multi-turn run uses a lot of
**input tokens per minute (TPM)**. If your org is capped (e.g. 10k TPM), you may see
`rate_limit_error`.

The orchestrator **waits** (default 60s, or `Retry-After`) and retries on 429, up to
`LLM_429_RETRY_MAX` **total HTTP attempts per single** `messages.create` (not “extra”
retries on top of the first try — the value is capped at 20 to avoid runaway loops).

A **per-run backoff** also applies: after a 429 wait or a successful call, the next
turn will honor `LLM_MIN_SECONDS_BETWEEN_API` so you do not immediately hit the same
TPM window again.

You can set `LLM_INTER_TURN_SECONDS=8` in `.env` to pause between turns. If a **single**
request is still over your TPM, shorten prompts or raise your Anthropic limit.

---

## What gets produced per run

```
output/feed.json          # scheduled agent feed (global)
output/users/{user_id}/   # per-user web runs (feed, traces, scorecard)
traces/trace_<run_id>.json # scheduled-agent traces (global TRACES_DIR)
state/analysed_topics.json # 48hr cooldown tracker
state/category_rotation.json
state/users.db            # web app users + agent_jobs (git-ignored with state/)
```

---

## The 4 Tools

| Tool            | Purpose                                | Primary Layers |
|-----------------|----------------------------------------|----------------|
| `gamma_api`     | Polymarket REST API for market data    | 1              |
| `tavily_search` | Breaking news + current events         | 1, 2, 3        |
| `exa_search`    | Neural/semantic deep factor research   | 3              |
| `tavily_extract`| Full article extraction by URL         | 3              |

---

## The 4-Layer Loop

The orchestrator is an **agentic loop**, not a pipeline. After every tool
result Claude sees the output and decides what to do next.

- **Layer 1** — verify the pre-selected market with news. Retry with the
  next-best market if no news drives the move.
- **Layer 2** — surface impact research. Identify the precise upcoming
  data event that will resolve the market.
- **Layer 3** — deep factor research. Decompose the event into
  components (e.g. CPI → shelter, energy, food) and research each one.
- **Layer 4** — synthesise a concrete sub-prediction + market verdict.

See `polyPredictionAgent/spec.md` for the full design document.

---

## Manual triggers

```bash
python test_run.py --market-id fed-rate-july-2026
python reanalysis_run.py --run-id run_20260511_140000
python reanalysis_run.py --stale-days 3
python scorecard.py
```

---

## Hard rules (don't break)

- Agent state uses flat JSON files only (no SQL for agent data).
- Web UI is static HTML served by FastAPI in `web/`; feed JSON is not publicly mounted.
- No LangChain. Raw Anthropic SDK — Discovery Agent monitors raw tool calls.
- All prompts live in `agent/prompts/`. Never inline prompt strings.
- Every tool call and every LLM call goes through `tracer.py`.
