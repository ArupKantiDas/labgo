"""Python repo -> call/import graph, via the standard-library AST.

Design note (worth defending in a design review):

Static call resolution in Python is *provably incomplete*. `getattr(obj, name)()`,
duck-typed dispatch, decorators that swap implementations, and runtime monkey-patching
cannot be resolved without executing the program. So this module does not pretend to
produce a sound call graph. It produces a graph plus an honest confidence tier per edge
and an unresolved-call count, and lets the evaluation layer report recall per tier.

The alternative — a type-inferring resolver (pyright/jedi) — buys precision at a large
complexity and runtime cost. Start here, measure, and only upgrade if the eval set shows
call-graph recall is the binding constraint. That measurement is the point.
"""

from __future__ import annotations

import ast
import builtins
import sys
from collections import defaultdict
from pathlib import Path

from labgo.ingest.models import (
    Confidence,
    Edge,
    EdgeKind,
    ExtractionStats,
    Graph,
    Node,
    NodeKind,
)

# Directories that are never interesting and dominate parse time if included.
SKIP_DIRS = {
    ".git", ".venv", "venv", "env", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", "node_modules", "build", "dist", ".tox", ".eggs", "site-packages",
}


BUILTIN_NAMES = frozenset(dir(builtins))
STDLIB_MODULES = frozenset(sys.stdlib_module_names)


def _is_external(
    name: str,
    base: str | None,
    v: "_FileVisitor",
    module_to_file: dict[str, str],
    by_name: dict[str, list[str]],
) -> bool:
    """Is this call site targeting something outside the repo?

    Conservative on purpose: a name that also exists as a repo definition is *not*
    treated as external, even if it shadows a builtin. Over-classifying as external
    would inflate the resolution rate by shrinking the denominator — the exact
    failure mode this function exists to correct.
    """
    if base is not None:
        # `mod.func()` where `mod` was imported. External unless that module is
        # a file in this repo.
        if base in v.imports:
            mod, _ = v.imports[base]
            if mod in module_to_file:
                return False
            return mod.split(".")[0] in STDLIB_MODULES or True  # stdlib or third-party
        return False  # unknown receiver (a local variable) — could be internal

    # Bare `name()`. Builtin, unless the repo defines something by that name.
    if name in BUILTIN_NAMES and name not in by_name:
        return True

    # `from <stdlib> import name`
    if name in v.imports:
        mod, _ = v.imports[name]
        if mod not in module_to_file:
            return True

    return False


def _is_test_path(rel: str) -> bool:
    name = Path(rel).name
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or "tests" in Path(rel).parts
    )


def iter_python_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for p in root.rglob("*.py"):
        if any(part in SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        out.append(p)
    return sorted(out)


def _module_name(root: Path, path: Path) -> str:
    """`pkg/sub/mod.py` -> `pkg.sub.mod`; `pkg/__init__.py` -> `pkg`."""
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


class _FileVisitor(ast.NodeVisitor):
    """Single-file pass: collect definitions, imports, and raw call sites."""

    def __init__(self, rel_path: str) -> None:
        self.rel_path = rel_path
        self.nodes: list[Node] = []
        self.edges: list[Edge] = []
        # (caller_id, called_name, attr_base, enclosing_class_qualname)
        # Resolved later, once every file's definitions are known.
        self.calls: list[tuple[str, str, str | None, str | None]] = []
        # `from x import y as z` -> {z: (x, y)};  `import x as z` -> {z: (x, None)}
        self.imports: dict[str, tuple[str, str | None]] = {}
        self.import_modules: set[str] = set()
        self._scope: list[str] = []          # qualname stack (class/function names)
        self._func_stack: list[str] = []     # ids of enclosing *functions* only
        self._class_stack: list[str] = []    # qualnames of enclosing classes

    # --- definitions -----------------------------------------------------

    def _qual(self, name: str) -> str:
        return ".".join([*self._scope, name])

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qual = self._qual(node.name)
        node_id = f"{self.rel_path}::{qual}"
        self.nodes.append(
            Node(
                id=node_id,
                kind=NodeKind.CLASS,
                name=node.name,
                path=self.rel_path,
                lineno=node.lineno,
                end_lineno=getattr(node, "end_lineno", None),
                props={"qualname": qual},
            )
        )
        self.edges.append(Edge(src=self.rel_path, dst=node_id, kind=EdgeKind.CONTAINS))
        self._scope.append(node.name)
        self._class_stack.append(qual)
        self.generic_visit(node)
        self._class_stack.pop()
        self._scope.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        qual = self._qual(node.name)
        node_id = f"{self.rel_path}::{qual}"
        self.nodes.append(
            Node(
                id=node_id,
                kind=NodeKind.FUNCTION,
                name=node.name,
                path=self.rel_path,
                lineno=node.lineno,
                end_lineno=getattr(node, "end_lineno", None),
                props={
                    "qualname": qual,
                    "is_test": node.name.startswith("test_") and _is_test_path(self.rel_path),
                    "is_async": isinstance(node, ast.AsyncFunctionDef),
                    "doc": (ast.get_docstring(node) or "")[:2000],
                },
            )
        )
        self.edges.append(Edge(src=self.rel_path, dst=node_id, kind=EdgeKind.CONTAINS))
        self._scope.append(node.name)
        self._func_stack.append(node_id)
        self.generic_visit(node)
        self._func_stack.pop()
        self._scope.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    # --- imports ---------------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            local = alias.asname or alias.name.split(".")[0]
            self.imports[local] = (alias.name, None)
            self.import_modules.add(alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module is None:      # `from . import x` — relative, skip for v1
            self.generic_visit(node)
            return
        for alias in node.names:
            local = alias.asname or alias.name
            self.imports[local] = (node.module, alias.name)
        self.import_modules.add(node.module)
        self.generic_visit(node)

    # --- call sites ------------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        if not self._func_stack:
            self.generic_visit(node)      # module-level call; no caller to attribute to
            return
        caller = self._func_stack[-1]
        cls = self._class_stack[-1] if self._class_stack else None
        func = node.func
        if isinstance(func, ast.Name):
            self.calls.append((caller, func.id, None, cls))
        elif isinstance(func, ast.Attribute):
            base = func.value.id if isinstance(func.value, ast.Name) else None
            self.calls.append((caller, func.attr, base, cls))
        self.generic_visit(node)


def extract_repo(root: Path) -> Graph:
    """Parse every Python file under `root` into a Graph."""
    root = root.resolve()
    graph = Graph()
    stats = graph.stats

    files = iter_python_files(root)
    visitors: dict[str, _FileVisitor] = {}
    module_to_file: dict[str, str] = {}

    # --- pass 1: parse every file, build the symbol table ---
    for path in files:
        rel = str(path.relative_to(root))
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=rel)
        except SyntaxError:
            stats.files_failed += 1
            continue

        graph.nodes.append(
            Node(
                id=rel,
                kind=NodeKind.FILE,
                name=Path(rel).name,
                path=rel,
                props={
                    "module": _module_name(root, path),
                    "is_test": _is_test_path(rel),
                    "loc": len(path.read_text(encoding="utf-8", errors="replace").splitlines()),
                },
            )
        )
        v = _FileVisitor(rel)
        v.visit(tree)
        visitors[rel] = v
        module_to_file[_module_name(root, path)] = rel
        graph.nodes.extend(v.nodes)
        graph.edges.extend(v.edges)
        stats.files_parsed += 1

    stats.functions = sum(1 for n in graph.nodes if n.kind is NodeKind.FUNCTION)
    stats.classes = sum(1 for n in graph.nodes if n.kind is NodeKind.CLASS)

    # Indexes for resolution.
    defs_by_id = {n.id: n for n in graph.nodes if n.kind is NodeKind.FUNCTION}
    # bare name -> [ids].  Ambiguous names (len > 1) only ever resolve heuristically.
    by_name: dict[str, list[str]] = defaultdict(list)
    for n in defs_by_id.values():
        by_name[n.name].append(n.id)
    # (file, name) -> id, for same-file resolution
    local_index = {(n.path, n.name): n.id for n in defs_by_id.values()}

    # --- pass 2: IMPORTS edges ---
    for rel, v in visitors.items():
        for mod in v.import_modules:
            target = module_to_file.get(mod)
            if target and target != rel:
                graph.edges.append(Edge(src=rel, dst=target, kind=EdgeKind.IMPORTS))

    # --- pass 3: resolve CALLS ---
    seen: set[tuple[str, str]] = set()
    for rel, v in visitors.items():
        for caller, name, base, cls_qual in v.calls:
            stats.total_calls += 1
            target: str | None = None
            conf: Confidence | None = None

            # (0) External: builtins, stdlib, and third-party calls were never
            # repo-internal. Counting them as "unresolved" makes the resolution rate
            # meaningless, so classify and exclude them from the denominator.
            if _is_external(name, base, v, module_to_file, by_name):
                stats.external_calls += 1
                continue

            # (a) self.foo() / cls.foo() inside a class -> that class's method
            if base in ("self", "cls") and cls_qual:
                cand = defs_by_id.get(f"{rel}::{cls_qual}.{name}")
                if cand:
                    target, conf = cand.id, Confidence.SELF

            # (b) explicit import: `from mod import name`
            if target is None and name in v.imports:
                mod, orig = v.imports[name]
                tgt_file = module_to_file.get(mod)
                if tgt_file and orig:
                    cand = local_index.get((tgt_file, orig))
                    if cand:
                        target, conf = cand, Confidence.EXACT

            # (c) defined in the same file
            if target is None:
                cand = local_index.get((rel, name))
                if cand:
                    target, conf = cand, Confidence.LOCAL

            # (d) unique name anywhere in the repo
            if target is None:
                cands = by_name.get(name, [])
                if len(cands) == 1:
                    target, conf = cands[0], Confidence.HEURISTIC

            if target is None or conf is None:
                stats.unresolved_calls += 1
                continue

            if conf is Confidence.EXACT:
                stats.resolved_exact += 1
            elif conf is Confidence.SELF:
                stats.resolved_self += 1
            elif conf is Confidence.LOCAL:
                stats.resolved_local += 1
            else:
                stats.resolved_heuristic += 1

            if caller == target or (caller, target) in seen:
                continue
            seen.add((caller, target))

            graph.edges.append(
                Edge(src=caller, dst=target, kind=EdgeKind.CALLS,
                     props={"confidence": conf.value})
            )

            # A test function calling a function is evidence of coverage.
            caller_node = defs_by_id.get(caller)
            if caller_node and caller_node.props.get("is_test"):
                graph.edges.append(
                    Edge(src=caller, dst=target, kind=EdgeKind.TESTS,
                         props={"confidence": conf.value})
                )

    return graph
