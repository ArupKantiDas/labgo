"""Stage 3b: vector-only and hybrid impact prediction, measured against the same benchmark.

D001's claim — "vector search cannot answer transitive impact" — was, until now, an
assertion backed by reasoning, not a number. `baseline.py` measured the call-graph side in
isolation (22.4% recall, D012); this module measures the vector side in isolation, and a
naive hybrid (set union of both), so the claim can be checked instead of just believed.

Deliberately **not** in `embed.py`: prediction here reuses each node's already-stored
`embedding` property via `db.index.vector.queryNodes` and needs no Voyage API call, so this
module has no `voyageai` dependency at all — only `neo4j`. Scoring the vector/hybrid
baseline never touches the network beyond Neo4j itself, however many times it's rerun.
`INDEX_NAME` is duplicated from `embed.py` rather than imported, specifically so importing
this module never pulls in `voyageai` transitively (see D014's note on that dependency's
weight).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from labgo.baseline import CaseScore, predict_impact

if TYPE_CHECKING:
    from neo4j import Driver

    from labgo.ingest.gitlog import EvalCase

# Kept in sync with embed.py's INDEX_NAME by hand, not by import — see module docstring.
INDEX_NAME = "node_embedding"


def predict_impact_vector(driver: Driver, seed: str, *, k: int = 10) -> set[str]:
    """Predict impact via cosine similarity over the seed file's already-embedded nodes.

    Returns the **k files** whose most similar node to any of the seed's functions/classes
    scores highest — "find code that resembles this seed's code", the librarian half of the
    WHY.md distinction, with no graph traversal.

    Ranks by each candidate *file*'s best matching score and caps the result at `k` files,
    rather than unioning every seed function's own top-`k` neighbors unfiltered: on a small
    corpus (httpx: 60 files), a seed file with many functions would otherwise contribute
    many separate top-k neighbor sets and sweep in a large fraction of the entire repo —
    inflating recall the same way predicting "most of the corpus" always would, not because
    the vectors found anything specific. A node is never its own neighbor (`node <> fn`),
    and same-file matches don't count as impact (resembling your own sibling isn't evidence
    of anything).
    """
    predicted: set[str] = set()
    with driver.session() as session:
        result = session.run(
            "MATCH (:File {id: $seed})-[:CONTAINS]->(fn) "
            "WHERE fn.embedding IS NOT NULL "
            "CALL db.index.vector.queryNodes($index, $k, fn.embedding) YIELD node, score "
            "WHERE node <> fn "
            "MATCH (f:File)-[:CONTAINS]->(node) "
            "WHERE f.id <> $seed "
            "WITH f, max(score) AS best "
            "ORDER BY best DESC LIMIT $k "
            "RETURN f.id AS file",
            seed=seed,
            index=INDEX_NAME,
            k=k,
        )
        predicted.update(r["file"] for r in result)
    return predicted


def predict_impact_hybrid(
    driver: Driver, seed: str, *, hops: int = 2, use_cochange: bool = True, k: int = 10
) -> set[str]:
    """Union of the call-graph (+ optional co-change) prediction and the vector prediction.

    Naive on purpose for a first measurement — no weighting, no re-ranking, just set union.
    If this doesn't beat the call-graph-only floor, a smarter blend isn't worth building
    yet; if it does, *how much* it helps is the number that justifies building one.
    """
    calls = predict_impact(driver, seed, hops=hops, use_cochange=use_cochange)
    vectors = predict_impact_vector(driver, seed, k=k)
    return calls | vectors


@dataclass
class RetrievalResult:
    """Aggregate score over a benchmark for a non-call-graph-only retrieval method.

    Mirrors `baseline.BaselineResult`'s fields but keeps its own config in `params` rather
    than named `hops`/`use_cochange` fields, since "vectors" and "hybrid" don't share a
    config shape with the call-graph-only method.
    """

    method: str  # "vectors" | "hybrid"
    params: dict[str, Any]
    n_cases: int
    recall_mean: float
    precision_mean: float
    hit_rate: float
    empty_predictions: int

    def to_dict(self) -> dict[str, Any]:
        """Serialise for logging alongside a DECISIONS.md entry."""
        return asdict(self)


def aggregate_scores(
    scores: list[CaseScore], *, method: str, params: dict[str, Any]
) -> RetrievalResult:
    """Reduce per-case scores to the same aggregate shape `baseline.py` reports."""
    n = len(scores)
    precisions = [s.precision for s in scores if s.precision is not None]
    return RetrievalResult(
        method=method,
        params=params,
        n_cases=n,
        recall_mean=sum(s.recall for s in scores) / n if n else 0.0,
        precision_mean=sum(precisions) / len(precisions) if precisions else 0.0,
        hit_rate=sum(1 for s in scores if s.recall > 0) / n if n else 0.0,
        empty_predictions=sum(1 for s in scores if not s.predicted),
    )


def score_vector_baseline(
    driver: Driver, cases: list[EvalCase], *, k: int = 10
) -> tuple[RetrievalResult, list[CaseScore]]:
    """Run vector-only prediction over every case and aggregate. Returns per-case scores too."""
    scores = [
        CaseScore(
            seed=case.seed,
            expected=set(case.expected),
            predicted=predict_impact_vector(driver, case.seed, k=k),
        )
        for case in cases
    ]
    return aggregate_scores(scores, method="vectors", params={"k": k}), scores


def score_hybrid_baseline(
    driver: Driver,
    cases: list[EvalCase],
    *,
    hops: int = 2,
    use_cochange: bool = True,
    k: int = 10,
) -> tuple[RetrievalResult, list[CaseScore]]:
    """Run the hybrid (call-graph | vector) prediction over every case and aggregate."""
    scores = [
        CaseScore(
            seed=case.seed,
            expected=set(case.expected),
            predicted=predict_impact_hybrid(
                driver, case.seed, hops=hops, use_cochange=use_cochange, k=k
            ),
        )
        for case in cases
    ]
    params = {"hops": hops, "use_cochange": use_cochange, "k": k}
    return aggregate_scores(scores, method="hybrid", params=params), scores
