"""Persistence for `card_rail_outcomes` — one row per handoff, keyed on recommendation_id.

The vocabularies live here rather than in the route because the CHECK constraints in migration
199 are the real contract; a second copy that drifts from them would turn a clean 4xx into a
500 from the database. `test_card_rail_outcomes` asserts these lists and the migration agree.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from db.database import database

# Kept in the order the audit specifies them, which is roughly "how far the buyer got".
FAILURE_REASONS = (
    "bot_blocked",
    "out_of_stock",
    "price_mismatch",
    "variant_unavailable",
    "checkout_error",
    "payment_declined",
    "guest_checkout_required",
    "shipping_unsupported",
    "pdp_404",
    "spec_expired",
    "agent_timeout",
)

OUTCOMES = ("completed", "abandoned", "failed", "aborted_on_mismatch")

REPORTERS = ("agent", "reap", "pivota_poller")

# One row per handoff: a re-report REPLACES. See the migration's note on grain — an agent that
# reports "failed" and then, after a retry, "completed" must leave one row saying completed, not
# two rows that have to be windowed apart.
#
# `updated_at` moves on every write so a late correction is distinguishable from the original.
# Columns absent from a correction are NOT nulled: an agent reporting only the final outcome must
# not wipe the quoted totals it reported earlier. COALESCE keeps the earlier value; passing an
# explicit null cannot clear a field, which is the right trade for a measurement table.
UPSERT_SQL = """
    INSERT INTO card_rail_outcomes (
        recommendation_id, recommendation_set_id, trace_id, click_id, agent_id,
        merchant_domain, product_key, variant_id, rail,
        quoted_item_total, quoted_grand_total, quoted_currency, quoted_at, spec_expires_at,
        actual_item_total, actual_grand_total, actual_currency,
        outcome, failure_reason, failure_reason_raw,
        latency_ms, auth_outcome, reported_by, occurred_at
    ) VALUES (
        :recommendation_id, :recommendation_set_id, :trace_id, :click_id, :agent_id,
        :merchant_domain, :product_key, :variant_id, :rail,
        :quoted_item_total, :quoted_grand_total, :quoted_currency, :quoted_at, :spec_expires_at,
        :actual_item_total, :actual_grand_total, :actual_currency,
        :outcome, :failure_reason, :failure_reason_raw,
        CAST(:latency_ms AS jsonb), :auth_outcome, :reported_by, :occurred_at
    )
    ON CONFLICT (recommendation_id) DO UPDATE SET
        recommendation_set_id = COALESCE(EXCLUDED.recommendation_set_id, card_rail_outcomes.recommendation_set_id),
        trace_id              = COALESCE(EXCLUDED.trace_id, card_rail_outcomes.trace_id),
        click_id              = COALESCE(EXCLUDED.click_id, card_rail_outcomes.click_id),
        merchant_domain       = COALESCE(EXCLUDED.merchant_domain, card_rail_outcomes.merchant_domain),
        product_key           = COALESCE(EXCLUDED.product_key, card_rail_outcomes.product_key),
        variant_id            = COALESCE(EXCLUDED.variant_id, card_rail_outcomes.variant_id),
        rail                  = COALESCE(EXCLUDED.rail, card_rail_outcomes.rail),
        quoted_item_total     = COALESCE(EXCLUDED.quoted_item_total, card_rail_outcomes.quoted_item_total),
        quoted_grand_total    = COALESCE(EXCLUDED.quoted_grand_total, card_rail_outcomes.quoted_grand_total),
        quoted_currency       = COALESCE(EXCLUDED.quoted_currency, card_rail_outcomes.quoted_currency),
        quoted_at             = COALESCE(EXCLUDED.quoted_at, card_rail_outcomes.quoted_at),
        spec_expires_at       = COALESCE(EXCLUDED.spec_expires_at, card_rail_outcomes.spec_expires_at),
        actual_item_total     = COALESCE(EXCLUDED.actual_item_total, card_rail_outcomes.actual_item_total),
        actual_grand_total    = COALESCE(EXCLUDED.actual_grand_total, card_rail_outcomes.actual_grand_total),
        actual_currency       = COALESCE(EXCLUDED.actual_currency, card_rail_outcomes.actual_currency),
        -- The outcome and its reason DO overwrite: they are the thing being corrected. A reason
        -- is cleared when the new outcome carries none, so a row that becomes `completed` does
        -- not keep asserting the failure it recovered from.
        outcome               = EXCLUDED.outcome,
        failure_reason        = EXCLUDED.failure_reason,
        failure_reason_raw    = EXCLUDED.failure_reason_raw,
        latency_ms            = COALESCE(NULLIF(EXCLUDED.latency_ms, '{}'::jsonb), card_rail_outcomes.latency_ms),
        auth_outcome          = COALESCE(EXCLUDED.auth_outcome, card_rail_outcomes.auth_outcome),
        reported_by           = EXCLUDED.reported_by,
        occurred_at           = EXCLUDED.occurred_at,
        updated_at            = CURRENT_TIMESTAMP
    RETURNING recommendation_id, (xmax = 0) AS inserted
"""


async def record_outcome(values: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Insert or update one handoff outcome. Returns {recommendation_id, inserted} or None.

    `xmax = 0` is Postgres's own answer to "was this an INSERT or an UPDATE" on an upsert — read
    from the row rather than inferred from a prior SELECT, which would race.
    """
    row = await database.fetch_one(UPSERT_SQL, values)
    return dict(row) if row else None
