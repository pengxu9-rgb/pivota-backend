#!/usr/bin/env bash

set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
SUMMARY_QUERY_SUFFIX="summary_only=true&sample_limit=25"

BASE_URL=""
MERCHANT_ID="${READINESS_ALPHA_MERCHANT_ID:-}"
INTERNAL_KEY="${READINESS_INTERNAL_API_KEY:-${READINESS_KEY:-}}"
RUN_ID="${RUN_ID:-$(date -u +"%Y%m%dT%H%M%SZ")}"
OUT_DIR=""
CANARY_WRITE=0
READY_VARIANT_ID=""
BLOCKED_VARIANT_ID=""
CREATE_PAYMENT_INTENT=0
PAYMENT_STATUS_SYNC=0
PAYMENT_REFERENCE="${PAYMENT_REFERENCE:-}"
PAYMENT_PSP="${PAYMENT_PSP:-stripe}"
PAYMENT_BRIDGE_SOURCE="${PAYMENT_BRIDGE_SOURCE:-operator_canary_bridge}"
PAYMENT_INTENT_PREFERRED_PSPS="${PAYMENT_INTENT_PREFERRED_PSPS:-}"
PAYMENT_INTENT_PSP_MODE="${PAYMENT_INTENT_PSP_MODE:-}"
PAYMENT_INTENT_TEST_PSP_PROBE="${PAYMENT_INTENT_TEST_PSP_PROBE:-0}"
RUN_REFUND=0
RUN_RETURN_ELIGIBILITY=0
RUN_RETURN_SYNC=0
REFUND_AMOUNT="${REFUND_AMOUNT:-}"
REFUND_REASON="${REFUND_REASON:-readiness_alpha_refund}"
BUYER_EMAIL="${BUYER_EMAIL:-ops-canary@example.com}"
CUSTOMER_NAME="${CUSTOMER_NAME:-Pivota Canary}"
ADDRESS_NAME="${ADDRESS_NAME:-Pivota Canary}"
ADDRESS_LINE1="${ADDRESS_LINE1:-1 Market St}"
ADDRESS_LINE2="${ADDRESS_LINE2:-}"
CITY="${CITY:-San Francisco}"
STATE="${STATE:-CA}"
POSTAL_CODE="${POSTAL_CODE:-94105}"
COUNTRY="${COUNTRY:-US}"
PHONE="${PHONE:-}"

usage() {
  cat <<EOF
Usage:
  $SCRIPT_NAME --base-url https://prod.example.com --internal-key <key> [options]

Safe default:
  - runs read-only readiness report/export
  - runs a fail-closed blocked checkout check if a blocked variant exists
  - does not create merchant orders unless --canary-write is passed

Options:
  --base-url URL               Required. Production or staging base URL.
  --internal-key KEY           Internal readiness API key. Falls back to READINESS_INTERNAL_API_KEY or READINESS_KEY.
  --merchant-id ID             Merchant ID. Required unless READINESS_ALPHA_MERCHANT_ID is set.
  --out-dir DIR                Output directory. Default: /tmp/pivota-readiness-smoke-\$RUN_ID
  --run-id ID                  Run identifier. Default: current UTC timestamp
  --ready-variant-id ID        Override the ready variant selected from the report.
  --blocked-variant-id ID      Override the blocked variant selected from the report.
  --canary-write               Opt in to one live checkout + order-sync canary.
  --create-payment-intent      After canary order-sync, mint a readiness-owned PSP payment intent.
  --payment-status-sync        After canary order-sync, poll PSP status for the readiness payment intent.
  --refund                     After canary write, attempt a readiness refund.
  --return-eligibility        After canary write, run a read-only Shopify return eligibility probe.
  --return-sync                After canary write, trigger readiness return sync and refresh the audit.
  --refund-amount VALUE        Optional. Partial refund amount. Default: full remaining refundable amount.
  --refund-reason VALUE        Optional. Refund reason. Default: $REFUND_REASON
  --payment-reference REF      Optional. Attach an external paid payment reference after canary write.
  --payment-psp PSP            Optional. PSP provider for the payment bridge. Default: $PAYMENT_PSP
  --payment-bridge-source SRC  Optional. Source label for the payment bridge event. Default: $PAYMENT_BRIDGE_SOURCE
  --payment-intent-preferred-psps CSV
                               Optional. Comma-separated PSP preference order for payment-intent creation.
  --payment-intent-psp-mode VALUE
                               Optional. Pass through psp_mode for payment-intent creation, e.g. stripe_checkout.
  --payment-intent-test-psp-probe
                               Explicitly mark payment-intent creation as an allowlisted test PSP probe.
  --buyer-email EMAIL          Canary checkout buyer email. Default: $BUYER_EMAIL
  --customer-name NAME         Canary checkout customer name. Default: $CUSTOMER_NAME
  --address-name NAME          Canary shipping recipient. Default: $ADDRESS_NAME
  --address-line1 VALUE        Canary shipping address line 1. Default: $ADDRESS_LINE1
  --address-line2 VALUE        Canary shipping address line 2.
  --city VALUE                 Canary shipping city. Default: $CITY
  --state VALUE                Canary shipping state/province. Default: $STATE
  --postal-code VALUE          Canary shipping postal code. Default: $POSTAL_CODE
  --country VALUE              Canary shipping country code. Default: $COUNTRY
  --phone VALUE                Canary shipping phone.
  --help                       Show this help.

Examples:
  $SCRIPT_NAME --base-url https://prod.example.com --internal-key "\$READINESS_INTERNAL_API_KEY"
  $SCRIPT_NAME --base-url https://prod.example.com --internal-key "\$READINESS_INTERNAL_API_KEY" --canary-write
  $SCRIPT_NAME --base-url https://prod.example.com --internal-key "\$READINESS_INTERNAL_API_KEY" --canary-write --payment-reference pi_live_123
  $SCRIPT_NAME --base-url https://prod.example.com --internal-key "\$READINESS_INTERNAL_API_KEY" --canary-write --create-payment-intent --payment-status-sync
EOF
}

info() {
  printf '[info] %s\n' "$*"
}

warn() {
  printf '[warn] %s\n' "$*" >&2
}

die() {
  printf '[error] %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --base-url)
        BASE_URL="${2:-}"
        shift 2
        ;;
      --internal-key)
        INTERNAL_KEY="${2:-}"
        shift 2
        ;;
      --merchant-id)
        MERCHANT_ID="${2:-}"
        shift 2
        ;;
      --out-dir)
        OUT_DIR="${2:-}"
        shift 2
        ;;
      --run-id)
        RUN_ID="${2:-}"
        shift 2
        ;;
      --ready-variant-id)
        READY_VARIANT_ID="${2:-}"
        shift 2
        ;;
      --blocked-variant-id)
        BLOCKED_VARIANT_ID="${2:-}"
        shift 2
        ;;
      --canary-write)
        CANARY_WRITE=1
        shift
        ;;
      --create-payment-intent)
        CREATE_PAYMENT_INTENT=1
        shift
        ;;
      --payment-status-sync)
        PAYMENT_STATUS_SYNC=1
        shift
        ;;
      --refund)
        RUN_REFUND=1
        shift
        ;;
      --return-eligibility)
        RUN_RETURN_ELIGIBILITY=1
        shift
        ;;
      --return-sync)
        RUN_RETURN_SYNC=1
        shift
        ;;
      --refund-amount)
        REFUND_AMOUNT="${2:-}"
        shift 2
        ;;
      --refund-reason)
        REFUND_REASON="${2:-}"
        shift 2
        ;;
      --payment-reference)
        PAYMENT_REFERENCE="${2:-}"
        shift 2
        ;;
      --payment-psp)
        PAYMENT_PSP="${2:-}"
        shift 2
        ;;
      --payment-bridge-source)
        PAYMENT_BRIDGE_SOURCE="${2:-}"
        shift 2
        ;;
      --payment-intent-preferred-psps)
        PAYMENT_INTENT_PREFERRED_PSPS="${2:-}"
        shift 2
        ;;
      --payment-intent-psp-mode)
        PAYMENT_INTENT_PSP_MODE="${2:-}"
        shift 2
        ;;
      --payment-intent-test-psp-probe)
        PAYMENT_INTENT_TEST_PSP_PROBE=1
        shift
        ;;
      --buyer-email)
        BUYER_EMAIL="${2:-}"
        shift 2
        ;;
      --customer-name)
        CUSTOMER_NAME="${2:-}"
        shift 2
        ;;
      --address-name)
        ADDRESS_NAME="${2:-}"
        shift 2
        ;;
      --address-line1)
        ADDRESS_LINE1="${2:-}"
        shift 2
        ;;
      --address-line2)
        ADDRESS_LINE2="${2:-}"
        shift 2
        ;;
      --city)
        CITY="${2:-}"
        shift 2
        ;;
      --state)
        STATE="${2:-}"
        shift 2
        ;;
      --postal-code)
        POSTAL_CODE="${2:-}"
        shift 2
        ;;
      --country)
        COUNTRY="${2:-}"
        shift 2
        ;;
      --phone)
        PHONE="${2:-}"
        shift 2
        ;;
      --help|-h)
        usage
        exit 0
        ;;
      *)
        die "Unknown argument: $1"
        ;;
    esac
  done
}

request_json() {
  local method="$1"
  local url="$2"
  local outfile="$3"
  local data_file="${4:-}"
  local status

  if [[ -n "$data_file" ]]; then
    status="$(
      curl -sS -o "$outfile" -w '%{http_code}' \
        -X "$method" \
        -H 'Content-Type: application/json' \
        -H "X-Pivota-Internal-Key: $INTERNAL_KEY" \
        --data @"$data_file" \
        "$url"
    )"
  else
    status="$(
      curl -sS -o "$outfile" -w '%{http_code}' \
        -X "$method" \
        -H "X-Pivota-Internal-Key: $INTERNAL_KEY" \
        "$url"
    )"
  fi

  printf '%s' "$status"
}

expect_status() {
  local actual="$1"
  local expected="$2"
  local context="$3"
  local body_file="${4:-}"
  if [[ "$actual" != "$expected" ]]; then
    if [[ -n "$body_file" && -f "$body_file" ]]; then
      warn "$context response body:"
      cat "$body_file" >&2 || true
    fi
    die "$context returned HTTP $actual, expected $expected"
  fi
}

pretty_json() {
  local file="$1"
  jq . "$file"
}

maybe_run_db_query() {
  local label="$1"
  local sql="$2"
  local outfile="$3"

  if [[ -z "${DATABASE_URL:-}" ]]; then
    return 0
  fi
  if ! command -v psql >/dev/null 2>&1; then
    warn "Skipping DB watch for $label because psql is unavailable"
    return 0
  fi

  info "DB watch: $label"
  if ! psql "$DATABASE_URL" -X -q -P pager=off -c "$sql" | tee "$outfile"; then
    warn "DB watch failed for $label"
  fi
}

validate_report_contract() {
  local report_file="$1"
  jq -e \
    --arg merchant_id "$MERCHANT_ID" \
    '
      .merchant_id == $merchant_id and
      .merchant_alpha_mode == "real_merchant_alpha" and
      (
        .source_of_truth | keys
      ) as $keys
      | ["catalog","checkout_capability","fulfillment_policy","inventory","order_status","price","reviews_confidence"]
      | all(. as $required | ($keys | index($required) != null))
    ' \
    "$report_file" >/dev/null || die "Readiness report contract check failed"
}

validate_export_contract() {
  local export_file="$1"
  jq -e \
    --arg merchant_id "$MERCHANT_ID" \
    '
      .merchant_id == $merchant_id and
      .merchant_alpha_mode == "real_merchant_alpha" and
      (
        .source_of_truth | keys
      ) as $keys
      | ["catalog","checkout_capability","fulfillment_policy","inventory","order_status","price","reviews_confidence"]
      | all(. as $required | ($keys | index($required) != null))
    ' \
    "$export_file" >/dev/null || die "UCP export contract check failed"
}

build_canary_payload() {
  local variant_id="$1"
  local outfile="$2"

  jq -n \
    --arg variant_id "$variant_id" \
    --arg idempotency_key "$RUN_ID-canary" \
    --arg buyer_email "$BUYER_EMAIL" \
    --arg customer_name "$CUSTOMER_NAME" \
    --arg address_name "$ADDRESS_NAME" \
    --arg address_line1 "$ADDRESS_LINE1" \
    --arg address_line2 "$ADDRESS_LINE2" \
    --arg city "$CITY" \
    --arg state "$STATE" \
    --arg postal_code "$POSTAL_CODE" \
    --arg country "$COUNTRY" \
    --arg phone "$PHONE" \
    '
      {
        variant_id: $variant_id,
        quantity: 1,
        idempotency_key: $idempotency_key,
        buyer_email: $buyer_email,
        customer_name: $customer_name,
        shipping_address: {
          name: $address_name,
          address_line1: $address_line1,
          city: $city,
          postal_code: $postal_code,
          country: $country
        }
      }
      | if $address_line2 != "" then .shipping_address.address_line2 = $address_line2 else . end
      | if $state != "" then .shipping_address.state = $state else . end
      | if $phone != "" then .shipping_address.phone = $phone else . end
    ' >"$outfile"
}

main() {
  parse_args "$@"

  require_command curl
  require_command jq

  [[ -n "$BASE_URL" ]] || die "--base-url is required"
  [[ -n "$INTERNAL_KEY" ]] || die "--internal-key is required or set READINESS_INTERNAL_API_KEY"
  [[ -n "$MERCHANT_ID" ]] || die "--merchant-id is required or set READINESS_ALPHA_MERCHANT_ID"

  BASE_URL="${BASE_URL%/}"
  OUT_DIR="${OUT_DIR:-/tmp/pivota-readiness-smoke-$RUN_ID}"
  mkdir -p "$OUT_DIR"

  local report_json="$OUT_DIR/report.json"
  local export_json="$OUT_DIR/export_ucp.json"
  local blocked_checkout_json="$OUT_DIR/blocked_checkout.json"
  local checkout_payload_json="$OUT_DIR/checkout_payload.json"
  local checkout_json="$OUT_DIR/checkout.json"
  local checkout_session_before_json="$OUT_DIR/checkout_session_before_sync.json"
  local order_sync_json="$OUT_DIR/order_sync.json"
  local checkout_session_after_json="$OUT_DIR/checkout_session_after_sync.json"
  local order_sync_audit_json="$OUT_DIR/order_sync_audit.json"
  local payment_intent_payload_json="$OUT_DIR/payment_intent_payload.json"
  local payment_intent_json="$OUT_DIR/payment_intent.json"
  local payment_status_sync_payload_json="$OUT_DIR/payment_status_sync_payload.json"
  local payment_status_sync_json="$OUT_DIR/payment_status_sync.json"
  local payment_bridge_payload_json="$OUT_DIR/payment_bridge_payload.json"
  local payment_bridge_json="$OUT_DIR/payment_bridge.json"
  local order_sync_audit_after_status_sync_json="$OUT_DIR/order_sync_audit_after_status_sync.json"
  local order_sync_audit_after_payment_json="$OUT_DIR/order_sync_audit_after_payment.json"
  local refund_payload_json="$OUT_DIR/refund_payload.json"
  local refund_json="$OUT_DIR/refund.json"
  local order_sync_audit_after_refund_json="$OUT_DIR/order_sync_audit_after_refund.json"
  local return_eligibility_json="$OUT_DIR/return_eligibility.json"
  local return_sync_payload_json="$OUT_DIR/return_sync_payload.json"
  local return_sync_json="$OUT_DIR/return_sync.json"
  local order_sync_replay_json="$OUT_DIR/order_sync_replay.json"

  info "Run ID: $RUN_ID"
  info "Output directory: $OUT_DIR"
  info "Merchant: $MERCHANT_ID"
  info "Canary write enabled: $CANARY_WRITE"
  info "Create payment intent: $CREATE_PAYMENT_INTENT"
  info "Payment intent test PSP probe: $PAYMENT_INTENT_TEST_PSP_PROBE"
  info "Payment status sync: $PAYMENT_STATUS_SYNC"
  info "Run refund: $RUN_REFUND"
  info "Run return eligibility: $RUN_RETURN_ELIGIBILITY"
  info "Run return sync: $RUN_RETURN_SYNC"

  maybe_run_db_query \
    "merchant_onboarding preflight" \
    "SELECT merchant_id,status,business_name,mcp_platform,mcp_connected,psp_connected FROM merchant_onboarding WHERE merchant_id='$MERCHANT_ID';" \
    "$OUT_DIR/db_merchant_onboarding_preflight.txt"
  maybe_run_db_query \
    "merchant_psps preflight" \
    "SELECT provider,psp_id,status,connected_at FROM merchant_psps WHERE merchant_id='$MERCHANT_ID' ORDER BY connected_at DESC;" \
    "$OUT_DIR/db_merchant_psps_preflight.txt"
  maybe_run_db_query \
    "products_cache preflight" \
    "SELECT COUNT(*) AS products_cache_rows FROM products_cache WHERE merchant_id='$MERCHANT_ID' AND platform='shopify';" \
    "$OUT_DIR/db_products_cache_preflight.txt"

  info "Step 1/4: readiness report"
  local report_status
  report_status="$(request_json GET "$BASE_URL/internal/readiness/merchants/$MERCHANT_ID/report?channel=ucp&$SUMMARY_QUERY_SUFFIX" "$report_json")"
  expect_status "$report_status" "200" "Readiness report" "$report_json"
  validate_report_contract "$report_json"
  jq '{merchant_id,merchant_alpha_mode,response_mode,readiness_score,capability_status,blockers,warnings,source_of_truth,summary}' "$report_json"

  info "Step 2/4: UCP export"
  local export_status
  export_status="$(request_json GET "$BASE_URL/internal/readiness/merchants/$MERCHANT_ID/exports/ucp?$SUMMARY_QUERY_SUFFIX" "$export_json")"
  expect_status "$export_status" "200" "UCP export" "$export_json"
  validate_export_contract "$export_json"
  jq '{merchant_id,merchant_alpha_mode,response_mode,readiness_score,capability_status,blockers,warnings,validation_warnings,source_of_truth,summary}' "$export_json"

  local ready_variant_count
  ready_variant_count="$(jq '.summary.ready_variant_count // 0' "$report_json")"
  if [[ "$ready_variant_count" -gt 0 ]]; then
    jq -e '.summary.offer_count > 0' "$export_json" >/dev/null || die "Export returned zero offers even though the report has ready variants"
  fi

  if [[ -z "$READY_VARIANT_ID" ]]; then
    READY_VARIANT_ID="$(jq -r '(.summary.ready_variant_ids_sample[0] // "")' "$report_json")"
  fi
  if [[ -z "$BLOCKED_VARIANT_ID" ]]; then
    BLOCKED_VARIANT_ID="$(jq -r '(.summary.blocked_variant_ids_sample[0] // "")' "$report_json")"
  fi

  local checkout_capability
  checkout_capability="$(jq -r '.capability_status.checkout // ""' "$report_json")"
  info "Selected ready variant: ${READY_VARIANT_ID:-<none>}"
  info "Selected blocked variant: ${BLOCKED_VARIANT_ID:-<none>}"
  info "Checkout capability: ${checkout_capability:-<unknown>}"

  info "Step 3/4: fail-closed blocked checkout"
  if [[ -n "$BLOCKED_VARIANT_ID" ]]; then
    local blocked_payload_json="$OUT_DIR/blocked_checkout_payload.json"
    jq -n \
      --arg variant_id "$BLOCKED_VARIANT_ID" \
      --arg idempotency_key "$RUN_ID-blocked" \
      '{variant_id: $variant_id, quantity: 1, idempotency_key: $idempotency_key}' >"$blocked_payload_json"
    local blocked_status
    blocked_status="$(request_json POST "$BASE_URL/internal/readiness/merchants/$MERCHANT_ID/checkout" "$blocked_checkout_json" "$blocked_payload_json")"
    expect_status "$blocked_status" "409" "Blocked checkout" "$blocked_checkout_json"
    jq -e '(.error.code // "") == "VARIANT_NOT_READY_FOR_CHECKOUT"' "$blocked_checkout_json" >/dev/null || die "Blocked checkout did not return top-level VARIANT_NOT_READY_FOR_CHECKOUT"
    pretty_json "$blocked_checkout_json"
  else
    warn "No blocked variant found in the report; skipping blocked checkout smoke"
  fi

  if [[ "$CANARY_WRITE" -ne 1 ]]; then
    info "Read-only smoke complete. Canary write was not requested."
    info "Artifacts saved under $OUT_DIR"
    exit 0
  fi

  info "Step 4/4: supervised canary write"
  [[ "$checkout_capability" == "ready" ]] || die "Checkout capability is '$checkout_capability', not 'ready'. Refusing live canary write."
  [[ -n "$READY_VARIANT_ID" ]] || die "No ready variant found in the report. Refusing live canary write."

  build_canary_payload "$READY_VARIANT_ID" "$checkout_payload_json"

  local checkout_status
  checkout_status="$(request_json POST "$BASE_URL/internal/readiness/merchants/$MERCHANT_ID/checkout" "$checkout_json" "$checkout_payload_json")"
  expect_status "$checkout_status" "200" "Canary checkout creation" "$checkout_json"
  jq '{checkout_id,status,payment_mode,merchant_alpha_mode,capability_status,blockers,warnings}' "$checkout_json"

  local checkout_id
  checkout_id="$(jq -r '.checkout_id // ""' "$checkout_json")"
  [[ -n "$checkout_id" ]] || die "Checkout response did not include checkout_id"

  local session_before_status
  session_before_status="$(request_json GET "$BASE_URL/internal/readiness/checkout-sessions/$checkout_id" "$checkout_session_before_json")"
  expect_status "$session_before_status" "200" "Checkout session fetch before sync" "$checkout_session_before_json"
  jq '{checkout:.checkout|{checkout_id,status,order_id,payment_mode},event_types:[.events[].event_type]}' "$checkout_session_before_json"

  local order_sync_payload="$OUT_DIR/order_sync_payload.json"
  printf '%s\n' '{"replay": false}' >"$order_sync_payload"
  local order_sync_status
  order_sync_status="$(request_json POST "$BASE_URL/internal/readiness/merchants/$MERCHANT_ID/order-sync/$checkout_id" "$order_sync_json" "$order_sync_payload")"
  expect_status "$order_sync_status" "200" "Order sync" "$order_sync_json"
  jq '{checkout_id,status,order_id,replayed,event_types:[.events[].event_type]}' "$order_sync_json"

  jq -e '.status == "state_synced"' "$order_sync_json" >/dev/null || die "Order sync did not reach state_synced"
  jq -e '.events | map(.event_type) | index("order_created") != null' "$order_sync_json" >/dev/null || die "Order sync did not emit order_created"
  jq -e '.events | map(.event_type) | index("order_forwarded_to_merchant") != null' "$order_sync_json" >/dev/null || die "Order sync did not emit order_forwarded_to_merchant"
  jq -e '.events | map(.event_type) | index("state_synced") != null' "$order_sync_json" >/dev/null || die "Order sync did not emit state_synced"

  local order_id
  order_id="$(jq -r '.order_id // ""' "$order_sync_json")"
  [[ -n "$order_id" ]] || die "Order sync response did not include order_id"

  local session_after_status
  session_after_status="$(request_json GET "$BASE_URL/internal/readiness/checkout-sessions/$checkout_id" "$checkout_session_after_json")"
  expect_status "$session_after_status" "200" "Checkout session fetch after sync" "$checkout_session_after_json"
  jq '{checkout:.checkout|{checkout_id,status,order_id,payment_mode,updated_at},event_types:[.events[].event_type]}' "$checkout_session_after_json"

  local audit_status
  audit_status="$(request_json GET "$BASE_URL/internal/readiness/merchants/$MERCHANT_ID/order-sync-audit/$checkout_id?sample_limit=10" "$order_sync_audit_json")"
  expect_status "$audit_status" "200" "Order sync audit" "$order_sync_audit_json"
  jq '{checkout_id,order_id,shopify_order_id,checkout_status,order_state,sync_signals,warnings,recommendations}' "$order_sync_audit_json"
  jq -e '.sync_signals.merchant_writeback.status == "ready"' "$order_sync_audit_json" >/dev/null || die "Order sync audit did not confirm merchant_writeback=ready"

  if [[ "$CREATE_PAYMENT_INTENT" -eq 1 ]]; then
    info "Optional payment-intent: creating readiness-owned PSP intent"
    jq -n \
      --arg preferred_psps_csv "$PAYMENT_INTENT_PREFERRED_PSPS" \
      --arg psp_mode "$PAYMENT_INTENT_PSP_MODE" \
      --argjson test_psp_probe "$PAYMENT_INTENT_TEST_PSP_PROBE" \
      '
        {
          preferred_psps: (
            if $preferred_psps_csv == "" then null
            else ($preferred_psps_csv | split(",") | map(gsub("^\\s+|\\s+$"; "")) | map(select(length > 0)))
            end
          ),
          psp_mode: (if $psp_mode == "" then null else $psp_mode end),
          test_psp_probe: ($test_psp_probe == 1)
        }
      ' >"$payment_intent_payload_json"

    local payment_intent_status
    payment_intent_status="$(request_json POST "$BASE_URL/internal/readiness/merchants/$MERCHANT_ID/checkout-sessions/$checkout_id/payment-intent" "$payment_intent_json" "$payment_intent_payload_json")"
    expect_status "$payment_intent_status" "200" "Payment intent creation" "$payment_intent_json"
    jq '{
      checkout_id,
      order_id,
      status,
      payment_status,
      payment_intent_id,
      psp_used,
      payment_intent_status,
      bridged_to_paid,
      replayed,
      payment_action: (
        (.payment_action // {})
        | with_entries(
            if (.key | ascii_downcase | test("secret|token|url"))
            then .value = "[REDACTED]"
            else .
            end
          )
      )
    }' "$payment_intent_json"
    jq -e '(.payment_intent_id // "") != ""' "$payment_intent_json" >/dev/null || die "Payment intent response did not include payment_intent_id"
  fi

  if [[ "$PAYMENT_STATUS_SYNC" -eq 1 ]]; then
    info "Optional payment-status-sync: polling PSP state for readiness payment intent"
    jq -n \
      '{
        mark_paid_on_success: true,
        sync_shopify_transaction: true
      }' >"$payment_status_sync_payload_json"

    local payment_status_sync_status
    payment_status_sync_status="$(request_json POST "$BASE_URL/internal/readiness/merchants/$MERCHANT_ID/checkout-sessions/$checkout_id/payment-status-sync" "$payment_status_sync_json" "$payment_status_sync_payload_json")"
    expect_status "$payment_status_sync_status" "200" "Payment status sync" "$payment_status_sync_json"
    jq '{checkout_id,order_id,status,payment_status,payment_intent_id,psp_used,payment_intent_status,normalized_payment_status,bridged_to_paid,replayed}' "$payment_status_sync_json"
    jq -e '(.payment_intent_id // "") != ""' "$payment_status_sync_json" >/dev/null || die "Payment status sync did not return a payment_intent_id"
    jq -e '(.normalized_payment_status // "") != ""' "$payment_status_sync_json" >/dev/null || die "Payment status sync did not return normalized_payment_status"

    local audit_after_status_sync_status
    audit_after_status_sync_status="$(request_json GET "$BASE_URL/internal/readiness/merchants/$MERCHANT_ID/order-sync-audit/$checkout_id?sample_limit=10" "$order_sync_audit_after_status_sync_json")"
    expect_status "$audit_after_status_sync_status" "200" "Order sync audit after payment status sync" "$order_sync_audit_after_status_sync_json"
    jq '{checkout_id,order_id,checkout_status,order_state,sync_signals,warnings,recommendations}' "$order_sync_audit_after_status_sync_json"
  fi

  if [[ -n "$PAYMENT_REFERENCE" ]]; then
    info "Optional payment bridge: attaching external payment reference"
    jq -n \
      --arg payment_reference "$PAYMENT_REFERENCE" \
      --arg psp_used "$PAYMENT_PSP" \
      --arg source "$PAYMENT_BRIDGE_SOURCE" \
      '{
        payment_reference: $payment_reference,
        psp_used: $psp_used,
        source: $source,
        mark_paid: true,
        sync_shopify_transaction: true
      }' >"$payment_bridge_payload_json"

    local payment_bridge_status
    payment_bridge_status="$(request_json POST "$BASE_URL/internal/readiness/merchants/$MERCHANT_ID/checkout-sessions/$checkout_id/payment-bridge" "$payment_bridge_json" "$payment_bridge_payload_json")"
    expect_status "$payment_bridge_status" "200" "Payment bridge" "$payment_bridge_json"
    jq '{checkout_id,order_id,status,payment_status,payment_reference,psp_used,transaction_sync,replayed,event_types:[.events[].event_type]}' "$payment_bridge_json"

    local audit_after_payment_status
    audit_after_payment_status="$(request_json GET "$BASE_URL/internal/readiness/merchants/$MERCHANT_ID/order-sync-audit/$checkout_id?sample_limit=10" "$order_sync_audit_after_payment_json")"
    expect_status "$audit_after_payment_status" "200" "Order sync audit after payment bridge" "$order_sync_audit_after_payment_json"
    jq '{checkout_id,order_id,checkout_status,order_state,sync_signals,warnings,recommendations}' "$order_sync_audit_after_payment_json"
    jq -e '.sync_signals.refund_sync.refund_eligible == true' "$order_sync_audit_after_payment_json" >/dev/null || die "Payment bridge did not make refund_sync eligible"
  fi

  if [[ "$RUN_REFUND" -eq 1 ]]; then
    info "Optional refund: invoking readiness refund route"
    jq -n \
      --arg refund_amount "$REFUND_AMOUNT" \
      --arg reason "$REFUND_REASON" \
      '{
        amount: (if $refund_amount == "" then null else ($refund_amount | tonumber) end),
        reason: $reason,
        sync_shopify_refund_transaction: true
      }' >"$refund_payload_json"

    local refund_status
    refund_status="$(request_json POST "$BASE_URL/internal/readiness/merchants/$MERCHANT_ID/checkout-sessions/$checkout_id/refund" "$refund_json" "$refund_payload_json")"
    expect_status "$refund_status" "200" "Readiness refund" "$refund_json"
    jq '{checkout_id,order_id,status,payment_status,refund_status,refund_id,psp_refund_id,amount,remaining_refundable_before,transaction_sync,replayed}' "$refund_json"

    local audit_after_refund_status
    audit_after_refund_status="$(request_json GET "$BASE_URL/internal/readiness/merchants/$MERCHANT_ID/order-sync-audit/$checkout_id?sample_limit=10" "$order_sync_audit_after_refund_json")"
    expect_status "$audit_after_refund_status" "200" "Order sync audit after refund" "$order_sync_audit_after_refund_json"
    jq '{checkout_id,order_id,checkout_status,order_state,sync_signals,warnings,recommendations}' "$order_sync_audit_after_refund_json"
  fi

  if [[ "$RUN_RETURN_ELIGIBILITY" -eq 1 ]]; then
    info "Optional return eligibility: probing Shopify-side return readiness"
    local return_eligibility_status
    return_eligibility_status="$(request_json GET "$BASE_URL/internal/readiness/merchants/$MERCHANT_ID/checkout-sessions/$checkout_id/return-eligibility?sample_limit=10" "$return_eligibility_json")"
    expect_status "$return_eligibility_status" "200" "Readiness return eligibility" "$return_eligibility_json"
    jq '{checkout_id,order_id,shopify_order_id,eligibility,return_sync_status:.sync_audit.sync_signals.return_sync.status}' "$return_eligibility_json"
  fi

  if [[ "$RUN_RETURN_SYNC" -eq 1 ]]; then
    info "Optional return sync: invoking readiness return-sync route"
    jq -n '{limit: 20, sample_limit: 10}' >"$return_sync_payload_json"

    local return_sync_status
    return_sync_status="$(request_json POST "$BASE_URL/internal/readiness/merchants/$MERCHANT_ID/checkout-sessions/$checkout_id/return-sync" "$return_sync_json" "$return_sync_payload_json")"
    expect_status "$return_sync_status" "200" "Readiness return sync" "$return_sync_json"
    jq '{checkout_id,order_id,shopify_order_id,return_sync_result,return_sync_status:.sync_audit.sync_signals.return_sync.status}' "$return_sync_json"
  fi

  maybe_run_db_query \
    "readiness checkout session" \
    "SELECT checkout_id,status,payment_mode,order_id,created_at,updated_at FROM readiness_checkout_sessions WHERE checkout_id='$checkout_id';" \
    "$OUT_DIR/db_readiness_checkout_session.txt"
  maybe_run_db_query \
    "readiness order sync events" \
    "SELECT checkout_id,event_type,created_at,event_payload FROM readiness_order_sync_events WHERE checkout_id='$checkout_id' ORDER BY created_at;" \
    "$OUT_DIR/db_readiness_order_sync_events.txt"
  maybe_run_db_query \
    "orders row" \
    "SELECT order_id,merchant_id,status,payment_status,fulfillment_status,shopify_order_id,metadata->>'checkout_id' AS checkout_id FROM orders WHERE order_id='$order_id' OR metadata->>'checkout_id'='$checkout_id';" \
    "$OUT_DIR/db_orders_row.txt"

  printf '%s\n' '{"replay": true}' >"$order_sync_payload"
  local replay_status
  replay_status="$(request_json POST "$BASE_URL/internal/readiness/merchants/$MERCHANT_ID/order-sync/$checkout_id" "$order_sync_replay_json" "$order_sync_payload")"
  expect_status "$replay_status" "200" "Order sync replay" "$order_sync_replay_json"
  jq '{checkout_id,status,order_id,replayed,event_types:[.events[].event_type]}' "$order_sync_replay_json"
  jq -e '.replayed == true' "$order_sync_replay_json" >/dev/null || die "Replay call did not report replayed=true"

  info "Production canary smoke passed."
  info "Artifacts saved under $OUT_DIR"
}

main "$@"
