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
    the parser always exhausts first: measured end to end with the bound reverted, the cliff is
    a return below it and a clean 502 above, with no depth producing a 500. The exact integer is
    a property of the measuring harness, not of the route -- it lands near 965 through the app
    and a few frames higher when the service is driven directly -- so it is deliberately not
    asserted here. The earlier claim of a 500 came from a probe that added a frame PER LEVEL; a
    deeper stack adds a CONSTANT, which shifts parser and walk equally and preserves their order.

    It is load-bearing on 3.12+, where the budgets are separate -- measured on 3.12.8,
    `json.loads` accepts 9997 levels while this walk still stops at ~996, so the parser stops
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
                           "total_shipping": {"amount": {"value": 400}}})

    assert covers["tax"] is False
    # POSITIVE counterpart, and it must enter the RECURSIVE branch. The first draft asserted
    # `shipping is False` against a fixture with no shipping key -- true with the bound, without
    # it, and for an empty dict. The second used a bare int, which never recurses at all. A
    # legitimately NESTED shipping amount alongside the hostile tax is what proves the bounded
    # walk still RESOLVES real nesting instead of only proving it stops.
    assert covers["shipping"] is True


# Deeper than the C encoder's limit on EVERY runtime we may run on. 2000 was enough on 3.11
# (limit 993) but `json.dumps` SUCCEEDS at 2000 on 3.12.8 (limit 9997), so both guard tests
# below would have started failing on exactly the upgrade their guards are justified against.
_DEEPER_THAN_ANY_ENCODER = 12_000


def test_the_guard_depth_constant_actually_exceeds_the_encoder():
    """A RATCHET for the constant above, which otherwise has none.

    Setting it back to 2_000 -- the exact regression that made both dump-guard tests
    green-but-dead on 3.12 -- leaves the whole suite passing on the pinned 3.11, because 2_000
    trips the encoder THERE. So the numeric floor has to be asserted against the highest limit
    any runtime we may move to imposes (9997 on 3.12.8), not merely against this one.

    The second assertion is the property the constant actually needs, checked against whatever
    interpreter is running: at this depth the encoder must genuinely refuse. Together they catch
    both a value lowered below a future runtime's limit and a runtime whose limit outgrows it.
    """
    assert _DEEPER_THAN_ANY_ENCODER > 9997, "must exceed the 3.12.8 encoder limit, not just 3.11's"

    deep = 1
    for _ in range(_DEEPER_THAN_ANY_ENCODER):
        deep = {"amount": deep}

    with pytest.raises(RecursionError):
        _json.dumps(deep)


def test_the_snapshot_dump_refuses_deep_merchant_json_instead_of_500ing():
    """Bounding the walk alone would have MOVED this 500, not removed it.

    `_is_quoted_amount` is not the only thing that recurses over merchant `totals`: the audit
    snapshot is `json.dumps`ed twice on the same request, once in `resolve_merchant_quote` and
    once in the route. Both sites carried `except (ValueError, TypeError)` for `allow_nan`, and
    RecursionError is a RuntimeError -- so with the walk bounded, the identical input simply
    surfaced from `json.dumps` a few lines later instead. Measured before this guard: an
    uncaught RecursionError at agent_card_issuance.py, in our own source.

    NEITHER site is reachable in production, on either runtime: `json.dumps` and `json.loads`
    are the same C encoder on one shared limit (993/993 on 3.11, 9997/9997 on 3.12.8) and
    `resp.json()` parses strictly deeper, so the parser always refuses first. An earlier version
    of this docstring said these guards were "reachable on 3.12+, exactly like the walk" -- that
    was the claim measurement withdrew, and it survived here after being deleted from both
    sources. The 3.12 argument holds only for the pure-Python walk (~996 vs 9997).

    That is precisely why this drives the helper directly: an unreachable guard is invisible to
    every end-to-end fixture, so nothing else notices if it is deleted.
    """
    from fastapi import HTTPException

    from routes.agent_cards import _dump_snapshot

    deep = 1
    for _ in range(_DEEPER_THAN_ANY_ENCODER):
        deep = {"amount": deep}
    quote = {"quote_snapshot": {"totals": deep}}

    with pytest.raises(HTTPException) as excinfo:
        _dump_snapshot(quote, {"max_minor": 7500})

    # 502, not 422: the error middleware rewrites every 422 leaving the app into a 400
    # INVALID_REQUEST whose `message` is a generic "Request validation failed". It does copy the
    # detail through to `body["detail"]`, but the STATUS and the MESSAGE an agent reads both say
    # the caller's request was invalid -- because the MERCHANT nested its reply.
    assert excinfo.value.status_code == 502
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


@pytest.mark.parametrize("depth,expected", [(1, True), (2, True), (8, True), (9, False)])
def test_the_amount_nesting_bound_is_pinned_at_its_actual_value(depth, expected):
    """`_MAX_AMOUNT_NESTING = 1` passed the entire agent-card suite; only 0 failed, and via a
    pre-existing test. So the constant's value was unpinned and its comment ("a real quote nests
    once or twice; 8 is far past that") was an unverified claim -- a mutant lowering 8 to 1 or 2
    survived. This pins both ends: legitimate nesting up to the bound still reads as a quoted
    amount, and one level past it does not."""
    from services.agent_card_issuance import _MAX_AMOUNT_NESTING, _is_quoted_amount

    assert _MAX_AMOUNT_NESTING == 8

    nested = 500
    for _ in range(depth):
        nested = {"amount": nested}

    assert _is_quoted_amount(nested) is expected


@pytest.mark.asyncio
async def test_resolve_merchant_quote_refuses_a_dump_that_recurses(monkeypatch):
    """The SIBLING half of the dump guard, which no test covered: deleting
    `resolve_merchant_quote`'s `except RecursionError` left the whole suite green (14,580
    passed). Only the route helper was pinned, while the commit claimed both were.

    Driven by patching `get_checkout`, because the guard is unreachable over the wire on both
    runtimes -- `resp.json()` parses strictly deeper than this dump and refuses first. That is
    exactly why it needs a direct test: an unreachable guard is invisible to every end-to-end
    fixture, so nothing else would notice if it were deleted.
    """
    import services.merchant_ucp_checkout as muc
    from services.agent_card_issuance import MerchantQuoteError, resolve_merchant_quote

    deep = 5
    for _ in range(_DEEPER_THAN_ANY_ENCODER):
        deep = {"amount": deep}

    async def fake_get_checkout(domain, checkout_id):
        return {"currency": "USD", "total_amount": 2317, "status": "ready_for_complete",
                "totals": deep}

    monkeypatch.setattr(muc, "get_checkout", fake_get_checkout)

    with pytest.raises(MerchantQuoteError) as excinfo:
        await resolve_merchant_quote("example.com", "chk_1")

    assert "nested beyond the readable depth" in str(excinfo.value)


def test_the_snapshot_dump_refuses_a_non_finite_amount_it_calls_itself_a_belt_for():
    """`_dump_snapshot` calls `allow_nan=False` "the BELT", then caught only RecursionError.

    So the belt itself was uncaught: `allow_nan=False` raises ValueError, which is a 500 one
    exception type over from the one that had just been guarded. Unreachable through the route
    today -- `resolve_merchant_quote` dumps the same `totals` first and `to_minor_units` refuses
    a non-finite `picked` -- which is exactly why it needs a direct test: dropping the clause
    leaves every end-to-end fixture green.
    """
    from fastapi import HTTPException

    from routes.agent_cards import _dump_snapshot

    quote = {"quote_snapshot": {"totals": [{"type": "tax", "amount": float("nan")}]}}

    with pytest.raises(HTTPException) as excinfo:
        _dump_snapshot(quote, {"max_minor": 7500})

    assert excinfo.value.status_code == 502
    assert "unserialisable" in str(excinfo.value.detail)


def test_the_dump_guard_does_not_swallow_our_own_errors_from_the_snapshot_builder(monkeypatch):
    """Pins that the catch covers the ENCODER only, not the snapshot builder.

    `MerchantQuoteError` and `MerchantUcpError` are both ValueError SUBCLASSES, so
    `except (ValueError, TypeError)` around `_snapshot_with_headroom(...)` would report any of
    OUR failures there to the agent as "the merchant sent an unserialisable amount" -- a 502
    blaming a third party for our bug. Demonstrated before the builder was hoisted out of the
    try: dropping `_snapshot_with_headroom`'s non-dict guard turned `{**None}` into
    `502 merchant quote carried an unserialisable amount`; hoisted, the same TypeError surfaces
    as a loud 500 that reads as ours. merchant_ucp_checkout.py already carries this exact
    warning about the same subclass overlap.

    This pins it without needing that second mutation: an error raised by the builder must
    travel, not be relabelled.
    """
    import routes.agent_cards as mod
    from services.agent_card_issuance import MerchantQuoteError

    def boom(quote, cap):
        raise MerchantQuoteError("our own failure, not the merchant's")

    monkeypatch.setattr(mod, "_snapshot_with_headroom", boom)

    with pytest.raises(MerchantQuoteError):
        mod._dump_snapshot({"quote_snapshot": {}}, {"max_minor": 7500})


def test_a_deep_non_dict_snapshot_is_a_502_not_an_uncaught_recursion():
    """The regression the hoist itself introduced, caught by review and pinned here.

    `_snapshot_with_headroom`'s non-dict fallback does `repr(base)[:200]`, and `repr()` recurses.
    Hoisting the builder out of the encoder's `try` -- which is what stops our own ValueErrors
    being relabelled as the merchant's -- took that recursion out of cover with it. Measured: a
    12,000-deep non-dict `quote_snapshot` answered 502 before the hoist and an uncaught 500
    after, i.e. the hardening opened the hole it was hardening.

    Dead today (`resolve_merchant_quote` only ever emits a dict), which is exactly why it needs a
    direct test: it is the "older cached shape, hand-built dict, future refactor" the builder's
    own docstring anticipates, and nothing end-to-end would notice.
    """
    from fastapi import HTTPException

    from routes.agent_cards import _dump_snapshot

    deep = []
    for _ in range(_DEEPER_THAN_ANY_ENCODER):
        deep = [deep]

    with pytest.raises(HTTPException) as excinfo:
        _dump_snapshot({"quote_snapshot": deep}, {"max_minor": 7500})

    assert excinfo.value.status_code == 502
    assert "nested beyond the readable depth" in str(excinfo.value.detail)


@pytest.fixture
def pivota_log(caplog):
    """Capture the `pivota` logger, which does NOT propagate.

    `utils.logger` sets `propagate = False` and attaches its own stdout handler, so caplog --
    which installs a handler on the ROOT logger -- sees none of these records. A test written the
    usual way collects an empty list and every `assert not any(...)` in it passes for free. The
    first draft of these tests did exactly that and failed loudly only because they also assert a
    POSITIVE line; attach the handler to the real logger instead.
    """
    import logging

    lg = logging.getLogger("pivota")
    lg.addHandler(caplog.handler)
    prev = lg.level
    lg.setLevel(logging.WARNING)
    try:
        yield caplog
    finally:
        lg.removeHandler(caplog.handler)
        lg.setLevel(prev)


def _pivota_warnings(log):
    return [r.getMessage() for r in log.records if r.levelname == "WARNING"]


def test_an_unexpected_snapshot_shape_is_logged_because_the_card_still_mints(pivota_log):
    """The quiet failure here is NOT a status code -- it is a live card with no provenance.

    A non-dict `quote_snapshot` does not fail the request. Measured: it renders to
    `{"unexpected_snapshot_shape": "None"}`, serialises cleanly, and the mint returns 200 with a
    real spending cap whose audit row no longer records which quote justified it. Every cause of
    that branch is ours -- a stale cached shape, a hand-built dict, a refactor -- so nothing
    external will ever report it, and without this line it reaches production silently.

    The review that asked for this framed it as a 502 going out under EXTERNAL_SERVICE_ERROR;
    that is true only of the DEEP non-dict case, which is the rarer one.
    """
    from routes.agent_cards import _dump_snapshot

    out = _dump_snapshot({"quote_snapshot": None}, {"max_minor": 7500})

    assert _json.loads(out)["unexpected_snapshot_shape"] == "None", "the mint SUCCEEDS"
    assert any("was not a dict" in m and "NoneType" in m for m in _pivota_warnings(pivota_log))
    # The VALUE must never reach the sink -- `base` is unbounded and partly merchant-derived.
    assert not any("unexpected_snapshot_shape\": \"" in m for m in _pivota_warnings(pivota_log))


def test_a_normal_snapshot_logs_nothing(pivota_log):
    """The negative counterpart: a warning on every healthy mint is a warning nobody reads."""
    from routes.agent_cards import _dump_snapshot

    _dump_snapshot({"quote_snapshot": {"totals": [], "currency": "USD"}}, {"max_minor": 7500})

    assert _pivota_warnings(pivota_log) == []


def test_the_two_identical_502s_are_distinguishable_in_the_LOG(pivota_log):
    """Both depth guards raise byte-identical detail, so the response cannot say which fired --
    but they mean different things: the snapshot builder's own `repr()` recursing, versus the
    encoder walking merchant `totals`. The log is the only place that distinction survives."""
    from fastapi import HTTPException

    from routes.agent_cards import _dump_snapshot

    deep_list = []
    for _ in range(_DEEPER_THAN_ANY_ENCODER):
        deep_list = [deep_list]
    deep_dict = 1
    for _ in range(_DEEPER_THAN_ANY_ENCODER):
        deep_dict = {"amount": deep_dict}

    with pytest.raises(HTTPException):
        _dump_snapshot({"quote_snapshot": deep_list}, {"max_minor": 7500})
    builder = _pivota_warnings(pivota_log)
    pivota_log.clear()
    with pytest.raises(HTTPException):
        _dump_snapshot({"quote_snapshot": {"totals": deep_dict}}, {"max_minor": 7500})
    encoder = _pivota_warnings(pivota_log)

    assert any("snapshot builder recursed" in m for m in builder)
    assert any("nested beyond the encoder" in m for m in encoder)
    assert not any("snapshot builder recursed" in m for m in encoder), "must not be the same line"
