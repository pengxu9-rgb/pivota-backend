#!/usr/bin/env bash
# Roll the `worker` Cloud Run service - the single-instance drainer - to one backend image.
#   infra/gcp/deploy_worker.sh staging|prod <image-tag>
#
#   CONFIG=preserve  (default) change the image, restamp PIVOTA_COMMIT_SHA, re-pin the handful of
#                    knobs this file owns, and leave every other env var and secret mount exactly
#                    as the SERVICE TEMPLATE has them. No prereqs, no Railway. This is what CI uses.
#   CONFIG=apply     rewrite env + secrets from the ported files. Needs env.<env>.yaml and
#                    secrets.<env>.list, which port_railway_env.py generates from `railway
#                    variables` - Railway was decommissioned 2026-08-22, so this mode cannot be
#                    run on a fresh checkout. Kept because it is the only mode that can move
#                    AUDIT_WORKER_ENABLED.
#
# ── WHY THIS FILE EXISTS ───────────────────────────────────────────────────────────────────────
# The worker's shape used to live inside setup_scheduler.sh, one block among twenty Cloud Run Jobs
# and every Cloud Scheduler trigger, behind a mandatory <gateway-tag> argument. Rolling the worker
# therefore meant either running all of that or hand-copying eight lines out of it - and the
# hand-copy is what actually happened, twice (2026-09-02 `worker-00016-rmv`, 2026-09-05
# `worker-00018-rm6`), because reconciling every trigger is not a thing you want to do to ship one
# image. A critical section that is copied by hand at the moment of use is not a definition; the
# copy and the original drift, and nobody finds out until the shapes disagree in production.
#
# So the shape is defined ONCE, here. setup_scheduler.sh calls this file. So does deploy-prod.yml.
#
# ── WHY NOT deploy_backend.sh ──────────────────────────────────────────────────────────────────
# Three reasons, each of which alone is disqualifying:
#
#   1. ITS SHAPE IS WEB'S. deploy_backend.sh applies cpu 2 / 4Gi / concurrency 20 / min 2 / max 10,
#      which is a connection budget computed for the public API. The worker is cpu 1 / 2Gi /
#      concurrency 1 / min=max=1, and `--min-instances 2` on a drainer whose whole design is
#      "exactly one process" is not a tuning difference, it is a second drainer.
#   2. ITS CANDIDATE FLOW IS ACTIVELY WRONG HERE. It ships every revision `--tag c-<sha>
#      --no-traffic`, probes it, then promotes. For a service with minScale 1 that tagged
#      0%-traffic revision KEEPS AN INSTANCE RUNNING (deploy_backend.sh's own sweep_stale_tags
#      comment documents these immortal instances), and a worker instance does not need traffic to
#      do work - the scheduler starts from the app lifespan. So the candidate window runs TWO
#      drainers. The queue survives that (every claim is `FOR UPDATE SKIP LOCKED`), but APScheduler
#      enforces max_instances=1 PER PROCESS, so two processes double-fire every tick, including
#      the settlement and refund lanes. Traffic splitting is not a safety mechanism for this
#      service, and treating it as one is how you get the duplicate-capture hazard the WORKERS
#      flag exists to prevent.
#   3. SERVICE=worker is refused outright at its ENV_PREFIX check (deploy_backend.sh line ~135),
#      which demands a per-service env file the worker has never had - it uses env.<env>.yaml.
#
# ── WHAT A ROLL COSTS, SAID OUT LOUD ───────────────────────────────────────────────────────────
# Cloud Run replaces the instance, so whatever the old process was mid-way through is SIGTERMed.
# main.shutdown_event calls services.audit_scheduler.stop_scheduler, which PAUSES the scheduler,
# waits a few seconds for in-flight runs to land (SCHEDULER_DRAIN_SECONDS, default 3, 0 to
# disable), and then cancels whatever is still going. A short tick therefore completes; a long
# one is still cancelled. Nothing is lost either way: an audit run holds a lease
# (DEFAULT_LEASE_SECONDS=600, STALE_LEASE_GRACE_SECONDS=30) and the next worker reclaims it inline
# via claim_next_pending_run, with fail_abandoned_runs as the terminal backstop. The cost is
# latency - up to ~10.5 minutes for a run that was mid-stage - and it is the same cost the two
# hand-deploys above already paid. Automating the roll does not create this cost; it makes it
# regular and predictable instead of arriving whenever someone remembers.
set -euo pipefail
ENV="${1:-}"; TAG="${2:-}"
[ -n "$ENV" ] && [ -n "$TAG" ] || { echo "usage: $0 staging|prod <image-tag>" >&2; exit 2; }
case "$ENV" in
  staging) PROJECT=pivota-staging; PIVOTA_ENV=staging ;;
  prod)    PROJECT=pivota-prod;    PIVOTA_ENV=production ;;
  *) echo "bad env '$ENV' (want staging|prod)" >&2; exit 2 ;;
esac

: "${CONFIG:=preserve}"
case "$CONFIG" in apply|preserve) ;; *) echo "CONFIG must be apply or preserve (got '$CONFIG')" >&2; exit 2 ;; esac

# WORKERS arms the drainers, and it only reaches the service through the env FILE - so it is
# meaningful under `apply` and inert under `preserve`. Refuse rather than ignore: a caller who
# writes `WORKERS=true infra/gcp/deploy_worker.sh prod <tag>` is asking for the drainers to come
# on, and reporting a clean successful deploy that did not do that is the exact shape
# deploy_backend.sh already had to add this same guard for.
_WORKERS_EXPLICIT="${WORKERS+1}"
: "${WORKERS:=false}"
case "$WORKERS" in true|false) ;; *) echo "WORKERS must be exactly true or false (got '$WORKERS')" >&2; exit 2 ;; esac
if [ "$CONFIG" = preserve ] && [ -n "$_WORKERS_EXPLICIT" ]; then
  echo "WORKERS has no effect under CONFIG=preserve (AUDIT_WORKER_ENABLED is applied via the env" >&2
  echo "file). Use CONFIG=apply, or set it directly:" >&2
  echo "  gcloud run services update worker --region us-west1 --project $PROJECT \\" >&2
  echo "    --update-env-vars AUDIT_WORKER_ENABLED=$WORKERS" >&2
  exit 2
fi

GCLOUD="${GCLOUD:-gcloud}"
REGION=us-west1
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=infra/gcp/_serving_revision.sh
. "$HERE/_serving_revision.sh"
SA="sa-worker@$PROJECT.iam.gserviceaccount.com"
IMAGE="$REGION-docker.pkg.dev/pivota-shared/pivota/backend:$TAG"

# THE SHAPE. min=max=1 is the load-bearing one and the reason every other number is small:
# `--concurrency 1` and one instance is what makes "the single-instance drainer" true.
# --no-cpu-throttling because the drainers work between requests and a throttled instance
# stops running the scheduler; gen2 for the same reason.
CPU=1; MEM=2Gi; CONCURRENCY=1; MIN=1; MAX=1

# The image must exist BEFORE the deploy. Cloud Run accepts a deploy naming an absent image and
# fails when the revision cannot pull it, which arrives as a generic Ready=False several minutes
# later - a bad error for something a one-line check answers exactly.
"$GCLOUD" artifacts docker images describe "$IMAGE" >/dev/null 2>&1 \
  || { echo "no such image: $IMAGE" >&2
       echo "build it first (deploy-prod.yml's web job does this, and this script reuses that image):" >&2
       echo "  gcloud builds submit --config infra/gcp/cloudbuild.backend.yaml --project pivota-shared \\" >&2
       echo "    --substitutions=COMMIT_SHA=$TAG ." >&2
       exit 1; }

# What is running RIGHT NOW, captured before anything changes, so the failure path below can put
# it back. Read from the service rather than remembered from a previous run of this script: the
# thing to roll back to is whatever is actually serving, including a revision this file never
# deployed.
# ONE trap for every temp file this script makes. `trap` REPLACES the handler rather than
# appending, so a second `trap ... EXIT` further down would silently disable the first and leak
# whatever it guarded - which is what happened when each temp file brought its own.
_TMPFILES=""
_cleanup(){ [ -z "$_TMPFILES" ] || rm -f $_TMPFILES; }
trap _cleanup EXIT INT TERM

# `|| true` ON A FAILING describe IS NOT SAFE HERE, and that was the bug: it cannot tell "the
# service does not exist yet" from "the API returned 500 / auth blipped / we were throttled".
# Both produced an empty PREV_IMAGE, which reads as a first deploy, prints a false "does not exist
# yet", and - the part that matters - leaves NOTHING TO ROLL BACK TO. A probe failure after that
# exits 1 with production stranded on the broken image. So: keep stderr, and let only a genuine
# not-found mean absent. Anything else refuses while refusing is still free.
# THE ROLLBACK TARGET IS WHAT IS SERVING, not what the template asks for. Those differ exactly
# when it matters: if an earlier deploy created a revision that never became Ready — including
# this script's own rollback failing — the template names that broken image while the previous
# revision still serves. Rolling "back" to the template would then roll FORWARD into the thing
# that was already broken. See _serving_revision.sh.
PREV_ERR="$(mktemp)"; _TMPFILES="$_TMPFILES $PREV_ERR"
PREV_RC=0
PREV_IMAGE="$(serving_image worker 2>"$PREV_ERR")" || PREV_RC=$?
if [ "$PREV_RC" != 0 ]; then
  # MEASURED against gcloud 581.0.0, because the previous pattern here was guessed and the
  # guess was wrong: a missing Cloud Run service produces
  #     ERROR: (gcloud.run.services.describe) Cannot find service [worker]
  # which matches none of NOT_FOUND / could not be found / does not exist. So the "create it"
  # branch was DEAD for real gcloud, and a genuine first deploy would have refused.
  #
  # SCOPED TO THIS SERVICE ON PURPOSE. A bare `NOT_FOUND` also appears in `NOT_FOUND: Project
  # ... not found`, and treating a mis-scoped project as "the service is absent" would deploy
  # with no rollback anchor - fail-open in the exact direction this guard exists to close.
  # Anything this does not recognise refuses, which is the safe direction: a first deploy is
  # rare and attended, a blind deploy over a live worker is neither.
  if grep -qiE 'cannot find service \[?worker\]?' "$PREV_ERR"; then
    PREV_IMAGE=""
  else
    # SAY WHICH FAILURE THIS IS. Three different things land here — gcloud itself failing, a
    # traffic block with no single named 100% revision, and the JSON/python read not working —
    # and they used to print one empty-bodied message asserting "gcloud exited 1", which is
    # false for the last two (gcloud exited 0). An operator got a blocked production deploy
    # with no cause. The exit code is the helper's, not gcloud's, so it cannot be reported as
    # gcloud's.
    if [ -s "$PREV_ERR" ]; then
      echo "could not read the worker's current image; gcloud said:" >&2
      sed 's/^/  /' "$PREV_ERR" >&2
    else
      echo "could not read the worker's current image, and gcloud reported no error." >&2
      echo "That means the service was describable but had no single NAMED revision at 100%" >&2
      echo "traffic (a split or a half-finished rollout), or its JSON could not be parsed." >&2
      echo "  gcloud run services describe worker --project $PROJECT --region $REGION \\" >&2
      echo "    --format='value(status.traffic)'" >&2
    fi
    echo "Refusing to deploy: without knowing what is running there is nothing to roll back to," >&2
    echo "and a failed probe would leave production on the new image with no way back." >&2
    exit 1
  fi
fi
if [ -n "$PREV_IMAGE" ]; then
  echo "worker is on ${PREV_IMAGE##*:}"
  if [ "$PREV_IMAGE" = "$IMAGE" ]; then
    # Not an error, and not a no-op either: the health probe below is still worth running, because
    # "the image tag matches" and "the scheduler booted and registered its jobs" are different
    # claims and this repo has been bitten by treating the first as evidence of the second.
    echo "already on $TAG - redeploying anyway to re-verify (a matching tag is not a health check)"
  fi
else
  echo "note: worker does not exist yet in $PROJECT - creating it"
fi

ENV_FILE="$HERE/env.$ENV.yaml"; SECRETS_FILE="$HERE/secrets.$ENV.list"
CONFIG_ARGS=()
if [ "$CONFIG" = apply ]; then
  [ -f "$ENV_FILE" ] && [ -f "$SECRETS_FILE" ] \
    || { echo "CONFIG=apply needs $ENV_FILE / $SECRETS_FILE - run port_railway_env.py first (it reads Railway, which is retired)" >&2; exit 1; }
  MERGED=$(mktemp); chmod 600 "$MERGED"; _TMPFILES="$_TMPFILES $MERGED"
  # gcloud's --env-vars-file resolves a DUPLICATE KEY to the FIRST occurrence, not the last, so an
  # override appended after the ported file does nothing whenever that file already defines the
  # key. Strip the keys we are about to set before appending them.
  grep -vE '^(PIVOTA_ENV|PIVOTA_SERVICE_NAME|PIVOTA_COMMIT_SHA|SKIP_HEAVY_STARTUP_INIT|AUDIT_WORKER_ENABLED|REVIEWS_INVITATION_WORKER_ENABLED|DB_POOL_MIN_SIZE|DB_POOL_MAX_SIZE):' "$ENV_FILE" > "$MERGED"
  { printf 'PIVOTA_ENV: "%s"\nPIVOTA_SERVICE_NAME: "worker"\nPIVOTA_COMMIT_SHA: "%s"\n' "$PIVOTA_ENV" "$TAG"
    printf 'SKIP_HEAVY_STARTUP_INIT: "true"\n'
    printf 'AUDIT_WORKER_ENABLED: "%s"\nREVIEWS_INVITATION_WORKER_ENABLED: "%s"\n' "$WORKERS" "$WORKERS"
    printf 'DB_POOL_MIN_SIZE: "2"\nDB_POOL_MAX_SIZE: "10"\n'
  } >> "$MERGED"
  CONFIG_ARGS=(--env-vars-file "$MERGED"
    --set-secrets "DATABASE_URL=DATABASE_URL:latest,REDIS_URL=REDIS_URL:latest,PCI_KB_DATABASE_URL=PCI_KB_DATABASE_URL:latest,$(paste -sd, "$SECRETS_FILE")")
else
  # PRESERVE. Restamp the commit and re-pin the knobs this file owns, and NOTHING else:
  # `--update-env-vars` merges these keys and leaves the other ~236 variables and every secret
  # mount exactly as the SERVICE TEMPLATE has them -- `--update-env-vars` merges into the
  # template, not into whatever is serving, and the two differ after a deploy that did not
  # take. Worth knowing for the rollback below: it restores the SERVING revision's IMAGE onto
  # the template's env. AUDIT_WORKER_ENABLED is deliberately absent -
  # see the WORKERS guard above.
  #
  # PIVOTA_COMMIT_SHA is not optional and not cosmetic. config/platform.py cannot read the commit
  # from Cloud Run (nothing injects it), so this variable is the ONLY source of the sha the service
  # reports about itself. A roll that changed the image and left it stale would ship new code under
  # the old commit - and prod-deploy-drift.yml, which is the independent check on this pipeline,
  # would go green over that lie. A silent alarm is worse than a missing one.
  CONFIG_ARGS=(--update-env-vars "PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=worker,PIVOTA_COMMIT_SHA=$TAG,SKIP_HEAVY_STARTUP_INIT=true,DB_POOL_MIN_SIZE=2,DB_POOL_MAX_SIZE=10")
fi

deploy_image(){ # image -> deploys it with the shape above
  "$GCLOUD" run deploy worker --project "$PROJECT" --region "$REGION" --image "$1" \
    --service-account "$SA" \
    --network default --subnet default --vpc-egress all-traffic \
    ${CONFIG_ARGS[@]+"${CONFIG_ARGS[@]}"} \
    --port 8080 --cpu "$CPU" --memory "$MEM" --concurrency "$CONCURRENCY" \
    --min-instances "$MIN" --max-instances "$MAX" \
    --no-cpu-throttling --execution-environment gen2 --ingress internal \
    --labels "env=$ENV,service=worker,managed-by=infra-gcp" --quiet
}

echo "== worker service (min=max=1, ingress internal, CONFIG=$CONFIG) -> $TAG"
deploy_image "$IMAGE"

# ── DID THE SCHEDULER ACTUALLY COME UP ─────────────────────────────────────────────────────────
# `gcloud run deploy` returns success as soon as the container passes its startup probe, and this
# container passes that probe whether or not the scheduler booted: the drainers are started from
# the app lifespan, and a scheduler that raised on boot leaves a perfectly healthy HTTP server
# answering /health with 200. That is the exact failure this service has: silent. There is no
# logging config, so Python's WARNING default drops every logger.info the scheduler emits, and the
# worker's steady state is ZERO log lines. "It deployed" has never been evidence that it works.
#
# /__scheduler_health reports the real runtime state - state_name, boot_error, worker_enabled, and
# fireable_job_count, the count of registered jobs that actually have a next_run_time. A worker
# with fireable_job_count 0 is a process that is up and will do nothing until someone notices.
#
# It has to be asked from INSIDE the VPC: the service is `--ingress internal`, so from anywhere
# else Google's front end answers 404 before the request reaches the app - even with a valid
# identity token. Same one-shot Cloud Run job as deploy_backend.sh's probe_health, and for the
# same reason: THE VERDICT IS THE JOB'S EXIT CODE, never a scrape of Cloud Logging, whose
# ingestion lag is unbounded and which stranded a healthy revision on 2026-08-25.
WORKER_URL="$("$GCLOUD" run services describe worker --project "$PROJECT" --region "$REGION" \
  --format='value(status.url)')"
[ -n "$WORKER_URL" ] || { echo "could not read the worker's URL - cannot verify the deploy" >&2; exit 1; }

PROBE_JOB="verify-worker-$$-$RANDOM"
# EXPECT the tag we just shipped. Asking the RUNNING PROCESS what commit it is is a stronger claim
# than reading the image off the service spec: the spec says what was requested, /__build says what
# is executing. `full_sha` and `commit_sha` are both top-level keys of _runtime_build_payload().
#
# AN ABSENT SHA IS A FAILURE, not a pass. main.py computes `full_sha = commit_sha or None` from
# PIVOTA_COMMIT_SHA, which nothing in Cloud Run injects and which THIS DEPLOY sets - so a process
# reporting no sha is one that did not get the variable this script just wrote, which is precisely
# the "new code under the old commit" state the restamp exists to prevent. An earlier version
# guarded this with `if sha and ...`, so an empty string skipped the comparison entirely and the
# probe exited 0 having verified nothing about the commit. Caught in review, 2026-09-05.
# AN IDENTITY TOKEN IS REQUIRED, and its absence is what broke the first real run of this
# script (2026-09-06, run 34012321564): every request came back 403 and the deploy rolled
# itself back. The reasoning that put it there was "deploy_backend.sh probes without a token
# and that is proven in this VPC" - true, and irrelevant: deploy_backend.sh probes `web`, which
# is deployed --allow-unauthenticated. The worker is --ingress internal AND not publicly
# invokable, so Google's front end rejects an unauthenticated request before the app sees it.
# Being inside the VPC satisfies INGRESS; it does not satisfy IAM.
#
# The token comes from the metadata server, audience-scoped to the service URL.
PROBE_PY="import json,urllib.request,sys
def _get(u, tok):
    r=urllib.request.Request(u); r.add_header('Authorization','Bearer '+tok); return json.load(urllib.request.urlopen(r,timeout=25))
_t=urllib.request.Request('http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity?audience=$WORKER_URL')
_t.add_header('Metadata-Flavor','Google')
tok=urllib.request.urlopen(_t,timeout=25).read().decode()
d=_get('$WORKER_URL/__scheduler_health',tok)
b=_get('$WORKER_URL/__build',tok)
sha=str(b.get('full_sha') or b.get('commit_sha') or '')
print('PROBE state='+str(d.get('state_name'))+' fireable='+str(d.get('fireable_job_count'))+' worker_enabled='+str(d.get('worker_enabled'))+' boot_error='+str(d.get('boot_error'))+' sha='+sha)
ok = d.get('state_name')=='RUNNING' and not d.get('boot_error') and (d.get('fireable_job_count') or 0)>0
if sha != '$TAG' and not sha.startswith('$TAG'):
    print('SHA MISMATCH: running '+(sha or '<none>')+' but deployed $TAG'); ok=False
sys.exit(0 if ok else 1)"
# ^|^ below sets the --args delimiter to `|`, because gcloud splits --args on COMMAS by default and
# this probe is Python full of them. That makes `|` the one character the program may not contain -
# a future edit adding `or`-piping, a regex alternation or a bitwise or would be SHREDDED into
# separate argv entries and the probe would fail for a reason having nothing to do with the worker.
# Check it here rather than discover it in a deploy.
case "$PROBE_PY" in
  *"|"*) echo "the probe program contains a '|', which is the --args delimiter - it would be split into separate arguments. Rewrite it without one." >&2; exit 2 ;;
esac
probe_rc=0; create_rc=0
"$GCLOUD" run jobs create "$PROBE_JOB" --region "$REGION" --project "$PROJECT" \
  --image "$IMAGE" --service-account "$SA" \
  --network default --subnet default --vpc-egress all-traffic \
  --max-retries 0 --task-timeout 180s --command python \
  --args="^|^-c|$PROBE_PY" --quiet >/dev/null 2>&1 || create_rc=$?
if [ "$create_rc" != 0 ]; then
  # An unverifiable deploy is NOT a passing one. The previous revision is already gone at this
  # point (there is no candidate to hold back), so the honest move is to say so and exit non-zero
  # rather than print a success line nobody can back up.
  echo "::error::could not create the in-VPC probe job (gcloud exited $create_rc) - the worker was" >&2
  echo "deployed but NOT verified. Check it by hand before trusting it:" >&2
  echo "  gcloud run services describe worker --project $PROJECT --region $REGION" >&2
  exit 1
fi
PROBE_ERR="$(mktemp)"; _TMPFILES="$_TMPFILES $PROBE_ERR"
PROBE_LOG="$(mktemp)"; _TMPFILES="$_TMPFILES $PROBE_LOG"
"$GCLOUD" run jobs execute "$PROBE_JOB" --region "$REGION" --project "$PROJECT" --wait --quiet \
  >/dev/null 2>"$PROBE_ERR" || probe_rc=$?
# Pull the probe's own output BEFORE deleting the job. Unlike deploy_backend.sh, this is not used
# to decide success - the exit code already did that - only to classify a FAILURE as "the app said
# no" versus "we never reached the app". A scrape that comes back empty leaves PROBE_REACHED_APP=1,
# i.e. the conservative reading that the verdict was real.
if [ "$probe_rc" != 0 ]; then
  for _i in 1 2 3; do
    "$GCLOUD" logging read "resource.labels.job_name=\"$PROBE_JOB\"" --project "$PROJECT" \
      --limit 30 --format='value(textPayload)' >"$PROBE_LOG" 2>/dev/null || true
    [ -s "$PROBE_LOG" ] && break
    sleep 5
  done
fi
# Best-effort cleanup, never fatal: a leaked probe JOB costs nothing (jobs hold no instances, which
# is why this pattern is safe here and a tagged candidate REVISION is not).
"$GCLOUD" run jobs delete "$PROBE_JOB" --region "$REGION" --project "$PROJECT" --quiet >/dev/null 2>&1 || true

# WAS THE VERDICT ABOUT THE WORKER, OR ABOUT OUR ABILITY TO ASK?
#
# The first real run of this script rolled production back on a 403 - an answer about IAM, not
# about the scheduler - and because PREV_IMAGE is re-read every run, repeating that on each push
# to main is an image FLAP: deploy, fail to ask, roll back, deploy again next merge. A rollback
# is a production change and must rest on evidence about production.
#
# So a probe that could not COMPLETE leaves the new image in place and fails loudly: the deploy
# is unverified, which is not a pass, but it is also not grounds to move production twice on no
# information. Only a probe that ran and returned a verdict rolls back. Same distinction the
# drift alarm draws between its own plumbing failing and production being unverifiable.
PROBE_REACHED_APP=1
if [ "$probe_rc" != 0 ] && grep -qiE 'HTTP Error (401|403)|URLError|metadata.google.internal|Forbidden' "$PROBE_LOG" 2>/dev/null; then
  PROBE_REACHED_APP=0
fi
if [ "$probe_rc" != 0 ] && [ "$PROBE_REACHED_APP" = 0 ]; then
  echo "::error::could not ASK the worker whether it is healthy on $TAG - the probe never got an" >&2
  echo "answer from the application (auth, IAM or networking), so there is no evidence about the" >&2
  echo "scheduler either way. The new image is LEFT IN PLACE deliberately: rolling production back" >&2
  echo "on a question we failed to ask would move it twice on no information, and repeating that" >&2
  echo "every merge is an image flap." >&2
  echo "This deploy is UNVERIFIED. Check it by hand, and check that sa-worker can invoke the" >&2
  echo "service (it needs roles/run.invoker on it):" >&2
  echo "  gcloud run services get-iam-policy worker --region $REGION --project $PROJECT" >&2
  echo "  gcloud logging read 'resource.labels.job_name=\"$PROBE_JOB\"' --project $PROJECT --limit 20" >&2
  exit 1
fi
if [ "$probe_rc" != 0 ]; then
  echo "::error::the worker deployed but did not verify on $TAG." >&2
  echo "The probe asserts state_name=RUNNING, no boot_error, fireable_job_count>0, and that the" >&2
  echo "running process reports this commit. Its line is in the job's logs:" >&2
  # `jobs execute` exits non-zero both when the TASK failed and when the API CALL failed, and
  # those are different facts: the first is evidence about the worker, the second is evidence
  # about nothing. Do not assert the scheduler is broken on the strength of a quota error.
  if [ -s "$PROBE_ERR" ]; then
    echo "gcloud also wrote to stderr, so this may be an API failure rather than a verdict:" >&2
    sed 's/^/  /' "$PROBE_ERR" >&2
  fi
  # AND THE MOST LIKELY NON-BUG CAUSE, named so it is not misdiagnosed as a boot failure:
  # services/audit_scheduler.py's _add_job registers a job ONLY `if worker_enabled`, so a worker
  # with AUDIT_WORKER_ENABLED=false reports state RUNNING, boot_error None, and fireable_job_count
  # exactly 0. That is a DISARMED worker, not a broken one - and since this script deliberately
  # cannot set that flag, the fix is not here.
  echo "If the probe line says worker_enabled=False the drainers are DISARMED and this is NOT a" >&2
  echo "boot failure - arm them deliberately, then redeploy:" >&2
  echo "  gcloud run services update worker --region $REGION --project $PROJECT \\" >&2
  echo "    --update-env-vars AUDIT_WORKER_ENABLED=true" >&2
  echo "  gcloud logging read 'resource.labels.job_name=\"$PROBE_JOB\"' --project $PROJECT --limit 20" >&2
  if [ -n "$PREV_IMAGE" ] && [ "$PREV_IMAGE" != "$IMAGE" ]; then
    echo "rolling back to ${PREV_IMAGE##*:}" >&2
    # Roll the IMAGE back rather than shifting traffic. Traffic is not the control here: a worker
    # instance does work from its lifespan, not from requests, so `update-traffic` to the old
    # revision would leave the unhealthy one's instance alive and draining alongside it. Replacing
    # the image is what actually removes the process.
    #
    # PRESERVE SEMANTICS EVEN IF THE FORWARD DEPLOY WAS `apply`, deliberately: a rollback should
    # change back the one thing it is rolling back. Re-running the env/secret rewrite here would
    # make a failed deploy's recovery a second full configuration write, at the moment we least
    # understand what is wrong. (Reachable only in principle today - `apply` needs the
    # Railway-ported files, which no longer exist.)
    CONFIG_ARGS=(--update-env-vars "PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=worker,PIVOTA_COMMIT_SHA=${PREV_IMAGE##*:},SKIP_HEAVY_STARTUP_INIT=true,DB_POOL_MIN_SIZE=2,DB_POOL_MAX_SIZE=10")
    deploy_image "$PREV_IMAGE" \
      && echo "rolled back to ${PREV_IMAGE##*:}" >&2 \
      || echo "::error::ROLLBACK ALSO FAILED. The worker needs hands." >&2
  fi
  exit 1
fi

echo "worker is on $TAG, scheduler RUNNING with jobs registered"
