"""Pinned, reproducible benchmarks.

A score is only meaningful if four things are fixed together (D008):

1. the **corpus**, at an exact commit — the world the system sees
2. the **eval set** — the questions
3. the **extraction parameters** — change `eval_max_files` and you have a different exam
4. **temporal consistency** between (1) and (2)

Point 4 is the one that bites. Questions are mined from commits across all of history,
but the code graph is built from the corpus at one commit. Files referenced by old
commits may since have been deleted or renamed — the system cannot name a file that is
not in its map. On httpx that made 278 of 610 cases unanswerable *by construction*, and
the resulting score would have read as the system underperforming.

So a benchmark here is a directory containing the filtered cases plus a manifest that
records exactly what produced them. `verify_corpus()` refuses to score against the
wrong world rather than silently returning a wrong number.
"""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from labgo.ingest.gitlog import EvalCase


class CorpusMismatchError(RuntimeError):
    """The corpus is not at the commit this benchmark was built against."""


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def corpus_sha(repo: Path) -> str:
    """Current HEAD of the corpus."""
    return _git(repo, "rev-parse", "HEAD")


def files_at(repo: Path, sha: str) -> set[str]:
    """Files present in the tree at `sha`.

    Read from git rather than the filesystem on purpose: the working tree may be dirty,
    or checked out somewhere else entirely. The tree at the commit is the ground truth
    for what the system could possibly know about.
    """
    out = _git(repo, "ls-tree", "-r", "--name-only", sha)
    return set(out.splitlines())


@dataclass
class FilterReport:
    """What the answerability filter kept, dropped, and why."""

    before: int
    after: int
    dropped_dead_seed: int
    dropped_dead_expected: int

    @property
    def dropped_pct(self) -> float:
        """Share of raw cases removed as unanswerable."""
        return 0.0 if self.before == 0 else 100 * (self.before - self.after) / self.before

    def to_dict(self) -> dict[str, Any]:
        """Serialise into the manifest, carrying the rule and its bias with the numbers."""
        return {
            "rule": "every file (seed and all expected) must exist in the corpus "
            "tree at corpus.sha",
            "reason": "D008 — temporal drift; cases referencing deleted or renamed "
            "files are unanswerable",
            "known_bias": (
                "survivorship — only exercises code that survived to the pinned "
                "commit, which differs systematically from code since deleted"
            ),
            "cases_before": self.before,
            "cases_after": self.after,
            "dropped_because_seed_gone": self.dropped_dead_seed,
            "dropped_because_expected_gone": self.dropped_dead_expected,
            "dropped_pct": round(self.dropped_pct, 1),
        }


def filter_answerable(cases: list[EvalCase], live: set[str]) -> tuple[list[EvalCase], FilterReport]:
    """Keep only cases every one of whose files exists at the pinned commit."""
    kept: list[EvalCase] = []
    dead_seed = dead_expected = 0

    for c in cases:
        if c.seed not in live:
            dead_seed += 1
            continue
        if any(e not in live for e in c.expected):
            dead_expected += 1
            continue
        kept.append(c)

    return kept, FilterReport(
        before=len(cases),
        after=len(kept),
        dropped_dead_seed=dead_seed,
        dropped_dead_expected=dead_expected,
    )


def write_benchmark(  # noqa: PLR0913  (each argument is a distinct provenance field;
    #  bundling them into a config object would hide what gets recorded)
    *,
    out_dir: Path,
    name: str,
    repo: Path,
    cases: list[EvalCase],
    report: FilterReport,
    extraction: dict[str, Any],
    pairs: Counter[tuple[str, str]],
) -> Path:
    """Write cases, the manifest, and the raw co-change evidence (D012).

    `pairs` is the *unfiltered* aggregate from `extract_history` — every count, not just
    `min_count`-and-above — pinned alongside the cases so `labgo baseline --cochange` can
    later subtract each case's own commit before scoring it (leave-one-out, D012). Without
    this, only the already-`min_count`-filtered `data/cochange.json` would be available,
    which is too lossy to subtract a single commit's contribution from correctly.
    """
    sha = corpus_sha(repo)
    try:
        url = _git(repo, "config", "--get", "remote.origin.url")
    except RuntimeError:
        url = ""
    try:
        described = _git(repo, "describe", "--tags", "--always")
    except RuntimeError:
        described = sha[:12]

    bench = out_dir / name
    bench.mkdir(parents=True, exist_ok=True)

    manifest = {
        "name": name,
        "corpus": {"url": url, "sha": sha, "describe": described},
        "extraction": extraction,
        "filter": report.to_dict(),
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "note": (
            "Committed to version control on purpose. Regenerating this changes the exam; "
            "do it as a logged decision, never as a side effect of updating the corpus."
        ),
    }

    (bench / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (bench / "cases.json").write_text(
        json.dumps([c.to_dict() for c in cases], indent=2) + "\n", encoding="utf-8"
    )
    (bench / "evidence.json").write_text(
        json.dumps({"pairs": [[a, b, n] for (a, b), n in pairs.most_common()]}, indent=2) + "\n",
        encoding="utf-8",
    )
    return bench


def load_benchmark(bench_dir: Path) -> tuple[dict[str, Any], list[EvalCase]]:
    """Read a pinned benchmark back off disk."""
    manifest = json.loads((bench_dir / "manifest.json").read_text(encoding="utf-8"))
    raw = json.loads((bench_dir / "cases.json").read_text(encoding="utf-8"))
    cases = [EvalCase(sha=c["sha"], seed=c["seed"], expected=c["expected"]) for c in raw]
    return manifest, cases


def load_evidence(bench_dir: Path) -> Counter[tuple[str, str]]:
    """Read a benchmark's raw co-change pairs back — the input to leave-one-out (D012)."""
    raw = json.loads((bench_dir / "evidence.json").read_text(encoding="utf-8"))
    return Counter({(a, b): n for a, b, n in raw["pairs"]})


def verify_corpus(manifest: dict[str, Any], repo: Path) -> None:
    """Fail loudly if the corpus is not at the pinned commit.

    Scoring against a drifted corpus produces a number that looks fine and means nothing.
    A wrong answer is recoverable; a plausible wrong answer is not.
    """
    expected = manifest["corpus"]["sha"]
    actual = corpus_sha(repo)
    if actual != expected:
        raise CorpusMismatchError(
            f"corpus is at {actual[:12]}, benchmark '{manifest['name']}' was built "
            f"against {expected[:12]}.\n"
            f"Scores would not be comparable. Fix with:\n"
            f"    git -C {repo} checkout {expected}"
        )
