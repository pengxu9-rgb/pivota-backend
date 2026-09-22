"""jobs/reap_agentic_purchase_poll.py against REAL Postgres — the half SQLite cannot test.

Picked up automatically by .github/workflows/postgres-dialect-gate.yml via the
`tests/test_*_postgres.py` glob.

NOT A COPY OF THE SQLITE ARM. Loop semantics, counts and the dial table belong there; what is
here is the set of properties whose truth depends on the ENGINE:

  1. TWO REAL BACKEND CONNECTIONS. The SQLite arm's concurrency test proves the claim under
     INTERLEAVING — databases==0.7.0 serialises everything onto one connection there, so it
     cannot say anything about two POD PROCESSES. Here a second worker runs on its own asyncpg
     connection and commits, which is the deployment this job actually has: the worker service
     may be scaled past one replica, and prod and staging share one Postgres.

  2. THE JOB'S OWN STATEMENT PLANS. `_LEFTOVER_CLAIMS_SQL` is the only SQL this package adds, and
     an unplannable statement on a live path is the #1588 shape this repo has already paid for.
     SQLite plans nothing, so the check has to live here.

  3. THE SERVER-SIDE CLOCK ON THE ERROR PATH. `next_poll_at` after `advance` RAISED is the one
     timestamp this job computes in Python, and asyncpg encodes a naive datetime for a
     timestamptz as THIS PROCESS's local wall time — measured seven hours off from a Pacific box
     against a UTC database. A backoff that lands seven hours out is a purchase that stalls for
     an afternoon; one that lands seven hours in the PAST is a hot loop against a partner.

  4. THE SWEEPS' BOUNDED UPDATE UNDER READ COMMITTED, including the `:include_processing = 1`
     bind compared against an integer literal — an AmbiguousParameter shape SQLite cannot have.

── FIXTURES ARE DUPLICATED, NOT IMPORTED, and that is the house convention on this rail (both
ledger arms and both state-machine arms do the same). Importing the SQLite module would execute
its module body — including a `pytestmark` that skips on Postgres — and would make the dialect
gate's collection depend on a file it is not collecting.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_reap_wp3_test \\
        .venv/bin/python -m pytest tests/test_reap_agentic_purchase_poll_postgres.py
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
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

#: BOTH migrations, in order. 225 adds the resolution-hint columns #2204 now persists, and
#: applying only 224 would build a schema the repo no longer declares — a dialect gate testing a
#: table that does not exist anywhere else is worse than no gate.
_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db/migrations"
_MIGRATIONS = (
    _MIGRATIONS_DIR / "224_reap_agentic_ledger.sql",
    _MIGRATIONS_DIR / "225_reap_agentic_purchase_hints.sql",
    # 233 adds consent_version + consented_at to reap_agentic_purchases. The self-heal carries
    # it, so a migration build without it is not the schema production has — and every purchase this suite opens
    # would fail on an UndefinedColumn. See
    # feedback_a_later_migration_that_alters_a_table_breaks_that_tables_own_parity_test.
    _MIGRATIONS_DIR / "233_reap_agentic_purchase_consent.sql",
)

# Same convention as the other gates on this rail: this file DROPS its tables, so it must be
# INCAPABLE of running anywhere but a throwaway — made true, not merely stated.
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")


# ── payloads ─────────────────────────────────────────────────────────────────────────────────

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


def _future_iso(seconds: int = 3600) -> str:
    """The partner's own expiry, RELATIVE TO NOW.

    A frozen literal here would be swept up by `expire_overdue_purchases` — which this job runs
    first thing in every run, unlike the state machine's arms, where the same constants are
    harmless.
    """
    return (
        (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
    )


def _enrollment_created(attempt_id: str) -> dict:
    # A REAL UUID, derived from our attempt id. #2204's fix round validates every partner id it
    # stores through `rc._path_id(..., uuid=True)`; our own `re_...` ledger id is refused and the
    # purchase fails with `partner_id_malformed` instead of enrolling. uuid5 keeps it unique per
    # enrollment (the table has a unique index on `reap_enrollment_id`) and stable within a run.
    return {
        "id": str(uuid.uuid5(uuid.NAMESPACE_OID, str(attempt_id))),
        "status": "REQUIRES_ACTION",
        "nextAction": {
            "type": "REDIRECT",
            "url": "https://pay.prava.space/enroll/3fa85f64",
            "expiresAt": _future_iso(),
        },
    }


def _enrollment_active(partner_id: str) -> dict:
    return {
        "id": str(partner_id),
        "status": "ACTIVE",
        "paymentMethod": {"type": "CARD", "network": "VISA", "last4": "4242"},
        "nextAction": None,
    }


QUOTE_200 = {
    "id": "f1e2d3c4",
    # THE ECHO, and it is not optional any more. #2204's adoption round made `verify_quote`
    # compare what the quote says it PRICED against the variant we asked for -- Reap returns 200
    # for a SUBSTITUTED variant, so every other check looks at what it costs and none of them
    # would notice. A quote with no `items` is refused, which is correct and which is exactly
    # what a stale copy of this fixture produced here: `price_changed` on every happy path.
    #
    # This is the running cost of the duplicate-the-fixtures convention on this rail (see the
    # module docstring). It is paid deliberately: importing the SQLite arm would make the dialect
    # gate's collection depend on a file it is not collecting.
    "items": [{"variantId": "var_abc123", "quantity": 1}],
    # FAR future on purpose: the quoting step now refuses to create a checkout from a quote it
    # can already see is dead, so a fixture with a past expiry refuses every happy path.
    "expiresAt": "2099-01-01T00:00:00Z",
    "amountBreakdown": {
        "itemsSubtotal": {"amount": 42.50, "currency": "USD"},
        "shipping": {"amount": 1.00, "currency": "USD"},
        "tax": {"amount": {"amount": 1.50, "currency": "USD"}, "includedInPrices": False},
        "finalAmount": {"amount": 45.00, "currency": "USD"},
    },
}


def _checkout_created() -> dict:
    return {
        "id": "chk_7f3a",
        "status": "REQUIRES_ACTION",
        "quoteId": "f1e2d3c4",
        "nextAction": {
            "type": "REDIRECT",
            "url": "https://pay.prava.space/checkout/chk_7f3a",
            "expiresAt": _future_iso(),
        },
    }


CHECKOUT_COMPLETED = {
    "id": "chk_7f3a",
    "status": "COMPLETED",
    "orderId": "ord_991",
    "finalAmount": {"amount": 45.00, "currency": "USD"},
    "nextAction": None,
}


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(
            f"refusing to drop the agentic-ledger tables in database {dbname!r} — "
            f"throwaway only (e.g. pivota_reap_wp3_test)"
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
        for _table in ('reap_agentic_purchases', 'reap_agentic_enrollments'):
            try:
                await database.execute(f"DELETE FROM {_table}")
            except Exception:  # noqa: BLE001 - a table this run never built is not a leak
                continue
        if not was_connected and database.is_connected:
            await database.disconnect()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    import jobs.reap_agentic_purchase_poll as job

    monkeypatch.setenv("REAP_AGENTIC_ENABLED", "1")
    monkeypatch.setenv("REAP_API_BASE_URL", "https://sandbox.api.reap.global")
    monkeypatch.setenv("REAP_API_KEY", "sk_test_key")
    monkeypatch.delenv("REAP_RETURN_URL_HOSTS", raising=False)
    for dial in job.DIALS.values():
        monkeypatch.delenv(dial.env, raising=False)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    import httpx

    class _NetworkForbidden:
        def __init__(self, *a, **k):
            raise AssertionError("a test reached the network; the Reap client must be faked")

    monkeypatch.setattr(httpx, "AsyncClient", _NetworkForbidden)


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
        # A DISTINCT partner enrollment id per call: `reap_agentic_enrollments` has a unique
        # index on `reap_enrollment_id`, and a batch of purchases for different buyers enrolls
        # more than once in a single run.
        self.create_enrollment = lambda **kw: _ok(
            _enrollment_created(kw.get("attempt_id") or ENROLLMENT_UUID)
        )
        self.get_enrollment = lambda **kw: _ok(_enrollment_active(kw.get("id")))
        self.create_checkout = _ok(_checkout_created())
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


async def _all_claims():
    from db.database import database

    rows = await database.fetch_all(
        "SELECT id, claimed_by FROM reap_agentic_purchases WHERE claimed_by IS NOT NULL", {}
    )
    return {str(r["id"]): r["claimed_by"] for r in rows}


async def _make_due(purchase_id: str) -> None:
    await _raw(
        "UPDATE reap_agentic_purchases SET next_poll_at = clock_timestamp() WHERE id = :i",
        {"i": purchase_id},
    )


async def _run(**kwargs):
    import jobs.reap_agentic_purchase_poll as job

    return await job.run_reap_agentic_purchase_poll(**kwargs)


_BIND_RE = re.compile(r":([a-zA-Z_][a-zA-Z0-9_]*)")


def _to_positional(sql: str):
    """Rewrite `:name` binds to asyncpg's `$n`, keeping first-appearance order.

    This lets the tests below execute THE JOB'S OWN SQL TEXT on a raw asyncpg connection rather
    than a hand-copied paraphrase — a paraphrase keeps passing after somebody changes the real
    statement, which is the failure mode these tests exist for."""
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


# ══ 1. two REAL backend connections ═══════════════════════════════════════════════════════════


async def test_a_second_pod_that_claimed_the_row_first_leaves_this_run_with_nothing(reap):
    """THE CLAIM, ACROSS BACKENDS. Another pod takes the lease on its own asyncpg connection and
    COMMITS; this run's `claim_due_purchases` then finds nothing to take, makes no partner call,
    and — the part that matters — does not touch the other pod's lease on its way out.

    Deterministic rather than gathered: a gathered race can pass by accident when the runtime
    happens to serialise both calls onto one connection, which is exactly what SQLite does."""
    purchase_id = await _start()

    conn = await _raw_connection()
    try:
        taken = await conn.fetch(
            "UPDATE reap_agentic_purchases SET claimed_by = $1, claimed_at = clock_timestamp() "
            "WHERE id = $2 AND claimed_by IS NULL RETURNING id",
            "pod-b",
            purchase_id,
        )
        assert len(taken) == 1
    finally:
        await conn.close()

    report = await _run(worker_id="pod-a")

    assert report.claimed == 0 and report.advanced == 0 and report.errors == 0
    assert reap.calls == []
    assert await _all_claims() == {purchase_id: "pod-b"}, "pod-a freed pod-b's live lease"


async def test_a_second_pod_that_steals_the_lease_mid_step_yields_lost_claim_not_a_write(reap):
    """The interleaving `transition_as_holder` exists for, on two real connections: this run has
    the lease and is awaiting the partner when another pod takes it. Every write the step then
    makes carries `AND claimed_by = 'pod-a'`, which no longer matches, so the answer is
    `lost_claim` and the row is where it was."""
    purchase_id = await _start()
    before = await _get(purchase_id)

    async def _steal_then_resolve(**kwargs):
        conn = await _raw_connection()
        try:
            await conn.execute(
                "UPDATE reap_agentic_purchases SET claimed_by = $1 WHERE id = $2",
                "pod-b",
                purchase_id,
            )
        finally:
            await conn.close()
        return _resolved()

    reap.resolve_our_row = _steal_then_resolve

    report = await _run(worker_id="pod-a")

    assert report.claimed == 1
    assert report.lost_claim == 1
    assert report.advanced == 0
    assert report.errors == 0, "a lost claim is never an error"
    after = await _get(purchase_id)
    assert after["state"] == before["state"] == "resolving"
    assert after["claimed_by"] == "pod-b", "pod-a released the thief's lease"


async def test_two_concurrent_runs_on_one_connection_never_advance_a_row_twice(reap):
    """The gathered form as well, because it is the shape an operator's `run-now` alongside a
    scheduled tick really takes. On this driver both coroutines share one Connection, so this is
    the INTERLEAVING half; the two-connection half is the two tests above."""
    purchase_id = await _start()

    a, b = await asyncio.gather(_run(worker_id="pod-a"), _run(worker_id="pod-b"))

    assert a.claimed + b.claimed == 1, (a, b)
    assert a.advanced + b.advanced == 1, (a, b)
    assert a.errors + b.errors == 0
    assert len(reap.named("resolve_our_row")) == 1
    assert (await _get(purchase_id))["state"] == "needs_enrollment"
    assert await _all_claims() == {}


# ══ 2. the job's own statement ════════════════════════════════════════════════════════════════


async def test_the_jobs_only_sql_constant_prepares():
    """The #1588 class: Postgres refuses to plan a statement whose parameter types it cannot
    infer, and SQLite cannot see it. This package adds exactly one statement."""
    import jobs.reap_agentic_purchase_poll as job

    constants = {
        name: value
        for name, value in vars(job).items()
        if name.endswith("_SQL") and isinstance(value, str)
    }
    assert constants, "the job has no SQL constants — has it started building SQL at call time?"

    conn = await _raw_connection()
    failures = []
    try:
        for name, sql in sorted(constants.items()):
            positional, _order = _to_positional(sql)
            try:
                await conn.prepare(positional)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
    finally:
        await conn.close()
    assert not failures, "these statements cannot be planned by Postgres:\n  " + "\n  ".join(
        failures
    )


async def test_the_repo_prepare_sweep_can_see_the_jobs_sql():
    """MERGED IS NOT RUNNING. tests/test_repo_sql_prepare_postgres.py resolves a `database.*`
    first argument only when it is a literal or a MODULE-level name bound to one. SQL assembled
    at call time — an f-string, a helper's return value, a local bound to
    `A if IS_POSTGRES else B` — is a counted blind spot, and a live path nothing PREPAREs is how
    #1588 reached production."""
    import ast

    import jobs.reap_agentic_purchase_poll as job

    tree = ast.parse(Path(job.__file__).read_text(encoding="utf-8"))
    module_level = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant)
    }

    call_sites = 0
    unresolvable = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
            continue
        if func.value.id != "database":
            continue
        call_sites += 1
        first = node.args[0] if node.args else None
        if isinstance(first, ast.Constant):
            continue
        if isinstance(first, ast.Name) and first.id in module_level:
            continue
        unresolvable.append(ast.dump(first) if first is not None else "<no args>")

    # TWO: the leftover-claims sweep and the read-only `processing_over_attempts` count. Pinned
    # by number so a third arrives with a deliberate decision rather than by accident.
    assert call_sites == 2, f"the job now has {call_sites} database call sites"
    assert not unresolvable, f"SQL the repo's PREPARE sweep cannot follow: {unresolvable}"


async def test_the_leftover_claims_statement_only_matches_this_worker(reap):
    """Executed as its own statement on a raw connection, against the real schema: the conjunct
    is on `claimed_by`, so a run's cleanup can never free another pod's live lease."""
    import jobs.reap_agentic_purchase_poll as job

    mine = await _start(buyer_ref="bref_mine")
    theirs = await _start(buyer_ref="bref_theirs")
    await _raw(
        "UPDATE reap_agentic_purchases SET claimed_by = 'pod-b', claimed_at = clock_timestamp() "
        "WHERE id = :i",
        {"i": theirs},
    )
    await _raw(
        "UPDATE reap_agentic_purchases SET claimed_by = 'pod-a', claimed_at = clock_timestamp() "
        "WHERE id = :i",
        {"i": mine},
    )

    conn = await _raw_connection()
    try:
        positional, order = _to_positional(job._LEFTOVER_CLAIMS_SQL)
        rows = await conn.fetch(positional, *[{"worker_id": "pod-a"}[n] for n in order])
    finally:
        await conn.close()

    assert [str(r["id"]) for r in rows] == [mine]


# ══ 3. the server-side clock on the error path ════════════════════════════════════════════════


async def test_the_error_backoff_lands_in_the_future_by_the_configured_amount(
    monkeypatch, reap
):
    """`next_poll_at` after `advance` RAISED is the ONE timestamp this job computes in Python.
    asyncpg encodes a NAIVE datetime for a timestamptz as this process's local wall time —
    measured seven hours off from a Pacific box against a UTC database. Seven hours late is a
    purchase that stalls for an afternoon; seven hours early is a hot loop against a partner.

    Compared against the SERVER's own clock, in SQL, so the assertion cannot be satisfied by two
    equally-wrong Python values."""
    import jobs.reap_agentic_purchase_poll as job
    from db.database import database

    purchase_id = await _start()

    async def _raises(pid, worker):
        raise RuntimeError("driver")

    monkeypatch.setattr(job.purchase_svc, "advance", _raises)
    monkeypatch.setenv("REAP_AGENTIC_ERROR_BACKOFF_SECONDS", "300")

    report = await _run(worker_id="pod-a")
    assert report.errors == 1

    row = await database.fetch_one(
        "SELECT EXTRACT(EPOCH FROM (next_poll_at - clock_timestamp())) AS delta "
        "FROM reap_agentic_purchases WHERE id = :i",
        {"i": purchase_id},
    )
    delta = float(row["delta"])
    assert 280 <= delta <= 320, f"the backoff landed {delta}s out, not ~300s"
    assert await _all_claims() == {}


async def test_the_release_after_a_normal_step_does_not_move_the_schedule(reap):
    """`release_claim` is issued with NO `next_poll_at`, so it COALESCEs to the value the state
    machine wrote. On Postgres that value is a timestamptz the service bound through `_bind_dt`;
    a release that overwrote it with `clock_timestamp()` would make every human-wait state due
    immediately, and the buyer's hosted page would be polled every tick."""
    from db.database import database

    purchase_id = await _start()
    await _run(worker_id="pod-a")

    row = await database.fetch_one(
        "SELECT state, claimed_by, "
        "EXTRACT(EPOCH FROM (next_poll_at - clock_timestamp())) AS delta "
        "FROM reap_agentic_purchases WHERE id = :i",
        {"i": purchase_id},
    )
    assert row["state"] == "needs_enrollment"
    assert row["claimed_by"] is None
    # `needs_enrollment` is a human-wait state: a full 30s interval out.
    assert 20 <= float(row["delta"]) <= 35, float(row["delta"])


# ══ 4. the sweeps, on the real engine ═════════════════════════════════════════════════════════


async def test_the_sweeps_run_before_the_claim_and_a_dead_pods_lease_is_recovered(reap):
    """The ORDER, on Postgres, where `clock_timestamp()` computes every cutoff server-side. A row
    held by a dead pod is not claimable until the requeue frees it, so a claim-first run would
    skip exactly the rows that most need attention."""
    purchase_id = await _start()
    await _raw(
        "UPDATE reap_agentic_purchases SET claimed_by = 'dead_pod', "
        "claimed_at = clock_timestamp() - INTERVAL '4000 seconds' WHERE id = :i",
        {"i": purchase_id},
    )

    report = await _run(worker_id="pod-a")

    assert report.requeued == 1
    assert report.claimed == 1, "the requeued row was not picked up in the same run"
    assert report.advanced == 1
    assert await _all_claims() == {}


async def test_the_expire_sweep_is_the_pii_deadline_and_reports_its_count(reap):
    purchase_id = await _start()
    await _raw(
        "UPDATE reap_agentic_purchases SET state = 'awaiting_approval', "
        "state_entered_at = clock_timestamp() - INTERVAL '4000 seconds' WHERE id = :i",
        {"i": purchase_id},
    )

    report = await _run(worker_id="pod-a")

    assert report.expired == 1
    row = await _get(purchase_id)
    assert row["state"] == "expired"
    assert row["buyer_email"] is None and row["shipping_address"] is None
    assert row["terminal_at"] is not None


async def test_a_processing_purchase_is_never_auto_failed_by_the_counter(monkeypatch, reap):
    """`include_processing=False` on the real statement, where the flag is a BIND compared against
    an integer literal (`:include_processing = 1 OR state <> 'processing'`) — an AmbiguousParameter
    shape SQLite cannot have. A purchase in 'processing' has been APPROVED BY THE BUYER and its
    payment is in flight; auto-failing it writes 'failed' over a charge we cannot see."""
    stuck = await _start(buyer_ref="bref_flight")
    monkeypatch.setenv("REAP_AGENTIC_MAX_ATTEMPTS", "1")
    await _raw(
        "UPDATE reap_agentic_purchases SET state = 'processing', attempts = 9999, "
        "reap_checkout_id = 'chk_7f3a' WHERE id = :i",
        {"i": stuck},
    )

    report = await _run(worker_id="pod-a")

    assert report.failed_exhausted == 0
    assert report.claimed == 1
    assert (await _get(stuck))["state"] == "completed"


async def test_the_fail_sweep_terminates_an_exhausted_row_before_any_claim(monkeypatch, reap):
    exhausted = await _start(buyer_ref="bref_tired")
    monkeypatch.setenv("REAP_AGENTIC_MAX_ATTEMPTS", "3")
    await _raw(
        "UPDATE reap_agentic_purchases SET attempts = 5 WHERE id = :i", {"i": exhausted}
    )

    report = await _run(worker_id="pod-a")

    assert report.failed_exhausted == 1
    assert report.claimed == 0
    row = await _get(exhausted)
    assert row["state"] == "failed" and row["last_error_code"] == "attempts_exhausted"
    assert row["buyer_email"] is None


# ══ 5. the gate and the happy path, on the real engine ════════════════════════════════════════


async def test_a_disabled_rail_still_runs_the_pii_deadline(monkeypatch, reap):
    """FIX F3 ON THE PRODUCTION SCHEMA, where `buyer_email` and `shipping_address` are real
    columns and the expire sweep's cutoffs are computed by the SERVER.

    The first cut gated the whole run, so with the rail off an abandoned purchase kept both
    values indefinitely. The dial stops us calling a partner; it is not permission to stop
    forgetting people."""
    purchase_id = await _start()
    await _raw(
        "UPDATE reap_agentic_purchases SET state = 'awaiting_approval', "
        "state_entered_at = clock_timestamp() - INTERVAL '4000 seconds' WHERE id = :i",
        {"i": purchase_id},
    )
    monkeypatch.setenv("REAP_AGENTIC_ENABLED", "0")

    report = await _run()

    assert report.skipped_disabled == 1
    assert report.claimed == 0
    assert reap.calls == [], "a disarmed run reached the partner"
    assert report.expired == 1
    row = await _get(purchase_id)
    assert row["state"] == "expired"
    assert row["buyer_email"] is None and row["shipping_address"] is None
    assert row["terminal_at"] is not None


async def test_a_disarmed_run_cannot_terminate_a_payment_in_flight(monkeypatch, reap):
    """The safety argument F3 rests on, on the real statements: neither ungated terminal sweep
    can reach a 'processing' row. `expire_overdue_purchases` names only the two waiting states,
    and `fail_exhausted_purchases` runs with `include_processing=False` -- a BIND compared against
    an integer literal, which is an AmbiguousParameter shape SQLite cannot have."""
    stuck = await _start(buyer_ref="bref_flight")
    await _raw(
        "UPDATE reap_agentic_purchases SET state = 'processing', attempts = 100005, "
        "state_entered_at = clock_timestamp() - INTERVAL '99999 seconds' WHERE id = :i",
        {"i": stuck},
    )
    monkeypatch.setenv("REAP_AGENTIC_MAX_ATTEMPTS", "1")
    monkeypatch.setenv("REAP_AGENTIC_ENABLED", "0")

    report = await _run()

    assert report.expired == 0 and report.failed_exhausted == 0
    assert (await _get(stuck))["state"] == "processing"
    # And fix F6 makes the refusal visible instead of silent.
    assert report.processing_over_attempts == 1


async def test_a_cancelled_run_propagates_and_still_releases_every_claim(monkeypatch, reap):
    """FIX F1/F2 ON POSTGRES, where a leaked claim is a real row in a shared database.

    The reviewer measured this on both dialects: with no `try/finally`, a deadline cancellation
    on the 2nd of 4 advances left 3 rows claimed for a whole lease -- and in the two waiting
    states that is a buyer's address and email held for the whole window.

    Both halves are asserted: `CancelledError` PROPAGATES (it is a `BaseException` so cleanup
    handlers do not swallow it, and the scheduler's watchdog depends on that), and the claims are
    released ANYWAY by the `finally`.
    """
    import asyncio as _asyncio

    import jobs.reap_agentic_purchase_poll as job

    ids = [await _start(buyer_ref=f"bref_{n}") for n in range(4)]
    seen = []
    real_advance = job.purchase_svc.advance

    async def _cancel_on_the_second(pid, worker):
        seen.append(pid)
        if len(seen) == 2:
            raise _asyncio.CancelledError()
        return await real_advance(pid, worker)

    monkeypatch.setattr(job.purchase_svc, "advance", _cancel_on_the_second)

    with pytest.raises(_asyncio.CancelledError):
        await _run(worker_id="pod-a")

    assert len(seen) == 2
    assert await _all_claims() == {}, "a cancelled run left claims behind on Postgres"
    states = {}
    for purchase_id in ids:
        states[purchase_id] = (await _get(purchase_id))["state"]
    assert sorted(states.values()) == [
        "needs_enrollment", "resolving", "resolving", "resolving"
    ]


async def test_the_full_happy_path_reaches_completed_over_n_runs(reap, attribution):
    """Each step a separate RUN, with the human-wait schedule brought forward between them —
    through the real jsonb columns (`queries_tried`, `shipping_address`) and the real partial
    unique index on `reap_checkout_id`."""
    from db.database import database

    purchase_id = await _start()
    seen = []
    for _ in range(6):
        report = await _run(worker_id="pod-a")
        row = await _get(purchase_id)
        seen.append(row["state"])
        if row["state"] in ("completed", "failed", "refused", "expired"):
            break
        assert report.errors == 0
        await _make_due(purchase_id)

    assert seen[-1] == "completed", seen
    row = await _get(purchase_id)
    assert row["reap_order_id"] == "ord_991"
    assert row["final_total_minor"] == 4500
    assert row["buyer_email"] is None and row["shipping_address"] is None
    assert row["claimed_by"] is None
    assert len(attribution.calls) == 1
    assert await _all_claims() == {}

    # jsonb COMES BACK AS TEXT on a raw statement — no SQLAlchemy result processor runs — so the
    # decoded assertion above is checked against the RAW column too. A decoded-only assertion
    # passes on a driver that never had the problem.
    raw = await database.fetch_one(
        "SELECT queries_tried FROM reap_agentic_purchases WHERE id = :i", {"i": purchase_id}
    )
    assert isinstance(raw["queries_tried"], str)
    assert json.loads(raw["queries_tried"]) == ["Brand fragrance", "Standard Eau de Parfum"]


async def test_a_batch_of_purchases_is_processed_sequentially_and_leaves_no_claims(reap):
    """The batch shape on the real engine: N rows, one partner call chain at a time, and every
    lease given back. `resolve_our_row` must be called exactly once per row — a gathered
    implementation would multiply the concurrent load on a partner that rate-limits."""
    ids = [await _start(buyer_ref=f"bref_{n}") for n in range(5)]

    report = await _run(worker_id="pod-a")

    assert report.claimed == 5 and report.advanced == 5 and report.errors == 0
    assert len(reap.named("resolve_our_row")) == 5
    assert await _all_claims() == {}
    for purchase_id in ids:
        assert (await _get(purchase_id))["state"] == "needs_enrollment"


async def test_no_pii_reaches_the_report_on_any_path(reap):
    """The report is counts and a duration, on every path. Asserted here as well as on SQLite
    because the PG arm is the one that runs against the production schema, where the row the job
    handles really does carry `buyer_email` and `shipping_address` as stored columns."""
    await _start()
    reports = [await _run(worker_id="pod-a"), await _run(worker_id="pod-a")]
    for report in reports:
        assert all(isinstance(v, int) for v in vars(report).values())
        for secret in PII_STRINGS:
            assert secret not in repr(report)
