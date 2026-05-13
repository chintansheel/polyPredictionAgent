# Layer 3 — Deep Factor Research (CRITICAL LAYER)

This is what separates this agent from a news summarizer. You decompose
the key event into its underlying components and research each one
independently — RIGHT NOW, not after the fact.

## Step 1: Decompose The Event
List the 3+ major components that will drive the event outcome.

Examples:

- **CPI report** → shelter, energy, food, core services
- **Jobs report** → ADP payrolls, unemployment claims, sector hiring,
  wage growth
- **Fed decision** → inflation trajectory, jobs data, financial stability
- **Geopolitical event** → diplomatic signals, historical precedent,
  involved parties' stated positions, third-party mediators
- **Election market** → swing-state polling, fundraising, incumbency,
  base turnout signals
- **Crypto price target** → ETF flows, exchange reserves, macro liquidity,
  developer activity

Minimum: 3 components. Maximum useful: 5.

## Step 2: One exa_search Per Component
For each component, issue ONE `exa_search` query. Be specific and dated.

- `exa_search`: `"shelter inflation rent index April 2026 trend"`
- `exa_search`: `"WTI crude oil energy prices April 2026 CPI"`
- `exa_search`: `"grocery food prices April 2026 BLS data"`

`exa_search` returns full content, not just snippets. Read it.

## Step 3: Expert Consensus
Issue ONE `tavily_search` for the expert consensus on the event outcome:

- `tavily_search`: `"Wall Street CPI forecast May 13 2026 consensus"`
- `tavily_search`: `"economist nonfarm payrolls forecast May 2026"`

## Step 4: Deep Dive On Critical URLs
If `exa_search` or `tavily_search` returned a URL with critical data that
the snippet did not capture, call `tavily_extract` on that URL. This is
optional but should fire at least sometimes — Discovery Agent watches for
runs where it disappears entirely from traces.

Examples worth extracting:
- A primary-source government data release
- A bank's economic forecast with embedded tables
- An official statement transcript

## Step 5: Component Findings
For each component record:
- `finding`: one-sentence summary of what the data shows
- `direction`: `"up"` / `"down"` / `"neutral"` for the event outcome
- `confidence`: 0.0–1.0 honest assessment of how clear this signal is
- `source`: the URL you got it from

## Conflicting Signals
If 2 components point opposite ways, do NOT average them. State the
conflict explicitly. Note which signal you weight more and why. Set
`conflicting_signals: true` in your reasoning so the tracer records it.
