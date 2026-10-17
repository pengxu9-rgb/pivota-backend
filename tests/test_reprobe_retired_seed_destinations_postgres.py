"""reprobe_retired_seed_destinations.select_targets on real PostgreSQL: the note match, the host
filter, de-duplication, and the READ ONLY transaction. Isolated schema; never run against production."""
from __future__ import annotations

import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL", "").startswith("postgres"), reason="requires test PostgreSQL"
)

ROWS = [
    # id, status, canonical_url, destination_url, notes
    ("r1", "inactive", None, "https://www.fentybeauty.com/products/a",
     "auto-retired 2026-10-15: destination redirected_off_product (http 200)"),
    ("r2", "inactive", "https://murad.com/products/b", "https://x.example/ignored",
     "older note\nauto-retired 2026-10-15: destination redirected_off_product (http 200)"),
    ("a1", "inactive", None, "https://addros.com/products/c",
     "auto-retired 2026-10-14: destination dead_404 (http 404)"),
    ("a2", "inactive", None, "https://www.addros.com/products/d",
     "auto-retired 2026-10-14: destination dead_404 (http 404)"),
    ("o1", "inactive", None, "https://other.example/products/e",
     "auto-retired 2026-10-14: destination dead_404 (http 404)"),
    ("live", "active", None, "https://addros.com/products/f",
     "auto-retired 2026-10-14: destination dead_404 (http 404)"),
    ("wrongday", "inactive", None, "https://addros.com/products/g",
     "auto-retired 2026-10-13: destination dead_404 (http 404)"),
    ("prefix", "inactive", None, "https://fentybeauty.com/products/h",
     "auto-retired 2026-10-15: destination redirected_off_product_x (http 200)"),
]


@pytest.fixture
async def conn():
    import asyncpg

    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    schema = "reprobe_" + uuid.uuid4().hex
    admin = await asyncpg.connect(url)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    try:
        await admin.execute(f'SET search_path TO "{schema}"')
        await admin.execute("CREATE TABLE external_product_seeds (id TEXT PRIMARY KEY, status TEXT NOT NULL, "
                            "canonical_url TEXT, destination_url TEXT, notes TEXT)")
        await admin.executemany("INSERT INTO external_product_seeds VALUES ($1, $2, $3, $4, $5)", ROWS)
        c = await asyncpg.connect(url, server_settings={"search_path": schema})
        try:
            yield c
        finally:
            await c.close()
    finally:
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def test_the_selection_matches_the_note_and_the_host(conn) -> None:
    from scripts.ops import reprobe_retired_seed_destinations as tool

    rows = await tool.select_targets(conn, [("2026-10-15", "redirected_off_product", None),
                                            ("2026-10-14", "dead_404", "addros.com")])
    assert sorted(r["id"] for r in rows) == ["a1", "a2", "r1", "r2"]
    assert {r["id"]: r["url"] for r in rows}["r2"] == "https://murad.com/products/b", "canonical_url first"


async def test_overlapping_selections_are_deduplicated(conn) -> None:
    from scripts.ops import reprobe_retired_seed_destinations as tool

    rows = await tool.select_targets(conn, [("2026-10-14", "dead_404", None), ("2026-10-14", "dead_404", "addros.com")])
    assert sorted(r["id"] for r in rows) == ["a1", "a2", "o1"]


async def test_the_selection_runs_read_only(conn) -> None:
    from scripts.ops import reprobe_retired_seed_destinations as tool

    seen = []

    class Spy:
        """The real connection, with every fetch recording whether the transaction was read-only."""

        def __getattr__(self, name):
            return getattr(conn, name)

        async def fetch(self, sql, *args):
            seen.append(await conn.fetchval("SHOW transaction_read_only"))
            return await conn.fetch(sql, *args)

    await tool.select_targets(Spy(), [("2026-10-15", "redirected_off_product", None)])
    assert seen == ["on"]
