# ADR-008: Brand-Identity Reconciliation Across External-Seed and Connected-Merchant Catalogs

**Status:** Proposed
**Date:** 2026-07-03
**Deciders:** Commerce-index / Trust & Identity owners (peng)

## Context

The commerce index ingests a brand's products through two independent paths that
never reconcile:

- **Path B — crawl onboard** (`scripts/onboard_external_brand_from_crawl.py` →
  `scripts/mirror_external_seeds_to_catalog_products.py`): rows land under the
  **shared** `merchant_id='external_seed'`, `catalog_track='external_referral'`,
  and — critically — are **published**: they get a `pivota_signature_id` and a
  `pivota_canonical_url` on `agent.pivota.cc`, become `serving_eligible`, and are
  the identities agents actually cite.
- **Path A — connected merchant** (`routes/merchant_store_connections.py` →
  `services/catalog_sync_service.py:931`): rows land under the brand's real
  `merchant_id`, `catalog_track='internal_merchant'`, with **no signature and no
  canonical URL** — unpublished.

`content_key` is minted deterministically from the product, not the merchant
(`services/catalog_identity.py:133` `make_content_key = ck_ + sha256(normalize_brand ::
normalize_title :: gtin)`), and Path B always passes `gtin=None`
(`mirror_external_seeds_to_catalog_products.py:1058`). The canonical entity
grouper unifies listings **only when they already share a `content_key`**
(`src/services/catalogEntityResolution.js:790` `resolveCanonicalCatalogEntityGroup`).
So the same brand's differently-titled (Korean vs English) or differently-GTIN'd
SKUs land on **different keys and are never linked**.

There is **no reconciliation at connect/sync time**, and **no content_key
merge/alias primitive**. `approve_first_party_canonical`
(`src/services/pdpIdentityGraph.js:4836`) is a *label flip* on a single
`pdp_identity_listing` (sets `identity_status='approved'`, `source_tier='brand'`)
— it does **not** collapse two identities or reassign a key. Existing merge
scripts (`scripts/align-external-seed-identity-to-catalog-sig.cjs`,
`review-and-merge-same-canonical-external-seed-identities.cjs`) write
`force_exact_group` overrides but operate **within/among external_seed by
signature**, not across a connected merchant.

**Why it matters (impact surfaces):**
- **Citation deposit** — `resolve_deposit_content_key`
  (`services/catalog_identity.py:220`) deposits entity-scoped citations only on a
  GTIN / high-confidence-identity / reviewed key; a fragmented, unpublished
  connected listing deposits on an **`unresolved`** basis and is silently
  dropped. The brand's citation signal splits across two keys and one half never
  accretes.
- **Serving** — `agent_pdp_view` is `content_key`-keyed
  (`services/agent_pdp_view_assembler.py:73`; recall join
  `services/pivot_query_service.py:1191`), so two keys = two PDP rows, and the
  connected first-party SKUs — which carry the better US-market English title —
  aren't the ones served/cited.

**Observed case (ANUKO, DB-verified 2026-07-03):** the brand exists as an
`external_seed` set (4 Korean SKUs — brush, 2 oils, butter kit — all with
signatures + canonical URLs) and a connected `merch_924…` set (Hair Butter 200ml
in English + a Korean shampoo, no signatures). **No shared content_key or
signature.** Note the two sets are **mostly different SKUs**, so this is
brand-level fragmentation, *not* a pile of duplicate keys to merge. The shampoo
row was created 2 s after URL-wedge audit `bfabfe9c` — i.e. the audit's
index-intake (`ENABLE_AUDIT_INDEX_INTAKE`) minted a **fresh brand-fragmenting
row** under the wedge/connected identity instead of attaching to the brand's
existing canonical.

**Blast radius today:** exactly **one** genuine case — and `merch_924…` is our
**bare-signup pitch demo account**, not ANUKO actually connecting. The other 7
brand-name overlaps are large multi-brand **test merchants**
(`merch_bbd34645…`, `merch_efbc46b4…` [the stale billing orphan],
`merch_test_ownist_001`) — coincidental brand-name matches, not real
same-brand connects.

## Decision

**Prevent, don't retro-merge.** Build the two prevention paths and explicitly
defer the expensive merge primitive and any ANUKO backfill:

1. **Reconciliation-at-connect (primary).** When a merchant connects/syncs,
   detect an existing `external_seed` canonical identity for the same brand and
   reconcile the connected listing **to the seed's canonical `content_key` /
   entity group** — then flip `approve_first_party_canonical` so the connected
   store becomes the first-party owner of the already-published identity. Reuse
   the existing `force_exact_group` + `approve_first_party_canonical` override
   machinery; add the trigger + the brand-match proposal, which are what's
   missing.
2. **Audit-intake guard (secondary, cheap, independent).** Before
   `ENABLE_AUDIT_INDEX_INTAKE` mints a catalog row from a URL-wedge audit, check
   for an existing brand/canonical identity (by brand + host) and attach/skip
   rather than mint a fragmenting duplicate. This closes the vector that created
   the ANUKO shampoo row.
3. **Do not backfill-merge ANUKO or the test merchants now.** One demo account,
   mostly-distinct SKUs, near-zero production value, real risk (identity merge in
   a shared index that feeds citations + serving). If a *real* seed brand
   connects, run reconciliation (#1) for that brand specifically.
4. **Defer a general `content_key` merge/alias primitive** until a real
   same-SKU-different-key collision appears. It's the highest-risk, lowest-demand
   piece; reconciliation-at-connect handles the forward case without it.

## Options Considered

### Option A: Reconciliation-at-connect + audit-intake guard (recommended)
| Dimension | Assessment |
|-----------|------------|
| Complexity | Medium — new trigger + brand-match proposal; reuses existing override machinery |
| Cost | Bounded — runs per connect/sync, not a mass migration |
| Scalability | Fixes every future connect; no growing backlog |
| Team familiarity | High — same `pdp_identity_override` / `force_exact_group` path used by existing merge scripts |

**Pros:** stops the bleed at the source; reuses proven levers; no risky bulk
mutation; targets the exactly-one real future scenario (a seed brand connecting).
**Cons:** doesn't retroactively unify already-fragmented rows (acceptable — the
only ones are a demo + test merchants).

### Option B: Build a general content_key merge/alias primitive + backfill
| Dimension | Assessment |
|-----------|------------|
| Complexity | High — new alias table, citation/serving redirect, re-materialization |
| Cost | High — plus a reviewed bulk backfill over fragmented brands |
| Scalability | Powerful but speculative |
| Team familiarity | Low — no existing content_key merge path |

**Pros:** can unify even different-key/same-SKU cases; reusable primitive.
**Cons:** highest blast radius in the shared index; solves a problem that has
**one demo instance**; premature. Deposit/serving redirect is genuinely hard.

### Option C: Label-only (`approve_first_party_canonical` on the connected listing)
| Dimension | Assessment |
|-----------|------------|
| Complexity | Low |
| Cost | Low |
| Scalability | N/A — doesn't actually unify |

**Pros:** trivial; publishes the connected first-party listing.
**Cons:** does **not** merge identities or keys — the brand stays split across two
content_keys; citation signal still fragments. Treats a symptom.

### Option D: Do nothing
**Pros:** zero cost; today's impact is a demo account.
**Cons:** the gap is systemic — the first real seed-brand connect silently
fragments and mis-serves. The failure is invisible (`unresolved` deposits are
dropped without warning), so it won't be noticed until citation coverage is
mysteriously low.

## Trade-off Analysis

The core tension is **prevention vs. retro-merge**. Retro-merge (B) is where all
the risk lives — rewriting identity/citation/serving keys in a shared index — and
the demand for it is a single demo account. Prevention (A) reuses levers the team
already ships (`force_exact_group`, `approve_first_party_canonical`) and fixes the
only real future case (a seed brand connecting) at the moment it happens, when we
have both identities in hand and can reconcile cleanly. Option C is a trap: it
*looks* like a fix (the listing goes live) but leaves the keys split, so citation
signal keeps fragmenting silently. Option D's real cost is that the failure mode
is **silent** (`DEPOSIT_BASIS_UNRESOLVED` rows vanish), so "do nothing" defers
detection to a confusing coverage regression.

## Consequences

**Easier:** future seed-brand connects unify automatically; connected first-party
SKUs inherit the published canonical identity and the US-market English title;
citation signal accretes on one key; the audit-intake stops minting duplicates.

**Harder / to revisit:** already-fragmented rows stay split until a brand-specific
reconciliation is run (fine for a demo/test); if a real **same-SKU-different-key**
collision ever appears (e.g. a GTIN-bearing connect vs a GTIN-less seed), we still
need the deferred merge primitive (B) — revisit then.

**Watch:** add a metric/log when a deposit resolves to `DEPOSIT_BASIS_UNRESOLVED`
so silent fragmentation becomes visible (turns Option D's hidden failure into a
signal). Reconciliation must key on a **normalized brand + domain** match, not raw
brand string (casing/alias differ: "ANUKO" vs "Anuko") — reuse
`services/brand_alias.py`.

## Action Items

1. [x] **Audit-intake guard** — **SHIPPED (PR #1130)**. Before
       `ENABLE_AUDIT_INDEX_INTAKE` mints a row, a same-brand+host canonical under
       another merchant routes the seed to identity review and skips the orphan
       mint. Closes the vector that created the ANUKO shampoo row. **W5 update:**
       the guard now **follows intake** — it runs whenever audit-index intake is
       enabled for that merchant (it's a correctness feature of the main path, not
       a separate gate), and `ENABLE_AUDIT_BRAND_FRAGMENTATION_GUARD` was inverted
       into an explicit opt-out `DISABLE_AUDIT_BRAND_FRAGMENTATION_GUARD` (default
       false) kept only as a canary escape hatch. Guard logic unchanged; still
       fail-open.
2. [x] **Deposit-unresolved telemetry** — **SHIPPED (PR #1131)**. Implemented at
       the caller (`extract_citation_observations`) rather than the pure classifier
       `resolve_deposit_content_key` — that's where an unresolved basis actually
       drops a citation. Emits `citation_deposit_dropped_{skus,observations}_total`.
3. [~] **Reconciliation-at-connect** — **designed, build PARKED** (see Appendix A).
       Mechanism fully mapped (both repos); parked because the live blast radius is
       ~0 real brands and auto-applying brand authority carries real risk. Revisit
       when a real seed-brand connects.
4. [ ] **Reuse, don't rebuild**: model the proposal/apply on the existing merge
       scripts (`scripts/align-external-seed-identity-to-catalog-sig.cjs`) and the
       `pdp_identity_recovery.py` proposal shape; gate behind a flag, dry-run first.
5. [ ] **Explicitly defer** the general content_key merge/alias primitive (Option B)
       and any ANUKO/test-merchant backfill until a real same-SKU-different-key
       case exists.
6. [ ] Pairs with the English-identity work (PR #1128): the connected first-party
       listing should carry the resolved English `title_override` as the canonical
       title once unified.

---

## Appendix A: Reconcile-at-Connect (#3) — Design & Decision to Park

_Added 2026-07-03 after a full cross-repo scoping pass (pivota-backend + PIVOTA-Agent). The mechanism is understood and buildable; the build is deliberately parked — see the decision at the end._

### A.1 How the reconcile works (end-to-end)

The two repos **share one Postgres**, and `pdp_identity_override` rows are honored by
**both** the Node identity graph (`applyIdentityOverrides`,
`pdpIdentityGraph.js:4808`) and the Python trust policy
(`catalog_trust_policy._derive_identity:351` — a `force_exact_group` override →
`status=approved, confidence=1.0`). Reconciling a connected listing to its brand's
existing `external_seed` canonical = writing **two overrides** keyed on the
connected listing's `source_listing_ref`:

1. **`force_exact_group`** — `payload.target_sellable_item_group_id` = the
   external_seed brand's group → joins both listings into one
   `sellable_item_group_id`.
2. **`approve_first_party_canonical`** — sets `source_tier='brand'`,
   `identity_status='approved'`; the Python trust policy then sets
   `identity_confidence=1.0`, clearing the deposit gate and making the connected
   first-party listing the canonical within the group.

`source_listing_ref` is `"{merchant_id}:{source_product_id}"`
(`buildSourceListingRef`, `pdpIdentityGraph.js:449`) — trivially computable in
Python; the connected listing must have a `pdp_identity_listing` row for the
override to attach.

### A.2 Anchors

| Piece | Location |
|---|---|
| Post-sync trigger point (best-effort, sync flow) | `routes/universal_product_sync.py:242` (after `ingest_standard_products`, before status update) |
| Brand-match helper to reuse | `services/brand_alias.py` (+ a new `external_seed`-by-brand query) |
| Override write endpoint (id-hash + re-resolution trigger) | Node `POST /api/admin/pdp-identity/overrides` → `applyPdpIdentityOverride` (`pdpIdentityGraph.js:5197`) |
| Override table (shared DB) | `pdp_identity_override` (`src/db/migrations/036_pdp_identity_graph.sql:67`) |
| Python consumes override | `services/catalog_trust_policy.py:351`; loaded via `catalog_row_trust_upserter.py:80` |
| Re-resolution (persists group to `pdp_identity_listing`) | Node `backfillPdpIdentityGraph`/`writeIdentityRows` (`pdpIdentityGraph.js:5066`) |

### A.3 Three findings that gate the build

1. **It unifies _serving_, not _content_keys_.** `force_exact_group` merges the
   `sellable_item_group_id` (one canonical PDP served) and
   `approve_first_party_canonical` makes the connected listing deposit-eligible —
   **on its own `content_key`**. The two content_keys' `citation_observations`
   rows still do not merge. So reconcile fixes _unpublished first-party SKUs_ and
   makes the connected half _deposit instead of drop_ — but true content_key-level
   citation merge remains the deferred Option B / item #5.
2. **`approve_first_party_canonical` is high-authority** (brand tier, confidence
   1.0, becomes canonical). Auto-applying it on a brand-name match is risky: a
   wrong match hands a connected store authority over another brand's canonical and
   pollutes serving + deposit. → **propose-first**, consistent with the ER gate and
   the #1130 guard (both route to review, never auto-merge).
3. **No Python→Node path exists** (no `PIVOTA_AGENT_URL`/client in pivota-backend),
   and the override write wants the Node endpoint (it bundles id-hashing + the
   re-resolution trigger). So auto-apply needs **new cross-service infra**. And the
   live blast radius is **~0 real brands** (ANUKO = a bare-signup demo; the other 7
   overlaps are multi-brand test merchants).

### A.4 Recommended build shape (when un-parked)

- **First cut — detect-and-propose** (cheap, dark, safe): post-sync hook →
  brand-match → enqueue a reconcile _proposal_ (reuse the review-task pattern),
  behind `RECONCILE_AT_CONNECT_ENABLED=false`, dry-run. No override write, no
  cross-service call, no identity mutation — a **tripwire** so a real brand
  connecting onto an existing seed surfaces a proposal instead of silent
  fragmentation.
- **Second cut — human-validated apply**: on approval, write the two overrides via
  the Node endpoint. Auto-apply only after match precision is proven on real cases.

### A.5 Decision: PARK the build

The #1130 audit-intake guard already catches the intake fragmentation vector, and
**nothing is actively fragmenting today** (zero live real cases; ANUKO is a demo).
Building cross-service auto-merge infra that grants brand authority — for zero
current demand and real mis-merge risk — is not justified now. **Park the build;
keep this design ready.** Trigger to revisit: a real (non-test, non-demo)
seed-brand connects a store — start with A.4's detect-and-propose tripwire.
