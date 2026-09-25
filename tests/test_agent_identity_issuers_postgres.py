"""agent_identity_issuers.upsert_issuer against REAL Postgres — re-registration must UPDATE.

THE FILENAME IS LOAD-BEARING (`.github/workflows/postgres-dialect-gate.yml` globs
`tests/test_*_postgres.py`).

THE DEFECT (prod, 2026-09-25, web-01152-yan, 21 occurrences): registering an (agent_id, issuer)
that already had a row 500'd with

    sqlalchemy.exc.ArgumentError: This text() construct doesn't define a bound parameter
    named 'agent_id'

The UPDATE branch passed the whole INSERT params dict ({agent_id, issuer, ...}) to an UPDATE that
references neither. `databases` 0.7.0 wraps a str query in text() and calls
`.bindparams(**values)`, which refuses any key the SQL does not name. The existing federated
tests monkeypatch `upsert_issuer` away, so no test ever ran the UPDATE's SQL.

Runs in its own scratch schema against the migration's real DDL:

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        .venv/bin/python -m pytest tests/test_agent_identity_issuers_postgres.py
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

_MIGRATION = Path(__file__).resolve().parent.parent / "db/migrations/193_agent_identity_issuers.sql"
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_SCHEMA = f"agent_identity_issuers_test_{os.getpid()}"


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to create scratch schemas in database {dbname!r} — throwaway only")


def _async_url() -> str:
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


@pytest.fixture(autouse=True)
async def _scratch_db(monkeypatch):
    import databases

    from db.sql_migrations import split_statements
    import db.agent_identity_issuers as store

    _assert_throwaway_database()
    admin = databases.Database(_async_url())
    await admin.connect()
    await admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await admin.execute(f"CREATE SCHEMA {_SCHEMA}")
    scoped = databases.Database(_async_url(), server_settings={"search_path": _SCHEMA})
    await scoped.connect()
    try:
        # The migration prod applied, not a hand-typed lookalike.
        for statement in split_statements(_MIGRATION.read_text()):
            await scoped.execute(statement)
        monkeypatch.setattr(store, "database", scoped)
        monkeypatch.setattr(store, "_DDL_READY", True)  # applied above; the backstop must not run
        yield scoped
    finally:
        await scoped.disconnect()
        await admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await admin.disconnect()


def _reg(**over):
    from db.agent_identity_issuers import IssuerRegistration

    base = dict(
        issuer=f"https://idp-{uuid.uuid4().hex[:8]}.example",
        jwks_uri="https://idp.example/jwks.json",
        audience="https://api.pivota.cc",
        algs=["RS256"],
        authorized_party=None,
        required_scopes=None,
    )
    base.update(over)
    return IssuerRegistration(**base)


async def test_registering_the_same_agent_and_issuer_twice_updates_the_row(_scratch_db):
    from db.agent_identity_issuers import upsert_issuer

    reg = _reg()
    first = await upsert_issuer("agent_a", reg, jwks_ok=False)
    assert first["status"] == "active"
    assert first["last_jwks_ok_at"] is None  # jwks_ok=False on the INSERT branch

    # Pre-fix this raised ArgumentError("... bound parameter named 'agent_id'").
    second = await upsert_issuer(
        "agent_a",
        _reg(
            issuer=reg.issuer,
            jwks_uri="https://idp.example/v2/jwks.json",
            audience="https://commerce.mcp.pivota.cc/mcp",
            algs=["ES256", "EdDSA"],
            authorized_party="minds-web",
            required_scopes=["checkout"],
        ),
        jwks_ok=True,
    )
    assert second["id"] == first["id"]  # updated in place, not a second row
    assert second["jwks_uri"] == "https://idp.example/v2/jwks.json"
    assert second["audience"] == "https://commerce.mcp.pivota.cc/mcp"
    assert second["algs"] == ["ES256", "EdDSA"]
    assert second["authorized_party"] == "minds-web"
    assert second["required_scopes"] == ["checkout"]
    assert second["last_jwks_ok_at"] is not None  # jwks_ok=True landed on the UPDATE branch

    count = await _scratch_db.fetch_val(
        "SELECT COUNT(*) FROM agent_identity_issuers WHERE agent_id = :a AND issuer = :i",
        {"a": "agent_a", "i": reg.issuer},
    )
    assert count == 1


async def test_reregistering_a_disabled_issuer_reactivates_it(_scratch_db):
    from db.agent_identity_issuers import disable_issuer, get_active_issuer, upsert_issuer

    reg = _reg()
    first = await upsert_issuer("agent_a", reg, jwks_ok=True)
    assert await disable_issuer("agent_a", first["id"]) is True
    assert await get_active_issuer("agent_a", reg.issuer) is None

    again = await upsert_issuer("agent_a", reg, jwks_ok=False)
    assert again["id"] == first["id"] and again["status"] == "active"
    # jwks_ok=False on the UPDATE keeps the previous success timestamp (CASE ... ELSE last_jwks_ok_at).
    assert again["last_jwks_ok_at"] == first["last_jwks_ok_at"]
    assert (await get_active_issuer("agent_a", reg.issuer))["id"] == first["id"]
