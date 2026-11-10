#!/usr/bin/env bash
# Apply the alert DEFINITIONS in infra/gcp/monitoring/money_path_alerts.json (today: FIX_PLAN R-03,
# the protocol-directory freshness check). Generic: it applies whatever metrics and policies the file holds.
#   ALERT_EMAIL=<the address that is actually read> infra/gcp/apply_money_path_alerts.sh prod|staging
#
# The definitions are data: infra/gcp/monitoring/money_path_alerts.json. This script is the only
# thing that sends them to Cloud Monitoring, and nothing runs it on deploy. Run setup_monitoring.sh
# first: this script REUSES the email notification channel that one creates for ALERT_EMAIL and
# refuses to run without it.
#
# What it does, in order, and what it never does:
#   1. every log metric: update if it exists, else create (gcloud logging metrics).
#   2. every alert policy, matched by displayName: PATCH the existing one in place, else create it.
#      A policy over a metric created seconds ago may be refused with "Cannot find metric(s)" for
#      up to 10 minutes: such a policy is DEFERRED (named at the end, exit 1), never half-written.
#      Re-run the script ten minutes later; it is idempotent.
#   It never deletes a metric, a policy or a channel.
#
# The same REST approach as setup_monitoring.sh (no `gcloud alpha monitoring`, whose missing
# component exits 0 with an install prompt).
set -euo pipefail

ENV="${1:-}"
case "$ENV" in
  prod)    PROJECT=pivota-prod ;;
  staging) PROJECT=pivota-staging ;;
  *) echo "usage: ALERT_EMAIL=... $0 prod|staging" >&2; exit 2 ;;
esac
[ -n "${ALERT_EMAIL:-}" ] || { echo "ALERT_EMAIL is required (the address setup_monitoring.sh pages)" >&2; exit 2; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFS="${DEFS:-$HERE/monitoring/money_path_alerts.json}"
GCLOUD="${GCLOUD:-gcloud}"
API="https://monitoring.googleapis.com/v3/projects/$PROJECT"
TOKEN="$("$GCLOUD" auth print-access-token)"

api() { # METHOD PATH [BODY]
  if [ -n "${3:-}" ]; then
    curl -sS -m 60 -X "$1" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d "$3" "$API/$2"
  else
    curl -sS -m 60 -X "$1" -H "Authorization: Bearer $TOKEN" "$API/$2"
  fi
}

echo "== notification channel ($ALERT_EMAIL)"
CHANNEL="$(api GET notificationChannels | python3 -c '
import json, sys
d = json.load(sys.stdin)
if not isinstance(d, dict) or "error" in d:
    sys.exit("channel list FAILED: " + json.dumps(d)[:200])
for c in d.get("notificationChannels", []):
    if c.get("type") == "email" and c.get("labels", {}).get("email_address") == sys.argv[1]:
        print(c["name"]); break
' "$ALERT_EMAIL")"
[ -n "$CHANNEL" ] || { echo "no email channel for $ALERT_EMAIL: run infra/gcp/setup_monitoring.sh $ENV first" >&2; exit 2; }
echo "   $CHANNEL"

echo "== log metrics"
python3 -c '
import json, sys
for m in json.load(open(sys.argv[1]))["log_metrics"]:
    print("\t".join((m["name"], m["description"], m["filter"])))
' "$DEFS" | while IFS=$'\t' read -r NAME DESC FILTER; do
  if "$GCLOUD" logging metrics describe "$NAME" --project "$PROJECT" >/dev/null 2>&1; then
    "$GCLOUD" logging metrics update "$NAME" --project "$PROJECT" --description "$DESC" --log-filter="$FILTER" --quiet
    echo "   updated $NAME"
  else
    "$GCLOUD" logging metrics create "$NAME" --project "$PROJECT" --description "$DESC" --log-filter="$FILTER" --quiet
    echo "   created $NAME"
  fi
done

echo "== alert policies"
EXISTING="$(api GET "alertPolicies?pageSize=500")"
DEFERRED=()
COUNT="$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["alert_policies"]))' "$DEFS")"
for ((i = 0; i < COUNT; i++)); do
  BODY="$(python3 -c '
import json, sys
p = json.load(open(sys.argv[1]))["alert_policies"][int(sys.argv[2])]
p["notificationChannels"] = [sys.argv[3] if c == "${CHANNEL}" else c for c in p["notificationChannels"]]
print(json.dumps(p))' "$DEFS" "$i" "$CHANNEL")"
  NAME_OF="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["displayName"])' "$BODY")"
  CURRENT="$(python3 -c '
import json, sys
d = json.loads(sys.argv[1])
if not isinstance(d, dict) or "error" in d:
    sys.exit("policy list FAILED: " + json.dumps(d)[:200])
hits = [p["name"] for p in d.get("alertPolicies", []) if p.get("displayName") == sys.argv[2]]
if len(hits) > 1:
    sys.exit("more than one policy named " + sys.argv[2] + "; resolve by hand first")
print(hits[0] if hits else "")' "$EXISTING" "$NAME_OF")"
  if [ -n "$CURRENT" ]; then
    RESP="$(api PATCH "${CURRENT#projects/*/}" "$BODY")"
    VERB=updated
  else
    RESP="$(api POST alertPolicies "$BODY")"
    VERB=created
  fi
  if python3 -c 'import json,sys; d=json.loads(sys.argv[1]); sys.exit(0 if isinstance(d, dict) and d.get("name") else 1)' "$RESP"; then
    echo "   $VERB $NAME_OF"
  elif grep -q -e "Cannot find metric" -e "could take up to 10 minutes" <<<"$RESP"; then
    echo "   DEFERRED $NAME_OF (its metric is not visible to Monitoring yet)"
    DEFERRED+=("$NAME_OF")
  else
    echo "FAILED $NAME_OF: ${RESP:0:300}" >&2
    exit 1
  fi
done

if [ "${#DEFERRED[@]}" -gt 0 ]; then
  echo "NOT CREATED YET (re-run in 10 minutes): ${DEFERRED[*]}" >&2
  exit 1
fi
echo "done. Send a test notification for each new policy before relying on it."
