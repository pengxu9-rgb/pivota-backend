from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import routes.agent_pdp_v1 as agent_pdp_v1  # noqa: E402


CK_A = "ck_" + "a" * 32
CK_B = "ck_" + "b" * 32
CK_C = "ck_" + "c" * 32
SIG_A = "sig_" + "1" * 32
SIG_C = "sig_" + "3" * 32
GROUP_ID = "pg_" + "9" * 32


def _row(
    *,
    content_key: str = CK_A,
    pivota_signature_id: Optional[str] = SIG_A,
    product_group_id: str = GROUP_ID,
    title: str = "Watermelon Glow Serum",
) -> Dict[str, Any]:
    return {
        "content_key": content_key,
        "pivota_signature_id": pivota_signature_id,
        "product_group_id": product_group_id,
        "brand": "Glow Recipe",
        "title": title,
        "description": "A brightening serum.",
        "image_url": "https://cdn.example.com/main.jpg",
        "image_urls": [
            "https://cdn.example.com/main.jpg",
            "https://cdn.example.com/alt.jpg",
        ],
        "currency": "USD",
        "price_min": Decimal("34.00"),
        "price_max": Decimal("39.00"),
        "offer_count": 2,
        "offers": [
            {
                "merchant_id": "merch_1",
                "merchant_name": "Primary Store",
                "price": Decimal("34.00"),
                "currency": "USD",
                "availability": "in_stock",
                "url": "https://merchant.example/products/serum",
            }
        ],
        "variants": [
            {
                "variant_id": "sku_1",
                "sku": "SKU-1",
                "title": "30ml",
                "options": {"size": "30ml"},
                "price": Decimal("34.00"),
                "currency": "USD",
                "availability": "in_stock",
            }
        ],
        "variants_count": 1,
        "gtin13": "00123456789012",
        "category_path": "beauty/skincare/serum",
        "taxonomy_tags": ["serum", "brightening"],
        "breadcrumb": [
            {"position": 1, "name": "Products", "item": "/products"},
            {"position": 2, "name": "Serum", "item": "/products?category=serum"},
        ],
        "pdp_lifecycle_stage": "validated",
        "sync_status": "synced",
        "primary_merchant_id": "merch_1",
        "refreshed_at": datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc),
        "refreshed_by_proposal_id": 123,
        "refresh_source": "test",
    }


class FakeAgentPdpDatabase:
    def __init__(
        self,
        rows: List[Dict[str, Any]],
        ext_id_to_content_key: Optional[Dict[str, str]] = None,
        serving_eligible_by_content_key: Optional[Dict[str, bool]] = None,
        index_eligible_by_content_key: Optional[Dict[str, bool]] = None,
        citations_by_content_key: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    ) -> None:
        self.rows = rows
        self.ext_id_to_content_key = ext_id_to_content_key or {}
        self.serving_eligible_by_content_key = serving_eligible_by_content_key
        self.index_eligible_by_content_key = index_eligible_by_content_key or {}
        self.citations_by_content_key = citations_by_content_key or {}
        self.calls: List[Dict[str, Any]] = []

    async def fetch_all(self, query: str, values: Optional[Dict[str, Any]] = None):
        query_text = str(query)
        params = values or {}
        if "citation_observations" in query_text:
            return list(self.citations_by_content_key.get(str(params.get("ck") or ""), []))
        return []

    def _passes_serving_eligibility(self, query: str, row: Dict[str, Any]) -> bool:
        if "index_pipeline_state ips" not in query:
            return True
        if self.serving_eligible_by_content_key is None:
            return True
        serving_ok = (
            self.serving_eligible_by_content_key.get(row["content_key"]) is True
        )
        # ADR-008 SLICE 1: when INDEX_ELIGIBLE_READ is ON the emitted SQL widens
        # to (serving_eligible OR index_eligible). Mirror that here.
        if "ips.index_eligible = TRUE" in query:
            index_ok = self.index_eligible_by_content_key.get(row["content_key"]) is True
            return serving_ok or index_ok
        return serving_ok

    async def fetch_one(self, query: str, values: Optional[Dict[str, Any]] = None):
        query_text = str(query)
        self.calls.append({"query": query_text, "values": dict(values or {})})
        params = values or {}

        # ext_* resolution path (different bind name + JOIN shape)
        if "FROM external_product_seeds eps" in query_text:
            ext_id = str(params.get("ext_id") or "")
            ck = self.ext_id_to_content_key.get(ext_id)
            return {"content_key": ck} if ck else None

        lookup_id = str(params.get("id") or "")

        if "WHERE apv.content_key = :id" in query_text or "WHERE content_key = :id" in query_text:
            row = next((row for row in self.rows if row["content_key"] == lookup_id), None)
            return row if row and self._passes_serving_eligibility(query_text, row) else None

        if "WHERE apv.pivota_signature_id = :id" in query_text or "WHERE pivota_signature_id = :id" in query_text:
            row = next(
                (row for row in self.rows if row.get("pivota_signature_id") == lookup_id),
                None,
            )
            return row if row and self._passes_serving_eligibility(query_text, row) else None

        if "WHERE apv.product_group_id = :id" in query_text or "WHERE product_group_id = :id" in query_text:
            group_rows = [
                row
                for row in self.rows
                if row.get("product_group_id") == lookup_id
                and self._passes_serving_eligibility(query_text, row)
            ]
            group_rows.sort(
                key=lambda row: (
                    0 if row.get("pivota_signature_id") else 1,
                    str(row.get("content_key") or ""),
                )
            )
            return group_rows[0] if group_rows else None

        raise AssertionError(f"unexpected query: {query}")


def _client(
    monkeypatch,
    rows: List[Dict[str, Any]],
    *,
    ext_id_to_content_key: Optional[Dict[str, str]] = None,
    serving_eligible_by_content_key: Optional[Dict[str, bool]] = None,
    index_eligible_by_content_key: Optional[Dict[str, bool]] = None,
    citations_by_content_key: Optional[Dict[str, List[Dict[str, Any]]]] = None,
):
    db = FakeAgentPdpDatabase(
        rows,
        ext_id_to_content_key=ext_id_to_content_key,
        serving_eligible_by_content_key=serving_eligible_by_content_key,
        index_eligible_by_content_key=index_eligible_by_content_key,
        citations_by_content_key=citations_by_content_key,
    )
    monkeypatch.setattr(agent_pdp_v1, "database", db)
    app = FastAPI()
    app.include_router(agent_pdp_v1.router)
    return TestClient(app), db


def _canonical_product(body: Dict[str, Any]) -> Dict[str, Any]:
    modules = body["modules"]
    canonical = next(module for module in modules if module["type"] == "canonical")
    return canonical["data"]["pdp_payload"]["product"]


def _offers_module(body: Dict[str, Any]) -> Dict[str, Any]:
    return next(module for module in body["modules"] if module["type"] == "offers")


def test_get_agent_pdp_by_content_key_returns_modules_envelope(monkeypatch) -> None:
    client, db = _client(monkeypatch, [_row()])

    response = client.get(f"/api/agent/pdp/{CK_A}")

    assert response.status_code == 200
    body = response.json()
    product = _canonical_product(body)
    assert product["content_key"] == CK_A
    assert product["pivota_signature_id"] == SIG_A
    assert product["product_group_id"] == GROUP_ID
    assert product["title"] == "Watermelon Glow Serum"
    assert product["name"] == "Watermelon Glow Serum"
    assert product["brand"] == "Glow Recipe"
    assert product["brand_name"] == "Glow Recipe"
    assert product["product_id"] == SIG_A
    assert product["image_url"] == "https://cdn.example.com/main.jpg"
    assert product["image_urls"][1] == "https://cdn.example.com/alt.jpg"
    assert product["variants"][0]["variant_id"] == "sku_1"
    assert body["subject"] == {"type": "product_group", "id": GROUP_ID}
    assert _offers_module(body)["data"]["offers_count"] == 2
    assert db.calls[0]["values"] == {"id": CK_A}
    assert "INNER JOIN index_pipeline_state ips" in db.calls[0]["query"]
    assert "WHERE apv.content_key = :id" in db.calls[0]["query"]
    assert "ips.serving_eligible = TRUE" in db.calls[0]["query"]


def test_get_agent_pdp_emits_citable_canonical_url(monkeypatch) -> None:
    # An agent grounding on this product needs a URL to attribute; the API must
    # emit the Pivota canonical PDP URL (parity with the crawlable page/JSON-LD).
    client, _ = _client(monkeypatch, [_row()])

    product = _canonical_product(client.get(f"/api/agent/pdp/{CK_A}").json())

    expected = f"https://agent.pivota.cc/products/{SIG_A}"
    assert product["pivota_canonical_url"] == expected
    assert product["url"] == expected


def test_get_agent_pdp_omits_canonical_url_when_no_signature(monkeypatch) -> None:
    # No minted signature → no citable Pivota page → don't fabricate a URL.
    client, _ = _client(
        monkeypatch, [_row(content_key=CK_B, pivota_signature_id=None)]
    )

    product = _canonical_product(client.get(f"/api/agent/pdp/{CK_B}").json())

    assert "pivota_canonical_url" not in product
    assert "url" not in product


def test_get_agent_pdp_by_content_key_returns_when_serving_eligible(monkeypatch) -> None:
    client, db = _client(
        monkeypatch,
        [_row()],
        serving_eligible_by_content_key={CK_A: True},
    )

    response = client.get(f"/api/agent/pdp/{CK_A}")

    assert response.status_code == 200
    assert _canonical_product(response.json())["content_key"] == CK_A
    assert "INNER JOIN index_pipeline_state ips" in db.calls[0]["query"]
    assert "ips.serving_eligible = TRUE" in db.calls[0]["query"]


def test_get_agent_pdp_returns_404_when_serving_state_missing(monkeypatch) -> None:
    client, db = _client(
        monkeypatch,
        [_row()],
        serving_eligible_by_content_key={},
    )

    response = client.get(f"/api/agent/pdp/{CK_A}")

    assert response.status_code == 404
    assert response.json()["detail"] == "PDP not found"
    assert "INNER JOIN index_pipeline_state ips" in db.calls[0]["query"]
    assert "ips.serving_eligible = TRUE" in db.calls[0]["query"]


def test_get_agent_pdp_returns_404_when_serving_eligible_false(monkeypatch) -> None:
    client, db = _client(
        monkeypatch,
        [_row()],
        serving_eligible_by_content_key={CK_A: False},
    )

    response = client.get(f"/api/agent/pdp/{CK_A}")

    assert response.status_code == 404
    assert response.json()["detail"] == "PDP not found"
    assert "INNER JOIN index_pipeline_state ips" in db.calls[0]["query"]
    assert "ips.serving_eligible = TRUE" in db.calls[0]["query"]


def test_get_agent_pdp_bypass_env_reverts_to_legacy_query(monkeypatch, caplog) -> None:
    monkeypatch.setenv("AGENT_PDP_V1_BYPASS_SERVING_ELIGIBILITY", "true")
    client, db = _client(
        monkeypatch,
        [_row()],
        serving_eligible_by_content_key={CK_A: False},
    )

    with caplog.at_level(logging.WARNING, logger=agent_pdp_v1.__name__):
        response = client.get(
            f"/api/agent/pdp/{CK_A}",
            headers={"x-request-id": "req_01", "x-merchant-id": "merch_1"},
        )

    assert response.status_code == 200
    query = db.calls[0]["query"]
    assert "index_pipeline_state" not in query
    assert "WHERE content_key = :id" in query
    warning = next(
        record
        for record in caplog.records
        if "agent_pdp_v1_serving_eligibility_bypass_enabled" in record.message
    )
    payload = json.loads(warning.message)
    assert payload["event"] == "agent_pdp_v1_serving_eligibility_bypass_enabled"
    assert payload["request_id"] == "req_01"
    assert payload["merchant_id"] == "merch_1"
    assert payload["lookup_id"] == CK_A
    assert warning.request_id == "req_01"
    assert warning.merchant_id == "merch_1"
    assert warning.lookup_id == CK_A


def test_get_agent_pdp_by_signature_returns_same_row(monkeypatch) -> None:
    client, db = _client(monkeypatch, [_row()])

    response = client.get(f"/api/agent/pdp/{SIG_A}")

    assert response.status_code == 200
    product = _canonical_product(response.json())
    assert product["content_key"] == CK_A
    assert product["pivota_signature_id"] == SIG_A
    assert db.calls[0]["values"] == {"id": SIG_A}
    assert "WHERE apv.pivota_signature_id = :id" in db.calls[0]["query"]


def test_get_agent_pdp_by_group_id_prefers_signature_bearing_row(monkeypatch) -> None:
    signed = _row(
        content_key=CK_C,
        pivota_signature_id=SIG_C,
        title="Canonical Signed Row",
    )
    unsigned = _row(
        content_key=CK_B,
        pivota_signature_id=None,
        title="Unsigned Row",
    )
    client, db = _client(monkeypatch, [unsigned, signed])

    response = client.get(f"/api/agent/pdp/{GROUP_ID}")

    assert response.status_code == 200
    product = _canonical_product(response.json())
    assert product["content_key"] == CK_C
    assert product["title"] == "Canonical Signed Row"
    query = db.calls[0]["query"]
    assert "WHERE apv.product_group_id = :id" in query
    assert "CASE WHEN apv.pivota_signature_id IS NOT NULL THEN 0 ELSE 1 END" in query
    assert "apv.content_key ASC" in query


def test_get_agent_pdp_by_group_id_uses_lowest_content_key_when_no_signature(monkeypatch) -> None:
    higher = _row(content_key=CK_C, pivota_signature_id=None, title="Higher")
    lower = _row(content_key=CK_B, pivota_signature_id=None, title="Lower")
    client, _db = _client(monkeypatch, [higher, lower])

    response = client.get(f"/api/agent/pdp/{GROUP_ID}")

    assert response.status_code == 200
    product = _canonical_product(response.json())
    assert product["content_key"] == CK_B
    assert product["title"] == "Lower"


def test_get_agent_pdp_unknown_id_returns_404(monkeypatch) -> None:
    client, db = _client(monkeypatch, [])

    response = client.get(f"/api/agent/pdp/{CK_A}")

    assert response.status_code == 404
    assert db.calls[0]["values"] == {"id": CK_A}


def test_get_agent_pdp_malformed_id_returns_404_without_query(monkeypatch) -> None:
    client, db = _client(monkeypatch, [_row()])

    response = client.get("/api/agent/pdp/prod_123")
    malformed_ck_response = client.get("/api/agent/pdp/ck_nothex")

    assert response.status_code == 404
    assert malformed_ck_response.status_code == 404
    assert db.calls == []


def test_get_agent_pdp_strips_pg_colon_prefix_before_group_lookup(monkeypatch) -> None:
    """Gateway emits canonical refs as `pg:<actual_group_id>`. The route
    must strip the `pg:` so the SQL queries product_group_id correctly."""
    row = _row()  # has product_group_id = GROUP_ID
    client, db = _client(monkeypatch, [row])

    response = client.get(f"/api/agent/pdp/pg:{GROUP_ID}")

    assert response.status_code == 200
    # The SQL bind must be the bare GROUP_ID, not "pg:GROUP_ID"
    assert db.calls[0]["values"] == {"id": GROUP_ID}


def test_get_agent_pdp_resolves_ext_id_via_external_product_seeds(monkeypatch) -> None:
    """ext_* ids resolve through external_product_seeds.external_product_id
    → catalog_products.content_key → agent_pdp_view."""
    row = _row()
    client, db = _client(
        monkeypatch,
        [row],
        ext_id_to_content_key={"ext_xyz123": CK_A},
    )

    response = client.get("/api/agent/pdp/ext_xyz123")

    assert response.status_code == 200
    # Two queries: resolve ext → content_key, then SELECT by content_key
    assert len(db.calls) == 2
    assert "external_product_seeds eps" in db.calls[0]["query"]
    assert db.calls[0]["values"] == {"ext_id": "ext_xyz123"}
    assert "WHERE apv.content_key = :id" in db.calls[1]["query"]
    assert db.calls[1]["values"] == {"id": CK_A}


def test_get_agent_pdp_unresolvable_ext_id_returns_404(monkeypatch) -> None:
    """ext_* that doesn't resolve to any content_key returns 404 without
    falling back to other dispatch paths."""
    client, db = _client(monkeypatch, [_row()], ext_id_to_content_key={})

    response = client.get("/api/agent/pdp/ext_missing")

    assert response.status_code == 404
    assert len(db.calls) == 1  # only the resolve attempt


def test_sql_uses_agent_pdp_view_indexed_lookup_paths() -> None:
    sql_by_kind = {
        "content_key": agent_pdp_v1.SELECT_BY_CONTENT_KEY_SQL,
        "signature": agent_pdp_v1.SELECT_BY_SIGNATURE_SQL,
        "product_group": agent_pdp_v1.SELECT_BY_PRODUCT_GROUP_SQL,
    }

    for sql in sql_by_kind.values():
        normalized = " ".join(sql.split())
        assert "SELECT *" not in normalized
        assert "FROM agent_pdp_view apv" in normalized
        assert "INNER JOIN index_pipeline_state ips ON ips.content_key = apv.content_key" in normalized
        assert "ips.serving_eligible = TRUE" in normalized
        # THE FIVE JOINS migration 085 exists to have removed. The old gateway
        # joined catalog_products x catalog_skus x catalog_offers x
        # product_group_members x subject_resolve plus a 3-step fallback chain, at
        # 200-700ms cold; agent_pdp_view is the denormalized answer, targeting
        # <10ms p99. Reintroducing any of them as a JOIN is the regression.
        #
        # catalog_products is now permitted in ONE narrow shape and no other: the
        # correlated existence check behind `pdp_renderable`
        # (services.pdp_renderability.sig_pdp_will_render). That is not the join
        # this pin was written against — measured with
        # EXPLAIN (ANALYZE, BUFFERS) on prod it is **0.181 ms** and ~10 shared
        # buffers, all index lookups, i.e. ~2% of the p99 budget. It is asserted
        # positively below so the exemption cannot silently widen into a real join.
        assert "JOIN catalog_products" not in normalized
        assert "FROM catalog_products apv" not in normalized
        assert "catalog_skus" not in normalized
        assert "catalog_offers" not in normalized
        assert "product_group_members" not in normalized
        assert "subject_resolve" not in normalized
        # The ONLY catalog_products reference allowed: the renderability EXISTS,
        # aliased so it cannot be confused with a join, and keyed on apv's own sig.
        assert "AS pdp_renderable" in normalized
        assert "FROM catalog_products AS _rsig_cp" in normalized
        assert "_rsig_cp.pivota_signature_id = apv.pivota_signature_id" in normalized
        # Exactly one such subquery — not one per lane, not a lateral per row.
        assert normalized.count("FROM catalog_products") == 1

    assert "WHERE apv.content_key = :id" in " ".join(sql_by_kind["content_key"].split())
    assert "WHERE apv.pivota_signature_id = :id" in " ".join(sql_by_kind["signature"].split())
    group_sql = " ".join(sql_by_kind["product_group"].split())
    assert "WHERE apv.product_group_id = :id" in group_sql
    assert "CASE WHEN apv.pivota_signature_id IS NOT NULL THEN 0 ELSE 1 END" in group_sql
    assert "apv.content_key ASC" in group_sql


# ---------------------------------------------------------------------------
# ADR-008 SLICE 1: INDEX_ELIGIBLE_READ read-gate widening
# ---------------------------------------------------------------------------

def test_index_eligible_read_off_is_serving_only_byte_identical(monkeypatch) -> None:
    """Flag OFF (default): an index-eligible-but-not-serving product is 404,
    and the emitted SQL is the serving-only query (no index_eligible clause)."""
    monkeypatch.delenv("INDEX_ELIGIBLE_READ", raising=False)
    client, db = _client(
        monkeypatch,
        [_row()],
        serving_eligible_by_content_key={CK_A: False},
        index_eligible_by_content_key={CK_A: True},
    )

    response = client.get(f"/api/agent/pdp/{CK_A}")

    assert response.status_code == 404
    assert "ips.serving_eligible = TRUE" in db.calls[0]["query"]
    assert "ips.index_eligible" not in db.calls[0]["query"]


def test_index_eligible_read_on_returns_offer_free_index_eligible_product(monkeypatch) -> None:
    """Flag ON: a product that is index_eligible (offer-free) but not
    serving_eligible is now served, and the SQL carries the widened clause."""
    monkeypatch.setenv("INDEX_ELIGIBLE_READ", "true")
    client, db = _client(
        monkeypatch,
        [_row()],
        serving_eligible_by_content_key={CK_A: False},
        index_eligible_by_content_key={CK_A: True},
    )

    response = client.get(f"/api/agent/pdp/{CK_A}")

    assert response.status_code == 200
    assert _canonical_product(response.json())["content_key"] == CK_A
    assert "ips.index_eligible = TRUE" in db.calls[0]["query"]
    assert "ips.serving_eligible = TRUE" in db.calls[0]["query"]


def test_index_eligible_read_on_still_blocks_when_neither_eligible(monkeypatch) -> None:
    monkeypatch.setenv("INDEX_ELIGIBLE_READ", "on")
    client, db = _client(
        monkeypatch,
        [_row()],
        serving_eligible_by_content_key={CK_A: False},
        index_eligible_by_content_key={CK_A: False},
    )

    response = client.get(f"/api/agent/pdp/{CK_A}")

    assert response.status_code == 404
    assert "ips.index_eligible = TRUE" in db.calls[0]["query"]


def test_get_agent_pdp_emits_honest_freshness_block(monkeypatch) -> None:
    # _row() bakes refreshed_at far in the past (2026-05-13), so the served
    # offers/price are well past the 1h price-freshness window -> is_stale.
    client, _ = _client(monkeypatch, [_row()])

    response = client.get(f"/api/agent/pdp/{CK_A}")

    assert response.status_code == 200
    product = _canonical_product(response.json())
    freshness = product["freshness"]
    assert product["is_stale"] is True
    assert freshness["is_stale"] is True
    # observed_at echoes the row's refreshed_at (normalized to naive UTC);
    # fresh_until is observed_at + the 1h TTL.
    assert freshness["observed_at"] == "2026-05-13T12:00:00"
    assert freshness["fresh_until"] == "2026-05-13T13:00:00"
    assert freshness["ttl_seconds"] == 3600


def test_get_agent_pdp_marks_foreign_offer_cross_border(monkeypatch) -> None:
    # A KRW/market=KR brand-direct offer served to the US surface is cross_border
    # (a same-market purchase would be domestic). With no domestic alternative it
    # is still the buy pick — flagged cross_border, not erased.
    row = _row()
    row["offers"] = [{
        "merchant_id": "brand_direct", "merchant_name": "ANUKO",
        "price": 26900.0, "currency": "KRW", "availability": "in_stock",
        "market": "KR", "offer_type": "brand_direct",
    }]
    client, _ = _client(monkeypatch, [row])

    response = client.get(f"/api/agent/pdp/{CK_A}")

    assert response.status_code == 200
    offers = _offers_module(response.json())["data"]["offers"]
    assert offers[0]["market"] == "KR"
    assert offers[0]["market_availability"] == "cross_border"
    assert offers[0]["is_buy_pick"] is True


def test_get_agent_pdp_freshness_stale_when_refreshed_at_missing(monkeypatch) -> None:
    row = _row()
    row["refreshed_at"] = None
    client, _ = _client(monkeypatch, [row])

    response = client.get(f"/api/agent/pdp/{CK_A}")

    assert response.status_code == 200
    product = _canonical_product(response.json())
    assert product["is_stale"] is True
    assert product["freshness"]["observed_at"] is None
    assert product["freshness"]["fresh_until"] is None


def test_get_agent_pdp_serves_independent_signals_separately(monkeypatch) -> None:
    # A credible independent citation is served as independent_signals, DISTINCT
    # from merchant-asserted evidence_claims (SEPARATION invariant); a competitor
    # citation is excluded.
    citations = {
        CK_A: [
            {"cited_host": "allure.com", "host_type": "editorial", "citation_role": "editorial_review",
             "evidence_url": "https://allure.com/x", "provider": "chatgpt",
             "first_party": False, "is_competitor": False, "observed_at": None},
            {"cited_host": "rival.com", "host_type": "brand", "citation_role": "competitor",
             "evidence_url": "https://rival.com/y", "provider": "chatgpt",
             "first_party": False, "is_competitor": True, "observed_at": None},
        ]
    }
    client, _ = _client(monkeypatch, [_row()], citations_by_content_key=citations)

    product = _canonical_product(client.get(f"/api/agent/pdp/{CK_A}").json())
    sigs = product["independent_signals"]
    assert [s["cited_host"] for s in sigs] == ["allure.com"]        # credible only
    assert "independent_signals" != "evidence_claims"               # distinct fields
    assert "allure.com" not in str(product.get("evidence_claims"))  # not merged into merchant evidence


def test_get_agent_pdp_independent_signals_empty_by_default(monkeypatch) -> None:
    client, _ = _client(monkeypatch, [_row()])  # no citations
    product = _canonical_product(client.get(f"/api/agent/pdp/{CK_A}").json())
    assert product["independent_signals"] == []


# ---------------------------------------------------------------------------
# pdp_renderable — is the citable `url` this route emits followable?
# ---------------------------------------------------------------------------
#
# The last read surface that emitted a citable URL with no way to tell whether it
# renders. Measured on the live feed 2026-07-26: 879 of 5,887 rows do NOT render
# and this route answered 200 with a `url` for every one.
#
# Three states, and the third is the point: True (follow it), False (cite the
# content, not the link), None (unknown — no URL, or the emergency bypass served
# the row and that path deliberately does not read index_pipeline_state, which
# gate 1 of the predicate depends on).


def test_pdp_renderable_true_when_both_gates_pass(monkeypatch) -> None:
    client, _db = _client(monkeypatch, [_row() | {"pdp_renderable": True}])
    body = client.get(f"/api/agent/pdp/{CK_A}").json()
    product = _canonical_product(body)
    assert product["pdp_renderable"] is True
    assert product["url"] == f"https://agent.pivota.cc/products/{SIG_A}"


def test_pdp_renderable_false_still_serves_the_row(monkeypatch) -> None:
    """A signal, not a filter. The content stays citable; only the link is flagged."""
    client, _db = _client(monkeypatch, [_row() | {"pdp_renderable": False}])
    res = client.get(f"/api/agent/pdp/{CK_A}")
    assert res.status_code == 200
    product = _canonical_product(res.json())
    assert product["pdp_renderable"] is False
    # URL is still emitted — consumers decide, using the flag.
    assert product["url"] == f"https://agent.pivota.cc/products/{SIG_A}"
    assert product["title"]


def test_pdp_renderable_is_null_when_no_sig_means_no_url(monkeypatch) -> None:
    client, _db = _client(
        monkeypatch, [_row(pivota_signature_id=None) | {"pdp_renderable": True}]
    )
    product = _canonical_product(client.get(f"/api/agent/pdp/{CK_A}").json())
    assert product.get("url") in (None, "")
    # Nothing to characterise ⇒ null, even though the column said True.
    assert product["pdp_renderable"] is None


def test_pdp_renderable_is_null_not_false_on_the_emergency_bypass(
    monkeypatch,
) -> None:
    """The bypass overrides index_pipeline_state; the flag must not pretend.

    The bypass SELECTs omit the column entirely, so the row genuinely lacks the
    key — `.get` returning None is the signal, not a default. Reporting False here
    would assert a check we did not run, against the very gate the operator
    overrode.
    """
    monkeypatch.setenv("AGENT_PDP_V1_BYPASS_SERVING_ELIGIBILITY", "true")
    client, db = _client(
        monkeypatch,
        [_row()],  # note: NO pdp_renderable key, as the bypass SQL produces
        serving_eligible_by_content_key={CK_A: False},
    )
    res = client.get(f"/api/agent/pdp/{CK_A}")
    assert res.status_code == 200
    product = _canonical_product(res.json())
    assert product["pdp_renderable"] is None
    # …and the bypass query really is the one that ran, still free of ips.
    assert "index_pipeline_state" not in db.calls[0]["query"]


def test_bypass_sql_does_not_carry_the_renderability_predicate() -> None:
    """Pins the asymmetry at the SQL level, not just the response.

    Adding it to the bypass variants would reintroduce index_pipeline_state into
    the one path that exists for when that table is the problem.
    """
    for sql in (
        agent_pdp_v1.BYPASS_SELECT_BY_CONTENT_KEY_SQL,
        agent_pdp_v1.BYPASS_SELECT_BY_SIGNATURE_SQL,
        agent_pdp_v1.BYPASS_SELECT_BY_PRODUCT_GROUP_SQL,
    ):
        normalized = " ".join(sql.split())
        assert "pdp_renderable" not in normalized
        assert "index_pipeline_state" not in normalized
        assert "catalog_products" not in normalized


def test_widened_and_group_lanes_also_carry_the_flag() -> None:
    """All THREE gated lanes, both eligibility variants — not just by-content_key."""
    for sql in (
        agent_pdp_v1.SELECT_BY_CONTENT_KEY_SQL,
        agent_pdp_v1.SELECT_BY_SIGNATURE_SQL,
        agent_pdp_v1.SELECT_BY_PRODUCT_GROUP_SQL,
        agent_pdp_v1.INDEX_SELECT_BY_CONTENT_KEY_SQL,
        agent_pdp_v1.INDEX_SELECT_BY_SIGNATURE_SQL,
        agent_pdp_v1.INDEX_SELECT_BY_PRODUCT_GROUP_SQL,
    ):
        assert "AS pdp_renderable" in " ".join(sql.split())
