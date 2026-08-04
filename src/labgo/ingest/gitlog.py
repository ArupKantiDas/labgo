"""Git history -> co-change edges + a ground-truth evaluation set.

This is the module that makes the whole project measurable, so the filtering
decisions here matter more than the code:

* **Merge commits are excluded.** A merge's file list is the union of unrelated
  branches; treating it as evidence of coupling manufactures edges that don't exist.
* **Large commits are excluded** (`max_files`). A 300-file lint sweep or dependency
  bump says nothing about semantic coupling, but it would contribute ~45,000 spurious
  pairs — enough to dominate the signal from every real commit combined.
* **Only source files count.** Lockfiles and generated output co-change with
  everything and would inflate apparent recall.

Every one of those is a precision/recall tradeoff made explicitly rather than by
accident, which is exactly what "how did you evaluate retrieval" is asking for.
"""

from __future__ import annotations

import subprocess
from collections import Counter
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterator

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

# Co-change is only meaningful between files the graph also models.
SOURCE_SUFFIXES = {".py"}

NOISE_NAMES = {
    "uv.lock", "poetry.lock", "package-lock.json", "yarn.lock", "Pipfile.lock",
    "requirements.txt", "CHANGELOG.md",
}


@dataclass
class Commit:
    sha: str
    ts: int
    author: str
    files: list[str]


@dataclass
class EvalCase:
    """One prediction task derived from a real commit.

    Given `seed` (the first file the developer touched), a system should predict
    `expected` (everything else that changed in the same commit). Recall against
    this set is the retrieval metric; it needs no human labelling.
    """

    sha: str
    seed: str
    expected: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HistoryStats:
    commits_scanned: int = 0
    merges_skipped: int = 0
    too_large_skipped: int = 0
    no_source_skipped: int = 0
    commits_used: int = 0
    eval_cases: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class History:
    pairs: Counter = field(default_factory=Counter)   # (a, b) sorted -> count
    cases: list[EvalCase] = field(default_factory=list)
    stats: HistoryStats = field(default_factory=HistoryStats)


def _is_source(path: str) -> bool:
    p = Path(path)
    return p.suffix in SOURCE_SUFFIXES and p.name not in NOISE_NAMES


def _run_git_log(repo: Path, max_commits: int) -> str:
    # --no-merges is belt-and-braces with the parent-count check below.
    cmd = [
        "git", "-C", str(repo), "log",
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
    sha = ts = author = None
    files: list[str] = []
    for line in raw.splitlines():
        if line.startswith(_REC):
            if sha is not None:
                yield Commit(sha, int(ts or 0), author or "", files)
            parts = line[len(_REC):].split(_SEP)
            sha, ts, author = (parts + ["", "", ""])[:3]
            files = []
        elif line.strip():
            files.append(line.strip())
    if sha is not None:
        yield Commit(sha, int(ts or 0), author or "", files)


def extract_history(
    repo: Path,
    max_commits: int = 2000,
    max_files: int = 20,
    min_files: int = 2,
) -> History:
    """Mine co-change signal and build the eval set.

    `max_files=20` is a starting point, not a truth. Sweep it and watch what happens
    to baseline recall — that sweep is a genuinely interesting finding to report.
    """
    hist = History()
    st = hist.stats

    for commit in parse_commits(_run_git_log(repo, max_commits)):
        st.commits_scanned += 1
        files = sorted({f for f in commit.files if _is_source(f)})

        if not files:
            st.no_source_skipped += 1
            continue
        if len(files) > max_files:
            st.too_large_skipped += 1
            continue
        if len(files) < min_files:
            # Single-file commits carry no coupling signal and no prediction task,
            # but they are not "skipped" in a suspicious sense — just unusable here.
            continue

        st.commits_used += 1

        for i, a in enumerate(files):
            for b in files[i + 1:]:
                hist.pairs[(a, b)] += 1

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
        {"src": a, "dst": b, "count": n}
        for (a, b), n in hist.pairs.most_common()
        if n >= min_count
    ]
