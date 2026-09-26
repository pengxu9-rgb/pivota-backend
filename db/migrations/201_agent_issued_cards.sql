-- AGENT-ISSUED CARDS — the instrument half of the Reap card rail.
--
-- Topology, because it decides the safety model: on this rail Pivota is the BUYER-side
-- orchestrator. The MERCHANT charges the card on its own store checkout; Pivota never executes a
-- payment and never sees the PAN. So the enforcement point is not "verify before charging" (that
-- is the seller door's kernel) — it is CONSTRAIN THE INSTRUMENT AT MINT TIME and reconcile after
-- (card_rail_outcomes, migration 199, whose REPORTERS vocabulary already names 'reap').
--
-- Every constraint below is one of those mint-time constraints:
--   amount_cap_minor  = the landed total from the MERCHANT'S OWN UCP quote — never a caller
--                       number, never our index price (31.1% of index records are wrong-spec per
--                       the 2026-08-21 audit; the merchant's quote is the only number the
--                       merchant is guaranteed to honour).
--   merchant_domain   = the lock. Stamped from the quote we resolved, not from the request body.
--   single_use        = default true; a card that can be charged twice is a standing liability.
--   expires_at        = NOT NULL; an unexpiring cap is not a cap.
--
-- WHY quote_total_minor AND amount_cap_minor are separate columns even though v1 sets them
-- equal: the day ops wants headroom (FX drift on non-USD merchants), the policy becomes a
-- visible delta between two audited numbers instead of a silent multiplier in code.

CREATE TABLE IF NOT EXISTS agent_issued_cards (
    card_id VARCHAR(64) PRIMARY KEY,

    -- Stamped from the authenticated context, NEVER from the request body — same rule and same
    -- reason as card_rail_outcomes: an agent that could name itself could spend under another
    -- agent's caps.
    agent_id VARCHAR(128) NOT NULL,

    -- Joins to card_rail_outcomes.recommendation_id, closing mint -> spend -> outcome.
    recommendation_id VARCHAR(64),

    merchant_domain VARCHAR(255) NOT NULL,
    checkout_id VARCHAR(255) NOT NULL,

    quote_total_minor BIGINT NOT NULL CHECK (quote_total_minor > 0),
    amount_cap_minor BIGINT NOT NULL CHECK (amount_cap_minor > 0),
    currency VARCHAR(8) NOT NULL,

    -- The totals array exactly as the merchant returned it. This is evidence: when a settlement
    -- disputes the cap, the answer is "here is the quote we constrained to", not a re-fetch of a
    -- price that has since moved.
    quote_snapshot JSONB,

    issuer VARCHAR(32) NOT NULL,

    -- The issuer's card identifier and its hosted reveal handle. The PAN NEVER enters this
    -- system: the reveal handle is how the agent obtains credentials DIRECTLY from the issuer,
    -- which is what keeps Pivota outside PCI scope — the same line the ACP door draws by
    -- permanently refusing delegate_payment.
    issuer_card_ref VARCHAR(128),
    reveal_handle TEXT,

    status VARCHAR(16) NOT NULL DEFAULT 'requested'
        CHECK (status IN ('requested', 'issued', 'revoked', 'exhausted', 'expired', 'failed')),

    single_use BOOLEAN NOT NULL DEFAULT TRUE,
    expires_at TIMESTAMPTZ NOT NULL,

    failure_reason TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The two scans the caps guard runs on every mint: outstanding instruments, and today's minted
-- volume. Both are per-agent.
CREATE INDEX IF NOT EXISTS idx_agent_issued_cards_agent_status
    ON agent_issued_cards (agent_id, status);
CREATE INDEX IF NOT EXISTS idx_agent_issued_cards_agent_created
    ON agent_issued_cards (agent_id, created_at);
