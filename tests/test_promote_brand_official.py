"""Tests for the brand-official canonical promotion lane (draft → serving).

Covers the glue this lane adds on top of the existing machinery: payload
shaping (INCI attach), per-row/per-key failure isolation, and the
quality → IPS → trust call order. The heavy lifting (scorer, classifier,
trust policy) is the production services' own code and stays real where it
is pure (preview scoring); I/O boundaries are faked.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./pivota_test.db")

import scripts.promote_brand_official_canonicals as promote  # noqa: E402


def _row(pk: str, ck: str, **over: Any) -> Dict[str, Any]:
    row = {
        "product_key": pk, "content_key": ck, "source_product_id": f"sid-{pk}",
        "merchant_id": "external_seed", "platform": "external_seed",
        "title": "Advanced Snail 96 Mucin Power Essence", "brand": "COSRX",
        "description": "A lightweight essence with 96% snail secretion filtrate "
                       "to repair and soothe stressed skin over time.",
        "product_type": "Essence", "category": "skincare",
        "image_url": "https://cosrx.com/i.jpg", "pdp_lifecycle_stage": "published",
        "price": 17.50, "raw_inci": None,
    }
    row.update(over)
    return row


def test_payload_attaches_inci_when_present():
    payload = promote._payload_for(_row("pk1", "ck1", raw_inci="Aqua, Snail Secretion Filtrate"))
    assert payload["seed_data"]["inci_list"] == ["Aqua", "Snail Secretion Filtrate"]
    no_inci = promote._payload_for(_row("pk1", "ck1"))
    assert "seed_data" not in no_inci


def test_payload_scores_above_threshold_with_real_description():
    from services.product_quality_service import preview_quality

    q = preview_quality(promote._payload_for(_row("pk1", "ck1")),
                        score_source_backed_components=True)
    assert q["content_quality_score"] >= 65


@pytest.mark.asyncio
async def test_apply_isolates_failures_and_orders_stages(monkeypatch, capsys):
    rows = [_row("pk1", "ck1"), _row("pk2", "ck2"), _row("pk3", "ck2")]  # ck2 shared
    calls: List[Any] = []

    class FakeDB:
        async def connect(self): pass
        async def disconnect(self): pass
        async def fetch_all(self, sql: str, values: Any = None):
            if "agent_pdp_view" in sql:
                return []  # apv-heal population (step 0): nothing stale
            if "FROM catalog_products" in sql:
                if values and values.get("after"):
                    return []  # keyset pagination: single page
                return rows
            return []  # censuses

    async def fake_refresh(ck: str, *, refresh_source: str):
        calls.append(("apv", ck))

    async def fake_eval(**kw: Any):
        calls.append(("quality", kw["platform_product_id"]))
        if kw["platform_product_id"] == "sid-pk2":
            raise RuntimeError("scorer blew up")
        return {}

    async def fake_recompute(ck: str, *, reason: str):
        calls.append(("ips", ck))
        if ck == "ck1":
            raise RuntimeError("recompute blew up")
        return True

    async def fake_trust(*, db: Any, product_keys: Any):
        calls.append(("trust", tuple(product_keys)))
        return len(list(product_keys))

    monkeypatch.setattr(promote, "database", FakeDB())
    monkeypatch.setattr(promote, "refresh_agent_pdp_view_for_content_key", fake_refresh)
    monkeypatch.setattr(promote, "full_quality_eval", fake_eval)
    monkeypatch.setattr(promote, "recompute_serving_eligibility", fake_recompute)
    monkeypatch.setattr(promote, "upsert_catalog_row_trust_many", fake_trust)

    rc = await promote.run(True, 0)
    assert rc == 0
    # pk2's scorer failure and ck1's recompute failure must not stop the batch
    assert [c for c in calls if c[0] == "quality"] == [
        ("quality", "sid-pk1"), ("quality", "sid-pk2"), ("quality", "sid-pk3")]
    assert [c for c in calls if c[0] == "ips"] == [("ips", "ck1"), ("ips", "ck2")]
    assert [c for c in calls if c[0] == "trust"] == [("trust", ("pk1", "pk2", "pk3"))]
    # every quality call happens before the first ips call (classifier reads snapshots)
    first_ips = calls.index(("ips", "ck1"))
    assert all(calls.index(q) < first_ips for q in calls if q[0] == "quality")
    out = capsys.readouterr().out
    assert "serving_eligible=1" in out  # ck2 True, ck1 raised


@pytest.mark.asyncio
async def test_backfill_provenance_guard_skips_foreign_domains(monkeypatch, capsys):
    """A row whose canonical_url host != source_domain (retailer-URL Gemini rows,
    NULL-source_domain rows) must never be filled — retailer copy must not
    become a brand-official description."""
    import scripts.backfill_brand_official_descriptions as bf

    rows = [
        {"product_key": "pk_own", "content_key": "ck1", "source_domain": "cosrx.com",
         "canonical_url": "https://cosrx.com/products/snail-essence",
         "title": "T", "image_url": "i", "description": "", "category_path": "b/s",
         "tags": "[]", "demographic": None, "use_case_tags": "[]", "lifestyle_tags": "[]",
         "pdp_scope": "merchant_owned", "source_system": "catalog_enrichment_agent_v1",
         "pdp_lifecycle_stage": "draft"},
        {"product_key": "pk_retail", "content_key": "ck2", "source_domain": "cosrx.com",
         "canonical_url": "https://sokoglam.com/products/cosrx-snail-essence",
         "title": "T", "image_url": "i", "description": "", "category_path": "b/s",
         "tags": "[]", "demographic": None, "use_case_tags": "[]", "lifestyle_tags": "[]",
         "pdp_scope": "merchant_owned", "source_system": "catalog_enrichment_agent_v1",
         "pdp_lifecycle_stage": "draft"},
        {"product_key": "pk_nullsrc", "content_key": "ck3", "source_domain": None,
         "canonical_url": "https://cosrx.com/products/other",
         "title": "T", "image_url": "i", "description": "", "category_path": "b/s",
         "tags": "[]", "demographic": None, "use_case_tags": "[]", "lifestyle_tags": "[]",
         "pdp_scope": "merchant_owned", "source_system": "catalog_enrichment_agent_v1",
         "pdp_lifecycle_stage": "draft"},
    ]
    fetched_domains: List[str] = []

    class BfFakeDB:
        async def connect(self): pass
        async def disconnect(self): pass
        async def fetch_all(self, sql: str, values: Any = None): return rows
        async def execute(self, sql: str, values: Any = None): pass

    async def fake_fetch(domain: str, *, max_products: int = 800):
        fetched_domains.append(domain)
        return [{"handle": "snail-essence",
                 "body_html": "<p>" + "brand copy words " * 10 + "</p>"}]

    monkeypatch.setattr(bf, "database", BfFakeDB())
    monkeypatch.setattr(bf, "fetch_shopify_products", fake_fetch)

    rc = await bf.run(False, [], 800)
    assert rc == 0
    out = capsys.readouterr().out
    # only the own-domain row survives the guard; the other two are skipped
    assert "[provenance] 2 row(s) skipped" in out
    assert fetched_domains == ["cosrx.com"]
    assert "fill=   1" in out
