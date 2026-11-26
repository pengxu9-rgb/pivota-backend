#!/usr/bin/env bash
# The sandbox's own global HTTPS load balancer: two hostnames, two Cloud Run services.
#
#   infra/gcp/sandbox/setup_sandbox_lb.sh            # dry run
#   APPLY=1 infra/gcp/sandbox/setup_sandbox_lb.sh    # controller + owner go (RUNBOOK.md step 7)
#   infra/gcp/sandbox/setup_sandbox_lb.sh dns        # print the DNS rounds (read-only)
#
#   $GATEWAY_HOST (sandbox.mcp.pivota.cc)  -> Cloud Run `gateway`
#   $AGENPAZ_HOST (sandbox.agenpaz.com)    -> Cloud Run `agenpaz` (the AgenPaz page; exclusive host)
#
# Its own IP and its own URL map, in pivota-sandbox. It shares nothing with prod's pivota-urlmap,
# so a mistake here cannot route a prod hostname, and a prod mistake cannot route a sandbox one.
#
# The URL map's DEFAULT is a redirect to the gateway host, not a backend. In prod the default is
# the backend service, and that is how an early A record for an AgenPaz host once served the prod
# backend under that name (memory: verify-dns-after-each-round). Here an unknown Host header can
# only ever get a 301. The cert map has no catch-all either, so an unknown name fails TLS first.
#
# DNS ORDER (the rule from the AgenPaz host setup): certificates validate by DNS CNAME, so they go
# ACTIVE before any traffic record exists. Round 1 = the two _acme-challenge CNAMEs ONLY, no A/AAAA.
# Round 2 = the A records, only after RUNBOOK.md step 12 (the check through the LB) passes.
set -euo pipefail
# shellcheck source=infra/gcp/sandbox/config.sh
. "$(cd "$(dirname "$0")" && pwd)/config.sh"
PREFIX=sandbox
MODE="${1:-build}"

slug(){ printf '%s' "$1" | tr '.' '-'; }

print_dns(){
  local ip
  ip=$(g compute addresses describe "$PREFIX-lb-ip" --global --format='value(address)' 2>/dev/null || echo '<LB_IP>')
  say "DNS round 1 - certificate validation ONLY (no A/AAAA records yet)"
  for h in "$GATEWAY_HOST" "$AGENPAZ_HOST"; do
    g certificate-manager dns-authorizations describe "auth-$(slug "$h")" \
      --format="value[separator='  '](dnsResourceRecord.name,dnsResourceRecord.type,dnsResourceRecord.data)" 2>/dev/null \
      | sed 's/^/   /' || echo "   _acme-challenge.$h  CNAME  <shown after APPLY=1>"
  done
  say "DNS round 2 - ONLY after RUNBOOK.md step 12 passes (checked through the LB, certs ACTIVE)"
  echo "   $GATEWAY_HOST  A  $ip"
  echo "   $AGENPAZ_HOST  A  $ip"
  echo "   (no AAAA: the LB has an IPv4 address only)"
}
if [ "$MODE" = dns ]; then print_dns; exit 0; fi
[ "$MODE" = build ] || { echo "usage: $0 [dns]" >&2; exit 2; }

say "sandbox LB in $SANDBOX_PROJECT"; mode_banner

say "static anycast IP"
have g compute addresses describe "$PREFIX-lb-ip" --global \
  || run g compute addresses create "$PREFIX-lb-ip" --global --ip-version=IPV4 \
       --description="partner sandbox HTTPS LB"

say "serverless NEGs + backend services"
for svc in gateway agenpaz; do
  have g compute network-endpoint-groups describe "$PREFIX-neg-$svc" --region "$REGION" \
    || run g compute network-endpoint-groups create "$PREFIX-neg-$svc" \
         --region "$REGION" --network-endpoint-type=serverless --cloud-run-service="$svc"
  # No --protocol: a portName makes a backend service refuse a serverless NEG (setup_load_balancer.sh).
  have g compute backend-services describe "$PREFIX-bes-$svc" --global \
    || run g compute backend-services create "$PREFIX-bes-$svc" --global \
         --load-balancing-scheme=EXTERNAL_MANAGED --enable-logging --logging-sample-rate=1.0 --timeout=300
  if ! grep -q . <<<"$(g compute backend-services describe "$PREFIX-bes-$svc" --global --format='value(backends[0].group)' 2>/dev/null)"; then
    run g compute backend-services add-backend "$PREFIX-bes-$svc" --global \
      --network-endpoint-group="$PREFIX-neg-$svc" --network-endpoint-group-region="$REGION"
  fi
done

say "certificates (DNS-authorized: they issue before any A record exists)"
have g certificate-manager maps describe "$PREFIX-certmap" || run g certificate-manager maps create "$PREFIX-certmap"
for h in "$GATEWAY_HOST" "$AGENPAZ_HOST"; do
  s=$(slug "$h")
  have g certificate-manager dns-authorizations describe "auth-$s" \
    || run g certificate-manager dns-authorizations create "auth-$s" --domain="$h"
  have g certificate-manager certificates describe "cert-$s" \
    || run g certificate-manager certificates create "cert-$s" --domains="$h" --dns-authorizations="auth-$s"
  have g certificate-manager maps entries describe "entry-$s" --map="$PREFIX-certmap" \
    || run g certificate-manager maps entries create "entry-$s" --map="$PREFIX-certmap" \
         --hostname="$h" --certificates="cert-$s"
done

say "url map (declarative: exactly two hosts, default = redirect)"
URLMAP_YAML=$(mktemp); trap 'rm -f "$URLMAP_YAML"' EXIT
BES="https://www.googleapis.com/compute/v1/projects/$SANDBOX_PROJECT/global/backendServices"
cat > "$URLMAP_YAML" <<YAML
name: $PREFIX-urlmap
defaultUrlRedirect:
  hostRedirect: $GATEWAY_HOST
  httpsRedirect: true
  redirectResponseCode: MOVED_PERMANENTLY_DEFAULT
hostRules:
- hosts: ['$GATEWAY_HOST']
  pathMatcher: pm-gateway
- hosts: ['$AGENPAZ_HOST']
  pathMatcher: pm-agenpaz
pathMatchers:
- name: pm-gateway
  defaultService: $BES/$PREFIX-bes-gateway
- name: pm-agenpaz
  defaultService: $BES/$PREFIX-bes-agenpaz
YAML
sed 's/^/   /' "$URLMAP_YAML"
run g compute url-maps import "$PREFIX-urlmap" --source="$URLMAP_YAML" --global --quiet

say "https proxy + forwarding rule"
have g compute target-https-proxies describe "$PREFIX-https-proxy" --global \
  || run g compute target-https-proxies create "$PREFIX-https-proxy" --global \
       --url-map="$PREFIX-urlmap" --certificate-map="$PREFIX-certmap"
have g compute forwarding-rules describe "$PREFIX-https-fr" --global \
  || run g compute forwarding-rules create "$PREFIX-https-fr" --global \
       --load-balancing-scheme=EXTERNAL_MANAGED --address="$PREFIX-lb-ip" \
       --target-https-proxy="$PREFIX-https-proxy" --ports=443

say "http -> https redirect"
REDIR_YAML=$(mktemp); trap 'rm -f "$URLMAP_YAML" "$REDIR_YAML"' EXIT
cat > "$REDIR_YAML" <<YAML
name: $PREFIX-redirect
defaultUrlRedirect:
  redirectResponseCode: MOVED_PERMANENTLY_DEFAULT
  httpsRedirect: true
YAML
have g compute url-maps describe "$PREFIX-redirect" \
  || run g compute url-maps import "$PREFIX-redirect" --source="$REDIR_YAML" --global --quiet
have g compute target-http-proxies describe "$PREFIX-http-proxy" --global \
  || run g compute target-http-proxies create "$PREFIX-http-proxy" --url-map="$PREFIX-redirect" --global
have g compute forwarding-rules describe "$PREFIX-http-fr" --global \
  || run g compute forwarding-rules create "$PREFIX-http-fr" --global \
       --load-balancing-scheme=EXTERNAL_MANAGED --address="$PREFIX-lb-ip" \
       --target-http-proxy="$PREFIX-http-proxy" --ports=80

print_dns
