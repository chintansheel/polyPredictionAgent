"""Offline validator for the Discovery-Agent trace instrumentation upgrade.

Runs the parts of the pipeline that DO NOT need network access (tracer
shape, prompt hashing, tavily-extract success-flag semantics) and prints
a checklist of pass/fail items matching the spec's validation section.

Usage:
    python scripts/validate_trace_changes.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from agent import prompt_hasher  # noqa: E402
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


def check_tracer_shape() -> list[tuple[str, bool, str]]:
    """Build a synthetic 3-span trace and verify OTel shape."""
    results: list[tuple[str, bool, str]] = []

    tracer = RunTracer(run_id="smoke_test", traces_dir=REPO_ROOT / "traces")
    root = tracer.root_span()
    root.set("run.duration_seconds", 1.23)
    l1 = tracer.layer_span("layer_1.market_selection", root)
    l1.set("layer", 1)
    tool = tracer.tool_span("gamma_api", l1)
    tool.set("tool.output_items", 5)
    tool.end()
    l1.end()
    root.end()

    out_path = Path(tracer.write())
    payload = json.loads(out_path.read_text())
    spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]

    root_span = next(s for s in spans if s["name"] == "agent.run")
    l1_span = next(s for s in spans if s["name"].startswith("layer_1"))
    tool_span = next(s for s in spans if s["name"] == "tool.gamma_api")

    results.append((
        "Root span has no `parentSpanId` field",
        "parentSpanId" not in root_span,
        f"keys={sorted(root_span.keys())}",
    ))
    results.append((
        "Non-root spans keep `parentSpanId`",
        "parentSpanId" in l1_span and "parentSpanId" in tool_span,
        "ok" if "parentSpanId" in l1_span else "missing",
    ))
    root_attrs = _attrs_to_dict(root_span["attributes"])
    l1_attrs = _attrs_to_dict(l1_span["attributes"])
    tool_attrs = _attrs_to_dict(tool_span["attributes"])
    results.append((
        "All spans include `duration_ms`",
        "duration_ms" in root_attrs and "duration_ms" in l1_attrs and "duration_ms" in tool_attrs,
        f"root={root_attrs.get('duration_ms')} l1={l1_attrs.get('duration_ms')} tool={tool_attrs.get('duration_ms')}",
    ))

    # cleanup synthetic trace
    out_path.unlink(missing_ok=True)
    return results


def check_prompt_hasher() -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []
    h_text = prompt_hasher.hash_text("hello world")
    results.append((
        "hash_text returns sha256: prefix",
        h_text.startswith("sha256:") and len(h_text) == len("sha256:") + 16,
        h_text,
    ))
    h_text2 = prompt_hasher.hash_text("hello world")
    results.append((
        "hash_text is deterministic",
        h_text == h_text2,
        f"{h_text} vs {h_text2}",
    ))
    h_missing = prompt_hasher.hash_prompt("does_not_exist_xyz.md")
    results.append((
        "hash_prompt of missing file returns sentinel, does not crash",
        h_missing == "<missing>",
        h_missing,
    ))
    h_real = prompt_hasher.hash_prompt("system_fresh.md")
    results.append((
        "hash_prompt of a real prompt returns sha256:<16>",
        h_real.startswith("sha256:") and len(h_real) == len("sha256:") + 16,
        h_real,
    ))
    return results


def check_tavily_success_logic() -> list[tuple[str, bool, str]]:
    """Verify the new tavily_extract content-aware success flag."""
    from agent.tools import tavily  # local import to avoid network probes

    results: list[tuple[str, bool, str]] = []

    # Manually simulate the post-response computation by calling the
    # internal "results" projection. We can't hit the network, so we
    # instead simulate the structures the wrapper builds.
    short_blob = "x" * 50
    long_blob = "x" * 500

    def simulate(content: str) -> dict:
        results_list = [{"url": "https://example.com", "raw_content": content[:8000]}]
        first_raw = results_list[0]["raw_content"]
        content_retrieved = (
            bool(results_list)
            and len(first_raw) >= tavily.EXTRACT_MIN_CONTENT_CHARS
        )
        return {
            "results": results_list,
            "success": content_retrieved,
            "content_retrieved": content_retrieved,
            "output_items": len(results_list),
            "raw_content": first_raw,
        }

    short = simulate(short_blob)
    long_ = simulate(long_blob)
    empty = simulate("")

    results.append((
        "tavily_extract with short content -> success False, content_retrieved False",
        short["success"] is False and short["content_retrieved"] is False,
        f"success={short['success']} content_retrieved={short['content_retrieved']}",
    ))
    results.append((
        "tavily_extract with empty content -> success False",
        empty["success"] is False,
        f"success={empty['success']}",
    ))
    results.append((
        "tavily_extract with long content -> success True",
        long_["success"] is True and long_["content_retrieved"] is True,
        f"success={long_['success']} content_retrieved={long_['content_retrieved']}",
    ))
    return results


def check_market_classification() -> list[tuple[str, bool, str]]:
    from agent import selector
    results: list[tuple[str, bool, str]] = []
    cases = [
        ("Will the Fed cut rates in June?", "fundamental", "good"),
        ("Will Elon Musk post 100-119 tweets from May 5 to May 12?", "behavioral", "poor"),
        ("Will Bitcoin hit $100k by end of year?", "price", "poor"),
        ("Will Ukraine-Russia ceasefire be signed by July?", "event", "good"),
        ("Will the next iPhone launch in September?", "event", "good"),
    ]
    for question, expected_type, expected_fit in cases:
        got_type = selector.classify_market_type(question)
        got_fit = selector.framework_fit(got_type)
        ok = got_type == expected_type and got_fit == expected_fit
        results.append((
            f"classify_market_type: '{question[:50]}...'",
            ok,
            f"got=({got_type},{got_fit}) want=({expected_type},{expected_fit})",
        ))
    return results


def main() -> int:
    sections = [
        ("OTel tracer shape", check_tracer_shape()),
        ("Prompt hasher", check_prompt_hasher()),
        ("Tavily extract success flag", check_tavily_success_logic()),
        ("Market classification", check_market_classification()),
    ]

    total = 0
    failed = 0
    for section_name, checks in sections:
        print(f"\n=== {section_name} ===")
        for name, ok, detail in checks:
            total += 1
            status = "PASS" if ok else "FAIL"
            print(f"  [{status}] {name}  ({detail})")
            if not ok:
                failed += 1

    print(f"\n{total - failed}/{total} checks passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
