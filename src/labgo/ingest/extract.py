"""Multi-language dispatch: route every source file to the right backend (D018).

Three tiers, in order of fidelity:

1. **Python -> `pyast`**, completely unchanged. Its resolution numbers are measured
   and logged (D004/D005); this module folds its output in without touching its
   code path, so the httpx baseline stays exactly reproducible.
2. **Grammar languages -> `tsast`** (tree-sitter): definitions, calls, imports,
   tests — with the same confidence tiers.
3. **Everything else the registry recognizes -> file-level fallback**: a File node
   with language/loc/is_test and nothing more. Not a stub — co-change evidence from
   git history is language-agnostic and carries most of the retrieval signal on its
   own (D010/D012), so impact mode, the eval set, and the viewer all still work.

One census walk decides the routing; `languages.ALL_SKIP_DIRS` prunes vendored and
build directories before they are ever visited.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path, PurePosixPath

from labgo.ingest import pyast, tsast
from labgo.ingest.languages import (
    ALL_SKIP_DIRS,
    FALLBACK_EXTENSIONS,
    language_for_path,
    spec_for_path,
)
from labgo.ingest.models import Graph, Node, NodeKind


def extract_repo(root: Path) -> Graph:
    """Parse every recognized source file under `root` into one Graph."""
    root = root.resolve()
    graph = Graph()

    py_present = False
    ts_files: dict[str, list[tuple[str, Path]]] = {}
    fallback: list[tuple[str, str]] = []  # (rel, language)

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in ALL_SKIP_DIRS)
        for fname in sorted(filenames):
            path = Path(dirpath) / fname
            rel = path.relative_to(root).as_posix()
            spec = spec_for_path(fname)
            if spec is not None and spec.name == "python":
                py_present = True
            elif spec is not None and spec.grammar is not None:
                ts_files.setdefault(spec.grammar, []).append((rel, path))
            else:
                lang = FALLBACK_EXTENSIONS.get(PurePosixPath(fname).suffix.lower())
                if lang is not None:
                    fallback.append((rel, lang))

    if py_present:
        _merge_python(graph, root)

    if ts_files:
        unhandled = tsast.extract(root, ts_files, graph)
        fallback.extend(
            (rel, language_for_path(rel) or "unknown") for rel, _ in unhandled
        )

    for rel, lang in fallback:
        _file_level_node(graph, root, rel, lang)

    return graph


def _merge_python(graph: Graph, root: Path) -> None:
    """Run the untouched pyast extractor and fold its output into `graph`.

    Nodes gain `props["language"] = "python"` via `dataclasses.replace` (they are
    frozen) — a post-pass, so `pyast` itself stays byte-for-byte the measured path.
    """
    py_graph = pyast.extract_repo(root)
    graph.nodes.extend(
        dataclasses.replace(n, props={**n.props, "language": "python"}) for n in py_graph.nodes
    )
    graph.edges.extend(py_graph.edges)

    src, dst = py_graph.stats, graph.stats
    per: dict[str, int] = {}
    for key in (
        "files_parsed",
        "files_failed",
        "functions",
        "classes",
        "total_calls",
        "external_calls",
        "resolved_exact",
        "resolved_self",
        "resolved_local",
        "resolved_heuristic",
        "unresolved_calls",
    ):
        value = getattr(src, key)
        setattr(dst, key, getattr(dst, key) + value)
        if value:
            per[key] = value
    dst.per_language["python"] = per


def _file_level_node(graph: Graph, root: Path, rel: str, lang: str) -> None:
    """Tier-3 ingestion: one File node, no definitions — git signal only."""
    try:
        text = (root / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        graph.stats.files_failed += 1
        return
    parts = PurePosixPath(rel).parts
    graph.nodes.append(
        Node(
            id=rel,
            kind=NodeKind.FILE,
            name=PurePosixPath(rel).name,
            path=rel,
            props={
                "language": lang,
                "loc": len(text.splitlines()),
                # No per-language conventions at this tier; the generic directory
                # heuristic matches pyast's `"tests" in parts` fallback.
                "is_test": any(p in ("test", "tests", "spec", "__tests__") for p in parts[:-1]),
            },
        )
    )
    graph.stats.files_fallback += 1
    per = graph.stats.per_language.setdefault(lang, {})
    per["files_fallback"] = per.get("files_fallback", 0) + 1
