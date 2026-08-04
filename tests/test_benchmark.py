"""Benchmark filtering and the corpus-pinning guard (D008)."""

from __future__ import annotations

import pytest

from labgo.benchmark import CorpusMismatch, filter_answerable, verify_corpus
from labgo.ingest.gitlog import EvalCase


def _case(seed: str, *expected: str) -> EvalCase:
    return EvalCase(sha="deadbeef", seed=seed, expected=list(expected))


def test_drops_case_whose_seed_was_deleted() -> None:
    """The exact failure that made 259 httpx cases unanswerable."""
    live = {"a.py", "b.py"}
    kept, report = filter_answerable([_case("gone.py", "a.py")], live)

    assert kept == []
    assert report.dropped_dead_seed == 1
    assert report.dropped_dead_expected == 0


def test_drops_case_with_any_deleted_expected_file() -> None:
    """Partial credit is not on offer: one dead expected file makes the case unscorable."""
    live = {"a.py", "b.py"}
    kept, report = filter_answerable([_case("a.py", "b.py", "gone.py")], live)

    assert kept == []
    assert report.dropped_dead_expected == 1


def test_keeps_fully_live_case() -> None:
    live = {"a.py", "b.py", "c.py"}
    kept, report = filter_answerable([_case("a.py", "b.py", "c.py")], live)

    assert len(kept) == 1
    assert report.after == 1
    assert report.dropped_pct == 0.0


def test_report_arithmetic() -> None:
    live = {"a.py"}
    cases = [_case("a.py"), _case("dead.py", "a.py"), _case("a.py", "dead.py")]
    kept, report = filter_answerable(cases, live)

    assert report.before == 3
    assert report.after == len(kept) == 1
    assert report.dropped_dead_seed + report.dropped_dead_expected == 2
    assert report.dropped_pct == pytest.approx(66.7, abs=0.1)


def test_empty_input_does_not_divide_by_zero() -> None:
    kept, report = filter_answerable([], {"a.py"})
    assert kept == []
    assert report.dropped_pct == 0.0


def test_corpus_mismatch_raises_rather_than_scoring_wrong_world(tmp_path) -> None:
    """A drifted corpus must fail loudly.

    Scoring against the wrong commit returns a plausible number that means nothing,
    and a plausible wrong answer is far more dangerous than an error.
    """
    import subprocess

    repo = tmp_path / "corpus"
    repo.mkdir()
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    (repo / "f.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "one"], check=True, capture_output=True)

    manifest = {"name": "t", "corpus": {"sha": "0" * 40}}
    with pytest.raises(CorpusMismatch) as exc:
        verify_corpus(manifest, repo)

    # The error has to be actionable, not just true.
    assert "git -C" in str(exc.value) and "checkout" in str(exc.value)
