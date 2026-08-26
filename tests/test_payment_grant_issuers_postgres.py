"""payment_grant_issuers against REAL Postgres — the store functions and the migration's own
constraints, per the lesson PR #1883 taught: faked-DB tests cannot see a bind or constraint
defect, so every new table ships with a gate that executes the actual SQL.

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        .venv/bin/python -m pytest tests/test_payment_grant_issuers_postgres.py
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL — see the module docstring for the one-line setup",
)

_MIGRATION = Path(__file__).resolve().parent.parent / "db/migrations/203_payment_grant_issuers.sql"
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to drop payment_grant_issuers in database {dbname!r} — throwaway only")


@pytest.fixture(autouse=True)
async def _db():
    from db.database import database
    from db.sql_migrations import split_statements
    import db.payment_grant_issuers as store

    _assert_throwaway_database()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    # Drop first so a constraint deleted from the migration cannot survive via IF NOT EXISTS.
    await database.execute("DROP TABLE IF EXISTS payment_grant_issuers")
    for statement in split_statements(_MIGRATION.read_text()):
        await database.execute(statement)
    store._DDL_READY = True  # the migration IS applied; the backstop must not re-run mid-test
    yield
    store._DDL_READY = False
    if not was_connected and database.is_connected:
        await database.disconnect()


def _reg(**over):
    from db.payment_grant_issuers import PaymentIssuerRegistration

    base = dict(
        issuer=f"https://psp-{uuid.uuid4().hex[:8]}.example",
        jwks_uri="https://psp.example/jwks.json",
        audience="https://commerce.mcp.pivota.cc/mcp",
        algs=["ES256"],
        authorized_party=None,
        methods=["signed_grant"],
        expected_vct=None,
    )
    base.update(over)
    return PaymentIssuerRegistration(**base)


async def test_register_appears_active_and_in_verifier_shape():
    from db.payment_grant_issuers import list_active, upsert_issuer

    reg = _reg()
    row = await upsert_issuer(reg, registered_by="admin@pivota.cc", jwks_ok=True)
    assert row["status"] == "active" and row["registered_by"] == "admin@pivota.cc"
    assert row["last_jwks_ok_at"] is not None  # jwks_ok=True actually landed
    active = await list_active()
    assert [r["issuer"] for r in active] == [reg.issuer]
    assert active[0]["algs"] == ["ES256"] and active[0]["methods"] == ["signed_grant"]


async def test_reregister_updates_in_place_and_reactivates():
    from db.payment_grant_issuers import disable_issuer, list_active, list_all, upsert_issuer

    reg = _reg()
    first = await upsert_issuer(reg, registered_by="a@pivota.cc", jwks_ok=True)
    assert await disable_issuer(first["id"]) is True
    assert await list_active() == []
    assert await disable_issuer(first["id"]) is False  # already disabled -> route 404s honestly

    again = await upsert_issuer(
        _reg(issuer=reg.issuer, methods=["signed_grant", "ap2_mandate"], expected_vct="PaymentMandate"),
        registered_by="b@pivota.cc", jwks_ok=False,
    )
    assert again["id"] == first["id"]  # same row reactivated, not a duplicate
    assert again["status"] == "active" and again["methods"] == ["signed_grant", "ap2_mandate"]
    (active,) = await list_active()
    assert active["expected_vct"] == "PaymentMandate"
    assert len(await list_all()) == 1


async def test_pipe_in_issuer_is_refused_by_the_table_itself():
    from db.database import database

    with pytest.raises(Exception) as err:
        await database.execute(
            """
            INSERT INTO payment_grant_issuers (issuer, jwks_uri, audience, registered_by)
            VALUES ('https://x.example|sub', 'https://x.example/jwks', 'aud', 'test')
            """
        )
    assert "payment_grant_issuers_issuer_no_pipe" in str(err.value)


async def test_methods_check_constraint_holds():
    from db.database import database

    with pytest.raises(Exception) as err:
        await database.execute(
            """
            INSERT INTO payment_grant_issuers (issuer, jwks_uri, audience, methods, registered_by)
            VALUES ('https://x.example', 'https://x.example/jwks', 'aud',
                    ARRAY['settlement']::TEXT[], 'test')
            """
        )
    assert "payment_grant_issuers_methods_check" in str(err.value)


async def test_one_active_owner_per_issuer_string():
    from db.database import database

    await database.execute(
        """
        INSERT INTO payment_grant_issuers (issuer, jwks_uri, audience, registered_by)
        VALUES ('https://dup.example', 'https://dup.example/jwks', 'aud', 'test')
        """
    )
    with pytest.raises(Exception) as err:
        await database.execute(
            """
            INSERT INTO payment_grant_issuers (issuer, jwks_uri, audience, registered_by)
            VALUES ('https://dup.example', 'https://other.example/jwks', 'aud2', 'test')
            """
        )
    assert "payment_grant_issuers_active_issuer_uidx" in str(err.value)


async def test_ddl_backstop_matches_the_migration():
    """The runtime self-heal must declare the SAME schema the migration does — a drifted copy
    is a prod table the tests never saw. Byte-compare information_schema after each."""
    from db.database import database
    import db.payment_grant_issuers as store

    async def snapshot():
        rows = await database.fetch_all(
            """
            SELECT column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_name = 'payment_grant_issuers'
            ORDER BY ordinal_position
            """
        )
        # Indexes too: the review ran the mutant — backstop minus the unique index — and the
        # columns-only compare passed. Deploys skip db/migrations/, so the backstop IS the
        # prod path, and prod without the partial unique index has no one-active-row arbiter.
        idx = await database.fetch_all(
            """
            SELECT indexname, indexdef FROM pg_indexes
            WHERE tablename = 'payment_grant_issuers'
            ORDER BY indexname
            """
        )
        return [tuple(r) for r in rows] + [tuple(r) for r in idx]

    from_migration = await snapshot()
    await database.execute("DROP TABLE payment_grant_issuers")
    store._DDL_READY = False
    await store.ensure_payment_grant_issuers_table()
    from_backstop = await snapshot()
    store._DDL_READY = True
    assert from_backstop == from_migration
