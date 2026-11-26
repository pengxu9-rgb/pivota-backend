#!/usr/bin/env bash
# Read-only isolation checks for the partner sandbox. Changes nothing, prints no secret value.
#
#   infra/gcp/sandbox/verify_sandbox.sh            # every check that applies to what exists
#   infra/gcp/sandbox/verify_sandbox.sh secrets    # one group: project|iam|secrets|network|sql|run|bucket|edge
#
# Exit 1 if any check fails. Each RUNBOOK.md step names the group to run after it.
# Reads: IAM policies of pivota-sandbox, pivota-prod, pivota-staging and pivota-shared (project
# level), Cloud Asset IAM search results in those three (resource level), the shared registry
# repos' policies, each sandbox secret's METADATA and IAM policy (never `versions access`), and the
# sandbox's Cloud Run specs and invoker policies (plain env values hold no secrets).
# The decisions live in verify_checks.py; each ok line states exactly what was checked (review S8).
# Real prod/staging reads: give the controller a heads-up before running the iam group.
set -euo pipefail
# shellcheck source=infra/gcp/sandbox/config.sh
. "$(cd "$(dirname "$0")" && pwd)/config.sh"
# Every read below ends in `|| true`: under pipefail a failed read inside $(...) would otherwise
# make set -e END the script after the first group, silently (found by test_s8_iam_group_is_opt_in).
# A failed read must surface as a FAIL line from the check that needed it, never as an exit.
ONLY="${1:-all}"
FAIL=0
TMPF=$(mktemp); trap 'rm -f "$TMPF"' EXIT
VC="$HERE_SANDBOX/verify_checks.py"
ok(){ printf '  \033[32mok\033[0m   %s\n' "$*"; }
bad(){ printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAIL=1; }
# The iam group reads REAL prod/staging/shared/org policies, so it is opt-in: a bare run skips it
# (review S8). Run `verify_sandbox.sh iam` explicitly, after the controller's heads-up.
want(){ if [ "$1" = iam ]; then [ "$ONLY" = iam ]; else [ "$ONLY" = all ] || [ "$ONLY" = "$1" ]; fi; }
PN=$(g projects describe "$SANDBOX_PROJECT" --format='value(projectNumber)' 2>/dev/null || true)
# Feed one JSON document to a verify_checks.py check and turn its lines into ok/bad.
judge(){ # label check [extra args...] < json
  local label="$1" check="$2"; shift 2
  python3 "$VC" "$check" --project "$SANDBOX_PROJECT" --number "$PN" --label "$label" "$@" > "$TMPF" \
    || echo "FAIL $label: verify_checks.py crashed (nothing verified)" > "$TMPF"
  while read -r l; do case "$l" in FAIL*) bad "${l#FAIL }";; ok*) ok "${l#ok }";; esac; done < "$TMPF"
}
sa_for_list(){ case "$1" in web) echo "$SA_BACKEND";; worker) echo "$SA_WORKER";; gateway) echo "$SA_GATEWAY";;
  agenpaz) echo "$SA_AGENPAZ";; catalog-import|schema-apply) echo "$SA_IMPORT";; ops) echo "$SA_OPS";; esac; }

expected_secrets(){ { cat "$HERE_SANDBOX"/secrets/*.list | sed 's/^[^=]*=//'; echo pivota-db-password; } | sort -u; }

if want project; then
  say "project"
  parent=$("$GCLOUD" projects describe "$SANDBOX_PROJECT" --format='value(parent.type,parent.id)' 2>/dev/null | tr '\t' ' ' || true)
  [ "$parent" = "organization $ORG_ID" ] && ok "in org $ORG_ID" || bad "project parent is '$parent'"
  [ "$(g projects describe "$SANDBOX_PROJECT" --format='value(labels.env)' 2>/dev/null)" = sandbox ] \
    && ok "label env=sandbox" || bad "label env=sandbox missing"
fi

if [ "$ONLY" = all ]; then
  say "iam: SKIPPED in a bare run (it reads prod/staging/shared and org policies); run: $0 iam"
fi

# Cloud Asset search, fail-closed: each query's OWN exit status decides. Merging an empty, failed
# answer would read as "[]", i.e. "searched and found nothing" (tests: asset-fails, first-fails).
asset_docs(){ # scope query...   -> one JSON list on stdout, or nothing when ANY query failed
  local scope="$1" q out; shift; local -a parts=()
  for q in "$@"; do
    out=$("$GCLOUD" asset search-all-iam-policies --scope="$scope" ${q:+--query="$q"} --format=json 2>/dev/null) || return 1
    parts+=("$out")
  done
  P="$(printf '%s\n\x1e' "${parts[@]}")" python3 -c 'import json,os
docs=[]
for chunk in os.environ["P"].split("\x1e"):
    chunk=chunk.strip()
    if chunk:
        o=json.loads(chunk); docs += o if isinstance(o,list) else [o]
print(json.dumps(docs))' 2>/dev/null
}

if want iam; then
  say "iam: sandbox principals outside the sandbox, other principals inside it"
  [ -n "$PN" ] || bad "could not read $SANDBOX_PROJECT's project number: service-agent and default-SA checks below are blind"
  for p in pivota-prod pivota-staging pivota-shared; do
    judge "$p project policy" foreign-policy < <("$GCLOUD" projects get-iam-policy "$p" --format=json 2>/dev/null)
  done
  # Resource-level bindings anywhere in the ORG (every project, folder and the org itself): one
  # search by the sandbox's name and one by its number. Its own project's results are judged below.
  if res=$(asset_docs "organizations/$ORG_ID" "policy:$SANDBOX_PROJECT" ${PN:+"policy:$PN"}) && [ -n "$res" ]; then
    judge "org $ORG_ID resource policies" asset-search --allow-registry-read <<<"$res"
  else
    bad "org-wide resource-level policies NOT verified (Cloud Asset search failed or is not enabled)"
  fi
  for repo in pivota pivota-gateway; do
    judge "$SHARED_PROJECT/$repo registry" registry-policy < <("$GCLOUD" artifacts repositories get-iam-policy "$repo" --location "$REGION" --project "$SHARED_PROJECT" --format=json 2>/dev/null)
  done
  owners=(); IFS=, read -r -a _o <<<"${SANDBOX_ALLOWED_OWNERS:-}"; for o in ${_o[@]+"${_o[@]}"}; do [ -n "$o" ] && owners+=(--allowed-owner "$o"); done
  judge "$SANDBOX_PROJECT project policy" sandbox-policy ${owners[@]+"${owners[@]}"} < <(g projects get-iam-policy "$SANDBOX_PROJECT" --format=json 2>/dev/null)
  # The sandbox's OWN resource-level policies (secrets, bucket, run services, service accounts...).
  if res=$(asset_docs "projects/$SANDBOX_PROJECT" "") && [ -n "$res" ]; then
    judge "$SANDBOX_PROJECT resource policies" sandbox-resources --drop-bucket "$DROP_BUCKET" --export-sa "$PROD_EXPORT_SA" <<<"$res"
  else
    bad "$SANDBOX_PROJECT resource-level policies NOT verified (Cloud Asset search failed or is not enabled)"
  fi
fi

if want secrets; then
  say "secrets: the inventory is exactly secrets/*.list + the DB password"
  have_list=$(g secrets list --format='value(name.basename())' 2>/dev/null | sort -u || true)
  extra=$(comm -13 <(expected_secrets) <(printf '%s\n' "$have_list" | sed '/^$/d'))
  absent=$(comm -23 <(expected_secrets) <(printf '%s\n' "$have_list" | sed '/^$/d'))
  [ -z "$extra" ] && ok "no unexpected secret" || bad "unexpected secret(s): $(echo $extra)"
  [ -z "$absent" ] && ok "every expected secret exists" || bad "missing secret(s): $(echo $absent)"
  for s in $have_list; do
    lab=$(g secrets describe "$s" --format='value(labels.env,labels.origin)' 2>/dev/null | tr '\t' ' ' || true)
    case "$lab" in
      "sandbox sandbox-cloudsql"|"sandbox reap-sandbox"|"sandbox minted-in-sandbox") ;;
      *) bad "$s labels '$lab' (expected env=sandbox and an origin)";;
    esac
    n=$(g secrets versions list "$s" --filter='state=ENABLED' --format='value(name)' 2>/dev/null | wc -l | tr -d ' ' || true)
    [ "$n" -ge 1 ] || bad "$s has no enabled version"
    # Exactly the accessors the secrets lists imply (pivota-db-password: none; it is read by humans only).
    exp=()
    for lst in "$HERE_SANDBOX"/secrets/*.list; do
      if grep -qx "$s" <<<"$(cut -d= -f2 "$lst")"; then exp+=(--expect-member "serviceAccount:$(sa_for_list "$(basename "$lst" .list)")"); fi
    done
    judge "secret $s" secret-policy ${exp[@]+"${exp[@]}"} < <(g secrets get-iam-policy "$s" --format=json 2>/dev/null)
  done
  ok "labels and versions checked on $(printf '%s\n' "$have_list" | sed '/^$/d' | wc -l | tr -d ' ') secrets"
fi

if want network; then
  say "network"
  peers=$(g compute networks peerings list --network "$VPC" --format='value(peerings[].network)' 2>/dev/null | tr ';' '\n' | sed '/^$/d' || true)
  # An empty list is its own failure (no Cloud SQL peering), not "a peer that is not servicenetworking".
  if [ -z "$peers" ]; then bad "VPC $VPC has no peering at all (Cloud SQL's servicenetworking peering is missing)"
  elif grep -vq 'servicenetworking' <<<"$peers"; then bad "VPC $VPC peers with something other than servicenetworking: $peers"
  else ok "VPC $VPC peers only with servicenetworking (Cloud SQL)"; fi
  g compute networks describe default >/dev/null 2>&1 && bad "the auto-mode 'default' VPC still exists" || ok "no 'default' VPC"
  ip=$(g compute addresses describe "$EGRESS_IP_NAME" --region "$REGION" --format='value(address)' 2>/dev/null || true)
  [ -n "$ip" ] && ok "egress IP $ip (reserved; the sandbox's own)" || bad "egress IP $EGRESS_IP_NAME missing"
fi

if want sql; then
  say "cloud sql"
  types=$(g sql instances describe "$SQL_INSTANCE" --format='value(ipAddresses[].type)' 2>/dev/null | tr ';' ' ' || true)
  [ "$types" = PRIVATE ] && ok "$SQL_INSTANCE has a private IP only" || bad "$SQL_INSTANCE ip types: '$types'"
fi

if want run; then
  say "cloud run: services, identities, images, env contract, secret references, invokers"
  svcs=$(g run services list --region "$REGION" --format='value(metadata.name)' 2>/dev/null | sort | tr '\n' ' ' || true)
  [ "$svcs" = "agenpaz gateway web worker " ] && ok "services: $svcs" || bad "services are '$svcs' (expected agenpaz gateway web worker)"
  for svc in web worker gateway agenpaz; do
    exp=(); for sec in $(cut -d= -f2 "$HERE_SANDBOX/secrets/$svc.list"); do exp+=(--expect-member "$sec"); done
    case "$svc" in web) sa=$SA_BACKEND;; worker) sa=$SA_WORKER;; gateway) sa=$SA_GATEWAY;; agenpaz) sa=$SA_AGENPAZ;; esac
    judge "run $svc" run-service --svc "$svc" --service-account "$sa" ${exp[@]+"${exp[@]}"} < <(g run services describe "$svc" --region "$REGION" --format=json 2>/dev/null)
    judge "run $svc invokers" run-invoker --svc "$svc" < <(g run services get-iam-policy "$svc" --region "$REGION" --format=json 2>/dev/null)
  done
fi

if want bucket; then
  say "catalog drop bucket"
  pap=$(g storage buckets describe "gs://$DROP_BUCKET" --format=json 2>/dev/null | python3 -c '
import json,sys
d=json.load(sys.stdin)
print("ok" if d.get("public_access_prevention")=="enforced" and d.get("uniform_bucket_level_access") else "FAIL")' 2>/dev/null || true)
  [ "$pap" = ok ] && ok "uniform access + public access prevention" || bad "gs://$DROP_BUCKET not uniform/PAP-enforced (or missing)"
  members=$(g storage buckets get-iam-policy "gs://$DROP_BUCKET" --format=json 2>/dev/null | python3 -c '
import json,sys
d=json.load(sys.stdin)
for b in d.get("bindings",[]):
    for m in b.get("members",[]): print(b["role"], m)' 2>/dev/null | sort || true)
  expected=$(printf '%s\n' "roles/storage.objectCreator serviceAccount:$PROD_EXPORT_SA" "roles/storage.objectViewer serviceAccount:$SA_IMPORT" | sort)
  [ "$members" = "$expected" ] && ok "bucket IAM is exactly: prod export SA creates, sandbox import SA reads" \
    || bad "bucket IAM differs from the two expected bindings:\n$members"
fi

if want edge; then
  say "edge (public hostnames)"
  for h in "$GATEWAY_HOST" "$AGENPAZ_HOST"; do
    st=$(g certificate-manager certificates describe "cert-$(printf '%s' "$h" | tr . -)" --format='value(managed.state)' 2>/dev/null || true)
    [ "$st" = ACTIVE ] && ok "cert for $h ACTIVE" || bad "cert for $h is '${st:-missing}'"
  done
fi

[ "$FAIL" = 0 ] && { say "all checks passed ($ONLY)"; exit 0; } || { say "CHECKS FAILED ($ONLY)"; exit 1; }
