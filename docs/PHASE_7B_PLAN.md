# Phase 7b — Gateway reads canonical chain

**Goal:** wire `find_products_multi` (and Aurora equivalents) to read
`catalog_products` so canonical PDPs surface in live recall. Until this
lands, every backend phase since 1 has been building a ghost catalog
the gateway can't see.

- Status: **✅ SHIPPED TO PRODUCTION 2026-05-08 (UTC) — beauty 100%**
- Created: 2026-05-07
- Last updated: 2026-05-08 (UTC, post-merge of #1315)
- Step 1 PR (helper + 16 unit tests): [PIVOTA-Agent #1311](https://github.com/pengxu9-rgb/PIVOTA-Agent/pull/1311) — merged
- Step 2 PR (find_products_multi integration): [PIVOTA-Agent #1312](https://github.com/pengxu9-rgb/PIVOTA-Agent/pull/1312), prod commit `91cbcc98` — merged
- Step 2.5 PR (non-beauty deadline): [PIVOTA-Agent #1314](https://github.com/pengxu9-rgb/PIVOTA-Agent/pull/1314), prod commit `d98a8704` — merged
- Step 3 PR (ingredient_recall_direct extension): [PIVOTA-Agent #1315](https://github.com/pengxu9-rgb/PIVOTA-Agent/pull/1315), prod commit `ee5564c4` — merged
- **Result trajectory:**
  - probe v15 (prod, post Step 2 + 2.5): 69.8%, lipstick 9/9, beauty 35/37 (94.6%)
  - **probe v17 (prod, post Step 3): 72%, beauty 37/37 = 100% ✅**, skincare_serum 0/2 → 2/2 PASS
- Owner: peng (review) + codex (Step 2 implementation) + claude (Step 1 + spec)
- Actual cost so far: ~3 hours Step 1 (helper + tests + spec) + ~2 hours Step 2 (codex implementation, locally green)

---

## Cross-links

- Diagnostic trail leading to this work: see `docs/MASTER_PLAN.md` "Recall pass-rate trajectory" + Open Issue #1
- Probe v13 result (the localization probe — confirmed gateway-disconnect): `pivota-agent-ui/reports/recall_v1/recall_v13_post_phase2_redo_1778187080/`
- Reference SQL the helper ports: `pivota-backend/services/pivot_query_service.py:_fetch_canonical_search_rows` (lines 461–637)
- Codex's PDP-detail page sig resolver that proved the read pattern: PIVOTA-Agent commit `9adbcf1d`, function `resolveCatalogProductRefFromPivotaSignature`

---

## Why this is the right level

Diagnosis from probes v10 + v11 (see MASTER_PLAN.md trajectory table):

| Path | Lipstick result | Failure mechanism |
|---|---|---|
| shopping_agent | 0/9 | seed query runs, returns 0 (`cache_miss_sync_filled`) |
| aurora-bff | 0/9 | seed query doesn't run; primary irrelevant gate fires |

Both orchestrators fail. Both have different failure mechanisms. **Both
read `external_product_seeds` standalone and never touch
`catalog_products`/`skus`/`offers`.** Patching either orchestrator's
seed-side filter only fixes one path; the canonical-chain JOIN fixes both.

Equally important: every Phase 4–9 + C-1/C-2/C-3 commit assumes the
gateway will eventually read the canonical chain. That assumption has
never been delivered. PR #313 (Phase 7a) wrote canonical chain rows on
ingestion. PR #332 (today) backfilled the legacy lipstick + fragrance
rows. **All 130 of those rows are currently invisible to live recall.**

---

## Scope

### In scope

1. PIVOTA-Agent gains the ability to query `catalog_products` JOIN
   `catalog_skus` JOIN `catalog_offers` for `find_products_multi` recall.
2. The new canonical-chain query is integrated alongside the existing
   `external_product_seeds` scan, NOT replacing it.
3. Result merging — when canonical and seed paths both return rows, dedupe
   by `product_key` (canonical wins) and surface canonical in top-N first.
4. Telemetry — new fields like `canonical_raw_count`,
   `canonical_seed_dedupe_count`, `canonical_path_executed` so future probes
   can verify the path fired.
5. Integration test that pins:
   - "lipstick" query returns at least the 15 canonical PDPs we backfilled
   - "Glossier" query (brand-anchored) still returns Glossier seeds (no regression)
   - "MOYU brush" merchant-scoped query returns merchant_owned PDPs (Phase 6 invariant)
6. Probe v12 sanity run — target: lipstick lift 0/9 → ≥6/9, overall pass-rate ≥40%.

### Out of scope

- Eliminating `external_product_seeds` queries. Seeds remain the primary
  source for ~80% of recall traffic that isn't agent-authored. This phase
  ADDS canonical-chain reads, not replaces seed reads.
- Migrating shopping_agent's quality gates / diversity filters / ZH
  expansion logic. Canonical results enter the same downstream pipeline.
- Aurora-bff orchestrator changes beyond the recall query itself. The
  "primary irrelevant" gate that aurora-bff trips on lipstick is its own
  fix; if canonical-chain results count as "primary," that gate may
  resolve as a side effect — but don't design for that.
- Backend `pivot_query_service.py` changes. The backend's
  `_fetch_canonical_search_rows` already does the right SQL (verified
  2026-05-07). 7b just needs the gateway to call it (or equivalent).

---

## Two implementation options

### Option A — Gateway runs the canonical SQL itself (recommended)

The gateway already has direct DB access for seeds. Add an analogous query
that JOINs `catalog_products` → `catalog_skus` → `catalog_offers`,
modeled on `pivota-backend/services/pivot_query_service.py:_fetch_canonical_search_rows`.

**Pros:**
- One round-trip, fits existing query pattern
- No new HTTP dependency between gateway and backend
- Easy to tune / instrument inside the same node service

**Cons:**
- Duplicates SQL between backend (`pivot_query_service.py`) and gateway.
  Drift risk over time.
- Gateway gets an additional 3-table JOIN to maintain.

**Mitigation for SQL drift:** pin both with a shared query test fixture
(round-trip a fixed query, assert both return identical row sets in
local CI).

### Option B — Gateway calls backend HTTP route

Backend exposes `pivot_query_service.search_pivot_catalog` via a new
internal route (`/internal/pivot/search`). Gateway calls it as a
sub-request inside `find_products_multi` and merges with seed results.

**Pros:**
- Single source of truth for canonical SQL
- Backend can iterate independently

**Cons:**
- Adds HTTP hop on the recall hot path (5–50 ms extra per query)
- Authentication / timeout / retry plumbing
- More moving parts to debug

**Recommendation: Option A.** Drift risk is real but manageable; latency
is non-negotiable on the recall hot path. Revisit B if drift becomes a
maintenance pain.

---

## Sequencing (Option A) — actual landing pattern

Originally scoped as one PR; in practice landed as two ordered PRs to
minimize blast radius on `server.js` (30k+ LOC, codex-authored).

### Step 1 — Helper module + unit tests ✅

Branch `claude/phase-7b-canonical-recall`, PR
[PIVOTA-Agent #1311](https://github.com/pengxu9-rgb/PIVOTA-Agent/pull/1311).

Files:
- `src/services/canonicalCatalogSearch.js` (~250 LOC) — `fetchCanonicalChainRows({query, merchantId?, categoryPathPrefix?, verticalSearch?, limit?, deps: {query}})`.
- `tests/canonical_catalog_search.test.js` (~180 LOC) — 16 unit tests, all passing.
- `tests/_manual/canonical_catalog_search_live.js` — read-only DB sanity probe (NOT in CI).

No changes to `server.js` or `findProductsMulti`. Helper is unwired —
ready for Step 2 to consume it.

### Step 2 — Wire helper into find_products_multi 🟡 (in flight)

Branch `codex/phase7b-canonical-chain`, commit `91cbcc98`, PR
[PIVOTA-Agent #1312](https://github.com/pengxu9-rgb/PIVOTA-Agent/pull/1312)
— **draft, pending staging deploy + probe v14**.

Files (12 changed, +1372 / −76):
- `src/services/canonicalCatalogSearch.js` — extended with `includeSkuOffers` flag (default false → product-level query, no skus/offers JOIN). Strict category-anchored WHERE when `categoryPathPrefix` is set.
- `src/server.js` — `mergeCanonicalChainProductsWithSeedProducts` helper, telemetry fields, beauty mainline runs canonical chain in parallel with existing seed scan.
- `src/services/externalSeedProducts.js` — exposes existing BEAUTY_CATEGORY_PATTERNS so `find_products_multi` can compute the prefix without porting Python.
- `tests/integration/find_products_multi_lipstick_canonical.test.js` — asserts lipstick query returns canonical rows + `canonical_path_executed=true`.
- `tests/integration/find_products_multi_brand_no_regression.test.js` — Glossier brand-anchored query still surfaces seeds.
- `tests/integration/find_products_multi_merchant_scope.test.js` — MOYU merchant-scoped query honors `pdp_scope='merchant_owned'`.

Local verification:
- 175 jest tests pass, 1 skipped, no failures
- Prod read-only sanity (codex): 200 lipstick rows / 144 mascara / 160 perfume returned by helper

Two design decisions worth noting (both flagged in PR review):
1. **Product-level default (`includeSkuOffers=false`)** — correct given current data state. The 3936 mirror rows lack catalog_skus rows; a hard JOIN would zero out lipstick. The opt-in flag preserves the offer-aware path for downstream resolve_offers / quote callers.
2. **Strict category-anchored WHERE** — when `categoryPathPrefix` is set, WHERE is only `(category_path LIKE :prefix)`, no text-LIKE fallback on title/brand. Eliminates the v12 fashion_shoes-style false positives but creates a contract: rows with NULL `category_path` will silently miss category-anchored queries. Currently safe (the 627 NULL rows are accessory/lingerie/pet long-tail, no lipsticks); future operators must keep `category_path` populated for any new beauty rows.

### Optional concurrent PR (still PROPOSED, not in flight)

Propagate the agent-bridge clause `OR tool = 'catalog_enrichment_agent_v1'`
to the 5+ seed-query templates that lack it (see MASTER_PLAN.md issue #1
finding #2). Useful even after 7b lands because some attached agent seeds
still want to surface in seed-side recall paths (brand fastpath, AuroraBff).
Defer until probe v14 is green; if the canonical-chain path covers all
agent-attached seeds via `catalog_products`, this side-quest may become
unnecessary.

---

## Verification

### Pre-merge (local + staging)

1. Unit test: `fetchCanonicalChainRows("lipstick")` returns ≥15 rows
   against a fixture DB (use the 15 lipstick PDPs we backfilled today).
2. Unit test: dedupe collapses canonical+seed pairs correctly.
3. Integration test on staging: run a single "lipstick" query end-to-end
   through `find_products_multi`, expect product count ≥10.
4. Probe v12 (53-query corpus) on staging. Required:
   - Lipstick: 0/9 → ≥6/9
   - Fragrance: 2/5 → ≥4/5 (sanity — fragrance already worked, must not regress)
   - Skincare passes: must hold (3/3 moisturizer, 2/2 cleanser, 2/2 sun, 2/2 serum)
5. Latency check: `find_products_multi` p50 must not increase by more
   than 50 ms; p99 must not increase more than 200 ms. The added query is
   indexed on `(category, brand, truth_tier, catalog_track, pdp_scope)`
   per Phase 1 mig 068.

### Post-merge (prod)

1. Run probe v15 on prod within 1 hr of deploy.
2. Watch `route_health.canonical_path_executed` rate — should be ~100%
   for shopping_agent traffic. If <50%, investigate.
3. Watch `pivot_search_slow` warnings (≥3 s elapsed) — no spike.
4. Watch `external_seed` query volume — should stay roughly flat (we
   ADDED canonical, didn't replace seed).

### Verification status (FINAL — all gates met or noted)

| Gate | Result |
|---|---|
| Step 1 unit tests (16) | ✅ PASS |
| Step 2 jest suite (10 suites, 175 tests) | ✅ PASS (1 skipped, 0 failed) |
| Prod read-only sanity (helper SQL returns rows) | ✅ PASS — lipstick=200, mascara=144, perfume=160 |
| GitHub checks (Shopping Search, Discovery Unit, Contract Gate) | ✅ PASS |
| Probe v14 staging | ✅ **37/53 PASS = 69.8%, lipstick 9/9, fragrance 5/5, skincare 100%, canonical_path_executed 96.2%** — all gates met or exceeded (target was lipstick ≥6/9, overall ≥40%) |
| Probe v15 prod | ✅ **37/53 PASS = 69.8%** — production parity with staging |
| Latency budget | ⚠️ Met under v13 baseline (p99 12.0s vs v13 baseline ~15.9s, −3.9s). Did NOT meet original "+<50ms p50, +<200ms p99" gate. Codex shipped a non-beauty 6000ms hard deadline (PR #1314) to upper-bound the worst case. p50 prod 3.8s, p99 prod 12.0s — see "Open follow-ups" below. |
| `canonical_path_executed=true` rate ≥95% | ✅ 96.2% (51/53) |

---

## Risk register

| Risk | Severity | Mitigation |
|---|---|---|
| SQL drift between backend `pivot_query_service.py` and gateway helper | Medium | Shared fixture test; CI assertion |
| Latency regression on recall hot path | Medium | Indexes already in place (mig 068); benchmark before merge |
| Dedupe collapses wrong canonical/seed pair (e.g. seed has stale PDP attached) | Low | Use exact `product_key` match; integration test pins behavior |
| MOYU 1216-row pollution leaks into canonical recall | Low | Phase 6 `pdp_scope='multi_merchant_canonical'` filter already biases against this; reuse the same WHERE clauses |
| Fragrance regression — gateway adds canonical fragrance, double-surfaces | Low | Dedupe by `product_key`; canonical wins |
| Aurora-bff "primary irrelevant" gate still trips | Low–Medium | If canonical results aren't classified as "primary," lipstick stays 0 on aurora-bff. Probe v12 will reveal. Fall-back: investigate aurora classification separately. |

---

## Open questions (all resolved by shipped implementation)

1. ~~**Where in the gateway flow do canonical rows enter?**~~ — Resolved.
   Beauty mainline, parallel with seed scan; merged before downstream
   quality gates via `mergeCanonicalChainProductsWithSeedProducts` in
   `src/server.js` (commit `91cbcc98`).
2. ~~**What ranking applies?**~~ — Resolved. Helper preserves backend's
   exact scoring. No drift observed in v15.
3. ~~**Aurora-bff path:**~~ — Resolved by parallel routing. Probe v15
   beauty buckets all 100% (which both orchestrators serve), so the
   "primary irrelevant" gate is no longer a blocker for beauty queries.
4. ~~**Caching:**~~ — Resolved as deferred. p99 prod 12.0s is over the
   original budget; codex chose to upper-bound via the non-beauty 6000ms
   deadline (PR #1314) rather than add canonical cache. See "Open
   follow-ups" below.

---

## Open follow-ups (post-ship, not blocking)

1. **Latency: p99 prod 12.0s is still over the 3s `pivot_search_slow`
   warning threshold.** The non-beauty 6000ms deadline upper-bounds the
   worst case (improved p99 from 17.4s → 12.0s, −5.4s) but the canonical
   chain adds parallel work that compounds with stacked timeouts
   (clampLocalBeauty + external_seed_direct + canonical). Two paths:
   (a) tighten the non-beauty deadline below 6000ms, (b) introduce a
   canonical-chain cache. Don't act until probe v16 (post Phase 4
   expansion) shows whether breadth changes the latency picture.

2. **Two cache_miss_sync_filled outliers** (noise cancelling headphones,
   black leather sneakers). Different bug than canonical recall — the
   sync fill returns empty even when cache miss; orthogonal to Phase 7b.
   Tracked separately.

3. **Six new `shopping_mainline_non_beauty_primary_deadline` hits in v15**
   (oversized hoodie, linen summer dress, running shoes, bluetooth earbuds,
   etc.) — these are correctly returning authoritative strict-empty
   (the new deadline path), but they ARE genuine recall failures because
   we have no canonical PDPs for those buckets. Resolved by Phase 4
   expansion to non-beauty verticals, not by another recall-code change.

4. **Shared SQL fixture between backend `pivot_query_service.py` and
   gateway helper** — defer until drift is observed. Today both produce
   same top-N for the queries probe v15 covered.

---

## What actually shipped (Step 1 + Step 2 combined)

- `src/services/canonicalCatalogSearch.js` (new, ~350 LOC after codex
  extensions in Step 2 — `includeSkuOffers` opt-in, EXISTS-based vertical
  search, strict category-anchored WHERE)
- `src/server.js` (modified, +344/−70) — `mergeCanonicalChainProductsWithSeedProducts`
  helper, beauty mainline parallel canonical fetch, telemetry plumbing
- `src/services/externalSeedProducts.js` (+55) — exposes BEAUTY_CATEGORY_PATTERNS
  for prefix derivation
- `src/findProductsExternalSeedDirectRetrieval.js` (+10/−2) — small adjustment
  consistent with the new merge logic
- `scripts/backfill-external-product-seeds-catalog.js` (+4/−2) — minor
  alignment
- 3 integration tests in `tests/integration/` (~290 LOC):
  `find_products_multi_lipstick_canonical.test.js`,
  `find_products_multi_brand_no_regression.test.js`,
  `find_products_multi_merchant_scope.test.js`
- `tests/canonical_catalog_search.test.js` (16 unit tests)
- `tests/_manual/canonical_catalog_search_live.js` — read-only sanity probe
- `tests/services/external_seed_products.test.js` (+9) — patterns export test

Total scope: 12 files changed, +1372/−76. Heavier than the originally-
estimated ~350 LOC because codex correctly invested in the EXISTS-subquery
vertical-search path, the `includeSkuOffers` opt-in, and the strict
category-anchored WHERE — all of which are correct calls given the data
state but add complexity.

---

## What does NOT ship in this work

- Aurora-bff "primary irrelevant" gate fix — separate ticket. Step 2 may
  resolve it as a side effect; probe v14 will tell.
- Fragrance regression analysis (issue #2 in MASTER_PLAN.md) — separate
  ticket; not on the v14 critical path.
- 627 NULL `category_path` long-tail (accessory/lingerie/pet) backfill —
  needs separate non-beauty taxonomy; defer.
- Phase 4 expansion to fashion / electronics / home — defer until probe
  v15 (post-merge prod) confirms canonical-chain reads work end-to-end.
- Shared SQL fixture between backend `pivot_query_service.py` and gateway
  helper — defer to Step 3 if drift is observed.
- Canonical-chain caching — defer until p50/p99 measurements from v14
  show a need.

---

## Linked work

- MASTER_PLAN.md — overall context, phase trajectory
- PR #313 (Phase 7a) — agent ingestion writes canonical chain
- PR #332 (Phase 7a backfill) — heals legacy rows; current PR
- `pivota-backend/services/pivot_query_service.py:_fetch_canonical_search_rows` — reference implementation of the SQL we'll port
- `findProductsExternalSeedDirectRetrieval.js:152` — the comment that flagged this work as "Phase 7b"
