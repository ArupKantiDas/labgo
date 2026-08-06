"""Push the extracted code graph into Neo4j, and read it back.

Persistence is split from extraction (`labgo.ingest`) on purpose: the AST/git extractors
run and are testable with no database (Stage 1). This module is what Stage 2 adds — a
store that can answer multi-hop traversal in one query, which is the whole argument for
Neo4j over a table (D001). Every node also gets a generic `:Node` label alongside its
specific kind label (`:File` / `:Function` / `:Class`), so a single uniqueness constraint
covers all three and callers can traverse by either the general or the specific label.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from neo4j import Driver, GraphDatabase

from labgo.ingest.models import Edge, EdgeKind, Graph, Node, NodeKind

_BATCH_SIZE = 500


def connect(uri: str | None = None, user: str | None = None, password: str | None = None) -> Driver:
    """Open a driver from explicit args, falling back to NEO4J_* env vars."""
    uri = uri or os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    user = user or os.environ.get("NEO4J_USER", "neo4j")
    password = password or os.environ.get("NEO4J_PASSWORD")
    if not password:
        raise RuntimeError(
            "NEO4J_PASSWORD not set. Copy .env.example to .env and fill it in, or pass "
            "--password. It must match docker-compose.yml's NEO4J_AUTH. "
            "Run `labgo doctor` to see what each stage still needs."
        )
    return GraphDatabase.driver(uri, auth=(user, password))


def read_graph_json(data_dir: Path) -> tuple[Graph, list[dict[str, Any]]]:
    """Read `graph.json` (required) and `cochange.json` (optional) back into objects.

    `cochange.json` is written separately by `labgo history` — it is pairs of file ids
    with a count, not `Edge` objects — so it is returned alongside rather than folded
    into `Graph`, matching how the impact viewer already treats the two as separate
    signals (README: call graph vs. co-change are deliberately not merged).
    """
    graph_path = data_dir / "graph.json"
    raw = json.loads(graph_path.read_text(encoding="utf-8"))
    nodes = [
        Node(
            id=n["id"],
            kind=NodeKind(n["kind"]),
            name=n["name"],
            path=n["path"],
            lineno=n.get("lineno"),
            end_lineno=n.get("end_lineno"),
            props=n.get("props", {}),
        )
        for n in raw["nodes"]
    ]
    edges = [
        Edge(src=e["src"], dst=e["dst"], kind=EdgeKind(e["kind"]), props=e.get("props", {}))
        for e in raw["edges"]
    ]

    cochange_path = data_dir / "cochange.json"
    cochange = (
        json.loads(cochange_path.read_text(encoding="utf-8")) if cochange_path.exists() else []
    )

    return Graph(nodes=nodes, edges=edges), cochange


@dataclass
class LoadStats:
    """What got written, for the CLI to report."""

    nodes: int = 0
    edges: int = 0
    cochange_edges: int = 0


def _chunks(rows: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [rows[i : i + size] for i in range(0, len(rows), size)]


def clear_database(driver: Driver) -> None:
    """Delete every node and relationship. Only for `--clear` — not run by default."""
    with driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n")


def _ensure_constraint(driver: Driver) -> None:
    with driver.session() as session:
        session.run("CREATE CONSTRAINT node_id IF NOT EXISTS FOR (n:Node) REQUIRE n.id IS UNIQUE")


def load_graph(
    driver: Driver,
    graph: Graph,
    cochange: list[dict[str, Any]] | None = None,
    *,
    batch_size: int = _BATCH_SIZE,
) -> LoadStats:
    """MERGE the graph into Neo4j in batches. Idempotent — safe to re-run after a re-ingest."""
    stats = LoadStats()
    _ensure_constraint(driver)

    with driver.session() as session:
        for kind in NodeKind:
            rows = [
                {
                    "id": n.id,
                    "name": n.name,
                    "path": n.path,
                    "lineno": n.lineno,
                    "end_lineno": n.end_lineno,
                    **n.props,
                }
                for n in graph.nodes
                if n.kind is kind
            ]
            for batch in _chunks(rows, batch_size):
                session.run(
                    f"UNWIND $rows AS row MERGE (n:Node:{kind.value} {{id: row.id}}) SET n += row",
                    rows=batch,
                )
            stats.nodes += len(rows)

        for kind in EdgeKind:
            rows = [
                {"src": e.src, "dst": e.dst, "props": e.props}
                for e in graph.edges
                if e.kind is kind
            ]
            for batch in _chunks(rows, batch_size):
                session.run(
                    f"UNWIND $rows AS row "
                    f"MATCH (a:Node {{id: row.src}}), (b:Node {{id: row.dst}}) "
                    f"MERGE (a)-[r:{kind.value}]->(b) "
                    f"SET r += row.props",
                    rows=batch,
                )
            stats.edges += len(rows)

        # CO_CHANGED is loaded directed (src->dst as stored) but queried undirected —
        # git history carries no inherent direction between two files that changed together.
        cc_rows = [
            {"src": c["src"], "dst": c["dst"], "count": c["count"]} for c in (cochange or [])
        ]
        for batch in _chunks(cc_rows, batch_size):
            session.run(
                "UNWIND $rows AS row "
                "MATCH (a:File {id: row.src}), (b:File {id: row.dst}) "
                "MERGE (a)-[r:CO_CHANGED]->(b) "
                "SET r.count = row.count",
                rows=batch,
            )
        stats.cochange_edges = len(cc_rows)

    return stats
