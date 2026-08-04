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
