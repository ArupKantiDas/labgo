"""Git history -> co-change edges + a ground-truth evaluation set.

This is the module that makes the whole project measurable, so the filtering
decisions here matter more than the code:

* **Merge commits are excluded.** A merge's file list is the union of unrelated
  branches; treating it as evidence of coupling manufactures edges that don't exist.
* **Large commits are excluded**, under two separate caps (D010). Pairs grow as N²,
  so on httpx five commits out of 620 contributed 30% of all co-change evidence. A
  300-file lint sweep says nothing about semantic coupling but would swamp every real
  commit combined.
* **Only source files count.** Lockfiles and generated output co-change with
  everything and would inflate apparent recall.

Every one of those is a precision/recall tradeoff made explicitly rather than by
accident, which is exactly what "how did you evaluate retrieval" is asking for.
"""

from __future__ import annotations

import subprocess
from collections import Counter
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from labgo.ingest.languages import ALL_NOISE_NAMES, ALL_SOURCE_SUFFIXES

# Two separate traps, both hit while building this:
#
# 1. argv cannot contain a null byte (execve strings are null-terminated), so
#    `--format=...\x00...` raises ValueError. The fix is git's own `%x1f` code:
#    the argv holds the literal text "%x1f" and *git* emits the byte.
# 2. `str.splitlines()` splits on \x1c, \x1d, AND \x1e — so a \x1e record marker
#    is silently consumed as a line break and no line ever startswith() it.
#    \x1f (unit separator) is NOT a splitlines boundary, so it is safe for fields.
#
# Hence: literal text marker for records, %x1f for fields.
_REC = "@@C@@"
_SEP = "\x1f"
_FMT = "@@C@@%H%x1f%at%x1f%an"

# Co-change is only meaningful between files the graph also models. Both sets come
# from the language registry so ingest and history mining agree on what "source" means.
# Deliberately the *full* registry set, not "extensions detected at HEAD" (D018):
# history references files that no longer exist, and the eval set must be deterministic
# — not dependent on ingest having run first or on what HEAD happens to contain.
SOURCE_SUFFIXES = ALL_SOURCE_SUFFIXES

NOISE_NAMES = ALL_NOISE_NAMES


@dataclass
class Commit:
    """One non-merge commit and the files it touched."""

    sha: str
    ts: int
    author: str
    files: list[str]


@dataclass
class EvalCase:
    """One prediction task derived from a real commit.

    Given `seed` — *one* file from the commit — a system should predict `expected`,
    the other files that changed alongside it. Recall against this is the retrieval
    metric, and it needs no human labelling.

    Two things `seed` is **not**, both worth stating so nobody infers them:

    * Not "the file the developer touched first". Git records no edit order; that
      information does not exist in history. It is the alphabetically-first source
      file, chosen purely so the set is deterministic.
    * Not a clean causal label. `expected` is *what changed together*, not *what had
      to change*, so it includes bundled unrelated edits and mechanical sweeps. The
      key therefore contains false positives and no system can score near 100% (D009).
      This is why relative comparison against a baseline is the meaningful measure and
      absolute accuracy claims are not.
    """

    sha: str
    seed: str
    expected: list[str]

    def to_dict(self) -> dict[str, Any]:
        """Serialise for the benchmark cases file."""
        return asdict(self)


@dataclass
class HistoryStats:
    """Why commits were kept or dropped — the audit trail for the filters above."""

    commits_scanned: int = 0
    merges_skipped: int = 0
    too_large_skipped: int = 0  # over evidence_max_files -> no pairs, no case
    too_large_for_eval: int = 0  # over eval_max_files -> pairs kept, no case
    no_source_skipped: int = 0
    commits_used: int = 0  # contributed co-change evidence
    eval_cases: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialise for the benchmark manifest."""
        return asdict(self)


@dataclass
class History:
    """Mined co-change evidence plus the eval cases derived from the same commits."""

    pairs: Counter = field(default_factory=Counter)  # (a, b) sorted -> count
    cases: list[EvalCase] = field(default_factory=list)
    stats: HistoryStats = field(default_factory=HistoryStats)


def _is_source(path: str) -> bool:
    p = Path(path)
    # .lower(): .CPP / .Rb occur in the wild; the registry stores lowercase suffixes.
    return p.suffix.lower() in SOURCE_SUFFIXES and p.name not in NOISE_NAMES


def _run_git_log(repo: Path, max_commits: int) -> str:
    # --no-merges is belt-and-braces with the parent-count check below.
    cmd = [
        "git",
        "-C",
        str(repo),
        "log",
        "--no-merges",
        f"--max-count={max_commits}",
        f"--format={_FMT}",
        "--name-only",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"git log failed: {proc.stderr.strip()}")
    return proc.stdout


def parse_commits(raw: str) -> Iterator[Commit]:
    """Parse `git log --name-only` output into commits.

    Separated from `_run_git_log` so it can be tested without a repository — which
    matters, because this parser once returned zero commits silently (D006).
    """
    sha = ts = author = None
    files: list[str] = []
    for line in raw.splitlines():
        if line.startswith(_REC):
            if sha is not None:
                yield Commit(sha, int(ts or 0), author or "", files)
            parts = line[len(_REC) :].split(_SEP)
            sha, ts, author = [*parts, "", "", ""][:3]
            files = []
        elif line.strip():
            files.append(line.strip())
    if sha is not None:
        yield Commit(sha, int(ts or 0), author or "", files)


def extract_history(
    repo: Path,
    max_commits: int = 2000,
    evidence_max_files: int = 20,
    eval_max_files: int = 10,
    min_files: int = 2,
) -> History:
    """Mine co-change signal and build the eval set.

    **Two separate size caps, deliberately (D010).** They were one parameter until the
    coupling turned out to be a mild form of leakage: tuning the threshold to improve the
    evidence silently changed the exam it was being scored against.

    * `evidence_max_files` (looser) — commits above this contribute no co-change pairs.
      Evidence tolerates noise because `min_count>=2` filters it downstream.
    * `eval_max_files` (tighter) — commits above this produce no eval case. Exam labels
      cannot be de-noised after the fact, so they need focused commits.

    Neither default is derived. Sweep them against baseline recall and pick from the
    curve; the measured distribution that motivated the split is in D010.
    """
    hist = History()
    st = hist.stats

    for commit in parse_commits(_run_git_log(repo, max_commits)):
        st.commits_scanned += 1
        files = sorted({f for f in commit.files if _is_source(f)})

        if not files:
            st.no_source_skipped += 1
            continue
        if len(files) < min_files:
            # Single-file commits carry no coupling signal and no prediction task,
            # but they are not "skipped" in a suspicious sense — just unusable here.
            continue
        if len(files) > evidence_max_files:
            st.too_large_skipped += 1
            continue

        st.commits_used += 1

        for i, a in enumerate(files):
            for b in files[i + 1 :]:
                hist.pairs[(a, b)] += 1

        # Exam questions are held to the stricter cap.
        if len(files) > eval_max_files:
            st.too_large_for_eval += 1
            continue

        # The seed is the alphabetically-first file purely for determinism. Git does
        # not record edit order, so "the file the developer touched first" is not
        # recoverable — worth stating plainly rather than implying we know it.
        hist.cases.append(EvalCase(sha=commit.sha, seed=files[0], expected=files[1:]))

    st.eval_cases = len(hist.cases)
    return hist


def co_change_edges(hist: History, min_count: int = 2) -> list[dict[str, Any]]:
    """Pairs seen together at least `min_count` times.

    `min_count=1` would admit every coincidental pairing; 2 is the cheapest filter
    that requires a repeated observation.
    """
    return [
        {"src": a, "dst": b, "count": n} for (a, b), n in hist.pairs.most_common() if n >= min_count
    ]


def leave_one_out_neighbors(
    pairs: Counter[tuple[str, str]], seed: str, exclude_files: set[str], *, min_count: int = 2
) -> set[str]:
    """Co-change neighbors of `seed`, with one commit's own contribution subtracted first.

    Fixes D012: scoring a case mined from commit C must not let C itself count as
    co-change evidence for predicting C. `pairs` is the *raw*, unfiltered aggregate
    (every count, not just `min_count`-and-above) — filtering first would make a pair
    that only clears the threshold *because of* the excluded commit look like it never
    existed, when the correct behaviour is to drop it from the leave-one-out view but
    leave it alone for every other case.

    `exclude_files` is the excluded commit's own filtered+sorted file list (for an
    `EvalCase`, that's exactly `{case.seed, *case.expected}` — the two are the same set
    by construction, see `EvalCase`'s docstring). A commit contributes at most 1 to any
    pair (files within one commit form a set, no repeats), so a pair with both endpoints
    in `exclude_files` had exactly 1 subtracted, never more.

    O(len(pairs)) per call — a full scan, not indexed. Fine at httpx scale (~14k pairs x
    236 cases is sub-second); the honest number matters more than the constant factor,
    and D012 already flagged this fix as real added cost, not a one-line change.
    """
    neighbors: set[str] = set()
    for (a, b), count in pairs.items():
        if seed not in (a, b):
            continue
        other = b if a == seed else a
        adjusted = count - 1 if other in exclude_files else count
        if adjusted >= min_count:
            neighbors.add(other)
    return neighbors
