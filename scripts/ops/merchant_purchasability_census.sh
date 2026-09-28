#!/usr/bin/env bash
# The per-host merchant purchasability coverage census, as a CPU-GATED, READ-ONLY one-off job.
#
#   scripts/ops/merchant_purchasability_census.sh <backend-image-tag> [out-dir]
#
# <backend-image-tag> is a commit whose backend image carries scripts/merchant_purchasability_census.py
# (any main sha at or after the PR that added it; prod `web`'s current tag is the usual choice:
#   gcloud run services describe web --region us-west1 --project pivota-prod \
#     --format='value(spec.template.spec.containers[0].image)'
# ). What the census reports, and why each state means what it does, is in that script's header.
#
# THREE STEPS, each refusing rather than guessing:
#   1. CPU GATE. pivota-pg is a 2-vCPU primary serving live traffic, and on 2026-09-26/27 one-off
#      measurement jobs pushed it to 54-90 % CPU with statement timeouts on serving paths. This
#      reads cloudsql.googleapis.com/database/cpu/utilization for the last 10 minutes (60 s max-
#      aligned) and REFUSES if any point is at or above MAX_CPU (default 0.35), or if there are no
#      points at all (an unreadable gate is not an open one).
#   2. THE JOB, via scripts/ops/run_oneoff_job.sh on the default subnet (it reads the database
#      only; it fetches no merchant). ONE pooled connection (DB_POOL_MAX_SIZE=1), statement
#      timeout 30 s. The script itself opens ONE `READ ONLY` transaction and refuses unless the
#      server confirms it; see its header.
#   3. DECODE. The job's stdout is fenced and numbered; Cloud Logging drops lines, so the report is
#      re-read from Cloud Logging and `decode` refuses a log missing any row or the summary. Re-run
#      step 3 alone with:  python3 scripts/merchant_purchasability_census.py decode --log <out>/job.log
#
# Exit: 0 = report written; 2 = refused by the CPU gate (nothing ran); 3 = a complete report was
# decoded and written but the job exited non-zero — the census exits 3 when a population lane was
# unreadable or incomplete, so the report under-counts (its summary's population_counts says
# which); anything else = the job never produced a complete report.
set -euo pipefail

TAG="${1:-}"
[ -n "$TAG" ] || { echo "usage: $0 <backend-image-tag> [out-dir]" >&2; exit 2; }
OUT="${2:-$(mktemp -d "${TMPDIR:-/tmp}/mp-census.XXXXXX")}"
mkdir -p "$OUT"
PROJECT="${PROJECT:-pivota-prod}"
DATABASE_ID="${DATABASE_ID:-pivota-prod:pivota-pg}"
MAX_CPU="${MAX_CPU:-0.35}"
HERE="$(cd "$(dirname "$0")/../.." && pwd)"

# ── 1. the CPU gate ────────────────────────────────────────────────────────────────────────
END="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
START="$(python3 -c 'import datetime as d; print((d.datetime.now(d.timezone.utc)-d.timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ"))')"
curl -sf -G -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  "https://monitoring.googleapis.com/v3/projects/$PROJECT/timeSeries" \
  --data-urlencode "filter=metric.type=\"cloudsql.googleapis.com/database/cpu/utilization\" AND resource.labels.database_id=\"$DATABASE_ID\"" \
  --data-urlencode "interval.startTime=$START" --data-urlencode "interval.endTime=$END" \
  --data-urlencode "aggregation.alignmentPeriod=60s" \
  --data-urlencode "aggregation.perSeriesAligner=ALIGN_MAX" > "$OUT/cpu.json"
if ! python3 - "$OUT/cpu.json" "$MAX_CPU" <<'PY'
import json, sys
points = [p["value"]["doubleValue"] for ts in json.load(open(sys.argv[1])).get("timeSeries", [])
          for p in ts.get("points", [])]
limit = float(sys.argv[2])
if not points:
    print("CPU gate: no CPU points for the last 10 minutes; refusing (an unreadable gate is not open)")
    sys.exit(1)
peak = max(points)
print(f"CPU gate: pivota-pg peak {peak:.1%} over the last 10 minutes ({len(points)} points), limit {limit:.0%}")
sys.exit(0 if peak < limit else 1)
PY
then
  echo "==> refused by the CPU gate; nothing ran. Try again when pivota-pg is quieter." >&2
  exit 2
fi

# ── 2. the job ─────────────────────────────────────────────────────────────────────────────
RC=0
IMAGE="us-west1-docker.pkg.dev/pivota-shared/pivota/backend:$TAG" \
ENV_VARS="PIVOTA_ENV=production,DB_POOL_MIN_SIZE=1,DB_POOL_MAX_SIZE=1,DB_STATEMENT_TIMEOUT_SECONDS=30,DB_COMMAND_TIMEOUT_SECONDS=600" \
TASK_TIMEOUT=900s JOB_PREFIX=mp-census \
  bash "$HERE/scripts/ops/run_oneoff_job.sh" scripts/merchant_purchasability_census.py \
  > "$OUT/run.log" 2> "$OUT/run.err" || RC=$?
cat "$OUT/run.err" >&2
JOB="$(sed -n 's/^==> job \([^ ]*\) .*/\1/p' "$OUT/run.err" | head -1)"
[ -n "$JOB" ] || { echo "==> could not find the job name; see $OUT/run.err" >&2; exit 1; }

# ── 3. decode, from Cloud Logging (the runner's own read is detail only) ────────────────────
sleep "${CENSUS_LOG_WAIT_S:-30}"
gcloud logging read "resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"$JOB\" AND textPayload:\"MPCENSUS\"" \
  --project "$PROJECT" --freshness 2h --limit 5000 --format 'value(textPayload)' > "$OUT/job.log"
python3 "$HERE/scripts/merchant_purchasability_census.py" decode --log "$OUT/job.log" --json "$OUT/census.json" \
  | tee "$OUT/census.txt"
echo "==> report: $OUT/census.txt  (json: $OUT/census.json)  job exit: $RC" >&2
[ "$RC" = 0 ] || exit 3
