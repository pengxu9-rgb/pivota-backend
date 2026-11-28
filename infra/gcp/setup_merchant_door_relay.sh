#!/usr/bin/env bash
# Provision the merchant-door relay: its OWN egress lane (subnet + router + Cloud NAT + reserved
# address + an egress fence) and the small internal Cloud Run service that the gateway's
# merchant-door calls go through. Operator-run; not run by CI, not run by deploy_gateway.sh.
#
#   DOOR_RELAY_SUBNET_CIDR=<an unused RFC1918 range, /26 or larger> \
#     infra/gcp/setup_merchant_door_relay.sh staging|prod <gateway-sha>
#
#   DOOR_RELAY_ACCEPTED_PROJECT_INVOKERS=serviceAccount:<a>,serviceAccount:<b>   (optional; see
#     WHO ELSE CAN CALL THE RELAY below - each listed member is accepted on THIS run only)
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
#   router   pivota-door-relay-router       network `default`, us-west1
#   address  pivota-door-relay-egress-ip    reserved, regional, external
#   NAT      pivota-door-relay-nat          on that router, covering ONLY pivota-door-relay, with
#                                           ONLY that address (manual allocation); dynamic port
#                                           allocation, 1024..8192 ports per instance (door calls
#                                           go to few edge addresses on one port, so a fixed small
#                                           allocation runs out first)
#   firewall pivota-door-relay-deny-private-egress
#                                           EGRESS DENY, priority 100, from the relay subnet's range
#                                           and the relay's network tag to every private,
#                                           shared-address and link-local range (DENY_RANGES).
#                                           The relay's SSRF guard is then not the only fence
#                                           between a hostile door URL and our VPC.
#   identity sa-door-relay@<project>        NO project roles, NO secret access
#   service  merchant-door-relay            Cloud Run, the gateway image at <gateway-sha>, command
#                                           `node src/services/doorRelay/server.js` (health
#                                           GET /healthz, listens on $PORT), ingress internal,
#                                           invoker IAM check pinned ON, min-instances 1, all egress
#                                           via the new subnet with network tag pivota-door-relay;
#                                           env is metadata only (PIVOTA_ENV, PIVOTA_SERVICE_NAME,
#                                           PIVOTA_COMMIT_SHA), no secrets
#   IAM      roles/run.invoker on merchant-door-relay for the GATEWAY's runtime identity
#            (sa-gateway, as deploy_gateway.sh runs it) - and nothing else
#
# WHAT IT NEVER DOES. It never modifies the payment NAT (`pivota-router`/`pivota-nat`) or the crawl
# NAT (`pivota-crawl-router`/`pivota-crawl-nat`); it only READS the payment router's scope, for the
# same reason setup_crawl_egress.sh does (Cloud NAT allows one ALL_SUBNETWORKS NAT per VPC/region,
# so a second NAT cannot exist until the payment NAT covers only `default`). It never touches the
# `gateway` service, its revisions or its env. It never mounts a secret on the relay, and it never
# updates an existing subnet, router, address, NAT or firewall rule: one that does not match what
# this script would create is REFUSED, for a human to look at.
#
# WHO ELSE CAN CALL THE RELAY. `--ingress internal` admits the WHOLE default VPC (the crawl subnet
# included), so the relay's IAM check is the fence: an identity that can invoke it can use it as a
# proxy out of our VPC. The invoker check is pinned on every run (`--invoker-iam-check`), so a
# hand-set `--no-invoker-iam-check` cannot survive a re-run. And the PROJECT policy is read before
# anything is created, because a project-level grant reaches this service too:
#   REFUSED (rc 1, before anything is created):
#     allUsers / allAuthenticatedUsers holding roles/run.invoker, run.admin, editor or owner;
#     any service account other than sa-gateway holding one of them, unless it is listed in
#       DOOR_RELAY_ACCEPTED_PROJECT_INVOKERS on this run (printed as ACCEPTED - narrowing the
#       project grant is the real fix; the list is the operator saying so out loud);
#     sa-door-relay itself holding ANY project role.
#   WARNED (the run continues):
#     users, groups and domains holding one of the four roles (humans and CI owners: refusing
#       them would make the script unrunnable where people hold owner, and they can change the
#       relay anyway); Google-managed service agents (service-<n>@...iam.gserviceaccount.com,
#       <n>@cloudservices.gserviceaccount.com), which run no workload in our VPC; deleted members.
#   Grants inherited from a folder or the organisation are not visible here.
#
# DARK BY DEFAULT. Creating the relay changes no live traffic: the gateway keeps calling doors
# directly until a SEPARATE, reviewed gateway env change sets MERCHANT_DOOR_RELAY_URL (the service
# URL printed at the end) and MERCHANT_DOOR_RELAY=on. This script prints that step; it does not
# run it. The gateway env routes through the controller.
#
# REACHABILITY, assumed rather than created here. The gateway runs with `--network default
# --subnet default --vpc-egress all-traffic` (deploy_gateway.sh), so its call to the relay's
# run.app URL goes through the VPC and arrives as INTERNAL - which `--ingress internal` admits -
# provided Private Google Access is on for subnet `default`. That is a hard precondition of the
# env flip and its own reviewed step: this script only READS the flag and warns when it is off; it
# never changes the default subnet. The relay is IAM-gated as well, so the gateway must send an ID
# token whose audience is the relay's service URL.
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

# Each accepted project-level invoker must be named exactly, as a service account: a bare email, a
# user or a public member is a typo or a mistake, and is refused here, before any gcloud call.
ACCEPTED="${DOOR_RELAY_ACCEPTED_PROJECT_INVOKERS:-}"
if [ -n "$ACCEPTED" ]; then
  _old_ifs="$IFS"; IFS=,
  for _m in $ACCEPTED; do
    [[ "$_m" =~ ^serviceAccount:[a-z0-9-]+@[a-z0-9.-]+\.gserviceaccount\.com$ ]] \
      || { IFS="$_old_ifs"; echo "DOOR_RELAY_ACCEPTED_PROJECT_INVOKERS: '$_m' is not serviceAccount:<email>" >&2; exit 2; }
  done
  IFS="$_old_ifs"
fi

GCLOUD="${GCLOUD:-gcloud}"
REGION=us-west1
NETWORK=default
SUBNET=pivota-door-relay
ROUTER=pivota-door-relay-router
ADDR=pivota-door-relay-egress-ip
NAT=pivota-door-relay-nat
# Ports per relay instance. Power-of-2 bounds, as dynamic port allocation requires. At MAX instances
# the ceiling stays well inside one address's port range.
NAT_MIN_PORTS=1024
NAT_MAX_PORTS=8192
FIREWALL=pivota-door-relay-deny-private-egress
FIREWALL_PRIORITY=100
NETWORK_TAG=pivota-door-relay
# THE ONE PLACE ADDRESS RANGES APPEAR IN THIS FILE: the standard private (RFC1918), shared-address
# (RFC6598) and link-local (RFC3927) blocks the relay must never reach. Constants of the protocol,
# not anyone's infrastructure; tests/test_setup_merchant_door_relay.py allows exactly these five.
# Traffic to the metadata server is not subject to VPC firewall rules, so the relay can still
# resolve names and mint tokens.
DENY_RANGES="10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10,169.254.0.0/16"
SERVICE=merchant-door-relay
RELAY_SA_NAME=sa-door-relay
RELAY_SA="$RELAY_SA_NAME@$PROJECT.iam.gserviceaccount.com"
# The gateway's runtime identity, exactly as deploy_gateway.sh deploys it. Read, never changed.
GATEWAY_SA="sa-gateway@$PROJECT.iam.gserviceaccount.com"
# Read only (see the header): the payment router, to check its NAT's scope.
PAYMENT_ROUTER=pivota-router
export CLOUDSDK_CORE_PROJECT="$PROJECT"
have(){ "$@" >/dev/null 2>&1; }

# One reader for every existing resource and for the project policy: JSON on stdin, the kind and
# what to expect as arguments. Prints `ok`, or the names of the fields that differ, or
# `unreadable`. For `policy` it prints one `REFUSE|WARN|ACCEPTED <role> <member> <why>` line each.
CHECK_PY='
import json, re, sys
kind, args = sys.argv[1], sys.argv[2:]
try:
    d = json.load(sys.stdin)
except Exception:
    print("unreadable"); sys.exit(0)
last = lambda u: str(u or "").rstrip("/").rsplit("/", 1)[-1]
bad = []
def want(name, ok):
    if not ok: bad.append(name)
if kind == "subnet":
    cidr, network, region = args
    want("range", d.get("ipCidrRange") == cidr)
    want("private-google-access", d.get("privateIpGoogleAccess") is True)
    want("network", last(d.get("network")) == network)
    want("region", last(d.get("region")) == region)
elif kind == "router":
    network, region = args
    want("network", last(d.get("network")) == network)
    want("region", last(d.get("region")) == region)
elif kind == "address":
    region, router = args
    want("region", last(d.get("region")) == region)
    want("type", d.get("addressType", "EXTERNAL") == "EXTERNAL")
    want("users", all("/routers/" in str(u) and last(u) == router for u in (d.get("users") or [])))
elif kind == "nat":
    subnet, addr, lo, hi = args
    want("scope", d.get("sourceSubnetworkIpRangesToNat") == "LIST_OF_SUBNETWORKS")
    want("subnetworks", [last(s.get("name")) for s in (d.get("subnetworks") or [])] == [subnet])
    want("allocation", d.get("natIpAllocateOption") == "MANUAL_ONLY")
    want("addresses", [last(u) for u in (d.get("natIps") or [])] == [addr])
    want("dynamic-port-allocation", d.get("enableDynamicPortAllocation") is True)
    want("min-ports", str(d.get("minPortsPerVm")) == lo)
    want("max-ports", str(d.get("maxPortsPerVm")) == hi)
    want("endpoint-independent-mapping", not d.get("enableEndpointIndependentMapping"))
elif kind == "firewall":
    network, cidr, ranges, tag, priority = args
    want("network", last(d.get("network")) == network)
    want("direction", d.get("direction") == "EGRESS")
    want("action", [r.get("IPProtocol") for r in (d.get("denied") or [])] == ["all"] and not d.get("allowed"))
    want("destination-ranges", sorted(d.get("destinationRanges") or []) == sorted(ranges.split(",")))
    want("source-ranges", (d.get("sourceRanges") or []) == [cidr])
    want("target-tags", (d.get("targetTags") or []) == [tag])
    want("priority", str(d.get("priority")) == priority)
    want("enabled", not d.get("disabled"))
elif kind == "policy":
    gateway, relay, accepted = args[0], args[1], set(filter(None, args[2].split(",")))
    watched = {"roles/run.invoker", "roles/run.admin", "roles/editor", "roles/owner"}
    agent = re.compile(r"(service-[0-9]+@[a-z0-9-]+\.iam|[0-9]+@cloudservices)\.gserviceaccount\.com")
    for b in d.get("bindings") or []:
        role = b.get("role", "")
        cond = " (conditional: %s)" % b["condition"].get("title", "") if b.get("condition") else ""
        for m in b.get("members") or []:
            if m == relay:
                print("REFUSE", role, m, "the relay identity holds a project role" + cond); continue
            if role not in watched or m == gateway:
                continue
            if m in ("allUsers", "allAuthenticatedUsers"):
                print("REFUSE", role, m, "public at project level" + cond)
            elif m.startswith("serviceAccount:"):
                if agent.fullmatch(m.split(":", 1)[1]):
                    print("WARN", role, m, "Google-managed service agent" + cond)
                elif m in accepted:
                    print("ACCEPTED", role, m, "listed in DOOR_RELAY_ACCEPTED_PROJECT_INVOKERS" + cond)
                else:
                    print("REFUSE", role, m, "a workload identity that can invoke the relay" + cond)
            else:
                print("WARN", role, m, "a person or group can invoke the relay" + cond)
    sys.exit(0)
print(",".join(bad) or "ok")
'
verdict(){ python3 -c "$CHECK_PY" "$@" 2>/dev/null || echo unreadable; }
# describe-then-create. An existing resource is read as JSON and must match; it is never updated.
existing_ok(){ # <label> <kind> <kind args...> -- <describe argv...>
  local label="$1" kind="$2"; shift 2
  local args=() json v
  while [ "$1" != -- ]; do args+=("$1"); shift; done; shift
  json=$("$GCLOUD" "$@" --format=json 2>/dev/null) || json=""
  v=$(printf '%s' "$json" | verdict "$kind" "${args[@]}")
  [ "$v" = ok ] || { echo "$label exists but does not match (mismatch: $v); refusing (nothing changed)" >&2; exit 1; }
}

# ── PREFLIGHT (reads only; every refusal here leaves NOTHING created or changed) ────────────────
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

echo "== project IAM: who else could invoke the relay"
POLICY_JSON=$("$GCLOUD" projects get-iam-policy "$PROJECT" --format=json) \
  || { echo "could not read $PROJECT's IAM policy; refusing (nothing changed)" >&2; exit 1; }
POLICY_LINES=$(printf '%s' "$POLICY_JSON" | verdict policy "serviceAccount:$GATEWAY_SA" "serviceAccount:$RELAY_SA" "$ACCEPTED")
case "$POLICY_LINES" in
  unreadable) echo "could not parse $PROJECT's IAM policy; refusing (nothing changed)" >&2; exit 1 ;;
esac
if [ -n "$POLICY_LINES" ]; then
  while IFS= read -r _line; do
    case "$_line" in
      REFUSE*) echo "  $_line" >&2 ;;
      WARN*) echo "  WARNING: ${_line#WARN }" >&2 ;;
      ACCEPTED*) echo "  $_line (accepted on this run only; narrow the project grant)" ;;
    esac
  done <<EOF
$POLICY_LINES
EOF
fi
# A here-string, not `printf | grep -q`: under pipefail grep -q's early exit can SIGPIPE the printf
# and turn a match into a pipeline failure - a refusal silently skipped.
if grep -q '^REFUSE' <<<"$POLICY_LINES"; then
  echo "$PROJECT's IAM policy lets more than $GATEWAY_SA invoke the relay (REFUSE lines above)." \
    "Narrow those grants, or list a service account in DOOR_RELAY_ACCEPTED_PROJECT_INVOKERS to accept it; refusing (nothing changed)" >&2
  exit 1
fi

PAYMENT_SCOPE=$("$GCLOUD" compute routers describe "$PAYMENT_ROUTER" --region "$REGION" --project "$PROJECT" \
  --format="value(nats[0].sourceSubnetworkIpRangesToNat)" 2>/dev/null || true)
if [ "$PAYMENT_SCOPE" = "ALL_SUBNETWORKS_ALL_IP_RANGES" ]; then
  echo "payment NAT still covers all subnets; run the guarded payment-NAT scope migration first (migrate_payment_nat_to_default_subnet.sh)" >&2
  exit 3
fi

# The gateway reaches the relay as INTERNAL only through Private Google Access on its own subnet.
# Warn, never fix: the default subnet is the payment lane, and turning PGA on there is its own
# reviewed step of the env-flip runbook.
GATEWAY_SUBNET_PGA=$("$GCLOUD" compute networks subnets describe "$NETWORK" --region "$REGION" --project "$PROJECT" \
  --format="value(privateIpGoogleAccess)" 2>/dev/null || true)
case "$GATEWAY_SUBNET_PGA" in
  True|true) ;;
  *) echo "WARNING: could not confirm Private Google Access on subnet '$NETWORK' (got '${GATEWAY_SUBNET_PGA:-unreadable}')." \
       "It MUST be on before MERCHANT_DOOR_RELAY=on, or the gateway's calls to the internal relay are refused" \
       "(its own reviewed step in the env-flip runbook; this script does not change it)." >&2 ;;
esac

# ── NETWORK: SUBNET, ADDRESS, ROUTER, NAT, FENCE ────────────────────────────────────────────────
echo "== subnet: $SUBNET"
if have "$GCLOUD" compute networks subnets describe "$SUBNET" --region "$REGION" --project "$PROJECT"; then
  existing_ok "$SUBNET" subnet "$CIDR" "$NETWORK" "$REGION" -- \
    compute networks subnets describe "$SUBNET" --region "$REGION" --project "$PROJECT"
else
  "$GCLOUD" compute networks subnets create "$SUBNET" --network "$NETWORK" --region "$REGION" --project "$PROJECT" \
    --range "$CIDR" --enable-private-ip-google-access
fi

echo "== address: $ADDR"
if have "$GCLOUD" compute addresses describe "$ADDR" --region "$REGION" --project "$PROJECT"; then
  existing_ok "$ADDR" address "$REGION" "$ROUTER" -- \
    compute addresses describe "$ADDR" --region "$REGION" --project "$PROJECT"
else
  "$GCLOUD" compute addresses create "$ADDR" --region "$REGION" --project "$PROJECT" \
    --description="Dedicated outbound address for the merchant-door relay (not for payment partners)"
fi

echo "== router: $ROUTER"
if have "$GCLOUD" compute routers describe "$ROUTER" --region "$REGION" --project "$PROJECT"; then
  existing_ok "$ROUTER" router "$NETWORK" "$REGION" -- \
    compute routers describe "$ROUTER" --region "$REGION" --project "$PROJECT"
else
  "$GCLOUD" compute routers create "$ROUTER" --network "$NETWORK" --region "$REGION" --project "$PROJECT"
fi

echo "== nat: $NAT"
if have "$GCLOUD" compute routers nats describe "$NAT" --router "$ROUTER" --region "$REGION" --project "$PROJECT"; then
  # Never updated. An existing NAT must cover ONLY our subnet with ONLY our address, manually, and
  # carry the port allocation below.
  existing_ok "$NAT" nat "$SUBNET" "$ADDR" "$NAT_MIN_PORTS" "$NAT_MAX_PORTS" -- \
    compute routers nats describe "$NAT" --router "$ROUTER" --region "$REGION" --project "$PROJECT"
else
  # Endpoint-independent mapping stays at its default (off), which dynamic port allocation needs.
  "$GCLOUD" compute routers nats create "$NAT" --router "$ROUTER" --region "$REGION" --project "$PROJECT" \
    --nat-custom-subnet-ip-ranges="$SUBNET" --nat-external-ip-pool="$ADDR" \
    --enable-dynamic-port-allocation --min-ports-per-vm="$NAT_MIN_PORTS" --max-ports-per-vm="$NAT_MAX_PORTS" \
    --enable-logging --log-filter=ERRORS_ONLY
fi

echo "== firewall: $FIREWALL"
# Created BEFORE the service, so the relay never runs without it. Both the subnet's range and the
# relay's network tag select it: the range covers every address the relay can egress from.
if have "$GCLOUD" compute firewall-rules describe "$FIREWALL" --project "$PROJECT"; then
  existing_ok "$FIREWALL" firewall "$NETWORK" "$CIDR" "$DENY_RANGES" "$NETWORK_TAG" "$FIREWALL_PRIORITY" -- \
    compute firewall-rules describe "$FIREWALL" --project "$PROJECT"
else
  "$GCLOUD" compute firewall-rules create "$FIREWALL" --project "$PROJECT" --network "$NETWORK" \
    --direction EGRESS --action DENY --rules all --priority "$FIREWALL_PRIORITY" \
    --source-ranges="$CIDR" --target-tags="$NETWORK_TAG" --destination-ranges="$DENY_RANGES" \
    --enable-logging \
    --description="The merchant-door relay never reaches private, shared or link-local addresses"
fi

# ── IDENTITY: sa-door-relay, NO ROLES (its project roles were checked in the preflight) ─────────
echo "== identity: $RELAY_SA"
have "$GCLOUD" iam service-accounts describe "$RELAY_SA" --project "$PROJECT" \
  || "$GCLOUD" iam service-accounts create "$RELAY_SA_NAME" --project "$PROJECT" \
       --display-name="Merchant-door relay (no roles, no secrets)" --quiet

# ── THE SERVICE ─────────────────────────────────────────────────────────────────────────────────
verb=created; have "$GCLOUD" run services describe "$SERVICE" --region "$REGION" --project "$PROJECT" && verb=updated
echo "== service: $SERVICE (will be $verb; ingress internal, min-instances 1, max-instances $MAX)"
# `gcloud run deploy` is create-or-update. --set-env-vars REPLACES the whole env, and --clear-secrets
# drops any secret a hand edit may have mounted, so every run leaves exactly these three metadata
# values and no secret. --invoker-iam-check is PINNED: internal ingress admits the whole VPC, so a
# hand-set --no-invoker-iam-check would make the relay an open proxy until someone noticed.
# min-instances 1: no cold start inside the gateway's 5 s door budget. all-traffic (not
# private-ranges-only): only then do public requests leave via the new NAT, and private ones meet
# the firewall rule above. --network-tags is what that rule targets.
"$GCLOUD" run deploy "$SERVICE" --project "$PROJECT" --region "$REGION" \
  --image "$IMAGE" \
  --command=node --args=src/services/doorRelay/server.js \
  --service-account "$RELAY_SA" \
  --network "$NETWORK" --subnet "$SUBNET" --network-tags="$NETWORK_TAG" --vpc-egress all-traffic \
  --ingress internal --no-allow-unauthenticated --invoker-iam-check \
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
echo "  egress:  subnet $SUBNET (tag $NETWORK_TAG) -> $NAT -> $ADDR; private ranges denied by $FIREWALL"
echo
echo "FOLLOW-UP 1 (via the controller, never publicly): the relay's egress address is $EGRESS_IP."
echo "  Do not give it to payment partners; do not commit it; do not deploy anything else to $SUBNET."
echo "FOLLOW-UP 2 (a separate, reviewed gateway env change via the controller - NOT done here):"
echo "  precondition: Private Google Access ON for subnet '$NETWORK' (its own reviewed step)."
echo "  MERCHANT_DOOR_RELAY_URL=$RELAY_URL"
echo "  MERCHANT_DOOR_RELAY=on"
echo "  staging first; in prod it is the step that moves live door calls, with a watch on door error rates."
