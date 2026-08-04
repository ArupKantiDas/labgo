"""Graph node and edge types.

Deliberately plain dataclasses rather than a Neo4j-aware ORM: the extractor should
be testable and runnable with no database, and the graph schema should be readable
in one file. Persistence is a separate concern (see `labgo.graph`).
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class NodeKind(str, Enum):
    FILE = "File"
    FUNCTION = "Function"
    CLASS = "Class"


class EdgeKind(str, Enum):
    CONTAINS = "CONTAINS"          # File -> Function | Class
    CALLS = "CALLS"                # Function -> Function
    IMPORTS = "IMPORTS"            # File -> File
    TESTS = "TESTS"                # Function(test) -> Function
    CO_CHANGED = "CO_CHANGED"      # File -> File  (from git history)


class Confidence(str, Enum):
    """How much to trust a CALLS edge.

    Python call resolution from a static AST is inherently approximate — dynamic
    dispatch, monkey-patching, and `getattr` calls are unresolvable in principle.
    Rather than pretend otherwise, every CALLS edge carries its confidence so
    downstream precision/recall can be reported per tier.
    """

    EXACT = "exact"      # resolved via an explicit import to a known definition
    SELF = "self"        # self.foo() / cls.foo() resolved within the enclosing class
    LOCAL = "local"      # resolved to a definition in the same file
    HEURISTIC = "heuristic"  # unique name match somewhere in the repo
    # (unresolved calls are not emitted as edges; they are counted as a metric)


@dataclass(frozen=True)
class Node:
    id: str                 # stable unique key, e.g. "pkg/mod.py::MyClass.method"
    kind: NodeKind
    name: str
    path: str
    lineno: int | None = None
    end_lineno: int | None = None
    props: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        return d


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    kind: EdgeKind
    props: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        return d


@dataclass
class ExtractionStats:
    """Metrics reported after every ingest run.

    These are not decoration — they are the retrieval baseline. But the *denominator*
    has to be right to mean anything: calls to builtins (`len`, `isinstance`) and to
    the stdlib were never repo-internal functions, so counting them as "unresolved"
    understates resolution badly (measured on httpx: 20% vs 71% for the same graph).

    So `external_calls` is tracked separately and excluded from the rate. The honest
    metric is: of the calls that *could* have been internal, how many did we resolve?
    """

    files_parsed: int = 0
    files_failed: int = 0
    functions: int = 0
    classes: int = 0
    total_calls: int = 0
    external_calls: int = 0       # builtin / stdlib / third-party — out of scope
    resolved_exact: int = 0
    resolved_self: int = 0
    resolved_local: int = 0
    resolved_heuristic: int = 0
    unresolved_calls: int = 0

    @property
    def resolved(self) -> int:
        return (
            self.resolved_exact
            + self.resolved_self
            + self.resolved_local
            + self.resolved_heuristic
        )

    @property
    def in_scope_calls(self) -> int:
        """Call sites that could plausibly target a repo-internal function."""
        return self.total_calls - self.external_calls

    @property
    def resolution_rate(self) -> float:
        if self.in_scope_calls == 0:
            return 0.0
        return self.resolved / self.in_scope_calls

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["in_scope_calls"] = self.in_scope_calls
        d["resolved"] = self.resolved
        d["resolution_rate"] = round(self.resolution_rate, 4)
        return d


@dataclass
class Graph:
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    stats: ExtractionStats = field(default_factory=ExtractionStats)

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "stats": self.stats.to_dict(),
        }
