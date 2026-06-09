-- Provider-aware AI-citation operator.
--
-- Turns the diagnostic audit into an operator loop:
--   citation_targets         — discovered cited sources the merchant is ABSENT from,
--                              segmented by provider (chatgpt|gemini) + channel
--                              (community|editorial|retailer|scientific|review|video).
--   citation_actions         — a move the merchant takes to close a target
--                              (reddit_reply|owned_faq|owned_evidence_page|...),
--                              with a draft + a scheduled revisit (next_check_at).
--   citation_action_outcomes — re-probe results proving (or disproving) the lift.
--
-- The revisit loop (next_check_at + check_count) is the product's tracking spine:
-- "merchant did X -> we re-check on a cadence -> show whether AI now cites them."
-- Cadence is tuned to ChatGPT/Gemini re-index latency (see citation_operator_service).

CREATE TABLE IF NOT EXISTS citation_targets (
  target_id        TEXT PRIMARY KEY,
  merchant_id      TEXT NOT NULL,
  sku_key          TEXT,                       -- NULL = brand-level target
  provider         TEXT NOT NULL,              -- chatgpt | gemini
  channel          TEXT NOT NULL,              -- community|editorial|retailer|scientific|review|video|other
  source_host      TEXT,                       -- reddit.com, ... ; NULL when only a human title is cited (Gemini)
  source_label     TEXT,                       -- the grounding label/title as cited
  subreddit        TEXT,                       -- NULL unless community/reddit
  question_text    TEXT,                       -- the buyer question the source answers
  merchant_brand   TEXT,                       -- captured at scan time so rechecks can detect citation
  merchant_host    TEXT,
  cited_in_runs    INTEGER NOT NULL DEFAULT 0, -- how many audit runs cited this source
  engagement_jsonb JSONB,                      -- upvotes/comments/freshness (from discovery enrich)
  status           TEXT NOT NULL DEFAULT 'discovered', -- discovered|actioned|dismissed
  target_key       TEXT NOT NULL,              -- idempotency: hash(merchant|provider|host|sku|question)
  first_seen       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Idempotent add: if an earlier version of this migration already created the
-- table without these columns (no migration ledger in this repo), CREATE TABLE
-- IF NOT EXISTS is a silent no-op, so add them explicitly.
ALTER TABLE citation_targets ADD COLUMN IF NOT EXISTS merchant_brand TEXT;
ALTER TABLE citation_targets ADD COLUMN IF NOT EXISTS merchant_host  TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_citation_targets_target_key
  ON citation_targets (target_key);
CREATE INDEX IF NOT EXISTS idx_citation_targets_merchant
  ON citation_targets (merchant_id, provider, channel, status);

CREATE TABLE IF NOT EXISTS citation_actions (
  action_id        TEXT PRIMARY KEY,
  target_id        TEXT NOT NULL REFERENCES citation_targets (target_id),
  merchant_id      TEXT NOT NULL,
  sku_key          TEXT,
  action_type      TEXT NOT NULL,              -- reddit_reply|owned_faq|owned_evidence_page|editorial_pitch|retailer_listing|review_nudge
  draft_content    TEXT,
  draft_model      TEXT,
  draft_credits    NUMERIC,
  status           TEXT NOT NULL DEFAULT 'draft', -- draft|in_review|approved|published|measuring|confirmed|no_effect
  posted_url       TEXT,
  posted_at        TIMESTAMPTZ,
  next_check_at    TIMESTAMPTZ,                -- when the revisit loop should re-probe
  check_count      INTEGER NOT NULL DEFAULT 0,
  idempotency_key  TEXT NOT NULL,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_citation_actions_idempotency
  ON citation_actions (idempotency_key);
CREATE INDEX IF NOT EXISTS idx_citation_actions_merchant
  ON citation_actions (merchant_id, status);
-- The tracking spine: find actions whose revisit is due.
CREATE INDEX IF NOT EXISTS idx_citation_actions_due
  ON citation_actions (next_check_at)
  WHERE status = 'measuring';

CREATE TABLE IF NOT EXISTS citation_action_outcomes (
  outcome_id       TEXT PRIMARY KEY,
  action_id        TEXT NOT NULL REFERENCES citation_actions (action_id),
  provider           TEXT NOT NULL,
  probed_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  brand_cited        BOOLEAN NOT NULL DEFAULT FALSE,
  source_cited       BOOLEAN NOT NULL DEFAULT FALSE, -- the specific target source still cited at recheck
  merchant_cited_runs INTEGER,                       -- absolute count of runs citing the merchant
  raw_jsonb          JSONB
);

CREATE INDEX IF NOT EXISTS idx_citation_action_outcomes_action
  ON citation_action_outcomes (action_id, probed_at DESC);

-- DOWN (manual rollback only):
-- DROP TABLE IF EXISTS citation_action_outcomes;
-- DROP TABLE IF EXISTS citation_actions;
-- DROP TABLE IF EXISTS citation_targets;
