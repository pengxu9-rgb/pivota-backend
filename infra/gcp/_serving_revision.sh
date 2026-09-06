# What a Cloud Run service is ACTUALLY SERVING — not what its template asks for.
#
# Sourced by deploy_worker.sh and setup_scheduler.sh. `prod-deploy-drift.yml` implements the
# same read inline because it is a workflow and cannot source a shell file; if you change the
# semantics here, change it there too.
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
# Requires GCLOUD, PROJECT and REGION to be set by the caller. Every function prints nothing on
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
  out="$("$GCLOUD" run services describe "$svc" --project "$PROJECT" --region "$REGION" \
        --format=json)" || return 1
  printf '%s' "$out" | python3 -c '
import json,sys
try: d = json.load(sys.stdin)
except Exception: sys.exit(1)
live = [t["revisionName"] for t in (d.get("status", {}).get("traffic") or [])
        if t.get("percent") == 100 and t.get("revisionName")]
if len(live) != 1: sys.exit(1)
print(live[0])' 2>/dev/null
}

# The container image on the revision that is serving.
serving_image(){ # <service>
  local rev; rev="$(serving_revision "$1")" || return 1
  [ -n "$rev" ] || return 1
  "$GCLOUD" run revisions describe "$rev" --project "$PROJECT" --region "$REGION" \
    --format='value(spec.containers[0].image)' | head -1
}

# One env var's value on the revision that is serving.
serving_env(){ # <service> <VAR>
  local rev; rev="$(serving_revision "$1")" || return 1
  [ -n "$rev" ] || return 1
  "$GCLOUD" run revisions describe "$rev" --project "$PROJECT" --region "$REGION" \
    --format=json | VAR="$2" python3 -c '
import json,os,sys
try: c = json.load(sys.stdin)["spec"]["containers"][0]
except Exception: sys.exit(1)
for e in (c.get("env") or []):
    if e.get("name") == os.environ["VAR"]:
        print(e.get("value", "")); sys.exit(0)
sys.exit(1)' 2>/dev/null
}
