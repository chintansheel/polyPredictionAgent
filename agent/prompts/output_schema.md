# Output Card Schema

Your final response MUST be a single JSON object exactly matching this
schema. No markdown fences. No prose. The orchestrator passes your final
message text directly to `json.loads`. Any deviation fails validation
and the run is written as `status: "failed"`.

## Schema (Fresh Mode)

```json
{
  "market": {
    "selection_reason": "string — why this market was interesting",
    "news_headline": "string — headline you verified",
    "news_source": "string — outlet name (Reuters, Bloomberg, etc.)",
    "news_url": "string — URL of the verifying article",
    "news_verified": true,
    "plain_english": "string — 2-3 sentences explaining the market to a layperson with concrete impact",
    "key_event": "string — precise name + date, e.g. 'April CPI Report — May 13 2026'"
  },
  "layer3": {
    "components_researched": {
      "<component_name>": {
        "finding": "string — 1-sentence data summary",
        "direction": "up" ,
        "confidence": 0.8,
        "source": "string — URL"
      }
    },
    "expert_consensus": {
      "median_forecast": "string",
      "range": "string",
      "source": "string"
    },
    "conflicting_signals": false
  },
  "sub_prediction": {
    "predicted_value": "string — '2.8%' or 'YES' or '175k jobs'",
    "predicted_range": "string — '2.6% — 3.0%'",
    "confidence": 0.66,
    "basis": [
      "string — one piece of evidence + its inference",
      "string — ..."
    ],
    "invalidation_condition": "string — specific observable that would break this",
    "market_implication": "string — what the prediction means for market probability"
  },
  "verdict": {
    "call": "underpriced",
    "confidence": 0.64,
    "reasoning": "string — base rate or analytic argument supporting verdict",
    "watch_up": "string — what would push probability higher",
    "watch_down": "string — what would push probability lower"
  }
}
```

## Schema (Reanalysis Mode — ADD these fields, keep base fields)

```json
{
  "market": { "...same as fresh..." },
  "layer3": { "...same as fresh..." },
  "changes_since_prior": {
    "probability_delta": "string — e.g. '+0.08'",
    "new_developments": ["string", "string"],
    "prior_prediction_event_resolved": false,
    "prior_prediction_correct": null,
    "components_changed": ["energy"],
    "components_unchanged": ["shelter", "food"]
  },
  "sub_prediction": {
    "...same fields as fresh...",
    "change_from_prior": "string — e.g. 'revised down from 2.8% — energy fell more'"
  },
  "verdict": {
    "...same fields as fresh...",
    "change_from_prior": "string — e.g. 'confidence increased — more evidence'"
  }
}
```

## Field Notes

- `direction` allowed values: `"up"`, `"down"`, `"neutral"`
- `verdict.call` allowed values: `"underpriced"`, `"overpriced"`, `"fair"`
- All `confidence` values are floats `0.0–1.0`
- `basis` array must have at least 3 entries (one per component minimum)
- `components_researched` must have at least 3 keys
- `news_verified: false` is allowed if Layer 1 search returned no named source

## Reminder
Respond with ONLY the JSON. No prose around it. No `\`\`\`json` fences.
The orchestrator does `json.loads(final_message_text)` directly.
