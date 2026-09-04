# Indexing the Meitu US list with what we already have — runbook

Goal, in order: (1) get the Meitu-US brands' *current* catalogs into the commerce index through the
existing ingestion lanes, (2) only then widen the seller set (retailer offers on those canonicals).
Nothing here invents a new pipeline. Two lanes apply, one per storefront platform.

Status 2026-09-04: dry-runs done locally (no DB). The prod write step needs a Cloud Run Job and was
blocked by the session's permission classifier — run the commands in §2 yourself.

## 1. Which lane covers which brand

| Brand (Meitu try-ons) | Storefront | Lane | Volume (dry-run 2026-09-04) |
|---|---|---|---|
| Flower Beauty (6) | flowerbeauty.com, Shopify | **A — curated brand feed** (`scripts/onboard_curated_brands.py`) | 49 products → 49 PDPs / 49 SKUs / 49 offers |
| Stila (2) | stilacosmetics.com, Shopify | A | 125 → 124 PDPs |
| Tarte (8) | tartecosmetics.com, Shopify | A | 249 → 249 PDPs |
| M·A·C (6) | maccosmetics.com, Shopify | A, **with the shade-listing decision in §3** | 2,016 products → 1,998 PDPs as-is, **299** with `--base-listings-only` (measured) |
| Clarins (4) | clarinsusa.com, Salesforce CC | **C — validated JSONL → `run_catalog_enrichment.py ingest`** (hand-validated; no Gemini key exists in prod or locally) | 4 PDPs, see §4 |
| Lord & Berry (3) | lordandberry.com/usa, Magento | C | 3 PDPs |
| Charlotte Tilbury (2) | charlottetilbury.com (blocked search) / Bluemercury / Ulta | C, anchored on the brand PDP once a URL is confirmed; Ulta rows already exist as external seeds | 2 PDPs |
| NOTE Cosmetics (2) | notecosmetics.com, WooCommerce | C | 2 successor PDPs |
| Surratt (2) | surrattbeauty.com hides its catalog; Bluemercury carries Lipslique | C via Bluemercury PDP | 1 PDP |
| Lime Crime (3) · I'M MEME (4) | Amazon only | Phase 2 only — no brand-owned PDP to anchor on; attach as retailer offers if a canonical exists | — |
| UZI · TYRA · GLAMGLOW · PIXEL (11) · PONY EFFECT · Memebox (4) | dead / no US channel | nothing to index | — |

Lane A records are *depositable canonical anchors*: brand-direct, `official_url` = the brand's own PDP, GTIN
from the variant barcode, body text and INCI captured deterministically. They land in the same
`catalog_products / catalog_skus / catalog_offers / external_product_seeds` chain as merchant-sync PDPs
(`services/catalog_enrichment_agent/apply.py`), `source_system = catalog_enrichment_agent_v1`,
`source_domain = <brand domain>`, batch id from `curated_brands:<n>`.

## 2. Lane A — the prod run (Cloud Run Job, backend image, crawl egress)

Shape mirrors `external-seed-destination-sweep` (the other job that fetches storefronts): backend image,
compute SA, `DATABASE_URL` secret, **pivota-crawl subnet** (this is 2,400 storefront page fetches — crawl
traffic must not leave on the payment NAT, per docs/commerce-index-crawl-lane.md). The image already
carries the lane (`scripts/onboard_curated_brands.py` present at d872c362, the current prod web image).

```bash
IMG=us-west1-docker.pkg.dev/pivota-shared/pivota/backend:d872c36263e5721b81e2743cca56d8337afa6ce7
gcloud run jobs create catalog-curated-brand-onboard --region us-west1 --project pivota-prod \
  --image "$IMG" --service-account 388293626878-compute@developer.gserviceaccount.com \
  --network default --subnet pivota-crawl --vpc-egress all-traffic \
  --max-retries 0 --task-timeout 3600s --cpu 1 --memory 2Gi \
  --labels "env=prod,managed-by=manual,lane=catalog-curated-brand-onboard" \
  --set-secrets "DATABASE_URL=DATABASE_URL:latest" \
  --set-env-vars "PIVOTA_ENV=production,PIVOTA_SERVICE_NAME=catalog-curated-brand-onboard,PIVOTA_COMMIT_SHA=d872c36263e5721b81e2743cca56d8337afa6ce7,DB_POOL_MIN_SIZE=1,DB_POOL_MAX_SIZE=2" \
  --command python \
  --args="-m,scripts.onboard_curated_brands,--domain,flowerbeauty.com,--category,beauty/makeup,--max-products,2500" --quiet
```

Step 1 — dry-run inside prod (proves egress + DB reachability, writes nothing):

```bash
gcloud run jobs execute catalog-curated-brand-onboard --region us-west1 --project pivota-prod --wait
```

Expect in Cloud Logging: `flowerbeauty.com: 49 products` then `plan: pdps=49 …` then `DRY-RUN`.

Step 2 — apply, one execution per brand (the `--args=` form is required because the value starts with `-m`;
note the `=`):

```bash
for d in flowerbeauty.com stilacosmetics.com tartecosmetics.com; do
  gcloud run jobs execute catalog-curated-brand-onboard --region us-west1 --project pivota-prod --wait \
    --args="-m,scripts.onboard_curated_brands,--domain,$d,--category,beauty/makeup,--max-products,2500,--apply"
done
```

M·A·C: decide §3 first, then either the same line with `maccosmetics.com`, or the filtered variant.

Step 3 — verify (any prod psql / ops shell):

```sql
SELECT source_domain, COUNT(*) pdps,
       COUNT(*) FILTER (WHERE category_path LIKE 'beauty/makeup/lip%') lip
FROM catalog_products
WHERE source_system = 'catalog_enrichment_agent_v1'
  AND source_domain IN ('flowerbeauty.com','stilacosmetics.com','tartecosmetics.com','maccosmetics.com')
GROUP BY 1;
```

Then re-run the four `search_catalog` brand queries from `index_coverage.md`; the expected change is
first-party rows for Flower Beauty / Stila / Tarte / MAC where there were none.

Rollback: rows are keyed `ext:<brand>-<handle>::<hash>` with `source_domain` set, so a brand can be
withdrawn with one `DELETE … WHERE source_system='catalog_enrichment_agent_v1' AND source_domain=$1`
across the four tables (offers → skus → products → seeds), same FK order the executor uses.

## 3. The M·A·C decision — shade listings

maccosmetics.com's `/products.json` lists **every shade as its own product**: in a 1,500-product sample,
1,366 rows are single-variant, shade-suffixed listings ("Retro Matte Lipstick - Ruby Woo") collapsing onto
112 base names, and for 106 of those the un-suffixed base listing also exists. Lane A keys PDPs on
(brand, title), so ingesting as-is mints ~1,900 near-duplicate MAC PDPs (81 "Small Eye Shadow - …", 71
"Studio Fix Powder Plus Foundation - …"). Tarte (6 of 429), Stila (28 of 130) and Flower Beauty (0) do
not have this shape.

Options:
- **Filter to base listings** (recommended, implemented in this branch): `--base-listings-only` skips
  single-variant rows whose title ends in ` - <shade>` when the base title is also in the feed
  (`services.curated_brand_feed.drop_shade_listings`, 4 unit tests). Measured 2026-09-04: maccosmetics.com
  2,016 → **299 PDPs**, Amplified / Retro Matte / Retro Matte Liquid Lipcolour all kept as base listings.
  The flag is NOT in the pinned image d872c362 → merge + new backend tag before the MAC execution, then:
  `--args="-m,scripts.onboard_curated_brands,--domain,maccosmetics.com,--category,beauty/makeup,--max-products,2500,--base-listings-only,--apply"`.
  Ulta's index row for MAC is shade-level, so the identity graph will still attach it by retailer_match_key.
- **Ingest as-is**: ~2,000 MAC PDPs today, dedupe later with the crawl lane's `dedupe_cohort` logic
  (docs/HANDOFF_crawl_side_dedup.md), which currently targets "(Copy)" clones, not shades.

Either way the two Meitu-exact lines (Retro Matte Lipstick $24, Retro Matte Liquid Lipcolour $27) exist as
base listings and land.

## 4. Lane C — the non-Shopify brands (hand-validated JSONL)

`run_catalog_enrichment.py validate` is Gemini-only and no `GEMINI_API_KEY` exists in Secret Manager or the
prod service env (only `OPENAI_API_KEY`). So the validated file was written by hand from storefronts read on
2026-09-04 — `data/catalog_enrichment/meitu_us_nonshopify_validated.jsonl`, 8 records:

| Record | Anchor PDP | Price | Strong id |
|---|---|---|---|
| Lord & Berry Vogue Matte Lipstick (#8 exact) | lordandberry.com/usa/vogue-matte-lipstick | $29 | none |
| Lord & Berry Timeless Kissproof Lipstick (#9 exact) | …/timeless-kissproof-lipstick | $25 | none |
| Lord & Berry Ultimate Lip Liner (#7 exact) | …/ultimate-lip-liner | $24 | none |
| Clarins Joli Rouge (#42 exact) | clarinsusa.com …/CS00759921.html | $40 | GTIN 3666057117008 |
| Clarins Joli Rouge Shine (#43 successor) | …/CS00760008.html | $40 | GTIN 3666057117220 |
| Clarins Lip Comfort Oil (#44 exact) | …/CS01219820.html | $32 | GTIN 3666057222481 |
| Clarins Lip Perfector gloss (#41 successor) | …/CS02015706.html | $30 | none |
| NOTE Aura Silk Lipstick (#53 successor) | notecosmetics.com/product/aura-silk-lipstick/ | **not captured** → destination-only offer | none |

Local dry-run: `ingest plan: pdps=8 skus=8 merchants=6 offers=8 seeds=8 skipped=0`, audit
`no_strong_identifier: 5`. (The runner's dry-run sample-row log crashed on a datetime — fixed in this branch
with `default=str`; the plan itself was already built.) Apply:

```bash
gcloud run jobs execute catalog-curated-brand-onboard --region us-west1 --project pivota-prod --wait \
  --args="scripts/run_catalog_enrichment.py,ingest,--category,meitu_us_nonshopify,--data-dir,data/catalog_enrichment,--apply"
```

…once the job is pointed at an image that carries the file. Note `data/catalog_enrichment/.gitignore` ignores
`*_validated.jsonl` (validated files are normally generated by the Gemini stage), so this hand-validated one
needs `git add -f`, or a candidates-style name, to ride in the image. A copy sits next to this runbook.

Not minted, on purpose (ADR-001, brand-official first): Charlotte Tilbury Hot Lips / Matte Revolution and
Surratt Lipslique, whose only readable PDPs today are Bluemercury's (Shopify JSON gives barcodes
5056446628921 / 5056446640756 / 617037660642 and prices $37 / $39 / $34). They are RESIDUE until a brand PDP
URL is confirmed — charlottetilbury.com blocks automated reads and surrattbeauty.com hides its catalog —
and then Bluemercury attaches as a retailer offer in Phase 2 with those prices.

## 5. Phase 2 — extend sellers (after §2 lands)

Sellers we already hold a trusted price for, to attach with `scripts/attach_retailer_offer.py` once the
canonical `product_key` exists (dry-run first; `--price` only from a feed we read ourselves):

| Canonical (Lane A/C) | Retailer | Evidence | Price |
|---|---|---|---|
| Charlotte Tilbury Hot Lips | ulta.com | already an external seed in the index (`search_raw.json`) | $37.00 |
| Charlotte Tilbury Hot Lips 2 | bluemercury.com | UCP door priced 2026-09-04 (`us_lip_checkouts.json`) | $39.00 |
| Charlotte Tilbury Matte Revolution | bluemercury.com | UCP door priced | $37.00 |
| Surratt Lipslique | bluemercury.com | UCP door priced | $34.00 |
| MAC Dazzlelips Crayon | ulta.com | already an external seed | $28.00 |
| Lime Crime Velvetines | amazon.com (B00PZ8DXJW) | listing live, price not captured → destination-only until a price feed | — |
| I'M MEME Color Key Ring Velvet Lip Tint | amazon.com | listing live | $9.60 |

The retailer keying rule (W2, etld1 alone) already applies to ulta.com seeds, so the two Ulta rows attach to the
new brand canonicals through the identity graph rather than a new offer row; verify with
`scripts/parity_watch_seller_ref.py` after §2.

Sellers still to source: Sephora / Nordstrom / Macy's for Clarins + Charlotte Tilbury (no crawlable price;
needs a feed), Walmart / CVS for Flower Beauty (mass retail, feed needed). `scripts/ingest_stylekorean_brand.py`
does not apply — StyleKorean's brand sitemap carries none of these brands (checked 2026-09-04).
