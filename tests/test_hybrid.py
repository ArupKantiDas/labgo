"""Stage 3b scoring math — pure Python, no live Neo4j required for these."""

from __future__ import annotations

from labgo.baseline import CaseScore
from labgo.hybrid import aggregate_scores


def test_aggregate_averages_recall_and_precision() -> None:
    scores = [
        CaseScore(seed="a.py", expected={"b.py", "c.py"}, predicted={"b.py"}),
        CaseScore(seed="d.py", expected={"e.py"}, predicted={"e.py", "f.py"}),
    ]

    result = aggregate_scores(scores, method="vectors", params={"k": 10})

    assert result.method == "vectors"
    assert result.params == {"k": 10}
    assert result.n_cases == 2
    assert result.recall_mean == (0.5 + 1.0) / 2
    assert result.precision_mean == (1.0 + 0.5) / 2


def test_aggregate_hit_rate_counts_cases_with_any_recall() -> None:
    scores = [
        CaseScore(seed="a.py", expected={"b.py"}, predicted={"b.py"}),
        CaseScore(seed="c.py", expected={"d.py"}, predicted=set()),
    ]

    result = aggregate_scores(scores, method="hybrid", params={})

    assert result.hit_rate == 0.5
    assert result.empty_predictions == 1


def test_aggregate_precision_mean_ignores_empty_predictions() -> None:
    """Empty predictions have no precision (None) — they must not drag the mean to 0."""
    scores = [
        CaseScore(seed="a.py", expected={"b.py"}, predicted=set()),
        CaseScore(seed="c.py", expected={"d.py"}, predicted={"d.py"}),
    ]

    result = aggregate_scores(scores, method="vectors", params={"k": 10})

    assert result.precision_mean == 1.0


def test_aggregate_on_no_cases_returns_zeros_not_a_crash() -> None:
    result = aggregate_scores([], method="vectors", params={"k": 10})

    assert result.n_cases == 0
    assert result.recall_mean == 0.0
    assert result.precision_mean == 0.0
    assert result.hit_rate == 0.0
    assert result.empty_predictions == 0
