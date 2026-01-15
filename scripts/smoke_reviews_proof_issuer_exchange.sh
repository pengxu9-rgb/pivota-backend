#!/bin/bash
set -euo pipefail

# Convenience wrapper so you can run from `pivota-backend/`.
exec /bin/bash "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/scripts/smoke_reviews_proof_issuer_exchange.sh" "$@"

