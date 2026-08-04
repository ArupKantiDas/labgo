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
