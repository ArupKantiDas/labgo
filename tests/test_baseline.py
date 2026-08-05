"""Baseline scoring math — pure Python, no live database required for these."""

from __future__ import annotations

from labgo.baseline import CaseScore


def test_recall_is_share_of_expected_covered() -> None:
    score = CaseScore(seed="a.py", expected={"b.py", "c.py"}, predicted={"b.py"})
    assert score.recall == 0.5


def test_precision_is_share_of_prediction_correct() -> None:
    score = CaseScore(seed="a.py", expected={"b.py"}, predicted={"b.py", "z.py"})
    assert score.precision == 0.5


def test_precision_is_none_when_nothing_predicted() -> None:
    """None, not 0.

    An empty prediction has no precision, and averaging it as 0 would punish the
    same failure twice (it already tanks recall).
    """
    score = CaseScore(seed="a.py", expected={"b.py"}, predicted=set())
    assert score.precision is None


def test_recall_is_zero_when_expected_is_empty() -> None:
    score = CaseScore(seed="a.py", expected=set(), predicted={"b.py"})
    assert score.recall == 0.0


def test_hits_is_the_intersection() -> None:
    score = CaseScore(seed="a.py", expected={"b.py", "c.py"}, predicted={"b.py", "z.py"})
    assert score.hits == {"b.py"}
