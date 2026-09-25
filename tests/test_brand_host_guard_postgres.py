"""The ADR-008 brand-host guard's finder, executed on real Postgres.

Its host match was `canonical_url ILIKE '%host%'`: `palacebeauty.com` matched every `shoppalacebeauty.com`
URL, so a retailer ingest of palacebeauty.com was held on a conflict with another store's O HUI rows
(2026-09-25). Both halves are pinned against the real `~*` operator: the rows the guard must still find
(same host, `www.`, a port, any case, the `source_domain` column) and the look-alikes it must not.
"""
import os
from urllib.parse import urlsplit

import pytest

URL = os.getenv("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL.startswith("postgres"), reason="requires test Postgres")

PREFIX = "bhg-gate-"

# The dialect gate shares ONE database across files; another file may have created catalog_products first
# with a narrower schema. Add exactly the columns the finder reads, idempotently.
_LIGHTWEIGHT_DDL = """
ALTER TABLE catalog_products ADD COLUMN IF NOT EXISTS brand varchar(255);
ALTER TABLE catalog_products ADD COLUMN IF NOT EXISTS source_domain text;
ALTER TABLE catalog_products ADD COLUMN IF NOT EXISTS canonical_url text;
ALTER TABLE catalog_products ADD COLUMN IF NOT EXISTS content_key text;
ALTER TABLE catalog_products ADD COLUMN IF NOT EXISTS pivota_signature_id text;
ALTER TABLE catalog_products ADD COLUMN IF NOT EXISTS suppression_reason text;
"""


@pytest.fixture
async def db():
    import asyncpg
    from databases import Database
    from sqlalchemy import create_engine

    import db.catalog  # noqa: F401  (registers catalog_products on the MetaData)
    from db.database import metadata

    if "test" not in urlsplit(URL).path.rsplit("/", 1)[-1] and not URL.endswith("pivota_dialect_check"):
        pytest.skip("synthetic guard tests require a test database")
    engine = create_engine(URL)
    metadata.create_all(engine, checkfirst=True)
    engine.dispose()
    connection = await asyncpg.connect(URL)
    try:
        await connection.execute(_LIGHTWEIGHT_DDL)
    finally:
        await connection.close()
    database = Database(URL)
    await database.connect()
    await database.execute("DELETE FROM catalog_products WHERE product_key LIKE :p", {"p": PREFIX + "%"})
    try:
        yield database
    finally:
        await database.execute("DELETE FROM catalog_products WHERE product_key LIKE :p", {"p": PREFIX + "%"})
        await database.disconnect()


async def _row(db, key, *, url, source_domain=None, brand="BHG Brand", merchant="bhg_other"):
    await db.execute(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, brand,"
        " canonical_url, source_domain, pivota_signature_id) VALUES (:k, :m, 'external_seed', :k, 'Lipstick',"
        " :b, :u, :sd, :sig)",
        {"k": PREFIX + key, "m": merchant, "b": brand, "u": url, "sd": source_domain, "sig": PREFIX + "sig-" + key})


async def _find(db, host, merchant="bhg_new"):
    from services.audit_index_intake import _existing_brand_canonical_conflict
    return await _existing_brand_canonical_conflict(
        merchant, {"brand": "bhg brand", "source_domain": host}, database=db)


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://shoppalacebeauty-bhg.com/products/lipstick",   # a suffix host: another store
    "https://palacebeauty-bhg.com.evil.example/products/x",  # the host as a label prefix
    "https://other-bhg.example/r?ref=palacebeauty-bhg.com",  # the host only in the query
    "https://other-bhg.example/palacebeauty-bhg.com/p",      # the host only in the path
])
async def test_a_look_alike_host_is_not_a_conflict(db, url):
    await _row(db, "lookalike", url=url)
    assert await _find(db, "palacebeauty-bhg.com") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://palacebeauty-bhg.com/products/lipstick",
    "http://www.palacebeauty-bhg.com/products/lipstick",
    "https://PALACEBEAUTY-BHG.COM:443/products/lipstick",
    "https://palacebeauty-bhg.com",
    "https://palacebeauty-bhg.com?variant=1",
    "https://us.palacebeauty-bhg.com/products/lipstick",   # a subdomain is the same site
])
async def test_the_same_host_is_still_a_conflict(db, url):
    await _row(db, "same", url=url)
    found = await _find(db, "palacebeauty-bhg.com")
    assert found and found["product_key"] == PREFIX + "same"


@pytest.mark.asyncio
async def test_the_source_domain_column_still_matches_without_a_url(db):
    await _row(db, "sd", url=None, source_domain="palacebeauty-bhg.com")
    assert (await _find(db, "palacebeauty-bhg.com"))["product_key"] == PREFIX + "sd"


@pytest.mark.asyncio
async def test_a_regex_metacharacter_in_the_host_is_literal(db):
    # `.` unescaped would let `palacebeautyXcom` match `palacebeauty.com`.
    await _row(db, "dot", url="https://palacebeauty-bhgxcom/products/x")
    assert await _find(db, "palacebeauty-bhg.com") is None


@pytest.mark.asyncio
async def test_the_same_merchant_is_never_its_own_conflict(db):
    await _row(db, "own", url="https://palacebeauty-bhg.com/p", merchant="bhg_new")
    assert await _find(db, "palacebeauty-bhg.com") is None


@pytest.mark.asyncio
async def test_a_host_passed_with_its_scheme_still_matches(db):
    # WooCommerce store domains are stored as `https://...` and reach the guard as source_domain.
    await _row(db, "scheme", url="https://palacebeauty-bhg.com/products/lipstick")
    assert (await _find(db, "https://palacebeauty-bhg.com"))["product_key"] == PREFIX + "scheme"
    assert (await _find(db, "https://www.palacebeauty-bhg.com/"))["product_key"] == PREFIX + "scheme"
