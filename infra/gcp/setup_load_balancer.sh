#!/usr/bin/env bash
# Global external HTTPS load balancer in front of the Cloud Run services.
#   infra/gcp/setup_load_balancer.sh staging|prod
#
# WHY CERTIFICATE MANAGER RATHER THAN A CLASSIC MANAGED CERT:
# A google-managed SSL certificate on a target proxy validates by HTTP, so it only goes ACTIVE once
# the hostname ALREADY points at this load balancer. During a cutover that means: flip DNS -> serve
# TLS errors -> wait for issuance. Certificate Manager validates via a DNS TXT record instead, so
# every certificate can reach ACTIVE while traffic is still on Railway, and the cutover becomes a
# pure DNS change with no TLS gap. That is the whole reason for the extra moving parts here.
#
# The TXT records this prints must be added at the DNS host (HiChina) BEFORE the certs can issue.
# They are additive and independent of the A/CNAME records that carry live traffic.
set -euo pipefail
ENV="${1:-}"
case "$ENV" in
  staging) PROJECT=pivota-staging; PREFIX=pivota-staging ;;
  prod)    PROJECT=pivota-prod;    PREFIX=pivota ;;
  *) echo "usage: $0 staging|prod" >&2; exit 2 ;;
esac
GCLOUD="${GCLOUD:-gcloud}"; REGION=us-west1
export CLOUDSDK_CORE_PROJECT="$PROJECT"
have(){ "$@" >/dev/null 2>&1; }
say(){ printf '\n\033[1;34m== %s\033[0m\n' "$*"; }

# host -> cloud run service
# These are PRODUCTION hostnames. Running this for staging would mint certificates for them inside
# pivota-staging and then print "repoint api.pivota.cc at <staging LB>" - pointing production DNS at
# a stack holding a restored prod snapshot. Staging has no pivota.cc names of its own, so refuse.
if [ "$ENV" != prod ]; then
  echo "refusing: the hostname list in this script is production-only. Give staging its own names first." >&2
  exit 2
fi
HOSTS_WEB="api.pivota.cc"
HOSTS_GATEWAY="gateway.pivota.cc mcp.pivota.cc commerce.mcp.pivota.cc ucp.pivota.cc acp.pivota.cc"
ALL_HOSTS="$HOSTS_WEB $HOSTS_GATEWAY"

say "static anycast IP"
have "$GCLOUD" compute addresses describe "$PREFIX-lb-ip" --global \
  || "$GCLOUD" compute addresses create "$PREFIX-lb-ip" --global --ip-version=IPV4 \
       --description="Global HTTPS LB frontend for $ENV"
LB_IP=$("$GCLOUD" compute addresses describe "$PREFIX-lb-ip" --global --format='value(address)')

say "serverless NEGs + backend services"
for svc in web gateway; do
  have "$GCLOUD" compute network-endpoint-groups describe "$PREFIX-neg-$svc" --region "$REGION" \
    || "$GCLOUD" compute network-endpoint-groups create "$PREFIX-neg-$svc" \
         --region "$REGION" --network-endpoint-type=serverless --cloud-run-service="$svc"
  # NO --protocol here. Passing one sets portName (e.g. `https`), and a backend service with a
  # portName REFUSES a serverless NEG: "Port name is not supported for a backend service with
  # Serverless network endpoint groups". portName cannot be cleared by `update` either - the service
  # has to be recreated - so getting this right at creation matters.
  have "$GCLOUD" compute backend-services describe "$PREFIX-bes-$svc" --global \
    || "$GCLOUD" compute backend-services create "$PREFIX-bes-$svc" --global \
         --load-balancing-scheme=EXTERNAL_MANAGED --enable-logging --logging-sample-rate=1.0 \
         --timeout=300
  # Do NOT swallow errors here. This attach failing silently is what produced six hostnames serving
  # "no healthy upstream" with a perfectly valid certificate - the LB looked built and was not.
  if ! "$GCLOUD" compute backend-services describe "$PREFIX-bes-$svc" --global --format='value(backends[0].group)' | grep -q .; then
    "$GCLOUD" compute backend-services add-backend "$PREFIX-bes-$svc" --global \
      --network-endpoint-group="$PREFIX-neg-$svc" --network-endpoint-group-region="$REGION"
  fi
done

say "certificates (DNS-authorized, so they issue BEFORE the cutover)"
CERT_LIST=""
for h in $ALL_HOSTS; do
  slug=$(printf '%s' "$h" | tr '.' '-')
  have "$GCLOUD" certificate-manager dns-authorizations describe "auth-$slug" \
    || "$GCLOUD" certificate-manager dns-authorizations create "auth-$slug" --domain="$h"
  have "$GCLOUD" certificate-manager certificates describe "cert-$slug" \
    || "$GCLOUD" certificate-manager certificates create "cert-$slug" --domains="$h" --dns-authorizations="auth-$slug"
  CERT_LIST="${CERT_LIST}${CERT_LIST:+,}cert-$slug"
done
have "$GCLOUD" certificate-manager maps describe "$PREFIX-certmap" \
  || "$GCLOUD" certificate-manager maps create "$PREFIX-certmap"
for h in $ALL_HOSTS; do
  slug=$(printf '%s' "$h" | tr '.' '-')
  have "$GCLOUD" certificate-manager maps entries describe "entry-$slug" --map="$PREFIX-certmap" \
    || "$GCLOUD" certificate-manager maps entries create "entry-$slug" --map="$PREFIX-certmap" \
         --hostname="$h" --certificates="cert-$slug"
done

say "url map"
if ! have "$GCLOUD" compute url-maps describe "$PREFIX-urlmap"; then
  # Default to the backend service; every known host is matched explicitly below.
  "$GCLOUD" compute url-maps create "$PREFIX-urlmap" --default-service="$PREFIX-bes-web"
fi
# Do NOT swallow these. If add-path-matcher fails, the host falls through to the url map's DEFAULT
# service - which is the BACKEND - so mcp/ucp/acp/gateway would quietly serve the wrong application
# behind a valid certificate. Skip only when the host rule already exists; fail on anything else.
add_host(){ # host, backend-service
  local h="$1" be="$2" slug err
  slug=$(printf '%s' "$h" | tr '.' '-')
  if "$GCLOUD" compute url-maps describe "$PREFIX-urlmap" --format='value(hostRules.hosts.list())' | tr ';,' '\n\n' | grep -qx "$h"; then
    echo "   host rule exists: $h"; return 0
  fi
  if ! err=$("$GCLOUD" compute url-maps add-path-matcher "$PREFIX-urlmap" \
        --path-matcher-name="pm-$slug" --default-service="$be" --new-hosts="$h" 2>&1); then
    echo "FAILED to route $h -> $be:" >&2; echo "$err" >&2; exit 1
  fi
  echo "   routed $h -> $be"
}
for h in $HOSTS_GATEWAY; do add_host "$h" "$PREFIX-bes-gateway"; done
add_host "$HOSTS_WEB" "$PREFIX-bes-web"

say "https proxy + forwarding rule"
have "$GCLOUD" compute target-https-proxies describe "$PREFIX-https-proxy" --global \
  || "$GCLOUD" compute target-https-proxies create "$PREFIX-https-proxy" --global \
       --url-map="$PREFIX-urlmap" --certificate-map="$PREFIX-certmap"
have "$GCLOUD" compute forwarding-rules describe "$PREFIX-https-fr" --global \
  || "$GCLOUD" compute forwarding-rules create "$PREFIX-https-fr" --global \
       --load-balancing-scheme=EXTERNAL_MANAGED --address="$PREFIX-lb-ip" --target-https-proxy="$PREFIX-https-proxy" --ports=443

say "http -> https redirect"
if ! have "$GCLOUD" compute url-maps describe "$PREFIX-redirect"; then
  cat > /tmp/redirect-$$.yaml <<YAML
name: $PREFIX-redirect
defaultUrlRedirect:
  redirectResponseCode: MOVED_PERMANENTLY_DEFAULT
  httpsRedirect: true
YAML
  "$GCLOUD" compute url-maps import "$PREFIX-redirect" --source=/tmp/redirect-$$.yaml --global --quiet
  rm -f /tmp/redirect-$$.yaml
fi
have "$GCLOUD" compute target-http-proxies describe "$PREFIX-http-proxy" --global \
  || "$GCLOUD" compute target-http-proxies create "$PREFIX-http-proxy" --url-map="$PREFIX-redirect" --global
have "$GCLOUD" compute forwarding-rules describe "$PREFIX-http-fr" --global \
  || "$GCLOUD" compute forwarding-rules create "$PREFIX-http-fr" --global \
       --load-balancing-scheme=EXTERNAL_MANAGED --address="$PREFIX-lb-ip" --target-http-proxy="$PREFIX-http-proxy" --ports=80

say "DNS records required"
echo "  LOAD BALANCER IP: $LB_IP"
echo
echo "  1) Certificate validation TXT records - add these NOW; certs issue without touching traffic:"
for h in $ALL_HOSTS; do
  slug=$(printf '%s' "$h" | tr '.' '-')
  "$GCLOUD" certificate-manager dns-authorizations describe "auth-$slug" \
    --format="value[separator='  '](dnsResourceRecord.name,dnsResourceRecord.type,dnsResourceRecord.data)" | sed 's/^/     /'
done
echo
echo "  2) AT CUTOVER, repoint each host from its Railway CNAME to this LB:"
for h in $ALL_HOSTS; do echo "     $h  ->  A  $LB_IP"; done
