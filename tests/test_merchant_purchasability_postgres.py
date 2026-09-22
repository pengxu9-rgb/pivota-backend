"""The merchant purchasability gate on the PRODUCTION dialect.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_purchasability_wp6_test \\
        .venv/bin/python -m pytest tests/test_merchant_purchasability_postgres.py

WHAT ONLY POSTGRES CAN SHOW, and why each is here rather than in the SQLite twin:

  * CATALOG PARITY. Production deploys skip db/migrations/, so `db/schema_guard.py` IS the
    production schema and nothing compared the two builds until this file. The migration adds a
    TABLE, and the schema-guard coverage gate inspects `ADD COLUMN` only — so that gate cannot
    see a missing CREATE TABLE self-heal here. This test is what closes that hole.
  * The `market_country ~ '^[A-Z]{2}$'` CHECK, which SQLite cannot express as a regex.
  * `CAST(... AS JSONB)` round-tripping, and the interval arithmetic the TTL window is computed
    with — the two places the dialects' statements genuinely differ.
  * The route, behaviourally, against the schema production actually gets.

THIS MODULE MUST NOT IMPORT `main`. The Postgres gate runs every `test_*_postgres.py` in one
process and `main` triggers a gate-wide `metadata.create_all`, which then builds the MODEL's
shape ahead of the migrations for every suite in the run. The route test therefore mounts one
router on a minimal FastAPI app, and drives it with httpx's ASGITransport — never TestClient,
which hangs against the asyncpg pool.

Everything dialect-agnostic lives in tests/test_merchant_purchasability.py and is imported
wholesale below, so the dialect gate (which collects only `*_postgres.py`) runs those cases too.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason=(
        "needs a Postgres DATABASE_URL — this is the production-dialect gate; see the module "
        "docstring for the one-line setup"
    ),
)

# The dialect-agnostic cases, so the gate runs them on Postgres too. Star-import is deliberate:
# a case added to the SQLite file joins this gate automatically rather than by being remembered.
if _IS_PG:
    from tests.test_merchant_purchasability import *  # noqa: F401,F403,E402
    # The underscore-prefixed names a `import *` skips, imported BY NAME. Three of them are
    # fixtures the shared cases depend on: pytest resolves a fixture by NAME in the collecting
    # module's namespace, so a star-import alone leaves every shared database case erroring with
    # "fixture '_db' not found" — 63 errors that look like a schema problem and are not.
    from tests.test_merchant_purchasability import (  # noqa: E402,F401
        LIVE_CASES,
        TABLE,
        UNVERIFIABLE,
        _db,
        _dial_off,
        _no_real_network,
        _population,
        page,
        res,
    )

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"
_MIGRATION = _MIGRATIONS_DIR / "231_merchant_purchasability.sql"
_DOWN = _MIGRATIONS_DIR / "down" / "231_merchant_purchasability_down.sql"
_MIG_228 = _MIGRATIONS_DIR / "228_tierb_cart_link_eligibility.sql"
_MIG_232 = _MIGRATIONS_DIR / "232_tierb_verdict_vocabulary.sql"

#: The two members migration 232 adds to migration 228's verdict vocabulary.
_WIDENED = ("NO_CARD_PAYMENT", "PRICE_DRIFT")
_TIERB_CHECKS = (
    "tierb_cart_link_eligibility_verdict_check",
    "tierb_cart_link_eligibility_previous_verdict_check",
)

# Same convention as the sibling gates: this file DROPS its table, so it must be INCAPABLE of
# running anywhere but a throwaway — made true, not merely stated.
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "purchasability_wp6")


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(
            f"refusing to drop {TABLE} in database {dbname!r} — throwaway only "
            "(e.g. pivota_purchasability_wp6_test)"
        )


async def _apply(path: Path) -> None:
    from db.database import database
    from db.sql_migrations import split_statements

    for statement in split_statements(path.read_text(encoding="utf-8")):
        await database.execute(statement)


async def _drop() -> None:
    from db.database import database

    await database.execute(f"DROP TABLE IF EXISTS {TABLE}")


@pytest.fixture
async def _migration_db():
    """The table as THE MIGRATION builds it. Distinct from the shared `_db` fixture, which builds
    it the way production does (the self-heal) — the parity test needs both, one at a time."""
    from db.database import database

    _assert_throwaway_database()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    # Drop FIRST, so a constraint deleted from the migration cannot survive via IF NOT EXISTS
    # and leave this gate testing a schema the repo no longer declares.
    await _drop()
    await _apply(_MIGRATION)
    yield
    await _drop()
    if not was_connected and database.is_connected:
        await database.disconnect()


async def _fingerprint():
    """What the DATABASE actually built, read out of the catalog.

    Three parts, because each catches something the others cannot:
      columns  — name, type, nullability, default, length;
      indexes  — `pg_indexes.indexdef`, the DDL the database RECONSTRUCTED. This is the part
                 that catches a UNIQUE silently becoming non-unique or a predicate being
                 dropped; a token search of the source text cannot.
      checks   — `pg_get_constraintdef` for every CHECK, which is where the ISO-2 market
                 vocabulary actually lives.
    """
    from db.database import database

    columns = await database.fetch_all(
        """
        SELECT column_name, data_type, is_nullable, column_default, character_maximum_length
          FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = 'merchant_purchasability'
         ORDER BY column_name
        """
    )
    indexes = await database.fetch_all(
        """
        SELECT indexname, indexdef FROM pg_indexes
         WHERE schemaname = 'public' AND tablename = 'merchant_purchasability'
         ORDER BY indexname
        """
    )
    checks = await database.fetch_all(
        """
        SELECT c.conname, pg_get_constraintdef(c.oid) AS def
          FROM pg_constraint c
          JOIN pg_class t ON t.oid = c.conrelid
          JOIN pg_namespace n ON n.oid = t.relnamespace
         WHERE n.nspname = 'public' AND c.contype = 'c'
           AND t.relname = 'merchant_purchasability'
         ORDER BY c.conname
        """
    )

    def _norm(text):
        return " ".join((text or "").split())

    return (
        [tuple(dict(r).values()) for r in columns],
        {r["indexname"]: _norm(r["indexdef"]) for r in indexes},
        [(r["conname"], _norm(r["def"])) for r in checks],
    )


# ══ catalog parity ═════════════════════════════════════════════════════════════════════════


async def test_the_self_heal_builds_the_231_catalog_the_migration_builds(_migration_db):
    """Production never runs db/migrations, so the self-heal IS the production schema. Built
    sequentially in this one throwaway database rather than in two: CREATE DATABASE needs a
    privilege the CI role may not have, and dropping between the two builds gives the same
    comparison. Both halves start from the same empty state."""
    from db.schema_guard import ensure_required_schema_light

    from_migration = await _fingerprint()
    columns_m, indexes_m, checks_m = from_migration
    assert [c[0] for c in columns_m] == [
        "card_available", "checked_at", "consecutive_failures", "evidence",
        "expected_price_minor", "landed_currency", "landed_price_minor", "market_country",
        "merchant_domain", "payment_methods", "positive_until", "price_drift_minor",
        "vantage", "variant_id", "verdict",
    ], "precondition: the migration built the columns this rail reads"
    assert set(indexes_m) == {"merchant_purchasability_pkey", "idx_merchant_purchasability_due"}
    assert "ck_merchant_purchasability_market" in dict(checks_m)

    await _drop()
    await ensure_required_schema_light()
    columns_s, indexes_s, checks_s = await _fingerprint()

    assert columns_s == columns_m, "columns differ between the self-heal and the migration"
    assert set(indexes_s) == set(indexes_m), (
        f"index NAMES differ: only in self-heal {sorted(set(indexes_s) - set(indexes_m))}, "
        f"only in migration {sorted(set(indexes_m) - set(indexes_s))}"
    )
    for name in sorted(indexes_m):
        assert indexes_s[name] == indexes_m[name], (
            f"{name} is built differently by the self-heal:\n"
            f"  migration: {indexes_m[name]}\n  self-heal: {indexes_s[name]}"
        )
    assert checks_s == checks_m, (
        f"CHECK constraints differ:\n  migration: {checks_m}\n  self-heal: {checks_s}"
    )


async def test_the_self_heal_is_idempotent_and_leaves_the_rail_usable(_migration_db):
    from db.schema_guard import ensure_required_schema_light
    import db.merchant_purchasability as mp

    await ensure_required_schema_light()
    once = await _fingerprint()
    await ensure_required_schema_light()
    await ensure_required_schema_light()
    assert await _fingerprint() == once

    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True))
    assert await mp.is_purchasable("judydoll.com", "US") is True


async def test_the_migration_reapplies_over_a_self_healed_database(_migration_db):
    from db.schema_guard import ensure_required_schema_light

    before = await _fingerprint()
    await ensure_required_schema_light()
    await _apply(_MIGRATION)
    assert await _fingerprint() == before


async def test_the_down_migration_removes_the_table(_migration_db):
    from db.database import database

    await _apply(_DOWN)
    row = await database.fetch_one(
        "SELECT to_regclass('public.merchant_purchasability') AS present"
    )
    assert row["present"] is None
    await _apply(_MIGRATION)  # leave the fixture's teardown something to drop


# ══ what only the production dialect enforces ══════════════════════════════════════════════


async def test_the_market_check_refuses_anything_that_is_not_iso2_upper(_migration_db):
    """A regex CHECK SQLite cannot express. The module normalises before writing, so this is the
    BACKSTOP — it is what stops a future writer that forgets."""
    import asyncpg
    from db.database import database

    # TWO different refusals, named separately rather than caught together, because they are two
    # different guarantees: `USA` is stopped by the COLUMN WIDTH (VARCHAR(2)) before any CHECK
    # runs, while `us`, `U`, `1S` and `''` are the right length (or shorter) and are stopped by
    # the regex. A test that accepted either for every value would still pass with the CHECK
    # deleted, because the width alone would carry the three-letter case.
    for bad in ("us", "U", "1S", "", "u1"):
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await database.execute(
                "INSERT INTO merchant_purchasability (merchant_domain, market_country, vantage) "
                "VALUES (:d, :m, 'worker')",
                {"d": "x.com", "m": bad},
            )
    with pytest.raises(asyncpg.exceptions.StringDataRightTruncationError):
        await database.execute(
            "INSERT INTO merchant_purchasability (merchant_domain, market_country, vantage) "
            "VALUES ('x.com', 'USA', 'worker')"
        )


async def test_the_primary_key_is_domain_market_vantage(_migration_db):
    """Two vantages for one merchant x market are TWO rows; a second write to the same vantage
    is an upsert, not a duplicate."""
    import db.merchant_purchasability as mp
    from db.database import database

    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True), vantage="worker")
    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True), vantage="proxy")
    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True), vantage="worker")
    row = await database.fetch_one(
        "SELECT COUNT(*) AS n FROM merchant_purchasability WHERE merchant_domain = 'judydoll.com'"
    )
    assert int(row["n"]) == 2


async def test_the_jsonb_columns_are_really_jsonb(_migration_db):
    """`CAST(:x AS JSONB)` is one of the two places the two dialects' statements differ. If the
    column were TEXT the cast would still 'work' and the evidence would stop being queryable."""
    import db.merchant_purchasability as mp
    from db.database import database

    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True))
    row = await database.fetch_one(
        """
        SELECT payment_methods -> 0 AS first_method,
               evidence ->> 'verdict' AS verdict
          FROM merchant_purchasability WHERE merchant_domain = 'judydoll.com'
        """
    )
    assert row["verdict"] == "ELIGIBLE"
    assert row["first_method"] is not None


async def test_the_ttl_window_is_computed_by_the_server(_migration_db, monkeypatch):
    """A naive client datetime binds as LOCAL wall time under asyncpg, so a window computed in
    Python would be wrong by the machine's UTC offset. Both ends come from the server here."""
    import db.merchant_purchasability as mp

    monkeypatch.setenv("MERCHANT_PURCHASABILITY_TTL_HOURS", "5")
    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True))
    fact = await mp.get_fact("judydoll.com", "US")
    delta = fact["positive_until"] - fact["checked_at"]
    assert abs(delta.total_seconds() - 5 * 3600) < 60, delta
    assert fact["checked_at"].tzinfo is not None, "timestamps come back tz-aware"


# ══ the ops route, on a MINIMAL app (never `main`) ══════════════════════════════════════════


@pytest.fixture
def _app():
    """One router on a bare FastAPI app. Importing `main` here would run a gate-wide
    `metadata.create_all` and build the MODEL's schema ahead of the migrations for every suite in
    the Postgres gate's single process."""
    from fastapi import FastAPI

    from routes.merchant_purchasability_ops import router
    from utils.gateway_oidc_auth import require_admin_or_gateway_identity

    app = FastAPI()
    app.include_router(router)
    # The route's dependency since the OIDC follow-up. FastAPI keys overrides on the EXACT
    # callable the route declares, so an override of `require_admin` would silently stop
    # applying and every case here would 403 on a missing Authorization header — a failure that
    # reads as a schema problem in this file and is not one. The auth dependency itself has no
    # database in it and is tested dialect-free in tests/test_ops_gateway_oidc.py.
    app.dependency_overrides[require_admin_or_gateway_identity] = lambda: {"role": "admin"}
    return app


async def _get(app, url):
    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.get(url)


async def test_the_ops_route_reports_the_tier_the_door_would_serve(_migration_db, _app):
    import db.merchant_purchasability as mp

    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True))
    response = await _get(_app, "/ops/merchant-purchasability?domain=judydoll.com&market=US")
    assert response.status_code == 200
    body = response.json()
    assert body["tier"] == "purchase"
    assert body["buyer_vantage"] == "worker"
    assert len(body["facts"]) == 1
    assert body["facts"][0]["card_available"] is True
    assert "PAYPAL_EXPRESS" in body["facts"][0]["payment_methods"]
    assert body["facts"][0]["evidence"]["verdict"] == "ELIGIBLE"


async def test_the_ops_route_says_browse_only_for_a_merchant_with_no_fact(_migration_db, _app):
    response = await _get(_app, "/ops/merchant-purchasability?domain=never-seen.com&market=US")
    assert response.status_code == 200
    body = response.json()
    assert body["tier"] == "browse_only" and body["facts"] == []
    assert "never been checked" in body["note"]


async def test_the_ops_route_shows_a_wrong_vantage_fact_as_browse_only(_migration_db, _app):
    """The trap the vantage column exists for: a reader who looks only at `card_available` sees
    True and concludes the merchant is purchasable. `tier` is the answer."""
    import db.merchant_purchasability as mp

    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True), vantage="proxy")
    body = (await _get(_app, "/ops/merchant-purchasability?domain=judydoll.com&market=US")).json()
    assert body["facts"][0]["card_available"] is True
    assert body["facts"][0]["positive_now"] is False
    assert body["tier"] == "browse_only"
    assert "buyer vantage" in body["note"]


async def test_the_ops_route_normalises_the_key_the_way_the_writer_does(_migration_db, _app):
    """An operator must not be shown a different row than the door reads."""
    import db.merchant_purchasability as mp

    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True))
    body = (await _get(_app, "/ops/merchant-purchasability?domain=WWW.JudyDoll.com&market=us")).json()
    assert body["domain"] == "judydoll.com" and body["market"] == "US"
    assert body["tier"] == "purchase"


async def test_the_ops_route_carries_no_buyer_data(_migration_db, _app):
    import db.merchant_purchasability as mp

    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True))
    text = (await _get(_app, "/ops/merchant-purchasability?domain=judydoll.com&market=US")).text
    for forbidden in ("<html", "<script", "email", "address1", "postal", "phone"):
        assert forbidden not in text.lower(), forbidden


async def test_the_ops_route_reports_the_gate_state_but_is_not_gated_by_it(_migration_db, _app, monkeypatch):
    """A route that went dark with a dial would be unreadable at exactly the moment an operator
    needs it — before arming. `enforced` is also the gateway's contract: it must be able to tell
    "this merchant is browse_only" from "the backend is not enforcing yet"."""
    import db.merchant_purchasability as mp

    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True))
    monkeypatch.delenv("MERCHANT_PURCHASABILITY_ENFORCE", raising=False)
    body = (await _get(_app, "/ops/merchant-purchasability?domain=judydoll.com&market=US")).json()
    assert body["enforced"] is False
    assert body["facts"], "the facts are still readable with the gate off"
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_ENFORCE", "1")
    body = (await _get(_app, "/ops/merchant-purchasability?domain=judydoll.com&market=US")).json()
    assert body["enforced"] is True


# ══ migration 232: the widened Tier B verdict vocabulary IS self-healed ════════════════════
#
# F8. The self-heal's CREATE TABLE already carries the wide list, which is the whole heal on a
# FRESH database — and NOTHING on a database that already holds a 228-shaped table, because
# `CREATE TABLE IF NOT EXISTS` does nothing there. Production is exactly that database. Measured:
# before the fix, healing a 228-only database left the narrow CHECK and the first
# NO_CARD_PAYMENT write raised.


async def _tierb_check_defs():
    from db.database import database

    rows = await database.fetch_all(
        """
        SELECT c.conname, pg_get_constraintdef(c.oid) AS def
          FROM pg_constraint c
          JOIN pg_class t ON t.oid = c.conrelid
         WHERE c.contype = 'c' AND t.relname = 'tierb_cart_link_eligibility'
         ORDER BY c.conname
        """
    )
    return {r["conname"]: " ".join(r["def"].split()) for r in rows}


@pytest.fixture
async def _tierb_228():
    """A database holding migration 228's table and NOT 232 — i.e. production's shape."""
    from db.database import database

    _assert_throwaway_database()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await database.execute("DROP TABLE IF EXISTS tierb_cart_link_eligibility")
    await _apply(_MIG_228)
    yield
    await database.execute("DROP TABLE IF EXISTS tierb_cart_link_eligibility")
    if not was_connected and database.is_connected:
        await database.disconnect()


async def test_a_228_only_database_really_does_refuse_the_new_verdicts(_tierb_228):
    """THE PRECONDITION, measured rather than asserted in prose. If this ever stops failing, the
    heal below is testing nothing."""
    import asyncpg
    from db.database import database

    defs = await _tierb_check_defs()
    for name in _TIERB_CHECKS:
        assert name in defs, f"precondition: 228 built {name}"
        for verdict in _WIDENED:
            assert verdict not in defs[name], f"precondition: 228 does not admit {verdict}"

    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await database.execute(
            "INSERT INTO tierb_cart_link_eligibility (shop_domain, market, verdict, checked_at) "
            "VALUES ('x.com', 'US', 'NO_CARD_PAYMENT', now())"
        )


async def test_the_self_heal_widens_the_verdict_check_on_a_228_shaped_database(_tierb_228):
    """The heal, on the database production actually has."""
    from db.database import database
    from db.schema_guard import ensure_required_schema_light

    await ensure_required_schema_light()

    defs = await _tierb_check_defs()
    for name in _TIERB_CHECKS:
        for verdict in _WIDENED:
            assert verdict in defs[name], f"{name} still refuses {verdict} after the heal"

    # Behaviourally, not just in the catalog.
    for verdict in _WIDENED:
        await database.execute(
            "INSERT INTO tierb_cart_link_eligibility (shop_domain, market, verdict, checked_at) "
            "VALUES (:d, 'US', :v, now())",
            {"d": f"{verdict.lower()}.com", "v": verdict},
        )


async def test_the_232_self_heal_builds_the_checks_the_migration_builds(_tierb_228):
    """Catalog parity for 232, the same comparison the 231 test makes: migration-built vs
    self-heal-built, from the same starting state."""
    from db.database import database
    from db.schema_guard import ensure_required_schema_light

    await _apply(_MIG_232)
    from_migration = await _tierb_check_defs()

    await database.execute("DROP TABLE IF EXISTS tierb_cart_link_eligibility")
    await _apply(_MIG_228)
    await ensure_required_schema_light()
    from_self_heal = await _tierb_check_defs()

    for name in _TIERB_CHECKS:
        assert from_self_heal[name] == from_migration[name], (
            f"{name} differs:\n  migration: {from_migration[name]}\n"
            f"  self-heal: {from_self_heal[name]}"
        )


async def test_the_232_heal_is_idempotent(_tierb_228):
    from db.schema_guard import ensure_required_schema_light

    await ensure_required_schema_light()
    once = await _tierb_check_defs()
    await ensure_required_schema_light()
    await ensure_required_schema_light()
    assert await _tierb_check_defs() == once


async def test_the_migration_reapplies_over_a_self_healed_tierb_table(_tierb_228):
    from db.schema_guard import ensure_required_schema_light

    await ensure_required_schema_light()
    before = await _tierb_check_defs()
    await _apply(_MIG_232)
    assert await _tierb_check_defs() == before


async def test_a_fresh_database_gets_the_wide_vocabulary_without_the_migration():
    """The other arrival path: a database with NO tierb table at all is built by the self-heal's
    CREATE TABLE, whose list is already wide."""
    from db.database import database
    from db.schema_guard import ensure_required_schema_light

    _assert_throwaway_database()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    try:
        await database.execute("DROP TABLE IF EXISTS tierb_cart_link_eligibility")
        await ensure_required_schema_light()
        defs = await _tierb_check_defs()
        for name in _TIERB_CHECKS:
            for verdict in _WIDENED:
                assert verdict in defs[name]
    finally:
        await database.execute("DROP TABLE IF EXISTS tierb_cart_link_eligibility")
        if not was_connected and database.is_connected:
            await database.disconnect()


async def test_the_ops_route_carries_the_gateway_contract_fields(_migration_db, _app, monkeypatch):
    """`enforced` is not decoration: with enforcement off EVERY merchant reads browse_only, so a
    gateway acting on `tier` alone would take the whole catalogue browse-only on day one."""
    import db.merchant_purchasability as mp

    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True))
    monkeypatch.delenv("MERCHANT_PURCHASABILITY_ENFORCE", raising=False)
    monkeypatch.delenv("MERCHANT_PURCHASABILITY_SWEEP_ENABLED", raising=False)
    body = (await _get(_app, "/ops/merchant-purchasability?domain=judydoll.com&market=US")).json()
    assert body["enforced"] is False and body["sweep_enabled"] is False
    assert body["tier"] == "purchase", "the FACT is reported regardless of the dials"

    monkeypatch.setenv("MERCHANT_PURCHASABILITY_ENFORCE", "1")
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_SWEEP_ENABLED", "1")
    body = (await _get(_app, "/ops/merchant-purchasability?domain=judydoll.com&market=US")).json()
    assert body["enforced"] is True and body["sweep_enabled"] is True


async def test_a_merchant_with_no_fact_is_browse_only_but_not_enforced(_migration_db, _app, monkeypatch):
    """The exact day-one state the gateway must NOT act on."""
    monkeypatch.delenv("MERCHANT_PURCHASABILITY_ENFORCE", raising=False)
    body = (await _get(_app, "/ops/merchant-purchasability?domain=never-seen.com&market=US")).json()
    assert body["tier"] == "browse_only" and body["enforced"] is False
