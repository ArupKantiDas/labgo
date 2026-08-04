"""Stage 1 tests.

Note the emphasis: D006 records a bug where the git parser silently returned zero
commits and looked plausible. So these assert on *non-emptiness* and on the metric
invariants, not just on happy-path shapes.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from labgo.ingest.gitlog import parse_commits
from labgo.ingest.models import EdgeKind, NodeKind
from labgo.ingest.pyast import extract_repo


def _write(root: Path, rel: str, src: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(src), encoding="utf-8")


def test_resolves_local_import_and_self_calls(tmp_path: Path) -> None:
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
        "pkg/core.py",
        """
        from pkg.util import helper

        class Engine:
            def run(self):
                return self.step() + helper()

            def step(self):
                return 2
    """,
    )

    g = extract_repo(tmp_path)
    calls = {(e.src, e.dst) for e in g.edges if e.kind is EdgeKind.CALLS}

    assert ("pkg/core.py::Engine.run", "pkg/util.py::helper") in calls, "import call missed"
    assert ("pkg/core.py::Engine.run", "pkg/core.py::Engine.step") in calls, "self.* call missed"
    assert g.stats.resolved_self >= 1
    assert ("pkg/core.py", "pkg/util.py") in {
        (e.src, e.dst) for e in g.edges if e.kind is EdgeKind.IMPORTS
    }


def test_builtins_excluded_from_denominator(tmp_path: Path) -> None:
    """D005: counting len()/isinstance() as unresolved understated resolution."""
    _write(
        tmp_path,
        "m.py",
        """
        def f(xs):
            return len(xs) + int(isinstance(xs, list))
    """,
    )

    g = extract_repo(tmp_path)
    assert g.stats.external_calls == 3, "len/int/isinstance should be external"
    assert g.stats.in_scope_calls == 0
    assert g.stats.resolution_rate == 0.0  # 0/0 must not divide by zero


def test_shadowed_builtin_is_not_external(tmp_path: Path) -> None:
    """A repo-defined `list()` is internal, however much it looks like a builtin."""
    _write(
        tmp_path,
        "m.py",
        """
        def list():
            return []

        def g():
            return list()
    """,
    )

    g = extract_repo(tmp_path)
    assert g.stats.external_calls == 0
    assert ("m.py::g", "m.py::list") in {
        (e.src, e.dst) for e in g.edges if e.kind is EdgeKind.CALLS
    }


def test_test_functions_emit_tests_edges(tmp_path: Path) -> None:
    _write(tmp_path, "app.py", "def target():\n    return 1\n")
    _write(
        tmp_path,
        "tests/test_app.py",
        """
        from app import target

        def test_target():
            assert target() == 1
    """,
    )

    g = extract_repo(tmp_path)
    assert any(e.kind is EdgeKind.TESTS for e in g.edges)
    fn = {n.id: n for n in g.nodes if n.kind is NodeKind.FUNCTION}
    assert fn["tests/test_app.py::test_target"].props["is_test"] is True


def test_syntax_errors_are_counted_not_fatal(tmp_path: Path) -> None:
    _write(tmp_path, "ok.py", "def a():\n    return 1\n")
    _write(tmp_path, "broken.py", "def (((\n")

    g = extract_repo(tmp_path)
    assert g.stats.files_parsed == 1
    assert g.stats.files_failed == 1


def test_git_log_parser_handles_the_real_format() -> None:
    r"""D006: \x1e as a record marker was silently eaten by splitlines()."""
    raw = "@@C@@abc123\x1f1700000000\x1fAda\na.py\nb.py\n@@C@@def456\x1f1700000001\x1fGrace\nc.py\n"
    commits = list(parse_commits(raw))

    assert len(commits) == 2, "record marker must survive splitlines()"
    assert commits[0].sha == "abc123"
    assert commits[0].author == "Ada"
    assert commits[0].files == ["a.py", "b.py"]
    assert commits[1].files == ["c.py"]


def test_git_log_parser_rejects_silent_emptiness() -> None:
    """The failure mode from D006: plausible input, zero output, no error."""
    assert list(parse_commits("")) == []
    # But a well-formed record must never parse to nothing.
    assert len(list(parse_commits("@@C@@s\x1f1\x1fA\nf.py\n"))) == 1
