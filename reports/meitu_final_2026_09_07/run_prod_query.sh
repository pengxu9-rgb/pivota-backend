#!/usr/bin/env bash
# Wrapper: base64 a local read-only program and run it in prod via the oneoff job.
# Usage: run_prod_query.sh <local_program.py>
set -euo pipefail
WT=/Users/pengchydan/dev/pivota-backend-quality-gate
PROG="$1"
B64="$(base64 < "$PROG" | tr -d '\n')"
exec "$WT/scripts/ops/run_oneoff_job.sh" -c "import base64;exec(base64.b64decode('$B64'))"
