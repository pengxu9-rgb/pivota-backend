# Reap cart proofs (two daily jobs)

The Reap cart-link lane buys through a Shopify cart permalink and refuses any row without a
**fresh storefront proof**. Two writers author those proofs, and two Cloud Run Jobs run them on a
schedule:

| Job | Lane | Writer (the only proof logic) | Writes | Valid for | Schedule (UTC) |
|---|---|---|---|---|---|
| `reap-cart-proof-enrichment` | `enrichment` | `jobs/enrichment_cart_variant_proof.py` (#2464) | `enrichment_cart_variant_proofs`, one row per (product_key, sku_key): `ok` or a recorded refusal | 72 h | 06:41 daily |
| `reap-cart-proof-mirror` | `mirror` | `scripts/backfill_shopify_variant_ids.py` (#2459) | `external_product_seeds.seed_data.snapshot.shopify_cart_proof` (sole or named variant; JSON `null` revokes), plus stamped variant ids | 7 days | 10:13 daily |

| Piece | Where |
|---|---|
| Entry point (both jobs) | `python -m jobs.reap_cart_proof_refresh mirror\|enrichment --on-crawl-egress --budget-seconds N` |
| Provisioning | `infra/gcp/setup_reap_cart_proof_jobs.sh staging\|prod <backend-tag> [--enable]` |
| Domains, mirror | every domain on `config/tierb_cart_link_merchants.json` (42), order rotated by UTC day |
| Domains, enrichment | `ENRICHMENT_DOMAINS` in the entry point: stila, jsmbeauty.sg, bluemercury, tarte, MAC (last) |
| Readers | mirror: `services.shopify_variant_identity.verified_cart_variant_id`; enrichment: `services.reap_enrichment_cart_proof.verify_enrichment_cart_proof`, behind `REAP_AGENTIC_CART_LINK_ENRICHMENT_ENABLED` |

The entry point owns no proof logic. It walks each domain page by page by calling the writer's
own `run()` on the writer's own cursor, inside a wall-clock budget, and prints one report line.

## The egress rule (read this first)

**Both jobs run only on the crawl subnet `pivota-crawl` (NAT 34.82.199.35).** Never on `worker`,
`web` or any job on the `default` subnet: their egress is 8.231.167.230, **the address payment
partners allowlist**, and NAT port exhaustion is per IP. The entry point refuses to start without
`--on-crawl-egress` (exit 2), and the setup script refuses to create a job when the subnet is
missing. If you run a writer by hand with `scripts/ops/run_oneoff_job.sh`, `SUBNET=pivota-crawl`
is **not optional** (the runner's default is `default`).

## Dark, dry run, apply

One environment variable decides whether a run writes: **`REAP_CART_PROOF_APPLY`** (1/true/yes/on
applies; anything else, unset included, is a dry run). The setup script sets it and the triggers
together:

| | `REAP_CART_PROOF_APPLY` | triggers | a scheduled run | a hand `gcloud run jobs execute` |
|---|---|---|---|---|
| dark (default) | `false` | paused | none | **a dry run**: fetches every store, writes nothing |
| `--enable` | `true` | resumed | writes | writes |

**A dark job is not inert when executed by hand.** Unlike the Tier B job (dark = contacts nobody),
a hand execution of a dark cart-proof job crawls, exactly as hard as a real run (both writers gate
the write, not the crawl). That is deliberate: it is the dry run on the real job definition. Do not
start one by hand inside another crawl's window (02:20–04:20, 05:15–06:15 UTC) or while a
scheduled run of the other cart-proof job is in flight: they share stores.

Re-running the setup script **without** `--enable` disarms both jobs.

## Procedure, per environment

Staging first. Staging has its own Postgres (10.122.0.3) and may hold no enrichment rows: the
enrichment report then shows `products: 0` per domain and nothing is fetched. The backend tag must
be an image in `us-west1-docker.pkg.dev/pivota-shared/pivota/backend` that contains this code.

```sh
ENV=staging; PROJECT=pivota-staging; TAG=<backend sha>

# 1. provision DARK (creates or updates both jobs; gate false; triggers paused)
infra/gcp/setup_reap_cart_proof_jobs.sh $ENV $TAG

# 2. DRY RUN each job on its real definition (fetches, writes nothing)
gcloud run jobs execute reap-cart-proof-enrichment --project $PROJECT --region us-west1 --wait
gcloud run jobs execute reap-cart-proof-mirror     --project $PROJECT --region us-west1 --wait

# 3. read the reports (below). Then ONE APPLY per job, still dark (the override is for this
#    execution only; the job keeps REAP_CART_PROOF_APPLY=false and its trigger stays paused)
gcloud run jobs execute reap-cart-proof-enrichment --project $PROJECT --region us-west1 --wait \
  --update-env-vars REAP_CART_PROOF_APPLY=true
gcloud run jobs execute reap-cart-proof-mirror --project $PROJECT --region us-west1 --wait \
  --update-env-vars REAP_CART_PROOF_APPLY=true

# 4. ARM (gate true on both jobs, both triggers resumed)
infra/gcp/setup_reap_cart_proof_jobs.sh $ENV $TAG --enable
```

Then prod with `ENV=prod PROJECT=pivota-prod`. **Steps 3 and 4 write to production: Peng's go
first.** Announce each prod apply to the owner of the lane it writes (the Reap rail).

`--wait` returns non-zero for any non-zero exit; the exit code alone does not say which (gcloud
reports every failure as 1). Read the report line for the job's own code.

A backend deploy **never re-images these jobs**: they run `<backend-tag>` until the setup script is
re-run with a newer one, and re-running it without `--enable` disarms, so re-arm deliberately.

## Reading the report

Each run prints a `REAP_CART_PROOF_PROGRESS {...}` line as each domain ends, then one
`REAP_CART_PROOF_REPORT {...}` line. Text-prefixed, so both land in `textPayload`:

```sh
gcloud logging read \
  'resource.type=cloud_run_job AND resource.labels.job_name=reap-cart-proof-mirror
   AND textPayload:"REAP_CART_PROOF_REPORT"' \
  --project pivota-prod --limit 1 --freshness 2d --format 'value(textPayload)'
```

If the run was killed by its task timeout there is no REPORT line; the PROGRESS lines say which
domains finished (their writes are committed).

Top level: `lane`, `mode` (`dry_run` / `apply`), `budget_s`, `elapsed_s`, `exit_code`,
`status_counts`, `domains_order`, and `domains.<domain>` = `{status, pages, elapsed_s, writer, …}`.

| `status` | meaning |
|---|---|
| `done` | walked to its end |
| `aborted_on_block` | the writer hit consecutive 429/403/5xx/transport errors; **the whole pass stopped** (the block is IP-level) |
| `crashed` | the writer raised; `error` says what; the next domain still ran |
| `cursor_stuck` | the writer returned the cursor it was given; the domain was stopped rather than looped |
| `budget_stopped` | the budget ran out while walking this domain; `last_cursor` is where it stopped |
| `not_reached` | the budget (or an abort) ended the pass before this domain |

| exit | meaning | do |
|---|---|---|
| 0 | every domain walked to its end | nothing |
| 1 | aborted on a block | suspect the crawl address; check `most_blocked_domains` / `fetches`; do not re-run straight away |
| 2 | bad arguments, no `--on-crawl-egress`, or the writer refused the domain list; nothing attempted | fix the job args or `config/tierb_cart_link_merchants.json` |
| 3 | a writer crashed (or `cursor_stuck`), or the pass itself could not start (`REAP_CART_PROOF_CRASH` on stderr, no report) | read the job log |
| 4 | the budget ran out before every domain was walked | the next day starts elsewhere (mirror rotates); if it repeats, raise the budget AND the task timeout together in the setup script |

A non-zero exit fails the execution. `--max-retries 0`: nothing re-runs automatically.

**Mirror** (`writer` is the backfill's report, summed over pages): `candidates` (the first dry run
is the measurement this lane was sized without: seeds walked per domain), `cart_proofs`
(`sole_variant` / `named_variant` proofs computed, dry run too), `rows_with_new_ids`,
`variant_ids_stamped`, `write_conflicts` (the refresh job rewrote the row under us: skipped, never
forced), `fetch_outcomes` (`dead_handle` = the seed's URL is gone; `not_json` = a challenge page or
a themed soft-404), `most_blocked_domains`. A proof the fetch no longer supports is written as JSON
`null` (revoked) by the same run.

**Enrichment** (`writer.pages[]` is the writer's own per-domain report, see the docstring of
`jobs/enrichment_cart_variant_proof.py`): `outcomes` (written: `ok`, `product_changed`,
`parent_stub`, `variant_gone`, `currency_not_market`, …), `skipped` (nothing written:
`skip_transient:*`, `skip_listing_incomplete`), `written`, `write_older_than_stored`,
`currency_read` / `currency_problems`, `price_check` + `price_drift_samples`, and per host
`hosts.<host>.source_mode` and `listing` (`complete`, `stop`). Watch `listing.stop == "page_cap"`:
a store with more than ~25,000 published products cannot be paged to its end (100 pages x 250),
so `auto` mode leaves its unlisted handles as `skip_listing_incomplete` every day; bluemercury is
the store to check. The fix is a per-handle run for that store (`--source products_js_v1`, below).

## Reading the tables

```sql
-- enrichment: outcome mix and age per store
SELECT shop_host, outcome, count(*), min(checked_at), max(checked_at)
  FROM enrichment_cart_variant_proofs GROUP BY 1, 2 ORDER BY 1, 2;

-- mirror: proofs per store and the oldest one (valid 7 days)
SELECT domain,
       count(*) FILTER (WHERE jsonb_typeof(seed_data->'snapshot'->'shopify_cart_proof') = 'object') AS proofs,
       count(*) FILTER (WHERE seed_data->'snapshot'->'shopify_cart_proof' = 'null'::jsonb) AS revoked,
       min(seed_data->'snapshot'->'shopify_cart_proof'->>'checked_at') AS oldest
  FROM external_product_seeds WHERE status = 'active' GROUP BY 1 HAVING count(*) FILTER
       (WHERE seed_data->'snapshot' ? 'shopify_cart_proof') > 0 ORDER BY 1;
```

Prod Postgres is private: run SQL through `scripts/ops/run_oneoff_job.sh -c "<program>"` (a
throwaway job on the default subnet; it does not fetch from merchants, so the default subnet is
right for it).

## Clearing a `product_changed` row (enrichment)

`product_changed` means the storefront handle now holds a **different Shopify product** than the
one the proof recorded (or a placeholder's live title no longer matches the catalog title). The row
keeps the recorded `shopify_product_id`, so the refusal is **sticky**: no later run turns it into
an `ok` for the new product. Clearing it is a person's decision:

1. Find the rows:
   ```sql
   SELECT product_key, sku_key, shop_host, handle, shopify_product_id, checked_at
     FROM enrichment_cart_variant_proofs WHERE outcome = 'product_changed' ORDER BY shop_host, product_key;
   ```
2. For each, open `https://<shop_host>/products/<handle>.js` and compare with the catalog row
   (`catalog_products.title`, the sku's `source_variant_id`). If the live product is **not** the
   catalog's product, leave the row: it is doing its job; the catalog row needs fixing instead.
3. If it **is** the same product (a re-created listing, a retitle the catalog has now followed),
   delete that one row, and only while it still says `product_changed`:
   ```sql
   DELETE FROM enrichment_cart_variant_proofs
    WHERE product_key = '<product_key>' AND sku_key = '<sku_key>' AND outcome = 'product_changed';
   ```
   The next run records the live product id afresh. Until then the sku has no proof and the
   purchase lane refuses it, which is the safe direction.

## Running a writer by hand (outside the jobs)

Only for a targeted fix; the jobs are the normal path. Always `SUBNET=pivota-crawl`, and
re-list the whole `ENV_VARS` (it replaces the runner's defaults):

```sh
# enrichment, one store, per-handle (e.g. a store whose listing hits page_cap); dry run first
SUBNET=pivota-crawl TASK_TIMEOUT=7200s \
ENV_VARS=PIVOTA_ENV=production,DB_STATEMENT_TIMEOUT_SECONDS=30,DB_COMMAND_TIMEOUT_SECONDS=600 \
  scripts/ops/run_oneoff_job.sh -m jobs.enrichment_cart_variant_proof --on-crawl-egress \
  --domain bluemercury.com --source products_js_v1

# mirror, one seed
SUBNET=pivota-crawl \
  scripts/ops/run_oneoff_job.sh scripts/backfill_shopify_variant_ids.py \
  --seed-id <epsv_...> --domain <domain> --limit 1
```

Add `--apply` to write. Do not run either inside the scheduled window of the matching job.

## Disarm

```sh
# both jobs dark again (gate false, triggers paused):
infra/gcp/setup_reap_cart_proof_jobs.sh prod <backend-tag>

# fastest, without the script (the job's env still says apply until the script is re-run):
gcloud scheduler jobs pause reap-cart-proof-enrichment-cron --location us-west1 --project pivota-prod
gcloud scheduler jobs pause reap-cart-proof-mirror-cron     --location us-west1 --project pivota-prod

# stop a run in flight (writes already committed stay; enrichment commits per store at its end):
gcloud run jobs executions list --job reap-cart-proof-mirror --region us-west1 --project pivota-prod --limit 3
gcloud run jobs executions cancel <execution> --region us-west1 --project pivota-prod
```

Disarming stops new proofs; it does not remove the ones already written. They age out on their
own (72 h enrichment, 7 days mirror). To stop purchases relying on them **now**, turn off the
reader instead: `REAP_AGENTIC_CART_LINK_ENRICHMENT_ENABLED` (enrichment rows) or
`REAP_AGENTIC_CART_LINK_ENABLED` (the whole cart-link lane) on `web`.

## Schedules and timeouts

The crawl address's daily neighbours, as their longest windows: external-seed-destination-sweep
(script 02:20–04:20 with its retry; **live prod reads 03:15, no retry, to 04:15** — the trigger
has drifted from `setup_scheduler.sh`), tierb-cart-link-eligibility 03:30–04:00, and
external-referral-refresh (live only, no script here) 05:15–06:15. The hourly purchasability sweep
(:07–:27) and the every-5/10-minute probe and drain lanes cannot be avoided by a multi-hour run,
only not coincided with.

- **enrichment**, 06:41, budget 3,600 s, task timeout 10,800 s. A normal pass is minutes
  (`/products.json` listing pages). The timeout covers the fallback where every store is read one
  `.js` per handle at 3 s: 962 handles before MAC (2,886 s, inside the budget) and MAC's ~1,870
  (5,610 s), 8,496 s in all. MAC is last so it can never starve the other four.
- **mirror**, 10:13, budget 10,800 s, task timeout 12,600 s (the budget plus one 50-seed page).
  At 3 s per seed that is ~3,600 seeds a day. **Not measured**: the first dry run's
  `writer.candidates` per domain is the measurement; if the pass is cut (exit 4), raise both
  numbers in the setup script.

`tests/test_setup_reap_cart_proof_jobs.py` re-derives the neighbour windows from their scripts (and
pins the two live-only ones) and fails if a schedule or timeout moves into one.
