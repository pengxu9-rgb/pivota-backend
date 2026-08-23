#!/usr/bin/env bash
# Provision a dedicated Cloud Run egress lane for public catalogue crawling.
#
#   CRAWL_SUBNET_CIDR=10.42.0.0/24 infra/gcp/setup_crawl_egress.sh staging
#   CRAWL_SUBNET_CIDR=10.43.0.0/24 infra/gcp/setup_crawl_egress.sh prod
#
# This deliberately does NOT modify the existing default-subnet NAT or any
# payment service. Antom/Adyen payment traffic stays on pivota-egress-ip; crawl
# requests use the separate address created here. Deploy a crawl Job onto this
# subnet only after its robots, per-domain rate-limit, consent, and dry-run
# gates are implemented.
#
# Cloud NAT allows only one ALL_SUBNETWORKS_ALL_IP_RANGES NAT in a VPC/region.
# Therefore this script fails before creating anything until the guarded
# `migrate_payment_nat_to_default_subnet.sh` one-time migration has narrowed
# the existing payment NAT to the default subnet while retaining its IP.
set -euo pipefail

ENV="${1:-}"
case "$ENV" in
  staging) PROJECT=pivota-staging ;;
  prod) PROJECT=pivota-prod ;;
  *) echo "usage: CRAWL_SUBNET_CIDR=<non-overlapping-cidr> $0 staging|prod" >&2; exit 2 ;;
esac

: "${CRAWL_SUBNET_CIDR:?set CRAWL_SUBNET_CIDR to a non-overlapping RFC1918 CIDR}"
GCLOUD="${GCLOUD:-gcloud}"
REGION=us-west1
NETWORK=default
SUBNET=pivota-crawl
ROUTER=pivota-crawl-router
NAT=pivota-crawl-nat
ADDR=pivota-crawl-egress-ip
PAYMENT_ROUTER=pivota-router
PAYMENT_NAT=pivota-nat
export CLOUDSDK_CORE_PROJECT="$PROJECT"

have() { "$@" >/dev/null 2>&1; }

PAYMENT_SCOPE=$("$GCLOUD" compute routers describe "$PAYMENT_ROUTER" --region "$REGION" \
  --format="value(nats[0].sourceSubnetworkIpRangesToNat)" 2>/dev/null || true)
if [ "$PAYMENT_SCOPE" = "ALL_SUBNETWORKS_ALL_IP_RANGES" ]; then
  echo "payment NAT still covers all subnets; run the guarded payment-NAT scope migration first" >&2
  exit 3
fi

have "$GCLOUD" compute networks subnets describe "$SUBNET" --region "$REGION" \
  || "$GCLOUD" compute networks subnets create "$SUBNET" --network "$NETWORK" --region "$REGION" \
       --range "$CRAWL_SUBNET_CIDR" --enable-private-ip-google-access

have "$GCLOUD" compute addresses describe "$ADDR" --region "$REGION" \
  || "$GCLOUD" compute addresses create "$ADDR" --region "$REGION" \
       --description="Dedicated outbound IP for rate-limited public catalogue crawling"

have "$GCLOUD" compute routers describe "$ROUTER" --region "$REGION" \
  || "$GCLOUD" compute routers create "$ROUTER" --network "$NETWORK" --region "$REGION"

have "$GCLOUD" compute routers nats describe "$NAT" --router "$ROUTER" --region "$REGION" \
  || "$GCLOUD" compute routers nats create "$NAT" --router "$ROUTER" --region "$REGION" \
       --nat-custom-subnet-ip-ranges="$SUBNET" --nat-external-ip-pool="$ADDR" \
       --enable-logging --log-filter=ERRORS_ONLY

EGRESS_IP=$("$GCLOUD" compute addresses describe "$ADDR" --region "$REGION" --format='value(address)')
echo "Dedicated crawl egress is ready: subnet=$SUBNET ip=$EGRESS_IP"
echo "Do not give this address to payment partners; do not deploy payment workloads to $SUBNET."
