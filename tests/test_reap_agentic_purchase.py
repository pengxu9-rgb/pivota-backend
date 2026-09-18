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
        "expiresAt": "2026-09-17T21:00:00Z",
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
    "expiresAt": "2026-09-17T20:35:00Z",
    "amountBreakdown": {
        "itemsSubtotal": {"amount": 42.50, "currency": "USD"},
        "shipping": {"amount": 1.00, "currency": "USD"},
        "tax": {"amount": {"amount": 1.50, "currency": "USD"}, "includedInPrices": False},
        "finalAmount": {"amount": 45.00, "currency": "USD"},
    },
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
        return {"id": "edge_1"}


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
    (
        "an alias the ledger cannot carry",
        dict(row=_row(accept_variant_labels=("Flamingo Flirt - Cream",))),
        "resolution_hints_not_persistable",
    ),
    (
        "an extra domain the ledger cannot carry",
        dict(row=_row(also_accept_domains=("shop.brand.example",))),
        "resolution_hints_not_persistable",
    ),
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
    assert call["note_attrs_or_payload"] == {
        "source": "reap_agentic",
        "partner_reported": True,
        "purchase_id": purchase_id,
        "reap_checkout_id": "chk_7f3a",
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
    assert row["last_error_code"] == "ENROLLMENT_NOT_ACTIVE"
    assert row["state"] == "failed"
    assert row["buyer_email"] is None
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
    assert row["hosted_url_expires_at"] == datetime(2026, 9, 17, 21, 0, tzinfo=timezone.utc)


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
    assert result.last_error_code == "transport_error:ReadTimeout"
    after = await _get(purchase_id)
    assert after["state"] == state, "a transport failure must not move the row"
    assert after["claimed_by"] is None, "the lease is given back"
    assert after["next_poll_at"] > before["next_poll_at"]


def test_the_backoff_is_capped(monkeypatch):
    """Uncapped, a long interval plus repeated failures walks off into hours — and
    `expire_overdue_purchases` would terminate the purchase before the next poll ever ran."""
    assert svc.MAX_BACKOFF_SECONDS == 600
    for interval in svc.POLL_INTERVALS.values():
        assert min(interval * svc.TRANSPORT_BACKOFF_MULTIPLIER, svc.MAX_BACKOFF_SECONDS) <= 600


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
    assert result.last_error_code == "unknown_checkout_status"
    assert (await _get(purchase_id))["state"] == "awaiting_approval"
    assert attribution.calls == []


# ── 8. the fence ─────────────────────────────────────────────────────────────────────────────


async def test_the_worker_that_does_not_hold_the_claim_writes_nothing(reap, attribution):
    """TWO WORKERS, ONE ROW. `worker-a` holds the lease; `worker-b` runs a full step against the
    same purchase and every write it makes carries `AND claimed_by = 'worker-b'`, which matches
    nothing. It gets `lost_claim` and the row is untouched.

    This is the interleaving the fence exists for: A's view of the STATE is correct, and it is
    A's view of OWNERSHIP that is stale. Nothing in `from_states` can catch it."""
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
