"""tracing.span() behavior tests.

Must behave the same whether opentelemetry-sdk is installed or not — that's the whole
point of it being an optional extra (module docstring) — so none of these special-case
`tracing.TRACING_AVAILABLE`; they hold either way.
"""

from __future__ import annotations

import pytest

from labgo import tracing


def test_span_is_usable_as_a_context_manager() -> None:
    with tracing.span("test.span", foo="bar") as s:
        assert s is None or hasattr(s, "set_attribute")


def test_span_yields_something_attribute_settable_or_none() -> None:
    with tracing.span("test.span") as s:
        if s is not None:
            s.set_attribute("checked", 1)


def test_exceptions_inside_a_span_propagate() -> None:
    """A traced block must not swallow the caller's exception."""
    with pytest.raises(ValueError, match="boom"), tracing.span("test.span"):
        raise ValueError("boom")


def test_span_accepts_no_attributes() -> None:
    with tracing.span("test.span"):
        pass
