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
#
# 🚨 DO NOT DELETE OR RECREATE `pivota-egress-ip` IN prod (8.231.167.230).
# It is the OUTBOUND source address partners allowlist. Deleting the address resource, or deleting
# and recreating the NAT that holds it, hands back the IP and you will not get it back - and a
# partner allowlist entry is expensive to correct once integration has started. The reservation is
# a convention enforced by this comment, not by an org policy.
#
# Two adjacent facts, both easy to get wrong in a partner-facing document:
#  * DIRECTION. 8.231.167.230 is OUTBOUND ONLY - our source address when we call a partner. Traffic
#    a partner sends TO US arrives at the global load balancer, `pivota-lb-ip` = 34.8.67.235. They
#    are different addresses and an allowlist that conflates them is wrong in one direction.
#  * STAGING IS A DIFFERENT IP: 136.66.216.216. Partner certification testing usually runs against
#    staging before prod, so that address needs allowlisting too.
#
# WHEN TO HARDEN THIS. Nothing external depends on the address today (Antom is not integrated; the
# live UCP door advertises `payment_handlers: {}`). The moment it lands in a real partner allowlist
# that changes. Natural trigger: when ADR-023 (PR #2005) merges and Antom session work begins,
# revisit whatever enforcement is available rather than relying on this comment.
#
# CRAWL EGRESS MUST NOT SHARE THIS IP. This script creates ONE router + ONE NAT covering
# ALL_SUBNETWORKS_ALL_IP_RANGES, so every service shares 8.231.167.230. A scheduled re-crawl would
# then share both IP reputation and the NAT port pool with the payment path - and NAT port
# exhaustion is per-IP, so a burst crawl can starve payment egress even with clean reputation.
# Measured 2026-08-21: ~50 requests across 37 Cloudflare-fronted merchant domains in ~1 minute trips
# a cross-domain IP-level 429 lasting ~15 minutes. Before the first crawl job ships, give crawl its
# own subnet + Router/NAT/reserved IP via `--nat-custom-subnet-ip-ranges`. The NEW address goes to
# crawl; 8.231.167.230 stays on the payment path.
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
