"""A click id is recorded when its link is ISSUED, and `/r` only counts it (ADR-025 D1).

Before this, surface_click_events got a row only on a `/r` hit: a link an agent handed out that no
buyer followed left no trace, the agent that issued it was never recorded (the signed `/r` token
carries no agent), and two concurrent hits could lose an increment (read, then write).

    DATABASE_URL=postgresql://postgres@localhost:5432/pivota_issue_click_test \\
        python -m pytest tests/test_issue_click_postgres.py
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
pytestmark = pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL")

# A local throwaway (`..._test`) or the CI gate's own database (postgres-dialect-gate.yml).
_SAFE_DB_MARKERS = ("_test", "dialect_check")
# Reduced tables, DROPPED in teardown so a later create_all builds the real ones.
_TABLES = ("commerce_attribution_edges", "surface_click_events")


def _assert_throwaway_database():
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to drop tables in {dbname!r}; throwaway only")


async def _drop(database):
    for table in _TABLES:
        try:
            await database.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        except Exception:  # noqa: BLE001
            continue


@pytest.fixture(autouse=True)
async def db(monkeypatch):
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateIndex, CreateTable

    from db.commerce_attribution import commerce_attribution_edges, surface_click_events
    from db.database import database
    from services import commerce_attribution_service as cas

    _assert_throwaway_database()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()

    async def _event(*a, **k):
        return {"interaction_id": "int_stub"}

    monkeypatch.setattr(cas, "record_commerce_event_best_effort", _event)
    monkeypatch.setattr(cas, "record_traffic_taxonomy", lambda **_: None)
    await _drop(database)
    dialect = postgresql.dialect()
    for table in (surface_click_events, commerce_attribution_edges):
        await database.execute(str(CreateTable(table).compile(dialect=dialect)))
        for index in table.indexes:
            await database.execute(str(CreateIndex(index).compile(dialect=dialect)))
    try:
        yield database
    finally:
        await _drop(database)
        if not was_connected and database.is_connected:
            await database.disconnect()


async def _row(db, click_id):
    row = await db.fetch_one("SELECT * FROM surface_click_events WHERE click_id = :c", {"c": click_id})
    if row is None:
        return None
    row = dict(row)
    if isinstance(row.get("context"), str):
        row["context"] = json.loads(row["context"])
    return row


def _token(click_id, **ctx):
    """What `/r` hands record_surface_event: the signed token's payload. It never carries an agent."""
    return {
        "tool": "offers.resolve",
        "dest": "https://brand.example/products/widget",
        "dest_domain": "brand.example",
        "ctx": {"pvt_click_id": click_id, "merchant_id": "m_anchor", **ctx},
    }


async def _hit(click_id, event_type="click", **ctx):
    from services.commerce_attribution_service import record_surface_event

    return await record_surface_event(
        token_payload=_token(click_id, **ctx),
        request_meta={"user_agent": "ua/1", "ip": "203.0.113.9"},
        event_type=event_type,
    )


def _issued(click_id="clk_issued_1", **over):
    from services.commerce_attribution_service import IssuedClick

    fields = dict(
        click_id=click_id, surface="offers_resolve", agent_id="agent_minds", merchant_id="m_anchor",
        canonical_product_id="prod_1", canonical_variant_id="var_1",
        destination_url="https://brand.example/cart/1:1?attributes[pivota_click_id]=" + click_id,
        dest_domain="brand.example",
        context={"seller_ref": "m_seller", "seed_kind": "cross", "join_mode": "cart_permalink"},
    )
    fields.update(over)
    return IssuedClick(**fields)


# --- issue_clicks -------------------------------------------------------------------------------


async def test_issuing_records_the_link_with_its_agent_and_no_click(db):
    from services.commerce_attribution_service import issue_clicks

    assert await issue_clicks([_issued()]) == 1

    row = await _row(db, "clk_issued_1")
    assert row["agent_id"] == "agent_minds"
    assert row["issued_at"] is not None
    assert (row["click_count"], row["impression_count"], row["first_click_at"]) == (0, 0, None)
    assert row["context"]["seller_ref"] == "m_seller"


async def test_a_wrong_sized_or_sentinel_identity_is_stored_as_none_never_truncated(db):
    from services.commerce_attribution_service import issue_clicks

    await issue_clicks([
        _issued("clk_long_agent", agent_id="a" * 65),
        _issued("clk_unknown", agent_id="unknown"),
        _issued("clk_long_merchant", merchant_id="m" * 51),
    ])

    assert (await _row(db, "clk_long_agent"))["agent_id"] is None
    assert (await _row(db, "clk_unknown"))["agent_id"] is None
    assert (await _row(db, "clk_long_merchant"))["merchant_id"] is None


async def test_re_issuing_never_resets_a_link_a_buyer_already_followed(db):
    from services.commerce_attribution_service import issue_clicks

    await issue_clicks([_issued()])
    await _hit("clk_issued_1")
    await issue_clicks([_issued(), _issued()])  # a retried request, with a duplicate in the batch

    row = await _row(db, "clk_issued_1")
    assert row["click_count"] == 1
    assert row["agent_id"] == "agent_minds"


# --- /r on an issued link -----------------------------------------------------------------------


async def test_a_click_counts_on_the_issued_row_and_keeps_its_agent(db):
    from services.commerce_attribution_service import issue_clicks

    await issue_clicks([_issued(merchant_id=None, canonical_product_id=None)])
    await _hit("clk_issued_1", productId="prod_from_token", seller_ref="m_other", brief_id="br_1")

    row = await _row(db, "clk_issued_1")
    assert row["click_count"] == 1 and row["first_click_at"] is not None
    # The token names no agent. The issued one stays.
    assert row["agent_id"] == "agent_minds"
    # Fields the issue left empty are filled from the hit.
    assert row["merchant_id"] == "m_anchor"
    # Context: what was issued wins, the hit fills what is missing.
    assert row["context"]["seller_ref"] == "m_seller"
    assert row["context"]["brief_id"] == "br_1"
    assert row["issued_at"] is not None
    assert (row["user_agent"], row["ip"]) == ("ua/1", "203.0.113.9")


async def test_a_click_never_overwrites_an_issued_field_with_the_tokens_value(db):
    from services.commerce_attribution_service import issue_clicks

    await issue_clicks([_issued(merchant_id="m_issued")])
    await _hit("clk_issued_1")  # the token says m_anchor

    assert (await _row(db, "clk_issued_1"))["merchant_id"] == "m_issued"


async def test_an_impression_counts_as_an_impression_not_a_click(db):
    from services.commerce_attribution_service import issue_clicks

    await issue_clicks([_issued()])
    await _hit("clk_issued_1", event_type="impression")

    row = await _row(db, "clk_issued_1")
    assert (row["impression_count"], row["click_count"]) == (1, 0)
    assert row["first_impression_at"] is not None and row["first_click_at"] is None


# --- /r on a link with no issue record ----------------------------------------------------------


async def test_a_link_issued_before_this_change_is_recorded_as_a_legacy_click(db):
    await _hit("clk_legacy_1", seller_ref="m_seller")

    row = await _row(db, "clk_legacy_1")
    assert row["issued_at"] is None
    assert row["click_count"] == 1
    assert row["merchant_id"] == "m_anchor"
    assert row["context"]["seller_ref"] == "m_seller"


# --- concurrency --------------------------------------------------------------------------------


async def test_concurrent_hits_on_separate_connections_are_all_counted(db):
    """THE LOST UPDATE the read-then-write had. Each hit runs the real count statement on its own
    backend, at the same moment; the row must end at exactly the number of hits."""
    import asyncpg

    from services.commerce_attribution_service import _COUNT_CLICK_SQL, issue_clicks

    await issue_clicks([_issued()])
    sql = _COUNT_CLICK_SQL.replace(":now", "$2").replace(":click_id", "$1")
    url = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
    hits = 8
    conns = [await asyncpg.connect(url) for _ in range(hits)]
    try:
        now = datetime.now(timezone.utc)
        await asyncio.gather(*(c.fetchrow(sql, "clk_issued_1", now) for c in conns))
    finally:
        for c in conns:
            await c.close()

    assert (await _row(db, "clk_issued_1"))["click_count"] == hits


async def test_two_first_hits_on_an_unissued_link_count_twice_and_write_one_row(db):
    await asyncio.gather(_hit("clk_race"), _hit("clk_race"))

    rows = await db.fetch_all("SELECT click_count FROM surface_click_events WHERE click_id = 'clk_race'")
    assert [r["click_count"] for r in rows] == [2]


# --- the lanes ----------------------------------------------------------------------------------


async def test_the_reap_cart_link_records_an_issued_link_not_a_click(db):
    from routes.agent_commerce_reap import _record_cart_link_click

    await _record_cart_link_click(
        click_id="clk_cart_1", cart_url="https://shop.example/cart/1:1?attributes[pivota_click_id]=clk_cart_1",
        shop_domain="shop.example", seller_ref="shop.example", product_key="pk_1", variant_key="1",
        agent_id="agent_minds", seed_kind="self",
    )

    row = await _row(db, "clk_cart_1")
    assert row["issued_at"] is not None
    assert (row["click_count"], row["first_click_at"]) == (0, None)
    assert (row["agent_id"], row["surface"], row["merchant_id"]) == ("agent_minds", "reap_cart_link", "shop.example")
    assert row["context"]["item_source"] == "cart_link"


async def test_an_order_on_a_link_nobody_clicked_still_closes_against_its_issued_click(db):
    """The point of D1. An agent hands the buyer our cart URL directly, so `/r` is never hit. The
    order still carries the click id, and closure reads the seller and product from the issued
    row. Before, there was no row and the edge closed as click_matched=False."""
    from services.commerce_attribution_service import close_external_order_conversion, issue_clicks

    await issue_clicks([_issued(merchant_id="m_seller", context={"seller_ref": "m_seller", "seed_kind": "self"})])

    closed = await close_external_order_conversion(
        merchant_id="m_seller", click_id="clk_issued_1", external_order_id="5551",
        gross_amount_cents=12_000, currency="USD", converting_shop_domain="brand.example",
    )

    assert closed["click_matched"] is True
    assert closed["canonical_product_id"] == "prod_1"
    assert closed["seller_ref"] == "m_seller"
    edge = await db.fetch_one("SELECT agent_id FROM commerce_attribution_edges WHERE external_order_id = '5551'")
    assert dict(edge)["agent_id"] == "agent_minds"


# --- an agent-less issued link stays agent-less (review of #2294) ---------------------------------


async def test_a_click_on_an_agentless_issued_link_leaves_it_agentless(db):
    """Traffic taxonomy says agent_id='unknown' whenever the token names no agent. The first click
    used to fill that into the issued NULL, and the edge then credited 'unknown'."""
    from services.commerce_attribution_service import issue_clicks

    await issue_clicks([_issued(agent_id=None)])
    await _hit("clk_issued_1")

    row = await _row(db, "clk_issued_1")
    assert row["agent_id"] is None
    assert row["context"].get("agent_id") in (None,)
    assert (row["context"].get("traffic") or {}).get("agent_id") is None


async def test_a_token_cannot_name_the_agent_of_an_issued_link(db):
    """An issued NULL is a decision (shared search link, internal key, degraded auth). A value in
    the token, possibly body-supplied at mint time, must never fill it."""
    from services.commerce_attribution_service import close_external_order_conversion, issue_clicks

    await issue_clicks([_issued(agent_id=None, merchant_id="m_seller",
                                context={"seller_ref": "m_seller", "seed_kind": "self"})])
    await _hit("clk_issued_1", agent_id="agent_x")

    row = await _row(db, "clk_issued_1")
    assert row["agent_id"] is None
    assert row["context"].get("agent_id") is None

    await close_external_order_conversion(
        merchant_id="m_seller", click_id="clk_issued_1", external_order_id="5552",
        gross_amount_cents=12_000, currency="USD", converting_shop_domain="brand.example",
    )
    edge = await db.fetch_one("SELECT agent_id FROM commerce_attribution_edges WHERE external_order_id = '5552'")
    assert dict(edge)["agent_id"] is None


async def test_a_legacy_click_with_no_agent_stores_none_not_the_sentinel(db):
    await _hit("clk_legacy_2")

    row = await _row(db, "clk_legacy_2")
    assert row["agent_id"] is None
    assert row["context"].get("agent_id") is None


async def test_an_edge_never_credits_the_sentinel_a_legacy_row_already_holds(db):
    """Rows /r wrote before this change can hold agent_id='unknown'."""
    from services.commerce_attribution_service import close_external_order_conversion

    await db.execute(
        "INSERT INTO surface_click_events (click_id, merchant_id, surface, agent_id, impression_count, "
        "click_count, created_at, updated_at) VALUES ('clk_old', 'm_seller', 'offers_resolve', 'unknown', "
        "0, 1, now(), now())"
    )
    await close_external_order_conversion(
        merchant_id="m_seller", click_id="clk_old", external_order_id="5553",
        gross_amount_cents=5_000, currency="USD", converting_shop_domain="brand.example",
    )
    edge = await db.fetch_one("SELECT agent_id FROM commerce_attribution_edges WHERE external_order_id = '5553'")
    assert dict(edge)["agent_id"] is None


async def test_concurrent_fills_keep_the_first_value_not_the_last(db):
    """Fill-only is decided in SQL (COALESCE), not against a snapshot: two hits carrying different
    values for a NULL column leave the value of whichever wrote first."""
    from services.commerce_attribution_service import issue_clicks

    await issue_clicks([_issued(canonical_product_id=None)])
    await _hit("clk_issued_1", productId="prod_first")
    await _hit("clk_issued_1", productId="prod_second")

    assert (await _row(db, "clk_issued_1"))["canonical_product_id"] == "prod_first"


# --- readers that meant "a buyer clicked" must not count a link that was only issued ------------


async def test_the_inferred_fallback_never_recovers_a_link_nobody_followed(db):
    """#1481's fallback recovers "the most recent agent→merchant CLICK" for an order with no
    signal. An issued row with no click or impression is an offer, not a click: inferring from it
    would attribute an order to every agent that merely showed the merchant."""
    from services import commerce_attribution_service as cas

    await cas.issue_click(_issued("clk_inf_issued"))
    now = datetime.now(timezone.utc)
    assert await cas._infer_attribution_from_recent_click({"agent_id": "agent_minds"}, "m_anchor", now) is None

    await _hit("clk_inf_issued")
    got = await cas._infer_attribution_from_recent_click({"agent_id": "agent_minds"}, "m_anchor", now)
    assert got is not None and got.get("canonical_product_id") == "prod_1"


async def test_the_traffic_dashboard_counts_clicked_links_not_issued_ones(db, monkeypatch):
    from services import commerce_attribution_service as cas
    from services import traffic_analytics_service as tas

    await cas.issue_clicks([_issued(f"clk_ta_{i}") for i in range(5)])
    await _hit("clk_ta_0")
    await _hit("clk_ta_1", event_type="impression")

    async def _none(**_):
        return []

    monkeypatch.setattr(tas, "_fetch_request_rows", _none)
    monkeypatch.setattr(tas, "_fetch_edge_rows", _none)
    out = await tas.build_employee_traffic_overview(window="1d")
    assert out["clicked_exposure"] == 2
