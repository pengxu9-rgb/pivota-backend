#!/usr/bin/env bash
# Edge rate limiting for the public API.
#   infra/gcp/setup_cloud_armor.sh prod            # create/update, rules in PREVIEW (default)
#   ENFORCE=1 infra/gcp/setup_cloud_armor.sh prod  # promote the rate limit to actually block
#
# The 2026-08-22 audit (SEV-2.2) found no Cloud Armor policy at all, and an application rate
# limiter that returns early unless the path starts with /agent/ (middleware/rate_limiter.py) -
# so of 1,011 published paths, everything outside that one prefix had no rate limiting at any
# layer. That included the unauthenticated credential-writing routes fixed in #1818.
#
# THE THRESHOLD IS MEASURED, NOT GUESSED. Over a 6h window on 2026-08-24, excluding this
# project's own uptime checks, real traffic was 317 requests across 32 client IPs. Peak per-IP
# rate was 52 req/min (one browser session); every other client was <= 16 req/min. 600/min is
# roughly 11x the observed peak - high enough that no current caller can reach it, low enough to
# stop a flood.
#
# RULES SHIP IN PREVIEW. This fronts a live payments API, and the partner integration has not
# happened yet, so the traffic profile is about to change in a way nobody can predict. Preview
# logs what WOULD have been denied without denying it. Review the log (command printed at the
# end), confirm no legitimate caller is being caught, then re-run with ENFORCE=1.
set -euo pipefail

ENV="${1:-}"
case "$ENV" in
  prod) PROJECT=pivota-prod; BACKENDS=(pivota-bes-web pivota-bes-gateway) ;;
  *) echo "usage: $0 prod   (staging has no external LB)" >&2; exit 2 ;;
esac

GCLOUD="${GCLOUD:-gcloud}"
POLICY=pivota-edge-protection
RATE_PER_MIN="${RATE_PER_MIN:-600}"
PREVIEW_FLAG="--preview"
MODE="PREVIEW (logs only, blocks nothing)"
if [ "${ENFORCE:-0}" = "1" ]; then PREVIEW_FLAG="--no-preview"; MODE="ENFORCING (returns 429)"; fi

echo "== policy $POLICY on $PROJECT  [$MODE]"

if ! "$GCLOUD" compute security-policies describe "$POLICY" --project "$PROJECT" >/dev/null 2>&1; then
  "$GCLOUD" compute security-policies create "$POLICY" --project "$PROJECT" \
    --description="Edge rate limiting for the public API (audit SEV-2.2)." >/dev/null
  echo "   created"
else
  echo "   exists"
fi

# Rule 1000: per-IP rate limit. enforce-on-key=IP so one abusive client cannot deny service to
# everyone else - the failure mode of a global counter behind a load balancer, where every request
# shares the LB's own address unless the key is set explicitly.
if "$GCLOUD" compute security-policies rules describe 1000 --security-policy="$POLICY" \
     --project "$PROJECT" >/dev/null 2>&1; then
  ACTION=update
else
  ACTION=create
fi
"$GCLOUD" compute security-policies rules "$ACTION" 1000 \
  --project "$PROJECT" --security-policy="$POLICY" \
  --description="Per-IP rate limit ${RATE_PER_MIN}/min. Threshold measured, not guessed: observed peak was 52/min from a browser session, all other clients <=16/min." \
  --src-ip-ranges="*" \
  --action=throttle \
  --rate-limit-threshold-count="$RATE_PER_MIN" \
  --rate-limit-threshold-interval-sec=60 \
  --conform-action=allow \
  --exceed-action=deny-429 \
  --enforce-on-key=IP \
  $PREVIEW_FLAG >/dev/null
echo "   rule 1000: ${RATE_PER_MIN}/min per IP, exceed=429, ${MODE%% *}"

for b in "${BACKENDS[@]}"; do
  cur="$("$GCLOUD" compute backend-services describe "$b" --global --project "$PROJECT" \
        --format='value(securityPolicy.basename())' 2>/dev/null || true)"
  if [ "$cur" = "$POLICY" ]; then
    echo "   $b already attached"
  else
    "$GCLOUD" compute backend-services update "$b" --global --project "$PROJECT" \
      --security-policy="$POLICY" >/dev/null
    echo "   $b attached"
  fi
done

cat <<EOF

Mode: $MODE

Cloud Armor changes take a few MINUTES to reach every edge. A burst immediately after applying
this will pass unthrottled and prove nothing - verified the hard way: a 700-request burst ~5
minutes in produced zero preview denials, while an identical test after propagation produced 889.

Review what the rule WOULD have blocked:

  gcloud logging read 'resource.type="http_load_balancer"
    AND jsonPayload.previewSecurityPolicy.outcome="DENY"' \\
    --project $PROJECT --freshness=24h --limit=50 \\
    --format="value(timestamp,httpRequest.remoteIp,httpRequest.requestUrl)"

Confirm nothing legitimate appears there - especially after the partner integration starts, since
an agent's traffic profile is nothing like a browser's - then enforce:

  ENFORCE=1 infra/gcp/setup_cloud_armor.sh prod

To roll back instantly, detach without deleting anything:

  gcloud compute backend-services update pivota-bes-web --global --project $PROJECT \\
    --security-policy=""
EOF
