"""
HTTP-level tests for the canonical PDP resolver
(routes.pivota_canonical_routes).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List

import pytest

os.environ.setdefault(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres"
)

from fastapi import FastAPI
from fastapi.testclient import TestClient


class FakeDb:
    """Minimal in-memory DB stub. Parses the compiled SQL loosely to
    decide which canned response to return — enough for the resolver's
    two queries (single-sig SELECT + paginated list)."""

    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self._rows = rows

    async def fetch_one(self, query):
        try:
            sql = str(query.compile(compile_kwargs={"literal_binds": True}))
        except Exception:
            return None
        import re
        m = re.search(r"pivota_signature_id\s*=\s*'([^']+)'", sql)
        if not m:
            return None
        sig = m.group(1)
        for r in self._rows:
            if r.get("pivota_signature_id") == sig:
                return r
        return None

    async def fetch_all(self, query):
        # Used by the list endpoint; return all rows that have a sig.
        try:
            sql = str(query.compile(compile_kwargs={"literal_binds": True}))
        except Exception:
            return []
        # Crude limit/offset parse.
        import re
        m_lim = re.search(r"LIMIT\s+(\d+)", sql, re.I)
        m_off = re.search(r"OFFSET\s+(\d+)", sql, re.I)
        lim = int(m_lim.group(1)) if m_lim else 200
        off = int(m_off.group(1)) if m_off else 0
        with_sig = [r for r in self._rows if r.get("pivota_signature_id")]
        return with_sig[off : off + lim]

    async def fetch_val(self, query):
        # Just used for COUNT — return total of rows with sig.
        return len([r for r in self._rows if r.get("pivota_signature_id")])


def _row(sig_suffix: str, **overrides) -> Dict[str, Any]:
    base = {
        "product_key": f"prod::merch_a::shopify::{sig_suffix}",
        "merchant_id": "merch_a",
        "platform": "shopify",
        "source_product_id": sig_suffix,
        "title": f"Product {sig_suffix}",
        "brand": "Test Brand",
        "product_type": "face mask",
        "canonical_url": f"https://example.com/p/{sig_suffix}",
        "image_url": f"https://example.com/img/{sig_suffix}.jpg",
        "product_payload": {"id": sig_suffix, "handle": sig_suffix},
        "pivota_signature_id": f"sig_{sig_suffix}",
        "pivota_canonical_url": f"https://agent.pivota.cc/products/sig_{sig_suffix}",
        "updated_at": datetime(2026, 5, 7, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return base


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    from routes import pivota_canonical_routes as pcr

    rows = [
        _row("abc"),
        _row("def", brand="Other Brand"),
        _row("xyz", title="No-image SKU", image_url=None),
        # one row without a sig to confirm the list endpoint filters it out
        {
            **_row("nosig"),
            "pivota_signature_id": None,
            "pivota_canonical_url": None,
        },
    ]
    monkeypatch.setattr(pcr, "database", FakeDb(rows))

    app = FastAPI()
    app.include_router(pcr.router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# /api/canonical/products/{sig_id}
# ---------------------------------------------------------------------------


def test_get_canonical_pdp_returns_product_for_existing_sig(env):
    client = env
    res = client.get("/api/canonical/products/sig_abc")
    assert res.status_code == 200
    body = res.json()
    p = body["product"]
    assert p["title"] == "Product abc"
    assert p["brand"] == "Test Brand"
    assert p["canonical_url"] == "https://agent.pivota.cc/products/sig_abc"
    assert p["merchant_canonical_url"] == "https://example.com/p/abc"
    assert p["platform"] == "shopify"


def test_get_canonical_pdp_404_for_unknown_sig(env):
    client = env
    res = client.get("/api/canonical/products/sig_doesnotexist")
    assert res.status_code == 404
    assert res.json()["detail"]["sig_id"] == "sig_doesnotexist"


def test_get_canonical_pdp_400_for_malformed_sig(env):
    client = env
    res = client.get("/api/canonical/products/not_a_sig")
    assert res.status_code == 400
    assert "sig_" in res.json()["detail"]


def test_get_canonical_pdp_handles_missing_image_gracefully(env):
    client = env
    res = client.get("/api/canonical/products/sig_xyz")
    assert res.status_code == 200
    p = res.json()["product"]
    assert p["title"] == "No-image SKU"
    assert p["image_url"] is None
    assert p["main_image_url"] is None


# ---------------------------------------------------------------------------
# /api/canonical/products (list)
# ---------------------------------------------------------------------------


def test_list_canonical_pdps_returns_only_rows_with_sig(env):
    client = env
    res = client.get("/api/canonical/products")
    assert res.status_code == 200
    body = res.json()
    # 3 rows have sigs; 1 doesn't
    assert body["total"] == 3
    assert len(body["items"]) == 3
    sigs = [item["sig_id"] for item in body["items"]]
    assert all(s.startswith("sig_") for s in sigs)
    assert "sig_nosig" not in sigs


def test_list_canonical_pdps_pagination_bounds(env):
    client = env
    res = client.get("/api/canonical/products?limit=2&offset=0")
    body = res.json()
    assert body["limit"] == 2
    assert body["offset"] == 0
    assert len(body["items"]) == 2

    res2 = client.get("/api/canonical/products?limit=2&offset=2")
    body2 = res2.json()
    assert len(body2["items"]) == 1


def test_list_canonical_pdps_rejects_oversized_limit(env):
    """Cap at 1000 to keep response sizes sane."""
    client = env
    res = client.get("/api/canonical/products?limit=10000")
    assert res.status_code == 422
