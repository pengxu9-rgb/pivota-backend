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

# Public hostname checks belong only to prod. Staging must never probe prod.
HOSTS=()
if [ "$ENV" = prod ]; then
  HOSTS=(api.pivota.cc gateway.pivota.cc mcp.pivota.cc commerce.mcp.pivota.cc ucp.pivota.cc acp.pivota.cc)
fi

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
print(json.dumps({"type": "email", "displayName": "pivota " + sys.argv[2] + " alerts",
                  "labels": {"email_address": sys.argv[1]}, "enabled": True}))' "$ALERT_EMAIL" "$ENV")"
  RESP="$(api POST notificationChannels "$BODY")"
  check "$RESP" "create channel"
  CHANNEL="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])' <<<"$RESP")"
  echo "   created $CHANNEL"
fi

# Validate the actual resource after BOTH create and reuse. Explicit UNVERIFIED
# requires verification; omitted/UNSPECIFIED can mean verification is not required.
# Neither a usable API state nor VERIFIED establishes delivery of the current alert:
# a controlled notification and confirmation from the intended recipient are still required.
# https://docs.cloud.google.com/monitoring/api/ref_v3/rest/v3/projects.notificationChannels
#
# `${CHANNEL#projects/*/}` is a SHORTEST-prefix strip: `*` cannot swallow the
# second `/` because the character after `projects/` is `p`, so this yields
# `notificationChannels/<id>`, which `api()` prefixes with the project path. `##`
# would yield a bare id and GET a 404, making the warning fire unconditionally
# and training the reader to ignore it. A test pins the single `#`.
if ! CHANNEL_RESPONSE="$(api GET "${CHANNEL#projects/*/}")"; then
  echo "FAILED: notification channel GET failed; delivery readiness is unknown." >&2
  exit 1
fi
CHANNEL_VERIFIED="$(python3 -c '
import json, sys
raw = sys.stdin.read().strip()
try:
    channel = json.loads(raw)
except ValueError:
    sys.exit("FAILED: notification channel GET returned missing or malformed JSON.")
if not isinstance(channel, dict) or "error" in channel:
    sys.exit("FAILED: notification channel GET returned an invalid resource or API error.")
if channel.get("name") != sys.argv[1] or channel.get("type") != "email":
    sys.exit("FAILED: notification channel GET returned the wrong resource or channel type.")
if channel.get("enabled") is not True:
    sys.exit("FAILED: notification channel is disabled or its enabled state is missing.")
labels = channel.get("labels")
if not isinstance(labels, dict) or labels.get("email_address") != sys.argv[2]:
    sys.exit("FAILED: notification channel recipient does not match ALERT_EMAIL.")
status = channel.get("verificationStatus", "VERIFICATION_STATUS_UNSPECIFIED")
if status not in ("VERIFIED", "UNVERIFIED", "VERIFICATION_STATUS_UNSPECIFIED"):
    sys.exit("FAILED: notification channel verification state is invalid or unsupported.")
print(status)
' "$CHANNEL" "$ALERT_EMAIL" <<<"$CHANNEL_RESPONSE")"
if [ "$CHANNEL_VERIFIED" = UNVERIFIED ]; then
  echo "   WARNING: channel is UNVERIFIED and requires verification before it can function." >&2
  echo "   Complete the channel verification process, then confirm actual alert receipt." >&2
  echo "FAILED: notification channel cannot receive alerts; no policy reconciliation was performed." >&2
  exit 1
elif [ "$CHANNEL_VERIFIED" = VERIFICATION_STATUS_UNSPECIFIED ]; then
  echo "   Channel verification status is omitted/UNSPECIFIED; verification may not be required."
fi
echo "   Channel API state does not prove alert delivery. Confirm a controlled notification"
echo "   with the intended recipient before marking delivery readiness complete."

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

# The retailer ingest drain (jobs/retailer_ingest_drain.py) prints ONE summary line per STAGE (an
# execution may run several back to back), sort_keys JSON with json.dumps' default ", " / ": "
# separators (the filters match that spacing), carrying the lane's job counts by status on EVERY
# line, idle ones included:
#   retailer_ingest_drain: {"job_id": "rij_...", "jobs": {"held": 1, "queued": 3}, "outcome": "held", ...}
#
# These are the two conditions the "Cloud Run job failing" policy CANNOT see: the stage was
# recorded, so the execution exits 0. An unexpected exception is NOT here - that exits 1 and is the
# job-failing policy's.
#
#   HELD is a STANDING condition, so it keys on the count (`"held": N`, N >= 1), not only on the
#   stage that produced it: a store waiting for review keeps matching on every tick, idle ticks
#   included, until someone approves or cancels it. `"outcome": "held"` is kept as an alternative so
#   the stage that holds a store alerts even if its line's count predates the transition. The regex
#   cannot match `"outcome": "held"` or `"status": "held"` (a string, not a digit, follows the
#   colon), nor anything inside a reason string (json.dumps escapes those quotes).
#   FAILED is per EVENT (`"status": "failed"` on the stage that gave up). A failed job is terminal
#   and stays failed, so a count-based alert would page forever.
#
# Created OR UPDATED, unlike pool_checkout_timeout above: these filters are keyed on the job's own
# output format, so a change to either side has to reach the live metric on the next run.
RID_HELD_FILTER='resource.type="cloud_run_job" AND resource.labels.job_name="retailer-ingest-drain" AND textPayload:"retailer_ingest_drain: " AND (textPayload=~"\"held\": [1-9]" OR textPayload:"\"outcome\": \"held\"")'
RID_FAILED_FILTER='resource.type="cloud_run_job" AND resource.labels.job_name="retailer-ingest-drain" AND textPayload:"retailer_ingest_drain: " AND textPayload:"\"status\": \"failed\""'
upsert_log_metric() { # NAME DESCRIPTION FILTER
  if "$GCLOUD" logging metrics describe "$1" --project "$PROJECT" >/dev/null 2>&1; then
    "$GCLOUD" logging metrics update "$1" --project "$PROJECT" --description "$2" --log-filter="$3" --quiet
    echo "   updated $1"
  else
    "$GCLOUD" logging metrics create "$1" --project "$PROJECT" --description "$2" --log-filter="$3" --quiet
    echo "   created $1"
  fi
}
upsert_log_metric retailer_ingest_drain_held \
  "Retailer ingest drain summary lines (one per stage) that saw a HELD job (a store needs review before it can be applied)" \
  "$RID_HELD_FILTER"
upsert_log_metric retailer_ingest_drain_failed \
  "Retailer ingest drain stages that left a job FAILED - the ledger gave up on that store" \
  "$RID_FAILED_FILTER"

# The Reap agentic purchase rail (jobs/reap_agentic_purchase_poll.py) is a scheduler job on the
# `worker` SERVICE, not a Cloud Run job: it has no task to fail, so "prod: Cloud Run job failing"
# cannot see it, and Cloud Monitoring cannot read the ledger. Everything below keys on the lines
# that job writes. Read on staging 2026-10-01, where the rail is armed - these are whole
# textPayloads, and NEITHER carries a severity:
#   stdout  [2026-10-01 09:39:48,028] INFO - reap_agentic_poll: PollReport(requeued=0, ..., errors=0, skipped_disabled=0, duration_ms=19)
#   stderr  reap_agentic: purchase=rp_... state=needs_enrollment held at enrollment_pending, retry in 30s
# The first is the per-run report, through utils.logger's own stdout handler. The second is what
# every MODULE logger's WARNING/ERROR looks like on this service: nothing configures the root
# logger, so the line leaves through Python's last-resort handler as the bare message on stderr -
# no level name in the text and no severity on the entry. A filter with `severity>=ERROR` would
# be an enabled metric that can never count; these match text only.
#
#   REPORT   every report line. A report is printed by every completed maintenance tick, including missing
#            credentials and disabled provider reads. This is job/retention heartbeat, not arming. It is the input of the "went silent" policy below, not an alert itself.
#   STUCK    a report whose `stuck_over_age` is >= 1. A STANDING condition: every completed maintenance tick
#            repeats it until the purchase moves. `[1-9]` cannot match `stuck_over_age=0`, nor the
#            `stuck_over_age=-1` the job prints when the count could not be taken - that tick is
#            FAILING's (errors=1), not a claim that nothing is stuck.
#   FAILING  (a) a report with `errors` >= 1 - anchored `, errors=` so it is that field and not
#            the tail of some later one; (b) ANY other `reap_agentic_poll: ` line - with the root
#            logger at WARNING the only ones that survive are the job's WARNING and ERROR lines
#            (a raised step, a claim that outlived the loop, 'processing' past the attempt
#            ceiling, a bad dial, a budget spent before the claim); (c) the scheduler runner
#            saying this job's run blew its deadline or would not unwind. The runner quotes the
#            job id with repr(), hence the `.` either side of it.
#
#            A DEPLOY THAT LANDS MID-STEP MUST NOT PAGE, and it writes up to 2 + N lines for a
#            run holding N claims. Two are excluded by text, one is simply not matched:
#              `released on cancellation`  the job's own line, one per claim it was interrupted
#                                          holding. Its `finally` releases them on a cancel
#                                          exactly as it does a leftover - but a leftover on a
#                                          run that FINISHED its loop is a bug and keeps its own
#                                          ERROR text (`STILL CLAIMED after the loop`), which
#                                          this still matches, as does the report's errors=N.
#              `wrapper cancelled`         the runner abandoning the in-flight run.
#              `Job "..." raised an exception`  APScheduler, for the CancelledError that follows.
#            The first of these was missed in the first cut of this filter: measured by review,
#            a real poller cancelled through the real runner with three claimed rows matched 3
#            lines. A run cancelled at its DEADLINE writes the same per-claim lines, and pages
#            anyway through the runner's own `exceeded its ... run deadline` line (c). A run
#            that raises on every tick prints no report, and that is the "went silent" policy's.
REAP_POLL_REPORT_FILTER='resource.type="cloud_run_revision" AND resource.labels.service_name="worker" AND textPayload:"reap_agentic_poll: PollReport("'
REAP_POLL_STUCK_FILTER='resource.type="cloud_run_revision" AND resource.labels.service_name="worker" AND textPayload:"reap_agentic_poll: PollReport(" AND textPayload=~"stuck_over_age=[1-9]"'
REAP_POLL_FAILING_FILTER='resource.type="cloud_run_revision" AND resource.labels.service_name="worker" AND ((textPayload:"reap_agentic_poll: " AND (textPayload=~", errors=[1-9]" OR NOT textPayload:"PollReport(") AND NOT textPayload:"released on cancellation") OR (textPayload=~"scheduler job .reap_agentic_purchase_poll." AND NOT textPayload:"wrapper cancelled"))'
upsert_log_metric reap_agentic_poll_report \
  "Reap agentic purchase poller report lines - one per completed maintenance run, including provider/create pauses" \
  "$REAP_POLL_REPORT_FILTER"
upsert_log_metric reap_agentic_poll_stuck \
  "Reap agentic purchase poller reports that counted a purchase stuck more than 30 minutes past its deadline" \
  "$REAP_POLL_STUCK_FILTER"
upsert_log_metric reap_agentic_poll_failing \
  "Reap agentic purchase poller failures - a report with errors, any warning or error line of the job, or its run deadline exceeded" \
  "$REAP_POLL_FAILING_FILTER"

# Separate queues: neither is an ordinary stuck purchase or a transient poll failure.
REAP_POLL_HUMAN_FILTER='resource.type="cloud_run_revision" AND resource.labels.service_name="worker" AND textPayload:"reap_agentic_poll: PollReport(" AND textPayload=~"checkout_needs_human=[1-9]"'
REAP_POLL_CONTACT_FILTER='resource.type="cloud_run_revision" AND resource.labels.service_name="worker" AND textPayload:"reap_agentic_poll: PollReport(" AND textPayload=~"contact_retention_blocked=[1-9]"'
upsert_log_metric reap_agentic_poll_human \
  "Reap checkout outcomes requiring authenticated human reconciliation" \
  "$REAP_POLL_HUMAN_FILTER"
upsert_log_metric reap_agentic_poll_contact \
  "Reap precheckout purchases held after buyer contact retention elapsed" \
  "$REAP_POLL_CONTACT_FILTER"

echo "== alert policies"
# Two replies used to be read as good news, and both now ABORT where the run previously went on:
#   * an ERROR DOCUMENT from the list. `d.get("alertPolicies", [])` over {"error": ...} is an empty
#     list, i.e. "this policy does not exist yet" - so the create ran without the delete and left
#     a second policy under the same displayName.
#   * a create reply with NO POLICY NAME in it. check() passes an empty body (curl exit 0, nothing
#     written), so the predecessor was deleted and nothing had replaced it.
# Neither changes what is sent: the bodies come from policy()/promql_policy() and are untouched.
require_policy_name() { # JSON LABEL   -> abort unless the reply is a created policy
  python3 -c '
import json, sys
raw = sys.stdin.read().strip()
label = sys.argv[1]
try:
    d = json.loads(raw)
except ValueError:
    d = None
name = d.get("name") if isinstance(d, dict) else None
if not (isinstance(name, str) and "/alertPolicies/" in name):
    sys.exit(label + " FAILED: the reply carries no policy name, so nothing was created: "
             + (raw[:200] or "an empty reply"))
' "$2" <<<"$1"
}

# Policy generators retain the exact prod payloads. Normalize at BOTH write paths
# so staging cannot inherit prod names, including the new-metric retry path.
environment_policy_name() {
  printf '%s:%s' "$ENV" "${1#prod:}"
}
environment_policy_body() {
  if [ "$ENV" = prod ]; then printf '%s' "$1"; return; fi
  python3 -c '
import json, sys
body = json.loads(sys.argv[1])
for entry in [body, *body.get("conditions", [])]:
    name = entry.get("displayName", "")
    if name.startswith("prod:"):
        entry["displayName"] = sys.argv[2] + name[5:]
print(json.dumps(body))' "$1" "$ENV:"
}

upsert() { # DISPLAY_NAME BODY   -> replace by displayName so thresholds live in git
  set -- "$(environment_policy_name "$1")" "$(environment_policy_body "$2")"
  OLD="$(api GET alertPolicies | python3 -c '
import json, sys
d = json.load(sys.stdin)
if not isinstance(d, dict) or "error" in d:
    sys.exit("list policies FAILED: " + json.dumps(d)[:200])
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
  require_policy_name "$R" "policy $1"
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

# RENOTIFY (optional, e.g. 3600s) re-sends the notification for an incident that is still open:
# alertStrategy.notificationChannelStrategy[].renotifyInterval, which the API bounds to between 30
# minutes and 24 hours. Omitted, the JSON is byte for byte what it was before the argument existed
# - upsert() deletes and re-creates, so a field added to every policy here would land on the live
# load-balancer policy on the next run.
#
# NEW_METRIC (optional, the literal word) sets disableMetricValidation on the condition. A PromQL
# policy is otherwise refused unless every metric it names already has a descriptor, and a log
# metric this script created seconds ago may not have one yet; with the check off the policy is
# accepted, evaluates to nothing until the metric exists, and to data after. Use it only for a
# metric this script creates, and keep the name under test: the API will no longer catch a typo.
promql_policy() { # DISPLAY_NAME CONDITION_NAME DOC QUERY DURATION EVAL_INTERVAL AUTOCLOSE [RENOTIFY [NEW_METRIC]]
  python3 -c '
import json, sys
args = sys.argv[1:]
channel = args.pop()
(name, condition_name, doc, query, duration, evaluation_interval, autoclose) = args[:7]
condition = {"query": query, "duration": duration, "evaluationInterval": evaluation_interval}
strategy = {"autoClose": autoclose}
if len(args) > 7:
    strategy["notificationChannelStrategy"] = [
        {"notificationChannelNames": [channel], "renotifyInterval": args[7]}]
if len(args) > 8:
    if args[8] != "NEW_METRIC":
        sys.exit("promql_policy: the ninth argument can only be NEW_METRIC, got " + repr(args[8]))
    condition["disableMetricValidation"] = True
print(json.dumps({
  "displayName": name,
  "documentation": {"content": doc, "mimeType": "text/markdown"},
  "combiner": "OR",
  "conditions": [{
    "displayName": condition_name,
    "conditionPrometheusQueryLanguage": condition}],
  "notificationChannels": [channel],
  "alertStrategy": strategy}))' "$@" "$CHANNEL"
}

# A policy over a log metric THIS RUN may have created seconds ago. Monitoring rejects such a
# policy until the metric's descriptor is visible to it - "Cannot find metric(s) that match type
# ... If a metric was created recently, it could take up to 10 minutes to become available" - and
# through upsert() that rejection would abort the whole run from check(). That wording is the
# API's as it has been reported, NOT captured by running this script: the match below is on two
# fragments of it, and if the real text carries neither, the first run stops at check() with the
# API's own message after every upsert() above has completed. Re-running ten minutes later is the
# remedy either way. So, for these only:
#
#   * CREATE FIRST, DELETE AFTER. upsert() deletes the old policy and then posts the new one, so a
#     rejected post leaves NO policy. Here the old one (every one carrying this displayName - a
#     run that died between the two steps leaves a pair) is removed only once its replacement
#     exists, so a failed run leaves whatever was live exactly as it was.
#   * THAT ONE REJECTION is waited out, NEW_METRIC_RETRY_SECONDS at a time, on a budget of
#     NEW_METRIC_TRIES shared by every policy that goes through here (10 minutes by default, not
#     10 minutes each). ANY OTHER API error still aborts through check(), as everywhere else.
#   * THE LIST MUST BE A LIST AND THE CREATE MUST NAME A POLICY, as in upsert() - an error
#     document read as "no predecessors" leaves a duplicate, and an empty create reply read as
#     success deletes the predecessor of a policy that was never made. Both abort.
#   * OUT OF BUDGET, the policy is DEFERRED, not fatal: its name is kept, the run carries on to
#     the summary, and the script exits 1 at the very end saying which policies to re-run for.
#     Every policy posted through upsert() above has already been written by then.
NEW_METRIC_TRIES="${NEW_METRIC_TRIES:-20}"
NEW_METRIC_RETRY_SECONDS="${NEW_METRIC_RETRY_SECONDS:-30}"
NEW_METRIC_DEFERRED=""
upsert_on_new_metric() { # DISPLAY_NAME BODY
  set -- "$(environment_policy_name "$1")" "$(environment_policy_body "$2")"
  local stale old resp err
  stale="$(api GET alertPolicies | python3 -c '
import json, sys
d = json.load(sys.stdin)
if not isinstance(d, dict) or "error" in d:
    sys.exit("list policies FAILED: " + json.dumps(d)[:200])
for p in d.get("alertPolicies", []):
    if p.get("displayName") == sys.argv[1]:
        print(p["name"])
' "$1")"
  while :; do
    resp="$(api POST alertPolicies "$2")"
    err="$(python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read() or "{}")
except ValueError:
    d = {}
if isinstance(d, dict) and "error" in d:
    print(d["error"].get("message", ""))
' <<<"$resp")"
    case "$err" in
      *"Cannot find metric"*|*"could take up to 10 minutes"*)
        if [ "$NEW_METRIC_TRIES" -le 0 ]; then
          NEW_METRIC_DEFERRED="${NEW_METRIC_DEFERRED}${NEW_METRIC_DEFERRED:+, }$1"
          echo "   DEFERRED $1 - its log metric is not visible to Monitoring yet" >&2
          return 0
        fi
        NEW_METRIC_TRIES=$((NEW_METRIC_TRIES - 1))
        echo "   $1: log metric not visible to Monitoring yet, retrying in ${NEW_METRIC_RETRY_SECONDS}s"
        sleep "$NEW_METRIC_RETRY_SECONDS"
        ;;
      *) break ;;
    esac
  done
  check "$resp" "policy $1"
  # Nothing is deleted until the reply NAMES the policy it created.
  require_policy_name "$resp" "policy $1"
  for old in $stale; do
    resp="$(api DELETE "${old#projects/*/}")"
    check "$resp" "delete $1"
  done
  echo "   $1"
}

if [ "${#HOSTS[@]}" -gt 0 ]; then
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
fi

# Preserve the 2026-09-25 hand-edited ratio: absolute error rate does not track changing traffic.
# upsert deletes/re-creates by displayName, so reconciling a new policy must not revert this one.
upsert "prod: load balancer 5xx" "$(promql_policy \
  "prod: load balancer 5xx" \
  "LB 5xx ratio > 1% over 10m" \
  "The external load balancer is returning 5xx. This is the user-visible symptom of most backend failures - a revision that will not start, database saturation, or an unhandled exception." \
  'sum(rate(loadbalancing_googleapis_com:https_request_count{monitored_resource="https_lb_rule",response_code_class="500"}[10m])) / sum(rate(loadbalancing_googleapis_com:https_request_count{monitored_resource="https_lb_rule"}[10m])) > 0.01' \
  600s 60s 3600s)"

upsert "prod: Cloud Run job failing" "$(policy \
  "prod: Cloud Run job failing" \
  "A scheduled Cloud Run job task is failing. reviews-invitation-send runs every minute and relgraph-sync daily; a persistent failure in either is otherwise completely silent. For retailer-ingest-drain, a failure here is an UNEXPECTED exception (the run is recorded as outcome=error in retailer_ingest_runs); stages that end held or failed exit 0 and page through their own retailer-ingest policies instead." \
  'metric.type="run.googleapis.com/job/completed_task_attempt_count" AND resource.type="cloud_run_job" AND metric.label.result="failed"' \
  ALIGN_SUM REDUCE_SUM resource.label.job_name COMPARISON_GT 0 300s 300s 3600s)"

# A 300s alignment covers several 60s samples; empty windows must not reset the duration timer.
# This is continuous job occupancy (running_executions > 0 for 2h), not per-execution age: one wedged
# run or runs chained back to back both fire it; two short overlapping runs do not.
upsert "prod: relgraph-sync running over two hours" "$(policy \
  "prod: relgraph-sync running over two hours" \
  "relgraph-sync has running executions continuously for two hours. Today's daily run takes ~37 minutes (build ~8, sequential review ~29); the inner step timeout is 45 minutes. Task timeout is 14400s with max-retries 1, so a wedged task can remain alive well after the expected run. It fires on continuous occupancy of two hours or more, whether one wedged run or runs chained back to back; two short overlapping runs do not trip it. Sampling/visibility lag can delay the alert several minutes." \
  'metric.type="run.googleapis.com/job/running_executions" AND resource.type="cloud_run_job" AND resource.label.job_name="relgraph-sync"' \
  ALIGN_MAX REDUCE_SUM resource.label.job_name COMPARISON_GT 0 300s 7200s 14400s)"

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

# DURATION 0s, not the 300s the pool policy uses: a log-based counter writes no points between
# matching lines, so a condition that must hold for 300s can miss a lone event entirely.
#
# HELD aligns over 3600s, TWICE the drain's 30-minute cadence. While a store is held every tick
# matches, so the hour-wide sum never drops to 0 between ticks and this stays ONE open incident
# rather than a fresh email every half hour; it closes about an hour after the last held job is
# approved or cancelled. It also closes if the trigger is PAUSED - no executions, no lines - so a
# disarmed drain with a held store is silent: check the ledger before disarming.
# FAILED is a one-off event and keeps the 300s window.
upsert "prod: retailer ingest held for review" "$(policy \
  "prod: retailer ingest held for review" \
  "At least one retailer ingest job is HELD: a BLOCK flag stopped the store and nothing is written until someone decides. This stays open while any job is held. Read the flags: \`SELECT id, domain, brand, status_reason FROM retailer_ingest_jobs WHERE status = 'held'\` and the job's latest retailer_ingest_runs row (checks, flags). Then approve (optionally excluding handles or accepting flag keys) or cancel it via db/retailer_ingest.py approve()/cancel()." \
  'metric.type="logging.googleapis.com/user/retailer_ingest_drain_held" AND resource.type="cloud_run_job"' \
  ALIGN_SUM REDUCE_SUM resource.label.job_name COMPARISON_GT 0 3600s 0s 3600s)"

upsert "prod: retailer ingest job failed" "$(policy \
  "prod: retailer ingest job failed" \
  "A retailer-ingest-drain stage left a job FAILED: crawl capped or failed, currency unproven, retry budget spent, apply refused (MAY BE PARTIAL), or the apply gate / read-back disagreed. The execution itself exited 0, so the Cloud Run job-failing policy does not fire for this. Read \`SELECT id, domain, brand, status_reason FROM retailer_ingest_jobs WHERE status = 'failed' ORDER BY updated_at DESC\` and the job's latest retailer_ingest_runs row." \
  'metric.type="logging.googleapis.com/user/retailer_ingest_drain_failed" AND resource.type="cloud_run_job"' \
  ALIGN_SUM REDUCE_SUM resource.label.job_name COMPARISON_GT 0 300s 0s 3600s)"

# The Reap agentic purchase rail. Three policies over the three log metrics above; the runbook
# for all of them is docs/runbooks/reap_agentic_purchase.md, "Alerts".
#
# STUCK aligns over 900s. The armed poller reports every 30s, but one run may legitimately take
# most of its 600s run deadline, and the window must outlast the longest gap between two reports
# or a standing condition becomes a fresh incident per slow tick. DURATION 0s for the reason the
# drain policies give: a log counter writes no points between matching lines.
upsert_on_new_metric "prod: Reap purchase stuck over 30 minutes" "$(policy \
  "prod: Reap purchase stuck over 30 minutes" \
  "The Reap purchase poller reported stuck_over_age >= 1: at least one purchase is more than 30 minutes past the last moment the rail's own rules allow it to stay in its state. In quoting, processing and resolving that is 30 minutes in the state - and processing means the buyer APPROVED and the payment has no outcome yet. A buyer still on a live hosted page is NOT counted: needs_enrollment and awaiting_approval count only when the expire sweep should have taken the row 30 minutes ago. Find them: \`SELECT id, state, state_entered_at, last_error_code, attempts, claimed_by, hosted_url_expires_at FROM reap_agentic_purchases WHERE state IN ('resolving','needs_enrollment','quoting','awaiting_approval','processing') ORDER BY state_entered_at\`. Do NOT fail a processing row before reconciling its checkout with Reap. This stays open while any purchase is stuck. Runbook: docs/runbooks/reap_agentic_purchase.md, Alerts." \
  'metric.type="logging.googleapis.com/user/reap_agentic_poll_stuck" AND resource.type="cloud_run_revision"' \
  ALIGN_SUM REDUCE_SUM resource.label.service_name COMPARISON_GT 0 900s 0s 3600s)"

upsert_on_new_metric "prod: Reap purchase poller failing" "$(policy \
  "prod: Reap purchase poller failing" \
  "The Reap purchase poller on the worker service logged a failure: a report line with errors >= 1, a warning or error line of its own (advance RAISED, a claim STILL CLAIMED after the loop, purchases in processing at or past max_attempts, a count that could not be taken, a dial outside its bounds, the run budget spent before the claim), or the scheduler cancelled its run at the 600s deadline. Read the lines: worker service logs, textPayload containing reap_agentic_poll. The messages carry purchase ids and exception TYPES only. Runbook: docs/runbooks/reap_agentic_purchase.md, Alerts and When errors > 0." \
  'metric.type="logging.googleapis.com/user/reap_agentic_poll_failing" AND resource.type="cloud_run_revision"' \
  ALIGN_SUM REDUCE_SUM resource.label.service_name COMPARISON_GT 0 300s 0s 3600s)"

# SILENT is "the heartbeat WAS there and has STOPPED", not "there is no heartbeat". The left side
# needs a report line in the 24 hours before the last 15 minutes; `unless` then drops it while the
# last 15 minutes still have one. An environment that has never been armed has no series, the left
# side is an empty vector, and the policy cannot fire. Arming the rail arms the alert; nothing has
# to be switched on with it. Checked 2026-10-01 through the PromQL query API (read-only): this
# shape answers an empty vector in pivota-prod, where REAP_AGENTIC_ENABLED is unset; and over an
# existing log metric there (retailer_ingest_drain_failed) it answers a value when the earlier
# window has lines and the later one has none, and nothing when both have them.
#
# sum_over_time, NOT increase or rate: in PromQL a log counter is a per-minute DELTA sample (the
# count for that minute, zero-filled only next to a match), not a cumulative counter, so increase()
# reads every drop as a reset - it answered 12 for a week whose samples add up to 11.
#
# THE NUMBERS, and the documentation each one leans on:
#   15m   of silence, then the 300s duration: ~20 minutes to fire. One run can take its whole 600s
#         deadline between two reports, and a log-based metric "may take up to 10 minutes" to
#         reach Monitoring (logging/docs/logs-based-metrics/troubleshooting), so a 10-minute
#         window could read empty over a healthy poller.
#   24h   of look-back, the most the platform allows here: "You can only alert on the most recent
#         25 hours of data for ... user-defined Log-Based metrics", and "the sum of your retest
#         window, alignment period, and any time shift caused by using the offset modifier must
#         be at most 25 hours" (monitoring/promql/promql-in-alerting). 5m + 24h + 15m = 24h20m.
#   3600s renotify: while the incident is open the email repeats hourly, so one missed message is
#         not the only page. The API allows 30 minutes to 24 hours (AlertStrategy.
#         NotificationChannelStrategy.renotifyInterval). No other policy in this file sets it.
#
# WHAT THIS CANNOT DO. 24 hours after the last report the left side is empty, the condition stops
# being true and the incident closes - fixed or not. From then on a dead poller produces NO signal
# from any of the three policies: the other two read lines only a running poller prints. A
# DELIBERATE disarm of an armed rail looks identical from here (the job prints nothing either way):
# it opens this once and renotifies until it is snoozed or the 24 hours run out.
upsert_on_new_metric "prod: Reap purchase poller went silent" "$(promql_policy \
  "prod: Reap purchase poller went silent" \
  "report line present in the last 24h, absent for 15m" \
  "The Reap purchase poller was printing its per-run report and has printed none for about 20 minutes. Nothing is advancing purchases: a buyer who approves now waits. Either the worker service is down or its scheduler is wedged (check /__scheduler_health for reap_agentic_purchase_poll), every run is raising before it reports (worker logs: Job run_reap_agentic_purchase_poll raised an exception), the Reap client lost its configuration so step 4 is skipped without a line, or someone turned REAP_AGENTIC_ENABLED off. This repeats hourly while it is open. If the rail was disarmed on purpose this is expected, and the incident cannot be closed by hand while the condition holds: snooze this policy instead. The condition stops being true 24 hours after the last report, fixed or not, and after that a dead poller raises nothing. It cannot fire in an environment that printed no report in the previous 24 hours. Runbook: docs/runbooks/reap_agentic_purchase.md, Alerts." \
  '(sum(sum_over_time(logging_googleapis_com:user_reap_agentic_poll_report{monitored_resource="cloud_run_revision"}[24h] offset 15m)) > 0) unless (sum(sum_over_time(logging_googleapis_com:user_reap_agentic_poll_report{monitored_resource="cloud_run_revision"}[15m])) > 0)' \
  300s 60s 3600s 3600s NEW_METRIC)"


upsert_on_new_metric "prod: Reap checkout needs human reconciliation" "$(policy \
  "prod: Reap checkout needs human reconciliation" \
  "The poller counted checkout_needs_human >= 1. A repeated permanent checkout read failure needs authenticated provider evidence and an audited operator decision. Keep the purchase nonterminal; do not create a replacement, fail an uncertain payment, or restore contact. Inspect purchase IDs and exact error categories through owner-authorized tooling. Runbook: docs/runbooks/reap_agentic_purchase.md, Checkout reads requiring human reconciliation and Audited manual checkout resolution." \
  'metric.type="logging.googleapis.com/user/reap_agentic_poll_human" AND resource.type="cloud_run_revision"' \
  ALIGN_SUM REDUCE_SUM resource.label.service_name COMPARISON_GT 0 900s 0s 3600s)"

upsert_on_new_metric "prod: Reap buyer contact retention blocked" "$(policy \
  "prod: Reap buyer contact retention blocked" \
  "The poller counted contact_retention_blocked >= 1. Contact has been scrubbed while the purchase and payment evidence remain. Resuming flags cannot restore contact or restart checkout. Inspect whether an external operation exists, then use an independently reviewed contact reauthorization or reconciliation procedure. Do not copy old PII or blindly retry. Runbook: docs/runbooks/reap_agentic_purchase.md, Worker stop and independent contact retention." \
  'metric.type="logging.googleapis.com/user/reap_agentic_poll_contact" AND resource.type="cloud_run_revision"' \
  ALIGN_SUM REDUCE_SUM resource.label.service_name COMPARISON_GT 0 900s 0s 3600s)"

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

if [ -n "$NEW_METRIC_DEFERRED" ]; then
  echo "NOT CREATED: $NEW_METRIC_DEFERRED" >&2
  echo "This run created their log metrics and Monitoring could not see them yet. Every other" >&2
  echo "policy above was written. Re-run this script in 10 minutes, with the same ALERT_EMAIL." >&2
fi

if [ -n "$NEW_METRIC_DEFERRED" ]; then
  exit 1
fi
