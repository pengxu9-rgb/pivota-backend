# Runbook: offer market/currency drift (weekly)

Ingest stamps external-seed offers `market='US', currency='USD'` from a DEFAULT,
not from the store. `mintree.us` is an Indian storefront (INR); `upcirclebeauty.com`
is UK (GBP). This lane asks each storefront what currency it actually uses
(Shopify `/meta.json`) and reports the offers whose stamped currency contradicts it.

## The lane is two Cloud Run Jobs, and the schedule NEVER writes

| Job | Scheduler trigger | What it covers |
|---|---|---|
| `derive-offer-market-currency` | `derive-offer-market-currency-cron`, `11 9 * * 1` (Mon 09:11 UTC) | offers keyed by `source_domain`, or by the attached seed's domain |
| `audit-domainless-offer-currency` | `audit-domainless-offer-currency-cron`, `41 9 * * 1` (Mon 09:41 UTC) | the blind spot above: offers with NULL/empty `source_domain` |

Both are **read-only on the schedule**. The first Job's baked `--args` carry no
`--apply`; the second refuses `--apply` without `--confirm AUDIT_DOMAINLESS_OFFER_CURRENCY`.

**Why the weekly run must not write, stated so nobody "fixes" it by adding
`--apply` to the Job.** Base currency is not the same thing as a US buyer's
price. A Shopify Markets store that sells into the US in USD carries a non-USD
base currency and a genuinely US-converted USD price, and currency-mismatch
cannot tell that apart from a foreign price mislabelled USD. The detector is
therefore a **drift alarm** — it finds newly-arrived mispriced-ingest domains —
and a human decides which of them are real before anything is relabelled.

`--min-offers 3` and `--max-domains 25` in the Job's args are the defaults the
old workflow's dispatch form carried, pinned so the weekly report keeps the
shape operators learned to read. `--max-domains` is not a guard on this Job: it
only ever refuses an `--apply`.

## Read the weekly report

Both scripts print the entire report to stdout, so it lands in Cloud Logging.
(The GitHub workflow uploaded it as a run artifact; Cloud Run Jobs have no
artifact store, and stdout is the replacement — same as PR #1887.)

```bash
gcloud logging read 'resource.type=cloud_run_job AND resource.labels.job_name=derive-offer-market-currency' \
  --project pivota-prod --limit 500 --format='value(textPayload)' --freshness=8d
```

Swap `job_name` for `audit-domainless-offer-currency` to read the companion.
`--freshness` is required in practice, and 8d rather than 7d so a Monday report
is still in range when you read it the following Monday.

The lines that matter are the `domain USD -> CUR/MARKET (n offers)` block and
the trailing `(DRY-RUN — would relabel N offers across M domains)`.

## Apply corrections (manual, reviewed)

This replaces the old `workflow_dispatch` with `apply=true`. There is no button;
`gcloud run jobs execute --args` **overrides** the Job's baked args, so the full
argument list has to be given each time.

Re-run the dry-run first — the report you are acting on may be days old, and a
seed refresh between the two can move offers between domain groups:

```bash
gcloud run jobs execute derive-offer-market-currency --region us-west1 --project pivota-prod --wait \
  --args='-m,scripts.backfill_offer_market_currency,--min-offers,3,--max-domains,25'
```

Then apply, restricted to the domains you actually reviewed. `--only-domain` is
the safe form — it is what the old `only_domains` input built.

**ONE DOMAIN PER EXECUTION.** `--only-domain` is `action="append"`, so more than
one domain means repeating the flag — and gcloud's `--args` is an ArgList that
**refuses a repeated element**:

```
ERROR: (gcloud.run.jobs.execute) argument --args: "--only-domain" cannot be specified multiple times
```

That is a gcloud parser limit, not a script limit, and the alternate delimiter
below does not help — `--args='^:^…:--only-domain:a:--only-domain:b'` fails
identically. So run the job once per reviewed domain:

```bash
for d in mintree.us upcirclebeauty.com; do
  gcloud run jobs execute derive-offer-market-currency --region us-west1 --project pivota-prod --wait \
    --args="-m,scripts.backfill_offer_market_currency,--min-offers,3,--max-domains,25,--only-domain,$d,--apply"
done
```

Each execution re-runs the classification and applies only to `$d`, so the loop
is equivalent to the one-shot form the dispatch input used to build — just N
scans instead of one. Do NOT work around it by dropping `--only-domain`; that
applies to every classified domain, which is the blast radius this step exists
to avoid.

Three things to know before you press it:

* **`gcloud` splits `--args` on commas.** That is why the list above reads as one
  flat comma-separated string. No current flag value contains a comma; if one
  ever does, use gcloud's alternate delimiter (`--args=^:^a:b`) rather than
  quoting. `scripts/ops/run_oneoff_job.sh` already picks a delimiter that does
  not occur in the payload. The delimiter changes only the SPLIT character — it
  does not lift the no-repeated-element rule above, so it is no help for a
  repeatable flag.
* **Use `--args=`, never `--args `.** A value beginning with `-` (every one here
  starts `-m`) is read as the next flag in the space-separated form:
  `argument --args: expected one argument`.
* **Omitting `--only-domain` applies to every classified domain** in the report,
  up to `--max-domains`. That is a much bigger blast radius than the dispatch
  form's blank-means-all default made obvious.
* **`--live-only` narrows to unsuppressed rows and is almost never what you
  want.** Suppressed rows are in scope deliberately: the stores suppressed *for*
  a currency defect are exactly the ones stamped USD, and `has_us_offer` derives
  from `currency='USD'`, so a suppressed row left mislabelled re-enters the index
  as "US-buyable" the moment suppression lifts.

`gcloud run jobs execute` reports a failure with no exit code of its own, so a
non-zero exit here means "the container did not exit 0" and nothing finer. Read
the log for detail, never for the verdict.

## Backfilling `source_domain` (the companion Job's write path)

`audit-domainless-offer-currency` can fill `source_domain` on the domain-less
cohort, which moves those offers into the domain-keyed lane above. It is fill-only
and never overwrites, but it was never scheduled and should not be:

```bash
gcloud run jobs execute audit-domainless-offer-currency --region us-west1 --project pivota-prod --wait \
  --args='-m,scripts.audit_domainless_offer_currency,--apply,--confirm,AUDIT_DOMAINLESS_OFFER_CURRENCY'
```

## Why this is not a GitHub Actions workflow any more

It was `.github/workflows/derive-offer-market-currency.yml` until 2026-08-26, and
that lane cannot come back. Cloud SQL `pivota-pg` has no public IP
(`ipv4Enabled=false`, private `10.25.0.2` only), so a GitHub-hosted runner has no
route to it — there is no value the `DATABASE_URL` repo secret could hold that
would work. It only ever ran because that secret pointed at Railway's public
proxy, and Railway was decommissioned 2026-08-25. The workflow last ran 08-24,
while Railway was still up, so it was still reporting green; its next firing
(2026-08-31) would have been its first failure.

`tests/test_no_scheduled_workflow_reaches_cloud_sql.py` is the ratchet that stops
that shape from being re-added.
