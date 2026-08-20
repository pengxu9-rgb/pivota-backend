#!/usr/bin/env bash
# Cloud Router + Cloud NAT with a RESERVED static egress IP, and switch the env's Cloud Run services
# to internal ingress + all-traffic egress.
#   infra/gcp/setup_egress_nat.sh staging|prod
#
# Why this shape:
#  * Cloud Run -> Cloud Run over *.run.app takes the PUBLIC path. With `private-ranges-only` egress
#    the caller is anonymous from the internet and gets 401 on an IAM-gated callee. Routing all
#    egress through the VPC makes those calls arrive as INTERNAL, so `--ingress internal` is the
#    perimeter and no identity-token code is needed.
#  * `--ingress internal` + `--allow-unauthenticated` means: unreachable from the internet, reachable
#    from our own VPC. For staging that is strictly stronger than a public IAM-gated URL, because it
#    removes the public attack surface entirely.
#  * A RESERVED (not ephemeral) NAT IP is the stable outbound address Antom/Adyen IP-allowlisting
#    needs. Reserving it now means the address partners allowlist never changes.
set -euo pipefail
ENV="${1:-}"
case "$ENV" in staging) PROJECT=pivota-staging ;; prod) PROJECT=pivota-prod ;; *) echo "usage: $0 staging|prod" >&2; exit 2 ;; esac
GCLOUD="${GCLOUD:-gcloud}"; REGION=us-west1; NETWORK=default
ROUTER="pivota-router"; NAT="pivota-nat"; ADDR="pivota-egress-ip"
export CLOUDSDK_CORE_PROJECT="$PROJECT"
have(){ "$@" >/dev/null 2>&1; }

have "$GCLOUD" compute addresses describe "$ADDR" --region "$REGION" \
  || "$GCLOUD" compute addresses create "$ADDR" --region "$REGION" \
       --description="Stable outbound IP for Cloud Run egress (partner IP allowlisting)"
EGRESS_IP=$("$GCLOUD" compute addresses describe "$ADDR" --region "$REGION" --format='value(address)')

have "$GCLOUD" compute routers describe "$ROUTER" --region "$REGION" \
  || "$GCLOUD" compute routers create "$ROUTER" --network "$NETWORK" --region "$REGION"

have "$GCLOUD" compute routers nats describe "$NAT" --router "$ROUTER" --region "$REGION" \
  || "$GCLOUD" compute routers nats create "$NAT" --router "$ROUTER" --region "$REGION" \
       --nat-all-subnet-ip-ranges --nat-external-ip-pool="$ADDR" \
       --enable-logging --log-filter=ERRORS_ONLY

echo "NAT ready. Stable egress IP: $EGRESS_IP"
echo "Give this address to Antom/Adyen for IP allowlisting; it is reserved and will not change."
