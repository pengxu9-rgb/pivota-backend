# ADR-011 Intake Identity — Rollout Handoff (2026-07-09)

> ⚠️ **Production is GCP Cloud Run (`pivota-prod`, `us-west1`) since 2026-08-22. Railway is the
> ROLLBACK.** This document is a rollout handoff, but §6 "How to operate / verify" is a LIVE
> instruction set for work still marked incomplete — so the `railway` commands in it are not
> historical record, they are steps someone may still follow. They were not translated by
> guesswork. Translate with
> [operating_on_gcp_production.md](../runbooks/operating_on_gcp_production.md) before acting.


Status snapshot for review / fresh-session pickup. Companion to
`docs/adr/ADR-011-intake-identity-contract.md`.

## 1. What shipped

**PR #1273 — merged to `main` as squash `3fc305d1` (2026-07-09).** One PR, three
logical parts:

- **The contract.** A shared `resolve_or_attach_content_identity(...)` primitive
  (`services/intake_identity.py`) that every writer of `catalog_products` calls
  before inserting. Tier-0 exact matching only; returns
  `{content_key, product_group_id, action: ATTACH|MINT|FLAG|SKIP, gtin, evidence, attach}`.
  Composes existing machinery (make_content_key, the ER-gate exact matchers, the
  deposit gate, the ADR-008/P1.4 brand guard). Fail-open to MINT — never blocks intake.
- **The SPU identity model** (founder direction, replacing the ADR's literal R3):
  `content_key = make_content_key(brand, title)` **always** — the GTIN-less
  brand+title FAMILY key. **GTIN is a match-ATTRIBUTE, never key-material**,
  stored in a new `catalog_products.gtin` column (migration 178). Rationale: most
  products lack a standard GTIN, and folding GTIN into the key fragmented a
  product seen with-then-without a barcode. Same-GTIN/diff-title → ATTACH+FLAG
  drift; same-title/diff-GTIN → FLAG collision (shared family key, gtin
  discriminates downstream — ADR-010 two-grain).
- **Provenance + tooling.** `intake_identity_events` table (migration 177) records
  `{door, action, matcher, evidence}` per outcome. CI tripwire
  (`tests/test_catalog_products_writer_tripwire.py`) pins the writer set to the
  five chokepoints (all four insert idioms). Step-4 backfill script
  `scripts/backfill_catalog_products_gtin.py` (dry-run default, `gtin IS NULL`-guarded).

Migrations 177/178 self-heal via `db/schema_guard.py` (Railway skips `db/migrations/`).

### The five doors (only writers of catalog_products)
| # | Door | Function | Conflict semantics |
|---|------|----------|--------------------|
| 1 | connected sync | `catalog_sync_service.ingest_standard_products` | FLAG-only (never blocks first-party) |
| 2 | external-seed mirror | `scripts/mirror_external_seeds_to_catalog_products._apply` | SKIP |
| 3 | brand-authored | `brand_authored_intake.upsert_brand_authored_catalog_row` | FLAG (first-party manual) |
| 4 | retailer crawl/feed | `catalog_enrichment_agent/apply.apply_ingest_plan` | SKIP (+ skipped PDP's child rows dropped) |
| 5 | audit / URL-wedge | `audit_index_intake.upsert_audited_sku_to_index` | SKIP; R4 same-merchant ATTACH re-keys onto existing sig |

## 2. Rollout state — LIVE

**All five door flags are ON in Railway `web`/production:**
`ENABLE_INTAKE_IDENTITY_{SYNC,MIRROR,AUDIT,BRAND_AUTHORED,ENRICHMENT}=1`.

- Deploy confirmed live (schema_guard ran migs 177/178 — `intake_identity_events` present).
- **Primitive validated in prod** via a surgical one-shot re-audit of an existing
  `url_audit` seed (Mojawa pilot, `ck_a6dc8c29…`): `action=ATTACH matcher=content_key`,
  returned the **same** content_key (idempotent, no fragmentation), provenance row
  written. Confirms DB integration + provenance + flag-gating beyond the unit tests.
- `intake_identity_events`: **1 row** so far (that validation ATTACH). Organic
  events pending because prod intake is dormant (see §3).

**Coverage gap:** the mirror door also runs in a **GitHub Actions workflow**
(`external-seeds-catalog-products-mirror.yml`, workflow_dispatch) and
`scripts/onboard_external_brand_from_crawl.py` — those run in **other
environments** and do NOT see the Railway var. The Railway flag covers the
continuous 15-min APScheduler tick (the main line). Set the flag in those envs
before calling the mirror door 100% rolled out.

## 3. Existing product catalog status (snapshot 2026-07-09)

| Metric | Value |
|--------|-------|
| catalog_products total | **11,021** |
| content_key-keyed | 11,001 |
| distinct content_keys | **8,286** (~2,735 products share a key) |
| **same-merchant duplicate content_keys** | **1,076** (intra-merchant fragmentation) |
| **content_keys shared across >1 merchant** | **374** (cross-merchant overlap — some legit, some to reconcile) |
| rows with `gtin` populated | **0** — catalog is essentially GTIN-less |
| rows with a usable `catalog_skus.barcode` | **1 of 11,021** |
| external_product_seeds active / unmirrored | 8,953 / **0** (mirror drained) |

**Platform spread:** external_seed 9,456 (crawl/observed supply — dominant),
shopify 1,524 (first-party), url_audit 20, wix 20, brand_authored 1.

**Intake is DORMANT right now:** sync's last new product was 2026-06-20 (~19d
ago), mirror drained 2026-07-04, audit ~2/day (the freshest door). This is why
passive canary-watching showed no organic events — there's little live traffic.

**Implication:** the GTIN matcher is *future-proofing* (populates via the doors
going forward — esp. sync writing `product.barcode` → `catalog_products.gtin`);
existing-catalog convergence rides content_key / canonical_url / source_product_id,
not GTIN.

## 4. Validated vs. still-unobserved

- **Validated:** primitive correctness (32 unit-golden tests + the prod ATTACH),
  config on all doors, provenance writes, flag-gating, idempotency, CI green.
- **Not yet observed organically** (dormant traffic): MINT / FLAG / SKIP in prod;
  the GTIN matcher (no GTINs in catalog yet); R4 sig re-key live. All are covered
  by the unit-golden matrix; they just haven't fired against live prod data.

## 5. Open follow-ups — the "fix the existing catalog" work

Ordered; none started beyond the tooling:

1. **Step-4 GTIN backfill** (`backfill_catalog_products_gtin.py`) — a **near no-op
   today** (catalog is GTIN-less; would populate ~1 row). Revisit once barcoded
   intake accumulates. Dry-run-verified against prod.
2. **Step-5 identity reconciliation / retro-merge** — the real catalog fix and the
   dangerous one. Reconcile the **1,076 same-merchant duplicate content_keys** and
   review the **374 cross-merchant shares**, via the ADR-010 resolver + D-2 backlog,
   human-reviewed, using the new provenance + gold labels. **Not started.** Do NOT
   fold into intake; it's a separate, careful, reviewed effort. (Mis-merge is worse
   than fragmentation — ADR Option B was rejected for this reason.)
3. **Mirror flag in the other envs** (GH workflow + crawl-onboard) — §2 coverage gap.
4. **Monthly `measure_identity_duplication.py`** (ADR action item 7) — success
   metric: the same-merchant dup count (1,076) should **plateau** now that intake
   is fixed, then **fall** as the D-2 backlog is consumed.

## 6. How to operate / verify

- **Flags:** `railway variables --set "ENABLE_INTAKE_IDENTITY_<DOOR>=1" --service web`
  (triggers a redeploy). In code: `services.intake_identity.intake_identity_enabled(door)`.
  Turn a door OFF the same way (`=0`) — flag-off reverts to the legacy path,
  identity/serving byte-identical.
- **Read prod (read-only):**
  `railway run bash -c 'DATABASE_URL="$DATABASE_PUBLIC_URL" PYTHONPATH="$PWD" python3.11 "$0"' <script>`.
  The public proxy (`nozomi.proxy…`) flakes intermittently — use a **single
  `asyncpg.connect` + retry loop + longer timeout** for ad-hoc reads, not the
  pooled `database.connect()`. `timeout` cmd is absent on this Mac (bound streams
  with `head` or the tool timeout).
- **Watch:** `intake_identity_events` grouped by `door, action` (the ATTACH/MINT/
  FLAG/SKIP mix); `catalog_products.gtin` populating; `measure_identity_duplication.py`.
- **Key files:** `services/intake_identity.py` (primitive); the five door modules
  (§1 table); `db/catalog.py` (gtin column + index); `db/migrations/177,178`;
  `db/schema_guard.py` (self-heal); `scripts/backfill_catalog_products_gtin.py`;
  `tests/services/test_intake_identity*.py`, `tests/test_catalog_products_writer_tripwire.py`.

## 7. Update 2026-07-10 — first organic traffic burst (verified read-only)

Hours after this snapshot, the sync door woke up (2026-07-09 16:53–22:54 UTC)
and processed its first organic batch. Results:

- **924 organic events**: catalog_sync 879 ATTACH + 40 MINT; url_audit_intake
  4 ATTACH + 1 MINT. MINT is now prod-observed; FLAG/SKIP remain unit-only.
- **Duplication metric held at exact plateau** (canonical
  `measure_identity_duplication.py` definitions): same-merchant dup keys
  **1,076** (unchanged), cross-merchant shares **374** (unchanged), and no dup
  key gained a member row created ≥ 2026-07-09. First live evidence the
  intake fix stops new fragmentation under real traffic.
- Catalog: 11,002 keyed rows (+1 — the url_audit MINT), 8,287 content_keys,
  9 merchants. `gtin` still 0 populated (batch carried no barcodes — expected).
- **Observation worth a follow-up:** all 40 sync MINTs are the Wix door's
  rows with `brand: null` → `reason: no_identity_inputs` — the fail-open path
  minting with **no content_key** (these are the ~20 perpetually-unkeyed wix
  rows, hit by two sync ticks). Wix intake never yields identity keys until
  brand extraction (or a merchant/store-name fallback) exists for that
  platform. Low volume today (20 rows) but it re-fires every sync tick.
- Ops note confirmed: the pooled `database.connect()` path timed out against
  the public proxy exactly as §6 warns; the single-conn retry recipe worked.

## TL;DR
Contract shipped and merged; SPU model (GTIN as attribute, not key); all five
doors enabled in prod and the primitive is validated live (clean idempotent
ATTACH). Catalog is quiet and GTIN-less: 11,021 products, 1,076 same-merchant
duplicate keys + 374 cross-merchant shares awaiting the step-5 reconciliation —
the real "fix the catalog" work, deliberately not yet started. New fragmentation
is now stopped at every door.
