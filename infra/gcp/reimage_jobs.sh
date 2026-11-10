#!/usr/bin/env bash
# Roll pinned Cloud Run Jobs onto a merged commit's image, or report how stale they are.
#
#   infra/gcp/reimage_jobs.sh staging|prod status
#   infra/gcp/reimage_jobs.sh staging|prod backend|gateway <sha> [--apply] [job ...]
#
# FOR THE OPERATOR, from any machine with gcloud + python3 + curl (Cloud Shell has all three).
# Runbook: docs/runbooks/catalog_jobs_on_gcp.md.
#
# WHY. Deploys on merge roll SERVICES (web, worker); they never touch a Job. Every Job runs the image
# it was last pointed at until someone updates it, so a merged fix reaches the nightly refresh, the
# relgraph sync or a sweep only after a hand re-image. Those re-images were done from laptops, with
# a different one-off script per job (relgraph-sync 21 times and external-referral-refresh 23 times
# in four weeks to 2026-11-09). This is the one committed way to do it.
#
# WHAT A RE-IMAGE CHANGES: the image and PIVOTA_COMMIT_SHA, nothing else. It runs
# `gcloud run jobs update <job> --image <repo>:<sha> --update-env-vars PIVOTA_COMMIT_SHA=<sha>`,
# which leaves args, command, secrets, every other env var, the network and the limits alone, then
# RE-READS the job and fails if anything other than those two fields differs from before.
#
# DRY RUN BY DEFAULT. Without --apply it prints, per job, current -> target and changes nothing.
# --apply NEEDS JOB NAMES: a dry run may plan every registered job, but a roll names each job it
# changes, because the jobs that write prices or the graph (relgraph-sync, external-referral-refresh,
# the sweeps) are each re-imaged on the owner's go. `--apply` with no names is refused.
#
# GATEWAY IMAGES resolve through _gateway_image.sh: `pivota` (hand builds, and every image built
# before build-on-merge) first, then `pivota-gateway` (built on merge, immutable tags). A lookup error
# other than "not found" refuses. The plan line prints which repository the sha resolved to.
#
# REFUSALS, each a hard stop:
#   - the sha is not on the repo's main (checked against GitHub's compare API, public repos);
#   - the image <repo>:<sha> is not in Artifact Registry (build it first);
#   - relgraph-sync or relgraph-health between 10:15 and 11:45 UTC (relgraph-sync runs at 10:37);
#   - any job with an execution still running (it keeps its old image; re-image after it ends);
#   - retailer-ingest-drain unless named explicitly AND RETAILER_INGEST_DRAIN_WRITER=1: that job has
#     a single writer for its image, and a default "all backend jobs" run must not roll it.
#   - a job whose executions cannot be listed (a failed read is not "nothing running").
# One refusal stops the run before anything changes. Any failure mid-roll (update, re-read or verify)
# stops at that job; the jobs rolled before it stay rolled (each was verified), and the message names
# them.
set -euo pipefail

ENV="${1:-}"; MODE="${2:-}"
case "$ENV" in
  prod) PROJECT=pivota-prod ;;
  staging) PROJECT=pivota-staging ;;
  *) echo "usage: $0 staging|prod status | $0 staging|prod backend|gateway <sha> [--apply] [job ...]" >&2; exit 2 ;;
esac
GCLOUD="${GCLOUD:-gcloud}"; PYTHON="${PYTHON:-python3}"; CURL="${CURL:-curl}"
REGION=us-west1; SHARED=pivota-shared; OWNER=pengxu9-rgb
export CLOUDSDK_CORE_PROJECT="$PROJECT"
# shellcheck source=infra/gcp/_gateway_image.sh
. "$(cd "$(dirname "$0")" && pwd)/_gateway_image.sh"

# THE REGISTRY: every pinned job this script may roll, by the repo its image is built from.
# store-audit-commerce-probe is absent on purpose (store-audit-browser image, its own build).
BACKEND_JOBS="external-referral-refresh external-seed-destination-sweep merchant-purchasability-sweep
tierb-cart-link-eligibility reap-cart-proof-mirror reap-cart-proof-enrichment content-canonical-election
agent-pdp-orphan-reaper derive-offer-market-currency audit-domainless-offer-currency
store-audit-ucp-reprobe-enqueue store-audit-commerce-reprobe-enqueue brand-official-canonical-promote
reviews-invitation-send retailer-ingest-drain"
GATEWAY_JOBS="relgraph-sync relgraph-health external-seed-sentinel-nongrowth pdp-identity-graph-backfill
pdp-live-read-promote store-audit-ucp-probe"
# Rolled only when named and RETAILER_INGEST_DRAIN_WRITER=1.
EXPLICIT_ONLY="retailer-ingest-drain"
# Refused in the relgraph window.
RELGRAPH_WINDOW_JOBS="relgraph-sync relgraph-health"

in_words(){ case " $(echo $2) " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }
repo_of(){ # job -> "PIVOTA-Agent" | "pivota-backend" | ""
  if in_words "$1" "$BACKEND_JOBS"; then echo pivota-backend; elif in_words "$1" "$GATEWAY_JOBS"; then echo PIVOTA-Agent; fi
}

# job_facts JOB -> "image<TAB>PIVOTA_COMMIT_SHA" (empty output: the job does not exist)
job_facts(){
  local json
  json="$("$GCLOUD" run jobs describe "$1" --region "$REGION" --format=json 2>/dev/null)" || return 0
  JOB_JSON="$json" "$PYTHON" -c '
import json, os
c = json.loads(os.environ["JOB_JSON"])["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]
sha = next((e.get("value", "") for e in c.get("env", []) if e["name"] == "PIVOTA_COMMIT_SHA"), "")
print(c["image"] + "\t" + sha)'
}

# job_shape JOB -> canonical JSON of everything a re-image must NOT change
job_shape(){
  JOB_JSON="$("$GCLOUD" run jobs describe "$1" --region "$REGION" --format=json)" "$PYTHON" -c '
import json, os
d = json.loads(os.environ["JOB_JSON"])
t = d["spec"]["template"]
c = t["spec"]["template"]["spec"]["containers"][0]
c = dict(c); c.pop("image", None)
c["env"] = sorted([e for e in c.get("env", []) if e["name"] != "PIVOTA_COMMIT_SHA"], key=lambda e: e["name"])
spec = dict(t["spec"]["template"]["spec"]); spec["containers"] = [c]
ann = {k: v for k, v in (t.get("metadata", {}).get("annotations") or {}).items()
       if k.startswith("run.googleapis.com/") and k not in ("run.googleapis.com/client-name", "run.googleapis.com/client-version")}
print(json.dumps({"spec": spec, "annotations": ann, "task_count": t["spec"].get("taskCount"),
                  "parallelism": t["spec"].get("parallelism")}, sort_keys=True))'
}

# compare REPO SHA -> "status<TAB>ahead_by<TAB>behind_by" of SHA against main (GitHub compare API)
compare(){
  # Unauthenticated, GitHub allows 60 calls an hour PER IP, shared with everything else on that
  # network; set GITHUB_TOKEN (e.g. `GITHUB_TOKEN=$(gh auth token)`) to lift it. A failed call is a
  # failure, never a pass: status prints "?", a re-image refuses.
  # The token travels to curl as a config file on STDIN (`--config -`), never on argv, where any
  # other user of the machine could read it in `ps` while curl runs.
  local body config=""
  [ -n "${GITHUB_TOKEN:-}" ] && config="header = \"Authorization: Bearer $GITHUB_TOKEN\""
  # The body lists every file changed since the sha (megabytes for an old image), so it goes to
  # python on STDIN: as an environment variable it exceeds ARG_MAX and the parse never runs.
  body="$(printf '%s\n' "$config" | "$CURL" --config - -fsS --max-time 60 -H 'Accept: application/vnd.github+json' \
    "https://api.github.com/repos/$OWNER/$1/compare/$2...main?per_page=1")" || return 1
  printf '%s' "$body" | "$PYTHON" -c '
import json, sys
d = json.load(sys.stdin)
print(d["status"] + "\t" + str(d["ahead_by"]) + "\t" + str(d["behind_by"]))'
}

running_executions(){ # job -> count of executions with no completion time; exit 1 if unreadable
  local listing
  listing="$("$GCLOUD" run jobs executions list --job "$1" --region "$REGION" --limit 20 \
    --format='value(metadata.name,status.completionTime)')" || return 1
  printf '%s\n' "$listing" | awk -F'\t' 'NF && $2 == "" {n++} END {print n+0}'
}

sha_of_image(){ # image ref -> the tag when it is a 40-hex sha, else ""
  local tag="${1##*:}"
  case "$1" in *@sha256:*) echo ""; return ;; esac
  [[ "$tag" =~ ^[0-9a-f]{40}$ ]] && echo "$tag" || echo ""
}

# ------------------------------------------------------------------ status
if [ "$MODE" = status ]; then
  [ "$#" -eq 2 ] || { echo "status takes no further arguments" >&2; exit 2; }
  printf '%-38s %-15s %-12s %s\n' JOB REPO SHA BEHIND_MAIN
  SEEN=""
  for job in $BACKEND_JOBS $GATEWAY_JOBS; do
    facts="$(job_facts "$job")"; [ -n "$facts" ] || continue
    image="${facts%%$'\t'*}"; stamp="${facts#*$'\t'}"
    sha="$(sha_of_image "$image")"; [ -n "$sha" ] || sha="$stamp"
    repo="$(repo_of "$job")"; behind="?"
    if [[ "$sha" =~ ^[0-9a-f]{7,40}$ ]]; then
      # One call per (repo, sha): many jobs share an image, and the API budget is small.
      key="$repo@$sha"
      cached="$(printf '%s\n' "$SEEN" | awk -F'\t' -v k="$key" '$1 == k {print $2; exit}')"
      if [ -n "$cached" ]; then
        behind="$cached"
      elif cmp="$(compare "$repo" "$sha" 2>/dev/null)"; then
        behind="$(echo "$cmp" | cut -f2)"
        SEEN="$SEEN"$'\n'"$key"$'\t'"$behind"
      fi
    else
      sha="(digest, no stamp)"
    fi
    # compare(sha...main): ahead_by = commits main has that sha lacks, i.e. how far behind the job is.
    printf '%-38s %-15s %-12s %s\n' "$job" "$repo" "${sha:0:12}" "$behind"
  done
  exit 0
fi

# ------------------------------------------------------------------ re-image
case "$MODE" in
  backend) REPO_NAME=pivota-backend; IMAGE_NAME=backend; REGISTERED="$BACKEND_JOBS" ;;
  gateway) REPO_NAME=PIVOTA-Agent; IMAGE_NAME=gateway; REGISTERED="$GATEWAY_JOBS" ;;
  *) echo "mode must be status, backend or gateway" >&2; exit 2 ;;
esac
SHA="${3:-}"
[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "sha must be a full 40-hex commit (got '${SHA}')" >&2; exit 2; }
shift 3
APPLY=false; NAMED=""
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=true ;;
    -*) echo "unknown option '$arg'" >&2; exit 2 ;;
    *) in_words "$arg" "$REGISTERED" || { echo "'$arg' is not a registered $MODE job" >&2; exit 2; }
       NAMED="$NAMED $arg" ;;
  esac
done

[ "$APPLY" = true ] && [ -z "$NAMED" ] && {
  echo "--apply needs job names: a roll names every job it changes (dry-run without --apply to see the plan)" >&2; exit 2; }

cmp="$(compare "$REPO_NAME" "$SHA")" \
  || { echo "cannot reach GitHub to verify $SHA is on $REPO_NAME main; refusing" >&2; exit 1; }
case "$(echo "$cmp" | cut -f1)" in
  identical|ahead) ;;  # sha...main: main is identical to or ahead of sha -> sha is on main
  *) echo "$SHA is not on $REPO_NAME main (compare: $(echo "$cmp" | cut -f1)); refusing" >&2; exit 1 ;;
esac
if [ "$MODE" = gateway ]; then
  _rc=0; TARGET_IMAGE="$(resolve_gateway_image "$SHA")" || _rc=$?
  case "$_rc" in
    0) ;;
    1) echo "missing image gateway:$SHA in pivota and pivota-gateway; build it first" >&2; exit 1 ;;
    *) echo "cannot resolve gateway:$SHA (lookup error above); refusing" >&2; exit 1 ;;
  esac
else
  TARGET_IMAGE="$REGION-docker.pkg.dev/$SHARED/pivota/$IMAGE_NAME:$SHA"
  "$GCLOUD" artifacts docker images describe "$TARGET_IMAGE" >/dev/null 2>&1 \
    || { echo "missing image $TARGET_IMAGE; build it first" >&2; exit 1; }
fi

if [ -n "$NAMED" ]; then JOBS="$NAMED"; else JOBS="$REGISTERED"; fi
NOW_HHMM="${NOW_HHMM:-$(date -u +%H%M)}"
in_relgraph_window(){ [ "$((10#$NOW_HHMM))" -ge 1015 ] && [ "$((10#$NOW_HHMM))" -lt 1145 ]; }

plan=""; refused=0
for job in $JOBS; do
  if in_words "$job" "$EXPLICIT_ONLY"; then
    if [ -z "$NAMED" ]; then echo "skip  $job (rolled only when named)"; continue; fi
    if [ "${RETAILER_INGEST_DRAIN_WRITER:-}" != 1 ]; then
      echo "REFUSE $job: single-writer image; set RETAILER_INGEST_DRAIN_WRITER=1 only if you are its writer" >&2
      refused=1; continue
    fi
  fi
  facts="$(job_facts "$job")"
  if [ -z "$facts" ]; then
    if [ -n "$NAMED" ]; then echo "REFUSE $job: does not exist in $PROJECT" >&2; refused=1; fi
    continue
  fi
  if in_words "$job" "$RELGRAPH_WINDOW_JOBS" && in_relgraph_window; then
    echo "REFUSE $job: 10:15-11:45 UTC is the relgraph-sync window" >&2; refused=1; continue
  fi
  if ! running="$(running_executions "$job")"; then
    echo "REFUSE $job: cannot list its executions, so cannot tell whether one is running" >&2; refused=1; continue
  fi
  if [ "$running" != 0 ]; then
    echo "REFUSE $job: an execution is still running" >&2; refused=1; continue
  fi
  image="${facts%%$'\t'*}"; stamp="${facts#*$'\t'}"
  if [ "$image" = "$TARGET_IMAGE" ] && [ "$stamp" = "$SHA" ]; then
    echo "same  $job (already on ${SHA:0:12})"; continue
  fi
  # The plan names the REPOSITORY too: the operator names a sha, and must see which image it resolved to.
  echo "plan  $job: ${image#*/pivota-shared/} (stamp ${stamp:0:12}) -> ${TARGET_IMAGE#*/pivota-shared/}"
  plan="$plan $job"
done

[ "$refused" = 0 ] || { echo "nothing changed: resolve the refusals above first" >&2; exit 1; }
[ -n "$plan" ] || { echo "nothing to do"; exit 0; }
if [ "$APPLY" != true ]; then echo "DRY RUN: re-run with --apply to roll:$plan"; exit 0; fi

rolled=""
# Any failure mid-roll (a failed update, a failed re-read, a failed verify) names what is already done.
trap 'echo "STOPPED mid-roll; already rolled and verified:${rolled:- none}" >&2' ERR
for job in $plan; do
  before="$(job_shape "$job")"
  "$GCLOUD" run jobs update "$job" --region "$REGION" --image "$TARGET_IMAGE" \
    --update-env-vars "PIVOTA_COMMIT_SHA=$SHA" --quiet >/dev/null
  after="$(job_shape "$job")"
  facts="$(job_facts "$job")"
  if [ "$before" != "$after" ] || [ "${facts%%$'\t'*}" != "$TARGET_IMAGE" ] || [ "${facts#*$'\t'}" != "$SHA" ]; then
    echo "FAILED VERIFY $job: the job changed beyond image + PIVOTA_COMMIT_SHA, or did not take the new image" >&2
    echo "already rolled and verified before it:${rolled:- none}; not attempted after it: the rest of$plan" >&2
    exit 1
  fi
  rolled="$rolled $job"
  echo "rolled $job -> ${TARGET_IMAGE##*/} (verified: only image + PIVOTA_COMMIT_SHA changed)"
done
