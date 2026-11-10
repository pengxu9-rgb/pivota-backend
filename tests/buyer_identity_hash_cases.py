"""S-29 cases: the agent_user_ref hash has its own key, and rows made with the old key still match.

Shared by two collectors, so the one statement the legacy read adds runs on both engines:
  * tests/test_buyer_identity_hash_legacy_read.py — SQLite, a file in tmp_path;
  * tests/test_buyer_identity_hash_legacy_read_postgres.py — Postgres (the dialect gate), inside a
    fresh schema of the gate's DATABASE_URL, so it never touches the gate's real tables.

The owner-keyed tables are built with just the two key columns (plus the link's own columns): the
statement under test reads nothing else. `buyer_identity_links` is built with every column the
link upsert writes, because one case runs the real upsert.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import uuid

import pytest

import db.buyer_vault as buyer_vault

AGENT = "agent_s29"
OTHER_AGENT = "agent_s29_other"
USER_REF = "end-user-placeholder-1"
BUYER = "buy_s29_placeholder"
DEDICATED = "dedicated-test-key"
TOKEN_KEY = "checkout-token-test-key"

#: Written out, not read from the module: a table dropped from the lookup must fail a case.
OWNER_TABLES = (
    "buyer_identity_links",
    "agent_purchases",
    "reap_agentic_purchases",
    "reap_agentic_purchase_keys",
    "reap_unopened_attempt_retirements",
    "a2a_rail_buyer_refs",
    "a2a_rail_purchases",
)


def _hmac_hex(key: str, raw: str) -> str:
    return hmac.new(key.encode(), raw.encode(), hashlib.sha256).hexdigest()


def _is_pg() -> bool:
    url = (os.getenv("DATABASE_URL") or "").strip()
    return url.startswith("postgresql://") or url.startswith("postgres://")


@pytest.fixture(autouse=True)
def hash_env(monkeypatch):
    """Dedicated key set, window at its default (open), the checkout-token key as the legacy key."""
    for name in (
        "BUYER_IDENTITY_LINK_SECRET",
        "BUYER_ACTION_TOKEN_SECRET",
        "CHECKOUT_TOKEN_SECRET",
        "AGENT_CHECKOUT_TOKEN_SECRET",
        "BUYER_IDENTITY_LINK_LEGACY_READ",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BUYER_IDENTITY_LINK_SECRET", DEDICATED)
    monkeypatch.setenv("CHECKOUT_TOKEN_SECRET", TOKEN_KEY)


@pytest.fixture
async def owner_db(monkeypatch, tmp_path):
    import databases

    schema = None
    if _is_pg():
        url = os.environ["DATABASE_URL"].strip()
        schema = f"s29_{uuid.uuid4().hex[:12]}"
        admin = databases.Database(url)
        await admin.connect()
        await admin.execute(f"CREATE SCHEMA {schema}")
        await admin.disconnect()
        db = databases.Database(url, server_settings={"search_path": schema})
        link_id = "id BIGSERIAL PRIMARY KEY"
        ts = "TIMESTAMPTZ"
    else:
        admin = None
        url = f"sqlite+aiosqlite:///{tmp_path / 'owner.db'}"
        db = databases.Database(url)
        link_id = "id INTEGER PRIMARY KEY AUTOINCREMENT"
        ts = "TIMESTAMP"
    await db.connect()
    try:
        for table in OWNER_TABLES:
            if table == "buyer_identity_links":
                continue
            await db.execute(
                f"CREATE TABLE {table} (agent_id VARCHAR(100) NOT NULL, agent_user_ref_hash VARCHAR(64))"
            )
        await db.execute(
            f"""CREATE TABLE buyer_identity_links (
                {link_id},
                agent_id VARCHAR(100) NOT NULL,
                agent_user_ref_hash VARCHAR(64) NOT NULL,
                buyer_id VARCHAR(50) NOT NULL,
                created_at {ts} DEFAULT CURRENT_TIMESTAMP,
                updated_at {ts} DEFAULT CURRENT_TIMESTAMP,
                last_seen_at {ts},
                UNIQUE (agent_id, agent_user_ref_hash)
            )"""
        )
        await db.execute("CREATE TABLE shop_users (id VARCHAR(50) PRIMARY KEY, email VARCHAR(255))")

        import routes.agent_checkout_intents as intents
        import routes.buyer_api as buyer_api

        for module in (buyer_vault, intents, buyer_api):
            monkeypatch.setattr(module, "database", db)
        yield db
    finally:
        await db.disconnect()
        if schema is not None:
            admin = databases.Database(os.environ["DATABASE_URL"].strip())
            await admin.connect()
            await admin.execute(f"DROP SCHEMA {schema} CASCADE")
            await admin.disconnect()


async def _seed(db, table: str, agent_id: str, ref_hash: str) -> None:
    if table == "buyer_identity_links":
        await db.execute(
            "INSERT INTO buyer_identity_links (agent_id, agent_user_ref_hash, buyer_id) VALUES (:a, :h, :b)",
            {"a": agent_id, "h": ref_hash, "b": BUYER},
        )
    else:
        await db.execute(
            f"INSERT INTO {table} (agent_id, agent_user_ref_hash) VALUES (:a, :h)",
            {"a": agent_id, "h": ref_hash},
        )


def legacy_hash() -> str:
    return _hmac_hex(TOKEN_KEY, USER_REF)


def dedicated_hash() -> str:
    return _hmac_hex(DEDICATED, USER_REF)


# --- the legacy read --------------------------------------------------------------------------


async def test_a_new_identity_resolves_to_the_dedicated_key_hash(owner_db):
    assert await buyer_vault.resolve_agent_user_ref_hash(AGENT, USER_REF) == dedicated_hash()


@pytest.mark.parametrize("table", OWNER_TABLES)
async def test_an_identity_with_legacy_rows_in_any_owner_table_keeps_its_legacy_hash(owner_db, table):
    await _seed(owner_db, table, AGENT, legacy_hash())

    assert await buyer_vault.resolve_agent_user_ref_hash(AGENT, USER_REF) == legacy_hash()


async def test_another_agents_legacy_row_does_not_pull_this_identity_back(owner_db):
    await _seed(owner_db, "buyer_identity_links", OTHER_AGENT, legacy_hash())

    assert await buyer_vault.resolve_agent_user_ref_hash(AGENT, USER_REF) == dedicated_hash()


async def test_a_closed_window_ignores_legacy_rows(owner_db, monkeypatch):
    await _seed(owner_db, "buyer_identity_links", AGENT, legacy_hash())
    monkeypatch.setenv("BUYER_IDENTITY_LINK_LEGACY_READ", "off")

    assert await buyer_vault.resolve_agent_user_ref_hash(AGENT, USER_REF) == dedicated_hash()


async def test_a_failed_lookup_answers_the_legacy_hash(owner_db):
    await owner_db.execute("DROP TABLE a2a_rail_purchases")

    assert await buyer_vault.resolve_agent_user_ref_hash(AGENT, USER_REF) == legacy_hash()


# --- the routes that read a link, end to end against the same tables ---------------------------


async def test_checkout_prefill_still_finds_a_link_written_under_the_legacy_key(owner_db):
    import routes.agent_checkout_intents as intents

    await _seed(owner_db, "buyer_identity_links", AGENT, legacy_hash())
    await owner_db.execute(
        "INSERT INTO shop_users (id, email) VALUES (:id, :e)", {"id": BUYER, "e": "buyer@example.com"}
    )

    prefill = await intents._buyer_prefill_from_identity_link(agent_id=AGENT, agent_user_ref=USER_REF)

    assert prefill is not None and prefill["customer_email"] == "buyer@example.com"


async def test_saving_a_legacy_identity_again_updates_its_link_and_adds_none(owner_db):
    import routes.buyer_api as buyer_api

    await _seed(owner_db, "buyer_identity_links", AGENT, legacy_hash())

    written = await buyer_api._upsert_buyer_identity_link(buyer_id=BUYER, agent_id=AGENT, agent_user_ref=USER_REF)

    rows = await owner_db.fetch_all("SELECT agent_user_ref_hash FROM buyer_identity_links WHERE agent_id = :a", {"a": AGENT})
    assert written == legacy_hash()
    assert [dict(r)["agent_user_ref_hash"] for r in rows] == [legacy_hash()]


async def test_saving_a_new_identity_writes_the_dedicated_key_hash(owner_db):
    import routes.buyer_api as buyer_api

    written = await buyer_api._upsert_buyer_identity_link(buyer_id=BUYER, agent_id=AGENT, agent_user_ref=USER_REF)

    rows = await owner_db.fetch_all("SELECT agent_user_ref_hash FROM buyer_identity_links WHERE agent_id = :a", {"a": AGENT})
    assert written == dedicated_hash()
    assert [dict(r)["agent_user_ref_hash"] for r in rows] == [dedicated_hash()]
