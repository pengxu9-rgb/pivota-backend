"""The AgenPas internal purchase read's LEDGER SQL on Postgres: db/agent_purchase_ledger.get_for_agent and
get_by_rail_id_for_agent, the two agent-scoped parent reads routes/agent_internal_commerce_purchases.py
uses. What only Postgres can show: the statements run on the production dialect, scope by BOTH the agent
and the id, and hand back the owner hash the route passes to the view builders.

The ROUTE is tested on SQLite (tests/test_agent_internal_commerce_purchases.py, over the real app). This
file calls the ledger directly.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_dialect_check \\
        pytest tests/test_agent_internal_commerce_purchases_postgres.py

THIS MODULE MUST NOT IMPORT `main` (the gate runs every test_*_postgres.py in one process: main registers
every model on the shared metadata at collection time and poisons later create_all calls).
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL; the production-dialect gate for the internal read's ledger SQL",
)

assert "main" not in sys.modules or not _IS_PG, (
    "main is imported: it registers every model on the shared metadata at collection time "
    "and poisons later create_all calls in the Postgres gate"
)

AGENT = "agent_internal_read_pg"
OTHER_AGENT = "agent_internal_read_pg_other"
OWNER = "h" * 64


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to touch agent_purchases in database {dbname!r}: throwaway only")


@pytest.fixture(autouse=True)
async def _db():
    from db.database import database
    from db.schema_guard import ensure_required_schema_light

    _assert_throwaway_database()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await ensure_required_schema_light()
    await database.execute("DELETE FROM agent_purchases WHERE agent_id IN (:a, :b)", {"a": AGENT, "b": OTHER_AGENT})
    try:
        yield
    finally:
        try:
            await database.execute("DELETE FROM agent_purchases WHERE agent_id IN (:a, :b)",
                                   {"a": AGENT, "b": OTHER_AGENT})
            await database.execute("DELETE FROM a2a_rail_purchases WHERE agent_id IN (:a, :b)",
                                   {"a": AGENT, "b": OTHER_AGENT})
        except Exception:  # noqa: BLE001 - a table this run never built is not a leak
            pass
        if not was_connected and database.is_connected:
            await database.disconnect()


async def _a2a_purchase(agent_id: str = AGENT) -> str:
    """An A2A purchase with its parent row, as the create route leaves them."""
    import db.a2a_rail_ledger as a2a_ledger
    import db.agent_purchase_ledger as purchases

    row = await a2a_ledger.create_purchase(
        agent_id=agent_id, agent_user_ref_hash=OWNER, merchant_domain="merchant.example",
        checkout_url="https://merchant.example/cart/40000000001:1", currency="USD", our_price_minor=4650)
    await purchases.ensure_a2a_parent(row["id"], basis="backfill")
    return row["id"]


async def test_the_agent_scoped_reads_find_the_agents_own_purchase_by_either_id():
    import db.agent_purchase_ledger as purchases

    rail_id = await _a2a_purchase()
    by_rail = await purchases.get_by_rail_id_for_agent("a2a", rail_id, AGENT)
    assert by_rail is not None and by_rail["rail_purchase_id"] == rail_id and by_rail["rail"] == "a2a"
    # The owner hash comes back for the route's owner-scoped view builders (never into a response).
    assert by_rail["agent_user_ref_hash"] == OWNER
    by_parent = await purchases.get_for_agent(by_rail["id"], AGENT)
    assert by_parent is not None and by_parent["id"] == by_rail["id"]
    assert by_parent["agent_user_ref_hash"] == OWNER
    assert by_parent["created_at"] is not None and by_parent["created_at"].tzinfo is not None


async def test_another_agents_purchase_is_none_like_no_purchase():
    import db.agent_purchase_ledger as purchases

    rail_id = await _a2a_purchase(OTHER_AGENT)
    parent_id = (await purchases.get_by_rail_id_for_agent("a2a", rail_id, OTHER_AGENT))["id"]
    assert await purchases.get_by_rail_id_for_agent("a2a", rail_id, AGENT) is None
    assert await purchases.get_for_agent(parent_id, AGENT) is None


async def test_unknown_ids_and_the_wrong_rail_are_none():
    import db.agent_purchase_ledger as purchases

    rail_id = await _a2a_purchase()
    assert await purchases.get_for_agent("pp_" + "0" * 24, AGENT) is None
    assert await purchases.get_by_rail_id_for_agent("a2a", "ap_" + "0" * 24, AGENT) is None
    assert await purchases.get_by_rail_id_for_agent("reap", rail_id, AGENT) is None


async def test_the_agent_name_read_and_the_rename_hide_run_on_postgres():
    """db/agents.get_buyer_display_name's SELECT and portal_name_rename_hide's UPDATE are raw SQL on a column
    the model does not have: here they run on the production dialect, on the table the model builds plus the
    column the boot heal adds."""
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    import db.agents as agents_db
    from db.database import database
    from db.schema_guard import ensure_required_schema_light

    await database.execute(str(CreateTable(agents_db.agents, if_not_exists=True).compile(dialect=postgresql.dialect())))
    await ensure_required_schema_light()  # the heal adds buyer_display_name now that agents exists
    await database.execute("DELETE FROM agents WHERE agent_id = :a", {"a": AGENT})
    try:
        await database.execute(
            "INSERT INTO agents (agent_id, agent_name, agent_type, api_key, api_key_hash, is_active) "
            "VALUES (:a, 'Example Assistant', 'custom', :k, :h, TRUE)",
            {"a": AGENT, "k": f"redacted:{AGENT}", "h": "0" * 64})
        assert await agents_db.get_buyer_display_name(AGENT) == "Example Assistant"
        # The same name: nothing hidden.
        await database.execute(*agents_db.portal_name_rename_hide(AGENT, "Example Assistant"))
        assert await agents_db.get_buyer_display_name(AGENT) == "Example Assistant"
        # A rename, as PUT /agents/{agent_id} runs it: the hide, then the write, in one transaction.
        async with database.transaction():
            await database.execute(*agents_db.portal_name_rename_hide(AGENT, "Renamed Assistant"))
            await database.execute("UPDATE agents SET agent_name = 'Renamed Assistant' WHERE agent_id = :a",
                                   {"a": AGENT})
        row = await database.fetch_one("SELECT buyer_display_name FROM agents WHERE agent_id = :a", {"a": AGENT})
        assert dict(row)["buyer_display_name"] == ""
        assert await agents_db.get_buyer_display_name(AGENT) is None
        # A hand-set name is left as it is by a later rename, and decides.
        await database.execute("UPDATE agents SET buyer_display_name = 'Hand Set Name' WHERE agent_id = :a",
                               {"a": AGENT})
        await database.execute(*agents_db.portal_name_rename_hide(AGENT, "Third Name"))
        assert await agents_db.get_buyer_display_name(AGENT) == "Hand Set Name"
        assert await agents_db.get_buyer_display_name(OTHER_AGENT) is None
    finally:
        await database.execute("DELETE FROM agents WHERE agent_id = :a", {"a": AGENT})


async def test_the_product_image_reads_run_on_postgres():
    """services/payment_orchestration/purchase_product_image's raw SQL (the owner-scoped rail rows, the catalog
    product and its skus) on the production dialect, on the catalog tables the model builds."""
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    import db.a2a_rail_ledger as a2a_ledger
    import services.payment_orchestration.purchase_product_image as product_image
    from db.catalog import catalog_products, catalog_skus
    from db.database import database
    from services.commerce_attribution_service import new_click_id
    from services.outbound_links_service import build_shopify_cart_permalink

    for table in (catalog_products, catalog_skus):
        await database.execute(str(CreateTable(table, if_not_exists=True).compile(dialect=postgresql.dialect())))
    pk = "pk_image_pg_gate"
    rose, plum = "https://cdn.example.com/rose.jpg", "https://cdn.example.com/plum.jpg"

    async def _clean():
        await database.execute("DELETE FROM catalog_skus WHERE product_key = :pk", {"pk": pk})
        await database.execute("DELETE FROM catalog_products WHERE product_key = :pk", {"pk": pk})

    await _clean()
    try:
        await database.execute(
            "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, image_url) "
            "VALUES (:pk, 'm_1', 'shopify', '1001', 'Placeholder Serum', 'https://cdn.example.com/hero.jpg')",
            {"pk": pk})
        for sku_key, variant_id, image in ((f"{pk}::v:1", "40000000001", rose), (f"{pk}::v:2", "40000000002", plum)):
            await database.execute(
                "INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform, source_product_id, "
                "source_variant_id, title, image_url) VALUES (:sk, :pk, 'm_1', 'shopify', '1001', :vid, :title, :img)",
                {"sk": sku_key, "pk": pk, "vid": variant_id, "title": sku_key, "img": image})
        cart_url = build_shopify_cart_permalink(shop_domain="merchant.example", variant_id="40000000002",
                                                click_id=new_click_id(), quantity=1, country="US")
        row = await a2a_ledger.create_purchase(
            agent_id=AGENT, agent_user_ref_hash=OWNER, merchant_domain="merchant.example", checkout_url=cart_url,
            currency="USD", our_price_minor=4650, product_key=pk)
        assert await product_image.product_image_for_owner("a2a", row["id"], AGENT, OWNER) == plum
        assert await product_image.product_image_for_owner("a2a", row["id"], OTHER_AGENT, OWNER) is None
        # The Reap row read runs too (no such row: None).
        assert await product_image.product_image_for_owner("reap", "rp_" + "0" * 24, AGENT, OWNER) is None
    finally:
        await _clean()


def test_this_module_does_not_import_main():
    assert "main" not in sys.modules
