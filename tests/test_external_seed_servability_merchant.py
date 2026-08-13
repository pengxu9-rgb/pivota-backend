"""make_external_seed_servable must write the quality snapshot under the
product's REAL merchant (ADR-009 per-brand merch_obs_ seller), not the legacy
"external_seed" merchant — otherwise the serving classifier can't find the
snapshot and serving_eligible/index_eligible never flip."""
from __future__ import annotations

import re

import services.external_seed_servability as mod


class _FakeDB:
    """Returns only the columns the statement actually SELECTs.

    A fake that hands back its whole canned row regardless of the query cannot
    tell "the code reads this column" from "the code forgot to ask for it".
    Measured: with a projection-blind fake, deleting `platform` from the SELECT
    list passed every test here while production would fall back to
    EXTERNAL_SEED_PLATFORM and silently reinstate the blind spot.
    """

    def __init__(self, row):
        self._row = row

    @staticmethod
    def _selected(sql):
        match = re.search(r"SELECT\s+(.*?)\s+FROM", " ".join(sql.split()), re.IGNORECASE)
        if not match:
            return None
        return [c.strip() for c in match.group(1).split(",")]

    async def fetch_one(self, sql, params=None):
        if self._row is None:
            return None
        cols = self._selected(sql)
        if cols is None or "*" in cols:
            return dict(self._row)
        return {c: self._row.get(c) for c in cols}


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


# ---------------------------------------------------------------------------
# platform — the same argument as merchant, left hardcoded when merchant was fixed
# ---------------------------------------------------------------------------
# _ELIGIBILITY_LATERAL_JOINS matches the snapshot on `platform = cp.platform`
# exactly as it matches on merchant. A product whose catalog row says
# `url_audit` had its snapshot written under `external_seed`, so the classifier
# could never find it and its score froze at whatever an older path last wrote.
# Measured 2026-08-11: HBN Double Retinol and both Anuko rows sit on `v1-lite`
# scores while the gate compares against the v3 71.4 bar.


async def test_snapshot_written_under_real_platform_not_external_seed(monkeypatch):
    _patch_common(monkeypatch)
    captured = {}

    async def _fqe(*, merchant_id, platform, platform_product_id, geo_code, payload, score_source_backed_components):
        captured["platform"] = platform
        captured["merchant_id"] = merchant_id

    monkeypatch.setattr(mod, "full_quality_eval", _fqe)
    db = _FakeDB({"merchant_id": "merch_obs_abc123", "platform": "url_audit",
                  "content_key": "ck_x"})

    await mod.make_external_seed_servable(
        product_key="prod::merch_obs_abc123::url_audit::hbn_1",
        seed_id="external_brand_crawl::hbn_1",
        source_product_id="hbn_1",
        quality_payload={},
        db=db,
    )

    assert captured["platform"] == "url_audit"
    assert captured["platform"] != mod.EXTERNAL_SEED_PLATFORM, (
        "the snapshot went under external_seed again — the classifier matches on "
        "the product's own platform and will never find it"
    )
    # The merchant half must keep working; these two are resolved together.
    assert captured["merchant_id"] == "merch_obs_abc123"


async def test_platform_falls_back_to_external_seed_when_catalog_row_missing(monkeypatch):
    _patch_common(monkeypatch)
    captured = {}

    async def _fqe(*, merchant_id, platform, platform_product_id, geo_code, payload, score_source_backed_components):
        captured["platform"] = platform

    monkeypatch.setattr(mod, "full_quality_eval", _fqe)
    db = _FakeDB(None)  # no catalog row

    await mod.make_external_seed_servable(
        product_key="prod::x::external_seed::y", seed_id="s", source_product_id="y",
        quality_payload={}, db=db,
    )
    assert captured["platform"] == mod.EXTERNAL_SEED_PLATFORM


async def test_platform_falls_back_when_catalog_platform_is_blank(monkeypatch):
    """Blank must not become the snapshot's platform — an empty-string platform
    is as unfindable as the wrong one, and `or` alone would let '' through if
    the strip() were dropped."""
    _patch_common(monkeypatch)
    captured = {}

    async def _fqe(*, merchant_id, platform, platform_product_id, geo_code, payload, score_source_backed_components):
        captured["platform"] = platform

    monkeypatch.setattr(mod, "full_quality_eval", _fqe)
    db = _FakeDB({"merchant_id": "m", "platform": "   ", "content_key": "ck"})

    await mod.make_external_seed_servable(
        product_key="p", seed_id="s", source_product_id="y", quality_payload={}, db=db,
    )
    assert captured["platform"] == mod.EXTERNAL_SEED_PLATFORM


async def test_an_external_seed_product_is_unchanged(monkeypatch):
    """The common case must not move: platform='external_seed' in the catalog
    still writes under external_seed, so this fix cannot disturb the rows that
    already work."""
    _patch_common(monkeypatch)
    captured = {}

    async def _fqe(*, merchant_id, platform, platform_product_id, geo_code, payload, score_source_backed_components):
        captured["platform"] = platform

    monkeypatch.setattr(mod, "full_quality_eval", _fqe)
    db = _FakeDB({"merchant_id": "merch_obs_x", "platform": "external_seed",
                  "content_key": "ck"})

    await mod.make_external_seed_servable(
        product_key="p", seed_id="s", source_product_id="y", quality_payload={}, db=db,
    )
    assert captured["platform"] == "external_seed"
