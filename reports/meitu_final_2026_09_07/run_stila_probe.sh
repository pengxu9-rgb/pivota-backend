#!/usr/bin/env bash
# Run reports/meitu_final_2026_09_07/stila_probe.py inside PROD as an inline base64 program.
# READ-ONLY: the program issues SELECTs only.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
MAIN=/Users/pengchydan/dev/pivota-backend-quality-gate
SRC="${1:-$HERE/stila_probe.py}"
B64="$(python3 -c "import base64,sys;sys.stdout.write(base64.b64encode(open(sys.argv[1],'rb').read()).decode())" "$SRC")"
PROG="exec(__import__('base64').b64decode('$B64'))"
TASK_TIMEOUT=900s bash "$MAIN/scripts/ops/run_oneoff_job.sh" -c "$PROG"
