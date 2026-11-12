"""The affiliate sub-ID on real Postgres: a network tracking link minted by the real producer, followed
through the real `/r`, lands our click id in the sub-ID AND on both click rows' JSONB context, under
the same id as the surface row's click_id column. The rules themselves: tests/test_affiliate_subid_redirect.py.

    DATABASE_URL=postgresql://<user>@localhost:5432/<name>_test_dialect_check \\
        .venv/bin/python -m pytest tests/test_affiliate_subid_redirect_postgres.py
"""

from __future__ import annotations

import json
import os
import sys
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
pytestmark = pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL")

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
CLICK_ID = "clk_af5ab9000000000000000001"  # the minted shape: clk_ + 24 lowercase hex
LINK_HOST = "click.linksynergy.com"
FEED_LINK = f"https://{LINK_HOST}/deeplink?id=AbC123&mid=00000&murl=https%3A%2F%2Fshop-a.example.com%2Fp&u1=stale"


def _assert_throwaway_database():
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to write in {dbname!r}; throwaway only")


def _obj(value):
    return value if isinstance(value, dict) else json.loads(value)


@pytest.fixture
async def db(monkeypatch):
    from db.commerce_attribution import surface_click_events
    from db.database import database, engine, metadata
    from db.outbound_links import outbound_click_events

    _assert_throwaway_database()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    metadata.create_all(engine, tables=[surface_click_events, outbound_click_events], checkfirst=True)

    async def _clean():
        await database.execute("DELETE FROM surface_click_events WHERE click_id LIKE 'clk_af5ab9%'")
        await database.execute("DELETE FROM outbound_click_events WHERE dest_domain = :h", {"h": LINK_HOST})

    await _clean()
    monkeypatch.setenv("OUTBOUND_LINKS_SIGNING_SECRET", "test-signing-secret-not-real")
    monkeypatch.setenv("AFFILIATE_SUBID_CLICK_ID_ENABLED", "true")
    monkeypatch.delenv("AFFILIATE_SUBID_PARAMS_JSON", raising=False)
    monkeypatch.delenv("OUTBOUND_WARM_HANDOFF_ENABLED", raising=False)
    try:
        yield database
    finally:
        await _clean()
        if not was_connected:
            await database.disconnect()


async def test_a_minted_network_link_carries_the_recorded_click_id_in_u1_on_postgres(db):
    from starlette.requests import Request

    import routes.agent_shop_gateway as gw
    import routes.outbound_links as outbound_routes
    from services.affiliate_subid import click_id_from_subid
    from services.commerce_attribution_service import issue_clicks
    from services.outbound_links_service import parse_and_verify_redirect_token

    issued = []
    link = await gw._make_external_redirect_url(
        market="US", market_observed=False, tool="find_products",
        destination_url=FEED_LINK, utm_template=None, ctx={},
        allowed_domains=[LINK_HOST], merchant_id="merch_a", variant_id=None, cart_variant_id=None,
        shop_domain=None, platform=None, click_id=CLICK_ID, issued_clicks=issued,
    )
    token = parse_qs(urlparse(link).query)["token"][0]
    signed_dest = parse_and_verify_redirect_token(token)["dest"]
    await issue_clicks(issued)

    # The route itself, in this test's event loop (a TestClient runs another loop).
    request = Request({"type": "http", "method": "GET", "path": "/r", "query_string": b"",
                       "headers": [(b"user-agent", b"Mozilla/5.0")], "client": ("127.0.0.1", 1)})
    resp = await outbound_routes.redirect_endpoint(request, token=token)
    location = resp.headers["location"]

    assert resp.status_code == 302
    hex_form = CLICK_ID[len("clk_"):]  # u1 is alphanumeric: the bare hex
    assert parse_qs(urlparse(location).query)["u1"] == [hex_form]  # the stale u1 replaced, one copy
    assert location == signed_dest.replace("u1=stale", f"u1={hex_form}")

    surface = await db.fetch_one(
        "SELECT click_id, click_count, context FROM surface_click_events WHERE click_id = :c", {"c": CLICK_ID}
    )
    assert surface["click_count"] == 1
    sctx = _obj(surface["context"])
    assert (sctx["affiliate_subid_param"], sctx["affiliate_subid_value"]) == ("u1", hex_form)
    # The import's re-prefix lands on the surface row's click_id column.
    assert click_id_from_subid(sctx["affiliate_subid_value"]) == surface["click_id"]

    click = await db.fetch_one(
        "SELECT destination_url, context FROM outbound_click_events WHERE dest_domain = :h", {"h": LINK_HOST}
    )
    assert click["destination_url"] == signed_dest  # the row keeps the SIGNED destination
    cctx = _obj(click["context"])
    assert (cctx["affiliate_subid_param"], cctx["affiliate_subid_value"]) == ("u1", hex_form)
