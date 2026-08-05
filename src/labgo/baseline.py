"""Deterministic Cypher impact prediction — no LLM, no ranking, just graph traversal.

This is Stage 2's actual deliverable (see README status table): a number, measured
*before* any agent touches the problem. Every later stage (vector search, LangGraph
agents) has to beat this or it did not earn its complexity (D001).

Prediction for a seed file is the union of two independent signals, kept separate
rather than blended (same reasoning as the impact viewer):

* **call graph** — files containing functions that call (transitively, up to `hops`)
  any function defined in the seed file.
* **co-change** — files that have historically changed alongside the seed file
  (D002), which catches real coupling the call graph misses (only 27.2% of calls
  resolve statically on httpx — D004).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from neo4j import Driver

    from labgo.ingest.gitlog import EvalCase


def predict_impact(
    driver: Driver, seed: str, *, hops: int = 2, use_cochange: bool = True
) -> set[str]:
    """Predict the impact set for one seed file. `hops` must be >= 1.

    The hop count is interpolated into the Cypher text rather than bound as a
    parameter — Neo4j does not allow parameterising a variable-length relationship's
    range. Safe here because `hops` is an internal int, never user-supplied text.
    """
    if hops < 1:
        raise ValueError("hops must be >= 1")

    predicted: set[str] = set()
    with driver.session() as session:
        result = session.run(
            f"MATCH (:File {{id: $seed}})-[:CONTAINS]->(fn:Function) "
            f"MATCH (caller:Function)-[:CALLS*1..{hops}]->(fn) "
            f"MATCH (callerFile:File)-[:CONTAINS]->(caller) "
            f"RETURN DISTINCT callerFile.id AS file",
            seed=seed,
        )
        predicted.update(r["file"] for r in result)

        if use_cochange:
            result = session.run(
                "MATCH (:File {id: $seed})-[:CO_CHANGED]-(other:File) "
                "RETURN DISTINCT other.id AS file",
                seed=seed,
            )
            predicted.update(r["file"] for r in result)

    predicted.discard(seed)
    return predicted


@dataclass
class CaseScore:
    """One case's prediction against its ground truth."""

    seed: str
    expected: set[str]
    predicted: set[str]

    @property
    def hits(self) -> set[str]:
        """Correctly predicted files."""
        return self.expected & self.predicted

    @property
    def recall(self) -> float:
        """Share of the ground truth this case's prediction covered."""
        return 0.0 if not self.expected else len(self.hits) / len(self.expected)

    @property
    def precision(self) -> float | None:
        """Share of the prediction that was correct. `None` when nothing was predicted."""
        return None if not self.predicted else len(self.hits) / len(self.predicted)


@dataclass
class BaselineResult:
    """Aggregate score over a benchmark, plus the config it was measured under."""

    hops: int
    use_cochange: bool
    n_cases: int
    recall_mean: float
    precision_mean: float
    hit_rate: float  # fraction of cases where recall > 0
    empty_predictions: int  # cases where the baseline predicted nothing at all

    def to_dict(self) -> dict[str, Any]:
        """Serialise for logging alongside a DECISIONS.md entry."""
        return asdict(self)


def score_baseline(
    driver: Driver, cases: list[EvalCase], *, hops: int = 2, use_cochange: bool = True
) -> tuple[BaselineResult, list[CaseScore]]:
    """Run the baseline over every case and aggregate. Returns per-case scores too."""
    scores = [
        CaseScore(
            seed=case.seed,
            expected=set(case.expected),
            predicted=predict_impact(driver, case.seed, hops=hops, use_cochange=use_cochange),
        )
        for case in cases
    ]

    n = len(scores)
    precisions = [s.precision for s in scores if s.precision is not None]

    result = BaselineResult(
        hops=hops,
        use_cochange=use_cochange,
        n_cases=n,
        recall_mean=sum(s.recall for s in scores) / n if n else 0.0,
        precision_mean=sum(precisions) / len(precisions) if precisions else 0.0,
        hit_rate=sum(1 for s in scores if s.recall > 0) / n if n else 0.0,
        empty_predictions=sum(1 for s in scores if not s.predicted),
    )
    return result, scores
