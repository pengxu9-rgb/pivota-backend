#!/usr/bin/env bash
# Replace project-wide Secret Manager access with per-secret grants.
#   infra/gcp/scope_secret_access.sh prod                          report only (default)
#   infra/gcp/scope_secret_access.sh prod --apply                  add the per-secret grants
#   infra/gcp/scope_secret_access.sh prod --revoke-project-level   remove the project-wide role
#
# WHY. `sa-backend` and `sa-worker` hold roles/secretmanager.secretAccessor at PROJECT level, so
# each can read all 105 secrets in pivota-prod - including the whole gateway-env-* family it never
# mounts: AWS keys, SendGrid, OpenAI, pivota-db-password, and UCP_BUSINESS_SIGNING_PRIVATE_JWK.
# That matters beyond the services themselves, because the GitHub Actions deploy identity holds
# iam.serviceAccountUser on both. Deploying a job that RUNS AS sa-backend inherits its token, so a
# compromised Actions run reads every secret in the project. Scoping the grant is what makes that
# path bounded rather than total.
#
# `sa-gateway` is already done this way - per-secret bindings, no project-level role. This brings
# the other two to the pattern already in use here rather than inventing one.
#
# ORDER MATTERS, AND IT IS ADDITIVE FIRST. Cloud Run resolves --set-secrets at INSTANCE START, not
# per request. Revoking before granting would not disturb running instances and would then fail
# every new one - so the damage appears at the next autoscale event or deploy, not when you run
# this. Grant, verify a candidate boots, and only then revoke.
#
# RE-RUN THIS WHEN A SERVICE GAINS A SECRET. The sets below are derived from what the live
# services and jobs actually mount, so they cannot go stale on their own - but a newly mounted
# secret has no grant until this runs again. That failure is caught safely: the new revision fails
# to start, and deploy_backend.sh's candidate gate holds it at 0% traffic with the previous
# revision serving.
set -euo pipefail

ENV="${1:-}"; shift || true
case "$ENV" in
  prod)    PROJECT=pivota-prod ;;
  staging) PROJECT=pivota-staging ;;
  *) echo "usage: $0 prod|staging [--apply] [--revoke-project-level]" >&2; exit 2 ;;
esac
REGION=us-west1
GCLOUD="${GCLOUD:-gcloud}"
ROLE=roles/secretmanager.secretAccessor
APPLY=0; REVOKE=0
for a in "$@"; do
  case "$a" in
    --apply) APPLY=1 ;;
    --revoke-project-level) REVOKE=1 ;;
    *) echo "unknown flag: $a" >&2; exit 2 ;;
  esac
done

# Which service accounts we manage here. sa-gateway is deliberately absent: it already has
# per-secret bindings and no project-level role, so there is nothing to migrate.
MANAGED=("sa-backend@$PROJECT.iam.gserviceaccount.com" "sa-worker@$PROJECT.iam.gserviceaccount.com")

# ---------------------------------------------------------------- derive, never hardcode
# A hardcoded list is wrong the first time anyone adds a secret mount. Read what the live services
# and jobs declare. Services and jobs nest the pod spec differently - a job buries it one level
# deeper under spec.template.spec.template.spec - which is why this is not one loop.
MAP="$(mktemp)"; trap 'rm -f "$MAP"' EXIT INT TERM
"$GCLOUD" run services list --project="$PROJECT" --region="$REGION" --format='value(metadata.name)' \
  > "$MAP.svc"
"$GCLOUD" run jobs list --project="$PROJECT" --region="$REGION" --format='value(metadata.name)' \
  > "$MAP.job" 2>/dev/null || : > "$MAP.job"

python3 - "$PROJECT" "$REGION" "$MAP" <<'PY'
import json, subprocess, sys, collections
project, region, mapfile = sys.argv[1], sys.argv[2], sys.argv[3]
def gcloud(*a):
    r = subprocess.run(["gcloud", *a], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""
def secrets_of(container):
    return {e["valueFrom"]["secretKeyRef"]["name"]
            for e in container.get("env", []) if "valueFrom" in e}
m = collections.defaultdict(set)
for name in open(mapfile + ".svc").read().split():
    o = gcloud("run", "services", "describe", name, f"--project={project}",
               f"--region={region}", "--format=json")
    if not o.strip(): continue
    sp = json.loads(o)["spec"]["template"]["spec"]
    m[sp.get("serviceAccountName", "")] |= secrets_of(sp["containers"][0])
for name in open(mapfile + ".job").read().split():
    o = gcloud("run", "jobs", "describe", name, f"--project={project}",
               f"--region={region}", "--format=json")
    if not o.strip(): continue
    sp = json.loads(o)["spec"]["template"]["spec"]["template"]["spec"]
    m[sp.get("serviceAccountName", "")] |= secrets_of(sp["containers"][0])
with open(mapfile, "w") as f:
    for sa, secs in sorted(m.items()):
        for s in sorted(secs):
            f.write(f"{sa}\t{s}\n")
PY

TOTAL="$("$GCLOUD" secrets list --project="$PROJECT" --format='value(name)' | wc -l | tr -d ' ')"
echo "project $PROJECT holds $TOTAL secrets"
echo

for SA in "${MANAGED[@]}"; do
  NEED="$(awk -F'\t' -v sa="$SA" '$1==sa{print $2}' "$MAP" | sort -u)"
  COUNT="$(printf '%s\n' "$NEED" | grep -c . || true)"
  if [ "$COUNT" = 0 ]; then
    # Every managed account should run something. Zero means the enumeration failed - a describe
    # that 403'd, or a spec shape change - and proceeding would revoke access to everything.
    echo "refusing to continue: derived ZERO mounted secrets for $SA." >&2
    echo "that is far more likely to be a broken enumeration than a service that mounts nothing." >&2
    exit 1
  fi
  echo "$SA needs $COUNT of $TOTAL"
  if [ "$APPLY" = 1 ]; then
    n=0
    while IFS= read -r s; do
      [ -n "$s" ] || continue
      n=$((n + 1))
      printf '\r   granting %s/%s' "$n" "$COUNT"
      "$GCLOUD" secrets add-iam-policy-binding "$s" --project="$PROJECT" \
        --role="$ROLE" --member="serviceAccount:$SA" --quiet >/dev/null
    done <<< "$NEED"
    printf '\r   granted %s per-secret bindings\n' "$COUNT"
  fi
done

if [ "$APPLY" != 1 ] && [ "$REVOKE" != 1 ]; then
  echo
  echo "report only. Re-run with --apply to add the per-secret grants."
  exit 0
fi

if [ "$REVOKE" = 1 ]; then
  echo
  # Verify before removing, rather than trusting that --apply ran. A revoke on top of incomplete
  # grants is the one ordering that breaks production, so it is checked here even though --apply
  # just did it: the two flags can be passed on different days, by different people.
  for SA in "${MANAGED[@]}"; do
    NEED="$(awk -F'\t' -v sa="$SA" '$1==sa{print $2}' "$MAP" | sort -u)"
    while IFS= read -r s; do
      [ -n "$s" ] || continue
      "$GCLOUD" secrets get-iam-policy "$s" --project="$PROJECT" --format=json 2>/dev/null \
        | grep -q "$SA" || {
          echo "refusing to revoke: $SA has no per-secret binding on $s" >&2
          echo "run with --apply first." >&2; exit 1; }
    done <<< "$NEED"
    echo "verified every mounted secret has a per-secret binding for $SA"
  done
  for SA in "${MANAGED[@]}"; do
    "$GCLOUD" projects remove-iam-policy-binding "$PROJECT" \
      --member="serviceAccount:$SA" --role="$ROLE" --quiet >/dev/null
    echo "removed project-level $ROLE from $SA"
  done
  echo
  echo "Deploy a candidate and confirm it boots before trusting this:"
  echo "  gh workflow run deploy-prod.yml -f sha=\$(git rev-parse origin/main) -f promote=false"
fi
