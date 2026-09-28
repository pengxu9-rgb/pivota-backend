#!/usr/bin/env bash
# Launch scripts/ops/external_seed_price_parse_census.py as a one-off prod job, ONLY when pivota-pg
# can take it. Read-only; see the census docstring for what it reads.
#
# Gates, each a hard refusal (exit 2), checked immediately before launch:
#   1. not inside 05:15-06:15Z, the nightly external-referral refresh
#   2. Cloud SQL CPU (cloudsql.googleapis.com/database/cpu/utilization) for pivota-pg stayed under
#      CPU_MAX (default 0.35) at EVERY sample of the last 5 minutes, and there was at least one
#      sample (no data is a refusal, not a pass)
# The census itself refuses to run while an index build is in progress or another copy runs.
#
# Usage:
#   bash scripts/ops/run_price_parse_census.sh            # inline program (works before deploy)
#   FROM_IMAGE=1 bash scripts/ops/run_price_parse_census.sh   # the file baked into the image
set -euo pipefail

PROJECT="${PROJECT:-pivota-prod}"
DB_ID="${DB_ID:-pivota-prod:pivota-pg}"
CPU_MAX="${CPU_MAX:-0.35}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CENSUS="$HERE/external_seed_price_parse_census.py"

hhmm=$((10#$(date -u +%H%M)))
if [ "$hhmm" -ge 515 ] && [ "$hhmm" -lt 615 ]; then
  echo "refusing: $(date -u +%H:%MZ) is inside the 05:15-06:15Z refresh window" >&2
  exit 2
fi

start="$(date -u -v-5M +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '5 minutes ago' +%Y-%m-%dT%H:%M:%SZ)"
end="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
token="$(gcloud auth print-access-token)"
series="$(curl -sf -G -H "Authorization: Bearer $token" \
  "https://monitoring.googleapis.com/v3/projects/$PROJECT/timeSeries" \
  --data-urlencode "filter=metric.type=\"cloudsql.googleapis.com/database/cpu/utilization\" AND resource.labels.database_id=\"$DB_ID\"" \
  --data-urlencode "interval.startTime=$start" \
  --data-urlencode "interval.endTime=$end")"

verdict="$(printf '%s' "$series" | python3 -c '
import json, sys
cpu_max = float(sys.argv[1])
points = [p["value"]["doubleValue"] for s in json.load(sys.stdin).get("timeSeries", []) for p in s.get("points", [])]
if not points:
    print("NODATA")
else:
    peak = max(points)
    print(("OK " if peak < cpu_max else "HIGH ") + f"{peak:.3f} over {len(points)} samples")
' "$CPU_MAX")"

case "$verdict" in
  OK*) echo "pivota-pg CPU gate passed: $verdict (max allowed $CPU_MAX)" ;;
  *) echo "refusing: pivota-pg CPU gate: $verdict (max allowed $CPU_MAX, last 5 min)" >&2; exit 2 ;;
esac

if [ "${FROM_IMAGE:-0}" = 1 ]; then
  exec bash "$HERE/run_oneoff_job.sh" scripts/ops/external_seed_price_parse_census.py
fi
exec bash "$HERE/run_oneoff_job.sh" -c "$(cat "$CENSUS")"
