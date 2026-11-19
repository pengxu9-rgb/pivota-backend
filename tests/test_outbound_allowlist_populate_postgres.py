"""The allowlist populate job on real Postgres (the dialect gate): the plan's READ ONLY transaction, the table's real
unique index and integer ids, insert-only apply, and rollback of exactly one batch. example.test hosts only."""
from __future__ import annotations

import asyncio
import os

import pytest

from services import outbound_allowlist_populate as core

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(not _IS_PG, reason="requires a real Postgres DATABASE_URL (postgres dialect gate)")

TABLES = ("outbound_link_rules", "external_product_seeds", "outbound_link_allowed_domains")


async def _with_db(fn):
    from databases import Database

    db = Database(DATABASE_URL)
    await db.connect()
    try:
        for t in reversed(TABLES):
            await db.execute(f"DROP TABLE IF EXISTS olp_twin_{t}")
        # Own tables, named apart from the real ones, so the shared gate database is never touched; the module's SQL
        # is pointed at them by swapping its table names for the duration of the test.
        await db.execute("CREATE TABLE olp_twin_outbound_link_rules (id text PRIMARY KEY, market varchar(8) NOT NULL, "
                         "destination_url text NOT NULL, status varchar(16) NOT NULL)")
        await db.execute("CREATE TABLE olp_twin_external_product_seeds (id text PRIMARY KEY, market text NOT NULL, "
                         "canonical_url text, destination_url text, status text NOT NULL DEFAULT 'active')")
        await db.execute("CREATE TABLE olp_twin_outbound_link_allowed_domains (id serial PRIMARY KEY, market varchar(8) NOT NULL, "
                         "domain varchar(256) NOT NULL, status varchar(16) NOT NULL DEFAULT 'active', notes text, "
                         "created_by varchar(128), created_at timestamp DEFAULT now(), updated_at timestamp DEFAULT now())")
        await db.execute("CREATE UNIQUE INDEX olp_twin_allow_md ON olp_twin_outbound_link_allowed_domains (market, domain)")
        await db.execute("INSERT INTO olp_twin_outbound_link_rules VALUES ('r1','US','https://shop.store-one.test/p','published'),"
                         "('r2','SG','https://store-two.test/p','published'),('r3','USA','https://store-four.test/p','published')")
        await db.execute("INSERT INTO olp_twin_external_product_seeds VALUES ('s1','US','https://brand-a.test/p',"
                         "'https://brand-a.test/p?x=1','active'),('s2','*','https://brand-star.test/p',NULL,'active')")
        await db.execute("INSERT INTO olp_twin_outbound_link_allowed_domains (market, domain, status, created_by) "
                         "VALUES ('SG','store-two.test','active','emp_1')")
        return await fn(db)
    finally:
        for t in reversed(TABLES):
            await db.execute(f"DROP TABLE IF EXISTS olp_twin_{t}")
        await db.disconnect()


class _Twin:
    """The module's database calls, pointed at the twin tables."""

    def __init__(self, db):
        self.db, self.url = db, db.url

    @staticmethod
    def _q(q):
        for t in TABLES:
            q = q.replace(t, f"olp_twin_{t}")
        return q

    def transaction(self):
        return self.db.transaction()

    async def fetch_all(self, q, v=None):
        return await self.db.fetch_all(self._q(q), v)

    async def fetch_one(self, q, v=None):
        return await self.db.fetch_one(self._q(q), v)

    async def execute(self, q, v=None):
        return await self.db.execute(self._q(q), v)


async def _rows(db):
    return sorted(tuple(dict(r).values()) for r in await db.fetch_all(
        "SELECT market, domain, status, created_by FROM olp_twin_outbound_link_allowed_domains"))


def test_plan_apply_rollback_on_postgres() -> None:
    async def go(db):
        twin = _Twin(db)
        before = await _rows(db)
        plan = await core.read_plan(twin, batch_id="olp-twin-1")
        assert await _rows(db) == before, "the plan wrote nothing"
        assert [(e["market"], e["domain"]) for e in plan["entries"]] == [("US", "brand-a.test"), ("US", "store-one.test")]
        receipt = await core.apply_plan(twin, plan)
        assert receipt["inserted"] == 2 and all(isinstance(r[0], int) for r in receipt["rows"])
        assert await _rows(db) == sorted(before + [("US", "brand-a.test", "disabled", "allowlist_populate:olp-twin-1"),
                                                   ("US", "store-one.test", "disabled", "allowlist_populate:olp-twin-1")])
        with pytest.raises(core.Refused):
            await core.apply_plan(twin, plan)
        assert (await core.read_plan(twin, batch_id="olp-twin-2"))["entries"] == []
        assert (await core.rollback_batch(twin, receipt))["deleted"] == 2
        assert await _rows(db) == before

    asyncio.run(_with_db(go))


def test_the_plan_transaction_is_read_only_on_postgres() -> None:
    async def go(db):
        async with db.transaction():
            await core._read_only(db)
            with pytest.raises(Exception, match="read-only"):
                await db.execute("INSERT INTO olp_twin_outbound_link_allowed_domains (market, domain) VALUES ('US','x.test')")

    asyncio.run(_with_db(go))
