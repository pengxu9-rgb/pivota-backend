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

import json as _json

import pytest

from services.agent_card_issuance import (
    MerchantQuoteError,
    cap_for_quote,
    headroom_policy,
    quote_covers,
)


def _quote(total_minor: int, **kw):
    # covers_* present by default: their ABSENCE is now its own (fail-closed) case, tested below.
    base = {"total_minor": total_minor, "currency": "USD", "covers_shipping": False, "covers_tax": False}
    base.update(kw)
    return base


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


# ------------------------------------------------------ the seam nothing was executing

class _FakeResp:
    status_code = 200
    headers: "dict[str, str]" = {}

    def __init__(self, payload):
        self._payload = payload

    def _body(self):
        return {"jsonrpc": "2.0", "id": "1", "result": {"structuredContent": self._payload}}

    async def aiter_raw(self):
        # `_read_bounded` rebuilds the body from streamed bytes, so this must be the real
        # payload rather than a placeholder.
        yield _json.dumps(self._body()).encode()

    def json(self):
        return self._body()


class _FakeClient:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        return _FakeResp(self._payload)

    def stream(self, method, url, *, json=None, headers=None):
        """The transport reads through `client.stream` now, so this double must model it.

        It did not, and only the full sweep caught that: this file was outside the subset run
        while iterating on merchant_ucp_checkout, so `AttributeError: no attribute 'stream'`
        reached CI rather than the local run.
        """
        return _FakeStream(_FakeResp(self._payload))


class _FakeStream:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


async def _quote_from(monkeypatch, payload):
    """Drive the REAL resolve_merchant_quote, faking only the HTTP transport.

    The transport now lives in services/merchant_ucp_checkout.py, so the stubs go THERE — patching
    `agent_card_issuance.resolves_only_public` would silently miss, because the call is resolved
    in the other module's namespace. The profile env is required for the same reason the real
    call needs it: without `meta["ucp-agent"]["profile"]` the merchant refuses outright.
    """
    import httpx

    from services import agent_card_issuance as mod
    from services import merchant_ucp_checkout as transport

    monkeypatch.setenv("UCP_AGENT_PROFILE_URL", "https://ucp.pivota.cc/.well-known/ucp-agent")
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: _FakeClient(payload))
    # The SSRF guard is a live-DNS dependency, not the thing under test here, and it makes these
    # rows flaky. It keeps its own coverage in tests/test_agent_card_issuance.py.
    monkeypatch.setattr(transport, "resolves_only_public", lambda domain: True)
    return await mod.resolve_merchant_quote("shop.example.com", "chk_1")


@pytest.mark.asyncio
async def test_a_LANDED_ucp_quote_gets_no_headroom_end_to_end(monkeypatch):
    """THE TEST WHOSE ABSENCE HID EVERYTHING ELSE.

    `resolve_merchant_quote` had no direct test — every route test monkeypatches it — so
    `quote_covers` and `cap_for_quote` were each verified in isolation while the SEAM between
    them ran nowhere. Deleting `covers_shipping`/`covers_tax` from the returned dict, or swapping
    them, passed all 149 card-rail tests.

    The payload is spec-shaped: UCP's totals type enum is `subtotal, items_discount, discount,
    FULFILLMENT, tax, fee, total` — `shipping`/`delivery` are only `display_text` examples. An
    earlier cut matched the display words, so this landed quote read as unlanded and earned full
    headroom, which is the blanket multiplier the policy exists not to be.
    """
    quote = await _quote_from(monkeypatch, {
        "currency": "USD",
        "totals": [
            {"type": "subtotal", "amount": 2317, "display_text": "Subtotal"},
            {"type": "fulfillment", "amount": 800, "display_text": "Shipping"},
            {"type": "tax", "amount": 190, "display_text": "Tax"},
            {"type": "total", "amount": 3307},
        ],
    })

    assert quote["covers_shipping"] is True, "the spec calls shipping `fulfillment`"
    assert quote["covers_tax"] is True
    out = cap_for_quote(quote)
    assert out["headroom_minor"] == 0
    assert out["amount_cap_minor"] == quote["total_minor"] == 3307


@pytest.mark.asyncio
async def test_the_live_pre_address_shape_DOES_get_headroom_end_to_end(monkeypatch):
    """The other half of the seam: B7's measured shape — a bare subtotal, tax null."""
    quote = await _quote_from(monkeypatch, {
        "currency": "USD",
        "tax": None,
        "totals": [
            {"type": "subtotal", "amount": 2317},
            {"type": "total", "amount": 2317},
        ],
    })

    assert quote["covers_shipping"] is False
    assert quote["covers_tax"] is False
    out = cap_for_quote(quote)
    assert out["headroom_minor"] == 1778
    assert out["amount_cap_minor"] == 4095


@pytest.mark.asyncio
async def test_the_coverage_flags_are_not_swapped(monkeypatch):
    """A mutant that swapped them survived the whole suite, because no test ever saw a payload
    that covered one component and not the other THROUGH resolve_merchant_quote."""
    quote = await _quote_from(monkeypatch, {
        "currency": "USD",
        "totals": [
            {"type": "subtotal", "amount": 2317},
            {"type": "fulfillment", "amount": 800},
            {"type": "total", "amount": 3117},
        ],
    })
    assert quote["covers_shipping"] is True
    assert quote["covers_tax"] is False


@pytest.mark.asyncio
async def test_the_snapshot_records_what_the_cap_was_reasoned_from(monkeypatch):
    """`quote_snapshot` is the audit trail for a cap that no longer equals the quote. Without
    `covers` in it, nothing recoverable explains why headroom was or was not applied."""
    quote = await _quote_from(monkeypatch, {
        "currency": "USD",
        "totals": [{"type": "subtotal", "amount": 2317}, {"type": "total", "amount": 2317}],
    })
    assert quote["quote_snapshot"]["covers"] == {"shipping": False, "tax": False}


# ---------------------------------------------------------------- fail-closed and bounded

def test_an_unknown_coverage_shape_gets_NO_headroom():
    """`.get()` with a falsy default handed MAXIMUM headroom to any quote missing these keys.
    On a money path the missing-key direction must be the safe one."""
    out = cap_for_quote({"total_minor": 2317, "currency": "USD"})
    assert out["amount_cap_minor"] == 2317
    assert out["headroom_basis"] == "coverage_unknown"


def test_a_non_usd_quote_gets_NO_headroom():
    """`flat_minor`/`max_minor` are raw minor units calibrated in USD. 1500 minor is $15.00, but
    ¥1,500 and ₩1,500 are entirely different amounts. Until non-USD has its own calibration the
    honest answer is v1's, which cannot overspend."""
    out = cap_for_quote(_quote(2317, currency="JPY", covers_shipping=False, covers_tax=False))
    assert out["amount_cap_minor"] == 2317
    assert out["headroom_basis"] == "currency_not_calibrated"


def test_the_cap_cannot_be_pushed_past_the_absolute_bound(monkeypatch):
    """`_MAX_CAP_MINOR` keeps a hostile merchant total inside BIGINT so it refuses cleanly
    instead of raising a Postgres 22003 as a 500 — but it is enforced on the QUOTE, and headroom
    is added afterwards. Two misconfigured env vars were enough to clear it."""
    from services.agent_card_issuance import _MAX_CAP_MINOR

    monkeypatch.setenv("AGENT_CARD_HEADROOM_FLAT_MINOR", str(10**18))
    monkeypatch.setenv("AGENT_CARD_HEADROOM_MAX_MINOR", str(10**18))
    out = cap_for_quote(_quote(_MAX_CAP_MINOR))
    assert out["amount_cap_minor"] <= _MAX_CAP_MINOR


@pytest.mark.parametrize("bad", [str(10**18), "-5"])
def test_an_out_of_range_policy_value_falls_back_to_the_default(monkeypatch, bad):
    monkeypatch.setenv("AGENT_CARD_HEADROOM_MAX_MINOR", bad)
    assert headroom_policy()["max_minor"] == 7500


# ------------------------------------------------- tolerance for non-conformant merchants

@pytest.mark.parametrize(
    "totals_type,component",
    [
        ("fulfillment", "shipping"),  # the SPEC's wire name — the one that must never be dropped
        ("shipping", "shipping"),     # the display word, used by merchants that ignore the enum
        ("delivery", "shipping"),     # the other display word the gateway's indexTotals accepts
        ("tax", "tax"),
        ("taxes", "tax"),             # plural, likewise from the gateway's alias list
    ],
)
def test_each_accepted_spelling_is_actually_accepted(totals_type, component):
    """Every alias is either load-bearing or dead weight, and an untested one is indistinguishable
    from the second. Mutants dropping `delivery` and `taxes` survived until these rows existed —
    the aliases were carried over from the gateway's `indexTotals` on faith.

    The spec permits `Businesses MAY use additional values`, so tolerating the display spellings
    is right; asserting them is what keeps that a decision rather than an accident.
    """
    covers = quote_covers({"totals": [{"type": totals_type, "amount": 500}]})
    assert covers[component] is True


@pytest.mark.parametrize("raw", ["Fulfillment", "  fulfillment  ", "FULFILLMENT", "\tTax\n"])
def test_the_totals_type_key_is_normalised(raw):
    """`type` is a free-text string in the schema, so casing and stray whitespace are the
    merchant's to choose. Dropping `.strip().lower()` survived the suite — and would have turned
    a landed `"Fulfillment"` quote into an unlanded one earning full headroom."""
    covers = quote_covers({"totals": [{"type": raw, "amount": 500}]})
    assert covers["shipping"] is True or covers["tax"] is True


@pytest.mark.parametrize(
    "amount, why",
    [
        (1e400, "JSON 1e400 parses to inf; int(inf) is OverflowError"),
        (float("nan"), "comparing a NaN raises InvalidOperation"),
        ("NaN", "the string form parses to a Decimal NaN"),
        ("sNaN", "signalling NaN"),
        ("1e999999999", "finite, so is_finite passes — only the multiply overflows"),
        ("Infinity", "spelled out"),
    ],
)
def test_a_non_finite_merchant_amount_is_refused_not_an_arithmetic_error(amount, why):
    """A merchant controls this number. Every one of these raised an ArithmeticError that neither
    `to_minor_units` nor the route (which catches only MerchantQuoteError) translates — an
    unhandled 500 at card issuance. Same class as the `int(inf)` fixed for CALLER input in
    merchant_ucp_checkout.build_line_items; this is the MERCHANT-input half."""
    from services.agent_card_issuance import to_minor_units

    try:
        assert to_minor_units(amount, "USD") is None, why
    except Exception as exc:  # noqa: BLE001
        import pytest as _pt

        _pt.fail(f"{why}: raised {type(exc).__name__}")


def test_a_finite_amount_still_converts():
    """The positive counterpart — refusing everything would pass the test above."""
    from services.agent_card_issuance import to_minor_units

    assert to_minor_units("12.34", "USD") == 1234
    assert to_minor_units("0.005", "USD") == 1, "ROUND_CEILING, because this is a cap"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
async def test_a_non_finite_amount_anywhere_in_the_quote_is_a_502_not_a_500(monkeypatch, bad):
    """`json.loads` accepts bare `NaN`/`Infinity`, so a merchant `tax: NaN` beside a VALID total
    survives quoting. Adding `allow_nan=False` at the route turned the DB-insert 500 into a
    json-dumps 500 one line earlier — the raise is caught by nothing there. The refusal has to
    happen where MerchantQuoteError is the contract."""
    payload = _json.loads(
        '{"currency":"USD","status":"ready_for_complete","totals":['
        '{"type":"total","amount":2317},{"type":"fulfillment","amount":500},'
        f'{{"type":"tax","amount":{bad}}}]}}'
    )
    with pytest.raises(MerchantQuoteError) as excinfo:
        await _quote_from(monkeypatch, payload)
    assert "non-finite" in str(excinfo.value)


@pytest.mark.asyncio
async def test_the_snapshot_of_a_finite_quote_still_serialises(monkeypatch):
    """The positive counterpart — refusing every snapshot would pass the test above."""
    quote = await _quote_from(
        monkeypatch,
        _json.loads('{"currency":"USD","status":"ready_for_complete","totals":['
                    '{"type":"total","amount":2317},{"type":"tax","amount":217}]}'),
    )
    assert _json.dumps(quote["quote_snapshot"], allow_nan=False)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "NaN", "inf", "-inf"])
def test_a_non_finite_tax_does_not_read_as_a_quoted_amount(bad):
    """The eighth escape, and it mints a bad card rather than raising: NaN/inf read as "the
    merchant DID quote tax", which strips headroom and caps at the quote — so the card declines
    the moment real tax lands. That is the decline direction this function exists to prevent."""
    from services.agent_card_issuance import _is_quoted_amount

    assert _is_quoted_amount(bad) is False, f"{bad!r} is not a quoted amount"
    assert _is_quoted_amount({"amount": bad}) is False


def test_a_finite_tax_still_reads_as_quoted():
    from services.agent_card_issuance import _is_quoted_amount

    assert _is_quoted_amount(217) is True
    assert _is_quoted_amount("217") is True
    assert _is_quoted_amount({"amount": "217"}) is True


def test_a_huge_exponent_is_refused_in_constant_time():
    """The hazard on `int(minor)` is COST, not exception class. `Decimal("9e999997")` is finite,
    and `d * 100` lands at exponent 999999 — still inside Emax — so `decimal.Overflow` never
    fires and `int()` materialises a million-digit integer. Measured 19.1 SECONDS, from 8 bytes
    of merchant input, blocking the event loop because this function is sync on an async path.
    The `is_finite` and `ArithmeticError` guards both pass it straight through."""
    import time

    from services.agent_card_issuance import to_minor_units

    for probe in ("9e999997", "1e999998", "9" * 300000):
        started = time.monotonic()
        assert to_minor_units(probe, "USD") is None, probe
        elapsed = time.monotonic() - started
        assert elapsed < 1.0, f"{probe[:12]!r} took {elapsed:.2f}s — the big int was built"


def test_an_ordinary_large_amount_still_converts():
    """The positive counterpart: an 18-digit bound must not refuse real money. _MAX_CAP_MINOR is
    10**15, so the largest legitimate cap is three orders of magnitude inside it."""
    from services.agent_card_issuance import to_minor_units

    assert to_minor_units("10000000000000.00", "USD") == 1000000000000000
    assert to_minor_units("12.34", "USD") == 1234


def test_a_deeply_nested_amount_is_bounded_not_walked_to_a_RecursionError():
    """`_is_quoted_amount` follows `amount`/`value` into merchant-authored JSON, one Python frame
    per level. Whether that is reachable depends entirely on the runtime.

    On the PINNED runtime (runtime.txt: python-3.11) this is NOT a live 500. C and Python
    recursion share one budget there and `resp.json()` runs STRICTLY DEEPER than this walk, so
    the parser always exhausts first: measured end to end through the real transport with the
    bound reverted, depth 970 returns and depth 980 is a clean 502, with no depth producing a
    500. The claim that it did came from a probe that added a frame PER LEVEL; a deeper stack
    adds a CONSTANT, which shifts parser and walk equally and preserves their order.

    It is load-bearing on 3.12+, where the budgets are separate -- measured on 3.12.8,
    `json.loads` accepts 4000+ levels while this walk still stops at 996, so the parser stops
    gating. RecursionError is a RuntimeError, which no `except (TypeError, ValueError)` on this
    path catches. This test pins the bound so that upgrade is not a 500.

    The bound answers False, which reads as "the merchant did NOT quote tax" and ADDS headroom.
    That is the direction `_is_quoted_amount`'s docstring exists to protect: reading an unquoted
    tax as quoted strips headroom and mints a card that declines when real tax lands.
    """
    depth = 994
    nested = 1
    for _ in range(depth):
        nested = {"amount": nested}

    covers = quote_covers({"currency": "USD", "total_amount": 2317, "tax": nested,
                           "total_shipping": 400})

    assert covers["tax"] is False
    # POSITIVE counterpart. The first draft asserted `shipping is False` against a fixture with
    # no shipping key at all -- true with the bound and without it, and true of an empty dict.
    # A real shipping amount alongside the hostile tax proves the walk still WORKS after the
    # bound, instead of only proving it stopped.
    assert covers["shipping"] is True


def test_the_snapshot_dump_refuses_deep_merchant_json_instead_of_500ing():
    """Bounding the walk alone would have MOVED this 500, not removed it.

    `_is_quoted_amount` is not the only thing that recurses over merchant `totals`: the audit
    snapshot is `json.dumps`ed twice on the same request, once in `resolve_merchant_quote` and
    once in the route. Both sites carried `except (ValueError, TypeError)` for `allow_nan`, and
    RecursionError is a RuntimeError -- so with the walk bounded, the identical input simply
    surfaced from `json.dumps` a few lines later instead. Measured before this guard: an
    uncaught RecursionError at agent_card_issuance.py, in our own source.

    Both sites are gated by the parser on the pinned 3.11 and reachable on 3.12+, exactly like
    the walk. This drives the route helper directly, which is reachable on either.
    """
    from fastapi import HTTPException

    from routes.agent_cards import _dump_snapshot

    deep = 1
    for _ in range(2000):
        deep = {"amount": deep}
    quote = {"quote_snapshot": {"totals": deep}}

    with pytest.raises(HTTPException) as excinfo:
        _dump_snapshot(quote, {"max_minor": 7500})

    assert excinfo.value.status_code == 422
    assert "nested beyond the readable depth" in str(excinfo.value.detail)


def test_the_snapshot_dump_still_serialises_a_normal_quote():
    """The positive counterpart: the guard must not swallow ordinary snapshots."""
    from routes.agent_cards import _dump_snapshot

    out = _dump_snapshot(
        {"quote_snapshot": {"totals": [{"type": "tax", "amount": 500}], "currency": "USD"}},
        {"max_minor": 7500},
    )

    assert _json.loads(out)["totals"] == [{"type": "tax", "amount": 500}]
    assert _json.loads(out)["headroom"] == {"max_minor": 7500}
