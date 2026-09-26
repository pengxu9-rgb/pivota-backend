# Variant identity runbook — Meitu try-on merchants

**Date:** 2026-09-07 · **Author:** investigation session (read-only; nothing below was executed against prod)

Goal: make the four merchants whose UCP checkout reaches `ready_for_complete` resolvable at
**variant grain** end to end — `flowerbeauty.com` (US), `jsmbeauty.sg`, `cocomo.sg`,
`makeupforever.sg` (SG).

Every command in this file that WRITES is written out for a human to run. Nothing here was run.
The read-only probes that produced the numbers are in this directory (`prod_*.py`, `run_*.sh`).

---

## 0. The headline

`#2113` (`d466bc6ee`) fixed ingestion's habit of **discarding** real merchant variant ids.
It does **not** fix Flower Beauty, because the curated feed never put those ids into the record
for ingestion to keep. Measured from prod egress on 2026-09-07:

| stage | before | after the fix in this branch |
|---|---|---|
| `flowerbeauty.com/products.json` products | 49 | 49 |
| …of which multi-variant | 29 | 29 |
| real Shopify variant ids the storefront serves | **185** | 185 |
| ids that survive `records_for_brand` into the record | **0** | **185** |
| planned `catalog_skus` rows | 49 | **234** (49 canonical + 185 variant) |
| planned variant SKUs carrying a real numeric id | **0** | **185** |
| …of those, with a paired `catalog_offers` row | 0 | **185 / 185** |
| the four Tier-A variants present | **0 / 4** | **4 / 4**, each with an offer |

There are **three** independent gates between a crawled variant id and a spendable SKU. `#2113`
closed the second. This branch closes the first. The third (the gateway allow-list) is section 3.

1. **`services/curated_brand_feed.py:915`** — variants were emitted only for rows the shade-fold
   had just built (`product.get(FOLDED_INTO_KEY)`). Flower Beauty publishes shades natively, so
   `fold_shade_listings` folds **zero** of its products (`bases=0, shades=0`) and the gate refused
   all 185 ids. **Fixed on branch `feat/curated-feed-real-variants`.**
2. **`services/catalog_enrichment_agent/ingestion.py:_build_variant_sku_inserts`** — returned `[]`
   for `len(variants) < 2`. **Fixed by #2113**, already deployed on `web`.
3. **Gateway `MERCHANT_VARIANT_SOURCING_*`** — absent in prod, so runtime variant sourcing is off
   for every brand including `murad.com`. **Section 3.**

---

## 1. Deploy state (verified 2026-09-07)

| thing | value |
|---|---|
| `#2113` merge SHA on `origin/main` | `d466bc6eece02233f603ea682fcdd34da920122a` |
| `web` service image | `…/backend:d466bc6eece02233f603ea682fcdd34da920122a` — **contains #2113** |
| `backend:latest` | same digest `sha256:7401862a…` — **contains #2113** |
| job `catalog-curated-brand-onboard` image | `…/backend:23594c37f291fd36a99757d54ccbda7ce0ac239c` — **23 commits BEHIND #2113** |
| gateway service image | `…/gateway:14d54ffb9001a3300bb644986fc6e6fdb153a1a6` = `origin/main` HEAD, 0 behind |

Two consequences:

- `scripts/ops/run_oneoff_job.sh` defaults to `IMAGE=backend:latest`, so **anything run through it
  already gets post-#2113 code**. That is how every probe in this directory was run.
- The **`catalog-curated-brand-onboard` job is stale and must be repointed** before it is used.
  Note it is currently also *repurposed*: its `--args` run `scripts.ops_currency_probe`, not the
  onboard, and its `PIVOTA_COMMIT_SHA` env (`a8e4a605…`) disagrees with its own image tag
  (`23594c37f…`) — so the job does not know what it is running. **Prefer `run_oneoff_job.sh`**,
  which creates a throwaway job on `:latest` and deletes it on every exit path.

  If the job must be used anyway, repoint the image first — `jobs update` MERGES, so this touches
  nothing but the image:

  ```bash
  gcloud run jobs update catalog-curated-brand-onboard \
    --region us-west1 --project pivota-prod \
    --image us-west1-docker.pkg.dev/pivota-shared/pivota/backend:<sha of main AFTER the feed fix>
  ```

  Pin a full SHA, never `:latest` — a job that floats on a moving tag cannot be rolled back to the
  thing that ran. Then re-describe and confirm both the image and the args before executing:

  ```bash
  gcloud run jobs describe catalog-curated-brand-onboard --region us-west1 --project pivota-prod \
    --format='yaml(spec.template.spec.template.spec.containers[0].image,
                   spec.template.spec.template.spec.containers[0].args)'
  ```

Verification commands used:

```bash
git log origin/main --oneline --grep=2113
git merge-base --is-ancestor d466bc6ee 23594c37f291fd36a99757d54ccbda7ce0ac239c   # -> non-zero
gcloud run jobs describe catalog-curated-brand-onboard --region us-west1 --project pivota-prod \
  --format='yaml(spec.template.spec.template.spec.containers[0].image)'
gcloud run services describe web --region us-west1 --project pivota-prod \
  --format='value(spec.template.spec.containers[0].image)'
```

---

## 2. Flower Beauty — re-onboard at variant grain

### 2.0 Prod baseline (read-only, already taken)

```
products                          49
  with seed_data.snapshot.variants 0
  with product_payload.variants    0
catalog_skus                      49   ALL shape = source_variant_id == product_key (fabricated)
  with a catalog_offers row       49
  ingestion `::v:` shape           0
  promoter `::v::` shape           0
  with variant_id_provenance       0
real numeric (>=8 digit) ids       0
Tier-A ids present in DB           0 / 4
```

**The backfill in `#2113` is a NO-OP for Flower Beauty**: it recovers ids from
`seed_data.snapshot.variants` / `product_payload.variants`, and Flower Beauty has **zero** of
both. The identity has to come from a fresh crawl. That is what section 2.2 does.

Re-run this baseline any time with:

```bash
reports/meitu_final_2026_09_07/run_prod_query.sh \
  reports/meitu_final_2026_09_07/prod_baseline_query.py
```

### 2.1 Land the feed fix

Branch `feat/curated-feed-real-variants` (PR opened; see the session report for the link).
It adds `records_for_brand(emit_real_variants=True)` and the CLI flag `--emit-real-variants`.
**It changes no existing caller's behaviour** — the flag defaults off, and the fold lane is
untouched.

Wait for merge, then confirm `backend:latest` has moved past it:

```bash
gcloud artifacts docker images list us-west1-docker.pkg.dev/pivota-shared/pivota/backend \
  --include-tags --filter='tags:latest' --format='value(version,tags,createTime)' --limit=3
```

`deploy-prod` auto-deploys `web` on merge, which is what republishes `:latest`.

> **`SUBNET=pivota-crawl` on both runs below, and it is not optional.** This step uses the job
> runner *specifically* to obtain merchant egress — the note further down says not to run it from
> a laptop because `flowerbeauty.com` answers 429 to this office's address. But the runner's
> default subnet egresses from `8.231.167.230`, the address given to payment partners for
> allowlisting, and NAT port exhaustion is per-IP, so paging `/products.json` for 49 products and
> ~185 SKUs there can starve payment egress. `pivota-crawl` egresses from `34.82.199.35`, which is
> reserved for exactly this. The override landed with the crawl-subnet change; if your checkout
> predates it, `scripts/ops/run_oneoff_job.sh` will ignore `SUBNET` and you must rebase first.

### 2.2 Dry run in prod (SAFE — no writes)

```bash
SUBNET=pivota-crawl scripts/ops/run_oneoff_job.sh scripts/onboard_curated_brands.py \
  --domain flowerbeauty.com \
  --category "beauty/makeup/lips" \
  --max-products 500 \
  --emit-real-variants
```

**Expected, and the gate for proceeding** (measured on this branch from prod egress):

```
  flowerbeauty.com: 49 products
plan: pdps=49 skus=234 offers=234 seeds=49 skipped=0
```

If `skus` is 49 rather than 234, the image does not have the feed fix — stop and recheck 2.1.

> Do NOT run this from a laptop. `flowerbeauty.com` answers **429** to this office's egress
> (Cloudflare); the local run returns `0 products` and reads exactly like an empty storefront.

> **After #2123/#2124 (seed row gains the lone real id):** the re-run below backfills the seed variant for
> Petal Pout Lip Color. Its lone variant is a NAMED shade (`Flamingo Flirt - Cream`, variant `17281773207622`,
> see `report.md` and `sku_list.csv`), so it should acquire a one-entry `options` axis and become visible in the
> selector. If §2.5 still shows `hidden_from_selector: true`, that is NOT expected — check the axis name the feed
> emitted (`option_name`) before reading it as the gateway's placeholder rule.

### 2.3 Apply — WRITES TO PROD

```bash
SUBNET=pivota-crawl scripts/ops/run_oneoff_job.sh scripts/onboard_curated_brands.py \
  --domain flowerbeauty.com \
  --category "beauty/makeup/lips" \
  --max-products 500 \
  --emit-real-variants \
  --apply
```

Expect `applied: {...}` with ~185 new `catalog_skus` and ~185 new `catalog_offers`.
Both upserts are keyed (`derive_variant_sku_key`, `derive_offer_id`), so a re-run is idempotent.

### 2.4 Promote the official canonicals

```bash
scripts/ops/run_oneoff_job.sh scripts/promote_brand_official_canonicals.py --apply
```

> The script takes only `--apply` and `--limit N` (verified in review, 2026-09-07). It has NO brand
> or domain filter: it promotes the whole unpromoted Path-C population, which is the documented
> step 2 after every curated-brand run. Dry-run first (omit `--apply`) and read the blocker
> breakdown it prints; the Flower Beauty rows are expected to move from `blocked` to `shadow`.

### 2.5 Verify (read-only)

Re-run the baseline probe from 2.0. The pass condition:

```
catalog_skus shape `real_numeric`   185   with_offer 185   ingestion_shape 185
                   `equals_product_key`  49  (the canonical rows; these are EXPECTED to remain)
Tier-A ids present in DB            4 / 4
```

Spot-check one Tier-A row end to end:

```
ext:flower-beauty-petal-pout-lip-color::ffc9b11a::v:17281773207622
ext:flower-beauty-perfect-pout-moisturizing-lipstick::72132185::v:32120934727750
ext:flower-beauty-perfect-pout-sculpting-lip-liner::a5f25c8a::v:39777342488646
ext:flower-beauty-perfect-pout-soft-matte-lip-color::4632b275::v:39406294532166
```

### 2.6 Rollback

Every row this writes is stamped `source_system` = the ingestion agent version and carries
`sku_payload.variant_id_provenance='merchant_issued'`, and every `sku_key` carries the `::v:`
infix. A reversal is therefore a bounded `DELETE` over
`catalog_offers`/`catalog_skus WHERE sku_key LIKE 'ext:flower-beauty-%::v:%'`.
**Write it out and have it reviewed before running it; do not improvise a DELETE against prod.**

### 2.7 The three SG brands

`jsmbeauty.sg`, `cocomo.sg`, `makeupforever.sg` are being ingested by another agent — do not
duplicate that work. When their rows land, run the 2.0 baseline probe against each (edit `DOM` in
`prod_baseline_query.py`) before assuming they need the same treatment: if they arrived through
`onboard_curated_brands`, they need `--emit-real-variants` too; if they arrived with
`seed_data.snapshot.variants` populated, the `#2113` backfill (section 4) covers them instead.

---

## 3. Gateway — arm runtime variant sourcing

Take the BEFORE snapshot of the deployed env first — it is the baseline every later diff and
rollback decision reads, and it is deliberately NOT committed (a service env dump carries legacy
hosts and plaintext values that the repo's URL ratchet refuses):

```bash
gcloud run services describe gateway --region us-west1 --project pivota-prod --format=yaml \
  > reports/meitu_final_2026_09_07/gateway_env_before.yaml   # gitignored artefact, keep local
```

### 3.1 What is actually there

**Both variables are ABSENT from the deployed service** — not "set to murad.com". Verified on
`gateway-00108-cup` and every retained revision back to `gateway-00078-lep` (2026-08-31), and on
all five Cloud Run services in the project. There is no `.env`, Dockerfile or deploy-script default
supplying them either.

So today, with `MERCHANT_VARIANT_SOURCING_ENABLED` unset (`src/server.js:30131-30134`):

```js
const normalized = String(process.env.MERCHANT_VARIANT_SOURCING_ENABLED || '').trim().toLowerCase();
return ['1', 'true', 'on', 'yes'].includes(normalized);   // '' -> false
```

…the feature is **off for every brand, including `murad.com`**. This is an **arming**, not an
extension of an existing list.

### 3.2 The matching shape (file:line evidence)

`src/services/merchantVariantSource.js:553-560` — the list is split on `,`, trimmed, lowercased,
a leading `.` and a leading `www.` stripped, empties dropped:

```js
const brands = String(raw || '')
  .split(',')
  .map((b) => str(b).toLowerCase().replace(/^\./, '').replace(/^www\./, ''))
  .filter(Boolean);
```

`:546-551` — the comparison is **exact host equality or a dot-boundary subdomain suffix**, never a
substring:

```js
function hostMatchesBrand(host, brand) {
  const h = str(host).toLowerCase().replace(/^www\./, '');
  const b = str(brand).toLowerCase().replace(/^\./, '').replace(/^www\./, '');
  if (!h || !b) return false;
  return h === b || h.endsWith(`.${b}`);
}
```

The value it is matched against is a **HOST**, not a brand name, merchant id or slug: the hostname
of the first non-Pivota URL on the product row, read in order
`destination_url` → `external_redirect_url` → `source_url` (`:113-122`), lowercased and
`www.`-stripped by `urlIdentity` (`:70-84`). `url` and `canonical_url` are never consulted (Pivota's
own hosts are excluded at `server.js:30177`). The check runs before any outbound contact
(`:373`, "SCOPE BEFORE CONTACT").

**So the four bare domains are the correct form.** Write `flowerbeauty.com`, not `Flower Beauty`
and not `https://flowerbeauty.com`; a protocol or a trailing slash will NOT match.

### 3.3 The command — do NOT use `--set-env-vars`

`--set-env-vars` REPLACES the whole set; the live gateway carries ~382 vars, and a deploy once
wiped five. `--update-env-vars` MERGES.

```bash
gcloud run services update gateway \
  --region us-west1 --project pivota-prod \
  --update-env-vars MERCHANT_VARIANT_SOURCING_ENABLED=1,MERCHANT_VARIANT_SOURCING_BRANDS=murad.com\,flowerbeauty.com\,jsmbeauty.sg\,cocomo.sg\,makeupforever.sg
```

> The commas **inside** the brand list must be escaped (`\,`), otherwise gcloud splits them as
> separate `KEY=VALUE` pairs and the command fails. The alternative is gcloud's
> alternate-delimiter form:
> `--update-env-vars ^@^MERCHANT_VARIANT_SOURCING_ENABLED=1@MERCHANT_VARIANT_SOURCING_BRANDS=murad.com,flowerbeauty.com,jsmbeauty.sg,cocomo.sg,makeupforever.sg`

`murad.com` is retained deliberately: it is the documented pilot brand, and since the flag is
currently off nothing is lost by naming it now.

**Post-change diff:**

```bash
gcloud run services describe gateway --region us-west1 --project pivota-prod --format=yaml \
  > reports/meitu_final_2026_09_07/gateway_env_after.yaml
diff -u reports/meitu_final_2026_09_07/gateway_env_before.yaml \
        reports/meitu_final_2026_09_07/gateway_env_after.yaml
```

The diff must show **only** the two new env keys, a new revision name, and the
`serving.knative.dev/*` timestamps. Anything else — a dropped env var, a changed secret mount, a
changed concurrency/CPU/memory — is a regression: roll back to `gateway-00108-cup` with

```bash
gcloud run services update-traffic gateway --region us-west1 --project pivota-prod \
  --to-revisions gateway-00108-cup=100
```

Also confirm the count is unchanged:

```bash
grep -c 'name:' reports/meitu_final_2026_09_07/gateway_env_before.yaml
grep -c 'name:' reports/meitu_final_2026_09_07/gateway_env_after.yaml
```

### 3.4 Notes

- **No rebuild is needed.** Both variables are read live per call — `isEnabled` is a thunk
  (`server.js:30172`) and `isBrandAllowed` re-reads `process.env` on every call (`:30174`).
  The deployed image is already `origin/main` HEAD.
- **The env survives later code deploys.** `infra/gcp/deploy_gateway.sh` defaults to
  `CONFIG=preserve`, which restamps only `PIVOTA_COMMIT_SHA` via `--update-env-vars`
  (`:128-140`). Do **not** run it with `CONFIG=apply` — that rewrites env from Railway-derived
  files that no longer exist post-cutover.
- **Gateway deploys are manual**, never on merge (`.github/workflows/production-deploy-promote.yml`
  is `workflow_dispatch`-only and says so; `gateway-prod-drift.yml` is an alarm, not a deploy).
- One flip arms **both** consumers: the checkout intake resolver (`server.js:31638`) and the PDP
  variant publisher (`:44496`/`:44520`).
- **Check before declaring success:** the allow-list matches the host on the product row's
  `destination_url`/`external_redirect_url`/`source_url`. `cocomo.sg` and `jsmbeauty.sg` were
  confirmed present in those exact columns; for `flowerbeauty.com` and `makeupforever.sg` the host
  was observed on catalog `url` fields only. If a row was seeded from a **retailer** PDP the host
  will be the retailer's and the brand entry will not match. Spot-check one row per brand first.
- All five apexes answer 200 with no redirect and serve a real `dev.ucp.shopping` mcp profile, so
  `discoverEndpoint`'s `redirect: 'manual'` refusal does not bite here. It is the first thing to
  check for any future brand added to this list.

---

## 4. The wider backfill (#2113 wrote it; #2118 made it safe to run)

`scripts/backfill_variant_identity_skus.py` writes the **SKU + offer pair** in ingestion's shape
(`derive_variant_sku_key` / `derive_offer_id`), refuses ids it cannot place as merchant-issued,
refuses to let a variant inherit the product's price, and stamps `source_system` +
`variant_id_provenance` on every row.

It exists because `services/catalog_variant_promoter` writes SKUs and **no** offers: measured
2026-09-07, 5,271 promoter-built `<pk>::v::<id>` SKUs on the external track carry **0** offers,
while 1,583 of 1,583 ingestion-built `<pk>::v:<id>` SKUs carry one. Recall joins offers on
`sku_key`, so a promoter SKU is inert. (The *price* gate is product-grain `EXISTS` and does not
flip — an earlier version of this section said it did.)

**As shipped in #2113 it would have crashed on 30% of its own plan.** Three adversarial review
rounds found: `ON CONFLICT (sku_key)` missing the second unique index, an `ORDER BY` used as a
suppression filter (which would have resurrected withdrawn offers), an unscoped seed join, an
`offer_id` that moved between runs, and a unique-violation handler matching on exception *text*
that also swallowed "no unique constraint matching the ON CONFLICT specification". All fixed in
#2118 (`aa5802e6c`). Do not run a build older than that.

### Invoking it

`run_oneoff_job.sh` takes a **Python program via `-c`**, not a script path. `--apply` also
requires the contract token, so a stale image fails on the argument instead of silently running
the pre-#2118 copy. And the report must be **extracted**: Cloud Logging drops lines, so read the
fenced single line rather than the surrounding log.

Dry run (safe, writes nothing):

```bash
bash scripts/ops/run_oneoff_job.sh -c "import runpy,sys; sys.argv=['bf']; runpy.run_path('scripts/backfill_variant_identity_skus.py', run_name='__main__')" \
  | grep -o 'BFREPORT>>>{.*}<<<BFREPORT' \
  | sed 's/^BFREPORT>>>//; s/<<<BFREPORT$//' | python3 -m json.tool
```

Apply — **WRITES TO PROD**. Start bounded:

```bash
bash scripts/ops/run_oneoff_job.sh -c "import runpy,sys; sys.argv=['bf','--apply','--limit','50','--expect-contract','backfill-v2-identity-index']; runpy.run_path('scripts/backfill_variant_identity_skus.py', run_name='__main__')" \
  | grep -o 'BFREPORT>>>{.*}<<<BFREPORT' \
  | sed 's/^BFREPORT>>>//; s/<<<BFREPORT$//' | python3 -m json.tool
```

Resume from a previous run's `resume_after` with `--after <key>`.

### Reading the result

**Read `offers`, not `skus`, and verify in SQL.** A run whose pairs all rolled back would still
report products planned. Every written row carries
`source_system = 'variant_identity_backfill_v1'`, so the batch is countable and reversible:

```sql
SELECT count(*) offers, count(DISTINCT product_key) products
FROM catalog_offers WHERE source_system = 'variant_identity_backfill_v1';
-- and: no offer may hang off a sku_key that does not exist
SELECT count(*) FROM catalog_offers o WHERE o.source_system = 'variant_identity_backfill_v1'
  AND NOT EXISTS (SELECT 1 FROM catalog_skus s WHERE s.sku_key = o.sku_key);
```

**READ THE AUDIT ROW. THE LOG IS A CONVENIENCE.** Cloud Logging drops lines, and it can drop
the report *entirely*: the 2026-09-08 full run exited 0, printed `==> job succeeded`, and showed
neither the fenced report nor even the "Disconnected from database" line. One line cannot be
silently truncated — which is why it is one line — but it can still go missing wholesale, and an
absent report looks the same as a job that never got that far.

The recovery is `writer_audit_log`, which carries the whole counter set on every applied run,
including one that ends in an exception:

```sql
SELECT batch_id, applied_rows, skipped_rows, reasons
FROM writer_audit_log
WHERE writer_name = 'backfill_variant_identity_skus'
ORDER BY id DESC LIMIT 1;
```

`reasons` holds every counter plus a `zero_counters` list, so "measured zero" stays
distinguishable from "never measured" — `applied_rows` is `pairs x 2`. The entire report of the
final run was recovered from this row after the log lost it.

### Progress, 2026-09-08

| run | pairs | products | adoptions |
|---|---|---|---|
| pilot `--limit 50` | 78 | 50 | 0 |
| adoption window `--limit 50` | 153 | 50 | 148 |
| **remaining** | **~5,813** | **3,165** | — |

Observed write rate ~10 ms/pair (per-pair transactions, so a 120-variant product is 120 short
transactions, not one long one — `DB_STATEMENT_TIMEOUT_SECONDS=30` is not the binding limit).
39 of the remaining products carry more than 12 planned variants; the widest is 120.

**This does nothing for Flower Beauty** (no seed variants — see 2.0). It is for the ~4,467
external-referral products that do carry crawled ids.

---

## 5. Order of operations

1. Merge `feat/curated-feed-real-variants`; confirm `backend:latest` moved (2.1).
2. Flower Beauty dry run → expect `skus=234` (2.2). **Gate: do not proceed on 49.**
3. Flower Beauty apply + promote + verify (2.3–2.5).
4. Arm the gateway (3.3) and diff the env (3.3).
5. Backfill dry run, then a bounded apply (4) — independent of 1–4.
6. Re-probe the four merchants' UCP checkout to confirm `ready_for_complete` now carries a real
   variant id rather than a restated product id.
