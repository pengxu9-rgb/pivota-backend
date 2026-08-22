#!/usr/bin/env bash
# Let GitHub Actions deploy to Cloud Run without a service-account key.
#   infra/gcp/setup_github_wif.sh [repo]        default repo: pengxu9-rgb/pivota-backend
#
# Idempotent, like the other setup_* scripts here: re-running reconciles rather than duplicates.
#
# WHY KEYLESS. The alternative is a downloaded service-account JSON key pasted into a GitHub
# secret. That key is a bearer credential with deploy rights on a payments platform, it never
# expires, and nothing here would notice if it leaked. Workload Identity Federation trades it for
# a GitHub-signed OIDC token that is minted per job, expires in minutes, and is bound by an
# attribute condition to ONE repository. There is no long-lived secret to leak.
#
# WHAT IT CAN DO, deliberately narrowly: build the backend image in pivota-shared, and roll
# Cloud Run `web` in pivota-prod forward onto it. It cannot read prod secret VALUES, cannot touch
# Cloud SQL, and cannot rewrite the service's environment - see CONFIG=preserve in
# deploy_backend.sh for why a code deploy must not be able to change 188 env vars.
set -euo pipefail

REPO="${1:-pengxu9-rgb/pivota-backend}"
[[ "$REPO" == */* ]] || { echo "repo must be owner/name (got '$REPO')" >&2; exit 2; }

SHARED=pivota-shared
PROD=pivota-prod
POOL=github-actions
PROVIDER=github
SA_NAME=sa-github-deploy
SA="$SA_NAME@$SHARED.iam.gserviceaccount.com"
GCLOUD="${GCLOUD:-gcloud}"

SHARED_NUM="$("$GCLOUD" projects describe "$SHARED" --format='value(projectNumber)')"
[ -n "$SHARED_NUM" ] || { echo "cannot read $SHARED project number" >&2; exit 1; }

echo "repo    : $REPO"
echo "pool    : projects/$SHARED_NUM/locations/global/workloadIdentityPools/$POOL"
echo "identity: $SA"
echo

# ---------------------------------------------------------------- APIs
# sts + iamcredentials are what the token exchange itself runs on; without them the federation
# exists but every job fails at the auth step with a permission error that reads like a bad binding.
echo "== enabling APIs on $SHARED"
"$GCLOUD" services enable \
  iamcredentials.googleapis.com sts.googleapis.com \
  cloudbuild.googleapis.com artifactregistry.googleapis.com \
  --project "$SHARED" --quiet

# ---------------------------------------------------------------- pool + provider
if "$GCLOUD" iam workload-identity-pools describe "$POOL" \
     --location=global --project="$SHARED" >/dev/null 2>&1; then
  echo "== pool $POOL exists"
else
  echo "== creating pool $POOL"
  "$GCLOUD" iam workload-identity-pools create "$POOL" \
    --location=global --project="$SHARED" \
    --display-name="GitHub Actions" \
    --description="Keyless OIDC federation for GitHub Actions" --quiet
fi

# THE ATTRIBUTE CONDITION IS THE WHOLE SECURITY BOUNDARY. Without it, any GitHub repository in
# the world - not just yours - can mint a token this provider will accept, and then impersonate a
# service account with run.admin on prod. Google refuses to create an unconditioned provider for
# exactly this reason, but it will happily accept a condition that is too loose. Pin the full
# owner/name; `assertion.repository_owner` alone would admit every repo under the account,
# including a fork someone pushes to.
CONDITION="assertion.repository == '$REPO'"
MAPPING="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref"

if "$GCLOUD" iam workload-identity-pools providers describe "$PROVIDER" \
     --workload-identity-pool="$POOL" --location=global --project="$SHARED" >/dev/null 2>&1; then
  echo "== provider $PROVIDER exists - reconciling condition and mapping"
  "$GCLOUD" iam workload-identity-pools providers update-oidc "$PROVIDER" \
    --workload-identity-pool="$POOL" --location=global --project="$SHARED" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="$MAPPING" \
    --attribute-condition="$CONDITION" --quiet
else
  echo "== creating provider $PROVIDER"
  "$GCLOUD" iam workload-identity-pools providers create-oidc "$PROVIDER" \
    --workload-identity-pool="$POOL" --location=global --project="$SHARED" \
    --display-name="GitHub OIDC" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="$MAPPING" \
    --attribute-condition="$CONDITION" --quiet
fi

# ---------------------------------------------------------------- service account
if "$GCLOUD" iam service-accounts describe "$SA" --project="$SHARED" >/dev/null 2>&1; then
  echo "== service account exists"
else
  echo "== creating service account $SA_NAME"
  "$GCLOUD" iam service-accounts create "$SA_NAME" --project="$SHARED" \
    --display-name="GitHub Actions deployer" \
    --description="Keyless CI identity for $REPO. Builds the backend image and rolls Cloud Run web." --quiet
fi

# Only tokens carrying attribute.repository == $REPO may impersonate the account. This is scoped to
# the repository, NOT to a branch: the deploy workflow verifies the target SHA is an ancestor of
# main itself, which survives a branch being renamed and is checked against git rather than a claim
# in a token. Adding attribute.ref here as well would break workflow_dispatch from a tag or a
# re-run, without adding a control the workflow does not already enforce.
PRINCIPAL="principalSet://iam.googleapis.com/projects/$SHARED_NUM/locations/global/workloadIdentityPools/$POOL/attribute.repository/$REPO"
echo "== binding workloadIdentityUser for $REPO"
"$GCLOUD" iam service-accounts add-iam-policy-binding "$SA" --project="$SHARED" \
  --role="roles/iam.workloadIdentityUser" --member="$PRINCIPAL" --quiet >/dev/null

# ---------------------------------------------------------------- roles
# pivota-shared: submit the build, push the image, read build logs.
echo "== granting build roles on $SHARED"
for role in roles/cloudbuild.builds.editor roles/artifactregistry.writer \
            roles/storage.admin roles/logging.viewer roles/serviceusage.serviceUsageConsumer; do
  "$GCLOUD" projects add-iam-policy-binding "$SHARED" \
    --member="serviceAccount:$SA" --role="$role" --quiet --condition=None >/dev/null
  echo "   $role"
done

# `gcloud builds submit` runs the build AS the Cloud Build service agent, so the caller must be
# allowed to act as it. Without this the submit fails with a serviceAccountUser error that names
# an account the operator never chose and did not know was involved.
CB_SA="$SHARED_NUM@cloudbuild.gserviceaccount.com"
if "$GCLOUD" iam service-accounts describe "$CB_SA" --project="$SHARED" >/dev/null 2>&1; then
  "$GCLOUD" iam service-accounts add-iam-policy-binding "$CB_SA" --project="$SHARED" \
    --role="roles/iam.serviceAccountUser" --member="serviceAccount:$SA" --quiet >/dev/null
  echo "   roles/iam.serviceAccountUser on $CB_SA"
fi

# pivota-prod: deploy revisions, shift traffic, and run the in-VPC health probe job.
# roles/run.admin covers services AND jobs - deploy_backend.sh verifies an internal-ingress
# candidate by creating a one-shot Cloud Run job inside the VPC, then reading its log line.
echo "== granting deploy roles on $PROD"
for role in roles/run.admin roles/logging.viewer; do
  "$GCLOUD" projects add-iam-policy-binding "$PROD" \
    --member="serviceAccount:$SA" --role="$role" --quiet --condition=None >/dev/null
  echo "   $role"
done

# Deploying a service that RUNS AS another account requires actAs on that account. Bound per
# service account rather than project-wide: project-level serviceAccountUser would also grant
# actAs on every future service account in prod, including ones created for something else.
#   sa-backend - what `web` runs as
#   sa-worker  - what the in-VPC probe job runs as
for target in "sa-backend@$PROD.iam.gserviceaccount.com" "sa-worker@$PROD.iam.gserviceaccount.com"; do
  if "$GCLOUD" iam service-accounts describe "$target" --project="$PROD" >/dev/null 2>&1; then
    "$GCLOUD" iam service-accounts add-iam-policy-binding "$target" --project="$PROD" \
      --role="roles/iam.serviceAccountUser" --member="serviceAccount:$SA" --quiet >/dev/null
    echo "   roles/iam.serviceAccountUser on $target"
  else
    echo "   WARNING: $target does not exist - deploys running as it will fail" >&2
  fi
done

cat <<EOF

done.

Put these in the deploy workflow (they are identifiers, not secrets):

  workload_identity_provider: projects/$SHARED_NUM/locations/global/workloadIdentityPools/$POOL/providers/$PROVIDER
  service_account:            $SA

Verify the boundary holds - this should be the ONLY repository listed:

  gcloud iam workload-identity-pools providers describe $PROVIDER \\
    --workload-identity-pool=$POOL --location=global --project=$SHARED \\
    --format='value(attributeCondition)'
EOF
