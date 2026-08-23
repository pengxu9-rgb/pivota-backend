#!/usr/bin/env bash
# One-time, guarded prerequisite for an isolated crawl NAT.
#
# It narrows pivota-nat from ALL_SUBNETWORKS_ALL_IP_RANGES to the existing
# `default` subnet only, preserving the exact pivota-egress-ip reservation used
# by payment partners. Do this in staging first; production requires a reviewed
# maintenance window and an immediate egress verification from a default-subnet
# Cloud Run Job.
#
#   CONFIRM_PAYMENT_NAT_SCOPE=default \
#     infra/gcp/migrate_payment_nat_to_default_subnet.sh staging
set -euo pipefail

ENV="${1:-}"
case "$ENV" in
  staging) PROJECT=pivota-staging ;;
  prod) PROJECT=pivota-prod ;;
  *) echo "usage: CONFIRM_PAYMENT_NAT_SCOPE=default $0 staging|prod" >&2; exit 2 ;;
esac
[ "${CONFIRM_PAYMENT_NAT_SCOPE:-}" = default ] \
  || { echo "set CONFIRM_PAYMENT_NAT_SCOPE=default after reviewing payment impact" >&2; exit 2; }

GCLOUD="${GCLOUD:-gcloud}"
REGION=us-west1
ROUTER=pivota-router
NAT=pivota-nat
ADDR=pivota-egress-ip
export CLOUDSDK_CORE_PROJECT="$PROJECT"

SUBNETS=$("$GCLOUD" compute networks subnets list --filter="region:($REGION) AND network:default" \
  --format='value(name)' | tr '\n' ',' | sed 's/,$//')
[ "$SUBNETS" = default ] || {
  echo "refusing to narrow payment NAT: expected only default subnet in $REGION, found: ${SUBNETS:-none}" >&2
  exit 3
}

SCOPE=$("$GCLOUD" compute routers describe "$ROUTER" --region "$REGION" \
  --format="value(nats[0].sourceSubnetworkIpRangesToNat)")
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
echo "Payment NAT now covers default only and retains $ADDR. Verify payment egress before provisioning crawl NAT."
