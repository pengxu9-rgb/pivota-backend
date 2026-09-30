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
# DARK BY DEFAULT. ARMED ONLY BY --enable, DISARMED ONLY BY --disable. One flag moves all four
# switches together:
#   --enable:  REAP_CART_PROOF_APPLY=true  on both jobs, both triggers RESUMED
#   --disable: REAP_CART_PROOF_APPLY=false on both jobs, both triggers PAUSED (a loud DISARMING line)
#   neither:   KEEP the current state (read with `describe` before anything is touched); new jobs are
#              created dark. A plain re-run (e.g. to re-image onto a newer <backend-tag>) therefore
#              never arms and never silently disarms.
# The state is read for BOTH jobs before EITHER is touched, and the script refuses to guess: a plain
# re-run exits 1 without writing when one job is armed and the other dark OR MISSING (a SPLIT; a
# plain re-run never creates a job armed), or when one job's gate and trigger disagree (e.g. a
# failed earlier run). Pass --enable or --disable then. A trigger this script CREATES is paused
# immediately (gcloud has no create --paused), before any other write.
# ORDER: disarming pauses both triggers FIRST, then updates the jobs; arming updates both jobs and
# both triggers first and resumes the triggers LAST, so a failure part-way through an arm never
# leaves a trigger firing (the next plain re-run then sees the disagreement and asks for a flag).
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
# connection at a time, and the prod primary has 2 vCPUs). The writers' own pacing knobs are NOT
# set: their defaults (mirror 1.0 s global / 3.0 s per store; enrichment 3.0 s) are what the task
# timeouts below are sized from, and a job-level override would silently invalidate that sizing.
# THE SHARED SHOPIFY-EDGE PACER (#2474) IS ON FOR BOTH JOBS, dark or armed, because a dry run fetches
# exactly as hard as an apply:
#   CRAWL_SHOPIFY_EDGE_PACER_ENABLED=true  every request to a Shopify-served host (all of ours) also
#                                          takes a slot of the aggregate budget every crawl job on
#                                          the crawl IP shares (CRAWL_SHOPIFY_EDGE_RPS, default 2/s);
#   CRAWL_SHOPIFY_EDGE_LEASE=2             a CAP on the lease size. #2474 sizes each lease by demand
#                                          (waiters + grants in the last second), so these lanes, at
#                                          ~1 request / 3 s, lease one slot at a time anyway; the cap
#                                          bounds what either lane can ever hold of the SHARED
#                                          schedule to 2 slots (~1 s at 2 req/s), even in a burst.
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
#     Its 3.0 s gap is 0.33 req/s, under the shared 2 req/s and its 0.5 req/s fail-open share; at
#     06:41-09:41 the only other edge users are the hourly purchasability sweep and the probes.
#     Normal cost is small: `--source auto` pages /products.json when that is fewer requests than
#     one .js per handle (MAC: ~1,870 handles vs ~10 listing pages), so a pass is minutes. The
#     timeout is sized for the FALLBACK, where /meta.json is unreadable and the writer reads one
#     .js per handle at 3.0 s: stila ~124 + jsm ~171 + bluemercury ~250 + tarte ~417 handles
#     = 962 x 3 s = 2,886 s, inside the 3,600 s budget, so MAC (last on purpose) still starts and
#     runs its ~1,870 x 3 s = 5,610 s: 8,496 s in all, plus 2,304 s for polite waits (each bounded
#     at 60 s) and 20 s request timeouts. (The wrapper calls the writer 250 products at a time, so
#     the budget can also stop between pages of one store; the timeout still assumes a single page
#     can hold every MAC handle, the worst case.) A per-domain split was considered and not taken:
#     it buys no wall time (the stores must not be crawled in parallel from one address), and the
#     wrapper already isolates a store's crash, commits each 250-product page as it ends, stores a
#     cursor per store, and prints its partial report on the timeout's SIGTERM.
#     Daily against a 72 h proof: two missed days of margin.
#   mirror      10:13 daily. Budget 10800 s, task timeout 15000 s -> worst case over by 14:23.
#     MEASURED (prod census, read-only, 2026-09-30, the backfill's own eligibility SQL): 2,092
#     candidate seeds over the 42 Tier B domains (34 with any; fentybeauty.com 724, paulmitchell.com
#     243, mixsoon.us 197, tonymoly.us 185, then <= 73 each; 0 hold a proof today). One .js per
#     candidate at 3.0 s per store (stores are walked one at a time) is 6,276 s, plus ~70 writer
#     calls x the 3 s inter-call gap = ~6,500 s. THE SHARED 2 req/s DOES NOT LENGTHEN IT: this lane
#     sends 0.33 req/s, under even the pacer's fail-open local rate (a quarter of 2 = 0.5 req/s), and
#     between 10:13 and 14:23 only the hourly purchasability sweep (1.5 s pacing, 0.67 req/s) shares
#     the edge budget: 1.0 req/s of 2. The 10,800 s budget is 1.66x the measured need, which also
#     absorbs an average edge wait of up to ~2 s per request. If the budget cuts the pass (exit 4),
#     the cut store resumes from its stored cursor, stalest stores first; exit 4 on consecutive days
#     means raise the budget and the task timeout here.
#     The extra 4,200 s is one 50-candidate page the budget cannot stop, now with the politeness
#     gate in front of every request: 50 x (3 s pacing + up to 60 s waiting for crawl_politeness,
#     MIRROR_MAX_POLITE_WAIT_S, + a 20 s request timeout) = 4,150 s at worst.
#     Daily against a 7-day proof: six missed days of margin.
# tests/test_setup_reap_cart_proof_jobs.py re-derives these windows from the neighbours' scripts.
set -euo pipefail

ENV="${1:-}"; BACKEND_TAG="${2:-}"; FLAG="${3:-}"
case "$ENV" in
  prod) PROJECT=pivota-prod; PIVOTA_ENV=production ;;
  staging) PROJECT=pivota-staging; PIVOTA_ENV=staging ;;
  *) echo "usage: $0 staging|prod <backend-tag> [--enable|--disable]" >&2; exit 2 ;;
esac
[ -n "$BACKEND_TAG" ] || { echo "backend-tag is required" >&2; exit 2; }
case "$FLAG" in
  "") REQUEST=keep ;;
  --enable) REQUEST=enable ;;
  --disable) REQUEST=disable ;;
  *) echo "unknown argument '$FLAG' (the options are --enable and --disable)" >&2; exit 2 ;;
esac
[ "$#" -le 3 ] || { echo "too many arguments" >&2; exit 2; }

GCLOUD="${GCLOUD:-gcloud}"; REGION=us-west1; SHARED=pivota-shared
SUBNET=pivota-crawl
MIRROR_JOB=reap-cart-proof-mirror
MIRROR_SCHEDULE="13 10 * * *"
MIRROR_BUDGET_SECONDS=10800
MIRROR_TASK_TIMEOUT=15000s
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

# ---- the current state of both jobs, read before anything is written --------------------------
job_gate(){ # job -> true | false | absent | unknown
  local json
  json=$("$GCLOUD" run jobs describe "$1" --region "$REGION" --format=json 2>/dev/null) || { echo absent; return 0; }
  printf '%s' "$json" | python3 -c '
import json, sys
try:
    doc = json.load(sys.stdin)
    env = doc["spec"]["template"]["spec"]["template"]["spec"]["containers"][0].get("env") or []
    values = [e.get("value") for e in env if e.get("name") == "REAP_CART_PROOF_APPLY"]
    print(values[0] if len(values) == 1 and values[0] in ("true", "false") else "unknown")
except Exception:
    print("unknown")
'
}
trigger_state(){ # job -> ENABLED | PAUSED | absent | <other>
  local state
  state=$("$GCLOUD" scheduler jobs describe "$1-cron" --location "$REGION" --format='value(state)' 2>/dev/null) \
    || { echo absent; return 0; }
  echo "${state:-unknown}"
}
classify(){ # gate trigger -> new | armed | dark | inconsistent
  case "$1/$2" in
    absent/absent) echo new ;;
    true/ENABLED) echo armed ;;
    false/PAUSED|false/absent) echo dark ;;
    *) echo inconsistent ;;
  esac
}

STATES=""
for job in "$ENRICHMENT_JOB" "$MIRROR_JOB"; do
  gate=$(job_gate "$job"); trig=$(trigger_state "$job"); state=$(classify "$gate" "$trig")
  echo "== current: $job gate=$gate trigger=$trig -> $state"
  STATES="$STATES $state"
done
case "$REQUEST" in
  enable) ENABLED=true ;;
  disable) ENABLED=false ;;
  keep)
    case "$STATES" in
      *inconsistent*)
        echo "REFUSING: a job's gate and trigger disagree (see above). Nothing was changed." >&2
        echo "Re-run with --enable or --disable to set both jobs explicitly." >&2
        exit 1 ;;
    esac
    # A MISSING job next to an armed one is a split too: a plain re-run never creates a job armed.
    case "$STATES" in
      *armed*dark*|*dark*armed*|*armed*new*|*new*armed*)
        echo "REFUSING: the two jobs are SPLIT (one armed, the other dark or missing). Nothing was changed." >&2
        echo "Re-run with --enable (arms both, creating a missing one armed) or --disable." >&2
        exit 1 ;;
      *armed*) ENABLED=true ;;
      *) ENABLED=false ;;
    esac
    echo "== no flag: KEEPING the current state (apply=$ENABLED)" ;;
esac
if [ "$REQUEST" = disable ]; then
  echo "!!!!!!!! DISARMING $ENRICHMENT_JOB AND $MIRROR_JOB: triggers paused, REAP_CART_PROOF_APPLY=false !!!!!!!!"
fi

mkproofjob(){ # job lane budget-seconds task-timeout
  local job="$1" lane="$2" budget="$3" timeout="$4"
  # PIVOTA_ENV is required (a Job inherits nothing; see scripts/ops/run_oneoff_job.sh).
  local env_vars="PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=$job,PIVOTA_COMMIT_SHA=$BACKEND_TAG"
  env_vars="$env_vars,DB_POOL_MIN_SIZE=1,DB_POOL_MAX_SIZE=2"
  env_vars="$env_vars,DB_STATEMENT_TIMEOUT_SECONDS=30,DB_COMMAND_TIMEOUT_SECONDS=600"
  env_vars="$env_vars,REAP_CART_PROOF_APPLY=$ENABLED"
  env_vars="$env_vars,CRAWL_SHOPIFY_EDGE_PACER_ENABLED=true,CRAWL_SHOPIFY_EDGE_LEASE=2"
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
  # `scheduler jobs create` has no --paused and a new trigger starts ENABLED: pause it before
  # anything else is written, so a trigger is never live because it was just created.
  if [ "$verb" = create ]; then
    "$GCLOUD" scheduler jobs pause "$trigger" --location "$REGION" --quiet \
      || { echo "FAILED to pause the new $trigger - it may be LIVE" >&2; exit 1; }
  fi
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

if [ "$ENABLED" = false ]; then
  # Stop the firing first: an existing trigger is paused before either job is touched.
  for job in "$ENRICHMENT_JOB" "$MIRROR_JOB"; do
    if [ "$(trigger_state "$job")" != absent ]; then settrigger "$job"; fi
  done
fi
mkproofjob "$ENRICHMENT_JOB" enrichment "$ENRICHMENT_BUDGET_SECONDS" "$ENRICHMENT_TASK_TIMEOUT"
mkproofjob "$MIRROR_JOB" mirror "$MIRROR_BUDGET_SECONDS" "$MIRROR_TASK_TIMEOUT"
mktrigger "$ENRICHMENT_JOB" "$ENRICHMENT_SCHEDULE"
mktrigger "$MIRROR_JOB" "$MIRROR_SCHEDULE"
# A trigger CREATED above starts ENABLED: set both explicitly, last.
settrigger "$ENRICHMENT_JOB"
settrigger "$MIRROR_JOB"

if [ "$ENABLED" = true ]; then
  echo "ARMED: $ENRICHMENT_JOB daily at 06:41 UTC and $MIRROR_JOB daily at 10:13 UTC, writing."
else
  echo "DARK: both triggers are paused and REAP_CART_PROOF_APPLY=false on both jobs (a hand"
  echo "execution is a DRY RUN: it fetches, writes nothing). Arm with --enable."
fi
