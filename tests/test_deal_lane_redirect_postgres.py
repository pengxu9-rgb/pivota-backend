"""The deal lane's link on real Postgres: the click row's code and carried flag (migration 276's
columns, the boolean COALESCE `/r` fills with), and a mint whose deal reference `/r` re-checks and
carries. The rules themselves: tests/test_deal_lane_redirect.py.

    DATABASE_URL=postgresql://pgtest@127.0.0.1:55433/pivota_deal_dialect_check \\
        .venv/bin/python -m pytest tests/test_deal_lane_redirect_postgres.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
pytestmark = pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL")

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_MIG = Path(__file__).resolve().parent.parent / "db" / "migrations"
_DEAL_TABLES = ("observed_store_offer_verifications", "observed_store_offers", "deal_lane_stops", "deal_lane_optout")
SHOP = "conn-store.myshopify.com"
VARIANT = "46000000001"


def _assert_throwaway_database():
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to write in {dbname!r}; throwaway only")


@pytest.fixture
async def db(monkeypatch):
    from db.commerce_attribution import surface_click_events
    from db.database import database, engine, metadata
    from db.sql_migrations import split_statements

    _assert_throwaway_database()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    metadata.create_all(engine, tables=[surface_click_events], checkfirst=True)
    # The hand apply, as its runbook runs it: one transaction (a sibling may have built the table
    # from an older shape).
    async with database.transaction():
        for stmt in split_statements((_MIG / "276_surface_click_events_discount_code.sql.disabled").read_text()):
            await database.execute(stmt)
    for stmt in split_statements((_MIG / "275_deal_lane_tables.sql").read_text()):
        await database.execute(stmt)

    async def _clean():
        for table in _DEAL_TABLES:
            await database.execute(f"DELETE FROM {table}")
        await database.execute("DELETE FROM surface_click_events WHERE click_id LIKE 'dlrpg_%'")

    await _clean()
    monkeypatch.setenv("DEAL_LANE_EMIT_ENABLED", "true")
    monkeypatch.setenv("DEAL_LANE_DISCOUNT_PARAM_ENABLED", "true")
    try:
        yield database
    finally:
        await _clean()
        if not was_connected:
            await database.disconnect()


async def _seed(db):
    from db.deal_lane import observed_store_offer_key

    now = datetime.now(timezone.utc)
    await db.execute(
        """
        INSERT INTO observed_store_offers
          (deal_id, deal_key, seller_id, market, code, kind, value, currency, source, eligibility_known, observed_at)
        VALUES ('dlrpg_deal', :key, 'merch_conn', 'US', 'GLOW10', 'percent', 10, 'USD', 'merchant_published', TRUE, :now)
        """,
        {"now": now, "key": observed_store_offer_key(
            seller_id="merch_conn", market="US", code="GLOW10", platform_rule_ref=None, program_id=None)},
    )
    await db.execute(
        """
        INSERT INTO observed_store_offer_verifications
          (deal_id, variant_id, state, verified_at, expires_at, verified_via, authorized_by,
           ref_unit_price, ref_subtotal, ref_discount_amount, ref_currency, ref_automatic_discounts)
        VALUES ('dlrpg_deal', :variant, 'checkout_verified', :verified, :expires, 'shopify_storefront_connected',
                'connected', 40.00, 40.00, 4.00, 'USD', '[]'::jsonb)
        """,
        {"variant": VARIANT, "verified": now - timedelta(hours=1), "expires": now + timedelta(hours=23)},
    )


async def test_the_carried_flag_only_ever_turns_true_on_postgres(db):
    from services.commerce_attribution_service import IssuedClick, issue_clicks, record_surface_event

    await issue_clicks([IssuedClick(click_id="dlrpg_row", surface="find_products", discount_code="GLOW10")])

    async def click(carried):
        await record_surface_event(
            token_payload={"tool": "find_products", "dest": "https://x.example/cart/1:1",
                           "ctx": {"pvt_click_id": "dlrpg_row"}, "discount_carried": carried},
            request_meta={}, event_type="click",
        )
        row = await db.fetch_one(
            "SELECT discount_code, discount_carried, click_count FROM surface_click_events WHERE click_id = 'dlrpg_row'"
        )
        return row["discount_code"], row["discount_carried"], row["click_count"]

    assert await click(False) == ("GLOW10", False, 1)
    assert await click(True) == ("GLOW10", True, 2)
    assert await click(False) == ("GLOW10", True, 3)


async def test_a_minted_deal_is_carried_by_r_on_postgres(db):
    from starlette.requests import Request

    import routes.agent_shop_gateway as gw
    import routes.outbound_links as outbound_routes
    from services.deal_lane_decision import DealOffer, TOKEN_DEAL_REF_KEY
    from services.outbound_links_service import parse_and_verify_redirect_token

    await _seed(db)
    issued = []
    link = await gw._make_external_redirect_url(
        market="US", market_observed=False, tool="find_products",
        destination_url=f"https://{SHOP}/products/serum", utm_template=None, ctx={},
        allowed_domains=[SHOP], merchant_id="merch_conn", variant_id=VARIANT, cart_variant_id=VARIANT,
        shop_domain=SHOP, platform="shopify", click_id="dlrpg_clk", issued_clicks=issued,
        deal_offer=DealOffer(seller_id="merch_conn", buyer_market="US", observed_unit_price="40",
                             observed_currency="USD"),
    )
    token = parse_qs(urlparse(link).query)["token"][0]
    assert parse_and_verify_redirect_token(token)[TOKEN_DEAL_REF_KEY]["deal_id"] == "dlrpg_deal"
    from services.commerce_attribution_service import issue_clicks

    await issue_clicks(issued)
    # The route itself, in this test's event loop (a TestClient runs another loop, where this
    # connection is unusable and the re-check fails closed as read_failed).
    request = Request({"type": "http", "method": "GET", "path": "/r", "query_string": b"",
                       "headers": [(b"user-agent", b"Mozilla/5.0")], "client": ("127.0.0.1", 1)})
    resp = await outbound_routes.redirect_endpoint(request, token=token)
    assert resp.status_code == 302 and resp.headers["location"].endswith("&country=US&discount=GLOW10")
    row = await db.fetch_one(
        "SELECT discount_code, discount_carried FROM surface_click_events WHERE click_id = 'dlrpg_clk'"
    )
    assert (row["discount_code"], row["discount_carried"]) == ("GLOW10", True)
