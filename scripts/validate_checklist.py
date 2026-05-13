"""End-to-end shape validator (no network).

Builds a synthetic trace using the SAME tracer + helper code paths the
orchestrator uses, then runs every item from the spec's "Validation"
checklist against it. Catches any wiring regression without burning
Anthropic / Tavily / Exa credits.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from agent import selector as selector_module  # noqa: E402
from agent.orchestrator import (  # noqa: E402
    LAYER_NAMES,
    _annotate_llm_prompt_provenance,
    _emit_gamma_selection_span,
    _mirror_health_onto_root,
    estimate_cost_usd,
)
from agent.tracer import RunTracer  # noqa: E402


def _attrs_to_dict(attrs: list[dict]) -> dict:
    out: dict = {}
    for a in attrs:
        v = a.get("value") or {}
        if "stringValue" in v:
            out[a["key"]] = v["stringValue"]
        elif "intValue" in v:
            out[a["key"]] = v["intValue"]
        elif "doubleValue" in v:
            out[a["key"]] = v["doubleValue"]
        elif "boolValue" in v:
            out[a["key"]] = v["boolValue"]
    return out


def build_fake_trace() -> tuple[dict, Path]:
    tracer = RunTracer(run_id="checklist_test", traces_dir=REPO_ROOT / "traces")
    root = tracer.root_span()
    root.set("run.market_question", "Will the Fed cut rates in July?")

    # --- Layer 1 with gamma_api + llm + tavily_search ----------------------
    selected = selector_module.SelectedMarket(
        market={
            "id": "fed-july",
            "question": "Will the Fed cut rates in July?",
            "category": "economics",
            "probability_now": 0.55,
            "volume_24hr": 12345.0,
            "description": "Fed market description",
        },
        score=0.73,
        reason="high volume; uncertain probability",
        category="economics",
        retries=0,
        markets_fetched=100,
        gamma_fetch_ms=234,
        gamma_fetch_calls=1,
    )
    l1 = tracer.layer_span(LAYER_NAMES[1], root)
    l1.set("layer", 1)
    _emit_gamma_selection_span(tracer, parent=l1, selected=selected)

    llm1 = tracer.llm_span(l1)
    _annotate_llm_prompt_provenance(
        llm1,
        layer_num=1,
        system_prompt_file="subagent_layer1.md",
        layer_prompt_file="layer1_scoring.md",
        messages_count=1,
        max_tokens=1500,
        temperature=1.0,
    )
    llm1.set("llm.model", "claude-sonnet-4-6")
    llm1.set("llm.input_tokens", 1862)
    llm1.set("llm.output_tokens", 250)
    llm1.set("llm.stop_reason", "tool_use")
    llm1.set("llm.response_summary", "I'll verify Fed news with one tavily_search.")
    llm1.set("llm.context_window_used_tokens", 1862)
    llm1.set("llm.context_window_pct", round(1862 / 200000 * 100, 2))
    llm1.end()

    t1 = tracer.tool_span("tavily_search", l1)
    t1.set("tool.query", "fed rate cut july 2026")
    t1.set("tool.output_items", 5)
    t1.set("tool.output_preview", "Top result: Fed signals July cut likely")
    t1.set("tool.output_raw_chars", 4200)
    t1.set("tool.content_retrieved", True)
    t1.set("tool.success", True)
    t1.end()

    l1.set("markets_fetched", 100)
    l1.set("markets_scored", 100)
    l1.set("market_selected_id", "fed-july")
    l1.set("market_selected_question", "Will the Fed cut rates in July?")
    l1.set("selection_score", 0.73)
    l1.set("selection_reason", "high volume; uncertain probability")
    l1.set("retries", 0)
    l1.set("news_verified", True)
    l1.set("market.type", "fundamental")
    l1.set("market.framework_fit", "good")
    l1.set("probability_now", 0.55)
    l1.set("volume_24hr", 12345.0)
    l1.end()

    # --- Layer 2 -----------------------------------------------------------
    l2 = tracer.layer_span(LAYER_NAMES[2], root)
    l2.set("layer", 2)
    llm2 = tracer.llm_span(l2)
    _annotate_llm_prompt_provenance(
        llm2, layer_num=2,
        system_prompt_file="subagent_layer2.md",
        layer_prompt_file="layer2_impact.md",
        messages_count=1, max_tokens=1500,
    )
    llm2.set("llm.model", "claude-sonnet-4-6")
    llm2.set("llm.input_tokens", 2400)
    llm2.set("llm.output_tokens", 180)
    llm2.set("llm.stop_reason", "end_turn")
    llm2.set("llm.response_summary", "Surface impact: jobs report softer than expected.")
    llm2.end()
    l2.set("key_event_identified", "July FOMC meeting 2026-07-31")
    l2.set("impact_severity", "moderate")
    l2.set("market_topic_category", "economics")
    l2.set("sources_found", 3)
    l2.set("components_to_research", "shelter inflation,core goods CPI,labor market")
    l2.set("components_to_research_count", 3)
    l2.set("surface_impact_summary", "Softening jobs data raises odds of a July cut.")
    l2.end()

    # --- Layer 3 -----------------------------------------------------------
    l3 = tracer.layer_span(LAYER_NAMES[3], root)
    l3.set("layer", 3)
    for i in range(3):
        llm3 = tracer.llm_span(l3)
        _annotate_llm_prompt_provenance(
            llm3, layer_num=3,
            system_prompt_file="subagent_layer3.md",
            layer_prompt_file="layer3_factors.md",
            messages_count=i + 1, max_tokens=2500,
        )
        llm3.set("llm.model", "claude-sonnet-4-6")
        llm3.set("llm.input_tokens", 5000 + i * 2000)
        llm3.set("llm.output_tokens", 300)
        llm3.set("llm.stop_reason", "tool_use" if i < 2 else "end_turn")
        llm3.set(
            "llm.response_summary",
            f"Researching component {i+1} via exa_search.",
        )
        llm3.end()
        if i < 3:
            tx = tracer.tool_span("exa_search", l3)
            tx.set("tool.query", f"component {i+1}")
            tx.set("tool.output_items", 5)
            tx.set("tool.output_preview", f"Top result: source for component {i+1}")
            tx.set("tool.output_raw_chars", 6000)
            tx.set("tool.success", True)
            tx.end()
    # one tavily_extract — content retrieved
    tx = tracer.tool_span("tavily_extract", l3)
    tx.set("tool.query", "https://example.com/article")
    tx.set("tool.output_items", 1)
    tx.set("tool.output_preview", "Article content first 150 chars...")
    tx.set("tool.output_raw_chars", 7800)
    tx.set("tool.content_retrieved", True)
    tx.set("tool.success", True)
    tx.end()

    l3.set("components_researched", "shelter inflation,core goods CPI,labor market")
    l3.set("components_count", 3)
    l3.set("conflicting_signals_found", True)
    l3.set("total_sources", 4)
    l3.set("exa_queries_made", 3)
    l3.set("tavily_extracts_made", 1)
    l3.set("tavily_expert_queries_made", 1)
    l3.end()

    # --- Layer 4 -----------------------------------------------------------
    l4 = tracer.layer_span(LAYER_NAMES[4], root)
    l4.set("layer", 4)
    llm4 = tracer.llm_span(l4)
    _annotate_llm_prompt_provenance(
        llm4, layer_num=4,
        system_prompt_file="subagent_layer4.md",
        layer_prompt_file="layer4_synthesis.md",
        messages_count=1, max_tokens=3500,
    )
    llm4.set("llm.model", "claude-sonnet-4-6")
    llm4.set("llm.input_tokens", 3200)
    llm4.set("llm.output_tokens", 900)
    llm4.set("llm.stop_reason", "end_turn")
    llm4.set("llm.response_summary", "Synthesis complete. Verdict: fair (62%).")
    llm4.end()
    l4.end()

    # --- root finalisation -------------------------------------------------
    root.set("run.status", "ok")
    root.set("run.total_input_tokens", 14724)
    root.set("run.total_output_tokens", 1880)
    root.set("llm.prompt_tokens_total", 14724)
    root.set("llm.completion_tokens_total", 1880)
    root.set("llm.estimated_cost_usd", estimate_cost_usd(14724, 1880))
    root.set("run.max_context_window_tokens", 9000)
    root.set("run.context_growth_ratio", 4.83)
    root.set("run.rate_limit_events", 0)
    root.set("run.rate_limit_wait_seconds_total", 0.0)
    root.set("run.rate_limit_pct_of_runtime", 0.0)
    fake_health = {
        "checks": {
            "layer3_exa_depth": True,
            "tool_variety": True,
            "layer2_query_relevant": True,
            "confidence_not_flat": True,
            "tavily_extract_used": True,
            "no_empty_extracts": True,
            "retry_logic_fired_recent": False,
        },
        "summary": {"pass": 6, "fail": 1},
    }
    _mirror_health_onto_root(root, fake_health)
    root.end()

    path = Path(tracer.write())
    payload = json.loads(path.read_text())
    return payload, path


def _spans(payload: dict) -> list[dict]:
    return payload["resourceSpans"][0]["scopeSpans"][0]["spans"]


def main() -> int:
    payload, path = build_fake_trace()
    spans = _spans(payload)
    by_name = {s["name"]: s for s in spans}

    root = by_name["agent.run"]
    l1 = by_name["layer_1.market_selection"]
    l2 = by_name["layer_2.surface_research"]
    l3 = by_name["layer_3.deep_factor_research"]
    root_attrs = _attrs_to_dict(root["attributes"])
    l1_attrs = _attrs_to_dict(l1["attributes"])
    l2_attrs = _attrs_to_dict(l2["attributes"])
    l3_attrs = _attrs_to_dict(l3["attributes"])

    llm_calls = [s for s in spans if s["name"] == "llm.call"]
    tool_spans = [s for s in spans if s["name"].startswith("tool.")]
    extract_spans = [s for s in spans if s["name"] == "tool.tavily_extract"]
    gamma_spans = [s for s in spans if s["name"] == "tool.gamma_api"]

    def all_have(attrname: str, spans_subset: list[dict]) -> bool:
        return all(attrname in _attrs_to_dict(s["attributes"]) for s in spans_subset)

    checks: list[tuple[str, bool]] = [
        ("Root span has no `parentSpanId` field", "parentSpanId" not in root),
        ("`tool.gamma_api` span present under layer_1", any(
            s["parentSpanId"] == l1["spanId"] for s in gamma_spans
        )),
        ("Every `llm.call` span has `llm.system_prompt_hash`",
         all_have("llm.system_prompt_hash", llm_calls)),
        ("Every `llm.call` span has `llm.layer_prompt_file`",
         all_have("llm.layer_prompt_file", llm_calls)),
        ("Every `llm.call` span has `llm.response_summary`",
         all_have("llm.response_summary", llm_calls)),
        ("`layer_1` span has `markets_fetched`",
         "markets_fetched" in l1_attrs),
        ("`layer_1` span has `selection_score`",
         "selection_score" in l1_attrs),
        ("`layer_1` span has `retries`",
         "retries" in l1_attrs),
        ("`layer_2` span has `key_event_identified`",
         "key_event_identified" in l2_attrs),
        ("`layer_2` span has `impact_severity`",
         "impact_severity" in l2_attrs),
        ("`layer_3` span has `components_researched`",
         "components_researched" in l3_attrs),
        ("`layer_3` span has `conflicting_signals_found`",
         "conflicting_signals_found" in l3_attrs),
        ("Every tool span has `tool.output_preview`",
         all_have("tool.output_preview", tool_spans)),
        ("Every tool span has `tool.content_retrieved` OR is a search",
         all(
             "tool.content_retrieved" in _attrs_to_dict(s["attributes"])
             or s["name"] in ("tool.tavily_search", "tool.exa_search", "tool.gamma_api")
             for s in tool_spans
         )),
        ("Root span has `run.rate_limit_wait_seconds_total`",
         "run.rate_limit_wait_seconds_total" in root_attrs),
        ("Root span has `llm.estimated_cost_usd` as a double",
         isinstance(root_attrs.get("llm.estimated_cost_usd"), float)),
        ("Root span has `health.overall`",
         "health.overall" in root_attrs),
        ("All span durations have `duration_ms`",
         all_have("duration_ms", spans)),
    ]

    # Bonus: tavily_extract success flag matches content_retrieved
    for s in extract_spans:
        a = _attrs_to_dict(s["attributes"])
        ok = a.get("tool.success") == a.get("tool.content_retrieved")
        checks.append((
            f"tavily_extract span success matches content_retrieved (extract: {a.get('tool.success')})",
            ok,
        ))

    failed = 0
    for name, ok in checks:
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}")
        if not ok:
            failed += 1

    total = len(checks)
    print(f"\n{total - failed}/{total} validation-checklist items pass")

    # cleanup
    path.unlink(missing_ok=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
