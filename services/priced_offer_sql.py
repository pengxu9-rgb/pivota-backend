"""Single source of truth for "does THIS catalog_products row carry a real price?".

Three consumers have to agree on this question or the catalog develops a
split-brain that only shows up as an invariant violation days later:

  * ``services/index_pipeline_state_service`` — the ``has_price`` signal that
    gates ``index_pipeline_state.serving_eligible``;
  * ``services/catalog_row_trust_upserter`` — the per-row input behind the
    ``OFFER_PRICE_MISSING`` gate in ``services/catalog_trust_policy``;
  * ``services/catalog_invariant_checks`` — ``public_without_priced_offer``,
    the check that fails the build when the first two get it wrong.

They diverged once already. The invariant asked ``co.list_price > 0`` with no
suppression conjunct while ``has_price`` asked ``suppressed_at IS NULL AND
list_price > 0``; the two happened to agree on prod, but nothing made them.

WHAT COUNTS AS PRICED (and why):

  * ``suppressed_at IS NULL`` — a suppressed offer is withdrawn supply. It must
    not keep a row public, which is exactly what the un-suppressed half of the
    2026-07-30 currency remediation left behind.
  * ``coalesce(merchant_effective_price, list_price) > 0`` — the same
    coalesce order the served surface prints. ``estimated_best_price`` is
    DELIBERATELY EXCLUDED: it is a derived estimate, not a merchant-quoted
    price, and a PDP must not be published on the strength of our own guess.

    Measured on prod 2026-07-31: 0 unsuppressed offers are priced by
    ``merchant_effective_price`` while ``list_price`` is null-or-zero, so
    coalescing is a no-op on today's corpus and changes no row's verdict. It
    is here so the predicate stays right the day a writer populates only the
    effective price — a rewrite of the price columns is precisely how this
    class of defect arrives (PR #1647, the 2026-07-30 drain).

    > 0 rather than IS NOT NULL: a 0.00 price is not buyable either, and the
    invariant this backs has always asked ``> 0``.

The 2026-07-30 remediation NULLed prices on placeholder/discontinued offers
WITHOUT suppressing the offer rows, so an unsuppressed offer carrying no price
at all is a real and recurring state — 432 such rows on prod 2026-07-31. It is
not an anomaly to code around; it is the honest representation of "we do not
know this price", and every gate here must read it as "not buyable".
"""

from __future__ import annotations

from typing import Tuple

# The price expression itself, exposed separately so a caller that needs it in a
# SELECT list (rather than an EXISTS) cannot re-spell it by hand.
PRICED_OFFER_PRICE_EXPR = "coalesce({alias}.merchant_effective_price, {alias}.list_price)"


def priced_offer_price_expr(*, alias: str = "co") -> str:
    """The buyable-price expression for one ``catalog_offers`` row."""
    return PRICED_OFFER_PRICE_EXPR.format(alias=alias)


def priced_offer_row_conjuncts(*, alias: str = "co") -> Tuple[str, str]:
    """The two per-ROW conjuncts that make a ``catalog_offers`` row served supply.

    ``priced_offer_exists_sql`` below is these two conjuncts plus the
    correlated ``product_key`` join, and it is built from this function so the
    two spellings cannot diverge. Exposed separately because not every consumer
    asks the question at PRODUCT grain: ADR-024's market/currency invariant
    (``services/catalog_invariant_checks``) asks it at OFFER grain — "which
    served offers disagree with their market" — and an EXISTS cannot answer
    that. The whole reason this module exists is that the same rule got spelled
    twice and drifted; a second grain must not become a second spelling.

    Returned as separate conjuncts rather than one joined string so each caller
    supplies its own indentation and no whitespace is baked in here.
    """
    return (
        f"{alias}.suppressed_at IS NULL",
        f"{priced_offer_price_expr(alias=alias)} > 0",
    )


def priced_offer_exists_sql(
    product_key_expr: str,
    *,
    alias: str = "co",
    extra_predicate: str = "",
) -> str:
    """``EXISTS`` over ``catalog_offers`` for one product_key's real price.

    ``product_key_expr`` is SQL, not a value — pass the correlated column
    (``cp.product_key``, ``crt.subject_key``), never a bind parameter name.

    ``extra_predicate`` is appended as an extra ``AND`` conjunct for callers
    that narrow further (the US-buyable signal adds a currency test). It must
    be a literal SQL fragment; it is never a place to interpolate user input.

    Emits ``EXISTS`` and not ``(SELECT TRUE ... LIMIT 1)`` on purpose: EXISTS is
    TRUE/FALSE and never NULL, so an ABSENT key in a consumer's row dict
    unambiguously means "this query did not compute it" rather than "no priced
    offer". The trust policy's tri-state gate depends on that distinction.
    """
    extra = f"\n          AND {extra_predicate}" if extra_predicate else ""
    row = "".join(
        f"\n          AND {conjunct}"
        for conjunct in priced_offer_row_conjuncts(alias=alias)
    )
    return (
        "EXISTS (\n"
        "        SELECT 1\n"
        f"        FROM catalog_offers {alias}\n"
        f"        WHERE {alias}.product_key = {product_key_expr}"
        f"{row}"
        f"{extra}\n"
        "    )"
    )
