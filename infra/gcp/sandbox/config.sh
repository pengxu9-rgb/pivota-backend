# shellcheck shell=bash
# Sourced by every script in infra/gcp/sandbox/. Constants for the partner sandbox, and the
# dry-run / apply switch every mutating script obeys.
#
# THE SANDBOX IS ITS OWN GCP PROJECT (owner decision, 2026-11-25). Nothing here may name
# pivota-prod or pivota-staging as a target. The only cross-project bindings the sandbox gets are:
#   - read of the shared image registry (pivota-shared/pivota and pivota-shared/pivota-gateway)
#   - ONE prod service account allowed to CREATE objects in the sandbox's catalog drop bucket
#     (setup_catalog_drop.sh, its own step and its own go)
# and verify_sandbox.sh checks that no other binding exists in either direction.
#
# DRY RUN IS THE DEFAULT. Every mutating gcloud call goes through `run`, which only PRINTS the
# command unless APPLY=1. Read calls (`describe`, `list`) always run, so a dry run shows what
# would be created against the real current state. A dry run creates nothing.

SANDBOX_PROJECT="${SANDBOX_PROJECT:-pivota-sandbox}"
REGION=us-west1
SHARED_PROJECT=pivota-shared
ORG_ID=867785023602            # the same org as pivota-prod / pivota-staging (infra/gcp/README.md)

# Hard refusal: whatever SANDBOX_PROJECT says, it can never be one of the real environments.
# 1. It must be a project ID in GCP's exact format: 6-30 chars, a lowercase letter first, then
#    lowercase letters, digits and hyphens, no trailing hyphen. gcloud's --project also accepts a
#    project NUMBER (pivota-prod's number names pivota-prod), and a value with spaces or capitals
#    would not match the names below; the format rules all of those out before any name check.
# 2. It may not be, or contain as a hyphen-separated word, a name of a real environment.
_sbx_refuse(){ printf 'refusing: SANDBOX_PROJECT=%q is not a sandbox project (%s)\n' "$SANDBOX_PROJECT" "$1" >&2; exit 2; }
[[ "$SANDBOX_PROJECT" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] \
  || _sbx_refuse "not a GCP project id: lowercase letter first, 6-30 of [a-z0-9-]; project numbers are refused"
case "$SANDBOX_PROJECT" in
  pivota-prod|pivota-staging|pivota-shared) _sbx_refuse "a real environment" ;;
esac
for _w in ${SANDBOX_PROJECT//-/ }; do
  case "$_w" in prod|production|staging|stage|stg|shared) _sbx_refuse "names a real environment ('$_w')" ;; esac
done
unset _w

# Hostnames. The gateway host is fixed by the design doc; the AgenPaz host is a proposal
# (RUNBOOK.md, open question Q6) and can be changed here before the LB step runs.
GATEWAY_HOST="${GATEWAY_HOST:-sandbox.mcp.pivota.cc}"
AGENPAZ_HOST="${AGENPAZ_HOST:-sandbox.agenpaz.com}"
# An override may not name a real host: setup_sandbox_lb.sh mints a certificate, a DNS
# authorization and a host rule for whatever these say. Each must be a plain lowercase hostname
# under one of our zones, with a label that contains "sandbox"; the two must differ.
_sbx_host_ok(){
  local h="$1" z zone_ok=0 label
  [[ "$h" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$ ]] || return 1
  for z in pivota.cc agenpaz.com agenpas.com; do
    case "$h" in *."$z") zone_ok=1 ;; esac
  done
  [ "$zone_ok" = 1 ] || return 1
  # Never under a prod host that sets cookies or is exclusive (sandbox.pay.agenpaz.com would sit
  # under the real payment page's name). mcp.pivota.cc is the design doc's parent for the gateway.
  case "$h" in
    *.pay.agenpaz.com|*.pay.agenpas.com|*.api.pivota.cc|*.gateway.pivota.cc|*.agent.pivota.cc|*.agents.pivota.cc|*.developer.pivota.cc) return 1 ;;
    *.ucp.pivota.cc|*.acp.pivota.cc|*.commerce.mcp.pivota.cc) return 1 ;;   # prod door hosts (check_profile's list)
  esac
  for label in ${h//./ }; do case "$label" in *sandbox*) return 0 ;; esac; done
  return 1
}
_sbx_host_ok "$GATEWAY_HOST" || { printf 'refusing: GATEWAY_HOST=%q is not a sandbox host (a label with "sandbox", under pivota.cc/agenpaz.com/agenpas.com)\n' "$GATEWAY_HOST" >&2; exit 2; }
_sbx_host_ok "$AGENPAZ_HOST" || { printf 'refusing: AGENPAZ_HOST=%q is not a sandbox host (a label with "sandbox", under pivota.cc/agenpaz.com/agenpas.com)\n' "$AGENPAZ_HOST" >&2; exit 2; }
[ "$GATEWAY_HOST" != "$AGENPAZ_HOST" ] || { echo "refusing: GATEWAY_HOST and AGENPAZ_HOST must differ (the AgenPaz host is exclusive)" >&2; exit 2; }

# Network
VPC=sandbox-vpc
SUBNET=sandbox-run
PSA_RANGE_NAME=sandbox-psa
# The two private ranges are NOT in the repo (it is public; the owner's rule: no private IPs). They
# live in an untracked, gitignored file, config.local.sh beside this one (or SANDBOX_LOCAL_CONFIG),
# copied from config.local.example.sh and filled in by the operator:
#   SUBNET_RANGE     the Cloud Run subnet, a /24-/28 private range
#   PSA_RANGE_START  the start of the /20 for Cloud SQL's private IP. It must be the range
#                    sandbox-backend's config/sandbox_identity.py holds (as a hash): its guard
#                    refuses a DATABASE_URL anywhere else. Pinned, not auto-allocated, and apart
#                    from prod's and staging's ranges.
SANDBOX_LOCAL_CONFIG="${SANDBOX_LOCAL_CONFIG:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/config.local.sh}"
if [ ! -e "$SANDBOX_LOCAL_CONFIG" ] && [ ! -L "$SANDBOX_LOCAL_CONFIG" ]; then
  printf 'refusing: %s is missing. Copy config.local.example.sh to it and fill in the two private ranges (never commit it).\n' "$SANDBOX_LOCAL_CONFIG" >&2
  exit 2
fi
# A plain file only (a FIFO, a device or a directory is refused as what it is, not as "missing");
# not writable by group or others (anyone who could edit it could re-aim every script); and no NUL
# byte (bash's `read` drops a NUL silently, so the line it parses is not the line in the file).
if [ ! -f "$SANDBOX_LOCAL_CONFIG" ]; then
  printf 'refusing: %s is not a regular file\n' "$SANDBOX_LOCAL_CONFIG" >&2; exit 2
fi
# -L: judge what a symlink POINTS AT (the file actually read), not the link's own mode (review).
if [ -n "$(find -L "$SANDBOX_LOCAL_CONFIG" -prune \( -perm -g+w -o -perm -o+w \) -print 2>/dev/null)" ]; then
  printf 'refusing: %s is writable by group or others (chmod go-w it)\n' "$SANDBOX_LOCAL_CONFIG" >&2; exit 2
fi
if [ "$(wc -c < "$SANDBOX_LOCAL_CONFIG")" != "$(LC_ALL=C tr -d '\000' < "$SANDBOX_LOCAL_CONFIG" | wc -c)" ]; then
  printf 'refusing: %s holds a NUL byte\n' "$SANDBOX_LOCAL_CONFIG" >&2; exit 2
fi
# The file is DATA, never shell: it is PARSED here, not sourced (review: a sourced file ran after
# every guard above and could reset SANDBOX_PROJECT=pivota-prod, GATEWAY_HOST, or define a git()
# function). Accepted: blank lines, lines starting with #, and exactly one line each of
#   SUBNET_RANGE=<a.b.c.d/nn>
#   PSA_RANGE_START=<a.b.c.d>
# with nothing else on the line (no quotes, spaces, `;` or `$`). Any other line refuses the run.
SUBNET_RANGE="" PSA_RANGE_START=""
_sbx_local_refuse(){ printf 'refusing: %s line %s: %s\n' "$SANDBOX_LOCAL_CONFIG" "$1" "$2" >&2; exit 2; }
_sbx_n=0 _sbx_seen_s=0 _sbx_seen_p=0
while IFS= read -r _sbx_line || [ -n "$_sbx_line" ]; do
  _sbx_n=$((_sbx_n + 1))
  case "$_sbx_line" in ''|'#'*) continue ;; esac
  if [[ "$_sbx_line" =~ ^SUBNET_RANGE=([0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2})$ ]]; then
    [ "$_sbx_seen_s" = 0 ] || _sbx_local_refuse "$_sbx_n" "SUBNET_RANGE is set twice"
    SUBNET_RANGE="${BASH_REMATCH[1]}"; _sbx_seen_s=1
  elif [[ "$_sbx_line" =~ ^PSA_RANGE_START=([0-9]{1,3}(\.[0-9]{1,3}){3})$ ]]; then
    [ "$_sbx_seen_p" = 0 ] || _sbx_local_refuse "$_sbx_n" "PSA_RANGE_START is set twice"
    PSA_RANGE_START="${BASH_REMATCH[1]}"; _sbx_seen_p=1
  else
    _sbx_local_refuse "$_sbx_n" "only SUBNET_RANGE=<a.b.c.d/nn> and PSA_RANGE_START=<a.b.c.d> lines are allowed (the file is parsed, never run)"
  fi
done < "$SANDBOX_LOCAL_CONFIG"
unset _sbx_n _sbx_seen_s _sbx_seen_p _sbx_line
# Both private (RFC 1918 only, not merely "not public"), the subnet a /24-/28, the PSA a /20 on its
# own boundary, and no overlap.
if ! python3 -I -c '
import ipaddress, sys
rfc1918 = [ipaddress.ip_network((a << 24 | b << 16, n)) for a, b, n in ((10, 0, 8), (172, 16, 12), (192, 168, 16))]
net = ipaddress.ip_network(sys.argv[1])
psa = ipaddress.ip_network(sys.argv[2] + "/20")
private = lambda n: any(n.subnet_of(b) for b in rfc1918)
ok = private(net) and private(psa) and 24 <= net.prefixlen <= 28 and not net.overlaps(psa)
sys.exit(0 if ok else 1)' "$SUBNET_RANGE" "$PSA_RANGE_START" 2>/dev/null; then
  printf 'refusing: %s must set SUBNET_RANGE (an RFC 1918 /24-/28) and PSA_RANGE_START (the start of an RFC 1918 /20 that does not overlap it)\n' "$SANDBOX_LOCAL_CONFIG" >&2
  exit 2
fi
ROUTER=sandbox-router
NAT=sandbox-nat
EGRESS_IP_NAME=sandbox-egress-ip

# Cloud SQL. db-g1-small (shared core, 1.7 GB, max_connections 50) is enough for a catalog copy
# and a handful of partners; SQL_TIER=db-custom-1-3840 is the staging size if imports are slow.
SQL_INSTANCE=sandbox-pg
SQL_TIER="${SQL_TIER:-db-g1-small}"
DB_NAME=pivota
DB_USER=pivota

# Catalog drop bucket (prod export writes, sandbox import reads). See setup_catalog_drop.sh.
# NOT overridable: derived from SANDBOX_PROJECT only. An arbitrary DROP_BUCKET would let
# setup_catalog_drop.sh put a delete lifecycle and new IAM on any bucket, around the project guard.
# setup_catalog_drop.sh also checks the bucket's project NUMBER before changing anything.
_sbx_drop="${SANDBOX_PROJECT}-catalog-drop"
if [ -n "${DROP_BUCKET:-}" ] && [ "$DROP_BUCKET" != "$_sbx_drop" ]; then
  printf 'refusing: DROP_BUCKET=%q; the drop bucket is always %s (derived from SANDBOX_PROJECT)\n' "$DROP_BUCKET" "$_sbx_drop" >&2; exit 2
fi
DROP_BUCKET="$_sbx_drop"; unset _sbx_drop
PROD_EXPORT_SA="sa-sandbox-export@pivota-prod.iam.gserviceaccount.com"

# Service accounts, one per workload, so each secret can be granted to exactly who reads it.
SA_GATEWAY="sa-gateway@${SANDBOX_PROJECT}.iam.gserviceaccount.com"
SA_BACKEND="sa-backend@${SANDBOX_PROJECT}.iam.gserviceaccount.com"
SA_WORKER="sa-worker@${SANDBOX_PROJECT}.iam.gserviceaccount.com"
SA_AGENPAZ="sa-agenpaz@${SANDBOX_PROJECT}.iam.gserviceaccount.com"
SA_IMPORT="sa-catalog-import@${SANDBOX_PROJECT}.iam.gserviceaccount.com"
# One-off jobs only (RUNBOOK steps 10b and 14): the gateway schema migration and the gateway
# service-agent registration. No Cloud Run service runs as it.
SA_OPS="sa-sandbox-ops@${SANDBOX_PROJECT}.iam.gserviceaccount.com"

# The sandbox gateway's service agent id (not a secret). env/web.yaml and env/worker.yaml carry it in
# ISSUING_AGENT_EXCLUDED_AGENT_IDS / ISSUING_ASSERTION_VOUCHER_AGENT_IDS; RUNBOOK step 14 registers
# PIVOTA_API_KEY's hash under exactly this id. check_profile.py checks all three agree.
SANDBOX_GATEWAY_AGENT_ID=agent_388a63cd97b4fb54

# Registry
BACKEND_IMAGE_REPO="${REGION}-docker.pkg.dev/${SHARED_PROJECT}/pivota/backend"

HERE_SANDBOX="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GCLOUD="${GCLOUD:-gcloud}"
: "${APPLY:=0}"
case "$APPLY" in 0|1) ;; *) echo "APPLY must be 0 or 1 (got '$APPLY')" >&2; exit 2 ;; esac

say(){ printf '\n\033[1;34m== %s\033[0m\n' "$*"; }
have(){ "$@" >/dev/null 2>&1; }
# Every mutating call goes through here. In a dry run it prints a copy-pasteable command.
run(){
  # `run g ...` prints and runs the expanded command, so a dry run shows the real --project.
  if [ "${1:-}" = g ]; then shift; set -- "$GCLOUD" --project "$SANDBOX_PROJECT" "$@"; fi
  if [ "$APPLY" = 1 ]; then
    "$@"
  else
    # stderr, so a caller's `>/dev/null` (meant for gcloud's own output) cannot hide the plan.
    { printf '  [dry-run]'; printf ' %q' "$@"; printf '\n'; } >&2
  fi
}
# IAM is eventually consistent for new service accounts; retry instead of failing the run.
retry(){
  [ "$APPLY" = 1 ] || { run "$@"; return 0; }
  local n=0; until "$@"; do n=$((n+1)); [ $n -ge 8 ] && return 1; sleep $((n*5)); done
}
# Every gcloud call in these scripts names the project explicitly. Never rely on the
# operator's `gcloud config` default, which may be pivota-prod.
g(){ "$GCLOUD" --project "$SANDBOX_PROJECT" "$@"; }

mode_banner(){
  if [ "$APPLY" = 1 ]; then
    echo "APPLY=1: changes WILL be made in project $SANDBOX_PROJECT"
  else
    echo "dry run (APPLY=0): nothing will be created; mutating commands are printed only"
  fi
}
