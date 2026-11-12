"""The store-cart referral on real Postgres: the row write (JSONB context, NULL agent, the
test-traffic stamp), the members it returns, the hand-off record's later-state join on it (and on
an order the real conversion writer stamps as test traffic), and the staff click report beside
these rows, through services/storefront_cart_referral.py and the real surface_click_events writer.
The route over these is covered in tests/test_storefront_cart_referral.py.

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
REPORT_MERCHANT = "merch_obs_referral_pg_report"
RECORD_ID = "esh_" + "P" * 21 + "g"
RECORD_ID_2 = "esh_" + "P" * 21 + "h"
EDGE_ID = "edge_referral_pg_test"
_MERCHANTS = (MERCHANT, TEST_MERCHANT, REPORT_MERCHANT)


def _assert_throwaway_database():
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to write test rows in {dbname!r}; throwaway only")


async def _clean(database):
    await database.execute("DELETE FROM commerce_attribution_edges WHERE edge_id = :e", {"e": EDGE_ID})
    # Only this file's own rows: every one names one of its merchants or its host.
    for merchant in _MERCHANTS:
        await database.execute("DELETE FROM commerce_attribution_edges WHERE merchant_id = :m", {"m": merchant})
        await database.execute("DELETE FROM surface_click_events WHERE merchant_id = :m", {"m": merchant})
    await database.execute("DELETE FROM surface_click_events WHERE dest_domain = :h", {"h": HOST})
    for record_id in (RECORD_ID, RECORD_ID_2):
        await database.execute("DELETE FROM surface_click_events WHERE click_id = :c", {"c": record_id})


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

    first = await referrals.mint_referral(seller_host=HOST, market="US")
    second = await referrals.mint_referral(seller_host=HOST, market="US", partner_agent_id=PARTNER_B)
    rows = {r["click_id"]: r for r in await _rows(db)}
    assert set(rows) == {first["click_id"], second["click_id"]} and len(rows) == 2
    assert rows[first["click_id"]]["agent_id"] is None and rows[first["click_id"]]["merchant_id"] is None
    assert rows[second["click_id"]]["agent_id"] == PARTNER_B


async def test_no_market_is_no_row_and_no_default_on_postgres(db):
    """The market is the caller's: without one nothing is minted and nothing is written."""
    from services import storefront_cart_referral as referrals

    for market in (None, "", "us", "USA"):
        with pytest.raises(referrals.ReferralMarketRequired):
            await referrals.mint_referral(seller_host=HOST, market=market, merchant_id=MERCHANT)
    assert await _rows(db) == []
    named = await referrals.mint_referral(seller_host=HOST, market="GB", merchant_id=MERCHANT)
    (row,) = await _rows(db)
    assert named["attribution"]["utm_campaign"] == "GB" and _ctx(row)["market"] == "GB"


async def test_an_unlisted_market_is_no_row_on_postgres(db, monkeypatch):
    """Known = listed in services/market_registry.py: `ZZ` writes nothing, `gb` is malformed (case is
    not folded), `GB` and `US` are recorded as named, and the check follows the registry's table."""
    from services import market_registry
    from services import storefront_cart_referral as referrals

    with pytest.raises(referrals.ReferralMarketUnknown):
        await referrals.mint_referral(seller_host=HOST, market="ZZ", merchant_id=MERCHANT)
    with pytest.raises(referrals.ReferralMarketRequired):
        await referrals.mint_referral(seller_host=HOST, market="gb", merchant_id=MERCHANT)
    assert await _rows(db) == []
    for market in ("GB", "US"):
        answer = await referrals.mint_referral(seller_host=HOST, market=market, merchant_id=MERCHANT)
        assert answer["attribution"]["utm_campaign"] == market
    assert sorted(_ctx(r)["market"] for r in await _rows(db)) == ["GB", "US"]
    in_sql = await db.fetch_all(
        "SELECT context->>'market' AS market FROM surface_click_events WHERE dest_domain = :h ORDER BY 1", {"h": HOST}
    )
    assert [dict(r)["market"] for r in in_sql] == ["GB", "US"]
    changed = {code: entry for code, entry in market_registry.MARKETS.items() if code != "GB"}
    changed["ZZ"] = dict(market_registry.MARKETS["US"])
    monkeypatch.setattr(market_registry, "MARKETS", changed)
    await referrals.mint_referral(seller_host=HOST, market="ZZ", merchant_id=MERCHANT)
    with pytest.raises(referrals.ReferralMarketUnknown):
        await referrals.mint_referral(seller_host=HOST, market="GB", merchant_id=MERCHANT)
    assert sorted(_ctx(r)["market"] for r in await _rows(db)) == ["GB", "US", "ZZ"]
    monkeypatch.setattr(market_registry, "MARKETS", {"ZZ": changed["ZZ"]})
    with pytest.raises(referrals.ReferralMarketUnknown):
        await referrals.mint_referral(seller_host=HOST, market="US", merchant_id=MERCHANT)
    assert len(await _rows(db)) == 3


async def test_the_test_traffic_stamp_on_postgres(db, monkeypatch):
    from services import storefront_cart_referral as referrals
    from services import test_traffic

    monkeypatch.setenv("PIVOTA_TEST_MERCHANT_IDS", TEST_MERCHANT)
    monkeypatch.setenv(test_traffic.STAMP_FLAG, "true")
    rig = await referrals.mint_referral(seller_host=HOST, market="US", merchant_id=TEST_MERCHANT, partner_agent_id=PARTNER_A)
    real = await referrals.mint_referral(seller_host=HOST, market="US", merchant_id=MERCHANT, partner_agent_id=PARTNER_A)
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

    click_id = (await referrals.mint_referral(
        seller_host=HOST, market="US", merchant_id=MERCHANT, partner_agent_id=PARTNER_A
    ))["click_id"]
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


async def _hand_off(record_id, click_id, merchant_id):
    from services import storefront_handoff_record as handoffs

    binding = hashlib.sha256(("esc_" + record_id).encode("utf-8")).hexdigest()
    record = handoffs.HandoffRecord(
        record_id=record_id, binding=binding, partner_agent_id=PARTNER_A, via_agent_id=GATEWAY,
        seller_host=HOST, currency="USD", quoted_total_minor=2300, quote_source="seller_cart",
        buyer_market="US", referral_click_id=click_id, merchant_id=merchant_id,
    )
    assert await handoffs.record_handoff(record) == "recorded"

    async def status():
        return (await handoffs.read_handoff(record_id, binding, PARTNER_A))["handoff"]["status"]

    return status


async def test_a_test_order_on_a_referral_is_not_a_seller_order_on_postgres(db, monkeypatch):
    """The whole chain through the real writers: a referral, the hand-off naming it, then the order
    closed by the real conversion writer (a merchant's self-report). The writer stamps a rig
    seller's edge as test traffic, and the hand-off read does not report that order to the partner;
    a real seller's order, closed the same way, is reported."""
    from services import storefront_cart_referral as referrals
    from services import test_traffic
    from services.commerce_attribution_service import close_external_order_conversion

    monkeypatch.setenv("PIVOTA_TEST_MERCHANT_IDS", TEST_MERCHANT)
    later = datetime.now(timezone.utc) + timedelta(minutes=5)
    seen = {}
    for record_id, merchant in ((RECORD_ID, TEST_MERCHANT), (RECORD_ID_2, MERCHANT)):
        monkeypatch.delenv(test_traffic.STAMP_FLAG, raising=False)
        click_id = (await referrals.mint_referral(
            seller_host=HOST, market="US", merchant_id=merchant, partner_agent_id=PARTNER_A, via_agent_id=GATEWAY
        ))["click_id"]
        status = await _hand_off(record_id, click_id, merchant)
        assert await status() == "handed_off"
        monkeypatch.setenv(test_traffic.STAMP_FLAG, "true")
        closed = await close_external_order_conversion(
            merchant_id=merchant, click_id=click_id, external_order_id="ord_referral_pg_" + record_id[-1],
            gross_amount_cents=2300, currency="USD", converted_at=later, is_self_report=True,
        )
        assert closed and closed["click_matched"] is True and not closed.get("replayed")
        edge = dict(await db.fetch_one(
            "SELECT state, agent_id, metadata->>'test_traffic' AS stamped FROM commerce_attribution_edges "
            "WHERE click_id = :c", {"c": click_id},
        ))
        assert (edge["state"], edge["agent_id"]) == ("converted", PARTNER_A)
        seen[merchant] = (await status(), edge["stamped"])
    assert seen[TEST_MERCHANT] == ("handed_off", "true")
    assert seen[MERCHANT] == ("seller_order_observed", None)


async def test_a_real_order_on_a_test_referral_is_not_reported_on_postgres(db, monkeypatch):
    """A referral the writer stamped as test traffic, then a real seller's unstamped order closed on
    it by the real conversion writer: not reported. Unstamped referral, same order: reported."""
    from services import storefront_cart_referral as referrals
    from services import test_traffic
    from services.commerce_attribution_service import close_external_order_conversion

    monkeypatch.setenv("PIVOTA_TEST_MERCHANT_IDS", TEST_MERCHANT)
    later = datetime.now(timezone.utc) + timedelta(minutes=5)
    seen = {}
    for record_id, stamp_the_mint in ((RECORD_ID, True), (RECORD_ID_2, False)):
        monkeypatch.setenv(test_traffic.STAMP_FLAG, "true")
        # Minted for the rig seller so the real writer stamps the row; the row is then pointed at the real
        # seller so the conversion writer matches it and stamps nothing on the edge. The click's stamp stays.
        click_id = (await referrals.mint_referral(
            seller_host=HOST, market="US", merchant_id=TEST_MERCHANT if stamp_the_mint else MERCHANT,
            partner_agent_id=PARTNER_A, via_agent_id=GATEWAY,
        ))["click_id"]
        if stamp_the_mint:
            await db.execute("UPDATE surface_click_events SET merchant_id = :m WHERE click_id = :c", {"m": MERCHANT, "c": click_id})
        status = await _hand_off(record_id, click_id, MERCHANT)
        closed = await close_external_order_conversion(
            merchant_id=MERCHANT, click_id=click_id, external_order_id="ord_referral_pg_real_" + record_id[-1],
            gross_amount_cents=2300, currency="USD", converted_at=later, is_self_report=True,
        )
        assert closed and closed["click_matched"] is True
        row = dict(await db.fetch_one(
            "SELECT c.context->>'test_traffic' AS click_stamp, e.metadata->>'test_traffic' AS edge_stamp "
            "FROM surface_click_events c JOIN commerce_attribution_edges e ON e.click_id = c.click_id "
            "WHERE c.click_id = :c", {"c": click_id},
        ))
        seen[stamp_the_mint] = (row["click_stamp"], row["edge_stamp"], await status())
    assert seen[True] == ("true", None, "handed_off")
    assert seen[False] == (None, None, "seller_order_observed")


async def test_the_script_click_row_count_leaves_these_rows_out_on_postgres(db, monkeypatch):
    """scripts/smoke_commerce_channels_signoff.py's per-merchant click row count, its own SQL run on
    the production dialect beside a referral, the hand-off naming it and one ordinary link."""
    from services import storefront_cart_referral as referrals
    from services.commerce_attribution_service import IssuedClick, issue_clicks
    from tests.test_smoke_commerce_channels_signoff import click_row_count_sql

    sub_select = click_row_count_sql(monkeypatch).replace("%s", ":m")
    checkout = (await referrals.mint_referral(
        seller_host=HOST, market="US", merchant_id=REPORT_MERCHANT, partner_agent_id=PARTNER_A
    ))["click_id"]
    await _hand_off(RECORD_ID, checkout, REPORT_MERCHANT)
    await issue_clicks([IssuedClick(click_id="clk_referral_pg_smoke_0", merchant_id=REPORT_MERCHANT, surface="agent",
                                    dest_domain=HOST)])
    everything = await db.fetch_val("SELECT COUNT(*) FROM surface_click_events WHERE merchant_id = :m", {"m": REPORT_MERCHANT})
    assert (everything, await db.fetch_val(sub_select, {"m": REPORT_MERCHANT})) == (3, 1)


async def test_the_click_report_does_not_count_these_rows_on_postgres(db, monkeypatch):
    """services/click_reporting.py on the production dialect, for one seller: a door checkout (a
    referral and the hand-off naming it) and a referral whose cart was never built move no number;
    a normal link still counts."""
    from services import storefront_cart_referral as referrals
    from services.click_reporting import issued_vs_clicked
    from services.commerce_attribution_service import IssuedClick, issue_clicks

    start = datetime.now(timezone.utc) - timedelta(days=1)
    end = datetime.now(timezone.utc) + timedelta(days=1)

    async def report():
        return await issued_vs_clicked(start_at=start, end_at=end, merchant_id=REPORT_MERCHANT)

    def link(n):
        return IssuedClick(click_id=f"clk_referral_pg_report_{n}", merchant_id=REPORT_MERCHANT, surface="agent",
                           dest_domain=HOST, agent_id=PARTNER_A)

    await issue_clicks([link(n) for n in range(3)])
    await db.execute(
        "UPDATE surface_click_events SET click_count = 1, last_click_at = now() WHERE click_id = 'clk_referral_pg_report_0'"
    )
    before = await report()
    assert (before["issued"], before["clicked"], before["issued_not_clicked"], before["follow_rate"]) == (3, 1, 2, 0.3333)

    mint = dict(seller_host=HOST, market="US", merchant_id=REPORT_MERCHANT, partner_agent_id=PARTNER_A)
    checkout = (await referrals.mint_referral(**mint))["click_id"]
    await _hand_off(RECORD_ID, checkout, REPORT_MERCHANT)
    await referrals.mint_referral(**mint)
    on_the_surface = await db.fetch_all(
        "SELECT click_id FROM surface_click_events WHERE merchant_id = :m AND surface = 'ucp_storefront_handoff' "
        "AND issued_at IS NOT NULL", {"m": REPORT_MERCHANT},
    )
    assert len(on_the_surface) == 3
    assert await report() == before

    await issue_clicks([link(3)])
    after = await report()
    assert (after["issued"], after["clicked"], after["issued_not_clicked"], after["follow_rate"]) == (4, 1, 3, 0.25)
