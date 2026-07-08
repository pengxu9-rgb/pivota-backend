"""T2-2c — WooCommerce conversion closure (attributed-redirect lane, Phase 2).

Proves the Woo lane end to end with no live API:
  - mint side: referral_only destinations carry the click id as ``utm_content``
    (what WC 8.5+ core Order Attribution persists to order meta) alongside the
    legacy ``pvt_click_id`` param;
  - extraction: only OUR ``clk_…``-shaped values close conversions — a
    merchant's own utm_content campaign string never does;
  - closure + idempotency run through the REAL ``close_external_order_conversion``
    against the same FakeDB ON CONFLICT shape as test_t2_2b;
  - watermark rides the namespaced ``woo::<merchant_id>`` key so the Shopify
    lane's watermark is never stomped.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import commerce_attribution_service as svc  # noqa: E402
from services import external_conversion_poller as shopify_poller  # noqa: E402
from services import woocommerce_conversion_poller as woo  # noqa: E402
from services.outbound_links_service import append_referral_click_param  # noqa: E402


# --- fake DB (same shape as test_t2_2b) ----------------------------------------


class FakeDB:
    def __init__(
        self,
        *,
        click_row: Optional[Dict[str, Any]] = None,
        candidate_rows: Optional[List[Dict[str, Any]]] = None,
        woo_store_row: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.click_row = click_row
        self.candidate_rows = candidate_rows or []
        self.woo_store_row = woo_store_row
        self.edges: Dict[tuple, Dict[str, Any]] = {}
        self.insert_attempts = 0
        self.watermarks: Dict[str, Any] = {}

    async def fetch_one(self, query: Any, values: Any = None) -> Optional[Dict[str, Any]]:
        if isinstance(query, str) and "INSERT INTO commerce_attribution_edges" in query:
            self.insert_attempts += 1
            params = dict(values or {})
            key = (params["merchant_id"], params["external_order_id"])
            if key in self.edges:
                return None  # ON CONFLICT DO NOTHING
            self.edges[key] = params
            return {"edge_id": params["edge_id"]}
        if isinstance(query, str) and "external_conversion_poll_state" in query:
            mid = dict(values or {}).get("merchant_id")
            stored = self.watermarks.get(mid)
            return {"last_polled_at": stored} if stored is not None else None
        if isinstance(query, str) and "FROM merchant_stores" in query and "woocommerce" in query:
            return self.woo_store_row
        # SQLAlchemy select() on surface_click_events (the click lookup)
        return self.click_row

    async def fetch_all(self, query: Any, values: Any = None) -> List[Dict[str, Any]]:
        if isinstance(query, str) and "surface_click_events" in query and "merchant_stores" in query:
            return list(self.candidate_rows)
        return []

    async def execute(self, query: Any, values: Any = None) -> int:
        if isinstance(query, str) and "external_conversion_poll_state" in query:
            params = dict(values or {})
            self.watermarks[params["merchant_id"]] = params["last_polled_at"]
        return 0


@pytest.fixture(autouse=True)
def _silence_commerce_events(monkeypatch: pytest.MonkeyPatch):
    async def _noop(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {"interaction_id": "int_stub"}

    monkeypatch.setattr(svc, "record_commerce_event_best_effort", _noop)


def _install_db(monkeypatch: pytest.MonkeyPatch, db: FakeDB) -> None:
    monkeypatch.setattr(svc, "database", db)
    monkeypatch.setattr(shopify_poller, "database", db)  # watermark helpers live here
    monkeypatch.setattr(woo, "database", db)


def _click_row(**over: Any) -> Dict[str, Any]:
    base = {
        "click_id": "clk_wooknown1",
        "merchant_id": "merch_woo",
        "interaction_id": "int_click",
        "surface": "acp_feed",
        "commerce_surface": "acp_feed",
        "dest_domain": "brand-store.example",
    }
    base.update(over)
    return base


def _wc_order(**over: Any) -> Dict[str, Any]:
    base = {
        "id": 5501,
        "status": "processing",
        "total": "34.50",
        "currency": "USD",
        "date_created_gmt": "2026-07-06T09:00:00",
        "date_paid_gmt": "2026-07-06T09:00:10",
        "meta_data": [
            {"key": "_wc_order_attribution_source_type", "value": "utm"},
            {"key": "_wc_order_attribution_utm_content", "value": "clk_wooknown1"},
        ],
    }
    base.update(over)
    return base


def _install_wc_fetch(monkeypatch: pytest.MonkeyPatch, pages: List[List[Dict[str, Any]]]):
    calls: List[Dict[str, Any]] = []
    served = {"i": 0}

    async def fake_fetch(*, store_url, consumer_key, consumer_secret, page, modified_after, timeout_s=15.0):
        calls.append({"page": page, "modified_after": modified_after})
        idx = min(served["i"], len(pages) - 1)
        served["i"] += 1
        return pages[idx] if pages else []

    monkeypatch.setattr(woo, "_fetch_wc_orders_page", fake_fetch)
    return calls


_CREDS = {"store_url": "https://brand-store.example", "consumer_key": "ck_x", "consumer_secret": "cs_y"}


# --- mint side ---------------------------------------------------------------------


def test_referral_dest_carries_utm_content_click_id() -> None:
    url = append_referral_click_param("https://brand-store.example/p/serum", "clk_abc")
    assert "pvt_click_id=clk_abc" in url
    assert "utm_content=clk_abc" in url


def test_referral_dest_respects_existing_utm_content() -> None:
    url = append_referral_click_param(
        "https://brand-store.example/p/serum?utm_content=merchant_campaign", "clk_abc"
    )
    assert "pvt_click_id=clk_abc" in url
    assert url.count("utm_content=") == 1  # merchant's own value wins


# --- extraction --------------------------------------------------------------------


def test_extract_click_id_prefers_wc_order_attribution_meta() -> None:
    assert woo.extract_click_id_from_wc_order(_wc_order()) == "clk_wooknown1"


def test_extract_click_id_rejects_merchant_campaign_values() -> None:
    order = _wc_order(meta_data=[{"key": "_wc_order_attribution_utm_content", "value": "spring_sale_2026"}])
    assert woo.extract_click_id_from_wc_order(order) is None


def test_extract_click_id_fallback_keys() -> None:
    order = _wc_order(meta_data=[{"key": "pvt_click_id", "value": "clk_fallback9"}])
    assert woo.extract_click_id_from_wc_order(order) == "clk_fallback9"


# --- per-merchant poll: close, skip, idempotency, watermark --------------------------


@pytest.mark.asyncio
async def test_poll_closes_attributed_paid_wc_order(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDB(click_row=_click_row())
    _install_db(monkeypatch, db)
    _install_wc_fetch(monkeypatch, [[_wc_order()]])

    res = await woo.poll_wc_conversions_for_merchant(merchant_id="merch_woo", credentials=_CREDS)

    assert res["closed"] == 1 and res["ok"] is True
    assert ("merch_woo", "5501") in db.edges
    edge = db.edges[("merch_woo", "5501")]
    assert edge["gross_attributed_gmv_cents"] == 3450
    assert edge["currency"] == "USD"
    # woo:: namespaced watermark, Shopify key untouched
    assert "woo::merch_woo" in db.watermarks
    assert "merch_woo" not in db.watermarks


@pytest.mark.asyncio
async def test_poll_skips_unpaid_and_unattributed(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDB(click_row=_click_row())
    _install_db(monkeypatch, db)
    _install_wc_fetch(
        monkeypatch,
        [[
            _wc_order(id=1, status="pending"),                     # unpaid
            _wc_order(id=2, meta_data=[]),                         # no click
            _wc_order(id=3),                                       # closes
        ]],
    )

    res = await woo.poll_wc_conversions_for_merchant(merchant_id="merch_woo", credentials=_CREDS)

    assert res["closed"] == 1
    assert res["skipped_unpaid"] == 1
    assert res["skipped_no_click"] == 1
    assert list(db.edges) == [("merch_woo", "3")]


@pytest.mark.asyncio
async def test_repoll_is_idempotent_no_double_count(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDB(click_row=_click_row())
    _install_db(monkeypatch, db)
    _install_wc_fetch(monkeypatch, [[_wc_order()]])

    await woo.poll_wc_conversions_for_merchant(merchant_id="merch_woo", credentials=_CREDS)
    await woo.poll_wc_conversions_for_merchant(merchant_id="merch_woo", credentials=_CREDS)

    assert len(db.edges) == 1  # ON CONFLICT guard held
    assert db.insert_attempts == 2  # second run attempted and deduped


@pytest.mark.asyncio
async def test_missing_credentials_skips_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDB()
    _install_db(monkeypatch, db)

    res = await woo.poll_wc_conversions_for_merchant(merchant_id="merch_woo")  # no store row

    assert res["ok"] is False and res["reason"] == "no_woocommerce_credentials"
    assert db.edges == {}


# --- batch lane ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_lane_enumerates_woo_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDB(
        click_row=_click_row(),
        candidate_rows=[{"merchant_id": "merch_woo"}],
        woo_store_row={"store_id": "st1", "merchant_id": "merch_woo",
                       "domain": "brand-store.example", "api_key": "ck_x:cs_y"},
    )
    _install_db(monkeypatch, db)
    _install_wc_fetch(monkeypatch, [[_wc_order()]])

    results = await woo.poll_wc_conversions_batch_lane()

    assert len(results) == 1
    assert results[0]["platform"] == "woocommerce"
    assert results[0]["closed"] == 1


def test_api_key_unpack_variants() -> None:
    assert woo._unpack_wc_api_key('{"consumer_key":"ck_1","consumer_secret":"cs_2"}') == ("ck_1", "cs_2")
    assert woo._unpack_wc_api_key("ck_1:cs_2") == ("ck_1", "cs_2")
    assert woo._unpack_wc_api_key("ck_only") == ("ck_only", "")
    assert woo._unpack_wc_api_key(None) == ("", "")
