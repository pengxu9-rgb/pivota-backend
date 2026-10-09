"""retire_superseded_brand_keys.SEARCHABLE_SQL, executed on Postgres against a scratch schema.

THE FILENAME IS LOAD-BEARING (`.github/workflows/postgres-dialect-gate.yml` globs `tests/test_*_postgres.py`).

What only a real query proves (queue review of #2426): a product is searchable by ITS OWN trust row
(subject_type 'product', subject_key = product_key, serving_decision 'public') and a recall lifecycle
(NULL, validated, published). catalog_row_trust also holds offer / listing / content_key rows whose nullable
`product_key` names the product (mig 136); a public one of those must NOT make an unsearchable product look
searchable -- that is the direction in which the drain would retire the findable old row.

The tables are minimal stubs in a PRIVATE schema (dropped after), never under the shared gate schema: the
gate files share one database and a stub under a real table's name there breaks every sibling
(tests/test_canonical_feed_tombstoned_flag_postgres.py).
"""
import os
from urllib.parse import urlsplit

import pytest

from scripts import retire_superseded_brand_keys as tool

URL = os.getenv("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL.startswith("postgres"), reason="needs a Postgres DATABASE_URL")
_SCHEMA = f"retire_searchable_{os.getpid()}"


@pytest.fixture
async def db():
    import asyncpg
    import databases

    dbname = urlsplit(URL).path.rsplit("/", 1)[-1]
    if "test" not in dbname and dbname != "pivota_dialect_check":
        pytest.skip("scratch-schema tests require a test database")
    conn = await asyncpg.connect(URL)
    try:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE; CREATE SCHEMA {_SCHEMA};")
        await conn.execute(f"""
            CREATE TABLE {_SCHEMA}.catalog_products (product_key TEXT PRIMARY KEY, pdp_lifecycle_stage TEXT);
            CREATE TABLE {_SCHEMA}.catalog_row_trust (
                subject_type TEXT NOT NULL CHECK (subject_type IN ('product','offer','listing','content_key')),
                subject_key TEXT NOT NULL, product_key TEXT NULL, serving_decision TEXT NOT NULL,
                PRIMARY KEY (subject_type, subject_key));
        """)
    finally:
        await conn.close()
    database = databases.Database(URL, server_settings={"search_path": _SCHEMA})
    await database.connect()
    try:
        yield database
    finally:
        await database.disconnect()
        conn = await asyncpg.connect(URL)
        try:
            await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        finally:
            await conn.close()


async def _seed(db, products, trust):
    for key, stage in products:
        await db.execute("INSERT INTO catalog_products VALUES (:k, :s)", {"k": key, "s": stage})
    for subject_type, subject_key, product_key, decision in trust:
        await db.execute("INSERT INTO catalog_row_trust VALUES (:t, :sk, :pk, :d)",
                         {"t": subject_type, "sk": subject_key, "pk": product_key, "d": decision})


async def test_only_the_product_s_own_public_trust_row_makes_it_searchable(db, monkeypatch):
    await _seed(db, products=[("p_own", "published"), ("p_offer_only", "published"), ("p_listing_only", None),
                              ("p_ck_only", "validated"), ("p_shadow", "published"), ("p_candidate", "candidate"),
                              ("p_null_stage", None), ("p_no_trust", "published")],
                trust=[("product", "p_own", "p_own", "public"),
                       # public trust rows for OTHER subjects that carry the product_key: never enough
                       ("offer", "off_1", "p_offer_only", "public"),
                       ("listing", "lst_1", "p_listing_only", "public"),
                       ("content_key", "ck_1", "p_ck_only", "public"),
                       ("product", "p_shadow", "p_shadow", "shadow"),
                       ("product", "p_candidate", "p_candidate", "public"),
                       ("product", "p_null_stage", "p_null_stage", "public"),
                       # a second public row for p_own under another subject: no duplicate, no effect
                       ("offer", "off_2", "p_own", "public")])
    monkeypatch.setattr(tool, "database", db)
    keys = ["p_own", "p_offer_only", "p_listing_only", "p_ck_only", "p_shadow", "p_candidate",
            "p_null_stage", "p_no_trust", "p_absent"]
    assert await tool.load_searchable(keys) == {"p_own", "p_null_stage"}
    rows = await db.fetch_all(tool.SEARCHABLE_SQL, {"keys": keys})
    assert sorted(r["product_key"] for r in rows) == ["p_null_stage", "p_own"]  # one row each, no duplicates


async def test_a_new_key_public_only_through_an_offer_row_is_kept(db, monkeypatch):
    """The unsafe direction end to end: the old row is findable, the new one only LOOKS findable through an
    offer's trust row -- select_retirable must keep the old row."""
    await _seed(db, products=[("old0", "published"), ("new0", "published")],
                trust=[("product", "old0", "old0", "public"), ("offer", "off_new0", "new0", "public")])
    monkeypatch.setattr(tool, "database", db)
    searchable = await tool.load_searchable(["old0", "new0"])
    out = tool.select_retirable([{"stale_key": "old0", "new_key": "new0", "brand": "B", "title": "t"}],
                                {"old0": {"suppression_reason": None, "source_domain": "store.example"}},
                                {"new0"}, "store.example", serving={"old0", "new0"}, searchable=searchable)
    assert out["live"] == [] and [c["stale_key"] for c in out["new_not_serving"]] == ["old0"]


async def test_the_post_retire_trust_check_reads_only_the_product_s_own_public_row(db, monkeypatch):
    """PUBLIC_TRUST_SQL (write_retire's check after the trust refresh) joins the way the gateway's serving index
    does: a retired key reads still-public only by ITS product trust row, never by an offer / listing row that
    names it, and a key with no trust row is not public."""
    await _seed(db, products=[("k_public", None), ("k_blocked", None), ("k_offer_only", None), ("k_none", None)],
                trust=[("product", "k_public", "k_public", "public"),
                       ("product", "k_blocked", "k_blocked", "blocked"),
                       ("offer", "off_1", "k_offer_only", "public"),
                       ("listing", "lst_1", "k_blocked", "public")])
    rows = await db.fetch_all(tool.PUBLIC_TRUST_SQL,
                              {"keys": ["k_public", "k_blocked", "k_offer_only", "k_none", "k_absent"]})
    assert [r["subject_key"] for r in rows] == ["k_public"]
