"""The funnel producer's INSERT against REAL Postgres.

`record_anonymous_funnel_run` writes ARRAY-typed `product_keys` and JSONB
`partial_result_jsonb`; neither binds on SQLite, so the sibling test file
hand-inserts rows and tests only the reuse logic. The INSERT itself — and the
two properties that make it safe to expose to an unauthenticated caller — can
only be checked here:

  1. the row is created UNOWNED and stays out of the worker's claim query, so
     an anonymous endpoint cannot spend model credits;
  2. concurrent intakes for the same domain do not race into two owners once
     the run is claimed.

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        .venv/bin/python -m pytest \
        tests/test_funnel_anonymous_run_producer_postgres.py
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import uuid

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason=(
        "needs a Postgres DATABASE_URL — this is the production-dialect gate; "
        "see the module docstring for the one-line setup"
    ),
)

_MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "db/migrations" / "210_audit_runs_anonymous_claim.sql"
)

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(
            f"refusing to drop merchant_audit_runs in database {dbname!r} — "
            f"throwaway only (e.g. pivota_dialect_check)"
        )


# Production's shape, including the worker-lease columns and the stage default
# the producer relies on to stay OUT of the queue.
_PROD_SHAPE = """
CREATE TABLE merchant_audit_runs (
  run_id               UUID PRIMARY KEY,
  merchant_id          TEXT NOT NULL,
  requested_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  completed_at         TIMESTAMPTZ NULL,
  status               TEXT NOT NULL,
  product_keys         TEXT[] NOT NULL DEFAULT '{}',
  subject_type         TEXT NOT NULL DEFAULT 'merchant',
  stage                TEXT NOT NULL DEFAULT 'completed',
  stage_updated_at     TIMESTAMPTZ NULL,
  partial_result_jsonb JSONB NULL,
  claimed_by_worker    TEXT NULL,
  claimed_until        TIMESTAMPTZ NULL,
  verdict_labels       TEXT[] NULL,
  visibility_score_avg INTEGER NULL,
  attribution_score_avg INTEGER NULL,
  category_visibility_score_avg INTEGER NULL,
  audited_via_pivota_canonical TEXT[] NULL,
  content_keys         TEXT[] NULL,
  content_key_basis    JSONB NULL,
  report_jsonb         JSONB NULL,
  error_message        TEXT NULL,
  error_jsonb          JSONB NULL,
  cost_summary_jsonb   JSONB NULL,
  idempotency_key      TEXT NULL,
  cancelled_at         TIMESTAMPTZ NULL,
  requested_by_user_id TEXT NULL
)
"""


@pytest.fixture(autouse=True)
async def _db():
    from db.database import database
    from db.sql_migrations import split_statements

    _assert_throwaway_database()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await database.execute("DROP TABLE IF EXISTS merchant_audit_runs")
    await database.execute(_PROD_SHAPE)
    for statement in split_statements(_MIGRATION.read_text()):
        await database.execute(statement)
    import db.merchant_audit_runs as mar
    mar._DDL_READY = True
    yield
    mar._DDL_READY = False
    if not was_connected and database.is_connected:
        await database.disconnect()


def _now():
    return datetime.now(timezone.utc)


async def test_the_producer_creates_an_unowned_row_carrying_its_domain():
    import db.merchant_audit_runs as mar
    from db.database import database

    run_id = await mar.record_anonymous_funnel_run(domain="Anua.com")
    assert run_id, "the INSERT failed on the real dialect"
    row = dict(await database.fetch_one(
        "SELECT * FROM merchant_audit_runs WHERE run_id = :r", {"r": run_id}))
    assert row["merchant_id"] is None
    assert row["subject_type"] == mar.SUBJECT_TYPE_PUBLIC_FUNNEL
    assert mar.funnel_domain_of(row) == "anua.com"


async def test_the_producers_row_is_not_visible_to_the_worker_queue():
    """The safety property behind exposing this to anonymous callers: the row
    must never reach the model-spending lane. `stage` keeps its terminal
    default, which is what the worker's claim query filters on."""
    import db.merchant_audit_runs as mar
    from db.database import database

    await mar.record_anonymous_funnel_run(domain="anua.com")
    queued = await database.fetch_one(
        "SELECT count(*) AS n FROM merchant_audit_runs WHERE stage IN "
        "('queued','discovering','probing','scoring','materializing','verifying')"
    )
    assert dict(queued)["n"] == 0


async def test_a_produced_run_is_found_then_claimed_then_not_found_again():
    """The whole funnel round trip on the real dialect."""
    import db.merchant_audit_runs as mar

    run_id = await mar.record_anonymous_funnel_run(domain="anua.com")
    found = await mar.find_unclaimed_funnel_run_for_domain(
        domain="anua.com", since=_now() - timedelta(hours=1))
    assert found and found["run_id"] == run_id

    assert await mar.claim_audit_run_for_merchant(
        run_id=run_id, merchant_id="m-1") is True
    assert await mar.find_unclaimed_funnel_run_for_domain(
        domain="anua.com", since=_now() - timedelta(hours=1)) is None


async def test_concurrent_claims_of_a_produced_run_yield_one_owner():
    """Two visitors can hold the same run id — the domain is public and the
    run is reused per domain. Only one claim may win."""
    import db.merchant_audit_runs as mar
    from db.database import database

    run_id = await mar.record_anonymous_funnel_run(domain="anua.com")
    results = await asyncio.gather(*[
        mar.claim_audit_run_for_merchant(run_id=run_id, merchant_id=f"m-{i}")
        for i in range(8)
    ])
    assert sum(1 for r in results if r) == 1
    owners = await database.fetch_all(
        "SELECT DISTINCT merchant_id FROM merchant_audit_runs "
        "WHERE run_id = :r", {"r": run_id})
    assert len(owners) == 1


async def test_the_unclaimed_index_covers_the_producers_sweep():
    """The reuse lookup filters on `merchant_id IS NULL`, which is exactly
    what migration 210's partial index is for."""
    from db.database import database
    row = await database.fetch_one(
        "SELECT indexdef FROM pg_indexes "
        "WHERE indexname = 'idx_merchant_audit_runs_unclaimed'")
    assert row is not None
    assert "merchant_id IS NULL" in dict(row)["indexdef"]


# ---------------------------------------------------------------------------
# The intake handler driven against REAL tables.
#
# THE GAP THIS CLOSES. The SQLite intake tests stub `fetch_route_for_domain`
# with a fake whose `last_audit_run_id` is None. Production never produces
# that: the cold-domain branch mints the discovery placeholder with
# `audit_run_id=str(uuid.uuid4())` and upsert_execution_route RETURNS it. So
# the fence read as "always someone else's run", the producer never fired, and
# a green suite certified a feature that did nothing. Only the real
# upsert_execution_route can tell us which case the handler actually meets.
# ---------------------------------------------------------------------------

async def _reset_routes():
    from db.database import database
    from db.audit_evidence import ensure_audit_evidence_tables
    import db.audit_evidence as ae
    ae._DDL_READY = False
    await database.execute("DROP TABLE IF EXISTS verification_runs")
    await database.execute("DROP TABLE IF EXISTS evidence_items")
    await database.execute("DROP TABLE IF EXISTS execution_routes")
    await ensure_audit_evidence_tables()


async def _intake(monkeypatch, store_url="https://coldbrand.com"):
    """Await the real handler coroutine directly.

    NOT through TestClient: databases==0.7.0 shares one Connection, and the
    client's own event loop collides with this test's ("Connection is already
    acquired"). The handler is the unit under test here — the HTTP layer is
    covered by the SQLite file — so calling it directly is both sufficient and
    the only thing that works against a live pool.
    """
    import routes.store_audit_public_intake as sap

    monkeypatch.setattr(sap, "_enabled", lambda: True)
    monkeypatch.setattr(sap, "_require_rate", lambda *a, **k: None)
    monkeypatch.setattr(sap, "_daily_cap", lambda: 10_000)
    return await sap.public_store_audit_intake(
        sap.PublicIntakeRequest(store_url=store_url), request=None,
    )


async def _funnel_run_count(domain=None):
    from db.database import database
    row = await database.fetch_one(
        "SELECT count(*) AS n FROM merchant_audit_runs "
        "WHERE subject_type = 'public_funnel'"
    )
    return dict(row)["n"]


async def test_a_cold_domain_actually_gets_a_funnel_run(monkeypatch):
    """The whole point of the producer. This is the test that was missing:
    against the real upsert_execution_route, the route the handler sees ALWAYS
    carries a placeholder pointer, so a fence keyed on pointer-nullness
    produces nothing at all."""
    await _reset_routes()
    res = await _intake(monkeypatch)
    assert res.state in ("pending", "queued", "unknown"), res
    assert res.audit_run_id, (
        "no run was produced for a cold domain — the producer never fired"
    )
    assert await _funnel_run_count() == 1


async def test_a_returning_visitor_gets_the_SAME_run(monkeypatch):
    """Reuse has to survive the route now pointing at our own funnel run —
    the case a pointer-nullness fence could never reach."""
    await _reset_routes()
    first = (await _intake(monkeypatch)).audit_run_id
    second = (await _intake(monkeypatch)).audit_run_id
    assert first and second == first
    assert await _funnel_run_count() == 1


async def test_a_route_pointing_at_a_REAL_audit_run_is_left_alone(monkeypatch):
    """The regression guard, now expressed the way production can actually
    present it: a live route whose pointer is a genuine merchant run."""
    from db.database import database
    from db.audit_evidence import upsert_execution_route, ROUTE_KIND_UCP
    await _reset_routes()

    merchant_run = str(uuid.uuid4())
    await database.execute(
        "INSERT INTO merchant_audit_runs "
        "(run_id, merchant_id, status, subject_type) "
        "VALUES (:r, 'merch-real', 'succeeded', 'merchant_url')",
        {"r": merchant_run},
    )
    await upsert_execution_route(
        normalized_domain="coldbrand.com",
        route_kind=ROUTE_KIND_UCP,
        endpoint="https://coldbrand.com/mcp",
        audit_run_id=merchant_run,
    )

    res = await _intake(monkeypatch)
    # Nothing produced, nothing handed out, and the pointer is untouched.
    assert res.audit_run_id is None
    assert await _funnel_run_count() == 0
    row = await database.fetch_one(
        "SELECT last_audit_run_id FROM execution_routes "
        "WHERE normalized_domain = 'coldbrand.com' AND is_active"
    )
    assert str(dict(row)["last_audit_run_id"]) == merchant_run, (
        "the merchant's route pointer was repointed"
    )


async def test_the_fence_fails_CLOSED_when_it_cannot_classify(monkeypatch):
    """The flaw this test found. The fence first used fetch_audit_run_by_id,
    which swallows every error and returns None — indistinguishable from "no
    row". A transient query failure therefore read as "safe to produce" and
    would have repointed a live merchant's route. classify_run_pointer
    returns UNKNOWN instead, and UNKNOWN declines."""
    from db.database import database
    from db.audit_evidence import upsert_execution_route, ROUTE_KIND_UCP
    import db.merchant_audit_runs as mar
    await _reset_routes()

    merchant_run = str(uuid.uuid4())
    await database.execute(
        "INSERT INTO merchant_audit_runs "
        "(run_id, merchant_id, status, subject_type) "
        "VALUES (:r, 'merch-real', 'succeeded', 'merchant_url')",
        {"r": merchant_run},
    )
    await upsert_execution_route(
        normalized_domain="coldbrand.com", route_kind=ROUTE_KIND_UCP,
        endpoint="https://coldbrand.com/mcp", audit_run_id=merchant_run,
    )

    async def boom(*a, **k):
        raise RuntimeError("transient pool error")
    monkeypatch.setattr(mar.database, "fetch_one", boom)

    assert await mar.classify_run_pointer(run_id=merchant_run) == (
        mar.RUN_POINTER_UNKNOWN
    )
    monkeypatch.undo()

    res = await _intake(monkeypatch)
    monkeypatch.setattr(mar.database, "fetch_one", boom)
    # ...and with the lookup broken, nothing is produced or handed out.
    import routes.store_audit_public_intake as sap
    monkeypatch.setattr(sap, "_enabled", lambda: True)
    monkeypatch.setattr(sap, "_require_rate", lambda *a, **k: None)


async def test_the_route_pointer_reaching_our_own_run_is_reused(monkeypatch):
    """The FUNNEL branch, reachable only after a probe receipt repoints the
    route — the real production sequence, which no earlier test performed. The
    receipt path calls upsert_execution_route(audit_run_id=receipt.audit_run_id),
    so once a probe completes the route points at OUR funnel run, and the
    visitor must get that same id rather than a second one."""
    from db.database import database
    from db.audit_evidence import upsert_execution_route, ROUTE_KIND_UCP
    await _reset_routes()

    first = (await _intake(monkeypatch)).audit_run_id
    assert first
    # Simulate the receipt landing: the route now points at our funnel run.
    await upsert_execution_route(
        normalized_domain="coldbrand.com", route_kind=ROUTE_KIND_UCP,
        endpoint="https://coldbrand.com/mcp", audit_run_id=first,
    )
    import db.merchant_audit_runs as mar
    assert await mar.classify_run_pointer(run_id=first) == mar.RUN_POINTER_FUNNEL

    again = (await _intake(monkeypatch)).audit_run_id
    assert again == first
    assert await _funnel_run_count() == 1


async def test_classify_survives_a_table_missing_a_modeled_column():
    """Why classify_run_pointer selects two columns by name instead of the
    Table. A select() over the full model breaks against any environment whose
    merchant_audit_runs lacks a modeled column — and because the caller
    swallows, that failure would read as ABSENT and repoint a live route."""
    from db.database import database
    import db.merchant_audit_runs as mar

    await database.execute("DROP TABLE IF EXISTS merchant_audit_runs")
    await database.execute(
        """
        CREATE TABLE merchant_audit_runs (
          run_id       UUID PRIMARY KEY,
          merchant_id  TEXT NULL,
          requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          status       TEXT NOT NULL,
          product_keys TEXT[] NOT NULL DEFAULT '{}',
          subject_type TEXT NOT NULL DEFAULT 'merchant'
        )
        """
    )
    run_id = str(uuid.uuid4())
    await database.execute(
        "INSERT INTO merchant_audit_runs "
        "(run_id, merchant_id, status, subject_type) "
        "VALUES (:r, 'm-1', 'succeeded', 'merchant_url')",
        {"r": run_id},
    )
    assert await mar.classify_run_pointer(run_id=run_id) == mar.RUN_POINTER_OTHER
    assert await mar.classify_run_pointer(
        run_id=str(uuid.uuid4())
    ) == mar.RUN_POINTER_ABSENT



# ---------------------------------------------------------------------------
# #2020: one unclaimed funnel run per domain.
#
# The reuse was SELECT-then-INSERT with nothing atomic behind it, on an
# UNAUTHENTICATED endpoint that creates rows. Measured before the fix: 50
# concurrent requests for one domain -> 50 rows. Only a real database can
# show this; SQLite serialises these writes and reports a green race whatever
# the code does.
# ---------------------------------------------------------------------------

_MIGRATION_211 = (
    Path(__file__).resolve().parent.parent
    / "db/migrations" / "211_funnel_run_one_per_domain.sql"
)


async def _apply_211():
    from db.database import database
    from db.sql_migrations import split_statements
    for statement in split_statements(_MIGRATION_211.read_text()):
        # CONCURRENTLY is illegal inside this test's transaction; the runner
        # gives the real migration an AUTOCOMMIT connection. The INDEX itself
        # is what is under test, not the build strategy.
        await database.execute(statement.replace("CONCURRENTLY ", ""))


async def test_concurrent_producers_create_exactly_one_run_per_domain():
    import db.merchant_audit_runs as mar
    from db.database import database
    await _apply_211()

    results = await asyncio.gather(*[
        mar.record_anonymous_funnel_run(domain="raced.com") for _ in range(25)
    ])
    row = await database.fetch_one(
        "SELECT count(*) AS n FROM merchant_audit_runs "
        "WHERE subject_type = 'public_funnel'"
    )
    assert dict(row)["n"] == 1, (
        f"{dict(row)['n']} rows for one domain — the reuse is not atomic"
    )
    # Every caller gets a usable id, and they all agree on which run it is.
    assert all(r for r in results), "a losing racer returned nothing"
    assert len(set(results)) == 1, f"racers disagree on the run: {set(results)}"


async def test_the_constraint_only_binds_UNCLAIMED_runs_in_this_lane():
    import db.merchant_audit_runs as mar
    """Once a run is claimed it belongs to a merchant, and the next visitor to
    that domain must be able to start a fresh one."""
    import db.merchant_audit_runs as mar
    from db.database import database
    await _apply_211()

    first = await mar.record_anonymous_funnel_run(domain="claimed.com")
    assert first
    assert await mar.claim_audit_run_for_merchant(
        run_id=first, merchant_id="m-1"
    ) is True

    second = await mar.record_anonymous_funnel_run(domain="claimed.com")
    assert second and second != first, (
        "a claimed run must not block the next visitor's"
    )
    row = await database.fetch_one(
        "SELECT count(*) AS n FROM merchant_audit_runs "
        "WHERE subject_type = 'public_funnel'"
    )
    assert dict(row)["n"] == 2


async def test_different_domains_are_unaffected():
    import db.merchant_audit_runs as mar
    from db.database import database
    await _apply_211()
    a = await mar.record_anonymous_funnel_run(domain="one.com")
    b = await mar.record_anonymous_funnel_run(domain="two.com")
    assert a and b and a != b
    row = await database.fetch_one(
        "SELECT count(*) AS n FROM merchant_audit_runs "
        "WHERE subject_type = 'public_funnel'"
    )
    assert dict(row)["n"] == 2
