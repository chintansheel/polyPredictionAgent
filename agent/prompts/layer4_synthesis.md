# Layer 4 — Sub-Prediction Synthesis

No new tool calls in Layer 4. Pure reasoning over what you found in
Layers 1–3.

## What You Must Produce

A concrete sub-prediction with:

### 1. A specific value
- A number with a unit (e.g. `"2.8%"`, `"175,000 jobs"`, `"$68 / barrel"`)
- OR a binary (e.g. `"YES"`, `"NO"`, `"raise rates"`, `"hold"`)

Never `"likely lower"`, never `"around 3%"`. Specific.

### 2. A range
Give a tight prediction interval (e.g. `"2.6% — 3.0%"`). Width must
reflect your uncertainty — a wide range is OK; pretending precision is not.

### 3. A confidence score
- `0.50` = coin flip — you genuinely cannot tell
- `0.55–0.60` = small lean
- `0.65` = lean
- `0.70–0.75` = solid lean
- `0.80` = strong evidence
- `0.85+` = use sparingly, requires consistent multi-component signal

If every run you ever do is in `0.65–0.70`, you are filling a field, not
expressing uncertainty. Discovery Agent watches for flat confidence.

### 4. Basis points
A bullet list, each tying ONE piece of evidence to ONE inference:
- `"Energy: WTI -8.2% in April → ~0.3pp downward pressure on CPI"`
- `"Shelter: Zillow rent index -0.3% → modest downward pressure"`
- `"Food: Flat → neutral"`
- `"Expert consensus 2.9% — our estimate slightly below"`

Always cite the source per basis point either inline or in components.

### 5. Invalidation condition
ONE specific observable thing that would break the prediction:
- `"If energy spiked after April 22 BLS data cutoff, this breaks.
  Check EIA data on May 12."`

Not: `"if economic conditions change"`. That is not falsifiable.

### 6. Market implication
Derive logically from your sub-prediction:
- `"If CPI prints 2.8%, Fed cut probability should move from 62% to
  ~78-82%. Current 62% underpriced assuming our CPI prediction is correct."`

### 7. Verdict
- `"overpriced"` — market probability is too high
- `"underpriced"` — market probability is too low
- `"fair"`        — current probability matches our analysis

With its own `verdict_confidence` (often lower than `sub_prediction.confidence`
because market re-pricing has its own uncertainty).
