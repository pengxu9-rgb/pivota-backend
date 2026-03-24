#!/usr/bin/env bash

set -euo pipefail

BACKEND_REPO="${PIVOTA_BACKEND_REPO:-/Users/pengchydan/dev/Pivota-cursor-create-project-directory-structure-8344/pivota-backend}"
ACP_REPO="${PIVOTA_ACP_REPO:-/Users/pengchydan/dev/pivota-acp-revert}"
GATEWAY_REPO="${PIVOTA_AGENT_GATEWAY_REPO:-/Users/pengchydan/dev/PIVOTA-Agent}"

require_dir() {
  local path="$1"
  if [[ ! -d "$path" ]]; then
    echo "missing repo directory: $path" >&2
    exit 1
  fi
}

run_step() {
  local label="$1"
  local repo="$2"
  shift 2

  echo
  echo "== $label =="
  (
    cd "$repo"
    "$@"
  )
}

require_dir "$BACKEND_REPO"
require_dir "$ACP_REPO"
require_dir "$GATEWAY_REPO"

run_step \
  "gateway rollout canary" \
  "$GATEWAY_REPO" \
  npx jest tests/integration/checkout_rollout_canary.test.js --runInBand

run_step \
  "backend payment aftercare gate" \
  "$BACKEND_REPO" \
  bash ./scripts/run_payment_aftercare_gate.sh

run_step \
  "backend agent contract gate" \
  "$BACKEND_REPO" \
  python3 -m pytest \
    tests/test_agent_governance_contract.py \
    tests/test_runtime_interface_drift.py \
    tests/test_agent_v2_contract.py \
    tests/test_agent_rollout_contract.py \
    tests/test_merchant_risk_evidence_plan.py \
    tests/test_pcs_evidence_pack_task_actions.py \
    tests/test_pcs_evidence_pack_task_inbox.py \
    tests/test_route_uniqueness.py \
    tests/test_agent_docs_runtime.py \
    tests/contracts/test_agent_contracts.py \
    -q

run_step \
  "acp control-plane contract gate" \
  "$ACP_REPO" \
  bash ./scripts/run_control_plane_contract_gate.sh

echo
echo "agent rollout gate passed"
