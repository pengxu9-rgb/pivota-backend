"""
An external seed's OFFER carries the seed's own stock claim — the one the search card carries.

THE DEFECT. `offers.resolve` built each seed offer's `in_stock` from

    v.availability or seed_data.availability or row.availability or "unknown"

read against three spellings, anything else in stock. That is the stale side first: the nightly
refresh (`routes/employee_products._refresh_external_seed_by_id`) rewrites the `availability`
column and `snapshot.availability` from the page on every read, but writes
`seed_data.availability` only when missing and replaces stored variants only under its
overwrite predicates. Measured on the serving image 2026-09-25: of 3,593 seeds that pass the
referral gate into this path, 99 with column out_of_stock resolved to an in-stock offer (48
of them single-variant, e.g. roundlab.com eps_0a5f2785ba840d9fb02ce4b4). "Out of Stock" with
capitals and spaces (kyliecosmetics.com) and "unknown" were also served in stock.

THE RULE is `services/external_seed_stock` (#2325, #2333), which the search builders already
serve: column > explicit variants > seed_data / snapshot; a column an explicit variant
contradicts makes the seed unknown and withholds every variant's boolean. An unknown offer
ships `availability: "unknown"` and NO `in_stock` key.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from main import app

# Both liveness facts the referral gate needs before a seed reaches the offer lane; see the
# notes on the same constants in tests/test_offers_resolve.py.
_VERIFIED_DESTINATION = {
    "destination_checked_at": datetime.now(timezone.utc).isoformat(),
    "destination_http_status": 200,
    "destination_verdict": "live",
    "destination_failure_streak": 0,
}
_EXTRACTED_AT = datetime.now(timezone.utc).isoformat()

UNKNOWN = None  # an offer with no `in_stock` key


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _variant(vid: str, availability: Any) -> Dict[str, Any]:
    return {
        "variant_id": vid,
        "title": f"Variant {vid}",
        "price_amount": 18.0,
        "price_currency": "USD",
        **({"availability": availability} if availability is not None else {}),
    }


def _seed_row(
    *,
    availability: Any,
    variants: List[Dict[str, Any]],
    seed_data_extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    seed_data: Dict[str, Any] = {"brand": "Round Lab", "title": "Round Lab Dokdo Toner", "variants": variants}
    seed_data.update(seed_data_extra or {})
    seed_data["snapshot"] = {**(seed_data.get("snapshot") or {}), "extracted_at": _EXTRACTED_AT}
    return {
        "id": "eps_0a5f2785ba840d9fb02ce4b4",
        "external_product_id": "ext_roundlab_dokdo",
        "market": "US",
        "tool": "*",
        "destination_url": "https://roundlab.com/products/dokdo-toner",
        "canonical_url": "https://roundlab.com/products/dokdo-toner",
        "domain": "roundlab.com",
        "title": "Round Lab Dokdo Toner",
        "price_amount": 18.0,
        "price_currency": "USD",
        "availability": availability,
        "utm_template": None,
        "seed_data": seed_data,
        "status": "active",
        **_VERIFIED_DESTINATION,
    }


def _resolve(monkeypatch: pytest.MonkeyPatch, client: TestClient, seed: Dict[str, Any]) -> List[Dict[str, Any]]:
    import routes.agent_shop_gateway as gateway

    async def fake_fetch_all(query: str, values=None):
        return [seed] if "FROM external_product_seeds" in str(query) else []

    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(
        gateway, "_make_external_redirect_url", AsyncMock(return_value="https://example.com/r?token=test")
    )
    res = client.post(
        "/agent/shop/v1/invoke",
        json={
            "operation": "offers.resolve",
            "payload": {
                "product": {"product_id": seed["external_product_id"]},
                "limit": 20,
                "market": "US",
                "tool": "*",
                "commerce_surface": "agent_api",
            },
            "metadata": {"source": "creator-agent-ui"},
        },
    )
    assert res.status_code == 200, res.text
    offers = [o for o in (res.json().get("offers") or []) if o.get("purchase_route") == "affiliate_outbound"]
    assert offers, "the seed must reach the offer lane, or this test measures the gate instead"
    return offers


def _stock_by_variant(offers: List[Dict[str, Any]]) -> Dict[str, Optional[bool]]:
    out: Dict[str, Optional[bool]] = {}
    for offer in offers:
        vid = offer["source"]["variant_id"]
        if "in_stock" in offer:
            assert isinstance(offer["in_stock"], bool)
            assert "availability" not in offer
            out[vid] = offer["in_stock"]
        else:
            # Unknown is spelled one way, never as `in_stock: None`.
            assert offer.get("availability") == "unknown"
            out[vid] = UNKNOWN
    return out


# Each case: the seed as stored, and the stock each variant's offer must carry. No zero-variant
# case: the referral gate sends a seed with no variants to live verification (`zero_variants`),
# so it never reaches this lane.
_CASES = [
    pytest.param(
        # roundlab.com eps_0a5f2785ba840d9fb02ce4b4 as served 2026-09-25: the refresh re-read the
        # page (column + snapshot out) but left the ingest-time variant and seed_data at in_stock.
        _seed_row(
            availability="out_of_stock",
            variants=[_variant("1", "in_stock")],
            seed_data_extra={"availability": "in_stock", "snapshot": {"availability": "out_of_stock"}},
        ),
        {"1": UNKNOWN},
        id="stale_in_stock_variant_under_out_of_stock_column",
    ),
    pytest.param(
        # A signal-less variant under an out-of-stock column, with seed_data.availability left
        # at in_stock by the refresh: the old chain read seed_data BEFORE the column.
        _seed_row(
            availability="out_of_stock",
            variants=[_variant("1", None)],
            seed_data_extra={"availability": "in_stock"},
        ),
        {"1": False},
        id="stale_seed_data_availability_under_out_of_stock_column",
    ),
    pytest.param(
        _seed_row(availability="out_of_stock", variants=[_variant("1", "out_of_stock"), _variant("2", "sold_out")]),
        {"1": False, "2": False},
        id="every_variant_out_agrees_with_the_column",
    ),
    pytest.param(
        _seed_row(availability="in_stock", variants=[_variant("1", "in_stock"), _variant("2", "out_of_stock")]),
        {"1": True, "2": False},
        id="one_shade_out_the_rest_in",
    ),
    pytest.param(
        _seed_row(availability="in_stock", variants=[_variant("1", None)]),
        {"1": True},
        id="signal_less_variant_inherits_an_in_stock_column",
    ),
    pytest.param(
        # kyliecosmetics.com spelling; the old three-spelling set read it as in stock.
        _seed_row(availability="Out of Stock", variants=[_variant("1", "Out of Stock")]),
        {"1": False},
        id="out_of_stock_spelled_with_capitals_and_spaces",
    ),
    pytest.param(
        _seed_row(availability="https://schema.org/OutOfStock", variants=[_variant("1", None)]),
        {"1": False},
        id="schema_org_out_of_stock",
    ),
    pytest.param(
        _seed_row(availability="unknown", variants=[_variant("1", "unknown")]),
        {"1": UNKNOWN},
        id="unknown_is_not_in_stock",
    ),
    pytest.param(
        # Column in, every stored variant out: contradiction, so nobody's boolean is served.
        _seed_row(availability="in_stock", variants=[_variant("1", "out_of_stock")]),
        {"1": UNKNOWN},
        id="stale_out_of_stock_variant_under_in_stock_column",
    ),
    pytest.param(
        # No usable column: explicit variants decide, then seed_data.
        _seed_row(availability=None, variants=[_variant("1", None)], seed_data_extra={"availability": "out_of_stock"}),
        {"1": False},
        id="no_column_seed_data_is_the_last_fallback",
    ),
]


@pytest.mark.parametrize("seed, expected", _CASES)
def test_a_seed_offer_carries_the_seeds_own_stock_claim(monkeypatch, client, seed, expected):
    assert _stock_by_variant(_resolve(monkeypatch, client, seed)) == expected


@pytest.mark.parametrize("seed, expected", _CASES)
@pytest.mark.asyncio
async def test_the_offer_and_the_search_card_make_the_same_claim(monkeypatch, client, seed, expected):
    """One rule, two surfaces: the offer an agent resolves must not say in stock where the
    search card for the same seed says sold out or unknown (routes/agent_api builder)."""
    import routes.agent_api as agent_api

    offers = _stock_by_variant(_resolve(monkeypatch, client, seed))

    async def _gate(row, **kwargs):
        return (False, None)

    monkeypatch.setattr(agent_api, "should_block_external_referral_runtime", _gate)

    class _Req:
        base_url = "https://api.pivota.cc/"

    card = await agent_api._build_external_seed_product(
        req=_Req(), seed_row=seed, allowed_domains=["roundlab.com"],
    )
    stored = seed["seed_data"]["variants"]
    card_variants = card["variants"]
    assert len(card_variants) == len(stored)
    assert offers == {v["variant_id"]: served.get("in_stock") for v, served in zip(stored, card_variants)}


def test_an_unknown_seed_offer_is_not_ranked_as_sold_out() -> None:
    import routes.agent_shop_gateway as gateway

    unknown = {"offer_id": "of:unknown", "confidence": 0.8, "purchase_route": "affiliate_outbound",
               "availability": "unknown"}
    assert gateway._offer_is_known_unavailable(unknown) is False
