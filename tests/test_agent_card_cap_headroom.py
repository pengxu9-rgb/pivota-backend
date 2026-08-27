"""A card capped at a pre-address quote is declined the moment an address is entered.

That is the whole reason this policy exists. §B7 measured that a pre-address UCP checkout returns
`total === subtotal`, `shipping_options: []` and `tax: null`, because Shopify collects the
delivery address on the STOREFRONT. In the Minds/Reap model the agent's one job is to enter that
address — so `cap == quote, exactly` guarantees a decline on the primary flow.

Headroom is NOT a fudge factor and must not become one. Two properties keep it honest, and both
are pinned below:

  * it is paid ONLY for components the merchant did not quote — a landed total gets none;
  * it is BOUNDED, and the ceiling is what actually limits the blast radius. What an agent could
    overspend is `max_minor`, once, at ONE merchant, on a single-use merchant-locked card.

Migration 201 anticipated exactly this and set the terms: keeping `quote_total_minor` and
`amount_cap_minor` as separate audited columns makes the policy "a visible delta between two
audited numbers instead of a silent multiplier in code". The defaults here are NOT measured, and
that delta beside a `card_rail_outcomes` decline is how they become measurable.
"""

from __future__ import annotations

import pytest

from services.agent_card_issuance import cap_for_quote, headroom_policy, quote_covers


def _quote(total_minor: int, **kw):
    return {"total_minor": total_minor, "currency": "USD", **kw}


# ------------------------------------------------------------------ what the quote already covers

@pytest.mark.parametrize(
    "payload,expected",
    [
        ({}, {"shipping": False, "tax": False}),
        ({"totals": [{"type": "total", "amount": "2317"}]}, {"shipping": False, "tax": False}),
        ({"totals": [{"type": "shipping", "amount": "500"}]}, {"shipping": True, "tax": False}),
        ({"totals": [{"type": "tax", "amount": "190"}]}, {"shipping": False, "tax": True}),
        (
            {"totals": [{"type": "shipping", "amount": "500"}, {"type": "tax", "amount": "190"}]},
            {"shipping": True, "tax": True},
        ),
        ({"total_shipping": "500", "total_tax": "190"}, {"shipping": True, "tax": True}),
    ],
)
def test_coverage_is_read_positively(payload, expected):
    assert quote_covers(payload) == expected


def test_a_null_amount_is_the_merchant_DECLINING_TO_SAY_not_a_zero():
    """The live path's shape exactly: `tax: null`. Treating null as "quoted, and it's zero" would
    read a pre-address checkout as landed and remove the headroom on the one case that needs it."""
    assert quote_covers({"totals": [{"type": "tax", "amount": None}]})["tax"] is False
    assert quote_covers({"total_tax": None})["tax"] is False


@pytest.mark.parametrize(
    "payload",
    [
        {"totals": [{"type": "shipping", "amount": 0}]},   # via the totals[] index
        {"total_shipping": 0},                              # via a top-level key
        {"totals": [{"type": "tax", "amount": 0}]},
        {"total_tax": 0},
    ],
)
def test_a_zero_amount_IS_a_quote(payload):
    """Free shipping, and zero-rated tax, are real answers. `0` is the merchant saying so;
    absent is not.

    BOTH LOOKUP PATHS are covered on purpose. `_named` checks the top-level key and the
    `totals[]` index separately, and a mutant that swapped `is not None` for truthiness on only
    the first survived a version of this test that exercised only the second — free shipping
    quoted as a top-level `total_shipping: 0` would then have silently stopped counting as
    covered, buying headroom on a quote that was already landed.
    """
    covers = quote_covers(payload)
    assert covers["shipping"] is True or covers["tax"] is True


# ------------------------------------------------------------------------------ the cap itself

def test_a_landed_quote_is_left_exactly_alone():
    out = cap_for_quote(_quote(2317, covers_shipping=True, covers_tax=True))
    assert out == {"amount_cap_minor": 2317, "headroom_minor": 0, "headroom_basis": "quote_is_landed"}


@pytest.mark.parametrize("covers_shipping,covers_tax", [(False, False), (True, False), (False, True)])
def test_anything_less_than_fully_landed_gets_headroom(covers_shipping, covers_tax):
    """Covering one component does not cover the other: a cap short by the tax declines exactly
    as one short by the shipping."""
    out = cap_for_quote(_quote(2317, covers_shipping=covers_shipping, covers_tax=covers_tax))
    assert out["headroom_minor"] == 1500 + (2317 * 1200) // 10_000 == 1778
    assert out["amount_cap_minor"] == 2317 + 1778


def test_the_flat_component_is_what_carries_a_cheap_order():
    """12% of a $10 order is $1.20, which does not pay for $8 of shipping. A percentage-only
    policy declines the cheapest orders — which for this cohort is a lot of them."""
    out = cap_for_quote(_quote(1000))
    assert out["headroom_minor"] == 1500 + 120


def test_the_ceiling_is_what_bounds_the_blast_radius():
    """On a large order the percentage runs away; `max_minor` is the number that actually limits
    what a compromised or buggy agent could spend beyond the quote."""
    out = cap_for_quote(_quote(1_000_000))  # $10,000
    assert out["headroom_minor"] == 7500
    assert out["headroom_basis"] == "ceiling"
    assert out["amount_cap_minor"] == 1_000_000 + 7500


def test_the_arithmetic_is_integer_minor_units_throughout():
    """A float here reintroduces exactly the rounding the minor-unit convention removes."""
    out = cap_for_quote(_quote(333))
    assert isinstance(out["headroom_minor"], int)
    assert isinstance(out["amount_cap_minor"], int)
    assert out["headroom_minor"] == 1500 + (333 * 1200) // 10_000  # floor, not round


def test_the_cap_is_never_below_the_quote():
    for total in (1, 333, 2317, 1_000_000):
        out = cap_for_quote(_quote(total))
        assert out["amount_cap_minor"] >= total
        assert out["headroom_minor"] >= 0


# ------------------------------------------------------------------------------------ the policy

def test_the_policy_is_configurable(monkeypatch):
    monkeypatch.setenv("AGENT_CARD_HEADROOM_BPS", "500")
    monkeypatch.setenv("AGENT_CARD_HEADROOM_FLAT_MINOR", "200")
    monkeypatch.setenv("AGENT_CARD_HEADROOM_MAX_MINOR", "1000")
    assert headroom_policy() == {"bps": 500, "flat_minor": 200, "max_minor": 1000}
    assert cap_for_quote(_quote(2000))["headroom_minor"] == 200 + 100


def test_zero_headroom_can_be_configured_back_to_v1(monkeypatch):
    """An operator must be able to switch the policy off without a deploy — and get exactly the
    old behaviour, not something near it."""
    for var in ("AGENT_CARD_HEADROOM_BPS", "AGENT_CARD_HEADROOM_FLAT_MINOR", "AGENT_CARD_HEADROOM_MAX_MINOR"):
        monkeypatch.setenv(var, "0")
    out = cap_for_quote(_quote(2317))
    assert out["amount_cap_minor"] == 2317
    assert out["headroom_minor"] == 0


@pytest.mark.parametrize("bad", ["", "  ", "abc", "-1", "1.5"])
def test_a_malformed_policy_value_falls_back_to_the_default(monkeypatch, bad):
    """Fail to the published default, never to zero (silent declines) or to something unbounded."""
    monkeypatch.setenv("AGENT_CARD_HEADROOM_MAX_MINOR", bad)
    assert headroom_policy()["max_minor"] == 7500
