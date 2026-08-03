# Identity Reference — merchants, products, offers, pages

**Purpose:** the single citable guide to every identifier in the commerce index. Multiple
identifier generations and near-miss formats coexist in this codebase and have repeatedly
caused misunderstanding (including a confirmed dead query path — see Trap T1). Before
writing code that mints, parses, joins on, or migrates ANY identifier below, read its row
here; when you change identity behavior, update this file in the same PR and cite the
section from the code comment.

All file:line references verified against main @ `2a7feb57` (2026-07-05).

---

## 1. The identity ladder (top = most canonical / cross-merchant; bottom = most local)

| Layer | Identifier | Answers | Scope |
|---|---|---|---|
| Product group | `product_group_id` (pg) | "which physical product family is this?" | cross-merchant |
| Content identity | `content_key` (`ck_…`) | "which physical product is this?" | cross-merchant |
| Catalog listing | `product_key` — TWO generations, see T8 | "which merchant's listing of it?" (`prod::…`) | per-merchant |
| ↳ Path-C minted | `product_key` (`ext:…`) | "which physical product is this?" | **cross-merchant** ⚠️ |
| Variant | `sku_key` (two forms — see T2) | "which variant of that listing?" | per-merchant |
| Public page | `pivota_signature_id` (`sig_…`) | "which citable Pivota PDP URL?" | per-merchant, public, write-once |

**Rule of thumb:** the *decision layer* (offers, alternatives, comparisons, outcomes-per-
product) keys on the top of the ladder (pg / content_key). The *serving layer* (PDPs,
sitemap, GSC) keys on `sig`. The middle (`product_key`/`sku_key`) is merchant-catalog
plumbing and must not leak into agent-facing or cross-merchant semantics.

**⚠️ The one place that rule of thumb breaks (Trap T8).** `product_key` has TWO live
generations at DIFFERENT GRAINS. `prod::{merchant}::{platform}::{source_id}` is
per-merchant plumbing as described. `ext:{brand-slug}::{hash}` — minted by Path-C and
still minted today — is derived from **(brand, product_name) only**, so it is
merchant-AGNOSTIC and sits at content grain despite living in the `product_key` column.
Do not reason about "the product_key layer" as uniformly per-merchant, and never convert
one generation to the other. See §2 `product_key` — the `ext:` generation, and T8.

---

## 2. Product-side identifiers

### `content_key` (`ck_<32hex>`)
- **Minted by:** `services/catalog_identity.py:133` `make_content_key(brand, title, gtin)` =
  `ck_ + sha256(normalize_brand :: normalize_title :: gtin-or-'')`.
- **Scope:** cross-merchant — one key per physical product. Two merchants selling the same
  product share a `content_key`; that's the join that makes multi-merchant canonical PDPs
  and cross-seller comparison possible.
- **Traps:** brand/title spelling variants (Korean vs English titles) or a present-vs-absent
  GTIN mint **different** keys — the ADR-008 fragmentation problem. Never "fix" a mismatch
  by overwriting a content_key in place; that's a merge decision (ADR-008 reconciliation).
- **MUST NOT:** be treated as merchant-scoped. `index_pipeline_state` is keyed by
  content_key + merchant_id for exactly this reason — a bare content_key lookup without a
  merchant filter can cross merchants (see the merchant-scoping warning inside
  `services/pivota_indexing_request.py`).

### `product_group_id` (pg)
- **What:** the canonical grouping above content_key (family/variant collapse). Set by
  `services/catalog_variant_promoter.py` and consumed by `agent_pdp_view_assembler`,
  `index_pipeline_state_service`, `brand_verified_graduation` (which deliberately does
  **NOT** touch it — "no mis-merge").
- **Use:** the front-facing product identity for the decision layer (ADR-009). When pg is
  absent, content_key is the fallback grouping key.
- **MUST NOT:** be set/merged casually — mis-merge is worse than fragmentation
  (`brand_verified_graduation.py:71`).

### `product_key` (`prod::{merchant_id}::{platform}::{source_product_id}`)
- **Minted by:** `services/catalog_sync_service.py:297` `make_catalog_product_key`.
  **Double-colon**, `prod::` prefix. This is the STORAGE format everywhere
  (`catalog_products.product_key`, `external_product_seeds.attached_product_key`).
- **Scope:** per-merchant per-platform listing.
- **Traps:** see **T1 (pipe transport form)** — a pipe-delimited *transport* form of the
  same triple exists and is NOT interchangeable with this storage form.
- **A9-4 historical-format residue (ADR-009 D4.2):** the seller-of-record backfill
  (`scripts/backfill_seller_of_record.py`) re-subjects `catalog_products.merchant_id`
  off the `external_seed` bucket but deliberately does **NOT** re-key `product_key`
  (ADR-009 D4.2 says "re-key `merchant_id`", not `product_key`). So a re-subjected
  crawled row keeps `prod::external_seed::external_seed::{pid}` — the merchant segment
  is now a HISTORICAL-FORMAT storage token, not the current owner. This is safe
  because the decision layer keys on `content_key`/`product_group_id` (§1) and the
  discipline is "look up by key, never parse the merchant out of it" (T1/T2/T6). The
  one path that parses a merchant from a product key —
  `services/crawled_inci_ingest.merchant_id_from_product_key` (feeds
  `beauty_sku_ingredients.merchant_id` at INCI ingest) — already yields
  `external_seed` for these rows and stays byte-identical. **MUST NOT** read
  ownership out of a `prod::` key; join `catalog_products.merchant_id` instead.

### `product_key` — the `ext:` generation (`ext:{brand-name-slug}::{sha1[:8]}`)

- **Minted by:** `services/catalog_enrichment_agent/ingestion.py:98`
  `derive_product_key(brand, product_name)` — Path-C / door 4 (retailer crawl,
  `source_system = 'catalog_enrichment_agent_v1'`). Live: called at
  `ingestion.py:233`. New rows are still minted today.
- **~1,795 unsuppressed rows** in prod (2026-08-01).
- **⚠️ THIS IS A DIFFERENT GRAIN, NOT A DIFFERENT SPELLING.** `prod::` is
  `{merchant_id}::{platform}::{source_product_id}` — **per-merchant**. `ext:` is
  derived from **(brand, product_name) ONLY** — it is **merchant-agnostic**. Two
  merchants selling the same product produce ONE `ext:` key and TWO `prod::`
  keys. So an `ext:` value is content-grained living in the `product_key`
  column; the ladder's "per-merchant plumbing" description in §1 does **not**
  apply to it. Consequences that follow directly:
  - it has its own product-group namespace, `pg_ext_<hash>`
    (`services/pdp_identity_recovery.py:98`);
  - Path C attaches **one seed PER OFFER** to a single `ext:` key, so several
    merchant offers converge on one row — which is why the seed pick among them
    is a real decision (see `minted_seed_identity_leg_sql`), not a formality.
- **Treated as CANONICAL by live code, not as residue.**
  `services/pdp_identity_recovery.py:596-606` resolves an `ext:` product_key as
  `resolved_identity_source = 'canonical_ext_product_key'` and PREFERS it over
  the product group; `scripts/source_pdp_content_repair.py:112` and
  `scripts/source_pdp_offer_image_repair.py:210` sort `ext:` rows FIRST
  (`CASE WHEN product_key LIKE 'ext:%' THEN 0 ELSE 1 END`).
- **ADR-011 R2 calls `ext:`/`ba-` "frozen legacy" — read that precisely.**
  ADR-011 is **Status: Proposed**; its action item 1 ("sign off R1–R7") is
  UNCHECKED, and nothing in code enforces the freeze. R2 governs what NEW rows
  may use. It has never meant that existing `ext:` rows are retired, detachable,
  or safe to re-key.
- **🚨 MUST NOT:** "repair" an `ext:` key to a `prod::` key. The gateway routes
  minted PDPs by matching `external_product_seeds.attached_product_key =
  cp.product_key`, so a seed must point at whichever format its row actually
  uses. **This happened:** A9-4 phase 2 rewrote 720 seeds from `ext:` to
  `prod::` — conforming ONE side of a two-sided pair — and 364 elected, public
  PDPs returned HTTP 500 (2026-08-01; 587 restored, 133 unmatched). Guarded
  since #1663: the backfill now skips any key that already resolves.
- **MUST NOT:** parse a merchant out of it. There isn't one in there — that is
  the whole point of the format.

### The PIPE transport form (`{merchant_id}|{platform}|{source_id}`)
- **What:** a pipe-delimited rendering of the product triple used as a REPORT/EVIDENCE
  transport key: `_url_audit_seed_report_identity`
  (`services/agent_center_bd_report_service.py:6289`) emits it; the merchant portal's
  `parseProductKey` consumes it; W5 P4 converts it back via
  `make_catalog_product_key(*pipe.split("|"))` before anything touches the DB.
- **MUST NOT:** be used in a SQL match against `product_key` / `attached_product_key`
  columns — those store the `prod::` form. See **Trap T1** for the confirmed dead path
  this caused.

### `sku_key` — TWO coexisting generations (Trap T2)
- **Gen A:** `services/catalog_sync_service.py:301` `make_catalog_sku_key` =
  `sku::{product_key}::{source_variant_id}`.
- **Gen B:** `services/catalog_variant_promoter.py:237` = `{primary_product_key}::v::{variant_id}`
  (variant-promoted rows). Prod `catalog_skus.sku_key` data contains Gen B rows
  (verified 2026-07-05: `prod::…::shopify::…::v::…`).
- **MUST NOT:** assume one format when parsing. Match by table lookup
  (`catalog_skus.sku_key = :key`), never by splitting on a delimiter you assumed.

### `pivota_signature_id` (`sig_<32hex>`) + `pivota_canonical_url`
- **Minted by:** `services/catalog_sync_service.py:317` `make_pivota_signature_id
  (merchant_id, platform, source_product_id)` — deterministic hash; same product re-syncs
  to the same sig; different merchants can never collide.
- **Scope:** per-merchant PUBLIC page identity — `agent.pivota.cc/products/<sig>` — the
  citation/GSC/serving artifact (ADR-006/007).
- **WRITE-ONCE + PUBLIC:** sigs are persisted with existing-first COALESCE (W5 P3) and are
  submitted to Google (OPS-A/GSC). **Never re-mint a sig because an owning identity was
  re-keyed** — persisted sigs keep their value forever; only NEW rows mint under new
  identities. (Load-bearing constraint for the ADR-009 backfill.)
- **MUST NOT:** be used as the decision-layer product identity (it is merchant-scoped and
  would fragment cross-seller comparison), nor carry a seller column (its merchant IS its
  subject).

### `external_product_id` (external seeds)
- **What:** the storefront-exposed id for an external seed product; backfilled from a hash
  of canonical/destination URL (`_stable_external_product_id`,
  `routes/agent_shop_gateway.py:6248`). NOT the platform product id — a Shopify pid will
  not match it (this broke a validation probe; see T1 aftermath).

### `urlwedge:*` (ephemeral)
- **What:** synthetic per-run sku_key for URL-audit SKUs with no catalog row. Deliberately
  ephemeral — "the portal can't act on it."
- **Status:** since W5 P4, report CTAs stamp the REAL seed product_key instead when a seed
  exists; `urlwedge:` remains only for the no-seed honest-error path.
- **MUST NOT:** be persisted, resolved against catalog tables, or handed to any endpoint.

---

## 3. Merchant-side identifiers (Trap T3 — the tenant/seller conflation)

`merchant_id` (`merch_…`) is used today to mean TWO different things, and several tables
partially overlap. This is the root cause behind the shared `external_seed` bucket
(ADR-008 context; ADR-009 decision).

| Table | What it actually is | Notes |
|---|---|---|
| `merchant_onboarding` (`db/merchant_onboarding.py:15`) | the TENANT/account — login, API keys, billing, `mcp_shop_domain` | the thing that authenticates |
| `catalog_merchants` (`db/catalog.py:21`) | the INDEX/serving SUBJECT — `status`, `indexable`, `source_system`, `source_ref` | joined by canonical PDP reads + sitemap; W5 P3 upserts minimal rows for url_audit brands (`source_system='url_audit_intake'`) |
| `merchants` (legacy, present in older DBs) | pre-split merchant rows | do not build against |
| `agent_merchants` | agent-side projection | separate concern |

- **The conflation:** `merchant_id` on `orders`, `commerce_attribution_edges`,
  `aggregated_outcomes`, `seller_trust` means "the economic SELLER-OF-RECORD" — but crawl
  ingestion (ADR-008 Path B) stuffs a placeholder TENANT (`merchant_id='external_seed'`)
  into it, merging every crawled brand into one meaningless subject.
- **Direction (ADR-009):** seller-of-record = a `catalog_merchants` row (possibly
  `status='observed'`, no tenant attached). Tenants ATTACH to a seller identity at claim
  time (`brand_claims`, `brand_verified_graduation`); the identity itself never re-keys.
- **MUST NOT:** introduce another shared placeholder merchant for any ingestion path; every
  supply row gets a real per-brand subject (the W5 P3 rule, generalized).

---

## 4. Offer / attribution identifiers

### `external_product_seeds.seller_ref` + `seed_kind` (ADR-009 D3, A9-3)
- **What:** the seed's explicit SELLER-OF-RECORD. `seller_ref` is a
  `catalog_merchants.merchant_id` (§3); `seed_kind` is `'self'` (destination ==
  the anchor merchant's own store — anchor IS the seller) or `'cross'`
  (destination is a different seller than the anchor). Derived AT WRITE TIME by
  `services/seller_identity.derive_seed_seller` for every new-seed writer
  (`onboard_external_brand_from_crawl`, `catalog_enrichment_agent/apply`,
  `seed_data_writer`, and the four `routes/employee_products` seed INSERTs):
  SELF → the anchor merchant_id; CROSS → `ensure_observed_seller(brand, dest)`
  (A9-2); unresolvable → NULL + a loud log (never guessed).
- **NULL = pre-A9-4 legacy** (the ~9.4k existing rows). Migration 169 adds the
  columns (schema_guard self-heals in prod — the migration-167/168 idiom); A9-4
  backfills the NULLs via the W1 RunFacts parity pattern. **MUST NOT** be silently
  treated as `'self'` anywhere — every decision on a NULL seller_ref keeps today's
  behavior and stamps an observability marker (e.g. closure's
  `metadata.seller_ref_missing=true`) so A9-4 has a kill metric.
- **Threaded through T2 (A9-3):** the T2-1 redirect
  (`_external_seed_redirect_identity` / `_make_external_redirect_url`) carries
  `seller_ref` + `seed_kind` in the signed token ctx alongside the ANCHOR
  merchant_id; `record_surface_event` persists them into
  `surface_click_events.context` (JSONB — reused rather than adding two columns to
  the high-volume click table; the closure reads them back via a driver-agnostic
  JSON coerce). T2-2 closure keys the edge's conversion SUBJECT
  (`commerce_attribution_edges.merchant_id`) by `seller_ref`; the anchor becomes a
  separate dimension (`metadata.converting_merchant_id`). T2-3 aggregates by
  `merchant_id`, which now means SELLER for external edges — no SQL change.

### `external_product_seeds.attached_product_key` + `attached_variant_id`
- **What:** binds an external seed (a referral offer) to the catalog listing it represents.
- **⚠️ TWO storage formats, both live and both correct** (prod 2026-07-05: 8,004
  `prod::`, **720 `ext:`**, 659 NULL, **zero pipe-format rows**). A seed must
  point at whichever format the row it is attached to actually uses — `prod::`
  for mirror/sync rows, `ext:` for Path-C minted rows (see the `ext:` generation
  above).
  **This line previously read "720 other/bare", and that phrasing caused an
  outage.** "Other" reads as unclassified residue; those 720 were `ext:` keys
  resolving correctly. A backfill "repaired" them to `prod::` and took 364 public
  PDPs to HTTP 500 (2026-08-01). Name the format; never call a live one "other".
- **Trap T1 (CONFIRMED DEAD PATH):** `_fetch_attached_seed_rows`
  (`routes/agent_shop_gateway.py:3471`) builds pipe-format match keys
  (`{merchant}|{platform}|{pid}` and prefix `{merchant}|%`) against this column → matches
  **nothing** in prod. The `external_seed_by_attached_ref` and
  `…_canonical_attached_prefetch` lookup paths are dead; attached seeds surface only via
  the fuzzy fallback (`external_product_id` / title / URL LIKE). Fix tracked in ADR-009
  prerequisites: build match keys with `make_catalog_product_key`.
- Existing Shopify seeds in prod have **empty `attached_variant_id`** → they take the T2-1
  referral-only fallback (no cart-permalink / no order-side join) until backfilled.

### `click_id` (`surface_click_events`) and `edge_id` (`commerce_attribution_edges`)
- `click_id`: minted at redirect-build time (T2-1) and threaded through the signed `/r`
  token ctx (`pvt_click_id`) so `materialize_attribution_context` does NOT mint a throwaway.
  Carried onto Shopify orders via cart-permalink `attributes[pivota_click_id]` →
  `note_attributes` (the order-side join key).
- `edge_id` for external conversions: deterministic
  `uuid5(merchant_id:ext:external_order_id)`; idempotency = UNIQUE
  `(merchant_id, external_order_id)` (migration 167 + schema_guard self-heal).
- **Integrity:** `metadata.click_matched=false` edges are FORGEABLE (note_attributes is
  merchant-controllable) and are excluded from `aggregated_outcomes` (T2-3). Never widen
  that gate.
- **ADR-009:** the edge's `merchant_id` means the SELLER of the conversion.
  - **Seller-keyed subject (A9-3, §D3, shipped):** when the matched click carries a
    `seller_ref` (threaded from the seed), `close_external_order_conversion` sets the
    edge SUBJECT (`merchant_id`) to that `seller_ref` and UPGRADES the seller check to
    an IDENTITY compare — the converting store's own tenant `merchant_id` (the
    caller-authenticated/polled param) must equal `seller_ref` to count. This resolves
    the A9-1 limitation: a seed whose destination is a custom storefront domain while
    the webhook authenticates the `.myshopify.com` domain no longer false-mismatches,
    because identity (not raw host) is compared. Idempotency is preserved — SELF seeds
    keep `subject == converting merchant`; CROSS seeds re-subject to `seller_ref`, and
    a replay under the same seller is stable (click_id → seller_ref is fixed).
  - **Interim seller-mismatch guard (A9-1, §D3, LEGACY path):** for a click with NO
    `seller_ref` (pre-A9-4 legacy seed), the subject stays the converting merchant and
    `close_external_order_conversion` keeps the A9-1 host compare BYTE-IDENTICAL: it
    takes `converting_shop_domain` (the caller-authenticated / polled store the sale
    happened on) and compares its normalized host against the click's `dest_domain`.
    Host compare is EXACT (no eTLD+1 helper exists — do not hand-roll). It additionally
    stamps `metadata.seller_ref_missing=true` so A9-4 can size the un-migrated volume.
  - On mismatch (either path) the edge is still recorded but stamped
    `metadata.seller_mismatch=true` and EXCLUDED from `aggregated_outcomes` (T2-3 gate
    `->>'seller_mismatch' IS NOT TRUE`, NULL-safe). A caller that supplies no domain on
    the legacy path stamps `metadata.seller_domain_unverified=true` and is still counted
    (unknown is neither a pass nor an exclusion) — expected only from callers predating
    the guard.

---

## 5. Trap index (the short list to check before touching identity)

- **T1 — pipe vs `prod::`:** pipe is a report/portal TRANSPORT form; `prod::` is STORAGE.
  Never SQL-match pipe against storage columns. One confirmed dead query path
  (`_fetch_attached_seed_rows`).
- **T2 — two `sku_key` generations:** `sku::{pk}::{v}` (make_catalog_sku_key) vs
  `{pk}::v::{v}` (variant promoter). Look up, don't parse.
- **T3 — `merchant_id` conflates tenant and seller-of-record:** the `external_seed` bucket
  is the symptom. Seller subjects live in `catalog_merchants`; tenants in
  `merchant_onboarding`; claiming attaches, never re-keys.
- **T4 — `urlwedge:` is ephemeral:** never persist/resolve it (W5 P4 stamped real keys).
- **T5 — sigs are write-once and public:** identity re-keys must never re-mint sigs.
- **T6 — `content_key` is cross-merchant:** always merchant-scope joins that must not
  cross merchants (`index_pipeline_state` pattern).
- **T7 — `external_product_id` ≠ platform product id:** it's a URL-hash; don't match a
  Shopify pid against it.
- **T8 — TWO `product_key` generations at DIFFERENT GRAINS:** `prod::{merchant}::…` is
  per-merchant; `ext:{brand-slug}::{hash}` is derived from (brand, product_name) alone
  and is merchant-AGNOSTIC. Both are live and still minted. Never convert one to the
  other: the gateway matches `attached_product_key = cp.product_key`, so rewriting one
  side of that pair dark-routes the PDP. Cost when this was missed: 364 public PDPs
  returning HTTP 500 (2026-08-01). "Frozen legacy" in ADR-011 R2 (still **Proposed**,
  unenforced) constrains NEW minting only — it never authorised re-keying.
