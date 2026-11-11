"""The store-cart referral on real Postgres: the row write (JSONB context, NULL agent, the
test-traffic stamp), the members it returns, and the hand-off record's later-state join on it,
through services/storefront_cart_referral.py and the real surface_click_events writer. The route
over these is covered in tests/test_storefront_cart_referral.py.

The tables come from the models (tests/model_schema.ensure_model_tables). Nothing is dropped: the
gate database is shared by every file in the gate, so only this file's own rows are removed.

    DATABASE_URL=postgresql://postgres@localhost:5432/pivota_referral_test_dialect_check \\
        python -m pytest tests/test_storefront_cart_referral_postgres.py
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

PARTNER_A = "agent_referral_pg_partner_a"
PARTNER_B = "agent_referral_pg_partner_b"
GATEWAY = "agent_referral_pg_gateway"
HOST = "referral-pg-test-shop-a.example.com"
MERCHANT = "merch_obs_referral_pg_a"
TEST_MERCHANT = "merch_obs_referral_pg_rig"
RECORD_ID = "esh_" + "P" * 21 + "g"
EDGE_ID = "edge_referral_pg_test"


def _assert_throwaway_database():
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to write test rows in {dbname!r}; throwaway only")


async def _clean(database):
    await database.execute("DELETE FROM commerce_attribution_edges WHERE edge_id = :e", {"e": EDGE_ID})
    await database.execute("DELETE FROM surface_click_events WHERE dest_domain = :h", {"h": HOST})
    await database.execute("DELETE FROM surface_click_events WHERE click_id = :c", {"c": RECORD_ID})


@pytest.fixture(autouse=True)
async def db(monkeypatch):
    from db.commerce_attribution import commerce_attribution_edges, surface_click_events
    from db.database import database
    from services import commerce_attribution_service as cas
    from services import test_traffic
    from tests.model_schema import ensure_model_tables

    _assert_throwaway_database()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    monkeypatch.setattr(cas, "record_traffic_taxonomy", lambda **_: None)
    monkeypatch.delenv(test_traffic.STAMP_FLAG, raising=False)
    await ensure_model_tables([surface_click_events, commerce_attribution_edges])
    await _clean(database)
    try:
        yield database
    finally:
        await _clean(database)
        if not was_connected and database.is_connected:
            await database.disconnect()


async def _rows(db):
    found = await db.fetch_all(
        "SELECT * FROM surface_click_events WHERE dest_domain = :h ORDER BY created_at, click_id", {"h": HOST}
    )
    return [dict(r) for r in found]


def _ctx(row):
    return row["context"] if isinstance(row["context"], dict) else json.loads(row["context"])


async def test_the_row_and_the_members_on_postgres(db):
    from services import storefront_cart_referral as referrals

    answer = await referrals.mint_referral(
        seller_host=HOST, market="SG", merchant_id=MERCHANT, partner_agent_id=PARTNER_A, via_agent_id=GATEWAY
    )
    click_id = answer["click_id"]
    assert list(answer["attribution"].items()) == [
        ("utm_source", "pivota"), ("utm_medium", "create_checkout"), ("utm_campaign", "SG"),
        ("pvt_click_id", click_id), ("utm_content", click_id),
    ]
    (row,) = await _rows(db)
    assert row["click_id"] == click_id and row["agent_id"] == PARTNER_A
    assert row["surface"] == row["commerce_surface"] == "ucp_storefront_handoff"
    assert row["merchant_id"] == MERCHANT and row["destination_url"] is None
    assert row["click_count"] == 0 and row["issued_at"] is not None
    assert _ctx(row) == {
        "kind": "ucp_storefront_cart_referral", "v": 1, "tool": "create_checkout",
        "join_mode": "referral_only", "market": "SG", "via_agent_id": GATEWAY,
    }
    # The JSON path a report filters on reads the kind in SQL, on the stored JSONB.
    kinds = await db.fetch_all(
        "SELECT context->>'kind' AS kind FROM surface_click_events WHERE dest_domain = :h", {"h": HOST}
    )
    assert [dict(k)["kind"] for k in kinds] == ["ucp_storefront_cart_referral"]


async def test_no_partner_is_a_null_agent_and_each_mint_is_its_own_row(db):
    from services import storefront_cart_referral as referrals

    first = await referrals.mint_referral(seller_host=HOST)
    second = await referrals.mint_referral(seller_host=HOST, partner_agent_id=PARTNER_B)
    rows = {r["click_id"]: r for r in await _rows(db)}
    assert set(rows) == {first["click_id"], second["click_id"]} and len(rows) == 2
    assert rows[first["click_id"]]["agent_id"] is None and rows[first["click_id"]]["merchant_id"] is None
    assert "market" not in _ctx(rows[first["click_id"]]) and first["attribution"]["utm_campaign"] == "US"
    assert rows[second["click_id"]]["agent_id"] == PARTNER_B


async def test_the_test_traffic_stamp_on_postgres(db, monkeypatch):
    from services import storefront_cart_referral as referrals
    from services import test_traffic

    monkeypatch.setenv("PIVOTA_TEST_MERCHANT_IDS", TEST_MERCHANT)
    monkeypatch.setenv(test_traffic.STAMP_FLAG, "true")
    rig = await referrals.mint_referral(seller_host=HOST, merchant_id=TEST_MERCHANT, partner_agent_id=PARTNER_A)
    real = await referrals.mint_referral(seller_host=HOST, merchant_id=MERCHANT, partner_agent_id=PARTNER_A)
    rows = {r["click_id"]: _ctx(r) for r in await _rows(db)}
    assert rows[rig["click_id"]][test_traffic.STAMP_KEY] is True
    assert rows[rig["click_id"]][test_traffic.REASONS_KEY] == [test_traffic.REASON_TEST_MERCHANT]
    assert test_traffic.STAMP_KEY not in rows[real["click_id"]]
    # The shared money-read predicate, in SQL on the stored JSONB, keeps the real row and drops the rig's.
    kept = await db.fetch_all(
        "SELECT click_id FROM surface_click_events WHERE dest_domain = :h AND "
        + test_traffic.not_test_traffic_sql("context"),
        {"h": HOST},
    )
    assert [dict(k)["click_id"] for k in kept] == [real["click_id"]]


async def test_a_hand_off_record_joins_the_minted_click_on_postgres(db):
    from db.commerce_attribution import commerce_attribution_edges
    from services import storefront_cart_referral as referrals
    from services import storefront_handoff_record as handoffs

    click_id = (await referrals.mint_referral(seller_host=HOST, merchant_id=MERCHANT, partner_agent_id=PARTNER_A))["click_id"]
    binding = hashlib.sha256(b"esc_referral_pg_test").hexdigest()
    record = handoffs.HandoffRecord(
        record_id=RECORD_ID, binding=binding, partner_agent_id=PARTNER_A, via_agent_id=GATEWAY,
        seller_host=HOST, currency="USD", quoted_total_minor=2300, quote_source="seller_cart",
        buyer_market="US", referral_click_id=click_id,
    )
    assert await handoffs.record_handoff(record) == "recorded"
    assert (await handoffs.read_handoff(RECORD_ID, binding, PARTNER_A))["handoff"]["status"] == "handed_off"
    later = datetime.now(timezone.utc) + timedelta(minutes=5)
    await db.execute(commerce_attribution_edges.insert().values(
        edge_id=EDGE_ID, merchant_id=MERCHANT, click_id=click_id, order_id="ord_" + EDGE_ID,
        state="converted", agent_id=PARTNER_A, converted_at=later, refund_count=0, refunded_amount=0,
        created_at=later, updated_at=later,
    ))
    assert (await handoffs.read_handoff(RECORD_ID, binding, PARTNER_A))["handoff"]["status"] == "seller_order_observed"
    # Another partner's hand-off naming the same click reads nothing of it.
    assert (await handoffs.read_handoff(RECORD_ID, binding, PARTNER_B)) == {"outcome": "not_owner"}
