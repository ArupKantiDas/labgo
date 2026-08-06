"""Leave-one-out co-change scoring (D012) — pure Python, no live database required."""

from __future__ import annotations

from collections import Counter

from labgo.ingest.gitlog import leave_one_out_neighbors


def test_neighbor_survives_when_other_commits_also_contributed() -> None:
    """Three commits coupled a.py/b.py — excluding one still leaves count >= min_count."""
    pairs = Counter({("a.py", "b.py"): 3})
    neighbors = leave_one_out_neighbors(
        pairs, "a.py", exclude_files={"a.py", "b.py"}, min_count=2
    )
    assert neighbors == {"b.py"}


def test_neighbor_drops_when_its_only_support_is_the_excluded_commit() -> None:
    """This is D012's actual bug: without the fix, a case's own commit inflates its score."""
    pairs = Counter({("a.py", "b.py"): 2})
    neighbors = leave_one_out_neighbors(
        pairs, "a.py", exclude_files={"a.py", "b.py", "c.py"}, min_count=2
    )
    assert neighbors == set()


def test_pair_not_touching_excluded_commit_is_unaffected() -> None:
    """A pair whose files weren't both in the excluded commit loses nothing."""
    pairs = Counter({("a.py", "b.py"): 2})
    neighbors = leave_one_out_neighbors(pairs, "a.py", exclude_files={"a.py"}, min_count=2)
    assert neighbors == {"b.py"}


def test_only_pairs_touching_seed_are_returned() -> None:
    pairs = Counter({("a.py", "b.py"): 5, ("c.py", "d.py"): 5})
    neighbors = leave_one_out_neighbors(pairs, "a.py", exclude_files=set(), min_count=2)
    assert neighbors == {"b.py"}


def test_a_commit_can_only_subtract_its_own_single_contribution() -> None:
    """Count 3 from three different commits, one excluded -> 2 remain, still a neighbor."""
    pairs = Counter({("a.py", "b.py"): 3})
    neighbors = leave_one_out_neighbors(
        pairs, "a.py", exclude_files={"a.py", "b.py"}, min_count=3
    )
    assert neighbors == set()
    neighbors = leave_one_out_neighbors(
        pairs, "a.py", exclude_files={"a.py", "b.py"}, min_count=2
    )
    assert neighbors == {"b.py"}
