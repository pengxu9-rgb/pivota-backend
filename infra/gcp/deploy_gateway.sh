#!/usr/bin/env bash
# Deploy the PIVOTA-Agent gateway image to Cloud Run in one environment.
#   infra/gcp/deploy_gateway.sh staging|prod <commit-sha>
#
#   CONFIG=preserve  (default) roll the image forward and restamp PIVOTA_COMMIT_SHA, leaving every
#                    environment variable and secret mount exactly as the running service has them.
#                    No prereqs, no Railway.
#   CONFIG=apply     rewrite env + secrets from the ported files. Prereqs: bootstrap_env.sh ran;
#                    port_railway_env.py --prefix gateway --apply produced env.<env>.gateway.yaml
#                    and secrets.<env>.gateway.list.
#
# WHY `preserve` IS THE DEFAULT HERE, when deploy_backend.sh defaults to `apply`.
#
# `apply` reads two git-ignored files that port_railway_env.py generates from `railway variables
# --json`. Railway was decommissioned at the 2026-08-22 cutover, so on a fresh checkout those files
# do not exist and cannot be regenerated - which made the hard `exit 1` below the outcome of EVERY
# run of this script, in both environments. That is not a dormant edge case. It is why the prod
# gateway has been deployed by hand with `gcloud` since the cutover, and a hand deploy runs none of
# the gates underneath: sweep_stale_tags() left three tagged revisions pinned at minScale 2, running
# old code against live secrets, until they were swept by hand on 2026-08-25; and one revision
# INHERITED the previous revision's PIVOTA_COMMIT_SHA - `6aa49526db95` on an image built from
# `17e7cfa8` - so /health under-reported the deployed commit by 7 and the gateway-prod-drift alarm
# computed "30 commits behind" against a true 23. An unusable deploy script does not stop deploys;
# it moves them somewhere with no gates at all, and the gates are the whole point of this file.
#
# `apply` is also the dangerous direction on its own merits. The live prod gateway carries 382
# environment variables and Cloud Run, not Railway, is now where they live. Re-deriving all 382
# from the retired platform in order to ship a code change is a config rollback riding along with a
# release, and it would report success. Shipping code must not be able to do that silently, so
# `apply` is opt-in and named by the operator.
#
# The gateway image comes from the PIVOTA-Agent repo's own root Dockerfile (node:20-bookworm-slim,
# non-root `node`, CMD node src/server.js). That repo's Railway services are pinned to the RAILPACK
# builder explicitly, so the root Dockerfile is inert there - unlike pivota-backend, where adding one
# hijacked the builder for 8 services.
set -euo pipefail
ENV="${1:-}"; TAG="${2:-}"
[ -n "$ENV" ] && [ -n "$TAG" ] || { echo "usage: $0 staging|prod <commit-sha>   (CONFIG=preserve|apply)" >&2; exit 2; }
case "$ENV" in
  staging) PROJECT=pivota-staging; PIVOTA_ENV=staging;    MIN=1; MAX=4;  CPU=2; MEM=2Gi; GW_POOL_MAIN=3; GW_POOL_AUX=2 ;;
  prod)    PROJECT=pivota-prod;    PIVOTA_ENV=production; MIN=2; MAX=20; CPU=2; MEM=4Gi; GW_POOL_MAIN=2; GW_POOL_AUX=1 ;;
  *) echo "bad env" >&2; exit 2 ;;
esac
HERE="$(cd "$(dirname "$0")" && pwd)"

# ONE VARIABLE FEEDS BOTH THE IMAGE AND THE STAMP.
#
# $TAG builds $IMAGE below and is the only source of PIVOTA_COMMIT_SHA in BOTH config modes, so the
# two cannot name different commits. Worth stating, because the divergence actually observed did not
# come from setting them to different values - it came from setting only ONE. `gcloud run deploy
# --image X` with no env flag inherits the previous revision's environment wholesale, so a hand
# deploy that rolled the image forward and said nothing about PIVOTA_COMMIT_SHA carried the OLD
# commit onto the NEW code. Both branches below write the stamp on EVERY deploy; neither can inherit
# it. The failure mode is now "the deploy did not happen", never "the deploy lied about what ran".
#
# `latest` defeats that and is refused: the tag floats, so the stamp names no commit for the drift
# alarm to compare. It is a live hazard rather than a theoretical one - cloudbuild.gateway.yaml
# pushes `:latest` beside `:$COMMIT_SHA`, so it is always sitting there to be typed.
case "$TAG" in
  latest) echo "refusing the floating tag 'latest' - pass the commit sha, so PIVOTA_COMMIT_SHA names a real commit" >&2; exit 2 ;;
esac
# Prod additionally requires a full 40-character sha, the same rule deploy-prod.yml applies to the
# backend. A short sha matches no image tag in Artifact Registry, and an abbreviation stamped into
# /health is not a value the drift alarm can compare against main. Staging is only warned: it is
# routinely fed locally built tags, and a wrong stamp there misleads nobody in production.
if ! [[ "$TAG" =~ ^[0-9a-f]{40}$ ]]; then
  [ "$ENV" = prod ] && { echo "prod needs a full 40-character lowercase commit sha (got '$TAG')" >&2; exit 2; }
  echo "note: '$TAG' is not a 40-character sha - PIVOTA_COMMIT_SHA will carry it verbatim" >&2
fi

# CONFIG=preserve leaves every environment variable and secret mount exactly as the running service
#                 has them, and changes only the image and PIVOTA_COMMIT_SHA.
# CONFIG=apply    rewrites both from env.<env>.gateway.yaml / secrets.<env>.gateway.list.
# The header says why the default is the opposite of deploy_backend.sh's.
: "${CONFIG:=preserve}"
case "$CONFIG" in preserve|apply) ;; *) echo "CONFIG must be preserve or apply (got '$CONFIG')" >&2; exit 2 ;; esac
# WHAT `preserve` DOES NOT PRESERVE, said out loud so the name does not overpromise. It keeps
# environment variables and secret mounts. It still reasserts the SHAPE of the service from the
# constants above and the overrides beside them - --cpu, --memory, --min/--max-instances,
# --concurrency, --timeout, --ingress, --vpc-egress, --labels, --service-account. Those match live
# prod today, so nothing drifts; but an operator who widened --max-instances by hand during an
# incident will have it pulled back silently by the next deploy.
#
# PIVOTA_ENV, PIVOTA_SERVICE_NAME and the four GW_POOL_* sizings travel in the env FILE, so under
# `preserve` they are computed and then not sent. The running service already carries them, and a
# CHANGE to any of those numbers needs CONFIG=apply to land.

# all-traffic, NOT private-ranges-only. Under private-ranges-only outbound traffic to the public
# internet does not traverse the VPC, so it never leaves via Cloud NAT and the reserved address is
# NOT the source IP. `8.231.167.230` is published to Antom/Adyen for allowlisting, so a deploy that
# reverted this would silently break their IP checks. Verified from inside the VPC: a Cloud Run job
# on this egress mode reports EGRESS_IP=8.231.167.230.
: "${VPC_EGRESS:=all-traffic}"
# `internal` alone does NOT admit the load balancer - only internal-and-cloud-load-balancing does,
# so prod (which sits behind the LB) must use the latter.
# Staging defaults to `all` deliberately: the live staging gateway is reachable on its run.app URL
# today, and silently flipping a SERVICE-level setting mid-deploy would take it offline for anything
# outside the VPC - including the operator running the deploy. Pass INGRESS=internal explicitly to
# tighten it as a considered change rather than a side effect of shipping code.
: "${INGRESS:=$([ "$ENV" = prod ] && echo internal-and-cloud-load-balancing || echo all)}"
: "${PUBLIC:=$([ "$ENV" = prod ] && echo 1 || echo 0)}"
[ "$PUBLIC" = 1 ] && PUBLIC_FLAG=--allow-unauthenticated || PUBLIC_FLAG=--no-allow-unauthenticated
GCLOUD="${GCLOUD:-gcloud}"
REGION=us-west1
SERVICE="${SERVICE:-gateway}"
IMAGE="$REGION-docker.pkg.dev/pivota-shared/pivota/gateway:$TAG"
CANDIDATE_TAG="c-$(printf '%s' "$TAG" | tr -cd '[:alnum:]' | tail -c 12)"
# `--no-traffic` is rejected on service CREATION, so the candidate-then-verify flow only applies to
# an existing service. A brand-new service has no previous revision to protect anyway.
if "$GCLOUD" run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" >/dev/null 2>&1; then
  NO_TRAFFIC="--tag $CANDIDATE_TAG --no-traffic"; FIRST_DEPLOY=0
else
  NO_TRAFFIC=""; FIRST_DEPLOY=1; echo "note: $SERVICE does not exist yet - first revision takes traffic immediately"
fi
# `preserve` has nothing to preserve on a service that does not exist yet, and this is the one place
# where getting it wrong is not caught downstream: a first revision takes traffic IMMEDIATELY - no
# --no-traffic, no candidate tag, no health gate in front of users. gcloud would create the service
# carrying a single environment variable, with no DSN mounted at all and no PIVOTA_ENV, so
# requirePlatformEnv() throws at boot. Refuse up front instead of failing obscurely mid-deploy.
if [ "$FIRST_DEPLOY" = 1 ] && [ "$CONFIG" = preserve ]; then
  echo "$SERVICE does not exist in $PROJECT - a first deploy has no running config to preserve. Use CONFIG=apply." >&2
  exit 2
fi
ENV_FILE="$HERE/env.$ENV.gateway.yaml"
SECRETS_FILE="$HERE/secrets.$ENV.gateway.list"

CONFIG_ARGS=()
if [ "$CONFIG" = preserve ]; then
  # ONE env var still has to move, and getting this wrong is worse than not deploying at all.
  #
  # PIVOTA_COMMIT_SHA is what /health reports and what the gateway-prod-drift alarm compares against
  # main. A preserve-mode deploy that left it alone would not merely fail to update it - Cloud Run
  # would carry the PREVIOUS revision's value forward onto the new image, so /health would keep
  # swearing to the old commit while new code served traffic and the alarm went green over a lie.
  # That is the exact 7-commit under-report seen on the hand deploy this branch replaces, and a
  # silent alarm is strictly worse than a loud one: it invites trust.
  #
  # --update-env-vars MERGES one key. --set-env-vars / --env-vars-file REPLACE the whole set, and on
  # the live prod gateway that is the other 381.
  CONFIG_ARGS=(--update-env-vars "PIVOTA_COMMIT_SHA=$TAG")
  # THE ONE WAY THIS DEFAULT CAN BITE, said out loud where the operator will see it.
  #
  # `preserve` being the default means a deploy run straight after `port_railway_env.py --apply`
  # silently ships the image and NOTHING ELSE: the two files that were just generated are never
  # read, and the script still prints a promoted, health-checked, 100%-traffic success. That is
  # the same shape deploy_backend.sh refuses for WORKERS/MOUNT_DB - a successful-looking deploy
  # that did not do the one thing it was run for.
  #
  # Their presence on disk is the intent signal, so say so rather than ignoring it. A WARNING and
  # not a refusal: these files are git-ignored build output that can sit in a checkout for months
  # after the port that made them, so refusing would break the ordinary case to catch the rare
  # one. The deploy is still correct - it just is not the deploy the operator may have meant.
  if [ -f "$ENV_FILE" ] || [ -f "$SECRETS_FILE" ]; then
    echo "WARNING: ported config files exist next to this script but CONFIG=preserve IGNORES them." >&2
    echo "         Only the image and PIVOTA_COMMIT_SHA will change. Re-run with CONFIG=apply to" >&2
    echo "         apply $ENV_FILE / $SECRETS_FILE." >&2
  fi
else
[ -f "$ENV_FILE" ] && [ -f "$SECRETS_FILE" ] || { echo "missing $ENV_FILE / $SECRETS_FILE - run port_railway_env.py --prefix gateway first" >&2; exit 1; }

# The gateway talks to Postgres directly (internal_catalog / external_seeds discovery providers), so
# it needs DATABASE_URL and the pci_kb DSN - neither of which the porter emits: it DROPS DATABASE_URL
# by design, because the value must come from Cloud SQL rather than Railway. Mount them explicitly,
# or /health reports db_backed_providers_ready=false with code "missing_database" and discovery runs
# single-provider without ever erroring.
#
# The _NOVERIFY variants exist because node-pg validates the server certificate chain differently to
# asyncpg against Cloud SQL's private IP; the Python services use the plain sslmode=require DSNs.
SECRETS="DATABASE_URL=DATABASE_URL_NOVERIFY:latest,PCI_KB_DATABASE_URL=PCI_KB_DATABASE_URL_NOVERIFY:latest,INGREDIENT_REFERENCE_DATABASE_URL=PCI_KB_DATABASE_URL_NOVERIFY:latest,$(paste -sd, "$SECRETS_FILE")"
MERGED=$(mktemp); chmod 600 "$MERGED"; trap 'rm -f "$MERGED"' EXIT INT TERM
# gcloud's --env-vars-file resolves a DUPLICATE KEY to the FIRST occurrence, not the last (verified
# against the SDK's own loader). Appending an override after the ported file therefore does NOTHING
# whenever the ported file already defines that key - which silently made WORKERS, DB_POOL_* and even
# PIVOTA_ENV inert. Strip the keys we are about to set before appending them.
# Strip exactly the keys re-set below. DB_POOL_MIN_SIZE/DB_POOL_MAX_SIZE are kept in this list only
# because a ported Railway env may still carry them (they are inert here); the four names that DO
# matter must be stripped, or a ported value would win - gcloud takes the FIRST duplicate key - and
# the printf below would be silently inert, reverting the gateway to 5+3+3+3 per instance.
grep -vE '^(PIVOTA_ENV|PIVOTA_SERVICE_NAME|PIVOTA_COMMIT_SHA|PIVOTA_PLATFORM|SKIP_HEAVY_STARTUP_INIT|AUDIT_WORKER_ENABLED|REVIEWS_INVITATION_WORKER_ENABLED|DB_POOL_MIN_SIZE|DB_POOL_MAX_SIZE|DB_POOL_MAX|PCI_KB_DB_POOL_MAX|INGREDIENT_REFERENCE_DB_POOL_MAX|INGREDIENT_SIGNAL_DB_POOL_MAX):' "$ENV_FILE" > "$MERGED"
{ :
  # requirePlatformEnv() throws at boot without this (src/server.js:52114).
  printf 'PIVOTA_ENV: "%s"\nPIVOTA_SERVICE_NAME: "%s"\nPIVOTA_COMMIT_SHA: "%s"\n' "$PIVOTA_ENV" "$SERVICE" "$TAG"
  # The gateway opens FOUR pools, not one, and reads these exact names:
  #   DB_POOL_MAX                      src/db/index.js:132                 (default 5)
  #   PCI_KB_DB_POOL_MAX               src/services/pciKbClient.js:46      (default 3)
  #   INGREDIENT_REFERENCE_DB_POOL_MAX src/services/ingredientReferenceStore.js:82 (default 3)
  #   INGREDIENT_SIGNAL_DB_POOL_MAX    src/services/ingredientSignalStore.js:93    (default 3)
  # Defaults therefore sum to 14 connections PER INSTANCE, not 10. PG_POOL_MAX / PGPOOL_MAX /
  # DB_POOL_MAX_SIZE - which an earlier version of this script set - are read by NOTHING in this
  # repo, so that sizing was inert. Measure the variable names before trusting a budget.
  #
  # BUDGET against max_connections=300 (raised from 200):
  #   web 10x12=120 + gateway 20x5=100 + worker 1x10=10 = 230, leaving 70 for ops and superuser.
  #   (web was 20x6 until 2026-08-29; same 120 ceiling, but concurrency 80->20 so its pool is
  #    1.7x oversubscribed instead of 13x — see deploy_backend.sh's prod case arm.)
  #   proof-issuer and acp mount no DATABASE_URL at all, so they contribute 0.
  printf 'DB_POOL_MAX: "%s"\nPCI_KB_DB_POOL_MAX: "%s"\nINGREDIENT_REFERENCE_DB_POOL_MAX: "%s"\nINGREDIENT_SIGNAL_DB_POOL_MAX: "%s"\n' \
    "$GW_POOL_MAIN" "$GW_POOL_AUX" "$GW_POOL_AUX" "$GW_POOL_AUX"
} >> "$MERGED"
CONFIG_ARGS=(--env-vars-file "$MERGED" --set-secrets "$SECRETS")
fi

# The candidate gate must be able to REACH the candidate. Every prod service is
# `internal-and-cloud-load-balancing`, so a curl from an operator's laptop gets Google's 404 before
# the container is ever consulted - the gate would then exit 1 on every prod deploy and strand a
# perfectly good revision at 0%. Measured 2026-08-20: web/gateway/proof-issuer/acp all 404 from
# outside; `internal` 404s before IAM, `all` gets as far as a 403.
#
# So: try directly, and if the answer is an ingress/IAM rejection rather than the app, re-probe from
# INSIDE the VPC with a one-shot Cloud Run job. Only a real 200 from the application passes.
probe_health(){ # url -> echoes the status code
  local url="$1" code
  # curl emits the -w template even when the transfer FAILS - "000", no trailing newline - and
  # only then exits non-zero. The old `|| echo 000` therefore appended a second one and produced
  # "000000", which matches no arm of the case below except the catch-all, so the function returned
  # early and the in-VPC re-probe underneath - the entire reason this function exists - was dead
  # code for every timeout and connection reset. Only a real HTTP status ever reached it.
  code=$(curl -sS -o /tmp/pivota-health.$$ -w '%{http_code}' -m 30 ${AUTH_ARGS[@]+"${AUTH_ARGS[@]}"} "$url" 2>/dev/null) || true
  code="${code:-000}"
  case "$code" in
    200) rm -f /tmp/pivota-health.$$; echo 200; return 0 ;;
    403|404|000) : ;;                     # possibly ingress/IAM, not the app - fall through
    *) rm -f /tmp/pivota-health.$$; echo "$code"; return 0 ;;
  esac
  rm -f /tmp/pivota-health.$$
  echo "   direct probe got $code (ingress-blocked from here); re-probing from inside the VPC" >&2
  # ^|^ delimiter: gcloud splits --args on COMMAS, and this probe is Python that contains commas
  # (`,timeout=25`), which would otherwise be shredded into separate argv entries.
  local job="verify-$$-$RANDOM"
  local out="" i probe_rc=0 create_rc=0
  "$GCLOUD" run jobs create "$job" --region "$REGION" --project "$PROJECT" \
    --image "$REGION-docker.pkg.dev/pivota-shared/pivota/backend:latest" \
    --service-account "sa-worker@$PROJECT.iam.gserviceaccount.com" \
    --network default --subnet default --vpc-egress all-traffic \
    --max-retries 0 --task-timeout 120s --command python \
    --args="^|^-c|import urllib.request,sys;r=urllib.request.urlopen('$url',timeout=25);s=r.status;u=r.geturl();print('PROBE_STATUS='+str(s)+' FINAL_URL='+u);sys.exit(0 if s==200 and u=='$url' else 1)" \
    --quiet >/dev/null 2>&1 || create_rc=$?
  # THE VERDICT IS THE JOB'S EXIT CODE, NOT ITS LOGS.
  #
  # urlopen raises on any non-2xx (HTTPError) and on any connection failure (URLError), and the
  # probe now exits non-zero for a 2xx that is not exactly 200 - so `exit 0` means, precisely,
  # "the application answered 200 from inside the VPC". The job already carries that answer out
  # through --wait. Scraping it back out of Cloud Logging was asking a second, slower, less
  # reliable system to re-tell us something we had already been told.
  #
  # That indirection stranded a healthy revision on 2026-08-25. The probe DID return 200
  # (PROBE_STATUS=200, logged 02:25:10.788Z) and the candidate was Ready/ContainerHealthy, but the
  # entry had not become QUERYABLE inside the 30s poll below, so the read came back empty, the
  # function returned 000, and the deploy refused to promote. Ingestion lag is unbounded; any
  # fixed window is a guess, and every guess eventually loses. The exit code has no such window.
  #
  # Failure direction is unchanged and still safe: a missing image, a shredded --args, no python,
  # an unroutable URL, or a non-200 all exit non-zero and still refuse the promotion.
  # An unchecked create would make "exit 0 means healthy" rest on an unverified precondition:
  # `set -e` does not fire inside this function (see the `|| true` note below), so a failed create
  # was entirely silent, and only the execute failing afterwards kept it safe by luck.
  [ "$create_rc" = 0 ] || { echo "   could not create the in-VPC probe job (gcloud exited $create_rc)" >&2; echo 000; return 0; }
  "$GCLOUD" run jobs execute "$job" --region "$REGION" --project "$PROJECT" --wait --quiet >/dev/null 2>&1 \
    || probe_rc=$?

  if [ "$probe_rc" = 0 ]; then
    # Nothing left to look up: the exit code already said 200. Skipping the scrape on the happy
    # path also keeps ~15s of `sleep` and three `logging read` calls off every good deploy.
    "$GCLOUD" run jobs delete "$job" --region "$REGION" --project "$PROJECT" --quiet >/dev/null 2>&1 || true
    echo 200
    return 0
  fi

  # FAILURE PATH ONLY, and best-effort: recover the specific status for the operator's message.
  #
  # `|| true` is load-bearing, for the same reason it is on the CAND_URL pipeline below: under
  # `set -o pipefail` a grep that matches NOTHING makes the whole pipeline exit 1, `VAR=$(...)`
  # adopts that status, and the `set -euo pipefail` at the top kills the script. A missing log line is
  # the NORMAL case here — the probe raises before printing for any non-2xx — so without this the
  # common failure would abort mid-function and leak the probe job that the delete below reaps.
  # It survived review only because a function called inside `$( )` is exempt from `-e`; that is
  # an accident of the ONE call site, not a property of this function.
  for i in 1 2 3; do
    out=$("$GCLOUD" logging read "resource.labels.job_name=\"$job\"" --project "$PROJECT" --limit 15 \
      --format='value(textPayload)' 2>/dev/null | grep -oE 'PROBE_STATUS=[0-9]+' | head -1 | cut -d= -f2 || true)
    [ -n "$out" ] && break
    sleep 5
  done
  "$GCLOUD" run jobs delete "$job" --region "$REGION" --project "$PROJECT" --quiet >/dev/null 2>&1 || true
  echo "   in-VPC probe job exited $probe_rc${out:+ (PROBE_STATUS=$out)}" >&2
  # A PASS is the exit code and nothing else. If the scrape reports 200 for a job that FAILED
  # the two disagree, and the scrape does not get to win: echoing it here would hand the caller
  # the single value that promotes, on the strength of the slower, less reliable signal this
  # function was just rewritten to stop trusting. Any other recovered code is safe to pass
  # through - it cannot promote, and it makes the failure message specific.
  [ "${out:-000}" = 200 ] && out=000
  echo "${out:-000}"
}

"$GCLOUD" run deploy "$SERVICE" --project "$PROJECT" --region "$REGION" \
  --image "$IMAGE" \
  --service-account "sa-gateway@$PROJECT.iam.gserviceaccount.com" \
  --network default --subnet default --vpc-egress "$VPC_EGRESS" \
  ${CONFIG_ARGS[@]+"${CONFIG_ARGS[@]}"} \
  --port 8080 --cpu "$CPU" --memory "$MEM" --concurrency 80 --timeout 300 \
  --min-instances "${MIN_INSTANCES:-$MIN}" --max-instances "${MAX_INSTANCES:-$MAX}" \
  --no-cpu-throttling --cpu-boost --execution-environment gen2 \
  --ingress "$INGRESS" \
  $PUBLIC_FLAG \
  --labels "env=$ENV,service=$SERVICE,managed-by=infra-gcp" \
  $NO_TRAFFIC \
  --quiet

CAND_URL=$("$GCLOUD" run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" \
  --format="value(status.traffic.extract(\"url\").flatten())" | tr ',;' '\n\n' | grep -F "$CANDIDATE_TAG" | head -1 || true)
# ^ `|| true` is load-bearing. Under `set -o pipefail` a grep that matches nothing makes the whole
# pipeline exit 1, `VAR=$(...)` adopts that status, and `set -e` kills the script - silently, right
# after a successful `run deploy`. That is exactly the FIRST_DEPLOY case the next two lines were
# written to handle, so they could never run.
[ "$FIRST_DEPLOY" = 1 ] && CAND_URL=""
CAND_URL="${CAND_URL:-$("$GCLOUD" run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format='value(status.url)')}"
# macOS ships bash 3.2, where "${AUTH[@]}" on an EMPTY array is an unbound-variable error under
# `set -u` — which is exactly the public (PUBLIC=1) case. Use the ${arr[@]+"${arr[@]}"} guard.
AUTH=()
[ "$PUBLIC" = 1 ] || AUTH=(-H "Authorization: Bearer $("$GCLOUD" auth print-identity-token)")
AUTH_ARGS=(${AUTH[@]+"${AUTH[@]}"})
echo "verifying candidate at $CAND_URL"
# /health, NOT /healthz: Cloud Run's frontend intercepts /healthz and returns its own 404 before the
# request reaches the container (proved 2026-08-19: /health -> 200 application/json from the app,
# /healthz -> 404 text/html from GFE). Railway's healthcheckPath is /healthz, so this differs by platform.
CODE=$(probe_health "$CAND_URL/health")
[ "$CODE" = 200 ] || { echo "candidate /health returned $CODE - NOT shifting traffic." >&2; exit 1; }

# Retire stale candidate tags, and say what was retired.
#
# WHY THIS IS NOT COSMETIC. A tagged revision is never garbage-collected, and every service
# here sets min-instances >= 1, so each old candidate keeps instances RUNNING forever. Cloud Run
# resolves `--set-secrets ...:latest` at INSTANCE START, not per request, so those immortal
# instances stay pinned to whatever secret VERSION was latest when they booted.
#
# That is not hypothetical. After the 2026-08-22 cutover, `gateway-00010-mar` (booted 08-20, so
# holding DATABASE_URL_NOVERIFY v1) kept running its in-process pdp_identity_auto_resolve timer
# every 30 minutes against the RETIRED `pivota` database - including 400 rows written AFTER the
# secret had been repointed to the cutover snapshot. Repointing a secret fixes the revision that
# takes traffic; it does nothing to the ones still pinned behind a tag.
#
# Only tags on revisions serving 0% are removed; the live revision keeps its own tag. Parsed from
# JSON rather than `--format=filter("percent:0")` - that filter currently matches the 100%-traffic
# entry as well (gcloud warns its operator semantics are changing), which would untag the LIVE
# revision. Cleanup failure is reported but never fails the deploy: the promotion already
# succeeded, and leaving a stale tag is worse-but-not-broken.
sweep_stale_tags() {
  local stale
  stale=$("$GCLOUD" run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" \
    --format=json 2>/dev/null | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception: sys.exit(0)
print(",".join(t["tag"] for t in d.get("status",{}).get("traffic",[])
                if t.get("tag") and not t.get("percent")))' 2>/dev/null) || return 0
  [ -n "$stale" ] || { echo "no stale candidate tags"; return 0; }
  echo "retiring stale candidate tags: $stale"
  "$GCLOUD" run services update-traffic "$SERVICE" --project "$PROJECT" --region "$REGION" \
    --remove-tags="$stale" --quiet >/dev/null 2>&1 \
    || echo "WARNING: could not remove stale tags ($stale) - remove them by hand, they keep instances alive on old secret versions" >&2
}

[ "$FIRST_DEPLOY" = 1 ] || "$GCLOUD" run services update-traffic "$SERVICE" --project "$PROJECT" --region "$REGION" --to-latest --quiet
sweep_stale_tags
echo "deployed $SERVICE -> $("$GCLOUD" run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format='value(status.url)') (100% traffic)"
