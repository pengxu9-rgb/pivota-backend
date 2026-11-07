"""FACT-DRIVEN warm eligibility (OUTBOUND_WARM_HANDOFF_FACT_ELIGIBLE).

The click lane upgrades a cold PDP redirect into a prefilled cart only for hand-allowlisted brands.
With the flag on, a token the mint signed `warm_fact: human_handoff` is eligible too: offers.resolve
asked `db.merchant_purchasability.human_handoff_allowed` for that cold link's store and the buyer's
market and got a fresh yes.

Pinned here, against the real `evaluate_warm_eligibility`, `could_upgrade_at_click_time` and
`_CartPurchasabilityGate.warm_fact` (only the fact read is faked):
  * the stamp only ever WIDENS (allowlist and rollout behave exactly as before);
  * it does nothing with the flag off, and nothing without the exact stamp value;
  * every knockout still beats it (already-a-cart, purchase/stock/variant declines, live
    verification, bot, affiliate, missing key);
  * the resolve-time answer and the click-time answer agree;
  * the gate asks only with flag + dial + market, fails closed, keys like the cart minter, and
    shares the per-request memo.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import services.outbound_warm_handoff as warm

HUMAN_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
DEST = "https://www.not-on-the-list.com/products/serum"
STAMP = {warm.WARM_FACT_KEY: warm.WARM_FACT_HUMAN_HANDOFF, "join_mode": "referral_only"}


def _settings(*, fact: bool = True, brands=("cosrx.com",), pct: int = 0, key: str = "k", enabled: bool = True):
    return SimpleNamespace(
        outbound_warm_handoff_enabled=enabled,
        outbound_warm_handoff_internal_key=key,
        outbound_warm_handoff_brands=frozenset(brands),
        outbound_warm_handoff_rollout_pct=pct,
        outbound_warm_handoff_fact_eligible=fact,
    )


def _eligible(ctx, *, dest=DEST, ua=HUMAN_UA, **kw):
    return warm.evaluate_warm_eligibility(dest=dest, user_agent=ua, token="tok", ctx=ctx, settings=_settings(**kw))


# ---- the rule -------------------------------------------------------------------------------------


def test_a_stamped_token_outside_the_allowlist_is_eligible_by_fact():
    assert _eligible(STAMP) == (True, "fact")


def test_the_flag_off_ignores_the_stamp():
    assert _eligible(STAMP, fact=False) == (False, "not_allowlisted")


def test_no_stamp_means_the_allowlist_alone_decides():
    assert _eligible({"join_mode": "referral_only"}) == (False, "not_allowlisted")


@pytest.mark.parametrize("value", ["", "yes", "true", "browse_only", "human", None])
def test_only_the_exact_stamp_value_counts(value):
    assert _eligible({warm.WARM_FACT_KEY: value}) == (False, "not_allowlisted")


def test_an_allowlisted_host_keeps_its_own_reason():
    assert _eligible(STAMP, dest="https://www.cosrx.com/products/x") == (True, "allowlisted")


def test_with_no_allowlist_the_stamp_beats_the_rollout_control():
    assert _eligible(STAMP, brands=(), pct=0) == (True, "fact")
    assert _eligible({}, brands=(), pct=0) == (False, "control")
    assert _eligible({}, brands=(), pct=100) == (True, "rollout")


@pytest.mark.parametrize(
    "extra, reason",
    [
        ({"join_mode": "cart_permalink"}, "already_cart"),
        ({warm.PURCHASABILITY_TIER_KEY: warm.PURCHASABILITY_BROWSE_ONLY}, "purchase_declined"),
        ({warm.STOCK_DECLINE_KEY: warm.STOCK_OUT_OF_STOCK}, "stock_declined"),
        ({warm.LIVE_VERIFICATION_KEY: warm.LIVE_VERIFICATION_REQUIRED}, "live_verification_required"),
        ({warm.VARIANT_DECLINE_KEY: warm.VARIANT_NOT_NAMED}, "variant_declined"),
    ],
)
def test_every_mint_knockout_still_beats_the_stamp(extra, reason):
    assert _eligible({**STAMP, **extra}) == (False, reason)


def test_a_cart_shaped_dest_beats_the_stamp():
    assert _eligible(STAMP, dest="https://www.not-on-the-list.com/cart/123:1") == (False, "already_cart")


def test_a_bot_an_affiliate_link_and_a_missing_key_all_beat_the_stamp():
    assert _eligible(STAMP, ua="facebookexternalhit/1.1") == (False, "bot")
    assert _eligible(STAMP, dest="https://click.linksynergy.com/deeplink?murl=x") == (False, "affiliate")
    assert _eligible(STAMP, key="") == (False, "no_internal_key")


@pytest.mark.parametrize("ctx, fact, expected", [(STAMP, True, True), (STAMP, False, False), ({}, True, False)])
def test_resolve_time_and_click_time_agree(ctx, fact, expected):
    """`cart_prefilled` is answered from `could_upgrade_at_click_time`; a click with a human UA
    must get the same verdict, or the published claim lies."""
    s = _settings(fact=fact)
    resolve_time = warm.could_upgrade_at_click_time(dest=DEST, token="tok", ctx=ctx, settings=s)
    click_time, _ = warm.evaluate_warm_eligibility(dest=DEST, user_agent=HUMAN_UA, token="tok", ctx=ctx, settings=s)
    assert resolve_time is click_time is expected


# ---- the mint-side gate ------------------------------------------------------------------------------


@pytest.fixture
def fact_reads(monkeypatch):
    import db.merchant_purchasability as mp

    calls: list = []
    answer = {"value": True}

    async def fake_human_handoff_allowed(domain, market):
        calls.append((domain, market))
        return answer["value"]

    monkeypatch.setattr(mp, "human_handoff_allowed", fake_human_handoff_allowed)
    return calls, answer


@pytest.fixture
def armed(monkeypatch):
    from config.settings import settings as app_settings

    monkeypatch.setenv("MERCHANT_PURCHASABILITY_ENFORCE", "1")
    monkeypatch.setattr(app_settings, "outbound_warm_handoff_fact_eligible", True)
    return app_settings


async def _ask(gate, *, dest=DEST, shop_domain=None):
    return await gate.warm_fact(destination_url=dest, shop_domain=shop_domain)


async def test_a_fresh_fact_is_stamped(armed, fact_reads):
    from routes.agent_shop_gateway import _CartPurchasabilityGate

    calls, _ = fact_reads
    assert await _ask(_CartPurchasabilityGate("US")) == warm.WARM_FACT_HUMAN_HANDOFF
    assert calls == [("not-on-the-list.com", "US")]


async def test_no_fact_no_stamp(armed, fact_reads):
    from routes.agent_shop_gateway import _CartPurchasabilityGate

    calls, answer = fact_reads
    answer["value"] = False
    assert await _ask(_CartPurchasabilityGate("US")) is None
    assert len(calls) == 1


async def test_keyed_like_the_cart_minter_shop_domain_first(armed, fact_reads):
    """`resolve_cart_permalink` builds the cart on `shop_domain or destination_url`, and the sweep
    records facts for the hosts the cart minter builds on, so the stamp asks the same host."""
    from routes.agent_shop_gateway import _CartPurchasabilityGate

    calls, _ = fact_reads
    await _ask(_CartPurchasabilityGate("US"), shop_domain="brand-store.myshopify.com")
    assert calls == [("brand-store.myshopify.com", "US")]


async def test_the_flag_off_never_asks(armed, fact_reads, monkeypatch):
    from routes.agent_shop_gateway import _CartPurchasabilityGate

    monkeypatch.setattr(armed, "outbound_warm_handoff_fact_eligible", False)
    calls, _ = fact_reads
    assert await _ask(_CartPurchasabilityGate("US")) is None
    assert calls == []


async def test_the_dial_off_never_asks(armed, fact_reads, monkeypatch):
    from routes.agent_shop_gateway import _CartPurchasabilityGate

    monkeypatch.delenv("MERCHANT_PURCHASABILITY_ENFORCE", raising=False)
    calls, _ = fact_reads
    assert await _ask(_CartPurchasabilityGate("US")) is None
    assert calls == []


@pytest.mark.parametrize("market", [None, "", "USA", "zz-1"])
async def test_no_usable_market_never_asks(armed, fact_reads, market):
    from routes.agent_shop_gateway import _CartPurchasabilityGate

    calls, _ = fact_reads
    assert await _ask(_CartPurchasabilityGate(market)) is None
    assert calls == []


async def test_one_read_per_host_and_the_memo_is_shared_with_the_cart_gate(armed, fact_reads):
    from routes.agent_shop_gateway import _CartPurchasabilityGate

    calls, _ = fact_reads
    gate = _CartPurchasabilityGate("US")
    await _ask(gate)
    await _ask(gate, dest="https://www.not-on-the-list.com/products/other")
    assert await gate.allows_cart("https://not-on-the-list.com/cart/1:1") is True
    assert calls == [("not-on-the-list.com", "US")]
    assert gate.stats == {"asked": 0, "declined": 0}, "a cold link was never a cart to decline"


async def test_a_database_error_stamps_nothing(armed, monkeypatch):
    """`human_handoff_allowed` fails closed on a database error; the real reader is used here."""
    import db.merchant_purchasability as mp
    from routes.agent_shop_gateway import _CartPurchasabilityGate

    async def boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(mp.database, "fetch_one", boom)
    assert await _ask(_CartPurchasabilityGate("US")) is None
