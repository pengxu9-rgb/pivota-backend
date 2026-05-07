"""
Tests for the canonical PDP quality gate (db.canonical_pdp_filter +
its application in routes.pivota_canonical_routes).
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

from db.canonical_pdp_filter import (
    MIN_DESCRIPTION_LENGTH,
    visible_canonical_clause,
)


# ---------------------------------------------------------------------------
# 1. SQL-shape tests — guard against the WHERE clause silently losing
#    predicates (e.g., refactor drops the description length check).
# ---------------------------------------------------------------------------


def _compiled_sql() -> str:
    from sqlalchemy import select
    from db.catalog import catalog_products

    stmt = select(catalog_products.c.product_key).where(
        visible_canonical_clause()
    )
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


def test_clause_requires_non_null_signature():
    sql = _compiled_sql()
    assert "pivota_signature_id IS NOT NULL" in sql


def test_clause_requires_non_empty_image():
    sql = _compiled_sql()
    assert "image_url IS NOT NULL" in sql
    # The length>0 guard catches whitespace-only / empty-string cases
    assert "length(coalesce(catalog_products.image_url" in sql.lower()


def test_clause_requires_minimum_description_length():
    sql = _compiled_sql()
    assert f">= {MIN_DESCRIPTION_LENGTH}" in sql
    assert "description" in sql.lower()


# ---------------------------------------------------------------------------
# 2. Behavioural tests via a gate-aware FakeDb. Mirrors the SQL logic
#    in Python so we can exercise the resolver routes end-to-end without
#    a real Postgres.
# ---------------------------------------------------------------------------


class GateAwareFakeDb:
    """In-memory stub that applies the same quality-gate logic the real
    SQL clause does. Lets us assert behaviour through the route layer."""

    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self._rows = rows

    @staticmethod
    def _passes(r: Dict[str, Any]) -> bool:
        if not r.get("pivota_signature_id"):
            return False
        img = (r.get("image_url") or "").strip()
        if not img:
            return False
        desc = r.get("description") or ""
        if len(desc) < MIN_DESCRIPTION_LENGTH:
            return False
        return True

    def _visible(self) -> List[Dict[str, Any]]:
        return [r for r in self._rows if self._passes(r)]

    async def fetch_one(self, query):
        # Single-sig lookup: parse the literal sig from the SQL.
        import re

        sql = str(query.compile(compile_kwargs={"literal_binds": True}))
        m = re.search(r"pivota_signature_id\s*=\s*'([^']+)'", sql)
        if not m:
            return None
        sig = m.group(1)
        for r in self._visible():
            if r.get("pivota_signature_id") == sig:
                return r
        return None

    async def fetch_all(self, query):
        import re

        sql = str(query.compile(compile_kwargs={"literal_binds": True}))
        m_lim = re.search(r"LIMIT\s+(\d+)", sql, re.I)
        m_off = re.search(r"OFFSET\s+(\d+)", sql, re.I)
        lim = int(m_lim.group(1)) if m_lim else 200
        off = int(m_off.group(1)) if m_off else 0
        return self._visible()[off : off + lim]

    async def fetch_val(self, query):
        return len(self._visible())


# Helper to build rows that pass the gate by default; override fields
# to simulate failures.
def _good_row(suffix: str, **overrides) -> Dict[str, Any]:
    base = {
        "product_key": f"prod::merch_a::shopify::{suffix}",
        "merchant_id": "merch_a",
        "platform": "shopify",
        "source_product_id": suffix,
        "title": f"Product {suffix}",
        "description": "x" * 80,  # well above MIN_DESCRIPTION_LENGTH
        "brand": "Test Brand",
        "product_type": "face mask",
        "canonical_url": f"https://example.com/p/{suffix}",
        "image_url": f"https://example.com/img/{suffix}.jpg",
        "product_payload": {"id": suffix, "handle": suffix},
        "pivota_signature_id": f"sig_{suffix}",
        "pivota_canonical_url": f"https://agent.pivota.cc/products/sig_{suffix}",
        "created_at": datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 5, 7, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return base


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    from routes import pivota_canonical_routes as pcr

    rows = [
        _good_row("good"),
        _good_row("noimg", image_url=None),
        _good_row("emptyimg", image_url="   "),
        _good_row("shortdesc", description="too short"),
        # external_seed must NOT be filtered by platform — gate judges
        # on content like any other row. This one has full content.
        _good_row("seed_ok", platform="external_seed"),
        # external_seed without content → gated out (on content, not
        # platform).
        _good_row(
            "seed_empty",
            platform="external_seed",
            image_url=None,
            description="",
        ),
    ]
    monkeypatch.setattr(pcr, "database", GateAwareFakeDb(rows))

    app = FastAPI()
    app.include_router(pcr.router)
    return TestClient(app)


# ---- single-sig endpoint behaviour ----


def test_get_returns_200_for_passing_row(env):
    res = env.get("/api/canonical/products/sig_good")
    assert res.status_code == 200
    assert res.json()["product"]["title"] == "Product good"


def test_get_returns_404_for_row_missing_image(env):
    res = env.get("/api/canonical/products/sig_noimg")
    assert res.status_code == 404


def test_get_returns_404_for_row_with_whitespace_only_image(env):
    res = env.get("/api/canonical/products/sig_emptyimg")
    assert res.status_code == 404


def test_get_returns_404_for_row_with_short_description(env):
    res = env.get("/api/canonical/products/sig_shortdesc")
    assert res.status_code == 404


def test_get_returns_200_for_external_seed_with_full_content(env):
    """external_seed is bootstrap content — gate on body, not platform."""
    res = env.get("/api/canonical/products/sig_seed_ok")
    assert res.status_code == 200


def test_get_returns_404_for_external_seed_with_empty_content(env):
    """Seed rows still have to clear the same content bar."""
    res = env.get("/api/canonical/products/sig_seed_empty")
    assert res.status_code == 404


# ---- list endpoint behaviour ----


def test_list_excludes_all_failing_rows(env):
    res = env.get("/api/canonical/products?limit=100")
    assert res.status_code == 200
    body = res.json()
    sigs = {item["sig_id"] for item in body["items"]}
    # 2 should pass: good + seed_ok
    assert sigs == {"sig_good", "sig_seed_ok"}
    assert body["total"] == 2


def test_list_total_reflects_gate(env):
    res = env.get("/api/canonical/products?limit=1")
    body = res.json()
    # total counts visible rows, not just the current page
    assert body["total"] == 2
    assert len(body["items"]) == 1
