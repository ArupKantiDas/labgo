"""Voyage embeddings for Function/Class nodes, indexed in Neo4j for vector search.

Stage 3 (README): the second retrieval signal, deliberately measured on its own before
being blended with the call graph — same reasoning as keeping CALLS and CO_CHANGED
separate in `baseline.py`, and the same lesson D012 paid for: a combined number without
an isolated one first is not trustworthy.

**What gets embedded, and why not docstrings.** The original plan (README, pre-D014) was
to embed function/file docstrings. Measured on httpx: only 19.8% of functions have one
(225 of 1,134) — embedding docstrings alone would leave 80% of nodes with no vector at
all, which is not a fair test of vector search's ceiling, it's a test of documentation
coverage. So the embedded text is each Function/Class node's *source code*, read straight
from the corpus using the line range AST extraction already recorded — every node has
that, and `voyage-code-3` is built to embed code, not prose. See D014.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import voyageai

if TYPE_CHECKING:
    from neo4j import Driver

EMBED_MODEL = "voyage-code-3"
EMBED_DIM = 1024
INDEX_NAME = "node_embedding"

# Voyage's per-call caps for voyage-code-3 are 1,000 texts / 120K tokens; source-code
# snippets for one function are rarely more than a few hundred tokens, so 100 texts a
# call stays comfortably under the token cap without needing to count tokens ourselves.
_BATCH_SIZE = 100


def read_source(repo: Path, path: str, lineno: int | None, end_lineno: int | None) -> str | None:
    """Read one node's source lines straight from the corpus on disk.

    Neo4j (and `graph.json`) store line ranges, not source text — re-reading from `repo`
    keeps the graph small and treats the corpus as the thing that can drift (D008), not
    something to duplicate into our own artifacts.
    """
    if lineno is None or end_lineno is None:
        return None
    try:
        lines = (repo / path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    snippet = "\n".join(lines[lineno - 1 : end_lineno]).strip()
    return snippet or None


def _client(api_key: str | None) -> voyageai.Client:
    key = api_key or os.environ.get("VOYAGE_API_KEY")
    if not key:
        raise RuntimeError(
            "VOYAGE_API_KEY not set. Copy .env.example to .env and fill it in, or pass --api-key."
        )
    return voyageai.Client(api_key=key)


def _batched(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def ensure_vector_index(driver: Driver) -> None:
    """Create the native Neo4j vector index if missing. Idempotent, safe to call anytime.

    Runs on the generic `:Node` label (every node has it — see `graph.py`), so it covers
    Function and Class nodes without a separate index per label. Nodes with no `embedding`
    property (Files, or anything not yet embedded) are simply absent from the index, not
    an error.
    """
    with driver.session() as session:
        session.run(
            f"CREATE VECTOR INDEX {INDEX_NAME} IF NOT EXISTS "
            f"FOR (n:Node) ON (n.embedding) "
            f"OPTIONS {{indexConfig: {{"
            f"`vector.dimensions`: {EMBED_DIM}, "
            f"`vector.similarity_function`: 'cosine'}}}}"
        )


@dataclass
class EmbedStats:
    """What got embedded, for the CLI to report."""

    candidates: int = 0
    embedded: int = 0
    skipped_no_source: int = 0
    total_tokens: int = 0


def embed_nodes(driver: Driver, repo: Path, *, api_key: str | None = None) -> EmbedStats:
    """Embed every Function/Class node's source and write the vectors into Neo4j.

    Reads node metadata (path, line range) from Neo4j itself rather than `graph.json` —
    after `labgo load`, Neo4j is the canonical copy, and this keeps the step usable any
    time after a load with no extra file to keep in sync.
    """
    client = _client(api_key)
    stats = EmbedStats()
    ensure_vector_index(driver)

    with driver.session() as session:
        candidates = [
            dict(r)
            for r in session.run(
                "MATCH (n:Node) WHERE n:Function OR n:Class "
                "RETURN n.id AS id, n.path AS path, n.lineno AS lineno, n.end_lineno AS end_lineno"
            )
        ]
    stats.candidates = len(candidates)

    pairs: list[tuple[str, str]] = []
    for c in candidates:
        text = read_source(repo, c["path"], c["lineno"], c["end_lineno"])
        if text is None:
            stats.skipped_no_source += 1
            continue
        pairs.append((c["id"], text))

    with driver.session() as session:
        for batch in _batched(pairs, _BATCH_SIZE):
            ids = [p[0] for p in batch]
            texts = [p[1] for p in batch]
            result = client.embed(texts, model=EMBED_MODEL, input_type="document")
            stats.total_tokens += result.total_tokens
            stats.embedded += len(ids)
            session.run(
                "UNWIND $rows AS row MATCH (n:Node {id: row.id}) SET n.embedding = row.vec",
                rows=[
                    {"id": i, "vec": v} for i, v in zip(ids, result.embeddings, strict=True)
                ],
            )

    return stats


def semantic_search(
    driver: Driver, query: str, *, k: int = 10, api_key: str | None = None
) -> list[dict[str, Any]]:
    """Embed a query and return its k nearest Function/Class nodes by cosine similarity.

    No graph traversal, no LLM — this is the librarian half of the WHY.md distinction,
    measured on its own so it can be honestly compared against the subway-map half
    (`baseline.py`).
    """
    client = _client(api_key)
    vector = client.embed([query], model=EMBED_MODEL, input_type="query").embeddings[0]

    with driver.session() as session:
        rows = session.run(
            "CALL db.index.vector.queryNodes($index, $k, $vector) "
            "YIELD node, score "
            "RETURN node.id AS id, node.path AS path, score",
            index=INDEX_NAME,
            k=k,
            vector=vector,
        )
        return [dict(r) for r in rows]
