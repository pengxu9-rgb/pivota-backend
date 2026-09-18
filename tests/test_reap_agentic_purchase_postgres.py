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

_MIGRATION = Path(__file__).resolve().parent.parent / "db/migrations/224_reap_agentic_ledger.sql"

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

    for statement in split_statements(_MIGRATION.read_text(encoding="utf-8")):
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
    await database.execute("DROP TABLE IF EXISTS reap_agentic_purchases")
    await database.execute("DROP TABLE IF EXISTS reap_agentic_enrollments")
    await _apply_migration()
    yield
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
        return {"id": "edge_1"}


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
    for _ in range(3):
        await _step(purchase_id)
    row = await _get(purchase_id)
    assert row["state"] == "failed"
    assert row["state"] != "awaiting_approval"
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
    assert row["last_error_code"] == "ENROLLMENT_NOT_ACTIVE"


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
    assert result.last_error_code == "unknown_checkout_status"
    assert (await _get(purchase_id))["state"] == "awaiting_approval"
    assert attribution.calls == []


async def test_every_start_refusal_leaves_the_table_empty(reap):
    """The same ordering contract as the SQLite arm, re-run here because an INSERT that fails at
    BIND time on asyncpg is a DIFFERENT failure from one refused in Python, and only one of them
    leaves nothing behind."""
    import services.reap_agentic_purchase as svc
    from db.database import database

    bad = [
        dict(quantity=0),
        dict(row=_row(currency="USDD")),
        dict(row=_row(accept_variant_labels=("x",))),
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


def _quote(**breakdown_over):
    payload = json.loads(json.dumps(QUOTE_200))
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
    reap.request_quote = _ok(_quote(itemsSubtotal=_usd(42.50), finalAmount=_usd(45.00)))
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
    reap.request_quote = _ok(_quote(itemsSubtotal=_usd(127.50), finalAmount=_usd(130.00)))
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


async def test_a_completed_checkout_with_no_order_id_does_not_complete(reap, attribution):
    """P1-3. The money is real; a row that says 'completed' while naming no order puts the GMV
    nowhere at all — the closer drops an empty `external_order_id` with a warning."""
    reap.get_checkout = _ok({k: v for k, v in CHECKOUT_COMPLETED.items() if k != "orderId"})
    purchase_id = await _start()
    for _ in range(4):
        result = await _step(purchase_id)
    row = await _get(purchase_id)
    assert result.state == "processing"
    assert row["state"] == "processing" and row["state"] != "completed"
    assert row["last_error_code"] == "completed_without_order_id"
    assert row["terminal_at"] is None
    assert attribution.calls == []


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
    assert row["state"] == "failed"
    assert row["last_error_code"] == "partner_id_malformed"
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
