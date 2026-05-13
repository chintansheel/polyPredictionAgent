"""OpenTelemetry-format JSON trace writer.

Every tool call and every LLM call must go through this tracer. Spans are
opened before the call and closed after. The full trace is flushed to a
single JSON file per run, matching the OTel export schema so Discovery
Agent can ingest it with no custom adapter.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


def _attr(key: str, value: Any) -> dict:
    """Convert a (key, value) into an OTel attribute object.

    OTel attribute values are typed. Booleans must be checked BEFORE int
    because `isinstance(True, int)` is True in Python.
    """
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": value}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    if value is None:
        return {"key": key, "value": {"stringValue": ""}}
    return {"key": key, "value": {"stringValue": str(value)}}


@dataclass
class Span:
    name: str
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    parent_span_id: Optional[str] = None
    start_time_ns: int = field(default_factory=lambda: time.time_ns())
    end_time_ns: Optional[int] = None
    attributes: dict = field(default_factory=dict)
    status_code: int = 1  # 1 = OK, 2 = ERROR
    kind: int = 1         # 1 = INTERNAL, 3 = CLIENT

    def end(self, status_code: int = 1) -> None:
        self.end_time_ns = time.time_ns()
        self.status_code = status_code
        # Auto-add an explicit `duration_ms` attribute so layer- and tool-level
        # durations are directly queryable (no need for the consumer to subtract
        # `endTimeUnixNano - startTimeUnixNano`).
        self.attributes["duration_ms"] = int(
            (self.end_time_ns - self.start_time_ns) / 1_000_000
        )

    def set(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def elapsed_ms(self) -> int:
        end = self.end_time_ns if self.end_time_ns is not None else time.time_ns()
        return int((end - self.start_time_ns) / 1_000_000)

    def to_otel(self, trace_id: str) -> dict:
        end_ns = self.end_time_ns if self.end_time_ns is not None else time.time_ns()
        # OTel JSON spec: `parentSpanId` MUST be absent on root spans. Setting
        # it to `null` causes strict collectors to reject the trace, so we only
        # include the field when there is an actual parent.
        span: dict = {
            "traceId": trace_id,
            "spanId": self.span_id,
            "name": self.name,
            "kind": self.kind,
            "startTimeUnixNano": str(self.start_time_ns),
            "endTimeUnixNano": str(end_ns),
            "status": {"code": self.status_code},
            "attributes": [_attr(k, v) for k, v in self.attributes.items()],
        }
        if self.parent_span_id is not None:
            span["parentSpanId"] = self.parent_span_id
        return span


class RunTracer:
    """Owns all spans for a single run and writes one OTel JSON file."""

    def __init__(self, run_id: str, traces_dir: str | Path = "traces") -> None:
        self.run_id = run_id
        self.trace_id = (uuid.uuid4().hex + uuid.uuid4().hex)[:32]
        self.spans: list[Span] = []
        self.traces_dir = Path(traces_dir)
        self.traces_dir.mkdir(parents=True, exist_ok=True)

    def root_span(self) -> Span:
        span = Span(name="agent.run", kind=1)
        span.set("run.id", self.run_id)
        self.spans.append(span)
        return span

    def layer_span(self, name: str, parent: Span) -> Span:
        span = Span(name=name, parent_span_id=parent.span_id, kind=1)
        self.spans.append(span)
        return span

    def tool_span(self, tool_name: str, parent: Span) -> Span:
        span = Span(
            name=f"tool.{tool_name}",
            parent_span_id=parent.span_id,
            kind=3,
        )
        span.set("tool.name", tool_name)
        self.spans.append(span)
        return span

    def llm_span(self, parent: Span) -> Span:
        span = Span(name="llm.call", parent_span_id=parent.span_id, kind=3)
        self.spans.append(span)
        return span

    def write(self) -> str:
        trace = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            _attr("service.name", "polymarket-agent"),
                            _attr("service.version", "1.0.0"),
                            _attr("agent.run_id", self.run_id),
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "polymarket_agent.orchestrator"},
                            "spans": [s.to_otel(self.trace_id) for s in self.spans],
                        }
                    ],
                }
            ]
        }
        path = self.traces_dir / f"trace_{self.run_id}.json"
        path.write_text(json.dumps(trace, indent=2))
        return str(path)
