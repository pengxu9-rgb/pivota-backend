# Master Plan — PDP-as-canonical catalog migration

**Live source of truth.** Update on every meaningful step. Originated from the
recall investigation closed at 23% pass-rate; tracks every phase since.

- Last updated: 2026-05-07 (post issue #1 trace — root cause is gateway/backend disconnect, not a backend bug)
- Owner: peng
- Origin: `~/.claude/plans/shimmying-soaring-ember.md` (now superseded — keep this file canonical going forward)

---

## How to use this doc

- **Read** this file at the start of every session before planning new work.
- **Update** this file when a phase ships, a probe runs, or scope changes. Stale = useless.
- **Open issues** belong in the table at the bottom; mark them resolved with a date and PR number rather than deleting.
- **Probe runs** go into the trajectory table (one row each).

---

## Where we are (one-paragraph summary)

The catalog architecture migration (PDP-first labeling + canonical chain
across `catalog_products` → `catalog_skus` → `catalog_offers` → `catalog_merchants`)
is mechanically complete for **lipstick (15 PDPs), fragrance (50), eye makeup (50),
face makeup (50)** as of 2026-05-07. Recall pass-rate peaked at **45%** after
fragrance ingestion (Phase 8) but regressed to **28%** after Phase 9, then
recovered to **30%** after the Phase 7a backfill applied today. The remaining
gap is no longer a data gap — **the recall query path is not using the
canonical chain for lipstick queries** even though all rows exist. That's the
next blocker.

---

## Phase status

All PR numbers refer to `pengxu9-rgb/pivota-backend` unless noted.

| Phase | What | Status | Refs |
|---|---|---|---|
| Deliv. A | Recall investigation handoff doc | ✅ | `pivota-agent-ui/reports/recall_v1/RECALL_INVESTIGATION_FINAL.md` |
| 1 | PDP-first SQL indexes | ✅ | #295, mig 068 |
| 2 | `category_path` columns + regex backfill | ✅ | #296, mig 069 (1216/1535 covered, 304 NULL — known long-tail) |
| 2b | Recall side wired to PDP-first | ✅ | #297, `services/pivot_query_service.py` |
| 3A | Deterministic seed→PDP matcher | ✅ | #299, `services/pdp_matcher/deterministic.py` |
| 3B | LLM tail matcher (gemini-2.5-flash) | ✅ | #305, `services/pdp_matcher/llm_match.py` |
| 4 (lipstick) | Catalog enrichment agent — 15 lipstick PDPs + 18 seeds | ✅ | #301, #303 |
| 6 | `pdp_scope` dimension (canonical vs merchant_owned) | ✅ | #311, mig 070 |
| 5 (probe v9) | Re-probe after Phase 6 | ✅ ran multiple times — see trajectory below | `pivota-agent-ui/reports/recall_v1/recall_v9_*` |
| 7a | Agent ingestion writes the full canonical chain | ✅ | #313 |
| 7a-backfill | Heal pre-7a PDPs (lipstick + fragrance) — 65 PDPs, 68 SKU/offer/merchant rows | ✅ applied to prod 2026-05-07 | #332 (script `scripts/backfill_canonical_chain_for_agent_seeds.py`) |
| 7c | Synthetic variant + availability fixes zero_variants blocker | ✅ | #317, #320 |
| 8 (fragrance) | 50 fragrance PDP candidates | ✅ | #323 |
| 9 (eye + face) | 50 makeup eye + face PDP candidates | ✅ | #328 |
| C-1 | Pivota canonical PDP foundation — schema + sig generator + audit fallback | ✅ | #327 |
| C-2 | Public sig_* → product API + sitemap list | ✅ | #329 |
| C-3 | One-shot script for legacy rows | ✅ | #331, plus #330 schema guard |

---

## Recall pass-rate trajectory

53-query `eval_corpus_recall_v1.jsonl`, EN+ZH, run against
`https://agent.pivota.cc/api/gateway --source shopping_agent --entry chat`.

| Run ID | Phase boundary | Pass | Thin | Empty | Fail | Pass-rate |
|---|---|---:|---:|---:|---:|---:|
| recall_v9_phase6_1778094214 | After Phase 6 (no canonical-chain data) | 11 | 9 | 32 | 0 | 21% |
| recall_v9_phase7c_1778120085 | After Phase 7c synthetic-variant fix | 19 | 8 | 26 | 0 | 36% |
| recall_v9_phase8_1778122760 | After fragrance ingestion (Phase 8) | 24 | 9 | 20 | 0 | **45% — peak** |
| recall_v9_phase9_1778124544 | After eye+face ingestion (Phase 9) | 15 | 10 | 27 | 1 | **28% — regression** |
| recall_v10_phase7a_backfill_1778130307 | After Phase 7a backfill (lipstick + fragrance SKUs) | 16 | 10 | 26 | 1 | **30%** |
| recall_v11_aurora_bff_1778170034 | Same data, source=aurora-bff (orchestrator localization probe) | 16 | 3 | 33 | 1 | **30%** |

**Headline insight:** every probe since v9_phase8 has *more* canonical data
than the previous one, yet pass-rate has not returned to peak. The bottleneck
is no longer "do the rows exist" but "does the recall query reach them."

**v11 (aurora-bff) localization probe**: ran the same 53-query corpus under a
different orchestrator (`source=aurora-bff` instead of `shopping_agent`).
**Lipstick still 0/9 on both paths**, with different failure modes:

- shopping_agent: seed query runs, returns 0 (`cache_miss_sync_filled,
  external_raw_count=0`)
- aurora-bff: seed query NEVER RUNS (`external_seed_executed=false,
  fallback_route=invoke_primary_irrelevant`) — internal catalog returns 0
  → "primary irrelevant" gate fires → skips external seed entirely

This rules out a per-orchestrator one-line patch. The architectural fix
(Phase 7b — gateway reads `catalog_offers`/`skus`) is the unambiguous
next step.

---

## Probe v10 detailed (2026-05-07)

Per-bucket breakdown (PASS/THIN/EMPTY):

| Bucket | n | PASS | THIN | EMPTY | Notes |
|---|---:|---:|---:|---:|---|
| skincare_moisturizer | 3 | 3 | 0 | 0 | full pass |
| skincare_sun | 2 | 2 | 0 | 0 | full pass |
| skincare_cleanser | 2 | 2 | 0 | 0 | full pass |
| skincare_bare_noun | 4 | 2 | 0 | 2 | partial |
| skincare_serum | 2 | 0 | 1 | 1 | thin |
| fragrance | 5 | 2 | 3 | 0 | **lifted from 0/5 → 2/5** after Phase 7a backfill |
| makeup_eye | 3 | 2 | 0 | 1 | lifted from 0/3 → 2/3 |
| makeup_eye_bare_noun | 2 | 1 | 0 | 1 | lifted from 0/2 → 1/2 |
| makeup_face | 5 | 1 | 3 | 1 | unchanged |
| **makeup_lip** | **6** | **0** | **0** | **6** | **broken — see open issue #1** |
| **makeup_lip_bare_noun** | **3** | **0** | **0** | **3** | **broken — see open issue #1** |
| fashion_top | 2 | 0 | 1 | 1 | data-empty (no Phase 4 yet) |
| fashion_dress | 2 | 0 | 1 | 1 | data-empty |
| fashion_shoes | 3 | 0 | 1 | 2 | data-empty |
| electronics | 5 | 1 | 0 | 4 | data-empty |
| home | 4 | 0 | 0 | 4 | data-empty |

---

## Open issues

### #1 — Lipstick recall returns zero (BLOCKER — diagnosed 2026-05-07, fix scoped)

**Symptom:** all 9 lipstick queries (EN + ZH) layer-attribute to
`C-external_seed / seed_ran_returned_zero`. Probe metadata:
`external_seed_executed=true, external_raw_count=0,
external_seed_skip_reason=cache_miss_sync_filled, internal_raw_count=0`.

**Diagnosis: the gateway never reads the backend canonical chain.**
Three layered findings, in escalating severity:

1. **Architectural gap (root cause).** PIVOTA-Agent (`agent.pivota.cc/api/gateway`)
   has **zero SQL references to `catalog_products` / `catalog_skus` /
   `catalog_offers`**. Every backend phase that landed under the "canonical
   migration" banner — Phases 1, 2, 2b, 7a, 7c, 8, 9, C-1/C-2/C-3 — is
   invisible to live `find_products_multi` recall. The comment at
   `findProductsExternalSeedDirectRetrieval.js:152` acknowledges this:
   *"the gateway does not yet JOIN catalog_offers (Phase 7b)"*.

2. **Bridge clause is in the wrong file.** `findProductsExternalSeedDirectRetrieval.js`
   has `OR tool = 'catalog_enrichment_agent_v1'` to let attached agent seeds
   pass. But 5+ other seed-query templates in `server.js` and `auroraBff/`
   hard-code `attached_product_key IS NULL` with no bridge. Whichever path
   fires for `shopping_agent → find_products_multi` lacks it.

3. **Even unattached lipstick seeds (36 in prod) return 0.** Some additional
   filter (quality gate, merchant scope, or cache key mismatch on
   `cache_miss_sync_filled`) is dropping rows beyond the IS-NULL filter.
   Needs a separate trace.

**Verification done:**
- ✅ Backend `_fetch_canonical_search_rows` returns all 18 lipstick rows
  when SQL is run directly with `query='lipstick'` — backend recall is
  correct, just unused.
- ✅ Direct seed-table SQL with `attached_product_key IS NULL AND
  market='US' AND lower(title) LIKE '%lipstick%'` returns 36 rows in prod.
- ✅ Agent-authored seeds populate `title` correctly but leave
  `seed_data.derived` NULL; non-agent seeds populate
  `seed_data.derived.recall.category='Lipstick'`. Either should match the
  LIKE predicate via `lower(title)` alone — yet the gateway returns 0,
  pointing to finding #3.

**Trace summary (2026-05-07 cold-read of PIVOTA-Agent):**

- Probe metadata pinpoints the failing query: `pq_candidates=0,
  primary_quality_gate_passed=false, sd_query_semantic_class='default'`
  for lipstick vs `pq_candidates>0, semantic_class='fragrance'` for fragrance
  (which DOES surface our agent seeds — Aventus, Baccarat).
- `inferFragranceSemanticClass()` only classifies fragrance. Lipstick
  / makeup queries fall through to `'default'`.
- **Strong candidate filter:** `src/findProductsMulti/policy.js:5390-5410`
  — beauty-diversity gate requires ≥2 non-tool category buckets; fragrance is
  explicitly exempt (`isFragranceFlow`); lipstick isn't. A pure-lipstick
  result set (1 bucket = `lip_makeup`) gets `filtered = []`.
- **But** the probe reports `external_raw_count=0`, `domain_filter_dropped_external=0`
  — if the diversity filter were the cause, drops would be non-zero. So
  there's likely an upstream filter ALSO returning 0 (the SQL itself).
- Phase 8 (fragrance) commit was **data-only** (50 PDP candidates JSONL); no
  code change went in to enable fragrance retrieval. So fragrance succeeds
  through whatever existing path also exists for lipstick — meaning the
  difference is purely in pre-retrieval expansion / classification, not a
  new fragrance-specific SQL path.

**Recommendation: skip Track A as originally scoped.**

Cold-reading 30k+ LOC across PIVOTA-Agent's `server.js`, `policy.js`, and
`auroraBff/` was sufficient to identify the architectural gap and a
filter candidate, but not enough to confidently land a single-line patch.
A real Track A would need either:
- runtime log instrumentation deployed to staging, OR
- running the gateway locally with a debugger against the prod DB

Both are bigger commitments than "today, ~2 hr" anticipated. Pivot to
Track B, which addresses root cause and avoids guessing:

- **B. Phase 7b — wire gateway recall to JOIN `catalog_offers` /
  `catalog_skus`.** Estimated 1–2 day spike on PIVOTA-Agent side. Needs
  design alignment because the seed-scan code is load-bearing for the
  existing ~80% of recall traffic that isn't agent-authored. The
  comment at `findProductsExternalSeedDirectRetrieval.js:152` already
  flags this work as planned.

**Don't extend Phase 4 to fashion/electronics/home until B is in flight**
— otherwise we're adding to a ghost catalog the gateway doesn't read.

### #2 — Phase 9 regression (45% → 28%) not root-caused

**Symptom:** v9_phase8 (45%) → v9_phase9 (28%). The eye+face ingestion that
was supposed to *add* candidates appears to have *displaced* prior passes.

**Hypothesis:** Phase 9 SKUs ingested with a category_path or pdp_scope that
caused them to outrank fragrance/lipstick passes in some buckets, dropping
those queries to THIN/EMPTY. Probe v10 partially recovered (30%), but neither
fragrance nor face has returned to phase-8 levels for every query.

**Next move:** diff `recall_v9_phase8` and `recall_v9_phase9` per-query to
isolate which queries flipped state. Cheap diagnostic — 10 minutes if
`LAYER_ATTRIBUTION.md` already exists for both.

### #3 — `run_catalog_enrichment.py` swallows `Exception` on table-level INSERTs

**Where:** `scripts/run_catalog_enrichment.py:250, 282, 316` — bare
`except Exception: logger.exception(...)` around each table's INSERT loop.

**Why it matters:** this is how the fragrance Phase 8 SKU UniqueViolationError
went silent (root cause fixed in PR #326, but the data gap remained until
PR #332 backfill today). Same pattern would mask any future FK-target
INSERT failure.

**Fix:** fail-fast on FK-target tables (merchants, products, skus). Per-row
`except` is OK for `catalog_offers` and `external_product_seeds` if at all,
since those are leaf rows. Probably <50 lines of code, half-day with tests.

**Priority:** medium. Not user-facing, but it's the kind of silent-corruption
bug that costs days the next time it strikes.

### #4 — 304 catalog_products rows with NULL `category_path`

**Origin:** Phase 2 regex backfill covered 1216/1535 = 79%. The remaining 304
are pet, fashion, lingerie, and other long-tail categories the regex doesn't
have patterns for.

**Why it's deferred:** Phase 5 success criteria don't require these. Not in
the lipstick/fragrance/makeup recall path.

**When to revisit:** when extending Phase 4 to a new category beyond beauty.
Either expand the regex or run a one-shot enrichment-agent pass over the 304
rows.

### #5 — Phase 4 not yet run for fashion / electronics / home

**Status:** these buckets are still 100% EMPTY in probe v10. The Phase 4
agent + Phase 9 ingestion pattern works (proven by lipstick → fragrance →
eye/face). Repeating it for these buckets would unblock the next ~15 queries
in the corpus.

**Cost:** ~1 hr enrichment-agent runs per category, plus probe re-run.

**Priority:** lower than fixing #1 — there's no point ingesting more PDPs if
the recall path doesn't surface lipstick. Fix #1 first, then extend.

---

## Recommended next steps (priority order, post-cold-trace 2026-05-07)

The trace ruled out Track A as scoped — cold-reading PIVOTA-Agent without
runtime access was insufficient to land a confident single-line patch.

1. **Track B — Phase 7b (next, 1–2 day spike on PIVOTA-Agent side)**
   - Wire `find_products_multi` to JOIN `catalog_offers`/`catalog_skus`
     so canonical PDPs surface in recall.
   - This is the real fix and unblocks every category at once. Must align
     with the existing seed-scan path (load-bearing for ~80% of traffic).
   - Side-quest: while in there, also propagate the agent-bridge clause
     `OR tool = 'catalog_enrichment_agent_v1'` to the seed-query templates
     that lack it (5+ sites in `server.js` and `auroraBff/`).
2. **Track A as a stretch (only with runtime access)**
   - Deploy log instrumentation OR run gateway locally with debugger.
   - Identify which filter actually zeros out `external_raw_count` for
     lipstick. Strong candidate: beauty-diversity gate in
     `findProductsMulti/policy.js:5390-5410` (fragrance is explicitly
     exempted; lipstick isn't).
   - Lower-priority once B lands because the canonical-chain path will
     bypass these seed-side filters anyway.
3. **Parallel work eligible while B is in flight:**
   - Open issue #3 (runner swallow-and-log hardening) — independent, mechanical
   - Open issue #2 (Phase 9 regression) — diff v9_phase8 vs v9_phase9 records
4. **Phase 4 fashion/electronics/home — DEFER until B lands.** Adding more
   PDPs to a catalog the gateway doesn't read is wasted ingestion budget.
5. **304 NULL `category_path` rows** (issue #4) — defer indefinitely until
   Phase 4 expansion resumes.

---

## Probe re-run command

```bash
cd ~/dev/pivota-agent-ui
EVAL_INVOKE_URL=https://agent.pivota.cc/api/gateway \
EVAL_RUN_ID=recall_v$(date +%Y%m%d_%H%M%S) \
EVAL_CONCURRENCY=2 \
node scripts/eval_corpus_recall_runner.mjs \
  --source shopping_agent --entry chat \
  scripts/eval_corpus_recall_v1.jsonl

node scripts/eval_corpus_recall_summarize.mjs <run_id>
node scripts/eval_corpus_recall_layer_attribution.mjs <run_id>
```

Wall clock: ~5 min for the runner. Compare `SUMMARY.md` and
`LAYER_ATTRIBUTION.md` against the trajectory table above; add a new row.

---

## Related docs (don't duplicate; link)

- Recall investigation final handoff: `~/dev/pivota-agent-ui/reports/recall_v1/RECALL_INVESTIGATION_FINAL.md`
- Phase 6 design notes (pdp_scope dimension): `~/.claude/plans/shimmying-soaring-ember.md` (historical)
- BD report architecture: see `BD report:` PR titles in `git log` (#283 → #316 series)
- Canonical PDP sig_* foundation: PRs #327, #329, #330, #331 (separate track from recall)
