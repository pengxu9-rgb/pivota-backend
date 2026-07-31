"""The priced-offer predicate, pinned byte-for-byte to the Node twin.

DELIBERATELY NOT IN tests/test_priced_offer_gate_postgres.py: that file skips
itself unless DATABASE_URL is Postgres, and a cross-repo pin that only runs on
the dialect gate is a pin that is off most of the time. These assertions need no
database and must run on every suite.

WHY THE PIN EXISTS. This repo and PIVOTA-Agent both write ONE `catalog_row_trust`
table against one Postgres. A predicate edited in one repo only is a split-brain
with no flag to blame: this backend re-derives on a 6h cron, the twin re-derives
on every live-read promotion and identity override, and rows FLAP
public<->blocked on the live serving surface. No runtime check catches it — each
side is individually self-consistent. So the suites pin each other, exactly as
tests/test_pdp_renderability.py does for the seed-route fragment.
"""

from __future__ import annotations

from services.priced_offer_sql import (
    priced_offer_exists_sql,
    priced_offer_price_expr,
)

# The literal both twins must emit, byte for byte. PIVOTA-Agent
# tests/priced_offer_sql.node.test.cjs asserts the SAME string against
# src/services/pricedOfferSql.pricedOfferExistsSql('cp.product_key'), so the two
# suites fail together the moment either repo edits the fragment alone.
PRICED_OFFER_EXISTS_CP = "\n".join(
    [
        "EXISTS (",
        "        SELECT 1",
        "        FROM catalog_offers co",
        "        WHERE co.product_key = cp.product_key",
        "          AND co.suppressed_at IS NULL",
        "          AND coalesce(co.merchant_effective_price, co.list_price) > 0",
        "    )",
    ]
)


def test_priced_offer_fragment_is_byte_identical_to_the_node_twin():
    assert priced_offer_exists_sql("cp.product_key") == PRICED_OFFER_EXISTS_CP


def test_price_expression_coalesces_effective_over_list():
    assert priced_offer_price_expr() == (
        "coalesce(co.merchant_effective_price, co.list_price)"
    )


def test_estimated_best_price_is_excluded():
    """It is OUR estimate, not a merchant quote.

    A PDP must not be published on the strength of a guess — 73 of the 77
    offer-free sitemap rows surfaced a wrong-currency price from exactly that
    kind of derived field (see services/pdp_renderability).
    """
    assert "estimated_best_price" not in priced_offer_exists_sql("cp.product_key")


def test_suppression_conjunct_is_present():
    """`public_without_priced_offer` was hand-spelled WITHOUT this for months
    while `has_price` always had it. They agreed on prod by luck, not by
    construction. A suppressed offer is withdrawn supply."""
    assert "co.suppressed_at IS NULL" in priced_offer_exists_sql("cp.product_key")


def test_price_test_is_greater_than_zero_not_is_not_null():
    """A 0.00 price is not buyable either, and the invariant this backs has
    always asked `> 0`."""
    sql = priced_offer_exists_sql("cp.product_key")
    assert "> 0" in sql
    assert "IS NOT NULL" not in sql


def test_exists_is_correlated_to_the_outer_row():
    """THE REGRESSION GUARD.

    `co.product_key = cp.product_key` is what makes this a PER-ROW answer.
    Correlate it to `cp.content_key` instead and you have rebuilt the exact
    content-grained leak this predicate exists to close — a priced sibling would
    launder the price-less row all over again, which is how 4 price-less Tom
    Ford PDPs reached the public surface on 2026-07-31.
    """
    sql = priced_offer_exists_sql("cp.product_key")
    assert "co.product_key = cp.product_key" in sql
    assert "FROM catalog_products" not in sql


def test_alias_is_overridable_without_breaking_correlation():
    sql = priced_offer_exists_sql("cp.product_key", alias="co2")
    assert "FROM catalog_offers co2" in sql
    assert "co2.product_key = cp.product_key" in sql
    assert "co2.suppressed_at IS NULL" in sql


def test_extra_predicate_is_appended_as_an_and_conjunct():
    sql = priced_offer_exists_sql(
        "cp.product_key",
        extra_predicate="upper(trim(coalesce(co.currency, ''))) = 'USD'",
    )
    assert "AND upper(trim(coalesce(co.currency, ''))) = 'USD'" in sql
    assert sql.rstrip().endswith(")")
