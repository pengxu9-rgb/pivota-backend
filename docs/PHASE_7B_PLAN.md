# Phase 7b — Gateway reads canonical chain

**Goal:** wire `find_products_multi` (and Aurora equivalents) to read
`catalog_products` / `catalog_skus` / `catalog_offers` so canonical PDPs
surface in live recall. Until this lands, every backend phase since 1
has been building a ghost catalog the gateway can't see.

- Status: **PROPOSED — not yet started**
- Created: 2026-05-07
- Owner: TBD (PIVOTA-Agent side)
- Estimated cost: 1–2 day spike + 0.5 day staging probe + integration tests

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

## Sequencing (Option A)

Land in this order, all in **one PR** for atomicity:

1. **Add `fetchCanonicalChainRows()` helper** in PIVOTA-Agent
   (`src/services/canonicalCatalogSearch.js` — new file). SQL ported from
   `pivot_query_service.py:_fetch_canonical_search_rows`. Same WHERE,
   same rank scoring, same `pdp_scope='multi_merchant_canonical'` bonus.
2. **Wire into `findProductsMulti`** flow — call `fetchCanonicalChainRows`
   in parallel with existing seed scan. Both feed into the candidate pool
   before quality gates.
3. **Dedupe + merge** — canonical and seed rows that share `product_key`
   collapse to one row (canonical wins). Both seed-only and canonical-only
   rows pass through.
4. **Telemetry** — add `canonical_raw_count`, `canonical_path_executed`,
   `canonical_dedupe_count` to route_health and metadata.
5. **Tests** — three integration tests (see "In scope #5" above).
6. **Probe v12** run on staging before merging to main.

### Optional concurrent PR (PIVOTA-Agent)

Propagate the agent-bridge clause `OR tool = 'catalog_enrichment_agent_v1'`
to the 5+ seed-query templates that lack it (see MASTER_PLAN.md issue #1
finding #2). Useful even after 7b lands because some attached agent seeds
still want to surface in seed-side recall paths (brand fastpath, AuroraBff).

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

1. Run probe v13 on prod within 1 hr of deploy.
2. Watch `route_health.canonical_path_executed` rate — should be ~100%
   for shopping_agent traffic. If <50%, investigate.
3. Watch `pivot_search_slow` warnings (≥3 s elapsed) — no spike.
4. Watch `external_seed` query volume — should stay roughly flat (we
   ADDED canonical, didn't replace seed).

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

## Open questions (resolve during design, not post-hoc)

1. **Where in the gateway flow do canonical rows enter?** Before or
   after the ZH→EN query alias expansion? Before or after the brand
   fastpath? — Decide based on what produces the cleanest layer trace.
2. **What ranking applies?** Reuse backend's exact scoring
   (`pdp_scope='multi_merchant_canonical' = +200 bonus`,
   `category_path LIKE = +90`, etc.) or define gateway-specific ranking?
   — Recommend: copy backend's exactly to start; tune later if needed.
3. **Aurora-bff path:** does it share the same `findProductsMulti` flow
   or have its own? Probe v11 showed aurora-bff has a "primary irrelevant"
   gate shopping_agent doesn't have. Trace this in the design phase.
4. **Caching:** the existing `cache_miss_sync_filled` mechanism on seed
   queries — does canonical chain need its own cache, or piggyback on
   the same key? — Probably piggyback for simplicity; flag for review.

---

## What ships in this PR

- `pivota-agent/src/services/canonicalCatalogSearch.js` (new) — ~150 LOC
- `pivota-agent/src/findProductsInvokeSearchSupplements.js` or equivalent
  (modified) — ~30 LOC integration
- `pivota-agent/src/findProductsSearchTelemetry.js` (modified) — ~10 LOC
  for new metadata fields
- 3 new test files, ~150 LOC total
- One staging probe + one prod probe report linked in PR description

Total scope: ~350 net LOC + tests. Reviewable in one sitting.

---

## What does NOT ship in this PR

- Aurora-bff "primary irrelevant" gate fix — separate ticket if needed
- Fragrance regression analysis (issue #2 in MASTER_PLAN.md) — separate ticket
- 304 NULL `category_path` rows backfill (issue #4) — defer
- Phase 4 fashion / electronics / home expansion — defer until 7b lands
  and probe v12 confirms canonical-chain reads work end-to-end

---

## Linked work

- MASTER_PLAN.md — overall context, phase trajectory
- PR #313 (Phase 7a) — agent ingestion writes canonical chain
- PR #332 (Phase 7a backfill) — heals legacy rows; current PR
- `pivota-backend/services/pivot_query_service.py:_fetch_canonical_search_rows` — reference implementation of the SQL we'll port
- `findProductsExternalSeedDirectRetrieval.js:152` — the comment that flagged this work as "Phase 7b"
