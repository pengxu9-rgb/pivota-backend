#!/usr/bin/env bash
# Give PIVOTA-Agent's main branch a keyless identity that can PUSH the gateway image and nothing else.
#
#   infra/gcp/setup_gateway_image_build_identity.sh
#
# FOR THE OWNER TO RUN (it creates a Workload Identity provider, a service account and IAM bindings;
# agents do not make IAM changes). Idempotent. Runbook: docs/runbooks/catalog_jobs_on_gcp.md.
#
# WHY. The gateway never deploys on merge, and until now it was not even BUILT on merge: every
# `gateway:<sha>` was built by hand with `gcloud builds submit --config infra/gcp/cloudbuild.gateway.yaml`
# from a laptop checkout, before a job (relgraph-sync, the sentinel, ...) could be re-imaged onto it.
# PIVOTA-Agent's `.github/workflows/build-gateway-image.yml` builds and pushes `gateway:<sha>` (and only
# that tag; no `:latest`) on every push to main. It deploys nothing.
#
# WHAT THIS CREATES, all in pivota-shared:
#   repository  us-west1/pivota-gateway   Docker, IMMUTABLE TAGS; readers copied from `pivota`
#   provider    github-actions/github-gateway   condition: PIVOTA-Agent's immutable repo id, refs/heads/main,
#               the build-gateway-image.yml workflow only, and push/workflow_dispatch events only
#   account     sa-gateway-image-build    roles/artifactregistry.writer on `pivota-gateway` ONLY
#   binding     workloadIdentityUser on that account for principalSet attribute.repository_id/<id>
# The account cannot deploy, cannot read secrets, cannot touch pivota-prod or pivota-staging.
#
# WHY A REPOSITORY OF ITS OWN (review of be#2779, P2-1). `pivota` holds the backend image, its tags
# are mutable (the hand builds re-push `:latest`), and `backend:latest` RUNS in prod as sa-worker
# (deploy_backend.sh's in-VPC probe, on every backend merge). Writer on `pivota` would let any
# workflow on PIVOTA-Agent main get code running as sa-worker with no human step. So the CI identity
# writes only to `pivota-gateway`, whose tags are immutable: once `gateway:<sha>` is pushed there it
# cannot be re-pointed.
#
# WHAT IMMUTABILITY DOES NOT DO (delta review, D-P2-1). It protects a tag's FIRST push only, and every
# sha built before this repository existed lives in `pivota`, so its tag is still free here. That is
# why infra/gcp/_gateway_image.sh resolves a sha in `pivota` FIRST: a sha with a hand build keeps
# resolving to it, and only a sha that exists nowhere but here resolves here. It is also why only a
# 40-hex tag is ever read from here.
#
# WHO ELSE CAN WRITE HERE. Not only this identity: the backend's deploy identity holds project-level
# roles/artifactregistry.writer on pivota-shared (setup_github_wif.sh), and so do humans with
# project-level roles. This script grants nothing beyond the one repository-level writer below.
#
# Readers (the runtime and Cloud Run service-agent identities) are copied from `pivota`'s policy, ONCE,
# when this script runs: a reader added to `pivota` later must be added here too (re-running this
# script does it). Conditional bindings and `deleted:` members are skipped and printed, never copied.
#
# RESIDUAL RISK, stated so it is a decision: the workflow builds whatever is on PIVOTA-Agent main, and
# that code runs in prod once a sha is deployed or a job is re-imaged onto it, both by hand, naming a
# sha. A commit on main that rewrote the workflow could push an image whose content differs from its
# sha's source, but only under a sha that has no `pivota` build, and only for a deploy or re-image
# that names that sha.
set -euo pipefail

GCLOUD="${GCLOUD:-gcloud}"; PYTHON="${PYTHON:-python3}"
SHARED=pivota-shared; REGION=us-west1; AR_REPO=pivota-gateway; READERS_FROM=pivota
WORKFLOW=.github/workflows/build-gateway-image.yml
POOL=github-actions; PROVIDER=github-gateway
SA_NAME=sa-gateway-image-build; SA="$SA_NAME@$SHARED.iam.gserviceaccount.com"
REPO=pengxu9-rgb/PIVOTA-Agent
# The repository's IMMUTABLE numeric id (gh api repos/pengxu9-rgb/PIVOTA-Agent --jq .id). Pinned, not
# looked up by name, for the same reason as setup_github_wif.sh: a released name can be re-registered
# by someone else; an id cannot.
REPO_ID=1100586824

SHARED_NUM="$("$GCLOUD" projects describe "$SHARED" --format='value(projectNumber)')"
[ -n "$SHARED_NUM" ] || { echo "cannot read $SHARED project number" >&2; exit 1; }
"$GCLOUD" iam workload-identity-pools describe "$POOL" --location=global --project="$SHARED" >/dev/null \
  || { echo "pool $POOL missing; run setup_github_wif.sh first" >&2; exit 1; }

# workflow_ref is "<owner>/<repo>/<path>@<ref>". Pinning it means a future PIVOTA-Agent workflow that
# asks for id-token: write gets nothing from this provider; a repo rename breaks it closed.
CONDITION="assertion.repository_id == '$REPO_ID' && assertion.ref == 'refs/heads/main'"
CONDITION="$CONDITION && assertion.workflow_ref == '$REPO/$WORKFLOW@refs/heads/main'"
CONDITION="$CONDITION && assertion.event_name in ['push', 'workflow_dispatch']"
MAPPING="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_id=assertion.repository_id,attribute.ref=assertion.ref"

# ---------------------------------------------------------------- the repository
if "$GCLOUD" artifacts repositories describe "$AR_REPO" --location="$REGION" --project="$SHARED" >/dev/null 2>&1; then
  echo "== repository $AR_REPO exists"
else
  echo "== creating repository $AR_REPO (immutable tags)"
  "$GCLOUD" artifacts repositories create "$AR_REPO" --location="$REGION" --project="$SHARED" \
    --repository-format=docker --immutable-tags \
    --description="Gateway images built on merge by PIVOTA-Agent build-gateway-image.yml. Tags are immutable." --quiet
fi
IMMUTABLE="$("$GCLOUD" artifacts repositories describe "$AR_REPO" --location="$REGION" --project="$SHARED" \
  --format='value(dockerConfig.immutableTags)')"
case "$IMMUTABLE" in True|true) ;; *)
  echo "REFUSING: $AR_REPO exists without immutable tags; the point of it is that a tag cannot be re-pointed" >&2; exit 1 ;;
esac

echo "== copying readers from $READERS_FROM"
READERS="$("$GCLOUD" artifacts repositories get-iam-policy "$READERS_FROM" --location="$REGION" --project="$SHARED" --format=json \
  | "$PYTHON" -c '
import json, sys
out = []
for b in json.load(sys.stdin).get("bindings", []):
    if b.get("role") != "roles/artifactregistry.reader":
        continue
    if b.get("condition"):
        print("   SKIPPED conditional reader binding (copy it by hand if it is wanted): " + " ".join(b.get("members", [])), file=sys.stderr)
        continue
    for m in b.get("members", []):
        if m.startswith("deleted:"):
            print("   SKIPPED deleted member: " + m, file=sys.stderr)
            continue
        out.append(m)
print("\n".join(out))')"
[ -n "$READERS" ] || { echo "no readers found on $READERS_FROM; refusing to create a repository nothing can pull from" >&2; exit 1; }
while IFS= read -r member; do
  "$GCLOUD" artifacts repositories add-iam-policy-binding "$AR_REPO" --location="$REGION" --project="$SHARED" \
    --role="roles/artifactregistry.reader" --member="$member" --quiet >/dev/null
  echo "   reader $member"
done <<READERS_EOF
$READERS
READERS_EOF

# ---------------------------------------------------------------- provider
if "$GCLOUD" iam workload-identity-pools providers describe "$PROVIDER" \
     --workload-identity-pool="$POOL" --location=global --project="$SHARED" >/dev/null 2>&1; then
  echo "== provider $PROVIDER exists - reconciling condition and mapping"
  "$GCLOUD" iam workload-identity-pools providers update-oidc "$PROVIDER" \
    --workload-identity-pool="$POOL" --location=global --project="$SHARED" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="$MAPPING" --attribute-condition="$CONDITION" --quiet
else
  echo "== creating provider $PROVIDER"
  "$GCLOUD" iam workload-identity-pools providers create-oidc "$PROVIDER" \
    --workload-identity-pool="$POOL" --location=global --project="$SHARED" \
    --display-name="GitHub OIDC (gateway image build)" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="$MAPPING" --attribute-condition="$CONDITION" --quiet
fi

if "$GCLOUD" iam service-accounts describe "$SA" --project="$SHARED" >/dev/null 2>&1; then
  echo "== service account exists"
else
  echo "== creating service account $SA_NAME"
  "$GCLOUD" iam service-accounts create "$SA_NAME" --project="$SHARED" \
    --display-name="Gateway image build (GitHub Actions)" \
    --description="Keyless CI identity for $REPO main ($WORKFLOW). Pushes gateway:<sha> to $AR_REPO; deploys nothing." --quiet
fi

PRINCIPAL="principalSet://iam.googleapis.com/projects/$SHARED_NUM/locations/global/workloadIdentityPools/$POOL/attribute.repository_id/$REPO_ID"
echo "== binding workloadIdentityUser for $REPO (id $REPO_ID)"
"$GCLOUD" iam service-accounts add-iam-policy-binding "$SA" --project="$SHARED" \
  --role="roles/iam.workloadIdentityUser" --member="$PRINCIPAL" --quiet >/dev/null

echo "== granting artifactregistry.writer on $AR_REPO only"
"$GCLOUD" artifacts repositories add-iam-policy-binding "$AR_REPO" --location="$REGION" --project="$SHARED" \
  --role="roles/artifactregistry.writer" --member="serviceAccount:$SA" --quiet >/dev/null

echo
echo "workload_identity_provider: projects/$SHARED_NUM/locations/global/workloadIdentityPools/$POOL/providers/$PROVIDER"
echo "service_account:            $SA"
echo "These two values are what PIVOTA-Agent's build-gateway-image.yml uses."
