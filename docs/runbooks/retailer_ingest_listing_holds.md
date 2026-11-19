# Retailer-ingest drain: holds on same-title listings (#2463)

A brand_official / content-keyed row is keyed by (brand, title) alone, so every same-title listing of one store is the
same catalog row. Since #2463 the plan (`services/catalog_enrichment_agent/ingestion.ingest_validated_jsonl`) keeps
ONE listing per row per host, and never moves a row off the listing it names. Two flags come out of that. Either one
holds the WHOLE job (every other product of the store waits too), so answer it; do not let a store sit held.

## `same_key_other_listing:<handle>` -- a listing left out

Another listing on the store has this one's title, so the plan left it out (it would otherwise sell under the other
page's row). INFO (applies, nothing to do) when prices, merchant type and image all match -- an ad clone or a region
copy. BLOCK otherwise: a different price, type or image is how a different product wearing the same title shows
(COCODOR "Black Cherry" refill vs diffuser; MCoBeauty shades; Mr. Smith full size vs sachet).

- **Apply without it:** accept the key (`accepted_flags: ["same_key_other_listing:<handle>"]`), or accept every
  such flag of the job at once with job option `accept_listing_collisions: true` (recorded, still listed per run).
- **Make the other one the row's listing instead:** `exclude_handles: ["<the KEPT listing's handle>"]` (the flag's
  detail names it). Only for a row that names no listing yet -- see below for a row that does.
- A different product left out is left out, not written: giving it its own row needs a keying decision (not built).

## `listing_moved:<product_key>` -- a row the crawl would move

The row names listing A on this host; this crawl does not carry A (unpublished, renamed handle, or a page the crawl
dropped) and would write B instead. The plan HOLDS every record of that row on that host. Always BLOCK;
`accept_listing_collisions` does NOT cover it.

- **Keep the row on A** (A is only missing from this crawl, or B is not the same product): exclude every listing the
  crawl carries for that row -- `exclude_handles: [...]`, the handles in the flag's detail ("crawled ..."; the flag's
  `handle` is B, the one it would move to). Excluding only B moves the row to the next one. Nothing of that row is
  written this run; the row is untouched. A crawl that merely dropped A is also answered by re-running the job.
- **Move the row to B** (A is gone for good and B is its successor): accept `listing_moved:<product_key>`. The row is
  re-pointed and B's offers and seed are written. A's offer and seed are NOT touched by the ingest: afterwards run
  `scripts/repair_same_title_listings.py --product-key <product_key>` (dry run, then `--apply` with the owner's go) to
  suppress A's offer and deactivate A's seed.

## `current_listings_unread` -- never acceptable

The drain could not read `catalog_products.canonical_url` for the planned rows, so it cannot tell a move from a first
ingest. Fix the database access and re-run; it cannot be accepted.

## `listing_spellings_unread` -- never acceptable

The same read, for the `ext:retailer:` rows. Their listing never moves, but their offer and seed ids hash the URL as
spelled, so the drain writes each row under the host spelling (`www.` or not) its stored `canonical_url` already uses
(`checks.listing_spellings`, and `listing_respellings` in the plan). Without that read, a job queued under the other
spelling would write a second offer and seed for every row it re-crawls. Fix the database access and re-run.

## `seller_id_split:<existing merchant_id>` -- a second seller for one store

A brand_official offer's seller is `agent_seed::<slug of the job's brand>` (`ingestion.derive_merchant_id`). A store
re-run under another spelling of its brand ("Brand A" vs "Brand A Cosmetics") therefore writes its offers under a
second seller.

**What it reads.** The dry run, and the apply's re-check, read on one SELECT-only connection.
- **Candidates:** a `catalog_merchants.source_ref` on the host, or an offer on one of the plan's products.
- **Existing seller:** a candidate that has LIVE offers at the host whose products carry the cohort's brand. Both the
  offer and its product must be unsuppressed. The host spelling is folded: case, `www.`, a trailing dot, a `:port`.
- **Never a split:**
  - `merch_obs_*` per-brand sellers of record.
  - Sellers with no live offer at the host: a retired retailer chain or any other tombstone.
  - The plan's own sellers.
  - A planned product's stored seller.
  - A verified brand claim's tenant.
  - The old spelling that `retire_stale_brand` retires.
  - A sibling brand on a shared host, or a vendor the store no longer lists. They sell another brand, so they are
    not this cohort's split. A per-host accept would also pre-accept every future split on that host, because
    accepted flags ride along to the next weekly run.
- Retailer jobs are checked too. `source=shopify_markets` captures never reach the checks; the pin is refused there.

**Default: shadow.** It holds only with `RETAILER_INGEST_SELLER_SPLIT_BLOCK_ENABLED=true`. Off, it records
`checks.seller_identity` with `mode: "shadow"` and `would_flag`, and writes one `retailer_ingest seller_identity`
WARNING line. It adds no flag, so verdicts are unchanged. Census those records before turning it on.

**Fixing a re-spelled store.**
- Usually the fix is the job's `brand`, not a pin. If the brand already matches the spelling the store's seller was
  minted from, `derive_merchant_id` yields that id and the pin is redundant.
- `options.seller_merchant_id` (brand_official only) is for when the existing products and the existing offer seller
  disagree. Every offer is written under it, and no offer-seller row is minted.
- The pin must exist in `catalog_merchants`, otherwise the stage fails with `seller_merchant_id_unknown`.
- The pin must also sell live on the job's own host (www./apex only, never a sibling subdomain), otherwise the job
  holds with `seller_merchant_id_off_host`. `source_ref` is last-writer-wins and proves nothing.
- These pin checks run in shadow mode too.
- The pin is part of the cohort's scope (`db.retailer_ingest.scope_key`). So: cancel the open job, then re-enqueue
  with the pin. Otherwise the old lineage keeps being re-crawled.
- Accept a `seller_id_split` key only if the store really changed sellers.

**Products do NOT follow the pin.** `product_key` is `ext:<slug(record brand + title)>`, and the record brand is the
job's `brand` wherever the vendor names the same brand. A re-spelled job brand therefore mints NEW product keys beside
the store's existing ones: the same split, one level down.
- Set the job `brand` to the spelling on the store's existing products (`catalog_products.brand`).
- Then compare `checks.seller_identity.planned_products_existing` with `planned_products`. None existing, on a store
  that already has products, means the spelling is still wrong.
- Proposed, not built: a BLOCK when a host with existing `ext:` products shares no planned `product_key`. It needs a
  `catalog_products` read by host, which has no index yet.

## `seller_ids_unread` -- never acceptable

The seller read above failed. It holds only when the BLOCK is on or the job is pinned, and only if the plan's own
listing read did not already hold the job. Fix the database access and re-run.

## CLI (`scripts/onboard_curated_brands.py`)

- `--apply` always reads the rows' listings and exits 2 (`current_listings_unread`, or `listing_spellings_unread`
  for retailer rows) when it cannot.
- A dry run reads them only with `--check-current-listings`; held rows print as
  `held, row names a listing this crawl does not carry {...}`, left-out listings as
  `left out, same title as another listing {...}`.
- `--allow-listing-move <product_key>` (repeatable) is the CLI's form of accepting `listing_moved`.

## Approving a held job

`POST /admin/retailer-ingest/jobs/<job_id>/approve` with `{"accepted_flags": [...], "exclude_handles": [...]}`
(`routes/admin_retailer_ingest.py`). The apply re-runs every check; only the named keys and handles change its verdict.

## The existing rows

Rows written before #2463 can still carry several listings of one host. `scripts/repair_same_title_listings.py` aligns
them (dry run by default; manifest, drift guards and revert as in its docstring). Run it only after the drain runs the
#2463 image -- an older image re-points rows and re-activates left-out seeds -- and just before a seed-refresh run, so
the offers it restores are re-priced.
