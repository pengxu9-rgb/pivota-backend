#!/usr/bin/env bash
# Provision the merchant-door relay: its OWN egress lane (subnet + router + Cloud NAT + reserved
# address) and the small internal Cloud Run service that the gateway's merchant-door calls go
# through. Operator-run; not run by CI, not run by deploy_gateway.sh.
#
#   DOOR_RELAY_SUBNET_CIDR=<an unused RFC1918 range, /26 or larger> \
#     infra/gcp/setup_merchant_door_relay.sh staging|prod <gateway-sha>
#
# WHY. The gateway calls merchants' UCP doors today from the gateway's own egress, the default
# subnet's NAT `pivota-nat` (address `pivota-egress-ip`) - the address payment partners allowlist.
# A merchant door is a stranger's server: its rate limits, bot rules and IP reputation decisions
# then land on the payment address. The crawl lane (`pivota-crawl`, setup_crawl_egress.sh) is NOT
# the answer either: crawl throttles, the shared Shopify-edge pacer and the crawl IP breaker would
# then hit live buyer requests. So door calls get a third lane of their own, used by nothing else.
#
# WHAT IT CREATES (each describe-then-create; a re-run creates nothing twice):
#   subnet   pivota-door-relay              us-west1, network `default`, Private Google Access on,
#                                           range from DOOR_RELAY_SUBNET_CIDR (required; never
#                                           defaulted and never committed: no range is in this repo)
#   router   pivota-door-relay-router
#   address  pivota-door-relay-egress-ip    reserved, regional, external
#   NAT      pivota-door-relay-nat          on that router, covering ONLY pivota-door-relay, with
#                                           ONLY that address (manual allocation)
#   identity sa-door-relay@<project>        NO project roles, NO secret access
#   service  merchant-door-relay            Cloud Run, the gateway image at <gateway-sha>, command
#                                           `node src/services/doorRelay/server.js` (health
#                                           GET /healthz, listens on $PORT), ingress internal,
#                                           IAM-gated, min-instances 1, all egress via the new
#                                           subnet; env is metadata only (PIVOTA_ENV,
#                                           PIVOTA_SERVICE_NAME, PIVOTA_COMMIT_SHA), no secrets
#   IAM      roles/run.invoker on merchant-door-relay for the GATEWAY's runtime identity
#            (sa-gateway, as deploy_gateway.sh runs it) - and nothing else
#
# WHAT IT NEVER DOES. It never modifies the payment NAT (`pivota-router`/`pivota-nat`) or the crawl
# NAT (`pivota-crawl-router`/`pivota-crawl-nat`); it only READS the payment router's scope, for the
# same reason setup_crawl_egress.sh does (Cloud NAT allows one ALL_SUBNETWORKS NAT per VPC/region,
# so a second NAT cannot exist until the payment NAT covers only `default`). It never touches the
# `gateway` service, its revisions or its env. It never mounts a secret on the relay, and it never
# updates an existing subnet or NAT: one that does not match what this script would create is
# REFUSED, for a human to look at.
#
# DARK BY DEFAULT. Creating the relay changes no live traffic: the gateway keeps calling doors
# directly until a SEPARATE, reviewed gateway env change sets MERCHANT_DOOR_RELAY_URL (the service
# URL printed at the end) and MERCHANT_DOOR_RELAY=on. This script prints that step; it does not
# run it. The gateway env routes through the controller.
#
# REACHABILITY, assumed rather than created here. The gateway runs with `--network default
# --subnet default --vpc-egress all-traffic` (deploy_gateway.sh), so its call to the relay's
# run.app URL goes through the VPC and arrives as INTERNAL - which `--ingress internal` admits -
# provided Private Google Access is on for subnet `default`. This script only READS that flag and
# warns when it is off; it never changes the default subnet. The relay is IAM-gated as well, so the
# gateway must send an ID token whose audience is the relay's service URL.
#
# THE ADDRESS. The reserved address is printed at the end from `gcloud ... describe` at run time.
# It is not committed anywhere, and it is not given to payment partners or anyone else publicly:
# share it only through the controller session, where it is needed (a merchant asking which address
# our door calls come from).
#
# ROLLOUT ORDER. Staging first (this script + the gateway env change in staging, then a staging
# door run). Then prod, the same two steps. Moving the existing live door calls onto the relay is
# its own step - the prod gateway env flip - with a watch on door error rates (5xx, timeouts,
# refusals by host) before and after; rollback is MERCHANT_DOOR_RELAY unset/off on the gateway.
# Once the gateway uses the relay, re-running this script IS a relay deploy (the new revision
# takes traffic at once): do it in a watched window.
set -euo pipefail

ENV="${1:-}"; TAG="${2:-}"
case "$ENV" in
  staging) PROJECT=pivota-staging; PIVOTA_ENV=staging;    MAX=2 ;;
  prod)    PROJECT=pivota-prod;    PIVOTA_ENV=production; MAX=4 ;;
  *) echo "usage: DOOR_RELAY_SUBNET_CIDR=<unused RFC1918 range> $0 staging|prod <gateway-sha>" >&2; exit 2 ;;
esac
[ "$#" -le 2 ] || { echo "too many arguments (usage: $0 staging|prod <gateway-sha>)" >&2; exit 2; }
# A full 40-hex sha in BOTH environments: it is the image tag and the PIVOTA_COMMIT_SHA stamp, and
# `latest` (which cloudbuild.gateway.yaml pushes beside every sha) names no commit at all.
case "$TAG" in
  "") echo "gateway-sha is required (usage: $0 staging|prod <gateway-sha>)" >&2; exit 2 ;;
  latest) echo "refusing the floating tag 'latest' - pass the full 40-character gateway commit sha" >&2; exit 2 ;;
esac
[[ "$TAG" =~ ^[0-9a-f]{40}$ ]] || { echo "need a full 40-character lowercase gateway commit sha (got '$TAG')" >&2; exit 2; }

# ── THE SUBNET RANGE, VALIDATED BEFORE ANY gcloud CALL ───────────────────────────────────────────
# Required, never defaulted: a committed default would put a range in this public repo, and a
# guessed one could collide with a range someone is about to use. Refused unless it is an aligned
# RFC1918 network with a prefix from /16 to /26 (Direct VPC egress wants /26 or larger; Cloud Run
# takes addresses from it per instance).
CIDR="${DOOR_RELAY_SUBNET_CIDR:-}"
[ -n "$CIDR" ] || { echo "set DOOR_RELAY_SUBNET_CIDR to an unused RFC1918 range (/16 to /26); it is never defaulted" >&2; exit 2; }
cidr_ok(){
  local re='^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})/([0-9]{1,2})$'
  [[ "$1" =~ $re ]] || return 1
  local a="${BASH_REMATCH[1]}" b="${BASH_REMATCH[2]}" c="${BASH_REMATCH[3]}" d="${BASH_REMATCH[4]}" p="${BASH_REMATCH[5]}" o
  # No leading zeros: bash arithmetic would read them as octal, a human reads them as decimal.
  for o in "$a" "$b" "$c" "$d" "$p"; do
    case "$o" in 0?*) return 1 ;; esac
    [ "$o" -le 255 ] || return 1
  done
  [ "$p" -ge 16 ] && [ "$p" -le 26 ] || return 1
  [ "$a" -eq 10 ] || { [ "$a" -eq 172 ] && [ "$b" -ge 16 ] && [ "$b" -le 31 ]; } \
    || { [ "$a" -eq 192 ] && [ "$b" -eq 168 ]; } || return 1
  local ip=$(( (a << 24) | (b << 16) | (c << 8) | d )) host=$(( (1 << (32 - p)) - 1 ))
  [ $(( ip & host )) -eq 0 ]
}
cidr_ok "$CIDR" || { echo "DOOR_RELAY_SUBNET_CIDR='$CIDR' is not an aligned RFC1918 network with a /16 to /26 prefix" >&2; exit 2; }

GCLOUD="${GCLOUD:-gcloud}"
REGION=us-west1
NETWORK=default
SUBNET=pivota-door-relay
ROUTER=pivota-door-relay-router
ADDR=pivota-door-relay-egress-ip
NAT=pivota-door-relay-nat
SERVICE=merchant-door-relay
RELAY_SA_NAME=sa-door-relay
RELAY_SA="$RELAY_SA_NAME@$PROJECT.iam.gserviceaccount.com"
# The gateway's runtime identity, exactly as deploy_gateway.sh deploys it. Read, never changed.
GATEWAY_SA="sa-gateway@$PROJECT.iam.gserviceaccount.com"
# Read only (see the header): the payment router, to check its NAT's scope.
PAYMENT_ROUTER=pivota-router
export CLOUDSDK_CORE_PROJECT="$PROJECT"
have(){ "$@" >/dev/null 2>&1; }

# ── PREFLIGHT (reads only) ──────────────────────────────────────────────────────────────────────
HERE="$(cd "$(dirname "$0")" && pwd)"
# A gateway sha resolves in pivota (hand builds) first, then pivota-gateway (built on merge); see
# _gateway_image.sh for why that order, and why only "not found" falls through.
# shellcheck source=infra/gcp/_gateway_image.sh
. "$HERE/_gateway_image.sh"
_rc=0; IMAGE="$(resolve_gateway_image "$TAG")" || _rc=$?
case "$_rc" in
  0) echo "== image: $IMAGE" ;;
  1) echo "missing image gateway:$TAG in pivota and pivota-gateway; build it first (on merge to main by PIVOTA-Agent's build-gateway-image workflow, or by hand with infra/gcp/cloudbuild.gateway.yaml)" >&2; exit 1 ;;
  *) echo "cannot resolve gateway:$TAG (lookup error above); refusing" >&2; exit 1 ;;
esac

have "$GCLOUD" iam service-accounts describe "$GATEWAY_SA" --project "$PROJECT" \
  || { echo "missing $GATEWAY_SA (the gateway's runtime identity, the relay's only invoker)" >&2; exit 1; }

PAYMENT_SCOPE=$("$GCLOUD" compute routers describe "$PAYMENT_ROUTER" --region "$REGION" --project "$PROJECT" \
  --format="value(nats[0].sourceSubnetworkIpRangesToNat)" 2>/dev/null || true)
if [ "$PAYMENT_SCOPE" = "ALL_SUBNETWORKS_ALL_IP_RANGES" ]; then
  echo "payment NAT still covers all subnets; run the guarded payment-NAT scope migration first (migrate_payment_nat_to_default_subnet.sh)" >&2
  exit 3
fi

# The gateway reaches the relay as INTERNAL only through Private Google Access on its own subnet.
# Warn, never fix: the default subnet is the payment lane and is not this script's to change.
GATEWAY_SUBNET_PGA=$("$GCLOUD" compute networks subnets describe "$NETWORK" --region "$REGION" --project "$PROJECT" \
  --format="value(privateIpGoogleAccess)" 2>/dev/null || true)
case "$GATEWAY_SUBNET_PGA" in
  True|true) ;;
  *) echo "WARNING: could not confirm Private Google Access on subnet '$NETWORK' (got '${GATEWAY_SUBNET_PGA:-unreadable}')." \
       "The gateway's calls to an internal-ingress relay may be refused; check before the gateway env change." >&2 ;;
esac

# ── NETWORK: SUBNET, ADDRESS, ROUTER, NAT ───────────────────────────────────────────────────────
echo "== subnet: $SUBNET"
if have "$GCLOUD" compute networks subnets describe "$SUBNET" --region "$REGION" --project "$PROJECT"; then
  # An existing subnet is never updated. It must already be the one this script would create.
  EXISTING_RANGE=$("$GCLOUD" compute networks subnets describe "$SUBNET" --region "$REGION" --project "$PROJECT" \
    --format="value(ipCidrRange)")
  EXISTING_PGA=$("$GCLOUD" compute networks subnets describe "$SUBNET" --region "$REGION" --project "$PROJECT" \
    --format="value(privateIpGoogleAccess)")
  [ "$EXISTING_RANGE" = "$CIDR" ] \
    || { echo "$SUBNET exists with a different range than DOOR_RELAY_SUBNET_CIDR; refusing (nothing changed)" >&2; exit 1; }
  case "$EXISTING_PGA" in
    True|true) ;;
    *) echo "$SUBNET exists without Private Google Access; refusing (nothing changed)" >&2; exit 1 ;;
  esac
else
  "$GCLOUD" compute networks subnets create "$SUBNET" --network "$NETWORK" --region "$REGION" --project "$PROJECT" \
    --range "$CIDR" --enable-private-ip-google-access
fi

echo "== address: $ADDR"
have "$GCLOUD" compute addresses describe "$ADDR" --region "$REGION" --project "$PROJECT" \
  || "$GCLOUD" compute addresses create "$ADDR" --region "$REGION" --project "$PROJECT" \
       --description="Dedicated outbound address for the merchant-door relay (not for payment partners)"

echo "== router: $ROUTER"
have "$GCLOUD" compute routers describe "$ROUTER" --region "$REGION" --project "$PROJECT" \
  || "$GCLOUD" compute routers create "$ROUTER" --network "$NETWORK" --region "$REGION" --project "$PROJECT"

echo "== nat: $NAT"
if have "$GCLOUD" compute routers nats describe "$NAT" --router "$ROUTER" --region "$REGION" --project "$PROJECT"; then
  # Never updated. An existing NAT must cover ONLY our subnet with ONLY our address, manually.
  NAT_JSON=$("$GCLOUD" compute routers nats describe "$NAT" --router "$ROUTER" --region "$REGION" --project "$PROJECT" \
    --format=json)
  NAT_VERDICT=$(printf '%s' "$NAT_JSON" | python3 -c '
import json, sys
subnet, addr = sys.argv[1], sys.argv[2]
try:
    d = json.load(sys.stdin)
except Exception:
    print("unreadable"); sys.exit(0)
last = lambda u: str(u).rstrip("/").rsplit("/", 1)[-1]
subs = [last(s.get("name", "")) for s in (d.get("subnetworks") or [])]
ips = [last(u) for u in (d.get("natIps") or [])]
problems = []
if d.get("sourceSubnetworkIpRangesToNat") != "LIST_OF_SUBNETWORKS": problems.append("scope")
if subs != [subnet]: problems.append("subnetworks")
if d.get("natIpAllocateOption") != "MANUAL_ONLY": problems.append("allocation")
if ips != [addr]: problems.append("addresses")
print(",".join(problems) or "ok")' "$SUBNET" "$ADDR" 2>/dev/null || echo unreadable)
  [ "$NAT_VERDICT" = ok ] \
    || { echo "$NAT exists but does not match (mismatch: $NAT_VERDICT); refusing (nothing changed)" >&2; exit 1; }
else
  "$GCLOUD" compute routers nats create "$NAT" --router "$ROUTER" --region "$REGION" --project "$PROJECT" \
    --nat-custom-subnet-ip-ranges="$SUBNET" --nat-external-ip-pool="$ADDR" \
    --enable-logging --log-filter=ERRORS_ONLY
fi

# ── IDENTITY: sa-door-relay, NO ROLES ───────────────────────────────────────────────────────────
echo "== identity: $RELAY_SA"
have "$GCLOUD" iam service-accounts describe "$RELAY_SA" --project "$PROJECT" \
  || "$GCLOUD" iam service-accounts create "$RELAY_SA_NAME" --project "$PROJECT" \
       --display-name="Merchant-door relay (no roles, no secrets)" --quiet
# Refuse to run the relay as an identity that has picked up a project role since: the relay holds
# no credential on purpose, so a door it calls can never be handed one.
RELAY_ROLES=$("$GCLOUD" projects get-iam-policy "$PROJECT" --flatten="bindings[].members" \
  --filter="bindings.members:serviceAccount:$RELAY_SA" --format="value(bindings.role)") \
  || { echo "could not read $PROJECT's IAM policy to confirm $RELAY_SA has no project role; refusing" >&2; exit 1; }
[ -z "$RELAY_ROLES" ] \
  || { echo "$RELAY_SA holds project role(s): $(printf '%s' "$RELAY_ROLES" | tr '\n' ' ')- remove them first; refusing" >&2; exit 1; }

# ── THE SERVICE ─────────────────────────────────────────────────────────────────────────────────
verb=created; have "$GCLOUD" run services describe "$SERVICE" --region "$REGION" --project "$PROJECT" && verb=updated
echo "== service: $SERVICE (will be $verb; ingress internal, min-instances 1, max-instances $MAX)"
# `gcloud run deploy` is create-or-update. --set-env-vars REPLACES the whole env, and --clear-secrets
# drops any secret a hand edit may have mounted, so every run leaves exactly these three metadata
# values and no secret. min-instances 1: no cold start inside the gateway's 5 s door budget.
# all-traffic (not private-ranges-only): only then do public requests leave via the new NAT.
"$GCLOUD" run deploy "$SERVICE" --project "$PROJECT" --region "$REGION" \
  --image "$IMAGE" \
  --command=node --args=src/services/doorRelay/server.js \
  --service-account "$RELAY_SA" \
  --network "$NETWORK" --subnet "$SUBNET" --vpc-egress all-traffic \
  --ingress internal --no-allow-unauthenticated \
  --min-instances 1 --max-instances "$MAX" \
  --cpu 1 --memory 512Mi --port 8080 --timeout 60s --execution-environment gen2 \
  --set-env-vars "PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=$SERVICE,PIVOTA_COMMIT_SHA=$TAG" \
  --clear-secrets \
  --labels "env=$ENV,managed-by=infra-gcp,lane=merchant-door-relay" \
  --quiet

# ── THE ONE INVOKER ─────────────────────────────────────────────────────────────────────────────
echo "== invoker: $GATEWAY_SA"
"$GCLOUD" run services add-iam-policy-binding "$SERVICE" --region "$REGION" --project "$PROJECT" \
  --member="serviceAccount:$GATEWAY_SA" --role=roles/run.invoker --quiet >/dev/null
# Say so if anything else can invoke it (a hand grant). Reported, not removed: a human decides.
POLICY_JSON=$("$GCLOUD" run services get-iam-policy "$SERVICE" --region "$REGION" --project "$PROJECT" \
  --format=json 2>/dev/null || true)
EXTRA=$(printf '%s' "$POLICY_JSON" | python3 -c '
import json, sys
want = ("roles/run.invoker", "serviceAccount:" + sys.argv[1])
try:
    d = json.load(sys.stdin)
except Exception:
    print("unreadable"); sys.exit(0)
print(" ".join(sorted(b.get("role", "") + "=" + m for b in (d.get("bindings") or [])
                      for m in (b.get("members") or []) if (b.get("role"), m) != want)))' "$GATEWAY_SA" 2>/dev/null || echo unreadable)
[ -z "$EXTRA" ] || echo "WARNING: $SERVICE's IAM policy holds more than the gateway's invoker binding: $EXTRA" >&2

# ── WHAT THE OPERATOR DOES NEXT (printed, not run) ──────────────────────────────────────────────
EGRESS_IP=$("$GCLOUD" compute addresses describe "$ADDR" --region "$REGION" --project "$PROJECT" --format='value(address)')
RELAY_URL=$("$GCLOUD" run services describe "$SERVICE" --region "$REGION" --project "$PROJECT" --format='value(status.url)')
echo "Merchant-door relay is ready in $PROJECT (dark: the gateway does not use it yet)."
echo "  service: $SERVICE $verb, image gateway:$TAG, runs as $RELAY_SA, invokable only by $GATEWAY_SA"
echo "  egress:  subnet $SUBNET -> $NAT -> $ADDR"
echo
echo "FOLLOW-UP 1 (via the controller, never publicly): the relay's egress address is $EGRESS_IP."
echo "  Do not give it to payment partners; do not commit it; do not deploy anything else to $SUBNET."
echo "FOLLOW-UP 2 (a separate, reviewed gateway env change via the controller - NOT done here):"
echo "  MERCHANT_DOOR_RELAY_URL=$RELAY_URL"
echo "  MERCHANT_DOOR_RELAY=on"
echo "  staging first; in prod it is the step that moves live door calls, with a watch on door error rates."
