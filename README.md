# LabGo — Change Impact Analyst

Ask a codebase: **"if I change this, what breaks, which tests must run, and who should
review it?"**

A knowledge graph of a real repository (files, functions, call edges, imports, test
coverage, git co-change, ownership) plus semantic search over docstrings, queried by a
multi-agent workflow — and scored against ground truth mined from git history.

> **New here? Read [`WHY.md`](WHY.md) first.** It explains the problem, the idea, and the
> reasoning in plain English, with no code. This README assumes you already have.

## Why this shape

Built as portfolio evidence for an AI Architect role. Two constraints drive every design
choice (see [`DECISIONS.md`](DECISIONS.md)):

1. **The graph has to be load-bearing.** Transitive impact is multi-hop closure over a
   call graph. Vector search cannot answer it — similarity finds code that *resembles* a
   function, not code that *depends on* it. That makes "why Neo4j and not just pgvector?"
   a question with a real answer.
2. **Everything gets measured.** Git history supplies free ground truth: given one file
   from a historical commit, predict the rest. So retrieval and generation are evaluated
   *separately*, with numbers, and regressions are detectable.

## Status

| Stage | | |
|---|---|---|
| 1 | AST → code graph, git → eval set | ✅ done |
| 2 | Neo4j loader + **deterministic Cypher baseline, measured** | next |
| 3 | Vector index (local embeddings) + hybrid retrieval routing | |
| 4 | LangGraph multi-agent workflow | |
| 5 | MCP server — query it from Claude Code | |
| 6 | Observability (traces, cost/latency) + eval suite in CI | |

**Stage 2 is the one that matters.** A deterministic baseline, scored before any LLM is
added, is what proves the agents earned their place. Skipping it means never being able to
answer "how do you know the agents helped?"

## Quickstart

```bash
uv sync

# Analysis corpora live OUTSIDE this tree — uv would otherwise treat a cloned
# repo's pyproject.toml as a workspace member (see D007).
mkdir -p ../labgo-corpora
git clone --filter=blob:none https://github.com/encode/httpx.git ../labgo-corpora/httpx

uv run labgo ingest  ../labgo-corpora/httpx   # -> data/graph.json
uv run labgo history ../labgo-corpora/httpx   # -> data/cochange.json, data/evalset.json
uv run labgo view                             # -> http://127.0.0.1:4173
```

None of the three needs a database, an API key, or a network call.

## Impact viewer

`labgo view` serves an interactive graph of whatever you just ingested — point it at
your own repo, not just httpx. Two modes:

- **Explore** — the raw code graph. Search, click through call/import/test edges, inspect
  any node.
- **Impact** — pick a file or function and see what would be affected by changing it,
  *before* you change it: everything that calls it (or calls its callers — hop depth is
  adjustable), plus files that have historically changed alongside it in git history. The
  two signals are deliberately separate, for the same reason the project measures retrieval
  and generation separately (see [`WHY.md`](WHY.md)) — static call resolution is only 27%
  complete on httpx (D004), so co-change evidence catches real coupling the call graph
  misses.

The viewer ships prebuilt in `viewer/dist/`, so `labgo view` needs no Node at runtime —
only `data/graph.json` (from `ingest`) and, optionally, `data/cochange.json` (from
`history`) for the co-change half of impact mode. Node is only needed if you're changing
the frontend itself:

```bash
cd viewer && npm install && npm run build   # regenerates viewer/dist/
```

## Measured on httpx (1,523 commits, 60 Python files)

```
AST         1,301 nodes · 2,100 edges (567 CALLS · 35 IMPORTS · 257 TESTS)
Resolution  27.2% of 2,845 in-scope call sites
            4 exact · 99 self/cls · 327 local · 343 heuristic · 2,072 unresolved
History     1,482 commits scanned -> 610 eval cases · 1,745 co-change edges
```

The 27.2% is deliberately reported rather than massaged. The residual is diagnosed —
method calls on untyped local receivers — and the decision to defer type inference until
the eval set proves it is the binding constraint is recorded in D004.

## Stack

| | |
|---|---|
| Graph | Neo4j |
| Vectors | local, `nomic-embed-text` via Ollama (free, offline) |
| Orchestration | LangGraph (LangChain used thinly — raw code where chains obscure routing) |
| Inference | Claude Sonnet 5 + Haiku 4.5 via API |
| Tooling | MCP server exposing the graph |
| Observability | Langfuse / OpenTelemetry |

Local only — no cloud providers by design.

## Layout

```
src/labgo/
  ingest/
    models.py   graph schema (nodes, edges, confidence tiers, metrics)
    pyast.py    Python AST -> call/import graph
    gitlog.py   git history -> co-change edges + eval set
  benchmark.py  pinned, reproducible benchmarks (D008)
  cli.py        ingest / history / benchmark / verify / view
viewer/         React + force-graph impact viewer (dist/ committed, served by `labgo view`)
DECISIONS.md    append-only decision log
```
