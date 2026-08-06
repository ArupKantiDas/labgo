"""Stage 5: an MCP server exposing this project's graph, vectors, and history.

Deliberately thin — this module owns no business logic of its own, only MCP wiring
around the same five tool functions `agent.py` built for the LangGraph loop (Stage 4):
`call_graph_traverse`, `co_change_neighbors`, `semantic_search`, `test_coverage`,
`likely_reviewer`. Two callers, one implementation — the point of promoting those
functions off `agent.py`'s private (`_tool_*`) names was exactly this reuse.

Where Stage 4 puts an LLM inside this project deciding which tool to call, Stage 5 puts
this project's tools inside *any* MCP client's own model — Claude Code, Claude Desktop,
or anything else that speaks the protocol. Same tools, opposite direction of control.

Connects to Neo4j once at process startup (`labgo mcp <repo>` keeps a driver open for the
life of the process) rather than per-call like the CLI's other commands — an MCP server
is a long-lived process talking to its client over stdio, not a one-shot invocation.

Each tool call is wrapped in `tracing.span()` (Stage 6) — latency only, not cost: the
calling model here is the *client's*, not one this project pays for, so there are no
tokens of labgo's own to report the way `agent.py`'s spans do.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from mcp.server.mcpserver import MCPServer

from labgo import tracing
from labgo.agent import (
    tool_call_graph,
    tool_co_change,
    tool_likely_reviewer,
    tool_semantic_search,
    tool_test_coverage,
)

if TYPE_CHECKING:
    from pathlib import Path

    from neo4j import Driver


def build_server(driver: Driver, repo: Path) -> MCPServer:
    """Wire the five tools onto a fresh `MCPServer`, closing over `driver`/`repo`.

    Each wrapper re-shapes a typed, MCP-friendly signature into the `{"file": ..., ...}`
    dict `agent.py`'s tool functions expect — the same JSON shape Anthropic's tool-use
    API and MCP both settle on, so no translation happens beyond the parameter names.
    """
    server = MCPServer(
        name="labgo",
        description="Change-impact analysis over a code graph: call graph, co-change "
        "history, semantic search, test coverage, and likely reviewers.",
    )

    @server.tool()
    def call_graph_traverse(file: str, hops: int = 2) -> str:
        """Files reachable via the static CALLS graph within `hops` of a seed file."""
        with tracing.span("mcp.tool_call", tool="call_graph_traverse"):
            return tool_call_graph(driver, {"file": file, "hops": hops})

    @server.tool()
    def co_change_neighbors(file: str, min_count: int = 2) -> str:
        """Files that historically changed together with this file in git history."""
        with tracing.span("mcp.tool_call", tool="co_change_neighbors"):
            return tool_co_change(driver, {"file": file, "min_count": min_count})

    @server.tool()
    def semantic_search(query: str, k: int = 8) -> str:
        """Search embedded function/class source by meaning, not by dependency."""
        with tracing.span("mcp.tool_call", tool="semantic_search"):
            return tool_semantic_search(driver, {"query": query, "k": k})

    @server.tool()
    def test_coverage(file: str) -> str:
        """Test functions that exercise functions defined in this file."""
        with tracing.span("mcp.tool_call", tool="test_coverage"):
            return tool_test_coverage(driver, {"file": file})

    @server.tool()
    def likely_reviewer(file: str) -> str:
        """Most frequent historical committers to this file — a review-routing heuristic."""
        with tracing.span("mcp.tool_call", tool="likely_reviewer"):
            return tool_likely_reviewer(repo, {"file": file})

    @server.resource("labgo://files")
    def known_files() -> str:
        """Every file id in the corpus.

        A resource, not a tool: static context a client reads once, not an action to
        invoke repeatedly — the same grounding `agent.py`'s system prompt gives the
        LangGraph loop, offered here for a client to fetch on its own terms.
        """
        with driver.session() as session:
            ids = sorted(r["id"] for r in session.run("MATCH (f:File) RETURN f.id AS id"))
        return json.dumps(ids)

    return server
