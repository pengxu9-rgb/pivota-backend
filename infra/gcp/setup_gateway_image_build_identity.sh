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
# PIVOTA-Agent's `.github/workflows/build-gateway-image.yml` builds and pushes `gateway:<sha>` (and
# `gateway:latest`, as the hand build does) on every push to main. It deploys nothing.
#
# WHAT THIS CREATES, all in pivota-shared:
#   provider  github-actions/github-gateway   condition: PIVOTA-Agent's immutable repo id AND refs/heads/main
#   account   sa-gateway-image-build          roles/artifactregistry.writer on the `pivota` repository ONLY
#   binding   workloadIdentityUser on that account for principalSet attribute.repository_id/<id>
# The account cannot deploy, cannot read secrets, cannot touch pivota-prod or pivota-staging.
#
# RESIDUAL RISK, stated so it is a decision and not an accident: `pivota` holds the backend image too,
# and artifactregistry.writer is repository-scoped, so a malicious commit on PIVOTA-Agent main could
# push a `backend:<tag>`. It cannot make anything RUN it (no run.* permission), and every deploy and
# re-image names an exact sha tag. Splitting the gateway into its own repository would close it, at
# the cost of re-pointing every gateway job and service.
set -euo pipefail

GCLOUD="${GCLOUD:-gcloud}"
SHARED=pivota-shared; REGION=us-west1; AR_REPO=pivota
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

CONDITION="assertion.repository_id == '$REPO_ID' && assertion.ref == 'refs/heads/main'"
MAPPING="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_id=assertion.repository_id,attribute.ref=assertion.ref"

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
    --description="Keyless CI identity for $REPO main. Pushes gateway:<sha> to $AR_REPO; deploys nothing." --quiet
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
