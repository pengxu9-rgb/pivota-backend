# PCS Reducer Operations (v0.2-b minimal)

This doc is an operator/developer runbook for the **PCS v0.2-b reducer**: turning append-only facts into a deterministic, replayable per-order “current state”.

Related docs:
- `external_repos/pivota-backend/QUOTE_FIRST_OPERATIONS.md`

---

## 1) Data Model (tables)

### `pcs_order_facts` (append-only)

Each row is an immutable fact from:
- Shopify webhooks (`source=shopify_webhook`)
- Internal flows (`source=internal`) emitting no-PII references (order created, payment updated, evidence pack frozen, etc.)

Dedupe:
- `UNIQUE (merchant_id, dedupe_key)` ensures idempotent ingestion.

### `pcs_orders_current` (derived state)

Per `(merchant_id, order_id)` JSON blob (`current_state_json`) with a minimal schema:
- `order_status`, `financial_status`, `fulfillment_status`
- `last_update_at`
- `currency`, `totals`
- optionally `quote` / `dispute` refs when present

### `pcs_reducer_checkpoints` (incremental cursor)

Checkpoint is stored in `checkpoint_json` per `(merchant_id, stream_id, reducer_name)`:
- `last_received_at`: highest `pcs_order_facts.received_at` processed
- `last_row_id`: highest `pcs_order_facts.id` processed at `last_received_at`

This `(last_received_at, last_row_id)` cursor prevents re-processing the last row forever while staying safe when multiple facts share the same timestamp.

---

## 2) Deterministic Ordering (out-of-order safe)

Reducer convergence relies on deterministic ordering for replay:

For a given `order_id`, facts are reduced in ascending order by:
1) `occurred_at` (event time; missing → epoch)
2) `received_at` (ingest time; missing → epoch)
3) `fact_id` (tie-breaker; missing → empty string)

Incremental processing is **out-of-order safe**:
- We scan “new” facts by the monotonic cursor `(received_at, id)`.
- For each impacted `order_id`, we recompute the order state from **all facts for that order** (sorted deterministically) to incorporate late-arriving facts with earlier `occurred_at`.

---

## 3) Ops Replay Endpoint

Employee-only endpoint:

`POST /ops/v1/pcs/reducer/replay?merchant_id=...&since=...&limit=...`

Behavior:
1) Backfills `pcs_shopify_webhook_events` → `pcs_order_facts` (best-effort, deduped).
2) Runs the reducer for `stream_id=orders` and updates `pcs_orders_current` + checkpoint.
3) Returns counts only (no payloads).

Example:

```bash
curl -sS "$API_BASE/ops/v1/pcs/reducer/replay?merchant_id=m_xxx&since=2025-01-01T00:00:00Z" \
  -H "Authorization: Bearer $EMPLOYEE_TOKEN" | jq
```

---

## 4) Debug SQL

### 4.1 Current state for an order

```sql
SELECT
  merchant_id,
  order_id,
  last_fact_occurred_at,
  last_reduced_at,
  current_state_json
FROM pcs_orders_current
WHERE merchant_id = 'm_xxx' AND order_id = '1001';
```

### 4.2 Facts for an order (time-ordered)

```sql
SELECT
  id,
  fact_type,
  occurred_at,
  received_at,
  source,
  topic,
  dedupe_key
FROM pcs_order_facts
WHERE merchant_id = 'm_xxx' AND order_id = '1001'
ORDER BY occurred_at ASC NULLS FIRST, received_at ASC, fact_id ASC;
```

### 4.3 Reducer checkpoint

```sql
SELECT
  merchant_id,
  stream_id,
  reducer_name,
  checkpoint_json,
  updated_at
FROM pcs_reducer_checkpoints
WHERE merchant_id = 'm_xxx' AND stream_id = 'orders' AND reducer_name = 'pcs_orders_current_v0_2';
```

---

## 5) Observability (events)

Reducer runs emit best-effort events into the existing sink:
- `reducer_run_started`
- `reducer_run_completed`
- `reducer_run_failed`

Payload includes: `merchant_id`, `stream_id`, counts, and `duration_ms` (no PII).

