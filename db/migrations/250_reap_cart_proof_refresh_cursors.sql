-- 250: where each scheduled cart-proof refresh left off, per (lane, domain) (db/reap_cart_proof_refresh_cursors.py).
--
-- jobs/reap_cart_proof_refresh.py walks each store page by page on the proof writer's own cursor, inside a
-- daily wall-clock budget. Without a stored cursor, a store the budget cut short restarted at its first page the
-- next day, so the tail of a large store was never reached and its 7-day (mirror) or 72-hour (enrichment) proofs
-- lapsed. One row per (lane, domain):
--   next_cursor        the writer cursor the next run resumes from; NULL = start at the beginning
--   last_status        how the last run ended for this store (done, budget_stopped, aborted_on_block, ...)
--   last_completed_at  when a run last walked this store to its end; the mirror lane orders stores by it
--                      (never-completed first) together with the oldest still-valid proof
--   blocked_until      set when the store blocked us (aborted_on_block): the store is skipped until then,
--                      and ordered LAST on the first run after it, so one store that always blocks cannot
--                      stall the others. Clear it (NULL) to retry the store sooner.
--   crash_count        consecutive runs that crashed at the SAME resume cursor; at 2 the cursor is reset
--                      to NULL (so the rows before it are walked again) and the count to 0
--   updated_at         when this row was last written
--
-- Written only by an APPLY run (a dry run reads it and writes nothing). Migrations do not self-apply in prod;
-- ensure_table() in db/reap_cart_proof_refresh_cursors.py runs the identical CREATE at first use. This file is
-- the record, and what the SQL gates plan against.

CREATE TABLE IF NOT EXISTS reap_cart_proof_refresh_cursors (
  lane               TEXT NOT NULL,
  domain             TEXT NOT NULL,
  next_cursor        TEXT,
  last_status        TEXT NOT NULL,
  last_completed_at  TIMESTAMPTZ,
  blocked_until      TIMESTAMPTZ,
  crash_count        INTEGER NOT NULL DEFAULT 0,
  updated_at         TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (lane, domain)
);
