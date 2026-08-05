# Decision log

Append-only. Every entry: what was decided, what the alternatives were, what evidence
drove it, and what later changed it.

This file exists because the AI Architect interview question that separates candidates is
*"what went wrong and what architectural decisions did you change?"* — and that is only
answerable if you wrote it down while it was still annoying.

---

## D001 — Graph must be load-bearing, not decorative
**Date:** 2026-08-04 · **Status:** accepted

"Chat with your codebase" needs no graph; a vector store answers it. So the project is
scoped to a question a vector store *provably cannot* answer: **transitive impact**
("if I change X, what breaks?"). That is multi-hop closure over a call graph. Semantic
similarity finds code that *resembles* X, not code that *depends on* X.

**Consequence:** the system must route between graph traversal, vector search, and plain
deterministic code — and be able to justify each. That routing decision is the deliverable.

**Rejected:** RAG-over-source-files. Cheap to build, no defensible reason for Neo4j.

---

## D002 — Ground truth comes from git history, not hand labels
**Date:** 2026-08-04 · **Status:** accepted

For each historical commit, feed the system one changed file and ask it to predict the
rest. Recall against what actually shipped requires zero human labelling.

**Evidence:** 1,482 commits scanned on httpx → **610 usable eval cases**.

**Consequence:** retrieval and generation can be evaluated *separately* — retrieval by
recall@k on co-changed files, generation by correctness given fixed retrieval.

**Known limitation:** file renames split one logical file into two identities. Visible in
the data — `httpx/client.py↔models.py` (25x) and `httpx/_client.py↔_models.py` (39x) are
the same coupling before and after an underscore rename. Not yet handled; needs
`--name-status -M` rename detection. Currently **understates** coupling strength.

---

## D003 — History filtering: exclude merges and large commits
**Date:** 2026-08-04 · **Status:** accepted

Merge commits union unrelated branches. A 300-file lint sweep would contribute ~45,000
spurious pairs — more than every real commit combined.

**Chosen:** `--no-merges`, `max_files=20`, source files only, co-change edge requires
count ≥ 2.

**Evidence:** on httpx, 10 commits dropped as too large, 503 as touching no source.
610 of 1,482 retained.

**Open:** `max_files=20` is a guess. Sweep it and plot the effect on baseline recall.

---

## D004 — Static call resolution: name-based, with honest confidence tiers
**Date:** 2026-08-04 · **Status:** accepted, revisit after baseline

Python call graphs are *provably* incomplete statically — `getattr(o, n)()`, duck typing,
and decorator swapping are unresolvable without executing the program. So every `CALLS`
edge carries a confidence tier (`exact` / `self` / `local` / `heuristic`) and unresolved
calls are counted rather than hidden.

**Measured on httpx:** 2,845 in-scope call sites, **27.2% resolved**
(4 exact · 99 self/cls · 327 local · 343 heuristic · 2,072 unresolved).

**The residual is diagnosed, not mysterious:** almost all remaining misses are method
calls on local variables (`response.read()`) where the receiver's type is unknown.

**Deferred:** type inference via pyright/jedi would lift this materially at significant
complexity and runtime cost. **Do not adopt it until the eval set shows call-graph recall
is the binding constraint on end-to-end task performance.** It may not be — co-change
signal may carry most of the weight.

---

## D005 — Corrected a misleading metric before recording it as baseline
**Date:** 2026-08-04 · **Status:** accepted

First implementation reported **20.2%** resolution. That number was wrong in a way that
flattered nothing and misled everything: `len()`, `isinstance()`, and stdlib calls were in
the denominator, though they were never repo-internal functions.

**Fixed:** classify builtin/stdlib/third-party calls as `external` and exclude them; add
`self.foo()` / `cls.foo()` resolution against the enclosing class.
**Result:** 20.2% → **27.2% of in-scope calls** (+99 self-resolutions).

**Lesson worth keeping:** a wrong denominator is worse than no metric, because it gets
recorded as a baseline and every later comparison inherits the error.

---

## D006 — Two subprocess/encoding traps in git log parsing
**Date:** 2026-08-04 · **Status:** resolved

1. `--format=...\x00...` → `ValueError: embedded null byte`. argv strings are
   null-terminated; you cannot embed one. Fix: git's `%x1f` code — argv carries the
   literal text, git emits the byte.
2. Switched the record marker to `\x1e` — and silently got **zero commits**.
   `str.splitlines()` treats `\x1c`, `\x1d`, and `\x1e` as line boundaries, so the marker
   was eaten as a newline and no line ever `startswith()` it. `\x1f` is *not* a
   splitlines boundary.

**Fix:** literal text record marker + `%x1f` field separator.
**Lesson:** the second bug failed *silently* with a plausible-looking zero. Any pipeline
stage that can return empty needs an assertion, not just a happy-path test.

---

## D007 — Test corpora live outside the project tree
**Date:** 2026-08-04 · **Status:** accepted

Cloning httpx into `./repos/` caused `uv` to discover its `pyproject.toml`, treat it as a
workspace member, and rebuild the venv around httpx instead of labgo.

**Fix:** corpora in `../labgo-corpora/`, plus an explicit `[tool.uv.workspace] exclude`
as a guard.

---

## D008 — 45% of the eval set was unanswerable (temporal drift)
**Date:** 2026-08-04 · **Status:** open — must fix before any score is quoted

Found by asking a simple question: does the eval set mean anything without the corpus?

Eval questions are mined from commits across all of history, but the code graph is built
from the corpus at **HEAD**. Files referenced by old commits have since been deleted or
renamed, so the system cannot name them — not "gets them wrong", *cannot produce them*.

**Measured on httpx:**

| | |
|---|---|
| cases whose seed no longer exists | **278 / 610** |
| cases with ≥1 expected file gone | 305 / 610 |
| cases where nothing survives | 101 / 610 |

Example: `httpx/_compat.py` is a seed in the eval set and was deleted years ago.

**Consequence:** any score computed today is silently depressed by ~45% unanswerable
questions, and would be misread as the system underperforming.

**Options:**
1. *Filter* to cases where every file survives at the pinned commit. Cheap. Introduces
   survivorship bias — only tests code that lasted, which is systematically different.
2. *Restrict the window* to recent history where file identity is stable. Fewer cases.
3. *Time-travel*: for each case, build the graph from the commit's parent. Methodologically
   correct, simulates the developer's actual moment, but rebuilds the graph N times.

**Chosen:** (1) now, with the bias documented; (3) recorded as the rigorous version to
build if a result ever hinges on it.

**Resolved.** `labgo benchmark` builds a pinned benchmark under `benchmarks/<name>/`:
filtered cases plus a manifest recording corpus URL and SHA, extraction parameters, the
filter rule, and the survivorship bias it introduces. `labgo verify` refuses to score
against a drifted corpus.

**Outcome on httpx** (corpus `b5addb64`, `eval_max_files=10`):

| | |
|---|---|
| raw cases | 565 |
| dropped — seed deleted | 259 |
| dropped — an expected file deleted | 70 |
| **answerable** | **236 (−58.2%)** |

Losing 58% of the exam is the right trade. 236 questions that can be answered beat 565
where two in five are impossible, because the second set produces a number that looks
like underperformance and is actually arithmetic.

**Corollary — what "pinning" has to mean.** Four things must be fixed together or scores
are not comparable across time: the corpus commit SHA, the eval set, the extraction
parameters, and temporal consistency between the first two. Committing the eval set alone
(the original plan) was half a fix.

---

## D009 — Co-change labels are noisy, and the ceiling is not 100%
**Date:** 2026-08-04 · **Status:** accepted, with mitigation

A commit's file list is *"files that changed together"*, not *"files that had to change
together"*. It also contains bundled unrelated work ("while I was in there"), mechanical
sweeps (rename across six files), and multiple logical changes squashed into one commit.

So `expected` contains false positives. The answer key has wrong answers in it.

**What follows:**
- **A ceiling exists.** With meaningful label noise, no system scores near 100%. Chasing a
  high absolute number means overfitting to noise.
- **Relative comparison survives.** Baseline 45% vs agents 60% is a real 15-point gap —
  both were scored against the same noisy key, so the noise cancels. *This is the core
  argument for building the deterministic baseline first.*
- **Absolute claims do not survive.** Never "60% accurate at impact analysis". Only "60%
  on this benchmark, which has known label noise."
- **The noise is biased, not random.** Certain authors, eras, and areas bundle more, so it
  cannot be modelled as uniform error.

**Mitigation (cheap, do it):** score on the full set *and* on a high-confidence subset
where the co-change is corroborated independently — an actual call-graph edge between the
files, or a pairing recurring across many commits. Similar scores ⇒ noise isn't binding.
Much higher on the clean subset ⇒ a chunk of apparent failures are the benchmark's fault.

**Not a flaw unique to this project.** It is the standing trade in mining version history:
thousands of noisy labels, or dozens of clean hand-written ones. The failure mode is not
knowing which you have.

---

## D010 — `max_files` was a guess; the data says it is too generous
**Date:** 2026-08-04 · **Status:** open — sweep required

Commit size punishes geometrically: N files produce N(N−1)/2 pairs. 5 files → 10 pairs,
20 → 190, 47 → 1,081. Without a cap, the largest and least meaningful commits dominate.

**Measured on httpx (620 commits touching ≥2 source files, 14,125 pairs total):**

| files/commit | commits | pairs | % of all pairs |
|---|---|---|---|
| 2–3 | 328 | 546 | 3.9% |
| 4–5 | 131 | 982 | 7.0% |
| 6–10 | 106 | 2,737 | 19.4% |
| 11–20 | 45 | 4,377 | 31.0% |
| 21–50 | 10 | 5,483 | 38.8% |

**The five largest commits alone contribute 30% of all co-change evidence.**

Reading the curve, `max_files=20` is likely too loose — a 15-file change in a 60-file
library is a refactor, not a coupled change. 8–10 looks more defensible.

**Not asserting a value.** Two effects oppose: a larger cap gives more coverage, a smaller
one gives cleaner signal. The crossover is empirical. Sweep the parameter against baseline
recall and pick from the curve; keep the plot.

**Design flaw exposed by the same question:** `max_files` currently governs *both* which
commits contribute co-change evidence *and* which become exam questions. These want
different values — the exam wants focused commits for clean labels; the evidence tolerates
more noise because `min_count=2` filters downstream. Coupling them also means tuning the
threshold silently tunes the exam to match the evidence, which is a mild form of leakage.
**Split into two parameters.**

---

## D011 — Embeddings: Voyage (cloud) over local Ollama, overriding the local-only default
**Date:** 2026-08-05 · **Status:** accepted

Stage 3 needs a dense vector index over docstrings/functions. The original plan (README,
pre-D011) was local-only: `nomic-embed-text` via Ollama, chosen to keep the whole project
free, offline, and consistent with the no-cloud-providers stance in [[D001]]'s spirit.

**Changed because:** the dev laptop is already resource-constrained, and running an
embedding model locally (even a small one, via Ollama) competes with everything else
running on the machine. Voyage's `voyage-code-3` is a hosted, code-specialized embedding
API — no local compute, and plausibly better retrieval quality than a general-purpose
local model, which matters here because Stage 3's job is to show what vector search's
*ceiling* looks like against the graph baseline (D001). A weaker embedding model would
be a weaker demonstration of that ceiling, not just a cost saving.

**Consequence:** this is no longer a 100%-local project. Requires `VOYAGE_API_KEY` and
network access at index-build and query time; has a free tier but can accrue per-token
cost beyond it. `README.md`'s "Local only — no cloud providers by design" line and stack
table are updated accordingly.

**Not reopening:** graph loading (Neo4j, local Docker) and inference routing choices are
unaffected — this decision is scoped to the embedding step only.

---

## D012 — The deterministic baseline leaks against its own eval set via CO_CHANGED
**Date:** 2026-08-05 · **Status:** open — needs leave-one-out fix before the combined number is quotable

First Stage 2 measurement, httpx benchmark (236 cases), 2-hop CALLS traversal:

| | recall (mean) | precision (mean) | hit rate | empty predictions |
|---|---|---|---|---|
| call-graph only | 22.4% | 12.2% | 28.8% | 94 |
| call-graph + CO_CHANGED | **94.9%** | 7.2% | 97.5% | 1 |

The 4x jump is not a capability difference — it's leakage. `expected` (D002) and the
`CO_CHANGED` edges (D009) are mined from the *same* git history, and every eval case's
originating commit is **guaranteed** to also fall inside the co-change evidence window:
the committed benchmark's `manifest.json` has `eval_max_files=10 <= evidence_max_files=20`,
so any commit small enough to produce an eval case is, by construction, small enough to
also contribute to `hist.pairs`. The exact commit being scored supplies part of the
CO_CHANGED edge weight used to predict it — the predictor gets partial credit for having
seen the answer.

**Consequence:** 94.9% is not a fair "beat this" target. **22.4% (call-graph only) is the
honest Stage 2 floor** — CALLS edges are structural, mined independently of history, so
that number is clean. It is also low enough to be a believable floor: only 27.2% of calls
resolve statically at all (D004), so a 2-hop closure over an incomplete graph missing most
of its edges recovering 22.4% of a noisy label set (D009) is plausible, not suspicious.

**Not a reason to drop CO_CHANGED as a signal** — historical coupling between files that
happens to be independent of any one test commit is real evidence (the whole argument for
D002/D009). It means *this specific measurement* of the combined signal is contaminated,
not that the signal itself is worthless.

**Fix (not yet built) — leave-one-out mining:** when scoring case C from commit `sha_C`,
exclude `sha_C`'s contribution to `hist.pairs` before computing the CO_CHANGED edges used
to predict C. Requires rebuilding evidence per-case (or at least per-commit) rather than
once globally, so it is real added cost, not a one-line fix.

**Until fixed:** report the call-graph-only number as the citable baseline; the combined
number may be shown for direction but must carry this caveat every time.

---

## D013 — Graph store: Neo4j AuraDB Free over local Docker, overriding D001's "doesn't need to be hosted"
**Date:** 2026-08-05 · **Status:** accepted

D001 scoped the graph to be *load-bearing*, not *decorative* — it never said it had to run
locally. `docker-compose.yml`'s original comment ("Local Neo4j only — no managed/cloud
graph service") stated a stronger claim than D001 actually made, and D011 had already
broken the project's local-only stance for embeddings. Once one cloud dependency
(Voyage) is accepted for laptop-resource reasons, a second free one for the same reason
is not a new trade-off — it's the same one, again.

**Changed because:** one free AuraDB instance is available; running Neo4j in Docker
alongside everything else on a resource-constrained laptop has the same cost as running
embeddings locally did in D011.

**Evidence (AuraDB Free tier, checked 2026-08-05):** single instance, capacity reported
inconsistently across Neo4j's own docs as either 50k nodes / 175k relationships or 200k /
400k — either bound comfortably covers httpx (1,301 nodes / 2,100 edges, D004). The binding
constraint isn't capacity, it's **auto-pause**: an idle instance pauses after 72 hours and
is deleted 30 days after that. [Source: Neo4j Aura FAQ, Neo4j community forum.]

**Consequence:**
- `NEO4J_URI` moves from `bolt://localhost:7687` to `neo4j+s://<dbid>.databases.neo4j.io`
  (TLS-required scheme) — no code change, `connect()` (`graph.py`) already takes the URI
  from env/args verbatim.
- Single instance, no local/prod split — `labgo load --clear` against Aura is now a
  destructive action against the only copy. `data/graph.json` remains the source of
  truth and is regenerable from the corpus, so this is recoverable, just slower.
- **Revisit if the instance gets auto-paused/deleted from inactivity** — expected for a
  learning project touched in bursts. `docker-compose.yml` is kept, not deleted, as the
  offline/scratch fallback for exactly that case.
- README's "Local only" framing and Stack table updated; D001 itself is not reopened —
  the load-bearing argument for using a graph at all is unaffected by where it's hosted.

**Not reopening:** D011 (Voyage) and inference routing choices are unaffected — this
decision is scoped to graph hosting only.

---

## D014 — Vector index embeds source code, not docstrings; voyageai kept out of the default install
**Date:** 2026-08-05 · **Status:** accepted

The plan going into Stage 3 (README, pre-D014, and D011) was to embed function/file
docstrings. Before writing that, checked how much text would actually exist to embed.

**Evidence (httpx):** 225 of 1,134 functions have a non-empty docstring — **19.8%**.
A docstring-only index would leave 80% of Function nodes with no vector at all, not
because vector search failed on them but because there was never any text to embed. That
would make any later "vector search recall" number mostly a documentation-coverage
measurement wearing a retrieval-quality costume — precisely the kind of misleading metric
D005 already burned time on once.

**Decided:** embed each Function/Class node's **source code**, read from the corpus using
the line range the AST extractor already records (`node.lineno`/`end_lineno`), not stored
redundantly in the graph itself. `voyage-code-3` is a code-specialized model (D011) — this
is closer to its intended input than prose docstrings would have been anyway.

**Measured on httpx:** 1,229 candidates (Function + Class nodes), 1,229 embedded, 0
skipped, 162,884 tokens billed — comfortably inside Voyage's 200M-token free tier for this
model.

**Index:** a single native Neo4j vector index (`node_embedding`) on the generic `:Node`
label's `embedding` property (D001's loader already gives every node that label), rather
than one index per `:Function` / `:Class`. Nodes without an `embedding` property (Files,
un-embedded nodes) are simply absent from the index — confirmed, not an error.

**Consequence — dependency weight:** `voyageai` pulls in `langchain-core`, `tokenizers`,
`huggingface-hub`, `numpy`, `pillow`, and more transitively — a much heavier install than
"one embeddings API call" suggests, and exactly the situation the existing `agents` extra
was designed to avoid for Stage 4. Put it in its own `vectors` extra
(`uv sync --extra vectors`) rather than a core dependency; `cli.py` imports `labgo.embed`
lazily inside the `embed`/`search` commands only (`PLC0415` ignored there, with a comment)
so every other command keeps working with a plain `uv sync`.

**Known follow-up, not yet acted on:** `db.index.vector.queryNodes` (used by
`semantic_search`) logs a server-side deprecation notice — Neo4j's replacement is a
`SEARCH` clause. Left as-is: the procedure still works, and swapping syntax before Stage 3b
needs it is premature.

**Not yet built:** blending this with the call-graph baseline, and scoring vector-only
recall against the same 236-case benchmark used in D012 — that comparison is Stage 3b, the
actual point of D001's "vector search cannot answer transitive impact" claim being made
with a number instead of an assertion.

---

## D015 — Stage 3b: vector-only recall measured, an inflated-prediction bug caught first
**Date:** 2026-08-05 · **Status:** accepted

D001's claim — "vector search cannot answer transitive impact" — had been an assertion
since Stage 1. This is the measurement. `hybrid.py` adds `predict_impact_vector()` (rank
candidate files by their best-matching node's cosine score, no graph traversal) and
`predict_impact_hybrid()` (naive set union of the call-graph and vector predictions),
scored against the same 236-case benchmark `baseline.py` was scored against (D012).
`labgo baseline` gained `--method calls|vectors|hybrid`; `--method calls` (the default) is
byte-for-byte the old code path — reran it after the change and got the identical 22.4%
recall / 28.8% hit rate, so nothing about Stage 2's citable number moved.

**First measurement was wrong, and wrong in the informative direction.** The first version
of `predict_impact_vector` unioned every one of a seed file's functions' own top-`k`
nearest neighbors, unfiltered. That scored **75.5% recall** — which should have been the
headline result disproving D001, except it wasn't measuring what it claimed to. httpx is a
60-file corpus; checked directly, that predictor's average prediction size was **17.4
files (max 36)** — nearly 30% of the entire repo per case — against the call-graph
baseline's mean of 4.1 files. A predictor that returns most of the corpus scores well on
recall for the same reason a broken clock is right twice a day, not because the vectors
found anything specific. Same category of mistake as D012 (a flattering number that isn't
measuring the claimed thing), different mechanism — there it was label leakage, here it was
an unbounded prediction size.

**Fix:** rank candidate files by their *single best* matching score across all the seed's
functions, and cap the result at `k` files total (`ORDER BY best DESC LIMIT $k` in Cypher),
so `k` means the same kind of thing `hops` does for the call-graph baseline — a bounded
retrieval budget — regardless of how many functions the seed file happens to contain.

**Measured on httpx, vector-only, sweeping `k`:**

| k | mean predicted size | recall | precision | hit rate |
|---|---|---|---|---|
| 3 | ~6.5 | 17.1% | 13.1% | 24.6% |
| 4 | ~7.5 | 19.3% | 13.2% | 28.0% |
| 5 | ~8.3 | 22.3% | 12.4% | 31.8% |
| 10 | ~8.5 | 45.5% | 9.6% | 61.0% |
| 20 | ~9.6 | 60.7% | 7.0% | 73.3% |

(Predicted size grows sub-linearly with `k` above ~10 — most seed files' functions run out
of distinct highly-similar files before `k` is reached.)

**The fair comparison is at matched prediction budget, not matched `k`.** Call-graph
(hops=2, no cochange) predicts a mean of **4.1 files** per case. Vectors at `k=4` predict a
comparable **~7.5 files** and score **19.3% recall / 13.2% precision** — call-graph alone
scores **22.4% / 12.2%** (D012). At matched budget, the graph signal is at least as good as
the vector signal for this task, which is D001's claim, now checked rather than assumed.
Vectors *can* be pushed to higher recall (60.7% at k=20), but only by spending precision
down to 7% — predicting a much larger slice of the corpus, the same trade the inflated
first measurement made by accident, now made on purpose and disclosed.

**Hybrid (naive union, hops=2, no cochange) beats either signal alone:**

| method | k | recall | precision | hit rate |
|---|---|---|---|---|
| calls only | — | 22.4% | 12.2% | 28.8% |
| vectors only | 4 | 19.3% | 13.2% | 28.0% |
| **hybrid** | **4** | **40.1%** | **14.8%** | **53.8%** |
| hybrid | 10 | 54.0% | 9.4% | 69.1% |

At `k=4`, the hybrid nearly *doubles* recall over call-graph alone while precision also
improves slightly (14.8% vs 12.2%) — the two signals are catching meaningfully different
true positives rather than mostly overlapping, which is the concrete version of WHY.md's
claim that graph and vector answer different kinds of questions. This is the first number
in the project where combining signals helps *without* the D012-style catch of it being an
artifact — verified by checking predicted-set sizes were comparable going in.

**Consequence:** 22.4% (call-graph only) remains the citable Stage 2 floor. **40.1% recall
/ 14.8% precision (hybrid, hops=2, no-cochange, k=4) is Stage 3b's citable result** — the
first evidence that blending is worth the complexity, ahead of Stage 4's agents.

**Not yet done:** the union is unweighted and unranked — a file predicted by both signals
counts the same as one predicted by either alone. `k` is not tuned past this sweep. Whether
`CO_CHANGED` (excluded here per D012) adds further honest lift on top of the hybrid, versus
just reintroducing leakage, is untested.

---

## Template

```
## Dxxx — <decision>
**Date:** · **Status:** proposed | accepted | superseded by Dyyy

**Context:**
**Alternatives considered:**
**Evidence:**
**Consequence:**
**Revisit when:**
```
