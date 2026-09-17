-- REAP AGENTIC PURCHASE + ENROLLMENT LEDGER — the durable half of the agentic-payment rail.
--
-- This is a DIFFERENT rail from the issued-card one (migrations 201/202/207). There we mint a
-- virtual card and authorize it ourselves. Here the buyer enrolls THEIR OWN card, once, on
-- Reap's hosted page, and every later purchase is approved by the buyer on another hosted page.
-- Pivota never holds money and never sees card data, which is the whole reason the rail is
-- worth having — and it is also why these two tables carry no card fields beyond the network
-- name and the last four digits Reap echoes back for display.
--
-- WHY TWO TABLES AND NOT ONE. An enrollment is LONG-LIVED and per-buyer; a purchase is
-- short-lived and per-attempt. Folding the enrollment into the purchase row would either
-- duplicate it across every purchase (and make "is this buyer enrolled?" a scan over purchase
-- history) or make the purchase row the enrollment's home, so that deleting purchase history
-- de-enrolls the buyer. They have different lifetimes, so they are different rows.
--
-- WHY buyer_ref AND NOT buyer_id. buyer_ref is an OPAQUE per-buyer reference we mint. It is
-- what we send to Reap as owner.id, so it leaves our system — and a value that leaves our
-- system must not be a key into the rest of it. The global buyer id is never written here.
--
-- WHY THE PURCHASE IS A STATE MACHINE IN A COLUMN. The flow is resolve -> quote -> hosted
-- approval -> poll-until-terminal, and the polling half runs in a worker that can be restarted,
-- duplicated, or run twice on the same row by two pods. The `state` column plus the CHECK is
-- what makes every advance expressible as ONE conditional UPDATE
-- (`WHERE id = ? AND state IN (...) RETURNING *`), which is the only concurrency guard on this
-- rail that does not depend on a lock being available. db/reap_agentic_ledger.py holds the
-- allowed-pair map; this CHECK holds the vocabulary.
--
-- WHY shipping_address AND buyer_email ARE HERE AT ALL, AND WHY THEY DO NOT STAY. A re-quote
-- needs the shipping address (the total depends on it) and Reap needs a contact email, so the
-- row has to carry both while the purchase is in flight. They are PII, so the ledger module
-- NULLs both in the same statement that writes a terminal state — the row survives for
-- accounting, the PII does not. Nothing here logs either column.
--
-- AMOUNTS ARE INTEGER MINOR UNITS, always. services/reap_webhooks.major_to_minor is the one
-- converter; it REFUSES rather than rounds. reap_variant_id is EVIDENCE, never identity: our
-- own product_key/variant_key are the identity, because Reap's ids are opaque and can change
-- under a substitution (a checkout can come back 200 with a different variant than we quoted).
--
-- Production deploys skip db/migrations/, so both CREATE TABLEs are ALSO in
-- db/schema_guard.ensure_required_schema_light, byte-identical. If you change one, change both.

CREATE TABLE IF NOT EXISTS reap_agentic_enrollments (
    -- Ours, minted by us. Reap's id lands in reap_enrollment_id and is not the key, because the
    -- row exists (status 'pending', with a hosted_url) BEFORE Reap has an id for it.
    id VARCHAR(64) PRIMARY KEY,

    -- The opaque per-buyer reference we send to Reap as owner.id. NOT the global buyer id.
    buyer_ref VARCHAR(128) NOT NULL,

    agent_id VARCHAR(128),

    -- Reap's enrollment id. Unique when present — enforced by a PARTIAL unique index below
    -- rather than a column UNIQUE, because Postgres treats NULLs as distinct and every pending
    -- row is NULL here.
    reap_enrollment_id VARCHAR(128),

    -- OURS: the three states this rail acts on.
    --   pending — hosted page minted, buyer has not finished
    --   active  — usable for a purchase
    --   dead    — superseded, revoked, or expired
    status VARCHAR(16) NOT NULL CHECK (status IN ('pending', 'active', 'dead')),

    -- Reap's raw upstream value, recorded and never matched on. Their vocabulary is theirs to
    -- change; `status` above is ours and is what the code branches on.
    reap_status VARCHAR(64),

    -- The ONLY two card fields that may ever exist here. Anything else is card data.
    card_network VARCHAR(32),
    card_last4 VARCHAR(4) CHECK (card_last4 IS NULL OR length(card_last4) = 4),

    hosted_url TEXT,
    hosted_url_expires_at TIMESTAMPTZ,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- "Is this buyer enrolled?" — the read every purchase makes before it quotes.
CREATE INDEX IF NOT EXISTS idx_reap_agentic_enrollments_buyer_status
    ON reap_agentic_enrollments (buyer_ref, status);

-- Reap's id is unique WHEN PRESENT. Partial, so the pending rows that have no id yet do not
-- collide with each other.
CREATE UNIQUE INDEX IF NOT EXISTS uq_reap_agentic_enrollments_reap_id
    ON reap_agentic_enrollments (reap_enrollment_id)
    WHERE reap_enrollment_id IS NOT NULL;

-- AT MOST ONE ACTIVE ENROLLMENT PER BUYER. This is the invariant the whole rail rests on: with
-- two active rows, "which card did this buyer authorize?" has no answer, and the purchase path
-- would pick one arbitrarily. db/reap_agentic_ledger.mark_enrollment_active demotes any other
-- active row in the same transaction; this index is what makes the demotion non-optional.
CREATE UNIQUE INDEX IF NOT EXISTS uq_reap_agentic_enrollments_one_active
    ON reap_agentic_enrollments (buyer_ref)
    WHERE status = 'active';


CREATE TABLE IF NOT EXISTS reap_agentic_purchases (
    -- Ours, prefix `rp_`.
    id VARCHAR(64) PRIMARY KEY,

    buyer_ref VARCHAR(128) NOT NULL,

    -- The OWNERSHIP PAIR. Every read a caller can reach is conjunct on BOTH of these, in SQL —
    -- an agent must not be able to read another agent's purchase, and within one agent, one end
    -- user must not be able to read another's. agent_user_ref_hash is a hash precisely so the
    -- agent's own user identifier never lands in our storage.
    agent_id VARCHAR(128),
    agent_user_ref_hash VARCHAR(64),

    -- FK BY VALUE, deliberately not a REFERENCES constraint: the purchase outlives the
    -- enrollment (an enrollment goes 'dead' while completed purchases made with it stay), and a
    -- real FK would either block that or cascade the history away.
    enrollment_id VARCHAR(64),

    -- The state machine. See the header; db/reap_agentic_ledger.ALLOWED_TRANSITIONS holds the
    -- legal pairs, this CHECK holds the vocabulary.
    state VARCHAR(24) NOT NULL CHECK (state IN (
        'resolving', 'needs_enrollment', 'quoting', 'awaiting_approval',
        'processing', 'completed', 'failed', 'refused', 'expired'
    )),

    merchant_domain VARCHAR(255),

    -- OUR identity for the thing being bought. The reap_* ids below are evidence.
    product_key TEXT,
    variant_key TEXT,
    product_name TEXT,
    variant_title TEXT,
    brand TEXT,
    category TEXT,

    quantity INTEGER NOT NULL DEFAULT 1 CHECK (quantity > 0),

    currency VARCHAR(8),

    -- What OUR catalog said the price was, minor units. Kept next to the quote so a divergence
    -- between our price and Reap's is a row you can look at rather than an inference.
    our_price_minor BIGINT,

    click_id VARCHAR(128),
    return_url TEXT,

    -- EVIDENCE ONLY. Reap's ids are opaque and a checkout can come back 200 having substituted
    -- a different variant, so these record what Reap said — they never become identity.
    reap_product_id VARCHAR(128),
    reap_variant_id VARCHAR(128),

    reap_quote_id VARCHAR(128),
    reap_quote_expires_at TIMESTAMPTZ,

    -- Unique when present, partial index below, same reason as reap_enrollment_id.
    reap_checkout_id VARCHAR(128),
    reap_order_id VARCHAR(128),

    -- Minor units throughout.
    quoted_total_minor BIGINT,
    final_total_minor BIGINT,
    shipping_minor BIGINT,
    tax_minor BIGINT,

    hosted_url TEXT,
    hosted_url_expires_at TIMESTAMPTZ,

    -- Why we refused, in OUR vocabulary, for the 'refused' state.
    refusal_reason VARCHAR(64),

    -- The product queries the resolver tried, in order. Diagnostic: when a resolve fails, this
    -- is the only record of what was actually asked.
    queries_tried JSONB,

    last_error_code VARCHAR(64),

    -- Poll bookkeeping. attempts counts CLAIMS, not retries of a failed call.
    attempts INTEGER NOT NULL DEFAULT 0,
    claimed_by VARCHAR(128),
    claimed_at TIMESTAMPTZ,
    next_poll_at TIMESTAMPTZ,

    -- PII. Present only while the purchase is in flight; NULLed by the same statement that
    -- writes a terminal state. Never logged.
    shipping_address JSONB,
    buyer_email TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    terminal_at TIMESTAMPTZ
);

-- THE POLLER'S READ. (state, next_poll_at) in this order: state is the equality, next_poll_at
-- is the range and the sort. Reversed, every poll scans every row in the table.
CREATE INDEX IF NOT EXISTS idx_reap_agentic_purchases_state_poll
    ON reap_agentic_purchases (state, next_poll_at);

-- "What has this buyer bought?" — the buyer-facing history read.
CREATE INDEX IF NOT EXISTS idx_reap_agentic_purchases_buyer_created
    ON reap_agentic_purchases (buyer_ref, created_at);

-- The OWNERSHIP read, in the same column order the list query's conjunct uses.
CREATE INDEX IF NOT EXISTS idx_reap_agentic_purchases_owner_created
    ON reap_agentic_purchases (agent_id, agent_user_ref_hash, created_at);

-- One purchase row per Reap checkout. Partial, because the id is NULL until we have created the
-- checkout, and every row before that point would otherwise collide.
CREATE UNIQUE INDEX IF NOT EXISTS uq_reap_agentic_purchases_checkout
    ON reap_agentic_purchases (reap_checkout_id)
    WHERE reap_checkout_id IS NOT NULL;
