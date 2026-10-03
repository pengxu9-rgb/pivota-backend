"""services/reap_agentic_purchase.py — the purchase state machine, on SQLite.

WHAT IS REAL HERE AND WHAT IS NOT, because that split is the whole design of this file:

  REAL   the ledger. Every assertion about a state, a field, the PII nulling or the claim fence
         is made against db/reap_agentic_ledger.py driving a real database. Nothing about storage
         is stubbed, so a test that says "the row is refused and the address is gone" is reading
         the row.

  REAL   the client's PURE half — `hosted_action`, `enrollment_state`, `checkout_state`,
         `quote_total`, `quote_tax`, `validate_return_url`, `build_shipping_address`. These carry
         the allowlists and the status vocabularies, and a test that stubbed them would be
         asserting against its own fixture. The hostile-hosted-URL test only means anything
         because `hosted_action` is the real one.

  FAKE   the client's TRANSPORT half — `resolve_our_row`, `request_quote`, `create_enrollment`,
         `get_enrollment`, `create_checkout`, `get_checkout`. Scripted per test, and an autouse
         fixture makes an unpatched `httpx.AsyncClient` raise, so a step that reached for the
         network would fail rather than hang. NOTHING IN THIS FILE HAS EVER ADDRESSED A
         reap.global OR prava.space HOST.

The Postgres arm is tests/test_reap_agentic_purchase_postgres.py. It is not a copy: it re-runs
the parts where the ENGINE is what is under test — the fence across two real connections, jsonb
round-tripping through `queries_tried`/`shipping_address`, and the server-side clock.

EVERY GUARD BELOW WAS MUTATED ONE AT A TIME AND CONFIRMED TO KILL AT LEAST ONE TEST; the table
is in the PR body.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.database import IS_POSTGRES, database  # noqa: E402
from db.schema_guard import ensure_required_schema_light  # noqa: E402
import db.reap_agentic_ledger as ledger  # noqa: E402
import services.reap_agentic_client as rc  # noqa: E402
import services.reap_agentic_purchase as svc  # noqa: E402

pytestmark = pytest.mark.skipif(
    IS_POSTGRES,
    reason=(
        "the SQLite arm of the agentic purchase machine; the Postgres arm is "
        "tests/test_reap_agentic_purchase_postgres.py"
    ),
)


# ── the buyer, the row, and the partner payloads ─────────────────────────────────────────────

RETURN_URL = "https://agent.pivota.cc/reap/return?click=abc123"
EMAIL = "ada@example.test"
#: mig 233 — the consent tag every purchase is opened under on this rail.
CONSENT = "terms-2026-09"
ADDRESS = {
    "firstName": "Ada",
    "lastName": "Lovelace",
    "phone": "+15550100",
    "addressLine1": "900 Brannan St",
    "city": "San Francisco",
    "country": "US",
    "postalCode": "94103",
}

#: Every string that must never appear in a log record or on an AdvanceResult. Written out here
#: rather than derived from ADDRESS so the PII test has an independent list — a test that built
#: its expectation from the same dict it feeds in cannot notice a field being dropped from both.
PII_STRINGS = ("ada@example.test", "900 Brannan St", "Lovelace", "+15550100")

ENROLLMENT_UUID = "3fa85f64-5717-4562-b3fc-2c963f66afa6"

ENROLLMENT_CREATED = {
    "id": ENROLLMENT_UUID,
    "status": "REQUIRES_ACTION",
    "nextAction": {
        "type": "REDIRECT",
        "url": "https://pay.prava.space/enroll/3fa85f64",
        # FAR future on purpose: a create whose link is already dead is now RETIRED rather than
        # handed to the buyer (a replayed attempt; see `_link_is_usable`). The dead-link
        # case has its own tests.
        "expiresAt": "2099-01-01T00:00:00Z",
    },
}

ENROLLMENT_ACTIVE = {
    "id": ENROLLMENT_UUID,
    "status": "ACTIVE",
    "paymentMethod": {"type": "CARD", "network": "VISA", "last4": "4242"},
    "nextAction": None,
}

QUOTE_200 = {
    "id": "f1e2d3c4",
    # NO `items` ECHO. This fixture used to carry one, and it was invented: Reap's quote
    # response has no `items` field, neither in the published schema nor in a live sandbox
    # quote (2026-09-18). `verify_quote` now treats `items` as OPTIONAL-BUT-STRICT, and the
    # tests that exercise a PRESENT echo add one explicitly (see `_quote`).
    # FAR future on purpose: the quoting step now REFUSES to create a checkout from a quote it
    # can already see is dead (P2-9), so a fixture with a past expiry would refuse every happy
    # path. The expired case has its own test.
    "expiresAt": "2099-01-01T00:00:00Z",
    "amountBreakdown": {
        "itemsSubtotal": {"amount": 42.50, "currency": "USD"},
        "shipping": {"amount": 1.00, "currency": "USD"},
        "tax": {"amount": {"amount": 1.50, "currency": "USD"}, "includedInPrices": False},
        "finalAmount": {"amount": 45.00, "currency": "USD"},
    },
}


#: A quote in EXACTLY the shape a live sandbox `POST /agentic/quotes` returned on 2026-09-18
#: (key names and value types; the amounts are ours): no `items`; `tax` nested one level deeper
#: with an INT amount; empty `discounts` / `additionalCharges`; FLOAT amounts everywhere else;
#: four shipping options, each `{id, name, selected, price}`. 42.50 + 2.50 + 0 = 45.00, so it
#: agrees with CHECKOUT_COMPLETED's `finalAmount`.
LIVE_QUOTE = {
    "id": "f1e2d3c4",
    "expiresAt": "2099-01-01T00:00:00Z",
    "amountBreakdown": {
        "itemsSubtotal": {"amount": 42.5, "currency": "USD"},
        "shipping": {"amount": 2.5, "currency": "USD"},
        "tax": {"amount": {"amount": 0, "currency": "USD"}},
        "discounts": [],
        "additionalCharges": [],
        "finalAmount": {"amount": 45.0, "currency": "USD"},
    },
    "shippingOptions": [
        {"id": "ship_std", "name": "Standard", "selected": True,
         "price": {"amount": 2.5, "currency": "USD"}},
        {"id": "ship_exp", "name": "Express", "selected": False,
         "price": {"amount": 9.99, "currency": "USD"}},
        {"id": "ship_ovn", "name": "Overnight", "selected": False,
         "price": {"amount": 24.0, "currency": "USD"}},
        {"id": "ship_free", "name": "Free over 50", "selected": False,
         "price": {"amount": 0.0, "currency": "USD"}},
    ],
}

CHECKOUT_CREATED = {
    "id": "chk_7f3a",
    "status": "REQUIRES_ACTION",
    "quoteId": "f1e2d3c4",
    "nextAction": {
        "type": "REDIRECT",
        "url": "https://pay.prava.space/checkout/chk_7f3a",
        "expiresAt": "2026-09-17T21:00:00Z",
    },
}

CHECKOUT_COMPLETED = {
    "id": "chk_7f3a",
    "status": "COMPLETED",
    "orderId": "ord_991",
    "finalAmount": {"amount": 45.00, "currency": "USD"},
    "nextAction": None,
}

HOSTILE_CHECKOUT = {
    "id": "chk_7f3a",
    "status": "REQUIRES_ACTION",
    "nextAction": {"type": "REDIRECT", "url": "https://evilprava.space/checkout/steal"},
}


def _row(**over) -> svc.PurchaseRow:
    kwargs = dict(
        merchant_domain="brand.example",
        product_key="pk_1",
        variant_key="vk_1",
        product_name="Standard Eau de Parfum",
        variant_title="Standard",
        brand="Brand",
        category="fragrance",
        our_price_minor=4250,
        currency="USD",
        market_country="US",
    )
    kwargs.update(over)
    return svc.PurchaseRow(**kwargs)


def _resolved(**over) -> rc.VariantResolution:
    kwargs = dict(
        ok=True,
        variant_id="var_abc123",
        product_id="prd_abc123",
        price=(42.50, "USD"),
        available=True,
        queries_tried=["Brand fragrance", "Standard Eau de Parfum"],
    )
    kwargs.update(over)
    return rc.VariantResolution(**kwargs)


def _ok(data) -> rc.ReapResponse:
    return rc.ReapResponse(ok=True, status=200, data=json.loads(json.dumps(data)))


def _transport(kind: str = "ReadTimeout") -> rc.ReapResponse:
    return rc.ReapResponse(ok=False, error=f"transport_error:{kind}")


def _transport_resolution(kind: str = "ReadTimeout") -> rc.VariantResolution:
    """`resolve_our_row` reports a transport failure through `reason`, not through an error field
    — it is a `VariantResolution`, not a `ReapResponse`, and it propagates `found.error` verbatim
    (see its search loop). A test that handed the resolve arm a `ReapResponse` would be testing a
    shape the client never produces."""
    return rc.VariantResolution(ok=False, reason=f"transport_error:{kind}", queries_tried=["q"])


# ── fixtures ─────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
async def _db():
    """The tables the way PRODUCTION builds them — the self-heal, not a test-only CREATE TABLE.
    Prod never runs db/migrations, so the self-heal IS the schema that ships."""
    if not database.is_connected:
        await database.connect()
    await database.execute("DROP TABLE IF EXISTS reap_checkout_manual_resolution_audit")
    await database.execute("DROP TABLE IF EXISTS reap_agentic_purchases")
    await database.execute("DROP TABLE IF EXISTS reap_agentic_enrollments")
    await ensure_required_schema_light()
    yield


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("REAP_AGENTIC_ENABLED", "1")
    monkeypatch.setenv("REAP_API_BASE_URL", "https://sandbox.api.reap.global")
    monkeypatch.setenv("REAP_API_KEY", "sk_test_key")
    monkeypatch.delenv("REAP_RETURN_URL_HOSTS", raising=False)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """A step that reached the wire FAILS rather than hangs.

    The fakes replace the client's six transport functions; this is the control that proves they
    are the only six. Without it, forgetting one would produce a test that either hangs on a real
    socket or quietly passes because the call happened to be on a branch nobody exercised.
    """
    import httpx

    class _NetworkForbidden:
        def __init__(self, *a, **k):
            raise AssertionError("a test reached the network; the Reap client must be faked")

    monkeypatch.setattr(httpx, "AsyncClient", _NetworkForbidden)


class FakeReap:
    """Scripted stand-ins for the client's six transport calls, plus a call log.

    Each attribute is either a value, a list (consumed one per call, the last one repeating), or
    a callable taking the call's kwargs. `calls` records `(name, kwargs)` in order — which is how
    the "the checkout is created in the SAME step as the quote" and "the quote step re-resolves"
    assertions are made, because both are statements about the SEQUENCE of calls and neither is
    visible in the resulting row.
    """

    NAMES = (
        "resolve_our_row",
        "request_quote",
        "create_enrollment",
        "get_enrollment",
        "create_checkout",
        "get_checkout",
    )

    def __init__(self):
        self.calls = []
        self.resolve_our_row = _resolved()
        self.request_quote = _ok(QUOTE_200)
        self.create_enrollment = _ok(ENROLLMENT_CREATED)
        self.get_enrollment = _ok(ENROLLMENT_ACTIVE)
        self.create_checkout = _ok(CHECKOUT_CREATED)
        self.get_checkout = _ok(CHECKOUT_COMPLETED)

    def named(self, name):
        return [kwargs for called, kwargs in self.calls if called == name]

    def sequence(self):
        return [called for called, _ in self.calls]

    def _answer(self, name, kwargs):
        scripted = getattr(self, name)
        if isinstance(scripted, list):
            scripted = scripted.pop(0) if len(scripted) > 1 else scripted[0]
        if callable(scripted):
            return scripted(**kwargs)
        return scripted


@pytest.fixture
def reap(monkeypatch):
    fake = FakeReap()

    def _install(name):
        async def _call(*args, **kwargs):
            # `get_enrollment(id)` and `get_checkout(id)` take their id POSITIONALLY; the other
            # four are keyword-only. Normalising here keeps `reap.named(...)` one shape.
            if args:
                kwargs = dict(kwargs, id=args[0])
            fake.calls.append((name, kwargs))
            answer = fake._answer(name, kwargs)
            if inspect.isawaitable(answer):
                answer = await answer
            return answer

        monkeypatch.setattr(rc, name, _call)

    for name in FakeReap.NAMES:
        _install(name)
    return fake


class Attribution:
    def __init__(self):
        self.calls = []
        self.raises = None

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return {"id": "edge_1", "edge_id": "edge_1"}


@pytest.fixture(autouse=True)
def attribution(monkeypatch):
    """The hook is replaced by default — a test asserting on a purchase must not depend on
    `surface_click_events` existing. `test_the_hook_is_the_real_function_by_default` is the
    control that the service really reaches for this name."""
    import services.commerce_attribution_service as cas

    recorder = Attribution()
    monkeypatch.setattr(cas, "close_external_order_conversion", recorder)
    return recorder


# ── helpers ──────────────────────────────────────────────────────────────────────────────────


async def _start(**over) -> str:
    kwargs = dict(
        agent_id="agent_one",
        agent_user_ref_hash="hash_alice",
        buyer_ref="bref_alice",
        row=_row(),
        buyer=svc.BuyerContact(email=EMAIL, shipping_address=dict(ADDRESS)),
        quantity=1,
        click_id="click_abc",
        return_url=RETURN_URL,
        # mig 233: REQUIRED on every lane now, not only cart_link. Passed by the helper so the
        # suites that are about something else keep testing that something else; the tests that
        # are about consent override it explicitly.
        consent_version=CONSENT,
    )
    kwargs.update(over)
    return await svc.start_purchase(**kwargs)


async def _raw_state(purchase_id: str, state: str) -> None:
    """Put a row in some state WITHOUT using the code under test."""
    await database.execute(
        "UPDATE reap_agentic_purchases SET state = :s WHERE id = :i",
        {"s": state, "i": purchase_id},
    )


async def _claim(purchase_id: str, worker_id: str) -> None:
    await database.execute(
        "UPDATE reap_agentic_purchases SET claimed_by = :w, claimed_at = CURRENT_TIMESTAMP "
        "WHERE id = :i",
        {"w": worker_id, "i": purchase_id},
    )


async def _get(purchase_id: str):
    return await ledger.get_purchase_internal(purchase_id)


async def _count() -> int:
    row = await database.fetch_one("SELECT COUNT(*) AS n FROM reap_agentic_purchases")
    return int(row["n"])


async def _active_enrollment(buyer_ref="bref_alice", reap_id=ENROLLMENT_UUID):
    created = await ledger.upsert_pending_enrollment(
        buyer_ref=buyer_ref, reap_enrollment_id=reap_id
    )
    await ledger.mark_enrollment_active(created["id"], reap_enrollment_id=reap_id)
    return created


async def _step(purchase_id: str, worker_id: str = "w1") -> svc.AdvanceResult:
    await _claim(purchase_id, worker_id)
    return await svc.advance(purchase_id, worker_id)


# ── 1. structural invariants ─────────────────────────────────────────────────────────────────


def test_every_non_terminal_state_has_a_step():
    """A state added to the CHECK constraint with no step here would be a RuntimeError the first
    time a row reached it in production. This is that discovery, moved to collection time."""
    assert set(svc._STEPS) | set(ledger.TERMINAL_STATES) == set(ledger.PURCHASE_STATES)
    assert set(svc._STEPS).isdisjoint(ledger.TERMINAL_STATES)


def test_the_backoff_table_covers_exactly_the_pollable_states():
    """The table is keyed by state and read with `POLL_INTERVALS[state]` — a missing key is a
    KeyError inside a poll step, and a spare key is a number nobody uses."""
    assert set(svc.POLL_INTERVALS) == set(ledger._POLLABLE_STATES)


def test_the_human_wait_states_are_the_ledgers_own_partition():
    """This module decides "schedule a full interval out" for the states that wait on a PERSON;
    the ledger decides "do not count an attempt" for the same reason. Two copies of one idea, so
    the drift is asserted rather than hoped for. The ledger's copy is parsed out of its SQL."""
    assert set(svc.HUMAN_WAIT_STATES) == set(ledger._CLAIM_ATTEMPT_EXEMPT_STATES)


def test_the_module_never_calls_the_unfenced_transition():
    """THE FENCE, AS A GREP. Every write is `transition_as_holder` or `release_claim`, both
    conjunct on `claimed_by = :worker_id`. A call to the bare `ledger.transition` would be an
    unfenced worker advance — the interleaving that charges a buyer for a checkout minted by a
    worker that no longer owns the row — and it would look completely ordinary at the call site.

    A source check, not a behaviour check, deliberately: the behavioural tests below prove the
    fence works on the paths they take, and this proves there is no seventh path."""
    import pathlib

    source = pathlib.Path(svc.__file__).read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    assert "ledger.transition_as_holder" in code, "control: the fenced form is what is used"
    assert "ledger.transition(" not in code


# ── 2. start_purchase: the dials and the refusals, all BEFORE any write ──────────────────────


async def test_the_dial_is_off_by_default_and_refuses(monkeypatch):
    monkeypatch.delenv("REAP_AGENTIC_ENABLED", raising=False)
    with pytest.raises(svc.PurchaseRefused) as exc:
        await _start()
    assert exc.value.reason == "rail_disabled"
    assert await _count() == 0


@pytest.mark.parametrize("value", ["0", "", "no", "ture", "off", "TRUE-ish"])
async def test_only_the_allowlisted_spellings_arm_the_rail(monkeypatch, value):
    """A denylist here would ARM the rail on a typo, and the thing being armed spends a buyer's
    own card at a third party."""
    monkeypatch.setenv("REAP_AGENTIC_ENABLED", value)
    assert svc.is_enabled() is False
    with pytest.raises(svc.PurchaseRefused):
        await _start()


@pytest.mark.parametrize("value", ["1", "true", "TRUE", " on ", "Yes"])
async def test_the_allowlisted_spellings_do_arm_it(monkeypatch, value, reap):
    monkeypatch.setenv("REAP_AGENTIC_ENABLED", value)
    assert svc.is_enabled() is True
    assert await _start()


async def test_an_unconfigured_client_refuses(monkeypatch):
    monkeypatch.delenv("REAP_API_BASE_URL", raising=False)
    with pytest.raises(svc.PurchaseRefused) as exc:
        await _start()
    assert exc.value.reason == "rail_unconfigured"
    assert await _count() == 0


BAD_STARTS = [
    ("blank agent_id", dict(agent_id="  "), "invalid_request"),
    ("blank user hash", dict(agent_user_ref_hash=""), "invalid_request"),
    ("blank buyer_ref", dict(buyer_ref=None), "invalid_request"),
    ("quantity zero", dict(quantity=0), "invalid_request"),
    ("quantity over the cap", dict(quantity=11), "invalid_request"),
    ("quantity is a bool", dict(quantity=True), "invalid_request"),
    ("quantity is a float", dict(quantity=2.0), "invalid_request"),
    ("currency too long", dict(row=_row(currency="USDD")), "invalid_request"),
    ("currency has digits", dict(row=_row(currency="US1")), "invalid_request"),
    ("no merchant", dict(row=_row(merchant_domain="")), "invalid_request"),
    ("no product key", dict(row=_row(product_key=" ")), "invalid_request"),
    ("price is zero", dict(row=_row(our_price_minor=0)), "invalid_request"),
    ("price is a bool", dict(row=_row(our_price_minor=True)), "invalid_request"),
    ("country is not two letters", dict(row=_row(market_country="USA")), "invalid_request"),
    (
        "a variant title AND the single-variant claim",
        dict(row=_row(declared_single_variant=True)),
        "invalid_request",
    ),
    (
        "no variant title and no claim",
        dict(row=_row(variant_title=None)),
        "invalid_request",
    ),
    # The two hint lists used to be refused here as `resolution_hints_not_persistable`.
    # Migration 225 gave them columns; section 22 covers them now, including the bad-shape cases
    # the LEDGER refuses, which still leave no row behind.
    ("an email with no @", dict(buyer=svc.BuyerContact("ada", dict(ADDRESS))), "invalid_request"),
    (
        "an email with whitespace",
        dict(buyer=svc.BuyerContact("a b@example.test", dict(ADDRESS))),
        "invalid_request",
    ),
    (
        "an address missing a required key",
        dict(buyer=svc.BuyerContact(EMAIL, {k: v for k, v in ADDRESS.items() if k != "city"})),
        "invalid_address",
    ),
    ("no address at all", dict(buyer=svc.BuyerContact(EMAIL, {})), "invalid_address"),
    ("an http return url", dict(return_url="http://agent.pivota.cc/x"), "invalid_return_url"),
    (
        "a return url on somebody else's host",
        dict(return_url="https://evil.example/x"),
        "invalid_return_url",
    ),
    (
        "a return url whose userinfo hides the real host",
        dict(return_url="https://agent.pivota.cc@evil.example/x"),
        "invalid_return_url",
    ),
]


@pytest.mark.parametrize(
    "label,over,reason", BAD_STARTS, ids=[case[0] for case in BAD_STARTS]
)
async def test_every_refusal_happens_before_any_row_is_written(label, over, reason, reap):
    """THE ORDERING IS THE CONTRACT, and the row count is what proves it.

    A row written and then refused would carry the buyer's address and email into a purchase
    nobody ever finishes — and the PII nulling lives in the TRANSITION statement, so a row that
    was never transitioned never loses it. The ledger closed the other half of this (a create
    cannot name a terminal state); this is our half."""
    with pytest.raises(svc.PurchaseRefused) as exc:
        await _start(**over)
    assert exc.value.reason == reason
    assert await _count() == 0, "a refused purchase must not leave a row behind"
    assert reap.calls == [], "start_purchase makes no partner call, refused or not"


async def test_a_started_purchase_is_resolving_and_due_now_and_carries_the_pii(reap):
    purchase_id = await _start()
    row = await _get(purchase_id)
    assert row["state"] == "resolving"
    assert row["next_poll_at"] is not None
    assert row["buyer_email"] == EMAIL
    assert row["shipping_address"]["addressLine1"] == "900 Brannan St"
    assert row["our_price_minor"] == 4250 and row["currency"] == "USD"
    assert row["click_id"] == "click_abc"
    assert reap.calls == []


async def test_the_stored_address_is_the_clients_whitelist_not_the_callers_dict(reap):
    """A caller cannot widen what reaches a third party by adding keys: `build_shipping_address`
    drops everything outside the spec's set, and what we STORE is its output."""
    hostile = dict(ADDRESS, ssn="000-00-0000", note="please charge twice")
    purchase_id = await _start(buyer=svc.BuyerContact(EMAIL, hostile))
    stored = (await _get(purchase_id))["shipping_address"]
    assert "ssn" not in stored and "note" not in stored
    assert set(stored) <= set(rc._ADDRESS_REQUIRED) | set(rc._ADDRESS_OPTIONAL)


async def test_a_row_with_no_variant_title_is_accepted_when_it_says_so(reap):
    purchase_id = await _start(row=_row(variant_title=None, declared_single_variant=True))
    assert (await _get(purchase_id))["variant_title"] is None


# ── 3. the happy path, every state, to 'completed' ───────────────────────────────────────────


async def _drive_to_completed(reap, attribution):
    purchase_id = await _start()
    assert (await _step(purchase_id)).state == "needs_enrollment"
    assert (await _step(purchase_id)).state == "quoting"
    assert (await _step(purchase_id)).state == "awaiting_approval"
    assert (await _step(purchase_id)).state == "completed"
    return purchase_id


async def test_the_happy_path_walks_every_state_and_records_the_evidence(reap, attribution):
    purchase_id = await _drive_to_completed(reap, attribution)
    row = await _get(purchase_id)

    assert row["state"] == "completed"
    assert row["terminal_at"] is not None
    # The partner's ids, as EVIDENCE.
    assert row["reap_product_id"] == "prd_abc123"
    assert row["reap_variant_id"] == "var_abc123"
    assert row["reap_quote_id"] == "f1e2d3c4"
    assert row["reap_checkout_id"] == "chk_7f3a"
    assert row["reap_order_id"] == "ord_991"
    # Minor units throughout, converted through the ONE converter.
    assert row["quoted_total_minor"] == 4500
    assert row["shipping_minor"] == 100
    assert row["tax_minor"] == 150
    assert row["final_total_minor"] == 4500
    # The PII is gone, nulled by the terminal write itself.
    assert row["buyer_email"] is None
    assert row["shipping_address"] is None
    # The claim was cleared by the same statement.
    assert row["claimed_by"] is None


async def test_the_attribution_hook_is_called_once_with_the_orders_own_numbers(
    reap, attribution
):
    purchase_id = await _drive_to_completed(reap, attribution)
    assert len(attribution.calls) == 1
    call = attribution.calls[0]
    assert call["merchant_id"] == "brand.example"
    assert call["converting_shop_domain"] == "brand.example"
    assert call["click_id"] == "click_abc"
    assert call["external_order_id"] == "ord_991"
    assert call["gross_amount_cents"] == 4500
    assert call["currency"] == "USD"
    assert call["is_self_report"] is False
    assert call["trusted_partner_provenance"] == {
        "partner_reported": True,
        "purchase_id": purchase_id,
        "reap_checkout_id": "chk_7f3a",
        # The agent whose authenticated call opened the purchase is what the order is credited to.
        "agent_id": "agent_one",
    }
    assert isinstance(call["converted_at"], datetime)


async def test_the_hook_sees_a_row_with_no_pii_on_it(reap, attribution):
    """It is handed the COMPLETED row, which the ledger has already emptied. Asserting on the
    payload it built is the closest thing to asserting on what crosses into another subsystem."""
    await _drive_to_completed(reap, attribution)
    blob = json.dumps(attribution.calls[0], default=str)
    for secret in PII_STRINGS:
        assert secret not in blob


async def test_the_hook_is_the_real_function_by_default(reap):
    """CONTROL for the autouse stub. If the service reached for some other name, every assertion
    about the hook above would be asserting on a recorder nothing calls."""
    import services.commerce_attribution_service as cas

    assert callable(cas.close_external_order_conversion)
    assert "close_external_order_conversion" in svc._close_attribution.__code__.co_names


async def test_a_quote_is_only_taken_after_the_row_is_re_resolved(reap, attribution):
    """THE RE-RESOLVE IS NOT OPTIONAL. `reap_variant_id` on the row is a per-search HANDLE, not
    an identity — five searches for one product on one day returned five different product ids.
    Quoting against the stored one is quoting against something nobody has checked since."""
    purchase_id = await _start()
    await _step(purchase_id)  # resolving -> needs_enrollment
    reap.calls.clear()
    await _step(purchase_id)  # needs_enrollment -> quoting
    reap.calls.clear()
    await _step(purchase_id)  # quoting -> awaiting_approval
    order = reap.sequence()
    assert order.index("resolve_our_row") < order.index("request_quote")
    assert reap.named("request_quote")[0]["items"] == [
        {"variantId": "var_abc123", "quantity": 1}
    ]


async def test_the_checkout_is_created_in_the_same_step_as_the_quote(reap, attribution):
    """A quote lives about five minutes and a poll cycle is not guaranteed to be shorter, so a
    step that quoted and stopped would routinely hand the next step an expired quote — and there
    is no legal state for the row to sit in meanwhile ('quoting' -> 'quoting' is not an edge)."""
    purchase_id = await _start()
    await _step(purchase_id)
    await _step(purchase_id)
    reap.calls.clear()
    result = await _step(purchase_id)
    assert result.state == "awaiting_approval"
    assert reap.sequence() == ["resolve_our_row", "request_quote", "create_checkout"]
    assert reap.named("create_checkout")[0]["quote_id"] == "f1e2d3c4"
    assert reap.named("create_checkout")[0]["enrollment_id"] == ENROLLMENT_UUID


async def test_the_two_hosted_pages_come_back_to_different_stages(reap, attribution):
    purchase_id = await _start()
    await _step(purchase_id)
    assert "stage=enroll" in reap.named("create_enrollment")[0]["return_url"]
    await _step(purchase_id)
    await _step(purchase_id)
    checkout_return = reap.named("create_checkout")[0]["return_url"]
    assert "stage=checkout" in checkout_return
    assert "click=abc123" in checkout_return, "our own click id must survive"
    assert "stage=enroll" not in checkout_return


async def test_the_enrollment_attempt_id_is_our_own_row_id(reap, attribution):
    """Reap keeps an idempotency key for 24 h while a hosted enrollment link dies in ~15 minutes.
    Keying on the buyer would replay the same DEAD link for the rest of the day."""
    purchase_id = await _start()
    await _step(purchase_id)
    attempt = reap.named("create_enrollment")[0]["attempt_id"]
    row = await _get(purchase_id)
    assert attempt == row["enrollment_id"]
    assert attempt.startswith("re_")


async def test_an_already_enrolled_buyer_skips_needs_enrollment_entirely(reap, attribution):
    await _active_enrollment()
    purchase_id = await _start()
    assert (await _step(purchase_id)).state == "quoting"
    assert reap.named("create_enrollment") == []


async def test_the_card_network_and_last_four_are_the_only_card_fields_stored(reap, attribution):
    purchase_id = await _start()
    await _step(purchase_id)
    await _step(purchase_id)
    enrollment = await ledger.get_active_enrollment("bref_alice")
    assert enrollment["card_network"] == "VISA"
    assert enrollment["card_last4"] == "4242"
    assert enrollment["reap_enrollment_id"] == ENROLLMENT_UUID
    assert enrollment["hosted_url"] is None, "the activation clears the spent link"


async def test_a_partner_last_four_that_is_not_four_digits_is_dropped_not_raised(
    reap, attribution
):
    """The ledger RAISES on a bad `card_last4`, and an enrollment the buyer actually completed
    must not become an exception because the partner sent `****`."""
    payload = json.loads(json.dumps(ENROLLMENT_ACTIVE))
    payload["paymentMethod"]["last4"] = "****"
    reap.get_enrollment = _ok(payload)
    purchase_id = await _start()
    await _step(purchase_id)
    assert (await _step(purchase_id)).state == "quoting"
    assert (await ledger.get_active_enrollment("bref_alice"))["card_last4"] is None


# ── 4. the refusals ──────────────────────────────────────────────────────────────────────────


async def test_a_resolve_refusal_is_recorded_and_the_pii_is_gone(reap, attribution):
    reap.resolve_our_row = rc.VariantResolution(
        ok=False,
        reason="options:sole_label_differs:shade",
        queries_tried=["Brand fragrance"],
    )
    purchase_id = await _start()
    result = await _step(purchase_id)

    assert result.outcome == "advanced" and result.state == "refused"
    row = await _get(purchase_id)
    assert row["state"] == "refused"
    assert row["refusal_reason"] == "options:sole_label_differs:shade"
    assert row["queries_tried"] == ["Brand fragrance"]
    assert row["buyer_email"] is None
    assert row["shipping_address"] is None
    assert row["terminal_at"] is not None
    assert attribution.calls == []


@pytest.mark.parametrize(
    "label,resolution",
    [
        ("their price moved", _resolved(price=(49.99, "USD"), price_disagrees=True)),
        ("a different currency", _resolved(price=(42.50, "EUR"), currency_mismatch=True)),
        (
            "the flags are clean but the minor units are not",
            _resolved(price=(49.99, "USD")),
        ),
        ("no price at all", _resolved(price=None)),
        ("an amount that cannot be stated exactly", _resolved(price=(42.500000000000004, "USD"))),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
async def test_a_price_that_is_not_our_price_refuses(label, resolution, reap, attribution):
    """FAIL CLOSED, and the comparison is in INTEGER MINOR UNITS.

    The third case is the load-bearing one: the client's own flags are the caller's to read, but
    a service that trusted them alone would buy at whatever the partner said whenever the flag
    was not set. The last case is the float adapter earning its place — an amount with binary
    noise cannot be converted exactly, and an amount we cannot state is not one we buy against."""
    reap.resolve_our_row = resolution
    purchase_id = await _start()
    assert (await _step(purchase_id)).state == "refused"
    row = await _get(purchase_id)
    assert row["refusal_reason"] == "price_changed"
    assert row["buyer_email"] is None
    assert reap.named("create_enrollment") == []


async def test_a_row_with_no_stored_price_refuses_rather_than_passing(reap, attribution):
    """The absence of the check is not a pass. `start_purchase` always writes the column, so this
    is about a row that came from somewhere else."""
    purchase_id = await _start()
    await database.execute(
        "UPDATE reap_agentic_purchases SET our_price_minor = NULL WHERE id = :i",
        {"i": purchase_id},
    )
    assert (await _step(purchase_id)).state == "refused"
    assert (await _get(purchase_id))["refusal_reason"] == "price_unverifiable"


async def test_a_structural_option_match_on_a_row_that_named_a_variant_refuses(
    reap, attribution
):
    """`single_value_axis_accepted_without_title` is a claim the CALLER made, and it is only ours
    to make for a row with no variant title. Our row has one."""
    reap.resolve_our_row = _resolved(single_value_axis_accepted_without_title=True)
    purchase_id = await _start()
    assert (await _step(purchase_id)).state == "refused"
    assert (await _get(purchase_id))["refusal_reason"] == "unverified_single_variant"


async def test_the_same_flag_is_fine_on_a_row_that_declared_no_variant(reap, attribution):
    """CONTROL for the test above: the guard is about the ROW, not about the flag."""
    reap.resolve_our_row = _resolved(single_value_axis_accepted_without_title=True)
    purchase_id = await _start(row=_row(variant_title=None, declared_single_variant=True))
    assert (await _step(purchase_id)).state == "needs_enrollment"


async def test_a_503_on_the_quote_refuses_as_not_completable(reap, attribution):
    """Measured on nine merchants: the two that 503 are the two that are not UCP merchants. n=2,
    so it is a refusal on THIS purchase and never a suppression of the merchant."""
    reap.request_quote = rc.ReapResponse(
        ok=False, status=503, error="reap_status_503", merchant_probably_not_completable=True
    )
    await _active_enrollment()
    purchase_id = await _start()
    await _step(purchase_id)
    result = await _step(purchase_id)
    assert result.state == "refused"
    row = await _get(purchase_id)
    assert row["refusal_reason"] == "merchant_not_completable"
    assert row["last_error_code"] == "reap_status_503"
    assert row["buyer_email"] is None
    assert reap.named("create_checkout") == [], "no checkout without a quote"


async def test_enrollment_not_active_on_the_checkout_fails_with_the_partners_own_code(
    reap, attribution
):
    """'quoting' -> 'needs_enrollment' is not a legal edge, so there is nowhere to send the buyer
    back to on this purchase. The owner starts a new one, which enrolls again."""
    reap.create_checkout = rc.ReapResponse(
        ok=False,
        status=400,
        error="reap_status_400",
        error_code="AGENTIC_REQUEST_REJECTED",
        error_detail_code="ENROLLMENT_NOT_ACTIVE",
    )
    await _active_enrollment()
    purchase_id = await _start()
    await _step(purchase_id)
    result = await _step(purchase_id)
    assert result.state == "failed"
    row = await _get(purchase_id)
    assert row["last_error_code"] == "enrollment_not_active", (
        "`last_error_code` is one column fed by three vocabularies — ours, the client's "
        "transport codes and the partner's UPPERCASE ones — and it is folded to lower case at "
        "the single place that writes it so it can be grouped on"
    )
    assert row["state"] == "failed"
    assert row["buyer_email"] is None
    assert row["reap_quote_id"] == "f1e2d3c4", "the quote we did take is still evidence"


async def test_a_row_without_a_buyer_email_fails_before_an_enrollment_is_minted(
    reap, attribution
):
    """Reap made the enrollment owner's email REQUIRED (spec, 2026-09-25). The route already
    refuses a purchase without one, so a row here without it was aged, migrated or edited by
    hand. It is refused with OUR code, before an enrollment row is minted (its id is the
    partner's idempotency attempt) and before the client's builder raises -- a
    `ReapRequestError` out of `create_enrollment` would escape `advance` on every poll."""
    purchase_id = await _start()
    await database.execute(
        "UPDATE reap_agentic_purchases SET buyer_email = NULL WHERE id = :i", {"i": purchase_id}
    )
    result = await _step(purchase_id)

    assert result.state == "failed"
    row = await _get(purchase_id)
    assert row["state"] == "failed"
    assert row["last_error_code"] == "buyer_email_missing"
    assert svc.ERROR_CODE_RE.match(row["last_error_code"])
    assert reap.named("create_enrollment") == [], "no body was sent"
    minted = await database.fetch_one("SELECT COUNT(*) AS n FROM reap_agentic_enrollments")
    assert int(minted["n"]) == 0, "no enrollment row for a body that was never sent"


async def test_the_email_the_row_carries_is_the_one_the_enrollment_sends(reap, attribution):
    """THE CONTROL for the test above: with the email present the enrollment is created and it
    carries that email."""
    purchase_id = await _start()
    await _step(purchase_id)
    assert reap.named("create_enrollment")[0]["email"] == EMAIL


async def test_quote_expired_on_the_checkout_create_releases_for_a_fresh_quote(
    reap, attribution
):
    """The partner's word for what the P2-9 pre-check catches on our clock. Since the 2026-09-25
    spec it is a 409 with `QUOTE_EXPIRED` at `error.code`. The generic branch would have ENDED
    the purchase over a five-minute timer; the answer is the pre-check's answer -- give the
    lease back and let the next step re-quote."""
    reap.create_checkout = rc.ReapResponse(
        ok=False, status=409, error="reap_status_409", error_code="QUOTE_EXPIRED",
    )
    await _active_enrollment()
    purchase_id = await _start()
    await _step(purchase_id)
    result = await _step(purchase_id)

    assert result.outcome == "released"
    assert result.state == "quoting"
    assert result.last_error_code == "quote_expired"
    assert result.next_poll_in_seconds == svc.POLL_INTERVALS["quoting"]
    row = await _get(purchase_id)
    assert row["state"] == "quoting"
    assert row["last_error_code"] == "quote_expired"


async def test_enrollment_not_active_at_error_code_on_a_409_is_the_same_failure(
    reap, attribution
):
    """Before 2026-09-25 the code arrived at `error.detail.code` on a 400; now it is at
    `error.code` on a 409 with `detail.reason = CARD_NOT_CAPTURED`. Same classification, same
    column value, whichever field the partner put it in."""
    reap.create_checkout = rc.ReapResponse(
        ok=False, status=409, error="reap_status_409", error_code="ENROLLMENT_NOT_ACTIVE",
    )
    await _active_enrollment()
    purchase_id = await _start()
    await _step(purchase_id)
    result = await _step(purchase_id)
    assert result.state == "failed"
    row = await _get(purchase_id)
    assert row["last_error_code"] == "enrollment_not_active"
    assert row["reap_quote_id"] == "f1e2d3c4", "the quote we did take is still evidence"


async def test_a_dead_enrollment_fails_the_purchase_and_retires_the_row(reap, attribution):
    reap.get_enrollment = _ok(dict(ENROLLMENT_ACTIVE, status="REVOKED"))
    purchase_id = await _start()
    await _step(purchase_id)
    result = await _step(purchase_id)
    assert result.state == "failed"
    assert (await _get(purchase_id))["last_error_code"] == "enrollment_dead"
    assert await ledger.get_active_enrollment("bref_alice") is None


@pytest.mark.parametrize(
    "status,expected_state,expected_code",
    [
        ("FAILED", "failed", "checkout_failed"),
        ("EXPIRED", "expired", "checkout_expired"),
    ],
)
async def test_a_terminal_checkout_status_lands_the_purchase_there(
    status, expected_state, expected_code, reap, attribution
):
    reap.get_checkout = _ok(dict(CHECKOUT_COMPLETED, status=status))
    purchase_id = await _start()
    await _step(purchase_id)
    await _step(purchase_id)
    await _step(purchase_id)
    result = await _step(purchase_id)
    assert result.state == expected_state
    row = await _get(purchase_id)
    assert row["state"] == expected_state
    assert row["last_error_code"] == expected_code
    assert row["buyer_email"] is None
    assert row["reap_order_id"] is None, "no order id without a COMPLETED"
    assert attribution.calls == []


async def test_processing_cannot_expire_because_the_buyer_already_approved(reap, attribution):
    """'processing' -> 'expired' is not an edge. A partner EXPIRED on an approved payment is
    recorded as a failure with its own code rather than forced into an illegal transition —
    which `transition` would raise for, killing the poll step."""
    purchase_id = await _start()
    await _raw_state(purchase_id, "processing")
    await database.execute(
        "UPDATE reap_agentic_purchases SET reap_checkout_id = 'chk_7f3a' WHERE id = :i",
        {"i": purchase_id},
    )
    reap.get_checkout = _ok(dict(CHECKOUT_COMPLETED, status="EXPIRED"))
    result = await _step(purchase_id)
    assert result.state == "failed"
    assert (await _get(purchase_id))["last_error_code"] == "checkout_expired"


# ── 4b. FAILED at the quote's expiry is a lapsed approval window ─────────────────────────────
#
# MEASURED 2026-09-25 in the Reap sandbox, twice: an unapproved checkout flips to FAILED — not
# EXPIRED, never PROCESSING — 1–10 s after the QUOTE's `expiresAt`. Reported as `checkout_failed`,
# that is a buyer who took six minutes shown as a failed purchase with no hint why.


async def _set_quote_expiry(purchase_id: str, when) -> None:
    await database.execute(
        "UPDATE reap_agentic_purchases SET reap_quote_expires_at = :t WHERE id = :i",
        {"t": ledger._bind_dt(when), "i": purchase_id},
    )


async def _drive_to_awaiting_approval(reap) -> str:
    purchase_id = await _start()
    await _step(purchase_id)
    await _step(purchase_id)
    result = await _step(purchase_id)
    assert result.state == "awaiting_approval", result
    return purchase_id


async def test_a_failed_read_on_a_lapsed_quote_names_the_lapsed_window(reap, attribution):
    purchase_id = await _drive_to_awaiting_approval(reap)
    await _set_quote_expiry(purchase_id, datetime.now(timezone.utc) - timedelta(seconds=5))
    reap.get_checkout = _ok(dict(CHECKOUT_COMPLETED, status="FAILED"))
    result = await _step(purchase_id)
    assert result.state == "failed", "the TARGET state does not move — only the reason does"
    row = await _get(purchase_id)
    assert row["state"] == "failed"
    assert row["last_error_code"] == "approval_window_lapsed"
    assert row["buyer_email"] is None
    assert attribution.calls == []


async def test_a_failed_read_on_a_live_quote_is_still_a_checkout_failure(reap, attribution):
    """KILLS the mutant that names every FAILED a lapsed window. A quote still four minutes
    from expiry cannot be why the partner failed the checkout."""
    purchase_id = await _drive_to_awaiting_approval(reap)
    await _set_quote_expiry(purchase_id, datetime.now(timezone.utc) + timedelta(minutes=4))
    reap.get_checkout = _ok(dict(CHECKOUT_COMPLETED, status="FAILED"))
    assert (await _step(purchase_id)).state == "failed"
    assert (await _get(purchase_id))["last_error_code"] == "checkout_failed"


async def test_a_failed_read_with_no_quote_expiry_is_a_checkout_failure(reap, attribution):
    """The spec does not require `expiresAt`; an absent expiry is not a lapsed one."""
    purchase_id = await _drive_to_awaiting_approval(reap)
    await _set_quote_expiry(purchase_id, None)
    reap.get_checkout = _ok(dict(CHECKOUT_COMPLETED, status="FAILED"))
    assert (await _step(purchase_id)).state == "failed"
    assert (await _get(purchase_id))["last_error_code"] == "checkout_failed"


async def test_a_failed_read_on_processing_is_never_a_lapsed_window(reap, attribution):
    """'processing' means the buyer DID approve in time. Whatever the quote column says, a
    FAILED there is a failure after approval and keeps its own code."""
    purchase_id = await _drive_to_awaiting_approval(reap)
    await _raw_state(purchase_id, "processing")
    await _set_quote_expiry(purchase_id, datetime.now(timezone.utc) - timedelta(minutes=10))
    reap.get_checkout = _ok(dict(CHECKOUT_COMPLETED, status="FAILED"))
    assert (await _step(purchase_id)).state == "failed"
    assert (await _get(purchase_id))["last_error_code"] == "checkout_failed"


async def test_an_expired_read_on_a_lapsed_quote_keeps_its_own_edge(reap, attribution):
    """CONTROL: the new code is for FAILED only. A partner EXPIRED still takes the 'expired'
    edge with `checkout_expired`, lapsed quote or not."""
    purchase_id = await _drive_to_awaiting_approval(reap)
    await _set_quote_expiry(purchase_id, datetime.now(timezone.utc) - timedelta(minutes=1))
    reap.get_checkout = _ok(dict(CHECKOUT_COMPLETED, status="EXPIRED"))
    assert (await _step(purchase_id)).state == "expired"
    assert (await _get(purchase_id))["last_error_code"] == "checkout_expired"


def test_the_lapsed_window_code_is_a_legal_error_code():
    """`release_claim` validates against ERROR_CODE_RE; `transition` does not. A code that fails
    the shape would be stored by one writer and refused by the other."""
    assert svc.ERROR_CODE_RE.match(svc.APPROVAL_WINDOW_LAPSED)
    assert svc.APPROVAL_WINDOW_LAPSED == "approval_window_lapsed"


@pytest.mark.parametrize(
    "from_state,quote,expected",
    [
        ("awaiting_approval", datetime(2020, 1, 1, tzinfo=timezone.utc), "approval_window_lapsed"),
        ("awaiting_approval", "2020-01-01 00:00:00", "approval_window_lapsed"),
        ("awaiting_approval", datetime(2099, 1, 1, tzinfo=timezone.utc), "checkout_failed"),
        ("awaiting_approval", None, "checkout_failed"),
        ("awaiting_approval", "not a timestamp", "checkout_failed"),
        ("processing", datetime(2020, 1, 1, tzinfo=timezone.utc), "checkout_failed"),
    ],
)
def test_the_failed_code_decision_table(from_state, quote, expected):
    assert svc._checkout_failed_code({"reap_quote_expires_at": quote}, from_state) == expected


# ── 5. the hostile hosted URL ────────────────────────────────────────────────────────────────


async def test_a_hostile_checkout_url_never_reaches_the_row(reap, attribution):
    """`hosted_action` is the REAL one here, which is the only reason this test means anything.
    `evilprava.space` passes a naive `endswith("prava.space")` and fails the exact-or-dot-suffix
    rule the client actually applies.

    'awaiting_approval' is a state whose entire content is a link to show a buyer, so a checkout
    with no link we will vouch for does not enter it."""
    reap.create_checkout = _ok(HOSTILE_CHECKOUT)
    await _active_enrollment()
    purchase_id = await _start()
    await _step(purchase_id)
    result = await _step(purchase_id)

    assert result.state == "failed"
    row = await _get(purchase_id)
    assert row["state"] == "failed"
    assert row["state"] != "awaiting_approval"
    assert row["last_error_code"] == "checkout_no_hosted_action"
    assert row["hosted_url"] is None
    assert "evilprava.space" not in json.dumps(row, default=str)


async def test_a_hostile_enrollment_url_never_reaches_the_row(reap, attribution):
    hostile = json.loads(json.dumps(ENROLLMENT_CREATED))
    hostile["nextAction"]["url"] = "https://prava.space.evil.example/enroll"
    reap.create_enrollment = _ok(hostile)
    purchase_id = await _start()
    result = await _step(purchase_id)

    assert result.state == "failed"
    row = await _get(purchase_id)
    assert row["last_error_code"] == "enrollment_no_hosted_action"
    assert row["hosted_url"] is None
    assert "evil.example" not in json.dumps(row, default=str)


async def test_the_good_url_does_reach_the_row(reap, attribution):
    """CONTROL. Both absence assertions above pass just as happily when the mechanism that would
    have written a URL is broken for some unrelated reason."""
    purchase_id = await _start()
    await _step(purchase_id)
    row = await _get(purchase_id)
    assert row["hosted_url"] == "https://pay.prava.space/enroll/3fa85f64"
    assert row["hosted_url_expires_at"] == datetime(2099, 1, 1, 0, 0, tzinfo=timezone.utc)


# ── 6. transport failures: release, do not move ──────────────────────────────────────────────


TRANSPORT_CASES = [
    ("resolving", "resolve_our_row", None, 120),
    ("needs_enrollment", "get_enrollment", None, 60),
    ("quoting", "request_quote", None, 120),
    ("awaiting_approval", "get_checkout", "chk_7f3a", 60),
    ("processing", "get_checkout", "chk_7f3a", 30),
]


@pytest.mark.parametrize(
    "state,call,checkout_id,expected_backoff",
    TRANSPORT_CASES,
    ids=[case[0] for case in TRANSPORT_CASES],
)
async def test_a_transport_failure_releases_with_the_states_doubled_backoff(
    state, call, checkout_id, expected_backoff, reap, attribution
):
    """A transport failure is the ONE class that means "nothing happened, try again unchanged".
    The state does not move — 'resolving' -> 'resolving' is not an edge and the ledger would
    raise for it — so this is `release_claim` with a later `next_poll_at`, which is the only
    non-transitioning write the ledger offers.

    The backoff is the state's own interval DOUBLED, capped at ten minutes. The doubling is what
    keeps a partner outage from being N workers hammering it at the ordinary cadence."""
    await _active_enrollment()
    purchase_id = await _start()
    await _raw_state(purchase_id, state)
    if checkout_id:
        await database.execute(
            "UPDATE reap_agentic_purchases SET reap_checkout_id = :c WHERE id = :i",
            {"c": checkout_id, "i": purchase_id},
        )
    if state == "needs_enrollment":
        # The enrollment row has to exist and be pending for the read to get that far.
        pending = await ledger.upsert_pending_enrollment(
            buyer_ref="bref_alice", reap_enrollment_id="11111111-1111-1111-1111-111111111111"
        )
        await ledger.mark_enrollment_dead(
            (await ledger.get_active_enrollment("bref_alice"))["id"]
        )
        await database.execute(
            "UPDATE reap_agentic_purchases SET enrollment_id = :e WHERE id = :i",
            {"e": pending["id"], "i": purchase_id},
        )
    setattr(reap, call, _transport_resolution() if call == "resolve_our_row" else _transport())

    before = await _get(purchase_id)
    result = await _step(purchase_id)

    assert result.outcome == "released"
    assert result.state == state
    assert result.next_poll_in_seconds == expected_backoff
    assert result.last_error_code == "transport_error:readtimeout"
    after = await _get(purchase_id)
    assert after["state"] == state, "a transport failure must not move the row"
    assert after["claimed_by"] is None, "the lease is given back"
    assert after["next_poll_at"] > before["next_poll_at"]


async def test_a_pending_enrollment_waits_the_plain_interval_not_the_doubled_one(
    reap, attribution
):
    reap.get_enrollment = _ok(ENROLLMENT_CREATED)  # REQUIRES_ACTION -> pending
    purchase_id = await _start()
    await _step(purchase_id)
    result = await _step(purchase_id)
    assert result.outcome == "released"
    assert result.state == "needs_enrollment"
    assert result.next_poll_in_seconds == 30


async def test_a_buyer_still_looking_at_the_page_waits_thirty_seconds(reap, attribution):
    reap.get_checkout = _ok(CHECKOUT_CREATED)  # REQUIRES_ACTION -> awaiting_buyer
    purchase_id = await _start()
    await _step(purchase_id)
    await _step(purchase_id)
    await _step(purchase_id)
    result = await _step(purchase_id)
    assert result.outcome == "released"
    assert result.state == "awaiting_approval"
    assert result.next_poll_in_seconds == 30


async def test_a_payment_in_flight_is_polled_every_fifteen_seconds(reap, attribution):
    reap.get_checkout = [_ok(dict(CHECKOUT_COMPLETED, status="PROCESSING"))]
    purchase_id = await _start()
    await _step(purchase_id)
    await _step(purchase_id)
    await _step(purchase_id)
    assert (await _step(purchase_id)).state == "processing"
    result = await _step(purchase_id)
    assert result.outcome == "released"
    assert result.state == "processing"
    assert result.next_poll_in_seconds == 15


# ── 7. unknown statuses never advance ────────────────────────────────────────────────────────


async def test_an_unrecognised_enrollment_status_never_advances(reap, attribution):
    """Folding an unknown status into 'active' sends a buyer to a checkout that cannot complete;
    folding it into 'dead' retires a live card. Reap has already changed a required field without
    moving its version, so this is a measured risk, not a hypothetical one."""
    reap.get_enrollment = _ok(dict(ENROLLMENT_ACTIVE, status="SOMETHING_NEW"))
    purchase_id = await _start()
    await _step(purchase_id)
    result = await _step(purchase_id)
    assert result.outcome == "released"
    assert result.last_error_code == "unknown_enrollment_status"
    assert (await _get(purchase_id))["state"] == "needs_enrollment"
    assert await ledger.get_active_enrollment("bref_alice") is None


async def test_an_unrecognised_checkout_status_never_advances(reap, attribution):
    reap.get_checkout = _ok(dict(CHECKOUT_COMPLETED, status="Completed"))  # wrong case on purpose
    purchase_id = await _start()
    await _step(purchase_id)
    await _step(purchase_id)
    await _step(purchase_id)
    result = await _step(purchase_id)
    assert result.outcome == "released"
    assert result.last_error_code == "checkout_read_permanent:1:unknown_checkout_status"
    assert (await _get(purchase_id))["state"] == "awaiting_approval"
    assert attribution.calls == []


# ── 8. the fence ─────────────────────────────────────────────────────────────────────────────


async def test_the_worker_that_does_not_hold_the_claim_writes_nothing(reap, attribution):
    """TWO WORKERS, ONE ROW. `worker-a` holds the lease; `worker-b` runs a step against the same
    purchase, is stopped by the ownership re-read before it reaches the partner, and would be
    stopped by the fence even if it were not. It gets `lost_claim` and the row is untouched.

    THIS TEST NO LONGER PROVES THE FENCE ON ITS OWN, and that matters for reading the mutant
    table: with the re-read in front, the step ends before any write is attempted. The fence
    itself is proven by `test_a_claim_lost_mid_step_still_writes_nothing`, where the claim moves
    INSIDE the partner call and only the conjunct in the UPDATE can catch it."""
    purchase_id = await _start()
    await _claim(purchase_id, "worker-a")
    before = await _get(purchase_id)

    result = await svc.advance(purchase_id, "worker-b")

    assert result.outcome == "lost_claim"
    assert result.state == "resolving"
    after = await _get(purchase_id)
    assert after["state"] == "resolving"
    assert after["claimed_by"] == "worker-a", "the loser must not steal or clear the lease"
    assert after["reap_variant_id"] is None, "no evidence written by a worker that lost"
    assert after["enrollment_id"] is None
    assert after["updated_at"] == before["updated_at"], "no write landed at all"


async def test_the_loser_of_a_release_also_reports_lost_claim(reap, attribution):
    """`release_claim` is fenced on the same conjunct, so the no-progress path answers the same
    way. Without this, a transport failure on a row somebody else owns would report a schedule
    that was never written."""
    reap.resolve_our_row = _transport_resolution()
    purchase_id = await _start()
    await _claim(purchase_id, "worker-a")
    result = await svc.advance(purchase_id, "worker-b")
    assert result.outcome == "lost_claim"
    assert (await _get(purchase_id))["claimed_by"] == "worker-a"


async def test_a_sweep_that_terminated_the_row_under_us_is_a_lost_claim_too(reap, attribution):
    """`expire_overdue_purchases` and `fail_exhausted_purchases` are UNFENCED bulk terminal
    writes: they can terminate a purchase a live worker holds and they clear the claim when they
    do. The worker finds out here, by getting None — never by retrying harder."""
    purchase_id = await _start()
    await _raw_state(purchase_id, "awaiting_approval")
    await database.execute(
        "UPDATE reap_agentic_purchases SET reap_checkout_id = 'chk_7f3a' WHERE id = :i",
        {"i": purchase_id},
    )
    await _claim(purchase_id, "w1")

    async def _sweep_first(**kwargs):
        await database.execute(
            "UPDATE reap_agentic_purchases SET state = 'expired', claimed_by = NULL, "
            "terminal_at = CURRENT_TIMESTAMP WHERE id = :i",
            {"i": purchase_id},
        )
        return _ok(CHECKOUT_COMPLETED)

    reap.get_checkout = _sweep_first
    result = await svc.advance(purchase_id, "w1")
    assert result.outcome == "lost_claim"
    assert (await _get(purchase_id))["state"] == "expired"
    assert attribution.calls == [], "no conversion for a purchase we did not complete"


async def test_a_terminal_row_makes_no_calls_at_all(reap, attribution):
    purchase_id = await _start()
    await _raw_state(purchase_id, "completed")
    result = await svc.advance(purchase_id, "w1")
    assert result.outcome == "terminal"
    assert result.state == "completed"
    assert reap.calls == []


async def test_an_unknown_purchase_id_is_missing_not_an_exception(reap):
    result = await svc.advance("rp_does_not_exist", "w1")
    assert result.outcome == "missing"
    assert reap.calls == []


# ── 9. the hook's failure mode ───────────────────────────────────────────────────────────────


async def test_a_failing_attribution_hook_leaves_the_purchase_completed(
    reap, attribution, caplog
):
    """The row is ALREADY terminal when the hook runs. Raising would turn a successful purchase
    into an exception in the poller's step, and the poller's only sane response — retry — cannot
    help, because the transition it would retry is no longer legal. A follow-up reconciliation
    job is the answer, not a louder failure here."""
    attribution.raises = RuntimeError("attribution ledger is down")
    caplog.set_level(logging.DEBUG)
    purchase_id = await _drive_to_completed(reap, attribution)

    assert (await _get(purchase_id))["state"] == "completed"
    complaints = [
        r for r in caplog.records if "attribution close failed" in r.getMessage()
    ]
    assert len(complaints) == 1
    message = complaints[0].getMessage()
    assert "RuntimeError" in message, "the exception TYPE is what is logged"
    assert "attribution ledger is down" not in message, "and not its message"


async def test_the_hook_is_not_called_when_the_completing_write_lost_its_claim(
    reap, attribution
):
    """THE ORDER IS THE GUARD. Closing a conversion for a purchase somebody else terminated puts
    GMV in the attribution ledger that no order backs."""
    purchase_id = await _start()
    await _raw_state(purchase_id, "awaiting_approval")
    await database.execute(
        "UPDATE reap_agentic_purchases SET reap_checkout_id = 'chk_7f3a' WHERE id = :i",
        {"i": purchase_id},
    )
    await _claim(purchase_id, "worker-a")
    result = await svc.advance(purchase_id, "worker-b")
    assert result.outcome == "lost_claim"
    assert attribution.calls == []
    assert (await _get(purchase_id))["state"] == "awaiting_approval"


# ── 10. PII never leaves ─────────────────────────────────────────────────────────────────────


async def test_no_log_record_and_no_result_ever_carries_the_buyers_details(
    reap, attribution, caplog
):
    """CAPLOG OVER THE WHOLE RUN, at DEBUG, across every state including the refusals.

    The address and the email reach exactly two places by design — `request_quote` and
    `create_enrollment` — and nowhere else. A log line is forever and is read by people who have
    no business seeing either."""
    caplog.set_level(logging.DEBUG)
    results = []

    purchase_id = await _start()
    for _ in range(4):
        results.append(await _step(purchase_id))

    reap.resolve_our_row = rc.VariantResolution(ok=False, reason="search:merchant_not_in_results")
    refused = await _start(buyer=svc.BuyerContact(EMAIL, dict(ADDRESS)))
    results.append(await _step(refused))

    reap.resolve_our_row = _transport_resolution()
    stalled = await _start()
    results.append(await _step(stalled))

    # SCOPED TO THE RAIL'S OWN LOGGERS, and the exclusion is named rather than implied. At DEBUG
    # the aiosqlite/databases/SQLAlchemy channels echo every statement WITH ITS BINDS — that is
    # what a driver debug log is for, and it is a deployment decision (never run those at DEBUG
    # in production), not a property of this module. What IS this module's property is that
    # nothing it or the two packages under it write ever carries either value.
    ours = [
        r
        for r in caplog.records
        if r.name.startswith(("services.reap_agentic", "db.reap_agentic"))
    ]
    haystack = "\n".join(
        [r.getMessage() for r in ours]
        + [repr(r.args) for r in ours]
        + [repr(result) for result in results]
    )
    for secret in PII_STRINGS:
        assert secret not in haystack, f"{secret!r} escaped into a log record or a result"

    # CONTROL: the same haystack DOES carry the things it is supposed to, so a test that found
    # nothing because nothing was logged at all would fail here instead of passing.
    assert "reap_agentic" in haystack
    assert any(r.name == "services.reap_agentic_purchase" for r in ours)
    assert any(result.purchase_id for result in results)


async def test_the_address_and_email_go_to_exactly_two_partner_calls(reap, attribution):
    await _drive_to_completed(reap, attribution)
    carried = [
        name
        for name, kwargs in reap.calls
        if EMAIL in json.dumps(kwargs, default=str)
        or "900 Brannan St" in json.dumps(kwargs, default=str)
    ]
    assert sorted(carried) == ["create_enrollment", "request_quote"]


async def test_the_public_view_of_an_in_flight_purchase_has_no_pii(reap, attribution):
    """Belt to the ledger's brace: the PII is nulled on terminal, and until then the owner read
    is an allowlist that never names either column."""
    purchase_id = await _start()
    await _step(purchase_id)
    view = await ledger.get_purchase_for_owner(purchase_id, "agent_one", "hash_alice")
    assert "buyer_email" not in view and "shipping_address" not in view
    assert view["state"] == "needs_enrollment"
    assert view["hosted_url"] == "https://pay.prava.space/enroll/3fa85f64"


# ── 11. scheduling on an advance ─────────────────────────────────────────────────────────────


async def test_entering_a_human_wait_state_schedules_a_full_interval_out(reap, attribution):
    purchase_id = await _start()
    await _step(purchase_id)
    row = await _get(purchase_id)
    assert row["state"] == "needs_enrollment"
    assert row["next_poll_at"] > datetime.now(timezone.utc) + timedelta(seconds=20)


async def test_entering_a_working_state_is_due_immediately(reap, attribution):
    """'quoting' is work we can do now. Waiting a minute there is a minute of a buyer looking at
    a spinner for no reason."""
    await _active_enrollment()
    purchase_id = await _start()
    await _step(purchase_id)
    row = await _get(purchase_id)
    assert row["state"] == "quoting"
    assert row["next_poll_at"] <= datetime.now(timezone.utc) + timedelta(seconds=5)


async def test_a_completed_row_is_never_claimed_again(reap, attribution):
    """The terminal write does NOT clear `next_poll_at`, and it does not need to: the claim
    statement carries its own `state IN (...)` conjunct, which is what stops a finished purchase
    being handed back to a poller. Asserted through `claim_due_purchases` rather than by reading
    the column, because the column is not the guard."""
    purchase_id = await _drive_to_completed(reap, attribution)
    await database.execute(
        "UPDATE reap_agentic_purchases SET next_poll_at = datetime('now', '-60 seconds') "
        "WHERE id = :i",
        {"i": purchase_id},
    )
    assert await ledger.claim_due_purchases("w2", limit=10) == []


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 12. THE QUOTE IS THE NUMBER THAT DECIDES THE CHARGE  (review P0-1)
# ══════════════════════════════════════════════════════════════════════════════════════════════
#
# The unit price is not the charge. Everything in this block exists because the first cut of this
# module verified the RESOLVED UNIT PRICE and then stored the quote's totals without comparing
# them to anything — and reached 'awaiting_approval', with a live hosted link in front of a
# buyer, on a 999.00 quote, on a wholly-EUR quote against a USD row, and on a quantity-3 purchase
# priced as one unit.


def _quote(_quantity=None, _variant=None, **breakdown_over):
    """QUOTE_200 with its breakdown overridden, and its ITEMS echo kept in step.

    `_quantity` / `_variant` exist because the echo check compares them: a test that raised the
    quantity without moving the echo would be testing `quote_items_mismatch` while believing it
    was testing the subtotal arithmetic."""
    """QUOTE_200 with its breakdown overridden. `None` DELETES a key, which is how the
    "missing component" cases are built — an absent block and a zero block are different claims
    and only one of them is a number."""
    payload = json.loads(json.dumps(QUOTE_200))
    # A PRESENT echo only when a test asks for one: Reap sends none (live sandbox, 2026-09-18).
    if _quantity is not None or _variant is not None:
        payload["items"] = [
            {"variantId": _variant or "var_abc123", "quantity": 1 if _quantity is None else _quantity}
        ]
    for key, value in breakdown_over.items():
        if value is None:
            payload["amountBreakdown"].pop(key, None)
        else:
            payload["amountBreakdown"][key] = value
    return payload


def _usd(amount):
    return {"amount": amount, "currency": "USD"}


async def _quote_outcome(reap, quote_payload, **start_over):
    """Drive a pre-enrolled buyer to the quoting step with `quote_payload` and return the row."""
    reap.request_quote = _ok(quote_payload)
    await _active_enrollment()
    purchase_id = await _start(**start_over)
    await _step(purchase_id)
    result = await _step(purchase_id)
    return result, await _get(purchase_id)


async def test_a_quote_total_that_is_not_our_purchase_refuses(reap, attribution):
    """THE P0. Unit price 42.50 verified at resolve, quote 999.00 — and the old code proceeded,
    because nothing ever compared the quote to the purchase."""
    payload = _quote(itemsSubtotal=_usd(999.00), finalAmount=_usd(1001.50))
    result, row = await _quote_outcome(reap, payload)
    assert result.state == "refused"
    assert row["state"] == "refused" and row["state"] != "awaiting_approval"
    assert row["refusal_reason"] == "price_changed"
    assert row["last_error_code"] == "quote_items_subtotal_mismatch"
    assert row["hosted_url"] is None
    assert reap.named("create_checkout") == [], "nothing is created against a price we refuse"
    assert row["buyer_email"] is None


async def test_a_quote_must_price_the_whole_quantity(reap, attribution):
    """QUANTITY > 1, WHICH IS THE CASE THE REVIEWER'S MUTANT SURVIVED. A check written as
    `subtotal == our_price_minor` passes every quantity-1 test in this file and buys three
    bottles for the price of one."""
    payload = _quote(_quantity=3, itemsSubtotal=_usd(42.50), finalAmount=_usd(45.00))
    result, row = await _quote_outcome(reap, payload, quantity=3)
    assert row["state"] == "refused"
    assert row["last_error_code"] == "quote_items_subtotal_mismatch"
    assert reap.named("create_checkout") == []


async def test_a_quote_that_prices_the_whole_quantity_is_accepted(reap, attribution):
    """CONTROL for the test above — the multiplication has to accept the right number too, or
    the refusal above would be proving nothing but that quantity 3 always refuses."""
    payload = _quote(_quantity=3, itemsSubtotal=_usd(127.50), finalAmount=_usd(130.00))
    result, row = await _quote_outcome(reap, payload, quantity=3)
    assert row["state"] == "awaiting_approval"
    assert row["quoted_total_minor"] == 13000
    assert row["quantity"] == 3


async def test_a_quote_in_another_currency_refuses_and_stores_no_totals(reap, attribution):
    """A WHOLLY-EUR QUOTE AGAINST A USD ROW. Every amount failed the old currency conjunct, every
    one came back None, and `COALESCE(:col, col)` in the transition swallowed all of them — so
    the row went to a live EUR checkout carrying NULL totals, with nothing to look at."""
    payload = _quote(
        itemsSubtotal={"amount": 42.50, "currency": "EUR"},
        shipping={"amount": 1.00, "currency": "EUR"},
        tax={"amount": {"amount": 1.50, "currency": "EUR"}, "includedInPrices": False},
        finalAmount={"amount": 45.00, "currency": "EUR"},
    )
    result, row = await _quote_outcome(reap, payload)
    assert row["state"] == "refused"
    assert row["refusal_reason"] == "price_changed"
    assert row["last_error_code"] == "quote_currency_mismatch"
    assert row["quoted_total_minor"] is None
    assert reap.named("create_checkout") == []


async def test_one_component_in_another_currency_is_enough_to_refuse(reap, attribution):
    """The conjunct is over EVERY amount in the breakdown, not just the total. A shipping line
    quoted in another currency is a total that cannot be the sum of its parts."""
    payload = _quote(shipping={"amount": 1.00, "currency": "GBP"})
    result, row = await _quote_outcome(reap, payload)
    assert row["last_error_code"] == "quote_currency_mismatch"


async def test_a_total_that_does_not_follow_from_its_breakdown_refuses(reap, attribution):
    """42.50 + 1.00 + 1.50 is 45.00, not 52.00. A total that does not reconcile is a line we did
    not read — a fee, a discount reversed — and this is the last place anything looks."""
    payload = _quote(finalAmount=_usd(52.00))
    result, row = await _quote_outcome(reap, payload)
    assert row["state"] == "refused"
    assert row["last_error_code"] == "quote_total_not_reconciled"


@pytest.mark.parametrize("delta,expected", [(0.01, "awaiting_approval"), (0.02, "refused")])
async def test_the_reconciliation_tolerance_is_exactly_one_minor_unit(
    delta, expected, reap, attribution
):
    """ONE unit is a rounding artifact — the partner rounds a percentage tax once and we round
    our reconstruction independently. TWO is a breakdown that does not describe the charge."""
    payload = _quote(finalAmount=_usd(round(45.00 + delta, 2)))
    result, row = await _quote_outcome(reap, payload)
    assert row["state"] == expected


MISSING_COMPONENTS = ["finalAmount", "itemsSubtotal", "shipping", "tax"]


@pytest.mark.parametrize("missing", MISSING_COMPONENTS)
async def test_any_missing_breakdown_component_is_unverifiable(missing, reap, attribution):
    """NEVER COALESCE A None INTO "FINE". An absent tax block and a zero tax block are different
    claims; only the second one is a number, and only a number can be checked."""
    result, row = await _quote_outcome(reap, _quote(**{missing: None}))
    assert row["state"] == "refused"
    assert row["refusal_reason"] == "price_unverifiable"
    assert row["last_error_code"] == "quote_amounts_unreadable"
    assert reap.named("create_checkout") == []


async def test_zero_shipping_and_zero_tax_are_amounts_not_absences(reap, attribution):
    """FREE SHIPPING IS THE COMMONEST QUOTE THERE IS. The repo's one converter refuses zero
    (`0 < scaled`), which is right for a spend and wrong for a breakdown component — using it for
    shipping would make every zero-shipping purchase `price_unverifiable`."""
    payload = _quote(
        shipping=_usd(0.00),
        tax={"amount": _usd(0.00), "includedInPrices": True},
        finalAmount=_usd(42.50),
    )
    result, row = await _quote_outcome(reap, payload)
    assert row["state"] == "awaiting_approval"
    assert row["shipping_minor"] == 0
    assert row["tax_minor"] == 0
    assert row["quoted_total_minor"] == 4250


async def test_a_negative_component_is_refused_rather_than_subtracted(reap, attribution):
    """A negative shipping line is a discount wearing the wrong field name. Accepting it would
    let the reconciliation pass on a total that is lower than the goods."""
    payload = _quote(shipping=_usd(-1.00), finalAmount=_usd(43.00))
    result, row = await _quote_outcome(reap, payload)
    assert row["refusal_reason"] == "price_unverifiable"
    assert row["last_error_code"] == "quote_amounts_unreadable"


async def test_decimal_strings_are_read_as_amounts(reap, attribution):
    """This partner has sent decimal STRINGS in other fields of the same API, and
    `rc.quote_total` refuses anything that is not a JSON number. A reader that answered None for
    `"45.00"` would turn a perfectly good quote into `price_unverifiable`."""
    payload = _quote(
        itemsSubtotal={"amount": "42.50", "currency": "USD"},
        shipping={"amount": "1.00", "currency": "USD"},
        tax={"amount": {"amount": "1.50", "currency": "USD"}},
        finalAmount={"amount": "45.00", "currency": "USD"},
    )
    result, row = await _quote_outcome(reap, payload)
    assert row["state"] == "awaiting_approval"
    assert row["quoted_total_minor"] == 4500
    assert row["shipping_minor"] == 100
    assert row["tax_minor"] == 150


@pytest.mark.parametrize("bad", ["not a number", True, None, float("inf"), "1e400"])
async def test_an_amount_that_is_not_an_amount_is_unverifiable(bad, reap, attribution):
    result, row = await _quote_outcome(reap, _quote(finalAmount={"amount": bad,
                                                                 "currency": "USD"}))
    assert row["refusal_reason"] == "price_unverifiable"


def test_verify_quote_names_which_check_refused():
    """The four codes, asserted against the function directly so the vocabulary is pinned in one
    place rather than inferred from five end-to-end tests."""
    row = {"currency": "USD", "our_price_minor": 4250, "quantity": 1}
    assert svc.verify_quote(QUOTE_200, row).ok is True
    assert svc.verify_quote(_quote(tax=None), row).last_error_code == "quote_amounts_unreadable"
    assert svc.verify_quote(
        _quote(finalAmount={"amount": 45.00, "currency": "EUR"}), row
    ).last_error_code == "quote_currency_mismatch"
    assert svc.verify_quote(
        _quote(itemsSubtotal=_usd(50.00), finalAmount=_usd(52.50)), row
    ).last_error_code == "quote_items_subtotal_mismatch"
    assert svc.verify_quote(_quote(finalAmount=_usd(90.00)), row).last_error_code == (
        "quote_total_not_reconciled"
    )
    # A row that cannot state its own price is the fifth, defensive, branch.
    assert svc.verify_quote(QUOTE_200, {"currency": "USD", "our_price_minor": None,
                                        "quantity": 1}).last_error_code == (
        "quote_row_unverifiable"
    )


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 13. NO PARTNER SIDE EFFECTS AFTER A LOST CLAIM  (review P1-2)
# ══════════════════════════════════════════════════════════════════════════════════════════════


LOSER_STATES = ["resolving", "needs_enrollment", "quoting", "awaiting_approval", "processing"]


@pytest.mark.parametrize("state", LOSER_STATES)
async def test_a_worker_that_does_not_hold_the_claim_calls_the_partner_not_at_all(
    state, reap, attribution
):
    """MEASURED BEFORE THIS GUARD EXISTED: a worker that had lost the lease in 'quoting' still ran
    resolve → request_quote → create_checkout, leaving a LIVE CHECKOUT at Reap bound to the
    buyer's enrollment and referenced by no row of ours; one that had lost it in 'resolving' still
    created a hosted enrollment page.

    The fence protects our database and cannot protect the partner — it is a conjunct in an
    UPDATE, and by the time it answers, the checkout exists. `_still_ours` is the addition."""
    await _active_enrollment()
    purchase_id = await _start()
    await _raw_state(purchase_id, state)
    await database.execute(
        "UPDATE reap_agentic_purchases SET reap_checkout_id = 'chk_7f3a' WHERE id = :i",
        {"i": purchase_id},
    )
    await _claim(purchase_id, "worker-a")
    before = await _get(purchase_id)
    reap.calls.clear()

    result = await svc.advance(purchase_id, "worker-b")

    assert result.outcome == "lost_claim"
    assert reap.calls == [], f"the loser in {state!r} called the partner"
    after = await _get(purchase_id)
    assert after["state"] == state
    assert after["claimed_by"] == "worker-a"
    assert after["updated_at"] == before["updated_at"]


async def test_the_loser_in_resolving_mints_no_enrollment_row(reap, attribution):
    """`upsert_pending_enrollment` takes NO holder, so it is an UNFENCED write and the re-read is
    the only thing in front of it. Without it the loser minted an enrollment row for the buyer."""
    purchase_id = await _start()
    await _claim(purchase_id, "worker-a")
    result = await svc.advance(purchase_id, "worker-b")
    assert result.outcome == "lost_claim"
    rows = await database.fetch_all("SELECT * FROM reap_agentic_enrollments")
    assert rows == [] or len(rows) == 0


async def test_the_loser_does_not_overwrite_the_holders_hosted_link(reap, attribution):
    """The second unfenced enrollment write. The holder's live 15-minute link was replaced by one
    the loser minted, so the buyer's page stopped matching the enrollment we were polling."""
    purchase_id = await _start()
    await _step(purchase_id, "worker-a")           # holder reaches needs_enrollment
    enrollment_before = await database.fetch_one(
        "SELECT * FROM reap_agentic_enrollments WHERE buyer_ref = 'bref_alice'"
    )
    await _raw_state(purchase_id, "resolving")
    await _claim(purchase_id, "worker-a")
    reap.create_enrollment = _ok(
        dict(ENROLLMENT_CREATED, nextAction=dict(ENROLLMENT_CREATED["nextAction"],
                                                 url="https://pay.prava.space/enroll/OTHER"))
    )
    reap.calls.clear()

    assert (await svc.advance(purchase_id, "worker-b")).outcome == "lost_claim"

    after = await database.fetch_one(
        "SELECT * FROM reap_agentic_enrollments WHERE buyer_ref = 'bref_alice'"
    )
    assert after["hosted_url"] == enrollment_before["hosted_url"]
    assert "OTHER" not in str(after["hosted_url"])
    assert reap.calls == []


async def test_a_claim_lost_mid_step_still_writes_nothing(reap, attribution):
    """THE FENCE ITSELF, past the re-read. The claim moves INSIDE the partner call, so
    `_still_ours` has already passed and only the `AND claimed_by = :worker_id` conjunct in the
    UPDATE can catch it. This is the test that dies when the fence's None is ignored — the
    re-read cannot stand in for it."""
    purchase_id = await _start()
    await _claim(purchase_id, "w1")

    async def _steal(**kwargs):
        await database.execute(
            "UPDATE reap_agentic_purchases SET claimed_by = 'worker-b' WHERE id = :i",
            {"i": purchase_id},
        )
        return _resolved()

    reap.resolve_our_row = _steal
    result = await svc.advance(purchase_id, "w1")
    assert result.outcome == "lost_claim"
    row = await _get(purchase_id)
    assert row["state"] == "resolving"
    assert row["reap_variant_id"] is None
    assert row["claimed_by"] == "worker-b"


async def test_a_claim_lost_mid_step_before_a_release_writes_nothing(reap, attribution):
    """The same, on the no-progress path: `release_claim` is fenced on the same conjunct, so a
    worker whose lease moved cannot reschedule — or clear — the new holder's row."""
    purchase_id = await _start()
    await _claim(purchase_id, "w1")
    before = await _get(purchase_id)

    async def _steal_then_fail(**kwargs):
        await database.execute(
            "UPDATE reap_agentic_purchases SET claimed_by = 'worker-b' WHERE id = :i",
            {"i": purchase_id},
        )
        return _transport_resolution()

    reap.resolve_our_row = _steal_then_fail
    result = await svc.advance(purchase_id, "w1")
    assert result.outcome == "lost_claim"
    row = await _get(purchase_id)
    assert row["claimed_by"] == "worker-b"
    assert row["next_poll_at"] == before["next_poll_at"], "the loser rescheduled nothing"


async def test_a_state_that_moved_under_us_stops_the_step_before_the_partner(reap, attribution):
    """The re-read checks the STATE as well as the claim. A sweep can terminate a row we still
    hold — both bulk sweeps take no holder — and `claimed_by` alone would let this worker quote a
    purchase that is already 'expired'."""
    purchase_id = await _start()
    await _claim(purchase_id, "w1")
    await _raw_state(purchase_id, "quoting")   # somebody else advanced it; our `row` says
    row = dict(await _get(purchase_id), state="resolving")  # ...what we read at step start
    result = await svc._step_resolving(row, "w1")
    assert result.outcome == "lost_claim"
    assert reap.calls == []


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 14. 'completed' IS A CLAIM ABOUT AN ORDER  (review P1-3 and P1-4)
# ══════════════════════════════════════════════════════════════════════════════════════════════


async def _to_awaiting(reap):
    await _active_enrollment()
    purchase_id = await _start()
    await _step(purchase_id)
    await _step(purchase_id)
    return purchase_id


async def test_a_completed_checkout_with_no_order_id_still_completes_and_sheds_its_pii(
    reap, attribution
):
    """REVIEW ROUND 2, P0 — AND THIS TEST USED TO ASSERT THE OPPOSITE.

    The previous fix parked a COMPLETED-without-an-orderId in 'processing' so a human would see
    it. 'processing' HAS NO PII DEADLINE: `expire_overdue_purchases` sweeps only
    'needs_enrollment' and 'awaiting_approval' (its source states are parsed out of its own SQL,
    asserted below), and `fail_exhausted_purchases` skips 'processing' unless asked. So the row
    kept `buyer_email` and `shipping_address` against every default sweep, indefinitely, for a
    purchase whose charge had already SUCCEEDED.

    Nothing is in flight once the partner says COMPLETED, so the row is terminal: the one
    statement that nulls the PII is the terminal write. The order id is simply absent, no edge is
    written, and `reap_checkout_id` is what a reconciliation re-reads to recover it."""
    reap.get_checkout = _ok({k: v for k, v in CHECKOUT_COMPLETED.items() if k != "orderId"})
    purchase_id = await _to_awaiting(reap)
    result = await _step(purchase_id)

    assert result.state == "completed"
    row = await _get(purchase_id)
    assert row["state"] == "completed"
    assert row["terminal_at"] is not None
    assert row["buyer_email"] is None, "the terminal write is what sheds the PII"
    assert row["shipping_address"] is None
    assert row["reap_order_id"] is None
    assert row["last_error_code"] == "completed_without_order_id"
    assert row["reap_checkout_id"] == "chk_7f3a", "the handle a reconciliation needs"
    assert attribution.calls == []


def test_the_states_with_no_pii_deadline_are_the_ones_this_module_never_parks_a_row_in():
    """THE CONTROL FOR THE TEST ABOVE, read out of the LEDGER'S OWN SQL rather than asserted from
    memory. Only these two states are swept on a clock; a terminal-by-success purchase left
    anywhere else keeps the buyer's address until somebody notices."""
    assert set(ledger._EXPIRE_SOURCE_STATES) == {"needs_enrollment", "awaiting_approval"}
    assert "processing" not in ledger._EXPIRE_SOURCE_STATES


@pytest.mark.parametrize("bad", ["", "   ", "ord 991", "ord/991", "o" * 129, "ord?991"])
async def test_an_order_id_we_could_not_use_as_a_key_counts_as_missing(bad, reap, attribution):
    """The order id becomes half of an idempotency key on `(merchant, external_order_id)`. A
    value that is not a usable key is not an order id — and the row still completes."""
    reap.get_checkout = _ok(dict(CHECKOUT_COMPLETED, orderId=bad))
    purchase_id = await _to_awaiting(reap)
    result = await _step(purchase_id)
    assert result.state == "completed"
    row = await _get(purchase_id)
    assert row["reap_order_id"] is None
    assert row["last_error_code"] == "completed_without_order_id"
    assert row["buyer_email"] is None
    assert attribution.calls == []


@pytest.mark.parametrize("bad", [True, False, 12345, 12.5, ["ord_1"], {"id": "ord_1"}, None])
async def test_an_order_id_that_is_not_a_string_is_not_an_order_id(bad, reap, attribution):
    """THE TYPE GUARD, AND IT IS THE ONE THAT COSTS MOST TO GET WRONG. `str(value).strip()` is
    generous in exactly the wrong way here: `orderId: true` became `"True"` and `orderId: 12345`
    became `"12345"`, and both pass the charset rule.

    That value is half of an idempotency key with `ON CONFLICT DO NOTHING`, so a partner sending
    a boolean would make EVERY order at that merchant collide on `(merchant, "True")`: one edge
    written, every later one silently dropped, that merchant's GMV reading as a single sale for
    ever, and no error anywhere to notice it by."""
    assert svc._order_id(bad) is None
    reap.get_checkout = _ok(dict(CHECKOUT_COMPLETED, orderId=bad))
    purchase_id = await _to_awaiting(reap)
    assert (await _step(purchase_id)).state == "completed"
    row = await _get(purchase_id)
    assert row["reap_order_id"] is None
    assert row["last_error_code"] == "completed_without_order_id"
    assert attribution.calls == []


async def test_a_string_order_id_is_still_accepted(reap, attribution):
    """CONTROL: the type guard must not refuse the ordinary case."""
    assert svc._order_id("ord_991") == "ord_991"
    purchase_id = await _to_awaiting(reap)
    assert (await _step(purchase_id)).state == "completed"
    assert (await _get(purchase_id))["reap_order_id"] == "ord_991"
    assert len(attribution.calls) == 1


async def test_a_completed_order_with_no_usable_amount_writes_no_edge(reap, attribution):
    """`close_external_order_conversion` is idempotent on `(merchant, external_order_id)` via
    ON CONFLICT DO NOTHING, so an edge written with `gross_amount_cents = None` is PERMANENT: the
    slot is taken, a later correct close is dropped, and that order reads as zero GMV for ever.

    The ROW still completes — the order exists and refusing to record that would be a worse lie —
    and it says why, so a reconciliation can close it from a re-read."""
    reap.get_checkout = _ok({k: v for k, v in CHECKOUT_COMPLETED.items() if k != "finalAmount"})
    purchase_id = await _to_awaiting(reap)
    result = await _step(purchase_id)

    assert result.state == "completed"
    row = await _get(purchase_id)
    assert row["state"] == "completed"
    assert row["reap_order_id"] == "ord_991"
    assert row["final_total_minor"] is None
    assert row["last_error_code"] == "final_amount_missing"
    assert attribution.calls == [], "no edge, so the idempotency slot stays free"


async def test_a_final_amount_in_another_currency_writes_no_edge(reap, attribution):
    reap.get_checkout = _ok(
        dict(CHECKOUT_COMPLETED, finalAmount={"amount": 45.00, "currency": "EUR"})
    )
    purchase_id = await _to_awaiting(reap)
    assert (await _step(purchase_id)).state == "completed"
    row = await _get(purchase_id)
    assert row["final_total_minor"] is None
    assert row["last_error_code"] == "final_amount_missing"
    assert attribution.calls == []


@pytest.mark.parametrize(
    "charged,edge",
    [(45.00, True), (45.01, True), (44.99, True), (45.02, False), (52.00, False), (1.00, False)],
)
async def test_the_edge_is_only_written_when_the_charge_matches_the_quote(
    charged, edge, reap, attribution
):
    """`quoted_total_minor` was written by the quoting step and, until this round, READ BY
    NOTHING. The buyer approved a hosted page showing the quote; a charge that differs from it is
    a partner-side re-price, and reporting it as ordinary GMV would launder that into our numbers
    where nobody would ever look at it again — the edge is idempotent, so the first value is the
    only value.

    Same one-minor-unit tolerance as `verify_quote`, for the same rounding reason."""
    reap.get_checkout = _ok(
        dict(CHECKOUT_COMPLETED, finalAmount={"amount": charged, "currency": "USD"})
    )
    purchase_id = await _to_awaiting(reap)
    assert (await _step(purchase_id)).state == "completed"

    row = await _get(purchase_id)
    assert row["quoted_total_minor"] == 4500, "control: the quote was recorded"
    assert row["final_total_minor"] == int(round(charged * 100))
    if edge:
        assert len(attribution.calls) == 1
        assert attribution.calls[0]["gross_amount_cents"] == int(round(charged * 100))
        assert row["last_error_code"] is None
    else:
        assert attribution.calls == []
        assert row["last_error_code"] == "charged_total_differs"


async def test_a_charge_that_cannot_be_compared_writes_no_edge(reap, attribution):
    """A row with no stored quote cannot be checked, and the absence of the check is not a pass.
    Reached only by a row some other writer made — `quoting` always records the total."""
    reap.get_checkout = _ok(CHECKOUT_COMPLETED)
    purchase_id = await _to_awaiting(reap)
    await database.execute(
        "UPDATE reap_agentic_purchases SET quoted_total_minor = NULL WHERE id = :i",
        {"i": purchase_id},
    )
    assert (await _step(purchase_id)).state == "completed"
    assert (await _get(purchase_id))["last_error_code"] == "charged_total_differs"
    assert attribution.calls == []


async def test_the_closer_refuses_a_keyless_or_amountless_edge_on_its_own(reap, attribution,
                                                                          caplog):
    """THE SECOND LAYER, called directly. `_complete` already decides this and is tested above;
    this exists because the cost of being wrong is permanent and a future caller of
    `_close_attribution` will not have read `_complete`."""
    caplog.set_level(logging.DEBUG)
    await svc._close_attribution(
        {"id": "rp_x", "merchant_domain": "brand.example", "reap_order_id": "",
         "final_total_minor": 4500, "currency": "USD"}
    )
    await svc._close_attribution(
        {"id": "rp_y", "merchant_domain": "brand.example", "reap_order_id": "ord_1",
         "final_total_minor": 0, "currency": "USD"}
    )
    assert attribution.calls == []
    assert sum("refusing to close a conversion" in r.getMessage() for r in caplog.records) == 2


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 15. PARTNER IDS ARE VALIDATED WHERE THEY ARE STORED  (review P2-5, P2-6, P2-7, P2-9)
# ══════════════════════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "field,bad",
    [
        ("create_checkout", "chk/../../admin"),
        ("create_checkout", "chk?ownerId=someone-else"),
        ("request_quote", "f1/../x"),
    ],
)
async def test_a_partner_id_that_could_move_a_request_never_reaches_the_row(
    field, bad, reap, attribution
):
    """STORED, THE ROW ENTERED 'awaiting_approval', AND THEN EVERY LATER POLL RAISED. `_path_id`
    inside the client refuses such an id on its way into a URL path — by raising, out of
    `advance`, for ever, because 'awaiting_approval' is exempt from the attempts counter. Caught
    at WRITE time instead: one named failure, and the value is never stored."""
    payload = _ok(dict(CHECKOUT_CREATED, id=bad)) if field == "create_checkout" \
        else _ok(dict(QUOTE_200, id=bad))
    setattr(reap, field, payload)
    await _active_enrollment()
    purchase_id = await _start()
    await _step(purchase_id)
    result = await _step(purchase_id)

    assert result.state == "failed"
    row = await _get(purchase_id)
    assert row["last_error_code"] == "partner_id_malformed"
    assert bad not in json.dumps(row, default=str)


def test_a_partner_id_the_client_would_refuse_is_refused_here():
    """PINNED TO THE CLIENT'S OWN RULE, not to a copy of it. If `rc._path_id` tightens, this
    tightens with it; a second regex here would be a second rule, and the one that decides which
    ids can be fetched is the client's."""
    for bad in ["../../admin", "abc/def", "abc?x=1", "abc#frag", "a b", "a" * 65, ""]:
        with pytest.raises(rc.ReapRequestError):
            rc._path_id(bad, what="checkout")
        assert svc._partner_id(bad, what="checkout") is None
    assert svc._partner_id("chk_7f3a", what="checkout") == "chk_7f3a"


async def test_a_malformed_id_already_on_the_row_fails_it_instead_of_raising(reap, attribution):
    """Written before the write-time check existed, or by another writer. Without the read-side
    check `rc.get_checkout` raises out of `advance` on every poll."""
    purchase_id = await _start()
    await _raw_state(purchase_id, "awaiting_approval")
    await database.execute(
        "UPDATE reap_agentic_purchases SET reap_checkout_id = 'chk/../../admin' WHERE id = :i",
        {"i": purchase_id},
    )
    result = await _step(purchase_id)
    assert result.state == "failed"
    assert (await _get(purchase_id))["last_error_code"] == "partner_id_malformed"
    assert reap.named("get_checkout") == []


async def test_the_checkout_id_survives_a_no_hosted_action_failure(reap, attribution):
    """P2-5. The checkout is REAL from the moment `create_checkout` returns 200. Dropping its id
    on the way out left a live checkout at the partner with nothing in our storage pointing at
    it — unfindable rather than merely unused."""
    reap.create_checkout = _ok({k: v for k, v in CHECKOUT_CREATED.items() if k != "nextAction"})
    await _active_enrollment()
    purchase_id = await _start()
    await _step(purchase_id)
    result = await _step(purchase_id)

    assert result.state == "failed"
    row = await _get(purchase_id)
    assert row["last_error_code"] == "checkout_no_hosted_action"
    assert row["reap_checkout_id"] == "chk_7f3a"
    assert row["reap_quote_id"] == "f1e2d3c4"
    assert row["hosted_url"] is None


async def test_a_hostile_url_arrives_as_a_client_refusal_not_as_a_missing_action(reap,
                                                                                 attribution):
    """THE TWO PATHS ARE DIFFERENT AND THE COMMENT USED TO CONFLATE THEM. A hostile
    `nextAction.url` is refused INSIDE the client, which replaces the response with
    `ok=False, error="hosted_url_not_allowed"` and DROPS `.data` — so it lands in the `not ok`
    branch and the URL never reaches this module. `checkout_no_hosted_action` is the other thing:
    a well-formed 200 with no action at all."""
    reap.create_checkout = rc.ReapResponse(
        ok=False, status=200, error="hosted_url_not_allowed"
    )
    await _active_enrollment()
    purchase_id = await _start()
    await _step(purchase_id)
    result = await _step(purchase_id)
    row = await _get(purchase_id)
    assert result.state == "failed"
    assert row["last_error_code"] == "hosted_url_not_allowed"
    assert row["last_error_code"] != "checkout_no_hosted_action"
    assert row["hosted_url"] is None


async def test_the_enrollment_the_checkout_is_bound_to_is_written_on_the_row(reap, attribution):
    """P2-7. The resolving step recorded whatever was active THEN. A buyer who re-enrolled since
    has a different active row, and a purchase pointing at the old one names a card that did not
    pay for it — while `create_checkout` was correctly sent the new one."""
    first = await _active_enrollment(reap_id="11111111-1111-1111-1111-111111111111")
    purchase_id = await _start()
    await _step(purchase_id)
    assert (await _get(purchase_id))["enrollment_id"] == first["id"]

    second = await _active_enrollment(reap_id="22222222-2222-2222-2222-222222222222")
    assert second["id"] != first["id"]
    await _step(purchase_id)

    row = await _get(purchase_id)
    assert row["state"] == "awaiting_approval"
    assert reap.named("create_checkout")[0]["enrollment_id"] == (
        "22222222-2222-2222-2222-222222222222"
    )
    assert row["enrollment_id"] == second["id"], "the row must name the card that will pay"


async def test_an_expired_quote_is_never_turned_into_a_checkout(reap, attribution):
    """P2-9. `reap_quote_expires_at` was written and never read. A quote lives about five
    minutes; one we can already see is dead is a round trip we know will fail."""
    reap.request_quote = _ok(dict(QUOTE_200, expiresAt="2020-01-01T00:00:00Z"))
    await _active_enrollment()
    purchase_id = await _start()
    await _step(purchase_id)
    result = await _step(purchase_id)

    assert result.outcome == "released"
    assert result.state == "quoting"
    assert result.last_error_code == "quote_expired"
    assert result.next_poll_in_seconds == svc.POLL_INTERVALS["quoting"]
    assert reap.named("create_checkout") == []
    assert (await _get(purchase_id))["state"] == "quoting"


async def test_a_quote_with_no_expiry_is_not_treated_as_expired(reap, attribution):
    """CONTROL. The spec does not require `expiresAt`, and a caller that read a missing expiry as
    "expired" would refuse every quote the partner sends without one."""
    reap.request_quote = _ok({k: v for k, v in QUOTE_200.items() if k != "expiresAt"})
    await _active_enrollment()
    purchase_id = await _start()
    await _step(purchase_id)
    assert (await _step(purchase_id)).state == "awaiting_approval"


async def test_a_quoting_row_with_no_active_enrollment_fails_without_calling_the_partner(
    reap, attribution
):
    """A REAL GUARD, not a formality: without it the partner enrollment id is "" and the checkout
    is either refused by the client's builder or — on any laxer builder — created bound to no
    card at all. 'quoting' cannot legally go back to 'needs_enrollment', so this fails."""
    purchase_id = await _start()
    await _raw_state(purchase_id, "quoting")
    result = await _step(purchase_id)
    assert result.state == "failed"
    assert (await _get(purchase_id))["last_error_code"] == "no_active_enrollment"
    assert reap.calls == []


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 16. THE BACKOFF ACTUALLY REACHES ITS CAP  (review P3-10)
# ══════════════════════════════════════════════════════════════════════════════════════════════


def test_the_transport_backoff_accumulates_until_it_hits_the_cap():
    """THE CAP USED TO BE DEAD CODE. A flat "interval × 2" tops out at 120 against a cap of 600,
    so `min(120, 600)` was a comparison that could never bind — and the only test available
    asserted `min(x, 600) <= 600`, which is true of every x. Asserted here as the real function's
    OUTPUT SEQUENCE."""
    seq = [svc.transport_backoff_seconds("resolving", n) for n in range(1, 8)]
    assert seq == [120, 240, 480, 600, 600, 600, 600]
    assert svc.MAX_BACKOFF_SECONDS in seq, "the cap is reachable"
    assert [svc.transport_backoff_seconds("processing", n) for n in range(1, 6)] == [
        30, 60, 120, 240, 480
    ]


def test_the_backoff_is_defined_for_a_missing_or_absurd_attempt_count():
    """`attempts` is 0 on a row nothing has claimed yet, and the poller's own claim is what moves
    it. A None, a string or a huge value must all produce a number, not an exception and not a
    thousand-digit integer."""
    for value in (None, 0, "", "nonsense", -5, 10 ** 6, True):
        seconds = svc.transport_backoff_seconds("quoting", value)
        assert isinstance(seconds, int)
        assert svc.POLL_INTERVALS["quoting"] <= seconds <= svc.MAX_BACKOFF_SECONDS


async def test_a_repeatedly_failing_row_backs_further_off_each_time(reap, attribution):
    """End to end, through the ledger's own `attempts` counter — which is incremented by the
    CLAIM, so this drives claims rather than setting the column."""
    reap.resolve_our_row = _transport_resolution()
    purchase_id = await _start()
    seen = []
    for _ in range(4):
        claimed = await ledger.claim_due_purchases("w1", limit=5)
        assert [c["id"] for c in claimed] == [purchase_id]
        result = await svc.advance(purchase_id, "w1")
        seen.append(result.next_poll_in_seconds)
        await database.execute(
            "UPDATE reap_agentic_purchases SET next_poll_at = datetime('now', '-5 seconds') "
            "WHERE id = :i",
            {"i": purchase_id},
        )
    assert seen == [120, 240, 480, 600]
    assert (await _get(purchase_id))["state"] == "resolving"


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 17. BUYER TEXT IS CLEANED BEFORE IT IS STORED OR SENT  (review P3-11)
# ══════════════════════════════════════════════════════════════════════════════════════════════


async def test_a_bidi_override_is_stripped_from_a_name_before_anything_sees_it(reap,
                                                                               attribution):
    """U+202E RIGHT-TO-LEFT OVERRIDE inside `firstName` was stored verbatim and forwarded to the
    partner, where it reverses the rendering of everything after it — on a shipping label, in a
    confirmation email, and in our own owner view."""
    await _active_enrollment()
    purchase_id = await _start(
        buyer=svc.BuyerContact(EMAIL, dict(ADDRESS, firstName="Ada\u202eelbaroved"))
    )
    stored = (await _get(purchase_id))["shipping_address"]
    assert stored["firstName"] == "Adaelbaroved"
    assert "\u202e" not in json.dumps(stored)

    await _step(purchase_id)
    sent = json.dumps(reap.named("request_quote") or [{}], default=str)
    assert "\u202e" not in sent


@pytest.mark.parametrize("field", ["firstName", "lastName", "addressLine1", "city"])
async def test_control_characters_are_stripped_from_every_address_field(field, reap):
    purchase_id = await _start(
        buyer=svc.BuyerContact(EMAIL, dict(ADDRESS, **{field: "A\u200bB\u0007C"}))
    )
    assert (await _get(purchase_id))["shipping_address"][field] == "ABC"


async def test_an_address_that_is_only_format_characters_is_refused(reap):
    """Stripping can empty a field, and an empty required field is the client's own refusal."""
    with pytest.raises(svc.PurchaseRefused) as exc:
        await _start(buyer=svc.BuyerContact(EMAIL, dict(ADDRESS, city="\u200b\u202e")))
    assert exc.value.reason == "invalid_address"
    assert await _count() == 0


async def test_an_email_carrying_a_format_character_is_cleaned_not_stored_as_sent(reap):
    """STRIP AND THEN REFUSE THE REMAINDER, here as everywhere else. `ada<RLO>@example.test`
    cleans to a perfectly ordinary address, and refusing the purchase over an invisible character
    somebody's web form inserted helps nobody. What must not happen is the override reaching
    storage or the partner."""
    purchase_id = await _start(
        buyer=svc.BuyerContact("ada\u202e@example.test", dict(ADDRESS))
    )
    assert (await _get(purchase_id))["buyer_email"] == EMAIL


async def test_an_email_that_is_not_an_address_once_cleaned_is_refused(reap):
    with pytest.raises(svc.PurchaseRefused) as exc:
        await _start(buyer=svc.BuyerContact("\u202e\u200b", dict(ADDRESS)))
    assert exc.value.reason == "invalid_request"
    assert await _count() == 0


async def test_the_cleaned_value_is_the_one_that_is_validated_and_sent(reap, attribution):
    """CONTROL for the ordering. Cleaning AFTER validation would mean we validated one string and
    sent another; cleaning before means the stored value, the validated value and the value on
    the wire are the same string."""
    await _active_enrollment()
    purchase_id = await _start(
        buyer=svc.BuyerContact(EMAIL, dict(ADDRESS, addressLine1="900\u200b Brannan St"))
    )
    stored = (await _get(purchase_id))["shipping_address"]["addressLine1"]
    await _step(purchase_id)   # resolving -> quoting (the buyer is already enrolled)
    await _step(purchase_id)   # quoting: this is the step that quotes
    assert stored == "900 Brannan St"
    assert reap.named("request_quote")[0]["shipping_address"]["addressLine1"] == stored


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 18. THE GUARDS THAT ONLY MATTER MID-STEP
# ══════════════════════════════════════════════════════════════════════════════════════════════
#
# Three mutants survived the first sweep of this fix, and all three for the same reason: a
# NEIGHBOURING guard was catching the case the test exercised, so removing the guard under test
# changed nothing observable. Each test below removes that cover.
#
# This is the shape the repo has been bitten by before — a guard whose test passes for the wrong
# reason is not a tested guard. The cover here is legitimate defence in depth, which is exactly
# why it has to be stepped around rather than deleted.


async def test_the_claim_is_re_checked_between_the_quote_and_the_checkout(reap, attribution):
    """M25. The re-read at the TOP of the quoting step catches the ordinary loser, so a test that
    starts with the lease already gone cannot see this one. A quote takes 13–19 SECONDS — the
    longest window in the whole machine, and comfortably long enough for a lease to age out and
    be requeued — so the claim moves INSIDE `request_quote` here.

    What it costs to get wrong: a live checkout at Reap, bound to the buyer's enrolled card, that
    no row of ours references and that the fence will then refuse to record."""
    await _active_enrollment()
    purchase_id = await _start()
    await _step(purchase_id)                      # -> quoting
    await _claim(purchase_id, "w1")

    async def _steal_then_quote(**kwargs):
        await database.execute(
            "UPDATE reap_agentic_purchases SET claimed_by = 'worker-b' WHERE id = :i",
            {"i": purchase_id},
        )
        return _ok(QUOTE_200)

    reap.request_quote = _steal_then_quote
    reap.calls.clear()

    result = await svc.advance(purchase_id, "w1")

    assert result.outcome == "lost_claim"
    assert reap.named("create_checkout") == [], (
        "the lease moved during the quote; no checkout may be created against it"
    )
    row = await _get(purchase_id)
    assert row["state"] == "quoting"
    assert row["reap_checkout_id"] is None
    assert row["reap_quote_id"] is None


async def test_the_claim_is_re_checked_before_the_enrollment_is_minted(reap, attribution):
    """M27. `upsert_pending_enrollment` and `create_enrollment` sit AFTER the resolve, and the
    resolve is three partner round trips (search → details → variant). The top-of-step re-read
    cannot speak for what is true that much later, so the claim moves inside the resolve here.

    Both writes are UNFENCED — the ledger's enrollment functions take no holder — so this re-read
    is the only thing standing in front of them. Getting it wrong minted an enrollment row for
    the buyer and replaced the real holder's live hosted link with one nobody will be sent to."""
    purchase_id = await _start()
    await _step(purchase_id, "worker-a")          # the holder reaches needs_enrollment
    held = await database.fetch_one(
        "SELECT * FROM reap_agentic_enrollments WHERE buyer_ref = 'bref_alice'"
    )
    await _raw_state(purchase_id, "resolving")
    await _claim(purchase_id, "w1")

    async def _steal_then_resolve(**kwargs):
        await database.execute(
            "UPDATE reap_agentic_purchases SET claimed_by = 'worker-b' WHERE id = :i",
            {"i": purchase_id},
        )
        return _resolved()

    reap.resolve_our_row = _steal_then_resolve
    reap.create_enrollment = _ok(
        dict(ENROLLMENT_CREATED, nextAction=dict(ENROLLMENT_CREATED["nextAction"],
                                                 url="https://pay.prava.space/enroll/STOLEN"))
    )
    reap.calls.clear()

    result = await svc.advance(purchase_id, "w1")

    assert result.outcome == "lost_claim"
    assert reap.named("create_enrollment") == []
    rows = await database.fetch_all("SELECT * FROM reap_agentic_enrollments")
    assert len(rows) == 1, "no second enrollment row may be minted by a worker that lost"
    assert rows[0]["hosted_url"] == held["hosted_url"]
    assert "STOLEN" not in str(rows[0]["hosted_url"])


async def test_the_edge_is_withheld_by_complete_itself_not_only_by_the_closer(
    reap, attribution, caplog
):
    """M29. `_close_attribution` carries its own refusal for a keyless or amountless edge, and
    that second layer was silently doing all the work: deleting the decision in `_complete` left
    `attribution.calls == []` true anyway.

    The two layers log DIFFERENT lines, so the test asserts which one fired. The first layer is
    the one that matters operationally — it is what names `final_amount_missing` on the row — and
    the second exists only because the cost of being wrong is a permanent zero-GMV edge that no
    later close can replace."""
    caplog.set_level(logging.DEBUG)
    reap.get_checkout = _ok({k: v for k, v in CHECKOUT_COMPLETED.items() if k != "finalAmount"})
    purchase_id = await _to_awaiting(reap)
    await _step(purchase_id)

    messages = [r.getMessage() for r in caplog.records]
    assert any("no attribution edge written" in m for m in messages), (
        "`_complete` must be the thing that declines to open an edge it cannot key"
    )
    assert not any("refusing to close a conversion" in m for m in messages), (
        "the closer's own guard must not be what caught this — it is the backstop"
    )
    assert attribution.calls == []
    assert (await _get(purchase_id))["last_error_code"] == "final_amount_missing"


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 19. WHAT WAS PRICED, NOT ONLY WHAT IT COST  (review round 2)
# ══════════════════════════════════════════════════════════════════════════════════════════════


async def test_a_quote_for_a_different_variant_is_refused_even_at_the_right_price(
    reap, attribution
):
    """REAP RETURNS 200 FOR A SUBSTITUTED VARIANT. Every other check in `verify_quote` looks at
    what the quote COSTS, and a substitution whose price happens to match sails through all of
    them: (b) compares OUR unit price against a subtotal, and if those agree, the fact that a
    different physical object is being priced is invisible.

    This is the only check that looks at WHAT is being bought."""
    payload = _quote(_variant="var_SOMETHING_ELSE")
    result, row = await _quote_outcome(reap, payload)
    assert row["state"] == "refused"
    assert row["refusal_reason"] == "price_unverifiable"
    assert row["last_error_code"] == "quote_items_mismatch"
    assert reap.named("create_checkout") == []
    assert row["buyer_email"] is None


async def test_a_quote_for_a_different_quantity_is_refused(reap, attribution):
    """The echo carries the quantity too, and the subtotal check cannot see a quantity change
    that the partner also repriced consistently."""
    payload = _quote(_quantity=2, itemsSubtotal=_usd(85.00), finalAmount=_usd(87.50))
    result, row = await _quote_outcome(reap, payload)   # the row asked for 1
    assert row["state"] == "refused"
    assert row["last_error_code"] == "quote_items_mismatch"


_ABSENT = object()


@pytest.mark.parametrize(
    "items",
    [
        None,  # PRESENT and null — only a MISSING key skips the echo
        [],
        "var_abc123",
        {"variantId": "var_abc123", "quantity": 1},
        [{"variantId": "var_abc123", "quantity": 1}, {"variantId": "var_x", "quantity": 1}],
        [{"variantId": "var_abc123"}],
        [{"variantId": "var_abc123", "quantity": True}],
        [{"variantId": "var_abc123", "quantity": "1"}],
        [{}],
    ],
)
async def test_a_quote_that_does_not_say_what_it_priced_is_unverifiable(
    items, reap, attribution
):
    """A PRESENT `items` that we cannot read as "exactly the line we sent" IS A REFUSAL: `null`,
    `[]`, a non-list, a dict-shaped one, two lines, a quantity that is a bool or a string. Only
    an ABSENT key skips the echo (Reap sends none) — see
    `test_the_happy_path_completes_on_the_live_quote_shape`."""
    payload = _quote()
    payload["items"] = items
    result, row = await _quote_outcome(reap, payload)
    assert row["refusal_reason"] == "price_unverifiable"
    assert row["last_error_code"] == "quote_items_mismatch"


def test_the_echo_check_is_skipped_when_no_variant_is_supplied():
    """CONTROL for the parameter's default. The direct unit tests call `verify_quote` without a
    variant id; an end-to-end caller always has one, and `_step_quoting` passes it."""
    row = {"currency": "USD", "our_price_minor": 4250, "quantity": 1}
    echoed = _quote(_variant="var_abc123")
    assert svc.verify_quote(echoed, row).ok is True
    assert svc.verify_quote(echoed, row, variant_id="var_abc123").ok is True
    assert svc.verify_quote(echoed, row, variant_id="var_other").last_error_code == (
        "quote_items_mismatch"
    )
    # And with NO echo — what Reap actually sends — any variant id passes the echo step; the
    # amount rules are what is left.
    assert "items" not in QUOTE_200
    assert svc.verify_quote(QUOTE_200, row, variant_id="var_other").ok is True


def test_verify_quote_re_applies_the_quantity_ceiling():
    """`MAX_QUANTITY` is enforced at `start_purchase`, and again here. Not decoration: this is
    the last arithmetic before a card is charged, the subtotal check MULTIPLIES by this number,
    and the row could have been written by something that is not `start_purchase`."""
    ok_row = {"currency": "USD", "our_price_minor": 4250, "quantity": svc.MAX_QUANTITY}
    over = dict(ok_row, quantity=svc.MAX_QUANTITY + 1)
    assert svc.verify_quote(QUOTE_200, over).last_error_code == "quote_row_unverifiable"
    assert svc.verify_quote(QUOTE_200, dict(ok_row, quantity=0)).last_error_code == (
        "quote_row_unverifiable"
    )
    # CONTROL: the ceiling itself is allowed, so the refusal above is about the bound and not
    # about every quantity above one.
    payload = _quote(_quantity=svc.MAX_QUANTITY,
                     itemsSubtotal=_usd(425.00), finalAmount=_usd(427.50))
    assert svc.verify_quote(payload, ok_row, variant_id="var_abc123").ok is True


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 20. THE CODE COLUMN, AND A CURRENCY THE CONVERTER CANNOT STATE  (review round 2)
# ══════════════════════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("currency", sorted(svc.THREE_DECIMAL_CURRENCIES))
async def test_a_three_decimal_currency_is_refused_at_the_door(currency, reap):
    """`_exponent` answers 2 for everything outside the repo's zero-decimal list, so 1.234 KWD
    would be read as 123 fils rather than 1234 — a tenfold error in the partner's favour, with no
    symptom at all, because the arithmetic stays self-consistent all the way to the charge.

    Refused rather than handled: handling it means a third exponent through `major_to_minor`,
    which is the repo's ONE rounding policy and not this package's to widen."""
    with pytest.raises(svc.PurchaseRefused) as exc:
        await _start(row=_row(currency=currency))
    assert exc.value.reason == "currency_unsupported"
    assert await _count() == 0


@pytest.mark.parametrize("currency", ["USD", "SGD", "EUR", "JPY"])
async def test_the_currencies_this_rail_can_state_are_accepted(currency, reap):
    """CONTROL, and JPY is the interesting one: zero decimals, which the repo's converter already
    knows about, so it must NOT be swept up by the three-decimal refusal."""
    assert await _start(row=_row(currency=currency))


def test_the_error_code_column_is_folded_and_the_other_columns_are_not():
    """ONE HELPER FOR SIX KINDS OF VALUE WAS A MISTAKE, and this test is what caught it: folding
    case inside the shared `_cap` turned `card_network` into `"visa"` on its way to a column
    whose only job is to be shown to a buyer.

    `last_error_code` is folded because it is ONE column fed by three vocabularies that disagree
    on case. `refusal_reason` is not, because a reader matches on the client's own reason
    strings."""
    assert svc._error_code("ENROLLMENT_NOT_ACTIVE") == "enrollment_not_active"
    assert svc._error_code("transport_error:ReadTimeout") == "transport_error:readtimeout"
    assert svc._error_code("quote_expired") == "quote_expired"
    assert svc._error_code("") is None and svc._error_code(None) is None
    assert svc._cap("VISA") == "VISA"
    assert svc._cap("options:sole_label_differs:shade") == "options:sole_label_differs:shade"
    assert len(svc._error_code("z" * 200)) == svc._CODE_MAX


async def test_a_partner_status_is_recorded_verbatim_not_folded(reap, attribution):
    """`reap_status` is the partner's raw value, which the ledger records and never matches on —
    folding it would corrupt the record rather than tidy it."""
    purchase_id = await _start()
    await _step(purchase_id)
    await _step(purchase_id)
    enrollment = await ledger.get_active_enrollment("bref_alice")
    assert enrollment["reap_status"] == "ACTIVE"
    assert enrollment["card_network"] == "VISA"


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 21. EVERY CODE THIS MODULE CAN EMIT FITS THE COLUMN'S SHAPE
# ══════════════════════════════════════════════════════════════════════════════════════════════
#
# The ledger validates `last_error_code` in `release_claim` and NOT in `transition`. So a code
# outside the shape lands in the column through a transition, silently, and is then REFUSED the
# next time a release carries the same value — the column would hold a value its own writer will
# not write. These two tests are what keep the module's vocabulary and the ledger's pattern from
# drifting apart, from both ends: the literals, and the fold.


def _error_code_literals():
    """Every string literal this module passes as `last_error_code=` or `error_code=`, read out
    of the SOURCE by walking its AST.

    WHY THE AST AND NOT A LIST WRITTEN OUT HERE. A list in the test is a list somebody has to
    remember to extend, and the failure of forgetting is invisible — a new code with a capital
    letter or a space in it would be stored by a transition and then rejected by the next
    release, with nothing failing in between. Walking the source means adding a code to the
    module IS adding it to this test."""
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(svc.__file__).read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg in ("last_error_code", "error_code"):
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                out.append((node.lineno, value.value))
        # `QuoteCheck(False, "price_unverifiable", "quote_amounts_unreadable")` passes its code
        # POSITIONALLY, so the keyword walk above cannot see it — and those are the four newest
        # codes in the module.
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "QuoteCheck":
            for index, arg in enumerate(node.args):
                if index == 2 and isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    out.append((node.lineno, arg.value))
    return out


def test_no_error_code_this_module_can_emit_fails_the_ledgers_shape():
    literals = _error_code_literals()
    # CONTROL: the walk found something. An empty list would make every assertion below vacuous,
    # which is exactly how an AST-walking test rots when the code it reads is refactored.
    assert len(literals) >= 20, f"the AST walk found only {len(literals)} codes; it is broken"
    assert {"partner_id_malformed", "quote_expired", "quote_items_mismatch"} <= {
        code for _, code in literals
    }
    bad = [(line, code) for line, code in literals if not svc.ERROR_CODE_RE.match(code)]
    assert bad == [], f"codes the ledger's release_claim would refuse: {bad}"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("ENROLLMENT_NOT_ACTIVE", "enrollment_not_active"),
        ("AGENTIC_RESOURCE_NOT_FOUND", "agentic_resource_not_found"),
        ("transport_error:ReadTimeout", "transport_error:readtimeout"),
        ("reap_status_503", "reap_status_503"),
        ("hosted_url_not_allowed", "hosted_url_not_allowed"),
        ("  Quote_Expired  ", "quote_expired"),
    ],
)
def test_every_shape_the_client_can_hand_us_folds_into_the_column(raw, expected):
    """The three vocabularies that feed this column, each through `_error_code`. The partner's
    codes are pinned UPPERCASE by the client's own `^[A-Z_]{3,64}$` check before it keeps them,
    so the fold is the only thing making them groupable alongside ours."""
    assert svc._error_code(raw) == expected
    assert svc.ERROR_CODE_RE.match(expected)


@pytest.mark.parametrize("raw", ["bad code!", "héllo", "a/b", "A B", "code\\n", "x" * 200])
def test_a_code_outside_the_shape_becomes_a_named_placeholder_not_a_bad_write(raw, caplog):
    """NOT None, and not the raw value. `transition` does not validate, so the raw value would be
    written and then refused by the next `release_claim` carrying it. And None already means
    "there was no error", which is a different fact from "there was an error we cannot name".

    Nothing here is measured — every source feeding the column today is already inside the class.
    This is the guard for the partner or the client changing, which on this rail has happened
    twice without the version moving."""
    caplog.set_level(logging.DEBUG)
    folded = svc._error_code(raw)
    assert folded is not None
    assert svc.ERROR_CODE_RE.match(folded), f"{folded!r} would be refused by release_claim"
    if len(raw) <= svc._CODE_MAX and raw.strip():
        assert folded == svc.UNREPRESENTABLE_ERROR_CODE
        assert any("outside the ledger's shape" in r.getMessage() for r in caplog.records)


async def test_a_partner_code_reaches_the_column_already_folded(reap, attribution):
    """End to end, not just through the helper: the partner's `ENROLLMENT_NOT_ACTIVE` is written
    by a TRANSITION, which does not validate — so if the fold were not applied at the call site
    the column would carry a value `release_claim` refuses."""
    reap.create_checkout = rc.ReapResponse(
        ok=False, status=400, error="reap_status_400",
        error_code="AGENTIC_REQUEST_REJECTED", error_detail_code="ENROLLMENT_NOT_ACTIVE",
    )
    await _active_enrollment()
    purchase_id = await _start()
    await _step(purchase_id)
    await _step(purchase_id)
    stored = (await _get(purchase_id))["last_error_code"]
    assert stored == "enrollment_not_active"
    assert svc.ERROR_CODE_RE.match(stored)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 22. THE RESOLUTION HINTS SURVIVE start_purchase NOW  (#2206 adoption)
# ══════════════════════════════════════════════════════════════════════════════════════════════
#
# Until migration 225 these were REFUSED — `resolution_hints_not_persistable` — because the
# ledger had no columns for them and `advance` runs in another process, so accepting a purchase
# would have meant accepting an instruction we were going to drop. The columns exist; the refusal
# is gone; these tests are what stop it becoming a silent drop instead.


async def test_the_hints_a_human_confirmed_reach_the_resolver_that_needs_them(reap, attribution):
    """THE WHOLE POINT OF THE COLUMNS. `accept_variant_labels` is the client's documented remedy
    for a merchant whose label spells the shade differently — without it that row refuses as
    `options:sole_label_differs` for ever, blaming the label for what is really our storage.

    Asserted at the RESOLVER's own call, not just on the row: a value that is persisted and then
    not passed on is the same outcome as one that was never persisted."""
    purchase_id = await _start(
        row=_row(
            accept_variant_labels=("Flamingo Flirt - Cream", "Flamingo Flirt Cream"),
            also_accept_domains=("shop.brand.example", "Brand.Example"),
            market_country="us",
        )
    )
    stored = await _get(purchase_id)
    assert stored["accept_variant_labels"] == [
        "Flamingo Flirt - Cream", "Flamingo Flirt Cream"
    ]
    assert stored["also_accept_domains"] == ["shop.brand.example", "brand.example"]
    assert stored["market_country"] == "US"

    await _step(purchase_id)
    sent = reap.named("resolve_our_row")[0]
    assert sent["accept_variant_labels"] == (
        "Flamingo Flirt - Cream", "Flamingo Flirt Cream"
    )
    assert sent["also_accept_domains"] == ("shop.brand.example", "brand.example")
    assert sent["country"] == "US"


async def test_a_row_with_no_hints_passes_none_of_them(reap, attribution):
    """CONTROL. The absence has to stay an absence — `None`, not `[]` — because that is the one
    spelling the ledger stores, and the resolver reads an empty sequence as "no aliases"."""
    # The shared `_row()` fixture carries market_country="US", so it is cleared explicitly here
    # rather than by omission — a test of absence must not depend on a default.
    purchase_id = await _start(row=_row(market_country=None))
    stored = await _get(purchase_id)
    assert stored["accept_variant_labels"] is None
    assert stored["also_accept_domains"] is None
    assert stored["market_country"] is None

    await _step(purchase_id)
    sent = reap.named("resolve_our_row")[0]
    assert sent["accept_variant_labels"] == ()
    assert sent["also_accept_domains"] == ()
    assert sent["country"] is None


@pytest.mark.parametrize("given,expected", [("us", "US"), ("Us", "US"), (" gb ", "GB")])
async def test_the_market_country_is_uppercased_on_our_side(given, expected, reap):
    """THE LEDGER REFUSES RATHER THAN NORMALISING — its guard is `^[A-Z]{2}` — so an ordinary
    lowercase `"us"` from a caller would come back as a ValueError about a value the caller would
    reasonably think was fine. Case is not a decision anybody is making here, so it is normalised
    on this side, where a caller's mistake is still a caller's mistake."""
    purchase_id = await _start(row=_row(market_country=given))
    assert (await _get(purchase_id))["market_country"] == expected


@pytest.mark.parametrize("bad", ["USA", "u", "1s", "", "  "])
async def test_a_country_that_is_not_two_letters_is_still_refused(bad, reap):
    with pytest.raises(svc.PurchaseRefused) as exc:
        await _start(row=_row(market_country=bad))
    assert exc.value.reason == "invalid_request"
    assert await _count() == 0


HOSTILE_HINTS = [
    ("thirty-three labels", dict(accept_variant_labels=tuple(f"l{i}" for i in range(33)))),
    ("a label of 129 characters", dict(accept_variant_labels=("x" * 129,))),
    ("a label carrying a newline", dict(accept_variant_labels=("Flamingo\nFlirt",))),
    ("a label carrying a NUL", dict(accept_variant_labels=("Flamingo\x00Flirt",))),
    ("a blank label", dict(accept_variant_labels=("   ",))),
    ("a label that is not a string", dict(accept_variant_labels=(123,))),
    ("a domain with a scheme", dict(also_accept_domains=("https://shop.brand.example",))),
    ("a domain with a path", dict(also_accept_domains=("brand.example/shop",))),
    ("a domain with a port", dict(also_accept_domains=("brand.example:8443",))),
]


@pytest.mark.parametrize(
    "label,over", HOSTILE_HINTS, ids=[case[0] for case in HOSTILE_HINTS]
)
async def test_a_hint_the_ledger_refuses_is_a_purchase_refusal_not_a_valueerror(
    label, over, reap
):
    """THE LEDGER OWNS THE BOUNDS AND IT RAISES RATHER THAN TRUNCATING — deliberately, because a
    silently shortened alias is an alias that names a DIFFERENT object, and the whole purpose of
    the column is to say two names mean the same thing.

    What this module owns is the exception TYPE. A bare `ValueError` escaping `start_purchase`
    would be the one caller error on this surface that a route could not map, so it is turned
    into the same `PurchaseRefused` as every other bad input — and, like every other refusal
    here, it leaves no row behind."""
    with pytest.raises(svc.PurchaseRefused) as exc:
        await _start(row=_row(**over))
    assert exc.value.reason == "invalid_request"
    assert await _count() == 0
    assert reap.calls == []


async def test_the_hints_survive_a_terminal_transition(reap, attribution):
    """NOT PII, SO NOT NULLED. `shipping_address` and `buyer_email` go on a terminal write; these
    are catalog assertions about products and hostnames and name nobody, and a completed purchase
    that KEPT the alias it resolved through is exactly the record you want when that alias is
    questioned later."""
    reap.resolve_our_row = rc.VariantResolution(ok=False, reason="search:merchant_not_in_results")
    purchase_id = await _start(
        row=_row(accept_variant_labels=("Flamingo Flirt - Cream",), market_country="US")
    )
    await _step(purchase_id)
    row = await _get(purchase_id)
    assert row["state"] == "refused"
    assert row["buyer_email"] is None, "control: the PII did go"
    assert row["accept_variant_labels"] == ["Flamingo Flirt - Cream"]
    assert row["market_country"] == "US"


async def test_the_hints_are_not_in_the_owner_facing_view(reap):
    """Harmless to show, but the allowlist is kept minimal on principle and an owner-facing read
    has no use for our resolver's search configuration."""
    purchase_id = await _start(row=_row(accept_variant_labels=("Flamingo Flirt - Cream",)))
    view = await ledger.get_purchase_for_owner(purchase_id, "agent_one", "hash_alice")
    assert "accept_variant_labels" not in view
    assert "also_accept_domains" not in view
    assert "market_country" not in view


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 23. A RELEASE NOW RECORDS WHY  (#2206 adoption)
# ══════════════════════════════════════════════════════════════════════════════════════════════


async def test_a_transport_failure_records_its_reason_on_the_row(reap, attribution):
    """IT USED TO RECORD ONLY A SCHEDULE. `release_claim` took `next_poll_at` and nothing else,
    and there are no self-edges, so the code existed on the returned `AdvanceResult` and in one
    log line and nowhere a human would look. A stalled row showed a future poll time and no
    cause."""
    reap.resolve_our_row = _transport_resolution()
    purchase_id = await _start()
    result = await _step(purchase_id)

    assert result.outcome == "released"
    row = await _get(purchase_id)
    assert row["state"] == "resolving", "still not a transition"
    assert row["last_error_code"] == "transport_error:readtimeout"
    assert result.last_error_code == row["last_error_code"]


async def test_the_code_a_release_writes_is_folded_or_the_ledger_refuses_it(reap, attribution):
    """THE FOLD IS NOT COSMETIC HERE ANY MORE. `release_claim` validates against
    `^[a-z0-9_:.-]{1,64}` and REFUSES rather than folding — and this module builds
    `transport_error:ReadTimeout` out of an httpx exception TYPE name, which is mixed case by
    construction. Unfolded, every transport release on this rail would raise a ValueError out of
    `advance`.

    Asserted against the ledger's own pattern, so the two cannot drift."""
    reap.get_enrollment = _ok(dict(ENROLLMENT_ACTIVE, status="SOMETHING_NEW"))
    purchase_id = await _start()
    await _step(purchase_id)
    result = await _step(purchase_id)

    assert result.last_error_code == "unknown_enrollment_status"
    stored = (await _get(purchase_id))["last_error_code"]
    assert ledger._ERROR_CODE_RE.match(stored)
    assert ledger._require_error_code(stored) == stored, "the ledger would accept this again"


async def test_a_release_with_no_code_keeps_the_one_already_there(reap, attribution):
    """`None` means "do not write one", not "clear it" — the release statement COALESCEs. A buyer
    still looking at the hosted page must not erase the transport failure that preceded them."""
    reap.get_checkout = _ok(CHECKOUT_CREATED)  # REQUIRES_ACTION -> awaiting_buyer, no code
    purchase_id = await _start()
    await _step(purchase_id)
    await _step(purchase_id)
    await _step(purchase_id)
    await database.execute(
        "UPDATE reap_agentic_purchases SET last_error_code = 'earlier_trouble' WHERE id = :i",
        {"i": purchase_id},
    )
    result = await _step(purchase_id)
    assert result.outcome == "released"
    assert result.last_error_code is None
    assert (await _get(purchase_id))["last_error_code"] == "earlier_trouble"


async def test_a_clean_completion_clears_a_stale_code(reap, attribution):
    """THE TERMINAL WRITE NO LONGER COALESCES, and this is the behaviour that depends on it. A
    purchase that hit a transport failure, retried and then completed cleanly must not carry
    `transport_error:readtimeout` for ever — a terminal row's `last_error_code` is read as "why
    this ended", and a stale one says the purchase failed when it did not.

    Driven through the real sequence rather than by setting the column, so it is the module's own
    `last_error_code=None` on the completing transition that does the clearing."""
    reap.resolve_our_row = [_transport_resolution(), _resolved()]
    await _active_enrollment()
    purchase_id = await _start()

    assert (await _step(purchase_id)).outcome == "released"
    assert (await _get(purchase_id))["last_error_code"] == "transport_error:readtimeout"

    assert (await _step(purchase_id)).state == "quoting"
    assert (await _step(purchase_id)).state == "awaiting_approval"
    assert (await _step(purchase_id)).state == "completed"

    row = await _get(purchase_id)
    assert row["state"] == "completed"
    assert row["last_error_code"] is None, (
        "a clean completion must clear the stale code, not inherit it"
    )
    assert len(attribution.calls) == 1


async def test_a_terminal_failure_still_records_its_own_code_over_a_stale_one(reap, attribution):
    """CONTROL for the test above: "no COALESCE on terminal" must not mean "terminal always
    clears". An explicit code on the terminal write wins."""
    reap.resolve_our_row = [_transport_resolution(), _resolved(price=(99.99, "USD"))]
    purchase_id = await _start()
    await _step(purchase_id)
    assert (await _get(purchase_id))["last_error_code"] == "transport_error:readtimeout"

    reap.create_enrollment = rc.ReapResponse(
        ok=False, status=400, error="reap_status_400", error_code="AGENTIC_REQUEST_REJECTED"
    )
    await _step(purchase_id)
    row = await _get(purchase_id)
    assert row["state"] == "refused"
    assert row["refusal_reason"] == "price_changed"
    assert row["last_error_code"] is None, (
        "this refusal names itself in refusal_reason and carries no code of its own"
    )


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 24. THE ENROLLMENT READ IS A READ  (#2206 adoption)
# ══════════════════════════════════════════════════════════════════════════════════════════════


async def test_polling_an_enrollment_writes_nothing_to_the_enrollments_table(reap, attribution):
    """IT USED TO BE AN UPSERT. `get_active_enrollment` cannot see a PENDING row by definition, so
    recovering the partner's id meant calling `upsert_pending_enrollment` — a WRITE, unfenced,
    which bumped `updated_at` on every single poll and could mint a stray pending row."""
    reap.get_enrollment = _ok(ENROLLMENT_CREATED)  # still pending; the step will keep polling
    purchase_id = await _start()
    await _step(purchase_id)
    before = await database.fetch_all("SELECT * FROM reap_agentic_enrollments")
    assert len(before) == 1

    for _ in range(3):
        assert (await _step(purchase_id)).outcome == "released"

    after = await database.fetch_all("SELECT * FROM reap_agentic_enrollments")
    assert len(after) == 1, "polling must not mint enrollment rows"
    assert after[0]["updated_at"] == before[0]["updated_at"], (
        "a read must not touch updated_at — the old upsert did, on every poll"
    )


async def test_a_purchase_pointing_at_a_missing_enrollment_fails_by_name(reap, attribution):
    """None from the reader now means ONE thing — there is no such row — so this code is a true
    statement. Under the upsert it was a bucket that also caught a lost claim and a row that had
    stopped being pending mid-poll."""
    purchase_id = await _start()
    await _step(purchase_id)
    await database.execute("DELETE FROM reap_agentic_enrollments")
    result = await _step(purchase_id)
    assert result.state == "failed"
    assert (await _get(purchase_id))["last_error_code"] == "enrollment_row_unreadable"
    assert reap.named("get_enrollment") == []


async def test_the_unexported_reader_is_not_the_one_we_use(reap):
    """`get_enrollment_by_reap_id` exists on the ledger and is deliberately NOT exported. This
    module addresses its own row by OUR id, which is the id the purchase row carries and the one
    whose ownership the caller has already established."""
    import pathlib

    source = pathlib.Path(svc.__file__).read_text(encoding="utf-8")
    assert "get_enrollment_internal" in source
    assert "get_enrollment_by_reap_id" not in source
    assert "get_enrollment_by_reap_id" not in ledger.__all__


# ── the LIVE quote shape (2026-09-18): no `items`, nested int tax, four priced options ────────


async def test_the_happy_path_completes_on_the_live_quote_shape(reap, attribution):
    """Before the fix, EVERY real quote refused as `quote_items_mismatch`: the lane could never
    complete a purchase once armed. On the shape Reap actually sends, it completes."""
    reap.request_quote = _ok(LIVE_QUOTE)
    purchase_id = await _drive_to_completed(reap, attribution)
    row = await _get(purchase_id)
    assert row["state"] == "completed" and row["last_error_code"] is None
    assert (row["quoted_total_minor"], row["shipping_minor"], row["tax_minor"]) == (4500, 250, 0)
    assert len(attribution.calls) == 1


async def test_an_absent_items_key_skips_only_the_echo(reap, attribution):
    """Every other rule still runs without an echo — the subtotal above all."""
    payload = json.loads(json.dumps(LIVE_QUOTE))
    payload["amountBreakdown"]["itemsSubtotal"] = _usd(42.49)
    payload["amountBreakdown"]["finalAmount"] = _usd(44.99)
    result, row = await _quote_outcome(reap, payload)
    assert row["state"] == "refused"
    assert row["last_error_code"] == "quote_items_subtotal_mismatch"


@pytest.mark.parametrize(
    "options",
    [
        None, "ship_std", [{}], [{"id": "x"}],
        [{"id": "x", "price": {"amount": 1.0, "currency": "EUR"}}],
        [{"id": "x", "price": {"amount": -1.0, "currency": "USD"}}],
    ],
    ids=["null", "string", "empty-object", "id-only", "wrong-currency", "negative"],
)
def test_present_shipping_options_must_each_be_priced_in_the_row_currency(options):
    row = {"currency": "USD", "our_price_minor": 4250, "quantity": 1}
    payload = dict(LIVE_QUOTE, shippingOptions=options)
    check = svc.verify_quote(payload, row, variant_id="var_abc123")
    assert (check.ok, check.last_error_code) == (False, "quote_shipping_options_malformed")


def test_absent_or_empty_shipping_options_are_allowed_on_the_variant_lane():
    row = {"currency": "USD", "our_price_minor": 4250, "quantity": 1}
    assert svc.verify_quote({k: v for k, v in LIVE_QUOTE.items() if k != "shippingOptions"},
                            row, variant_id="var_abc123").ok
    assert svc.verify_quote(dict(LIVE_QUOTE, shippingOptions=[]), row,
                            variant_id="var_abc123").ok


# ── mig 233: consent is required on EVERY lane, and lands on the row ─────────────────────────
#
# It used to be the cart-link lane's alone (#2219), checked here for direct callers while the
# variant lane relied on the route having checked it. Migration 233 put the evidence on the
# purchase row, so a purchase opened with no consent is now a row nothing can explain, whichever
# lane opened it.


async def test_a_purchase_opened_through_this_service_carries_its_consent():
    purchase_id = await _start()
    row = await ledger.get_purchase_internal(purchase_id)
    assert row["consent_version"] == CONSENT
    assert row["consented_at"] is not None


async def test_the_variant_lane_refuses_without_a_consent_and_writes_nothing():
    """BEFORE THE INSERT — the contract `test_every_refusal_leaves_the_table_empty` states for
    every other refusal on this path, now including this one. A row written and then refused
    would carry the buyer's address into a state no transition ever nulls."""
    before = await _count()
    with pytest.raises(svc.PurchaseRefused) as caught:
        await _start(consent_version=None)
    assert caught.value.reason == "consent_required"
    assert await _count() == before


@pytest.mark.parametrize(
    "value",
    ["", "   ", "v" * 33, "v1\x00", "v1\x1b[31m"],
)
async def test_a_malformed_consent_is_refused_on_the_variant_lane_too(value):
    """The SAME three checks the route applies, so a direct caller cannot store a tag the route
    would have refused. `consent_required`, not `invalid_request`: a string-shaped value that is
    not usable is a missing act by a human, which is what the route's vocabulary says too."""
    before = await _count()
    with pytest.raises(svc.PurchaseRefused) as caught:
        await _start(consent_version=value)
    assert caught.value.reason == "consent_required"
    assert await _count() == before


async def test_the_consent_is_refused_before_the_item_is_looked_at():
    """ORDER. A caller that has not consented must not be able to learn, from the SHAPE of the
    refusal, whether its row would otherwise have been accepted — so the consent check sits
    ahead of every check on the row. A row that is ALSO malformed still answers
    `consent_required`, which is what proves the order rather than asserting it in prose."""
    with pytest.raises(svc.PurchaseRefused) as caught:
        await _start(consent_version=None, row=_row(currency="not-a-currency"))
    assert caught.value.reason == "consent_required"


async def test_the_consent_is_refused_after_the_dial_so_a_dark_rail_stays_dark(monkeypatch):
    """THE OTHER HALF OF THE ORDER, and the one with a security consequence. The dials run first:
    a `consent_required` from a rail that is switched off would be a working probe for a feature
    that is supposed to be absent."""
    monkeypatch.delenv("REAP_AGENTIC_ENABLED", raising=False)
    with pytest.raises(svc.PurchaseRefused) as caught:
        await _start(consent_version=None)
    assert caught.value.reason == "rail_disabled"


async def test_the_consent_survives_the_purchase_reaching_a_terminal_state():
    """END TO END through the service, not only through the ledger's own statement: whatever the
    poller does to this row, the consent is still on it when the row is terminal and the buyer's
    email and address are gone."""
    purchase_id = await _start()
    await ledger.transition(purchase_id, from_states=["resolving"], to_state="refused")

    row = await ledger.get_purchase_internal(purchase_id)
    assert row["state"] == "refused"
    assert row["buyer_email"] is None and row["shipping_address"] is None
    assert row["consent_version"] == CONSENT


# ── attribution keys ONE merchant, whichever spelling bought ────────────────────────────────


async def test_two_spellings_of_one_merchant_close_under_one_merchant_key(attribution):
    """The purchase row keeps the host as the door observed it; the attribution ledger must not.
    `www.brand.example` and `brand.example` are one merchant on the variant lane, and two
    `merchant_id`s would split its GMV across two rows."""
    import services.reap_agentic_purchase as svc

    for n, spelling in enumerate(("www.brand.example", "brand.example", "WWW.Brand.Example")):
        assert await svc._close_attribution(
            {"id": f"rp_spell_{n}", "merchant_domain": spelling, "reap_order_id": f"ord_s{n}",
             "final_total_minor": 4500, "currency": "USD", "click_id": f"clk_s{n}"}
        )
    assert len(attribution.calls) == 3
    assert {c["merchant_id"] for c in attribution.calls} == {"brand.example"}
    assert {c["converting_shop_domain"] for c in attribution.calls} == {"brand.example"}


async def test_an_uncanonicalisable_stored_domain_still_closes_as_stored(attribution):
    """NEVER RAISES: the closer swallows exceptions to protect a completed purchase, so a raise
    in the key function would silently drop the edge. A legacy value is used as stored."""
    import services.reap_agentic_purchase as svc

    assert await svc._close_attribution(
        {"id": "rp_legacy", "merchant_domain": "Legacy.Example.", "reap_order_id": "ord_leg",
         "final_total_minor": 4500, "currency": "USD", "click_id": "clk_leg"}
    )
    assert [c["merchant_id"] for c in attribution.calls] == ["legacy.example."]


# ── offer codes on the VARIANT lane (2026-09-28) ─────────────────────────────────────────────
# The fallback helper is shared with the cart-link lane (tests/reap_cart_link_cases.py carries
# the full table); these pin that the items quote goes through it too.


def _offer_rejected(code):
    return rc.ReapResponse(ok=False, status=400, error="reap_status_400", error_code=code)


def _discounted(amount=-4.25, final=40.75):
    quote = json.loads(json.dumps(QUOTE_200))
    quote["amountBreakdown"]["discounts"] = [
        {"name": "Offer code", "amount": {"amount": amount, "currency": "USD"}}]
    quote["amountBreakdown"]["finalAmount"] = {"amount": final, "currency": "USD"}
    return quote


async def _to_quoting_with_code(code):
    purchase_id = await _start(offer_code=code)
    await _step(purchase_id)  # resolving -> needs_enrollment
    await _step(purchase_id)  # needs_enrollment -> quoting
    return purchase_id


async def test_the_items_quote_carries_the_code_and_records_the_discount(reap, attribution):
    purchase_id = await _to_quoting_with_code("SAVE10")
    reap.calls.clear()
    reap.request_quote = _ok(_discounted())
    assert (await _step(purchase_id)).state == "awaiting_approval"
    (call,) = reap.named("request_quote")
    assert call["offer_code"] == "SAVE10"
    row = await ledger.get_purchase_internal(purchase_id)
    assert (row["offer_code_outcome"], row["discount_minor"], row["quoted_total_minor"]) == (
        "applied", 425, 4075)


async def test_the_items_quote_drops_a_refused_code_and_requotes_once(reap, attribution):
    purchase_id = await _to_quoting_with_code("BOGUS")
    reap.calls.clear()
    reap.request_quote = [_offer_rejected("OFFER_CODE_INVALID"), _ok(QUOTE_200)]
    assert (await _step(purchase_id)).state == "awaiting_approval"
    first, second = reap.named("request_quote")
    assert first["offer_code"] == "BOGUS" and "offer_code" not in second
    assert first["items"] == second["items"]
    # The step re-resolves ONCE, not once per quote.
    assert reap.sequence().count("resolve_our_row") == 1
    row = await ledger.get_purchase_internal(purchase_id)
    assert row["offer_code_outcome"] == "dropped_invalid" and row["quoted_total_minor"] == 4500


@pytest.mark.parametrize("resolve_seconds,expect_quotes", [(114.0, 0), (112.0, 1)])
async def test_a_slow_resolve_leaves_no_budget_for_a_coded_quote(
    reap, attribution, monkeypatch, resolve_seconds, expect_quotes
):
    """B2(a), #2425 review. The variant lane resolves BEFORE it quotes, inside the same step
    budget (135 s for quoting). A resolve that ate more than 113 s leaves under
    MIN_QUOTE_BUDGET_S (22 s): the step is released as `quote_budget_exhausted` and NO quote is
    sent. 112 s is the control: 23 s left, one quote."""
    clock = {"now": 1000.0}
    monkeypatch.setattr(svc, "_monotonic", lambda: clock["now"])
    purchase_id = await _to_quoting_with_code("SAVE10")
    reap.calls.clear()

    def _slow_resolve(**kwargs):
        clock["now"] += resolve_seconds
        return _resolved()

    reap.resolve_our_row = _slow_resolve
    reap.request_quote = _ok(_discounted())
    moved = await _step(purchase_id)
    assert len(reap.named("request_quote")) == expect_quotes
    if expect_quotes == 0:
        assert moved.outcome == "released" and moved.state == "quoting"
        assert moved.last_error_code == "quote_budget_exhausted"
    else:
        assert moved.state == "awaiting_approval"
        assert reap.named("request_quote")[0]["timeout_seconds"] == pytest.approx(23.0)


# ── 2026-09-30: a returning buyer whose card IS active at Reap could never buy ───────────────
#
# Staging, all UTC, one demo buyer:
#   11:21:34  purchase A -> needs_enrollment; our row re_…, Reap enrollment 9041ef1a-…, link
#             expires 11:36:34.
#   ~11:23    buyer completes Reap's page. GET keeps saying REQUIRES_ACTION…
#   11:36:43  …until now: ACTIVE, last4 7847. 9 s AFTER the link expired.
#   11:37:02  the sweep expires A (hosted_url_expired). Our row stays 'pending'.
#   11:37:38  purchase B. No active row; `upsert_pending_enrollment` hands back the SAME pending
#             row; `create_enrollment` replays the SAME attempt and returns the OLD dead link;
#             B enters needs_enrollment already expired and is swept 54 s later.
#   11:41     purchase C, the same.
# The sequence below replays that against the real ledger and a fake Reap, then shows the fix.

STAGING_ENROLLMENT = "9041ef1a-1377-45f6-b09a-95d5eb07f908"
STAGING_LINK = "https://pay.prava.space/enroll/ses_01M3S0RSP39FEXTT7ZRYHSRDJQ"


def _iso_in(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace(
        "+00:00", "Z"
    )


def _enrollment_requires_action(
    reap_id: str = STAGING_ENROLLMENT, url: str = STAGING_LINK, expires_in: float = 900
) -> dict:
    return {
        "id": reap_id,
        "status": "REQUIRES_ACTION",
        "nextAction": {"type": "REDIRECT", "url": url, "expiresAt": _iso_in(expires_in)},
    }


def _enrollment_active(reap_id: str = STAGING_ENROLLMENT, last4: str = "7847") -> dict:
    return {
        "id": reap_id,
        "status": "ACTIVE",
        "paymentMethod": {"type": "CARD", "network": "VISA", "last4": last4},
        "nextAction": None,
    }


async def _age_links(seconds_past: int) -> None:
    """Move EVERY hosted-link expiry (enrollment rows and purchase rows) to `seconds_past` ago —
    the server-side way to let time pass for both the sweep and the service."""
    for table in ("reap_agentic_enrollments", "reap_agentic_purchases"):
        await database.execute(
            f"UPDATE {table} SET hosted_url_expires_at = datetime('now', :shift) "
            "WHERE hosted_url_expires_at IS NOT NULL",
            # SIGNED, so a negative `seconds_past` (time LEFT) is '+30 seconds', not the
            # '--30 seconds' SQLite reads as NULL — which would erase the expiry, not move it.
            {"shift": f"{-int(seconds_past):+d} seconds"},
        )


async def _enrollments(buyer_ref: str = "bref_alice"):
    return [
        dict(r) for r in await database.fetch_all(
            "SELECT * FROM reap_agentic_enrollments WHERE buyer_ref = :b ORDER BY created_at, id",
            {"b": buyer_ref},
        )
    ]


def _reap_echoes_the_session():
    """A `get_enrollment` that answers REQUIRES_ACTION with the SAME session it created — the
    stored link and the stored expiry, read back from our row — the way Reap's GET echoes a
    session that is still open (or has just died). Moving our row's clock (`_age_links`) moves
    what this fake says, so a test ages ONE clock and not two."""

    async def _read(**kwargs):
        stored = await database.fetch_one(
            "SELECT hosted_url, hosted_url_expires_at FROM reap_agentic_enrollments "
            "WHERE reap_enrollment_id = :r",
            {"r": kwargs["id"]},
        )
        action = None
        if stored is not None and stored["hosted_url"]:
            expires = ledger._decode_dt(stored["hosted_url_expires_at"])
            action = {"type": "REDIRECT", "url": stored["hosted_url"]}
            if expires is not None:
                action["expiresAt"] = expires.isoformat().replace("+00:00", "Z")
        return _ok({"id": kwargs["id"], "status": "REQUIRES_ACTION", "nextAction": action})

    return _read


async def _purchase_a_waiting_on_a_link(reap) -> tuple:
    """Step 1 and 2: A reaches needs_enrollment; Reap keeps saying REQUIRES_ACTION."""
    reap.create_enrollment = _ok(_enrollment_requires_action())
    reap.get_enrollment = _reap_echoes_the_session()
    purchase_a = await _start()
    assert (await _step(purchase_a)).state == "needs_enrollment"
    ours = (await _get(purchase_a))["enrollment_id"]
    result = await _step(purchase_a)
    assert (result.outcome, result.last_error_code) == ("released", "enrollment_pending")
    return purchase_a, ours


async def test_the_staging_sequence_a_returning_buyer_with_an_active_card_now_buys(reap):
    """Steps 1–5 exactly, then the fix. A is expired the way staging expired it (the sweep on
    Reap's exact expiry, `enrollment_grace_seconds=0`), our row is left 'pending' while Reap
    says ACTIVE — and B now RECONCILES instead of replaying: one GET, the row is activated with
    last4 7847, B goes to 'quoting', and NO enrollment is minted at Reap."""
    purchase_a, ours = await _purchase_a_waiting_on_a_link(reap)

    # Step 2/3: the link expires; 9 s later Reap says ACTIVE; 28 s after expiry the sweep runs.
    reap.get_enrollment = _ok(_enrollment_active())
    await _age_links(28)
    assert await ledger.expire_overdue_purchases(enrollment_grace_seconds=0) == [purchase_a]
    assert (await _get(purchase_a))["state"] == "expired"
    [row] = await _enrollments()
    assert (row["id"], row["status"]) == (ours, "pending"), "the staging state: row still pending"

    # Step 4: purchase B, same buyer.
    reap.calls.clear()
    purchase_b = await _start()
    result = await _step(purchase_b)

    assert result.state == "quoting"
    assert reap.named("create_enrollment") == [], "no new enrollment minted at Reap"
    assert [c["id"] for c in reap.named("get_enrollment")] == [STAGING_ENROLLMENT]
    b = await _get(purchase_b)
    assert b["enrollment_id"] == ours
    assert b["hosted_url"] is None and b["hosted_url_expires_at"] is None
    active = await ledger.get_active_enrollment("bref_alice")
    assert (active["id"], active["card_last4"], active["card_network"]) == (ours, "7847", "VISA")
    assert active["hosted_url"] is None

    # Step 5: purchase C goes straight to quoting on the active row; not even a GET.
    reap.calls.clear()
    purchase_c = await _start()
    assert (await _step(purchase_c)).state == "quoting"
    assert reap.calls == [("resolve_our_row", reap.calls[0][1])]
    assert (await _get(purchase_c))["enrollment_id"] == ours

    # And B completes on that card.
    assert (await _step(purchase_b)).state == "awaiting_approval"
    assert reap.named("create_checkout")[-1]["enrollment_id"] == STAGING_ENROLLMENT


async def test_the_staging_sequence_with_the_grace_a_itself_proceeds(reap):
    """The same timeline with the DEFAULT sweep: A is NOT expired 28 s after its link, the
    poller reads ACTIVE, and A goes to 'quoting' with its hosted link cleared."""
    purchase_a, ours = await _purchase_a_waiting_on_a_link(reap)
    reap.get_enrollment = _ok(_enrollment_active())
    await _age_links(28)
    assert await ledger.expire_overdue_purchases() == []
    result = await _step(purchase_a)
    assert result.state == "quoting"
    a = await _get(purchase_a)
    assert a["enrollment_id"] == ours
    assert a["hosted_url"] is None and a["hosted_url_expires_at"] is None
    assert (await ledger.get_active_enrollment("bref_alice"))["card_last4"] == "7847"


async def test_still_requires_action_after_the_grace_the_sweep_expires(reap):
    purchase_a, _ = await _purchase_a_waiting_on_a_link(reap)
    await _age_links(181)
    assert await ledger.expire_overdue_purchases() == [purchase_a]


async def test_a_live_pending_link_is_reused_not_reminted(reap):
    """REQUIRES_ACTION with the link still live: B waits on the SAME row and the SAME link. One
    pending row per buyer; no second enrollment at Reap."""
    purchase_a, ours = await _purchase_a_waiting_on_a_link(reap)
    reap.calls.clear()
    purchase_b = await _start()
    result = await _step(purchase_b)
    assert result.state == "needs_enrollment"
    assert reap.named("create_enrollment") == []
    b = await _get(purchase_b)
    assert b["enrollment_id"] == ours
    assert b["hosted_url"] == STAGING_LINK
    assert b["hosted_url_expires_at"] == (await _get(purchase_a))["hosted_url_expires_at"]
    assert len(await _enrollments()) == 1


async def test_a_dying_link_is_not_reused_inside_the_grace_the_purchase_holds(reap):
    """REFUSING EXAMPLE, twice: a link with < 60 s left, and one 9 s dead, are not handed to B —
    and inside the grace the row is not retired either (Reap may still turn it ACTIVE). B holds
    in 'resolving' on the needs_enrollment cadence and mints nothing."""
    await _purchase_a_waiting_on_a_link(reap)
    for seconds_past in (-30, 9):  # 30 s LEFT, then 9 s PAST
        await _age_links(seconds_past)
        reap.calls.clear()
        purchase_b = await _start()
        result = await _step(purchase_b)
        assert (result.outcome, result.state) == ("released", "resolving")
        assert result.last_error_code == "enrollment_settling"
        assert result.next_poll_in_seconds == 30
        assert reap.named("create_enrollment") == []
        assert [r["status"] for r in await _enrollments()] == ["pending"]
        b = await _get(purchase_b)
        assert b["hosted_url"] is None and b["enrollment_id"] is None


async def test_a_held_purchase_proceeds_when_reap_turns_active(reap):
    await _purchase_a_waiting_on_a_link(reap)
    await _age_links(9)
    purchase_b = await _start()
    assert (await _step(purchase_b)).last_error_code == "enrollment_settling"
    reap.get_enrollment = _ok(_enrollment_active())
    assert (await _step(purchase_b)).state == "quoting"
    assert reap.named("create_enrollment") == [reap.named("create_enrollment")[0]], "only A's"


@pytest.mark.parametrize(
    "reap_says,seconds_past",
    [("REQUIRES_ACTION", 400), ("EXPIRED", 400), ("FAILED", -600), ("REVOKED", 9)],
)
async def test_a_dead_pending_enrollment_is_retired_and_a_new_attempt_minted(
    reap, reap_says, seconds_past
):
    """The FRESH-ATTEMPT path. REQUIRES_ACTION past the grace, or a terminal status at any time:
    the old row goes 'dead', a NEW row (a NEW attempt id, so a new idempotency key at Reap) is
    minted, and B is handed the NEW link — never the dead one."""
    _, old = await _purchase_a_waiting_on_a_link(reap)
    await _age_links(seconds_past)
    if reap_says != "REQUIRES_ACTION":
        reap.get_enrollment = _ok(dict(_enrollment_requires_action(), status=reap_says, nextAction=None))
    new_reap_id = "22222222-2222-2222-2222-222222222222"
    new_link = "https://pay.prava.space/enroll/ses_NEW"
    reap.create_enrollment = _ok(_enrollment_requires_action(new_reap_id, new_link))
    reap.calls.clear()

    purchase_b = await _start()
    result = await _step(purchase_b)

    assert result.state == "needs_enrollment"
    [created] = reap.named("create_enrollment")
    assert created["attempt_id"] != old
    rows = {r["id"]: r for r in await _enrollments()}
    assert rows[old]["status"] == "dead"
    assert rows[old]["reap_status"] == reap_says
    fresh = rows[created["attempt_id"]]
    assert (fresh["status"], fresh["reap_enrollment_id"]) == ("pending", new_reap_id)
    assert sum(r["status"] == "pending" for r in rows.values()) == 1
    b = await _get(purchase_b)
    assert (b["enrollment_id"], b["hosted_url"]) == (created["attempt_id"], new_link)


async def test_a_stored_link_we_would_not_vouch_for_is_never_reused(reap):
    """REFUSING EXAMPLE: the row's link is re-checked against the allowlist before it is handed
    to another purchase. One that fails is retired and a fresh attempt minted."""
    _, old = await _purchase_a_waiting_on_a_link(reap)
    await database.execute(
        "UPDATE reap_agentic_enrollments SET hosted_url = 'https://evilprava.space/enroll/x' "
        "WHERE id = :i", {"i": old},
    )
    reap.create_enrollment = _ok(
        _enrollment_requires_action("33333333-3333-3333-3333-333333333333")
    )
    purchase_b = await _start()
    assert (await _step(purchase_b)).state == "needs_enrollment"
    assert "evilprava" not in str((await _get(purchase_b))["hosted_url"])
    assert (await ledger.get_enrollment_internal(old))["status"] == "dead"


async def test_an_unknown_status_on_the_pending_enrollment_never_advances(reap):
    """REFUSING EXAMPLE: not reused, not retired, not activated, nothing minted."""
    _, old = await _purchase_a_waiting_on_a_link(reap)
    reap.get_enrollment = _ok(dict(_enrollment_active(), status="SOMETHING_NEW"))
    reap.calls.clear()
    purchase_b = await _start()
    result = await _step(purchase_b)
    assert (result.outcome, result.last_error_code) == ("released", "unknown_enrollment_status")
    assert reap.named("create_enrollment") == []
    assert (await ledger.get_enrollment_internal(old))["status"] == "pending"
    assert await ledger.get_active_enrollment("bref_alice") is None


async def test_a_transport_failure_reading_the_pending_enrollment_releases(reap):
    _, old = await _purchase_a_waiting_on_a_link(reap)
    reap.get_enrollment = _transport()
    reap.calls.clear()
    purchase_b = await _start()
    result = await _step(purchase_b)
    assert (result.outcome, result.state) == ("released", "resolving")
    assert result.last_error_code == "transport_error:readtimeout"
    assert result.next_poll_in_seconds == 120
    assert reap.named("create_enrollment") == []
    assert (await ledger.get_enrollment_internal(old))["status"] == "pending"


@pytest.mark.parametrize("status,code", [(500, "reap_status_500"), (404, "reap_status_404")])
async def test_a_failed_read_inside_the_grace_releases_without_minting(reap, status, code):
    """Review of #2483, P2-4. Inside the grace the row may yet settle: a failed read — any
    status, even a 404 — RELEASES with backoff. Nothing is minted around it, nothing retired."""
    _, old = await _purchase_a_waiting_on_a_link(reap)
    reap.get_enrollment = rc.ReapResponse(ok=False, status=status, error=code)
    reap.calls.clear()
    purchase_b = await _start()
    result = await _step(purchase_b)
    assert (result.outcome, result.state, result.last_error_code) == ("released", "resolving", code)
    assert result.next_poll_in_seconds == 120, "the doubled backoff, not the plain interval"
    assert reap.named("create_enrollment") == []
    assert (await ledger.get_enrollment_internal(old))["status"] == "pending"


async def test_a_partner_error_past_the_grace_fails_without_minting(reap):
    """FAIL CLOSED: past the grace, a failure that is NOT "no such enrollment" fails the
    purchase by the partner's code; the row stays for an operator (runbook)."""
    _, old = await _purchase_a_waiting_on_a_link(reap)
    await _age_links(400)
    reap.get_enrollment = rc.ReapResponse(ok=False, status=500, error="reap_status_500")
    reap.calls.clear()
    purchase_b = await _start()
    result = await _step(purchase_b)
    assert (result.state, result.last_error_code) == ("failed", "reap_status_500")
    assert reap.named("create_enrollment") == []
    assert (await ledger.get_enrollment_internal(old))["status"] == "pending"


@pytest.mark.parametrize(
    "response",
    [
        rc.ReapResponse(ok=False, status=404, error="reap_status_404"),
        rc.ReapResponse(ok=False, status=410, error="reap_status_410"),
        rc.ReapResponse(ok=False, status=400, error="reap_status_400",
                        error_code="AGENTIC_RESOURCE_NOT_FOUND"),
    ],
    ids=["404", "410", "not_found_code"],
)
async def test_no_such_enrollment_past_the_grace_retires_the_row_and_mints_fresh(reap, response):
    """Review of #2483, P2-4. The sticky mode: a pending row Reap no longer knows used to fail
    EVERY later purchase of the buyer. Past the grace it is retired with the partner's code and
    a new attempt is minted."""
    _, old = await _purchase_a_waiting_on_a_link(reap)
    await _age_links(400)
    reap.get_enrollment = response
    reap.create_enrollment = _ok(
        _enrollment_requires_action("99999999-9999-9999-9999-999999999999")
    )
    reap.calls.clear()
    purchase_b = await _start()
    assert (await _step(purchase_b)).state == "needs_enrollment"
    retired = await ledger.get_enrollment_internal(old)
    assert retired["status"] == "dead"
    [created] = reap.named("create_enrollment")
    assert created["attempt_id"] != old


async def test_a_malformed_partner_id_on_the_pending_row_fails_without_a_read(reap):
    _, old = await _purchase_a_waiting_on_a_link(reap)
    await database.execute(
        "UPDATE reap_agentic_enrollments SET reap_enrollment_id = 'not/a-uuid' WHERE id = :i",
        {"i": old},
    )
    reap.calls.clear()
    purchase_b = await _start()
    result = await _step(purchase_b)
    assert (result.state, result.last_error_code) == ("failed", "partner_id_malformed")
    assert reap.named("get_enrollment") == [] and reap.named("create_enrollment") == []


async def test_the_reconcile_read_is_guarded_by_the_ownership_re_read(reap, monkeypatch):
    """A worker whose lease moved AFTER the step's earlier re-read (here: while the pending row
    was being read) spends no GET on the pending enrollment — the re-read in front of the GET is
    what catches it, not the one before the email check."""
    await _purchase_a_waiting_on_a_link(reap)
    purchase_b = await _start()
    await _claim(purchase_b, "w1")
    real = ledger.get_pending_enrollments

    async def _steal_then_read(buyer_ref):
        await database.execute(
            "UPDATE reap_agentic_purchases SET claimed_by = 'worker-b' WHERE id = :i",
            {"i": purchase_b},
        )
        return await real(buyer_ref)

    monkeypatch.setattr(ledger, "get_pending_enrollments", _steal_then_read)
    reap.calls.clear()
    result = await svc.advance(purchase_b, "w1")
    assert result.outcome == "lost_claim"
    assert reap.named("get_enrollment") == []
    assert reap.named("create_enrollment") == []


async def test_activation_that_loses_to_a_retire_does_not_quote(reap, monkeypatch):
    """FAIL CLOSED. Reap says ACTIVE but our row was retired between the read and the write, and
    the buyer has no other active card: 'resolving' holds, 'needs_enrollment' fails — neither
    quotes on a card our ledger calls dead."""
    purchase_a, old = await _purchase_a_waiting_on_a_link(reap)
    reap.get_enrollment = _ok(_enrollment_active())
    real = ledger.mark_enrollment_active

    async def _retired_first(enrollment_id, **kwargs):
        await ledger.mark_enrollment_dead(enrollment_id)
        return await real(enrollment_id, **kwargs)

    monkeypatch.setattr(ledger, "mark_enrollment_active", _retired_first)
    result = await _step(purchase_a)
    assert (result.state, result.last_error_code) == ("failed", "enrollment_not_activatable")


async def test_activation_that_loses_to_another_card_quotes_on_that_card(reap, monkeypatch):
    """The control: the concurrent winner IS an active card for this buyer, so it is the one."""
    purchase_a, old = await _purchase_a_waiting_on_a_link(reap)
    reap.get_enrollment = _ok(_enrollment_active())
    winner = await _active_enrollment(reap_id="44444444-4444-4444-4444-444444444444")

    async def _lost(enrollment_id, **kwargs):
        return None

    monkeypatch.setattr(ledger, "mark_enrollment_active", _lost)
    # The step reads get_active_enrollment FIRST and would short-circuit; hide the winner from
    # that first read only, so the activation path is the one under test.
    real_get = ledger.get_active_enrollment
    reads = []

    async def _hidden_once(buyer_ref):
        reads.append(buyer_ref)
        return None if len(reads) == 1 else await real_get(buyer_ref)

    monkeypatch.setattr(ledger, "get_active_enrollment", _hidden_once)
    result = await _step(purchase_a)
    assert result.state == "quoting"
    assert (await _get(purchase_a))["enrollment_id"] == winner["id"]


async def test_a_replayed_create_with_a_dead_link_is_retired_not_handed_out(reap):
    """THE OTHER REPLAY. A pending row with NO partner id (its create's response was lost) is
    replayed, as it should be — but the replay can come back with a link that is already dead.
    That link is recorded, retired, and never reaches the purchase; the next step mints a NEW
    attempt."""
    stale = await ledger.upsert_pending_enrollment(buyer_ref="bref_alice")
    reap.create_enrollment = _ok(_enrollment_requires_action(expires_in=-600))
    purchase = await _start()
    result = await _step(purchase)
    assert (result.outcome, result.state) == ("released", "resolving")
    assert result.last_error_code == "enrollment_link_expired"
    assert reap.named("create_enrollment")[0]["attempt_id"] == stale["id"]
    row = await ledger.get_enrollment_internal(stale["id"])
    assert (row["status"], row["reap_enrollment_id"]) == ("dead", STAGING_ENROLLMENT)
    assert (await _get(purchase))["hosted_url"] is None

    reap.create_enrollment = _ok(
        _enrollment_requires_action("55555555-5555-5555-5555-555555555555")
    )
    assert (await _step(purchase)).state == "needs_enrollment"
    assert reap.named("create_enrollment")[1]["attempt_id"] != stale["id"]


async def test_a_pending_row_the_ledger_refuses_holds_the_purchase(reap):
    """A pending row with a DEAD link but no partner id cannot be reconciled (nothing to read)
    and the ledger will not hand it back as an attempt. The purchase holds with a named code."""
    stale = await ledger.upsert_pending_enrollment(buyer_ref="bref_alice")
    await database.execute(
        "UPDATE reap_agentic_enrollments SET hosted_url_expires_at = datetime('now', '-60 seconds') "
        "WHERE id = :i", {"i": stale["id"]},
    )
    purchase = await _start()
    result = await _step(purchase)
    assert (result.outcome, result.last_error_code) == ("released", "enrollment_pending_expired")
    assert reap.named("create_enrollment") == []


@pytest.mark.parametrize(
    "raw,expected",
    [(None, 180), ("", 180), ("0", 0), ("600", 600), ("3600", 3600),
     ("-1", 180), ("3601", 180), ("abc", 180), ("1.5", 180)],
)
def test_the_enrollment_grace_dial(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv(svc.REAP_AGENTIC_ENROLLMENT_GRACE_SECONDS_ENV, raising=False)
    else:
        monkeypatch.setenv(svc.REAP_AGENTIC_ENROLLMENT_GRACE_SECONDS_ENV, raw)
    assert svc.enrollment_grace_seconds() == expected


async def test_the_grace_dial_moves_the_services_hold_window(reap, monkeypatch):
    """With the grace at 0, a link 9 s dead is past it: retired and re-minted, not held."""
    monkeypatch.setenv(svc.REAP_AGENTIC_ENROLLMENT_GRACE_SECONDS_ENV, "0")
    _, old = await _purchase_a_waiting_on_a_link(reap)
    await _age_links(9)
    reap.create_enrollment = _ok(
        _enrollment_requires_action("66666666-6666-6666-6666-666666666666")
    )
    purchase_b = await _start()
    assert (await _step(purchase_b)).state == "needs_enrollment"
    assert (await ledger.get_enrollment_internal(old))["status"] == "dead"


# ── review of #2483 at 93f585c26: the five findings, through `advance` on the real ledger ────


def _create_keyed_by_attempt():
    """`create_enrollment` as Reap's idempotency makes it: ONE enrollment per attempt id, and the
    same answer every time that attempt is replayed."""
    import uuid as _uuid

    def _create(**kwargs):
        attempt = str(kwargs["attempt_id"])
        enrollment = str(_uuid.uuid5(_uuid.NAMESPACE_OID, attempt))
        return _ok(_enrollment_requires_action(
            enrollment, url=f"https://pay.prava.space/enroll/{attempt}"
        ))

    return _create


async def _set_created_at(enrollment_id: str, seconds_ago: int) -> None:
    await database.execute(
        "UPDATE reap_agentic_enrollments SET created_at = datetime('now', :shift) WHERE id = :i",
        {"shift": f"-{int(seconds_ago)} seconds", "i": enrollment_id},
    )


async def _pending_count(buyer_ref: str = "bref_alice") -> int:
    return int(await database.fetch_val(
        "SELECT COUNT(*) FROM reap_agentic_enrollments WHERE buyer_ref = :b AND status = 'pending'",
        {"b": buyer_ref},
    ))


async def _drop_one_pending_index() -> None:
    """A database migration 252 has not reached (prod applies migrations by hand)."""
    await database.execute("DROP INDEX IF EXISTS uq_reap_agentic_enrollments_one_pending")


# P1-1 — a create that answers with an enrollment id another row of ours already holds.


async def test_r3_a_create_returning_an_id_we_hold_on_a_dead_row_fails_by_name(reap):
    """THE REVIEWER'S REPRO. A is retired past the grace (it keeps its partner id); B mints N;
    Reap answers N's create with A's enrollment. That used to be a raw IntegrityError out of
    `advance` on every poll, for every later purchase. Now: N is retired, B fails
    `enrollment_id_conflict`, A is not resurrected — and C, the next purchase, gets the same
    named answer instead of an exception."""
    _, old = await _purchase_a_waiting_on_a_link(reap)
    await _age_links(400)
    for _ in range(2):  # B, then C
        reap.calls.clear()
        purchase = await _start()
        result = await _step(purchase)
        assert (result.state, result.last_error_code) == ("failed", "enrollment_id_conflict")
        [created] = reap.named("create_enrollment")
        attempt = await ledger.get_enrollment_internal(created["attempt_id"])
        assert attempt["status"] == "dead" and attempt["reap_enrollment_id"] is None
        assert (await ledger.get_enrollment_internal(old))["status"] == "dead"
        assert await _pending_count() == 0
        assert await ledger.get_active_enrollment("bref_alice") is None


async def test_a_create_returning_another_buyers_enrollment_fails_and_touches_nothing(reap):
    """REFUSING EXAMPLE: the holder is ANOTHER buyer's row. Nothing of theirs is written."""
    bob = await ledger.upsert_pending_enrollment(
        buyer_ref="bref_bob", reap_enrollment_id=STAGING_ENROLLMENT,
        hosted_url=STAGING_LINK,
        hosted_url_expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    reap.create_enrollment = _ok(_enrollment_requires_action())
    purchase = await _start()
    result = await _step(purchase)
    assert (result.state, result.last_error_code) == ("failed", "enrollment_id_conflict")
    after = await ledger.get_enrollment_internal(bob["id"])
    assert (after["status"], after["hosted_url"]) == ("pending", STAGING_LINK)
    assert await _pending_count() == 0


async def test_a_create_returning_another_buyers_ACTIVE_card_never_quotes_on_it(reap):
    """FAIL CLOSED, the case that matters: the holder is another buyer's ACTIVE card. Quoting on
    it would charge THEIR card for OUR buyer's purchase. Refused by name; their row untouched."""
    theirs = "55555555-5555-5555-5555-555555555555"
    await database.execute(
        "INSERT INTO reap_agentic_enrollments (id, buyer_ref, status, reap_enrollment_id, "
        "card_last4) VALUES ('re_bobs_card', 'bref_bob', 'active', :r, '1111')",
        {"r": theirs},
    )
    reap.create_enrollment = _ok(_enrollment_requires_action(theirs))
    purchase = await _start()
    result = await _step(purchase)
    assert (result.state, result.last_error_code) == ("failed", "enrollment_id_conflict")
    assert (await _get(purchase))["enrollment_id"] != "re_bobs_card"
    assert reap.named("create_checkout") == []
    bob = await ledger.get_active_enrollment("bref_bob")
    assert (bob["id"], bob["card_last4"]) == ("re_bobs_card", "1111")


async def test_a_create_returning_this_buyers_active_enrollment_quotes_on_it(reap):
    """The holder became this buyer's ACTIVE card while we were creating (a concurrent step
    activated it). The purchase quotes on that card; our attempt is retired."""
    held = "66666666-6666-6666-6666-666666666666"

    async def _create(**kwargs):
        await database.execute(
            "INSERT INTO reap_agentic_enrollments (id, buyer_ref, status, reap_enrollment_id) "
            "VALUES ('re_winner', 'bref_alice', 'active', :r)",
            {"r": held},
        )
        return _ok(_enrollment_requires_action(held))

    reap.create_enrollment = _create
    purchase = await _start()
    result = await _step(purchase)
    assert result.state == "quoting"
    assert (await _get(purchase))["enrollment_id"] == "re_winner"
    [created] = reap.named("create_enrollment")
    assert (await ledger.get_enrollment_internal(created["attempt_id"]))["status"] == "dead"


async def test_a_create_returning_this_buyers_pending_enrollment_reuses_it(reap):
    """The holder is this buyer's PENDING row (created concurrently; possible only without
    migration 252's index). It is reconciled like any pending row — here its link is live, so
    the purchase waits on IT and our attempt is retired."""
    await _drop_one_pending_index()
    holder_link = "https://pay.prava.space/enroll/HOLDER"
    holder_id = "77777777-7777-7777-7777-777777777777"

    async def _create(**kwargs):
        # A concurrent mint's recorded answer, landing while our create is in flight.
        await database.execute(
            "INSERT INTO reap_agentic_enrollments (id, buyer_ref, status, reap_enrollment_id, "
            "hosted_url, hosted_url_expires_at) VALUES ('re_holder', 'bref_alice', 'pending', "
            ":r, :u, datetime('now', '+600 seconds'))",
            {"r": holder_id, "u": holder_link},
        )
        return _ok(_enrollment_requires_action(holder_id, url=holder_link))

    reap.create_enrollment = _create
    reap.get_enrollment = _reap_echoes_the_session()
    purchase = await _start()
    result = await _step(purchase)
    assert result.state == "needs_enrollment"
    row = await _get(purchase)
    assert (row["enrollment_id"], row["hosted_url"]) == ("re_holder", holder_link)
    [created] = reap.named("create_enrollment")
    assert (await ledger.get_enrollment_internal(created["attempt_id"]))["status"] == "dead"


# P2-1 — two purchases of one buyer in one tick.


async def _race_two_purchases(reap, monkeypatch):
    """Purchase 1 and purchase 2 of ONE buyer, stepped so that 2 runs its WHOLE step between
    1's read of the buyer's pending rows (none) and 1's INSERT — the interleaving two workers
    with two leases can produce in one poll tick."""
    await _purchase_a_waiting_on_a_link(reap)
    await _age_links(400)                   # A's link is dead past the grace: both will retire it
    reap.create_enrollment = _create_keyed_by_attempt()
    reap.calls.clear()
    first, second = await _start(), await _start()
    real = ledger.get_pending_enrollments
    calls = {"n": 0}

    async def _interleaved(buyer_ref):
        rows = await real(buyer_ref)
        calls["n"] += 1
        if calls["n"] == 2:                 # purchase 1, inside its mint, after A was retired
            await _step(second, "w2")
        return rows

    monkeypatch.setattr(ledger, "get_pending_enrollments", _interleaved)
    one = await _step(first, "w1")
    monkeypatch.setattr(ledger, "get_pending_enrollments", real)
    return first, second, one


async def test_r1_two_purchases_of_one_buyer_share_one_attempt(reap, monkeypatch):
    """THE REVIEWER'S REPRO, with migration 252's index: the second INSERT is refused, the
    ledger hands back the winner, and both purchases wait on ONE row, ONE enrollment, ONE link."""
    first, second, one = await _race_two_purchases(reap, monkeypatch)
    assert one.state == "needs_enrollment"
    assert (await _get(second))["state"] == "needs_enrollment"
    assert await _pending_count() == 1
    a, b = await _get(first), await _get(second)
    assert a["enrollment_id"] == b["enrollment_id"]
    assert a["hosted_url"] == b["hosted_url"]
    attempts = {c["attempt_id"] for c in reap.named("create_enrollment")}
    assert attempts == {a["enrollment_id"]}, "a replay of the winner's attempt, not a second one"


async def test_without_the_index_the_race_mints_two_and_the_next_purchase_reconciles_both(
    reap, monkeypatch
):
    """BEFORE MIGRATION 252 IS APPLIED the race still mints two pending rows (the index is the
    only thing that can prevent it) — but nothing is stranded: the next purchase reconciles
    EVERY pending row, and the one the buyer finished is activated."""
    await _drop_one_pending_index()
    first, second, _ = await _race_two_purchases(reap, monkeypatch)
    assert await _pending_count() == 2
    finished = (await _get(first))["enrollment_id"]
    finished_row = await ledger.get_enrollment_internal(finished)

    async def _reap_says(**kwargs):
        if kwargs["id"] == finished_row["reap_enrollment_id"]:
            return _ok(_enrollment_active(kwargs["id"]))
        return await _reap_echoes_the_session()(**kwargs)

    reap.get_enrollment = _reap_says
    third = await _start()
    assert (await _step(third)).state == "quoting"
    assert (await _get(third))["enrollment_id"] == finished
    assert (await ledger.get_active_enrollment("bref_alice"))["id"] == finished


async def _two_pending_rows_without_the_index():
    await _drop_one_pending_index()
    rows = []
    for n, (reap_id, age) in enumerate((
        ("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", 120),
        ("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", 60),
    )):
        await database.execute(
            "INSERT INTO reap_agentic_enrollments (id, buyer_ref, status, reap_enrollment_id, "
            "hosted_url, hosted_url_expires_at) VALUES (:i, 'bref_alice', 'pending', :r, :u, "
            "datetime('now', '+600 seconds'))",
            {"i": f"re_dup{n}", "r": reap_id, "u": f"https://pay.prava.space/enroll/dup{n}"},
        )
        await _set_created_at(f"re_dup{n}", age)
        rows.append((f"re_dup{n}", reap_id))
    return rows


async def test_with_two_live_pending_rows_the_oldest_is_reused_deterministically(reap):
    (older, _), (newer, _) = await _two_pending_rows_without_the_index()
    reap.get_enrollment = _reap_echoes_the_session()
    purchase = await _start()
    assert (await _step(purchase)).state == "needs_enrollment"
    assert (await _get(purchase))["enrollment_id"] == older
    assert reap.named("create_enrollment") == []
    assert [r["id"] for r in await ledger.get_pending_enrollments("bref_alice")] == [older, newer]


async def test_an_active_newer_pending_row_wins_over_a_live_older_one(reap):
    """ACTIVE anywhere wins: the buyer finished the NEWER link; reusing the older one would ask
    for the card again and leave the finished one pending here, ACTIVE at Reap."""
    (older, _), (newer, newer_reap) = await _two_pending_rows_without_the_index()

    async def _reap_says(**kwargs):
        if kwargs["id"] == newer_reap:
            return _ok(_enrollment_active(newer_reap))
        return await _reap_echoes_the_session()(**kwargs)

    reap.get_enrollment = _reap_says
    purchase = await _start()
    assert (await _step(purchase)).state == "quoting"
    assert (await _get(purchase))["enrollment_id"] == newer
    assert (await ledger.get_active_enrollment("bref_alice"))["id"] == newer


# P2-2 — a stored link with NO expiry.


async def test_r2_a_pending_link_with_no_expiry_is_not_reused_for_ever(reap):
    """THE REVIEWER'S REPRO. A link stored without an expiry, 7200 s old: dated from created_at
    + Reap's 15-minute session, it is long dead — retired, and a NEW attempt is minted."""
    _, old = await _purchase_a_waiting_on_a_link(reap)
    await database.execute(
        "UPDATE reap_agentic_enrollments SET hosted_url_expires_at = NULL WHERE id = :i",
        {"i": old},
    )
    await _set_created_at(old, 7200)
    reap.create_enrollment = _ok(
        _enrollment_requires_action("12121212-1212-1212-1212-121212121212",
                                    url="https://pay.prava.space/enroll/FRESH")
    )
    purchase = await _start()
    assert (await _step(purchase)).state == "needs_enrollment"
    assert (await _get(purchase))["hosted_url"] == "https://pay.prava.space/enroll/FRESH"
    assert (await ledger.get_enrollment_internal(old))["status"] == "dead"


async def test_a_young_pending_link_with_no_expiry_is_reused_with_an_estimated_expiry(reap):
    """The control: 100 s old, no expiry → still live (created + 15 min). It is reused, and the
    purchase carries the ESTIMATE so the sweep bounds it by the link's life."""
    _, old = await _purchase_a_waiting_on_a_link(reap)
    await database.execute(
        "UPDATE reap_agentic_enrollments SET hosted_url_expires_at = NULL WHERE id = :i",
        {"i": old},
    )
    await _set_created_at(old, 100)
    reap.calls.clear()
    purchase = await _start()
    assert (await _step(purchase)).state == "needs_enrollment"
    row = await _get(purchase)
    assert row["enrollment_id"] == old and reap.named("create_enrollment") == []
    created = (await ledger.get_pending_enrollments("bref_alice"))[0]["created_at"]
    assert row["hosted_url_expires_at"] == created + timedelta(seconds=ledger.HOSTED_SESSION_SECONDS)


# P2-3 — the hold re-check does not resolve again, and does not spend attempts.


async def test_a_settling_re_check_does_not_re_run_the_resolve(reap):
    await _purchase_a_waiting_on_a_link(reap)
    await _age_links(9)
    purchase = await _start()
    assert (await _step(purchase)).last_error_code == "enrollment_settling"
    reap.calls.clear()
    again = await _step(purchase)
    assert again.last_error_code == "enrollment_settling"
    assert reap.named("resolve_our_row") == [], "the hold re-check asks the enrollment question only"
    assert len(reap.named("get_enrollment")) == 1
    # And when the hold ends, the WHOLE step runs: resolve, then the decision, then quoting.
    reap.get_enrollment = _ok(_enrollment_active())
    reap.calls.clear()
    assert (await _step(purchase)).state == "quoting"
    assert reap.named("resolve_our_row"), "a hold that ends runs the resolve and its refusals"


async def test_a_settling_hold_does_not_spend_the_attempt_ceiling(reap):
    """Claims of a held row are EXEMPT from `attempts`; any other 'resolving' release counts."""
    await _purchase_a_waiting_on_a_link(reap)
    await _age_links(9)
    purchase = await _start()

    async def _claim_for_real():
        await database.execute(
            "UPDATE reap_agentic_purchases SET next_poll_at = datetime('now', '-1 seconds') "
            "WHERE id = :i", {"i": purchase},
        )
        claimed = [r for r in await ledger.claim_due_purchases("w1") if r["id"] == purchase]
        assert claimed, "the row must be claimable"
        return claimed[0]["attempts"]

    first = await _claim_for_real()                      # a fresh row: counts
    assert (await svc.advance(purchase, "w1")).last_error_code == "enrollment_settling"
    for _ in range(3):
        assert await _claim_for_real() == first, "a settling claim is not an attempt"
        assert (await svc.advance(purchase, "w1")).last_error_code == "enrollment_settling"
    reap.get_enrollment = _transport()
    await _claim_for_real()
    assert (await svc.advance(purchase, "w1")).last_error_code == "transport_error:readtimeout"
    assert await _claim_for_real() == first + 1, "any other release in 'resolving' still counts"


# Nits.


async def test_a_fresher_link_from_reaps_read_is_used_over_the_stored_one(reap):
    """Reap's GET carries a `nextAction`: it is vetted by the allowlist and used — link and
    expiry — instead of the stored one, which here has already died."""
    await _purchase_a_waiting_on_a_link(reap)
    await _age_links(9)
    fresh = "https://pay.prava.space/enroll/ses_REFRESHED"
    reap.get_enrollment = _ok(_enrollment_requires_action(url=fresh, expires_in=600))
    purchase = await _start()
    assert (await _step(purchase)).state == "needs_enrollment"
    row = await _get(purchase)
    assert row["hosted_url"] == fresh
    assert row["hosted_url_expires_at"] > datetime.now(timezone.utc) + timedelta(seconds=500)


async def test_a_fresher_link_we_would_not_vouch_for_is_not_used(reap):
    await _purchase_a_waiting_on_a_link(reap)
    await _age_links(9)
    reap.get_enrollment = _ok(
        _enrollment_requires_action(url="https://evilprava.space/enroll/x", expires_in=600)
    )
    purchase = await _start()
    result = await _step(purchase)
    assert result.last_error_code == "enrollment_settling", "stored link (dead, in grace) → hold"


async def test_a_repeated_settling_hold_logs_warning_once_then_info(reap, caplog):
    await _purchase_a_waiting_on_a_link(reap)
    await _age_links(9)
    purchase = await _start()
    caplog.set_level(logging.INFO, logger=svc.logger.name)
    for _ in range(3):
        await _step(purchase)
    held = [r for r in caplog.records if "enrollment_settling" in r.getMessage()]
    assert [r.levelno for r in held] == [logging.WARNING, logging.INFO, logging.INFO]


# ── round-2 review of #2483 at 8df71dd6e, P2-A: two creates of ONE attempt, concurrently ─────


def _in_progress() -> rc.ReapResponse:
    """Reap's 409 for an Idempotency-Key whose first call is still running — the shape the
    client builds from the error body (`error.code`, shape-checked)."""
    return rc.ReapResponse(
        ok=False, status=409, error="reap_status_409",
        error_code="IDEMPOTENCY_REQUEST_IN_PROGRESS",
    )


def _concurrent_create():
    """A `create_enrollment` whose FIRST call blocks until a SECOND call for the same attempt
    arrives, and answers that second one 409 IDEMPOTENCY_REQUEST_IN_PROGRESS — two workers
    creating the winner's attempt at the same moment, as Reap sees it."""
    import asyncio

    state = {"calls": [], "second_arrived": asyncio.Event()}

    async def _create(**kwargs):
        state["calls"].append(kwargs["attempt_id"])
        if len(state["calls"]) == 1:
            await asyncio.wait_for(state["second_arrived"].wait(), timeout=20)
            return _ok(_enrollment_requires_action())
        state["second_arrived"].set()
        return _in_progress()

    return _create, state


@pytest.mark.parametrize("second_answer", ["IDEMPOTENCY_REQUEST_IN_PROGRESS",
                                           "IDEMPOTENT_PARAMETER_MISMATCH"])
async def test_two_purchases_creating_one_attempt_concurrently_both_wait_on_one_link(
    reap, second_answer
):
    """P2-A. Both purchases of one first-time buyer step AT ONCE (asyncio.gather): one mints the
    buyer's pending row, the other is handed it (or loses the INSERT and is handed the winner),
    and both call `create_enrollment` with the SAME attempt while the first is still in flight.
    Reap's 409 for the second is a RELEASE, not a failure; on its next poll that purchase
    reuses the winner's stored session. One pending row, one enrollment at Reap, one link."""
    import asyncio

    create, state = _concurrent_create()
    if second_answer != "IDEMPOTENCY_REQUEST_IN_PROGRESS":
        async def _mismatch(**kwargs):
            answer = await create(**kwargs)
            if not answer.ok:
                return rc.ReapResponse(ok=False, status=422, error="reap_status_422",
                                       error_code=second_answer)
            return answer
        reap.create_enrollment = _mismatch
    else:
        reap.create_enrollment = create
    reap.get_enrollment = _reap_echoes_the_session()
    first, second = await _start(), await _start()
    await _claim(first, "w1")
    await _claim(second, "w2")

    results = await asyncio.gather(svc.advance(first, "w1"), svc.advance(second, "w2"))

    assert len(state["calls"]) == 2 and len(set(state["calls"])) == 1, "ONE attempt, created twice"
    outcomes = sorted((r.outcome, r.state, r.last_error_code) for r in results)
    loser_code = "idempotency_request_in_progress" if second_answer.startswith("IDEMPOTENCY") \
        else "idempotent_parameter_mismatch"
    assert outcomes == sorted([("advanced", "needs_enrollment", None),
                               ("released", "resolving", loser_code)])
    loser = next(pid for pid, r in zip((first, second), results) if r.outcome == "released")
    assert (await _get(loser))["state"] == "resolving", "released, not failed"

    reap.calls.clear()
    assert (await _step(loser, "w3")).state == "needs_enrollment"
    assert reap.named("create_enrollment") == [], "the next poll REUSES the winner's session"
    a, b = await _get(first), await _get(second)
    assert a["enrollment_id"] == b["enrollment_id"] == state["calls"][0]
    assert a["hosted_url"] == b["hosted_url"] == STAGING_LINK
    assert await _pending_count() == 1
    assert "failed" not in {a["state"], b["state"]}


# Checkout outcome reconciliation: local clocks never decide whether the buyer paid.
async def _recovery_checkout():
    await _active_enrollment()
    purchase_id = await _start()
    await _step(purchase_id)
    await _step(purchase_id)
    assert (await _get(purchase_id))["state"] == "awaiting_approval"
    from db.database import database, IS_POSTGRES
    old = "clock_timestamp() - INTERVAL '25 days'" if IS_POSTGRES else "datetime('now', '-25 days')"
    await database.execute(
        f"UPDATE reap_agentic_purchases SET hosted_url_expires_at = {old}, "
        f"state_entered_at = {old}, next_poll_at = {old}, attempts = 1005 WHERE id = :id",
        {"id": purchase_id},
    )
    import db.reap_agentic_ledger as ledger
    await ledger.release_claim(purchase_id, "w1")
    return purchase_id


@pytest.mark.parametrize("status,expected", [
    ("PROCESSING", "processing"), ("COMPLETED", "completed"),
    ("FAILED", "failed"), ("EXPIRED", "expired"),
])
async def test_recovery_late_authoritative_outcome_survives_all_local_sweeps(reap, attribution, status, expected):
    import db.reap_agentic_ledger as ledger
    import services.reap_agentic_purchase as svc
    purchase_id = await _recovery_checkout()
    before = await _get(purchase_id)
    assert await ledger.expire_overdue_purchases() == []
    assert await ledger.fail_exhausted_purchases(50, include_processing=True) == []
    assert await ledger.scrub_reconciling_purchase_pii() == [purchase_id]
    scrubbed = await _get(purchase_id)
    for key in ("reap_checkout_id", "reap_quote_id", "quoted_total_minor", "currency", "click_id", "consent_version", "consented_at"):
        assert scrubbed[key] == before[key]
    assert scrubbed["shipping_address"] is None and scrubbed["buyer_email"] is None
    assert await ledger.scrub_reconciling_purchase_pii() == []
    reap.get_checkout = _ok(dict(CHECKOUT_COMPLETED, status=status))
    result = await _step(purchase_id)
    assert result.state == expected
    assert (await _get(purchase_id))["state"] == expected
    if expected == "completed":
        assert len(attribution.calls) == 1
        assert (await svc.advance(purchase_id, "other-worker")).outcome == "terminal"
        assert len(attribution.calls) == 1


async def test_recovery_prolonged_outage_and_disarm_do_not_strand_a_checkout(reap, attribution, monkeypatch):
    from db.database import database
    import db.reap_agentic_ledger as ledger
    import jobs.reap_agentic_purchase_poll as job
    purchase_id = await _recovery_checkout()
    # A new pre-checkout purchase must remain untouched while creates are disarmed.
    untouched = await _start(agent_user_ref_hash="another-user", buyer_ref="another-buyer")
    monkeypatch.setenv("REAP_AGENTIC_ENABLED", "0")
    reap.get_checkout = _transport()
    reap.calls.clear()
    for n in range(3):
        await database.execute("UPDATE reap_agentic_purchases SET next_poll_at = CURRENT_TIMESTAMP WHERE id = :id", {"id": purchase_id})
        report = await job.run_reap_agentic_purchase_poll(worker_id=f"recovery-{n}")
        assert report.claimed == 1 and report.expired == 0
        assert (await _get(purchase_id))["state"] == "awaiting_approval"
    row = await _get(purchase_id)
    assert row["reap_checkout_id"] and row["buyer_email"] is None and row["shipping_address"] is None
    assert (await _get(untouched))["state"] == "resolving"
    assert reap.sequence() == ["get_checkout"] * 3
    reap.get_checkout = _ok(CHECKOUT_COMPLETED)
    await database.execute("UPDATE reap_agentic_purchases SET next_poll_at = CURRENT_TIMESTAMP WHERE id = :id", {"id": purchase_id})
    await job.run_reap_agentic_purchase_poll(worker_id="recovered")
    assert (await _get(purchase_id))["state"] == "completed"
    assert len(attribution.calls) == 1


async def test_recovery_sweep_during_provider_read_preserves_claim_and_one_conversion(reap, attribution, monkeypatch):
    import asyncio
    import db.reap_agentic_ledger as ledger
    import services.reap_agentic_purchase as svc
    purchase_id = await _recovery_checkout()
    entered, resume = asyncio.Event(), asyncio.Event()
    async def read(**kwargs):
        entered.set()
        await resume.wait()
        return _ok(CHECKOUT_COMPLETED)
    reap.get_checkout = read
    await _claim(purchase_id, "reader")
    task = asyncio.create_task(svc.advance(purchase_id, "reader"))
    await asyncio.wait_for(entered.wait(), 5)
    try:
        assert await ledger.expire_overdue_purchases() == []
        assert await ledger.fail_exhausted_purchases(50) == []
        assert await ledger.scrub_reconciling_purchase_pii() == []
        assert (await _get(purchase_id))["claimed_by"] == "reader"
    finally:
        resume.set()
    assert (await task).state == "completed"
    assert len(attribution.calls) == 1


async def test_recovery_partial_claim_cancellation_releases_lease(reap, monkeypatch):
    import asyncio
    import db.reap_agentic_ledger as ledger
    import jobs.reap_agentic_purchase_poll as job
    purchase_id = await _recovery_checkout()
    real = ledger.claim_due_purchases
    async def partial(*args, **kwargs):
        rows = await real(*args, **kwargs)
        assert rows
        raise asyncio.CancelledError()
    monkeypatch.setattr(ledger, "claim_due_purchases", partial)
    with pytest.raises(asyncio.CancelledError):
        await job.run_reap_agentic_purchase_poll(worker_id="cancelled-claim")
    assert (await _get(purchase_id))["claimed_by"] is None

# Independent stop, privacy and partial-acquisition regressions (#2490 follow-up).
async def test_reconciliation_stop_blocks_provider_but_keeps_privacy(reap, monkeypatch):
    import jobs.reap_agentic_purchase_poll as job
    purchase_id = await _recovery_checkout()
    monkeypatch.setenv('REAP_AGENTIC_RECONCILE_ENABLED', '0')
    reap.calls.clear()
    report = await job.run_reap_agentic_purchase_poll(worker_id='stop-all-provider')
    assert report.claimed == 0 and reap.calls == []
    row = await _get(purchase_id)
    assert row['state'] == 'awaiting_approval' and row['buyer_email'] is None

async def test_contact_scrub_uses_expired_link_before_max_age(reap):
    from db.database import database, IS_POSTGRES
    import db.reap_agentic_ledger as ledger
    await _active_enrollment()
    pid = await _start()
    await _step(pid)
    await _step(pid)
    old = "clock_timestamp() - INTERVAL '20 minutes'" if IS_POSTGRES else "datetime('now','-20 minutes')"
    await database.execute(f'UPDATE reap_agentic_purchases SET hosted_url_expires_at={old} WHERE id=:id', {'id':pid})
    assert await ledger.scrub_reconciling_purchase_pii(max_age_seconds=3600) == []
    await ledger.release_claim(pid, 'w1')
    assert await ledger.scrub_reconciling_purchase_pii(max_age_seconds=3600) == [pid]
    assert (await _get(pid))['state'] == 'awaiting_approval'

async def test_paused_precheckout_rows_do_not_feed_stuck_count(reap, monkeypatch):
    from db.database import database, IS_POSTGRES
    import jobs.reap_agentic_purchase_poll as job
    for state in ('resolving','quoting'):
        pid = await _start(buyer_ref='paused_'+state)
        old = "clock_timestamp() - INTERVAL '3 days'" if IS_POSTGRES else "datetime('now','-3 days')"
        await database.execute(f'UPDATE reap_agentic_purchases SET state=:state,state_entered_at={old} WHERE id=:id', {'state':state,'id':pid})
    monkeypatch.setenv('REAP_AGENTIC_ENABLED', '0')
    reap.calls.clear()
    report = await job.run_reap_agentic_purchase_poll(worker_id='paused-precheckout')
    assert report.claimed == 0 and report.stuck_over_age == 0 and reap.calls == []

async def test_missing_credentials_still_counts_exposed_stuck(reap, monkeypatch):
    import jobs.reap_agentic_purchase_poll as job
    await _recovery_checkout()
    monkeypatch.delenv('REAP_API_KEY', raising=False)
    reap.calls.clear()
    report = await job.run_reap_agentic_purchase_poll(worker_id='no-credentials')
    assert report.claimed == 0 and report.stuck_over_age == 1 and reap.calls == []

async def test_partial_acquisition_cancellation_releases_first_lease(reap, monkeypatch):
    import asyncio
    from db.database import database, IS_POSTGRES
    import db.reap_agentic_ledger as ledger
    import jobs.reap_agentic_purchase_poll as job
    for n in range(2): await _start(buyer_ref='partial_'+str(n))
    real = database.fetch_one
    seen = 0
    sql = ledger._CLAIM_PURCHASE_SQL if IS_POSTGRES else ledger._CLAIM_PURCHASE_SQL_SQLITE
    async def cancel_second(query, *args, **kwargs):
        nonlocal seen
        if query == sql:
            seen += 1
            if seen == 2: raise asyncio.CancelledError()
        return await real(query, *args, **kwargs)
    monkeypatch.setattr(database, 'fetch_one', cancel_second)
    with pytest.raises(asyncio.CancelledError):
        await job.run_reap_agentic_purchase_poll(worker_id='partial-real-loop')
    assert seen == 2
    rows = await database.fetch_all('SELECT claimed_by FROM reap_agentic_purchases')
    assert all(row['claimed_by'] is None for row in rows)

async def test_creation_privacy_cap_does_not_reset_and_resume_calls_no_provider(reap, monkeypatch):
    from db.database import database, IS_POSTGRES
    import db.reap_agentic_ledger as ledger
    import services.reap_agentic_purchase as svc
    old = "clock_timestamp() - INTERVAL '2 hours'" if IS_POSTGRES else "datetime('now','-2 hours')"
    pid = await _start()
    await database.execute(f"UPDATE reap_agentic_purchases SET state='quoting',created_at={old},attempts=55 WHERE id=:id", {'id':pid})
    assert await ledger.scrub_reconciling_purchase_pii(max_age_seconds=900) == [pid]
    row = await _get(pid)
    assert row['state'] == 'quoting' and row['last_error_code'] == 'contact_retention_elapsed'
    assert await ledger.fail_exhausted_purchases(50) == []
    reap.calls.clear()
    await _claim(pid, 'resume-no-contact')
    result = await svc.advance(pid, 'resume-no-contact')
    assert result.last_error_code == 'contact_retention_elapsed' and result.state == 'quoting'
    assert (await _get(pid))['attempts'] == 55 and reap.calls == []

@pytest.mark.parametrize('reason', ['reap_status_404','hosted_url_not_allowed','unknown_checkout_status'])
async def test_permanent_checkout_read_enters_human_review_without_terminalizing(reap, monkeypatch, reason):
    from db.database import database
    import db.reap_agentic_ledger as ledger
    import jobs.reap_agentic_purchase_poll as job
    import services.reap_agentic_client as rc
    pid = await _recovery_checkout()
    if reason == 'unknown_checkout_status':
        reap.get_checkout = _ok({'status':'UNRECOGNIZED'})
    else:
        reap.get_checkout = rc.ReapResponse(ok=False,status=404,error=reason)
    for n in range(5):
        await database.execute('UPDATE reap_agentic_purchases SET next_poll_at=CURRENT_TIMESTAMP WHERE id=:id', {'id':pid})
        report = await job.run_reap_agentic_purchase_poll(worker_id='permanent-'+str(n))
    row = await _get(pid)
    assert row['state'] == 'awaiting_approval'
    assert row['last_error_code'].startswith('checkout_unresolvable:')
    assert report.checkout_needs_human == 1 and report.stuck_over_age == 0
    assert await ledger.fail_exhausted_purchases(1,include_processing=True) == []
    from datetime import datetime,timezone
    assert (row['next_poll_at']-datetime.now(timezone.utc)).total_seconds() >= 899
    # A transient failure cannot silently remove the human-review classification.
    reap.get_checkout = _transport()
    await database.execute('UPDATE reap_agentic_purchases SET next_poll_at=CURRENT_TIMESTAMP WHERE id=:id', {'id':pid})
    await job.run_reap_agentic_purchase_poll(worker_id='human-outage')
    assert (await _get(pid))['last_error_code'].startswith('checkout_unresolvable:')
    # A valid provider waiting/processing outcome restores normal reconciliation.
    reap.get_checkout = _ok({'status':'PROCESSING'})
    await database.execute('UPDATE reap_agentic_purchases SET next_poll_at=CURRENT_TIMESTAMP WHERE id=:id', {'id':pid})
    report = await job.run_reap_agentic_purchase_poll(worker_id='provider-recovers')
    assert (await _get(pid))['state'] == 'processing' and report.checkout_needs_human == 0

async def test_mid_claim_stop_preserves_contact_pause_and_attempt_exemption(reap, monkeypatch):
    from db.database import database
    import db.reap_agentic_ledger as ledger
    import services.reap_agentic_purchase as svc
    pid = await _start()
    await database.execute("UPDATE reap_agentic_purchases SET state='quoting',buyer_email=NULL,shipping_address=NULL,last_error_code='contact_retention_elapsed' WHERE id=:id", {'id':pid})
    await _claim(pid, 'pause-flips')
    monkeypatch.setenv('REAP_AGENTIC_RECONCILE_ENABLED','0')
    reap.calls.clear()
    result = await svc.advance(pid,'pause-flips')
    assert result.last_error_code == 'contact_retention_elapsed'
    assert (await _get(pid))['last_error_code'] == 'contact_retention_elapsed'
    monkeypatch.setenv('REAP_AGENTIC_RECONCILE_ENABLED','1')
    await database.execute('UPDATE reap_agentic_purchases SET next_poll_at=CURRENT_TIMESTAMP WHERE id=:id', {'id':pid})
    await _claim(pid,'resume-pause')
    await svc.advance(pid,'resume-pause')
    assert (await _get(pid))['state']=='quoting' and (await _get(pid))['attempts']==0
    assert await ledger.count_contact_retention_blocked()==1 and reap.calls==[]

async def test_hung_contact_diagnostic_does_not_hide_other_health(reap, monkeypatch):
    import asyncio
    import db.reap_agentic_ledger as ledger
    import jobs.reap_agentic_purchase_poll as job
    await _recovery_checkout()
    monkeypatch.setenv('REAP_AGENTIC_RECONCILE_ENABLED','0')
    monkeypatch.setattr(job,'STUCK_COUNT_TIMEOUT_SECONDS',0.02)
    async def hang():await asyncio.Event().wait()
    monkeypatch.setattr(ledger,'count_contact_retention_blocked',hang)
    report=await asyncio.wait_for(job.run_reap_agentic_purchase_poll(worker_id='health-independence'),0.5)
    assert report.contact_retention_blocked==-1 and report.stuck_over_age==1
    assert report.errors==1 and report.checkout_needs_human==0

async def test_stop_after_quote_prevents_next_checkout(reap, monkeypatch):
    await _active_enrollment()
    pid = await _start()
    await _step(pid)
    def stop_after_quote(**kwargs):
        monkeypatch.setenv('REAP_AGENTIC_RECONCILE_ENABLED', '0')
        return _ok(QUOTE_200)
    reap.request_quote = stop_after_quote
    reap.calls.clear()
    await _step(pid)
    assert reap.sequence() == ['resolve_our_row', 'request_quote']
    assert (await _get(pid))['state'] == 'quoting'

async def test_contact_cap_needs_enrollment_cannot_resume_quote(reap):
    from db.database import database, IS_POSTGRES
    import db.reap_agentic_ledger as ledger
    pid = await _start()
    await _step(pid)
    await ledger.release_claim(pid, 'w1')
    assert (await _get(pid))['state'] == 'needs_enrollment'
    old = "clock_timestamp() - INTERVAL '2 hours'" if IS_POSTGRES else "datetime('now','-2 hours')"
    await database.execute(f"UPDATE reap_agentic_purchases SET created_at={old} WHERE id=:id", {'id':pid})
    assert await ledger.scrub_reconciling_purchase_pii(max_age_seconds=900) == [pid]
    reap.calls.clear()
    await _step(pid)
    await _step(pid)
    assert reap.calls == []
    assert (await _get(pid))['state'] == 'needs_enrollment'
    assert await ledger.count_contact_retention_blocked() == 1

async def test_worker_stop_guards_nested_real_client_transport(reap, monkeypatch):
    import services.reap_agentic_client as rc
    monkeypatch.setenv('REAP_AGENTIC_RECONCILE_ENABLED','0')
    from services.reap_agentic_purchase import is_reconciliation_enabled
    token = rc.worker_provider_permission.set(is_reconciliation_enabled)
    try:
        for operation in (rc._post('/agentic/products/search', {'query':'fixture'}), rc._get('/agentic/checkouts/fixture')):
            with pytest.raises(rc.ProviderOperationStopped):
                await operation
    finally:
        rc.worker_provider_permission.reset(token)
    assert rc.worker_provider_permission.get() is None

async def _manual_case(status='COMPLETED', state='awaiting_approval'):
    from datetime import datetime,timezone
    from db.database import database
    pid=await _recovery_checkout()
    await database.execute("UPDATE reap_agentic_purchases SET state=:state,last_error_code='checkout_unresolvable:3:reap_status_404' WHERE id=:id",{'id':pid,'state':state})
    row=await _get(pid)
    evidence={'source':'authenticated_reap_checkout_read','authoritative_verified':True,'reference':'support_case_synthetic_1','observed_at':datetime.now(timezone.utc),'provider_base_url':'https://sandbox.api.reap.global','payload':dict(CHECKOUT_COMPLETED,status=status,id=row['reap_checkout_id'])}
    return pid,row,evidence

async def test_manual_preview_and_exact_replay_are_read_only(reap,manual_attribution):
    attribution = manual_attribution
    from services.reap_checkout_recovery import resolve_checkout_manually
    from db.database import database
    pid,row,evidence=await _manual_case()
    reap.calls.clear();attribution.calls.clear()
    args=dict(evidence=evidence,operator_ref='operator_test',expected_updated_at=row['updated_at'])
    preview=await resolve_checkout_manually(pid,**args)
    assert preview['status']=='eligible' and await _get(pid)==row
    assert await database.fetch_val('SELECT count(*) FROM reap_checkout_manual_resolution_audit')==0
    assert reap.calls==[] and attribution.calls==[]
    result=await resolve_checkout_manually(pid,**args,dry_run=False)
    assert result['state']=='completed' and len(attribution.calls)==1
    terminal=await _get(pid)
    assert terminal['buyer_email'] is None and terminal['shipping_address'] is None and terminal['claimed_by'] is None
    assert terminal['consent_version']==row['consent_version'] and terminal['consented_at']==row['consented_at']
    assert await database.fetch_val('SELECT count(*) FROM reap_checkout_manual_resolution_audit')==1
    replay=await resolve_checkout_manually(pid,**args,dry_run=False)
    assert replay['status']=='already_resolved' and await _get(pid)==terminal
    assert len(attribution.calls)==1 and reap.calls==[]

@pytest.mark.parametrize('defect',['unverified','checkout','status','amount','currency','order','old','future','claimed','stale','origin_absent','origin_other'])
async def test_manual_refuses_unverified_or_unfenced_outcomes(reap,attribution,defect):
    from datetime import timedelta
    from db.database import database
    from services.reap_checkout_recovery import resolve_checkout_manually,ManualResolutionRefused
    pid,row,evidence=await _manual_case()
    expected=row['updated_at']
    if defect=='unverified':evidence['authoritative_verified']=False
    elif defect=='checkout':evidence['payload']['id']='another_checkout'
    elif defect=='status':evidence['payload']['status']='PROCESSING'
    elif defect=='amount':evidence['payload']['finalAmount']={'amount':100,'currency':'USD'}
    elif defect=='currency':evidence['payload']['finalAmount']={'amount':45,'currency':'SGD'}
    elif defect=='order':evidence['payload']['orderId']=True
    elif defect=='old':evidence['observed_at']-=timedelta(days=2)
    elif defect=='future':evidence['observed_at']+=timedelta(minutes=1)
    elif defect=='claimed':await _claim(pid,'actual_worker')
    elif defect=='stale':expected-=timedelta(seconds=1)
    elif defect=='origin_absent':evidence.pop('provider_base_url')
    elif defect=='origin_other':evidence['provider_base_url']='https://api.reap.global'
    before=await _get(pid);reap.calls.clear();attribution.calls.clear()
    with pytest.raises(ManualResolutionRefused):
        await resolve_checkout_manually(pid,evidence=evidence,operator_ref='operator_test',expected_updated_at=expected,dry_run=False)
    assert await _get(pid)==before
    assert await database.fetch_val('SELECT count(*) FROM reap_checkout_manual_resolution_audit')==0
    assert reap.calls==[] and attribution.calls==[]

@pytest.mark.parametrize('failure',['audit','terminal','edge'])
async def test_manual_failure_rolls_back_audit_outcome_and_claim(reap,manual_attribution,monkeypatch,failure):
    attribution = manual_attribution
    from db.database import database
    from services.reap_checkout_recovery import resolve_checkout_manually
    import services.reap_agentic_purchase as svc
    pid,row,evidence=await _manual_case();reap.calls.clear()
    if failure=='audit':
        original=database.execute
        async def broken(query,*args,**kwargs):
            if 'INSERT INTO reap_checkout_manual_resolution_audit' in str(query):raise RuntimeError('synthetic_audit_failure')
            return await original(query,*args,**kwargs)
        monkeypatch.setattr(database,'execute',broken)
    elif failure=='terminal':
        original=svc._move
        async def broken(*args,**kwargs):
            result=await original(*args,**kwargs)
            raise RuntimeError('synthetic_after_terminal_failure')
        monkeypatch.setattr(svc,'_move',broken)
    else:attribution.raises=RuntimeError('synthetic_edge_failure')
    with pytest.raises(RuntimeError):
        await resolve_checkout_manually(pid,evidence=evidence,operator_ref='operator_test',expected_updated_at=row['updated_at'],dry_run=False)
    assert await _get(pid)==row
    assert await database.fetch_val('SELECT count(*) FROM reap_checkout_manual_resolution_audit')==0
    assert reap.calls==[]

@pytest.mark.parametrize('status,state,target',[('FAILED','awaiting_approval','failed'),('EXPIRED','awaiting_approval','expired'),('EXPIRED','processing','failed')])
async def test_manual_authoritative_unpaid_terminal_evidence(reap,status,state,target):
    from db.database import database
    from services.reap_checkout_recovery import resolve_checkout_manually
    pid,row,evidence=await _manual_case(status,state);reap.calls.clear()
    result=await resolve_checkout_manually(pid,evidence=evidence,operator_ref='operator_test',expected_updated_at=row['updated_at'],dry_run=False)
    assert result['state']==target and (await _get(pid))['claimed_by'] is None and reap.calls==[]
    assert await database.fetch_val('SELECT count(*) FROM reap_checkout_manual_resolution_audit')==1

async def test_manual_compare_and_swap_loses_without_audit_or_outcome(reap,monkeypatch):
    from db.database import database
    from services.reap_checkout_recovery import resolve_checkout_manually,ManualResolutionRefused
    pid,row,evidence=await _manual_case()
    original=database.fetch_one
    async def moved(query,*args,**kwargs):
        if 'UPDATE reap_agentic_purchases SET claimed_by=:holder' in str(query):return None
        return await original(query,*args,**kwargs)
    monkeypatch.setattr(database,'fetch_one',moved)
    with pytest.raises(ManualResolutionRefused,match='compare_and_swap_lost'):
        await resolve_checkout_manually(pid,evidence=evidence,operator_ref='operator_test',expected_updated_at=row['updated_at'],dry_run=False)
    assert await _get(pid)==row
    assert await database.fetch_val('SELECT count(*) FROM reap_checkout_manual_resolution_audit')==0

async def test_manual_sql_edge_failure_rolls_back_both_business_and_audit(reap,monkeypatch):
    from db.database import database
    import services.commerce_attribution_service as cas
    from services.reap_checkout_recovery import resolve_checkout_manually
    await database.execute('DROP TABLE IF EXISTS synthetic_manual_edge')
    await database.execute('CREATE TABLE synthetic_manual_edge (id INTEGER PRIMARY KEY)')
    pid,row,evidence=await _manual_case()
    async def real_database_writer(**kwargs):
        await database.execute('INSERT INTO synthetic_manual_edge(id) VALUES (1)')
        # A actual database error after an edge write must not commit either on SQLite or PG.
        await database.execute('INSERT INTO synthetic_manual_edge(id) VALUES (1)')
    monkeypatch.setattr(cas,'close_external_order_conversion',real_database_writer)
    with pytest.raises(Exception):
        await resolve_checkout_manually(pid,evidence=evidence,operator_ref='operator_test',expected_updated_at=row['updated_at'],dry_run=False)
    assert await _get(pid)==row
    assert await database.fetch_val('SELECT count(*) FROM synthetic_manual_edge')==0
    assert await database.fetch_val('SELECT count(*) FROM reap_checkout_manual_resolution_audit')==0
    await database.execute('DROP TABLE synthetic_manual_edge')

async def test_manual_migration_and_selfheal_have_identical_audit_contract(reap):
    from pathlib import Path
    from db.database import database,IS_POSTGRES
    from db.schema_guard import ensure_required_schema_light
    from db.sql_migrations import split_statements
    async def columns():
        if IS_POSTGRES:
            rows=await database.fetch_all("SELECT column_name,data_type,is_nullable,column_default FROM information_schema.columns WHERE table_schema='public' AND table_name='reap_checkout_manual_resolution_audit' ORDER BY ordinal_position")
            checks=await database.fetch_all("SELECT pg_get_constraintdef(oid) AS definition FROM pg_constraint WHERE conrelid='reap_checkout_manual_resolution_audit'::regclass ORDER BY pg_get_constraintdef(oid)")
        else:
            rows=await database.fetch_all('PRAGMA table_info(reap_checkout_manual_resolution_audit)')
            checks=await database.fetch_all("SELECT sql FROM sqlite_master WHERE name='reap_checkout_manual_resolution_audit'")
        return [dict(r) for r in rows],[dict(r) for r in checks]
    await database.execute('DROP TABLE reap_checkout_manual_resolution_audit')
    migration=(Path(__file__).parent.parent/'db/migrations/253_reap_checkout_manual_resolution_audit.sql').read_text()
    if not IS_POSTGRES:migration=migration.replace('TIMESTAMPTZ','TIMESTAMP')
    for statement in split_statements(migration):await database.execute(statement)
    migrated=await columns()
    await database.execute('DROP TABLE reap_checkout_manual_resolution_audit')
    await ensure_required_schema_light()
    healed=await columns()
    assert migrated==healed

@pytest.mark.parametrize('failure',['suppressed_edge','native_edge_missing'])
async def test_manual_false_attribution_or_release_fence_rolls_back(reap,monkeypatch,failure):
    import services.reap_agentic_purchase as svc
    import db.reap_agentic_ledger as ledger
    from db.database import database
    from services.reap_checkout_recovery import resolve_checkout_manually
    pid,row,evidence=await _manual_case()
    async def refused(*args,**kwargs):return False if failure=='suppressed_edge' else None
    if failure=='suppressed_edge':
        monkeypatch.setattr(svc,'_close_attribution',refused)
    else:
        import services.commerce_attribution_service as cas
        monkeypatch.setattr(cas,'close_external_order_conversion',refused)
    with pytest.raises((RuntimeError,ValueError)):
        await resolve_checkout_manually(pid,evidence=evidence,operator_ref='operator_test',expected_updated_at=row['updated_at'],dry_run=False)
    assert await _get(pid)==row
    assert await database.fetch_val('SELECT count(*) FROM reap_checkout_manual_resolution_audit')==0

async def test_manual_other_channel_claim_is_audited_completion_without_reap_edge(reap,attribution):
    from db.database import database
    from services import conversion_click_claims as ccc
    from services.reap_checkout_recovery import resolve_checkout_manually
    pid,row,evidence=await _manual_case()
    click='synthetic_manual_other_channel'
    cart=f"https://brand.example/cart/49922977038613:1?attributes[pivota_click_id]={click}&country=US"
    await database.execute("UPDATE reap_agentic_purchases SET item_source='cart_link',cart_url=:cart,click_id=:click WHERE id=:id",{'id':pid,'cart':cart,'click':click})
    await database.execute('DELETE FROM conversion_click_claims WHERE click_id=:click',{'click':click})
    assert await ccc.claim_click(click,claimed_by=ccc.MERCHANT_CLAIMANT,external_order_id='merchant_synthetic_order')
    before=dict(await database.fetch_one('SELECT * FROM conversion_click_claims WHERE click_id=:click',{'click':click}))
    row=await _get(pid);reap.calls.clear();attribution.calls.clear()
    result=await resolve_checkout_manually(pid,evidence=evidence,operator_ref='operator_test',expected_updated_at=row['updated_at'],dry_run=False)
    assert result['state']=='completed' and (await _get(pid))['last_error_code']==ccc.CLOSED_BY_OTHER_CHANNEL
    audit=await database.fetch_one('SELECT * FROM reap_checkout_manual_resolution_audit WHERE purchase_id=:id',{'id':pid})
    assert audit['attribution_outcome']=='closed_by_other_channel'
    assert dict(await database.fetch_one('SELECT * FROM conversion_click_claims WHERE click_id=:click',{'click':click}))==before
    assert reap.calls==[] and attribution.calls==[]


# Manual completion validates real persisted attribution, including replay collisions.
import services.commerce_attribution_service as _manual_cas
_MANUAL_REAL_EXTERNAL_CLOSE = _manual_cas.close_external_order_conversion

@pytest.fixture
async def manual_attribution(monkeypatch):
    from db.database import IS_POSTGRES
    from sqlalchemy.schema import CreateTable
    from sqlalchemy.dialects import sqlite, postgresql
    dialect = postgresql.dialect() if IS_POSTGRES else sqlite.dialect()
    for table in (_manual_cas.commerce_attribution_edges, _manual_cas.surface_click_events):
        await database.execute('DROP TABLE IF EXISTS ' + table.name)
        await database.execute(str(CreateTable(table).compile(dialect=dialect)))
    await database.execute('CREATE UNIQUE INDEX manual_ext_order ON commerce_attribution_edges(merchant_id,external_order_id)')
    if not IS_POSTGRES:
        original_fetch = database.fetch_one
        async def sqlite_fetch(query, values=None):
            if isinstance(query, str) and 'INSERT INTO commerce_attribution_edges' in query:
                query = query.replace("'[]'::jsonb", "'[]'").replace('CAST(:metadata AS JSONB)', ':metadata')
            return await original_fetch(query, values)
        monkeypatch.setattr(database, 'fetch_one', sqlite_fetch)
    recorder = Attribution()
    async def real_close(**kwargs):
        recorder.calls.append(kwargs)
        if recorder.raises is not None:
            raise recorder.raises
        return await _MANUAL_REAL_EXTERNAL_CLOSE(**kwargs)
    monkeypatch.setattr(_manual_cas, 'close_external_order_conversion', real_close)
    return recorder

async def _persist_manual_expected_edge(row, evidence):
    # Use the actual attribution sink, not a fabricated success receipt.
    completed = dict(row, reap_order_id=evidence['payload']['orderId'], final_total_minor=4500)
    assert await svc._close_attribution(completed)
    return dict(await database.fetch_one('SELECT * FROM commerce_attribution_edges WHERE external_order_id=:order', {'order': completed['reap_order_id']}))

@pytest.mark.parametrize('defect', ['amount', 'currency', 'click', 'agent', 'order', 'state', 'source', 'purchase_provenance', 'checkout_provenance', 'partner_reported', 'metadata_missing'])
async def test_manual_durable_edge_collision_cannot_be_audited_closed(reap, manual_attribution, defect):
    from services.reap_checkout_recovery import resolve_checkout_manually
    pid, row, evidence = await _manual_case()
    edge = await _persist_manual_expected_edge(row, evidence)
    fields = {'amount': ('gross_attributed_gmv_cents',1), 'currency':('currency','EUR'), 'click':('click_id','foreign_click'), 'agent':('agent_id','foreign_agent'), 'order':('order_id','foreign_synthetic_order'), 'state':('state','refunded'), 'source':('source','other_channel')}
    if defect in fields:
        name, value = fields[defect]
        await database.execute('UPDATE commerce_attribution_edges SET '+name+'=:value WHERE edge_id=:edge', {'value':value, 'edge':edge['edge_id']})
    else:
        metadata = edge['metadata']
        if isinstance(metadata,str): metadata=json.loads(metadata)
        if defect=='metadata_missing':metadata={}
        elif defect=='purchase_provenance':metadata['partner_provenance']['purchase_id']='other_purchase'
        elif defect=='checkout_provenance':metadata['partner_provenance']['reap_checkout_id']='other_checkout'
        else:metadata['partner_provenance']['partner_reported']=False
        await database.execute(_manual_cas.commerce_attribution_edges.update().where(_manual_cas.commerce_attribution_edges.c.edge_id==edge['edge_id']).values(metadata=metadata))
    before=dict(await database.fetch_one('SELECT * FROM commerce_attribution_edges WHERE edge_id=:edge',{'edge':edge['edge_id']}))
    reap.calls.clear()
    with pytest.raises(RuntimeError, match='manual_attribution_persisted_'):
        await resolve_checkout_manually(pid,evidence=evidence,operator_ref='operator_test',expected_updated_at=row['updated_at'],dry_run=False)
    assert await _get(pid)==row
    assert await database.fetch_val('SELECT count(*) FROM reap_checkout_manual_resolution_audit')==0
    assert dict(await database.fetch_one('SELECT * FROM commerce_attribution_edges WHERE edge_id=:edge',{'edge':edge['edge_id']}))==before
    assert reap.calls==[]

@pytest.mark.parametrize('defect', ['absent_edge','absent_receipt'])
async def test_manual_durable_edge_requires_both_receipt_and_persisted_evidence(reap,manual_attribution,monkeypatch,defect):
    from services.reap_checkout_recovery import resolve_checkout_manually
    pid,row,evidence=await _manual_case()
    original=_manual_cas.close_external_order_conversion
    async def missing(**kwargs):
        if defect=='absent_receipt':
            await original(**kwargs)
            return None
        return {'edge_id':_manual_cas._ext_edge_keys(kwargs['merchant_id'],kwargs['external_order_id'])[0]}
    monkeypatch.setattr(_manual_cas,'close_external_order_conversion',missing)
    with pytest.raises(RuntimeError,match='manual_attribution_'):
        await resolve_checkout_manually(pid,evidence=evidence,operator_ref='operator_test',expected_updated_at=row['updated_at'],dry_run=False)
    assert await _get(pid)==row
    assert await database.fetch_val('SELECT count(*) FROM reap_checkout_manual_resolution_audit')==0
    assert await database.fetch_val('SELECT count(*) FROM commerce_attribution_edges')==0

async def test_manual_durable_matching_replay_closes_exactly_one_edge(reap,manual_attribution):
    from services.reap_checkout_recovery import resolve_checkout_manually
    pid,row,evidence=await _manual_case()
    edge=await _persist_manual_expected_edge(row,evidence)
    result=await resolve_checkout_manually(pid,evidence=evidence,operator_ref='operator_test',expected_updated_at=row['updated_at'],dry_run=False)
    assert result['state']=='completed'
    assert await database.fetch_val('SELECT count(*) FROM commerce_attribution_edges')==1
    assert await database.fetch_val('SELECT attribution_outcome FROM reap_checkout_manual_resolution_audit')=='edge_closed'
    assert dict(await database.fetch_one('SELECT * FROM commerce_attribution_edges WHERE edge_id=:edge',{'edge':edge['edge_id']}))==edge

async def test_manual_foreign_reap_click_claim_is_not_merchant_channel(reap,manual_attribution):
    from services.reap_checkout_recovery import resolve_checkout_manually
    pid,row,evidence=await _manual_case()
    click='synthetic_foreign_reap_claim'
    cart=f'https://brand.example/cart/49922977038613:1?attributes[pivota_click_id]={click}&country=US'
    await database.execute("UPDATE reap_agentic_purchases SET item_source='cart_link',cart_url=:cart,click_id=:click WHERE id=:id",{'id':pid,'cart':cart,'click':click})
    assert await svc.ccc.claim_click(click,claimed_by=svc.ccc.REAP_CLAIMANT,external_order_id='foreign_reap_order')
    before=dict(await database.fetch_one('SELECT * FROM conversion_click_claims WHERE click_id=:click',{'click':click}))
    row=await _get(pid)
    with pytest.raises(RuntimeError,match='manual_attribution_claim_conflict'):
        await resolve_checkout_manually(pid,evidence=evidence,operator_ref='operator_test',expected_updated_at=row['updated_at'],dry_run=False)
    assert await _get(pid)==row
    assert await database.fetch_val('SELECT count(*) FROM reap_checkout_manual_resolution_audit')==0
    assert await database.fetch_val('SELECT count(*) FROM commerce_attribution_edges')==0
    assert dict(await database.fetch_one('SELECT * FROM conversion_click_claims WHERE click_id=:click',{'click':click}))==before
