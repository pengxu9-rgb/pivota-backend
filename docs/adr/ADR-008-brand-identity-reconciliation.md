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

1. [ ] **Audit-intake guard** (cheap, ship first): before `ENABLE_AUDIT_INDEX_INTAKE`
       mints a row, look up an existing canonical identity by normalized brand +
       host; attach/skip instead of minting. Closes the vector that created the
       ANUKO shampoo row.
2. [ ] **Deposit-unresolved telemetry**: count/log `DEPOSIT_BASIS_UNRESOLVED` at
       `services/catalog_identity.py:265` so fragmentation is observable.
3. [ ] **Reconciliation-at-connect**: on connect/sync, build a brand-match
       proposal against `external_seed` canonical rows (normalized brand + domain,
       via `brand_alias.py`); on match, write `force_exact_group` +
       `approve_first_party_canonical` for the connected listing so it joins the
       published canonical group as first-party owner.
4. [ ] **Reuse, don't rebuild**: model the proposal/apply on the existing merge
       scripts (`scripts/align-external-seed-identity-to-catalog-sig.cjs`) and the
       `pdp_identity_recovery.py` proposal shape; gate behind a flag, dry-run first.
5. [ ] **Explicitly defer** the general content_key merge/alias primitive (Option B)
       and any ANUKO/test-merchant backfill until a real same-SKU-different-key
       case exists.
6. [ ] Pairs with the English-identity work (PR #1128): the connected first-party
       listing should carry the resolved English `title_override` as the canonical
       title once unified.
