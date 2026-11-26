#!/usr/bin/env bash
# Deploy ONE tagged sandbox release to pivota-sandbox.
#
#   GATEWAY_REPO_DIR=<PIVOTA-Agent checkout> infra/gcp/sandbox/deploy_sandbox.sh sandbox-r3            # dry run
#   GATEWAY_REPO_DIR=<PIVOTA-Agent checkout> APPLY=1 infra/gcp/sandbox/deploy_sandbox.sh sandbox-r3    # controller + owner go
#   SERVICES="gateway" ...   deploy a subset (default: web worker gateway agenpaz, in that order)
#
# WHAT A RELEASE IS. An ANNOTATED tag `sandbox-rN`, with the SAME name, in BOTH repositories:
#   pivota-backend  -> the backend commit (image pivota-shared/pivota/backend:<sha>; web + worker)
#   PIVOTA-Agent    -> the gateway commit (image resolved by infra/gcp/_gateway_image.sh; gateway + agenpaz)
# Both commits must already be on their repo's origin/main, and both images must already exist
# (they are built on merge). Nothing here builds. A merge to main never deploys the sandbox: only
# pushing a new tag pair and running this script does, so partners get a stable sandbox while
# staging and prod move many times a day. Pushing the tags is itself a controller step.
#
# WHICH CONFIG. The backend TAG's own infra/gcp/sandbox/, never this checkout's (review S4, step 1b):
# this copy resolves the tags, then runs the tag commit's deploy_sandbox.sh, which verifies it holds
# exactly that commit's files. Only a dry run without tags uses the working tree, and says so.
#
# HOW A SERVICE IS ROLLED. Image pinned by DIGEST. Env = the release's profile env/<svc>.yaml,
# rendered and re-checked by check_profile.py (R1-R17) - CONFIG is always "apply" here because
# the whole config is in git and reviewed; there is no hand-held state to preserve. Secrets from
# secrets/<svc>.list. A new revision goes out at 0% under tag c-<sha12>, is health-checked from
# INSIDE the sandbox VPC, and only then takes 100%, by revision NAME (never --to-latest).
# The previous serving revision of every service is written to a rollback file before promotion.
set -euo pipefail
# shellcheck source=infra/gcp/sandbox/config.sh
. "$(cd "$(dirname "$0")" && pwd)/config.sh"
# shellcheck source=infra/gcp/_gateway_image.sh
. "$HERE_SANDBOX/../_gateway_image.sh"

REL="${1:-}"
[[ "$REL" =~ ^sandbox-r[0-9]+$ ]] || { echo "usage: $0 sandbox-r<N>   (APPLY=1 to execute)" >&2; exit 2; }
SERVICES="${SERVICES:-web worker gateway agenpaz}"
BACKEND_REPO_DIR="${BACKEND_REPO_DIR:-$(cd "$HERE_SANDBOX/../../.." && pwd)}"
: "${GATEWAY_REPO_DIR:?set GATEWAY_REPO_DIR to a PIVOTA-Agent checkout (its origin is read, nothing is changed)}"

say "sandbox deploy $REL -> $SANDBOX_PROJECT ($SERVICES)"; mode_banner

# ------------------------------------------------------------------- 1. resolve the release
# Which repository each directory really fetches from (review: BACKEND_REPO_DIR's origin was not
# pinned). `git remote get-url` is the URL fetch USES (insteadOf rewrites expanded). Its last path
# part must be the repository's name, and two github.com origins must have the same owner (checked
# after both resolve). The owner is not written here: the public repo names no person. This guards
# against an ACCIDENTALLY wrong remote only: two non-GitHub remotes have no owner to compare, and a
# fork of both repos under one owner passes (RUNBOOK step 11).
origin_of(){ git -C "$1" remote get-url origin 2>/dev/null; }
repo_name_of(){ local u="${1%/}"; u="${u%.git}"; u="${u##*/}"; u="${u##*:}"; printf '%s\n' "$u" | tr '[:upper:]' '[:lower:]'; }
github_owner_of(){ # url -> owner when the url is github.com, else nothing
  local u="$1"
  case "$u" in
    https://github.com/*) u="${u#https://github.com/}" ;;
    git@github.com:*) u="${u#git@github.com:}" ;;
    ssh://git@github.com/*) u="${u#ssh://git@github.com/}" ;;
    *) return 0 ;;
  esac
  printf '%s\n' "${u%%/*}" | tr '[:upper:]' '[:lower:]'
}
resolve_tag(){ # repo_dir expected_repo_name -> peeled commit sha of an ANNOTATED tag on origin, on origin/main
  local dir="$1" want="$2" url lines obj peeled
  url=$(origin_of "$dir") || { echo "$dir has no origin remote" >&2; return 1; }
  [ "$(repo_name_of "$url")" = "$(printf '%s' "$want" | tr '[:upper:]' '[:lower:]')" ] \
    || { echo "refusing: $dir fetches from $url, which is not the $want repository" >&2; return 1; }
  lines=$(git -C "$dir" ls-remote origin "refs/tags/$REL" "refs/tags/$REL^{}")
  obj=$(printf '%s\n' "$lines" | awk -v t="refs/tags/$REL" '$2==t{print $1}')
  peeled=$(printf '%s\n' "$lines" | awk -v t="refs/tags/$REL^{}" '$2==t{print $1}')
  [ -n "$obj" ] || { echo "no tag $REL on $(git -C "$dir" remote get-url origin)" >&2; return 1; }
  [ -n "$peeled" ] || { echo "tag $REL in $dir is lightweight; a release tag must be annotated (git tag -a)" >&2; return 1; }
  git -C "$dir" fetch --quiet origin main
  git -C "$dir" cat-file -e "$peeled^{commit}" 2>/dev/null || git -C "$dir" fetch --quiet origin "refs/tags/$REL:refs/tags/$REL"
  git -C "$dir" merge-base --is-ancestor "$peeled" origin/main \
    || { echo "tag $REL in $dir points at $peeled, which is not on origin/main" >&2; return 1; }
  printf '%s\n' "$peeled"
}
same_owner(){
  local b g; b=$(github_owner_of "$(origin_of "$BACKEND_REPO_DIR")"); g=$(github_owner_of "$(origin_of "$GATEWAY_REPO_DIR")")
  [ "$b" = "$g" ] || { echo "refusing: the backend origin's owner '${b:-<not github>}' and the gateway origin's '${g:-<not github>}' differ" >&2; return 1; }
}
if BACKEND_SHA=$(resolve_tag "$BACKEND_REPO_DIR" pivota-backend) && GATEWAY_SHA=$(resolve_tag "$GATEWAY_REPO_DIR" PIVOTA-Agent) \
   && same_owner; then
  echo "  backend $BACKEND_SHA"; echo "  gateway $GATEWAY_SHA"
elif [ "$APPLY" = 1 ]; then
  exit 1
else
  echo "  [dry-run] release tags not found; continuing the plan with placeholder commits"
  BACKEND_SHA=$(printf '0%.0s' {1..40}); GATEWAY_SHA=$BACKEND_SHA
fi

# ------------------------------------------------------------------- 1b. the release's OWN deploy config
# Review S4: what is deployed must be what was tagged, not whatever this checkout holds. The env
# profiles, secret lists, check_profile.py, the service shapes below and this script itself all
# live in infra/gcp/sandbox/ of the BACKEND tag's commit (the gateway and agenpaz profiles too).
# So once the tag resolves, this copy only hands over: it writes that commit's infra/gcp/sandbox/
# and infra/gcp/_gateway_image.sh from git objects (never the working tree, never `git archive`,
# whose export-ignore attributes could drop a file) into a temp dir and runs THAT deploy script.
# The working tree is used only for a dry run with no tag, and says so. The operator's untracked
# config.local.sh is not part of a release and is passed along by path.
extract_release_tree(){ # sha dest -> the sandbox config of commit sha, files and exec bits as committed
  local sha="$1" dest="$2" entry meta path mode type obj n=0
  while IFS= read -r -d '' entry; do
    meta="${entry%%$'\t'*}"; path="${entry#*$'\t'}"
    read -r mode type obj <<<"$meta"
    case "$type:$mode" in
      blob:100644|blob:100755) ;;
      *) echo "refusing: $path in $sha is a $type ($mode); a release's sandbox config holds plain files only" >&2; return 1 ;;
    esac
    mkdir -p "$dest/$(dirname "$path")"
    git -C "$BACKEND_REPO_DIR" cat-file blob "$obj" > "$dest/$path"
    [ "$mode" = 100755 ] && chmod 755 "$dest/$path"
    n=$((n + 1))
  done < <(git -C "$BACKEND_REPO_DIR" ls-tree -r -z "$sha" -- infra/gcp/sandbox infra/gcp/_gateway_image.sh)
  [ -f "$dest/infra/gcp/sandbox/deploy_sandbox.sh" ] && [ -f "$dest/infra/gcp/_gateway_image.sh" ] || {
    echo "refusing: backend $sha has no infra/gcp/sandbox/deploy_sandbox.sh or infra/gcp/_gateway_image.sh ($n files); tag a commit that carries the sandbox config" >&2
    return 1; }
}
verify_release_tree(){ # sha root -> 0 when root's sandbox config is byte-for-byte the commit's, nothing more
  local sha="$1" root="$2" entry meta path mode type obj bad=0 want got
  want=$(mktemp "${TMPDIR:-/tmp}/sbx-want.XXXXXX"); got=$(mktemp "${TMPDIR:-/tmp}/sbx-got.XXXXXX")
  while IFS= read -r -d '' entry; do
    meta="${entry%%$'\t'*}"; path="${entry#*$'\t'}"
    read -r mode type obj <<<"$meta"
    printf '%s\n' "$path" >> "$want"
    if [ -L "$root/$path" ] || [ ! -f "$root/$path" ] \
       || [ "$(git -C "$BACKEND_REPO_DIR" hash-object --no-filters "$root/$path")" != "$obj" ]; then
      echo "  $path differs from $sha" >&2; bad=1
    elif [ "$mode" = 100755 ] && [ ! -x "$root/$path" ]; then
      echo "  $path lost its exec bit" >&2; bad=1
    fi
  done < <(git -C "$BACKEND_REPO_DIR" ls-tree -r -z "$sha" -- infra/gcp/sandbox infra/gcp/_gateway_image.sh)
  # Nothing the commit does not hold may sit beside it (a stray secrets/*.list or env/*.yaml would be
  # read). Only the operator's untracked config.local.sh and Python's bytecode caches are allowed.
  (cd "$root" && find infra/gcp/sandbox ! -type d ! -path '*/__pycache__/*' ! -path infra/gcp/sandbox/config.local.sh) >> "$got"
  [ -e "$root/infra/gcp/_gateway_image.sh" ] && echo infra/gcp/_gateway_image.sh >> "$got"
  if ! diff <(LC_ALL=C sort "$want") <(LC_ALL=C sort "$got") >/dev/null; then
    echo "  the file set differs from $sha:" >&2
    diff <(LC_ALL=C sort "$want") <(LC_ALL=C sort "$got") | sed -n 's/^[<>] /    /p' >&2 || true
    bad=1
  fi
  rm -f "$want" "$got"
  return "$bad"
}
if [ -n "${SANDBOX_DEPLOY_TREE_OF:-}" ]; then
  # This IS the release's copy, started by a working-tree copy below. The tag must not have moved
  # between the two resolutions, and the files this copy reads must be exactly the tag's.
  [ "$SANDBOX_DEPLOY_TREE_OF" = "$BACKEND_SHA" ] || {
    echo "refusing: running the deploy config of $SANDBOX_DEPLOY_TREE_OF but $REL now resolves to $BACKEND_SHA" >&2; exit 1; }
  verify_release_tree "$BACKEND_SHA" "$(cd "$HERE_SANDBOX/../../.." && pwd)" || {
    echo "refusing: $HERE_SANDBOX is not $REL's sandbox config (backend $BACKEND_SHA)" >&2; exit 1; }
  echo "  deploy config: infra/gcp/sandbox as committed at backend $BACKEND_SHA ($REL), verified file by file"
elif [ "$BACKEND_SHA" = "$(printf '0%.0s' {1..40})" ]; then
  echo "  [dry-run] deploy config: this WORKING TREE (no release tag); an APPLY run always uses the tag's own copy"
else
  RELEASE_TREE=$(mktemp -d "${TMPDIR:-/tmp}/sbx-release.XXXXXX")
  trap 'rm -rf "$RELEASE_TREE"' EXIT
  trap 'exit 130' INT TERM
  extract_release_tree "$BACKEND_SHA" "$RELEASE_TREE" || exit 1
  echo "  handing over to $REL's own deploy script (backend $BACKEND_SHA)"
  rc=0
  SANDBOX_DEPLOY_TREE_OF="$BACKEND_SHA" SANDBOX_LOCAL_CONFIG="$SANDBOX_LOCAL_CONFIG" \
    BACKEND_REPO_DIR="$(cd "$BACKEND_REPO_DIR" && pwd)" GATEWAY_REPO_DIR="$(cd "$GATEWAY_REPO_DIR" && pwd)" \
    "$BASH" "$RELEASE_TREE/infra/gcp/sandbox/deploy_sandbox.sh" "$REL" || rc=$?
  exit "$rc"
fi

# ------------------------------------------------------------------- 2. images, by digest
digest_of(){ "$GCLOUD" artifacts docker images describe "$1" --format='value(image_summary.digest)' 2>/dev/null; }
BACKEND_IMAGE="$BACKEND_IMAGE_REPO:$BACKEND_SHA"
GW_IMAGE=$(resolve_gateway_image "$GATEWAY_SHA") || GW_IMAGE=""
BD=$(digest_of "$BACKEND_IMAGE" || true); GD=""; [ -n "$GW_IMAGE" ] && GD=$(digest_of "$GW_IMAGE" || true)
if [ -z "$BD" ] || [ -z "$GD" ]; then
  [ "$APPLY" = 1 ] && { echo "missing image: backend='${BD:-none}' gateway='${GD:-none}' - pick a release whose commits were built" >&2; exit 1; }
  echo "  [dry-run] images not found for the placeholder commits (expected in a dry run)"
  BD="sha256:<backend>"; GD="sha256:<gateway>"; GW_IMAGE="${GW_IMAGE:-$REGION-docker.pkg.dev/$SHARED_PROJECT/pivota-gateway/gateway:$GATEWAY_SHA}"
fi
BACKEND_PIN="${BACKEND_IMAGE%:*}@$BD"; GATEWAY_PIN="${GW_IMAGE%:*}@$GD"
echo "  web/worker image  $BACKEND_PIN"; echo "  gateway/agenpaz   $GATEWAY_PIN"

# ------------------------------------------------------------------- 3. preconditions in the project
PN=$(g projects describe "$SANDBOX_PROJECT" --format='value(projectNumber)' 2>/dev/null || true)
if [ -z "$PN" ]; then
  [ "$APPLY" = 1 ] && { echo "project $SANDBOX_PROJECT not found" >&2; exit 1; }
  PN=000000000000; echo "  [dry-run] project not created yet; using project number $PN for the plan"
fi
missing=0
for s in $(cat "$HERE_SANDBOX"/secrets/*.list | sed 's/^[^=]*=//' | sort -u); do
  if ! grep -q . <<<"$(g secrets versions list "$s" --filter='state=ENABLED' --limit=1 --format='value(name)' 2>/dev/null)"; then
    echo "  secret $s has no enabled version"; missing=1
  fi
done
[ "$missing" = 0 ] || [ "$APPLY" != 1 ] || { echo "secrets missing (REAP_API_KEY is added by the owner, runbook step 4)" >&2; exit 1; }

# ------------------------------------------------------------------- 4. render + check the profiles
WEB_URL="https://web-$PN.$REGION.run.app"
RENDER=$(mktemp -d); trap 'rm -rf "$RENDER"' EXIT
python3 "$HERE_SANDBOX/check_profile.py" --render "$RENDER" --web-url "$WEB_URL" \
  --gateway-host "$GATEWAY_HOST" --agenpaz-host "$AGENPAZ_HOST" --sandbox-project "$SANDBOX_PROJECT"

# ------------------------------------------------------------------- 5. per-service shape
# svc        image          sa           ingress                              public min max cpu mem  conc throttle
shape(){
  case "$1" in
    web)     echo "$BACKEND_PIN $SA_BACKEND internal                          1 1 2 1 2Gi 20 no" ;;
    worker)  echo "$BACKEND_PIN $SA_WORKER  internal                          0 1 1 1 1Gi 10 no" ;;
    gateway) echo "$GATEWAY_PIN $SA_GATEWAY internal-and-cloud-load-balancing 1 1 2 1 2Gi 40 no" ;;
    agenpaz) echo "$GATEWAY_PIN $SA_AGENPAZ internal-and-cloud-load-balancing 1 0 2 1 1Gi 40 yes" ;;
    *) echo "unknown service $1" >&2; exit 2 ;;
  esac
}
# web is invokable by allUsers but ingress=internal, as on staging: the gateway calls the
# introspect route without an ID token (infra/gcp/deploy_backend.sh). Only the VPC can reach it.

ROLLBACK_DIR="${SANDBOX_ROLLBACK_DIR:-$HOME/.pivota-sandbox-deploys/$SANDBOX_PROJECT}"; ROLLBACK_FILE="$ROLLBACK_DIR/$(date -u +%Y%m%dT%H%M%SZ)-$REL.rollback"
[ "$APPLY" = 1 ] && { mkdir -p "$ROLLBACK_DIR"; : > "$ROLLBACK_FILE"; }

probe_in_vpc(){ # url audience -> 0 when the app answers 200, from inside the sandbox VPC
  # The token's audience must be the SERVICE url even when the tag url is called (a tag-url
  # audience is refused 401; measured on staging, infra/gcp/deploy_backend.sh). Public services
  # ignore the header; the private worker needs it (sa-worker holds run.invoker on it).
  local url="$1" aud="$2"
  local py='import sys,urllib.request
t=urllib.request.urlopen(urllib.request.Request("http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity?audience="+sys.argv[2],headers={"Metadata-Flavor":"Google"}),timeout=10).read().decode()
r=urllib.request.urlopen(urllib.request.Request(sys.argv[1],headers={"Authorization":"Bearer "+t}),timeout=30)
sys.exit(0 if r.status==200 else 1)'
  # This function is called as `probe_in_vpc ... || {...}`, which turns set -e OFF inside it. Every
  # step therefore returns on its own failure: a failed `jobs deploy` must never fall through to
  # `jobs execute` of the PREVIOUS job definition (another URL) and report that one's success.
  run g run jobs deploy sandbox-probe --region "$REGION" --image "$BACKEND_PIN" \
    --service-account "$SA_WORKER" --network "$VPC" --subnet "$SUBNET" --vpc-egress all-traffic \
    --max-retries 0 --task-timeout 120s --command python --args="^|^-c|$py|$url|$aud" --quiet >/dev/null \
    || { echo "probe job deploy failed" >&2; return 1; }
  run g run jobs execute sandbox-probe --region "$REGION" --wait --quiet >/dev/null \
    || { echo "probe job execution failed" >&2; return 1; }
}

serving_revision(){ # svc -> the revision holding 100% of traffic, empty when the service does not exist
  g run services describe "$1" --region "$REGION" --format=json 2>/dev/null | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception: sys.exit(0)
st=d.get("status",{})
full=[t for t in st.get("traffic",[]) if t.get("percent")==100]
print((full[0].get("revisionName") if full else None) or st.get("latestReadyRevisionName") or "")' || true
}

deploy_one(){
  local svc="$1" image sa ingress public min max cpu mem conc throttle
  read -r image sa ingress public min max cpu mem conc throttle <<<"$(shape "$svc")"
  # sa-sandbox-ops exists for two one-off jobs (RUNBOOK 10b, 14a) and holds the gateway's key; no
  # long-lived service may ever run as it.
  [ "$sa" != "$SA_OPS" ] || { echo "refusing: $svc would run as $SA_OPS (one-off jobs only)" >&2; exit 2; }
  local envf="$RENDER/$svc.yaml" secrets tag="c-${BACKEND_SHA:0:12}" prev=""
  [ "$svc" = gateway ] || [ "$svc" = agenpaz ] && tag="c-${GATEWAY_SHA:0:12}"
  local sha="$BACKEND_SHA"; { [ "$svc" = gateway ] || [ "$svc" = agenpaz ]; } && sha="$GATEWAY_SHA"
  printf 'PIVOTA_SERVICE_NAME: "%s"\nPIVOTA_COMMIT_SHA: "%s"\nSANDBOX_RELEASE: "%s"\n' "$svc" "$sha" "$REL" >> "$envf"
  secrets=$(sed 's/^\([^=]*\)=\(.*\)$/\1=\2:latest/' "$HERE_SANDBOX/secrets/$svc.list" | paste -sd, -)

  say "$svc: $image"
  local traffic_args=()
  prev=$(serving_revision "$svc")
  if [ -n "$prev" ]; then
    traffic_args=(--no-traffic --tag "$tag")
    echo "  serving now: $prev (kept until the candidate passes)"
  else
    echo "  first deploy of $svc: it takes traffic directly (nothing routes to it before DNS round 2)"
  fi
  local auth=--no-allow-unauthenticated; [ "$public" = 1 ] && auth=--allow-unauthenticated
  local cpu_flag=--no-cpu-throttling; [ "$throttle" = yes ] && cpu_flag=--cpu-throttling
  run g run deploy "$svc" --region "$REGION" --image "$image" --service-account "$sa" \
    --env-vars-file "$envf" --set-secrets "$secrets" \
    --network "$VPC" --subnet "$SUBNET" --vpc-egress all-traffic --ingress "$ingress" "$auth" \
    --min-instances "$min" --max-instances "$max" --cpu "$cpu" --memory "$mem" --concurrency "$conc" \
    "$cpu_flag" --cpu-boost --timeout 300 --execution-environment gen2 \
    --labels "env=sandbox,service=$svc,release=$REL,managed-by=infra-gcp-sandbox" \
    ${traffic_args[@]+"${traffic_args[@]}"} --quiet

  [ "$svc" = worker ] && run g run services add-iam-policy-binding worker --region "$REGION" \
    --member="serviceAccount:$SA_WORKER" --role=roles/run.invoker --quiet >/dev/null
  [ "$APPLY" = 1 ] || return 0
  local rev; rev=$(g run services describe "$svc" --region "$REGION" --format='value(status.latestCreatedRevisionName)')
  local ready; ready=$(g run revisions describe "$rev" --region "$REGION" --format='value(status.conditions[0].status)')
  [ "$ready" = True ] || { echo "$svc revision $rev is not Ready; $prev keeps serving" >&2; exit 1; }
  # Two identity signals, read back off the REVISION that was created (not the file we sent): it
  # must say PIVOTA_ENV=sandbox and GOOGLE_CLOUD_PROJECT=<this project>. Only plain env values are
  # read; secret references carry no value in a revision spec.
  local ident
  ident=$(g run revisions describe "$rev" --region "$REGION" --format=json | SBX="$SANDBOX_PROJECT" python3 -c '
import json, os, sys
c = json.load(sys.stdin)["spec"]["containers"][0]
env = {e["name"]: e.get("value") for e in c.get("env", []) if "value" in e}
ok = env.get("PIVOTA_ENV") == "sandbox" and env.get("GOOGLE_CLOUD_PROJECT") == os.environ["SBX"]
print("ok" if ok else "PIVOTA_ENV=%r GOOGLE_CLOUD_PROJECT=%r" % (env.get("PIVOTA_ENV"), env.get("GOOGLE_CLOUD_PROJECT")))') \
    || ident="could not read revision $rev"
  [ "$ident" = ok ] || { echo "$svc revision $rev does not identify as the sandbox ($ident); $prev keeps serving" >&2; exit 1; }
  echo "  $rev identifies as the sandbox (PIVOTA_ENV + GOOGLE_CLOUD_PROJECT)"
  if [ "$svc" != agenpaz ]; then
    # agenpaz answers only on its exclusive host, so its run.app URL cannot prove anything; it is
    # checked through the LB with --resolve in RUNBOOK.md step 8.
    local url="https://$svc-$PN.$REGION.run.app/health"
    [ -n "$prev" ] && url="https://$tag---$svc-$PN.$REGION.run.app/health"
    probe_in_vpc "$url" "https://$svc-$PN.$REGION.run.app" || { echo "$svc candidate $rev failed /health from inside the VPC; $prev keeps serving" >&2; exit 1; }
    echo "  $rev healthy (in-VPC probe)"
  fi
  if [ -n "$prev" ]; then
    echo "$svc $prev" >> "$ROLLBACK_FILE"
    g run services update-traffic "$svc" --region "$REGION" --to-revisions "$rev=100" --quiet >/dev/null
    echo "  promoted $rev (100%); rollback: gcloud --project $SANDBOX_PROJECT run services update-traffic $svc --region $REGION --to-revisions $prev=100"
  fi
}

for svc in $SERVICES; do deploy_one "$svc"; done

say "done: $REL ($( [ "$APPLY" = 1 ] && echo "applied; rollback file $ROLLBACK_FILE" || echo "dry run" ))"
