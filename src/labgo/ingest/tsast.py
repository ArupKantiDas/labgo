"""Tree-sitter repo -> call/import graph for the non-Python grammar languages.

Same posture as `pyast` (D004/D005), generalized: name-based resolution with honest
confidence tiers, unresolved calls counted rather than hidden, and external calls
classified out of the denominator where a language makes that cheaply determinable.
No type inference anywhere — per-language stats in `ExtractionStats.per_language`
disclose exactly how far name-based resolution carries each language (D018).

Structure: one generic engine, one declarative `TSSpec` per language. A spec is
three tree-sitter queries (definitions, calls, imports) plus small tables — scope
node types for qualnames, self-receiver spellings, curated builtins, an import
style. The engine parses every file (pass 1), then resolves calls per *language*
across all of that language's files (pass 2), exactly the shape `pyast` uses.

Failure is graded, never fatal (D018): if tree-sitter itself is missing this module
sets `AVAILABLE = False` and the dispatcher falls back to file-level ingestion; if
one grammar or query fails to compile, only that language degrades; if one file
fails to parse, it is counted in `files_failed` and skipped.
"""

from __future__ import annotations

import os
import re
from bisect import bisect_right
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from labgo.ingest.languages import ALL_SKIP_DIRS, LANGUAGES, LanguageSpec, is_test_path
from labgo.ingest.models import (
    Confidence,
    Edge,
    EdgeKind,
    Graph,
    Node,
    NodeKind,
)

if TYPE_CHECKING:
    from tree_sitter import Node as TSNode
    from tree_sitter import Tree

try:
    from tree_sitter import Query, QueryCursor
    from tree_sitter_language_pack import get_language, get_parser

    AVAILABLE = True
except ImportError:  # pragma: no cover — exercised via monkeypatch in tests
    AVAILABLE = False


@dataclass(frozen=True)
class TSSpec:
    """One language's declarative extraction rules.

    `defs_query` captures `@def.function` / `@def.method` / `@def.class` with `@name`
    (plus optional `@recv_type`/`@recv_name` for Go receivers, `@class_scope` for C++
    out-of-line methods, `@anno` for Java/C# test annotations, `@package` for Go).
    `calls_query` captures `@call` + `@callee` (+ optional `@receiver`).
    `imports_query` captures per `import_style` — see `_collect_imports`.
    """

    grammar: str
    defs_query: str
    calls_query: str
    imports_query: str
    # Ancestor node type -> field holding its name; contributes to qualnames.
    scope_types: dict[str, str]
    # Subset of scope_types marking class-like scopes (for SELF resolution).
    class_types: frozenset[str]
    self_receivers: tuple[str, ...]
    implicit_self: bool  # bare foo() inside a class may be a method call (Java/C#/Ruby)
    builtins: frozenset[str]
    import_style: str  # go | relative_js | java | rust_mod | include | relative_file | none
    external_receivers: frozenset[str] = frozenset()  # e.g. {"std"} for Rust/C++
    test_annotations: frozenset[str] = frozenset()  # @Test / [Fact] style markers


@dataclass
class _Func:
    """One extracted function/method definition, pass-1 output."""

    id: str
    name: str
    start: int  # byte offset — call sites attribute to the innermost containing def
    end: int
    class_qual: str | None  # enclosing class qualname (or Go receiver type)
    recv_name: str | None  # Go: the receiver variable (`s` in `func (s *Server)`)
    is_test: bool


@dataclass
class _FileFacts:
    """Everything pass 1 learned about one file, before cross-file resolution."""

    rel: str
    lang: str
    spec: TSSpec
    funcs: list[_Func] = field(default_factory=list)
    calls: list[tuple[int, str, str | None]] = field(default_factory=list)  # (byte, name, recv)
    symbol_imports: dict[str, tuple[str, str]] = field(default_factory=dict)  # local->(path,orig)
    ns_imports: dict[str, str] = field(default_factory=dict)  # alias -> raw path
    import_paths: list[str] = field(default_factory=list)  # whole-file imports (include/require)
    external_names: set[str] = field(default_factory=set)  # names known to come from outside
    package: str | None = None  # Go package name


@dataclass
class _Resolved:
    """Per-file import resolution results, pass-2 intermediate."""

    sym: dict[str, tuple[str, str]] = field(default_factory=dict)  # local -> (target_file, orig)
    recv: dict[str, list[str]] = field(default_factory=dict)  # alias -> target files
    whole: list[str] = field(default_factory=list)  # include/require-style target files
    external_receivers: set[str] = field(default_factory=set)  # aliases proven external


class _LangIndex:
    """Cross-file lookup tables for one language — the pass-2 symbol table."""

    def __init__(self, facts: list[_FileFacts]) -> None:
        self.files: set[str] = {f.rel for f in facts}
        self.local: dict[tuple[str, str], str] = {}  # (file, name) -> func id
        self.by_id: dict[str, _Func] = {}
        self.by_name: dict[str, list[str]] = {}
        self.pkg_files: dict[str, list[str]] = {}  # Go: dir -> files in that package
        for f in facts:
            d = str(PurePosixPath(f.rel).parent)
            self.pkg_files.setdefault(d, []).append(f.rel)
            for fn in f.funcs:
                self.local[(f.rel, fn.name)] = fn.id
                self.by_id[fn.id] = fn
                self.by_name.setdefault(fn.name, []).append(fn.id)


def _text(node: TSNode) -> str:
    """A node's source text, decoded defensively."""
    return (node.text or b"").decode("utf-8", errors="replace")


def _one(caps: dict[str, list[TSNode]], name: str) -> TSNode | None:
    """First capture by name, or None."""
    nodes = caps.get(name)
    return nodes[0] if nodes else None


def _scope_prefix(node: TSNode, spec: TSSpec) -> tuple[str, str | None]:
    """Qualname prefix from enclosing scopes, plus the nearest class-like qualname.

    Walks ancestors through `spec.scope_types`, collecting names outermost-first.
    Joined with `.` uniformly — even where the source language writes `::` — so ids
    keep the exact `path::A.B.c` shape `pyast` established (one id scheme downstream).
    """
    parts: list[str] = []
    cur = node.parent
    while cur is not None:
        fld = spec.scope_types.get(cur.type)
        if fld is not None:
            name_node = cur.child_by_field_name(fld)
            if name_node is not None:
                parts.insert(0, _text(name_node))
        cur = cur.parent
    prefix = ".".join(parts) + "." if parts else ""
    return prefix, _nearest_class_qual(node, spec)


def _nearest_class_qual(node: TSNode, spec: TSSpec) -> str | None:
    """Qualname of the innermost class-like ancestor, or None."""
    cur = node.parent
    while cur is not None:
        if cur.type in spec.class_types:
            fld = spec.scope_types.get(cur.type, "name")
            name_node = cur.child_by_field_name(fld)
            if name_node is None:
                return None
            inner_prefix, _ = _scope_prefix(cur, spec)
            return f"{inner_prefix}{_text(name_node)}"
        cur = cur.parent
    return None


def _strip_quotes(s: str) -> str:
    """Remove one layer of string quotes from an import path literal."""
    return s.strip("\"'`")


class _Extractor:
    """One extraction run over one repository — engine state and the two passes."""

    def __init__(self, root: Path, graph: Graph) -> None:
        self.root = root
        self.graph = graph
        self.stats = graph.stats
        self.facts_by_lang: dict[str, list[_FileFacts]] = {}
        self.go_modules: list[tuple[str, str]] = []  # (module path, dir rel prefix)

    # ------------------------------------------------------------------ pass 1

    def parse_files(self, grammar: str, files: list[tuple[str, Path]]) -> list[str] | None:
        """Parse `files` under one grammar into pass-1 facts and File/def nodes.

        Returns the rels handled, or None if the grammar or its queries fail to
        load — the caller degrades those files to file-level ingestion (D018).
        """
        spec = SPECS.get(grammar)
        if spec is None or not AVAILABLE:
            return None
        try:
            language = get_language(spec.grammar)  # type: ignore[arg-type]
            parser = get_parser(spec.grammar)  # type: ignore[arg-type]
            queries = (
                Query(language, spec.defs_query),
                Query(language, spec.calls_query),
                Query(language, spec.imports_query),
            )
        except Exception:  # noqa: BLE001 — any grammar/query failure degrades one language
            return None

        lang_spec = _REGISTRY_BY_GRAMMAR[grammar]
        handled: list[str] = []
        for rel, path in files:
            try:
                src = path.read_bytes()
                tree = parser.parse(src)
            except (OSError, ValueError):
                self._bump(lang_spec.name, "files_failed")
                continue
            facts = _FileFacts(rel=rel, lang=lang_spec.name, spec=spec)
            file_is_test = is_test_path(rel, lang_spec)
            self._file_node(rel, lang_spec.name, src, file_is_test=file_is_test)
            self._collect_defs(facts, queries[0], tree, lang_spec, file_is_test=file_is_test)
            self._collect_calls(facts, queries[1], tree)
            _collect_imports(facts, queries[2], tree)
            self.facts_by_lang.setdefault(lang_spec.name, []).append(facts)
            self._bump(lang_spec.name, "files_parsed")
            handled.append(rel)
        return handled

    def _file_node(self, rel: str, lang: str, src: bytes, *, file_is_test: bool) -> None:
        """Emit the File node for one parsed file."""
        self.graph.nodes.append(
            Node(
                id=rel,
                kind=NodeKind.FILE,
                name=PurePosixPath(rel).name,
                path=rel,
                props={
                    "language": lang,
                    "is_test": file_is_test,
                    "loc": src.count(b"\n") + 1,
                },
            )
        )

    def _collect_defs(
        self,
        facts: _FileFacts,
        query: Query,
        tree: Tree,
        lang_spec: LanguageSpec,
        *,
        file_is_test: bool,
    ) -> None:
        """Turn definition-query matches into Function/Class nodes + CONTAINS edges."""
        spec = facts.spec
        test_re = re.compile(lang_spec.test_func_pattern) if lang_spec.test_func_pattern else None
        seen_ids: set[str] = set()
        for _, caps in QueryCursor(query).matches(tree.root_node):  # type: ignore[attr-defined]
            pkg = _one(caps, "package")
            if pkg is not None:
                facts.package = _text(pkg)
                continue
            name_node = _one(caps, "name")
            def_node = (
                _one(caps, "def.function") or _one(caps, "def.method") or _one(caps, "def.class")
            )
            if name_node is None or def_node is None:
                continue
            name = _text(name_node)
            kind = NodeKind.CLASS if _one(caps, "def.class") is not None else NodeKind.FUNCTION

            recv_type = _one(caps, "recv_type")
            class_scope = _one(caps, "class_scope")
            if recv_type is not None:
                qual, class_qual = f"{_text(recv_type)}.{name}", _text(recv_type)
            elif class_scope is not None:
                qual, class_qual = f"{_text(class_scope)}.{name}", _text(class_scope)
            else:
                prefix, class_qual = _scope_prefix(def_node, spec)
                qual = f"{prefix}{name}"

            node_id = f"{facts.rel}::{qual}"
            if node_id in seen_ids:  # overloads / duplicate pattern hits collapse to one
                continue
            seen_ids.add(node_id)

            annos = {_text(n) for n in caps.get("anno", [])}
            is_test = kind is NodeKind.FUNCTION and (
                bool(annos & spec.test_annotations)
                or (file_is_test and test_re is not None and bool(test_re.match(name)))
            )
            self.graph.nodes.append(
                Node(
                    id=node_id,
                    kind=kind,
                    name=name,
                    path=facts.rel,
                    lineno=def_node.start_point[0] + 1,
                    end_lineno=def_node.end_point[0] + 1,
                    props={"qualname": qual, "language": facts.lang, "is_test": is_test},
                )
            )
            self.graph.edges.append(Edge(src=facts.rel, dst=node_id, kind=EdgeKind.CONTAINS))
            self._bump(facts.lang, "functions" if kind is NodeKind.FUNCTION else "classes")
            if kind is NodeKind.FUNCTION:
                recv_name = _one(caps, "recv_name")
                facts.funcs.append(
                    _Func(
                        id=node_id,
                        name=name,
                        start=def_node.start_byte,
                        end=def_node.end_byte,
                        class_qual=class_qual,
                        recv_name=_text(recv_name) if recv_name is not None else None,
                        is_test=is_test,
                    )
                )

    def _collect_calls(self, facts: _FileFacts, query: Query, tree: Tree) -> None:
        """Record raw call sites: (byte offset, callee name, receiver or None)."""
        for _, caps in QueryCursor(query).matches(tree.root_node):  # type: ignore[attr-defined]
            call = _one(caps, "call")
            callee = _one(caps, "callee")
            if call is None or callee is None:
                continue
            recv = _one(caps, "receiver")
            facts.calls.append(
                (call.start_byte, _text(callee), _text(recv) if recv is not None else None)
            )

    # ------------------------------------------------------------------ pass 2

    def resolve_all(self) -> None:
        """Cross-file resolution, one language at a time (indexes never mix languages)."""
        for facts_list in self.facts_by_lang.values():
            idx = _LangIndex(facts_list)
            resolved = {f.rel: self._resolve_imports(f, idx) for f in facts_list}
            for f in facts_list:
                self._resolve_calls(f, idx, resolved[f.rel])

    def _resolve_imports(self, facts: _FileFacts, idx: _LangIndex) -> _Resolved:
        """Resolve one file's imports to repo files; emit IMPORTS edges; mark externals."""
        r = _Resolved()
        style = facts.spec.import_style
        targets_seen: set[str] = set()

        def _edge(target: str) -> None:
            if target != facts.rel and target not in targets_seen:
                targets_seen.add(target)
                self.graph.edges.append(Edge(src=facts.rel, dst=target, kind=EdgeKind.IMPORTS))

        for local, (raw_path, orig) in facts.symbol_imports.items():
            files = self._import_targets(style, raw_path, facts.rel, idx)
            if files:
                r.sym[local] = (files[0], orig)
                _edge(files[0])
            elif _is_external_import(style, raw_path):
                facts.external_names.add(local)
        for alias, raw_path in facts.ns_imports.items():
            files = self._import_targets(style, raw_path, facts.rel, idx)
            if files:
                r.recv[alias] = files
                for t in files:
                    _edge(t)
            elif _is_external_import(style, raw_path):
                r.external_receivers.add(alias)
        for raw_path in facts.import_paths:
            files = self._import_targets(style, raw_path, facts.rel, idx)
            for t in files:
                r.whole.append(t)
                _edge(t)
        r.external_receivers |= facts.spec.external_receivers
        return r

    def _import_targets(self, style: str, raw: str, rel: str, idx: _LangIndex) -> list[str]:
        """Map one import path to repo-relative file(s), per the language's style."""
        if style == "go":
            return _resolve_go(raw, idx, self.go_modules)
        if style == "relative_js":
            return _resolve_relative_js(raw, rel, idx.files)
        if style == "java":
            return _resolve_java(raw, idx.files)
        if style == "rust_mod":
            return _resolve_rust_mod(raw, rel, idx.files)
        if style in ("include", "relative_file"):
            return _resolve_relative_file(raw, rel, idx.files)
        return []

    def _resolve_calls(self, facts: _FileFacts, idx: _LangIndex, r: _Resolved) -> None:
        """Walk one file's call sites through the ladder; emit CALLS/TESTS edges."""
        funcs = sorted(facts.funcs, key=lambda f: f.start)
        starts = [f.start for f in funcs]
        seen: set[tuple[str, str]] = set()

        for offset, name, recv in facts.calls:
            caller = _innermost(funcs, starts, offset)
            if caller is None:  # module-level call — no caller to attribute to (pyast parity)
                continue
            self._bump(facts.lang, "total_calls")

            if self._is_external(facts, idx, r, name, recv):
                self._bump(facts.lang, "external_calls")
                continue

            target, conf = self._resolve_one(facts, idx, r, caller, name, recv)
            if target is None or conf is None:
                self._bump(facts.lang, "unresolved_calls")
                continue
            self._bump(facts.lang, _CONF_COUNTER[conf])

            if caller.id == target or (caller.id, target) in seen:
                continue
            seen.add((caller.id, target))
            props = {"confidence": conf.value}
            self.graph.edges.append(
                Edge(src=caller.id, dst=target, kind=EdgeKind.CALLS, props=props)
            )
            if caller.is_test:
                self.graph.edges.append(
                    Edge(src=caller.id, dst=target, kind=EdgeKind.TESTS, props=props)
                )

    def _is_external(
        self, facts: _FileFacts, idx: _LangIndex, r: _Resolved, name: str, recv: str | None
    ) -> bool:
        """D005: is this call site targeting something outside the repo?

        Conservative in the same direction as `pyast._is_external`: a name that also
        exists as a same-language repo definition is never external, and an unknown
        receiver (a local variable) stays in the denominator.
        """
        if recv is not None:
            if recv in facts.spec.self_receivers or recv in r.sym or recv in r.recv:
                return False
            # console.log(), Math.max(), std::… — a builtin/external *receiver* marks the
            # call external just as a builtin bare name would.
            return recv in r.external_receivers or recv in facts.spec.builtins
        if name in facts.external_names:
            return True
        return name in facts.spec.builtins and name not in idx.by_name

    def _resolve_one(
        self,
        facts: _FileFacts,
        idx: _LangIndex,
        r: _Resolved,
        caller: _Func,
        name: str,
        recv: str | None,
    ) -> tuple[str | None, Confidence | None]:
        """The generalized resolution ladder — pyast's `_resolve_one`, per language.

        Descending certainty: SELF (receiver is the enclosing instance), EXACT
        (import resolved to a repo file), LOCAL (same file; Go adds same-package),
        HEURISTIC (unique name in this language). Falls through on a miss at every
        rung, exactly like `pyast`.
        """
        spec = facts.spec

        # (a) SELF — this/self/$this, Go receiver variable, or implicit-self languages
        self_ish = (recv is not None and recv in spec.self_receivers) or (
            recv is not None and caller.recv_name is not None and recv == caller.recv_name
        )
        if (self_ish or (recv is None and spec.implicit_self)) and caller.class_qual:
            cand = idx.by_id.get(f"{facts.rel}::{caller.class_qual}.{name}")
            if cand is not None:
                return cand.id, Confidence.SELF
            if spec.import_style == "go":  # Go types span files within one package dir
                pkg_dir = str(PurePosixPath(facts.rel).parent)
                for sibling in idx.pkg_files.get(pkg_dir, []):
                    cand = idx.by_id.get(f"{sibling}::{caller.class_qual}.{name}")
                    if cand is not None:
                        return cand.id, Confidence.SELF

        # (b) EXACT — via a resolved import
        if recv is None:
            if name in r.sym:
                tfile, orig = r.sym[name]
                cand_id = idx.local.get((tfile, orig))
                if cand_id:
                    return cand_id, Confidence.EXACT
            for tfile in r.whole:  # include/require-style: the whole file's names bind
                cand_id = idx.local.get((tfile, name))
                if cand_id:
                    return cand_id, Confidence.EXACT
        else:
            for tfile in r.recv.get(recv, []):
                cand_id = idx.local.get((tfile, name))
                if cand_id:
                    return cand_id, Confidence.EXACT
            if recv in r.sym:  # Java-style: receiver is an imported class, static call
                tfile = r.sym[recv][0]
                cand_id = idx.local.get((tfile, name))
                if cand_id:
                    return cand_id, Confidence.EXACT

        # (c) LOCAL — same file; Go: same package directory
        cand_id = idx.local.get((facts.rel, name))
        if cand_id:
            return cand_id, Confidence.LOCAL
        if spec.import_style == "go":
            pkg_dir = str(PurePosixPath(facts.rel).parent)
            for sibling in idx.pkg_files.get(pkg_dir, []):
                cand_id = idx.local.get((sibling, name))
                if cand_id:
                    return cand_id, Confidence.LOCAL

        # (d) HEURISTIC — unique name within this language
        cands = idx.by_name.get(name, [])
        if len(cands) == 1:
            return cands[0], Confidence.HEURISTIC

        return None, None

    # ------------------------------------------------------------------ shared

    def _bump(self, lang: str, key: str, n: int = 1) -> None:
        """Increment a counter globally and in the per-language breakdown."""
        setattr(self.stats, key, getattr(self.stats, key) + n)
        per = self.stats.per_language.setdefault(lang, {})
        per[key] = per.get(key, 0) + n


_CONF_COUNTER = {
    Confidence.EXACT: "resolved_exact",
    Confidence.SELF: "resolved_self",
    Confidence.LOCAL: "resolved_local",
    Confidence.HEURISTIC: "resolved_heuristic",
}


def _innermost(funcs: list[_Func], starts: list[int], offset: int) -> _Func | None:
    """The innermost definition containing `offset`, by byte-interval containment.

    Nested definitions start later, so walking backward from the nearest start and
    taking the first containing interval yields the innermost enclosing function.
    """
    i = bisect_right(starts, offset) - 1
    while i >= 0:
        if funcs[i].end > offset:
            return funcs[i]
        i -= 1
    return None


# ---------------------------------------------------------------- import styles


def _is_external_import(style: str, raw: str) -> bool:
    """Should an *unresolved* import of `raw` classify its names as external?

    Per-style judgment call, conservative where unsure: a JS relative specifier that
    failed to resolve stays in the denominator (could be generated code), while a
    bare package specifier is definitionally external.
    """
    if style == "relative_js":
        return not raw.startswith(".")
    # go/java: internal targets always resolve (go.mod / path suffix); the rest is deps.
    return style in ("go", "java")


def _resolve_go(raw: str, idx: _LangIndex, modules: list[tuple[str, str]]) -> list[str]:
    """Go import path -> the .go files of that package directory, via go.mod."""
    path = _strip_quotes(raw)
    for mod_path, mod_dir in modules:
        if path == mod_path:
            sub = ""
        elif path.startswith(mod_path + "/"):
            sub = path[len(mod_path) + 1 :]
        else:
            continue
        pkg_dir = str(PurePosixPath(mod_dir) / sub) if (mod_dir or sub) else "."
        return sorted(idx.pkg_files.get(pkg_dir, []))
    return []


_JS_EXTS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
_JS_SWAP = {".js": (".ts", ".tsx"), ".mjs": (".mts",), ".cjs": (".cts",), ".jsx": (".tsx",)}


def _resolve_relative_js(raw: str, rel: str, files: set[str]) -> list[str]:
    """JS/TS relative specifier -> repo file: literal, +ext, /index, NodeNext .js->.ts."""
    spec = _strip_quotes(raw)
    if not spec.startswith("."):
        return []
    base = _normalize(PurePosixPath(rel).parent / spec)
    candidates = [base]
    candidates += [f"{base}{ext}" for ext in _JS_EXTS]
    candidates += [f"{base}/index{ext}" for ext in _JS_EXTS]
    suffix = PurePosixPath(base).suffix
    if suffix in _JS_SWAP:  # NodeNext style: `./x.js` in source, `x.ts` on disk
        stem = base[: -len(suffix)]
        candidates += [f"{stem}{ext}" for ext in _JS_SWAP[suffix]]
    return [c for c in candidates if c in files][:1]


def _resolve_java(raw: str, files: set[str]) -> list[str]:
    """`import a.b.C` -> the file whose path ends with a/b/C.java (any source root)."""
    suffix = raw.replace(".", "/") + ".java"
    hits = [f for f in files if f == suffix or f.endswith("/" + suffix)]
    return sorted(hits)[:1]


def _resolve_rust_mod(raw: str, rel: str, files: set[str]) -> list[str]:
    """`mod foo;` -> sibling foo.rs or foo/mod.rs."""
    parent = PurePosixPath(rel).parent
    for cand in (str(parent / f"{raw}.rs"), str(parent / raw / "mod.rs")):
        c = _normalize(cand)
        if c in files:
            return [c]
    return []


def _resolve_relative_file(raw: str, rel: str, files: set[str]) -> list[str]:
    """Quoted #include / require_relative / PHP require -> file relative to the includer."""
    spec = _strip_quotes(raw)
    parent = PurePosixPath(rel).parent
    candidates = [_normalize(parent / spec), _normalize(spec)]
    if not PurePosixPath(spec).suffix:  # require_relative "helper" -> helper.rb
        candidates += [f"{c}.rb" for c in list(candidates)]
    return [c for c in candidates if c in files][:1]


def _normalize(p: str | PurePosixPath) -> str:
    """Collapse ../ and ./ segments in a repo-relative posix path."""
    parts: list[str] = []
    for part in PurePosixPath(p).parts:
        if part == "..":
            if parts:
                parts.pop()
        elif part != ".":
            parts.append(part)
    return "/".join(parts)


def _collect_imports(facts: _FileFacts, query: Query, tree: Tree) -> None:
    """Turn import-query matches into the per-style raw import tables on `facts`."""
    style = facts.spec.import_style
    for _, caps in QueryCursor(query).matches(tree.root_node):  # type: ignore[attr-defined]
        path_node = _one(caps, "path")
        raw = _text(path_node) if path_node is not None else None
        if style == "go" and raw is not None:
            path = _strip_quotes(raw)
            alias_node = _one(caps, "alias")
            alias = _text(alias_node) if alias_node is not None else path.rsplit("/", 1)[-1]
            facts.ns_imports[alias] = path
        elif style == "relative_js" and raw is not None:
            sym = _one(caps, "symbol")
            if sym is not None:
                alias = _one(caps, "alias")
                local = _text(alias) if alias is not None else _text(sym)
                facts.symbol_imports[local] = (raw, _text(sym))
            ns = _one(caps, "ns_alias")
            if ns is not None:
                facts.ns_imports[_text(ns)] = raw
            default = _one(caps, "default_alias")
            if default is not None:
                facts.symbol_imports[_text(default)] = (raw, "default")
            if sym is None and ns is None and default is None:
                facts.import_paths.append(raw)  # side-effect import / require("...")
        elif style == "java" and raw is not None:
            cls = raw.rsplit(".", 1)[-1]
            facts.symbol_imports[cls] = (raw, cls)
        elif style == "rust_mod":
            mod = _one(caps, "modname")
            if mod is not None:
                facts.ns_imports[_text(mod)] = _text(mod)
            use = _one(caps, "use_path")
            if use is not None:
                _mark_rust_use(facts, _text(use))
        elif style in ("include", "relative_file") and raw is not None:
            facts.import_paths.append(raw)


_RUST_EXTERNAL_ROOTS = ("std", "core", "alloc")


def _mark_rust_use(facts: _FileFacts, use_path: str) -> None:
    """`use std::…` marks its leaf name(s) external — cheap D005 classification."""
    root = use_path.split("::", 1)[0].strip()
    if root not in _RUST_EXTERNAL_ROOTS:
        return
    leaf = use_path.rsplit("::", 1)[-1].strip()
    if leaf.startswith("{") and leaf.endswith("}"):
        for item in leaf[1:-1].split(","):
            facts.external_names.add(item.strip())
    else:
        facts.external_names.add(leaf)
    facts.external_names.add(root)


# ------------------------------------------------------------------- public API


def extract(
    root: Path, files_by_grammar: dict[str, list[tuple[str, Path]]], graph: Graph
) -> list[tuple[str, Path]]:
    """Parse grammar-language files into `graph`; return the files it could NOT handle.

    `files_by_grammar` maps a tree-sitter grammar key to `(rel, absolute_path)` pairs.
    Unhandled files (tree-sitter missing, grammar or query failed to compile) are
    returned for the dispatcher's file-level fallback — degradation is per language,
    never total (D018).
    """
    ex = _Extractor(root, graph)
    ex.go_modules = _find_go_modules(root) if "go" in files_by_grammar else []
    unhandled: list[tuple[str, Path]] = []
    for grammar, files in files_by_grammar.items():
        handled = ex.parse_files(grammar, files)
        if handled is None:
            lang = _REGISTRY_BY_GRAMMAR[grammar].name
            per = ex.stats.per_language.setdefault(lang, {})
            per["degraded"] = 1
            unhandled.extend(files)
    ex.resolve_all()
    return unhandled


def _find_go_modules(root: Path) -> list[tuple[str, str]]:
    """Every go.mod's (module path, directory) — the key Go import resolution needs."""
    modules: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ALL_SKIP_DIRS]
        if "go.mod" in filenames:
            try:
                text = (Path(dirpath) / "go.mod").read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            m = re.search(r"^module\s+(\S+)", text, flags=re.MULTILINE)
            if m:
                rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
                modules.append((m.group(1), "" if rel_dir == "." else rel_dir))
    return modules


_REGISTRY_BY_GRAMMAR: dict[str, LanguageSpec] = {
    spec.grammar: spec for spec in LANGUAGES if spec.grammar is not None
}


# -------------------------------------------------------------- language specs


GO = TSSpec(
    grammar="go",
    defs_query=r"""
        (function_declaration name: (identifier) @name) @def.function

        (method_declaration
          receiver: (parameter_list
            (parameter_declaration
              name: (identifier)? @recv_name
              type: [
                (pointer_type (type_identifier) @recv_type)
                (type_identifier) @recv_type
              ]))
          name: (field_identifier) @name) @def.method

        (type_declaration
          (type_spec name: (type_identifier) @name type: (struct_type))) @def.class
        (type_declaration
          (type_spec name: (type_identifier) @name type: (interface_type))) @def.class

        (package_clause (package_identifier) @package)
    """,
    calls_query=r"""
        (call_expression function: (identifier) @callee) @call
        (call_expression
          function: (selector_expression
            operand: (identifier) @receiver
            field: (field_identifier) @callee)) @call
    """,
    imports_query=r"""
        (import_spec name: (package_identifier)? @alias
                     path: (interpreted_string_literal) @path)
    """,
    scope_types={
        "function_declaration": "name",
        "method_declaration": "name",
        "type_spec": "name",
    },
    class_types=frozenset(),  # Go's "class" is the receiver type, handled via @recv_type
    self_receivers=(),  # the receiver variable is per-method, matched via _Func.recv_name
    implicit_self=False,
    builtins=frozenset(
        {
            "len", "cap", "make", "new", "append", "copy", "delete", "panic", "recover",
            "print", "println", "close", "min", "max", "clear", "complex", "real", "imag",
        }
    ),
    import_style="go",
)


_JS_BUILTINS = frozenset(
    {
        "console", "JSON", "Math", "Object", "Array", "Promise", "parseInt", "parseFloat",
        "setTimeout", "setInterval", "clearTimeout", "clearInterval", "fetch", "require",
        "String", "Number", "Boolean", "Symbol", "Error", "TypeError", "Map", "Set", "Date",
        "RegExp", "structuredClone", "isNaN", "encodeURIComponent", "decodeURIComponent",
    }
)

_TS_CALLS_QUERY = r"""
    (call_expression function: (identifier) @callee) @call
    (call_expression
      function: (member_expression
        object: [(identifier) (this)] @receiver
        property: (property_identifier) @callee)) @call
    (new_expression constructor: (identifier) @callee) @call
"""

_TS_IMPORTS_QUERY = r"""
    (import_statement (import_clause [
        (identifier) @default_alias
        (named_imports (import_specifier
          name: (identifier) @symbol
          alias: (identifier)? @alias))
        (namespace_import (identifier) @ns_alias)
      ]) source: (string (string_fragment) @path))
    (export_statement source: (string (string_fragment) @path))
    (call_expression
      function: (identifier) @_req (#eq? @_req "require")
      arguments: (arguments (string (string_fragment) @path)))
"""

TYPESCRIPT = TSSpec(
    grammar="typescript",
    defs_query=r"""
        (function_declaration name: (identifier) @name) @def.function
        (generator_function_declaration name: (identifier) @name) @def.function
        (method_definition name: (property_identifier) @name) @def.function
        (class_declaration name: (type_identifier) @name) @def.class
        (abstract_class_declaration name: (type_identifier) @name) @def.class
        (interface_declaration name: (type_identifier) @name) @def.class
        (enum_declaration name: (identifier) @name) @def.class
        (lexical_declaration
          (variable_declarator
            name: (identifier) @name
            value: [(arrow_function) (function_expression)])) @def.function
    """,
    calls_query=_TS_CALLS_QUERY,
    imports_query=_TS_IMPORTS_QUERY,
    scope_types={
        "class_declaration": "name",
        "abstract_class_declaration": "name",
        "function_declaration": "name",
        "generator_function_declaration": "name",
        "method_definition": "name",
        "variable_declarator": "name",
    },
    class_types=frozenset({"class_declaration", "abstract_class_declaration"}),
    self_receivers=("this",),
    implicit_self=False,
    builtins=_JS_BUILTINS,
    import_style="relative_js",
)

# .tsx parses under the tsx grammar variant; the node types are the same.
TSX = replace(TYPESCRIPT, grammar="tsx")

JAVASCRIPT = TSSpec(
    grammar="javascript",
    defs_query=r"""
        (function_declaration name: (identifier) @name) @def.function
        (generator_function_declaration name: (identifier) @name) @def.function
        (method_definition name: (property_identifier) @name) @def.function
        (class_declaration name: (identifier) @name) @def.class
        (lexical_declaration
          (variable_declarator
            name: (identifier) @name
            value: [(arrow_function) (function_expression)])) @def.function
    """,
    calls_query=_TS_CALLS_QUERY,
    imports_query=_TS_IMPORTS_QUERY,
    scope_types={
        "class_declaration": "name",
        "function_declaration": "name",
        "generator_function_declaration": "name",
        "method_definition": "name",
        "variable_declarator": "name",
    },
    class_types=frozenset({"class_declaration"}),
    self_receivers=("this",),
    implicit_self=False,
    builtins=_JS_BUILTINS,
    import_style="relative_js",
)


JAVA = TSSpec(
    grammar="java",
    defs_query=r"""
        (method_declaration
          (modifiers (marker_annotation name: (identifier) @anno))?
          name: (identifier) @name) @def.function
        (constructor_declaration name: (identifier) @name) @def.function
        (class_declaration name: (identifier) @name) @def.class
        (interface_declaration name: (identifier) @name) @def.class
        (enum_declaration name: (identifier) @name) @def.class
        (record_declaration name: (identifier) @name) @def.class
    """,
    calls_query=r"""
        (method_invocation !object name: (identifier) @callee) @call
        (method_invocation object: (identifier) @receiver name: (identifier) @callee) @call
        (method_invocation object: (this) @receiver name: (identifier) @callee) @call
        (object_creation_expression type: (type_identifier) @callee) @call
    """,
    imports_query=r"""
        (import_declaration (scoped_identifier) @path)
    """,
    scope_types={
        "class_declaration": "name",
        "interface_declaration": "name",
        "enum_declaration": "name",
        "record_declaration": "name",
        "method_declaration": "name",
        "constructor_declaration": "name",
    },
    class_types=frozenset(
        {"class_declaration", "interface_declaration", "enum_declaration", "record_declaration"}
    ),
    self_receivers=("this",),
    implicit_self=True,  # bare foo() in a class body is almost always a method call
    builtins=frozenset(
        {
            "System", "Math", "Objects", "Arrays", "Collections", "List", "Map", "Set",
            "Optional", "String", "Integer", "Long", "Double", "Boolean", "Character",
            "Thread", "Stream", "Files", "Paths",
        }
    ),
    import_style="java",
    test_annotations=frozenset({"Test", "ParameterizedTest", "RepeatedTest", "TestFactory"}),
)

RUST = TSSpec(
    grammar="rust",
    defs_query=r"""
        (function_item name: (identifier) @name) @def.function
        (struct_item name: (type_identifier) @name) @def.class
        (enum_item name: (type_identifier) @name) @def.class
        (trait_item name: (type_identifier) @name) @def.class
        (union_item name: (type_identifier) @name) @def.class
    """,
    calls_query=r"""
        (call_expression function: (identifier) @callee) @call
        (call_expression
          function: (field_expression
            value: [(self) (identifier)] @receiver
            field: (field_identifier) @callee)) @call
        (call_expression
          function: (scoped_identifier
            path: (identifier) @receiver
            name: (identifier) @callee)) @call
    """,
    imports_query=r"""
        (mod_item name: (identifier) @modname !body)
        (use_declaration argument: (_) @use_path)
    """,
    scope_types={"impl_item": "type", "mod_item": "name", "function_item": "name"},
    class_types=frozenset({"impl_item"}),
    self_receivers=("self",),
    implicit_self=False,  # a bare call inside an impl is a free function, not a method
    builtins=frozenset({"drop", "Box", "Some", "None", "Ok", "Err", "Vec", "String"}),
    import_style="rust_mod",
    external_receivers=frozenset({"std", "core", "alloc"}),
)

_LIBC_BUILTINS = frozenset(
    {
        "printf", "fprintf", "sprintf", "snprintf", "scanf", "sscanf", "malloc", "calloc",
        "realloc", "free", "memcpy", "memmove", "memset", "memcmp", "strlen", "strcpy",
        "strncpy", "strcat", "strcmp", "strncmp", "strchr", "strstr", "strtol", "atoi",
        "fopen", "fclose", "fread", "fwrite", "fseek", "fgets", "fputs", "exit", "abort",
        "assert", "qsort", "bsearch", "abs", "sizeof",
    }
)

C = TSSpec(
    grammar="c",
    defs_query=r"""
        (function_definition
          declarator: (function_declarator declarator: (identifier) @name)) @def.function
        (function_definition
          declarator: (pointer_declarator
            (function_declarator declarator: (identifier) @name))) @def.function
        (struct_specifier name: (type_identifier) @name
                          body: (field_declaration_list)) @def.class
    """,
    calls_query=r"""
        (call_expression function: (identifier) @callee) @call
    """,
    imports_query=r"""
        (preproc_include path: (string_literal) @path)
    """,
    scope_types={},
    class_types=frozenset(),
    self_receivers=(),
    implicit_self=False,
    builtins=_LIBC_BUILTINS,
    import_style="include",
)

CPP = TSSpec(
    grammar="cpp",
    defs_query=r"""
        (function_definition
          declarator: (function_declarator declarator: (identifier) @name)) @def.function
        (function_definition
          declarator: (function_declarator declarator: (field_identifier) @name)) @def.function
        (function_definition
          declarator: (function_declarator
            declarator: (qualified_identifier
              scope: (namespace_identifier) @class_scope
              name: (identifier) @name))) @def.method
        (class_specifier name: (type_identifier) @name
                         body: (field_declaration_list)) @def.class
        (struct_specifier name: (type_identifier) @name
                          body: (field_declaration_list)) @def.class
    """,
    calls_query=r"""
        (call_expression function: (identifier) @callee) @call
        (call_expression
          function: (field_expression
            argument: [(this) (identifier)] @receiver
            field: (field_identifier) @callee)) @call
        (call_expression
          function: (qualified_identifier
            scope: (namespace_identifier) @receiver
            name: (identifier) @callee)) @call
    """,
    imports_query=r"""
        (preproc_include path: (string_literal) @path)
    """,
    scope_types={
        "class_specifier": "name",
        "struct_specifier": "name",
        "namespace_definition": "name",
    },
    class_types=frozenset({"class_specifier", "struct_specifier"}),
    self_receivers=("this",),
    implicit_self=True,
    builtins=_LIBC_BUILTINS,
    import_style="include",
    external_receivers=frozenset({"std"}),
)

RUBY = TSSpec(
    grammar="ruby",
    defs_query=r"""
        (method name: (identifier) @name) @def.function
        (singleton_method name: (identifier) @name) @def.function
        (class name: (constant) @name) @def.class
        (module name: (constant) @name) @def.class
    """,
    # Known gap, honestly held: a bare `step` with no parens, args, or receiver parses
    # as a plain identifier — Ruby's own parser cannot tell it from a local-variable
    # read without a symbol table. Those calls are invisible here (not counted, not
    # unresolved), the same category as pyast's unattributable module-level calls.
    calls_query=r"""
        (call receiver: (self) @receiver method: (identifier) @callee) @call
        (call receiver: (identifier) @receiver method: (identifier) @callee) @call
        (call receiver: (constant) @receiver method: (identifier) @callee) @call
        (call !receiver method: (identifier) @callee) @call
    """,
    imports_query=r"""
        (call
          method: (identifier) @_m (#eq? @_m "require_relative")
          arguments: (argument_list (string (string_content) @path)))
    """,
    scope_types={"class": "name", "module": "name", "method": "name"},
    class_types=frozenset({"class", "module"}),
    self_receivers=("self",),
    implicit_self=True,  # a bare call inside a class body is usually a method call
    builtins=frozenset(
        {
            "puts", "print", "p", "pp", "require", "require_relative", "raise", "loop",
            "attr_accessor", "attr_reader", "attr_writer", "lambda", "proc", "gets",
            "rand", "sleep", "include", "extend", "freeze", "format", "sprintf", "Integer",
            "Float", "Array", "Hash",
        }
    ),
    import_style="relative_file",
)

CSHARP = TSSpec(
    grammar="csharp",
    defs_query=r"""
        (method_declaration
          (attribute_list (attribute name: (identifier) @anno))?
          name: (identifier) @name) @def.function
        (constructor_declaration name: (identifier) @name) @def.function
        (local_function_statement name: (identifier) @name) @def.function
        (class_declaration name: (identifier) @name) @def.class
        (struct_declaration name: (identifier) @name) @def.class
        (interface_declaration name: (identifier) @name) @def.class
        (record_declaration name: (identifier) @name) @def.class
        (enum_declaration name: (identifier) @name) @def.class
    """,
    calls_query=r"""
        (invocation_expression function: (identifier) @callee) @call
        (invocation_expression
          function: (member_access_expression
            expression: (identifier) @receiver
            name: (identifier) @callee)) @call
        (invocation_expression
          function: (member_access_expression
            "this" @receiver
            name: (identifier) @callee)) @call
        (object_creation_expression type: (identifier) @callee) @call
    """,
    imports_query=r"""
        (using_directive (identifier) @_using)
    """,
    scope_types={
        "class_declaration": "name",
        "struct_declaration": "name",
        "interface_declaration": "name",
        "record_declaration": "name",
        "enum_declaration": "name",
        "method_declaration": "name",
        "constructor_declaration": "name",
        "local_function_statement": "name",
    },
    class_types=frozenset(
        {"class_declaration", "struct_declaration", "interface_declaration",
         "record_declaration"}
    ),
    self_receivers=("this",),
    implicit_self=True,
    builtins=frozenset(
        {
            "Console", "Math", "String", "Convert", "Enumerable", "Task", "List",
            "Dictionary", "Object", "Guid", "DateTime", "TimeSpan", "Environment",
            "Activator", "Encoding",
        }
    ),
    import_style="none",  # `using` names namespaces, not files — no v1 mapping (D018)
    test_annotations=frozenset({"Test", "Fact", "TestMethod", "Theory"}),
)

_PHP_STRING = (
    "[(string (string_content) @path) (encapsed_string (string_content) @path) "
    "(parenthesized_expression [(string (string_content) @path) "
    "(encapsed_string (string_content) @path)])]"
)

PHP = TSSpec(
    grammar="php",
    defs_query=r"""
        (function_definition name: (name) @name) @def.function
        (method_declaration name: (name) @name) @def.function
        (class_declaration name: (name) @name) @def.class
        (interface_declaration name: (name) @name) @def.class
        (trait_declaration name: (name) @name) @def.class
        (enum_declaration name: (name) @name) @def.class
    """,
    calls_query=r"""
        (function_call_expression function: (name) @callee) @call
        (member_call_expression
          object: (variable_name (name) @receiver)
          name: (name) @callee) @call
        (scoped_call_expression scope: (relative_scope) @receiver name: (name) @callee) @call
        (scoped_call_expression scope: (name) @receiver name: (name) @callee) @call
        (object_creation_expression (name) @callee) @call
    """,
    imports_query=(
        f"(require_expression {_PHP_STRING})\n"
        f"(require_once_expression {_PHP_STRING})\n"
        f"(include_expression {_PHP_STRING})\n"
        f"(include_once_expression {_PHP_STRING})\n"
    ),
    scope_types={
        "class_declaration": "name",
        "interface_declaration": "name",
        "trait_declaration": "name",
        "function_definition": "name",
        "method_declaration": "name",
    },
    class_types=frozenset({"class_declaration", "trait_declaration"}),
    self_receivers=("this", "self", "static"),  # $this->, self::, static::
    implicit_self=False,  # PHP method calls always spell the receiver
    builtins=frozenset(
        {
            "strlen", "count", "array_map", "array_filter", "array_merge", "array_keys",
            "array_values", "in_array", "implode", "explode", "sprintf", "printf",
            "str_replace", "substr", "strpos", "trim", "json_encode", "json_decode",
            "is_array", "is_string", "is_null", "isset", "empty", "var_dump", "print_r",
            "file_get_contents", "file_put_contents", "preg_match", "preg_replace",
            "intval", "strval", "floatval", "array_key_exists", "usort", "sort",
        }
    ),
    import_style="relative_file",
)


SPECS: dict[str, TSSpec] = {
    "go": GO,
    "typescript": TYPESCRIPT,
    "tsx": TSX,
    "javascript": JAVASCRIPT,
    "java": JAVA,
    "rust": RUST,
    "c": C,
    "cpp": CPP,
    "ruby": RUBY,
    "csharp": CSHARP,
    "php": PHP,
}
