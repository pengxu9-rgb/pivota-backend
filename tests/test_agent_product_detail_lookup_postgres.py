"""The detail proxy's two lookups (services/agent_product_detail_gateway_proxy.resolve_signature),
executed as written against real PostgreSQL.

The route tests answer these lookups from a fake that never reads the SQL, so review of #2239
could strip the merchant scope and the suppression filter and still see every test pass. These
run the real statements, so each of those guards is pinned where it lives:
* a reference only resolves inside the requested merchant -- a Pivota id included, because the
  gateway lane it feeds is merchant-blind;
* suppressed rows are refused;
* two products for one reference is a refusal, never a pick; one product reached twice is one;
* a caller's `_` / `%` in a name are literal, not LIKE wildcards.

Named test_*_postgres.py so the Postgres dialect gate runs it. Deliberately does not import main.
"""

from __future__ import annotations

import os

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL — production-dialect gate")

A = "merch_obs_aaaaaaaaaaaaaaaa"
B = "merch_obs_bbbbbbbbbbbbbbbb"


@pytest.fixture(scope="module")
def pg_engine():
    import db.catalog  # noqa: F401
    from sqlalchemy import create_engine

    from db.database import metadata

    engine = create_engine(DATABASE_URL)
    metadata.create_all(engine, checkfirst=True)
    yield engine
    engine.dispose()


@pytest.fixture
def seeded(pg_engine):
    from sqlalchemy import text

    def product(conn, key, merchant, sig, *, source_id=None, content_key=None, suppressed=False):
        conn.execute(text(
            "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, "
            " pivota_signature_id, content_key, suppressed_at) "
            "VALUES (:k, :m, 'external_seed', :s, :k, :sig, :ck, CASE WHEN :sup THEN NOW() ELSE NULL END)"),
            {"k": key, "m": merchant, "s": source_id or key, "sig": sig, "ck": content_key, "sup": suppressed})

    def sku(conn, sku_key, key, merchant, variant, sku_code=None, *, suppressed=False):
        conn.execute(text(
            "INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform, source_product_id, "
            " source_variant_id, sku, title, suppressed_at) "
            "VALUES (:sk, :k, :m, 'external_seed', :k, :v, :code, :sk, CASE WHEN :sup THEN NOW() ELSE NULL END)"),
            {"sk": sku_key, "k": key, "m": merchant, "v": variant, "code": sku_code, "sup": suppressed})

    with pg_engine.begin() as conn:
        conn.execute(text("DELETE FROM catalog_skus WHERE merchant_id IN (:a, :b)"), {"a": A, "b": B})
        conn.execute(text("DELETE FROM catalog_products WHERE merchant_id IN (:a, :b)"), {"a": A, "b": B})
        product(conn, "ext:lip-gloss::h1", A, "sig_a_gloss", source_id="brand:gloss1", content_key="ck_a_gloss")
        sku(conn, "sku_a1", "ext:lip-gloss::h1", A, "50856826536257", "32168999")
        # Same product reached through two rows: one by variant id, the other by its SKU column.
        sku(conn, "sku_a2", "ext:lip-gloss::h1", A, "50865870831937", "50856826536257")
        product(conn, "ext:b-balm::h9", B, "sig_b_balm")
        sku(conn, "sku_b1", "ext:b-balm::h9", B, "777")
        product(conn, "ext:gone::h2", A, "sig_a_gone", suppressed=True)
        sku(conn, "sku_gone", "ext:gone::h2", A, "888")
        product(conn, "ext:twin::h3", A, "sig_a_twin1")
        product(conn, "ext:twin::h4", A, "sig_a_twin2")
        product(conn, "ext:aXb::h5", A, "sig_a_axb")
    yield
    with pg_engine.begin() as conn:
        conn.execute(text("DELETE FROM catalog_skus WHERE merchant_id IN (:a, :b)"), {"a": A, "b": B})
        conn.execute(text("DELETE FROM catalog_products WHERE merchant_id IN (:a, :b)"), {"a": A, "b": B})


async def _resolve(**kwargs):
    from databases import Database

    from services.agent_product_detail_gateway_proxy import resolve_signature

    db = Database(DATABASE_URL)
    await db.connect()
    try:
        return await resolve_signature(db, **kwargs)
    finally:
        await db.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("ref", ["sig_a_gloss", "brand:gloss1", "ext:lip-gloss::h1", "ck_a_gloss", "lip-gloss"])
async def test_every_reference_form_resolves_inside_its_merchant(seeded, ref: str) -> None:
    assert await _resolve(merchant_id=A, product_ref=ref) == ("sig_a_gloss", "resolved")


@pytest.mark.asyncio
async def test_a_pivota_id_from_another_merchant_is_refused(seeded) -> None:
    assert await _resolve(merchant_id=A, product_ref="sig_b_balm") == (None, "not_found")
    assert await _resolve(merchant_id=B, product_ref="sig_b_balm") == ("sig_b_balm", "resolved")
    assert await _resolve(merchant_id=A, variant_id="777") == (None, "not_found")


@pytest.mark.asyncio
async def test_suppressed_rows_are_refused(seeded) -> None:
    assert await _resolve(merchant_id=A, product_ref="sig_a_gone") == (None, "not_found")
    assert await _resolve(merchant_id=A, variant_id="888") == (None, "not_found")


@pytest.mark.asyncio
async def test_two_products_is_a_refusal_one_product_twice_is_one(seeded) -> None:
    assert await _resolve(merchant_id=A, product_ref="twin") == (None, "ambiguous")
    assert await _resolve(merchant_id=A, variant_id="50856826536257") == ("sig_a_gloss", "resolved")
    assert await _resolve(merchant_id=A, variant_id="32168999") == ("sig_a_gloss", "resolved")


@pytest.mark.asyncio
async def test_like_metacharacters_in_a_name_are_literal(seeded) -> None:
    # Unescaped, `a_b` would match `ext:aXb::h5` (`_` = any one character) and `%` everything.
    assert await _resolve(merchant_id=A, product_ref="a_b") == (None, "not_found")
    assert await _resolve(merchant_id=A, product_ref="%") == (None, "not_found")
    assert await _resolve(merchant_id=A, product_ref="aXb") == ("sig_a_axb", "resolved")
