#!/usr/bin/env bash
# One-time, guarded prerequisite for an isolated crawl NAT.
#
# It narrows pivota-nat from ALL_SUBNETWORKS_ALL_IP_RANGES to the existing
# `default` subnet only, preserving the exact pivota-egress-ip reservation used
# by payment partners. Do this in staging first; production requires a reviewed
# maintenance window and an immediate egress verification from a default-subnet
# Cloud Run Job.
#
# Read-only preflight (no mutation):
#   EXPECTED_PAYMENT_EGRESS_IP=<reserved-ip> \
#     infra/gcp/migrate_payment_nat_to_default_subnet.sh staging --check
#
# Scoped migration (requires payment-path egress evidence recorded immediately
# before the change):
#   CONFIRM_PAYMENT_NAT_SCOPE=default \
#   EXPECTED_PAYMENT_EGRESS_IP=<reserved-ip> \
#   PAYMENT_PATH_EGRESS_IP=<same-reserved-ip> \
#   PAYMENT_PATH_EGRESS_VERIFIED_AT=<UTC-ISO-8601> \
#     infra/gcp/migrate_payment_nat_to_default_subnet.sh staging
#
# PAYMENT_PATH_EGRESS_* is deliberately an operator-provided attestation from
# a live, non-mutating payment-path probe. A local gcloud/curl invocation does
# not traverse the payment workload and is not sufficient evidence.
set -euo pipefail

ENV="${1:-}"
MODE="${2:-apply}"
case "$ENV" in
  staging) PROJECT=pivota-staging ;;
  prod) PROJECT=pivota-prod ;;
  *) echo "usage: $0 staging|prod [--check]" >&2; exit 2 ;;
esac
case "$MODE" in
  apply|--check) ;;
  *) echo "usage: $0 staging|prod [--check]" >&2; exit 2 ;;
esac

GCLOUD="${GCLOUD:-gcloud}"
REGION=us-west1
ROUTER=pivota-router
NAT=pivota-nat
ADDR=pivota-egress-ip
export CLOUDSDK_CORE_PROJECT="$PROJECT"

CURRENT_EGRESS_IP=$("$GCLOUD" compute addresses describe "$ADDR" --region "$REGION" --format='value(address)')
: "${EXPECTED_PAYMENT_EGRESS_IP:?set EXPECTED_PAYMENT_EGRESS_IP to the currently reserved payment egress IP}"
[ "$EXPECTED_PAYMENT_EGRESS_IP" = "$CURRENT_EGRESS_IP" ] || {
  echo "payment egress IP mismatch: expected $EXPECTED_PAYMENT_EGRESS_IP, found $CURRENT_EGRESS_IP" >&2
  exit 3
}

SUBNETS=$("$GCLOUD" compute networks subnets list --filter="region:($REGION) AND network:default" \
  --format='value(name)' | tr '\n' ',' | sed 's/,$//')
# `pivota-crawl` is the one approved additional subnet once Phase 0b has
# completed. Its presence must not make a valid, already-isolated payment NAT
# look unsafe; every other regional subnet still requires an explicit review.
case "$SUBNETS" in
  default|default,pivota-crawl) ;;
  *) echo "refusing to narrow payment NAT: expected default or default,pivota-crawl in $REGION, found: ${SUBNETS:-none}" >&2; exit 3 ;;
esac

SCOPE=$("$GCLOUD" compute routers describe "$ROUTER" --region "$REGION" \
  --format="value(nats[0].sourceSubnetworkIpRangesToNat)")

if [ "$MODE" = "--check" ]; then
  case "$SCOPE" in
    ALL_SUBNETWORKS_ALL_IP_RANGES|LIST_OF_SUBNETWORKS) ;;
    *) echo "unexpected payment NAT scope: ${SCOPE:-missing}" >&2; exit 3 ;;
  esac
  echo "PRECHECK OK: payment NAT scope=$SCOPE, reserved IP=$CURRENT_EGRESS_IP, regional subnets=$SUBNETS"
  echo "No mutation was made. Before apply, run and record a live payment-path egress probe, then set PAYMENT_PATH_EGRESS_IP and PAYMENT_PATH_EGRESS_VERIFIED_AT."
  exit 0
fi

[ "${CONFIRM_PAYMENT_NAT_SCOPE:-}" = default ] \
  || { echo "set CONFIRM_PAYMENT_NAT_SCOPE=default after reviewing payment impact" >&2; exit 2; }
: "${PAYMENT_PATH_EGRESS_IP:?set PAYMENT_PATH_EGRESS_IP from a live payment-path probe immediately before apply}"
: "${PAYMENT_PATH_EGRESS_VERIFIED_AT:?set PAYMENT_PATH_EGRESS_VERIFIED_AT to the UTC time of that probe}"
[ "$PAYMENT_PATH_EGRESS_IP" = "$CURRENT_EGRESS_IP" ] || {
  echo "payment-path egress proof mismatch: expected $CURRENT_EGRESS_IP, got $PAYMENT_PATH_EGRESS_IP" >&2
  exit 3
}

case "$SCOPE" in
  ALL_SUBNETWORKS_ALL_IP_RANGES)
    "$GCLOUD" compute routers nats update "$NAT" --router "$ROUTER" --region "$REGION" \
      --nat-external-ip-pool="$ADDR" --nat-custom-subnet-ip-ranges=default \
      --enable-logging --log-filter=ERRORS_ONLY
    ;;
  LIST_OF_SUBNETWORKS)
    ;;
  *) echo "unexpected payment NAT scope: ${SCOPE:-missing}" >&2; exit 3 ;;
esac

POST_SCOPE=$("$GCLOUD" compute routers describe "$ROUTER" --region "$REGION" \
  --format="value(nats[0].sourceSubnetworkIpRangesToNat)")
[ "$POST_SCOPE" = LIST_OF_SUBNETWORKS ] || { echo "payment NAT scope update did not converge" >&2; exit 1; }
POST_IP=$("$GCLOUD" compute routers describe "$ROUTER" --region "$REGION" \
  --format="value(nats[0].natIps)" | tr ';' '\n' | tail -1)
case "$POST_IP" in *"/addresses/$ADDR") ;; *) echo "payment NAT no longer references $ADDR; stop and investigate" >&2; exit 1 ;; esac
POST_SUBNETS=$("$GCLOUD" compute routers describe "$ROUTER" --region "$REGION" \
  --format="value(nats[0].subnetworks[].name)" | tr ';' '\n' | sed '/^$/d')
case "$POST_SUBNETS" in
  default|*/subnetworks/default) ;;
  *) echo "payment NAT is LIST_OF_SUBNETWORKS but does not target only default: ${POST_SUBNETS:-none}" >&2; exit 1 ;;
esac
POST_EGRESS_IP=$("$GCLOUD" compute addresses describe "$ADDR" --region "$REGION" --format='value(address)')
[ "$POST_EGRESS_IP" = "$CURRENT_EGRESS_IP" ] || {
  echo "reserved payment egress IP changed from $CURRENT_EGRESS_IP to $POST_EGRESS_IP; stop and investigate" >&2
  exit 1
}
echo "Payment NAT now covers default only and retains $ADDR ($POST_EGRESS_IP)."
echo "Required postcheck: repeat the live payment-path probe and confirm it presents $POST_EGRESS_IP before provisioning crawl NAT."
