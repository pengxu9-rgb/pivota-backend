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
#
# SWAPPING THE CRAWL NAT'S ADDRESS (2026-10-16/17). Cloudflare challenge-listed the crawl address
# pivota-crawl-egress-ip (34.82.199.35) on 2026-10-16. A second address, pivota-crawl-egress-ip-2
# (136.67.250.98), is reserved. The swap is an EXPLICIT, NON-DEFAULT step, because a fresh address
# burns exactly as fast as the old one under an unpaced crawler: pace first (be#2640 refresh, be#2641
# destination sweep), then swap.
#
#   swap (prod):     CRAWL_NAT_SWAP_TO=pivota-crawl-egress-ip-2 infra/gcp/setup_crawl_egress.sh prod
#   rollback (prod): CRAWL_NAT_SWAP_TO=pivota-crawl-egress-ip   infra/gcp/setup_crawl_egress.sh prod
#
# which runs, after checking the named address exists in the region:
#   gcloud compute routers nats update pivota-crawl-nat --router pivota-crawl-router \
#     --region us-west1 --nat-external-ip-pool=<CRAWL_NAT_SWAP_TO>
# and prints the NAT's pool before and after, plus the rollback line. CRAWL_SUBNET_CIDR is still
# required (the provisioning steps run first and are no-ops on an existing lane).
#
# WITHOUT CRAWL_NAT_SWAP_TO NOTHING IS SWAPPED, whatever CRAWL_EGRESS_ADDR says: the NAT is only
# CREATED with CRAWL_EGRESS_ADDR (default pivota-crawl-egress-ip, unchanged), never updated, so
# re-running this script can never move the live crawl IP. Remember the address-bound side effects
# of a swap (docs/runbooks/crawl_identity.md, reap_cart_proofs.md: gate (a)'s 24 h window restarts).
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
# Used only when this script CREATES the NAT or the address. A parameter, not a new default: see
# CRAWL_NAT_SWAP_TO above for moving an existing NAT.
ADDR="${CRAWL_EGRESS_ADDR:-pivota-crawl-egress-ip}"
SWAP_TO="${CRAWL_NAT_SWAP_TO:-}"
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

nat_pool() {
  "$GCLOUD" compute routers nats describe "$NAT" --router "$ROUTER" --region "$REGION" \
    --format='value(natIps)' 2>/dev/null || true
}

if [ -n "$SWAP_TO" ]; then
  have "$GCLOUD" compute addresses describe "$SWAP_TO" --region "$REGION" \
    || { echo "CRAWL_NAT_SWAP_TO=$SWAP_TO: no such address in $REGION; nothing swapped" >&2; exit 4; }
  BEFORE=$(nat_pool)
  echo "Swapping $NAT's external IP pool: $BEFORE -> $SWAP_TO"
  "$GCLOUD" compute routers nats update "$NAT" --router "$ROUTER" --region "$REGION" \
    --nat-external-ip-pool="$SWAP_TO"
  echo "NAT pool now: $(nat_pool)"
  echo "Rollback: CRAWL_NAT_SWAP_TO=<previous address name, e.g. pivota-crawl-egress-ip> $0 $ENV"
fi

# The NAT's ACTUAL pool, which after a swap is not $ADDR.
EGRESS_IP=$("$GCLOUD" compute addresses describe "$ADDR" --region "$REGION" --format='value(address)')
echo "Dedicated crawl egress is ready: subnet=$SUBNET created-with=$ADDR ($EGRESS_IP) nat-pool=$(nat_pool)"
echo "Do not give this address to payment partners; do not deploy payment workloads to $SUBNET."
