# ADR-011: Intake Identity Contract — every intake door resolves-or-attaches before it mints

**Status:** Proposed
**Date:** 2026-07-09
**Deciders:** Founder (peng)
**Builds on:** ADR-001 (canonical record vs supplier), ADR-008 (prevent-at-intake), ADR-010 (resolver-owned canonical identity — signed off in principle 2026-07-09, incl. publish-surface addendum D-5/D-6).
**Scope:** the *intake side* of ADR-010 — this ADR operationalizes "prevent-at-intake" as a uniform, enforced contract at every door. It changes no serving behavior.

## Context

The target architecture (founder-stated, ADR-001/010): **one merchant-agnostic canonical
content identity per standard product (`content_key`) → merchant offers/listings hang
underneath → agent/user selects an offer.**

A five-door intake audit (2026-07-09, file:line-verified) found the doors do not follow one
standard. There are exactly **five functions** that write `catalog_products`:

| # | Door | Function | Guard today | Verdict vs target |
|---|------|----------|-------------|-------------------|
| 1 | Shopify/Wix connected sync | `catalog_sync_service.ingest_standard_products` | ADR-008 guard, flag-only (never blocks first-party) | PARTIAL |
| 2 | External-seed mirror (crawl onboard + 15-min job) | `mirror_external_seeds_to_catalog_products._apply` | ADR-008 guard, blocking | PARTIAL |
| 3 | Brand-authored (store-less) | `brand_authored_intake.upsert_brand_authored_catalog_row` | **no guard** | VIOLATES |
| 4 | Retailer crawl / curated feed (Path C) | `catalog_enrichment_agent/apply.apply_ingest_plan` | **no guard**; mints under the ADR-009-banned `external_seed` sentinel | PARTIAL |
| 5 | Audit / URL-wedge | `audit_index_intake.upsert_audited_sku_to_index` | ADR-008 guard, blocking; ER attach-gate exists but **dark** (`ENABLE_AUDIT_ER_GATE` off) | PARTIAL |

Systemic findings:

- **No door attaches before minting.** "Resolving" content identity is a hash computation
  (`make_content_key`), never a lookup against existing entities. The only attach machinery
  (audit ER gate; P1.3 seed attachment) is flag-dark. At crawl scale, every brand-alias /
  title-normalization drift mints a permanently parallel identity — the measured 1,218
  multi-sig content_keys grow linearly with crawled observations.
- **`pivota_signature_id` (the public PDP id) is merchant-scoped** (`make_pivota_signature_id`
  hashes `merchant::platform::source_product_id`), so one product reached via N
  merchants/URLs mints N "canonical" public PDPs. The audit door compounds fastest: its
  `stable_source_id` is per-URL. (Dual-length note: today's main has ONE 32-hex minter; the
  1,429 24-hex sigs are legacy write-once rows — a backfill/alias decision under ADR-010 D-2,
  not a live code path.)
- **GTIN is dropped at 3 of 5 doors** (mirror hardcodes None; audit and brand-authored pass no
  gtin), so crawl intake lives entirely on the weakest, deliberately non-unique
  `content_key = f(brand, title)` form.
- **Identity-namespace proliferation:** `prod::…`, `ext:…::sha1[:8]`, `ba-<slug>-<uuid12>`,
  `cp_` (24-hex, no content_key, manual-backfill-only but still read by attribution/portal),
  plus three `pg_` namespaces. Each new door invented one.
- What already works: **offers land correctly beneath the listing** at every door that has a
  purchasable offer (`catalog_offers` keyed under listing/pg; doors 3 and 5 write no offer
  rows by design — store-less and citation-only respectively) — the offers layer needs no new
  contract. And all five doors already call `ensure_singleton_group_membership` (a universal
  post-insert hook), so a universal pre-insert hook is structurally cheap. (Precision note on
  door 3: each re-create mints a fresh *listing* with a duplicated intra-merchant content_key;
  it mints **no sig** — `pivota_signature_id` is not in its insert columns.)

## Decision

**Every intake door MUST pass one shared identity primitive before inserting a catalog row:**

```
resolve_or_attach_content_identity(
    brand, title, gtin, canonical_url, source_product_id, door, merchant_ctx
) -> { content_key, product_group_id, action: ATTACH | MINT | FLAG | SKIP, evidence }
```

Semantics (composing existing machinery, not new invention):

1. **Tier-0 exact resolution first** (ADR-010's Tier 0): GTIN, `canonical_url`,
   `source_product_id` exact matches against existing entities → **ATTACH** (reuse the
   existing `content_key`/`pg`; the new row is a *listing/offer under* that identity).
   **ATTACH semantics, precisely:** a `catalog_products` row IS still inserted — it reuses the
   resolved `content_key`/`pg` instead of minting fresh ones. This is NOT P1.3's seed-detach
   ATTACH (which sets `attached_product_key` and drops the seed from the live
   `only_unattached` lane — the convergence co-gate hazard). ADR-011's ATTACH has **no
   serving-drop hazard**: serving keys on `content_key` and already selects one canonical row
   per key (`pick_canonical`), so multiple listings under one content_key is the normal
   served state.
2. **No exact match** → `make_content_key(brand, title, gtin)` → **MINT** a new identity
   (plus singleton pg), exactly as today.
3. **Brand/host conflict** (ADR-008 guard, extended to per-SKU inputs) → **FLAG** (first-party
   sync — never blocked) or **SKIP** (mirror/audit doors), as P1.4 already does.
4. Every outcome writes provenance `{door, action, matcher, evidence}` — feeding ADR-010's
   D-2 schema and gold-label capture.

**Hard rules (the contract):**

- **R1 — Attach-before-mint.** A door may not INSERT a catalog row without a
  resolve-or-attach result. No door computes identity by hashing alone.
- **R2 — One namespace.** New rows use the standard keys (`prod::` product_key, `ck_`,
  32-hex `sig_`, `pg_` from content_key). No door-local namespaces; `ext:`/`ba-` forms are
  frozen legacy. New doors MUST route through the five chokepoints (or the shared primitive)
  — adding a sixth direct writer of `catalog_products` is a review-blocking violation.
- **R3 — GTIN plumbs through every door, paired with backlog reconciliation.** If the source
  has a barcode/GTIN, it reaches `make_content_key` and the Tier-0 matcher; never hardcode
  None. **Pairing requirement (review finding):** `make_content_key(brand,title,gtin)` ≠
  `make_content_key(brand,title,'')`, and the legacy catalog is GTIN-less — so naively adding
  GTIN mints keys that never match legacy twins. R3 therefore ships WITH its reconciliation:
  Tier-0 falls back to the GTIN-less key form when the GTIN form misses, and a
  GTIN-disagreement (same GTIN, different brand+title — or vice versa) is a **FLAG**, never a
  silent second identity. GTIN backfill on legacy rows is D-2-adjacent follow-up.
- **R4 — One sig per listing, never a second sig for the same listing identity.** Sigs remain
  **per-merchant, write-once** (ADR-010 T5 — public cited URLs are per-merchant sigs; member
  pages keep resolving and cross-reference the canonical card). What R4 forbids is minting a
  *second* sig for the same underlying listing: concretely, the audit door's per-URL
  `stable_source_id` mints N sigs for one same-merchant product crawled at N URLs — on Tier-0
  ATTACH the door must resolve to the listing's existing `source_product_id`/sig rather than
  minting a URL-fresh one (this is ADR-010 **D-6**, applied at intake). Selecting THE one
  public card per content identity is ADR-010 **D-5** (canonical-URL policy) — R4 does not
  pre-empt it.
- **R5 — Sentinel discipline.** No new rows under the `external_seed` sentinel merchant.
  For brand-domain crawl, ADR-009's `ensure_observed_seller` applies directly. **Known gap
  (review finding):** retailer-crawl rows (Path C) have a multi-brand retailer as
  seller-of-record — a shape ADR-009's per-brand `(brand, domain)` keying never defined.
  Path C's move off the sentinel therefore requires a small retailer-seller extension of
  ADR-009 (retailer-domain-keyed observed seller), tracked in action item 5 — R5 does not
  claim the modeling exists today.
- **R6 — Offers stay beneath.** Prices/availability land only in `catalog_offers` (or the
  listing row), keyed under the listing — never as a new identity. (Already true where offers
  exist; stated to keep it true.)
- **R7 — Restated ADR-010 invariants bind at intake.** `content_key` is never re-minted or
  dropped for an existing row; the primitive attaches at **family grain** (`content_key`/pg)
  — variant-grain reconciliation (the buy-box grain) remains ADR-010's two-grain model, out
  of intake scope.

## Options considered

- **A. Status quo (per-door conventions).** Rejected: the audit shows each new door invented
  its own identity rules; crawl-scale intake multiplies fragmentation linearly.
- **B. Big-bang: re-key all doors to a content-first schema now.** Rejected: collides with
  ADR-010's staged resolver (mis-merge worse than fragmentation) and the serving co-gates.
- **C. One shared pre-insert primitive at the five chokepoints (CHOSEN).** Cheap (the
  universal post-insert hook proves the plumbing), incremental (Tier-0 exact only — the same
  auto-tier ADR-010 committed), and it makes every FUTURE door conform by construction.

## Consequences

- **Easier:** future crawl/ingestion paths get identity right by default; the 1,218 multi-sig
  backlog stops growing; ADR-010's resolver gets intake provenance + gold labels for free;
  D-5/D-6 (publish surface) have a stable upstream.
- **Harder / costs:** the primitive must be built (composes `make_content_key`, the audit ER
  gate's exact matchers, the deposit gate — est. small); doors 3/4 need the guard wired;
  Path C needs the retailer-seller extension (R5); one behavioral risk to stage carefully —
  R4 changes the audit door's source-id/sig derivation on Tier-0 attach (stage flag-on per
  door, mirror P1.4's rollout).
- **Explicitly out of scope:** fuzzy/attribute matching (Tier 1+, stays propose-only per
  ADR-010), any serving change, retro-merging existing rows (that is the resolver + D-2
  backlog), the `cp_`/`canonical_offers` layer cutover (tracked separately — it must either
  join the standard keys or be retired from its attribution/portal readers).

## Action items

1. [ ] Sign off the contract (R1–R7) and Option C.
2. [ ] Build `resolve_or_attach_content_identity` as a shared service composing the existing
   exact matchers; unit-golden the ATTACH/MINT/FLAG/SKIP matrix (including the R3 GTIN
   fallback + disagreement-FLAG cases).
3. [ ] Wire it pre-insert at the five chokepoints; extend the P1.4 guard to doors 3/4;
   per-door enable flags, mirror-then-sync rollout order.
4. [ ] R3 GTIN plumb-through at mirror/audit/brand-authored doors, WITH the GTIN-less
   fallback matcher; scope the legacy GTIN backfill as D-2-adjacent follow-up.
5. [ ] R5: Path C off the `external_seed` sentinel — includes the retailer-seller extension
   of ADR-009 (retailer-domain-keyed observed seller; per-brand `ensure_observed_seller`
   does not cover multi-brand retailer domains).
6. [ ] Add a CI/lint tripwire covering **all four live insert idioms** — raw
   `INSERT INTO catalog_products`, `_pg_insert(catalog_products`, `insert(catalog_products`,
   and `_upsert_by_pk(...catalog_products` (a literal-SQL grep catches only 2 of the 5
   existing doors) — any new writer outside the five chokepoints fails review.
7. [ ] Re-run `scripts/measure_identity_duplication.py` monthly (ADR-010 D-1) — the
   multi-sig-content_key count is this contract's success metric: it should plateau at
   1,218 and then fall as the D-2 backlog is consumed.
