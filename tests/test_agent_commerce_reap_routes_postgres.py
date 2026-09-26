"""routes/agent_commerce_reap.py against REAL Postgres — the half SQLite structurally cannot test.

Picked up automatically by .github/workflows/postgres-dialect-gate.yml via the
`tests/test_*_postgres.py` glob.

NOT A COPY OF THE SQLITE ARM, and it does not import it — the house rule is that a
`*_postgres.py` file stands alone, because a shared helper module means the two gates test one
thing twice instead of two things once. What is here is what the ENGINE decides:

  1. MIGRATIONS 226 + 227 AND THE SELF-HEAL BUILD THE SAME SCHEMA. Production skips
     db/migrations, so `db/schema_guard.ensure_required_schema_light` IS the production schema
     for these three tables — including 227's two consent columns, which the self-heal reaches
     by its own statement rather than by being folded into the CREATE (folded in, it would be
     dead on a fresh database and its deletion would pass every test). The comparison is through the CATALOG — `information_schema.columns`,
     `pg_indexes.indexdef`, `pg_get_constraintdef` — not through the source text, because a
     `CREATE UNIQUE INDEX` quietly downgraded to `CREATE INDEX` is invisible to a token check and
     a reviewer's mutant proved exactly that on the mig-224 pair.

  2. THE PRICE COMES BACK AS A `Decimal`, NOT A FLOAT. `catalog_offers.merchant_effective_price`
     is `numeric`, this route reads it with raw SQL so no result processor runs, and asyncpg
     hands back a `Decimal` where SQLite hands back a binary float. `major_to_minor` refuses a
     float outright. The `CAST(... AS TEXT)` in the offer query is what makes one code path
     correct on both, and only this file can prove the Postgres half of that.

  3. THE OWNERSHIP CONJUNCT OVER REAL SQL, with a real `VARCHAR` comparison and a real index.

  4. THE `market_country` CHECK IS A REGEX HERE. The SQLite twin spells the same rule with
     `length()` and `upper()`; only Postgres can refuse `usa` at the storage layer.

  5. EVERY SECURITY-RELEVANT REFUSAL, RE-RUN. This is the one place the file overlaps the SQLite
     arm on purpose, and the reason is the mutant table: a guard that dies on SQLite and survives
     here has only been shown to hold on the dialect production does not use. So the ownership
     conjuncts, the dial, the allowlist, the buyer link, the hosted-URL vetting, the price and
     the return URL are all exercised against real Postgres. Nothing else is duplicated.

  6. WP4b'S MINT AGAINST THE REAL CONSTRAINT. `ON CONFLICT (agent_id, agent_user_ref_hash) DO
     NOTHING` is Postgres' implementation over a real unique index here, not SQLite's; the
     consent column is a real `VARCHAR(32)` that raises on an over-long value rather than
     truncating; and `consented_at` is a real `timestamptz`.

THIS FILE DOES NOT IMPORT `main`. It assembles a minimal app from the router plus
`ErrorHandlerMiddleware`; see the long comment above the assert below for what importing main did
to two unrelated files in this gate. "The router is registered in main.py" is proved in the SQLite
arm, which never shares a process with the gate.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_reap_wp4b_test \\
        .venv/bin/python -m pytest tests/test_agent_commerce_reap_routes_postgres.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason=(
        "needs a Postgres DATABASE_URL — this is the production-dialect gate; "
        "see the module docstring for the one-line setup"
    ),
)

_MIGRATIONS_DIR = REPO_ROOT / "db/migrations"

#: EVERY migration this rail owns, IN ORDER. 226's tables stand alone, but the route writes a
#: purchase through the ledger, so 224 and 225 have to be here or every POST fails on an
#: UndefinedTable and the gate would be reporting on a schema half of which does not exist. The
#: list is explicit rather than a glob so the next migration on some other table cannot silently
#: join this fixture.
_MIGRATIONS = (
    _MIGRATIONS_DIR / "224_reap_agentic_ledger.sql",
    _MIGRATIONS_DIR / "225_reap_agentic_purchase_hints.sql",
    _MIGRATIONS_DIR / "226_reap_agentic_routes.sql",
    # 227 adds the two consent columns to 226's buyer_refs table. In the list because the parity
    # test below compares what the MIGRATIONS built against what the SELF-HEAL built, and the
    # self-heal carries 227 — without it here, parity would fail for a change that is correct.
    _MIGRATIONS_DIR / "227_reap_agentic_buyer_consent.sql",
    _MIGRATIONS_DIR / "228_tierb_cart_link_eligibility.sql",
    _MIGRATIONS_DIR / "229_reap_agentic_purchase_item_source.sql",
    _MIGRATIONS_DIR / "230_conversion_click_claims.sql",
    # 231 is the purchasability fact table. Built here because the canonical-domain cases below
    # drive the route's `is_purchasable` read against the sweep's own writer; it is dropped and
    # rebuilt with the rail's tables (see `_RAIL_TABLES`).
    _MIGRATIONS_DIR / "231_merchant_purchasability.sql",
    # 232 widens 228's verdict vocabulary in place. Same reason as 227 above: this file DROPS
    # tierb_cart_link_eligibility and rebuilds it from this list, and the self-heal carries 232,
    # so a migration build without it is not the schema production has. 226's parity fingerprint
    # does not span that table today — but a build that disagrees with the self-heal is a trap
    # waiting for whoever widens the fingerprint.
    _MIGRATIONS_DIR / "232_tierb_verdict_vocabulary.sql",
    # 233 adds consent_version + consented_at to reap_agentic_purchases. The self-heal carries
    # it, so a migration build without it is not the schema production has — and every POST this gate makes
    # would fail on an UndefinedColumn. See
    # feedback_a_later_migration_that_alters_a_table_breaks_that_tables_own_parity_test.
    _MIGRATIONS_DIR / "233_reap_agentic_purchase_consent.sql",
)

#: Same convention as the ledger's gate: this file DROPS its tables, so it must be INCAPABLE of
#: running anywhere but a throwaway — made true, not merely stated.
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")

#: Only 226's tables. The ledger's own parity test owns 224/225's, and a fingerprint spanning
#: both would fail here for a change nothing in this PR made.
_TABLES = (
    "reap_agentic_eligibility",
    "reap_agentic_buyer_refs",
    "reap_agentic_purchase_keys",
)
_RAIL_TABLES = _TABLES + (
    "reap_agentic_purchases", "reap_agentic_enrollments",
    "tierb_cart_link_eligibility", "conversion_click_claims",
    "merchant_purchasability",
)

BASE = "/agent/v2/commerce/reap"

AGENT = "agent_reap_pg"
OTHER_AGENT = "agent_reap_pg_other"
USER_REF = "pg-user-ada"
OTHER_USER_REF = "pg-user-grace"
BUYER_ID = "buy_pg_ada"
OTHER_BUYER_ID = "buy_pg_grace"

DOMAIN = "brand-pg.example"
PRODUCT_KEY = "prod::m_pg::shopify::2001"
SKU_KEY = "sku::prod::m_pg::shopify::2001::v1"

EMAIL = "ada-pg@example.test"
ADDRESS = {
    "firstName": "Ada",
    "lastName": "Lovelace",
    "phone": "+15550100",
    "addressLine1": "900 Brannan St",
    "city": "San Francisco",
    "country": "US",
    "postalCode": "94103",
}
PII_STRINGS = ("ada-pg@example.test", "900 Brannan St", "Lovelace", "+15550100")

from db.database import database, metadata  # noqa: E402
from db.buyer_vault import hash_agent_user_ref  # noqa: E402
import db.reap_agentic_ledger as ledger  # noqa: E402
import services.reap_agentic_purchase as svc  # noqa: E402
from routes.agent_auth import get_agent_context  # noqa: E402
from routes.agent_user_auth import AgentUserContext, get_agent_user_context  # noqa: E402

# ── THIS MODULE MUST NEVER IMPORT `main`, AND THE ASSERT BELOW IS LOAD-BEARING ───────────────
#
# The Postgres dialect gate runs EVERY `tests/test_*_postgres.py` in ONE pytest process against
# ONE database, and pytest imports every module at COLLECTION — before any test runs. So an
# import in this file is an import for the whole gate.
#
# `from main import app` took the shared `db.database.metadata` from 1 table to 135 (measured),
# because importing main imports every route and db module and each one registers its SQLAlchemy
# `Table` on that shared metadata. One of them is `db/audit_evidence.verification_runs`, whose
# MODEL declares `created_at` NOT NULL with **no server default** while its DDL owner,
# `ensure_audit_evidence_tables()`, creates the same table WITH the default.
#
# `tests/test_a9_4_barekey_guard_postgres.py` then calls `metadata.create_all(engine,
# checkfirst=True)` — every registered table — so `verification_runs` got built from the MODEL
# first, the DDL owner's `IF NOT EXISTS` became a no-op, and three inserts in
# `tests/test_bind_parameter_types_postgres.py` that legitimately omit `created_at` failed with
# `NotNullViolationError`. This file was the only gate module importing main, and it broke two
# other files that have nothing to do with it. Locally it is invisible: the two files pass in
# isolation and in pairs, and only fail with a9_4 collected in between.
#
# So: a MINIMAL app, assembled from the router under test plus the one middleware whose output
# this file asserts on. The cost is that "the router is registered in main.py" is not provable
# here — that test lives in the SQLite arm
# (`test_the_router_is_mounted_on_the_real_app`), which never shares a process with this gate.
from fastapi import FastAPI  # noqa: E402
from middleware.error_handler import ErrorHandlerMiddleware  # noqa: E402
import routes.agent_commerce_reap as routes_reap  # noqa: E402

assert "main" not in sys.modules, (
    "tests/test_agent_commerce_reap_routes_postgres.py imported `main`, directly or through one "
    "of its imports. The Postgres gate shares one process and one database, so main's model "
    "registrations poison `metadata.create_all` for every gate file collected after this one — "
    "see the comment above. Keep this module's import graph to the router and its dependencies."
)

#: THE MIDDLEWARE IS NOT DECORATION. Every refusal this file asserts on is read through
#: `_error()`, which reads `detail.error` — and `detail` is where `ErrorHandlerMiddleware` puts
#: the route's own body when it re-wraps a 4xx. Without it, this gate would be asserting against a
#: response shape production never returns.
app = FastAPI()
app.add_middleware(ErrorHandlerMiddleware)
app.include_router(routes_reap.router)

_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(
            f"refusing to drop the agentic route tables in database {dbname!r} — "
            f"throwaway only (e.g. pivota_reap_wp4b_test)"
        )


async def _apply_migrations():
    from db.sql_migrations import split_statements

    for path in _MIGRATIONS:
        for statement in split_statements(path.read_text(encoding="utf-8")):
            await database.execute(statement)


async def _drop_tables():
    for table in _RAIL_TABLES:
        await database.execute(f"DROP TABLE IF EXISTS {table} CASCADE")


async def _build_catalog_tables():
    """The catalog's real definitions, from the repo's own `metadata`. A hand-written fixture
    would let a renamed column pass here and fail in production."""
    import sqlalchemy
    from db.buyer_vault import buyer_identity_links
    from db.catalog import catalog_merchants, catalog_offers, catalog_products, catalog_skus
    from db.commerce_attribution import surface_click_events

    url = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
    engine = sqlalchemy.create_engine(url)
    metadata.create_all(
        engine,
        tables=[
            catalog_products,
            catalog_skus,
            catalog_offers,
            catalog_merchants,
            buyer_identity_links,
            surface_click_events,
        ],
        checkfirst=True,
    )
    engine.dispose()
    await _clear_shared_tables()


#: EVERY table this gate writes that it does NOT drop, in FK-safe order (offers -> skus ->
#: products). These are SHARED with suites that neither create nor drop them, which is what makes
#: leaving a row behind their problem rather than ours.
#:
#: WHY THIS IS A NAMED LIST AND NOT AN INLINE LOOP IN THE FIXTURE'S SETUP. It used to be exactly
#: that, and it ran at SETUP ONLY — so the LAST test in this file left its rows in place for
#: whatever ran next. The Postgres dialect gate runs the whole `tests/test_*_postgres.py` glob in
#: ONE process against ONE database, in alphabetical order, and the file after this one is
#: `tests/test_backfill_variant_identity_skus_postgres.py`, which opens with
#: `assert count(*) FROM catalog_skus == 0`. It failed four tests. Measured on a fresh database:
#: this suite alone left 1 row each in catalog_skus, catalog_products, catalog_offers and
#: buyer_identity_links.
#:
#: The rule this encodes: A SUITE THAT WRITES A SHARED TABLE MUST LEAVE IT AS IT FOUND IT,
#: whichever of its tests ran last. Setup-only cleaning protects THIS file from the previous one
#: and protects nobody from this one.
_SHARED_TABLES = (
    "catalog_offers",
    "catalog_skus",
    "catalog_products",
    "catalog_merchants",
    "buyer_identity_links",
    "surface_click_events",
)

#: The rail's own tables. This gate DROPs and rebuilds them at setup, so cleaning them again at
#: teardown is not what keeps this file correct — it is what keeps the NEXT file correct if it
#: reads one without dropping it first, and it costs a DELETE on an empty table.
_TEARDOWN_TABLES = _SHARED_TABLES + tuple(reversed(_RAIL_TABLES))


async def _clear_shared_tables(tables=_SHARED_TABLES) -> None:
    """DELETE every row this gate could have written, in FK-safe order.

    ONE try PER TABLE. A table that does not exist — because a DROP/rebuild failed, or because
    the teardown is running after a test that dropped one — must not abandon the DELETEs after
    it, which is the same argument db/schema_guard.py makes for its per-statement try blocks.
    Leaving four tables dirty because the first was missing is how a cleanup becomes a no-op.
    """
    for table in tables:
        try:
            await database.execute(f"DELETE FROM {table}")
        except Exception:  # noqa: BLE001 — see the docstring
            continue


@pytest.fixture(autouse=True)
async def _db():
    _assert_throwaway_database()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    # Drop first, so a constraint deleted from a migration cannot survive via IF NOT EXISTS and
    # leave this gate testing a schema the repo no longer declares.
    await _drop_tables()
    await _apply_migrations()
    await _build_catalog_tables()
    try:
        yield
    finally:
        # TEARDOWN, AND IT IS THE HALF THAT WAS MISSING. Setup-only cleaning leaves the LAST
        # test's rows for whatever the gate runs next — see `_SHARED_TABLES` for the four tests
        # it broke in tests/test_backfill_variant_identity_skus_postgres.py.
        #
        # In a `finally`, so a test that FAILS or errors still cleans up: a failing test is
        # exactly the one most likely to have left a half-written row behind, and a cleanup that
        # only runs on success turns one red test into a cascade in another file.
        await _clear_shared_tables(_TEARDOWN_TABLES)
        if not was_connected and database.is_connected:
            await database.disconnect()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("REAP_AGENTIC_ENABLED", "1")
    monkeypatch.setenv("REAP_API_BASE_URL", "https://sandbox.api.reap.global")
    monkeypatch.setenv("REAP_API_KEY", "sk_test_key")
    monkeypatch.delenv("REAP_RETURN_URL_HOSTS", raising=False)
    monkeypatch.delenv("REAP_AGENTIC_RETURN_URL", raising=False)
    monkeypatch.delenv("BUYER_IDENTITY_LINK_SECRET", raising=False)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """These routes make NO partner call, and this is the control rather than the claim."""

    class _NetworkForbidden:
        def __init__(self, *a, **k):
            raise AssertionError("a route reached the network; this rail's routes make no call")

    monkeypatch.setattr(httpx, "AsyncClient", _NetworkForbidden, raising=True)


class _Caller:
    agent_id = AGENT
    agent_user_ref: Optional[str] = USER_REF
    agent_name = "Reap Routes PG"
    allowed_merchants = None
    session_id = "session_reap_pg"

    def can_access_merchant(self, merchant_id: str) -> bool:
        return True


CALLER = _Caller()


@pytest.fixture
def client():
    CALLER.agent_id = AGENT
    CALLER.agent_user_ref = USER_REF

    async def _agent():
        return CALLER

    def _user():
        ref = CALLER.agent_user_ref
        return AgentUserContext(agent_user_ref=ref) if ref else None

    app.dependency_overrides[get_agent_context] = _agent
    app.dependency_overrides[get_agent_user_context] = _user

    class _Harness:
        async def request(self, method, url, **kwargs):
            transport = httpx.ASGITransport(app=app)
            async with _REAL_ASYNC_CLIENT(transport=transport, base_url="http://test") as http:
                return await http.request(method, url, **kwargs)

        async def post(self, url, **kwargs):
            return await self.request("POST", url, **kwargs)

        async def get(self, url, **kwargs):
            return await self.request("GET", url, **kwargs)

    try:
        yield _Harness()
    finally:
        app.dependency_overrides.pop(get_agent_context, None)
        app.dependency_overrides.pop(get_agent_user_context, None)


def _error(resp) -> Optional[str]:
    """The refusal reason out of the app-wide error envelope — see the route module's
    `_REFUSAL_STATUS` comment for why this app cannot emit a 422 and what it does to a 4xx body."""
    payload = resp.json()
    detail = payload.get("detail")
    if isinstance(detail, dict):
        return detail.get("error")
    error = payload.get("error")
    if isinstance(error, dict) and isinstance(error.get("details"), dict):
        return error["details"].get("error")
    return None


#: The consent tag every well-formed POST carries since WP4b.
CONSENT = "reap-agentic-v1"


def _buyer(**over) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "email": EMAIL,
        "shipping_address": dict(ADDRESS),
        "consent_version": CONSENT,
    }
    payload.update(over)
    return payload


def _body(**over) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "merchant_domain": DOMAIN,
        "product_key": PRODUCT_KEY,
        "variant_key": SKU_KEY,
        "quantity": 1,
        "buyer": _buyer(),
    }
    payload.update(over)
    return payload


async def _seed_catalog(
    *,
    price: str = "42.50",
    platform: str = "shopify",
    domain: str = DOMAIN,
    currency: str = "USD",
    priced: bool = True,
):
    await database.execute(
        """
        INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id,
                                      title, brand, category, product_type, source_domain)
        VALUES (:pk, 'm_pg', :platform, '2001', 'Standard Eau de Parfum', 'Brand',
                'fragrance', 'Fragrance', :domain)
        """,
        {"pk": PRODUCT_KEY, "platform": platform, "domain": domain},
    )
    await database.execute(
        """
        INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform,
                                  source_product_id, source_variant_id, title, currency)
        VALUES (:sk, :pk, 'm_pg', :platform, '2001', 'v1', 'Standard', :currency)
        """,
        {"sk": SKU_KEY, "pk": PRODUCT_KEY, "platform": platform, "currency": currency},
    )
    if not priced:
        return
    await database.execute(
        """
        INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id,
                                    currency, merchant_effective_price)
        VALUES ('off_pg_1', :sk, :pk, 'm_pg', :currency, :price)
        """,
        {"sk": SKU_KEY, "pk": PRODUCT_KEY, "price": price, "currency": currency},
    )


async def _seed_eligibility(*, domain: str = DOMAIN, market: str = "US", enabled: bool = True):
    await database.execute(
        """
        INSERT INTO reap_agentic_eligibility (merchant_domain, product_key, variant_key,
                                              market_country, enabled)
        VALUES (:d, '', '', :m, :enabled)
        """,
        {"d": domain, "m": market, "enabled": enabled},
    )


async def _seed_link(*, agent_id: str = AGENT, ref: str = USER_REF, buyer_id: str = BUYER_ID):
    await database.execute(
        """
        INSERT INTO buyer_identity_links (agent_id, agent_user_ref_hash, buyer_id)
        VALUES (:a, :h, :b)
        """,
        {"a": agent_id, "h": hash_agent_user_ref(ref), "b": buyer_id},
    )


async def _seed_all():
    await _seed_catalog()
    await _seed_eligibility()
    await _seed_link()


# ── 1. the self-heal and the migration build the same schema ─────────────────────────────────


async def _schema_fingerprint():
    """What the DATABASE built, read out of the catalog.

    Three parts, because each catches what the others cannot: columns (name, type, nullability,
    default, length); `pg_indexes.indexdef`, which is the reconstructed DDL and therefore the
    part that notices UNIQUE becoming non-unique; and `pg_get_constraintdef` for every CHECK,
    which is where `market_country ~ '^[A-Z]{2}$'` actually lives.
    """
    columns = await database.fetch_all(
        """
        SELECT table_name, column_name, data_type, is_nullable, column_default,
               character_maximum_length
          FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = ANY(:tables)
         ORDER BY table_name, column_name
        """,
        {"tables": list(_TABLES)},
    )
    indexes = await database.fetch_all(
        """
        SELECT indexname, indexdef FROM pg_indexes
         WHERE schemaname = 'public' AND tablename = ANY(:tables)
         ORDER BY indexname
        """,
        {"tables": list(_TABLES)},
    )
    checks = await database.fetch_all(
        """
        SELECT pg_get_constraintdef(c.oid) AS def
          FROM pg_constraint c
          JOIN pg_class t ON t.oid = c.conrelid
          JOIN pg_namespace n ON n.oid = t.relnamespace
         WHERE n.nspname = 'public' AND c.contype = 'c' AND t.relname = ANY(:tables)
        """,
        {"tables": list(_TABLES)},
    )

    def _norm(text: str) -> str:
        return " ".join((text or "").split())

    return (
        [tuple(dict(r).values()) for r in columns],
        {r["indexname"]: _norm(r["indexdef"]) for r in indexes},
        sorted(_norm(r["def"]) for r in checks),
    )


async def test_the_self_heal_builds_the_same_schema_as_migration_226():
    """Production never runs db/migrations, so the self-heal IS the production schema.

    Built sequentially in this one throwaway database rather than in two: `CREATE DATABASE` needs
    a privilege the CI gate's role may not have, and dropping between the two builds gives the
    same comparison from the same empty start.
    """
    from db.schema_guard import ensure_required_schema_light

    from_migration = await _schema_fingerprint()
    assert from_migration[0], "precondition: the migration built some columns"
    assert from_migration[1], "precondition: the migration built some indexes"
    assert from_migration[2], "precondition: the migration built some CHECK constraints"

    await _drop_tables()
    await ensure_required_schema_light()
    from_self_heal = await _schema_fingerprint()

    columns_m, indexes_m, checks_m = from_migration
    columns_s, indexes_s, checks_s = from_self_heal

    assert columns_s == columns_m, "columns differ between the self-heal and the migration"
    assert set(indexes_s) == set(indexes_m), (
        f"index NAMES differ: only in self-heal {sorted(set(indexes_s) - set(indexes_m))}, "
        f"only in migration {sorted(set(indexes_m) - set(indexes_s))}"
    )
    for name in sorted(indexes_m):
        assert indexes_s[name] == indexes_m[name], (
            f"{name} is built differently by the self-heal:\n"
            f"  migration: {indexes_m[name]}\n"
            f"  self-heal: {indexes_s[name]}"
        )
    assert checks_s == checks_m, (
        "CHECK constraints differ:\n"
        f"  migration: {checks_m}\n  self-heal: {checks_s}"
    )


async def test_the_self_heal_built_index_is_actually_unique():
    """The direct reading of the mutant, so the failure names itself rather than arriving as a
    diff. `indexdef` is the DDL the database reconstructed, not the DDL the source says."""
    from db.schema_guard import ensure_required_schema_light

    await _drop_tables()
    await ensure_required_schema_light()

    rows = await database.fetch_all(
        """
        SELECT indexname, indexdef FROM pg_indexes
         WHERE schemaname = 'public' AND tablename = ANY(:tables)
        """,
        {"tables": list(_TABLES)},
    )
    built = {r["indexname"]: " ".join(r["indexdef"].split()).upper() for r in rows}
    assert "uq_reap_agentic_buyer_refs_ref" in built, (
        "the self-heal did not create the buyer-ref uniqueness index; two buyers could then "
        "share one owner id at Reap, which is two buyers sharing one card"
    )
    assert "CREATE UNIQUE INDEX" in built["uq_reap_agentic_buyer_refs_ref"]


async def test_the_eligibility_key_admits_two_markets_for_one_domain():
    """The key carries `market_country`. Without it, "this merchant sells in the US and in
    Canada" is a statement the table cannot hold — the second row collides with the first."""
    await _seed_eligibility(market="US")
    await _seed_eligibility(market="CA")
    assert await database.fetch_val(
        "SELECT COUNT(*) FROM reap_agentic_eligibility WHERE merchant_domain = :d",
        {"d": DOMAIN},
    ) == 2


async def test_postgres_refuses_a_market_country_that_is_not_two_uppercase_letters():
    """The CHECK is a regex on this dialect. The route normalises and checks before binding, and
    the ledger checks again — this is the third line, and the one that stops an operator's
    hand-written INSERT."""
    import asyncpg

    for bad in ("usa", "us", "U1", "United States"):
        with pytest.raises((asyncpg.CheckViolationError, asyncpg.DataError, Exception)):
            await database.execute(
                """
                INSERT INTO reap_agentic_eligibility (merchant_domain, product_key, variant_key,
                                                      market_country, enabled)
                VALUES (:d, '', '', :m, TRUE)
                """,
                {"d": f"bad-{bad}.example", "m": bad},
            )


async def test_an_eligibility_row_defaults_to_disabled():
    """A row somebody created and did not finish thinking about is not an authorization to
    spend."""
    await database.execute(
        """
        INSERT INTO reap_agentic_eligibility (merchant_domain, product_key, variant_key,
                                              market_country)
        VALUES (:d, '', '', 'US')
        """,
        {"d": DOMAIN},
    )
    assert await database.fetch_val(
        "SELECT enabled FROM reap_agentic_eligibility WHERE merchant_domain = :d", {"d": DOMAIN}
    ) is False


async def test_two_buyers_cannot_share_one_reap_buyer_ref():
    """Behavioural, on the schema production actually gets. An owner id shared by two buyers is
    an enrollment shared by two buyers, and an enrollment is a card."""
    import asyncpg

    await database.execute(
        "INSERT INTO reap_agentic_buyer_refs (buyer_id, reap_buyer_ref) VALUES ('b1', 'ref-1')"
    )
    with pytest.raises(asyncpg.UniqueViolationError):
        await database.execute(
            "INSERT INTO reap_agentic_buyer_refs (buyer_id, reap_buyer_ref) VALUES ('b2', 'ref-1')"
        )


# ── 2. the price is a Decimal on this dialect ────────────────────────────────────────────────


async def test_a_numeric_price_survives_asyncpg_as_an_exact_minor_amount(client):
    """THE DIALECT SPLIT THIS FILE EXISTS FOR. `merchant_effective_price` is `numeric`; asyncpg
    hands it back as a `Decimal` and SQLite hands the same column back as a binary FLOAT, which
    `major_to_minor` refuses outright. The `CAST(... AS TEXT)` in the offer query is what makes
    one code path correct on both — this is the Postgres half of that proof."""
    await _seed_catalog(price="42.50")
    await _seed_eligibility()
    await _seed_link()

    raw = await database.fetch_val(
        "SELECT merchant_effective_price FROM catalog_offers WHERE offer_id = 'off_pg_1'"
    )
    from decimal import Decimal

    assert isinstance(raw, Decimal), "precondition: asyncpg really does hand back a Decimal here"

    resp = await client.post(f"{BASE}/purchases", json=_body())
    assert resp.status_code == 202, resp.text
    row = await ledger.get_purchase_internal(resp.json()["purchase_id"])
    assert row["our_price_minor"] == 4250


async def test_the_catalog_column_pins_the_scale_so_a_third_decimal_cannot_reach_this_route():
    """`major_to_minor` refuses an amount with more decimal places than the currency has, and it
    is worth knowing that this SOURCE can never trigger that refusal: `catalog_offers`' three
    price columns are `numeric(12,2)`, so Postgres rounds at the INSERT and the route is handed
    42.51, not 42.505.

    Asserted rather than assumed, because the conclusion matters both ways. If somebody widens
    the scale, the route starts refusing `row_unpriced` for prices that look perfectly ordinary
    in the catalogue — and this test is the thing that says where to look.
    """
    from decimal import Decimal

    await _seed_catalog(price="42.505")
    stored = await database.fetch_val(
        "SELECT merchant_effective_price FROM catalog_offers WHERE offer_id = 'off_pg_1'"
    )
    assert stored == Decimal("42.51"), (
        f"the offer column no longer rounds to two places (got {stored!r}); "
        "the route's converter will now refuse these rows as row_unpriced"
    )
    scale = await database.fetch_val(
        """
        SELECT numeric_scale FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = 'catalog_offers'
           AND column_name = 'merchant_effective_price'
        """
    )
    assert scale == 2


async def test_a_zero_price_is_refused(client):
    """The refusal that IS reachable from this source. `major_to_minor` refuses `<= 0` — a zero
    is not a spend, and a purchase opened against one would quote against nothing."""
    await _seed_catalog(price="0.00")
    await _seed_eligibility()
    await _seed_link()
    resp = await client.post(f"{BASE}/purchases", json=_body())
    assert resp.status_code == 409
    assert _error(resp) == "row_unpriced"


# ── 3. the ownership conjunct over real SQL ──────────────────────────────────────────────────


async def test_another_agent_cannot_read_this_purchase(client):
    await _seed_all()
    purchase_id = (await client.post(f"{BASE}/purchases", json=_body())).json()["purchase_id"]
    CALLER.agent_id = OTHER_AGENT
    resp = await client.get(f"{BASE}/purchases/{purchase_id}")
    assert resp.status_code == 404
    assert _error(resp) == "purchase_not_found"


async def test_another_end_user_of_the_same_agent_cannot_read_this_purchase(client):
    """The conjunct an agent-only check would miss entirely: same agent, same key, different
    human."""
    await _seed_all()
    purchase_id = (await client.post(f"{BASE}/purchases", json=_body())).json()["purchase_id"]
    CALLER.agent_user_ref = OTHER_USER_REF
    resp = await client.get(f"{BASE}/purchases/{purchase_id}")
    assert resp.status_code == 404
    assert _error(resp) == "purchase_not_found"


async def test_the_list_shows_only_this_buyers_purchases(client):
    await _seed_catalog()
    await _seed_eligibility()
    await _seed_link()
    await _seed_link(ref=OTHER_USER_REF, buyer_id=OTHER_BUYER_ID)

    mine = (await client.post(f"{BASE}/purchases", json=_body())).json()["purchase_id"]
    CALLER.agent_user_ref = OTHER_USER_REF
    theirs = (await client.post(f"{BASE}/purchases", json=_body())).json()["purchase_id"]

    CALLER.agent_user_ref = USER_REF
    ids = [item["id"] for item in (await client.get(f"{BASE}/purchases")).json()["purchases"]]
    assert ids == [mine]
    assert theirs not in ids


async def test_the_list_shows_only_this_agents_purchases(client):
    await _seed_catalog()
    await _seed_eligibility()
    await _seed_link()
    await _seed_link(agent_id=OTHER_AGENT, ref=USER_REF, buyer_id=OTHER_BUYER_ID)

    mine = (await client.post(f"{BASE}/purchases", json=_body())).json()["purchase_id"]
    CALLER.agent_id = OTHER_AGENT
    theirs = (await client.post(f"{BASE}/purchases", json=_body())).json()["purchase_id"]

    CALLER.agent_id = AGENT
    ids = [item["id"] for item in (await client.get(f"{BASE}/purchases")).json()["purchases"]]
    assert ids == [mine]
    assert theirs not in ids


# ── 4. the dial, the allowlist, and the rest of the contract on the production dialect ───────


async def test_every_route_answers_404_while_the_dial_is_off(client, monkeypatch):
    await _seed_all()
    monkeypatch.delenv("REAP_AGENTIC_ENABLED", raising=False)
    for resp in (
        await client.post(f"{BASE}/purchases", json=_body()),
        await client.get(f"{BASE}/purchases"),
        await client.get(f"{BASE}/purchases/rp_nope"),
    ):
        assert resp.status_code == 404
        assert _error(resp) == "not_available_on_this_rail"
    assert await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_purchases") == 0


async def test_an_ineligible_merchant_is_refused(client):
    await _seed_catalog()
    await _seed_link()
    resp = await client.post(f"{BASE}/purchases", json=_body())
    assert resp.status_code == 409
    assert _error(resp) == "merchant_not_eligible"


async def test_a_buyer_in_another_market_is_refused(client):
    await _seed_catalog()
    await _seed_link()
    await _seed_eligibility(market="SG")
    resp = await client.post(f"{BASE}/purchases", json=_body())
    assert resp.status_code == 409
    assert _error(resp) == "merchant_not_eligible"


async def test_the_price_is_ours_and_the_request_cannot_move_it(client):
    await _seed_all()
    resp = await client.post(
        f"{BASE}/purchases", json=_body(unit_price=1.00, our_price_minor=100, currency="EUR")
    )
    assert resp.status_code == 202
    row = await ledger.get_purchase_internal(resp.json()["purchase_id"])
    assert row["our_price_minor"] == 4250
    assert row["currency"] == "USD"


async def test_the_same_idempotency_key_returns_the_same_purchase(client):
    await _seed_all()
    first = await client.post(f"{BASE}/purchases", json=_body(idempotency_key="pg-k-1"))
    second = await client.post(f"{BASE}/purchases", json=_body(idempotency_key="pg-k-1"))
    assert first.json()["purchase_id"] == second.json()["purchase_id"]
    assert await database.fetch_val(
        "SELECT COUNT(*) FROM reap_agentic_purchases WHERE state = 'resolving'"
    ) == 1


async def test_an_expired_idempotency_key_starts_a_new_purchase(client):
    """A `timestamptz` compared against an aware `now()` — the shape SQLite cannot check, because
    there the column is text and the window is enforced through a parse."""
    await _seed_all()
    first = await client.post(f"{BASE}/purchases", json=_body(idempotency_key="pg-k-1"))
    await database.execute(
        "UPDATE reap_agentic_purchase_keys SET created_at = now() - INTERVAL '48 hours'"
    )
    second = await client.post(f"{BASE}/purchases", json=_body(idempotency_key="pg-k-1"))
    assert second.json()["purchase_id"] != first.json()["purchase_id"]


async def test_a_hosted_url_we_cannot_vouch_for_is_never_forwarded(client):
    await _seed_all()
    purchase_id = (await client.post(f"{BASE}/purchases", json=_body())).json()["purchase_id"]
    await ledger.transition(purchase_id, from_states=("resolving",), to_state="needs_enrollment")
    await database.execute(
        "UPDATE reap_agentic_purchases SET hosted_url = :u WHERE id = :id",
        {"u": "https://evilprava.space/enroll/steal", "id": purchase_id},
    )
    resp = await client.get(f"{BASE}/purchases/{purchase_id}")
    assert "hosted_url" not in resp.json()
    assert "evilprava" not in resp.text


async def test_the_read_carries_no_pii_and_no_identity(client):
    await _seed_all()
    purchase_id = (await client.post(f"{BASE}/purchases", json=_body())).json()["purchase_id"]
    for resp in (
        await client.get(f"{BASE}/purchases/{purchase_id}"),
        await client.get(f"{BASE}/purchases"),
    ):
        for secret in PII_STRINGS:
            assert secret not in resp.text
        for identity in (BUYER_ID, USER_REF, hash_agent_user_ref(USER_REF), AGENT):
            assert identity not in resp.text


async def test_the_jsonb_aliases_round_trip_through_the_eligibility_table(client):
    """jsonb comes back from asyncpg as TEXT, verbatim, because no result processor runs on a raw
    statement. Both halves are asserted: that the raw value really is a str, and that the module's
    decode turned it into a list — the second alone would pass on a driver that never had the
    problem."""
    await _seed_catalog()
    await _seed_link()
    await _seed_eligibility()
    await database.execute(
        """
        INSERT INTO reap_agentic_eligibility (merchant_domain, product_key, variant_key,
                                              market_country, enabled, accept_variant_labels)
        VALUES (:d, :pk, '', 'US', TRUE, CAST(:labels AS JSONB))
        """,
        {"d": DOMAIN, "pk": PRODUCT_KEY, "labels": json.dumps(["Standard 50ml"])},
    )
    raw = await database.fetch_val(
        "SELECT accept_variant_labels FROM reap_agentic_eligibility WHERE product_key = :pk",
        {"pk": PRODUCT_KEY},
    )
    assert isinstance(raw, str), "precondition: asyncpg really does hand jsonb back as text"

    resp = await client.post(f"{BASE}/purchases", json=_body())
    assert resp.status_code == 202, resp.text
    row = await ledger.get_purchase_internal(resp.json()["purchase_id"])
    assert ledger._decode_json(row["accept_variant_labels"]) == ["Standard 50ml"]



# ── 5. the refusals that must die on BOTH dialects ───────────────────────────────────────────


async def test_every_route_refuses_401_without_an_agent_user(client):
    await _seed_all()
    CALLER.agent_user_ref = None
    for resp in (
        await client.post(f"{BASE}/purchases", json=_body()),
        await client.get(f"{BASE}/purchases"),
        await client.get(f"{BASE}/purchases/rp_nope"),
    ):
        assert resp.status_code == 401
        assert _error(resp) == "agent_user_required"


# ── WP4b on real Postgres: the mint, the race, and the consent column ────────────────────────
#
# Replaces `test_a_missing_buyer_link_refuses_rather_than_minting_a_buyer`, which pinned the WP4
# behaviour the owner reversed on 2026-09-18. What is re-run here rather than left to the SQLite
# arm is what the ENGINE decides: a REAL unique constraint (so `ON CONFLICT DO NOTHING` is
# exercised against the implementation production runs, not SQLite's), a real `VARCHAR(32)` (so a
# value past the cap is a driver error rather than a silent truncation), and a real
# `timestamptz`.


async def _links() -> list:
    rows = await database.fetch_all(
        "SELECT agent_id, agent_user_ref_hash, buyer_id FROM buyer_identity_links"
    )
    return [dict(r) for r in rows]


async def test_the_first_purchase_mints_one_buyer_one_link_and_one_ref(client):
    await _seed_catalog()
    await _seed_eligibility()

    resp = await client.post(f"{BASE}/purchases", json=_body())

    assert resp.status_code == 202
    links = await _links()
    assert len(links) == 1
    assert links[0]["agent_id"] == AGENT
    assert links[0]["agent_user_ref_hash"] == hash_agent_user_ref(USER_REF)
    assert str(links[0]["buyer_id"]).startswith("u_")
    assert await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_buyer_refs") == 1


async def test_the_minted_buyer_id_carries_nothing_from_the_request(client):
    """`db.accounts.create_or_get_shop_user` is create-or-GET: minting through it would hand an
    agent that asserted a stranger's email that stranger's real buyer_id, and with it their
    prefill. The id must contain nothing the caller sent."""
    await _seed_catalog()
    await _seed_eligibility()
    await client.post(f"{BASE}/purchases", json=_body())

    buyer_id = (await _links())[0]["buyer_id"]
    for sent in (EMAIL, EMAIL.split("@")[0], USER_REF, hash_agent_user_ref(USER_REF)):
        assert sent not in buyer_id
    assert len(buyer_id) == 18


async def test_two_user_refs_at_one_agent_get_two_buyers(client):
    """THE OTHER DIRECTION from the test above, and the one that was undefended.

    Two END USERS OF ONE AGENT sending the SAME body — same `buyer.email` — must not collapse onto
    one buyer, because one buyer is one `reap_buyer_ref` is ONE STORED CARD. A mutant minting
    `"u_" + sha256(agent_id + "|" + email)[:16]` passes every other test in this file (no literal
    substring of the email survives a hash, and `agent_id` is in it so the cross-agent test is
    happy). Only this one kills it.
    """
    await _seed_catalog()
    await _seed_eligibility()

    CALLER.agent_user_ref = USER_REF
    first = await client.post(f"{BASE}/purchases", json=_body())
    CALLER.agent_user_ref = OTHER_USER_REF
    second = await client.post(f"{BASE}/purchases", json=_body())

    assert first.status_code == 202
    assert second.status_code == 202
    links = await _links()
    assert len(links) == 2
    assert {l["agent_id"] for l in links} == {AGENT}, "precondition: one agent, two end users"
    assert len({l["buyer_id"] for l in links}) == 2, (
        "two end users of one agent share a buyer id — and therefore one stored card"
    )
    assert await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_buyer_refs") == 2
    a = (await ledger.get_purchase_internal(first.json()["purchase_id"]))["buyer_ref"]
    b = (await ledger.get_purchase_internal(second.json()["purchase_id"]))["buyer_ref"]
    assert a != b, "two end users of one agent were enrolled against one card"


WINNER_BUYER_ID = "u_pgwinner000000"


def _race_a_link_in(monkeypatch, buyer_id: str = WINNER_BUYER_ID):
    """Make a competing link appear BETWEEN the route's SELECT and its INSERT.

    A PRE-INSERTED ROW DOES NOT TEST THIS. The route's opening SELECT finds a pre-seeded link
    and returns before the INSERT, so the statement whose `ON CONFLICT` behaviour is the point is
    never reached — the `DO NOTHING` -> `DO UPDATE` mutant survived that version of this test on
    both dialects. The race is a write that lands after our read missed, so the competing row is
    inserted from inside `database.execute`, on the way into the route's own link INSERT.
    """
    real_execute = database.execute
    state = {"raced": False}

    async def _racing_execute(query, values=None, *args, **kwargs):
        if not state["raced"] and "INSERT INTO buyer_identity_links" in str(query):
            state["raced"] = True
            await _seed_link(buyer_id=buyer_id)
        return await real_execute(query, values, *args, **kwargs)

    monkeypatch.setattr(database, "execute", _racing_execute)
    return state


async def test_the_real_unique_constraint_makes_the_loser_yield(client, monkeypatch):
    """THE RACE, against the constraint production has.

    `uq_buyer_identity_links_agent_ref_hash` is a real unique index here, so `ON CONFLICT
    (agent_id, agent_user_ref_hash) DO NOTHING` is the Postgres implementation being exercised —
    not SQLite's. The winner's buyer_id must be what the purchase is opened under.
    """
    await _seed_catalog()
    await _seed_eligibility()
    state = _race_a_link_in(monkeypatch)

    resp = await client.post(f"{BASE}/purchases", json=_body())

    assert state["raced"], "precondition: the route never reached its link INSERT"
    assert resp.status_code == 202
    links = await _links()
    assert len(links) == 1, "the loser inserted a second link instead of yielding"
    assert links[0]["buyer_id"] == WINNER_BUYER_ID, (
        "the route overwrote a link that was written first — `DO NOTHING` became `DO UPDATE`"
    )
    ref = await database.fetch_val(
        "SELECT reap_buyer_ref FROM reap_agentic_buyer_refs WHERE buyer_id = :b",
        {"b": WINNER_BUYER_ID},
    )
    row = await ledger.get_purchase_internal(resp.json()["purchase_id"])
    assert row["buyer_ref"] == ref


async def test_the_insert_does_not_overwrite_an_existing_link(client):
    """The already-linked path: the opening SELECT finds the human's link and the route returns
    on it. This is the common case, NOT the `ON CONFLICT` case — the test above is that one."""
    await _seed_all()
    await client.post(f"{BASE}/purchases", json=_body())
    assert [l["buyer_id"] for l in await _links()] == [BUYER_ID]


async def test_two_agents_with_the_same_user_ref_get_two_buyers(client):
    await _seed_catalog()
    await _seed_eligibility()

    await client.post(f"{BASE}/purchases", json=_body())
    CALLER.agent_id = OTHER_AGENT
    await client.post(f"{BASE}/purchases", json=_body())

    links = await _links()
    assert len(links) == 2
    assert len({l["buyer_id"] for l in links}) == 2
    assert await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_buyer_refs") == 2


async def test_consent_is_required_and_refused_before_any_row_is_written(client):
    await _seed_catalog()
    await _seed_eligibility()

    resp = await client.post(
        f"{BASE}/purchases", json=_body(buyer={"email": EMAIL, "shipping_address": dict(ADDRESS)})
    )

    assert resp.status_code == 400
    assert _error(resp) == "consent_required"
    assert await _links() == []
    assert await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_buyer_refs") == 0
    assert await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_purchases") == 0


async def test_the_dial_is_checked_before_consent(client, monkeypatch):
    """A consent check ahead of the dial answers 400 where every well-formed request answers
    404 — a working probe for a rail that is meant to be absent."""
    monkeypatch.delenv("REAP_AGENTIC_ENABLED", raising=False)
    resp = await client.post(
        f"{BASE}/purchases", json=_body(buyer={"email": EMAIL, "shipping_address": dict(ADDRESS)})
    )
    assert resp.status_code == 404
    assert _error(resp) == "not_available_on_this_rail"


async def test_the_consent_columns_are_written_and_are_the_declared_types(client):
    """`consented_at` must be a real `timestamptz` carrying an aware value — a naive one here
    would be read back as UTC on one engine and as local time on another."""
    await _seed_catalog()
    await _seed_eligibility()

    await client.post(f"{BASE}/purchases", json=_body())

    row = dict(
        await database.fetch_one(
            "SELECT consent_version, consented_at FROM reap_agentic_buyer_refs"
        )
    )
    assert row["consent_version"] == CONSENT
    assert isinstance(row["consented_at"], datetime)
    assert row["consented_at"].tzinfo is not None

    types = {
        r["column_name"]: (r["data_type"], r["character_maximum_length"])
        for r in await database.fetch_all(
            """
            SELECT column_name, data_type, character_maximum_length
              FROM information_schema.columns
             WHERE table_schema = 'public' AND table_name = 'reap_agentic_buyer_refs'
            """
        )
    }
    assert types["consent_version"] == ("character varying", 32)
    assert types["consented_at"][0] == "timestamp with time zone"


async def test_a_consent_version_past_the_column_width_is_refused_not_truncated(client):
    """THE REASON THE CAP IS IN THE ROUTE. `VARCHAR(32)` on this engine raises
    `StringDataRightTruncation` on a longer value — a 500, not a refusal — and a truncated version
    tag names a DIFFERENT version. The route caps it before the bind."""
    await _seed_all()
    resp = await client.post(
        f"{BASE}/purchases", json=_body(buyer=_buyer(consent_version="v" * 33))
    )
    assert resp.status_code == 400
    assert _error(resp) == "consent_required"
    assert await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_purchases") == 0


async def test_a_nul_byte_in_the_consent_version_is_a_refusal_and_not_a_500(client):
    """asyncpg raises `CharacterNotInRepertoireError` on a NUL, which is not a `PurchaseRefused`
    and arrives as a 500. Only this dialect can see it — SQLite stores NULs happily."""
    await _seed_all()
    resp = await client.post(
        f"{BASE}/purchases", json=_body(buyer=_buyer(consent_version="v1\x00"))
    )
    assert resp.status_code == 400
    assert _error(resp) == "consent_required"


async def test_the_latest_consent_version_replaces_the_stored_one(client):
    await _seed_catalog()
    await _seed_eligibility()

    await client.post(f"{BASE}/purchases", json=_body())
    first_at = await database.fetch_val("SELECT consented_at FROM reap_agentic_buyer_refs")
    await client.post(f"{BASE}/purchases", json=_body(buyer=_buyer(consent_version="v2")))

    assert await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_buyer_refs") == 1
    assert await database.fetch_val("SELECT consent_version FROM reap_agentic_buyer_refs") == "v2"
    assert await database.fetch_val("SELECT consented_at FROM reap_agentic_buyer_refs") >= first_at


async def test_consent_is_recorded_for_a_buyer_the_hosted_checkout_linked(client):
    """The already-linked path mints nothing, and is the one a consent write could be dropped
    from without any mint test noticing."""
    await _seed_all()
    await client.post(f"{BASE}/purchases", json=_body())
    assert await database.fetch_val(
        "SELECT consent_version FROM reap_agentic_buyer_refs WHERE buyer_id = :b",
        {"b": BUYER_ID},
    ) == CONSENT


async def test_a_replay_still_records_the_latest_consent(client):
    """A replay returns 202 before the buyer-ref code runs, and was the one successful POST that
    left the stored tag stale — while the contract page, the runbook and migration 227's header
    all promise it is rewritten on every purchase. Checked here as well as on SQLite because
    `consented_at` is a real `timestamptz` and the comparison is a real server-side one."""
    await _seed_all()
    first = await client.post(f"{BASE}/purchases", json=_body(idempotency_key="pg-replay"))
    assert first.status_code == 202
    before = await database.fetch_val("SELECT consented_at FROM reap_agentic_buyer_refs")

    second = await client.post(
        f"{BASE}/purchases",
        json=_body(idempotency_key="pg-replay", buyer=_buyer(consent_version="v9")),
    )

    assert second.status_code == 202
    assert second.json()["purchase_id"] == first.json()["purchase_id"], (
        "precondition: this was a replay, not a new purchase"
    )
    assert await database.fetch_val("SELECT consent_version FROM reap_agentic_buyer_refs") == "v9"
    assert await database.fetch_val("SELECT consented_at FROM reap_agentic_buyer_refs") >= before


async def test_a_replay_whose_link_was_deleted_does_not_re_mint_one(client):
    """An idempotency key outlives its link if the link is deleted inside the 24-hour window — an
    erasure request, or an operator cleaning up. The key knows nothing about the link, so the
    replay still resolves; resolving the buyer with the MINTING lookup would silently recreate an
    identity that was deliberately deleted, on a path that has not checked eligibility."""
    await _seed_catalog()
    await _seed_eligibility()
    first = await client.post(f"{BASE}/purchases", json=_body(idempotency_key="pg-k-x"))
    assert first.status_code == 202
    assert len(await _links()) == 1

    await database.execute("DELETE FROM buyer_identity_links")
    assert await _links() == []

    second = await client.post(f"{BASE}/purchases", json=_body(idempotency_key="pg-k-x"))

    assert second.status_code == 202
    assert second.json()["purchase_id"] == first.json()["purchase_id"], (
        "precondition: this was a replay — otherwise the mint below is not the thing under test"
    )
    assert await _links() == [], (
        "a replay re-created a buyer identity that had been deleted, on a path that has not "
        "checked eligibility"
    )


async def test_the_link_is_touched_when_it_is_reused(client):
    """`last_seen_at` on the reuse path, checked HERE because this dialect has the clock
    resolution to show the bump: `CURRENT_TIMESTAMP` is a real server-side `timestamptz` and the
    comparison is against a value Postgres itself wrote."""
    await _seed_all()
    await database.execute(
        "UPDATE buyer_identity_links SET last_seen_at = NULL WHERE agent_id = :a", {"a": AGENT}
    )
    assert await database.fetch_val("SELECT last_seen_at FROM buyer_identity_links") is None

    await client.post(f"{BASE}/purchases", json=_body())

    seen = await database.fetch_val("SELECT last_seen_at FROM buyer_identity_links")
    assert seen is not None, "the rail used a link without stamping that it had seen it"
    assert seen.tzinfo is not None
    created = await database.fetch_val("SELECT created_at FROM buyer_identity_links")
    assert seen >= created


async def test_the_minted_identity_is_never_in_a_response(client):
    await _seed_catalog()
    await _seed_eligibility()

    resp = await client.post(f"{BASE}/purchases", json=_body())
    buyer_id = (await _links())[0]["buyer_id"]
    ref = await database.fetch_val("SELECT reap_buyer_ref FROM reap_agentic_buyer_refs")

    read = await client.get(f"{BASE}/purchases/{resp.json()['purchase_id']}")
    listed = await client.get(f"{BASE}/purchases")
    # THE CONSENT TAG CAME OFF THIS LIST AT MIGRATION 233, deliberately and with the owner's
    # decision behind it. It was here because WP4b had no reason to hand it back and the
    # allowlist was kept minimal; 233 makes it the one column on the purchase row that records
    # something the OWNER did, and a buyer's own consent tag is theirs to read. The three
    # IDENTITY values below are what this test is actually about and they have not moved.
    for body in (resp.text, read.text, listed.text):
        for secret in (buyer_id, ref, hash_agent_user_ref(USER_REF)):
            assert secret not in body

    # Stated rather than left as an absence: the tag IS in the owner-facing reads now, so a
    # future edit that puts it back on the secret list above has to argue with this line.
    assert CONSENT in read.text and CONSENT in listed.text
    # And it is still absent from the 202, which carries no purchase fields at all.
    assert CONSENT not in resp.text


async def test_a_non_shopify_row_is_refused(client):
    await _seed_catalog(platform="external_seed")
    await _seed_eligibility()
    await _seed_link()
    resp = await client.post(f"{BASE}/purchases", json=_body())
    assert resp.status_code == 409
    assert _error(resp) == "row_not_shopify"


async def test_a_product_belonging_to_another_domain_answers_row_not_found(client):
    await _seed_catalog(domain="someone-else.example")
    await _seed_eligibility()
    await _seed_link()
    resp = await client.post(f"{BASE}/purchases", json=_body())
    assert resp.status_code == 409
    assert _error(resp) == "row_not_found"


async def test_a_return_url_on_a_host_that_is_not_ours_is_refused(client):
    await _seed_all()
    resp = await client.post(
        f"{BASE}/purchases", json=_body(return_url="https://evil.example/collect")
    )
    assert resp.status_code == 400
    assert _error(resp) == "invalid_return_url"
    assert await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_purchases") == 0


async def test_an_expired_hosted_url_is_not_shown(client):
    """A `timestamptz` compared against an aware `now()`. On SQLite the same column is text and
    the comparison runs through a parse, so this is the only arm that proves the aware path."""
    await _seed_all()
    purchase_id = (await client.post(f"{BASE}/purchases", json=_body())).json()["purchase_id"]
    await ledger.transition(
        purchase_id,
        from_states=("resolving",),
        to_state="needs_enrollment",
        hosted_url="https://pay.prava.space/enroll/abc",
        hosted_url_expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    body = (await client.get(f"{BASE}/purchases/{purchase_id}")).json()
    assert "hosted_url" not in body
    assert "hosted_url_expires_at" not in body


async def test_a_lapsed_quote_drops_the_link_while_the_page_is_still_live(client):
    """Two `timestamptz` columns compared against an aware `now()`: the approval deadline is the
    EARLIER of the quote's expiry and the page's. Measured 2026-09-25 in the sandbox — the
    checkout is FAILED at the quote's expiry, ten minutes before the page's. On SQLite both
    columns are text and the comparison runs through a parse, so this arm is the one that proves
    the aware path on the production dialect."""
    await _seed_all()
    purchase_id = (await client.post(f"{BASE}/purchases", json=_body())).json()["purchase_id"]
    await ledger.transition(purchase_id, from_states=("resolving",), to_state="quoting")
    await ledger.transition(
        purchase_id,
        from_states=("quoting",),
        to_state="awaiting_approval",
        reap_quote_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        hosted_url="https://pay.prava.space/checkout/chk_7f3a",
        hosted_url_expires_at=datetime.now(timezone.utc) + timedelta(minutes=9),
    )
    body = (await client.get(f"{BASE}/purchases/{purchase_id}")).json()
    assert "hosted_url" not in body
    assert "hosted_url_expires_at" not in body
    assert body["approval_deadline"] == body["reap_quote_expires_at"]
    assert datetime.fromisoformat(body["approval_deadline"]) < datetime.now(timezone.utc)


async def test_a_live_quote_names_itself_as_the_deadline_and_keeps_the_link(client):
    """CONTROL for the arm above on this dialect."""
    await _seed_all()
    purchase_id = (await client.post(f"{BASE}/purchases", json=_body())).json()["purchase_id"]
    await ledger.transition(purchase_id, from_states=("resolving",), to_state="quoting")
    await ledger.transition(
        purchase_id,
        from_states=("quoting",),
        to_state="awaiting_approval",
        reap_quote_expires_at=datetime.now(timezone.utc) + timedelta(minutes=4),
        hosted_url="https://pay.prava.space/checkout/chk_7f3a",
        hosted_url_expires_at=datetime.now(timezone.utc) + timedelta(minutes=14),
    )
    body = (await client.get(f"{BASE}/purchases/{purchase_id}")).json()
    assert body["hosted_url"] == "https://pay.prava.space/checkout/chk_7f3a"
    assert body["approval_deadline"] == body["reap_quote_expires_at"]
    assert body["approval_deadline"] < body["hosted_url_expires_at"]


async def test_a_live_hosted_url_is_shown(client):
    """The control for the test above: an absence assertion passes when the mechanism is absent
    too, so this proves a hosted URL reaches the buyer at all on this dialect."""
    await _seed_all()
    purchase_id = (await client.post(f"{BASE}/purchases", json=_body())).json()["purchase_id"]
    await ledger.transition(
        purchase_id,
        from_states=("resolving",),
        to_state="needs_enrollment",
        hosted_url="https://pay.prava.space/enroll/abc",
        hosted_url_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    body = (await client.get(f"{BASE}/purchases/{purchase_id}")).json()
    assert body["hosted_url"] == "https://pay.prava.space/enroll/abc"


async def test_an_order_reference_appears_only_when_the_purchase_is_complete(client):
    await _seed_all()
    purchase_id = (await client.post(f"{BASE}/purchases", json=_body())).json()["purchase_id"]
    await ledger.transition(purchase_id, from_states=("resolving",), to_state="quoting")
    await ledger.transition(
        purchase_id, from_states=("quoting",), to_state="awaiting_approval", reap_order_id="ord_9"
    )
    assert "order_reference" not in (await client.get(f"{BASE}/purchases/{purchase_id}")).json()

    await ledger.transition(
        purchase_id,
        from_states=("awaiting_approval",),
        to_state="completed",
        reap_order_id="ord_9",
        final_total_minor=4500,
    )
    body = (await client.get(f"{BASE}/purchases/{purchase_id}")).json()
    assert body["order_reference"] == "ord_9"
    assert body["totals"]["final_total_minor"] == 4500


async def test_a_replay_reports_the_purchases_real_state(client):
    """Not a hardcoded 'resolving'. This is also the assertion that notices a replay path which
    "works" only because the idempotency-key INSERT loses the race and re-reads the winner — the
    ids would match while the reported state was a lie."""
    await _seed_all()
    first = await client.post(f"{BASE}/purchases", json=_body(idempotency_key="pg-k-9"))
    purchase_id = first.json()["purchase_id"]
    await ledger.transition(purchase_id, from_states=("resolving",), to_state="quoting")

    second = await client.post(f"{BASE}/purchases", json=_body(idempotency_key="pg-k-9"))
    assert second.json()["purchase_id"] == purchase_id
    assert second.json()["status"] == "quoting"
    assert await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_purchases") == 1


# ── 6. the review findings, on the production dialect ────────────────────────────────────────


async def _seed_competitor_offer(*, price: str = "1.00", merchant_id: str = "m_pg_competitor"):
    """A SECOND SELLER on the SAME sku. `catalog_offers.merchant_id` is the offer seller and is a
    different column from `catalog_products.merchant_id` — `pivot_query_service` LEFT JOINs
    `catalog_merchants` twice, once on each, precisely because one sku carries many sellers."""
    await database.execute(
        """
        INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id,
                                    currency, merchant_effective_price)
        VALUES (:oid, :sk, :pk, :mid, 'USD', :price)
        """,
        {
            "oid": f"off_{merchant_id}",
            "sk": SKU_KEY,
            "pk": PRODUCT_KEY,
            "mid": merchant_id,
            "price": price,
        },
    )


async def test_a_competing_sellers_cheaper_offer_is_not_our_price(client):
    """THE P0, on real `numeric` ordering. Without the seller conjunct this was
    `ORDER BY price ASC LIMIT 1` across every seller, so a competitor's 1.00 became the price we
    would have committed a buyer's card to — and the quote would then have refused
    `price_changed` for a change that never happened."""
    await _seed_all()
    await _seed_competitor_offer(price="1.00")
    resp = await client.post(f"{BASE}/purchases", json=_body())
    assert resp.status_code == 202, resp.text
    row = await ledger.get_purchase_internal(resp.json()["purchase_id"])
    assert row["our_price_minor"] == 4250


async def test_a_merchant_with_no_offer_of_its_own_is_unpriced(client):
    await _seed_catalog(priced=False)
    await _seed_eligibility()
    await _seed_link()
    await _seed_competitor_offer(price="9.99")
    resp = await client.post(f"{BASE}/purchases", json=_body())
    assert resp.status_code == 409
    assert _error(resp) == "row_unpriced"


async def test_an_offer_priced_in_another_currency_is_refused(client):
    await _seed_catalog(currency="EUR")
    await _seed_eligibility(market="US")
    await _seed_link()
    resp = await client.post(f"{BASE}/purchases", json=_body())
    assert resp.status_code == 409
    assert _error(resp) == "row_currency_mismatch"


async def test_a_market_the_currency_map_does_not_know_fails_closed(client):
    """FAILS CLOSED. A market that is not in `_MARKET_CURRENCY` refuses rather than skipping the
    check — the same direction every other decision on this rail takes. The `market_country`
    column's own CHECK accepts any two uppercase letters, so an operator really can create this
    row; only the map stops the purchase."""
    assert "ZZ" not in routes_reap._MARKET_CURRENCY, "precondition: ZZ is not a known market"
    await _seed_catalog(currency="USD")
    await _seed_eligibility(market="ZZ")
    await _seed_link()
    address = dict(ADDRESS, country="ZZ")
    resp = await client.post(
        f"{BASE}/purchases", json=_body(buyer=_buyer(shipping_address=address))
    )
    assert resp.status_code == 409
    assert _error(resp) == "row_currency_mismatch"


async def test_a_matching_market_currency_is_accepted(client):
    """The control: an absence assertion passes when the mechanism is absent too."""
    await _seed_all()
    resp = await client.post(f"{BASE}/purchases", json=_body())
    assert resp.status_code == 202


async def test_tierb_cart_route_mints_owned_click_and_numeric_variant_on_postgres(
    client, monkeypatch
):
    """Exercise the new route against Postgres JSONB, NUMERIC, and the real 228–230 schema."""
    await _seed_catalog()
    await database.execute(
        "UPDATE catalog_products SET seller_ref = 'm_pg' WHERE product_key = :pk",
        {"pk": PRODUCT_KEY},
    )
    await database.execute(
        "UPDATE catalog_skus SET source_variant_id = '50041364447509' WHERE sku_key = :sk",
        {"sk": SKU_KEY},
    )
    await database.execute(
        "INSERT INTO tierb_cart_link_eligibility "
        "(shop_domain, market, verdict, checked_at, consecutive_same) "
        "VALUES (:domain, 'US', 'ELIGIBLE', now(), 1)",
        {"domain": DOMAIN},
    )
    monkeypatch.setenv("REAP_AGENTIC_CART_LINK_ENABLED", "1")
    monkeypatch.setattr(routes_reap.rc, "CART_LINK_QUOTE_FIELD", "hypotheticalCartUrl")
    resp = await client.post(f"{BASE}/purchases", json=_body(item_source="cart_link"))
    assert resp.status_code == 202, resp.text
    purchase = await database.fetch_one(
        "SELECT item_source, click_id, cart_url, our_price_minor FROM reap_agentic_purchases "
        "WHERE id = :id", {"id": resp.json()["purchase_id"]},
    )
    assert purchase["item_source"] == "cart_link"
    assert purchase["our_price_minor"] == 4250
    assert "/cart/50041364447509:1?" in purchase["cart_url"]
    click = await database.fetch_one(
        "SELECT merchant_id, context FROM surface_click_events WHERE click_id = :click_id",
        {"click_id": purchase["click_id"]},
    )
    assert click["merchant_id"] == "m_pg"
    context = click["context"]
    if isinstance(context, str):
        context = json.loads(context)
    assert context["seller_ref"] == "m_pg"


async def test_tierb_mirrored_seed_requires_storefront_evidence_on_postgres(client, monkeypatch):
    """Raw asyncpg JSONB reads must decode before the sole-stamped-variant decision."""
    from db.sql_migrations import split_statements

    seed_id = "seed_reap_cart_route_pg"
    # This file is restricted to a throwaway DB. Build the real 044 schema from scratch, since
    # another test module may have left a narrow fixture table with the same name.
    await database.execute("DROP TABLE IF EXISTS external_product_seeds")
    migration = _MIGRATIONS_DIR / "044_external_product_seeds.sql"
    for statement in split_statements(migration.read_text(encoding="utf-8")):
        await database.execute(statement)
    try:
        await _seed_catalog(platform="external_seed")
        await database.execute(
            "UPDATE catalog_products SET seller_ref = 'm_pg', seed_kind = 'self', "
            "source_system = 'external_product_seeds_mirror_v1', source_ref = :seed_id "
            "WHERE product_key = :pk", {"seed_id": seed_id, "pk": PRODUCT_KEY},
        )
        await database.execute(
            "UPDATE catalog_skus SET source_variant_id = :pk WHERE sku_key = :sk",
            {"pk": PRODUCT_KEY, "sk": SKU_KEY},
        )
        stamped = json.dumps({"snapshot": {"storefront_platform": "shopify", "variants": [
            {"shopify_variant_id": "50041364447509"}
        ], "shopify_cart_proof": {
            "source": "products_js_v1",
            "product_js_url": f"https://{DOMAIN}/products/test.js",
            "live_variant_count": 1,
            "variant_id": "50041364447509",
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }}})
        await database.execute(
            "INSERT INTO external_product_seeds "
            "(id, market, destination_url, domain, attached_product_key, "
            "attached_variant_id, seed_data) "
            "VALUES (:seed_id, 'US', :url, :domain, :pk, '50041364447509', "
            "CAST(:seed_data AS JSONB))",
            {"seed_id": seed_id, "url": f"https://{DOMAIN}/products/test",
             "domain": DOMAIN, "pk": PRODUCT_KEY, "seed_data": stamped},
        )
        await database.execute(
            "INSERT INTO tierb_cart_link_eligibility "
            "(shop_domain, market, verdict, checked_at, consecutive_same) "
            "VALUES (:domain, 'US', 'ELIGIBLE', now(), 1)", {"domain": DOMAIN},
        )
        monkeypatch.setenv("REAP_AGENTIC_CART_LINK_ENABLED", "1")
        monkeypatch.setattr(routes_reap.rc, "CART_LINK_QUOTE_FIELD", "hypotheticalCartUrl")
        response = await client.post(f"{BASE}/purchases", json=_body(item_source="cart_link"))
        assert response.status_code == 202, response.text
        purchase = await database.fetch_one(
            "SELECT cart_url FROM reap_agentic_purchases WHERE id = :id",
            {"id": response.json()["purchase_id"]},
        )
        assert "/cart/50041364447509:1?" in purchase["cart_url"]
    finally:
        await database.execute(
            "DELETE FROM external_product_seeds WHERE id = :seed_id", {"seed_id": seed_id}
        )
        await database.execute("DROP TABLE external_product_seeds")


@pytest.mark.parametrize(
    "field,value",
    [
        ("merchant_domain", "brand-pg.example\x00"),
        ("product_key", PRODUCT_KEY + "\x00"),
        ("variant_key", SKU_KEY + "\x00"),
        ("idempotency_key", "pg-k\x00"),
    ],
)
async def test_a_nul_byte_in_an_identifier_is_a_refusal_and_not_a_500(client, field, value):
    """THE DEFECT ONLY THIS ARM COULD SEE. asyncpg raises `CharacterNotInRepertoireError` on a NUL
    in a bind; it is not a `PurchaseRefused`, so it escaped the handler's one `except` and the
    error middleware turned it into a **500**. SQLite stores NULs happily and answered a clean
    409, so the SQLite arm could not observe this at all.

    A 500 is also the one answer that tells a prober a dark rail is really there.
    """
    await _seed_all()
    resp = await client.post(f"{BASE}/purchases", json=_body(**{field: value}))
    assert resp.status_code != 500, resp.text
    assert resp.status_code == 400
    assert _error(resp) == "invalid_request"
    assert await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_purchases") == 0


async def test_the_same_key_on_a_different_body_is_a_conflict(client):
    await _seed_all()
    first = await client.post(f"{BASE}/purchases", json=_body(idempotency_key="pg-c-1"))
    assert first.status_code == 202
    second = await client.post(
        f"{BASE}/purchases", json=_body(idempotency_key="pg-c-1", quantity=3)
    )
    assert second.status_code == 409
    assert _error(second) == "idempotency_conflict"

    # A DIFFERENT ADDRESS IS A DIFFERENT PURCHASE. Same key, same product, same quantity — and a
    # parcel going somewhere else. If the hash did not cover the address, this would answer 202
    # naming the first purchase and the buyer would approve a delivery to the old address.
    address = dict(ADDRESS, addressLine1="1 Other St", city="Oakland")
    third = await client.post(
        f"{BASE}/purchases",
        json=_body(idempotency_key="pg-c-1", buyer=_buyer(shipping_address=address)),
    )
    assert third.status_code == 409
    assert _error(third) == "idempotency_conflict"

    # A DIFFERENT BUYER EMAIL, likewise.
    fourth = await client.post(
        f"{BASE}/purchases",
        json=_body(
            idempotency_key="pg-c-1",
            buyer=_buyer(email="someone-else@example.test"),
        ),
    )
    assert _error(fourth) == "idempotency_conflict"

    assert await database.fetch_val(
        "SELECT COUNT(*) FROM reap_agentic_purchases WHERE state = 'resolving'"
    ) == 1


async def test_the_request_hash_column_is_not_null_and_is_populated(client):
    """`NOT NULL` with no default: there is no such thing as a key row whose request is unknown,
    and a path that forgot to supply one must fail loudly rather than write a row that can never
    conflict."""
    import asyncpg

    await _seed_all()
    await client.post(f"{BASE}/purchases", json=_body(idempotency_key="pg-c-2"))
    stored = await database.fetch_val(
        "SELECT request_hash FROM reap_agentic_purchase_keys WHERE idempotency_key = 'pg-c-2'"
    )
    assert isinstance(stored, str) and len(stored) == 64

    with pytest.raises(asyncpg.NotNullViolationError):
        await database.execute(
            """
            INSERT INTO reap_agentic_purchase_keys
                   (agent_id, agent_user_ref_hash, idempotency_key, purchase_id)
            VALUES ('a', 'h', 'no-hash', 'rp_x')
            """
        )


async def test_a_dark_rail_answers_404_for_every_shape_of_bad_input(client, monkeypatch):
    """The existence probe, on the dialect production runs. A validator in the handler signature
    fires BEFORE the handler, so a malformed body answered 400 with a field message while every
    valid request answered 404."""
    await _seed_all()
    monkeypatch.delenv("REAP_AGENTIC_ENABLED", raising=False)

    baseline = await client.post(f"{BASE}/purchases", json=_body())
    assert baseline.status_code == 404
    expected = _error(baseline)

    for probe in (
        await client.post(f"{BASE}/purchases", json=[1, 2]),
        await client.post(f"{BASE}/purchases", content=b"not json at all"),
        await client.get(f"{BASE}/purchases?limit=500"),
    ):
        assert probe.status_code == 404, probe.text
        assert _error(probe) == expected == "not_available_on_this_rail"


async def test_a_failing_unique_index_does_not_starve_the_keys_table():
    """ONE try PER STATEMENT, on the dialect where the failure is real. The block's own comment
    names `uq_reap_agentic_buyer_refs_ref` as the statement that cannot be created on a database
    already holding two buyers with one ref, and `reap_agentic_purchase_keys` used to sit after it
    in the SAME try — so the raise would have starved it forever, on exactly the databases that
    were already unwell, with no other route to it in production."""
    from db.schema_guard import ensure_required_schema_light

    await _drop_tables()
    await database.execute(
        """
        CREATE TABLE reap_agentic_buyer_refs (
            buyer_id VARCHAR(50) PRIMARY KEY,
            reap_buyer_ref VARCHAR(128) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    for buyer in ("b1", "b2"):
        await database.execute(
            "INSERT INTO reap_agentic_buyer_refs (buyer_id, reap_buyer_ref) "
            "VALUES (:b, 'collides')",
            {"b": buyer},
        )

    await ensure_required_schema_light()

    assert await database.fetch_val(
        "SELECT COUNT(*) FROM pg_indexes WHERE schemaname = 'public' "
        "AND indexname = 'uq_reap_agentic_buyer_refs_ref'"
    ) == 0, "precondition: the unique index really is impossible on this data"

    for table in ("reap_agentic_eligibility", "reap_agentic_purchase_keys"):
        assert await database.fetch_val(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = :t",
            {"t": table},
        ) == 1, f"{table} was starved by the failing index"


# ── mig 233: the two stores agree at open time, on the production dialect ────────────────────


async def test_the_route_writes_the_same_consent_to_both_stores_on_postgres(client):
    """ONE VALIDATED STRING, TWO WRITERS, ONE REQUEST. `_reap_buyer_ref` records the buyer's
    LATEST consent and `start_purchase` records what THIS purchase was opened under; a mutant
    that hands one of them a different value leaves two stores disagreeing about a human act,
    and every test that checks only one of them stays green.

    On this dialect the two columns are a real `VARCHAR(32)` and a real `timestamptz`, so the
    comparison is one the database performed rather than one Python performed on two strings.
    """
    await _seed_catalog()
    await _seed_eligibility()

    resp = await client.post(f"{BASE}/purchases", json=_body())
    assert resp.status_code == 202
    purchase_id = resp.json()["purchase_id"]

    row = await database.fetch_one(
        """
        SELECT p.consent_version AS on_purchase,
               p.consented_at    AS purchase_at,
               r.consent_version AS on_buyer,
               r.consented_at    AS buyer_at
          FROM reap_agentic_purchases p
         CROSS JOIN reap_agentic_buyer_refs r
         WHERE p.id = :i
        """,
        {"i": purchase_id},
    )
    got = dict(row)
    assert got["on_purchase"] == CONSENT
    assert got["on_purchase"] == got["on_buyer"], (
        "the route wrote a different consent tag to the purchase than to the buyer identity"
    )
    # Both are aware `timestamptz` values written seconds apart in the same request.
    assert got["purchase_at"].tzinfo is not None and got["buyer_at"].tzinfo is not None
    assert abs((got["purchase_at"] - got["buyer_at"]).total_seconds()) < 60


async def test_a_refused_consent_writes_to_neither_store_on_postgres(client):
    """The control: `consent_required` is decided before both writes."""
    await _seed_catalog()
    await _seed_eligibility()

    resp = await client.post(
        f"{BASE}/purchases", json=_body(buyer=_buyer(consent_version="v" * 33))
    )
    assert resp.status_code == 400
    assert _error(resp) == "consent_required"
    assert await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_purchases") == 0
    assert await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_buyer_refs") == 0


async def test_a_replay_moves_only_the_buyers_tag_on_postgres(client):
    """The two stores mean different things, and a replay is where that shows: the buyer's tag
    is current state and moves; the purchase's is evidence and does not."""
    await _seed_catalog()
    await _seed_eligibility()

    first = await client.post(f"{BASE}/purchases", json=_body(idempotency_key="pg-233"))
    purchase_id = first.json()["purchase_id"]

    again = await client.post(
        f"{BASE}/purchases",
        json=_body(idempotency_key="pg-233", buyer=_buyer(consent_version="v-later")),
    )
    assert again.json()["purchase_id"] == purchase_id

    assert await database.fetch_val(
        "SELECT consent_version FROM reap_agentic_buyer_refs"
    ) == "v-later"
    assert await database.fetch_val(
        "SELECT consent_version FROM reap_agentic_purchases WHERE id = :i", {"i": purchase_id}
    ) == CONSENT


# ── the merchant domain is matched canonically, on the production dialect ───────────────────
#
# Production's Shopify rows spell `source_domain` as `www.Brand.com`; operators write `brand.com`;
# the gateway sends either. Every comparison the variant lane makes folds BOTH sides by one rule
# (lower case, ONE leading `www.` removed), and the SQL half of that rule is a `CASE` over `LIKE`
# and `substr` that must mean the same thing here as on SQLite — Postgres' LIKE is
# case-sensitive, and SQLite's is not, which is exactly the kind of difference only this file can
# see.

PG_WWW_PRODUCT_DOMAIN = "www.Brand-PG.example"
PG_CANONICAL_DOMAIN = "brand-pg.example"


async def _seed_www_world(*, product_domain: str = PG_WWW_PRODUCT_DOMAIN,
                          eligibility_domain: str = PG_CANONICAL_DOMAIN):
    await _seed_catalog(domain=product_domain)
    await _seed_eligibility(domain=eligibility_domain)
    await _seed_link()


@pytest.mark.parametrize(
    "requested", ["www.brand-pg.example", "brand-pg.example", "WWW.BRAND-PG.EXAMPLE"]
)
async def test_every_spelling_of_one_merchant_reaches_the_same_row(client, requested):
    await _seed_www_world()
    resp = await client.post(f"{BASE}/purchases", json=_body(merchant_domain=requested))
    assert resp.status_code == 202, (requested, resp.text)
    row = await ledger.get_purchase_internal(resp.json()["purchase_id"])
    assert row["product_key"] == PRODUCT_KEY and row["variant_key"] == SKU_KEY
    assert row["our_price_minor"] == 4250
    assert row["merchant_domain"] == requested.lower()


async def test_an_eligibility_row_typed_with_the_www_still_matches(client):
    """Mixed case AND the `www.` on the stored row: only a fold that lowercases BEFORE the LIKE
    matches it on Postgres."""
    await _seed_www_world(eligibility_domain="www.Brand-PG.example")
    resp = await client.post(f"{BASE}/purchases", json=_body(merchant_domain="brand-pg.example"))
    assert resp.status_code == 202, resp.text


async def test_a_www_with_no_dot_after_it_is_part_of_the_name(client):
    await _seed_www_world(product_domain=PG_CANONICAL_DOMAIN)
    resp = await client.post(
        f"{BASE}/purchases", json=_body(merchant_domain="wwwbrand-pg.example")
    )
    assert resp.status_code == 409
    assert _error(resp) == "merchant_not_eligible"


async def test_a_product_under_a_www_less_lookalike_is_not_this_merchants(client):
    await _seed_www_world(product_domain="wwwbrand-pg.example")
    resp = await client.post(f"{BASE}/purchases", json=_body(merchant_domain="brand-pg.example"))
    assert resp.status_code == 409
    assert _error(resp) == "row_not_found"


async def test_only_one_www_is_stripped_from_the_request(client):
    await _seed_www_world(product_domain=PG_CANONICAL_DOMAIN)
    resp = await client.post(
        f"{BASE}/purchases", json=_body(merchant_domain="www.www.brand-pg.example")
    )
    assert resp.status_code == 409
    assert _error(resp) == "merchant_not_eligible"


async def test_only_one_www_is_stripped_from_either_stored_column(client):
    await _seed_www_world(
        product_domain="www.www.brand-pg.example", eligibility_domain="www.www.brand-pg.example"
    )
    resp = await client.post(f"{BASE}/purchases", json=_body(merchant_domain="brand-pg.example"))
    assert resp.status_code == 409
    assert _error(resp) == "merchant_not_eligible"
    resp = await client.post(
        f"{BASE}/purchases", json=_body(merchant_domain="www.www.brand-pg.example")
    )
    assert resp.status_code == 202, resp.text


@pytest.mark.parametrize(
    "value",
    ["https://brand-pg.example/", "brand-pg.example:443", "127.0.0.1", "www.example",
     "brand-pg.example.", "brand-pg.example.."],
)
async def test_a_merchant_domain_that_is_not_a_bare_host_is_refused_before_any_read(
    client, value
):
    await _seed_catalog()
    await _seed_eligibility()
    resp = await client.post(f"{BASE}/purchases", json=_body(merchant_domain=value))
    assert resp.status_code == 400, (value, resp.text)
    assert _error(resp) == "invalid_request"
    assert value not in resp.text
    assert await database.fetch_val("SELECT COUNT(*) FROM buyer_identity_links") == 0
    assert await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_purchases") == 0


@pytest.mark.parametrize("disabled_first", [True, False], ids=["disabled_first", "enabled_first"])
@pytest.mark.parametrize(
    "disabled_spelling", ["brand-pg.example", "www.brand-pg.example"], ids=["bare_off", "www_off"]
)
async def test_a_disabled_twin_spelling_turns_the_merchant_off(
    client, disabled_spelling, disabled_first
):
    """Which spelling is off x which was inserted first: whatever order Postgres returns the rows
    in (heap order here), some case puts the ENABLED row last and some first, so "last row wins"
    and "first row wins" each reach a 202 and die."""
    enabled_spelling = (
        "www.brand-pg.example" if disabled_spelling == "brand-pg.example" else "brand-pg.example"
    )
    await _seed_catalog(domain=PG_WWW_PRODUCT_DOMAIN)
    rows = [(disabled_spelling, False), (enabled_spelling, True)]
    for domain, enabled in (rows if disabled_first else list(reversed(rows))):
        await _seed_eligibility(domain=domain, enabled=enabled)
    await _seed_link()
    for requested in ("brand-pg.example", "www.brand-pg.example"):
        resp = await client.post(f"{BASE}/purchases", json=_body(merchant_domain=requested))
        assert resp.status_code == 409, (requested, resp.text)
        assert _error(resp) == "merchant_not_eligible"


async def test_the_offer_seller_is_still_a_conjunct_under_the_www_spelling(client):
    await _seed_www_world()
    await _seed_competitor_offer(price="1.00")
    resp = await client.post(
        f"{BASE}/purchases", json=_body(merchant_domain="www.brand-pg.example")
    )
    assert resp.status_code == 202, resp.text
    row = await ledger.get_purchase_internal(resp.json()["purchase_id"])
    assert row["our_price_minor"] == 4250


async def test_the_202_body_is_unchanged_by_canonical_matching(client):
    await _seed_www_world()
    for spelling in ("www.brand-pg.example", "brand-pg.example"):
        resp = await client.post(f"{BASE}/purchases", json=_body(merchant_domain=spelling))
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["purchase_id"].startswith("rp_")
        assert sorted(body) == ["poll_after_seconds", "purchase_id", "status"]
        assert {k: v for k, v in body.items() if k != "purchase_id"} == {
            "status": "resolving",
            "poll_after_seconds": svc.POLL_INTERVALS["resolving"],
        }


async def test_a_retry_in_the_other_spelling_replays_the_same_purchase(client):
    await _seed_www_world()
    first = await client.post(
        f"{BASE}/purchases",
        json=_body(merchant_domain="www.brand-pg.example", idempotency_key="k-pg-www"),
    )
    again = await client.post(
        f"{BASE}/purchases",
        json=_body(merchant_domain="Brand-PG.example", idempotency_key="k-pg-www"),
    )
    assert first.status_code == again.status_code == 202, again.text
    assert again.json()["purchase_id"] == first.json()["purchase_id"]


def _positive_preflight(host: str):
    from services.shopify_cart_link_preflight import PreflightResult, Verdict

    return PreflightResult(
        host=host, verdict=Verdict.ELIGIBLE, retryable=False, market="US",
        variant_id="50041364447509", variant_source="caller", payment_methods=("Airwallex",),
        card_available=True, landed_price_minor=4250, landed_currency="USD", final_status=200,
        final_host=host,
        chain=((302, f"https://{host}/cart/1:1"), (200, f"https://{host}/checkouts/cn/T")),
    )


@pytest.mark.parametrize(
    "eligibility_domain, product_domain, requested",
    [
        (PG_CANONICAL_DOMAIN, PG_WWW_PRODUCT_DOMAIN, "WWW.BRAND-PG.EXAMPLE"),
        # The sweep folds `www.www.brand-pg.example` to `www.brand-pg.example` and `record_check`
        # folds that again; only the canonical merchant, folded by `is_purchasable`, reads it.
        ("www.www.brand-pg.example", "www.www.brand-pg.example", "www.www.brand-pg.example"),
    ],
)
async def test_the_purchasability_lookup_is_keyed_as_the_sweep_keys_it(
    client, monkeypatch, eligibility_domain, product_domain, requested
):
    """The fact is written by the SWEEP'S OWN population and writer, so the route is tested
    against the key production writes rather than a key this test chose."""
    import db.merchant_purchasability as facts
    import jobs.merchant_purchasability_sweep as sweep

    monkeypatch.setenv("MERCHANT_PURCHASABILITY_ENFORCE", "1")
    monkeypatch.delenv("MERCHANT_PURCHASABILITY_BUYER_VANTAGE", raising=False)
    await _seed_www_world(product_domain=product_domain, eligibility_domain=eligibility_domain)

    refused = await client.post(f"{BASE}/purchases", json=_body(merchant_domain=requested))
    assert refused.status_code == 409
    assert _error(refused) == "merchant_not_purchasable", "control: no fact yet"

    targets = await sweep.load_population(50)
    assert len(targets) == 1
    for target in targets:
        assert await facts.record_check(
            target.domain, target.market, _positive_preflight(target.domain)
        ) is not None
    resp = await client.post(f"{BASE}/purchases", json=_body(merchant_domain=requested))
    assert resp.status_code == 202, resp.text


async def test_the_cart_link_lane_keeps_the_host_it_was_given(client, monkeypatch):
    """NOT canonicalised: the permalink's host and the storefront evidence's host are compared
    byte-for-byte downstream, so the cart lane reads the catalog under, and builds its URL on,
    the host the door observed."""
    await _seed_catalog(domain="www.brand-pg.example")
    await database.execute(
        "UPDATE catalog_products SET seller_ref = 'm_pg' WHERE product_key = :pk",
        {"pk": PRODUCT_KEY},
    )
    await database.execute(
        "UPDATE catalog_skus SET source_variant_id = '50041364447509' WHERE sku_key = :sk",
        {"sk": SKU_KEY},
    )
    await database.execute(
        "INSERT INTO tierb_cart_link_eligibility "
        "(shop_domain, market, verdict, checked_at, consecutive_same) "
        "VALUES (:domain, 'US', 'ELIGIBLE', now(), 1)",
        {"domain": PG_CANONICAL_DOMAIN},
    )
    monkeypatch.setenv("REAP_AGENTIC_CART_LINK_ENABLED", "1")
    monkeypatch.setattr(routes_reap.rc, "CART_LINK_QUOTE_FIELD", "hypotheticalCartUrl")
    resp = await client.post(
        f"{BASE}/purchases",
        json=_body(item_source="cart_link", merchant_domain="www.brand-pg.example"),
    )
    assert resp.status_code == 202, resp.text
    purchase = await ledger.get_purchase_internal(resp.json()["purchase_id"])
    assert purchase["cart_url"].startswith("https://www.brand-pg.example/cart/50041364447509:1?")
    assert purchase["merchant_domain"] == "www.brand-pg.example"


async def test_a_www_spelled_shopify_product_gives_the_sweep_its_variant_and_price():
    """THE SWEEP'S CATALOG HINT, on Postgres. `jobs/merchant_purchasability_sweep`'s
    `_CATALOG_VARIANT_SQL` / `_CATALOG_PRICE_SQL` compared a bare `lower(source_domain)` to a
    canonical population key, so a `www.Brand` Shopify merchant got no variant and no expected
    price. A lookalike under `www-brand-pg.example` (which, minus four characters, IS
    `brand-pg.example`) with a SHORTER variant id is what the variant query's ordering would pick
    if the fold ever accepted a `www` with no dot."""
    import jobs.merchant_purchasability_sweep as sweep

    await _seed_catalog(domain=PG_WWW_PRODUCT_DOMAIN)
    await _seed_eligibility(domain=PG_CANONICAL_DOMAIN)
    lookalike = "prod::m_pg_lookalike::shopify::2002"
    await database.execute(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, "
        "title, source_domain) VALUES (:pk, 'm_pg_lookalike', 'shopify', '2002', 'Lookalike', "
        "'www-brand-pg.example')",
        {"pk": lookalike},
    )
    await database.execute(
        "INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform, source_product_id, "
        "source_variant_id, title, currency) VALUES (:sk, :pk, 'm_pg_lookalike', 'shopify', "
        "'2002', '9', 'One', 'USD')",
        {"sk": f"sku::{lookalike}::v", "pk": lookalike},
    )

    targets = {t.domain: t for t in await sweep.load_population(50)}
    target = targets[PG_CANONICAL_DOMAIN]
    assert target.variant_id == "v1", "no catalog variant for a `www.` Shopify merchant"
    assert (target.expected_price_minor, target.expected_currency) == (4250, "USD")


async def test_a_population_row_that_is_not_a_bare_host_is_not_swept():
    """The route can never admit it, so a fact about it would gate nothing."""
    import jobs.merchant_purchasability_sweep as sweep

    await _seed_eligibility(domain=PG_CANONICAL_DOMAIN)
    await _seed_eligibility(domain="https://urlshaped-pg.example/")
    domains = {t.domain for t in await sweep.load_population(50)}
    assert domains == {PG_CANONICAL_DOMAIN}


async def test_the_sql_fold_agrees_with_the_python_canonicaliser():
    """The fold lifted out of the route's own two statements, run on Postgres against the
    Python canonicaliser. Includes the mixed-case inputs Postgres' case-sensitive LIKE would get
    wrong without the inner `lower()`."""
    import re as _re

    pattern = _re.compile(
        r"CASE WHEN lower\((?P<col>[\w.]+)\) LIKE 'www\.%'\s+THEN substr\(lower\((?P=col)\), 5\)"
        r"\s+ELSE lower\((?P=col)\) END = :merchant_domain"
    )
    found = []
    for statement in (routes_reap._ELIGIBILITY_SQL, routes_reap._PRODUCT_SQL):
        match = pattern.search(statement)
        assert match, "the canonical fold is missing from a statement the variant lane matches on"
        text = match.group(0).split(" = :merchant_domain")[0]
        found.append(" ".join(text.replace(match.group("col"), "COL").split()))
    assert found[0] == found[1], "the two statements fold the domain differently"

    expression = found[0].replace("COL", "CAST(:h AS TEXT)")
    for host in (
        "brand.example", "www.brand.example", "WWW.Brand.Example", "www.www.brand.example",
        "wwwbrand.example", "shop.brand.example", "www-brand.example",
    ):
        folded = await database.fetch_val(f"SELECT {expression}", {"h": host})
        assert folded == routes_reap.canonical_merchant_domain(host), host


# ── the suite must leave the shared tables as it found them ─────────────────────────────────


@pytest.fixture(scope="module", autouse=True)
def _leaves_no_rows_behind():
    """AFTER EVERY TEST IN THIS FILE, assert the shared tables are empty.

    WHY THIS EXISTS AND WHY IT IS NOT A TEST. A test asserting "the catalog is empty" would run
    with the per-test fixture's SETUP already done, so it would pass on the exact build that
    shipped the leak — setup-only cleaning makes every test start clean and says nothing about
    what it leaves. The property is about TEARDOWN, so the check has to outlive the tests. A
    module-scoped fixture's finalizer runs after the last function-scoped teardown, which is
    precisely the moment the next file in the gate's alphabetical order begins.

    SYNCHRONOUS, ON ITS OWN CONNECTION, DELIBERATELY. `asyncio_mode = auto` gives this repo a
    FUNCTION-scoped event loop; a module-scoped async fixture would need its own loop scope and
    would be a second way for this check to fail for reasons that are not about the database.
    `_build_catalog_tables` already opens a sync SQLAlchemy engine in this file, so this is the
    house pattern, and a connection of its own cannot be disturbed by whatever state the last
    test left the shared `database` object in.

    IT ASSERTS RATHER THAN CLEANS. Cleaning here would hide the defect from itself: the gate
    would stay green while the fixture's teardown quietly did nothing. The failure names the
    table and the count, and it fails THIS file rather than the innocent one that runs next —
    which is the whole point, because the four tests this leak actually broke were in
    tests/test_backfill_variant_identity_skus_postgres.py and had nothing to do with Reap.
    """
    yield
    if not _IS_PG:
        return

    import sqlalchemy

    engine = sqlalchemy.create_engine(DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://"))
    dirty = {}
    try:
        with engine.connect() as conn:
            for table in _SHARED_TABLES:
                try:
                    count = conn.execute(sqlalchemy.text(f"SELECT COUNT(*) FROM {table}")).scalar()
                except Exception:  # noqa: BLE001 — a table this run never built is not a leak
                    continue
                if count:
                    dirty[table] = count
    finally:
        engine.dispose()

    assert not dirty, (
        f"this suite left rows in shared tables: {dirty}. The Postgres gate runs every "
        f"tests/test_*_postgres.py in ONE process against ONE database, in alphabetical order, "
        f"and the next file asserts these are empty. Clean in the _db fixture's teardown, not "
        f"only in its setup."
    )
