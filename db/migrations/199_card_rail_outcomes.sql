-- CARD-RAIL OUTCOMES — the other half of `recommendation_id`.
--
-- #2080 mints a `recommendation_id` per item and a `recommendation_set_id` per response, and
-- #1846/#2091 carry them to the agent inside the execution spec. Nothing could report back, so
-- the join key existed with nothing to join TO: every handoff left our systems and its result was
-- unobservable. This is the table that closes it.
--
-- WHY ONE ROW PER HANDOFF, KEYED ON recommendation_id. The whole value is being able to ask "of
-- the recommendations we made, which completed, and when they failed, why". That question is
-- answered per recommendation, not per event — so the grain is the handoff and a re-report
-- UPDATES rather than appends. An append-only event log would make the basic question a
-- windowing query and would let one chatty agent outvote a quiet one.
--
-- WHY QUOTED *AND* ACTUAL. The audit's measured finding is that 31.1% of index records would
-- produce a wrong or unexecutable spec. `quoted_*` is what we promised; `actual_*` is what the
-- buyer was really charged. Storing only one of them makes the single most valuable number in
-- this table — the size and direction of our own error — permanently underivable.
--
-- failure_reason IS THE COMPOUNDING ASSET. It is what turns a completion-probability term from a
-- guess into a measurement. See the note on the two columns below for why the vocabulary is
-- constrained AND the raw string is kept.

CREATE TABLE IF NOT EXISTS card_rail_outcomes (
    recommendation_id VARCHAR(64) PRIMARY KEY,

    -- Joins outward. `trace_id` reaches commerce_interactions; `click_id` is the fallback join
    -- when an agent drops the recommendation_id, and it is the one that survives INTO the
    -- merchant's order via Shopify note_attributes.
    recommendation_set_id VARCHAR(64),
    trace_id VARCHAR(128),
    click_id VARCHAR(64),

    -- Stamped from the authenticated context, NEVER from the request body. An agent that could
    -- name itself could attribute its failures to a competitor.
    agent_id VARCHAR(128) NOT NULL,

    merchant_domain VARCHAR(255),
    product_key VARCHAR(255),
    variant_id VARCHAR(64),
    rail VARCHAR(32),

    -- What the spec promised, and when it would have stopped being true.
    quoted_item_total NUMERIC(18, 4),
    quoted_grand_total NUMERIC(18, 4),
    quoted_currency VARCHAR(8),
    quoted_at TIMESTAMPTZ,
    spec_expires_at TIMESTAMPTZ,

    -- What actually happened at the merchant.
    actual_item_total NUMERIC(18, 4),
    actual_grand_total NUMERIC(18, 4),
    actual_currency VARCHAR(8),

    outcome VARCHAR(32) NOT NULL,

    -- TWO columns on purpose, and the reason matters. `failure_reason` is constrained so the
    -- vocabulary stays countable — a metric whose domain drifts cannot be trended.
    -- `failure_reason_raw` preserves whatever the caller actually sent, including a value we do
    -- not know yet. Rejecting an unknown reason would lose the outcome entirely and teach us
    -- nothing; silently coercing it into 'other' would erase the evidence that our vocabulary is
    -- incomplete. Keeping both means an unrecognised reason is still counted AND still legible.
    failure_reason VARCHAR(48),
    failure_reason_raw VARCHAR(255),

    latency_ms JSONB NOT NULL DEFAULT '{}'::jsonb,
    auth_outcome VARCHAR(48),

    -- Who is asserting this. An agent's self-report and a poller's observation are different
    -- kinds of evidence and must not be averaged together without being able to tell them apart.
    reported_by VARCHAR(32) NOT NULL,

    occurred_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT ck_card_rail_outcome CHECK (
        outcome IN ('completed', 'abandoned', 'failed', 'aborted_on_mismatch')
    ),
    CONSTRAINT ck_card_rail_reported_by CHECK (
        reported_by IN ('agent', 'reap', 'pivota_poller')
    ),
    CONSTRAINT ck_card_rail_failure_reason CHECK (
        failure_reason IS NULL OR failure_reason IN (
            'bot_blocked', 'out_of_stock', 'price_mismatch', 'variant_unavailable',
            'checkout_error', 'payment_declined', 'guest_checkout_required',
            'shipping_unsupported', 'pdp_404', 'spec_expired', 'agent_timeout'
        )
    ),
    -- A failure that does not say why is the one row that teaches nothing. `abandoned` is exempt:
    -- a buyer who simply walked away has no failure to name, and demanding one there would push
    -- callers into inventing a reason — which is worse than an honest null.
    CONSTRAINT ck_card_rail_failure_has_reason CHECK (
        outcome <> 'failed' OR failure_reason IS NOT NULL OR failure_reason_raw IS NOT NULL
    ),
    -- Currencies are compared, never converted, when deciding whether a quote held. A quote in
    -- one currency and an actual in another is not a price mismatch, it is a different question —
    -- so both are recorded and the comparison is left to the reader.
    CONSTRAINT ck_card_rail_quoted_pair CHECK (
        (quoted_grand_total IS NULL) = (quoted_currency IS NULL)
        OR quoted_grand_total IS NULL
    )
);

-- The three questions this table exists to answer, in the order they will be asked.
CREATE INDEX IF NOT EXISTS ix_card_rail_outcomes_occurred
  ON card_rail_outcomes (occurred_at DESC);
CREATE INDEX IF NOT EXISTS ix_card_rail_outcomes_failure
  ON card_rail_outcomes (failure_reason, occurred_at DESC)
  WHERE failure_reason IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_card_rail_outcomes_domain
  ON card_rail_outcomes (merchant_domain, occurred_at DESC);
-- The fallback join, for handoffs whose recommendation_id the agent dropped.
CREATE INDEX IF NOT EXISTS ix_card_rail_outcomes_click
  ON card_rail_outcomes (click_id)
  WHERE click_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_card_rail_outcomes_set
  ON card_rail_outcomes (recommendation_set_id)
  WHERE recommendation_set_id IS NOT NULL;
