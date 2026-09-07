#!/usr/bin/env bash
# Replace Railway's cron + always-on workers with Cloud Run Jobs, Cloud Scheduler, and one
# single-instance worker service.
#   infra/gcp/setup_scheduler.sh staging|prod <backend-tag> <gateway-tag>
#
# THE SHAPE, and why it is not "one Scheduler job per cron":
#
#  1. FAST DRAINERS (audit/executor worker ticks, every 5-10s) CANNOT be Cloud Scheduler jobs -
#     Scheduler's minimum interval is 1 minute. They stay in-process, but on a DEDICATED service
#     pinned to exactly one instance (min=max=1), never on `web`. On Railway they ran inside the web
#     service, which is why every autoscaled Cloud Run instance would otherwise drain the same queue
#     concurrently (services/audit_scheduler.py has no cross-process lock; APScheduler's
#     max_instances=1 is per-process only).
#  2. PERIODIC BACKEND JOBS (daily_audit_check, nightly_index_health, outcome_aggregation_daily,
#     external_conversion_poll/15m, audit_health_tick/60m, catalog_onboard_queue_drain/30m,
#     store_lifecycle_reconciliation, audit_stability_canary) also live in that one worker process,
#     gated by AUDIT_WORKER_ENABLED. Moving each to Scheduler individually is a later refactor; the
#     single-worker shape reproduces Railway's semantics exactly, which is what a cutover needs.
#  3. TRUE CRON (relgraph-sync-routine, Railway schedule `37 10 * * *`) becomes a Cloud Run Job +
#     Cloud Scheduler trigger. It is a batch process that exits, which is what Jobs are for.
#  4. INVITATION WORKER (a bash `while true; sleep 60` loop on Railway) becomes a Cloud Run Job on a
#     1-minute schedule - the loop body is idempotent and the sleep IS the schedule.
#
# STAGING SAFETY: staging holds a restored PRODUCTION snapshot and still carries production
# third-party credentials. Every scheduled entity is therefore created PAUSED and the worker runs
# with AUDIT_WORKER_ENABLED=false in staging. Flipping them on in staging would execute
# production-derived queue rows against live Stripe/SendGrid/Shopify.
set -euo pipefail
ENV="${1:-}"; BACKEND_TAG="${2:-}"; GATEWAY_TAG="${3:-}"
[ -n "$ENV" ] && [ -n "$BACKEND_TAG" ] && [ -n "$GATEWAY_TAG" ] || { echo "usage: $0 staging|prod <backend-tag> <gateway-tag>" >&2; exit 2; }
case "$ENV" in
  staging) PROJECT=pivota-staging; PIVOTA_ENV=staging ;;
  prod)    PROJECT=pivota-prod;    PIVOTA_ENV=production ;;
  *) exit 2 ;;
esac

# WORKERS/PAUSED are OPT-IN, exactly as in deploy_backend.sh, and for the same reason: until the DNS
# flip, GCP prod runs against a COPY of production data with the REAL production credentials while
# Railway prod is still serving. Deriving these from $ENV is what makes `setup_scheduler.sh prod`
# quietly arm a second set of drainers over the same queue — duplicate emails, duplicate settlement
# attempts, duplicate captures. It did exactly that on 2026-08-20 (caught in under a minute, no
# executions fired) because deploy_backend.sh had been made opt-in and this script had not.
#
#   reconcile:    infra/gcp/setup_scheduler.sh prod <a> <b>
#                 Safe to re-run. Updates every job/trigger DEFINITION; changes no existing
#                 trigger's pause state and rewrites no worker env (CONFIG=preserve).
#   arm one lane: ARM=content-canonical-election-cron infra/gcp/setup_scheduler.sh prod <a> <b>
#   disarm one:   DISARM=reviews-invitation-send-cron infra/gcp/setup_scheduler.sh prod <a> <b>
#   rewrite env:  CONFIG=apply WORKERS=true infra/gcp/setup_scheduler.sh prod <a> <b>
#                 Needs the Railway-ported files, which no longer exist. See CONFIG below.
#
# WHY THE DEFAULT CHANGED. `WORKERS=false PAUSED=1` was the correct default while GCP prod ran
# against a COPY of production and Railway still served: everything had to come up inert. After
# the cutover those same defaults made the script a weapon — a run intended to add one Job would
# pause every live trigger and disarm the worker — and PAUSED=0, the apparent fix, was refused
# whenever Store Audit was armed, which in prod it always is. Between them there was no usable
# invocation, so the jobs migrated in #1892/#1894/#1895 were provisioned by hand instead. A
# provisioning script nobody can run does not prevent provisioning; it moves it somewhere with no
# review, which is the same lesson deploy_gateway.sh records.
# Was each feature flag SET BY THE CALLER, or merely defaulted? The disarm branches below turn a
# flag that is false into an active `scheduler jobs pause`, so without this distinction a run
# that never mentioned Store Audit would still disarm its four live triggers. `${F+set}` is
# empty only when F is genuinely unset.
_RELGRAPH_PUBLICATION_WORKER_EXPLICIT="${RELGRAPH_PUBLICATION_WORKER+set}"
_SEARCH_INDEX_PUBLICATION_WORKER_EXPLICIT="${SEARCH_INDEX_PUBLICATION_WORKER+set}"
_CHECKOUT_VALIDATION_WORKER_EXPLICIT="${CHECKOUT_VALIDATION_WORKER+set}"
_INSIGHT_REFRESH_WORKER_EXPLICIT="${INSIGHT_REFRESH_WORKER+set}"
_STORE_AUDIT_UCP_REPROBE_WORKER_EXPLICIT="${STORE_AUDIT_UCP_REPROBE_WORKER+set}"
_STORE_AUDIT_COMMERCE_REPROBE_WORKER_EXPLICIT="${STORE_AUDIT_COMMERCE_REPROBE_WORKER+set}"
_EXTERNAL_SEED_DESTINATION_SWEEP_EXPLICIT="${EXTERNAL_SEED_DESTINATION_SWEEP+set}"
# Did the CALLER ask for a WORKERS value, or is this the default? The summary at the end needs
# that distinction to tell "you asked and we ignored it" from "nobody mentioned it".
WORKERS_REQUESTED="${WORKERS-}"
: "${CONFIG:=preserve}"
case "$CONFIG" in apply|preserve) ;; *) echo "CONFIG must be apply or preserve (got '$CONFIG')" >&2; exit 2 ;; esac
: "${WORKERS:=false}"
: "${PAUSED:=1}"
# Trigger states to change DELIBERATELY, as comma-separated scheduler-job names. Everything not
# named here keeps whatever state it already has (see sched()). This is how you arm one lane
# without touching the other fifteen — the thing PAUSED=0 could never do.
#   ARM=content-canonical-election-cron infra/gcp/setup_scheduler.sh prod <a> <b>
: "${ARM:=}"
: "${DISARM:=}"
: "${RELGRAPH_PUBLICATION_WORKER:=false}"
: "${SEARCH_INDEX_PUBLICATION_WORKER:=false}"
: "${CHECKOUT_VALIDATION_WORKER:=false}"
: "${INSIGHT_REFRESH_WORKER:=false}"
: "${STORE_AUDIT_UCP_REPROBE_WORKER:=false}"
: "${STORE_AUDIT_UCP_REPROBE_ARMED:=false}"
: "${STORE_AUDIT_COMMERCE_REPROBE_WORKER:=false}"
: "${STORE_AUDIT_COMMERCE_REPROBE_ARMED:=false}"
# The destination sweep OBSERVES by default and RETIRES only when armed. Observing is safe
# and useful on its own — the readiness gate blocks a never-verified seed, so the sweep has to
# run before the external lane can serve at all. Retiring writes status=inactive and suppresses
# the mirrored catalog rows, which is a different blast radius and gets its own switch.
: "${EXTERNAL_SEED_DESTINATION_SWEEP:=false}"
: "${EXTERNAL_SEED_DESTINATION_SWEEP_RETIRE:=false}"
case "$WORKERS" in true|false) ;; *) echo "WORKERS must be exactly true or false (got '$WORKERS')" >&2; exit 2 ;; esac
case "$PAUSED"  in 0|1)         ;; *) echo "PAUSED must be exactly 0 or 1 (got '$PAUSED')" >&2; exit 2 ;; esac
case "$RELGRAPH_PUBLICATION_WORKER" in true|false) ;; *) echo "RELGRAPH_PUBLICATION_WORKER must be exactly true or false (got '$RELGRAPH_PUBLICATION_WORKER')" >&2; exit 2 ;; esac
case "$SEARCH_INDEX_PUBLICATION_WORKER" in true|false) ;; *) echo "SEARCH_INDEX_PUBLICATION_WORKER must be exactly true or false (got '$SEARCH_INDEX_PUBLICATION_WORKER')" >&2; exit 2 ;; esac
case "$CHECKOUT_VALIDATION_WORKER" in true|false) ;; *) echo "CHECKOUT_VALIDATION_WORKER must be exactly true or false (got '$CHECKOUT_VALIDATION_WORKER')" >&2; exit 2 ;; esac
case "$INSIGHT_REFRESH_WORKER" in true|false) ;; *) echo "INSIGHT_REFRESH_WORKER must be exactly true or false (got '$INSIGHT_REFRESH_WORKER')" >&2; exit 2 ;; esac
case "$STORE_AUDIT_UCP_REPROBE_WORKER" in true|false) ;; *) echo "STORE_AUDIT_UCP_REPROBE_WORKER must be exactly true or false (got '$STORE_AUDIT_UCP_REPROBE_WORKER')" >&2; exit 2 ;; esac
case "$STORE_AUDIT_UCP_REPROBE_ARMED" in true|false) ;; *) echo "STORE_AUDIT_UCP_REPROBE_ARMED must be exactly true or false (got '$STORE_AUDIT_UCP_REPROBE_ARMED')" >&2; exit 2 ;; esac
case "$STORE_AUDIT_COMMERCE_REPROBE_WORKER" in true|false) ;; *) echo "STORE_AUDIT_COMMERCE_REPROBE_WORKER must be exactly true or false (got '$STORE_AUDIT_COMMERCE_REPROBE_WORKER')" >&2; exit 2 ;; esac
case "$STORE_AUDIT_COMMERCE_REPROBE_ARMED" in true|false) ;; *) echo "STORE_AUDIT_COMMERCE_REPROBE_ARMED must be exactly true or false (got '$STORE_AUDIT_COMMERCE_REPROBE_ARMED')" >&2; exit 2 ;; esac
case "$EXTERNAL_SEED_DESTINATION_SWEEP" in true|false) ;; *) echo "EXTERNAL_SEED_DESTINATION_SWEEP must be exactly true or false (got '$EXTERNAL_SEED_DESTINATION_SWEEP')" >&2; exit 2 ;; esac
case "$EXTERNAL_SEED_DESTINATION_SWEEP_RETIRE" in true|false) ;; *) echo "EXTERNAL_SEED_DESTINATION_SWEEP_RETIRE must be exactly true or false (got '$EXTERNAL_SEED_DESTINATION_SWEEP_RETIRE')" >&2; exit 2 ;; esac
if [ "$EXTERNAL_SEED_DESTINATION_SWEEP_RETIRE" = true ] && [ "$EXTERNAL_SEED_DESTINATION_SWEEP" != true ]; then
  echo "EXTERNAL_SEED_DESTINATION_SWEEP_RETIRE=true requires EXTERNAL_SEED_DESTINATION_SWEEP=true" >&2; exit 2
fi
if [ "$STORE_AUDIT_UCP_REPROBE_ARMED" = true ] && [ "$STORE_AUDIT_UCP_REPROBE_WORKER" != true ]; then
  echo "STORE_AUDIT_UCP_REPROBE_ARMED=true requires STORE_AUDIT_UCP_REPROBE_WORKER=true" >&2; exit 2
fi
if [ "$STORE_AUDIT_COMMERCE_REPROBE_ARMED" = true ] && [ "$STORE_AUDIT_COMMERCE_REPROBE_WORKER" != true ]; then
  echo "STORE_AUDIT_COMMERCE_REPROBE_ARMED=true requires STORE_AUDIT_COMMERCE_REPROBE_WORKER=true" >&2; exit 2
fi
# These two guards used to refuse PAUSED=0 outright, because PAUSED=0 resumed EVERY trigger and
# would have armed lanes the operator never mentioned. That is no longer what PAUSED means: it now
# decides the state of triggers this run CREATES, and an existing trigger keeps its state unless
# named in ARM/DISARM. So the refusal is gone — and with both Store Audit workers armed in prod
# (they are), it was the reason no invocation of this script was usable at all.
#
# What the guards protected is now structural rather than conditional, so nothing is lost. Kept as
# an assertion of the invariant rather than deleted outright: if PAUSED ever goes back to meaning
# "state of every trigger", this is the line that should stop it.
if [ "$PAUSED" = 0 ] && [ -n "$ARM$DISARM" ]; then
  echo "PAUSED=0 sets the state of NEWLY CREATED triggers; ARM/DISARM set existing ones. Passing both is ambiguous - drop PAUSED=0 and name what you mean in ARM." >&2
  exit 2
fi
if [ "$STORE_AUDIT_UCP_REPROBE_WORKER" = true ]; then
  # Validate before touching even the ordinary worker/jobs below. A missing
  # Store Audit endpoint must be a no-write configuration error, not a partial
  # scheduler reconciliation followed by a failure.
  : "${STORE_AUDIT_UCP_PROBE_BACKEND_BASE_URL:?set an https backend base URL before enabling Store Audit UCP jobs}"
  [[ "$STORE_AUDIT_UCP_PROBE_BACKEND_BASE_URL" =~ ^https://[A-Za-z0-9][A-Za-z0-9.-]*$ ]] \
    || { echo "STORE_AUDIT_UCP_PROBE_BACKEND_BASE_URL must be a bare HTTPS origin (no path, query, userinfo, or port)" >&2; exit 2; }
  STORE_AUDIT_UCP_PROBE_BACKEND_BASE_URL="${STORE_AUDIT_UCP_PROBE_BACKEND_BASE_URL%/}"
fi
if [ "$STORE_AUDIT_COMMERCE_REPROBE_WORKER" = true ]; then
  : "${STORE_AUDIT_COMMERCE_PROBE_BACKEND_BASE_URL:?set an https backend base URL before enabling Store Audit commerce jobs}"
  [[ "$STORE_AUDIT_COMMERCE_PROBE_BACKEND_BASE_URL" =~ ^https://[A-Za-z0-9][A-Za-z0-9.-]*$ ]] \
    || { echo "STORE_AUDIT_COMMERCE_PROBE_BACKEND_BASE_URL must be a bare HTTPS origin (no path, query, userinfo, or port)" >&2; exit 2; }
  STORE_AUDIT_COMMERCE_PROBE_BACKEND_BASE_URL="${STORE_AUDIT_COMMERCE_PROBE_BACKEND_BASE_URL%/}"
fi
GCLOUD="${GCLOUD:-gcloud}"; REGION=us-west1; HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=infra/gcp/_serving_revision.sh
. "$HERE/_serving_revision.sh"
export CLOUDSDK_CORE_PROJECT="$PROJECT"
BACKEND_IMAGE="$REGION-docker.pkg.dev/pivota-shared/pivota/backend:$BACKEND_TAG"
GATEWAY_IMAGE="$REGION-docker.pkg.dev/pivota-shared/pivota/gateway:$GATEWAY_TAG"
BROWSER_AUDIT_IMAGE="$REGION-docker.pkg.dev/pivota-shared/pivota/store-audit-browser:$GATEWAY_TAG"
SA="sa-worker@$PROJECT.iam.gserviceaccount.com"
UCP_CRAWL_SA="sa-store-audit-ucp-crawl@$PROJECT.iam.gserviceaccount.com"
UCP_SELECTOR_SA="sa-store-audit-ucp-selector@$PROJECT.iam.gserviceaccount.com"
UCP_SCHEDULER_SA="sa-store-audit-ucp-scheduler@$PROJECT.iam.gserviceaccount.com"
COMMERCE_CRAWL_SA="sa-store-audit-commerce-crawl@$PROJECT.iam.gserviceaccount.com"
COMMERCE_SELECTOR_SA="sa-store-audit-commerce-sel@$PROJECT.iam.gserviceaccount.com"
COMMERCE_SCHEDULER_SA="sa-store-audit-commerce-sched@$PROJECT.iam.gserviceaccount.com"
have(){ "$@" >/dev/null 2>&1; }
require_service_account(){
  have "$GCLOUD" iam service-accounts describe "$1" \
    || { echo "missing $1; provision the matching Store Audit identity script for $ENV first" >&2; exit 1; }
}
if [ "$STORE_AUDIT_UCP_REPROBE_WORKER" = true ]; then
  WEB_UCP_SPEC="$("$GCLOUD" run services describe web --region "$REGION" --format=json)"
  EXPECTED_UCP_BACKEND_BASE_URL="$(printf '%s' "$WEB_UCP_SPEC" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status",{}).get("url", ""))')"
  [ "$STORE_AUDIT_UCP_PROBE_BACKEND_BASE_URL" = "$EXPECTED_UCP_BACKEND_BASE_URL" ] \
    || { echo "STORE_AUDIT_UCP_PROBE_BACKEND_BASE_URL must exactly match web service URL ($EXPECTED_UCP_BACKEND_BASE_URL)" >&2; exit 2; }
  # The service template can describe a 0%-traffic candidate. The service URL
  # used by the Job instead reaches the single 100%-traffic revision (which
  # normally carries its c-<sha> tag: deploy_backend.sh promotes the tagged
  # candidate). Refuse a split/candidate state rather than declaring receipt
  # ready based on code that is not actually serving the URL.
  WEB_ACTIVE_REVISION="$(serving_revision web || true)"
  [ -n "$WEB_ACTIVE_REVISION" ] \
    || { echo "web has no single 100%-traffic revision (traffic is split, or a rollout is half-finished) - resolve that before Store Audit Jobs are created" >&2; exit 2; }
  WEB_ACTIVE_SPEC="$("$GCLOUD" run revisions describe "$WEB_ACTIVE_REVISION" --region "$REGION" --format=json)"
  # Refuse to create or arm a crawler against a revision where Cloud Run would
  # accept OIDC but the app would still return a disabled-receipt 404. This
  # reads only the active revision's configuration; it never renders a secret.
  WEB_UCP_RECEIPT_READY="$(printf '%s' "$WEB_ACTIVE_SPEC" | python3 -c '
import json,sys
o=json.load(sys.stdin)
env=o.get("spec",{}).get("containers",[{}])[0].get("env",[])
by_name={e.get("name"):e for e in env}
flag=by_name.get("STORE_AUDIT_UCP_PROBE_RECEIPT_ENABLED",{}).get("value") == "true"
secret=by_name.get("STORE_AUDIT_UCP_PROBE_INTERNAL_KEY",{}).get("valueFrom",{}).get("secretKeyRef",{}).get("name") == "STORE_AUDIT_UCP_PROBE_INTERNAL_KEY"
print("true" if flag and secret else "false")
')"
  [ "$WEB_UCP_RECEIPT_READY" = true ] \
    || { echo "web must mount STORE_AUDIT_UCP_PROBE_INTERNAL_KEY and set STORE_AUDIT_UCP_PROBE_RECEIPT_ENABLED=true before Store Audit Jobs are created" >&2; exit 2; }
  require_service_account "$UCP_CRAWL_SA"
  require_service_account "$UCP_SELECTOR_SA"
  require_service_account "$UCP_SCHEDULER_SA"
fi
if [ "$STORE_AUDIT_COMMERCE_REPROBE_WORKER" = true ]; then
  WEB_COMMERCE_SPEC="$("$GCLOUD" run services describe web --region "$REGION" --format=json)"
  EXPECTED_COMMERCE_BACKEND_BASE_URL="$(printf '%s' "$WEB_COMMERCE_SPEC" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status",{}).get("url", ""))')"
  [ "$STORE_AUDIT_COMMERCE_PROBE_BACKEND_BASE_URL" = "$EXPECTED_COMMERCE_BACKEND_BASE_URL" ] \
    || { echo "STORE_AUDIT_COMMERCE_PROBE_BACKEND_BASE_URL must exactly match web service URL ($EXPECTED_COMMERCE_BACKEND_BASE_URL)" >&2; exit 2; }
  WEB_COMMERCE_ACTIVE_REVISION="$(serving_revision web || true)"
  [ -n "$WEB_COMMERCE_ACTIVE_REVISION" ] \
    || { echo "web has no single 100%-traffic revision (traffic is split, or a rollout is half-finished) - resolve that before Store Audit commerce Jobs are created" >&2; exit 2; }
  WEB_COMMERCE_ACTIVE_SPEC="$("$GCLOUD" run revisions describe "$WEB_COMMERCE_ACTIVE_REVISION" --region "$REGION" --format=json)"
  WEB_COMMERCE_RECEIPT_READY="$(printf '%s' "$WEB_COMMERCE_ACTIVE_SPEC" | python3 -c '
import json,sys
o=json.load(sys.stdin)
env=o.get("spec",{}).get("containers",[{}])[0].get("env",[])
by_name={e.get("name"):e for e in env}
flag=by_name.get("STORE_AUDIT_COMMERCE_PROBE_RECEIPT_ENABLED",{}).get("value") == "true"
secret=by_name.get("STORE_AUDIT_COMMERCE_PROBE_INTERNAL_KEY",{}).get("valueFrom",{}).get("secretKeyRef",{}).get("name") == "STORE_AUDIT_COMMERCE_PROBE_INTERNAL_KEY"
print("true" if flag and secret else "false")
')"
  [ "$WEB_COMMERCE_RECEIPT_READY" = true ] \
    || { echo "web must mount STORE_AUDIT_COMMERCE_PROBE_INTERNAL_KEY and set STORE_AUDIT_COMMERCE_PROBE_RECEIPT_ENABLED=true before Store Audit commerce Jobs are created" >&2; exit 2; }
  require_service_account "$COMMERCE_CRAWL_SA"
  require_service_account "$COMMERCE_SELECTOR_SA"
  require_service_account "$COMMERCE_SCHEDULER_SA"
fi

# ---------------------------------------------------------------- 1. the single-instance worker
# THE WORKER'S SHAPE IS NOT DEFINED HERE ANY MORE. It lives in deploy_worker.sh, which is also what
# .github/workflows/deploy-prod.yml calls on every push to main, so the service that CI rolls and
# the service this script reconciles are the same service by construction rather than by two
# people remembering to edit two files.
#
# Why that mattered enough to move: rolling the worker used to mean running THIS script, which
# also reconciles ~20 Cloud Run Jobs and every Scheduler trigger and demands a <gateway-tag>. So
# nobody ran it to ship an image - they hand-copied these eight lines instead (2026-09-02
# worker-00016-rmv, 2026-09-05 worker-00018-rm6), and in between the worker sat 94 commits behind
# `web` for a month with no alarm able to see it.
#
# CONFIG, WORKERS and the env-file merge all still behave exactly as they did; they are that
# script's arguments now. It additionally verifies /__scheduler_health from inside the VPC before
# calling the roll a success, which this block never did.
#
# ENV_FILE / SECRETS_FILE stay defined HERE even though the worker no longer reads them from this
# file: the Cloud Run Jobs further down (commerce-index-checkout-validation, -insight-refresh,
# reviews-invitation-send) each `paste` $SECRETS_FILE into their own --set-secrets. Deleting these
# two lines with the worker block would take those jobs out with it, under `set -u`, at the first
# mkjob that touches one.
ENV_FILE="$HERE/env.$ENV.yaml"; SECRETS_FILE="$HERE/secrets.$ENV.list"
# CONFIG=preserve (the default) needs NEITHER file. See the CONFIG note in the header: both are
# generated by port_railway_env.py from `railway variables --json`, Railway was decommissioned
# 2026-08-22, and on a fresh checkout they cannot be regenerated - so this hard exit was the
# outcome of EVERY run of this script, in both environments, before the first job was touched.
if [ "$CONFIG" = apply ]; then
  [ -f "$ENV_FILE" ] && [ -f "$SECRETS_FILE" ] || { echo "CONFIG=apply needs $ENV_FILE / $SECRETS_FILE - run port_railway_env.py first (it reads Railway, which is retired)" >&2; exit 1; }
fi

echo "== worker service (delegating to deploy_worker.sh)"
# WORKERS is passed ONLY under `apply`, and that is the contract, not a shortcut. It reaches the
# service exclusively through the env file, so it is inert under `preserve` - and deploy_worker.sh
# REFUSES an explicit WORKERS under `preserve` rather than accepting one it would ignore. Since
# this script defaults WORKERS to false unconditionally, forwarding it always would make every
# ordinary `setup_scheduler.sh prod <a> <b>` reconcile run die on that guard.
#
# `env -u WORKERS` IS LOAD-BEARING, and omitting it broke a documented command. An operator who
# writes `WORKERS=true ... setup_scheduler.sh prod <a> <b>` puts WORKERS in THIS script's own
# environment, so the child inherits it no matter which branch below runs - simply not naming it
# on the preserve branch does nothing. The arming command in infra/gcp/README.md is exactly that
# shape, and with CONFIG at its default `preserve` it would hit deploy_worker.sh's guard, exit 2,
# and `set -e` would kill this script before ONE Cloud Run Job or Scheduler trigger was
# reconciled. Unsetting it in the child's environment is what actually restores "inert under
# preserve". Caught in review, 2026-09-05.
if [ "$CONFIG" = apply ]; then
  env CONFIG=apply WORKERS="$WORKERS" GCLOUD="$GCLOUD" "$HERE/deploy_worker.sh" "$ENV" "$BACKEND_TAG"
else
  env -u WORKERS CONFIG=preserve GCLOUD="$GCLOUD" "$HERE/deploy_worker.sh" "$ENV" "$BACKEND_TAG"
fi

# ---------------------------------------------------------------- 2. Cloud Run Jobs
mkjob(){ # name image service-account command...
  local name="$1" image="$2" service_account="$3"; shift 3
  local verb=create; have "$GCLOUD" run jobs describe "$name" --region "$REGION" && verb=update
  "$GCLOUD" run jobs "$verb" "$name" --region "$REGION" --image "$image" --service-account "$service_account" \
    --network default --subnet default --vpc-egress all-traffic \
    --max-retries 1 --task-timeout 3600s --cpu 1 --memory 2Gi \
    --labels "env=$ENV,managed-by=infra-gcp" --quiet "$@"
}
mkcrawljob(){ # name image service-account command... -- the only crawl egress workload
  local name="$1" image="$2" service_account="$3"; shift 3
  local verb=create; have "$GCLOUD" run jobs describe "$name" --region "$REGION" && verb=update
  "$GCLOUD" run jobs "$verb" "$name" --region "$REGION" --image "$image" --service-account "$service_account" \
    --network default --subnet pivota-crawl --vpc-egress all-traffic \
    --max-retries 1 --task-timeout 300s --cpu 1 --memory 1Gi \
    --labels "env=$ENV,managed-by=infra-gcp,lane=store-audit-crawl" --quiet "$@"
}
mkbrowserauditjob(){ # Browser is intentionally isolated to the crawl subnet.
  local name="$1" image="$2" service_account="$3"; shift 3
  local verb=create; have "$GCLOUD" run jobs describe "$name" --region "$REGION" && verb=update
  "$GCLOUD" run jobs "$verb" "$name" --region "$REGION" --image "$image" --service-account "$service_account" \
    --network default --subnet pivota-crawl --vpc-egress all-traffic \
    --max-retries 1 --task-timeout 300s --cpu 2 --memory 2Gi \
    --labels "env=$ENV,managed-by=infra-gcp,lane=store-audit-crawl" --quiet "$@"
}
echo "== job: relgraph-sync (Railway cron 37 10 * * *)"
mkjob relgraph-sync "$GATEWAY_IMAGE" "$SA" \
  --set-secrets "DATABASE_URL=DATABASE_URL_NOVERIFY:latest,PCI_KB_DATABASE_URL=PCI_KB_DATABASE_URL_NOVERIFY:latest" \
  --set-env-vars "PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=relgraph-sync,PIVOTA_COMMIT_SHA=$GATEWAY_TAG,DB_POOL_MAX=3,PCI_KB_DB_POOL_MAX=1,INGREDIENT_REFERENCE_DB_POOL_MAX=1,INGREDIENT_SIGNAL_DB_POOL_MAX=1" \
  --command npm --args "run,relgraph:sync-routine:cron"

echo "== job: external-seed-sentinel-nongrowth (GH Actions cron 23 10 * * *)"
# ADR-009 data-side ratchet, migrated from PIVOTA-Agent's scheduled GitHub workflow
# when the Railway DATABASE_URL secret it ran on was decommissioned (2026-08-25).
# Read-only audit; exits non-zero on any enforce-lane breach, which fails the
# execution and pages via the "prod: Cloud Run job failing" alert policy — the
# replacement for the GH scheduled-run failure email. The script reads its
# watermarks from tests/fixtures/external_seed_sentinel_watermarks.json INSIDE
# the image, so GATEWAY_TAG must be at or after PIVOTA-Agent #2110 (which
# re-included that fixture in .dockerignore) — and a watermark ratchet only
# takes effect once a gateway image carrying it is rolled onto this job.
mkjob external-seed-sentinel-nongrowth "$GATEWAY_IMAGE" "$SA" \
  --set-secrets "DATABASE_URL=DATABASE_URL_NOVERIFY:latest" \
  --set-env-vars "PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=external-seed-sentinel-nongrowth,PIVOTA_COMMIT_SHA=$GATEWAY_TAG" \
  --task-timeout 600s \
  --command node --args "scripts/audit-external-seed-sentinel-nongrowth.cjs"

echo "== job: pdp-identity-graph-backfill (GH Actions cron 40 3 * * 1, weekly catch-up)"
# Weekly identity-listing catch-up for external seeds, migrated from the same
# decommissioned GH lane. Always --only-uncovered: it mints listings ONLY for
# seeds with no listing yet, so an unattended run structurally cannot rewrite an
# existing row (that is what made the GH schedule safe, and it carries over
# unchanged). The GH artifact upload is replaced by the result JSON on stdout,
# which lands in Cloud Logging.
mkjob pdp-identity-graph-backfill "$GATEWAY_IMAGE" "$SA" \
  --set-secrets "DATABASE_URL=DATABASE_URL_NOVERIFY:latest" \
  --set-env-vars "PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=pdp-identity-graph-backfill,PIVOTA_COMMIT_SHA=$GATEWAY_TAG,DB_POOL_MAX=3" \
  --command node --args "scripts/backfill-pdp-identity-graph.js,--limit,2000,--only-uncovered"

echo "== job: commerce-index-relgraph (v2 targeted graph publication)"
# This worker is independently opt-in. It is safe to create while inert, but it
# must not consume queue rows until migration 194 is applied, v2 fact emission is
# enabled, and the affected-product bridge has passed a canary. The script itself
# refuses to claim work without COMMERCE_INDEX_RELGRAPH_APPLY=true.
mkjob commerce-index-relgraph "$GATEWAY_IMAGE" "$SA" \
  --set-secrets "DATABASE_URL=DATABASE_URL_NOVERIFY:latest,PCI_KB_DATABASE_URL=PCI_KB_DATABASE_URL_NOVERIFY:latest" \
  --set-env-vars "PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=commerce-index-relgraph,PIVOTA_COMMIT_SHA=$GATEWAY_TAG,COMMERCE_INDEX_RELGRAPH_APPLY=$RELGRAPH_PUBLICATION_WORKER,DB_POOL_MAX=3,PCI_KB_DB_POOL_MAX=1" \
  --task-timeout 1800s \
  --command node --args "scripts/drain-commerce-index-relgraph.js,--worker-id,cloud-run-relgraph"

echo "== job: commerce-index-search-index (v2 targeted OpenSearch publication)"
# Requires CATALOG_SERVING_INDEX_BASE_URL and CATALOG_SERVING_INDEX_API_KEY to
# be provisioned on the job before SEARCH_INDEX_PUBLICATION_WORKER is enabled.
# The worker refuses to claim jobs if either the explicit apply gate or index
# configuration is absent, so an incomplete setup cannot drain the queue.
#
# THE KEY IS MOUNTED ONLY WHEN THE LANE IS BEING ARMED, because the comment above
# is otherwise self-contradicting: it says the secret may not be provisioned yet,
# and then mounted it unconditionally. Cloud Run resolves secret refs at
# create/update time, so on a project where CATALOG_SERVING_INDEX_API_KEY does
# not exist this line does not merely leave the lane inert — it makes
# `gcloud run jobs update` fail, and with `set -e` that aborts the WHOLE
# reconcile. Confirmed in prod 2026-08-26: the secret does not exist, the live
# job mounts it anyway, and the run died here before reaching the scheduler
# section. (GCP reports an absent secret as "Permission denied", which sent the
# first diagnosis after a nonexistent IAM grant.)
#
# Mounting it only under the flag matches what the Store Audit blocks below
# already do — refuse to wire a lane until its dedicated secret exists, rather
# than mount a name and hope. The lane is inert without the flag either way.
SEARCH_INDEX_SECRETS="DATABASE_URL=DATABASE_URL_NOVERIFY:latest,PCI_KB_DATABASE_URL=PCI_KB_DATABASE_URL_NOVERIFY:latest"
if [ "$SEARCH_INDEX_PUBLICATION_WORKER" = true ]; then
  SEARCH_INDEX_SECRETS="$SEARCH_INDEX_SECRETS,CATALOG_SERVING_INDEX_API_KEY=CATALOG_SERVING_INDEX_API_KEY:latest"
else
  echo "   (CATALOG_SERVING_INDEX_API_KEY not mounted; SEARCH_INDEX_PUBLICATION_WORKER=false)"
fi
mkjob commerce-index-search-index "$GATEWAY_IMAGE" "$SA" \
  --set-secrets "$SEARCH_INDEX_SECRETS" \
  --set-env-vars "PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=commerce-index-search-index,PIVOTA_COMMIT_SHA=$GATEWAY_TAG,COMMERCE_INDEX_SEARCH_PUBLICATION_APPLY=$SEARCH_INDEX_PUBLICATION_WORKER,DB_POOL_MAX=3,PCI_KB_DB_POOL_MAX=1" \
  --task-timeout 900s \
  --command node --args "scripts/drain-commerce-index-search-index.js,--worker-id,cloud-run-search-index"

echo "== job: commerce-index-checkout-validation (v2 quote-first marker)"
mkjob commerce-index-checkout-validation "$BACKEND_IMAGE" "$SA" \
  --set-secrets "DATABASE_URL=DATABASE_URL:latest,$(paste -sd, "$SECRETS_FILE")" \
  --set-env-vars "PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=commerce-index-checkout-validation,PIVOTA_COMMIT_SHA=$BACKEND_TAG,COMMERCE_INDEX_CHECKOUT_VALIDATION_ENABLED=$CHECKOUT_VALIDATION_WORKER,DB_POOL_MIN_SIZE=1,DB_POOL_MAX_SIZE=3" \
  --task-timeout 300s \
  --command python --args "scripts/drain_commerce_index_checkout_validation_jobs.py"

echo "== job: commerce-index-insight-refresh (v2 reviewed insights requests)"
mkjob commerce-index-insight-refresh "$BACKEND_IMAGE" "$SA" \
  --set-secrets "DATABASE_URL=DATABASE_URL:latest,$(paste -sd, "$SECRETS_FILE")" \
  --set-env-vars "PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=commerce-index-insight-refresh,PIVOTA_COMMIT_SHA=$BACKEND_TAG,COMMERCE_INDEX_INSIGHT_REFRESH_ENABLED=$INSIGHT_REFRESH_WORKER,DB_POOL_MIN_SIZE=1,DB_POOL_MAX_SIZE=3" \
  --task-timeout 300s \
  --command python --args "scripts/drain_commerce_index_insight_refresh_jobs.py"

echo "== job: reviews-invitation-send (was a 60s bash loop)"
# Run the one-shot processor DIRECTLY, not scripts/run_reviews_invitation_send_loop.sh.
# That wrapper is `while true; do ...; sleep $SLEEP_SECONDS; done` and reads no RUN_ONCE (grep it) -
# it never exits. As a Cloud Run Job on a * * * * * trigger it would run to the 3600s task timeout
# while Scheduler starts another every minute, and Jobs allow concurrent executions. Each holds an
# asyncpg pool (db/database.py defaults 5..20), so within ~4-14 minutes of un-pausing at cutover the
# connection headroom is gone and every service starts throwing TooManyConnectionsError.
# The wrapper's own loop body is exactly this script, so a per-minute Scheduler tick IS the loop.
# DB_POOL_* are pinned because a Job inherits no pool sizing from the deploy scripts.
mkjob reviews-invitation-send "$BACKEND_IMAGE" "$SA" \
  --set-secrets "DATABASE_URL=DATABASE_URL:latest,$(paste -sd, "$SECRETS_FILE")" \
  --set-env-vars "PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=reviews-invitation-send,PIVOTA_COMMIT_SHA=$BACKEND_TAG,DB_POOL_MIN_SIZE=1,DB_POOL_MAX_SIZE=4" \
  --task-timeout 300s \
  --command python --args "scripts/process_due_reviews_invitation_send_jobs.py"

echo "== job: content-canonical-election (GH Actions cron 52 */6 * * *)"
# Migrated from .github/workflows/content-canonical-election.yml, deleted in the
# same commit. Not a preference: Cloud SQL `pivota-pg` has NO public IP
# (ipv4Enabled=false, private 10.25.0.2 only), so a GitHub-hosted runner cannot
# reach the database from anywhere on the internet. The workflow held a
# DATABASE_URL repo secret pointing at the Railway public proxy; Railway was
# decommissioned 2026-08-25 and every scheduled run since has died in
# `database.connect()` with ConnectionResetError. There is no secret value that
# fixes it — the lane has to run inside the VPC.
#
# NOT a `mkcrawljob`: it fetches ONE URL, our own agent.pivota.cc sitemap, which
# is public-internet egress out of the `default` subnet through pivota-nat. The
# crawl subnet's reserved IP is for merchant storefronts and this is not one.
#
# `--apply` unconditionally, matching the workflow it replaces: a scheduled
# election auto-applied there too (the dispatch-only checkbox was for manual
# runs, and manual runs are now `gcloud run jobs execute`). --seed-from-sitemap
# every time is also deliberate and NOT a seed-once flag — stored winners
# outrank incumbency, so it changes nothing for settled content_keys and only
# supplies the tiebreak for groups being elected for the first time.
#
# The workflow also set DB_POOL_ACQUIRE_TIMEOUT_SECONDS=45. Deliberately NOT
# carried over: it existed because a GitHub runner reached Postgres over the
# public proxy, where the TLS+auth handshake regularly exceeded the 5s default.
# In-VPC to a private IP that reason is gone, and keeping it would only delay
# the failure signal on a genuinely wedged pool.
#
# INDEX_ELIGIBLE_SITEMAP=1 IS carried, and is the one env var here that is not
# plumbing. `candidates_query(widen=None)` defaults to `sitemap_widen_enabled()`,
# which reads that flag — so it decides WHICH ROWS ARE ELECTABLE. Live prod `web`
# runs it =1, and without it this job would elect against a NARROWER candidate
# set than the feed and get_pdp_v2 read with. The deleted workflow had the same
# gap and it cost nothing (candidates_query returns 4,380 rows either way today,
# and the skew direction is safe: writer's set is a subset of the reader's, so it
# under-elects rather than naming a sig the reader rejects). But this block is
# authoring the writer's environment from scratch, which is the moment to make
# the two sides agree explicitly rather than by coincidence.
#
# NOT carried, and worth naming rather than leaving silent: the workflow's
# `concurrency: {group: content-canonical-election, cancel-in-progress: false}`.
# Cloud Run Jobs allow concurrent executions and Cloud Scheduler does not
# serialize, so there is no equivalent to set. Schedule-vs-schedule overlap is
# structurally impossible (6h interval, 900s timeout). The residual risk is a
# HAND-RUN `gcloud run jobs execute` landing on top of a scheduled run:
# plan_elections snapshots its candidates OUTSIDE the write transaction, so the
# later writer would commit reasoning built on stale state. Take the runbook's
# dry-run first, and do not hand-run one within the timeout of a :52 tick.
mkjob content-canonical-election "$BACKEND_IMAGE" "$SA" \
  --set-secrets "DATABASE_URL=DATABASE_URL:latest" \
  --set-env-vars "PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=content-canonical-election,PIVOTA_COMMIT_SHA=$BACKEND_TAG,DB_POOL_MIN_SIZE=1,DB_POOL_MAX_SIZE=2,INDEX_ELIGIBLE_SITEMAP=1" \
  --task-timeout 900s \
  --command python \
  --args "scripts/elect_content_canonicals.py,--apply,--seed-from-sitemap,https://agent.pivota.cc/sitemap-products.xml"

echo "== job: derive-offer-market-currency (GH Actions cron 11 9 * * 1)"
# Migrated from .github/workflows/derive-offer-market-currency.yml, deleted in
# the same commit. Same forced move as content-canonical-election above: Cloud
# SQL `pivota-pg` has NO public IP (ipv4Enabled=false, private 10.25.0.2 only),
# so a GitHub-hosted runner has no route to it. The workflow's DATABASE_URL repo
# secret held Railway's PUBLIC proxy URL, which is the only reason it ever
# worked; Railway was decommissioned 2026-08-25. This lane last ran 08-24 while
# Railway was still up, so unlike the reaper it is still GREEN and LATENT — it
# would have died on its next firing, Monday 2026-08-31.
#
# NO `--apply`, and that omission is the whole safety property of this lane.
# `base currency != a US Markets buyer's price`: currency-mismatch cannot tell a
# genuinely US-converted USD price apart from a mislabelled foreign one, so the
# WEEKLY run only ever detected drift (new mispriced-ingest domains) and printed
# the by-domain report — the workflow's scheduled path passed no --apply either,
# and writes required a human dispatching with apply=true after reading it.
# Baking --apply into these args would convert a report into an unattended
# relabel of live offer currencies. The manual apply path is now
#   gcloud run jobs execute derive-offer-market-currency --region "$REGION" \
#     --project "$PROJECT" --wait --args='-m,scripts.backfill_offer_market_currency,...,--apply'
# which OVERRIDES these args; see docs/runbooks/derive_offer_market_currency.md
# for the reviewed-subset form (`--only-domain`) the workflow's `only_domains`
# input used to build.
#
# --min-offers 3 / --max-domains 25 are the workflow input DEFAULTS, pinned here
# so the scheduled report keeps the shape the operator learned to read. They are
# not safety limits on this Job (--max-domains only ever refuses an --apply);
# they are what decides which domains appear in the report at all.
#
# `mkjob`, NOT `mkcrawljob`, and this one is a judgement rather than a rule.
# scripts/backfill_offer_market_currency.py DOES fetch merchant storefronts
# (`https://{domain}/meta.json` via services/storefront_currency.py), which is
# the trigger the external-seed sweep below cites for using the reserved
# pivota-crawl egress IP. Two reasons it stays on `default`/pivota-nat anyway:
# a migration should preserve behaviour, and a GitHub runner was never on the
# reserved IP either — so `default` is at worst neutral and probably better
# (one stable NAT address instead of shared GitHub ranges); and the crawl IP is
# a shared reputational resource that the destination sweep and the Store Audit
# probes already depend on, so adding an 8-way-concurrent /meta.json fetcher to
# it is its own measured decision, not a side effect of this migration. The
# failure mode if some hosts do challenge us is a SHORTER report (an
# unresolvable storefront is counted and left alone), never a wrong write.
#
# The workflow uploaded reports/workflow_ops/.../run.log as an artifact. Cloud
# Run Jobs have no artifact store; both scripts already print the whole report
# to stdout, so it lands in Cloud Logging instead — see the runbook for the read.
# `--args=` NOT `--args `, here and at audit-domainless-offer-currency and the destination
# sweep. The value starts with `-m`, and in the space-separated form gcloud's parser reads that
# as the next FLAG and fails with "argument --args: expected one argument". Confirmed against
# prod 2026-08-26: the reconcile aborted here after already updating ten jobs, so those three
# job definitions were simply not updatable by this script. Any --args value beginning with `-`
# needs the `=` form; tests/test_setup_scheduler_is_safe_to_rerun.py refuses the other spelling.
#
# NB a comment must never sit between the backslash-continued lines below: it silently ends the
# command, and `bash -n` still passes. That is how the first draft of this fix truncated the
# invocation and reddened ten unrelated tests.
mkjob derive-offer-market-currency "$BACKEND_IMAGE" "$SA" \
  --set-secrets "DATABASE_URL=DATABASE_URL:latest" \
  --set-env-vars "PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=derive-offer-market-currency,PIVOTA_COMMIT_SHA=$BACKEND_TAG,DB_POOL_MIN_SIZE=1,DB_POOL_MAX_SIZE=2" \
  --task-timeout 1800s \
  --command python \
  --args="-m,scripts.backfill_offer_market_currency,--min-offers,3,--max-domains,25"

echo "== job: audit-domainless-offer-currency (companion report of the same GH lane)"
# The SECOND step of the same deleted workflow, and a separate Job because a
# Cloud Run Job runs one command. Dropping it would have silently deleted half
# the weekly signal: the domain-keyed scan above cannot see offers with
# NULL/empty source_domain (the external-seed mirror lane never wrote it), and
# this companion derives each such offer's storefront from seed provenance and
# reports stamped-vs-actual currency for exactly that blind spot.
#
# ALWAYS a dry-run, structurally: scripts/audit_domainless_offer_currency.py
# refuses --apply without --confirm AUDIT_DOMAINLESS_OFFER_CURRENCY, and
# backfilling source_domain is a reviewed manual step that was never scheduled.
#
# Sequencing. In the workflow this step ran only if the first one succeeded
# (plain step order, no `if: always()`), because a failed checkout / pip install
# / DATABASE_URL guard would make it fail identically and add noise. None of
# those three prerequisites exist here — the image is prebuilt and a secret that
# will not mount stops the Job before the container starts — so two independent
# Jobs lose nothing that reasoning was protecting. They are staggered rather
# than co-scheduled (see the trigger below) so they cannot hit the same
# storefronts concurrently from the same egress address.
#
# It also gets its own 1800s rather than the remainder of one shared budget. In
# the workflow both steps shared `timeout-minutes: 30`, so a slow first step
# could leave this one to be killed mid-report; that was a defect of the
# packaging, not a limit worth reproducing.
mkjob audit-domainless-offer-currency "$BACKEND_IMAGE" "$SA" \
  --set-secrets "DATABASE_URL=DATABASE_URL:latest" \
  --set-env-vars "PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=audit-domainless-offer-currency,PIVOTA_COMMIT_SHA=$BACKEND_TAG,DB_POOL_MIN_SIZE=1,DB_POOL_MAX_SIZE=2" \
  --task-timeout 1800s \
  --command python \
  --args="-m,scripts.audit_domainless_offer_currency"
echo "== job: agent-pdp-orphan-reaper (GH Actions cron 37 4 * * *)"
# Migrated from .github/workflows/agent-pdp-orphan-reaper.yml, deleted in the
# same commit, for the same reason as content-canonical-election above: Cloud SQL
# `pivota-pg` has NO public IP (ipv4Enabled=false, private 10.25.0.2 only), so a
# GitHub-hosted runner cannot reach the database from anywhere on the internet.
# The workflow's DATABASE_URL repo secret held Railway's public proxy URL;
# Railway was decommissioned 2026-08-25 and the lane has failed every run since
# (confirmed 2026-08-26 05:07, last green 08-25 05:07). No secret value fixes it.
#
# `--apply` unconditionally, matching the workflow: its apply step was gated
# `github.event_name == 'schedule' || inputs.apply`, so a SCHEDULED run always
# applied — that is the whole point of a backstop. The dispatch checkbox was for
# manual runs, which are now `gcloud run jobs execute` with overriding --args
# (recipe in the script's module docstring).
#
# `--limit 0` (= all) is what the workflow passed on the schedule: its REAP_LIMIT
# fell back to '0' for every non-dispatch event. Passed explicitly rather than
# leaning on the argparse default, so the scheduled contract is readable here and
# an override is obviously a --args replacement.
#
# NOT CARRIED OVER: the separate dry-run pass that ran before the apply step. It
# existed to put a report in the uploaded artifact; the apply report is a strict
# superset of it (same orphans/with_evidence/sample keys, plus deleted), so a
# second full table scan would buy nothing. A non-zero result now logs at WARNING
# from the script itself, which is where that artifact's signal went.
#
# `--set-secrets` is DATABASE_URL alone, not `$SECRETS_FILE`: the whole import
# chain (db.database, services.agent_pdp_view_assembler and its
# catalog_identity/claim_safety/source_quarantine/title_normalization imports)
# resolves with only DATABASE_URL and DB_POOL_* in scope, and
# reap_orphaned_agent_pdp_view_rows touches no second datastore — it is one
# SELECT plus per-row guarded DELETEs against the primary.
#
# DB_POOL_* pinned because a Job inherits no pool sizing from the deploy scripts;
# 1/2 are the values the workflow itself set.
mkjob agent-pdp-orphan-reaper "$BACKEND_IMAGE" "$SA" \
  --set-secrets "DATABASE_URL=DATABASE_URL:latest" \
  --set-env-vars "PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=agent-pdp-orphan-reaper,PIVOTA_COMMIT_SHA=$BACKEND_TAG,DB_POOL_MIN_SIZE=1,DB_POOL_MAX_SIZE=2" \
  --task-timeout 900s \
  --command python \
  --args "scripts/reap_orphaned_agent_pdp_view.py,--apply,--limit,0"

# Store Audit UCP work has a dedicated, explicitly armed pair of Jobs. The
# selector only writes bounded verification rows; the probe Job is the sole
# workload placed on the crawl subnet. Do not create either Job until its
# dedicated receipt secret and staging endpoint exist: an empty/dummy key would
# turn a configuration omission into a silently unauthenticated lane.
if [ "$STORE_AUDIT_UCP_REPROBE_WORKER" = true ]; then
  STORE_AUDIT_UCP_PROBE_CLAIM_URL="$STORE_AUDIT_UCP_PROBE_BACKEND_BASE_URL/internal/store-audit/ucp-probes/claims"
  STORE_AUDIT_UCP_PROBE_RECEIPT_URL="$STORE_AUDIT_UCP_PROBE_BACKEND_BASE_URL/internal/store-audit/ucp-probes/receipts"
  echo "== job: store-audit-ucp-reprobe-enqueue (domain/TTL selector)"
  mkjob store-audit-ucp-reprobe-enqueue "$BACKEND_IMAGE" "$UCP_SELECTOR_SA" \
    --set-secrets "DATABASE_URL=DATABASE_URL:latest,STORE_AUDIT_UCP_PROBE_INTERNAL_KEY=STORE_AUDIT_UCP_PROBE_INTERNAL_KEY:latest" \
    --set-env-vars "PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=store-audit-ucp-reprobe-enqueue,PIVOTA_COMMIT_SHA=$BACKEND_TAG,STORE_AUDIT_UCP_REPROBE_SCHEDULER_ENABLED=true,STORE_AUDIT_UCP_PROBE_RECEIPT_ENABLED=true,DB_POOL_MIN_SIZE=1,DB_POOL_MAX_SIZE=3" \
    --task-timeout 300s \
    --command python --args "scripts/run_scheduled_ucp_reprobes.py"
  echo "== job: store-audit-ucp-probe (anonymous crawl egress)"
  mkcrawljob store-audit-ucp-probe "$GATEWAY_IMAGE" "$UCP_CRAWL_SA" \
    --set-secrets "STORE_AUDIT_UCP_PROBE_INTERNAL_KEY=STORE_AUDIT_UCP_PROBE_INTERNAL_KEY:latest" \
    --set-env-vars "PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=store-audit-ucp-probe,PIVOTA_COMMIT_SHA=$GATEWAY_TAG,STORE_AUDIT_UCP_PROBE_CLAIM_URL=$STORE_AUDIT_UCP_PROBE_CLAIM_URL,STORE_AUDIT_UCP_PROBE_RECEIPT_URL=$STORE_AUDIT_UCP_PROBE_RECEIPT_URL,STORE_AUDIT_UCP_PROBE_ID_TOKEN_AUDIENCE=$STORE_AUDIT_UCP_PROBE_BACKEND_BASE_URL" \
    --command node --args "scripts/run_store_audit_ucp_worker.js"
  "$GCLOUD" run services add-iam-policy-binding web --region "$REGION" \
    --member="serviceAccount:$UCP_CRAWL_SA" --role=roles/run.invoker --quiet
else
  echo "== Store Audit UCP Jobs not created (STORE_AUDIT_UCP_REPROBE_WORKER=false)"
fi

# Commerce checkout audit is merchant-scoped, so the selector emits at most
# one active probe per merchant. The browser job may add one item to cart and
# reveal checkout routing only; its backend receipt contract rejects address,
# payment, and order-submission data.
if [ "$STORE_AUDIT_COMMERCE_REPROBE_WORKER" = true ]; then
  COMMERCE_CLAIM_URL="$STORE_AUDIT_COMMERCE_PROBE_BACKEND_BASE_URL/internal/store-audit/commerce-probes/claims"
  COMMERCE_RECEIPT_URL="$STORE_AUDIT_COMMERCE_PROBE_BACKEND_BASE_URL/internal/store-audit/commerce-probes/receipts"
  echo "== job: store-audit-commerce-reprobe-enqueue (merchant checkout selector)"
  mkjob store-audit-commerce-reprobe-enqueue "$BACKEND_IMAGE" "$COMMERCE_SELECTOR_SA" \
    --set-secrets "DATABASE_URL=DATABASE_URL:latest,STORE_AUDIT_COMMERCE_PROBE_INTERNAL_KEY=STORE_AUDIT_COMMERCE_PROBE_INTERNAL_KEY:latest" \
    --set-env-vars "PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=store-audit-commerce-reprobe-enqueue,PIVOTA_COMMIT_SHA=$BACKEND_TAG,STORE_AUDIT_COMMERCE_REPROBE_SCHEDULER_ENABLED=true,STORE_AUDIT_COMMERCE_REPROBE_ARMED=$STORE_AUDIT_COMMERCE_REPROBE_ARMED,STORE_AUDIT_COMMERCE_PROBE_RECEIPT_ENABLED=true,DB_POOL_MIN_SIZE=1,DB_POOL_MAX_SIZE=3" \
    --task-timeout 300s \
    --command python --args "scripts/run_scheduled_commerce_checkout_reprobes.py"
  echo "== job: store-audit-commerce-probe (anonymous browser crawl egress)"
  mkbrowserauditjob store-audit-commerce-probe "$BROWSER_AUDIT_IMAGE" "$COMMERCE_CRAWL_SA" \
    --set-secrets "STORE_AUDIT_COMMERCE_PROBE_INTERNAL_KEY=STORE_AUDIT_COMMERCE_PROBE_INTERNAL_KEY:latest" \
    --set-env-vars "PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=store-audit-commerce-probe,PIVOTA_COMMIT_SHA=$GATEWAY_TAG,STORE_AUDIT_COMMERCE_REPROBE_ARMED=$STORE_AUDIT_COMMERCE_REPROBE_ARMED,STORE_AUDIT_COMMERCE_PROBE_CLAIM_URL=$COMMERCE_CLAIM_URL,STORE_AUDIT_COMMERCE_PROBE_RECEIPT_URL=$COMMERCE_RECEIPT_URL,STORE_AUDIT_COMMERCE_PROBE_ID_TOKEN_AUDIENCE=$STORE_AUDIT_COMMERCE_PROBE_BACKEND_BASE_URL" \
    --command node --args "scripts/run_store_audit_commerce_worker.js"
  "$GCLOUD" run services add-iam-policy-binding web --region "$REGION" \
    --member="serviceAccount:$COMMERCE_CRAWL_SA" --role=roles/run.invoker --quiet
else
  echo "== Store Audit commerce Jobs not created (STORE_AUDIT_COMMERCE_REPROBE_WORKER=false)"
fi

# ---------------------------------------------------------------- 3. Cloud Scheduler triggers
PROJECT_NUMBER=$("$GCLOUD" projects describe "$PROJECT" --format='value(projectNumber)')
RUN_INVOKER="$SA"
have "$GCLOUD" projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$RUN_INVOKER" --role=roles/run.invoker --condition=None
in_list(){ # name list -- exact match against a comma-separated list
  case ",$2," in *",$1,"*) return 0 ;; *) return 1 ;; esac
}
sched(){ # name schedule job-name [invoker] [paused-on-CREATE: 0|1]
  local name="$1" cron="$2" job="$3" invoker="${4:-$RUN_INVOKER}" paused="${5:-$PAUSED}"
  local uri="https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT/jobs/$job:run"
  local verb=create; have "$GCLOUD" scheduler jobs describe "$name" --location "$REGION" && verb=update
  "$GCLOUD" scheduler jobs "$verb" http "$name" --location "$REGION" \
    --schedule="$cron" --time-zone=Etc/UTC --uri="$uri" --http-method=POST \
    --oauth-service-account-email="$invoker" \
    --attempt-deadline=1800s --quiet

  # WHOSE DECISION IS THE PAUSE STATE? Not this script's, for a trigger that already exists.
  #
  # This used to apply $PAUSED to EVERY trigger on EVERY run, and that is what made the script
  # unrunnable against live prod. The default PAUSED=1 meant a run intended to add ONE job also
  # paused relgraph-sync-cron, the per-minute reviews-invitation-send-cron and the commerce-index
  # lanes; PAUSED=0 meant the opposite blast radius, resuming things an operator never mentioned,
  # which is why the Store Audit guards below refuse it outright. Between them there was no
  # invocation that reconciled the definitions without also rewriting live state - so the jobs
  # migrated in #1892/#1894/#1895 had to be provisioned by hand.
  #
  # Now: $PAUSED decides the state of a trigger this run CREATES, and nothing else. An existing
  # trigger keeps the state it has unless it is named in ARM or DISARM. Re-running the script is
  # therefore idempotent for state, which is what makes it safe to run at all.
  local desired=""
  if [ "$verb" = create ]; then
    desired="$paused"                                   # new trigger: $PAUSED, fail-closed below
  elif in_list "$name" "$ARM"; then
    desired=0
  elif in_list "$name" "$DISARM"; then
    desired=1
  else
    echo "   (state unchanged: $name)"
    return 0
  fi
  # FAIL CLOSED on anything that is not exactly 0. `[ "$PAUSED" = 1 ]` treated PAUSED=true, yes, on
  # and "1 " as UNPAUSED - an operator typing PAUSED=true for safety would have armed
  # reviews-invitation-send on a one-minute schedule against a copy of production.
  case "$desired" in
    0) "$GCLOUD" scheduler jobs resume "$name" --location "$REGION" --quiet \
         || { echo "FAILED to resume $name" >&2; exit 1; }
       echo "   (RESUMED: $name)" ;;
    *) "$GCLOUD" scheduler jobs pause "$name" --location "$REGION" --quiet \
         || { echo "FAILED to pause $name - it may be LIVE" >&2; exit 1; }
       echo "   (paused: $name)" ;;
  esac
}
# ---- external-seed destination sweep -------------------------------------------------------
# Re-reads the third-party product URLs we publish and withdraws the ones that are gone.
# `mkcrawljob`, not `mkjob`: it fetches merchant storefronts, so it must leave from the reserved
# pivota-crawl egress IP. A run from anywhere else is answered with a Cloudflare bot challenge by
# most brand hosts and reports "0 dead links" while having seen almost nothing.
# task-timeout is raised over the crawl default because per-host pacing is the point: ~1,700 seeds
# a day is a full corpus pass inside the 7-day staleness window.
if [ "$EXTERNAL_SEED_DESTINATION_SWEEP" = true ]; then
  echo "== job: external-seed-destination-sweep (retire=$EXTERNAL_SEED_DESTINATION_SWEEP_RETIRE)"
  SWEEP_ARGS="-m,jobs.external_seed_destination_sweep,--limit,1700"
  [ "$EXTERNAL_SEED_DESTINATION_SWEEP_RETIRE" = true ] || SWEEP_ARGS="$SWEEP_ARGS,--no-retire"
  mkcrawljob external-seed-destination-sweep "$BACKEND_IMAGE" "$SA" \
    --set-secrets "DATABASE_URL=DATABASE_URL_NOVERIFY:latest" \
    --set-env-vars "PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=external-seed-destination-sweep,PIVOTA_COMMIT_SHA=$BACKEND_TAG,DB_POOL_MIN_SIZE=1,DB_POOL_MAX_SIZE=3" \
    --task-timeout 3600s \
    --command python --args="$SWEEP_ARGS"
else
  echo "== external-seed destination sweep not created (EXTERNAL_SEED_DESTINATION_SWEEP=false)"
fi

echo "== scheduler triggers"
sched relgraph-sync-cron "37 10 * * *" relgraph-sync
sched external-seed-sentinel-nongrowth-cron "23 10 * * *" external-seed-sentinel-nongrowth
sched pdp-identity-graph-backfill-cron "40 3 * * 1" pdp-identity-graph-backfill
if [ "$EXTERNAL_SEED_DESTINATION_SWEEP" = true ]; then
  sched external-seed-destination-sweep-cron "20 2 * * *" external-seed-destination-sweep
else
  # An idempotent disarm, not merely a creation skip: turning the flag off must stop an
  # existing trigger from continuing to crawl.
  if [ -n "$_EXTERNAL_SEED_DESTINATION_SWEEP_EXPLICIT" ] && have "$GCLOUD" scheduler jobs describe external-seed-destination-sweep-cron --location "$REGION"; then
    "$GCLOUD" scheduler jobs pause external-seed-destination-sweep-cron --location "$REGION" --quiet
    echo "   (paused: external-seed-destination-sweep-cron; EXTERNAL_SEED_DESTINATION_SWEEP=false)"
  fi
fi
sched reviews-invitation-send-cron "* * * * *" reviews-invitation-send
# 00:52/06:52/12:52/18:52 UTC. The exact cron the GH workflow carried, and the
# offset is load-bearing: it LEADS pivota-agent-ui's sitemap cron (17 1,7,13,19)
# by 25 minutes, so the sitemap regenerating next reads a fresh election rather
# than one 5h35m old. Moving this without moving that one silently inverts the
# order.
sched content-canonical-election-cron "52 */6 * * *" content-canonical-election
# 09:11 UTC every Monday. The exact cron the GH workflow carried, preserved
# rather than re-picked so the report keeps arriving where the operator already
# looks for it. DRY-RUN: the Job's baked args carry no --apply (see above).
sched derive-offer-market-currency-cron "11 9 * * 1" derive-offer-market-currency
# 09:41, the same Monday, +30 min. Deliberately NOT the same 11 9 minute: the
# two Jobs fetch overlapping storefronts, and 30 min is the first Job's own
# --task-timeout, so the offset is what guarantees they cannot be in flight at
# once no matter how long the first one runs.
sched audit-domainless-offer-currency-cron "41 9 * * 1" audit-domainless-offer-currency
# 04:37 UTC daily. The exact cron the GH workflow carried, preserved because its
# own comment named the minute as chosen rather than arbitrary: "off-peak, offset
# from the PDP production smoke (08:17)" — that smoke is still a GitHub workflow
# (.github/workflows/pdp-production-smoke.yml, cron 17 8 * * *) and still reads
# PDPs over HTTP, so the 3h40m gap it was given is a live constraint, not a
# leftover. Rounding this to :00 or nudging it toward 08:00 gives it back.
sched agent-pdp-orphan-reaper-cron "37 4 * * *" agent-pdp-orphan-reaper
sched commerce-index-relgraph-cron "*/10 * * * *" commerce-index-relgraph
sched commerce-index-search-index-cron "*/5 * * * *" commerce-index-search-index
sched commerce-index-checkout-validation-cron "*/5 * * * *" commerce-index-checkout-validation
sched commerce-index-insight-refresh-cron "*/10 * * * *" commerce-index-insight-refresh
if [ "$STORE_AUDIT_UCP_REPROBE_WORKER" = true ]; then
  for job in store-audit-ucp-reprobe-enqueue store-audit-ucp-probe; do
    "$GCLOUD" run jobs add-iam-policy-binding "$job" --region "$REGION" \
      --member="serviceAccount:$UCP_SCHEDULER_SA" --role=roles/run.invoker --quiet
  done
  UCP_PAUSED=1
  [ "$STORE_AUDIT_UCP_REPROBE_ARMED" = true ] && UCP_PAUSED=0
  sched store-audit-ucp-reprobe-enqueue-cron "30 3 * * *" store-audit-ucp-reprobe-enqueue "$UCP_SCHEDULER_SA" "$UCP_PAUSED"
  sched store-audit-ucp-probe-cron "*/5 * * * *" store-audit-ucp-probe "$UCP_SCHEDULER_SA" "$UCP_PAUSED"
else
  # Default false is an idempotent operational disarm, not merely a creation
  # skip. Existing Jobs may remain for forensic inspection, but their triggers
  # must never keep crawling after an operator turns the feature off.
  for trigger in store-audit-ucp-reprobe-enqueue-cron store-audit-ucp-probe-cron; do
    if [ -n "$_STORE_AUDIT_UCP_REPROBE_WORKER_EXPLICIT" ] && have "$GCLOUD" scheduler jobs describe "$trigger" --location "$REGION"; then
      "$GCLOUD" scheduler jobs pause "$trigger" --location "$REGION" --quiet
      echo "   (paused: $trigger; STORE_AUDIT_UCP_REPROBE_WORKER=false)"
    fi
  done
fi
if [ "$STORE_AUDIT_COMMERCE_REPROBE_WORKER" = true ]; then
  for job in store-audit-commerce-reprobe-enqueue store-audit-commerce-probe; do
    "$GCLOUD" run jobs add-iam-policy-binding "$job" --region "$REGION" \
      --member="serviceAccount:$COMMERCE_SCHEDULER_SA" --role=roles/run.invoker --quiet
  done
  COMMERCE_PAUSED=1
  [ "$STORE_AUDIT_COMMERCE_REPROBE_ARMED" = true ] && COMMERCE_PAUSED=0
  sched store-audit-commerce-reprobe-enqueue-cron "45 3 * * *" store-audit-commerce-reprobe-enqueue "$COMMERCE_SCHEDULER_SA" "$COMMERCE_PAUSED"
  sched store-audit-commerce-probe-cron "*/5 * * * *" store-audit-commerce-probe "$COMMERCE_SCHEDULER_SA" "$COMMERCE_PAUSED"
else
  for trigger in store-audit-commerce-reprobe-enqueue-cron store-audit-commerce-probe-cron; do
    if [ -n "$_STORE_AUDIT_COMMERCE_REPROBE_WORKER_EXPLICIT" ] && have "$GCLOUD" scheduler jobs describe "$trigger" --location "$REGION"; then
      "$GCLOUD" scheduler jobs pause "$trigger" --location "$REGION" --quiet
      echo "   (paused: $trigger; STORE_AUDIT_COMMERCE_REPROBE_WORKER=false)"
    fi
  done
fi
# A global scheduler resume must never activate this new writer by accident.
if [ "$RELGRAPH_PUBLICATION_WORKER" != true ] && [ -n "$_RELGRAPH_PUBLICATION_WORKER_EXPLICIT" ]; then
  "$GCLOUD" scheduler jobs pause commerce-index-relgraph-cron --location "$REGION" --quiet
  echo "   (paused: commerce-index-relgraph-cron; RELGRAPH_PUBLICATION_WORKER=false)"
fi
if [ "$SEARCH_INDEX_PUBLICATION_WORKER" != true ] && [ -n "$_SEARCH_INDEX_PUBLICATION_WORKER_EXPLICIT" ]; then
  "$GCLOUD" scheduler jobs pause commerce-index-search-index-cron --location "$REGION" --quiet
  echo "   (paused: commerce-index-search-index-cron; SEARCH_INDEX_PUBLICATION_WORKER=false)"
fi
if [ "$CHECKOUT_VALIDATION_WORKER" != true ] && [ -n "$_CHECKOUT_VALIDATION_WORKER_EXPLICIT" ]; then
  "$GCLOUD" scheduler jobs pause commerce-index-checkout-validation-cron --location "$REGION" --quiet
  echo "   (paused: commerce-index-checkout-validation-cron; CHECKOUT_VALIDATION_WORKER=false)"
fi
if [ "$INSIGHT_REFRESH_WORKER" != true ] && [ -n "$_INSIGHT_REFRESH_WORKER_EXPLICIT" ]; then
  "$GCLOUD" scheduler jobs pause commerce-index-insight-refresh-cron --location "$REGION" --quiet
  echo "   (paused: commerce-index-insight-refresh-cron; INSIGHT_REFRESH_WORKER=false)"
fi

echo
# REPORT WHAT WAS APPLIED, NOT WHAT WAS ASKED FOR. Under `preserve` this script does not send
# AUDIT_WORKER_ENABLED at all - it is applied through the env file, which only `apply` writes -
# so printing "AUDIT_WORKER_ENABLED=$WORKERS" here stated as fact something the run had not
# done. That is worse than the crash it replaced: `WORKERS=true ... setup_scheduler.sh` used to
# die loudly at deploy_worker.sh's guard, and briefly instead exited 0 having reconciled
# everything while telling the operator the drainers were armed. Read the live service.
# THE SERVING REVISION, because the line below says "live" and an operator will read it that
# way. `spec.template` is what the last deploy ASKED FOR; if that deploy did not take, the two
# disagree and this would report the arming state of a revision serving nobody. Written the
# first time as a template read, in the same commit whose comment said "Read the live service".
WORKER_ARMED="$(serving_env worker AUDIT_WORKER_ENABLED || true)"
echo "worker:    $("$GCLOUD" run services describe worker --region "$REGION" --format='value(status.url)') (min=max=1, AUDIT_WORKER_ENABLED=${WORKER_ARMED:-<unreadable>})"
if [ "$CONFIG" != apply ] && [ -n "${WORKERS_REQUESTED:-}" ] && [ "${WORKERS_REQUESTED:-}" != "$WORKER_ARMED" ]; then
  echo "   NOTE: you passed WORKERS=$WORKERS_REQUESTED, and CONFIG=preserve did NOT apply it." >&2
  echo "   AUDIT_WORKER_ENABLED is still '${WORKER_ARMED:-<unreadable>}'. To change it:" >&2
  echo "     gcloud run services update worker --region $REGION --project $PROJECT \\" >&2
  echo "       --update-env-vars AUDIT_WORKER_ENABLED=$WORKERS_REQUESTED" >&2
fi
"$GCLOUD" scheduler jobs list --location "$REGION" --format="table(name.basename(),schedule,state)"
