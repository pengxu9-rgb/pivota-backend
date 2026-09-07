# Indexing the three Singapore merchants — runbook

The three merchants on Meitu's Singapore try-on list whose UCP checkout reaches
`ready_for_complete`: **jsmbeauty.sg**, **makeupforever.sg**, **cocomo.sg** (VELY VELY only).

Same two-step shape as `reports/meitu_us_2026_09_04/INDEX_RUNBOOK.md`: Lane A
(`scripts/onboard_curated_brands.py`) deposits decision-grade rows, and
`scripts/promote_brand_official_canonicals.py` is the MANDATORY step 2 — nothing scheduled
promotes. Read that file first; this one records only what is different for SG, and the
differences are not small.

Written 2026-09-07. **Nothing here has been executed.** Every prod write below is for a human
to run. The four read-only prod queries in §1 and §6 were run and their answers are recorded.

---

## 0. The three things that make this different from the US run

1. **It is not a greenfield ingest.** Two of the three domains are ALREADY in the index
   (§1). Step 2 is a refresh and a scope correction, not a first landing.
2. **Currency is already stored honestly** (#2084, merged 2026-09-06). All 569 existing rows
   carry `SGD`, not USD. The SGD half of this job is already done and this runbook must not
   undo it.
3. **The blocker is `no_us_offer`, and it is live.** It is a currency test hardcoded to USD,
   sitting inside a deployment-wide `serving_eligible` bit. It blocks 410 of the 569 existing
   SG rows today, and would block every row this runbook ingests. **Nothing becomes servable
   until `PIVOTA_SERVING_PRICING_REGIONS` is set (§4).** This is the binding constraint.

---

## 1. Baseline — measured on prod 2026-09-07 (read-only)

Run via `scripts/ops/run_oneoff_job.sh` (project pivota-prod, region us-west1), base64'd inline
program, single-line JSON between sentinels. **The three domains are NOT at zero:**

| domain | catalog_products | catalog_skus | catalog_offers | external_product_seeds | offer currency | serving_eligible | trust |
|---|---|---|---|---|---|---|---|
| jsmbeauty.sg | 170 | 170 | 170 | 170 | `{SGD: 170}` | **0** | 170 blocked |
| makeupforever.sg | 0 | 0 | 0 | 0 | — | 0 | — |
| cocomo.sg | **399** | 399 | 399 | **178** | `{SGD: 399}` | **0** | 399 blocked |

All rows are `source_system = catalog_enrichment_agent_v1`. Blocker distribution
(`index_pipeline_state.blocker_code`, first-match so later gates are hidden behind earlier ones):

| domain | blockers |
|---|---|
| jsmbeauty.sg | `low_quality` 115, `short_description` 43, **`no_us_offer` 12** |
| cocomo.sg | **`no_us_offer` 398**, `non_core_product` 1 |

Corpus context: live priced offers by currency `{USD 15424, GBP 780, EUR 608, SGD 583, JPY 333,
AUD 26, SEK 25, KRW 23, HKD 22, CAD 16}`. Active seed markets `{US 11348, JP 306, EU-DE 50,
KR 23, GB 19}` — no `SG` market exists, and §5 explains why that is correct.

**Two pre-existing problems this exposes, neither caused by this change:**

- **cocomo.sg was ingested unfiltered.** Its 399 rows are the whole multi-brand retailer —
  MEDICUBE 51, BLANC DUBU 35, NUMBUZIN 24, DR. ALTHEA 15, … — deposited as brand-official
  anchors. Only **2** are VELY VELY (`PDRN Collagen Wrinkle Capsule Cream 50ml`,
  `Lacto Cleansing Pad 200ml`), and the target lip gloss is not among them:
  `SELECT … WHERE title ILIKE '%Dewy Glow Lip Gloss%'` returns **zero rows anywhere in the index**.
  See §7 Q1 — decide before running §4.
- **cocomo.sg has 399 products but 178 seeds.** Unexplained; 221 products have no
  `external_product_seeds` row and therefore no PDP the agent surface can read.

The four target lip products (product_keys as they exist today):

| product | product_key | price |
|---|---|---|
| JSM LIP-PRESSION Metal Serum Gloss | `ext:jungsaemmool-lip-pression-metal-serum-gloss::66a3c8a4` | SGD 28.20 |
| JSM New Classic Shine Lipstick | `ext:jungsaemmool-new-classic-shine-lipstick::2837ddd3` | SGD 32.90 |
| JSM New Classic Glaze Lipstick | `ext:jungsaemmool-new-classic-glaze-lipstick::6fe2d951` | SGD 28.20 |
| VELY VELY Dewy Glow Lip Gloss 4ml | **does not exist** | S$21.90 expected |

---

## 2. Local dry-run — measured 2026-09-07, no DB

Input `data/catalog_enrichment/meitu_sg_brands.jsonl` (committed on the branch in §3).

```
  jsmbeauty.sg: 171 products
  makeupforever.sg: 74 products
    vendor filter ['VELY VELY']: 1139 -> 24 products
  cocomo.sg: 24 products
plan: pdps=269 skus=269 offers=269 seeds=269 skipped=0
```

Per-domain feed shape, measured the same day:

| domain | /products.json | /meta.json currency | vendors | records |
|---|---|---|---|---|
| jsmbeauty.sg | 233 | **SGD** | 1 (`JUNGSAEMMOOL`) | 171 |
| makeupforever.sg | 74 | **SGD** | 2 spellings | 74 |
| cocomo.sg | 1,139 | **SGD** | 224 | 24 (VELY VELY) |

- `makeupforever.sg` carries two vendor spellings — `MAKE UP FOR EVER SINGAPORE` (35) and the
  slug `make-up-for-ever-singapore-official` (39). Without the `brand` override this mints two
  observed merchants, exactly as the Tarte feed did on 2026-09-05. The jsonl sets it.
- `cocomo.sg` at 1,139 PDPs unfiltered is far past the ~300 ceiling; `only_vendors` is not
  optional there.
- **`--base-listings-only` is NOT used and must not be.** None of these three feeds has the MAC
  shade-per-listing shape: jsmbeauty.sg and makeupforever.sg are single-vendor feeds with real
  multi-variant products, and the 24 VELY VELY rows are distinct products. Folding would
  collapse legitimately distinct lines.
- 233 → 171 on jsmbeauty.sg is `MIN_SELLABLE_PRICE`/no-variant/no-vendor attrition, unchanged
  behaviour from the US run.

---

## 3. Which image the job must be on

The lane changes in this runbook are on branch `feat/sg-market-catalog-onboarding`,
**PR #2121** (https://github.com/pengxu9-rgb/pivota-backend/pull/2121).
**Backend images build only on push to main**, so:

1. Merge the PR.
2. Take the merge commit SHA — call it `<SHA>` — and wait for the backend image
   `us-west1-docker.pkg.dev/pivota-shared/pivota/backend:<SHA>` to exist.
3. Point the job at it:

```bash
gcloud run jobs update catalog-curated-brand-onboard --region us-west1 --project pivota-prod \
  --image us-west1-docker.pkg.dev/pivota-shared/pivota/backend:<SHA> --quiet
```

The job `catalog-curated-brand-onboard` already exists (created 2026-09-04, see the US runbook
for its create line). **The `--only-vendor` / `--require-currency` flags and the
`meitu_sg_brands.jsonl` file do not exist in any older image** — running §4 on the currently
pinned image fails on an unrecognised argument, or worse, silently ingests all 1,139 cocomo.sg
products if only the file is missing and someone falls back to `--domain`.

---

## 4. The three commands

### Command 1 — dry-run inside prod (writes nothing)

```bash
gcloud run jobs execute catalog-curated-brand-onboard --region us-west1 --project pivota-prod --wait \
  --args="-m,scripts.onboard_curated_brands,--file,data/catalog_enrichment/meitu_sg_brands.jsonl,--max-products,2500"
```

Expect in Cloud Logging, matching §2: `jsmbeauty.sg: 171 products`, `makeupforever.sg: 74 products`,
`vendor filter ['VELY VELY']: 1139 -> 24 products`, then `plan: pdps=269 skus=269 offers=269
seeds=269 skipped=0`, then `DRY-RUN`.

**If it raises `CurrencyNotProven`, do not remove `require_currency`.** A 429 bot-check page on
`/meta.json` is indistinguishable from a missing file, and it happened repeatedly from a laptop
IP on 2026-09-07 while `/products.json` kept answering. Re-run the execution; the refusal is the
guard working.

### Command 2 — apply

```bash
gcloud run jobs execute catalog-curated-brand-onboard --region us-west1 --project pivota-prod --wait \
  --args="-m,scripts.onboard_curated_brands,--file,data/catalog_enrichment/meitu_sg_brands.jsonl,--max-products,2500,--apply"
```

Upserts on `product_key`; re-runs are idempotent and the lane never prunes. Expect roughly
`pdps=269 … merchants=4` (JUNGSAEMMOOL, the MAKE UP FOR EVER override, VELY VELY — plus whatever
the seller-of-record derivation mints). Expect the `W2 claimed-attach lookup failed … column
"status" does not exist` warning on each domain; it is the known `brand_claims.verification_status`
bug and is harmless here (no brand claims exist for these three).

This run does NOT remove cocomo.sg's 375 non-VELY-VELY rows. See §7 Q1.

### Command 3 — promote

```bash
gcloud run jobs execute catalog-curated-brand-onboard --region us-west1 --project pivota-prod --wait \
  --args="scripts/promote_brand_official_canonicals.py"                 # dry-run, read the report
gcloud run jobs execute catalog-curated-brand-onboard --region us-west1 --project pivota-prod --wait \
  --args="scripts/promote_brand_official_canonicals.py,--apply"
```

**⚠️ This script has no domain filter — it promotes ALL unpromoted Path-C rows. There are 3,460
`catalog_enrichment_agent_v1` products in prod.** That is its intended steady state (the US
runbook relies on it), but it means running it also promotes cocomo.sg's 375 out-of-scope
retailer rows. Read the dry-run report before applying.

### The step that actually makes SG servable

**None of the three commands above will produce a single serving-eligible SG row on their own.**
`no_us_offer` blocks them. After the PR merges, the region list must be widened —
**in two places, not one.**

`scripts/promote_brand_official_canonicals.py` imports
`index_pipeline_state_service.recompute_serving_eligibility` and runs it INSIDE THE JOB
(`:54`, `:278`). The predicate is a module-level constant built at import, so the value that
decides eligibility is the one in the **job's** environment, not the web service's. Setting only
`web` leaves command 3 computing the old US-only answer and writing `serving_eligible=false` —
and it would look exactly like the change not working.

```bash
# 1. the JOB — this is the one that decides what command 3 writes
gcloud run jobs update catalog-curated-brand-onboard --region us-west1 --project pivota-prod \
  --update-env-vars PIVOTA_SERVING_PRICING_REGIONS=US,SG --quiet

# 2. the SERVICE — for every later runtime recompute
gcloud run services update web --region us-west1 --project pivota-prod \
  --update-env-vars PIVOTA_SERVING_PRICING_REGIONS=US,SG

# 3. the WORKER — jobs/nightly_index_health_job.py (04:00 UTC, production worker only,
#    services/audit_scheduler.py:354-360) re-runs the eligibility batch over the WHOLE corpus
#    and rewrites serving_eligible. Unset here, every SG row goes back to no_us_offer on the
#    first nightly pass. The worker is NOT shipped by deploy-prod (manual roll) — it must also
#    be on an image that carries this change before the var means anything to it.
gcloud run services update worker --region us-west1 --project pivota-prod \
  --update-env-vars PIVOTA_SERVING_PRICING_REGIONS=US,SG
```

`scripts/recompute_index_serving_eligibility.py` is a fourth reader with the same requirement
(pass the var in `ENV_VARS` when running it through `run_oneoff_job.sh`).

Do all three BEFORE command 3. Unset, the value is `US` and the emitted SQL is byte-identical to
today's (asserted in `tests/test_region_pricing.py`). Set to `US,SG`, an offer priced in SGD
satisfies the gate — a membership test on a currency code, never a conversion.

⚠️ `--update-env-vars` on a job replaces nothing else, but the §3 `jobs update --image` and this
one are separate calls; run the image update first so the flags exist in the image being
configured. And `deploy_backend.sh` reasserts `web`'s shape on every service, so confirm the var
survives the next deploy or add it to the deploy script's env set — otherwise the SG rows go
dark again on an unrelated roll, silently.

---

## 5. Why `market` stays `US` and that is deliberate

Do not "fix" the seeds to `market='SG'`. `external_product_seeds.market` is a HARD serving
partition: `external_seed_search` appends `market = :market` and the primary agent lanes pass
`DEFAULT_EXTERNAL_SEED_MARKET = "US"` (`routes/agent_api.py:178`). Stamping `SG` deletes these
rows from the surfaces that read them. The writers know this and hardcode `US`
(`services/catalog_enrichment_agent/ingestion.py:578`).

The consequence is a recorded, **warn-only** ADR-024 invariant:
`market_currency_disagreement` counts `(market='US', currency='SGD')` pairs
(`services/catalog_invariant_checks.py:321-332`, `"warn_only": True` at :1339). This runbook adds
~269 offers to that count. **It warns; it does not fail the build.** Expect the number to rise and
do not raise the threshold to silence it — the ADR is explicit that raising it would bless the
rows it exists to flag. The real fix is ADR-024 Phase 2a (un-baking the market answer consumer by
consumer), which is not this change.

---

## 6. Verification — read-only

Run through `scripts/ops/run_oneoff_job.sh` with a base64'd inline program that prints ONE
single-line JSON blob between sentinels (Cloud Logging drops multi-line output). The SQL:

```sql
-- counts, currency, eligibility and trust, per domain
SELECT cp.source_domain,
       count(DISTINCT cp.product_key)                                        AS products,
       count(DISTINCT cs.sku_key)                                            AS skus,
       count(DISTINCT co.offer_id)                                           AS offers,
       count(DISTINCT eps.id)                                                AS seeds,
       array_agg(DISTINCT co.currency)                                       AS currencies,
       count(DISTINCT ips.content_key) FILTER (WHERE ips.serving_eligible)   AS serving_eligible,
       count(DISTINCT crt.subject_key) FILTER (WHERE crt.serving_decision = 'public') AS trust_public,
       count(DISTINCT crt.subject_key) FILTER (WHERE crt.serving_decision = 'shadow') AS trust_shadow,
       count(DISTINCT crt.subject_key) FILTER (WHERE crt.serving_decision = 'blocked') AS trust_blocked
FROM catalog_products cp
LEFT JOIN catalog_skus  cs  ON cs.product_key  = cp.product_key
LEFT JOIN catalog_offers co ON co.product_key  = cp.product_key
LEFT JOIN external_product_seeds eps ON eps.attached_product_key = cp.product_key
LEFT JOIN index_pipeline_state   ips ON ips.content_key = cp.content_key
LEFT JOIN catalog_row_trust      crt ON crt.subject_key = cp.product_key
WHERE cp.source_domain IN ('jsmbeauty.sg','makeupforever.sg','cocomo.sg')
GROUP BY 1;

-- what is still blocking, now that no_us_offer should be gone
SELECT cp.source_domain, ips.blocker_code, count(*)
FROM index_pipeline_state ips
JOIN catalog_products cp ON cp.content_key = ips.content_key
WHERE cp.source_domain IN ('jsmbeauty.sg','makeupforever.sg','cocomo.sg')
GROUP BY 1, 2 ORDER BY 1, 3 DESC;

-- the four target lip products
SELECT cp.product_key, cp.brand, cp.title, cp.source_domain,
       co.currency, co.list_price, ips.serving_eligible, crt.serving_decision
FROM catalog_products cp
LEFT JOIN catalog_offers co ON co.product_key = cp.product_key
LEFT JOIN index_pipeline_state ips ON ips.content_key = cp.content_key
LEFT JOIN catalog_row_trust    crt ON crt.subject_key = cp.product_key
WHERE (cp.source_domain = 'jsmbeauty.sg' AND (
         cp.title ILIKE 'LIP-PRESSION Metal Serum Gloss'
      OR cp.title ILIKE 'New Classic Shine Lipstick'
      OR cp.title ILIKE 'New Classic Glaze Lipstick'))
   OR (cp.source_domain = 'cocomo.sg' AND cp.title ILIKE '%Dewy Glow Lip Gloss%');
```

Expected after §4, if `PIVOTA_SERVING_PRICING_REGIONS=US,SG` is set: `no_us_offer` gone from both
domains; jsmbeauty.sg's residual blockers still `low_quality` / `short_description` (a content
problem this change does not touch); currencies `{SGD}` only; the three JSM lip product_keys in
§1 present; the VELY VELY gloss present at SGD 21.90 with variant
`gid://shopify/ProductVariant/44922188071158`.

Then re-probe `search_catalog` for the SG merchants. Note the US-run caveat still applies: served
rows are `readiness_tier: referral_only`, `buyable: false` — link-out PDPs, not a checkout path,
even though the UCP door completes.

---

## 7. Open questions — decide before §4

**Q1. cocomo.sg's 375 out-of-scope rows.** A multi-brand retailer's catalogue is deposited under
`source_system=catalog_enrichment_agent_v1`, which the promote script treats as brand-official.
Command 3 promotes them.
*Recommendation:* withdraw them before promoting, with `scripts/withdraw_catalog_rows.py`
(reversible suppression, never a DELETE — the US runbook documents it). They are identifiable as
`source_domain='cocomo.sg' AND brand <> 'VELY VELY'`. Do it as a separate, explicitly-approved
step; it is 375 rows and is not part of this ingest.

**Q2. The VELY VELY target gloss does not exist in the index.** `Dewy Glow Lip Gloss` returns zero
rows anywhere. It is in the 24-record dry-run plan, so command 2 should create it.
*Recommendation:* verify explicitly in §6 rather than assuming; if it is still absent, the
`MIN_SELLABLE_PRICE` / variant-pick path is the first place to look.

**Q3. cocomo.sg: 399 products vs 178 seeds.** 221 products carry no seed row and so have no PDP
the agent surface reads. Cause unknown.
*Recommendation:* diagnose before promoting cocomo.sg at all; a promoted row with no seed is a
`no_seed` blocker at best and an incoherent PDP at worst.

**Q4. `PIVOTA_SERVING_PRICING_REGIONS` is deployment-wide, not per-request.** Setting `US,SG`
makes an SGD row serving-eligible for every buyer, including US ones. The row is honest about its
currency everywhere it is rendered, but "eligible to serve" stops meaning "priced for the buyer
in front of you".
*Recommendation:* accept it for the SG pilot — it is strictly better than the status quo, where
the SG rows are ingested and then invisible — and treat per-request region resolution as the
ADR-024 Phase 2a work it is. Do not set this var on a deployment whose only buyers are US until
that lands.

**Q5. jsmbeauty.sg's 115 `low_quality` + 43 `short_description`.** Independent of currency: the
feed's body copy is thin. Even with SG served, most of the 171 rows stay blocked.
*Recommendation:* expect single- or low-double-digit serving_eligible from jsmbeauty.sg, not 171.
The description backfill / LLM enrichment lane is the fix, and is separate work.

**Q6. `routes/agent_api.py:3766` still does `price_currency = … or "USD"`,** and
`:7924` hardcodes `"currency": "USD"` in the cart estimate.
*Recommendation:* not triggered by these rows (they all carry SGD), so out of scope here — but it
is a live fabrication for any currency-less row and deserves its own PR.
