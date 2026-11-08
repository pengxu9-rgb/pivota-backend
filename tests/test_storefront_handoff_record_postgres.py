"""The UCP storefront hand-off record (FIX_PLAN O-02) on real Postgres: the row write, the bound
read, ownership and the later state, through services/storefront_handoff_record.py and the real
surface_click_events writer. The routes over these are covered in
tests/test_storefront_handoff_record.py.

    DATABASE_URL=postgresql://postgres@localhost:5432/pivota_handoff_test_dialect_check \\
        python -m pytest tests/test_storefront_handoff_record_postgres.py
"""

import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
pytestmark = pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL")

_SAFE_DB_MARKERS = ("_test", "dialect_check")
_TABLES = ("commerce_attribution_edges", "surface_click_events")

PARTNER_A = "agent_handoff_partner_a"
PARTNER_B = "agent_handoff_partner_b"
RECORD_A = "esh_" + "A" * 21 + "a"
ESC_A = "esc_eyJ2IjoxLCJpIjpbWyJwX2EiLDFdXSwiciI6ImVzaF9hIn0"
REFERRAL = "clk_" + "b2" * 12


def _binding(esc_id):
    return hashlib.sha256(esc_id.encode("utf-8")).hexdigest()


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


def _record(**over):
    from services.storefront_handoff_record import HandoffRecord

    fields = dict(
        record_id=RECORD_A, binding=_binding(ESC_A), partner_agent_id=PARTNER_A, via_agent_id="agent_gateway",
        seller_host="shop-a.example.com", currency="USD", quoted_total_minor=2500, quote_source="catalogue_fallback",
        buyer_market="US", referral_click_id=REFERRAL, price_as_of="2026-11-01T10:00:00Z", merchant_id="merch_obs_shop_a",
    )
    fields.update(over)
    return HandoffRecord(**fields)


async def test_the_row_is_written_once_and_read_back_bound_and_owned(db):
    from services import storefront_handoff_record as handoffs

    assert await handoffs.record_handoff(_record()) == "recorded"
    assert await handoffs.record_handoff(_record()) == "recorded"  # a replay
    assert await handoffs.record_handoff(_record(partner_agent_id=PARTNER_B)) == "conflict"
    row = dict(await db.fetch_one("SELECT * FROM surface_click_events WHERE click_id = :c", {"c": RECORD_A}))
    context = row["context"] if isinstance(row["context"], dict) else json.loads(row["context"])
    assert row["agent_id"] == PARTNER_A and row["surface"] == "ucp_storefront_handoff"
    assert row["click_count"] == 0 and row["issued_at"] is not None
    assert context["binding"] == _binding(ESC_A) and context["quote_source"] == "catalogue_fallback"

    ok = await handoffs.read_handoff(RECORD_A, _binding(ESC_A), PARTNER_A)
    assert ok["outcome"] == "accepted"
    assert ok["handoff"]["status"] == "handed_off"
    assert ok["handoff"]["quoted_total"] == {"amount": 2500, "currency": "USD"}
    assert await handoffs.read_handoff(RECORD_A, _binding(ESC_A), PARTNER_B) == {"outcome": "not_owner"}
    assert await handoffs.read_handoff(RECORD_A, _binding(ESC_A + "x"), PARTNER_A) == {"outcome": "not_found"}
    assert await handoffs.read_handoff("esh_" + "Z" * 22, _binding(ESC_A), PARTNER_A) == {"outcome": "not_found"}


async def test_a_click_row_of_another_surface_is_never_read_as_a_handoff(db):
    from db.commerce_attribution import surface_click_events
    from services import storefront_handoff_record as handoffs

    now = datetime.now(timezone.utc)
    await db.execute(surface_click_events.insert().values(
        click_id=RECORD_A, surface="find_products_multi", agent_id=PARTNER_A, click_count=0, impression_count=0,
        context={"binding": _binding(ESC_A)}, created_at=now, updated_at=now,
    ))
    assert await handoffs.read_handoff(RECORD_A, _binding(ESC_A), PARTNER_A) == {"outcome": "not_found"}
    assert await handoffs.record_handoff(_record()) == "conflict"


async def test_later_state_on_postgres(db):
    from db.commerce_attribution import commerce_attribution_edges, surface_click_events
    from services import storefront_handoff_record as handoffs

    await handoffs.record_handoff(_record())
    later = datetime.now(timezone.utc) + timedelta(minutes=5)
    await db.execute(surface_click_events.insert().values(
        click_id=REFERRAL, surface="find_products_multi", agent_id=PARTNER_A, click_count=1, impression_count=0,
        last_click_at=later, created_at=later - timedelta(hours=1), updated_at=later,
    ))
    assert (await handoffs.read_handoff(RECORD_A, _binding(ESC_A), PARTNER_A))["handoff"]["status"] == "buyer_followed_link"
    await db.execute(commerce_attribution_edges.insert().values(
        edge_id="edge_handoff_pg", merchant_id="merch_obs_shop_a", click_id=REFERRAL, order_id="ord_handoff_pg",
        state="converted", converted_at=later, refund_count=0, refunded_amount=0, created_at=later, updated_at=later,
    ))
    out = (await handoffs.read_handoff(RECORD_A, _binding(ESC_A), PARTNER_A))["handoff"]
    assert out["status"] == "seller_order_observed" and out["seller_order_observed_at"]
