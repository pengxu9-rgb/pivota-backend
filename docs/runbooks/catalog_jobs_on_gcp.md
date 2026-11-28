# Catalog jobs on GCP: provisioning and re-imaging

Every recurring catalog job (relation graph, price refresh, sweeps, ingest drains, promotes) runs as
a Cloud Run Job, usually with a Cloud Scheduler trigger. No step should need a laptop. Run the
commands below from Cloud Shell or any machine with `gcloud`, `python3` and `curl`.

Each prod change below (create or arm a job, re-image, IAM) needs the owner's go first.

## Lanes added here

| Job | Script | Trigger (UTC) | Default | Arm |
|---|---|---|---|---|
| `relgraph-health` | `infra/gcp/setup_relgraph_health_job.sh` | `relgraph-health-cron`, 17:05 daily (after relgraph-sync's latest possible end) | trigger paused | `--enable` |
| `brand-official-canonical-promote` | `infra/gcp/setup_brand_official_canonical_promote_job.sh` | `brand-official-canonical-promote-cron`, 08:25 daily | trigger paused, args dry run | `--enable` (dry run), `--enable --apply` (writes) |
| `pdp-live-read-promote` | `infra/gcp/setup_pdp_live_read_promote_job.sh` | none, by design | args dry run | run by hand with `--write` |

- **relgraph-health**: one line per job.
  - `infra/gcp/setup_relgraph_health_job.sh prod <gateway-sha> --enable` runs the graph's serving guard, expiry alarm and no-op detector every day after relgraph-sync.
  - It also prints the nightly readout: recent runs, review-error quarantine and review pool, as counts.
  - The readout is one log line, `jsonPayload.message="relgraph_nightly_readout"` on `cloud_run_job` `relgraph-health`.
  - It is read-only. Exit 1 is a breach and pages. A bad readout never pages.
- **brand-official-canonical-promote**: `infra/gcp/setup_brand_official_canonical_promote_job.sh prod <backend-sha> --enable [--apply]`.
  - This is the "step 2" after a curated ingest that was run by hand before.
  - The preflight refuses to provision when the pinned catalog flags differ from the live `worker`'s. Fix the pin in the script, then re-run.
- **pdp-live-read-promote**: `infra/gcp/setup_pdp_live_read_promote_job.sh prod <gateway-sha>` creates the job only.
  - Each cohort is the owner's decision. Run it with:
    `gcloud run jobs execute pdp-live-read-promote --region us-west1 --project pivota-prod --wait --args="scripts/promote-pdp-identity-live-read.js,--brand,<brand>,--limit,500,--created-by,<you>,--write"`
  - Leave out `--write` for a dry run. `--args` replaces the job's args entirely.
  - The preflight refuses on drift from the live `gateway`'s flags.

**Each setup script owns its job's whole env and secrets.** A re-run replaces both
(`--set-env-vars` / `--set-secrets`), so a value set by hand on the job is wiped. Put it in the
script.

**Disarm, fastest first.** A re-run that does not arm (relgraph-health without `--enable`; the promote
without `--apply`) pauses the trigger before any preflight, so it works even when the image is missing
or the worker's flags have drifted. Only "not found" is tolerated; any other pause error stops the
run. Without the script:

```bash
gcloud scheduler jobs pause relgraph-health-cron --location us-west1 --project pivota-prod
```

```bash
gcloud scheduler jobs pause brand-official-canonical-promote-cron --location us-west1 --project pivota-prod
```

**relgraph-health's suppressed-row threshold is at most 1% and at most 500 rows** (decided
2026-11-10). Its first hand run found 142 suppressed rows (0.28%), all with non-critical reasons,
which the old 0/0 threshold would have paged on every night. Critical reasons stay at zero tolerance,
and the expiry alarm is unchanged. Arming is still its own go.

**The brand-official promote has no `--limit`.** A row the classifier blocks stays in its
population, so a daily limit would re-score the same blocked rows forever. Size it instead: run
with `--enable` (a dry run) and read the census before adding `--apply`.

## Re-imaging pinned jobs after a merge

Deploys roll services, never jobs. Every job keeps running its last image until it is re-imaged.

```bash
GITHUB_TOKEN=$(gh auth token) infra/gcp/reimage_jobs.sh prod status
```

(`GITHUB_TOKEN` is optional. Without it, GitHub allows 60 calls an hour per IP, shared with
everything on that network. The token is passed to curl on stdin, never on argv.)

The output lists every registered job, the sha it runs, and how many commits it is behind main.

```bash
infra/gcp/reimage_jobs.sh prod backend <sha> [job ...]
```

This is a dry run: it prints current -> target for each job and changes nothing.

```bash
infra/gcp/reimage_jobs.sh prod backend <sha> --apply [job ...]
```

This rolls the named jobs. `--apply` needs job names; the dry run without names shows the plan
for every registered job. It changes only the image and `PIVOTA_COMMIT_SHA`, then re-reads each job
and fails if anything else changed. Use `gateway` instead of `backend` for gateway-image jobs.

- The script refuses in these cases:
  - the sha is not on that repo's main;
  - the image `<repo>:<sha>` is missing;
  - it is 10:15-11:45 UTC and the job is a relgraph job;
  - an execution is still running, or the executions cannot be listed;
  - the job is `retailer-ingest-drain` and it was not named with `RETAILER_INGEST_DRAIN_WRITER=1` (that job has a single writer).
- One refusal stops the run before anything changes. Any failure mid-roll (a failed update, re-read
  or verify) stops at that job, and the message lists the jobs already rolled (each one verified).
  The per-job `rolled` lines on stdout say the same.
- Each `--apply` on a job that writes prices or the graph (`relgraph-sync`, `external-referral-refresh`, the sweeps) is its own go.
- Backend images exist for every merged commit, because `deploy-prod.yml` builds `backend:<sha>`.
- Gateway images come from two repositories, and every script resolves a sha the same way
  (`infra/gcp/_gateway_image.sh`): `pivota` first, then `pivota-gateway` (full 40-hex shas only).
  `pivota` comes first because immutable tags protect only a tag's first push: every older sha lives
  only in `pivota`, so its tag is still free in `pivota-gateway`, and must not be shadowed there. Only
  "not found" counts as absent; any other lookup error refuses. `reimage_jobs.sh`'s plan line prints
  the repository a sha resolved to.
  - `pivota-gateway`: built on merge by PIVOTA-Agent's `build-gateway-image.yml` once it is armed (below). Its tags are immutable.
  - `pivota`: hand builds, `gcloud builds submit --config infra/gcp/cloudbuild.gateway.yaml --project pivota-shared --substitutions=COMMIT_SHA=<sha> .` from a PIVOTA-Agent checkout.

## Arming the gateway image build (owner, once)

1. `infra/gcp/setup_gateway_image_build_identity.sh` creates four things:
   - Artifact Registry repository `pivota-gateway`, with immutable tags and the same readers as `pivota`;
   - WIF provider `github-gateway`, pinned to PIVOTA-Agent's repo id, `refs/heads/main`, the
     `build-gateway-image.yml` workflow, and push/dispatch events;
   - service account `sa-gateway-image-build`, with `artifactregistry.writer` on `pivota-gateway` only
     (never on `pivota`, where `backend:latest` runs in prod as sa-worker);
   - the `workloadIdentityUser` binding that lets that repo's main use the account.

   It is an IAM change, so the owner runs it.
2. `gh variable set GATEWAY_IMAGE_BUILD_ENABLED --body true --repo pengxu9-rgb/PIVOTA-Agent`.
3. Push or dispatch on main, and check that `gateway:<sha>` appears in Artifact Registry.

The workflow only builds; the gateway still never deploys on merge.

## Jobs and one-offs that write content keys

Two writers build `content_key` from brand + title + barcode when their intake identity door flag is
off, instead of resolving it through `services/intake_identity.resolve_or_attach_content_identity`.
A listing written that way lands on a key no other door mints and leaves the group its barcode-mates
share (ADR-032 M11 item 2):

| Writer | Door flag |
|---|---|
| `services/catalog_sync_service.ingest_standard_products` (the fallback key) | `ENABLE_INTAKE_IDENTITY_SYNC=1` |
| `services/catalog_enrichment_agent/apply.apply_ingest_plan` (the plan key, written over the stored one) | `ENABLE_INTAKE_IDENTITY_ENRICHMENT=1` |

Web, the worker and the `retailer-ingest-drain` job run all five door flags. A one-off run through
`scripts/ops/run_oneoff_job.sh` gets none by default, so **pass the flags in `ENV_VARS` for every run of
the entrypoints below**. Pass the same five that web runs, so the run behaves as web does:

```
ENV_VARS=PIVOTA_ENV=production,DB_STATEMENT_TIMEOUT_SECONDS=30,DB_COMMAND_TIMEOUT_SECONDS=600,ENABLE_INTAKE_IDENTITY_SYNC=1,ENABLE_INTAKE_IDENTITY_ENRICHMENT=1,ENABLE_INTAKE_IDENTITY_MIRROR=1,ENABLE_INTAKE_IDENTITY_AUDIT=1,ENABLE_INTAKE_IDENTITY_BRAND_AUTHORED=1
```

(`ENV_VARS` replaces the runner's default, so it repeats the default's three values.)

Reach the enrichment apply (`ENABLE_INTAKE_IDENTITY_ENRICHMENT=1`):
`scripts/onboard_curated_brands.py`, `scripts/run_catalog_enrichment.py`,
`scripts/run_catalog_onboard_queue.py`, `scripts/audit_to_catalog_candidates.py`,
`scripts/ingest_stylekorean_brand.py`, `scripts/stylekorean_hitl.py`,
`scripts/ops/reviewed_retailer_offers.py`, `scripts/ops/kiokii_us_retailer_offers.py`,
`scripts/ops/surratt_retailer_offers.py`, `scripts/ops/amuse_us_six_expansion.py`,
and the `retailer-ingest-drain` job's `jobs/retailer_ingest_drain.py`.

Reach the sync fallback (`ENABLE_INTAKE_IDENTITY_SYNC=1`):
`scripts/backfill_shopify_storefront_handles.py`, `scripts/validate_fashion_metafield_pipeline.py`,
`scripts/catalog_backfill_verify.py`.

Folds whatever the flags say (no flag changes it): `scripts/backfill_content_key.py`.

`tests/test_content_key_fold_reach.py` keeps this list honest. It fails when an entrypoint starts or
stops reaching either writer, and when a job a committed `infra/gcp/*.sh` script defines reaches one
without its flag set to 1 on the same command. It cannot see web's or the worker's env (their deploy
scripts preserve it), or a job created by hand outside these scripts: give such a job the flags when it
is created or re-imaged.
