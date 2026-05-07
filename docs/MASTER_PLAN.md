# Master Plan — PDP-as-canonical catalog migration

**Live source of truth.** Update on every meaningful step. Originated from the
recall investigation closed at 23% pass-rate; tracks every phase since.

- Last updated: 2026-05-07
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

**Headline insight:** every probe since v9_phase8 has *more* canonical data
than the previous one, yet pass-rate has not returned to peak. The bottleneck
is no longer "do the rows exist" but "does the recall query reach them."

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

### #1 — Lipstick recall returns zero candidates despite full canonical chain (BLOCKER)

**Symptom:** all 9 lipstick queries (`lipstick`, `red lipstick long-lasting`,
`口红`, `推荐口红`, `平价口红`, etc.) layer-attribute to **C-external_seed /
seed_ran_returned_zero**. The seed query executes (executed=true) but
returns raw=0.

**Why it's surprising:** the data is there.
- 15 lipstick `catalog_products` (verified)
- 18 lipstick `external_product_seeds` (verified)
- 15 lipstick `catalog_skus` (just inserted today via PR #332 backfill)
- 18 lipstick `catalog_offers` (just inserted)
- All `pdp_scope='multi_merchant_canonical'` (Phase 6 + 7a)

**Hypotheses to test (in order):**
1. Lipstick PDP/seed `category_path` doesn't match the recall SQL's category
   filter (e.g. SQL looks for `'beauty/lip%'` but rows have `'beauty/makeup/lip/lipstick'`).
2. ZH→EN alias dict (PR-04 in PIVOTA-Agent) doesn't expand `口红`.
3. Phase 2b's PDP-first JOIN never engages for lipstick queries — the seed-side
   text-LIKE scan is short-circuiting before the JOIN runs.
4. Cache poisoning from the v9_phase9 regression — but probe v10 shows mostly
   `cache_miss_sync_filled`, not cache_hit, so cache isn't the cause.

**Next move:** read `services/pivot_query_service.py:_fetch_canonical_search_rows`
+ `_fetch_external_fallback_items` end-to-end against a sample lipstick query
  trace. Don't try fixes blind. ~1 hour read; pin a unit test for whichever
  hypothesis confirms.

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

## Recommended next steps (priority order)

1. **Diagnose open issue #1** — read recall path, find why lipstick seeds
   return 0 even with full canonical chain. Don't ingest more data until
   this is understood.
2. **After #1**: run probe v11. Target: lipstick lift 0/9 → ≥6/9.
3. **Then** root-cause #2 (Phase 9 regression). May resolve as a side effect
   of #1 if both are in `_fetch_canonical_search_rows`.
4. **Parallel work eligible** while #1 is in progress:
   - #3 (runner hardening) — independent, mechanical
   - Stretch: pick the next Phase 4 category for when #1 closes
5. **Phase 4 fashion/electronics/home** — only after #1 + #2 close, otherwise
   we're adding to a recall path that doesn't surface what we have.

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
