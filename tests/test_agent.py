"""The pure-Python slice of agent.py: output parsing and routing.

No live Anthropic or Neo4j call — the tool-calling loop itself is exercised against Aura
directly (same testing philosophy as baseline.predict_impact / hybrid.predict_impact_vector).
"""

from __future__ import annotations

import json

from langgraph.graph import END

from labgo.agent import _dispatch_tool, _route_after_agent, parse_impacted_files


def test_parses_recognized_files_and_drops_unrecognized() -> None:
    report = "IMPACTED_FILES: a.py, b.py, made_up.py\nTESTS_TO_RUN: NONE\n"
    recognized, unrecognized = parse_impacted_files(report, known_files={"a.py", "b.py"})
    assert recognized == {"a.py", "b.py"}
    assert unrecognized == ["made_up.py"]


def test_none_impacted_files_is_empty_not_a_string() -> None:
    report = "IMPACTED_FILES: NONE\n"
    recognized, unrecognized = parse_impacted_files(report, known_files={"a.py"})
    assert recognized == set()
    assert unrecognized == []


def test_missing_impacted_files_line_is_empty() -> None:
    """A malformed response shouldn't crash scoring — it just scores an empty prediction."""
    recognized, unrecognized = parse_impacted_files(
        "I didn't follow the format.", known_files=set()
    )
    assert recognized == set()
    assert unrecognized == []


def test_whitespace_around_file_names_is_stripped() -> None:
    report = "IMPACTED_FILES:  a.py ,  b.py\n"
    recognized, _ = parse_impacted_files(report, known_files={"a.py", "b.py"})
    assert recognized == {"a.py", "b.py"}


def test_route_continues_to_tools_when_under_the_turn_cap() -> None:
    state = {"stop_reason": "tool_use", "turns": 1, "max_turns": 6}
    assert _route_after_agent(state) == "tools"


def test_route_forces_finalize_at_the_turn_cap() -> None:
    """The escape hatch that guarantees the loop terminates (module docstring)."""
    state = {"stop_reason": "tool_use", "turns": 6, "max_turns": 6}
    assert _route_after_agent(state) == "finalize"


def test_route_ends_once_the_model_stops_calling_tools() -> None:
    state = {"stop_reason": "end_turn", "turns": 2, "max_turns": 6}
    assert _route_after_agent(state) == END


def test_dispatch_unknown_tool_returns_an_error_payload_not_a_crash() -> None:
    """A hallucinated tool name from the model shouldn't take the whole run down."""
    payload = json.loads(_dispatch_tool(driver=None, repo=None, name="not_a_real_tool", args={}))
    assert "error" in payload
