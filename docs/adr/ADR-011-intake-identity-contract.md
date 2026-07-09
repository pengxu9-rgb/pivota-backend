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
- What already works: **offers land correctly** at every door (`catalog_offers` keyed beneath
  the merchant listing) — the offers layer needs no new contract. And all five doors already
  call `ensure_singleton_group_membership` (a universal post-insert hook), so a universal
  pre-insert hook is structurally cheap.

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
   existing `content_key`/`pg`; the new row is a *listing/offer under* that identity; no new
   public identity is minted).
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
- **R3 — GTIN plumbs through every door.** If the source has a barcode/GTIN, it reaches
  `make_content_key` and the Tier-0 matcher. Never hardcode None.
- **R4 — Public identity is minted at most once per content identity.** When the resolver
  ATTACHes, the door must NOT mint a new `pivota_signature_id` for an already-sig'd identity
  (write-once/T5 respected; the existing sig is the public id; the new listing hangs under
  it). This stops the N-PDPs-per-product growth at the source.
- **R5 — Sentinel discipline.** No new rows under the `external_seed` sentinel merchant
  (Path C moves to the standard observed-seller resolution ADR-009 already provides).
- **R6 — Offers stay beneath.** Prices/availability land only in `catalog_offers` (or the
  listing row), keyed under the listing — never as a new identity. (Already true; stated to
  keep it true.)

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
  Path C needs seller-resolution rework (R5); one behavioral risk to stage carefully — R4
  changes what audit/crawl doors mint when Tier-0 attaches (stage flag-on per door, mirror
  P1.4's rollout).
- **Explicitly out of scope:** fuzzy/attribute matching (Tier 1+, stays propose-only per
  ADR-010), any serving change, retro-merging existing rows (that is the resolver + D-2
  backlog), the `cp_`/`canonical_offers` layer cutover (tracked separately — it must either
  join the standard keys or be retired from its attribution/portal readers).

## Action items

1. [ ] Sign off the contract (R1–R6) and Option C.
2. [ ] Build `resolve_or_attach_content_identity` as a shared service composing the existing
   exact matchers; unit-golden the ATTACH/MINT/FLAG/SKIP matrix.
3. [ ] Wire it pre-insert at the five chokepoints; extend the P1.4 guard to doors 3/4;
   per-door enable flags, mirror-then-sync rollout order.
4. [ ] R3 GTIN plumb-through at mirror/audit/brand-authored doors.
5. [ ] R5: Path C seller resolution off the `external_seed` sentinel.
6. [ ] Add a CI/lint tripwire: any new writer of `catalog_products` outside the five
   chokepoints fails review (grep-based check is sufficient v1).
7. [ ] Re-run `scripts/measure_identity_duplication.py` monthly (ADR-010 D-1) — the
   multi-sig-content_key count is this contract's success metric: it should plateau at
   1,218 and then fall as the D-2 backlog is consumed.
