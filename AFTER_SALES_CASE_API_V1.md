# After‑Sales Case API v1 (Agent)

目标：用一套“售后工单（case）”模型统一跑通：
- 无退货退款（full / partial）
- 有退货退款（含面单；目前 `label_url` 占位）

## Why cases (vs. direct refund/cancel)
- 售后是“用户诉求/流程” + “资金动作”的组合；直接 `refund` 很难覆盖退货、部分退、审计与幂等。
- 对外（agentic tools）只需对接一次：创建 case → 走 next_action。

## Endpoints

### 1) Create case
`POST /agent/v1/after-sales/cases`

Body (v1 minimal):
```json
{
  "order_id": "ORD_xxx",
  "case_type": "refund",
  "resolution": "refund_without_return",
  "reason_code": "PRICE_ADJUSTMENT",
  "reason_text": "Agent requested partial refund",
  "requested_refund_amount": 10.5,
  "currency": "USD",
  "line_items": [
    { "item_ref": "line_1", "quantity_requested": 1, "refund_amount": 10.5, "currency": "USD" }
  ],
  "amount_breakdown": {
    "subtotal_refund": "10.50",
    "shipping_refund": "0.00",
    "tax_refund": "0.00",
    "discount_refund": "0.00",
    "total_refund": "10.50"
  },
  "idempotency_key": "client-generated-key"
}
```

Auth / scoping:
- Requires Agent API key (`X-API-Key`) via existing agent auth.
- For end-user ownership enforcement, pass one of:
  - `X-Agent-User-JWT` (preferred, verified), OR
  - `X-Buyer-Ref` / `?buyer_ref=` (legacy guest/user ref)

Response (high level):
- `case`: current state
- `next_action`: tells caller whether to issue label or refund

### 2) Get case
`GET /agent/v1/after-sales/cases/{case_id}`

### 3) List cases for an order
`GET /agent/v1/after-sales/orders/{order_id}/cases`

### 4) Issue return label (placeholder)
`POST /agent/v1/after-sales/cases/{case_id}/labels`

Behavior:
- Only valid when `resolution=refund_with_return`
- Returns `label_url` placeholder: `https://pivota.cc/return-labels/{case_id}`

### 5) Process refund for the case
`POST /agent/v1/after-sales/cases/{case_id}/refund`

Behavior:
- Executes full refund when `requested_refund_amount` is null.
- Executes partial refund when `requested_refund_amount` is present.
- Uses `idempotency_key = after_sales_case:{case_id}` when calling internal refund pipeline.

## Status model (v1 minimal)
- `requested`
- `label_issued` (for return flow)
- `refund_pending`
- `refund_processed` / `partially_refunded` / `refunded`

## Storage / privacy
- Table: `after_sales_cases`
- Intentionally does not store shipping address / email.
- Stores minimal amounts, item refs, and audit_log.

## Next TODOs
- Step‑up approval (risk tier / amount threshold)
- Return received confirmation (`return_in_transit`, `return_received`)
- Provider integration for real labels (Shippo/EasyPost/etc.)
- Partial line‑item refund enforcement and reconciliation fields (charge vs order currency)

