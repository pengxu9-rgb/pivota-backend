#!/usr/bin/env bash
# Provision the Reap cart-proof refresh: TWO Cloud Run Jobs on the crawl subnet, each with its daily
# Cloud Scheduler trigger. Nothing else — no worker, no other job, no other trigger, no secret.
#
#   infra/gcp/setup_reap_cart_proof_jobs.sh staging|prod <backend-tag>            # dark
#   infra/gcp/setup_reap_cart_proof_jobs.sh staging|prod <backend-tag> --enable   # armed
#
#   reap-cart-proof-mirror      python -m jobs.reap_cart_proof_refresh mirror
#                               -> scripts/backfill_shopify_variant_ids.py over every Tier B domain;
#                                  writes seed_data.snapshot.shopify_cart_proof (valid 7 days)
#   reap-cart-proof-enrichment  python -m jobs.reap_cart_proof_refresh enrichment
#                               -> jobs/enrichment_cart_variant_proof.py over the five enrichment
#                                  stores; writes enrichment_cart_variant_proofs (valid 72 hours)
#
# FOR THE OPERATOR. Not run by CI, not run by deploy_backend.sh — and a backend deploy never
# re-images these jobs: they run <backend-tag> until this script is re-run with a newer one.
# Runbook: docs/runbooks/reap_cart_proofs.md. tests/test_setup_reap_cart_proof_jobs.py drives this
# script against a fake gcloud; tests/test_reap_cart_proof_refresh.py pins the "operator-run only".
#
# WHY JOBS ON pivota-crawl, AND NEVER A services/audit_scheduler.py ENTRY. Both writers fetch
# merchant storefronts. The worker's egress is the default NAT (8.231.167.230) — the address payment
# partners allowlist — and NAT port exhaustion is per-IP: ~50 requests over 37 Cloudflare-fronted
# domains in ~1 minute once tripped a cross-domain, IP-level 429 for ~15 minutes. So they leave from
# pivota-crawl (NAT 34.82.199.35), like every other storefront crawler, and the job passes
# --on-crawl-egress, which the wrapper requires (exit 2 without it).
#
# DARK BY DEFAULT, ARMED ONLY BY --enable. One flag moves all four switches together:
#   without --enable: REAP_CART_PROOF_APPLY=false on both jobs, both triggers PAUSED
#   with    --enable: REAP_CART_PROOF_APPLY=true  on both jobs, both triggers RESUMED
# Re-running WITHOUT --enable therefore disarms an armed lane — deliberately: the gate is set true
# only by someone who typed the flag on this run.
# ⚠️ UNLIKE THE TIER B JOB, A DARK JOB IS NOT INERT WHEN EXECUTED BY HAND: with the gate false the
# wrapper runs a DRY RUN — it fetches every storefront exactly as hard and writes nothing. That is
# the point: `gcloud run jobs execute reap-cart-proof-mirror --wait` on the dark job is the dry run
# on the real job definition (image, subnet, secret, args). A one-off APPLY without arming is
# `gcloud run jobs execute ... --update-env-vars REAP_CART_PROOF_APPLY=true`, which overrides that
# one execution and leaves the job dark. Both are in the runbook.
#
# THE SIDE EFFECT OF ARMING: writes only. Neither writer creates a checkout or a cart; each reads
# public products.js / products.json / meta.json. Mirror: updates seed_data.snapshot (variant ids,
# platform, the proof; JSON null REVOKES a proof whose variant vanished). Enrichment: upserts one row
# per (product_key, sku_key), 'ok' or a recorded refusal. No REAP key is needed or mounted.
#
# --max-retries 0: a failed execution is not re-run automatically. A re-run re-crawls every store
# (another burst on the crawl address); the next day's run is soon enough, or a human decides.
#
# ENV, AND NOTHING ELSE: DATABASE_URL (secret), PIVOTA_ENV, the two DB timeouts (db/database.py
# defaults both to OFF; `web` runs 30/600), the gate. Plus the plumbing every job on this address
# carries, copied from the Tier B job: PIVOTA_SERVICE_NAME / PIVOTA_COMMIT_SHA (log labels) and
# DB_POOL_MIN_SIZE=1 / DB_POOL_MAX_SIZE=2 (the pool defaults are 5/20; each writer holds one
# connection at a time, and the prod primary has 2 vCPUs). The pacing knobs are NOT set: the
# writers' defaults (mirror 1.0 s global / 3.0 s per store; enrichment 3.0 s) are what the task
# timeouts below are sized from, and a job-level override would silently invalidate that sizing.
#
# SCHEDULES AND TIMEOUTS (UTC). The crawl address's DAILY neighbours, each as its longest window
# (task timeout x (max-retries + 1)):
#   * external-seed-destination-sweep  setup_scheduler.sh: 02:20, 3600 s, mkcrawljob retries 1
#                                      -> 02:20-04:20. LIVE prod (read 2026-09-30) differs: 03:15,
#                                      3600 s, retries 0 -> 03:15-04:15. Both windows are avoided.
#   * tierb-cart-link-eligibility      03:30, 1800 s, retries 0 -> 03:30-04:00.
#   * external-referral-refresh        LIVE prod only (no script in this repo): 05:15, 3600 s,
#                                      retries 0 -> 05:15-06:15.
# The every-5 / every-10 minute lanes (store-audit probes, retailer-ingest-drain) and the hourly
# merchant-purchasability-sweep (:07-:27) cannot be avoided by a run longer than 40 minutes, only
# not coincided with: both start minutes are off the multiples of 5 and off :07. The two jobs here
# never overlap each other (they share stores: tarte, MAC, bluemercury, stila, jsm are on both lists).
#
#   enrichment  06:41 daily. Budget 3600 s, task timeout 10800 s -> worst case over by 09:41.
#     Normal cost is small: `--source auto` pages /products.json when that is fewer requests than
#     one .js per handle (MAC: ~1,870 handles vs ~10 listing pages), so a pass is minutes. The
#     timeout is sized for the FALLBACK, where /meta.json is unreadable and the writer reads one
#     .js per handle at 3.0 s: stila ~124 + jsm ~171 + bluemercury ~250 + tarte ~417 handles
#     = 962 x 3 s = 2,886 s, inside the 3,600 s budget, so MAC (last on purpose) still starts and
#     runs its ~1,870 x 3 s = 5,610 s: 8,496 s in all, plus 2,304 s for polite waits (each bounded
#     at 60 s) and 20 s request timeouts. A per-domain split was considered and not taken: it buys
#     no wall time (the stores must not be crawled in parallel from one address), and the wrapper
#     already isolates a domain's crash and commits each domain's writes when that domain ends.
#     Daily against a 72 h proof: two missed days of margin.
#   mirror      10:13 daily. Budget 10800 s, task timeout 12600 s -> worst case over by 13:43.
#     One .js per candidate seed at 3.0 s per store (domains are walked one at a time), so the
#     budget covers ~3,600 candidate fetches a day across the 42 Tier B domains. The per-domain
#     candidate counts are NOT measured yet (no read of prod was taken for this PR); the first dry
#     run reports them (`writer.candidates` per domain, `elapsed_s`). If the budget cuts the pass
#     (exit 4), the domain order rotates daily so tomorrow starts elsewhere; raise the budget here.
#     The extra 1,800 s is one 50-candidate page the budget cannot stop: 50 x (3 s + a 20 s
#     timeout) = 1,150 s at worst, since 8 consecutive block-shaped answers abort the pass.
#     Daily against a 7-day proof: six missed days of margin.
# tests/test_setup_reap_cart_proof_jobs.py re-derives these windows from the neighbours' scripts.
set -euo pipefail

ENV="${1:-}"; BACKEND_TAG="${2:-}"; FLAG="${3:-}"
case "$ENV" in
  prod) PROJECT=pivota-prod; PIVOTA_ENV=production ;;
  staging) PROJECT=pivota-staging; PIVOTA_ENV=staging ;;
  *) echo "usage: $0 staging|prod <backend-tag> [--enable]" >&2; exit 2 ;;
esac
[ -n "$BACKEND_TAG" ] || { echo "backend-tag is required" >&2; exit 2; }
case "$FLAG" in
  "") ENABLED=false ;;
  --enable) ENABLED=true ;;
  *) echo "unknown argument '$FLAG' (the only option is --enable)" >&2; exit 2 ;;
esac
[ "$#" -le 3 ] || { echo "too many arguments" >&2; exit 2; }

GCLOUD="${GCLOUD:-gcloud}"; REGION=us-west1; SHARED=pivota-shared
SUBNET=pivota-crawl
MIRROR_JOB=reap-cart-proof-mirror
MIRROR_SCHEDULE="13 10 * * *"
MIRROR_BUDGET_SECONDS=10800
MIRROR_TASK_TIMEOUT=12600s
ENRICHMENT_JOB=reap-cart-proof-enrichment
ENRICHMENT_SCHEDULE="41 6 * * *"
ENRICHMENT_BUDGET_SECONDS=3600
ENRICHMENT_TASK_TIMEOUT=10800s
# The worker identity, exactly as the Tier B job and setup_scheduler.sh's crawl jobs use it: it
# already holds Secret Manager access to DATABASE_URL and project-level run.invoker for Scheduler.
SA="sa-worker@$PROJECT.iam.gserviceaccount.com"
BACKEND_IMAGE="$REGION-docker.pkg.dev/$SHARED/pivota/backend:$BACKEND_TAG"
have(){ "$@" >/dev/null 2>&1; }
export CLOUDSDK_CORE_PROJECT="$PROJECT"

have "$GCLOUD" iam service-accounts describe "$SA" || { echo "missing $SA" >&2; exit 1; }
have "$GCLOUD" artifacts docker images describe "$BACKEND_IMAGE" \
  || { echo "missing image $BACKEND_IMAGE; build it first" >&2; exit 1; }
# The crawl subnet must exist, or --subnet would fail late — or an operator would 'fix' it by
# dropping the flag, which puts two storefront crawlers on the payment NAT.
have "$GCLOUD" compute networks subnets describe "$SUBNET" --region "$REGION" \
  || { echo "missing subnet $SUBNET; run setup_crawl_egress.sh first" >&2; exit 1; }

mkproofjob(){ # job lane budget-seconds task-timeout
  local job="$1" lane="$2" budget="$3" timeout="$4"
  # PIVOTA_ENV is required (a Job inherits nothing; see scripts/ops/run_oneoff_job.sh).
  local env_vars="PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=$job,PIVOTA_COMMIT_SHA=$BACKEND_TAG"
  env_vars="$env_vars,DB_POOL_MIN_SIZE=1,DB_POOL_MAX_SIZE=2"
  env_vars="$env_vars,DB_STATEMENT_TIMEOUT_SECONDS=30,DB_COMMAND_TIMEOUT_SECONDS=600"
  env_vars="$env_vars,REAP_CART_PROOF_APPLY=$ENABLED"
  echo "== job: $job (lane $lane, subnet $SUBNET, apply $ENABLED)"
  local verb=create; have "$GCLOUD" run jobs describe "$job" --region "$REGION" && verb=update
  # --args= in the EQUALS form: the value starts with a dash, and `--args "-m,..."` is parsed by
  # gcloud as a second flag ("argument --args: expected one argument") — #2367.
  "$GCLOUD" run jobs "$verb" "$job" --region "$REGION" --image "$BACKEND_IMAGE" --service-account "$SA" \
    --network default --subnet "$SUBNET" --vpc-egress all-traffic \
    --max-retries 0 --task-timeout "$timeout" --cpu 1 --memory 1Gi \
    --labels "env=$ENV,managed-by=infra-gcp,lane=reap-cart-proof" \
    --set-secrets "DATABASE_URL=DATABASE_URL:latest" \
    --set-env-vars "$env_vars" \
    --command python --args="-m,jobs.reap_cart_proof_refresh,$lane,--on-crawl-egress,--budget-seconds,$budget" \
    --quiet
}

mktrigger(){ # job schedule
  local job="$1" schedule="$2" trigger="$1-cron"
  echo "== trigger: $trigger ($schedule UTC)"
  local uri="https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT/jobs/$job:run"
  local verb=create; have "$GCLOUD" scheduler jobs describe "$trigger" --location "$REGION" && verb=update
  "$GCLOUD" scheduler jobs "$verb" http "$trigger" --location "$REGION" \
    --schedule="$schedule" --time-zone=Etc/UTC --uri="$uri" --http-method=POST \
    --oauth-service-account-email="$SA" --attempt-deadline=300s --quiet
}

settrigger(){ # job
  local trigger="$1-cron"
  if [ "$ENABLED" = true ]; then
    "$GCLOUD" scheduler jobs resume "$trigger" --location "$REGION" --quiet \
      || { echo "FAILED to resume $trigger" >&2; exit 1; }
  else
    "$GCLOUD" scheduler jobs pause "$trigger" --location "$REGION" --quiet \
      || { echo "FAILED to pause $trigger - it may be LIVE" >&2; exit 1; }
  fi
}

mkproofjob "$ENRICHMENT_JOB" enrichment "$ENRICHMENT_BUDGET_SECONDS" "$ENRICHMENT_TASK_TIMEOUT"
mktrigger "$ENRICHMENT_JOB" "$ENRICHMENT_SCHEDULE"
settrigger "$ENRICHMENT_JOB"
mkproofjob "$MIRROR_JOB" mirror "$MIRROR_BUDGET_SECONDS" "$MIRROR_TASK_TIMEOUT"
mktrigger "$MIRROR_JOB" "$MIRROR_SCHEDULE"
settrigger "$MIRROR_JOB"

if [ "$ENABLED" = true ]; then
  echo "ARMED: $ENRICHMENT_JOB daily at 06:41 UTC and $MIRROR_JOB daily at 10:13 UTC, writing."
else
  echo "DARK: both triggers are paused and REAP_CART_PROOF_APPLY=false on both jobs (a hand"
  echo "execution is a DRY RUN: it fetches, writes nothing). Arm with --enable."
fi
