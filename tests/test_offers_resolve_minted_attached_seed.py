"""offers.resolve for a MINTED catalog row, through the HTTP route with the reads faked by SQL shape
(OFFERS_RESOLVE_ATTACHED_SEED_ARM). tests/test_offers_resolve_minted_attached_seed_postgres.py sends
the same statements to a real Postgres; this twin runs wherever the suite runs and pins what the
flag sends: nothing when off, two statements when on, and only after the existing keys miss.

The seed row is the one the drain's own planner emits for the record (`ingest_validated_jsonl`),
stamped as a seed the refresh jobs have just read.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
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


def _offer(host, *, seller="Brand X", price=21.0):
    url = f"https://{host}/products/calm-barrier-cream"
    return {"merchant_inferred": seller, "destination_url": url, "canonical_url": url,
            "image_url": f"https://{host}/img/calm-barrier-cream.jpg", "price": price,
            "in_stock": True, "validated_at": "2026-11-10T10:00:00Z"}


def _record(*, currency="USD", host="brand-x.example", offers=None):
    return {
        "pdp": {"brand": "Brand X", "product_name": "Calm Barrier Cream",
                "category_path": "beauty/skincare/moisturizer", "currency": currency,
                "source_domain": host},
        "offers": offers or [_offer(host)],
    }


def _planned(records):
    """(catalog row, seed rows) as the drain plans them, the seeds stamped as just re-read."""
    from services.catalog_enrichment_agent.ingestion import ingest_validated_jsonl

    plan = ingest_validated_jsonl(records)
    now = datetime.now(timezone.utc).isoformat()
    seeds = []
    for planned in plan["seeds"]:
        seed = dict(planned)
        seed["seed_data"] = {**json.loads(seed["seed_data"]), "snapshot": {"extracted_at": now}}
        seed.update(destination_checked_at=now, destination_http_status=200, destination_verdict="live",
                    destination_failure_streak=0, utm_template=None)
        seeds.append(seed)
    [pdp] = plan["pdps"]
    return pdp, seeds


def _minted():
    """(catalog row, seed row) as the drain plans them for one brand-store listing."""
    pdp, [seed] = _planned([_record()])
    return pdp, seed


def _invoke(monkeypatch, client, *, flag, pdp, seed, mirror_seed=None, market="US",
            own_seed_ref=None, arm_error_on=None):
    """POST offers.resolve for the row's signature. Returns (body, the statements sent).

    `seed` is one seed row or a list of them: what is attached to the row's `product_key`.
    `own_seed_ref`: the row's `source_ref`, naming a seed that is no longer active (the existing
    keys read it and find nothing). `arm_error_on`: a marker of the arm statement that times out.
    The fake applies each statement's own filters to the rows, so a filter missing from the
    statement shows as a row served.
    """
    import routes.agent_shop_gateway as gateway

    sent = []
    attached = list(seed) if isinstance(seed, list) else [seed]

    async def fake_fetch_all(query, values=None):
        q = str(query)
        sent.append((q, dict(values or {})))
        if arm_error_on and arm_error_on in q:
            raise asyncio.TimeoutError()
        if "FROM catalog_products" in q and "SELECT source_ref, source_product_id" in q:
            # The existing map read: a minted row has no source_ref and a slug source_product_id.
            return [{"source_ref": mirror_seed["id"] if mirror_seed else own_seed_ref,
                     "source_product_id": pdp["source_product_id"]}]
        if "FROM external_product_seeds" in q and "id = ANY(:mapped_seed_ids)" in q:
            return [mirror_seed] if mirror_seed else []
        if "FROM catalog_products" in q and "SELECT product_key" in q:
            assert values["sig_aliases"] == [pdp["pivota_signature_id"]]
            assert "suppressed_at IS NULL" in q and "suppression_reason IS NULL" in q
            if "source_system = :minted_source_system" in q and (
                    values["minted_source_system"] != pdp["source_system"]):
                return []
            return [{"product_key": pdp["product_key"]}]
        if "FROM external_product_seeds" in q and "attached_product_key = ANY(:product_keys)" in q:
            assert "status = 'active'" in q and "price_currency" in q
            assert "catalog_source_quarantine" in q, "the shared quarantine anti-join"
            assert values["product_keys"] == [pdp["product_key"]]
            rows = [s for s in attached if s["price_currency"] == values["serving_currency"]]
            literal = re.search(r"LIMIT\s+(\d+)", q)
            return rows[: int(literal.group(1)) if literal else int(values["limit"])]
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


# ── the arm serves MINTED rows only ───────────────────────────────────────────────────────────

CONNECTED_PID = "7700000000001"


def _stable(body):
    """The answer without its per-call values."""
    volatile = {"latency_ms", "request_id", "trace_id", "generated_at", "resolved_at", "as_of", "elapsed_ms"}

    def scrub(v):
        if isinstance(v, dict):
            return {k: scrub(x) for k, x in v.items() if k not in volatile}
        if isinstance(v, list):
            return [scrub(x) for x in v]
        return v

    return json.dumps(scrub(body), sort_keys=True, default=str)


def _connected_row():
    """A connected merchant's own catalog row: its key and source_product_id are the store's
    product id, it has no source_ref, and its source_system is the store sync's."""
    return {"pivota_signature_id": "sig_" + "c1" * 16, "merchant_id": "merch_shop_a",
            "product_key": f"prod::merch_shop_a::shopify::{CONNECTED_PID}",
            "source_product_id": CONNECTED_PID, "source_system": "products_cache"}


def _mirror_row():
    """A mirror row: its key and source_product_id carry its own seed's external id."""
    return {"pivota_signature_id": "sig_" + "d1" * 16, "merchant_id": "external_seed",
            "product_key": "prod::external_seed::external_seed::ext_" + "ab" * 12,
            "source_product_id": "ext_" + "ab" * 12, "source_system": "external_product_seeds_mirror_v1"}


@pytest.mark.parametrize("row, own_seed_ref", [
    (_connected_row(), None),
    # The mirror row's own seed is no longer active; another store's active seed is on its key.
    (_mirror_row(), "eps_" + "cd" * 12),
], ids=["connected_row", "mirror_row_with_a_dead_own_seed"])
def test_flag_on_a_row_that_is_not_minted_gets_nothing_from_the_arm(monkeypatch, client, row, own_seed_ref):
    """An active seed is attached to the row's key, and the arm still serves none of it: the
    answer with the flag on is the answer with the flag off."""
    _pdp, seed = _minted()
    other_store_seed = {**seed, "attached_product_key": row["product_key"]}
    answers = {}
    for flag in (None, "1"):
        body, sent = _invoke(monkeypatch, client, flag=flag, pdp=row, seed=other_store_seed,
                             own_seed_ref=own_seed_ref)
        assert body["offers"] == [] and _seed_query(body) == ("external_seed_by_fuzzy_ref", 0), flag
        answers[flag] = _stable(body)
        if flag:
            # The row lookup ran and matched nothing; the seed table was never read by the arm.
            [key_statement] = _arm_statements(sent)
            assert "SELECT product_key" in key_statement
    assert answers["1"] == answers[None]

    # The fixture is otherwise servable: the same row and seed, written by the drain, is served.
    body, _sent = _invoke(monkeypatch, client, flag="1", seed=other_store_seed, own_seed_ref=own_seed_ref,
                          pdp={**row, "source_system": _minted()[0]["source_system"]})
    assert _seed_query(body) == (ARM, 1) and len(body["offers"]) == 1


def test_the_arm_binds_the_source_system_the_drain_writes():
    import routes.agent_shop_gateway as gateway

    pdp, _seed = _minted()
    assert gateway.MINTED_SOURCE_SYSTEM == pdp["source_system"]


# ── more than one seed on a minted row ────────────────────────────────────────────────────────

def test_flag_on_serves_every_seller_of_a_minted_row(monkeypatch, client):
    """The drain writes one seed per validated offer: the brand store's and a retailer's."""
    pdp, seeds = _planned([_record(offers=[_offer("brand-x.example"),
                                           _offer("shop-b.example", seller="Shop B", price=19.5)])])
    assert len(seeds) == 2 and {s["attached_product_key"] for s in seeds} == {pdp["product_key"]}
    body, sent = _invoke(monkeypatch, client, flag="1", pdp=pdp, seed=seeds)
    assert _seed_query(body) == (ARM, 2)
    assert {o["source"]["seed_id"] for o in body["offers"]} == {s["id"] for s in seeds}
    [statement] = [q for q, _ in sent if "attached_product_key = ANY(:product_keys)" in q]
    assert re.search(r"ORDER BY updated_at DESC, created_at DESC, id ASC\s+LIMIT :limit", statement)


@pytest.mark.parametrize("market, currency", [("US", "USD"), ("SG", "SGD")])
def test_flag_on_two_currencies_on_one_key_each_go_to_their_own_market(monkeypatch, client, market, currency):
    pdp, seeds = _planned([_record(), _record(currency="SGD", host="shop-c.example",
                                              offers=[_offer("shop-c.example", seller="Shop C", price=28.0)])])
    assert sorted(s["price_currency"] for s in seeds) == ["SGD", "USD"]
    assert {s["attached_product_key"] for s in seeds} == {pdp["product_key"]}
    body, _sent = _invoke(monkeypatch, client, flag="1", pdp=pdp, seed=seeds, market=market)
    [offer] = body["offers"]
    [expected] = [s for s in seeds if s["price_currency"] == currency]
    assert offer["source"]["seed_id"] == expected["id"] and offer["currency"] == currency


# ── the arm's own failure boundary ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("failing", ["SELECT product_key", "attached_product_key = ANY(:product_keys)"],
                         ids=["row_lookup_times_out", "seed_read_times_out"])
def test_flag_on_an_arm_failure_falls_through_to_the_later_lanes(monkeypatch, client, caplog, failing):
    """A timeout in either arm statement costs the request nothing it had with the flag off: the
    same later statements are sent, the answer is the same, and the failure carries the arm's
    name -- in the log and on the seed source -- not the fuzzy lane's."""
    pdp, seed = _minted()
    off_body, off_sent = _invoke(monkeypatch, client, flag=None, pdp=pdp, seed=seed)
    with caplog.at_level(logging.WARNING, logger="routes.agent_shop_gateway"):
        body, sent = _invoke(monkeypatch, client, flag="1", pdp=pdp, seed=seed, arm_error_on=failing)
    arm = _arm_statements(sent)
    assert arm and failing in arm[-1]
    later = [q for q, _ in sent if q not in arm]
    assert later == [q for q, _ in off_sent]
    assert len(later) > 2, "the lanes after the arm did run"

    [src] = [s for s in body["metadata"]["sources"] if s["source"] == "external_product_seeds"]
    assert (src["status"], src["query"], src["row_count"]) == ("ok", "external_seed_by_fuzzy_ref", 0)
    assert src.pop("attached_seed_arm_error") == "TimeoutError"
    assert _stable(body) == _stable(off_body)
    assert [r.getMessage() for r in caplog.records if "attached_seed_arm" in r.getMessage()] == [
        "offers.resolve.attached_seed_arm.failed error_type=TimeoutError"]
