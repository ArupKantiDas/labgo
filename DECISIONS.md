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
**Date:** 2026-08-04 · **Status:** resolved (2026-08-06) — sweep run, but the answer is
not the one the question assumed. See "Resolved" below.

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
**Split into two parameters.** *(Done separately, ahead of this sweep — `evidence_max_files`
/ `eval_max_files` already exist in `gitlog.extract_history` and the `history`/`benchmark`
CLI commands.)*

**Resolved.** Ran the sweep this entry called for — but D012's leave-one-out fix (done
first, since scoring `CO_CHANGED` at all needed it to be honest) had already surfaced the
real target: the leak-free combined predictor was returning 40.5 of ~60 files per case,
regardless of `evidence_max_files`. That's a *budget* problem, and `min_count` — not
`max_files` — turned out to be the lever that controls it.

**min_count sweep (evidence_max_files=20, hops=2, leave-one-out, 236 cases):**

| min_count | recall | precision | mean predicted size |
|---|---|---|---|
| 2 (shipped default) | 92.1% | 5.9% | 40.5 |
| 5 | 74.3% | 11.4% | 21.9 |
| 10 | 56.7% | 20.2% | 11.1 |
| 20 | 32.6% | 19.6% | 6.0 |
| **24–30 (plateau)** | **26.9%** | **15.7%** | **5.1** |

The curve plateaus at `min_count≈24` and stays flat through 35 — httpx simply has no
pairs left above that count to keep filtering (max observed count is 39, D012). **This is
the matched-budget point**: call-graph alone predicts 4.82 files/case (measured directly,
not the 1.6 an earlier draft of this sweep mistakenly reported by averaging over unique
seeds instead of per-case — same weighting bug the recall/precision means everywhere else
in this project deliberately avoid). At `min_count≈24–30`, combined recall (26.9%) and
precision (15.7%) both beat call-graph-alone (22.4% / 12.2%) — a real, fair win, the same
matched-budget standard D015 already established for vectors.

**Then checked whether `evidence_max_files` still matters, now that `min_count` is doing
the real filtering.** Crossed both parameters:

| evidence_max_files | min_count=20 | min_count=24 | min_count=30 |
|---|---|---|---|
| 10 | 26.9% / 15.5% / 5.1 | 26.9% / 15.7% / 5.1 | 22.4% / 12.2% / 4.8 |
| 15 | 28.9% / 15.5% / 5.7 | 26.9% / 15.7% / 5.1 | 26.9% / 15.7% / 5.1 |
| 20 | 32.6% / 19.6% / 6.0 | 26.9% / 15.7% / 5.1 | 26.9% / 15.7% / 5.1 |
| 30 | 34.0% / 17.7% / 6.5 | 28.9% / 16.3% / 5.3 | 26.9% / 15.7% / 5.1 |

(recall / precision / mean size). **Once `min_count` is tuned to a matched budget, the
four `evidence_max_files` values converge to nearly the same result.** `evidence_max_files`
only moves the number when `min_count` is left loose — exactly the regime this project
should not be citing a number from anyway (D012). The original hypothesis ("`max_files=20`
is too generous, tighten it") named the wrong knob: the fix isn't a smaller
`evidence_max_files`, it's a `min_count` chosen for the budget being compared at.

**Consequence:**
- **Not changing `evidence_max_files`'s default (20)** — the sweep shows it barely matters
  once scoring is done properly, so there's no evidence-backed reason to move it.
- **Not changing `min_count`'s default (2)** either — 2 remains the right default for
  `co_change_edges()`'s original purpose, the impact *viewer* (D001's "show a human what's
  coupled," not a fixed prediction budget to score against). Silently raising it there
  would hide real coupling from a person for the sake of a metric they're not computing.
- **The citable matched-budget combined number is now `--min-count 24` or higher** (25
  used going forward as a round, plateau-safe choice): 26.9% recall / 15.7% precision at
  hops=2, leave-one-out — beats the 22.4% / 12.2% call-graph floor on both axes, and unlike
  the `min_count=2` number (92.1% / 5.9%), this one is comparing at a budget nobody can
  dispute is fair.

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
**Date:** 2026-08-05 · **Status:** resolved (2026-08-06) — leave-one-out shipped; the
combined number moved less than expected, and checking *why* found a second, bigger
problem than leakage. See "Resolved" below.

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

**Resolved.** `benchmark.write_benchmark` now pins the *raw*, unfiltered co-change pairs
alongside a benchmark (`evidence.json` — every count, not just `min_count`-and-above,
because a pair that only clears the threshold *due to* the excluded commit needs to lose
that support, not have it hidden by an earlier filter). `gitlog.leave_one_out_neighbors`
subtracts exactly one commit's contribution before applying `min_count`. `score_baseline`
and `score_hybrid_baseline` now require `pairs=` (via `benchmark.load_evidence`) whenever
`use_cochange=True`, and raise rather than silently falling back to the old leaky path —
the old `predict_impact` global-CO_CHANGED query still exists for live/production use
(single ad-hoc queries, the impact viewer) where no case can leak into its own answer.

**Re-measured on httpx (236 cases, hops=2):**

| | recall (mean) | precision (mean) | hit rate |
|---|---|---|---|
| call-graph only (unaffected by this fix) | 22.4% | 12.2% | 28.8% |
| calls + CO_CHANGED, **old leaky number** | 94.9% | 7.2% | 97.5% |
| calls + CO_CHANGED, **leave-one-out (fixed)** | **92.1%** | **5.9%** | 97.0% |

**The fix barely moved the number, and that itself needed explaining before trusting
either one.** 94.9% → 92.1% is a 2.8-point drop, not the collapse a "was leaking"
diagnosis implies. Checked directly: of the 510 (seed, expected-file) pairs in the eval
set that have *any* co-change evidence, 441 (86.5%) are backed by co-occurrence in
commits *other than* the one being scored — leave-one-out removes one commit's support
from a pair that often recurs 5, 10, even 39 times elsewhere in httpx's history (D010's
own table already showed coupling is concentrated, not evenly spread). Only 69 pairs
(13.5%) were single-commit flukes that leave-one-out correctly kills. **Same-commit
leakage was real but never the dominant driver of the inflated score.**

**Checking predicted-set size — the same discipline D015 used to catch its own inflated
vector number — found the actual problem.** Mean prediction size: call-graph alone 4.8
files/case; calls + CO_CHANGED (leave-one-out) **40.5 files/case**, against a ~60-file
corpus — **two-thirds of the entire repository, per case, on average** (max 74 — larger
than the corpus's Python-file count alone, since the corpus also contains non-`.py`
files the prediction set doesn't restrict against). A predictor that returns most of the
codebase scores well on recall independent of whether it found anything specific — the
identical failure mode D015 named "a broken clock is right twice a day," just from
`min_count=2` being too loose for httpx's scale rather than an unbounded `k`.

**Consequence — the citable number does not change, but the reason does.** 22.4% /
12.2% (call-graph only) remains the honest floor. The leak-free combined number, 92.1% /
5.9%, is **not yet a fair "beat this" target either** — not because of leakage anymore
(that's fixed and tested), but because it is scored at an unbounded, uncapped prediction
budget nobody chose on purpose. Reporting it without its predicted-set size would repeat
D005/D015's exact mistake in a new spot.

**Follow-up, now scoped precisely by this data:** D010's sweep should treat `min_count`
as the primary lever (not just `evidence_max_files`) and evaluate at a budget matched to
the call-graph baseline (~4.8 files/case) — the same matched-budget methodology D015
already validated for vectors, now needed for co-change too.

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

**Follow-up (2026-08-06), after D012 and D010 made it safe to ask.** With leave-one-out
mining and a matched `min_count=25`, the "is it just leakage" question has an answer:
`predict_impact_hybrid(use_cochange=True, pairs=..., min_count=25)` already implements
this (D012's change to `hybrid.py`) — no new code needed, only the measurement.

| method (hops=2, k=4) | recall | precision | mean size |
|---|---|---|---|
| calls + vectors (D015's original hybrid) | 40.1% | 14.8% | 8.1 |
| calls + vectors + cochange (loo, min_count=25) | **43.3%** | **15.4%** | 8.3 |

Real, modest, honestly-measured lift — +3.2 recall points for +0.2 files/case, precision
*improving* slightly rather than being spent down. At `k=3` the same pattern holds
(38.6%/15.2% → 41.8%/15.9%); by `k=6` the two numbers converge exactly (47.1%/14.2% for
both) — cochange's marginal contribution at `min_count=25` is small enough (~0.3
files/case, D010) that a wide-enough vector net catches the same handful of hits on its
own. **Conclusion: CO_CHANGED adds real signal on top of the hybrid, not leakage — but
the effect is small and shrinks as `k` grows**, consistent with D010's finding that
`min_count=25`'s co-change contribution is inherently thin.

**Ranked/weighted union — deferred, not built, and for a reason worth stating rather than
just leaving silent.** The naive union already produces a smooth, sensible recall/precision
curve across every sweep run so far (D015's original, this one, D010's). Building a scorer
(e.g. rank by how many of the three signals agree, break ties by vector cosine) is real
engineering with no evidence yet that it's needed — the same posture D004 already took on
type inference: **do not adopt it until a measurement shows the naive union is the binding
constraint**. It may not be; the bottleneck visible in every number so far is retrieval
*recall* at any reasonable budget (the 22.4–43.3% range), not the union's ranking.

---

## D016 — Stage 4: the agent doesn't beat the deterministic baseline yet, and the reason is legible
**Date:** 2026-08-06 · **Status:** accepted — measured, not flattering, diagnosed rather than hidden

Stage 4 (`agent.py`) is a LangGraph tool-calling loop: an LLM (Haiku 4.5) chooses among
five tools — `call_graph_traverse`, `co_change_neighbors`, `semantic_search` (all thin
wrappers over what Stages 2/3 already measure) plus two new ones, `test_coverage` and
`likely_reviewer`, the "which tests" and "who reviews" halves of README's opening
question that nothing before this stage answered. The graph is `agent ⇄ tools` with a
`finalize` escape hatch at `max_turns` (forces a tools-disabled final call, guaranteeing
termination) — LangGraph for orchestration, the raw `anthropic` SDK for the model call,
no `langchain_anthropic`/`create_react_agent` (README's "LangChain used thinly" stance;
a prebuilt agent loop would hide exactly the routing behaviour this stage exists to
check, D001).

**Measured on httpx, `labgo agent-eval benchmarks/httpx --n 40 --max-turns 6`** (Haiku
4.5, sample of 40/236 cases — the full benchmark through a multi-turn LLM loop is real
time and real tokens, so the sample size is a disclosed choice, same honesty standard as
D003/D008's filtering, not a shortcut hidden from the number; `--sample-seed 42` makes it
reproducible):

| | recall | precision | hit rate | mean predicted size |
|---|---|---|---|---|
| call-graph only (floor) | 22.4% | 12.2% | 28.8% | 4.8 |
| hybrid + cochange, matched budget (D015 follow-up, citable) | 43.3% | 15.4% | 57.2% | 8.3 |
| **Stage 4 agent (n=40)** | **30.3%** | **5.8%** | **45.0%** | **13.05** |

**The agent does not beat the deterministic baseline.** Recall (30.3%) lands between the
floor and the tuned hybrid — reasonable — but precision (5.8%) is *worse than the
call-graph floor's own precision* (12.2%), let alone the hybrid+cochange's 15.4%. Cost:
429,839 input / 38,534 output tokens for 40 cases (≈11.7K tokens/case, mean 3.05 turns) —
cheap on Haiku in absolute terms, but currently buys a worse answer than the free
deterministic baseline. By D001's own standard — *does the added complexity earn its
place* — the honest answer right now is no.

**Diagnosed, not just reported (the same discipline D012/D015 already paid for).** Mean
predicted size is 13.05 files/case — 57% larger than the hybrid+cochange's own 8.3-file
budget, and nearly 3x the call-graph floor's 4.8. Every deterministic method has an
explicit, tunable size cap (`hops`, `k`, `min_count`); the agent has none — the system
prompt describes the tools' tradeoffs but never asks for a bounded final answer, so it
lists everything gathered across tool calls (`co_change_neighbors` called 51 times over
40 cases — often more than once per case, exploring several `min_count` values) without
self-imposed discipline about the final set size. **This is not a reasoning failure, it's
an uncalibrated budget** — structurally the same lesson as D015's first (wrong) 75.5%
vector number and D012's 40.5-file leak-free-but-unbounded combined predictor: an
otherwise-plausible number produced by predicting too much, not by finding more.

**Minor, secondary finding:** 3/40 cases had a stray `**` (markdown bold) leak into the
`IMPACTED_FILES:` line despite the "plain text, not JSON" instruction.
`parse_impacted_files()` correctly dropped the malformed entry as unrecognized rather
than mis-scoring it (`AgentResult.unrecognized_mentions` — module docstring's stated
design), so this cost a little recall on 3 cases but never corrupted a score.

**Not yet done — the obvious next fix, named rather than silently deferred:** apply
D015's matched-budget standard to the agent itself — instruct it to return its best *N*
files, ranked, and compare against the deterministic baselines at the same N. Until that
measurement exists, "the agent doesn't beat the baseline" is the citable number, and
"because it isn't budget-constrained" is the citable reason, not "LLMs aren't good at
this" — those are different claims, and only the first one has evidence behind it here.

---

## D017 — Stage 6: console-exported OpenTelemetry over Langfuse; CI runs the free half of the eval suite
**Date:** 2026-08-06 · **Status:** accepted

Stage 6 is two things README's Stack table already named: observability (traces,
cost/latency) and an eval suite in CI. Both landed with a cost-conscious choice each,
same posture as D011/D013.

**Observability: OpenTelemetry, console-exported, not Langfuse.** The Stack table listed
"Langfuse / OpenTelemetry" as either-or. Langfuse needs an account and an API key — one
more credential that can be unavailable at 2am, the exact failure mode this project has
already hit twice (D011, D013: laptop-resource and cost reasons pushed Voyage and Neo4j
to hosted services, but *adding a service* is a cost D011/D013 accepted only because there
was no free-and-local alternative that taught the same thing; here there is). `tracing.py`
uses real OpenTelemetry spans (`SimpleSpanProcessor(ConsoleSpanExporter())` — `Simple`, not
`Batch`: labgo's commands are short CLI processes, and a batched processor can still be
holding spans when the process exits) — genuinely swappable for an OTLP exporter later
(`_provider.add_span_processor(...)` is one line), not a rewrite, but nothing beyond stdout
is wired up now because nothing has asked for it yet.

**Traces, not just spans.** The first version of this wrapped each `agent.py` LLM/tool call
in its own `start_as_current_span()` independently, which — checked directly — gave every
span its *own* `trace_id`: a pile of unrelated spans, not a trace of one agent run. Fixed
by wrapping the whole `run_agent()` call in one root `agent.run` span, so every child span
nests under it and shares a `trace_id`. Verified against a live run: all spans in one
`labgo agent` invocation now share one `trace_id`, with correct `parent_id` chaining.

**Cost/latency, not cost-only.** Every `agent.llm_call` span carries `input_tokens`,
`output_tokens`, and `latency_ms`; every `agent.tool_call` span carries `latency_ms` per
tool, so a slow tool is visible in the trace, not just an expensive model call.
`mcp_server.py`'s tool wrappers get the same `mcp.tool_call` spans, latency only — the
model calling them there is the *client's*, not one this project pays for, so there are no
tokens of labgo's own to report.

**Genuinely optional, same shape as every other extra.** `opentelemetry-sdk` is a new
`observability` extra, not a core dependency. `tracing.span()` is a no-op context manager
when the SDK isn't installed (checked via `try`/`except ImportError` at module load, the
same pattern `voyageai` (D014) and `mcp` (Stage 5) already use) — `agent.py` and
`mcp_server.py` run identically either way, they just don't get traced.

**CI runs the free half of the eval suite, not the paid half.** `.github/workflows/ci.yml`
runs `ruff check` + `pytest` on every push/PR — confirmed first that all 42 (now 46) tests
need zero live services (grepped the suite for `connect(`/`GraphDatabase`/
`anthropic.Anthropic(`/`voyageai.Client(` before wiring this up; the one hit,
`test_graph.py`'s `connect()` call, is inside a test that unsets `NEO4J_PASSWORD` and
asserts `connect()` raises *before* ever touching the network). Deliberately does **not**
run `labgo baseline`/`agent-eval` in CI: those need real Neo4j/Voyage/Anthropic credentials
that shouldn't live in a public repo's CI secrets for a personal project, and `agent-eval`
specifically costs real per-token money on every push — a free, fast lint+test gate is the
honest scope for CI here, not a simulated full eval run.

---

## D018 — Multi-language ingestion: a registry, tree-sitter for ten languages, file-level fallback for the rest
**Date:** 2026-08-06 · **Status:** accepted

**Context:** the project was Python-only in exactly two places — `pyast.py` (the extractor)
and `gitlog.py`'s `SOURCE_SUFFIXES = {".py"}` — while everything downstream of `graph.json`
(Neo4j loader, baseline, embeddings, viewer, agent, MCP) was already language-agnostic.
Making the repo plug-and-play with any language is therefore an ingestion problem, not a
rewrite.

**Chosen:** three tiers behind one dispatcher (`ingest/extract.py`), described by one
registry (`ingest/languages.py`) that is the single source of truth for extensions,
grammar keys, test conventions, vendored dirs, and lockfile names:

1. **Python → `pyast.py`, byte-for-byte untouched.** Its 27.2% resolution number is
   measured and recorded; the dispatcher folds its output in post-hoc (a
   `dataclasses.replace` to stamp `language="python"`), so the httpx baseline stays
   exactly reproducible.
2. **JS/TS/TSX/Go/Java/Rust/C/C++/Ruby/C#/PHP → `tsast.py`** (tree-sitter): one generic
   engine, one declarative `TSSpec` per language (three queries + small tables). The
   D004 resolution ladder generalizes: SELF (this/self/`$this`/Go receiver var/implicit
   this) → EXACT (import resolved to a repo file) → LOCAL (same file; Go adds same
   package) → HEURISTIC (unique name **within the language**), unresolved counted.
   External classification stays D005-conservative: repo-defined names are never
   external, unknown receivers stay in the denominator.
3. **Any other recognized extension → file-level fallback**: a File node with
   language/loc/is_test and nothing else. Not a stub — co-change evidence is
   language-agnostic and D010/D012 showed it carries most of the retrieval signal, so
   impact mode, the eval set, and the viewer all work for *any* language.

**Alternatives considered:** tree-sitter for Python too (rejected: discards the measured
narrative for zero measured gain); file-level only for non-Python (rejected by scope
decision — call graphs for the top languages are the point); per-language AST libraries
(rejected: ten dependencies and ten APIs where tree-sitter is one of each).

**Dependency pin, deliberate:** `tree-sitter-language-pack >=0.13,<1.0` + `tree-sitter
>=0.25,<0.27`. The 0.x pack bundles every grammar precompiled in the wheel (~17–33 MB);
the 1.x line downloads grammars on first use — which breaks offline first-runs and adds
CI flake, the opposite of plug-and-play. Degradation is graded regardless: missing
package → file-level everywhere; one grammar/query failing to compile → that language
only; one file failing → counted, skipped.

**Honesty mechanism:** `ExtractionStats.per_language` carries every counter per language.
External-call classification quality varies (Go/JS/TS strong, C/PHP builtins-only), so a
weakly-classified language shows a visibly depressed rate of its own instead of silently
polluting the global number — the D005 lesson, structurally enforced. Known per-language
gaps are named, not hidden: TS `tsconfig` paths/baseUrl, Rust `use crate::` and
`#[cfg(test)]`, PHP PSR-4, C# namespace→file (C# gets no EXACT tier), Ruby bare
paren-less calls (grammatically indistinguishable from variable reads — invisible, not
"unresolved").

**Consequence for benchmarks:** `SOURCE_SUFFIXES` widening changes what `history` counts
as source, so the *first regeneration* of any benchmark after this change is a new exam
(the committed `benchmarks/httpx` is pinned and unaffected). Every manifest now records
`source_suffixes` so the filter that built an exam is provenance, not folklore.

**Revisit when:** a py-tree-sitter release the 0.x pack cannot load (move to 1.x +
`prefetch()`), or a measured corpus shows a named descope (e.g. tsconfig paths) is the
binding constraint on EXACT-tier recall.

---

## D019 — Plug-and-play surface: `labgo analyze` + `labgo doctor`, core stays zero-credential
**Date:** 2026-08-06 · **Status:** accepted

**Context:** the quickstart required knowing D007 (clone corpora *outside* the tree),
running three commands in order, and understanding which stages need which credentials.
"Download and done" needs one command and one diagnostic.

**Chosen:**
- **`labgo analyze <path-or-url>`** — the one command: a git URL is cloned
  (`--filter=blob:none`, full history for `labgo history`) to `~/.labgo/corpora/<name>`,
  which satisfies D007's outside-the-tree requirement automatically; a local path is used
  as-is. Runs ingest + history (skipped with a message when the target isn't a git repo),
  prints the language census, then serves the viewer. Zero credentials, zero config.
- **`labgo doctor`** — a per-stage readiness table (core / neo4j / vectors / agent /
  extras) where every failing row prints the exact fix (`uv sync --extra …`, the `.env`
  line, the npm build). `--probe` adds a live Neo4j connectivity check. It is a report,
  not a gate: always exits 0, and says explicitly that nothing in it blocks `analyze`.

**Alternatives considered:** auto-starting local Neo4j via Docker from `labgo load`
(rejected by scope decision: guided setup over hidden orchestration — a command that
silently starts containers is the opposite of legible); corpora as a CLI-managed cache
with eviction (rejected: a directory the user can `rm -rf` is simpler than cache
bookkeeping).

**Consequence:** the README quickstart is now one command against any repo, and the
cloud stages remain exactly as opt-in as D013/D014 designed them — `doctor` replaces
tribal knowledge about what each one needs.

**Revisit when:** corpora under `~/.labgo` need pinning/verification semantics beyond
what `labgo benchmark`'s manifest already provides.

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
