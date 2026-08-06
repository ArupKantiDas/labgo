"""Stage 6: lightweight tracing for the LLM/tool calls in `agent.py` and `mcp_server.py`.

Console-exported by default — no external service required, the same cost-consciousness
call as D011/D013 (a Langfuse account is one more API key and one more thing that can be
unavailable overnight; a `SimpleSpanProcessor(ConsoleSpanExporter())` needs neither). Spans
are still real OpenTelemetry spans, so pointing `_provider.add_span_processor()` at an OTLP
exporter later is a small, additive change, not a rewrite.

**Genuinely optional.** If `opentelemetry-sdk` isn't installed (the `observability` extra),
`span()` is a no-op context manager and every caller runs identically, just untraced — the
same lazy-optional-dependency shape as `voyageai` (D014) and `mcp` (Stage 5).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

AttributeValue = str | bool | int | float

try:
    from opentelemetry import trace as _otel_trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

    _provider = TracerProvider(resource=Resource.create({"service.name": "labgo"}))
    # Simple, not Batch: labgo's commands are short-lived CLI processes, not a long-running
    # server — a batched processor could still be buffering when the process exits.
    _provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    _otel_trace.set_tracer_provider(_provider)
    _tracer = _otel_trace.get_tracer("labgo")
    TRACING_AVAILABLE = True
except ImportError:
    _tracer = None
    TRACING_AVAILABLE = False


@contextmanager
def span(name: str, **attributes: AttributeValue) -> Iterator[object | None]:
    """A traced block with `attributes` set up front and a `latency_ms` set on exit.

    Yields the underlying OTel span (for `set_attribute` after the fact, e.g. once a
    result's token count is known) or `None` when tracing isn't installed — callers must
    tolerate `None` (`if s: s.set_attribute(...)`), matching every other optional-extra
    guard in this codebase rather than raising.
    """
    if not TRACING_AVAILABLE:
        yield None
        return
    start = time.monotonic()
    with _tracer.start_as_current_span(name) as s:
        for key, value in attributes.items():
            s.set_attribute(key, value)
        try:
            yield s
        finally:
            s.set_attribute("latency_ms", round((time.monotonic() - start) * 1000, 1))
