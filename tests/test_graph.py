"""Neo4j loader — pure-Python parts only. No live database required for these."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from labgo.graph import connect, read_graph_json


def test_read_graph_json_roundtrips_nodes_and_edges(tmp_path: Path) -> None:
    (tmp_path / "graph.json").write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "a.py",
                        "kind": "File",
                        "name": "a.py",
                        "path": "a.py",
                        "lineno": None,
                        "end_lineno": None,
                        "props": {"loc": 10},
                    }
                ],
                "edges": [{"src": "a.py", "dst": "a.py::f", "kind": "CONTAINS", "props": {}}],
                "stats": {},
            }
        ),
        encoding="utf-8",
    )

    graph, cochange = read_graph_json(tmp_path)

    assert len(graph.nodes) == 1
    assert graph.nodes[0].props == {"loc": 10}
    assert len(graph.edges) == 1
    assert cochange == []


def test_read_graph_json_picks_up_cochange_when_present(tmp_path: Path) -> None:
    (tmp_path / "graph.json").write_text(json.dumps({"nodes": [], "edges": [], "stats": {}}))
    (tmp_path / "cochange.json").write_text(
        json.dumps([{"src": "a.py", "dst": "b.py", "count": 3}])
    )

    _, cochange = read_graph_json(tmp_path)

    assert cochange == [{"src": "a.py", "dst": "b.py", "count": 3}]


def test_connect_requires_a_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """No password, no default — this must fail loudly, not connect to nothing."""
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="NEO4J_PASSWORD"):
        connect()
