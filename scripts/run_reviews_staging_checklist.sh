#!/bin/bash
set -euo pipefail

# Convenience wrapper so you can run from `pivota-backend/`.
exec /bin/bash "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/scripts/run_reviews_staging_checklist.sh" "$@"

