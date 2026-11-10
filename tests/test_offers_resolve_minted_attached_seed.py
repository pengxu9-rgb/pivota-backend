"""offers.resolve for a MINTED catalog row, through the HTTP route with the reads faked by SQL shape
(OFFERS_RESOLVE_ATTACHED_SEED_ARM). tests/test_offers_resolve_minted_attached_seed_postgres.py sends
the same statements to a real Postgres; this twin runs wherever the suite runs and pins what the
flag sends: nothing when off, two statements when on, and only after the existing keys miss.

The seed row is the one the drain's own planner emits for the record (`ingest_validated_jsonl`),
stamped as a seed the refresh jobs have just read.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from main import app

FLAG = "OFFERS_RESOLVE_ATTACHED_SEED_ARM"
ARM = "external_seed_by_signature_attached_key"
URL = "https://brand-x.example/products/calm-barrier-cream"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _minted():
    """(catalog row, seed row) as the drain plans them for one brand-store listing."""
    from services.catalog_enrichment_agent.ingestion import ingest_validated_jsonl

    plan = ingest_validated_jsonl([{
        "pdp": {"brand": "Brand X", "product_name": "Calm Barrier Cream",
                "category_path": "beauty/skincare/moisturizer", "currency": "USD",
                "source_domain": "brand-x.example"},
        "offers": [{"merchant_inferred": "Brand X", "destination_url": URL, "canonical_url": URL,
                    "image_url": "https://brand-x.example/img/calm-barrier-cream.jpg", "price": 21.0,
                    "in_stock": True, "validated_at": "2026-11-10T10:00:00Z"}],
    }])
    now = datetime.now(timezone.utc).isoformat()
    seed = dict(plan["seeds"][0])
    seed["seed_data"] = {**json.loads(seed["seed_data"]), "snapshot": {"extracted_at": now}}
    seed.update(destination_checked_at=now, destination_http_status=200, destination_verdict="live",
                destination_failure_streak=0, utm_template=None)
    return plan["pdps"][0], seed


def _invoke(monkeypatch, client, *, flag, pdp, seed, mirror_seed=None, market="US"):
    """POST offers.resolve for the minted signature. Returns (body, the statements sent)."""
    import routes.agent_shop_gateway as gateway

    sent = []

    async def fake_fetch_all(query, values=None):
        q = str(query)
        sent.append((q, dict(values or {})))
        if "FROM catalog_products" in q and "SELECT source_ref, source_product_id" in q:
            # The existing map read: a minted row has no source_ref and a slug source_product_id.
            return [{"source_ref": mirror_seed["id"] if mirror_seed else None,
                     "source_product_id": pdp["source_product_id"]}]
        if "FROM external_product_seeds" in q and "id = ANY(:mapped_seed_ids)" in q:
            return [mirror_seed] if mirror_seed else []
        if "FROM catalog_products" in q and "SELECT product_key" in q:
            assert values == {"sig_aliases": [pdp["pivota_signature_id"]]}
            assert "suppressed_at IS NULL" in q and "suppression_reason IS NULL" in q
            return [{"product_key": pdp["product_key"]}]
        if "FROM external_product_seeds" in q and "attached_product_key = ANY(:product_keys)" in q:
            assert "status = 'active'" in q and "price_currency" in q
            assert "catalog_source_quarantine" in q, "the shared quarantine anti-join"
            assert values["product_keys"] == [pdp["product_key"]]
            return [seed] if values["serving_currency"] == seed["price_currency"] else []
        return []

    if flag is None:
        monkeypatch.delenv(FLAG, raising=False)
    else:
        monkeypatch.setenv(FLAG, flag)
    # The anonymous-invoke limiter is module-level shared state; rpm 0 spends none of it.
    monkeypatch.setenv("SHOP_INVOKE_ANON_RPM", "0")
    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gateway, "_make_external_redirect_url",
                        AsyncMock(return_value="https://example.com/r?token=minted"))
    res = client.post("/agent/shop/v1/invoke", json={
        "operation": "offers.resolve",
        "payload": {"product": {"product_id": pdp["pivota_signature_id"], "merchant_id": pdp["merchant_id"]},
                    "limit": 10, "market": market, "tool": "*", "commerce_surface": "agent_api"},
        "metadata": {"source": "creator-agent-ui"},
    })
    assert res.status_code == 200, res.text
    return res.json(), sent


def _arm_statements(sent):
    return [q for q, _ in sent if "SELECT product_key" in q or "attached_product_key = ANY(:product_keys)" in q]


def _seed_query(body):
    [src] = [s for s in body["metadata"]["sources"] if s["source"] == "external_product_seeds"]
    return src["query"], src["row_count"]


def test_the_planned_rows_cannot_be_joined_by_the_existing_keys():
    pdp, seed = _minted()
    assert "source_ref" not in pdp or pdp["source_ref"] is None
    assert pdp["source_product_id"] not in (seed["id"], seed["external_product_id"])
    assert seed["attached_product_key"] == pdp["product_key"]


@pytest.mark.parametrize("off", [None, "", "0", "false", "shadow", "enforce"])
def test_flag_off_sends_neither_statement_and_serves_nothing(monkeypatch, client, off):
    pdp, seed = _minted()
    body, sent = _invoke(monkeypatch, client, flag=off, pdp=pdp, seed=seed)
    assert _arm_statements(sent) == []
    assert body["offers"] == [] and _seed_query(body) == ("external_seed_by_fuzzy_ref", 0)


def test_flag_on_serves_the_attached_seed_of_a_minted_row(monkeypatch, client):
    pdp, seed = _minted()
    body, sent = _invoke(monkeypatch, client, flag="1", pdp=pdp, seed=seed)
    assert len(_arm_statements(sent)) == 2
    assert _seed_query(body) == (ARM, 1)
    [offer] = body["offers"]
    assert offer["source"]["seed_id"] == seed["id"] and offer["purchase_route"] == "affiliate_outbound"
    assert offer["affiliate_url"] == "https://example.com/r?token=minted"
    assert (offer["price"], offer["currency"]) == (21.0, "USD")


def test_flag_on_never_runs_when_the_existing_keys_found_the_seed(monkeypatch, client):
    pdp, seed = _minted()
    mirror_seed = {**seed, "id": "eps_" + "cd" * 12, "external_product_id": "ext_" + "ab" * 12}
    body, sent = _invoke(monkeypatch, client, flag="1", pdp=pdp, seed=seed, mirror_seed=mirror_seed)
    assert _arm_statements(sent) == []
    assert _seed_query(body) == ("external_seed_by_pivota_signature", 1)
    assert [o["source"]["seed_id"] for o in body["offers"]] == [mirror_seed["id"]]


@pytest.mark.parametrize("market, statements", [("SG", 2), ("DE", 0)])
def test_flag_on_binds_the_buyer_markets_currency(monkeypatch, client, market, statements):
    """A USD seed is not an SG buyer's (the statement binds SGD and finds nothing); a market with no
    known currency reads no seed at all."""
    pdp, seed = _minted()
    body, sent = _invoke(monkeypatch, client, flag="1", pdp=pdp, seed=seed, market=market)
    assert len(_arm_statements(sent)) == statements
    assert body["offers"] == []
    if statements:
        [values] = [v for q, v in sent if "attached_product_key = ANY(:product_keys)" in q]
        assert values["serving_currency"] == "SGD"
