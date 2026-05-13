# Layer 2 — Surface Impact & Key Event ID

Use `tavily_search` 1–2 times. Queries MUST be topic-specific — derived
from the market question and Layer 1 news headline. Never generic.

## Examples of good queries
- Fed rate market →
  `"fed rate cut July 2026 mortgage savings impact"`
- Recession market →
  `"US recession probability 2026 jobs layoffs"`
- Election market →
  `"{candidate} swing state polling June 2026"`
- Geopolitical market →
  `"{event} ceasefire negotiations latest"`
- Crypto market →
  `"bitcoin ETF inflows institutional demand 2026"`

## Synthesize
Answer in your reasoning:
- Who does this market affect?
- How concretely (mortgage rate, job security, retirement balance)?
- What is the impact severity: **high** / **medium** / **low**?

## Critical: Key Event Identification
Identify the SPECIFIC upcoming data event or announcement that will resolve
the market uncertainty. Name it precisely with a date:

- Good: `"April CPI Report — May 13 2026"`
- Bad: `"upcoming economic data"`
- Good: `"FOMC meeting decision — June 18 2026"`
- Bad: `"Fed announcement"`

This becomes the input to Layer 3. If you cannot name the key event with
a date, search again until you can.

## Impact Severity
- **high** — affects millions, large dollar amounts, near-term consequences
- **medium** — affects a sector or region, indirect consequences
- **low** — niche outcome, mostly market-internal
