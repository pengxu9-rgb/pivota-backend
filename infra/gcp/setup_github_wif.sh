#!/usr/bin/env bash
# Let GitHub Actions deploy to Cloud Run without a service-account key.
#   infra/gcp/setup_github_wif.sh [repo] [repo_id]   default repo: pengxu9-rgb/pivota-backend
#
# repo_id is the repository's IMMUTABLE numeric id. Resolved with `gh` when omitted; see
# "WHY THE BOUNDARY IS THE ID" below for why it, and not the name, is the boundary.
#
# Idempotent, like the other setup_* scripts here: re-running reconciles rather than duplicates.
#
# WHY KEYLESS. The alternative is a downloaded service-account JSON key pasted into a GitHub
# secret. That key is a bearer credential with deploy rights on a payments platform, it never
# expires, and nothing here would notice if it leaked. Workload Identity Federation trades it for
# a GitHub-signed OIDC token that is minted per job, expires in minutes, and is bound by an
# attribute condition to ONE repository. There is no long-lived secret to leak.
#
# WHAT IT CAN DO: build the backend image in pivota-shared, and roll Cloud Run `web` in
# pivota-prod forward onto it.
#
# WHAT IT CAN ALSO DO, AND SHOULD NOT - stated plainly, because the first version of this header
# claimed the opposite and was wrong on all three counts. This identity holds `run.admin` on
# pivota-prod and `iam.serviceAccountUser` on `sa-backend`, which carries PROJECT-WIDE
# `secretmanager.secretAccessor` and `cloudsql.client`. Deploying a revision or a job that RUNS AS
# sa-backend therefore reaches every production secret - live payment keys, signing keys,
# DATABASE_URL - and Cloud SQL over the default VPC. `CONFIG=preserve` in deploy_backend.sh is a
# convention inside one shell script, not an IAM control; `run.admin` can rewrite the environment
# regardless of what that script chooses to send.
#
# Closing it properly means replacing sa-backend's project-level secretAccessor with per-secret
# grants for only what `web` mounts, and conditioning run.admin on the `web` service. That is a
# change to how the service itself is configured, so it is tracked separately rather than done
# quietly here. Until then, treat a compromised Actions run on this repo as equivalent to
# disclosure of every prod secret, and rotate on that basis.
set -euo pipefail

REPO="${1:-pengxu9-rgb/pivota-backend}"
# Validate the SHAPE, not just the slash. This string is interpolated into a CEL expression below,
# inside single quotes. `*/*` alone admits an argument like  a/b' || true || '  which closes the
# quote and appends a tautology, turning the provider's only security boundary into one that
# accepts an OIDC token from ANY repository on GitHub.
[[ "$REPO" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]] \
  || { echo "repo must be owner/name using [A-Za-z0-9._-] (got '$REPO')" >&2; exit 2; }

# WHY THE BOUNDARY IS THE ID, NOT THE NAME.
# This used to pin `assertion.repository == '$REPO'`, with a comment calling the rename hazard
# "STILL OPEN, deliberately". It came due on 2026-09-04: the account was renamed, and within
# minutes BOTH halves of that hazard were real.
#   * Availability - GitHub's OIDC token started carrying the new name, the condition rejected it,
#     and every prod deploy failed at google-github-actions/auth with
#     `unauthorized_client: The given credential is rejected by the attribute condition`.
#   * Security - GitHub does not reserve a released username. While the config still named the old
#     owner, `gh api users/<old-owner>` returned 404: anyone could have registered it, created a
#     repo of the same name, pushed a workflow on main, and minted a token this provider accepted
#     - impersonating an identity with run.admin on prod.
# The numeric id is immutable across renames and transfers (verified: identical before, during and
# after both renames that day) and is not reassignable to anyone else, so it is strictly stronger
# than the name. The name is therefore GONE from the condition rather than kept beside the id:
# keeping it would re-break deploys on the next rename while adding no security.
GH="${GH:-gh}"
REPO_ID="${2:-${REPO_ID:-}}"
if [ -z "$REPO_ID" ]; then
  # Best-effort, and deliberately not fatal on its own: the original comment here was right that a
  # bootstrap script should not REQUIRE an authenticated API call. It may require the ANSWER,
  # though - so resolve it when we can and tell the operator exactly how to supply it when we
  # cannot. Shipping without the pin is not one of the options; that is the hole above.
  REPO_ID="$("$GH" api "repos/$REPO" --jq .id 2>/dev/null || true)"
fi
[[ "$REPO_ID" =~ ^[0-9]+$ ]] || {
  echo "cannot resolve the immutable numeric id for '$REPO' (got '${REPO_ID:-}')" >&2
  echo "the provider's security boundary is that id, so this script will not configure one" >&2
  echo "without it. Supply it explicitly:" >&2
  echo "  gh api repos/$REPO --jq .id" >&2
  echo "  infra/gcp/setup_github_wif.sh $REPO <id>" >&2
  exit 2; }

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
echo "repo id : $REPO_ID   (the boundary; the name above is only a label)"
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
# Pin the BRANCH here, not only in the workflow. `workflow_dispatch` runs the workflow file, and
# the tooling it checks out, from the ref it was dispatched FROM - so an in-workflow branch check
# is a control the attacker edits in the same commit that attacks it. Anyone with push access can
# dispatch a branch whose deploy step has been rewritten. IAM is the layer they cannot reach.
#
# CLOSED (was "STILL OPEN, deliberately"): the repository is matched by immutable ID, so a released
# username buys an attacker nothing - their repo has a different id. See the block above.
CONDITION="assertion.repository_id == '$REPO_ID' && assertion.ref == 'refs/heads/main'"
# `attribute.repository` stays MAPPED even though nothing is conditioned on it: it is what makes a
# binding readable in the console and in audit logs, where an id alone tells an operator nothing.
# Mapped is not trusted - only what the CONDITION names is trusted.
MAPPING="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_id=assertion.repository_id,attribute.ref=assertion.ref"

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

# Only tokens carrying attribute.repository_id == $REPO_ID may impersonate the account. Keyed on
# the id for the same reason as the condition: a name-keyed binding stops matching the moment the
# repo is renamed, which breaks every deploy even while the condition still passes.
#
# Scoped to the repository, NOT to a branch: the deploy workflow verifies the target SHA is an
# ancestor of main itself, which survives a branch being renamed and is checked against git rather
# than a claim in a token. Adding attribute.ref here as well would break workflow_dispatch from a
# tag or a re-run, without adding a control the workflow does not already enforce.
PRINCIPAL="principalSet://iam.googleapis.com/projects/$SHARED_NUM/locations/global/workloadIdentityPools/$POOL/attribute.repository_id/$REPO_ID"
echo "== binding workloadIdentityUser for $REPO (id $REPO_ID)"
"$GCLOUD" iam service-accounts add-iam-policy-binding "$SA" --project="$SHARED" \
  --role="roles/iam.workloadIdentityUser" --member="$PRINCIPAL" --quiet >/dev/null

# REMOVE the name-keyed binding this script used to add. Adding the id-keyed one does not retire
# it: `add-iam-policy-binding` only ever adds, so a stale `attribute.repository/<old-owner>/<name>`
# member keeps granting impersonation to whoever ends up owning that name. That is precisely the
# exposure of 2026-09-04, and it would survive this hardening unless it is deleted by name.
# Tolerates "binding not found" so re-runs stay idempotent.
STALE_PRINCIPAL="principalSet://iam.googleapis.com/projects/$SHARED_NUM/locations/global/workloadIdentityPools/$POOL/attribute.repository/$REPO"
echo "== retiring any name-keyed binding for $REPO"
"$GCLOUD" iam service-accounts remove-iam-policy-binding "$SA" --project="$SHARED" \
  --role="roles/iam.workloadIdentityUser" --member="$STALE_PRINCIPAL" --quiet >/dev/null 2>&1 \
  && echo "   removed $STALE_PRINCIPAL" \
  || echo "   none present (nothing to retire)"

# ---------------------------------------------------------------- roles
# pivota-shared: submit the build, push the image, read build logs.
echo "== granting build roles on $SHARED"
for role in roles/cloudbuild.builds.editor roles/artifactregistry.writer \
            roles/storage.admin roles/logging.viewer roles/serviceusage.serviceUsageConsumer; do
  "$GCLOUD" projects add-iam-policy-binding "$SHARED" \
    --member="serviceAccount:$SA" --role="$role" --quiet --condition=None >/dev/null
  echo "   $role"
done

# `gcloud builds submit` runs the build AS a service account, and the caller must be allowed to
# act as it. Which account that is has CHANGED: builds used to default to the legacy Cloud Build
# agent <num>@cloudbuild.gserviceaccount.com, and now default to the project's Compute Engine
# default service account. On this project the legacy agent does not exist at all.
#
# The first version of this script assumed the legacy name and wrapped the grant in
# `if describe >/dev/null 2>&1`. The account was absent, so the guard SILENTLY SKIPPED the only
# binding that mattered, the script exited 0 twice, and the gap surfaced as a PERMISSION_DENIED
# naming a service account by NUMERIC ID - in the first real deploy, which is the worst place to
# learn it. A probe that cannot fail reports success for a job it never did.
#
# So: try both, grant on every one that exists, and fail loudly if none do.
CB_GRANTED=0
for CB_SA in "$SHARED_NUM@cloudbuild.gserviceaccount.com" "$SHARED_NUM-compute@developer.gserviceaccount.com"; do
  "$GCLOUD" iam service-accounts describe "$CB_SA" --project="$SHARED" >/dev/null 2>&1 || continue
  "$GCLOUD" iam service-accounts add-iam-policy-binding "$CB_SA" --project="$SHARED" \
    --role="roles/iam.serviceAccountUser" --member="serviceAccount:$SA" --quiet >/dev/null
  echo "   roles/iam.serviceAccountUser on $CB_SA"
  CB_GRANTED=$((CB_GRANTED + 1))
done
# NOTE ON SCOPE: the compute default service account usually carries project Editor, so actAs on
# it is a broad grant within pivota-shared. It is also exactly the account a human operator's
# `gcloud builds submit` already runs as, so this puts CI at parity with the operator rather than
# above them. Narrowing it means giving Cloud Build a dedicated, least-privileged build service
# account and passing --service-account - worth doing, but a change to how everyone builds, not
# something this script should decide on its own.
[ "$CB_GRANTED" -gt 0 ] || {
  echo "no Cloud Build service account found in $SHARED - 'gcloud builds submit' will fail with" >&2
  echo "PERMISSION_DENIED naming an account by numeric id. Resolve it with:" >&2
  echo "  gcloud iam service-accounts list --project=$SHARED --format='table(email,uniqueId)'" >&2
  exit 1; }

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
#
# sa-worker is NOT optional, and it is not obvious why: deploy_backend.sh verifies an
# internal-ingress candidate by creating a one-shot Cloud Run JOB inside the VPC that runs as
# sa-worker (deploy_backend.sh, probe_health). A security review recommended dropping this grant
# on the grounds that the workflow "only deploys web" - it would have broken the candidate health
# gate on every production deploy. Read probe_health before removing it.
MISSING=""
for target in "sa-backend@$PROD.iam.gserviceaccount.com" "sa-worker@$PROD.iam.gserviceaccount.com"; do
  if "$GCLOUD" iam service-accounts describe "$target" --project="$PROD" >/dev/null 2>&1; then
    "$GCLOUD" iam service-accounts add-iam-policy-binding "$target" --project="$PROD" \
      --role="roles/iam.serviceAccountUser" --member="serviceAccount:$SA" --quiet >/dev/null
    echo "   roles/iam.serviceAccountUser on $target"
  else
    MISSING="$MISSING $target"
  fi
done
# Exit non-zero rather than warn. A warning on stderr followed by "done." and exit 0 is how the
# Cloud Build grant went missing in the first place: the operator reads the exit code, not the
# scrollback, and the gap surfaces in the first deploy as a PERMISSION_DENIED naming an account by
# numeric id. Run bootstrap_env.sh first if these do not exist yet.
[ -z "$MISSING" ] || {
  echo "these service accounts do not exist in $PROD:$MISSING" >&2
  echo "deploys that run as them will fail. Run infra/gcp/bootstrap_env.sh first." >&2
  exit 1; }

cat <<EOF

done.

Put these in the deploy workflow (they are identifiers, not secrets):

  workload_identity_provider: projects/$SHARED_NUM/locations/global/workloadIdentityPools/$POOL/providers/$PROVIDER
  service_account:            $SA

Verify the boundary holds - this should name id $REPO_ID ($REPO) and nothing else:

  gcloud iam workload-identity-pools providers describe $PROVIDER \\
    --workload-identity-pool=$POOL --location=global --project=$SHARED \\
    --format='value(attributeCondition)'

And that no name-keyed member survives on the identity (a rename would hand it to a stranger):

  gcloud iam service-accounts get-iam-policy $SA --project=$SHARED \\
    --format=json | grep 'attribute.repository/' || echo "clean: id-keyed only"
EOF
