# What a Cloud Run service is ACTUALLY SERVING — not what its template asks for.
#
# Sourced by deploy_worker.sh, setup_scheduler.sh, setup_store_audit_commerce_jobs.sh and
# setup_store_audit_ucp_jobs.sh — including setup_scheduler's two Store Audit guards, which each
# carried their own copy of the 100%-traffic resolution until this file existed. `prod-deploy-drift.yml` keeps an inline implementation because it is a workflow
# and cannot source a shell file; if you change the semantics here, change it there too. That
# is the ONLY remaining copy, and it is one more than anybody wants.
#
# ── WHY THIS EXISTS ────────────────────────────────────────────────────────────────────────────
# `spec.template` is the template of the LAST REVISION CREATED. That is NOT the revision taking
# traffic, and the difference is exactly the state you care about when something has gone wrong:
#
#   - deploy_backend.sh creates every revision `--tag c-<sha> --no-traffic`, health-checks it,
#     and promotes only on 200. A candidate that FAILS leaves the template naming the new image
#     while the previous revision still serves every request.
#   - Any `gcloud run deploy` that errors after the revision is created leaves the same split.
#   - A revision that never becomes Ready never takes traffic, but it is still the template.
#
# So `spec.template` answers "what did the last deploy ASK FOR", and the 100%-traffic revision
# answers "what is RUNNING". Reading the first when you meant the second is how prod-deploy-drift
# came to report a FAILED deploy as shipped (fixed in #2091), and it is subtle enough that it was
# reproduced by hand, in a verification script, ten minutes after that fix merged.
#
# ── WHEN spec.template IS THE RIGHT READ, and this file is NOT what you want ────────────────────
# If the question is "what will the NEXT deploy inherit", the template IS the answer, because
# `gcloud run deploy --update-env-vars` merges into the service template rather than into the
# serving revision. deploy_backend.sh's pool-drift guard and restore_to_cloudsql.sh's minScale
# capture are both that question and both correctly read the template. Do not "fix" them.
#
# Requires GCLOUD and REGION. PROJECT is optional -- omitted from the gcloud call when unset, so
# a caller relying on CLOUDSDK_CORE_PROJECT still works. NOTE: every caller today sets PROJECT
# before sourcing, so that branch is currently unexercised; it is defensive, not load-bearing. Every function prints nothing on
# stdout and returns non-zero when it cannot answer — an unreadable service must never look like a
# clean one.
#
# GCLOUD'S STDERR IS DELIBERATELY NOT SWALLOWED. A caller that needs to CLASSIFY the failure —
# deploy_worker.sh distinguishes "no such service, create it" from "the API broke, refuse" — can
# only do that from gcloud's own message, and a helper that hid it forced an ugly ask-again-
# noisily dance. Redirect at the call site if you want it quiet.

# The revision serving 100% of traffic. Refuses a split, which has no single answer.
serving_revision(){ # <service>
  local svc="$1" out
  local proj=(); [ -n "${PROJECT:-}" ] && proj=(--project "$PROJECT")
  out="$("$GCLOUD" run services describe "$svc" ${proj[@]+"${proj[@]}"} --region "$REGION" \
        --format=json)" || return 1
  printf '%s' "$out" | python3 -c '
import json,sys
try: d = json.load(sys.stdin)
except Exception: sys.exit(1)
st = d.get("status", {})
live = [t for t in (st.get("traffic") or []) if t.get("percent") == 100]
# Exactly one entry at 100. A nameless entry is kept in the list rather than filtered out:
# filtering would let a two-entry split resolve by ignoring half of itself.
if len(live) != 1: sys.exit(1)
# `revisionName` on a STATUS traffic target is the resolved one and is populated in practice
# (measured across web/worker/proof-issuer/gateway 2026-09-06: always present). It is NOT
# always equal to latestReadyRevisionName: measured the same day on web, revisionName was
# web-00572-guh while latestReadyRevisionName was web-00575-gak -- a newer revision Ready but
# not yet taking traffic, i.e. the half-finished-rollout state this file exists for. So
# revisionName is preferred because it is the SERVING one, and the latestRevision fallback
# (kept from the UCP guard this function absorbed) answers only when the target itself says
# it follows latest.
name = live[0].get("revisionName") or (
    st.get("latestReadyRevisionName", "") if live[0].get("latestRevision") else "")
if not name: sys.exit(1)
print(name)' 2>/dev/null
}

# The container image on the revision that is serving.
#
# THIS IS A DIGEST REFERENCE, NEVER THE TAG. Cloud Run resolves the tag when it creates the
# revision, so `spec.containers[0].image` on a revision reads `.../backend@sha256:<64 hex>`
# even though the deploy asked for `.../backend:<commit sha>` (measured on prod 2026-09-06;
# prod-deploy-drift.yml documents the same). It is the right thing to DEPLOY on a rollback --
# it names exactly the bytes that were serving -- and the wrong thing to derive a commit from:
# `${{image##*:}}` yields the digest, not the sha. The commit a revision runs is what it
# DECLARES: `serving_env <svc> PIVOTA_COMMIT_SHA`.
serving_image(){ # <service>
  local rev out; rev="$(serving_revision "$1")" || return 1
  [ -n "$rev" ] || return 1
  # NO `| head -1`. A pipeline's status is the LAST command's, so `head` would swallow a
  # failing describe and return 0 with empty output — breaking the contract three lines above
  # ("returns non-zero when it cannot answer") in exactly the direction that makes an
  # unreadable service look clean. Measured under `set -eu` without pipefail: rc=0. It also
  # introduced a SIGPIPE path (rc=141, five times out of five with a chatty producer). Every
  # caller today sets pipefail, but this is a shared file and a GitHub Actions `run:` step is
  # `bash -e` WITHOUT it. `value()` on a single field is one line; head was never needed.
  local proj=(); [ -n "${PROJECT:-}" ] && proj=(--project "$PROJECT")
  out="$("$GCLOUD" run revisions describe "$rev" ${proj[@]+"${proj[@]}"} --region "$REGION" \
    --format='value(spec.containers[0].image)')" || return 1
  # gcloud's value() projection prints EMPTY with rc 0 when the field path does not resolve.
  # The contract in the header is "non-zero when it cannot answer", and deploy_worker.sh reads
  # an empty answer as "the service does not exist" -- which would deploy over a live worker
  # with no rollback anchor. Empty is not an answer.
  [ -n "$out" ] || return 1
  printf '%s\n' "$out"
}

# One env var's value on the revision that is serving.
serving_env(){ # <service> <VAR>
  local rev; rev="$(serving_revision "$1")" || return 1
  [ -n "$rev" ] || return 1
  local proj=(); [ -n "${PROJECT:-}" ] && proj=(--project "$PROJECT")
  "$GCLOUD" run revisions describe "$rev" ${proj[@]+"${proj[@]}"} --region "$REGION" \
    --format=json | VAR="$2" python3 -c '
import json,os,sys
try: c = json.load(sys.stdin)["spec"]["containers"][0]
except Exception: sys.exit(1)
for e in (c.get("env") or []):
    if e.get("name") == os.environ["VAR"]:
        print(e.get("value", "")); sys.exit(0)
sys.exit(1)' 2>/dev/null
}
