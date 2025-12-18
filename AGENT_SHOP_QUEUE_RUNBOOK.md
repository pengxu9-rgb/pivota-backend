# Shopping Agent Queue & Worker Pool Runbook

This document covers the in-process queue + worker pool that protects the
`/agent/shop/v1/invoke` gateway from overload and runaway agent behavior.

The queue is implemented in `services/agent_task_manager.py` and wired into
`routes/agent_shop_gateway.py` for the heavy operations:

- `find_products_multi` (cross-merchant search)
- `find_similar_products`

Other operations on `/agent/shop/v1/invoke` continue to execute directly.

---

## Tuning Knobs (Environment Variables)

All knobs are per-process (per uvicorn/gunicorn worker).

- `AGENT_SHOP_MAX_WORKERS`  
  - **Default:** `8`  
  - **Meaning:** Maximum number of concurrent heavy agent tasks per process.  
  - **Guidance:**  
    - Start with `4–8` depending on DB capacity.  
    - If DB CPU is high or connection pools saturate, **decrease** this value.

- `AGENT_SHOP_MAX_QUEUE_SIZE`  
  - **Default:** `64`  
  - **Meaning:** Maximum number of queued tasks waiting for a worker **per process**.  
  - When `running + queued >= max_workers + max_queue_size`, new requests are
    rejected with HTTP `429` and `detail="SHOP_BACKEND_OVERLOADED"`.  
  - **Guidance:**  
    - This should be large enough to absorb short spikes, but small enough
      that the process cannot grow memory without bounds.  
    - Typical range: `32–128`.

- `AGENT_SHOP_TASK_TIMEOUT_SECONDS`  
  - **Default:** `8.0`  
  - **Meaning:** Hard per-task wall-clock budget (per process) for heavy handlers.  
  - If exceeded, the task is marked `TIMEOUT` and the client receives
    HTTP `504` with `detail="UPSTREAM_TIMEOUT"`.  
  - **Guidance:**  
    - Keep this lower than your external reverse proxy timeout.  
    - Use `5–10s` for production; increase only if you have known slow queries.

- `AGENT_SHOP_MAX_CALLS_PER_SESSION`  
  - **Default:** `64`  
  - **Meaning:** Maximum number of **accepted** heavy tasks per logical session
    within a single process lifetime.  
  - Derived session IDs are based on:
    - `metadata.trace_id` (preferred), or  
    - `creator_id + user.id` for Creator Agent UI calls.  
  - When exceeded, the request returns HTTP `429` with
    `detail="SESSION_BUDGET_EXCEEDED"`.  
  - **Guidance:**  
    - This protects against very long or stuck conversations.  
    - Values between `32–128` are typical.

- `AGENT_SHOP_MAX_DUPLICATE_CALLS_PER_SESSION`  
  - **Default:** `3`  
  - **Meaning:** Loop detection budget for identical `operation + arguments`
    within a session.  
  - When exceeded, requests are rejected with HTTP `429` and
    `detail="TOOL_LOOP_DETECTED"`.  
  - **Guidance:**  
    - Keep this small (`2–4`) to catch misconfigured agents that repeatedly
      call the same tool with the same payload.

- `AGENT_SHOP_MAX_QUEUE_WAIT_SECONDS`  
  - **Default:** `5.0`  
  - **Meaning:** Maximum time a task is allowed to sit in the queue **before** it starts.  
  - If exceeded, the task is marked `EXPIRED` and the caller receives
    HTTP `503` with `detail="QUEUE_TIMEOUT"`.  
  - **Guidance:**  
    - This prevents very stale work from running long after the caller gave up.  
    - Typical range: `2–10s` depending on acceptable end-to-end latency.

- `AGENT_SHOP_INVOKE_MAX_WAIT_SECONDS`  
  - **Default:** `0.0` (disabled; `/invoke` waits for completion up to the task timeout).  
  - **Meaning:** Optional short-wait budget for `/agent/shop/v1/invoke` when using
    `find_products_multi` or `find_similar_products`.  
  - Behavior when set to `> 0` seconds:
    - The gateway waits up to this many seconds for the queued task to finish.  
    - If it completes within the window → normal `200` response.  
    - If it does **not** complete → returns `{ "status": "pending", "task_id": "..." }`
      so the caller can poll `GET /agent/shop/v1/creator/tasks/{task_id}`.  
  - **Guidance:**  
    - Use this to introduce async, non-blocking behavior for agents without
      changing the existing `/invoke` contract when the flag is left at `0`.

---

## Behavioral Guarantees

- **Bounded concurrency:**  
  At most `AGENT_SHOP_MAX_WORKERS` heavy tasks execute concurrently per process.

- **Bounded queue:**  
  At most `AGENT_SHOP_MAX_QUEUE_SIZE` heavy tasks can be queued per process.
  Once reached, new requests fail fast with 429 instead of piling up.

- **Per-session single-flight:**  
  For calls where a session can be derived, at most one in-flight task
  (queued or running) is allowed:
  - Additional calls for the same session are rejected with HTTP `409`
    and `detail="SESSION_ALREADY_RUNNING"`.

- **Per-session budgets:**  
  `AGENT_SHOP_MAX_CALLS_PER_SESSION` and
  `AGENT_SHOP_MAX_DUPLICATE_CALLS_PER_SESSION` enforce simple execution
  budgets aligned with “max steps / max tool calls / loop detection”.

- **Cancellation on client disconnect:**  
  If the HTTP request is cancelled (e.g. client disconnects), the handler
  attempts best-effort cancellation via `AgentTaskManager.cancel(...)`.
  DB queries may still run to completion, but results are dropped and the
  task is marked `CANCELLED`.

> **Note:** All of the above guarantees apply **per-process**. If you run
> multiple uvicorn/gunicorn workers, each maintains its own queue and budgets.

---

## Operational Signals & Alarms

### HTTP-Level Signals

Monitor aggregated metrics per endpoint:

- `POST /agent/shop/v1/invoke`
  - `status=200` — normal success
  - `status=429, detail="SHOP_BACKEND_OVERLOADED"` — queue backpressure
  - `status=429, detail="SESSION_BUDGET_EXCEEDED"` — session over budget
  - `status=429, detail="TOOL_LOOP_DETECTED"` — loop detection triggered
  - `status=409, detail="SESSION_ALREADY_RUNNING"` — per-session single-flight
  - `status=504, detail="UPSTREAM_TIMEOUT"` — task runtime exceeded

**Suggested alerts:**

- 429 (any reason) > **5–10%** of requests for 5–10 minutes.  
- 504 > **1–2%** of requests for 5–10 minutes.  
- Sudden increase in 409s for a specific `creator_id` or `trace_id`
  (indicates a misbehaving agent or integration).

### In-Process Queue Snapshot (Dev/Non-Prod)

In non-production (`APP_ENV != "production"`), two dev endpoints exist:

- `GET /agent/shop/v1/dev/queue-status`  
  Returns a JSON snapshot from `AgentTaskManager.snapshot()`:
  - `max_workers`, `max_queue_size`, `task_timeout_seconds`
  - `running`, `queued`
  - `metrics`: `enqueued`, `started`, `completed`, `failed`,
    `cancelled`, `timed_out`, `rejected`

- `GET /agent/shop/v1/dev/similar`  
  Existing dev endpoint to debug similar-products behavior; unaffected
  by the queue but useful when tuning `AGENT_SHOP_TASK_TIMEOUT_SECONDS`.

Use the queue status endpoint for debugging and load tests only; do not
expose it in production.

### Logs

Structured logs are emitted with the following event keys:

- `agent_queue.enqueued`
- `agent_queue.start`
- `agent_queue.task_error`
- `agent_queue.reject_full`
- `agent_queue.reject_session_budget`
- `agent_queue.reject_loop_detected`
- `agent_queue.reject_single_flight`
- `agent_queue.cancel_queued`
- `agent_queue.cancel_running`

These can be used to build dashboards around:

- Queue depth and saturation.
- Per-session budget violations.
- Loop detection incidents by `creator_id` or `trace_id`.

---

## Running Tests & Load Script

From `pivota-backend/`:

- **Unit + integration tests for the queue:**

  ```bash
  pytest -q
  ```

  Relevant test files:

  - `tests/test_agent_task_manager.py`
  - `tests/test_agent_shop_queue_integration.py`

From the repo root:

- **Load/regression script for `/agent/shop/v1/invoke`:**

  ```bash
  # Basic run against local dev server
  python scripts/agent_shop_load_test.py \
    --base-url http://localhost:8000 \
    --concurrency 8 \
    --requests-per-worker 20
  ```

  - Use `--mode session` (default) to exercise per-session budgets.  
  - Use `--mode anonymous` to stress global queue backpressure without
    deriving a session id.

There is no standardized Python linter/formatter configured in this repo.
If you use `black` or `ruff` locally, run them as you normally would.

---

## Rollback & Safe Reconfiguration

The queue and budgets are implemented purely in-process; rollback is a
matter of configuration and, if needed, code rollback.

### Configuration-Only Rollback (Keep Code, Relax Limits)

If you want to temporarily disable most backpressure/budgeting while
keeping the code deployed:

- Set very permissive values:

  - `AGENT_SHOP_MAX_WORKERS` → a higher value (e.g. `32`)  
  - `AGENT_SHOP_MAX_QUEUE_SIZE` → higher (e.g. `512`)  
  - `AGENT_SHOP_MAX_CALLS_PER_SESSION` → high (e.g. `1000`)  
  - `AGENT_SHOP_MAX_DUPLICATE_CALLS_PER_SESSION` → high (e.g. `100`)
  - `AGENT_SHOP_TASK_TIMEOUT_SECONDS` → slightly higher (e.g. `15.0`)

- Monitor DB and CPU closely; these settings effectively approximate the
  previous unbounded behavior, so they should only be used briefly.

### Full Code Rollback

If configuration tuning is not sufficient and you need to revert to the
pre-queue behavior:

1. Roll back `pivota-backend` to a commit prior to:
   - `services/agent_task_manager.py`
   - The changes in `routes/agent_shop_gateway.py` that call the manager.
2. Redeploy the backend.
3. Re-run:
   - `pytest` in `pivota-backend/`
   - Smoke tests for `/agent/shop/v1/invoke` and Creator Agent UI flows.

> **Reminder:** removing the queue removes protections against OOM and
> pool exhaustion under abusive or misconfigured agent workloads. Prefer
> configuration-based relaxation wherever possible.
