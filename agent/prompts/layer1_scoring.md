# Layer 1 — Market Scoring & News Verification

The selector has already scored markets using this rubric:

```
score = 0.40 * normalized_24hr_price_delta
      + 0.30 * normalized_volume_spike
      + 0.20 * urgency_factor          # 1 / max(days_to_close, 1)
      + 0.10 * normalized_liquidity
```

Top-scored market that is not in cooldown is provided to you.

## Your Layer 1 Task

Call `tavily_search` ONCE with a query like:
> `{market_question} news today` OR `{market_topic} latest news`

Look for a named-source headline (Reuters, Bloomberg, AP, WSJ, FT, CNBC,
official agency, etc.) that plausibly explains the recent price move.

**News verification threshold:** must find a named source headline, not
just a vague price commentary. A pump-and-dump tweet does not count.

If verified → set `news_verified: true` in your reasoning and proceed.
If not verified → set `news_verified: false`, note the gap, and proceed
to Layer 2 anyway. The orchestrator records this in the trace; Discovery
Agent will see it.
