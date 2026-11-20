"""An allowlist market the registry does not list allows NO destination.

An empty allowlist allows every domain (backward compatible: a market with no rows yet). That is only safe for a
market the registry lists. A look-alike ('ſg', kept as sent by the ASCII-first key), a three-letter code or an
unlisted code keyed its own empty allowlist and so allowed every domain: the employee seed preview answered
`domain_allowed: true` under 'ſg'. Now it is refused, named (`allowlist_market_unknown`). Absent still keys US.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, List

import pytest


@pytest.fixture
def empty_allowlists(monkeypatch: pytest.MonkeyPatch):
    import services.outbound_links_service as outbound_links_service

    asked: List[str] = []

    async def fake_fetch(cache_market: str) -> List[str]:
        asked.append(cache_market)
        return []

    cache, inflight = outbound_links_service._ALLOWED_DOMAIN_CACHE, outbound_links_service._ALLOWED_DOMAIN_INFLIGHT
    saved = (dict(cache), dict(inflight))
    cache.clear(); inflight.clear()
    monkeypatch.setattr(outbound_links_service, "_fetch_allowed_domains_for_market", fake_fetch)
    try:
        yield outbound_links_service, asked
    finally:
        cache.clear(); cache.update(saved[0]); inflight.clear(); inflight.update(saved[1])


@pytest.mark.parametrize("market", ["ſg", "uſ", "ＳＧ", "USA", "ZZ", "EU-DE", "en-US"])
def test_a_market_the_registry_does_not_list_allows_no_destination(empty_allowlists, market) -> None:
    service, asked = empty_allowlists
    assert service.allowlist_market_refusal(market) == "allowlist_market_unknown"
    allowed = asyncio.run(service._is_domain_allowed(market=market, destination_url="https://shop.example.com/p/1"))
    assert allowed is False
    assert asked == [], "nothing is read for a market the allowlist cannot key"


@pytest.mark.parametrize("market", [None, "", "  ", "US", "us", " SG ", "GB", "JP"])
def test_a_listed_or_absent_market_keeps_the_empty_allowlist_reading(empty_allowlists, market) -> None:
    service, _ = empty_allowlists
    assert service.allowlist_market_refusal(market) is None
    assert asyncio.run(service._is_domain_allowed(market=market, destination_url="https://shop.example.com/p/1")) is True


def _preview(monkeypatch: pytest.MonkeyPatch, market: Any, *, snapshot_fails: bool = False) -> dict:
    import routes.employee_products as ep

    async def fake_resolve(**_kw: Any) -> Any:
        if snapshot_fails:
            raise RuntimeError("store unreachable")
        return SimpleNamespace(canonical_url="https://shop.example.com/p/1", domain="shop.example.com", title="Item",
                               price_amount=10.0, price_currency="USD", availability="in_stock", image_url=None)

    monkeypatch.setattr(ep, "resolve_external_offer", fake_resolve)
    body = ep.PreviewExternalSeedRequest(destination_url="https://shop.example.com/p/1", market=market)
    return asyncio.run(ep.preview_external_seed(body=body, current_user={"role": "admin"}))


@pytest.mark.parametrize("snapshot_fails", [False, True])
def test_the_employee_preview_names_why_a_lookalike_market_allows_no_domain(monkeypatch, empty_allowlists, snapshot_fails) -> None:
    out = _preview(monkeypatch, "ſg", snapshot_fails=snapshot_fails)
    assert out["preview"]["domain_allowed"] is False
    assert out["preview"]["domain_allowed_reason"] == "allowlist_market_unknown"


@pytest.mark.parametrize("market", ["US", None, "SG"])
@pytest.mark.parametrize("snapshot_fails", [False, True])
def test_the_employee_preview_is_unchanged_for_a_listed_or_absent_market(monkeypatch, empty_allowlists, market, snapshot_fails) -> None:
    out = _preview(monkeypatch, market, snapshot_fails=snapshot_fails)
    assert out["preview"]["domain_allowed"] is True
    assert "domain_allowed_reason" not in out["preview"]


# --- the minters that do call _is_domain_allowed --------------------------------------------------------------


@pytest.mark.parametrize("market, reason", [("USA", "allowlist_market_unknown"), ("*", "allowlist_market_unknown"),
                                            ("ſg", "allowlist_market_unknown"), ("US", None), (None, None)])
def test_links_resolve_refuses_a_rule_under_a_market_the_registry_does_not_list(monkeypatch, empty_allowlists, market, reason) -> None:
    from fastapi.testclient import TestClient

    from main import app
    service, asked = empty_allowlists

    async def fake_rule(**_kw: Any) -> dict:
        return {"id": "rule_1", "destination_url": "https://shop.example.com/p/1"}

    monkeypatch.setattr(service, "_select_best_rule", fake_rule)
    body = {"tool": "*", "candidates": {}, **({} if market is None else {"market": market})}
    res = TestClient(app).post("/api/links/resolve", json=body)
    assert res.status_code == 200, res.text
    out = res.json()
    if reason:
        assert out == {"matched": False, "resolved": None, "reason": reason}
        assert asked == [], "no allowlist is read for it"
    else:
        assert out["matched"] is True and "/r?token=" in out["resolved"]["redirectUrl"]


@pytest.fixture
def employee_client(monkeypatch: pytest.MonkeyPatch):
    from unittest.mock import AsyncMock

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import routes.employee_products as ep

    async def override_employee():
        return {"employee_id": "emp_test", "email": "test@example.com"}

    app = FastAPI()
    app.include_router(ep.router)
    app.dependency_overrides[ep.get_current_employee] = override_employee
    monkeypatch.setattr(ep, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(ep, "_get_primary_offer", AsyncMock(return_value=None))
    minted: List[Any] = []

    async def fake_mint(**kw: Any) -> str:
        minted.append(kw["market"])
        return f"https://example.com/r?token={kw['market']}"

    monkeypatch.setattr(ep, "_make_redirect_url", fake_mint)
    return TestClient(app), ep, minted


def _rows(*markets: str) -> List[dict]:
    from tests.test_employee_external_seed_pdp import _seed_row

    return [_seed_row(id=f"eps_{i}", market=m) for i, m in enumerate(markets)]


@pytest.mark.parametrize("path", [
    "/employee/products/external_seed%7Cexternal%7Cext_test_1/offers",
    "/employee/products/external_seed%7Cexternal%7Cext_test_1/external-links",
])
def test_a_mixed_partition_list_refuses_only_the_rows_the_allowlist_cannot_key(monkeypatch, employee_client, path) -> None:
    client, ep, minted = employee_client
    rows = _rows("US", "*", "EU-DE", "SG")

    async def fake_fetch_all(query: Any, values: Any = None):
        return [dict(r) for r in rows] if "FROM external_product_seeds" in str(query) else []

    monkeypatch.setattr(ep.database, "fetch_all", fake_fetch_all)
    res = client.get(path, headers={"Authorization": "Bearer test-token"})
    assert res.status_code == 200, res.text
    items = {i.get("seed_id") or i.get("id"): i for i in (res.json().get("items") or res.json().get("links") or [])}
    assert set(items) == {"eps_0", "eps_1", "eps_2", "eps_3"}, "every row is still listed"
    for sid in ("eps_0", "eps_3"):
        assert items[sid]["action"]["redirect_url"].startswith("https://example.com/r?token=")
        assert "redirect_refused_reason" not in items[sid]["action"]
    for sid in ("eps_1", "eps_2"):
        assert items[sid]["action"]["redirect_url"] is None
        assert items[sid]["action"]["redirect_refused_reason"] == "allowlist_market_unknown"
    assert sorted(minted) == ["SG", "US"], "only the listed rows are minted"


@pytest.mark.parametrize("market, refused", [("*", True), ("EU-DE", True), ("US", False)])
def test_refresh_answers_a_named_refusal_after_its_write_never_a_400(monkeypatch, employee_client, market, refused) -> None:
    client, ep, minted = employee_client
    wrote: List[str] = []

    async def fake_refresh(seed_id: str) -> dict:
        wrote.append(seed_id)
        return {"status": "success", "market": market, "tool": "*", "canonical_url": "https://example.com/p/1",
                "dest": "https://example.com/p/1", "utm_template": None, "attached_product_key": None,
                "attached_variant_id": None, "disclosure_text": "d"}

    monkeypatch.setattr(ep, "_refresh_external_seed_by_id", fake_refresh)
    res = client.post("/employee/products/external-seeds/eps_9/refresh", headers={"Authorization": "Bearer test-token"})
    assert res.status_code == 200, res.text
    assert wrote == ["eps_9"]
    action = res.json()["action"]
    if refused:
        assert action["redirect_url"] is None and action["redirect_refused_reason"] == "allowlist_market_unknown"
        assert minted == []
    else:
        assert action["redirect_url"].startswith("https://example.com/r?token=") and "redirect_refused_reason" not in action


def test_the_preview_still_refuses_a_lookalike_when_the_allowlist_read_fails(monkeypatch, empty_allowlists) -> None:
    import routes.employee_products as ep

    async def boom(**_kw: Any) -> bool:
        raise RuntimeError("allowlist read failed")

    monkeypatch.setattr(ep, "_is_domain_allowed", boom)
    out = _preview(monkeypatch, "ſg")
    assert out["preview"]["domain_allowed"] is False, "the fail-open never opens a market the allowlist cannot key"
    assert out["preview"]["domain_allowed_reason"] == "allowlist_market_unknown"
    out = _preview(monkeypatch, "US")
    assert out["preview"]["domain_allowed"] is True, "a listed market keeps the fail-open, as before"


# --- the REAL raising mint on the one-seed read and the merchant-product /offers lane (P3) -------------------
# These lanes went through `_make_redirect_url`, which answers 400 DOMAIN_NOT_ALLOWED for a market the allowlist
# cannot key. The fixture below keeps that real mint (only its clock and signing secret are fixed), so a lane put
# back on it fails these tests with a 400 instead of passing on a fake.


def _placeholder_row(**overrides: Any) -> dict:
    from tests.test_employee_external_seed_pdp import _seed_row

    host = overrides.pop("host", "store-one.test")
    row = _seed_row(destination_url=f"https://{host}/p/1", canonical_url=f"https://{host}/p/1", domain=host,
                    image_url=f"https://{host}/img.jpg", **overrides)
    row["seed_data"] = {**row["seed_data"], "image_urls": [f"https://{host}/img.jpg"]}
    return row


@pytest.fixture
def real_mint_client(monkeypatch: pytest.MonkeyPatch, empty_allowlists):
    from unittest.mock import AsyncMock

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import routes.employee_products as ep
    service, _asked = empty_allowlists

    async def override_employee():
        return {"employee_id": "emp_test", "email": "test@example.test"}

    app = FastAPI()
    app.include_router(ep.router)
    app.dependency_overrides[ep.get_current_employee] = override_employee
    monkeypatch.setattr(ep, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(ep, "_get_primary_offer", AsyncMock(return_value=None))
    monkeypatch.setenv("OUTBOUND_LINKS_SIGNING_SECRET", "test-signing-secret")
    monkeypatch.setattr(service, "_now_ts", lambda: 1_700_000_000)
    return TestClient(app), ep


def test_one_seed_under_a_market_the_allowlist_cannot_key_gets_no_link_and_the_reason_not_a_400(
    monkeypatch, real_mint_client
) -> None:
    client, ep = real_mint_client
    row = _placeholder_row(id="eps_star", market="*")

    async def fake_fetch_one(query: Any, values: Any = None):
        return dict(row) if "FROM external_product_seeds" in str(query) else None

    monkeypatch.setattr(ep.database, "fetch_one", fake_fetch_one)
    res = client.get("/employee/products/external-seeds/eps_star", headers={"Authorization": "Bearer test-token"})
    assert res.status_code == 200, res.text
    action = res.json()["action"]
    assert action["redirect_url"] is None
    assert action["redirect_refused_reason"] == "allowlist_market_unknown"


def _merchant_offers(monkeypatch, client, ep, rows: List[dict]) -> dict:
    from unittest.mock import AsyncMock

    product_data = {"title": "Placeholder Item", "variants": [{"id": "v1", "price": "10.00"}]}
    monkeypatch.setattr(ep, "_fetch_latest_products_cache_row", AsyncMock(return_value={"product_data": product_data}))

    async def fake_fetch_one(query: Any, values: Any = None):
        return None  # no product group: the merchant product alone

    async def fake_fetch_all(query: Any, values: Any = None):
        return [dict(r) for r in rows] if "FROM external_product_seeds" in str(query) else []

    monkeypatch.setattr(ep.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(ep.database, "fetch_all", fake_fetch_all)
    res = client.get("/employee/products/merchant_test%7Cshopify%7Cp1/offers", headers={"Authorization": "Bearer test-token"})
    assert res.status_code == 200, res.text
    return {i.get("id"): i for i in res.json()["items"]}


def test_merchant_product_offers_refuse_only_the_unlisted_market_row_and_leave_the_listed_link_unchanged(
    monkeypatch, real_mint_client
) -> None:
    client, ep = real_mint_client
    listed = _placeholder_row(id="eps_us", market="US", host="store-one.test")
    unlisted = _placeholder_row(id="eps_star", market="*", host="store-two.test")

    alone = _merchant_offers(monkeypatch, client, ep, [listed])
    mixed = _merchant_offers(monkeypatch, client, ep, [listed, unlisted])
    assert set(mixed) == {"merchant_test|shopify|p1", "eps_us", "eps_star"}, "every offer is still listed"
    assert mixed["eps_us"]["action"]["redirect_url"].startswith("http://testserver/r?token=")
    assert mixed["eps_us"] == alone["eps_us"], "the listed row's offer, link included, is unchanged by its neighbour"
    assert "redirect_refused_reason" not in mixed["eps_us"]["action"]
    assert mixed["eps_star"]["action"]["redirect_url"] is None
    assert mixed["eps_star"]["action"]["redirect_refused_reason"] == "allowlist_market_unknown"
