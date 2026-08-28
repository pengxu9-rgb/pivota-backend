#!/usr/bin/env bash
# Alerting for a production environment that had none.
#   infra/gcp/setup_monitoring.sh prod|staging
#   ALERT_EMAIL=someone@example.com infra/gcp/setup_monitoring.sh prod
#
# The 2026-08-22 post-cutover audit found pivota-prod serving live payment traffic with 0 alert
# policies, 0 notification channels, 0 uptime checks and 0 log-based metrics. Every failure below
# was discoverable only by a user reporting it, or by somebody happening to look: LB 5xx, the
# invitation job failing (it runs 1,440x/day), Cloud SQL saturation, a revision that will not
# start, and TLS breaking because an _acme-challenge CNAME was dropped during a zone edit.
#
# WHY THESE FIVE - each is a failure that has already happened here, or would be silent:
#   host is down    the only check that fails when the whole path breaks, DNS included
#   TLS expiring    certs auto-renew; renewal stops SILENTLY if a DNS authorization is removed
#   LB 5xx          the user-visible symptom of nearly everything else
#   job failing     reviews-invitation-send runs every minute; a persistent failure is invisible
#   SQL saturation  this codebase has wedged on pool exhaustion before (2026-08-20 sitemap wedge)
#
# Idempotent: reuses the notification channel, skips existing uptime checks, and replaces alert
# policies by displayName so thresholds can be tuned in git and re-applied.
#
# Uses the Monitoring REST API directly rather than `gcloud alpha monitoring`, which requires the
# alpha component. A missing component makes those commands print an install prompt and exit 0 -
# so `... | wc -l` reads as "0 policies" when the truth is "the command never ran". That is how
# the audit's baseline was nearly mis-measured.
set -euo pipefail

ENV="${1:-}"
case "$ENV" in
  prod)    PROJECT=pivota-prod ;;
  staging) PROJECT=pivota-staging ;;
  *) echo "usage: $0 prod|staging" >&2; exit 2 ;;
esac

GCLOUD="${GCLOUD:-gcloud}"
API="https://monitoring.googleapis.com/v3/projects/$PROJECT"
TOKEN="$("$GCLOUD" auth print-access-token)"

# An alert with no channel is decoration - and an alert pointed at the WRONG channel is worse,
# because the dashboard says "5 policies, all enabled" either way.
#
# This used to default to `gcloud config get-value account`: whoever happened to be logged in when
# the script last ran decided where production pages go. On 2026-08-28 that silently aimed every
# prod alert at an address nobody watches, and the backend sat wedged on database pool exhaustion
# for 5h40m. The uptime check DID detect it - `check_passed` for api.pivota.cc was 0.00 from 12:32
# onward and "prod: host is down" fired correctly. Nobody saw it. Detection was never the problem.
#
# So prod must say the address out loud. Staging keeps the convenience default; it pages no one.
if [ -z "${ALERT_EMAIL:-}" ]; then
  if [ "$ENV" = prod ]; then
    echo "ALERT_EMAIL is required for prod. Set it to the address that is actually READ:" >&2
    echo "  ALERT_EMAIL=you@example.com $0 prod" >&2
    echo "Refusing to infer it from \`gcloud config get-value account\` - that is how prod alerts" >&2
    echo "ended up going to an unwatched inbox (see the comment above this check)." >&2
    exit 2
  fi
  ALERT_EMAIL="$("$GCLOUD" config get-value account 2>/dev/null)"
fi
[ -n "$ALERT_EMAIL" ] || { echo "ALERT_EMAIL empty and no gcloud account set" >&2; exit 2; }

# Public hostnames, prod only. Staging is IAM-gated with nothing public to probe.
HOSTS=(api.pivota.cc gateway.pivota.cc mcp.pivota.cc commerce.mcp.pivota.cc ucp.pivota.cc acp.pivota.cc)

api() { # METHOD PATH [BODY]
  if [ -n "${3:-}" ]; then
    curl -sS -m 60 -X "$1" -H "Authorization: Bearer $TOKEN" \
         -H "Content-Type: application/json" -d "$3" "$API/$2"
  else
    curl -sS -m 60 -X "$1" -H "Authorization: Bearer $TOKEN" "$API/$2"
  fi
}

check() { # JSON LABEL   -> abort on an API error rather than reporting success
  python3 -c '
import json, sys
raw = sys.stdin.read().strip()
label = sys.argv[1]
if not raw:
    sys.exit(0)
try:
    d = json.loads(raw)
except ValueError:
    sys.exit(label + " FAILED: non-JSON response: " + raw[:200])
if isinstance(d, dict) and "error" in d:
    sys.exit(label + " FAILED: " + d["error"].get("message", ""))
' "$2" <<<"$1"
}

echo "== notification channel ($ALERT_EMAIL)"
CHANNEL="$(api GET notificationChannels | python3 -c '
import json, sys
d = json.load(sys.stdin)
want = sys.argv[1]
for c in d.get("notificationChannels", []):
    if c.get("type") == "email" and c.get("labels", {}).get("email_address") == want:
        print(c["name"]); break
' "$ALERT_EMAIL")"

if [ -n "$CHANNEL" ]; then
  echo "   reusing $CHANNEL"
else
  BODY="$(python3 -c '
import json, sys
print(json.dumps({"type": "email", "displayName": "pivota prod alerts",
                  "labels": {"email_address": sys.argv[1]}, "enabled": True}))' "$ALERT_EMAIL")"
  RESP="$(api POST notificationChannels "$BODY")"
  check "$RESP" "create channel"
  CHANNEL="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])' <<<"$RESP")"
  echo "   created $CHANNEL"
fi

# Deliberately OUTSIDE the if/else. An API-created email channel is born
# UNVERIFIED and stays that way until a human completes the emailed code, so the
# CREATE arm is the one that manufactures an undeliverable channel. Checking only
# the reuse arm instruments the branch that OBSERVES the damage while staying
# silent on the branch that CAUSES it — which is how the live 08-28 channel came
# to exist with no verificationStatus while the script reported success.
#
# `${CHANNEL#projects/*/}` is a SHORTEST-prefix strip: `*` cannot swallow the
# second `/` because the character after `projects/` is `p`, so this yields
# `notificationChannels/<id>`, which `api()` prefixes with the project path. `##`
# would yield a bare id and GET a 404, making the warning fire unconditionally
# and training the reader to ignore it. A test pins the single `#`.
CHANNEL_VERIFIED="$(api GET "${CHANNEL#projects/*/}" 2>/dev/null | python3 -c '
import json, sys
raw = sys.stdin.read().strip()
try:
    print(json.loads(raw).get("verificationStatus", "") if raw else "")
except ValueError:
    print("")
')"
CHANNEL_UNDELIVERABLE=0
if [ "$CHANNEL_VERIFIED" != VERIFIED ]; then
  CHANNEL_UNDELIVERABLE=1
  echo "   WARNING: channel is '${CHANNEL_VERIFIED:-UNSET}', not VERIFIED." >&2
  echo "   Cloud Monitoring does not deliver to an unverified email channel. Every policy" >&2
  echo "   below will fire into the void. Open the channel in the console and complete" >&2
  echo "   verification, then re-run this script to confirm." >&2
fi

echo "== uptime checks"
UP="$(api GET uptimeCheckConfigs)"
for h in "${HOSTS[@]}"; do
  NAME="uptime-${h//./-}"
  FOUND="$(python3 -c '
import json, sys
d = json.load(sys.stdin)
for c in d.get("uptimeCheckConfigs", []):
    if c.get("displayName") == sys.argv[1]:
        print(c["name"]); break
' "$NAME" <<<"$UP")"
  if [ -n "$FOUND" ]; then echo "   $h exists"; continue; fi
  BODY="$(python3 -c '
import json, sys
host, name, project = sys.argv[1], sys.argv[2], sys.argv[3]
print(json.dumps({
  "displayName": name,
  "monitoredResource": {"type": "uptime_url", "labels": {"host": host, "project_id": project}},
  "httpCheck": {"path": "/health", "port": 443, "useSsl": True,
                "validateSsl": True, "requestMethod": "GET"},
  "period": "300s", "timeout": "10s",
  "selectedRegions": ["USA_OREGON", "EUROPE", "ASIA_PACIFIC"]}))' "$h" "$NAME" "$PROJECT")"
  RESP="$(api POST uptimeCheckConfigs "$BODY")"
  check "$RESP" "uptime $h"
  echo "   created $h"
done

echo "== log-based metrics"
# "prod: host is down" tells you the service is unreachable. It does not tell you WHY, and on
# 2026-08-28 the why took three hours of log spelunking to establish: every error in the window was
# `PoolCheckoutTimeout: timed out waiting 120.0s for a database connection`, while Cloud SQL sat at
# 28 of 300 connections with 23 of them IDLE. The database was healthy the whole time; the
# application was holding pool slots it never returned.
#
# TWO alternatives and NO `severity>=ERROR`, both load-bearing. The line this
# codebase deliberately emits for the condition is db/database.py's
# `logger.warning("database pool checkout timed out after %.1fs ...")` — a
# WARNING whose text does not contain the class name. Meanwhile the designed
# handling path (utils/db_retry.py -> db_busy_http_exception) turns the exception
# into HTTPException(503), for which FastAPI logs no traceback at all. A filter
# requiring ERROR *and* the class name therefore matches only when the exception
# escapes every classifier — the exact outcome PoolCheckoutTimeout exists to
# prevent. It caught 08-28 and would degrade toward never firing as more of the
# ~15 call sites adopt with_asyncpg_busy_retry: an enabled policy that cannot
# fire, the same defect this PR fixes on the LB 5xx rate.
#
# That distinction is invisible to every other policy here. "Cloud SQL connections high" watches
# num_backends, which was at 9% - it CANNOT catch a leak on the client side, because a leaked
# connection looks identical to an idle one from the server. This metric names the failure directly.
METRIC=pool_checkout_timeout
if "$GCLOUD" logging metrics describe "$METRIC" --project "$PROJECT" >/dev/null 2>&1; then
  echo "   $METRIC exists"
else
  "$GCLOUD" logging metrics create "$METRIC" --project "$PROJECT" \
    --description "Count of PoolCheckoutTimeout - the app cannot obtain a DB connection from its own pool" \
    --log-filter='resource.type="cloud_run_revision" AND (textPayload:"PoolCheckoutTimeout" OR textPayload:"database pool checkout timed out")' \
    --quiet
  echo "   created $METRIC"
fi

echo "== alert policies"
upsert() { # DISPLAY_NAME BODY   -> replace by displayName so thresholds live in git
  OLD="$(api GET alertPolicies | python3 -c '
import json, sys
d = json.load(sys.stdin)
for p in d.get("alertPolicies", []):
    if p.get("displayName") == sys.argv[1]:
        print(p["name"]); break
' "$1")"
  if [ -n "$OLD" ]; then
    R="$(curl -sS -m 60 -X DELETE -H "Authorization: Bearer $TOKEN" "https://monitoring.googleapis.com/v3/$OLD")"
    check "$R" "delete $1"
  fi
  R="$(api POST alertPolicies "$2")"
  check "$R" "policy $1"
  echo "   $1"
}

policy() { # DISPLAY_NAME DOC FILTER ALIGNER REDUCER GROUPBY COMPARISON THRESHOLD ALIGN_PERIOD DURATION AUTOCLOSE
  python3 -c '
import json, sys
(name, doc, filt, aligner, reducer, groupby, comparison,
 threshold, align_period, duration, autoclose, channel) = sys.argv[1:13]
agg = {"alignmentPeriod": align_period, "perSeriesAligner": aligner,
       "crossSeriesReducer": reducer}
if groupby:
    agg["groupByFields"] = groupby.split(",")
print(json.dumps({
  "displayName": name,
  "documentation": {"content": doc, "mimeType": "text/markdown"},
  "combiner": "OR",
  "conditions": [{
    "displayName": name,
    "conditionThreshold": {
      "filter": filt,
      "aggregations": [agg],
      "comparison": comparison,
      "thresholdValue": float(threshold),
      "duration": duration,
      "trigger": {"count": 1}}}],
  "notificationChannels": [channel],
  "alertStrategy": {"autoClose": autoclose}}))' "$@" "$CHANNEL"
}

upsert "prod: host is down" "$(policy \
  "prod: host is down" \
  "An uptime check against a public pivota.cc host is failing. This is the only alert that fires when the whole path breaks - DNS, load balancer, TLS or the service itself." \
  'metric.type="monitoring.googleapis.com/uptime_check/check_passed" AND resource.type="uptime_url"' \
  ALIGN_FRACTION_TRUE REDUCE_MEAN resource.label.host COMPARISON_LT 0.6 300s 300s 3600s)"

upsert "prod: TLS certificate expiring" "$(policy \
  "prod: TLS certificate expiring" \
  "A certificate is within 14 days of expiry. These are Certificate-Manager MANAGED certs that renew automatically, so this firing means renewal is BLOCKED - almost always because an _acme-challenge CNAME was removed from the pivota.cc zone. Fix the DNS authorization before the cert actually expires." \
  'metric.type="monitoring.googleapis.com/uptime_check/time_until_ssl_cert_expires" AND resource.type="uptime_url"' \
  ALIGN_MIN REDUCE_MIN resource.label.host COMPARISON_LT 14 3600s 3600s 86400s)"

# 0.2/s was UNREACHABLE and so this policy had never fired once. It asks for 12 5xx per second on
# an API whose baseline is roughly 2 requests per MINUTE (~0.03/s measured 2026-08-28) - a total
# outage returning 5xx to every single caller still tops out an order of magnitude below the
# threshold. A rate threshold has to be set against real traffic, not a round number.
#
# 0.01/s over 600s needs SEVEN errors in ten minutes — COMPARISON_GT is strict and 6/600 is exactly
# 0.01, so six does not trip it. Against a ~20-request baseline that is a ~35% error rate: high enough
# not to trip on one unhandled exception, low enough that a wedged service cannot hide under it. Revisit if traffic grows by an order of magnitude.
upsert "prod: load balancer 5xx" "$(policy \
  "prod: load balancer 5xx" \
  "The external load balancer is returning 5xx. This is the user-visible symptom of most backend failures - a revision that will not start, database saturation, or an unhandled exception." \
  'metric.type="loadbalancing.googleapis.com/https/request_count" AND resource.type="https_lb_rule" AND metric.label.response_code_class="500"' \
  ALIGN_RATE REDUCE_SUM "" COMPARISON_GT 0.01 600s 600s 3600s)"

upsert "prod: Cloud Run job failing" "$(policy \
  "prod: Cloud Run job failing" \
  "A scheduled Cloud Run job task is failing. reviews-invitation-send runs every minute and relgraph-sync daily; a persistent failure in either is otherwise completely silent." \
  'metric.type="run.googleapis.com/job/completed_task_attempt_count" AND resource.type="cloud_run_job" AND metric.label.result="failed"' \
  ALIGN_SUM REDUCE_SUM resource.label.job_name COMPARISON_GT 0 300s 300s 3600s)"

upsert "prod: Cloud SQL connections high" "$(policy \
  "prod: Cloud SQL connections high" \
  "Postgres backends are above 80% of max_connections (300). This codebase has wedged on pool exhaustion before - see the 2026-08-20 sitemap incident, where a plan built without statistics opened enough connections to exhaust the pool. Look for a stuck query or a revision that will not scale down." \
  'metric.type="cloudsql.googleapis.com/database/postgresql/num_backends" AND resource.type="cloudsql_database"' \
  ALIGN_MAX REDUCE_SUM "" COMPARISON_GT 240 300s 300s 3600s)"

upsert "prod: database pool exhausted" "$(policy \
  "prod: database pool exhausted" \
  "The backend cannot get a connection from its own pool (PoolCheckoutTimeout). Check Cloud SQL num_backends FIRST: if it is low - it was 28/300 on 2026-08-28 - the database is fine and the application is leaking pool slots, so restarting the revision restores service while the leak is found. There is no liveness probe on the web service, so a wedged instance is never recycled on its own." \
  'metric.type="logging.googleapis.com/user/pool_checkout_timeout" AND resource.type="cloud_run_revision"' \
  ALIGN_SUM REDUCE_SUM resource.label.service_name COMPARISON_GT 0 300s 300s 3600s)"

echo
echo "channel : $ALERT_EMAIL"
api GET alertPolicies | python3 -c '
import json, sys
d = json.load(sys.stdin)
ps = d.get("alertPolicies", [])
print("policies:", len(ps))
for p in ps:
    print("   -", p.get("displayName"), "| enabled:", p.get("enabled"))
'
api GET uptimeCheckConfigs | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("uptime  :", len(d.get("uptimeCheckConfigs", [])))
'

# A warning on stderr is only a result if a human is standing there to read it.
# This script is exactly the kind of thing that gets wrapped in CI or a runbook
# step, and an undeliverable channel means every policy above is decoration — so
# say it with the exit code too. Deliberately LAST: the policies are still
# created and reported first, because a half-configured project is worse than a
# fully configured one that reports a problem.
if [ "${CHANNEL_UNDELIVERABLE:-0}" = 1 ]; then
  echo "FAILED: alerts are configured but the channel cannot receive them." >&2
  exit 1
fi
