"""make_external_seed_servable must write the quality snapshot under the
product's REAL merchant (ADR-009 per-brand merch_obs_ seller), not the legacy
"external_seed" merchant — otherwise the serving classifier can't find the
snapshot and serving_eligible/index_eligible never flip."""
from __future__ import annotations

import services.external_seed_servability as mod


class _FakeDB:
    def __init__(self, row):
        self._row = row

    async def fetch_one(self, sql, params=None):
        return self._row


async def _noop(*a, **k):
    return None


def _patch_common(monkeypatch):
    monkeypatch.setattr(mod, "backlink_seed_to_product", _noop)
    monkeypatch.setattr(mod, "refresh_agent_pdp_view_for_seed", _noop)

    async def _recompute(content_key, reason=None):
        return True

    monkeypatch.setattr(mod, "recompute_serving_eligibility", _recompute)


async def test_snapshot_written_under_real_merch_obs_merchant(monkeypatch):
    _patch_common(monkeypatch)
    captured = {}

    async def _fqe(*, merchant_id, platform, platform_product_id, geo_code, payload, score_source_backed_components):
        captured["merchant_id"] = merchant_id
        captured["platform_product_id"] = platform_product_id

    monkeypatch.setattr(mod, "full_quality_eval", _fqe)
    db = _FakeDB({"merchant_id": "merch_obs_abc123", "content_key": "ck_x"})

    summary = await mod.make_external_seed_servable(
        product_key="prod::merch_obs_abc123::external_seed::goongbe_us_1",
        seed_id="external_brand_crawl::goongbe_us_1",
        source_product_id="goongbe_us_1",
        quality_payload={},
        db=db,
    )

    assert captured["merchant_id"] == "merch_obs_abc123"
    assert captured["merchant_id"] != mod.EXTERNAL_SEED_MERCHANT_ID
    assert captured["platform_product_id"] == "goongbe_us_1"
    assert summary["quality"] is True
    assert summary["serving_eligible"] is True


async def test_falls_back_to_legacy_merchant_when_catalog_row_missing(monkeypatch):
    _patch_common(monkeypatch)
    captured = {}

    async def _fqe(*, merchant_id, **k):
        captured["merchant_id"] = merchant_id

    monkeypatch.setattr(mod, "full_quality_eval", _fqe)
    db = _FakeDB(None)  # no catalog row → legacy fallback

    await mod.make_external_seed_servable(
        product_key="prod::external_seed::external_seed::x",
        seed_id="s",
        source_product_id="x",
        quality_payload={},
        db=db,
    )

    assert captured["merchant_id"] == mod.EXTERNAL_SEED_MERCHANT_ID


async def test_blank_merchant_falls_back_to_legacy(monkeypatch):
    _patch_common(monkeypatch)
    captured = {}

    async def _fqe(*, merchant_id, **k):
        captured["merchant_id"] = merchant_id

    monkeypatch.setattr(mod, "full_quality_eval", _fqe)
    db = _FakeDB({"merchant_id": "  ", "content_key": "ck_x"})

    await mod.make_external_seed_servable(
        product_key="prod::x", seed_id="s", source_product_id="x", quality_payload={}, db=db,
    )

    assert captured["merchant_id"] == mod.EXTERNAL_SEED_MERCHANT_ID
