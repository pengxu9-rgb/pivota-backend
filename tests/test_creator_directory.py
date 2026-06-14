"""Phase 3: data-driven creator directory + discovery-aware matcher.

The matcher/scoring/action/UI already exist; this makes the directory a DB table
any source can populate, and lets the matcher score those rows alongside the JSON.
"""

from __future__ import annotations

import pytest

from services import creator_directory as cd
from services import creator_matcher as cm


class _FakeDB:
    def __init__(self, rows=None):
        self.executes = []
        self._rows = rows or []

    async def execute(self, sql, params):
        self.executes.append(params)

    async def fetch_all(self, sql, params):
        self.last_params = params
        return self._rows


@pytest.mark.asyncio
async def test_upsert_skips_invalid_records_valid():
    db = _FakeDB()
    n = await cd.upsert_creators(
        [
            {"creator_id": "tiktok:val", "category_tags": ["skincare"], "display_name": "Val"},
            {"creator_id": "", "category_tags": ["x"]},          # no id
            {"creator_id": "y", "category_tags": []},            # no category
        ],
        source="bd_curated", db=db,
    )
    assert n == 1
    assert db.executes[0]["cid"] == "tiktok:val"
    assert db.executes[0]["source"] == "bd_curated"


@pytest.mark.asyncio
async def test_load_for_category_returns_matcher_shape():
    db = _FakeDB(rows=[{
        "creator_id": "tiktok:val", "display_name": "Val", "platform": "tiktok",
        "platform_url": "https://tiktok.com/@val", "category_tags": ["skincare"],
        "audience_size_band": "small", "recent_coverage": ["BrandA"],
        "contact_method": "dm", "contact_url": "https://...", "sample_brief_template": "hi",
    }])
    out = await cd.load_creators_for_category("Skincare", db=db)
    assert out[0]["creator_id"] == "tiktok:val"
    assert out[0]["category_tags"] == ["skincare"]
    assert out[0]["recent_coverage"] == ["BrandA"]


@pytest.mark.asyncio
async def test_load_for_category_empty_without_category():
    assert await cd.load_creators_for_category(None, db=_FakeDB()) == []


@pytest.mark.asyncio
async def test_match_with_discovery_scores_directory_creators(monkeypatch):
    cm.reset_database_cache()  # JSON db has only an excluded placeholder → []

    async def fake_load(cat, *, db=None, limit=200):
        return [{
            "creator_id": "tiktok:val", "display_name": "Val", "platform": "tiktok",
            "platform_url": "p", "category_tags": ["skincare"], "audience_size_band": "small",
            "recent_coverage": ["BrandA"], "contact_method": "dm", "contact_url": "u",
            "sample_brief_template": "hi",
        }]

    monkeypatch.setattr("services.creator_directory.load_creators_for_category", fake_load)
    out = await cm.match_creators_with_discovery(
        merchant_category="skincare", competitor_brands=["BrandA"], db=None,
    )
    assert len(out) == 1
    assert out[0]["creator_id"] == "tiktok:val"
    # category 1.0 + competitor overlap 0.5 + small-audience 0.3
    assert out[0]["score"] == pytest.approx(1.8)


@pytest.mark.asyncio
async def test_match_with_discovery_dedupes_json_wins(monkeypatch):
    cm.reset_database_cache()

    # Force the JSON loader to return a curated row, and discovery to return a
    # dupe of the same id — the JSON/curated one must win.
    monkeypatch.setattr(cm, "_load_database", lambda: [{
        "creator_id": "tiktok:val", "display_name": "JSON Val", "platform": "tiktok",
        "category_tags": ["skincare"], "audience_size_band": "small", "recent_coverage": [],
    }])

    async def fake_load(cat, *, db=None, limit=200):
        return [{"creator_id": "tiktok:val", "display_name": "DB Val",
                 "category_tags": ["skincare"], "audience_size_band": "small", "recent_coverage": []}]

    monkeypatch.setattr("services.creator_directory.load_creators_for_category", fake_load)
    out = await cm.match_creators_with_discovery(merchant_category="skincare", db=None)
    assert len(out) == 1
    assert out[0]["display_name"] == "JSON Val"  # curated wins the dedupe
