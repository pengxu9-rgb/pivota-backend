-- 207: durable queue for post-payment merchant-order sync work.
--
-- The merchant-side order write currently rides on FastAPI BackgroundTasks at
-- six call sites, all of which run AFTER the buyer has paid. Those tasks run in
-- the API process with no retry and no supervision, so a Cloud Run revision
-- swap or scale-down drops them silently: the buyer is charged and the merchant
-- never receives the order.
--
-- Five of those sites leave a queryable trace (a paid order with no merchant
-- order), so a reconciler can find them. The refund site does not: an order
-- whose Shopify cancel never fired is indistinguishable from one whose cancel
-- succeeded — both are refunded and both still carry their shopify_order_id.
-- There is nothing to reconcile against, so the intent has to be recorded
-- durably at the moment it is formed. That is what this table is for.
--
-- Only the refund op is wired initially; `op` exists so the five create sites
-- can migrate onto the same queue without another migration.

CREATE TABLE IF NOT EXISTS merchant_order_sync_jobs (
  job_id            UUID PRIMARY KEY,
  order_id          TEXT NOT NULL,
  merchant_id       TEXT NOT NULL,
  op                TEXT NOT NULL,
  -- Distinguishes two legitimate jobs for the same (order_id, op) — e.g. two
  -- partial refunds on one order. For refund_sync this is the PSP refund id.
  dedupe_key        TEXT NOT NULL,
  -- JSON text rather than JSONB: this row is written on the money path and read
  -- by a worker, never queried by its inner fields, and TEXT behaves identically
  -- under asyncpg and the SQLite used by hermetic tests.
  payload           TEXT NOT NULL,
  status            TEXT NOT NULL DEFAULT 'pending',
  attempts          INTEGER NOT NULL DEFAULT 0,
  max_attempts      INTEGER NOT NULL DEFAULT 8,
  claimed_by_worker TEXT,
  claimed_until     TIMESTAMPTZ,
  next_attempt_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_error        TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  completed_at      TIMESTAMPTZ
);

-- Enqueue is idempotent: a retried refund request must not queue the work twice.
CREATE UNIQUE INDEX IF NOT EXISTS idx_merchant_order_sync_jobs_dedupe
  ON merchant_order_sync_jobs (order_id, op, dedupe_key);

-- The worker's claim predicate. Partial so the index stays small as completed
-- rows accumulate.
CREATE INDEX IF NOT EXISTS idx_merchant_order_sync_jobs_claim
  ON merchant_order_sync_jobs (next_attempt_at, created_at)
  WHERE status IN ('pending', 'running');

-- Ops: surface terminally-failed jobs, which are money-path incidents.
CREATE INDEX IF NOT EXISTS idx_merchant_order_sync_jobs_failed
  ON merchant_order_sync_jobs (updated_at DESC)
  WHERE status = 'failed';
