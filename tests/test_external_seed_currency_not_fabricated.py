"""A seed with no recorded currency must carry NONE, not a fabricated "USD".

`_external_seed_to_shop_product` ended its currency chain in `or "USD"`, so a
seed that no source had ever assigned a currency was published as US dollars.
Two harms, and the second is the one that lasts:

  1. the row asserts a currency nobody observed; and
  2. the field is ALWAYS populated, so nothing downstream can ever ask "what
     currency is this actually?" — the fallback does not just emit a wrong
     value, it destroys the question.

Measured on prod 2026-07-30 before the removal: 159 of 11,381 active seeds have
no currency, and **zero** of those carry a price. So this was latent rather than
a live wrong-price bug — which is exactly why it was safe to remove, and why a
zero delta after deploy is the expected result rather than a sign the change
did nothing.

⚠️ An honest `null` has a blast radius (#1819: `Number(x ?? 0)` turned a null
price into 0 and silently dropped in-stock products). The consumer check for
THIS field is `isQuotableFeedItem`, which requires a non-empty STRING currency —
so a None makes the row fail the gate and be DROPPED from the public feed rather
than published with a null. Stricter, not broken. These tests pin that a
currency-less row is never quotable.
"""

from __future__ import annotations

import asyncio

import pytest

from routes.agent_shop_gateway import _budget_allows_price, _external_seed_to_shop_product


def _p(row=None, seed_data=None):
    return _external_seed_to_shop_product(
        row=row if row is not None else {"title": "X"},
        seed_data=seed_data if seed_data is not None else {"title": "X"},
        redirect_url=None,
    )


# ---------------------------------------------------------------------------
# the projection
# ---------------------------------------------------------------------------

def test_no_currency_anywhere_yields_None_not_USD():
    p = _p(row={"title": "Serum"}, seed_data={"title": "Serum"})
    assert p["currency"] is None, (
        f"fabricated a currency: {p['currency']!r}. No source asserted one."
    )


def test_a_recorded_currency_on_the_row_is_used():
    p = _p(row={"title": "Serum", "price_currency": "INR"}, seed_data={"title": "Serum"})
    assert p["currency"] == "INR"


def test_a_recorded_currency_in_seed_data_is_used():
    p = _p(row={"title": "Serum"}, seed_data={"title": "Serum", "price_currency": "GBP"})
    assert p["currency"] == "GBP"


def test_the_row_outranks_seed_data():
    p = _p(
        row={"title": "Serum", "price_currency": "INR"},
        seed_data={"title": "Serum", "price_currency": "USD"},
    )
    assert p["currency"] == "INR"


def test_a_blank_currency_is_unknown_not_blank():
    """`''` is not an assertion either — it must not survive as a falsy string
    that a `typeof x === 'string'` consumer would accept."""
    for blank in ("", "   "):
        p = _p(row={"title": "Serum", "price_currency": blank}, seed_data={})
        assert p["currency"] in (None, blank.strip() or None), (
            f"blank currency {blank!r} became {p['currency']!r}"
        )


def test_a_genuine_USD_seed_still_says_USD():
    """The removal must not make real USD rows indistinguishable from unknown."""
    p = _p(row={"title": "Serum", "price_currency": "USD"}, seed_data={})
    assert p["currency"] == "USD"


# ---------------------------------------------------------------------------
# the consumer contract: unknown currency must never become quotable
# ---------------------------------------------------------------------------

def test_a_currency_less_row_is_not_quotable_by_the_feed_gate():
    """Mirrors PIVOTA-Agent `isQuotableFeedItem`: a non-empty STRING currency is
    required, so None drops the row rather than publishing a null currency.

    Pinned here as well as there because the two repos ship separately and this
    is the contract that keeps an honest null from becoming a served defect.
    """
    p = _p(row={"title": "Serum", "price_amount": 42}, seed_data={})

    def is_quotable(item):  # the Node predicate, transcribed
        amount = item.get("price")
        if not isinstance(amount, (int, float)) or amount <= 0:
            return False
        cur = item.get("currency")
        return isinstance(cur, str) and cur.strip() != ""

    assert is_quotable(p) is False, "a currency-less row was quotable"
    priced = _p(row={"title": "Serum", "price_amount": 42, "price_currency": "USD"}, seed_data={})
    assert is_quotable(priced) is True, "the control row is not quotable — test is vacuous"


# ---------------------------------------------------------------------------
# the budget predicate must refuse, not assume
# ---------------------------------------------------------------------------

def _budget(**kw):
    return asyncio.run(_budget_allows_price(**kw))


def test_an_amount_with_no_currency_is_refused_when_a_budget_applies():
    """Falling through would compare the bare number against price_min/max as
    though it were already in the budget's currency — the same fabrication one
    layer down. ₹3,000 is not $3,000."""
    allowed, diag = _budget(
        price_amount=3000, price_currency=None,
        budget_currency="USD", price_min=None, price_max=50,
    )
    assert allowed is False
    assert diag.get("budget_currency_unknown") is True, (
        "refused without saying why — a silent drop is indistinguishable from "
        "an out-of-budget one"
    )


@pytest.mark.parametrize("pmin,pmax", [(None, 50), (10, None), (10, 50)])
def test_refused_for_any_budget_constraint(pmin, pmax):
    allowed, _ = _budget(
        price_amount=3000, price_currency=None,
        budget_currency=None, price_min=pmin, price_max=pmax,
    )
    assert allowed is False


def test_NOT_refused_when_no_budget_constraint_exists():
    """With no min, no max and no budget currency the currency is irrelevant.
    Excluding here would be a regression that quietly shrinks every unfiltered
    result set."""
    allowed, diag = _budget(
        price_amount=3000, price_currency=None,
        budget_currency=None, price_min=None, price_max=None,
    )
    assert allowed is True
    assert "budget_currency_unknown" not in diag


def test_a_known_currency_still_compares_normally():
    allowed, _ = _budget(
        price_amount=30, price_currency="USD",
        budget_currency=None, price_min=None, price_max=50,
    )
    assert allowed is True
    allowed, _ = _budget(
        price_amount=3000, price_currency="USD",
        budget_currency=None, price_min=None, price_max=50,
    )
    assert allowed is False


def test_a_row_with_no_amount_is_unaffected():
    """Only an AMOUNT with an unknown currency is uncomparable. A row with
    neither is filtered by other means and must not be dropped here."""
    allowed, diag = _budget(
        price_amount=None, price_currency=None,
        budget_currency="USD", price_min=None, price_max=50,
    )
    assert allowed is True
    assert "budget_currency_unknown" not in diag


# ---------------------------------------------------------------------------
# the resolver itself — ONE function, both projection sites
# ---------------------------------------------------------------------------
#
# Extracted after a mutation run: the first fix landed on
# `_external_seed_to_shop_product` and missed the ranked-candidate path, and
# restoring `or "USD"` on that second site passed the whole suite. Two sites
# open-coding the same chain is how the tail comes back.

def test_the_resolver_is_the_only_currency_chain_for_seed_projections():
    """Both sites must go through `_observed_currency`.

    A structural check, deliberately: driving the ranked-candidate path
    end-to-end needs the whole multi-lane fixture, and the property that matters
    is that neither site re-implements the chain.
    """
    import inspect

    import routes.agent_shop_gateway as gw

    def _code_only(src: str) -> str:
        """Strip comments — the docstrings here NAME the tail they forbid, and
        an assertion that trips on its own explanation is worse than none."""
        out = []
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            out.append(line.split("  #")[0])
        return "\n".join(out)

    for fn_name in ("_external_seed_to_shop_product",):
        src = inspect.getsource(getattr(gw, fn_name))
        assert "_observed_currency(" in src, f"{fn_name} no longer uses the shared resolver"
        assert 'or "USD"' not in _code_only(src), f"{fn_name} re-introduced the USD tail"

    # the ranked-candidate site lives inside the multi-lane inner function
    inner = _code_only(inspect.getsource(gw._handle_find_products_multi_inner))
    assert inner.count("_observed_currency(") >= 1, (
        "the ranked-candidate projection stopped using the shared resolver"
    )
    assert 'price_currency\n                or row_dict.get("price_currency")' not in inner, (
        "the ranked-candidate site re-opened its own currency chain"
    )


def test_resolver_returns_the_first_asserted_value():
    from routes.agent_shop_gateway import _observed_currency

    assert _observed_currency(None, "inr", "USD") == "INR"
    assert _observed_currency("", "  ", "gbp") == "GBP"
    assert _observed_currency(None, None) is None
    assert _observed_currency("", "   ") is None
    assert _observed_currency("usd") == "USD"


def test_resolver_never_invents_a_value():
    """The whole point. No arrangement of empties may produce a currency."""
    from routes.agent_shop_gateway import _observed_currency

    for args in [(), (None,), ("",), ("  ",), (None, "", "   ", None)]:
        assert _observed_currency(*args) is None, f"invented a currency from {args!r}"
