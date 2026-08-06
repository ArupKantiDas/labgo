"""Stage 4: a LangGraph tool-calling agent.

Answers "if I change X, what breaks, which tests must run, and who should review it?" —
the question the whole README is scoped around, and the first stage where an LLM decides
anything.

**The routing decision is the deliverable (D001).** Every earlier stage measured one
signal in isolation (calls, co-change, vectors) precisely so this stage's job could be
checked, not assumed: does an agent that can *choose* among them, plus two new tools this
stage adds (`test_coverage`, `likely_reviewer` — the "which tests" and "who reviews"
halves of the question nothing before Stage 4 answered), beat the deterministic 43.3%
recall / 15.4% precision hybrid+cochange baseline (D015)? If it doesn't, the extra
complexity and per-token cost didn't earn its place.

**LangChain used thinly, on purpose (README's Stack table).** The graph/state machinery
below is `langgraph.graph.StateGraph`; the actual model call is the raw `anthropic` SDK,
not `langchain_anthropic`'s `ChatAnthropic` wrapper or `create_react_agent` — a prebuilt
agent loop would hide exactly the routing behaviour this stage exists to inspect. State
carries Anthropic's own message/tool-use JSON shape unchanged, not a `langchain_core`
message type, so nothing is translated between the graph and the API twice.

**Grounding, not free-form paths.** The system prompt lists every file id in the corpus
up front (small enough at httpx's ~60 files) — otherwise a model that has to invent path
strings from memory would be scored on spelling, not reasoning. `parse_impacted_files()`
drops anything the model names that isn't a real file id rather than guessing what was
meant; `AgentResult.unrecognized_mentions` keeps that failure visible instead of silent.
This doesn't scale to a large corpus (a `list_files`/`search_files` tool would be needed)
— not built, same "don't build it until it's the binding constraint" call as D004/D015.
"""

from __future__ import annotations

import json
import operator
import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, TypedDict

import anthropic
from langgraph.graph import END, StateGraph

from labgo import tracing
from labgo.baseline import predict_impact_calls

if TYPE_CHECKING:
    from neo4j import Driver

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
MAX_TURNS = 6  # an unbounded tool-calling loop is a real failure mode, not a hypothetical one

TOOLS = [
    {
        "name": "call_graph_traverse",
        "description": (
            "Files reachable via the static CALLS graph within `hops` of a seed file. "
            "Structural — mined from source, not git history. The most reliable signal, "
            "but only 27.2% of calls resolve statically on httpx (D004), so it misses "
            "dynamic-dispatch coupling."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "File id, e.g. 'httpx/_client.py'"},
                "hops": {"type": "integer", "description": "Traversal depth, 1-3", "default": 2},
            },
            "required": ["file"],
        },
    },
    {
        "name": "co_change_neighbors",
        "description": (
            "Files that historically changed together with this file in git history. "
            "Catches real coupling the call graph misses (renames, config, dynamic "
            "dispatch) but is noisy — raise min_count for a stronger, cleaner signal "
            "(D010 found min_count~25 matches this project's call-graph budget)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file": {"type": "string"},
                "min_count": {"type": "integer", "default": 2},
            },
            "required": ["file"],
        },
    },
    {
        "name": "semantic_search",
        "description": (
            "Search embedded function/class source by meaning. Finds code that "
            "RESEMBLES the query, not code that DEPENDS on it (WHY.md's distinction) — "
            "useful for finding conceptually related code the graph signals miss, not "
            "for confirming a dependency exists."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "k": {"type": "integer", "default": 8},
            },
            "required": ["query"],
        },
    },
    {
        "name": "test_coverage",
        "description": "Test functions that exercise functions defined in this file.",
        "input_schema": {
            "type": "object",
            "properties": {"file": {"type": "string"}},
            "required": ["file"],
        },
    },
    {
        "name": "likely_reviewer",
        "description": (
            "The most frequent historical committers to this file — a simple heuristic "
            "for who should review a change to it. Not code-aware, just git log."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"file": {"type": "string"}},
            "required": ["file"],
        },
    },
]

SYSTEM_PROMPT_TEMPLATE = """\
You are LabGo's change-impact analyst. Given one file that is about to change, decide \
which of the available tools to call — and how many times — to answer: what else would \
break, which tests should run, and who should review the change.

Files in this corpus (use these exact ids, nothing else):
{file_list}

Guidance on the tools, from what this project has already measured (cite the numbers if \
relevant to your reasoning):
- call_graph_traverse is structural and precise but incomplete (D004).
- co_change_neighbors at a low min_count predicts most of the corpus (D012) — use a \
  higher min_count (~20-30) if you want a clean signal, a low one only to cast a wide net.
- semantic_search finds resemblance, not dependency (WHY.md) — a supporting signal, not \
  a substitute for the other two.
- Call as many tools, in whatever order, as you need — but stop once more calls stop \
  changing your answer. You have at most {max_turns} turns total.

When you are done gathering evidence, respond with NO further tool calls, using exactly \
this format (plain text, not JSON):

IMPACTED_FILES: comma-separated file ids from the list above, or NONE
TESTS_TO_RUN: comma-separated test ids, or NONE
LIKELY_REVIEWER: a name, or UNKNOWN
SUMMARY: 2-3 sentences on what you found and which tools you trusted most, and why.
"""


def tool_call_graph(driver: Driver, args: dict[str, Any]) -> str:
    """JSON tool payload for `call_graph_traverse` — see the `TOOLS` schema above."""
    hops = int(args.get("hops", 2))
    files = sorted(predict_impact_calls(driver, args["file"], hops=hops))
    return json.dumps({"files": files})


def tool_co_change(driver: Driver, args: dict[str, Any]) -> str:
    """JSON tool payload for `co_change_neighbors` — see the `TOOLS` schema above."""
    min_count = int(args.get("min_count", 2))
    with driver.session() as session:
        rows = session.run(
            "MATCH (:File {id: $file})-[r:CO_CHANGED]-(other:File) "
            "WHERE r.count >= $min_count AND other.id <> $file "
            "RETURN DISTINCT other.id AS file, r.count AS count ORDER BY r.count DESC",
            file=args["file"],
            min_count=min_count,
        )
        pairs = [(r["file"], r["count"]) for r in rows]
    return json.dumps({"files": [f for f, _ in pairs], "counts": dict(pairs)})


def tool_semantic_search(driver: Driver, args: dict[str, Any]) -> str:
    """JSON tool payload for `semantic_search` — see the `TOOLS` schema above."""
    from labgo.embed import semantic_search  # noqa: PLC0415  (lazy: keeps voyageai optional, D014)

    k = int(args.get("k", 8))
    hits = semantic_search(driver, args["query"], k=k)
    by_file: dict[str, float] = {}
    for h in hits:
        by_file[h["path"]] = max(by_file.get(h["path"], 0.0), h["score"])
    ranked = sorted(by_file.items(), key=lambda kv: -kv[1])
    return json.dumps({"files": [f for f, _ in ranked], "scores": dict(ranked)})


def tool_test_coverage(driver: Driver, args: dict[str, Any]) -> str:
    """JSON tool payload for `test_coverage` — see the `TOOLS` schema above."""
    with driver.session() as session:
        rows = session.run(
            "MATCH (:File {id: $file})-[:CONTAINS]->(target:Function) "
            "MATCH (test:Function)-[:TESTS]->(target) "
            "OPTIONAL MATCH (testFile:File)-[:CONTAINS]->(test) "
            "RETURN DISTINCT test.id AS test_id, testFile.id AS test_file",
            file=args["file"],
        )
        tests = [dict(r) for r in rows]
    return json.dumps({"tests": tests})


def tool_likely_reviewer(repo: Path, args: dict[str, Any]) -> str:
    """JSON tool payload for `likely_reviewer` — see the `TOOLS` schema above."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "log", "--no-merges", "--format=%an", "--", args["file"]],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return json.dumps({"top_committers": [], "note": "no commit history found"})
    counts = Counter(proc.stdout.strip().splitlines())
    return json.dumps(
        {"top_committers": [{"name": n, "commits": c} for n, c in counts.most_common(3)]}
    )


def _dispatch_tool(driver: Driver, repo: Path, name: str, args: dict[str, Any]) -> str:
    if name == "call_graph_traverse":
        return tool_call_graph(driver, args)
    if name == "co_change_neighbors":
        return tool_co_change(driver, args)
    if name == "semantic_search":
        return tool_semantic_search(driver, args)
    if name == "test_coverage":
        return tool_test_coverage(driver, args)
    if name == "likely_reviewer":
        return tool_likely_reviewer(repo, args)
    return json.dumps({"error": f"unknown tool {name!r}"})


class AgentState(TypedDict):
    """Anthropic's own message/tool-use JSON, unchanged — see module docstring."""

    messages: Annotated[list[dict[str, Any]], operator.add]
    turns: int
    max_turns: int
    stop_reason: str | None
    input_tokens: int
    output_tokens: int
    tool_calls: list[str]


IMPACTED_RE = re.compile(r"IMPACTED_FILES:\s*(.+)")
TESTS_RE = re.compile(r"TESTS_TO_RUN:\s*(.+)")
REVIEWER_RE = re.compile(r"LIKELY_REVIEWER:\s*(.+)")


def _final_text(state: AgentState) -> str:
    last = state["messages"][-1]
    parts = [b["text"] for b in last["content"] if b.get("type") == "text"]
    return "\n".join(parts)


def _agent_node(state: AgentState, *, client: anthropic.Anthropic, model: str, system: str) -> dict:
    with tracing.span("agent.llm_call", model=model, turn=state["turns"]) as s:
        resp = client.messages.create(
            model=model, max_tokens=1024, system=system, tools=TOOLS, messages=state["messages"]
        )
        if s:
            s.set_attribute("input_tokens", resp.usage.input_tokens)
            s.set_attribute("output_tokens", resp.usage.output_tokens)
            s.set_attribute("stop_reason", resp.stop_reason)
    msg = {"role": "assistant", "content": [b.model_dump() for b in resp.content]}
    return {
        "messages": [msg],
        "turns": state["turns"] + 1,
        "stop_reason": resp.stop_reason,
        "input_tokens": state["input_tokens"] + resp.usage.input_tokens,
        "output_tokens": state["output_tokens"] + resp.usage.output_tokens,
    }


def _finalize_node(
    state: AgentState, *, client: anthropic.Anthropic, model: str, system: str
) -> dict:
    """Max turns reached: one last call with no tools, forcing a text-only answer."""
    nudge = {
        "role": "user",
        "content": "No more tool calls are available. Respond now in the required format.",
    }
    messages = [*state["messages"], nudge]
    with tracing.span("agent.llm_call", model=model, turn=state["turns"], forced=True) as s:
        resp = client.messages.create(
            model=model, max_tokens=1024, system=system, messages=messages
        )
        if s:
            s.set_attribute("input_tokens", resp.usage.input_tokens)
            s.set_attribute("output_tokens", resp.usage.output_tokens)
    msg = {"role": "assistant", "content": [b.model_dump() for b in resp.content]}
    return {
        "messages": [nudge, msg],
        "stop_reason": "end_turn",
        "input_tokens": state["input_tokens"] + resp.usage.input_tokens,
        "output_tokens": state["output_tokens"] + resp.usage.output_tokens,
    }


def _tool_node(state: AgentState, *, driver: Driver, repo: Path) -> dict:
    last = state["messages"][-1]
    tool_uses = [b for b in last["content"] if b.get("type") == "tool_use"]
    results = []
    called = []
    for tu in tool_uses:
        called.append(tu["name"])
        with tracing.span("agent.tool_call", tool=tu["name"]):
            try:
                output = _dispatch_tool(driver, repo, tu["name"], tu.get("input", {}))
            except Exception as exc:  # noqa: BLE001  -- tool errors go back to the model as
                #  text, not raised: a bad Cypher arg from the model shouldn't crash the run.
                output = json.dumps({"error": str(exc)})
        results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": output})
    return {
        "messages": [{"role": "user", "content": results}],
        "tool_calls": [*state["tool_calls"], *called],
    }


def _route_after_agent(state: AgentState) -> str:
    if state["stop_reason"] == "tool_use":
        return "finalize" if state["turns"] >= state["max_turns"] else "tools"
    return END


def build_graph(
    driver: Driver, repo: Path, *, client: anthropic.Anthropic, model: str, system: str
) -> Any:  # noqa: ANN401  (CompiledStateGraph isn't re-exported from langgraph.graph's
    #  public surface; Any is more stable across langgraph versions than reaching into
    #  that internal module path)
    """Compile the agent loop: agent <-> tools, with a forced-finalize escape at max_turns."""
    graph = StateGraph(AgentState)
    graph.add_node("agent", lambda s: _agent_node(s, client=client, model=model, system=system))
    graph.add_node("tools", lambda s: _tool_node(s, driver=driver, repo=repo))
    graph.add_node(
        "finalize", lambda s: _finalize_node(s, client=client, model=model, system=system)
    )
    graph.set_entry_point("agent")
    graph.add_conditional_edges(
        "agent", _route_after_agent, {"tools": "tools", "finalize": "finalize", END: END}
    )
    graph.add_edge("tools", "agent")
    graph.add_edge("finalize", END)
    return graph.compile()


def _known_files(driver: Driver) -> list[str]:
    with driver.session() as session:
        return sorted(r["id"] for r in session.run("MATCH (f:File) RETURN f.id AS id"))


def parse_impacted_files(report: str, known_files: set[str]) -> tuple[set[str], list[str]]:
    """Split the model's `IMPACTED_FILES:` line into (recognized, unrecognized).

    Unrecognized mentions are dropped from scoring, not fuzzy-matched or guessed — but
    kept visible on `AgentResult` rather than silently discarded (module docstring).
    """
    match = IMPACTED_RE.search(report)
    if not match or match.group(1).strip().upper() == "NONE":
        return set(), []
    raw = [f.strip() for f in match.group(1).split(",") if f.strip()]
    recognized = {f for f in raw if f in known_files}
    unrecognized = [f for f in raw if f not in known_files]
    return recognized, unrecognized


@dataclass
class AgentResult:
    """One agent run's output plus everything needed to score and cost it."""

    seed: str
    predicted_files: set[str]
    unrecognized_mentions: list[str]
    tests_to_run: str
    likely_reviewer: str
    report: str
    turns: int
    tool_calls: list[str]
    input_tokens: int
    output_tokens: int

    def to_dict(self) -> dict[str, Any]:
        """Serialise for a transcript/eval log."""
        d = {**self.__dict__}
        d["predicted_files"] = sorted(self.predicted_files)
        return d


def run_agent(  # noqa: PLR0913  (driver/repo/seed/model/max_turns/api_key are each a
    #  distinct, independently-overridable knob — same reasoning as predict_impact_loo)
    driver: Driver,
    repo: Path,
    seed: str,
    *,
    model: str = DEFAULT_MODEL,
    max_turns: int = MAX_TURNS,
    api_key: str | None = None,
) -> AgentResult:
    """Run the agent loop for one seed file and return its scored, costed result.

    Wrapped in one root `agent.run` span so every `agent.llm_call`/`agent.tool_call` span
    the loop emits nests under it as a child, sharing one `trace_id` — without this, each
    node's `tracing.span()` call would start its own independent trace, and "trace" would
    describe a pile of unrelated spans rather than one agent invocation.
    """
    with tracing.span("agent.run", seed=seed, model=model, max_turns=max_turns) as run_span:
        client = anthropic.Anthropic(api_key=api_key)
        known = _known_files(driver)
        system = SYSTEM_PROMPT_TEMPLATE.format(file_list="\n".join(known), max_turns=max_turns)
        graph = build_graph(driver, repo, client=client, model=model, system=system)

        initial: AgentState = {
            "messages": [{"role": "user", "content": f"File about to change: {seed}"}],
            "turns": 0,
            "max_turns": max_turns,
            "stop_reason": None,
            "input_tokens": 0,
            "output_tokens": 0,
            "tool_calls": [],
        }
        final_state = graph.invoke(initial, config={"recursion_limit": max_turns * 4 + 10})

        report = _final_text(final_state)
        predicted, unrecognized = parse_impacted_files(report, set(known))
        predicted.discard(seed)
        tests_match = TESTS_RE.search(report)
        reviewer_match = REVIEWER_RE.search(report)

        if run_span:
            run_span.set_attribute("turns", final_state["turns"])
            run_span.set_attribute("input_tokens", final_state["input_tokens"])
            run_span.set_attribute("output_tokens", final_state["output_tokens"])
            run_span.set_attribute("predicted_files", len(predicted))

        return AgentResult(
            seed=seed,
            predicted_files=predicted,
            unrecognized_mentions=unrecognized,
            tests_to_run=tests_match.group(1).strip() if tests_match else "",
            likely_reviewer=reviewer_match.group(1).strip() if reviewer_match else "",
            report=report,
            turns=final_state["turns"],
            tool_calls=final_state["tool_calls"],
            input_tokens=final_state["input_tokens"],
            output_tokens=final_state["output_tokens"],
        )
