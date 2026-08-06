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

from labgo.ingest.gitlog import leave_one_out_neighbors

if TYPE_CHECKING:
    from collections import Counter

    from neo4j import Driver

    from labgo.ingest.gitlog import EvalCase


def predict_impact_calls(driver: Driver, seed: str, *, hops: int = 2) -> set[str]:
    """Call-graph-only prediction: files reachable by `hops` CALLS hops from the seed.

    Structural — mined from source, not git history — so this is the one half of impact
    prediction with no leakage risk against a history-derived eval set (D012). `hops` is
    interpolated into the Cypher text rather than bound as a parameter — Neo4j does not
    allow parameterising a variable-length relationship's range — safe because `hops` is
    an internal int, never user-supplied text.
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

    predicted.discard(seed)
    return predicted


def predict_impact(
    driver: Driver, seed: str, *, hops: int = 2, use_cochange: bool = True
) -> set[str]:
    """Live prediction: call-graph plus Neo4j's global CO_CHANGED edges.

    For production/ad-hoc use (a one-off query, the impact viewer) — fine there, because
    there is no eval case whose own commit could leak back into its own answer. **Do not
    use this for benchmark scoring** when `use_cochange=True`: every eval case's commit
    is, by construction, also inside the co-change evidence window that produced these
    edges, so scoring against them gives the predictor partial credit for having seen the
    answer (D012). `score_baseline`'s `use_cochange` path uses `predict_impact_loo`
    instead, which does not have this problem.
    """
    predicted = predict_impact_calls(driver, seed, hops=hops)

    if use_cochange:
        with driver.session() as session:
            result = session.run(
                "MATCH (:File {id: $seed})-[:CO_CHANGED]-(other:File) "
                "RETURN DISTINCT other.id AS file",
                seed=seed,
            )
            predicted.update(r["file"] for r in result)
        predicted.discard(seed)

    return predicted


def predict_impact_loo(  # noqa: PLR0913  (each arg is a distinct piece of scoring
    #  provenance — driver, seed, hops, the evidence, which commit to exclude from it,
    #  and the threshold; collapsing them into a config object would hide D012's point)
    driver: Driver,
    seed: str,
    *,
    hops: int = 2,
    pairs: Counter[tuple[str, str]],
    exclude_files: set[str],
    min_count: int = 2,
) -> set[str]:
    """Leave-one-out prediction for benchmark scoring (D012).

    Call-graph via Neo4j (leak-free already, D012); co-change via `pairs` with the
    scored case's own commit subtracted first — see `leave_one_out_neighbors`.
    """
    predicted = predict_impact_calls(driver, seed, hops=hops)
    predicted |= leave_one_out_neighbors(pairs, seed, exclude_files, min_count=min_count)
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


def score_baseline(  # noqa: PLR0913  (mirrors predict_impact_loo's args, plus cases)
    driver: Driver,
    cases: list[EvalCase],
    *,
    hops: int = 2,
    use_cochange: bool = True,
    pairs: Counter[tuple[str, str]] | None = None,
    min_count: int = 2,
) -> tuple[BaselineResult, list[CaseScore]]:
    """Run the baseline over every case and aggregate. Returns per-case scores too.

    `use_cochange=True` requires `pairs` (from `benchmark.load_evidence`) so each case is
    scored leave-one-out (D012) — there is no honest way to include co-change without it,
    so this raises rather than silently falling back to the leaky global-edge path.
    """
    if use_cochange and pairs is None:
        raise ValueError(
            "use_cochange=True needs `pairs` (benchmark.load_evidence(bench_dir)) for "
            "leave-one-out scoring — D012. Pass pairs=, or use_cochange=False for the "
            "call-graph-only floor."
        )

    scores = [
        CaseScore(
            seed=case.seed,
            expected=set(case.expected),
            predicted=(
                predict_impact_loo(
                    driver,
                    case.seed,
                    hops=hops,
                    pairs=pairs,
                    exclude_files={case.seed, *case.expected},
                    min_count=min_count,
                )
                if use_cochange
                else predict_impact_calls(driver, case.seed, hops=hops)
            ),
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
