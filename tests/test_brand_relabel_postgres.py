"""services/brand_relabel.py's SQL, executed on Postgres against a scratch schema.

THE FILENAME IS LOAD-BEARING (`.github/workflows/postgres-dialect-gate.yml` globs `tests/test_*_postgres.py`).

What only Postgres proves: the family match `lower(regexp_replace(brand, '[^[:alnum:]]', '', 'g'))`, the
drift-guarded UPDATE ... RETURNING through `databases`, the membership insert's ON CONFLICT, and the jsonb_set
seed rewrite -- forward, then reversed from the manifest back to the exact starting state.

Minimal stub tables in a PRIVATE schema (dropped after), never under the shared gate schema: the gate files
share one database and a stub under a real table's name there breaks every sibling
(tests/test_canonical_feed_tombstoned_flag_postgres.py).
"""
import json
import os
from urllib.parse import urlsplit

import pytest

from services import brand_relabel as rl
from services.catalog_identity import make_content_key
from services.product_group_autogrouper import derive_product_group_id

URL = os.getenv("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL.startswith("postgres"), reason="needs a Postgres DATABASE_URL")
_SCHEMA = f"brand_relabel_{os.getpid()}"


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
            CREATE TABLE {_SCHEMA}.catalog_products (
                product_key TEXT PRIMARY KEY, brand TEXT, title TEXT, gtin TEXT, content_key TEXT,
                merchant_id TEXT, platform TEXT, source_product_id TEXT, source_domain TEXT,
                suppression_reason TEXT, updated_at TIMESTAMPTZ);
            CREATE TABLE {_SCHEMA}.product_group_members (
                product_group_id TEXT NOT NULL, merchant_id TEXT NOT NULL, platform TEXT NOT NULL,
                platform_product_id TEXT NOT NULL, is_primary BOOLEAN, updated_at TIMESTAMPTZ,
                UNIQUE (merchant_id, platform, platform_product_id));
            CREATE TABLE {_SCHEMA}.external_product_seeds (
                id BIGSERIAL PRIMARY KEY, attached_product_key TEXT, seed_data JSONB, updated_at TIMESTAMPTZ);
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


async def _product(db, pk, brand, title, host, *, group=True, suppressed=None, seed=True):
    ck = make_content_key(brand, title)
    await db.execute("""INSERT INTO catalog_products VALUES (:pk, :b, :t, NULL, :ck, :m, 'external_seed', :spid,
                        :host, :sup, NOW())""",
                     {"pk": pk, "b": brand, "t": title, "ck": ck, "m": f"m_{host}", "spid": f"retailer:{pk}",
                      "host": host, "sup": suppressed})
    if group:
        await db.execute("INSERT INTO product_group_members VALUES (:pg, :m, 'external_seed', :spid, TRUE, NOW())",
                         {"pg": derive_product_group_id(ck), "m": f"m_{host}", "spid": f"retailer:{pk}"})
    if seed:
        await db.execute("INSERT INTO external_product_seeds (attached_product_key, seed_data) VALUES (:pk, "
                         "CAST(:d AS jsonb))", {"pk": pk, "d": json.dumps({"brand": brand, "title": title})})
    return ck


async def _state(db):
    rows = await db.fetch_all("SELECT product_key, brand, content_key FROM catalog_products ORDER BY 1")
    groups = await db.fetch_all("SELECT platform_product_id, product_group_id FROM product_group_members ORDER BY 1")
    seeds = await db.fetch_all("SELECT attached_product_key, seed_data ->> 'brand' AS b FROM external_product_seeds "
                               "ORDER BY 1")
    vals = lambda recs: [tuple(dict(r._mapping).values()) for r in recs]  # noqa: E731
    return (vals(rows), vals(groups), vals(seeds))


async def test_relabel_joins_the_canonical_product_and_reverts_exactly(db):
    canon_ck = await _product(db, "ext:retailer:c1", "ETUDE", "Fixing Tint", "dodoskin.com")
    await _product(db, "ext:retailer:a", "ETUDE HOUSE", "Fixing Tint", "luxiface.com")
    await _product(db, "ext:retailer:b", "Etude House", "Fixing Tint", "holiholic.com", group=False)
    await _product(db, "ext:retailer:e", "Etude", "Lash Perm", "theglowbeautyshop.com")
    await _product(db, "ext:retailer:gone", "ETUDE HOUSE", "Old", "luxiface.com", suppressed="x")
    await _product(db, "ext:retailer:other", "Innisfree", "Fixing Tint", "luxiface.com")
    before = await _state(db)

    rows, neighbours = await rl.load(db, ["etudehouse", "etude"], "ETUDE")
    assert sorted(r["product_key"] for r in rows) == ["ext:retailer:a", "ext:retailer:b", "ext:retailer:e"]
    plan = rl.plan_relabel(rows, neighbours, "ETUDE")
    assert plan["counts"] == {"joined_by_title": 2, "case_only": 1}
    manifest = rl.manifest_for(plan)
    counts = await rl.write_moves(db, manifest["moves"])
    assert counts == {"products": 3, "groups": 2, "seeds": 3}

    rows_after, groups_after, seeds_after = await _state(db)
    live = {pk: (b, ck) for pk, b, ck in rows_after}
    assert live["ext:retailer:a"] == ("ETUDE", canon_ck) and live["ext:retailer:b"] == ("ETUDE", canon_ck)
    assert live["ext:retailer:e"][0] == "ETUDE"
    assert live["ext:retailer:gone"][0] == "ETUDE HOUSE" and live["ext:retailer:other"][0] == "Innisfree"
    grp = dict(groups_after)
    assert grp["retailer:ext:retailer:a"] == grp["retailer:ext:retailer:b"] == derive_product_group_id(canon_ck)
    assert dict(seeds_after)["ext:retailer:a"] == "ETUDE" and dict(seeds_after)["ext:retailer:gone"] == "ETUDE HOUSE"

    await rl.write_moves(db, manifest["moves"], reverse=True)
    assert await _state(db) == before


async def test_a_drifted_row_rolls_the_whole_write_back(db):
    await _product(db, "ext:retailer:a", "ETUDE HOUSE", "Fixing Tint", "luxiface.com")
    await _product(db, "ext:retailer:b", "ETUDE HOUSE", "Lash Perm", "luxiface.com")
    rows, neighbours = await rl.load(db, ["etudehouse", "etude"], "ETUDE")
    manifest = rl.manifest_for(rl.plan_relabel(rows, neighbours, "ETUDE"))
    before = await _state(db)
    await db.execute("UPDATE catalog_products SET brand = 'Someone Else' WHERE product_key = 'ext:retailer:b'")
    drifted = await _state(db)
    with pytest.raises(RuntimeError, match="drift"):
        await rl.write_moves(db, manifest["moves"])
    assert await _state(db) == drifted != before  # row a's move rolled back with b's refusal
