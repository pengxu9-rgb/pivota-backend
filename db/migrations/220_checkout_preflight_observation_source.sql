-- 220: say WHERE each preflight observation came from, because the two sources answer different
-- questions and averaging them answers neither.
--
-- Rows written by live traffic are biased toward what agents actually ask for: a handful of hot
-- products, whatever a caller happened to request this week. Rows written by a corpus sweep
-- (scripts/measure_checkout_preflight.py) are unbiased and complete but describe the catalog
-- rather than demand. The enforcement decision needs both and must not confuse them — "the
-- merchants refuse 20% of what buyers ask for" and "the merchants refuse 20% of the catalog" are
-- different claims, and only one of them justifies arming a gate on the checkout path.
--
-- Mixing them silently is the same denominator mistake this gate has now made three times in
-- review: counting rows the question was never asked of, and reporting the result as an answer.
--
-- ADDITIVE AND DEFAULTED, so every row already in the table keeps its meaning: everything written
-- before this migration came from the request path, which is exactly what 'live' means.
ALTER TABLE checkout_preflight_observations
  ADD COLUMN IF NOT EXISTS source VARCHAR(32) NOT NULL DEFAULT 'live';

-- The report groups by (source, reason) over a window, so the window predicate leads.
CREATE INDEX IF NOT EXISTS idx_checkout_preflight_obs_source_created
  ON checkout_preflight_observations (source, created_at DESC);
