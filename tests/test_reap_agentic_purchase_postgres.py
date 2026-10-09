"""services/reap_agentic_purchase.py against REAL Postgres — the half SQLite cannot test.

Picked up automatically by .github/workflows/postgres-dialect-gate.yml via the
`tests/test_*_postgres.py` glob.

NOT A COPY OF THE SQLITE ARM. Row-level semantics belong there; what is here is the set of
properties whose truth depends on the ENGINE, each one a defect shape this repo has shipped
before on some rail or other:

  1. THE CLAIM FENCE ACROSS TWO BACKEND CONNECTIONS. `AND claimed_by = :worker_id` is the guard
     between two POD PROCESSES, not two coroutines. On SQLite databases==0.7.0 serialises
     everything onto one connection, so the SQLite fence test proves the guard under
     interleaving and nothing at all about two connections. Here the winning statement is issued
     on its own asyncpg connection, committed, and THEN the service's step runs.

  2. jsonb COMES BACK AS TEXT. No SQLAlchemy result processor runs on a raw statement, so
     asyncpg hands back `queries_tried` and `shipping_address` verbatim as `str`. Both are
     written by this service — `queries_tried` on every refusal, `shipping_address` by
     `start_purchase` — and both are asserted BOTH raw and decoded, because the decoded
     assertion alone would pass on a driver that never had the problem.

  3. THE SERVER-SIDE CLOCK. `next_poll_at` is the one column this module computes in Python, and
     asyncpg encodes a naive datetime for a timestamptz as THIS PROCESS's local wall time —
     measured seven hours off from a Pacific box against a UTC database. A backoff that lands
     seven hours out is a purchase that never polls again.

  4. PREPARE / AmbiguousParameter. Every statement this service reaches is planned against the
     real schema, which SQLite cannot do. The service adds no SQL of its own, so this is the
     ledger's statements exercised through the service's real call shapes.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_reap_wp2b_test \\
        .venv/bin/python -m pytest tests/test_reap_agentic_purchase_postgres.py
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason=(
        "needs a Postgres DATABASE_URL — this is the production-dialect gate; "
        "see the module docstring for the one-line setup"
    ),
)

#: BOTH migrations, in order. 225 adds the three resolution-hint columns this package now
#: persists, and applying only 224 would build a schema the repo no longer declares — the failure
#: is an UndefinedColumnError on every test, which is loud, but the gate exists to catch the
#: quiet version of that.
_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db/migrations"
_MIGRATIONS = (
    _MIGRATIONS_DIR / "253_reap_checkout_manual_resolution_audit.sql",
    _MIGRATIONS_DIR / "224_reap_agentic_ledger.sql",
    _MIGRATIONS_DIR / "225_reap_agentic_purchase_hints.sql",
    # 229 adds item_source + cart_url (the cart-link lane). Without it the whole-table parity
    # test compares a self-heal that HAS them against a migration build that does not.
    _MIGRATIONS_DIR / "229_reap_agentic_purchase_item_source.sql",
    # 230 builds the click-claims table. The self-heal builds it too, so the migration list
    # applies it for parity with what the self-heal leaves behind.
    _MIGRATIONS_DIR / "230_conversion_click_claims.sql",
    # 233 adds consent_version + consented_at to reap_agentic_purchases. The self-heal carries
    # it, so a migration build without it is not the schema production has — and every purchase this suite opens
    # would fail on an UndefinedColumn. See
    # feedback_a_later_migration_that_alters_a_table_breaks_that_tables_own_parity_test.
    _MIGRATIONS_DIR / "233_reap_agentic_purchase_consent.sql",
    _MIGRATIONS_DIR / "247_reap_agentic_purchase_offer_code.sql",  # offer code + outcome + discount
    # 252: at most one PENDING enrollment per buyer (the self-heal builds it too).
    _MIGRATIONS_DIR / "252_reap_agentic_enrollments_one_pending.sql",
    _MIGRATIONS_DIR / "254_reap_enrollment_expiry_provenance.sql",
    _MIGRATIONS_DIR / "256_reap_enrollment_continuation.sql",
    _MIGRATIONS_DIR / "257_reap_parked_dispatch_resolution.sql",
    _MIGRATIONS_DIR / "258_reap_price_witness.sql",  # the price witness (dark dials)
)

# Same convention as tests/test_reap_agentic_ledger_postgres.py: this gate DROPS its tables, so
# it must be INCAPABLE of running anywhere but a throwaway — made true, not merely stated.
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")


# ── the same fixtures the SQLite arm uses, written out ───────────────────────────────────────
#
# DUPLICATED RATHER THAN IMPORTED, which is the house convention on this rail (the two ledger
# arms do the same). Importing the SQLite module would execute its module body — including a
# `pytestmark` that skips on Postgres and a `from db.database import IS_POSTGRES` bound at a
# different moment — and would make the dialect gate's collection depend on a file it is not
# collecting. The payload fixtures are small; the coupling is not.

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


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(
            f"refusing to drop the agentic-ledger tables in database {dbname!r} — "
            f"throwaway only (e.g. pivota_reap_wp2b_test)"
        )


async def _apply_migration():
    from db.database import database
    from db.sql_migrations import split_statements

    for path in _MIGRATIONS:
        for statement in split_statements(path.read_text(encoding="utf-8")):
            await database.execute(statement)


@pytest.fixture(autouse=True)
async def _db():
    from db.database import database

    _assert_throwaway_database()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    # Drop FIRST, so a constraint deleted from the migration cannot survive via IF NOT EXISTS
    # and leave this gate testing a schema the repo no longer declares.
    await database.execute("DROP TABLE IF EXISTS reap_checkout_manual_resolution_audit")
    await database.execute("DROP TABLE IF EXISTS reap_checkout_dispatch_resolution_audit")
    await database.execute("DROP TABLE IF EXISTS reap_agentic_purchases")
    await database.execute("DROP TABLE IF EXISTS reap_agentic_enrollments")
    await _apply_migration()
    try:
        yield
    finally:
        # TEARDOWN, NOT ONLY SETUP. The Postgres gate runs every tests/test_*_postgres.py in ONE
        # process against ONE database, in alphabetical order, so whatever the LAST test here
        # leaves behind is what the next FILE starts with. Setup-only cleaning protects this
        # suite from its predecessor and protects nobody from this suite — which is exactly how
        # tests/test_agent_commerce_reap_routes_postgres.py broke four tests in
        # tests/test_backfill_variant_identity_skus_postgres.py on this branch.
        #
        # These tables are ones this file DROPS at setup, so the leak it prevents is narrower
        # than the routes suite's: a later file that READS one without dropping it first. Cheap,
        # symmetric, and it means "leave it as you found it" is the rule everywhere on this rail
        # rather than the patch applied to the one file that got caught.
        #
        # In a `finally`, so a failing test still cleans up: a failing test is the one most
        # likely to have left a half-written row, and a cleanup that runs only on success turns
        # one red test into a cascade in another file.
        for _table in ('reap_agentic_purchases', 'reap_agentic_enrollments', 'reap_checkout_dispatch_resolution_audit'):
            try:
                await database.execute(f"DELETE FROM {_table}")
            except Exception:  # noqa: BLE001 - a table this run never built is not a leak
                continue
        if not was_connected and database.is_connected:
            await database.disconnect()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("REAP_AGENTIC_ENABLED", "1")
    monkeypatch.setenv("REAP_API_BASE_URL", "https://sandbox.api.reap.global")
    monkeypatch.setenv("REAP_API_KEY", "sk_test_key")
    monkeypatch.delenv("REAP_RETURN_URL_HOSTS", raising=False)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    import httpx

    class _NetworkForbidden:
        def __init__(self, *a, **k):
            raise AssertionError("a test reached the network; the Reap client must be faked")

    monkeypatch.setattr(httpx, "AsyncClient", _NetworkForbidden)


def _row(**over):
    import services.reap_agentic_purchase as svc

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


def _resolved(**over):
    import services.reap_agentic_client as rc

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


def _ok(data):
    import services.reap_agentic_client as rc

    return rc.ReapResponse(ok=True, status=200, data=json.loads(json.dumps(data)))


def _transport(kind: str = "ReadTimeout"):
    import services.reap_agentic_client as rc

    return rc.ReapResponse(ok=False, error=f"transport_error:{kind}")


def _transport_resolution(kind: str = "ReadTimeout"):
    import services.reap_agentic_client as rc

    return rc.VariantResolution(ok=False, reason=f"transport_error:{kind}", queries_tried=["q"])


class FakeReap:
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
    import services.reap_agentic_client as rc

    fake = FakeReap()

    def _install(name):
        async def _call(*args, **kwargs):
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
    import services.commerce_attribution_service as cas

    recorder = Attribution()
    monkeypatch.setattr(cas, "close_external_order_conversion", recorder)
    return recorder


# ── helpers ──────────────────────────────────────────────────────────────────────────────────


async def _start(**over) -> str:
    import services.reap_agentic_purchase as svc

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


async def _get(purchase_id: str):
    import db.reap_agentic_ledger as ledger

    return await ledger.get_purchase_internal(purchase_id)


async def _raw(sql: str, params: dict):
    from db.database import database

    return await database.execute(sql, params)


async def _claim(purchase_id: str, worker_id: str) -> None:
    await _raw(
        "UPDATE reap_agentic_purchases SET claimed_by = :w, claimed_at = clock_timestamp() "
        "WHERE id = :i",
        {"w": worker_id, "i": purchase_id},
    )


async def _step(purchase_id: str, worker_id: str = "w1"):
    import services.reap_agentic_purchase as svc

    await _claim(purchase_id, worker_id)
    return await svc.advance(purchase_id, worker_id)


async def _active_enrollment(buyer_ref="bref_alice", reap_id=ENROLLMENT_UUID):
    import db.reap_agentic_ledger as ledger

    created = await ledger.upsert_pending_enrollment(
        buyer_ref=buyer_ref, reap_enrollment_id=reap_id
    )
    await ledger.mark_enrollment_active(created["id"], reap_enrollment_id=reap_id)
    return created


async def _drive_to_completed():
    purchase_id = await _start()
    for _ in range(4):
        await _step(purchase_id)
    return purchase_id


# ── the second-connection plumbing, lifted from the ledger's own gate ────────────────────────

_BIND_RE = re.compile(r":([a-zA-Z_][a-zA-Z0-9_]*)")


def _to_positional(sql: str):
    """Rewrite `:name` binds to asyncpg's `$n`, keeping first-appearance order.

    This lets the test below execute THE LEDGER'S OWN SQL TEXT on a raw asyncpg connection rather
    than a hand-copied paraphrase. A paraphrase would keep passing after somebody removed the
    fence from the real statement, which is the entire failure mode the test exists for."""
    order = []

    def repl(match):
        name = match.group(1)
        if name not in order:
            order.append(name)
        return f"${order.index(name) + 1}"

    assert "':" not in sql and ":'" not in sql
    return _BIND_RE.sub(repl, sql), order


async def _raw_connection():
    import asyncpg

    return await asyncpg.connect(DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://"))


async def _run_on(conn, sql: str, params: dict):
    positional, order = _to_positional(sql)
    return await conn.fetch(positional, *[params[name] for name in order])


def _transition_params(purchase_id: str, from_states, to_state: str, holder=None, **fields):
    """Exactly the parameter dict `ledger.transition` builds, from the module's own field list
    and slot count, so it cannot drift."""
    import db.reap_agentic_ledger as ledger

    sources = list(dict.fromkeys(from_states))
    params = {
        "id": purchase_id,
        "to_state": to_state,
        "to_state_probe": to_state,
        "holder": holder,
        "holder_unfenced": 1 if holder is None else 0,
    }
    for index in range(ledger._FROM_SLOTS):
        params[f"from_{index}"] = sources[index] if index < len(sources) else sources[0]
    for column in ledger._TRANSITION_FIELDS:
        raw = fields.get(column)
        params[column] = (
            ledger._bind_json(raw) if column in ledger._JSON_COLUMNS else ledger._bind_dt(raw)
        )
    return params


# ── 1. the fence, across two real backend connections ────────────────────────────────────────


async def test_a_second_connection_that_took_the_claim_makes_our_whole_step_a_no_op(reap):
    """THE FENCE, PROVEN ACROSS BACKENDS AND THROUGH THE SERVICE.

    Another pod claims the row on its OWN asyncpg connection and commits. Our worker then runs a
    complete `advance` — it reads the row, calls the partner, and tries to write — and every
    write it makes carries `AND claimed_by = 'worker-a'`, which no longer matches. It gets
    `lost_claim` and the row is byte-for-byte where it was.

    Deterministic on purpose. A gathered-coroutine race can pass by accident when the runtime
    happens to serialise both calls onto one connection — which is exactly what the SQLite arm
    does — so the load-bearing version forces the ordering."""
    import services.reap_agentic_purchase as svc

    purchase_id = await _start()
    await _claim(purchase_id, "worker-a")
    before = await _get(purchase_id)

    conn = await _raw_connection()
    try:
        rows = await conn.fetch(
            "UPDATE reap_agentic_purchases SET claimed_by = $1, claimed_at = clock_timestamp() "
            "WHERE id = $2 AND claimed_by = $3 RETURNING id",
            "worker-b",
            purchase_id,
            "worker-a",
        )
        assert len(rows) == 1, "the other pod's claim should have landed"
    finally:
        await conn.close()

    result = await svc.advance(purchase_id, "worker-a")

    assert result.outcome == "lost_claim"
    after = await _get(purchase_id)
    assert after["state"] == "resolving"
    assert after["claimed_by"] == "worker-b", "the loser must not steal or clear the new lease"
    assert after["reap_variant_id"] is None
    assert after["enrollment_id"] is None
    assert after["updated_at"] == before["updated_at"], "no write landed at all"


async def test_a_second_connection_that_already_advanced_the_row_makes_our_step_a_no_op(reap):
    """The other half: the lease is still ours, but the STATE moved under us. The ledger's own
    `AND state IN (...)` conjunct answers None and the service reports the same lost race — it
    does not distinguish, and it must not, because the response to both is "re-read"."""
    import db.reap_agentic_ledger as ledger
    import services.reap_agentic_purchase as svc

    purchase_id = await _start()
    await _claim(purchase_id, "worker-a")

    conn = await _raw_connection()
    try:
        rows = await _run_on(
            conn,
            ledger._TRANSITION_SQL,
            _transition_params(purchase_id, ["resolving"], "refused", holder="worker-a"),
        )
        assert len(rows) == 1
    finally:
        await conn.close()

    result = await svc.advance(purchase_id, "worker-a")
    assert result.outcome == "terminal", "the row is refused now; a terminal row makes no calls"
    assert reap.calls == []


async def test_the_losing_release_is_fenced_on_the_other_connection_too(reap):
    """The no-progress path. Without the fence on `release_claim`, a worker whose lease was
    requeued and handed on would clear the NEW holder's claim on its way out."""
    import services.reap_agentic_purchase as svc

    reap.resolve_our_row = _transport_resolution()
    purchase_id = await _start()
    await _claim(purchase_id, "worker-a")

    conn = await _raw_connection()
    try:
        await conn.execute(
            "UPDATE reap_agentic_purchases SET claimed_by = 'worker-b' WHERE id = $1", purchase_id
        )
    finally:
        await conn.close()

    result = await svc.advance(purchase_id, "worker-a")
    assert result.outcome == "lost_claim"
    assert (await _get(purchase_id))["claimed_by"] == "worker-b"


# ── 2. jsonb: what the driver really hands back ──────────────────────────────────────────────


async def test_the_refusals_queries_are_jsonb_and_come_back_as_text_until_the_module_decodes(
    reap,
):
    """TWO ASSERTIONS, AND THE FIRST ONE IS THE POINT. asyncpg hands back jsonb VERBATIM as a
    `str` for a raw statement — no SQLAlchemy result processor runs — so the decoded assertion on
    its own would pass just as happily on a driver that never had the problem.

    `queries_tried` is the column this SERVICE writes on every refusal, and it is written through
    the transition's `COALESCE(CAST(:x AS JSONB), CAST(col AS JSONB))`, which is unplannable on
    Postgres if either arm is left uncast."""
    import services.reap_agentic_client as rc
    from db.database import database

    reap.resolve_our_row = rc.VariantResolution(
        ok=False, reason="search:merchant_not_in_results", queries_tried=["a b", "c d"]
    )
    purchase_id = await _start()
    await _step(purchase_id)

    raw = await database.fetch_one(
        "SELECT queries_tried FROM reap_agentic_purchases WHERE id = :i", {"i": purchase_id}
    )
    assert isinstance(raw["queries_tried"], str), "jsonb arrives as text on this driver"
    assert json.loads(raw["queries_tried"]) == ["a b", "c d"]
    assert (await _get(purchase_id))["queries_tried"] == ["a b", "c d"]


async def test_the_shipping_address_round_trips_as_jsonb_and_is_nulled_on_terminal(reap):
    """`shipping_address` is written by `start_purchase` through the INSERT's `CAST(:x AS JSONB)`
    and read back by `advance` to build the quote. A dict bound through raw SQL dies at Bind time
    on asyncpg, which the PREPARE gate structurally cannot see — so it is exercised here."""
    from db.database import database

    purchase_id = await _start()
    raw = await database.fetch_one(
        "SELECT shipping_address FROM reap_agentic_purchases WHERE id = :i", {"i": purchase_id}
    )
    assert isinstance(raw["shipping_address"], str)
    assert json.loads(raw["shipping_address"])["addressLine1"] == "900 Brannan St"
    assert (await _get(purchase_id))["shipping_address"]["city"] == "San Francisco"

    await _drive_one(purchase_id, reap)
    done = await _get(purchase_id)
    assert done["state"] == "completed"
    assert done["shipping_address"] is None
    assert done["buyer_email"] is None


async def _drive_one(purchase_id, reap):
    for _ in range(4):
        await _step(purchase_id)


async def test_the_address_reaches_the_quote_as_a_dict_not_as_jsonb_text(reap):
    """The decode is what makes `request_quote(shipping_address=...)` receive an object. If the
    ledger handed back the raw jsonb string, `build_shipping_address` would raise
    `shippingAddress must be an object` and the purchase would fail on Postgres only."""
    purchase_id = await _start()
    await _step(purchase_id)
    await _step(purchase_id)
    await _step(purchase_id)
    sent = reap.named("request_quote")[0]["shipping_address"]
    assert isinstance(sent, dict)
    assert sent["addressLine1"] == "900 Brannan St"


# ── 3. the clock ─────────────────────────────────────────────────────────────────────────────


async def test_a_backoff_lands_where_the_server_thinks_it_should_under_a_shifted_timezone(
    reap, monkeypatch
):
    """asyncpg encodes a NAIVE datetime for a timestamptz as THIS PROCESS's local wall time —
    measured seven hours off from a Pacific box against a UTC database. `next_poll_at` is the one
    column this module computes in Python, so it is the one that can carry that error, and a
    backoff seven hours out is a purchase that never polls again.

    The process timezone is shifted for the duration; `_now()` returns an AWARE UTC datetime and
    `ledger._bind_dt` keeps it aware, so the value must land ~120 s ahead of the SERVER's clock
    whatever TZ says."""
    import time

    from db.database import database

    reap.resolve_our_row = _transport_resolution()
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    try:
        purchase_id = await _start()
        result = await _step(purchase_id)
        assert result.outcome == "released" and result.next_poll_in_seconds == 120
        row = await database.fetch_one(
            "SELECT EXTRACT(EPOCH FROM (next_poll_at - clock_timestamp())) AS delta "
            "FROM reap_agentic_purchases WHERE id = :i",
            {"i": purchase_id},
        )
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()
    assert 100 <= float(row["delta"]) <= 140, (
        "the backoff must be measured against the SERVER's clock, not this process's local time"
    )


async def test_a_released_row_becomes_claimable_again_only_when_its_poll_is_due(reap):
    """The service's schedule and the ledger's `claim_due_purchases` have to agree, and the
    comparison is `next_poll_at <= clock_timestamp()` on the server."""
    import db.reap_agentic_ledger as ledger

    reap.resolve_our_row = _transport_resolution()
    purchase_id = await _start()
    await _step(purchase_id)

    assert await ledger.claim_due_purchases("w2", limit=10) == []
    await _raw(
        "UPDATE reap_agentic_purchases SET next_poll_at = clock_timestamp() - "
        "INTERVAL '1 second' WHERE id = :i",
        {"i": purchase_id},
    )
    claimed = await ledger.claim_due_purchases("w2", limit=10)
    assert [c["id"] for c in claimed] == [purchase_id]


# ── 4. the whole machine, on the production dialect ──────────────────────────────────────────


async def test_the_happy_path_completes_and_closes_the_conversion(reap, attribution):
    purchase_id = await _drive_to_completed()
    row = await _get(purchase_id)
    assert row["state"] == "completed"
    assert row["reap_order_id"] == "ord_991"
    assert row["quoted_total_minor"] == 4500
    assert row["shipping_minor"] == 100
    assert row["tax_minor"] == 150
    assert row["final_total_minor"] == 4500
    assert row["terminal_at"] is not None
    assert row["claimed_by"] is None
    assert len(attribution.calls) == 1
    assert attribution.calls[0]["gross_amount_cents"] == 4500
    assert attribution.calls[0]["external_order_id"] == "ord_991"


async def test_two_purchases_cannot_claim_one_reap_checkout(reap):
    """`uq_reap_agentic_purchases_checkout` is a PARTIAL unique index (SQLite's self-heal twin
    declares it differently, so only this arm exercises the real one). Two of our rows believing
    they own ONE charge is the worst outcome available on this rail, and the index refuses it.

    THE REFUSAL IS A RAW DRIVER EXCEPTION OUT OF `advance`, NOT AN `AdvanceResult`, and that is
    recorded here rather than smoothed over: this service does not catch a unique violation, so
    the POLLER must run each row's step inside its own try/except or one impossible row stops the
    batch. That obligation is in docs/runbooks/reap_agentic_purchase.md. It is deliberate — the
    condition means the partner handed us an id we have already used, which is not something to
    swallow."""
    first = await _start()
    for _ in range(3):
        await _step(first)
    assert (await _get(first))["reap_checkout_id"] == "chk_7f3a"

    second = await _start()
    with pytest.raises(Exception) as exc:
        for _ in range(3):
            await _step(second)
    assert "uq_reap_agentic_purchases_checkout" in str(exc.value)


async def test_a_price_change_refuses_and_strips_the_pii(reap, attribution):
    reap.resolve_our_row = _resolved(price=(49.99, "USD"), price_disagrees=True)
    purchase_id = await _start()
    await _step(purchase_id)
    row = await _get(purchase_id)
    assert row["state"] == "refused"
    assert row["refusal_reason"] == "price_changed"
    assert row["buyer_email"] is None and row["shipping_address"] is None
    assert attribution.calls == []


async def test_a_hostile_checkout_url_never_reaches_the_row(reap):
    """`hosted_action` is the REAL one here. `evilprava.space` passes a naive
    `endswith("prava.space")` and fails the exact-or-dot-suffix rule the client actually applies.

    The buyer is pre-enrolled so the row carries NO hosted URL at all when the checkout is
    created — otherwise `hosted_url` would still hold the (legitimate) enrolment link and the
    absence assertion below would be about the wrong column."""
    reap.create_checkout = _ok(HOSTILE_CHECKOUT)
    await _active_enrollment()
    purchase_id = await _start()
    for _ in range(2):
        await _step(purchase_id)
    row = await _get(purchase_id)
    assert row["state"] == "awaiting_approval"
    assert row["hosted_url"] is None
    assert "evilprava.space" not in json.dumps(row, default=str)


async def test_enrollment_not_active_fails_with_the_partners_own_detail_code(reap):
    import services.reap_agentic_client as rc

    reap.create_checkout = rc.ReapResponse(
        ok=False,
        status=400,
        error="reap_status_400",
        error_code="AGENTIC_REQUEST_REJECTED",
        error_detail_code="ENROLLMENT_NOT_ACTIVE",
    )
    purchase_id = await _start()
    for _ in range(3):
        await _step(purchase_id)
    row = await _get(purchase_id)
    assert row["state"] == "failed"
    assert row["last_error_code"] == "enrollment_not_active"


async def test_a_503_on_the_quote_refuses_as_not_completable(reap):
    import services.reap_agentic_client as rc

    reap.request_quote = rc.ReapResponse(
        ok=False, status=503, error="reap_status_503", merchant_probably_not_completable=True
    )
    purchase_id = await _start()
    for _ in range(3):
        await _step(purchase_id)
    row = await _get(purchase_id)
    assert row["state"] == "refused"
    assert row["refusal_reason"] == "merchant_not_completable"
    assert reap.named("create_checkout") == []


async def test_an_unknown_checkout_status_never_advances(reap, attribution):
    reap.get_checkout = _ok(dict(CHECKOUT_COMPLETED, status="Completed"))
    purchase_id = await _start()
    for _ in range(4):
        result = await _step(purchase_id)
    assert result.outcome == "released"
    assert result.last_error_code == "checkout_read_permanent:1:unknown_checkout_status"
    assert (await _get(purchase_id))["state"] == "awaiting_approval"
    assert attribution.calls == []


async def test_a_failed_read_on_a_lapsed_quote_names_the_lapsed_window(reap, attribution):
    """`reap_quote_expires_at` is a `timestamptz` here, read back aware and compared against the
    service's aware `_now()`. Measured 2026-09-25: an unapproved checkout is FAILED — not
    EXPIRED — seconds after the quote's expiry, so the code says the window lapsed."""
    from datetime import datetime, timedelta, timezone

    purchase_id = await _start()
    for _ in range(3):
        result = await _step(purchase_id)
    assert result.state == "awaiting_approval", result
    await _raw(
        "UPDATE reap_agentic_purchases SET reap_quote_expires_at = :t WHERE id = :i",
        {"t": datetime.now(timezone.utc) - timedelta(seconds=5), "i": purchase_id},
    )
    reap.get_checkout = _ok(dict(CHECKOUT_COMPLETED, status="FAILED"))
    result = await _step(purchase_id)
    assert result.state == "failed"
    row = await _get(purchase_id)
    assert row["last_error_code"] == "approval_window_lapsed"
    assert row["buyer_email"] is None
    assert attribution.calls == []


async def test_a_failed_read_on_a_live_quote_is_still_a_checkout_failure(reap, attribution):
    """CONTROL on this dialect: a quote four minutes from expiry is not a lapsed window."""
    from datetime import datetime, timedelta, timezone

    purchase_id = await _start()
    for _ in range(3):
        await _step(purchase_id)
    await _raw(
        "UPDATE reap_agentic_purchases SET reap_quote_expires_at = :t WHERE id = :i",
        {"t": datetime.now(timezone.utc) + timedelta(minutes=4), "i": purchase_id},
    )
    reap.get_checkout = _ok(dict(CHECKOUT_COMPLETED, status="FAILED"))
    assert (await _step(purchase_id)).state == "failed"
    assert (await _get(purchase_id))["last_error_code"] == "checkout_failed"


async def test_every_start_refusal_leaves_the_table_empty(reap):
    """The same ordering contract as the SQLite arm, re-run here because an INSERT that fails at
    BIND time on asyncpg is a DIFFERENT failure from one refused in Python, and only one of them
    leaves nothing behind."""
    import services.reap_agentic_purchase as svc
    from db.database import database

    bad = [
        dict(quantity=0),
        dict(row=_row(currency="USDD")),
        # A hint the LEDGER refuses (a label carrying a control character). The list itself is
        # persisted now; what still refuses is a bad shape, and it must still leave no row.
        dict(row=_row(accept_variant_labels=("Flamingo\nFlirt",))),
        dict(return_url="https://evil.example/x"),
        dict(buyer=svc.BuyerContact(EMAIL, {})),
    ]
    for over in bad:
        with pytest.raises(svc.PurchaseRefused):
            await _start(**over)
    count = await database.fetch_one("SELECT COUNT(*) AS n FROM reap_agentic_purchases")
    assert int(count["n"]) == 0
    assert reap.calls == []


async def test_no_rail_log_record_carries_the_buyers_details(reap, attribution, caplog):
    caplog.set_level(logging.DEBUG)
    purchase_id = await _drive_to_completed()
    assert (await _get(purchase_id))["state"] == "completed"
    ours = [
        r
        for r in caplog.records
        if r.name.startswith(("services.reap_agentic", "db.reap_agentic"))
    ]
    haystack = "\n".join([r.getMessage() for r in ours] + [repr(r.args) for r in ours])
    for secret in PII_STRINGS:
        assert secret not in haystack
    assert "reap_agentic" in haystack


# ── 5. the review's four blocking findings, on the production dialect ────────────────────────
#
# Each of these is here rather than only on SQLite because the reviewer's mutants for them
# survived BOTH arms, and because the money checks have to hold where the money is.


def _quote(_quantity=None, _variant=None, **breakdown_over):
    """QUOTE_200 with its breakdown overridden, and its ITEMS echo kept in step.

    `_quantity` / `_variant` exist because the echo check compares them: a test that raised the
    quantity without moving the echo would be testing `quote_items_mismatch` while believing it
    was testing the subtotal arithmetic."""
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


async def test_a_quote_must_price_the_whole_quantity(reap):
    """QUANTITY > 1 — the case a check written as `subtotal == our_price_minor` passes on every
    quantity-1 test in either suite, while buying three bottles for the price of one."""
    reap.request_quote = _ok(_quote(_quantity=3, itemsSubtotal=_usd(42.50),
                                    finalAmount=_usd(45.00)))
    await _active_enrollment()
    purchase_id = await _start(quantity=3)
    for _ in range(3):
        await _step(purchase_id)
    row = await _get(purchase_id)
    assert row["state"] == "refused"
    assert row["refusal_reason"] == "price_changed"
    assert row["last_error_code"] == "quote_items_subtotal_mismatch"
    assert reap.named("create_checkout") == []
    assert row["buyer_email"] is None and row["shipping_address"] is None


async def test_the_right_quantity_at_the_right_price_is_accepted(reap):
    """CONTROL: the multiplication must accept the correct number too."""
    reap.request_quote = _ok(_quote(_quantity=3, itemsSubtotal=_usd(127.50),
                                    finalAmount=_usd(130.00)))
    await _active_enrollment()
    purchase_id = await _start(quantity=3)
    # two steps: the buyer is already enrolled, so resolving -> quoting -> awaiting_approval.
    for _ in range(2):
        await _step(purchase_id)
    row = await _get(purchase_id)
    assert row["state"] == "awaiting_approval"
    assert row["quoted_total_minor"] == 13000


async def test_a_second_currency_quote_refuses_and_stores_no_totals(reap):
    """A WHOLLY-EUR QUOTE AGAINST A USD ROW. On the old code every amount came back None and the
    transition's `COALESCE(:col, col)` swallowed all four — a live EUR checkout with NULL totals.
    Asserted on Postgres because the COALESCE that did the swallowing is the Postgres statement,
    jsonb casts and all."""
    reap.request_quote = _ok(_quote(
        itemsSubtotal={"amount": 42.50, "currency": "EUR"},
        shipping={"amount": 1.00, "currency": "EUR"},
        tax={"amount": {"amount": 1.50, "currency": "EUR"}, "includedInPrices": False},
        finalAmount={"amount": 45.00, "currency": "EUR"},
    ))
    await _active_enrollment()
    purchase_id = await _start()
    for _ in range(3):
        await _step(purchase_id)
    row = await _get(purchase_id)
    assert row["state"] == "refused"
    assert row["last_error_code"] == "quote_currency_mismatch"
    assert row["quoted_total_minor"] is None
    assert row["shipping_minor"] is None
    assert row["tax_minor"] is None
    assert reap.named("create_checkout") == []


async def test_zero_shipping_is_an_amount_on_this_dialect_too(reap):
    """The repo's one converter refuses zero, so shipping and tax go through a sibling that does
    not. Free shipping is the commonest quote there is; if this refused, almost everything
    would."""
    reap.request_quote = _ok(_quote(
        shipping=_usd(0.00),
        tax={"amount": _usd(0.00), "includedInPrices": True},
        finalAmount=_usd(42.50),
    ))
    await _active_enrollment()
    purchase_id = await _start()
    # two steps: the buyer is already enrolled, so resolving -> quoting -> awaiting_approval.
    for _ in range(2):
        await _step(purchase_id)
    row = await _get(purchase_id)
    assert row["state"] == "awaiting_approval"
    assert row["shipping_minor"] == 0 and row["tax_minor"] == 0


async def test_the_loser_calls_the_partner_not_at_all_from_a_second_connection(reap):
    """P1-2 ACROSS TWO REAL BACKEND CONNECTIONS. The other pod takes the claim on its own asyncpg
    connection and commits; our worker then runs a full step in 'quoting' and must make NO
    partner call — the old code ran resolve → request_quote → create_checkout and left a live
    checkout at Reap, bound to the buyer's enrollment, that no row references."""
    import services.reap_agentic_purchase as svc

    await _active_enrollment()
    purchase_id = await _start()
    await _step(purchase_id)                       # -> quoting
    await _claim(purchase_id, "worker-a")
    before = await _get(purchase_id)

    conn = await _raw_connection()
    try:
        rows = await conn.fetch(
            "UPDATE reap_agentic_purchases SET claimed_by = $1 WHERE id = $2 AND claimed_by = $3 "
            "RETURNING id",
            "worker-b", purchase_id, "worker-a",
        )
        assert len(rows) == 1
    finally:
        await conn.close()

    reap.calls.clear()
    result = await svc.advance(purchase_id, "worker-a")

    assert result.outcome == "lost_claim"
    assert reap.calls == []
    after = await _get(purchase_id)
    assert after["state"] == "quoting"
    assert after["reap_quote_id"] is None
    assert after["reap_checkout_id"] is None
    assert after["updated_at"] == before["updated_at"]


async def test_the_loser_in_resolving_writes_nothing_to_the_enrollments_table(reap):
    """`upsert_pending_enrollment` takes no holder, so these are UNFENCED writes and the re-read
    is all there is. Two real connections."""
    import services.reap_agentic_purchase as svc
    from db.database import database

    purchase_id = await _start()
    await _claim(purchase_id, "worker-a")
    conn = await _raw_connection()
    try:
        await conn.execute(
            "UPDATE reap_agentic_purchases SET claimed_by = 'worker-b' WHERE id = $1", purchase_id
        )
    finally:
        await conn.close()

    reap.calls.clear()
    assert (await svc.advance(purchase_id, "worker-a")).outcome == "lost_claim"
    assert reap.calls == []
    count = await database.fetch_one("SELECT COUNT(*) AS n FROM reap_agentic_enrollments")
    assert int(count["n"]) == 0


async def test_a_claim_lost_mid_step_is_still_caught_by_the_fence(reap):
    """PAST the re-read: the claim moves on another connection INSIDE the partner call, so only
    the `AND claimed_by = :worker_id` conjunct in the UPDATE can catch it. This is the test that
    dies when the fence's None is ignored."""
    import services.reap_agentic_purchase as svc

    purchase_id = await _start()
    await _claim(purchase_id, "w1")

    async def _steal(**kwargs):
        conn = await _raw_connection()
        try:
            await conn.execute(
                "UPDATE reap_agentic_purchases SET claimed_by = 'worker-b' WHERE id = $1",
                purchase_id,
            )
        finally:
            await conn.close()
        return _resolved()

    reap.resolve_our_row = _steal
    result = await svc.advance(purchase_id, "w1")
    assert result.outcome == "lost_claim"
    row = await _get(purchase_id)
    assert row["state"] == "resolving"
    assert row["reap_variant_id"] is None
    assert row["claimed_by"] == "worker-b"


async def test_a_completed_checkout_with_no_order_id_completes_and_sheds_its_pii(
    reap, attribution
):
    """REVIEW ROUND 2, P0, ON THE DIALECT WHERE THE SWEEPS RUN.

    The previous fix parked this in 'processing' so a human would see it — and 'processing' has
    NO PII deadline: `expire_overdue_purchases` sweeps only 'needs_enrollment' and
    'awaiting_approval', and `fail_exhausted_purchases` skips 'processing' by default. Asserted
    here by RUNNING both sweeps against a row aged past every threshold, rather than by reading
    their source: the row must already be terminal and already stripped."""
    import db.reap_agentic_ledger as ledger

    reap.get_checkout = _ok({k: v for k, v in CHECKOUT_COMPLETED.items() if k != "orderId"})
    purchase_id = await _start()
    for _ in range(4):
        result = await _step(purchase_id)
    row = await _get(purchase_id)

    assert result.state == "completed"
    assert row["state"] == "completed"
    assert row["terminal_at"] is not None
    assert row["buyer_email"] is None, "the terminal write is what sheds the PII"
    assert row["shipping_address"] is None
    assert row["reap_order_id"] is None
    assert row["last_error_code"] == "completed_without_order_id"
    assert row["reap_checkout_id"] == "chk_7f3a", "the handle a reconciliation needs"
    assert attribution.calls == []

    # THE CONTROL, and the measurement the reviewer made: age the row past every deadline and run
    # both default sweeps. Neither can reach 'processing', so had the row parked there it would
    # still be holding the buyer's address at this point.
    await _raw(
        "UPDATE reap_agentic_purchases SET state_entered_at = TIMESTAMPTZ '2020-01-01', "
        "updated_at = TIMESTAMPTZ '2020-01-01', attempts = 999 WHERE id = :i",
        {"i": purchase_id},
    )
    assert await ledger.expire_overdue_purchases(max_age_seconds=60, limit=50) == []
    assert await ledger.fail_exhausted_purchases(5, limit=50) == []
    still = await _get(purchase_id)
    assert still["buyer_email"] is None and still["shipping_address"] is None


async def test_a_completed_order_with_no_usable_amount_writes_no_edge(reap, attribution):
    """P1-4. `close_external_order_conversion` is idempotent on `(merchant, external_order_id)`
    via ON CONFLICT DO NOTHING, so a None-gross edge is PERMANENT and no later close can replace
    it. The row completes — the order exists — and says why, leaving the slot free."""
    reap.get_checkout = _ok({k: v for k, v in CHECKOUT_COMPLETED.items() if k != "finalAmount"})
    purchase_id = await _start()
    for _ in range(4):
        await _step(purchase_id)
    row = await _get(purchase_id)
    assert row["state"] == "completed"
    assert row["reap_order_id"] == "ord_991"
    assert row["final_total_minor"] is None
    assert row["last_error_code"] == "final_amount_missing"
    assert attribution.calls == []


async def test_a_malformed_partner_id_never_reaches_a_varchar_column(reap):
    """P2-6, on the dialect where the column is a real VARCHAR(128): `chk/../../admin` used to be
    stored, the row entered 'awaiting_approval', and every later poll raised out of `advance`
    when the client validated the id into a URL path — unbounded, because that state is exempt
    from the attempts counter."""
    reap.create_checkout = _ok(dict(CHECKOUT_CREATED, id="chk/../../admin"))
    await _active_enrollment()
    purchase_id = await _start()
    for _ in range(3):
        await _step(purchase_id)
    row = await _get(purchase_id)
    assert row["state"] == "quoting"
    assert row["last_error_code"] == "checkout_dispatch_unresolved"
    assert row["reap_checkout_id"] is None


async def test_an_expired_quote_is_never_turned_into_a_checkout(reap):
    """P2-9."""
    reap.request_quote = _ok(dict(QUOTE_200, expiresAt="2020-01-01T00:00:00Z"))
    await _active_enrollment()
    purchase_id = await _start()
    await _step(purchase_id)
    result = await _step(purchase_id)
    assert result.outcome == "released"
    assert result.last_error_code == "quote_expired"
    assert reap.named("create_checkout") == []
    assert (await _get(purchase_id))["state"] == "quoting"


async def test_the_backoff_accumulates_against_the_ledgers_own_attempts_counter(reap):
    """P3-10 end to end. `attempts` is incremented by the CLAIM statement, so this drives real
    claims on real Postgres rather than setting the column, and reads the sequence back out of
    the scheduled `next_poll_at`."""
    import services.reap_agentic_purchase as svc

    reap.resolve_our_row = _transport_resolution()
    purchase_id = await _start()
    seen = []
    for _ in range(4):
        claimed = await ledger_module().claim_due_purchases("w1", limit=5)
        assert [c["id"] for c in claimed] == [purchase_id]
        seen.append((await svc.advance(purchase_id, "w1")).next_poll_in_seconds)
        await _raw(
            "UPDATE reap_agentic_purchases SET next_poll_at = clock_timestamp() - "
            "INTERVAL '5 seconds' WHERE id = :i",
            {"i": purchase_id},
        )
    assert seen == [120, 240, 480, 600]
    assert svc.MAX_BACKOFF_SECONDS in seen, "the cap is reachable"


def ledger_module():
    import db.reap_agentic_ledger as ledger

    return ledger


async def test_a_bidi_override_never_reaches_the_jsonb_column(reap):
    """P3-11 on the dialect that actually stores jsonb. U+202E inside `firstName` reverses the
    rendering of everything after it — on a shipping label and in our own owner view."""
    import services.reap_agentic_purchase as svc
    from db.database import database

    purchase_id = await _start(
        buyer=svc.BuyerContact(EMAIL, dict(ADDRESS, firstName="Ada\u202eelbaroved"))
    )
    raw = await database.fetch_one(
        "SELECT shipping_address FROM reap_agentic_purchases WHERE id = :i", {"i": purchase_id}
    )
    assert "\u202e" not in raw["shipping_address"]
    assert "202e" not in raw["shipping_address"].lower()
    assert json.loads(raw["shipping_address"])["firstName"] == "Adaelbaroved"


async def test_a_substituted_variant_is_refused_even_at_the_right_price(reap):
    """Reap returns 200 for a SUBSTITUTED variant. Every other check in `verify_quote` looks at
    what the quote COSTS; only the items echo looks at what is being bought."""
    reap.request_quote = _ok(_quote(_variant="var_SOMETHING_ELSE"))
    await _active_enrollment()
    purchase_id = await _start()
    # two steps: the buyer is already enrolled, so resolving -> quoting -> the quote check.
    for _ in range(2):
        await _step(purchase_id)
    row = await _get(purchase_id)
    assert row["state"] == "refused"
    assert row["refusal_reason"] == "price_unverifiable"
    assert row["last_error_code"] == "quote_items_mismatch"
    assert reap.named("create_checkout") == []
    assert row["buyer_email"] is None


@pytest.mark.parametrize("bad", [True, 12345, None, ["ord_1"]])
async def test_an_order_id_that_is_not_a_string_completes_without_an_edge(bad, reap, attribution):
    """`str(value).strip()` turned `orderId: true` into `"True"`. That value is half of an
    idempotency key with ON CONFLICT DO NOTHING, so every order at the merchant would collide on
    `(merchant, "True")`: one edge written, the rest dropped, the GMV reading as one sale for
    ever."""
    reap.get_checkout = _ok(dict(CHECKOUT_COMPLETED, orderId=bad))
    purchase_id = await _start()
    for _ in range(4):
        await _step(purchase_id)
    row = await _get(purchase_id)
    assert row["state"] == "completed"
    assert row["reap_order_id"] is None
    assert row["last_error_code"] == "completed_without_order_id"
    assert row["buyer_email"] is None
    assert attribution.calls == []


@pytest.mark.parametrize("charged,edge", [(45.00, True), (45.01, True), (52.00, False)])
async def test_the_edge_is_only_written_when_the_charge_matches_the_quote(
    charged, edge, reap, attribution
):
    """`quoted_total_minor` was written by the quoting step and, until this round, read by
    NOTHING. The buyer approved a hosted page showing the quote; a charge that differs is a
    partner-side re-price, and the edge is idempotent — the first value written is the only value
    there will ever be."""
    reap.get_checkout = _ok(
        dict(CHECKOUT_COMPLETED, finalAmount={"amount": charged, "currency": "USD"})
    )
    purchase_id = await _start()
    for _ in range(4):
        await _step(purchase_id)
    row = await _get(purchase_id)
    assert row["state"] == "completed"
    assert row["quoted_total_minor"] == 4500, "control: the quote was recorded"
    if edge:
        assert len(attribution.calls) == 1
        assert attribution.calls[0]["gross_amount_cents"] == int(round(charged * 100))
        assert row["last_error_code"] is None
    else:
        assert attribution.calls == []
        assert row["last_error_code"] == "charged_total_differs"


async def test_a_three_decimal_currency_is_refused_before_any_row_is_written(reap):
    """`_exponent` assumes two decimals for everything outside the repo's zero-decimal list, so
    1.234 KWD would be stored as 123 fils rather than 1234 — a tenfold error that stays
    self-consistent all the way to the charge, with no symptom at all."""
    import services.reap_agentic_purchase as svc
    from db.database import database

    with pytest.raises(svc.PurchaseRefused) as exc:
        await _start(row=_row(currency="KWD"))
    assert exc.value.reason == "currency_unsupported"
    count = await database.fetch_one("SELECT COUNT(*) AS n FROM reap_agentic_purchases")
    assert int(count["n"]) == 0


async def test_the_error_code_column_is_lower_case_and_the_others_are_verbatim(reap):
    """`last_error_code` is one VARCHAR(64) fed by three vocabularies that disagree on case, so
    it is folded at the single place that writes it. `reap_status` and `card_network` are NOT:
    those columns record what the partner said and what a buyer is shown."""
    import db.reap_agentic_ledger as ledger

    purchase_id = await _start()
    await _step(purchase_id)
    await _step(purchase_id)
    enrollment = await ledger.get_active_enrollment("bref_alice")
    assert enrollment["reap_status"] == "ACTIVE"
    assert enrollment["card_network"] == "VISA"

    reap.request_quote = _ok(dict(QUOTE_200, expiresAt="2020-01-01T00:00:00Z"))
    result = await _step(purchase_id)
    assert result.last_error_code == "quote_expired"


# ── 6. the #2206 adoption, on the dialect that decides it ────────────────────────────────────


async def test_the_hint_columns_are_jsonb_and_come_back_as_text_until_the_ledger_decodes(reap):
    """TWO ASSERTIONS, AND THE FIRST IS THE POINT. asyncpg hands back jsonb VERBATIM as a `str`
    for a raw statement — no SQLAlchemy result processor runs — so the decoded assertion alone
    would pass on a driver that never had the problem. These are columns THIS package writes at
    create and reads at advance, across two processes, so the round trip is the whole feature."""
    from db.database import database

    purchase_id = await _start(
        row=_row(
            accept_variant_labels=("Flamingo Flirt - Cream",),
            also_accept_domains=("Shop.Brand.Example",),
            market_country="us",
        )
    )
    raw = await database.fetch_one(
        "SELECT accept_variant_labels, also_accept_domains, market_country "
        "FROM reap_agentic_purchases WHERE id = :i",
        {"i": purchase_id},
    )
    assert isinstance(raw["accept_variant_labels"], str), "jsonb arrives as text on this driver"
    assert json.loads(raw["accept_variant_labels"]) == ["Flamingo Flirt - Cream"]
    assert json.loads(raw["also_accept_domains"]) == ["shop.brand.example"]
    assert raw["market_country"] == "US"

    row = await _get(purchase_id)
    assert row["accept_variant_labels"] == ["Flamingo Flirt - Cream"]
    assert row["also_accept_domains"] == ["shop.brand.example"]


async def test_the_hints_reach_the_resolver_across_the_process_boundary(reap):
    """The point of the columns: `advance` runs in a DIFFERENT PROCESS from `start_purchase`, so
    anything not in a column is gone by the time the resolve happens. Asserted at the resolver's
    own call, because a value persisted and then not passed on is the same outcome as one never
    persisted."""
    purchase_id = await _start(
        row=_row(accept_variant_labels=("Flamingo Flirt - Cream",),
                 also_accept_domains=("shop.brand.example",), market_country="gb")
    )
    await _step(purchase_id)
    sent = reap.named("resolve_our_row")[0]
    assert sent["accept_variant_labels"] == ("Flamingo Flirt - Cream",)
    assert sent["also_accept_domains"] == ("shop.brand.example",)
    assert sent["country"] == "GB"


async def test_the_hints_survive_a_terminal_transition_but_the_pii_does_not(reap):
    """NOT PII, SO NOT NULLED — catalog assertions about products and hostnames that name nobody.
    The control is in the same test: `buyer_email` and `shipping_address` DID go, so this is
    about which columns the terminal write clears rather than about it not running."""
    import services.reap_agentic_client as rc

    reap.resolve_our_row = rc.VariantResolution(ok=False, reason="search:merchant_not_in_results")
    purchase_id = await _start(
        row=_row(accept_variant_labels=("Flamingo Flirt - Cream",), market_country="US")
    )
    await _step(purchase_id)
    row = await _get(purchase_id)
    assert row["state"] == "refused"
    assert row["buyer_email"] is None and row["shipping_address"] is None
    assert row["accept_variant_labels"] == ["Flamingo Flirt - Cream"]
    assert row["market_country"] == "US"


async def test_a_transport_failure_records_its_reason_on_the_row(reap):
    """`release_claim` used to take `next_poll_at` and nothing else, and there are no self-edges,
    so a stalled row showed a future poll time and NO CAUSE. The ledger validates the code and
    REFUSES rather than folding, so an unfolded `transport_error:ReadTimeout` would raise a
    ValueError out of `advance` on every transport release."""
    import db.reap_agentic_ledger as ledger

    reap.resolve_our_row = _transport_resolution()
    purchase_id = await _start()
    result = await _step(purchase_id)

    assert result.outcome == "released"
    row = await _get(purchase_id)
    assert row["state"] == "resolving", "still not a transition"
    assert row["last_error_code"] == "transport_error:readtimeout"
    assert ledger._ERROR_CODE_RE.match(row["last_error_code"])


async def test_a_clean_completion_clears_a_stale_code(reap, attribution):
    """THE TERMINAL WRITE NO LONGER COALESCES `last_error_code`, and this is the behaviour that
    rests on it: a purchase that hit a transport failure, retried and completed cleanly must not
    carry `transport_error:readtimeout` for ever. A terminal row's code is read as "why this
    ended"."""
    reap.resolve_our_row = [_transport_resolution(), _resolved()]
    await _active_enrollment()
    purchase_id = await _start()

    assert (await _step(purchase_id)).outcome == "released"
    assert (await _get(purchase_id))["last_error_code"] == "transport_error:readtimeout"
    for _ in range(3):
        await _step(purchase_id)

    row = await _get(purchase_id)
    assert row["state"] == "completed"
    assert row["last_error_code"] is None
    assert len(attribution.calls) == 1


async def test_polling_an_enrollment_writes_nothing(reap):
    """It used to be an UPSERT — unfenced, bumping `updated_at` on every poll, and capable of
    minting a stray pending row. `get_enrollment_internal` is a read."""
    from db.database import database

    reap.get_enrollment = _ok(ENROLLMENT_CREATED)  # stays pending
    purchase_id = await _start()
    await _step(purchase_id)
    before = await database.fetch_all("SELECT * FROM reap_agentic_enrollments")
    assert len(before) == 1

    for _ in range(3):
        assert (await _step(purchase_id)).outcome == "released"

    after = await database.fetch_all("SELECT * FROM reap_agentic_enrollments")
    assert len(after) == 1
    assert after[0]["updated_at"] == before[0]["updated_at"]


# ── the LIVE quote shape (2026-09-18), on the production engine ──────────────────────────────


async def test_the_happy_path_completes_on_the_live_quote_shape_on_postgres(reap, attribution):
    """No `items`, nested int tax, four priced shipping options, floats — what Reap sends. Before
    the optional-but-strict fix this refused as `quote_items_mismatch`, every time."""
    reap.request_quote = _ok(LIVE_QUOTE)
    purchase_id = await _drive_to_completed()
    row = await _get(purchase_id)
    assert row["state"] == "completed" and row["last_error_code"] is None
    assert (row["quoted_total_minor"], row["shipping_minor"], row["tax_minor"]) == (4500, 250, 0)
    assert len(attribution.calls) == 1


@pytest.mark.parametrize("items", [None, []], ids=["null", "empty"])
async def test_a_present_but_empty_items_still_refuses_on_postgres(reap, attribution, items):
    reap.request_quote = _ok(dict(LIVE_QUOTE, items=items))
    purchase_id = await _drive_to_completed()
    row = await _get(purchase_id)
    assert row["state"] == "refused" and row["last_error_code"] == "quote_items_mismatch"


# ── mig 233: the service's own consent gate, on the production dialect ───────────────────────


async def _purchase_count() -> int:
    from db.database import database

    return int(await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_purchases"))


async def test_a_purchase_opened_through_this_service_carries_its_consent_on_postgres():
    import db.reap_agentic_ledger as ledger

    purchase_id = await _start()
    row = await ledger.get_purchase_internal(purchase_id)
    assert row["consent_version"] == CONSENT
    # A real `timestamptz`, aware, not a string that looks like one.
    assert row["consented_at"] is not None and row["consented_at"].tzinfo is not None


async def test_the_variant_lane_refuses_without_a_consent_on_postgres():
    """THE SERVICE'S OWN GATE, not the route's. Both exist, and only a test that calls
    `start_purchase` DIRECTLY can see this one — the route validates first, so every route-level
    test stays green with the service's check removed. That is not hypothetical: the mutant that
    removes it survived this gate until this test existed.
    """
    import services.reap_agentic_purchase as svc

    before = await _purchase_count()
    with pytest.raises(svc.PurchaseRefused) as caught:
        await _start(consent_version=None)
    assert caught.value.reason == "consent_required"
    assert await _purchase_count() == before, "a refused consent left a row behind"


@pytest.mark.parametrize(
    "value",
    [
        "", "   ", "v" * 33, "v1" + chr(0x0000),
        # NOT A STRING. This one is not decoration: the service used to do
        # `str(consent_version or "")`, so `123` became the consent tag "123" — a consent
        # nobody gave, stored on a purchase row as evidence. It now goes through the shared
        # validator, which refuses a non-str outright.
        123, True, 1.5, {}, [],
    ],
)
async def test_a_malformed_consent_is_refused_at_the_service_on_postgres(value):
    """The NUL case is the one only this dialect can judge: reaching an asyncpg bind it is an
    untranslatable-character error naming a parameter index, i.e. a 500. Refused first, so both
    dialects answer `consent_required` and the answer names the field."""
    import services.reap_agentic_purchase as svc

    before = await _purchase_count()
    with pytest.raises(svc.PurchaseRefused) as caught:
        await _start(consent_version=value)
    assert caught.value.reason == "consent_required"
    assert await _purchase_count() == before


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


# ── 2026-09-30: a returning buyer whose card IS active at Reap could never buy (Postgres) ─────
#
# The SQLite arm (tests/test_reap_agentic_purchase.py) walks every branch. Here the same staging
# sequence runs against the real engine, because every decision in it is a CLOCK comparison —
# the sweep's grace in SQL, the ledger's expiry flag in SQL, the service's link check in Python
# against a timestamptz read back through asyncpg — and a clock that is off by a timezone turns
# "9 s past" into "hours past" without any other symptom.

STAGING_ENROLLMENT = "9041ef1a-1377-45f6-b09a-95d5eb07f908"
STAGING_LINK = "https://pay.prava.space/enroll/ses_01M3S0RSP39FEXTT7ZRYHSRDJQ"


def _iso_in(seconds: float) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace(
        "+00:00", "Z"
    )


def _requires_action(reap_id=STAGING_ENROLLMENT, url=STAGING_LINK, expires_in=900):
    return {
        "id": reap_id,
        "status": "REQUIRES_ACTION",
        "nextAction": {"type": "REDIRECT", "url": url, "expiresAt": _iso_in(expires_in)},
    }


def _active(reap_id=STAGING_ENROLLMENT):
    return {
        "id": reap_id,
        "status": "ACTIVE",
        "paymentMethod": {"type": "CARD", "network": "VISA", "last4": "7847"},
        "nextAction": None,
    }


async def _age_links(seconds_past: int) -> None:
    for table in ("reap_agentic_enrollments", "reap_agentic_purchases"):
        await _raw(
            f"UPDATE {table} SET hosted_url_expires_at = "
            f"clock_timestamp() - ({int(seconds_past)} * INTERVAL '1 second') "
            "WHERE hosted_url_expires_at IS NOT NULL",
            {},
        )


async def _a_waiting_on_a_link(reap):
    reap.create_enrollment = _ok(_requires_action())
    reap.get_enrollment = _ok(_requires_action())
    purchase_a = await _start()
    assert (await _step(purchase_a)).state == "needs_enrollment"
    ours = (await _get(purchase_a))["enrollment_id"]
    assert (await _step(purchase_a)).last_error_code == "enrollment_pending"
    return purchase_a, ours


async def test_the_staging_sequence_a_returning_buyer_with_an_active_card_now_buys_on_postgres(
    reap,
):
    import db.reap_agentic_ledger as ledger

    purchase_a, ours = await _a_waiting_on_a_link(reap)
    reap.get_enrollment = _ok(_active())
    await _age_links(28)
    # Staging's sweep: Reap's exact expiry. A goes; our row stays 'pending'.
    assert await ledger.expire_overdue_purchases(enrollment_grace_seconds=0) == []
    assert (await ledger.get_pending_enrollment("bref_alice"))["id"] == ours

    reap.calls.clear()
    purchase_b = await _start()
    assert (await _step(purchase_b)).state == "quoting"
    assert reap.named("create_enrollment") == []
    b = await _get(purchase_b)
    assert b["enrollment_id"] == ours
    assert b["hosted_url"] is None and b["hosted_url_expires_at"] is None
    active = await ledger.get_active_enrollment("bref_alice")
    assert (active["id"], active["card_last4"]) == (ours, "7847")

    purchase_c = await _start()
    assert (await _step(purchase_c)).state == "quoting"
    assert reap.named("create_enrollment") == []


async def test_active_proceeds_and_pending_survives_local_grace_on_postgres(reap):
    import db.reap_agentic_ledger as ledger

    purchase_a, ours = await _a_waiting_on_a_link(reap)
    reap.get_enrollment = _ok(_active())
    await _age_links(28)
    assert await ledger.expire_overdue_purchases() == []
    assert (await _step(purchase_a)).state == "quoting"
    a = await _get(purchase_a)
    assert a["hosted_url"] is None and a["hosted_url_expires_at"] is None

    stale, stale_enrollment = await _a_waiting_on_a_link_for("bref_bob", reap)
    await _age_links(181)
    reap.calls.clear()
    assert stale not in await ledger.expire_overdue_purchases()
    assert (await _get(stale))["state"] == "needs_enrollment"
    assert (await _get(stale))["enrollment_id"] == stale_enrollment
    assert (await ledger.get_enrollment_internal(stale_enrollment))["status"] == "pending"
    assert reap.calls == []

    # Only the provider's authoritative terminal status retires the bound enrollment.
    reap.get_enrollment = _ok({
        **_requires_action("77777777-7777-7777-7777-777777777777"), "status": "EXPIRED"
    })
    result = await _step(stale)
    assert result.state == "failed" and result.last_error_code == "enrollment_dead"
    assert (await _get(stale))["enrollment_id"] == stale_enrollment
    assert (await ledger.get_enrollment_internal(stale_enrollment))["status"] == "dead"
    assert reap.sequence() == ["get_enrollment"]


async def _a_waiting_on_a_link_for(buyer_ref, reap):
    reap.create_enrollment = _ok(_requires_action("77777777-7777-7777-7777-777777777777"))
    reap.get_enrollment = _ok(_requires_action("77777777-7777-7777-7777-777777777777"))
    purchase = await _start(buyer_ref=buyer_ref)
    assert (await _step(purchase)).state == "needs_enrollment"
    return purchase, (await _get(purchase))["enrollment_id"]


async def test_reuse_hold_and_fresh_attempt_on_postgres(reap):
    """Live link is reused; locally expired links stay held while enrollment is pending."""
    import db.reap_agentic_ledger as ledger

    _, old = await _a_waiting_on_a_link(reap)
    # Reap's read ECHOES the session it created (link + expiry): a fresh `nextAction` would now be
    # used over the stored link, so a fixed "+15 min" answer would make every read look live.
    reap.get_enrollment = _echo_session()

    reap.calls.clear()
    reused = await _start()
    assert (await _step(reused)).state == "needs_enrollment"
    assert (await _get(reused))["enrollment_id"] == old
    assert reap.named("create_enrollment") == []

    await _age_links(9)
    held = await _start()
    result = await _step(held)
    assert (result.state, result.last_error_code) == ("resolving", "enrollment_settling")
    assert reap.named("create_enrollment") == []

    await _age_links(400)
    fresh = await _start()
    assert (await _step(fresh)).last_error_code == "enrollment_settling"
    assert reap.named("create_enrollment") == []
    assert (await ledger.get_enrollment_internal(old))["status"] == "pending"
    assert (await _get(fresh))["hosted_url"] is None


# ── review of #2483 at 93f585c26, through `advance` on Postgres ─────────────────────────────


def _echo_session():
    """REQUIRES_ACTION echoing the stored session — see the SQLite arm's
    `_reap_echoes_the_session`."""

    async def _read(**kwargs):
        from db.database import database
        import db.reap_agentic_ledger as ledger

        stored = await database.fetch_one(
            "SELECT hosted_url, hosted_url_expires_at FROM reap_agentic_enrollments "
            "WHERE reap_enrollment_id = :r", {"r": kwargs["id"]},
        )
        action = None
        if stored is not None and stored["hosted_url"]:
            action = {"type": "REDIRECT", "url": stored["hosted_url"]}
            expires = ledger._decode_dt(stored["hosted_url_expires_at"])
            if expires is not None:
                action["expiresAt"] = expires.isoformat().replace("+00:00", "Z")
        return _ok({"id": kwargs["id"], "status": "REQUIRES_ACTION", "nextAction": action})

    return _read


async def test_r3_a_create_returning_an_id_we_hold_on_a_dead_row_fails_by_name_on_postgres(reap):
    """P1-1 on the engine whose unique-violation shape is asyncpg's."""
    import db.reap_agentic_ledger as ledger

    _, old = await _a_waiting_on_a_link(reap)
    reap.get_enrollment = _echo_session()
    await ledger.mark_enrollment_dead(old, reap_status="EXPIRED")
    for _ in range(2):
        reap.calls.clear()
        purchase = await _start()
        result = await _step(purchase)
        assert (result.state, result.last_error_code) == ("failed", "enrollment_id_conflict")
        [created] = reap.named("create_enrollment")
        assert (await ledger.get_enrollment_internal(created["attempt_id"]))["status"] == "dead"
    assert (await ledger.get_enrollment_internal(old))["status"] == "dead"


async def test_r1_two_purchases_of_one_buyer_share_one_attempt_on_postgres(reap, monkeypatch):
    """P2-1 on Postgres: purchase 2 runs its whole step between purchase 1's read and INSERT;
    migration 252's index refuses 1's row and both wait on ONE attempt."""
    import uuid as _uuid

    import db.reap_agentic_ledger as ledger

    _, old = await _a_waiting_on_a_link(reap)
    reap.get_enrollment = _echo_session()
    await ledger.mark_enrollment_dead(old, reap_status="EXPIRED")

    def _create(**kwargs):
        attempt = str(kwargs["attempt_id"])
        return _ok(_requires_action(
            str(_uuid.uuid5(_uuid.NAMESPACE_OID, attempt)),
            f"https://pay.prava.space/enroll/{attempt}",
        ))

    reap.create_enrollment = _create
    reap.calls.clear()
    first, second = await _start(), await _start()
    real = ledger.get_pending_enrollments
    calls = {"n": 0}

    async def _interleaved(buyer_ref):
        rows = await real(buyer_ref)
        calls["n"] += 1
        if calls["n"] == 2:
            await _step(second, "w2")
        return rows

    monkeypatch.setattr(ledger, "get_pending_enrollments", _interleaved)
    assert (await _step(first, "w1")).state == "needs_enrollment"
    monkeypatch.setattr(ledger, "get_pending_enrollments", real)
    a, b = await _get(first), await _get(second)
    assert b["state"] == "needs_enrollment"
    assert a["enrollment_id"] == b["enrollment_id"]
    assert len(await ledger.get_pending_enrollments("bref_alice")) == 1
    assert {c["attempt_id"] for c in reap.named("create_enrollment")} == {a["enrollment_id"]}


async def test_r2_and_the_settling_exemption_on_postgres(reap):
    """P2-2: a linked row with no expiry, 7200 s old, is retired, not reused. P2-3: a settling
    re-check neither resolves again nor counts an attempt."""
    import db.reap_agentic_ledger as ledger

    _, old = await _a_waiting_on_a_link(reap)
    reap.get_enrollment = _echo_session()
    await _raw(
        "UPDATE reap_agentic_enrollments SET hosted_url_expires_at = NULL, "
        "created_at = clock_timestamp() - INTERVAL '7200 seconds' WHERE id = :i", {"i": old},
    )
    reap.create_enrollment = _ok(_requires_action(
        "13131313-1313-1313-1313-131313131313", "https://pay.prava.space/enroll/FRESH"
    ))
    fresh = await _start()
    assert (await _step(fresh)).last_error_code == "enrollment_settling"
    assert (await _get(fresh))["hosted_url"] is None
    assert (await ledger.get_enrollment_internal(old))["status"] == "pending"

    await _age_links(9)                     # FRESH's link just died: the next purchase holds
    held = await _start()
    assert (await _step(held)).last_error_code == "enrollment_settling"
    await _raw(
        "UPDATE reap_agentic_purchases SET next_poll_at = clock_timestamp() - INTERVAL '1 second' "
        "WHERE id = :i", {"i": held},
    )
    before = (await _get(held))["attempts"]
    claimed = [r for r in await ledger.claim_due_purchases("w9") if r["id"] == held]
    assert claimed and claimed[0]["attempts"] == before
    reap.calls.clear()
    import services.reap_agentic_purchase as svc

    assert (await svc.advance(held, "w9")).last_error_code == "enrollment_settling"
    assert reap.named("resolve_our_row") == []


async def test_two_purchases_creating_one_attempt_concurrently_on_postgres(reap):
    """Round-2 P2-A on the real engine: two purchases of one first-time buyer, stepped at once
    on two connections; the second create of the SAME attempt gets Reap's 409 in-progress and is
    RELEASED; its next poll reuses the winner's session. One row, one enrollment, one link."""
    import asyncio

    import db.reap_agentic_ledger as ledger
    import services.reap_agentic_client as rc
    import services.reap_agentic_purchase as svc

    calls, second_arrived = [], asyncio.Event()

    async def _create(**kwargs):
        calls.append(kwargs["attempt_id"])
        if len(calls) == 1:
            await asyncio.wait_for(second_arrived.wait(), timeout=60)
            return _ok(_requires_action())
        second_arrived.set()
        return rc.ReapResponse(ok=False, status=409, error="reap_status_409",
                               error_code="IDEMPOTENCY_REQUEST_IN_PROGRESS")

    reap.create_enrollment = _create
    reap.get_enrollment = _echo_session()
    first, second = await _start(), await _start()
    await _claim(first, "w1")
    await _claim(second, "w2")
    results = await asyncio.gather(svc.advance(first, "w1"), svc.advance(second, "w2"))

    assert len(calls) == 2 and len(set(calls)) == 1
    assert sorted(r.outcome for r in results) == ["advanced", "released"]
    loser = next(pid for pid, r in zip((first, second), results) if r.outcome == "released")
    assert (await _get(loser))["last_error_code"] == "idempotency_request_in_progress"
    assert (await _step(loser, "w3")).state == "needs_enrollment"
    a, b = await _get(first), await _get(second)
    assert a["enrollment_id"] == b["enrollment_id"] == calls[0]
    assert a["hosted_url"] == b["hosted_url"] == STAGING_LINK
    assert len(await ledger.get_pending_enrollments("bref_alice")) == 1


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

@pytest.mark.parametrize('state', ['resolving', 'quoting'])
async def test_contact_paused_precheckout_is_rechecked_every_15_minutes_not_every_minute(reap, state):
    """A contact-paused row changes only when its owner resumes (which makes it due at once) or
    the re-entry window lapses it, so it must not take a claim slot from live work every 60 s."""
    from db.database import database
    import db.reap_agentic_ledger as ledger
    import services.reap_agentic_purchase as svc
    pid = await _start()
    await database.execute("UPDATE reap_agentic_purchases SET state=:s,buyer_email=NULL,shipping_address=NULL,"
                           "last_error_code='contact_retention_elapsed' WHERE id=:id", {'s': state, 'id': pid})
    await _claim(pid, 'paused-backoff')
    reap.calls.clear()
    result = await svc.advance(pid, 'paused-backoff')
    assert result.outcome == 'released' and result.last_error_code == 'contact_retention_elapsed'
    assert result.next_poll_in_seconds == svc.CONTACT_PAUSED_RECHECK_SECONDS == 900
    assert reap.calls == []
    assert [r for r in await ledger.claim_due_purchases('paused-next', limit=10) if r['id'] == pid] == []

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
    assert reap.sequence() == ["get_enrollment"]
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

async def test_manual_concurrent_decisions_have_one_audit_and_one_completion(reap,manual_attribution):
    attribution = manual_attribution
    import asyncio, contextvars
    from db.database import database
    from services.reap_checkout_recovery import resolve_checkout_manually,ManualResolutionRefused
    pid,row,evidence=await _manual_case();attribution.calls.clear();reap.calls.clear()
    async def attempt():
        try:
            return await resolve_checkout_manually(pid,evidence=evidence,operator_ref='operator_test',expected_updated_at=row['updated_at'],dry_run=False)
        except ManualResolutionRefused as exc:
            return str(exc)
    tasks=[contextvars.Context().run(asyncio.create_task,attempt()) for _ in range(2)]
    results=await asyncio.wait_for(asyncio.gather(*tasks),5)
    assert sum(isinstance(r,dict) and r['status']=='resolved' for r in results)==1
    assert (await _get(pid))['state']=='completed'
    assert await database.fetch_val('SELECT count(*) FROM reap_checkout_manual_resolution_audit')==1
    assert len(attribution.calls)==1 and reap.calls==[]

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
from db.database import database
import services.reap_agentic_purchase as svc
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

from datetime import timedelta
import services.reap_agentic_client as rc


@pytest.mark.parametrize('legacy', [False, True])
async def test_deadline_all_owner_reads_share_original_clock_and_redaction(reap,monkeypatch,legacy):
    from types import SimpleNamespace
    from starlette.requests import Request
    from db.database import database
    import db.reap_agentic_ledger as ledger
    import routes.agent_commerce_reap as route
    pid=await _deadline_missing_provider_expiry(reap)
    if legacy:await database.execute('UPDATE reap_agentic_purchases SET hosted_url_expires_at=NULL WHERE id=:id',{'id':pid})
    before=await _get(pid)
    enrollment=await ledger.get_enrollment_internal(before['enrollment_id'])
    expected=enrollment['created_at']+timedelta(seconds=ledger.HOSTED_SESSION_SECONDS)
    monkeypatch.setattr(route,'_require_rail',lambda:None)
    monkeypatch.setattr(route,'_require_agent_user',lambda _: 'synthetic-user')
    monkeypatch.setattr(route,'hash_agent_user_ref',lambda _: 'hash_alice')
    request=Request({'type':'http','method':'GET','path':'/purchases','query_string':b''})
    for _ in range(2):
        listed=await route.list_reap_purchases(request,SimpleNamespace(agent_id='agent_one'),None)
        owner=await route._owner_view(purchase_id=pid,agent_id='agent_one',agent_user_ref_hash='hash_alice')
        assert listed['purchases'][0]==owner
        assert owner['hosted_url_expires_at']==expected and owner['hosted_url']
        for key in ['buyer_ref','enrollment_id','buyer_email','shipping_address','hosted_url_expiry_invalid']:assert key not in owner
    assert await _get(pid)==before
    assert await ledger.get_enrollment_internal(enrollment['id'])==enrollment

@pytest.mark.parametrize('defect',['superseded','returned_id','wrong_buyer'])
async def test_deadline_recording_named_attempt_never_mints_a_new_clock(reap,monkeypatch,defect):
    import copy
    from db.database import database
    import db.reap_agentic_ledger as ledger
    payload=copy.deepcopy(ENROLLMENT_CREATED);payload['nextAction'].pop('expiresAt');reap.create_enrollment=_ok(payload)
    actual=ledger.upsert_pending_enrollment
    seen=[]
    async def changed(**kwargs):
        if kwargs.get('reap_enrollment_id'):
            seen.append(kwargs['enrollment_id'])
            if defect=='superseded':await database.execute("UPDATE reap_agentic_enrollments SET status='dead' WHERE id=:id",{'id':kwargs['enrollment_id']})
            if defect=='wrong_buyer':kwargs['buyer_ref']='foreign_buyer'
        result=await actual(**kwargs)
        if kwargs.get('reap_enrollment_id') and defect=='returned_id':result=dict(result,id='another_attempt')
        return result
    monkeypatch.setattr(ledger,'upsert_pending_enrollment',changed)
    pid=await _start();result=await _step(pid)
    assert result.outcome=='released' and result.state=='resolving'
    assert not (await _get(pid))['hosted_url'] and (await _get(pid))['hosted_url_expires_at'] is None
    rows=await database.fetch_all('SELECT * FROM reap_agentic_enrollments')
    assert len(rows)==1 and rows[0]['id']==seen[0]
    assert rows[0]['buyer_ref']=='bref_alice'

@pytest.mark.parametrize('later',['malformed','omitted','missing404'])
async def test_deadline_invalid_provenance_survives_age_and_new_purchase_without_remint(reap,later):
    import copy
    from db.database import database,IS_POSTGRES
    import db.reap_agentic_ledger as ledger
    bad=copy.deepcopy(ENROLLMENT_CREATED);bad['nextAction']['expiresAt']='malformed'
    reap.create_enrollment=_ok(bad)
    first=await _start();result=await _step(first)
    assert result.outcome=='released' and result.last_error_code=='enrollment_deadline_invalid'
    attempt=(await ledger.get_pending_enrollments('bref_alice'))[0]
    assert attempt['hosted_url_expiry_invalid']
    age="clock_timestamp()-INTERVAL '1 day'" if IS_POSTGRES else "datetime('now','-1 day')"
    await database.execute(f'UPDATE reap_agentic_enrollments SET created_at={age} WHERE id=:id',{'id':attempt['id']})
    reply=copy.deepcopy(bad)
    if later=='omitted':reply['nextAction'].pop('expiresAt')
    reap.get_enrollment=rc.ReapResponse(ok=False,status=404,error='reap_status_404') if later=='missing404' else _ok(reply)
    second=await _start()
    for pid in (first,second,second):
        result=await _step(pid)
        assert result.outcome=='released' and result.state=='resolving'
        assert not (await _get(pid))['hosted_url']
    assert len(reap.named('create_enrollment'))==1
    assert await database.fetch_val("SELECT count(*) FROM reap_agentic_enrollments WHERE status='pending'")==1
    assert await database.fetch_val("SELECT count(*) FROM reap_agentic_enrollments WHERE status='dead'")==0

@pytest.mark.parametrize('later',['valid_expiry','active'])
async def test_deadline_authoritative_response_can_resolve_retained_invalid_attempt(reap,later):
    import copy
    import db.reap_agentic_ledger as ledger
    bad=copy.deepcopy(ENROLLMENT_CREATED);bad['nextAction']['expiresAt']='malformed';reap.create_enrollment=_ok(bad)
    pid=await _start();await _step(pid)
    attempt=(await ledger.get_pending_enrollments('bref_alice'))[0]
    reap.get_enrollment=_ok(ENROLLMENT_ACTIVE if later=='active' else ENROLLMENT_CREATED)
    result=await _step(pid)
    assert result.state==('quoting' if later=='active' else 'needs_enrollment')
    recorded=await ledger.get_enrollment_internal(attempt['id'])
    assert not recorded['hosted_url_expiry_invalid'] and recorded['created_at']==attempt['created_at']
    assert len(reap.named('create_enrollment'))==1

async def test_deadline_malformed_read_suppresses_previously_valid_link_and_preserves_attempt(reap):
    import copy
    import db.reap_agentic_ledger as ledger
    import routes.agent_commerce_reap as route
    pid=await _deadline_missing_provider_expiry(reap);before=await _get(pid)
    bad=copy.deepcopy(ENROLLMENT_CREATED);bad['nextAction']['expiresAt']='malformed';reap.get_enrollment=_ok(bad)
    result=await _step(pid)
    assert result.last_error_code=='enrollment_deadline_invalid'
    public=await route._owner_view(purchase_id=pid,agent_id='agent_one',agent_user_ref_hash='hash_alice')
    assert not public.get('hosted_url') and not public.get('hosted_url_expires_at')
    recorded=await ledger.get_enrollment_internal(before['enrollment_id'])
    assert recorded['status']=='pending' and recorded['hosted_url_expiry_invalid']
    assert (await _get(pid))['hosted_url_expires_at']==before['hosted_url_expires_at']

async def test_deadline_allowlisted_authoritative_refresh_is_visible_to_existing_purchase(reap):
    import copy
    import db.reap_agentic_ledger as ledger
    import routes.agent_commerce_reap as route
    pid=await _deadline_missing_provider_expiry(reap);before=await _get(pid)
    refreshed=copy.deepcopy(ENROLLMENT_CREATED);refreshed['nextAction']['url']='https://pay.prava.space/enroll/session_refreshed'
    reap.get_enrollment=_ok(refreshed)
    assert (await _step(pid)).outcome=='released'
    public=await route._owner_view(purchase_id=pid,agent_id='agent_one',agent_user_ref_hash='hash_alice')
    assert public['hosted_url']==refreshed['nextAction']['url'] and public['hosted_url_expires_at'].year==2099
    row=await _get(pid);enrollment=await ledger.get_enrollment_internal(row['enrollment_id'])
    assert row['enrollment_id']==before['enrollment_id'] and row['state_entered_at']==before['state_entered_at']
    assert enrollment['created_at']<=before['created_at']+timedelta(seconds=1)

async def test_deadline_expiry_provenance_selfheal_and_migration_physical_parity(reap):
    from pathlib import Path
    from db.database import database,IS_POSTGRES
    from db.sql_migrations import split_statements
    from db.schema_guard import ensure_required_schema_light
    async def column():
        if IS_POSTGRES:
            rows=await database.fetch_all("SELECT column_name,data_type,is_nullable,column_default FROM information_schema.columns WHERE table_schema='public' AND table_name='reap_agentic_enrollments' AND column_name='hosted_url_expiry_invalid'")
        else:
            rows=[x for x in await database.fetch_all('PRAGMA table_info(reap_agentic_enrollments)') if x['name']=='hosted_url_expiry_invalid']
        return [dict(x) for x in rows]
    await database.execute('ALTER TABLE reap_agentic_enrollments DROP COLUMN hosted_url_expiry_invalid')
    migration=(Path(__file__).parent.parent/'db/migrations/254_reap_enrollment_expiry_provenance.sql').read_text()
    if not IS_POSTGRES:migration=migration.replace('ALTER TABLE IF EXISTS','ALTER TABLE').replace('ADD COLUMN IF NOT EXISTS','ADD COLUMN')
    for statement in split_statements(migration):await database.execute(statement)
    migrated=await column();assert migrated
    await database.execute('ALTER TABLE reap_agentic_enrollments DROP COLUMN hosted_url_expiry_invalid')
    await ensure_required_schema_light();assert await column()==migrated
    await ensure_required_schema_light();assert await column()==migrated


async def test_deadline_refresh_losing_lease_preserves_next_holders_recoverable_action(reap,monkeypatch):
    import copy
    from db.database import database
    import db.reap_agentic_ledger as ledger
    import routes.agent_commerce_reap as route
    pid=await _deadline_missing_provider_expiry(reap)
    fresh=copy.deepcopy(ENROLLMENT_CREATED);fresh['nextAction']['url']='https://pay.prava.space/enroll/lease_refreshed'
    reap.get_enrollment=_ok(fresh)
    original=ledger.record_enrollment_expiry_provenance
    async def lose_after_provenance(**kwargs):
        result=await original(**kwargs)
        await database.execute("UPDATE reap_agentic_purchases SET claimed_by='next_holder' WHERE id=:id",{'id':pid})
        return result
    monkeypatch.setattr(ledger,'record_enrollment_expiry_provenance',lose_after_provenance)
    assert (await _step(pid)).outcome=='lost_claim'
    assert (await _get(pid))['claimed_by']=='next_holder'
    public=await route._owner_view(purchase_id=pid,agent_id='agent_one',agent_user_ref_hash='hash_alice')
    assert not public.get('hosted_url')
    monkeypatch.setattr(ledger,'record_enrollment_expiry_provenance',original)
    assert (await svc.advance(pid,'next_holder')).outcome=='released'
    public=await route._owner_view(purchase_id=pid,agent_id='agent_one',agent_user_ref_hash='hash_alice')
    assert public['hosted_url']==fresh['nextAction']['url'] and public['hosted_url_expires_at'].year==2099
    assert len(reap.named('create_enrollment'))==1


# Bounded production pilot admission: durable query behavior and real quote totals.
def _bounded_pilot(**over):
    scope = {"agent_ids": ["agent_one"], "merchant_domains": ["brand.example"], "markets": ["US"],
             "product_keys": ["pk_1"], "quantities": [1], "variant_keys": ["vk_1"],
             "currency": "USD", "max_total_minor": 4500}
    scope.update(over)
    return scope


def test_production_pilot_requires_all_bounds_or_literal_optout(monkeypatch):
    import json
    import services.reap_agentic_purchase as svc
    monkeypatch.setenv("PIVOTA_ENV", "production")
    for value in [None, "", "UNRESTRICTED", " unrestricted", "null", json.dumps({
            k: v for k, v in _bounded_pilot().items() if k not in {"variant_keys", "currency", "max_total_minor"}})]:
        if value is None:
            monkeypatch.delenv("REAP_AGENTIC_PILOT_SCOPE", raising=False)
        else:
            monkeypatch.setenv("REAP_AGENTIC_PILOT_SCOPE", value)
        assert not svc.is_create_enabled()
    monkeypatch.setenv("REAP_AGENTIC_PILOT_SCOPE", "unrestricted")
    assert svc.is_create_enabled()
    monkeypatch.setenv("REAP_AGENTIC_PILOT_SCOPE", json.dumps(_bounded_pilot()))
    assert svc.is_create_enabled()


@pytest.mark.parametrize("quantity", [True, 1.0, "1"])
def test_create_quantity_strict_recovery_legacy_compatible(quantity):
    from pydantic import ValidationError
    import routes.agent_commerce_reap as route
    body = {"merchant_domain": "brand.example", "product_key": "pk_1", "quantity": quantity,
            "buyer": {"email": EMAIL, "shipping_address": ADDRESS}, "idempotency_key": "k-qty",
            "expected_unit_price_minor": 4250, "expected_currency": "USD"}
    with pytest.raises(ValidationError):
        route.StartPurchaseRequest.model_validate(body)
    assert route.RecoverPurchaseRequest.model_validate(body).quantity == 1
    # Create's ONLY objection is the quantity: the same body with an integer one validates.
    assert route.StartPurchaseRequest.model_validate({**body, "quantity": 1}).quantity == 1


@pytest.mark.parametrize("field,value", [("variant_keys", ["wrong"]), ("currency", "EUR"),
                                         ("max_total_minor", 4249)])
async def test_resolved_production_scope_refuses_before_insert(monkeypatch, field, value):
    import json
    import services.reap_agentic_purchase as svc
    monkeypatch.setenv("PIVOTA_ENV", "production")
    monkeypatch.setenv("REAP_AGENTIC_PILOT_SCOPE", json.dumps(_bounded_pilot(**{field: value})))
    with pytest.raises(svc.PurchaseRefused) as exc:
        await _start()
    assert exc.value.reason == "pilot_scope_refused"


@pytest.mark.parametrize("state", ["resolving", "quoting", "needs_enrollment"])
@pytest.mark.parametrize("settling", [False, True])
async def test_scope_paused_rows_never_claim_or_exhaust_after_52_ticks(monkeypatch, reap, state, settling):
    import json
    import db.reap_agentic_ledger as ledger
    from db.database import database
    import services.reap_agentic_purchase as svc
    purchase = await _start()
    await database.execute("UPDATE reap_agentic_purchases SET state=:state, attempts=51, last_error_code=:error WHERE id=:id",
                           {"id": purchase, "state": state, "error": "enrollment_settling" if settling else None})
    before = await ledger.get_purchase_internal(purchase)
    monkeypatch.setenv("PIVOTA_ENV", "production")
    monkeypatch.setenv("REAP_AGENTIC_PILOT_SCOPE", json.dumps(_bounded_pilot(agent_ids=["other-agent"])))
    for _ in range(52):
        assert await ledger.claim_due_purchases("scope-worker", pilot_scope=svc.pilot_admission_scope()) == []
        assert await ledger.fail_exhausted_purchases(50, pilot_scope=svc.pilot_admission_scope()) == []
    after = await ledger.get_purchase_internal(purchase)
    for name in ["state", "attempts", "state_entered_at", "last_error_code", "next_poll_at", "claimed_by"]:
        assert after[name] == before[name]
    assert reap.calls == []
    assert await ledger.count_precheckout_paused(pilot_scope=svc.pilot_admission_scope()) == 1
    assert await ledger.count_stuck_purchases(stuck_after_seconds=60, pilot_scope=svc.pilot_admission_scope()) == 0


@pytest.mark.parametrize("state", ["resolving", "quoting", "needs_enrollment"])
@pytest.mark.parametrize("settling", [False, True])
async def test_scope_change_after_claim_refunds_only_unadvanced_claim(monkeypatch, reap, state, settling):
    import json
    import db.reap_agentic_ledger as ledger
    from db.database import database
    import services.reap_agentic_purchase as svc
    purchase = await _start()
    await database.execute("UPDATE reap_agentic_purchases SET state=:state, attempts=7, last_error_code=:error WHERE id=:id",
                           {"id": purchase, "state": state, "error": "enrollment_settling" if settling else None})
    before = await ledger.get_purchase_internal(purchase)
    rows = await ledger.claim_due_purchases("scope-worker")
    assert len(rows) == 1
    monkeypatch.setenv("PIVOTA_ENV", "production")
    monkeypatch.setenv("REAP_AGENTIC_PILOT_SCOPE", json.dumps(_bounded_pilot(agent_ids=["other-agent"])))
    result = await svc.advance(purchase, "scope-worker")
    assert result.outcome == "released"
    after = await ledger.get_purchase_internal(purchase)
    assert after["claimed_by"] is None
    for name in ["state", "attempts", "state_entered_at", "last_error_code"]:
        assert after[name] == before[name]
    from datetime import datetime, timedelta, timezone
    # The paused row goes to the back of the claim order for one poll interval instead of
    # staying at its old, already-due next_poll_at.
    interval = timedelta(seconds=svc.POLL_INTERVALS[state])
    assert before["next_poll_at"] + interval - timedelta(seconds=2) <= after["next_poll_at"]
    assert after["next_poll_at"] <= datetime.now(timezone.utc) + interval + timedelta(seconds=2)
    assert await ledger.claim_due_purchases("scope-worker-2") == []
    assert reap.calls == []


@pytest.mark.parametrize("cap,allowed", [(4250, False), (4499, False), (4500, True)])
async def test_quote_tax_shipping_authoritative_total_bounded_before_checkout(monkeypatch, reap, cap, allowed):
    import json
    import services.reap_agentic_purchase as svc
    from db.database import database
    monkeypatch.setenv("PIVOTA_ENV", "production")
    monkeypatch.setenv("REAP_AGENTIC_PILOT_SCOPE", json.dumps(_bounded_pilot(max_total_minor=cap)))
    purchase = await _start()
    await _active_enrollment()
    await database.execute("UPDATE reap_agentic_purchases SET state='quoting' WHERE id=:id", {"id": purchase})
    result = await _step(purchase)
    assert bool(reap.named("create_checkout")) is allowed
    assert result.state == ("awaiting_approval" if allowed else "refused")
    if not allowed:
        assert result.refusal_reason == "pilot_scope_refused"


async def test_pause_refund_cannot_touch_changed_attempt_or_foreign_holder():
    import db.reap_agentic_ledger as ledger
    from db.database import database
    purchase = await _start()
    rows = await ledger.claim_due_purchases("scope-worker")
    assert len(rows) == 1
    stale = rows[0]
    await database.execute("UPDATE reap_agentic_purchases SET attempts=attempts+1 WHERE id=:id", {"id": purchase})
    assert await ledger.release_paused_claim(stale, "scope-worker", next_poll_in_seconds=60) is None
    current = await ledger.get_purchase_internal(purchase)
    assert await ledger.release_paused_claim(current, "foreign-worker", next_poll_in_seconds=60) is None
    assert (await ledger.get_purchase_internal(purchase))["claimed_by"] == "scope-worker"




async def _deadline_missing_provider_expiry(reap):
    import copy
    payload = copy.deepcopy(ENROLLMENT_CREATED)
    payload["nextAction"].pop("expiresAt")
    reap.create_enrollment = _ok(payload)
    reap.get_enrollment = _ok(payload)
    purchase_id = await _start()
    assert (await _step(purchase_id)).state == "needs_enrollment"
    return purchase_id


async def test_enrollment_deadline_initial_reuse_and_repeated_owner_read_are_stable(reap):
    from datetime import timedelta
    import db.reap_agentic_ledger as ledger
    import routes.agent_commerce_reap as route
    purchase_id = await _deadline_missing_provider_expiry(reap)
    row = await _get(purchase_id)
    enrollment = await ledger.get_enrollment_internal(row["enrollment_id"])
    expected = enrollment["created_at"] + timedelta(seconds=ledger.HOSTED_SESSION_SECONDS)
    assert row["hosted_url_expires_at"] == expected
    reused = await _start()
    assert (await _step(reused)).state == "needs_enrollment"
    assert (await _get(reused))["hosted_url_expires_at"] == expected
    for _ in range(2):
        public = await route._owner_view(purchase_id=purchase_id, agent_id="agent_one", agent_user_ref_hash="hash_alice")
        assert public["hosted_url_expires_at"] == expected
        assert "buyer_ref" not in public and "enrollment_id" not in public
        assert "buyer_email" not in public and "shipping_address" not in public
    assert await route._owner_view(purchase_id=purchase_id, agent_id="wrong-agent", agent_user_ref_hash="hash_alice") is None
    assert await route._owner_view(purchase_id=purchase_id, agent_id="agent_one", agent_user_ref_hash="wrong-buyer") is None


async def test_enrollment_deadline_legacy_owner_view_uses_originating_attempt_without_writes(reap):
    from db.database import database
    import db.reap_agentic_ledger as ledger
    import routes.agent_commerce_reap as route
    purchase_id = await _deadline_missing_provider_expiry(reap)
    expected = (await _get(purchase_id))["hosted_url_expires_at"]
    await database.execute("UPDATE reap_agentic_purchases SET hosted_url_expires_at = NULL WHERE id = :id", {"id": purchase_id})
    for _ in range(2):
        public = await route._owner_view(purchase_id=purchase_id, agent_id="agent_one", agent_user_ref_hash="hash_alice")
        assert public["hosted_url_expires_at"] == expected
        assert public["hosted_url"]
    assert (await _get(purchase_id))["hosted_url_expires_at"] is None


@pytest.mark.parametrize("bad", ["expired", "wrong_buyer", "wrong_id", "wrong_link", "unavailable", "malformed_clock", "dead", "active"])
async def test_enrollment_deadline_legacy_fails_closed_when_provenance_cannot_support_link(reap, monkeypatch, bad):
    from db.database import database, IS_POSTGRES
    import db.reap_agentic_ledger as ledger
    import routes.agent_commerce_reap as route
    purchase_id = await _deadline_missing_provider_expiry(reap)
    row = await _get(purchase_id)
    await database.execute("UPDATE reap_agentic_purchases SET hosted_url_expires_at = NULL WHERE id = :id", {"id": purchase_id})
    if bad == "expired":
        past = "clock_timestamp() - INTERVAL '25 days'" if IS_POSTGRES else "datetime('now', '-25 days')"
        await database.execute(f"UPDATE reap_agentic_enrollments SET created_at = {past} WHERE id = :id", {"id": row["enrollment_id"]})
    else:
        original = await ledger.get_enrollment_internal(row["enrollment_id"])
        if bad == "wrong_buyer": original["buyer_ref"] = "some-other-buyer"
        if bad == "wrong_id": original["id"] = "some-other-attempt"
        if bad == "wrong_link": original["hosted_url"] = "https://pay.prava.space/enroll/some-other-link"
        if bad == "malformed_clock": original["created_at"] = "not-a-timestamp"
        if bad in {"dead", "active"}: original["status"] = bad
        async def record(*args): return None if bad == "unavailable" else original
        monkeypatch.setattr(ledger, "get_enrollment_internal", record)
    for _ in range(2):
        public = await route._owner_view(purchase_id=purchase_id, agent_id="agent_one", agent_user_ref_hash="hash_alice")
        assert "hosted_url" not in public
    assert (await _get(purchase_id))["hosted_url_expires_at"] is None


@pytest.mark.parametrize("bad", ["invalid-date", "", 123])
async def test_enrollment_deadline_malformed_provider_expiry_is_not_treated_as_omitted(reap, bad):
    import copy
    payload = copy.deepcopy(ENROLLMENT_CREATED)
    payload["nextAction"]["expiresAt"] = bad
    reap.create_enrollment = _ok(payload)
    purchase_id = await _start()
    result = await _step(purchase_id)
    assert result.outcome == "released" and result.last_error_code == "enrollment_deadline_invalid"
    assert not (await _get(purchase_id))["hosted_url"]


async def test_enrollment_deadline_authoritative_expiry_still_wins(reap):
    import db.reap_agentic_ledger as ledger
    purchase_id = await _start()
    assert (await _step(purchase_id)).state == "needs_enrollment"
    row = await _get(purchase_id)
    assert row["hosted_url_expires_at"].year == 2099
    enrollment = await ledger.get_enrollment_internal(row["enrollment_id"])
    assert row["hosted_url_expires_at"] == enrollment["hosted_url_expires_at"]


async def test_enrollment_deadline_malformed_response_cannot_retire_new_holders_attempt(reap, monkeypatch):
    import copy
    from db.database import database
    import db.reap_agentic_ledger as ledger
    payload = copy.deepcopy(ENROLLMENT_CREATED)
    payload["nextAction"]["expiresAt"] = "invalid-date"
    reap.create_enrollment = _ok(payload)
    purchase_id = await _start()
    actual = ledger.upsert_pending_enrollment
    recorded = []
    async def record_then_lose(**kwargs):
        row = await actual(**kwargs)
        if kwargs.get("reap_enrollment_id"):
            recorded.append(row["id"])
            await database.execute("UPDATE reap_agentic_purchases SET claimed_by = 'new-holder' WHERE id = :id", {"id": purchase_id})
        return row
    monkeypatch.setattr(ledger, "upsert_pending_enrollment", record_then_lose)
    assert (await _step(purchase_id)).outcome == "lost_claim"
    assert (await ledger.get_enrollment_internal(recorded[0]))["status"] == "pending"
    assert (await _get(purchase_id))["claimed_by"] == "new-holder"


async def test_pilot_dispatch_permission_uses_authoritative_quote_total(monkeypatch, reap):
    import json
    import services.reap_agentic_purchase as svc
    import services.reap_agentic_client as rc
    from db.database import database
    monkeypatch.setenv("PIVOTA_ENV", "production")
    monkeypatch.setenv("REAP_AGENTIC_PILOT_SCOPE", json.dumps(_bounded_pilot()))
    purchase = await _start()
    await _active_enrollment()
    await database.execute("UPDATE reap_agentic_purchases SET state='quoting' WHERE id=:id", {"id": purchase})
    dispatched = []
    async def final_boundary(**kwargs):
        # Item remains admitted at4250, but authoritative4500 charge no longer fits.
        monkeypatch.setenv("REAP_AGENTIC_PILOT_SCOPE", json.dumps(_bounded_pilot(max_total_minor=4400)))
        assert rc.worker_provider_permission.get()() is False
        rc._check_worker_provider_permission()
        dispatched.append(kwargs)
        return _ok(CHECKOUT_CREATED)
    monkeypatch.setattr(rc, "create_checkout", final_boundary)
    result = await _step(purchase)
    assert result.outcome == "released" and result.state == "quoting"
    assert dispatched == []
    row = await _get(purchase)
    assert row["reap_checkout_id"] is None and row["claimed_by"] is None
    assert svc._WORKER_QUOTE_TOTAL.get() is None

import db.reap_agentic_ledger as ledger


_PILOT_REAL_CREATE_CHECKOUT = rc.create_checkout


@pytest.mark.parametrize("replacement", ["same_holder_attempt", "foreign_holder", "same_holder_clock", "exposed_checkout"])
async def test_scope_pause_actual_dispatch_does_not_adopt_replacement_claim(monkeypatch, reap, replacement):
    import httpx
    from db.database import IS_POSTGRES
    monkeypatch.setenv("PIVOTA_ENV", "production")
    monkeypatch.setenv("REAP_AGENTIC_RECONCILE_ENABLED", "1")
    monkeypatch.setenv("REAP_AGENTIC_PILOT_SCOPE", json.dumps(_bounded_pilot()))
    purchase = await _start()
    await _active_enrollment()
    await database.execute("UPDATE reap_agentic_purchases SET state='quoting' WHERE id=:id", {"id": purchase})
    assert len(await ledger.claim_due_purchases("fence-worker", pilot_scope=svc.pilot_admission_scope())) == 1
    captured = {}
    dispatched = []
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self):
            if replacement == "same_holder_clock":
                clock = "claimed_at + INTERVAL '1 second'" if IS_POSTGRES else "datetime(claimed_at, '+1 second')"
                await database.execute("UPDATE reap_agentic_purchases SET claimed_at=" + clock + " WHERE id=:id", {"id": purchase})
            elif replacement == "exposed_checkout":
                await database.execute("UPDATE reap_agentic_purchases SET state='awaiting_approval', reap_checkout_id='replacement-checkout' WHERE id=:id", {"id": purchase})
            else:
                holder = "foreign-worker" if replacement == "foreign_holder" else "fence-worker"
                await database.execute("UPDATE reap_agentic_purchases SET attempts=attempts+1,claimed_by=:holder WHERE id=:id", {"id": purchase, "holder": holder})
            captured.update(await ledger.get_purchase_internal(purchase))
            monkeypatch.setenv("REAP_AGENTIC_CREATE_ENABLED", "0")
            return self
        async def __aexit__(self, *args): return False
        def stream(self, *args, **kwargs):
            dispatched.append(args)
            raise AssertionError("checkout dispatched after a scope stop")
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    monkeypatch.setattr(rc, "create_checkout", _PILOT_REAL_CREATE_CHECKOUT)
    result = await svc.advance(purchase, "fence-worker")
    assert dispatched == [] and result.outcome == "lost_claim"
    after = await ledger.get_purchase_internal(purchase)
    for field in ["attempts", "claimed_by", "claimed_at", "state", "state_entered_at", "last_error_code", "next_poll_at", "reap_checkout_id"]:
        assert after[field] == captured[field], field


_PILOT_REAL_GET_ENROLLMENT = rc.get_enrollment


@pytest.mark.parametrize("state", ["needs_enrollment", "resolving"])
async def test_scope_pause_attempt_exempt_claim_timestamp_generation(monkeypatch, reap, state):
    import httpx
    from db.database import IS_POSTGRES
    monkeypatch.setenv("PIVOTA_ENV", "production")
    monkeypatch.setenv("REAP_AGENTIC_RECONCILE_ENABLED", "1")
    monkeypatch.setenv("REAP_AGENTIC_PILOT_SCOPE", json.dumps(_bounded_pilot()))
    purchase = await _start()
    assert (await _step(purchase)).state == "needs_enrollment"
    assert await ledger.release_claim(purchase, "w1") is not None
    await database.execute("UPDATE reap_agentic_purchases SET state=:state,last_error_code=:error,next_poll_at=CURRENT_TIMESTAMP WHERE id=:id",
                           {"id": purchase, "state": state, "error": "enrollment_settling" if state == "resolving" else None})
    original = await ledger.get_purchase_internal(purchase)
    claimed = await ledger.claim_due_purchases("fence-worker", pilot_scope=svc.pilot_admission_scope())
    assert len(claimed) == 1 and claimed[0]["attempts"] == original["attempts"]
    captured = {}
    dispatched = []
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self):
            clock = "claimed_at + INTERVAL '1 second'" if IS_POSTGRES else "datetime(claimed_at, '+1 second')"
            await database.execute("UPDATE reap_agentic_purchases SET claimed_at=" + clock + " WHERE id=:id", {"id": purchase})
            captured.update(await ledger.get_purchase_internal(purchase))
            monkeypatch.setenv("REAP_AGENTIC_CREATE_ENABLED", "0")
            return self
        async def __aexit__(self, *args): return False
        def stream(self, *args, **kwargs):
            dispatched.append(args)
            raise AssertionError("enrollment GET dispatched after scope stop")
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    monkeypatch.setattr(rc, "get_enrollment", _PILOT_REAL_GET_ENROLLMENT)
    result = await svc.advance(purchase, "fence-worker")
    assert len(dispatched) == (1 if state == "needs_enrollment" else 0) and result.outcome == "lost_claim"
    after = await ledger.get_purchase_internal(purchase)
    for field in ["attempts", "claimed_by", "claimed_at", "state", "last_error_code", "next_poll_at"]:
        assert after[field] == captured[field], field


async def test_an_interleaved_transient_read_error_does_not_reset_the_permanent_count(reap):
    import services.reap_agentic_client as rc
    pid = await _recovery_checkout()
    permanent = rc.ReapResponse(ok=False, status=404, error='reap_status_404')
    codes = []
    for answer in (permanent, _transport(), permanent, rc.ReapResponse(ok=False, status=500, error='reap_status_500'),
                   permanent):
        reap.get_checkout = answer
        await _step(pid)
        codes.append((await _get(pid))['last_error_code'])
    assert codes == ['checkout_read_permanent:1:reap_status_404', 'checkout_read_permanent:1:reap_status_404',
                     'checkout_read_permanent:2:reap_status_404', 'checkout_read_permanent:2:reap_status_404',
                     'checkout_unresolvable:3:reap_status_404']
    assert (await _get(pid))['state'] == 'awaiting_approval'
    # A valid provider read still clears it.
    reap.get_checkout = _ok({'status': 'PROCESSING'})
    await _step(pid)
    assert (await _get(pid))['state'] == 'processing'


async def test_a_transient_hold_after_a_permanent_read_keeps_the_transient_backoff(reap):
    import services.reap_agentic_client as rc
    pid = await _recovery_checkout()
    reap.get_checkout = rc.ReapResponse(ok=False, status=404, error='reap_status_404')
    await _step(pid)
    reap.get_checkout = _transport()
    held = await _step(pid)
    assert held.last_error_code == 'checkout_read_permanent:1:reap_status_404'
    assert held.next_poll_in_seconds == svc.transport_backoff_seconds('awaiting_approval', (await _get(pid))['attempts'])


@pytest.mark.parametrize('dry_run', [0, 1, None, 'false', ''])
async def test_recovery_resolvers_require_an_explicit_boolean_dry_run(dry_run):
    from datetime import datetime, timezone
    from services import reap_checkout_recovery as recovery
    at = datetime.now(timezone.utc)
    with pytest.raises(recovery.ManualResolutionRefused, match='explicit_boolean_preview_required'):
        await recovery.resolve_checkout_manually('rp_any', evidence={}, operator_ref='op', expected_updated_at=at,
                                                 dry_run=dry_run)
    with pytest.raises(recovery.ManualResolutionRefused, match='explicit_boolean_preview_required'):
        await recovery.resolve_parked_dispatch('rp_any', dispatch_key='0' * 64, outcome='confirmed_not_created',
                                               evidence={}, operator_ref='op', expected_updated_at=at,
                                               dry_run=dry_run)


async def test_bounded_pilot_posture_is_logged_at_warning_once_per_process(monkeypatch, caplog):
    import json
    import logging
    monkeypatch.setenv("PIVOTA_ENV", "production")
    monkeypatch.setattr(svc, "_SCOPE_POSTURES", set())
    monkeypatch.setenv("REAP_AGENTIC_PILOT_SCOPE", json.dumps(_bounded_pilot()))
    caplog.set_level(logging.DEBUG, logger=svc.logger.name)
    for _ in range(3):
        assert svc.pilot_admission_scope() is not None
    lines = [r for r in caplog.records if 'pilot posture=' in r.getMessage()]
    assert len(lines) == 1 and lines[0].levelno == logging.WARNING
    assert 'posture=bounded fingerprint=' in lines[0].getMessage()
    assert 'agent_one' not in caplog.text
    caplog.clear()
    monkeypatch.setenv("REAP_AGENTIC_PILOT_SCOPE", "{not json")
    for _ in range(2):
        with pytest.raises(svc.PurchaseRefused):
            svc._pilot_scope()
    lines = [r for r in caplog.records if 'pilot posture=' in r.getMessage()]
    assert len(lines) == 1 and lines[0].levelno == logging.ERROR
    assert 'posture=invalid_or_missing' in lines[0].getMessage()


# -- claim timing: the regular cadence is due on the next tick; every hold stays exact ---------
#
# The claim is `next_poll_at <= now`. Only `_release`'s regular-cadence fallback (and a move into
# a human-wait state) is scheduled `svc.cadence_slack_seconds` early; holds are exact.


def _seconds_ago(seconds):
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=seconds)


async def _awaiting_buyer(reap):
    reap.get_checkout = _ok(CHECKOUT_CREATED)  # REQUIRES_ACTION: the buyer is still looking
    await _active_enrollment()
    pid = await _start()
    await _step(pid)
    await _step(pid)
    assert (await _get(pid))['state'] == 'awaiting_approval'
    await ledger.release_claim(pid, 'w1')
    return pid


@pytest.mark.parametrize('into_tick', [1, 14])
async def test_a_regular_cadence_row_is_due_on_the_next_tick(reap, monkeypatch, into_tick):
    """Released `into_tick` seconds after a tick began, 30 s ago: the tick now must take it."""
    from datetime import timedelta
    monkeypatch.delenv('REAP_AGENTIC_POLL_INTERVAL_SECONDS', raising=False)
    assert svc.cadence_slack_seconds(30) == 15 and svc.cadence_slack_seconds(15) == 7
    pid = await _awaiting_buyer(reap)
    released_at = _seconds_ago(30 - into_tick)
    monkeypatch.setattr(svc, '_now', lambda: released_at)
    result = await _step(pid)
    assert (result.outcome, result.state, result.next_poll_in_seconds) == ('released', 'awaiting_approval', 30)
    assert (await _get(pid))['next_poll_at'] == released_at + timedelta(seconds=15)
    assert [r['id'] for r in await ledger.claim_due_purchases('next-tick')] == [pid]


def test_the_cadence_slack_follows_a_fast_tick_and_never_exceeds_half_the_interval(monkeypatch):
    monkeypatch.setenv('REAP_AGENTIC_POLL_INTERVAL_SECONDS', '5')
    assert svc.cadence_slack_seconds(30) == 2
    monkeypatch.setenv('REAP_AGENTIC_POLL_INTERVAL_SECONDS', '600')
    assert [svc.cadence_slack_seconds(i) for i in (15, 30, 60)] == [7, 15, 15]


async def test_a_retry_after_is_not_claimable_before_it_ends(reap, monkeypatch):
    """Reap asked for 20 s; the poller must not call it again 5 s later."""
    import jobs.reap_agentic_purchase_poll as job
    from datetime import timedelta
    monkeypatch.delenv('REAP_AGENTIC_POLL_INTERVAL_SECONDS', raising=False)
    await _active_enrollment()
    pid = await _start()
    await _step(pid)
    reap.request_quote = rc.ReapResponse(ok=False, status=503, error='reap_status_503',
                                         error_code='QUOTE_TEMPORARILY_UNAVAILABLE', retry_after_seconds=20)
    released_at = _seconds_ago(5)
    real_now = svc._now
    monkeypatch.setattr(svc, '_now', lambda: released_at)
    held = await _step(pid)
    assert (held.outcome, held.next_poll_in_seconds) == ('released', 20)
    assert (await _get(pid))['next_poll_at'] == released_at + timedelta(seconds=20)
    monkeypatch.setattr(svc, '_now', real_now)
    reap.calls.clear()
    report = await job.run_reap_agentic_purchase_poll(worker_id='retry-after-plus-5')
    assert report.claimed == 0 and reap.calls == []


async def test_the_error_backoff_is_not_claimable_before_it_ends(reap, monkeypatch):
    """`advance` raised 105 s ago; its 120 s backoff has 15 s left."""
    import jobs.reap_agentic_purchase_poll as job
    from datetime import timedelta
    monkeypatch.delenv('REAP_AGENTIC_POLL_INTERVAL_SECONDS', raising=False)
    pid = await _start()
    real_advance = svc.advance

    async def _raises(purchase_id, worker_id):
        raise RuntimeError('synthetic')

    monkeypatch.setattr(svc, 'advance', _raises)
    raised_at = _seconds_ago(105)
    report = await job.run_reap_agentic_purchase_poll(worker_id='raised', now=raised_at)
    assert report.errors == 1
    assert (await _get(pid))['next_poll_at'] == raised_at + timedelta(seconds=120)
    monkeypatch.setattr(svc, 'advance', real_advance)
    report = await job.run_reap_agentic_purchase_poll(worker_id='backoff-plus-105')
    assert report.claimed == 0 and reap.calls == []


async def test_the_not_created_hold_is_never_claimed_early_even_with_a_long_tick(reap, monkeypatch):
    """With a 120 s tick the 270 s hold (A2) must still land in a later quote bucket."""
    import jobs.reap_agentic_purchase_poll as job
    from datetime import timedelta
    monkeypatch.setenv('REAP_AGENTIC_POLL_INTERVAL_SECONDS', '120')
    await _active_enrollment()
    pid = await _start()
    await _step(pid)
    reap.create_checkout = rc.ReapResponse(ok=False, status=503, error='reap_status_503',
                                           error_code='CHECKOUT_TEMPORARILY_UNAVAILABLE')
    held_at = _seconds_ago(230)
    real_now = svc._now
    monkeypatch.setattr(svc, '_now', lambda: held_at)
    held = await _step(pid)
    assert held.next_poll_in_seconds == svc.PROVIDER_NOT_CREATED_HOLD_S == 270
    assert (await _get(pid))['next_poll_at'] == held_at + timedelta(seconds=270)
    monkeypatch.setattr(svc, '_now', real_now)
    reap.calls.clear()
    report = await job.run_reap_agentic_purchase_poll(worker_id='tick-120')
    assert report.claimed == 0 and reap.calls == []
    assert len(reap.named('create_checkout')) == 0
