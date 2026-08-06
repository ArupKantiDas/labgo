# LabGo — Change Impact Analyst

Ask a codebase: **"if I change this, what breaks, which tests must run, and who should
review it?"**

A knowledge graph of a real repository (files, functions, call edges, imports, test
coverage, git co-change, ownership) plus semantic search over function/class source code,
queried by a multi-agent workflow — and scored against ground truth mined from git history.

> **New here? Read [`WHY.md`](WHY.md) first.** It explains the problem, the idea, and the
> reasoning in plain English, with no code. This README assumes you already have.
>
> **Want the file-by-file tour?** [`docs/file-map.html`](docs/file-map.html) explains what
> every file is for and diagrams how data actually moves between them.

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
| 2 | Neo4j loader + **deterministic Cypher baseline, measured** | ✅ done |
| 3a | Vector index (Voyage embeddings over Function/Class source, D014) | ✅ done |
| 3b | Vector-only recall measured + naive hybrid (call-graph ∪ vectors), D015 | ✅ done |
| 4 | LangGraph multi-agent workflow | next |
| 5 | MCP server — query it from Claude Code | |
| 6 | Observability (traces, cost/latency) + eval suite in CI | |

**Stage 2 is the one that matters.** A deterministic baseline, scored before any LLM is
added, is what proves the agents earned their place. Skipping it means never being able to
answer "how do you know the agents helped?"

**Stage 2 result (httpx, 236 cases, 2-hop CALLS closure, no LLM):** 22.4% mean recall,
28.8% hit rate, call-graph only. This is the honest number every later stage has to beat.

A combined score with CO_CHANGED reads as 92.1% recall (D012, leave-one-out — each case's
own commit is excluded from its own evidence, fixing the leakage the first, 94.9%
measurement had). But the fix barely moved the number, and the reason turned out to
matter more than the leak: the leak-free combined predictor's mean prediction size is
**40.5 of ~60 files per case** — two-thirds of the repository. A predictor that returns
most of the codebase scores well on recall regardless of whether it found anything
specific, the same failure mode D015 caught for an early vector measurement. 92.1% is
not yet a fair number to cite either — see D012 for the full breakdown and D010 for the
matched-budget sweep this motivates.

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

## Stage 2: Neo4j baseline

```bash
cp .env.example .env               # fill in NEO4J_URI / NEO4J_PASSWORD from your Aura console (D013)
uv run labgo load --clear          # data/graph.json (+cochange.json) -> Neo4j

uv run labgo benchmark ../labgo-corpora/httpx --name httpx     # pin the exam (D008)
uv run labgo baseline benchmarks/httpx --hops 2 --no-cochange  # the honest floor (D012)
uv run labgo baseline benchmarks/httpx --hops 2 --cochange     # leave-one-out combined (D012)
```

No local database needed — the default target is a Neo4j AuraDB Free instance (D013).
`docker compose up -d` still works as an offline/local fallback; swap the three `NEO4J_*`
lines in `.env` per the comment in `.env.example`.

## Stage 3a: Vector index

```bash
uv sync --extra vectors              # pulls in voyageai — kept out of the default install
uv run labgo embed ../labgo-corpora/httpx   # embeds every Function/Class node's source -> Neo4j
uv run labgo search "retry a request with exponential backoff"   # sanity-check retrieval
```

Embeds source code, not docstrings — measured on httpx, only 19.8% of functions have one,
so a docstring-only index would silently starve 80% of nodes of any vector at all (D014).
`labgo search` is the librarian half of the [`WHY.md`](WHY.md) distinction on its own; it
does no graph traversal.

## Stage 3b: vector-only recall + hybrid

```bash
uv run labgo baseline benchmarks/httpx --method vectors --k 4    # vector-only, matched budget
uv run labgo baseline benchmarks/httpx --method hybrid --hops 2 --no-cochange --k 4
```

D001's claim ("vector search cannot answer transitive impact") checked with a number, not
just asserted. At a prediction budget matched to the call-graph baseline (~4 files/case),
vectors alone score **19.3% recall** vs. call-graph's **22.4%** — the graph signal holds up.
But a naive union of the two — no weighting, no ranking, just `predict_impact() |
predict_impact_vector()` — reaches **40.1% recall / 14.8% precision**, nearly double
call-graph alone with *better* precision, meaning the two signals catch different true
positives more often than they overlap. First measurement here actually scored 75.5% and
was wrong: an earlier version of `predict_impact_vector` let a seed file's many functions
each contribute their own top-k neighbors unfiltered, sweeping in ~30% of the entire
60-file corpus per case on average. Fixed to rank by best-matching file and cap at `k`
files total, same shape as `hops` bounding the call-graph baseline. Full sweep and the
fair-comparison reasoning are in D015.

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
Baseline    22.4% mean recall · 28.8% hit rate (call-graph only, 236-case benchmark, D012)
            calls+cochange (leave-one-out) 92.1% recall · 5.9% precision · predicts
            40.5/~60 files/case — not yet a fair number, see D012
Vectors     1,229 Function/Class nodes embedded (voyage-code-3) · 162,884 tokens billed
Retrieval   calls 22.4% · vectors 19.3% (matched ~4-file budget) · hybrid 40.1% (D015)
```

The 27.2% is deliberately reported rather than massaged. The residual is diagnosed —
method calls on untyped local receivers — and the decision to defer type inference until
the eval set proves it is the binding constraint is recorded in D004.

## Stack

| | |
|---|---|
| Graph | Neo4j (AuraDB Free, cloud) — see D013 |
| Vectors | Voyage `voyage-code-3` (cloud API, code-specialized) — see D011 |
| Orchestration | LangGraph (LangChain used thinly — raw code where chains obscure routing) |
| Inference | Claude Sonnet 5 + Haiku 4.5 via API |
| Tooling | MCP server exposing the graph |
| Observability | Langfuse / OpenTelemetry |

Orchestration runs local; the graph, embeddings, and inference all call out to hosted APIs
(D011 and D013 explain the trade-off — the laptop this runs on is resource-constrained,
so running Neo4j or an embedding model locally isn't free the way it looks on paper).

## Layout

```
src/labgo/
  ingest/
    models.py   graph schema (nodes, edges, confidence tiers, metrics)
    pyast.py    Python AST -> call/import graph
    gitlog.py   git history -> co-change edges + eval set
  benchmark.py  pinned, reproducible benchmarks (D008)
  graph.py      Neo4j loader (connect / read_graph_json / load_graph)
  baseline.py   deterministic Cypher impact prediction + scoring (no LLM)
  embed.py      Voyage embeddings over Function/Class source + Neo4j vector index (D014)
  hybrid.py     vector-only + hybrid impact prediction, scored the same way (D015)
  cli.py        ingest / history / benchmark / verify / load / baseline / embed / search / view
viewer/         React + force-graph impact viewer (dist/ committed, served by `labgo view`)
docs/file-map.html   file-by-file explainer + data flow diagram
docker-compose.yml   local Neo4j, community edition
DECISIONS.md    append-only decision log
```
