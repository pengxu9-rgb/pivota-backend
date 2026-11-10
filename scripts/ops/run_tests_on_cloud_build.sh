#!/usr/bin/env bash
# Run the backend test suites on Cloud Build instead of this machine (infra/gcp/cloudbuild.tests.yaml).
#
#   scripts/ops/run_tests_on_cloud_build.sh [<worktree>]      # default: the current directory
#
# WHAT IS UPLOADED: exactly the files git TRACKS in <worktree>, as they are on disk (so modified
# tracked files are included). An untracked file is never uploaded, whatever .gitignore or
# .gcloudignore say, which keeps local notes and scratch out of the build. Commit a new file
# (locally is enough) before running if the run needs it.
#
# The build config is always THIS checkout's copy, so a branch that predates it can still be tested.
# Prints the build id, waits for the result, prints the summary block, and exits 0 only on GREEN.
# Env: CLOUD_BUILD_TESTS_PROJECT (default pivota-shared). Deliberately not PROJECT, which this repo's
# deploy scripts export for the production project.
set -euo pipefail

SRC="${1:-.}"
PROJECT="${CLOUD_BUILD_TESTS_PROJECT:-pivota-shared}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONFIG="$HERE/infra/gcp/cloudbuild.tests.yaml"
HELPER="$HERE/scripts/ops/cloud_build_tests.py"

git -C "$SRC" rev-parse --show-toplevel >/dev/null 2>&1 || { echo "$SRC is not a git worktree" >&2; exit 2; }
SRC="$(git -C "$SRC" rev-parse --show-toplevel)"
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/cloud-build-tests.XXXXXX")"
trap 'rm -rf -- "$STAGE"' EXIT

# The tracked files only, as they are on disk. Deleted-but-tracked files are skipped.
( cd "$SRC" && git ls-files -z | while IFS= read -r -d '' f; do [ -e "$f" ] && printf '%s\0' "$f"; done ) \
  > "$STAGE/files.lst"
# The runner's own helper must be in the build even when <worktree> predates it.
mkdir -p "$STAGE/overlay/scripts/ops"
cp "$HELPER" "$STAGE/overlay/scripts/ops/cloud_build_tests.py"
# --no-recursion: a tracked path that is now a directory must not pull in its untracked contents.
tar -C "$SRC" --no-recursion --null -T "$STAGE/files.lst" -cf "$STAGE/src.tar"
tar -C "$STAGE/overlay" -rf "$STAGE/src.tar" scripts/ops/cloud_build_tests.py
gzip -9 "$STAGE/src.tar"
echo "uploading $(tr -cd '\0' < "$STAGE/files.lst" | wc -c | tr -d ' ') tracked files from $SRC" \
     "at $(git -C "$SRC" rev-parse --short HEAD)$(git -C "$SRC" diff HEAD --quiet || echo ' (+ uncommitted changes)')"

BUILD_ID="$(gcloud builds submit "$STAGE/src.tar.gz" --project "$PROJECT" --config "$CONFIG" \
  --async --format='value(id)')"
echo "build $BUILD_ID: https://console.cloud.google.com/cloud-build/builds/$BUILD_ID?project=$PROJECT"

# One failed describe (a network blip) must not abandon a running build without its summary; ten in
# a row (about five minutes) is a real problem and is reported as one.
STATUS=""
FAILS=0
while :; do
  if NEXT="$(gcloud builds describe "$BUILD_ID" --project "$PROJECT" --format='value(status)')"; then
    STATUS="$NEXT"; FAILS=0
  else
    FAILS=$((FAILS + 1))
    [ "$FAILS" -ge 10 ] && { echo "gcloud builds describe failed $FAILS times in a row; build $BUILD_ID may still be running" >&2; exit 3; }
  fi
  case "$STATUS" in
    SUCCESS|FAILURE|INTERNAL_ERROR|TIMEOUT|CANCELLED|EXPIRED) break ;;
  esac
  sleep 30
done
echo "build $BUILD_ID: $STATUS"

# The summary block, from the build's log in Cloud Logging. Logging can lag a few seconds.
for _ in 1 2 3 4 5 6; do
  LOG="$(gcloud logging read "resource.type=\"build\" AND resource.labels.build_id=\"$BUILD_ID\"" \
    --project "$PROJECT" --order=asc --limit=20000 --format='value(textPayload)' 2>/dev/null || true)"
  if grep -q "=== END SUMMARY ===" <<<"$LOG"; then
    sed -n '/=== CLOUD BUILD TEST SUMMARY ===/,/=== END SUMMARY ===/p' <<<"$LOG"
    break
  fi
  sleep 10
done
[ "$STATUS" = "SUCCESS" ]
