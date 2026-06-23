"""P1 — claim_state lifecycle + the syndicate-after-claim gate.

A canonical SKU progresses:
    unclaimed     (audit-seeded; OBSERVED info only)
 -> claimed       (a verified brand claim — brand_direct)
 -> attested      (the brand submitted info / lab evidence)
 -> substantiated (those claims were graded)

The gate: serve OBSERVED info always, but publish/syndicate BRAND-ATTESTED copy
only once the entity is claimed+. This module owns the vocabulary, the gate
predicate, and the unclaimed->claimed promotion fired on claim verification.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

CLAIM_STATE_UNCLAIMED = "unclaimed"
CLAIM_STATE_CLAIMED = "claimed"
CLAIM_STATE_ATTESTED = "attested"
CLAIM_STATE_SUBSTANTIATED = "substantiated"

# Ordered lifecycle — each step is >= the previous in trust.
_CLAIM_ORDER = {
    CLAIM_STATE_UNCLAIMED: 0,
    CLAIM_STATE_CLAIMED: 1,
    CLAIM_STATE_ATTESTED: 2,
    CLAIM_STATE_SUBSTANTIATED: 3,
}

# Minimum state at which brand-attested copy may be published/syndicated.
_PUBLISH_BRAND_ATTESTED_MIN = CLAIM_STATE_CLAIMED


def may_publish_brand_attested(claim_state: Optional[str]) -> bool:
    """The SYNDICATE-AFTER-CLAIM gate: brand-attested copy may be published /
    syndicated only once the entity is claimed+ (never for an unclaimed /
    observed seed). Observed info is served regardless; this gates only
    brand-AUTHORED content. Unknown/empty states are treated as un-publishable."""
    rank = _CLAIM_ORDER.get((claim_state or "").strip().lower(), -1)
    return rank >= _CLAIM_ORDER[_PUBLISH_BRAND_ATTESTED_MIN]


async def promote_merchant_skus_to_claimed(merchant_id: str) -> int:
    """Best-effort: promote a verified brand's UNCLAIMED catalog SKUs to
    'claimed'. Only advances unclaimed -> claimed (never downgrades an already
    attested/substantiated SKU). Returns rows updated, 0 on failure — never
    raises (it runs off the claim-verify path and must not break it)."""
    if not merchant_id:
        return 0
    try:
        from db.database import database

        result = await database.execute(
            """
            UPDATE catalog_products
               SET claim_state = :claimed, updated_at = NOW()
             WHERE merchant_id = :merchant_id
               AND claim_state = :unclaimed
            """,
            {
                "claimed": CLAIM_STATE_CLAIMED,
                "unclaimed": CLAIM_STATE_UNCLAIMED,
                "merchant_id": merchant_id,
            },
        )
        return int(result) if isinstance(result, int) else 0
    except Exception as exc:  # noqa: BLE001 — best-effort, never break claim-verify
        logger.warning(
            "promote_merchant_skus_to_claimed failed for %s: %s",
            merchant_id, str(exc)[:200],
        )
        return 0
