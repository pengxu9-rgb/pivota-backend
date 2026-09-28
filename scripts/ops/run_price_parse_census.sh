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
#   PROBE=1 bash scripts/ops/run_price_parse_census.sh    # census + currency probe, one crawl job
#
# PROBE=1 runs scripts/ops/external_seed_currency_probe.py --from-db: the census's read-only query,
# then a re-read of its per-host URL sample with the production extractor. It FETCHES FROM
# MERCHANTS, so it always runs on the crawl subnet (SUBNET=pivota-crawl, the pivota-crawl-nat
# address, never the payment NAT) and from the file baked into the image (the extractor it
# measures). Its no-start window is wider, 04:15-06:15Z: it must finish before the refresh, which
# leaves by the same NAT. The probe refuses the same window itself. Pin the image to the merged
# commit with IMAGE=us-west1-docker.pkg.dev/pivota-shared/pivota/backend:<sha>.
set -euo pipefail

PROJECT="${PROJECT:-pivota-prod}"
DB_ID="${DB_ID:-pivota-prod:pivota-pg}"
CPU_MAX="${CPU_MAX:-0.35}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CENSUS="$HERE/external_seed_price_parse_census.py"

hhmm=$((10#$(date -u +%H%M)))
window_start=515
[ "${PROBE:-0}" = 1 ] && window_start=415
if [ "$hhmm" -ge "$window_start" ] && [ "$hhmm" -lt 615 ]; then
  echo "refusing: $(date -u +%H:%MZ) is inside the $(printf '%04d' "$window_start" | sed 's/../&:/')-06:15Z no-start window (05:15-06:15Z refresh)" >&2
  exit 2
fi

start="$(date -u -v-5M +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '5 minutes ago' +%Y-%m-%dT%H:%M:%SZ)"
end="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
# Each read refuses LOUDLY on failure: under `set -e` a failed `$(...)` exits with no output,
# which on 2026-09-28 looked exactly like "the census ran and printed nothing".
token="$(gcloud auth print-access-token)" || { echo "refusing: no gcloud access token (gcloud auth login)" >&2; exit 2; }
series="$(curl -sf -G -H "Authorization: Bearer $token" \
  "https://monitoring.googleapis.com/v3/projects/$PROJECT/timeSeries" \
  --data-urlencode "filter=metric.type=\"cloudsql.googleapis.com/database/cpu/utilization\" AND resource.labels.database_id=\"$DB_ID\"" \
  --data-urlencode "interval.startTime=$start" \
  --data-urlencode "interval.endTime=$end")" || { echo "refusing: could not read pivota-pg CPU from Cloud Monitoring" >&2; exit 2; }

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

if [ "${PROBE:-0}" = 1 ]; then
  echo "probe: census + currency probe on the crawl subnet, image ${IMAGE:-backend:latest}" >&2
  SUBNET=pivota-crawl TASK_TIMEOUT="${TASK_TIMEOUT:-3600s}" \
    exec bash "$HERE/run_oneoff_job.sh" scripts/ops/external_seed_currency_probe.py --from-db
fi
if [ "${FROM_IMAGE:-0}" = 1 ]; then
  exec bash "$HERE/run_oneoff_job.sh" scripts/ops/external_seed_price_parse_census.py
fi
exec bash "$HERE/run_oneoff_job.sh" -c "$(cat "$CENSUS")"
