# ADR-009 orphan residue — reconnaissance findings and disposition proposal

Date: 2026-08-16. Read-only. Source: `scripts/recon_sentinel_orphans.py` (PR
#1757, run against prod through the Railway proxy 2026-08-16 ~15:30 UTC) plus
three follow-up read-only probes. Nothing was written. **This document
recommends; the founder gates every disposition.**

## State

`catalog_products` under the sentinel: **0**. Verifier
(`scripts/verify_seller_rekey.py`) verdict: **FAIL** on `orphan_residue` for
five ownership tables (`index_pipeline_state`, 9 on 2026-08-15, is now **0** —
the nightly IPS job / rescore re-derived it; IPS.merchant_id is a cache of the
elected catalog row and self-heals). Global residue as measured:

| bucket | table | rows | what the rows are |
|---|---|---|---|
| ownership | catalog_offers | 12 | US-market capture offers, products EXIST and were moved |
| ownership | product_reviews | 9 | QA canary reviews (2026-05-20) keyed in the reviews' own `merchant\|platform\|id` namespace |
| ownership | evidence_items | 5 | `product_key IS NULL`, one audit run (`ccb9deea…`, 2026-06-30) |
| ownership | action_plan_items | 2 | `product_key IS NULL`, same audit run |
| ownership | niche_target_outcomes | 317 | `content_key IS NULL` on all 317, one audit run (`49a80341…`, 2026-06-30) |
| history | pdp_identity_listing | 103 | listings of seeds that were never materialised (102 `inactive`, 1 `review_blocked`, all unattached), 91 active operator approvals attached |
| history | product_enrichment | 8 | curated copy for products that EXIST under `merch_obs_*`, target identity vacant |
| history | merchant_audit_runs | 8 | audit runs whose TENANT was `external_seed` |
| history | 12 other event/log tables | ~222k | true event-time records — correct as they stand |

## Per-table findings

### catalog_offers = 12 → **(b) re-key to the product's current merchant** + writer hardening
- All 12: `product_exists=true`, checkpoint `done`, current merchant `merch_obs_*`
  (6 distinct sellers). Every one of these products ALSO carries its moved
  mirror offer under the observed seller (`n_offers_total` 2–3), so the orphan
  is a second, sibling offer row — not the product's only offer.
- All 12 are `source_system='us_market_capture'` (`scripts/capture_us_market_offers.py`,
  PR #1751), created 2026-08-14 02:09–02:23 UTC — the capture selects its
  candidates up front (with `cp.merchant_id` as it was: still the sentinel), then
  probes each store over HTTP for minutes, then upserts. The flip moved those
  products and cascaded `catalog_offers` in between; the upsert landed after
  the cascade with the stale merchant. A TOCTOU, not a stray writer. (Confirmed
  in code: `CANDIDATES_SQL` is fetched once at `capture_us_market_offers.py:337`
  carrying `cp.merchant_id`, the per-domain HTTP probing starts at `:351`, and
  `plan_offer` at `:204` stamps `candidate["merchant_id"]` — the snapshot
  value, by then minutes stale.)
- Disposition: `UPDATE catalog_offers o SET merchant_id = cp.merchant_id FROM
  catalog_products cp WHERE cp.product_key = o.product_key AND o.merchant_id =
  <sentinel> AND cp.merchant_id <> <sentinel>` — abort if any residue row's
  product is missing (none is today). `offer_id` is the PK, so no conflict.
  Door re-measured 2026-08-16: of the 12, `product_missing = 0` and
  `target_still_sentinel = 0`, so the door passes today and would abort loudly
  if either became non-zero before the apply.
- Writer hardening (same PR, primary route): the capture upsert should stamp
  `merchant_id` from the catalog row **at write time** (subselect on
  `product_key`), never from the candidate snapshot. Hardening alone cannot
  repair the existing 12: `OFFER_UPSERT_SQL`'s `ON CONFLICT (offer_id) DO
  UPDATE` does not touch `merchant_id`, so a re-run would leave them as they
  are. The re-key UPDATE is mandatory, not optional.

### product_reviews = 9 → **(c) delete with a row dump** (alt: re-key + `status='removed'`)
- The reviews service builds its OWN key namespace: `product_key =
  merchant_id|platform|platform_product_id`, `sku_key = …|variant-or-∅`
  (`services/reviews_service.py:186`). The flip's cascade (catalog `product_key`)
  could never match these rows; the recon's `by_product` join was blind for the
  same reason (`product_exists=false` is an artefact — noted below).
- Resolved by `(platform, platform_product_id)`: the product EXISTS as
  `prod::external_seed::external_seed::ext_301201d0…` under
  `merch_obs_0337120ae149ea52`; the seed is active and attached; the target
  reviews key under that merchant is vacant.
- All 9 are `source_type=native / source_system=accounts / unverified / no
  author`, titles "Pivota QA DS moderation … canary", "Pivota QA TEST review
  20260520_…" — the 2026-05-20 moderation canary run, not merchant content.
- **Already out of every public reader**: `status` is `removed` on 8 and
  `under_review` on 1. So this is cleanup, not an exposure — nothing is
  serving today, and the "re-key + `status='removed'`" variant is a near
  no-op that would still park QA rows on a live tenant.
- A delete cascades 6 `media_assets` rows (`ON DELETE CASCADE`, migration 040);
  no `review_replies` / `review_interactions` / `review_featured` rows exist.
  The row dump must include the media rows.
- Recommendation: hard-delete the 9 rows (+ 6 cascaded media), printing every
  row as JSON in the run log first (reversible by re-insert). Founder's call.

### evidence_items = 5, action_plan_items = 2, niche_target_outcomes = 317 → **(a) reclassify**
- Every row has a NULL scope column (`product_key` / `content_key`). Their
  `merchant_id` is the audit's TENANT (migration 088: "tenancy is single-layer…
  add merchant_id"; `services/niche_outcomes.py` writes `merchant_id = the
  audited merchant`, never `content_key` — the column was added by migration
  158 for future deposits and no writer fills it). Two BD-report audit runs were
  executed FOR merchant `external_seed` on 2026-06-30; these rows, and the 8
  `merchant_audit_runs`, are that run's history.
- `niche_target_outcomes` specifically: migration 155 calls it "a history —
  one row per (merchant, niche query, audit run)"; the reader compares the two
  most-recent runs per (merchant, query). It is outcome-history, misfiled as
  ownership only because migration 158 bolted a nullable `content_key` on.
- No re-key can follow a NULL scope; rewriting the tenant would move one
  tenant's audit history to another. Leave the rows.
- Verifier fix: count ownership residue only where the scope column is
  **NOT NULL**; report NULL-scope sentinel rows in a third bucket
  (`unscoped`) — never failed, but visible, so a writer that stamps the sentinel
  and forgets its scope cannot hide. Documented reason in the code.

### product_enrichment = 8 → **(b) via the existing primitive** (`scripts/reattribute_orphaned_enrichment.py`)
- All 8: same platform, same `source_product_id`, product exists under
  `merch_obs_*` (3 sellers), **target identity vacant**, all carry bullets.
  Re-measured by the hardened tool 2026-08-16: `n_cache_rows=0`,
  `target_occupied=false`, `n_orphans_to_same_target=1`, `has_content=true` on
  all 8 — i.e. every H0 precondition now comes from the recon itself rather
  than a side probe.
  This is exactly the tool's H0 (cross-merchant exact id, restricted to
  `external_seed`) with bilateral uniqueness and vacancy satisfied. Re-checked
  2026-08-16 against the tool's OWN preconditions: `products_cache` holds 0 rows
  under the sentinel (so its orphan guard, which requires absence from BOTH
  `catalog_products` and `products_cache`, passes on all 8), and no two orphans
  map to one target (bilateral uniqueness holds).
- `product_enrichment` has no product-scope column, so the flip's reflection
  never saw it; the earlier re-attribution run (2026-08-14) preceded these
  products' move. Re-run the tool: dry-run first (expect 8 accepted under H0),
  then `--apply` (it re-keys `merchant_id`, republishes through the canonical
  refresh, and recomputes eligibility). No new tool.

### pdp_identity_listing = 103 → **(b) migrate refs, optional / lowest priority** (alt: leave as history)
- `product_id` matches no catalog row's `source_product_id`; every one matches a
  seed that is `inactive` (102) or `review_blocked` (1), **unattached**, with
  `seller_ref = merch_obs_*` (7 sellers). Nothing serves them today —
  independently confirmed by the hardened tool: `n_seeds_active = 0` and
  `n_seeds_attached_unsuppressed = 0` for all 103.
- Operator work attached: 91 active `approve_live_read`, 18 active
  `force_review_required`, 10 inactive. `new-ref` conflict check
  (`<seller_ref>:<product_id>` already present): **0**.
- If a seed is later re-activated and materialised, the materialiser mints the
  listing under `<seller>:<id>` and this operator work is stranded. Migrating
  the refs is the flip's own `_migrate_listing_refs` contract (rename
  `source_listing_ref`, `merchant_id`; move overrides/queue rows; 0 conflicts).
- The verifier files this table as history because it lacks a product-scope
  column, yet its `source_listing_ref = merchant_id:product_id` embeds the
  seller (ADR-008). Open verifier question below.

## Verifier changes proposed (part of the disposition PR)
1. Ownership residue counts rows with a **non-NULL** scope; NULL-scope rows go
   to a reported `unscoped` bucket (reason: tenant attribution, no product to
   follow). Clears evidence_items / action_plan_items / niche_target_outcomes
   without touching a row.
2. Open: identity-keyed tables. Rule candidate — a table whose `merchant_id`
   participates in its PRIMARY KEY / a UNIQUE constraint is ownership-by-identity
   (catches `product_enrichment`; **not** `pdp_identity_listing`, whose key is the
   composite string). Alternative: leave both in history and rely on this sweep.
   Recommend rule 2 for `product_enrichment` (schema-derived, no hand list) and
   an explicit, reasoned entry for `pdp_identity_listing`.

## Recon-tool notes
Adversarial review (2026-08-16) mutated the script 23 ways; 12 mutants survived
the fake-DB suite, so the recon now also carries a real-Postgres gate
(`tests/test_recon_sentinel_orphans_postgres.py`) whose assertions were each
confirmed to die under mutation. Fixed in the same PR:
- `by_product` for `product_reviews` joined on catalog `product_key` while the
  table keys in its OWN namespace, reporting "product missing" for a product
  that exists — the right disposition for the wrong reason. Every table now
  reports `scope_key_space` (`resolves_to_catalog` k/N, `keys_are_catalog_keys`),
  so a foreign key space is loud instead of silently mimicking deletion.
  Measured: `product_reviews` 0/9, `catalog_offers` 12/12.
- `n_seeds_attached_live` did not test suppression — a seed attached to a
  tombstoned product counted as live, and the listing disposition rests on that
  column. Now `n_seeds_attached_unsuppressed`, with `n_seeds_active` beside it.
- `has_content` treated an empty bullet array as content (`'[]' IS NOT NULL`).
- The enrichment classifier now reports the re-attribution tool's ACTUAL
  preconditions — `n_cache_rows` (its orphan guard also requires absence from
  `products_cache`), `target_occupied`, `n_orphans_to_same_target` — rather than
  implying vacancy the tool never measured. **Note the H0 restriction is
  `platform = 'external_seed'`, a platform test, not a merchant test.**
- Seller-column precedence now matches the flip's (`merchant_id` over
  `primary_merchant_id`); the identifier guard runs over the whole seller
  surface BEFORE any count, since the verifier's `global_residue` interpolates
  names unguarded and runs first.
- Sample reports `ordered_by` (null when a table has no timestamp column, so an
  arbitrary sample is not read as "the newest N"); `--sample < 1` aborts.
- The identity-join lint exemption was **removed**: aliasing the id in the CTE
  keeps the cross-merchant measurement and leaves the file under both lints
  permanently, which is strictly better than a file-scoped mute (that test's own
  header documents why).
- A prod-only dialect defect the local fixture missed: `bullet_points` is `json`
  in prod (the fresh-DB backstop DDL says JSONB), so `coalesce(...,'[]'::jsonb)`
  raised `CannotCoerceError` on the live run. Cast added; the gate fixture now
  uses `json` to match prod.

## Review round 2 (2026-08-17) — what the adversarial pass changed
Three findings were real defects in the first cut of the disposition tool, each
reproduced on a real engine:
1. **`apply()` re-read the population** instead of consuming the plan, so any
   row that became residue in the plan→apply window was written with no door
   having examined it and no dump recording it. Reproduced: a review inserted
   after the plan — still SERVING, and whose `product_key` resolves — was
   deleted, and it appeared in no dump. Now the plan's id set is binding and a
   mismatch aborts the table.
2. **The cascade-child list was hardcoded and incomplete.** Derived from
   `pg_constraint` instead. The live dry-run then found
   **`buyer_review_user_subject` = 9 rows** (created at runtime by
   `services/ugc_capabilities_service.py`, invisible to anyone reading migration
   040) plus `buyer_review_ownership` and a SET-NULL parent — i.e. the original
   "reversible by re-insert" claim was false by 9 rows.
3. **The verifier's `unscoped` bucket was a loophole** for tables carrying more
   than one scope column (`beauty_compatibility_rules`,
   `catalog_quote_snapshots` carry both `product_key` and `sku_key`): a
   sku-scoped row with a NULL `product_key` was excused. The split now spans
   every scope column (`num_nonnulls`).

Also: the row dump is written to a file and uploaded as a workflow artifact
(the run log is subject to retention and was the only copy); the printed copy
redacts review text and account ids; an unreadable cascade child is now door
**D7** rather than a recorded-and-ignored note; the verifier step runs under
`if: always()` so a half-applied state still gets graded.

## Execution (founder gated 2026-08-17)
Built as `scripts/dispose_sentinel_orphans.py` + `.github/workflows/adr009-orphan-dispose.yml`
(dry-run default; `apply=true` is a separate dispatch). Prod dry-run 2026-08-17:
all doors pass, 12 offers plan onto 6 distinct `merch_obs_*` sellers, 9 reviews
dumped with 6 cascaded `media_assets`.

On the one review that does not read like a canary: id 9320 "Eczema improvement
question" is the `needs_human_review` arm of the same battery — its own body
ends "please review before showing this", same product, same guest-actor
pattern, same three-minute window as the other eight, and its risk_flags carry
`moderation_decision: needs_human_review` with `employee_review_queue: true`.
Deleting it also removes that one item from the employee review queue.

The verifier change ships in the same PR: ownership counts only rows whose
scope key is NOT NULL; NULL-scope sentinel rows move to a reported `unscoped`
bucket. After the apply, expected verdict OK with `unscoped` = evidence_items 5,
action_plan_items 2, niche_target_outcomes 317.

The writer is hardened in the same PR: `capture_us_market_offers` now reads the
seller from the catalog row inside the upsert (both the insert and the
ON CONFLICT refresh), so the TOCTOU cannot recur.

## Original proposal (superseded by the section above)
`scripts/dispose_sentinel_orphans.py --tables catalog_offers,product_reviews [--apply]`
- dry-run default; per-table transaction; every row about to change/delete
  printed as JSON before the write; doors: bucket must be 0, every offer's
  product must exist and be off the sentinel, every review target key must be
  vacant / every review must fail to resolve under its own key; sig-frozen SQL
  assert; bind-exact fake tests; verifier grades (`orphan_residue` empty →
  verdict OK).
- enrichment: existing tool; listings: flip's `_migrate_listing_refs` via a
  thin driver, if approved.
- writer hardening in `scripts/capture_us_market_offers.py`.

## Gateway sentinel readers (task 4 sizing — separate PR after verdict OK)
Mapped read-only in PIVOTA-Agent: **22 production files, ~155 sentinel-merchant
comparison sites** (62 in `src/server.js`), 7 independent constant definitions
of the literal, and a shrink-only ratchet
(`tests/scripts/external_seed_merchant_literal_ratchet.test.js` + baseline JSON)
that must be lowered in the same PR — but whose regex leaves ~14 readers
unbaselined, by **two** distinct evasions (verified 2026-08-16): camelCase
(`src/pdpBuilder.js:248`, `src/services/catalogServingIndex.js:449`) and, more
often, a WRAPPER around a snake_case field the regex needs adjacent to the
operator — `asString(product?.merchant_id) === …`
(`src/services/pdpIngredientAuthority.js:956`), `String(x?.merchant_id || '')
.trim() === …` (`src/findProductsSearchRouteEntry.js:125/383`). The wrapper
class is the wider gap; a ratchet regex fix must cover both. Highest-leverage fixes: the two
minting sites `src/pdpConfig.js:21` (`inferCanonicalPdpMerchantId`) and
`src/productIntelResolve.js:6` (`inferMerchantIdFromProductId`) that still turn
any `ext_` id into a `merchant_id: 'external_seed'` ref — refs that now join
nothing; and the `server.js:40920–40930` `entryProductIsExternalSeed` cluster,
whose four consumers re-test the bucket themselves and must move to
`source_system` together (a widening-alone attempt was tried and reverted).
Platform/source-label uses (`platform === 'external_seed'`) stay — that lane
survives the re-key.
