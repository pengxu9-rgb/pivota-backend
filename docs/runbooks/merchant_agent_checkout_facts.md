# Merchant agent checkout facts: the census and its schedule

`jobs/merchant_protocol_profile_census.py` (the crawl and the `--store` writer),
`db/merchant_agent_checkout_facts.py` (the table's reader and writer, ADR-030 D8/D11),
migration `db/migrations/294_merchant_agent_checkout_facts.sql` (the table),
`infra/gcp/setup_merchant_protocol_profile_census_job.sh` (the scheduled job and its trigger).

## 1. Why it runs on a schedule

The census asks each directory merchant for `robots.txt`, `/.well-known/ucp` and
`/.well-known/acp.json`, and with `--store` keeps one row per domain: `publishes`,
`does_not_publish` or `unreadable`. The run condition (`PARTNER_AGENT_RUN_CONDITION_RAILS`) lets a
partner's checkout agent run only on a merchant with a FRESH positive: one younger than
`MERCHANT_AGENT_CHECKOUT_FACT_MAX_AGE_DAYS` (default 30). Nothing else renews a positive. With the
flag on and no census for 30 days, every merchant is refused at once.

So the census is a Cloud Run Job, `merchant-protocol-profile-census`, fired at **11:47 UTC on the
1st and 15th of every month** (gaps of 14 to 17 days).

- **No missed run:** the oldest positive is at most 17 days old.
- **One missed run:** the facts from the run before it age out 28 to 31 days after that run, around
  the 14th to the 17th. That leaves about **12 days** for a hand execution (§5).
- **Two missed runs in a row** always age the facts out.

## 2. The job

The reviewed first run (2026-11-28, 198 domains written), unchanged:

- `python -m jobs.merchant_protocol_profile_census --from-directory --store --deadline-seconds 690`;
- the crawl subnet `pivota-crawl` with all-traffic egress, never the payment-partner NAT;
- `sa-worker`, with `DATABASE_URL` as its only secret;
- one pooled connection, `DB_AUTO_MIGRATE=false`, the DB statement and command timeouts;
- `--max-retries 0`, `--task-timeout 780s` (690 s of crawl + 90 s).

The job refuses with `UCPPROF_REFUSED store_unavailable` (exit 2) when the table cannot be read, and
more than 300 domains. A write failure exits 3. It obeys robots.txt (fail closed), sends one request a
second, and takes no lease on the shared Shopify edge pacer, so no pacer holder may run in its slot.
The setup script's test re-derives that and the other slot rules from the neighbours' own scripts.

The image is `backend:<40-hex commit>`; the setup script refuses any other tag. A backend deploy
never re-images the job.

## 3. Provisioning, arming, disarming

Each line is its own go from the owner, in the controller session. Prod setup is infra in prod; `--enable`
makes the job a standing prod writer, like a flag flip.

```bash
# dark: creates or updates the job (gate false) and the trigger (paused)
infra/gcp/setup_merchant_protocol_profile_census_job.sh prod <40-hex sha>
# armed: gate true, then the trigger resumed
infra/gcp/setup_merchant_protocol_profile_census_job.sh prod <40-hex sha> --enable
# disarm: the same as dark; an existing trigger is paused before the job is touched
infra/gcp/setup_merchant_protocol_profile_census_job.sh prod <40-hex sha>
```

The two switches (the job's `MERCHANT_PROTOCOL_PROFILE_SWEEP_ENABLED` and the trigger's state) must
agree: gate true with the trigger ENABLED, or gate false with it PAUSED. The script reads both back
and prints `ARMED` or `DARK`. If they disagree it prints `MISMATCH` and exits non-zero. Then re-run it,
without `--enable` to go dark, before anything else. A dark job exits 0, and the missed-run alert
counts that as a success, so a disagreeing pair can hide a lapse.

Order for a first rollout: the build PR merges; staging dark plus one hand execution on a few named
domains (staging has its own crawl subnet and NAT; never the full directory from staging); prod dark;
the first prod hand execution (§4); `--enable`; the missed-run alert (§6). The laptop rerun kit stays
the fallback until two scheduled runs have succeeded.

## 4. A hand execution

A hand execution in prod is a prod WRITE one-off. It needs:

- the owner's go and the controller's OK of the exact line;
- the one-off gate open (`oneoff_gate.sh` GATE_OPEN) and the one-off slot rules;
- a start at :30 past the minute.

It should never overlap a scheduled run (11:47-12:00 UTC on the 1st and 15th). The job is dark until
armed, so the line overrides the gate for that execution only:

```bash
gcloud run jobs execute merchant-protocol-profile-census --region us-west1 --project pivota-prod \
  --update-env-vars MERCHANT_PROTOCOL_PROFILE_SWEEP_ENABLED=true
```

On staging (`--project pivota-staging`), add the domains the owner named. `--domains` takes one
comma-separated value, so give `--args` another delimiter (`gcloud topic escaping`):
`--args='^|^-m|jobs.merchant_protocol_profile_census|--domains|<a>,<b>|--store'`.

## 5. Reading a run

Read the run's lines from Cloud Logging, not from the launcher's output (it can miss lines):

```bash
gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="merchant-protocol-profile-census" AND textPayload:"UCPPROF_"' \
  --project pivota-prod --freshness 1d --format='value(textPayload)' | grep -v UCPPROF_ROW
```

A good run has `UCPPROF_PLAN`, `UCPPROF_SUM`, `UCPPROF_STORE` with `"failed":0`, and `UCPPROF_DONE`.

- `"deadline":n` above 0 in `UCPPROF_SUM` means the 690 s deadline left n domains unread. They are
  stored as unreadable, which neither renews nor revokes a positive.
- Above about 250 domains the deadline starts to bite; above 300 the job refuses.

## 6. Alerts

- **A failed run** pages through the existing `prod: Cloud Run job failing` policy
  (`infra/gcp/setup_monitoring.sh`). The job's name is not ad hoc, so it is watched.
- **A run that did not succeed**, including one that never started (a paused or deleted trigger,
  broken trigger auth, a deleted job), is the morning-after policy. It is checked 00:00-11:00 UTC on
  the 2nd and the 16th. It is added at the arming step, not before: with no runs yet it would page
  twice a month.

When either fires, the next action is a gated hand execution (§4), with the owner's go, within the
~12 days before the facts age out.

## 7. Re-imaging

The job runs the image it was last given. The alerts catch dead or failed runs, not a run that
succeeds while storing wrong answers, so a re-image is reviewed like the change it carries:

```bash
infra/gcp/reimage_jobs.sh prod backend <sha>                                          # dry run
infra/gcp/reimage_jobs.sh prod backend <sha> --apply merchant-protocol-profile-census # on the owner's go
```

A re-image changes the image and `PIVOTA_COMMIT_SHA` only; the gate keeps its value.
