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
    monkeypatch.setattr(app_settings, "outbound_warm_handoff_enabled", True)
    return app_settings


async def _ask(gate, *, dest=DEST, signed_market="US"):
    return await gate.warm_fact(dest, signed_market=signed_market)


@pytest.mark.parametrize("signed_market", ["CA", "*", "", None, "USA"])
async def test_a_token_signed_with_another_market_is_never_stamped_and_never_asked(
    armed, fact_reads, signed_market
):
    """be#2729 delta P3-A: the mint asks the REQUEST market, the token is signed with the row's
    listing market, and the click re-checks the token's. A CA-listed row on a US request must not
    be stamped off the US fact (the click would then re-check CA), and a `'*'` row never."""
    from routes.agent_shop_gateway import _CartPurchasabilityGate

    calls, _ = fact_reads
    assert await _ask(_CartPurchasabilityGate("US"), signed_market=signed_market) is None
    assert calls == []


async def test_a_token_signed_with_the_request_market_in_another_case_is_stamped(armed, fact_reads):
    from routes.agent_shop_gateway import _CartPurchasabilityGate

    calls, _ = fact_reads
    assert await _ask(_CartPurchasabilityGate("US"), signed_market=" us ") == warm.WARM_FACT_HUMAN_HANDOFF
    assert calls == [("not-on-the-list.com", "US")]


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


async def test_keyed_on_the_destination_host_only(armed, fact_reads):
    """The click lane builds its cart on the DESTINATION host (`brand_domain = host(dest)`), so
    the stamp asks about that host and no other. The seed's `domain` column can name another
    store, and a fact about that one must never authorize a cart on this one (be#2729 review P1)."""
    from routes.agent_shop_gateway import _CartPurchasabilityGate

    calls, _ = fact_reads
    await _ask(_CartPurchasabilityGate("US"), dest="https://shop.brand-b.com/products/x")
    assert calls == [("shop.brand-b.com", "US")]


async def test_the_gate_takes_no_store_domain_at_all():
    """Structural pin: there is no `shop_domain` input left to key on by mistake."""
    import inspect

    from routes.agent_shop_gateway import _CartPurchasabilityGate

    assert list(inspect.signature(_CartPurchasabilityGate.warm_fact).parameters) == ["self", "destination_url", "signed_market"]


async def test_the_flag_off_never_asks(armed, fact_reads, monkeypatch):
    from routes.agent_shop_gateway import _CartPurchasabilityGate

    monkeypatch.setattr(armed, "outbound_warm_handoff_fact_eligible", False)
    calls, _ = fact_reads
    assert await _ask(_CartPurchasabilityGate("US")) is None
    assert calls == []


async def test_the_warm_lane_off_never_asks(armed, fact_reads, monkeypatch):
    from routes.agent_shop_gateway import _CartPurchasabilityGate

    monkeypatch.setattr(armed, "outbound_warm_handoff_enabled", False)
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


# ---- the flag's default ---------------------------------------------------------------------------


def test_the_flag_defaults_off():
    """Merging deploys: the flag MUST be off unless the env names it. Checked in a fresh
    interpreter: the default is read at import, and reloading `config.settings` here would leave
    every module that already imported `settings` holding a stale object."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    env = {k: v for k, v in os.environ.items() if k != "OUTBOUND_WARM_HANDOFF_FACT_ELIGIBLE"}
    probe = "from config.settings import settings; print(settings.outbound_warm_handoff_fact_eligible)"
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip().splitlines()[-1] == "False"


# ---- which seed links may be asked --------------------------------------------------------------


_BASE = dict(
    has_cart=False,
    named_without_cart=False,
    purchasability_tier=None,
    stock_declined=False,
    live_verification_required=False,
    preflight_refused=False,
    in_stock=True,
    dest=DEST,
)


def test_a_plain_cold_link_may_be_asked():
    from routes.agent_shop_gateway import _warm_fact_skip_reason

    assert _warm_fact_skip_reason(**_BASE) is None
    assert _warm_fact_skip_reason(**{**_BASE, "in_stock": None}) is None, "unknown stock is not a decline"


@pytest.mark.parametrize(
    "override, reason",
    [
        ({"has_cart": True}, "cart"),
        ({"named_without_cart": True}, "named_variant"),
        ({"purchasability_tier": "browse_only"}, "purchasability_declined"),
        ({"stock_declined": True}, "stock_declined"),
        ({"live_verification_required": True}, "live_verification"),
        ({"preflight_refused": True}, "preflight_refused"),
        ({"in_stock": False}, "out_of_stock"),
        ({"dest": "https://click.linksynergy.com/deeplink?murl=x"}, "affiliate"),
    ],
    ids=["cart", "named", "tier", "stock", "live", "preflight", "oos", "affiliate"],
)
def test_every_skip_arm(override, reason):
    from routes.agent_shop_gateway import _warm_fact_skip_reason

    assert _warm_fact_skip_reason(**{**_BASE, **override}) == reason


# ---- the click-time re-check ------------------------------------------------------------------------


async def test_the_click_time_recheck_asks_the_destination_host_and_the_token_market(fact_reads):
    calls, answer = fact_reads
    assert await warm.warm_fact_recheck(dest="https://www.brand-b.com/products/x?utm=1", market="US") is None
    assert calls == [("brand-b.com", "US")]
    answer["value"] = False
    assert await warm.warm_fact_recheck(dest=DEST, market="US") == warm.FACT_LAPSED


@pytest.mark.parametrize("market", [None, ""])
async def test_the_click_time_recheck_without_a_market_refuses_without_asking(fact_reads, market):
    calls, _ = fact_reads
    assert await warm.warm_fact_recheck(dest=DEST, market=market) == warm.FACT_NO_MARKET
    assert calls == []


async def test_the_click_time_recheck_without_a_host_refuses_without_asking(fact_reads):
    calls, _ = fact_reads
    assert await warm.warm_fact_recheck(dest="not a url", market="US") == warm.FACT_NO_HOST
    assert calls == []


async def test_the_click_time_recheck_fails_closed_on_a_read_error(monkeypatch):
    import db.merchant_purchasability as mp

    async def boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(mp, "human_handoff_allowed", boom)
    assert await warm.warm_fact_recheck(dest=DEST, market="US") == warm.FACT_LAPSED


async def test_the_click_time_recheck_has_its_own_deadline(monkeypatch):
    """be#2729 delta P3-B: the read runs before the 302 a buyer is waiting on, so a stalled pool
    must not hold the redirect. A read that outlives the deadline keeps the cold redirect."""
    import asyncio

    import db.merchant_purchasability as mp

    async def stalled(*_a, **_k):
        await asyncio.sleep(30)
        return True

    monkeypatch.setattr(mp, "human_handoff_allowed", stalled)
    monkeypatch.setattr(warm, "WARM_FACT_RECHECK_TIMEOUT_S", 0.05)
    loop = asyncio.get_running_loop()
    started = loop.time()
    assert await warm.warm_fact_recheck(dest=DEST, market="US") == warm.FACT_TIMEOUT
    assert loop.time() - started < 5


def test_the_recheck_deadline_is_short():
    assert 0 < warm.WARM_FACT_RECHECK_TIMEOUT_S <= 0.5
