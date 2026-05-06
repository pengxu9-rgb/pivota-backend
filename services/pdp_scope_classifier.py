"""Pure-function classifier for catalog_products.pdp_scope.

Phase 6 of the PDP-as-canonical recall migration. The matcher and recall
need to know whether a PDP is shared across merchants (canonical) or
exclusive to one merchant (merchant_owned). See mig 070 for the column
contract.

Decision rule, in priority order:

  1. category_label_source == 'enrichment_agent_v1'
       → 'multi_merchant_canonical'
       (the agent's intent is to author canonical industry products;
        even if only one merchant has been validated this run, the PDP's
        nature is canonical and future runs will add more offers)

  2. seller_count >= 2
       (i.e. distinct merchants offering the same product_key, summed
        across catalog_offers + external_product_seeds)
       → 'multi_merchant_canonical'

  3. Otherwise → 'merchant_owned'
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

SCOPE_CANONICAL = "multi_merchant_canonical"
SCOPE_MERCHANT_OWNED = "merchant_owned"
SCOPE_UNVERIFIED = "unverified"

LABEL_SOURCE_BACKFILL = "backfill_2026_05"
LABEL_SOURCE_ENRICHMENT = "enrichment_agent_v1"
LABEL_SOURCE_MERCHANT_SYNC = "merchant_sync"
LABEL_SOURCE_MANUAL = "manual_review"


@dataclass(frozen=True)
class ScopeSignals:
    """All inputs the classifier needs from a single catalog_products row.

    seller_count is the count of distinct merchants observed across
    catalog_offers and external_product_seeds linked to this product_key
    (the catalog_products row's own merchant_id is included).
    """

    category_label_source: Optional[str]
    seller_count: int


def classify(signals: ScopeSignals) -> str:
    """Return the pdp_scope value for the given signals."""
    if signals.category_label_source == LABEL_SOURCE_ENRICHMENT:
        return SCOPE_CANONICAL
    if signals.seller_count >= 2:
        return SCOPE_CANONICAL
    return SCOPE_MERCHANT_OWNED
