"""Dispatch-layer tests (D018): routing, merging, degradation, schema stability."""

from __future__ import annotations

import textwrap
from pathlib import Path

from labgo.ingest import tsast
from labgo.ingest.extract import extract_repo
from labgo.ingest.models import NodeKind


def _write(root: Path, rel: str, src: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(src), encoding="utf-8")


def _mixed_repo(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "pkg/util.py",
        """
        def helper():
            return 1
        """,
    )
    _write(
        tmp_path,
        "cmd/main.go",
        """
        package main

        func main() {
            run()
        }

        func run() {}
        """,
    )
    _write(tmp_path, "app/Main.kt", "fun main() {}\n")


def test_mixed_repo_routes_all_three_tiers(tmp_path: Path) -> None:
    _mixed_repo(tmp_path)
    g = extract_repo(tmp_path)
    files = {n.id: n for n in g.nodes if n.kind is NodeKind.FILE}

    assert files["pkg/util.py"].props["language"] == "python"
    assert files["cmd/main.go"].props["language"] == "go"
    assert files["app/Main.kt"].props["language"] == "kotlin"

    assert "pkg/util.py::helper" in {n.id for n in g.nodes}, "pyast path must still run"
    assert "cmd/main.go::run" in {n.id for n in g.nodes}, "tsast path must still run"
    assert g.stats.files_fallback == 1
    assert set(g.stats.per_language) == {"python", "go", "kotlin"}


def test_global_counters_equal_per_language_sums(tmp_path: Path) -> None:
    """The merge-integrity invariant: the global stats are exactly the sum of parts."""
    _mixed_repo(tmp_path)
    g = extract_repo(tmp_path)
    for key in ("files_parsed", "functions", "total_calls", "unresolved_calls"):
        total = sum(per.get(key, 0) for per in g.stats.per_language.values())
        assert getattr(g.stats, key) == total, key


def test_degradation_when_tree_sitter_unavailable(tmp_path: Path, monkeypatch) -> None:
    """Without tree-sitter, grammar languages fall back to file-level — never crash."""
    _mixed_repo(tmp_path)
    monkeypatch.setattr(tsast, "AVAILABLE", False)
    g = extract_repo(tmp_path)
    files = {n.id: n for n in g.nodes if n.kind is NodeKind.FILE}
    assert "cmd/main.go" in files, "the Go file must still get a File node"
    assert "cmd/main.go::run" not in {n.id for n in g.nodes}
    assert g.stats.files_fallback == 2  # main.go + Main.kt
    assert g.stats.per_language["go"].get("degraded") == 1
    assert "pkg/util.py::helper" in {n.id for n in g.nodes}, "Python must be unaffected"


def test_graph_round_trips_through_serialization(tmp_path: Path) -> None:
    """to_dict must stay loadable by the Neo4j/viewer consumers (schema guarantee)."""
    _mixed_repo(tmp_path)
    g = extract_repo(tmp_path)
    d = g.to_dict()
    assert {n["id"] for n in d["nodes"]} == {n.id for n in g.nodes}
    assert d["stats"]["per_language"]["go"]["functions"] == 2
    assert all({"src", "dst", "kind"} <= set(e) for e in d["edges"])


def test_vendored_dirs_are_pruned(tmp_path: Path) -> None:
    _mixed_repo(tmp_path)
    _write(tmp_path, "vendor/dep/dep.go", "package dep\n\nfunc Dep() {}\n")
    _write(tmp_path, "node_modules/x/x.js", "export function x() {}\n")
    g = extract_repo(tmp_path)
    ids = {n.id for n in g.nodes}
    assert not any(i.startswith(("vendor/", "node_modules/")) for i in ids)
