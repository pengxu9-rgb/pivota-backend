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

## CLI (`scripts/onboard_curated_brands.py`)

- `--apply` always reads the rows' listings and exits 2 (`current_listings_unread`) when it cannot.
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
