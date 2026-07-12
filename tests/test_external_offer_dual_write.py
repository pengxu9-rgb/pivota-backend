"""Unit tests for the P1.6 external seed → catalog_offers projection.

Pure-logic + mocked-database tests (no real SQL execution). Post-review
(convergence v2): the offer must attach under the seed's REAL per-brand
observed seller (resolved by provenance, catalog_products.source_ref = seed id),
NOT the legacy 'external_seed' sentinel — the earlier draft keyed everything
under the sentinel and was a silent no-op against real (ADR-009 D2) data.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import external_offer_dual_write as mod  # noqa: E402


class FakeDB:
    """Mocks the two reads sync_offer_for_seed does: the seed fetch and the
    provenance mirror-product lookup (both fetch_one), plus execute capture."""

    def __init__(self, *, seed=None, mirror=None):
        self._seed = seed
        self._mirror = mirror  # {"product_key":..., "merchant_id":...} or None
        self.executed = []

    async def fetch_one(self, sql, params=None):
        s = str(sql)
        if "FROM external_product_seeds" in s and "WHERE id" in s:
            return dict(self._seed) if self._seed else None
        if "FROM catalog_products" in s and "source_ref" in s:
            return dict(self._mirror) if self._mirror else None
        return None

    async def execute(self, sql, params=None):
        self.executed.append({"sql": str(sql), "params": dict(params or {})})


_REAL_PK = "prod::merch_obs_deadbeef::external_seed::ext1"  # per-brand seller key
_REAL_SELLER = "merch_obs_deadbeef"


def test_derive_sku_key():
    assert mod.derive_mirror_sku_key(_REAL_PK) == f"{_REAL_PK}::canonical"


def test_derive_offer_id_deterministic_and_prefixed():
    a = mod.derive_mirror_offer_id(_REAL_PK)
    assert a == mod.derive_mirror_offer_id(_REAL_PK)
    assert a.startswith(mod.OFFER_ID_PREFIX)
    assert mod.derive_mirror_offer_id("other") != a


@pytest.mark.asyncio
async def test_dark_by_default(monkeypatch):
    """Genuine dark launch: with the flag UNSET, the dual-write is a no-op."""
    monkeypatch.delenv("EXTERNAL_OFFER_DUAL_WRITE_ENABLED", raising=False)
    assert mod.dual_write_enabled() is False
    fake = FakeDB(seed={"id": "s1"}, mirror={"product_key": _REAL_PK, "merchant_id": _REAL_SELLER})
    monkeypatch.setattr(mod, "database", fake)
    result = await mod.sync_offer_for_seed("s1")
    assert result["status"] == "disabled"
    assert fake.executed == []


@pytest.mark.asyncio
async def test_sync_seed_missing(monkeypatch):
    monkeypatch.setenv("EXTERNAL_OFFER_DUAL_WRITE_ENABLED", "1")
    fake = FakeDB(seed=None)
    monkeypatch.setattr(mod, "database", fake)
    assert (await mod.sync_offer_for_seed("gone"))["status"] == "seed_missing"


@pytest.mark.asyncio
async def test_sync_no_mirror_product_yet(monkeypatch):
    """No mirror product resolved by provenance → skip, write nothing."""
    monkeypatch.setenv("EXTERNAL_OFFER_DUAL_WRITE_ENABLED", "1")
    fake = FakeDB(seed={"id": "s1", "external_product_id": "ext1"}, mirror=None)
    monkeypatch.setattr(mod, "database", fake)
    result = await mod.sync_offer_for_seed("s1")
    assert result["status"] == "no_mirror_product"
    assert fake.executed == []


@pytest.mark.asyncio
async def test_sync_projects_offer_under_real_seller(monkeypatch):
    """The offer attaches under the mirror's REAL per-brand seller + product_key
    (resolved by provenance), never the 'external_seed' sentinel."""
    monkeypatch.setenv("EXTERNAL_OFFER_DUAL_WRITE_ENABLED", "1")
    seed = {
        "id": "s1", "external_product_id": "ext1",
        "price_amount": 19.99, "price_currency": "USD", "availability": "in_stock",
        "destination_url": "https://x.test/p", "canonical_url": "https://x.test/p",
        "domain": "x.test", "market": "US",
    }
    fake = FakeDB(seed=seed, mirror={"product_key": _REAL_PK, "merchant_id": _REAL_SELLER})
    monkeypatch.setattr(mod, "database", fake)

    result = await mod.sync_offer_for_seed("s1")
    assert result["status"] == "synced"
    assert result["product_key"] == _REAL_PK
    assert len(fake.executed) == 1
    params = fake.executed[0]["params"]
    assert "INSERT INTO catalog_offers" in fake.executed[0]["sql"]
    assert params["product_key"] == _REAL_PK
    assert params["merchant_id"] == _REAL_SELLER  # NOT "external_seed"
    assert params["merchant_id"] != "external_seed"
    assert params["list_price"] == 19.99
    assert params["catalog_track"] == "external_referral"
    assert params["truth_tier"] == "observed"
    assert params["readiness_tier"] == "referral_only"
    assert params["source_ref"] == "s1"


@pytest.mark.asyncio
async def test_offer_id_agrees_with_mirror_on_the_real_product_key(monkeypatch):
    """S2 regression: the dual-write's offer_id/sku_key are derived from the
    SAME product_key the mirror wrote (resolved via provenance), so a re-project
    refreshes the mirror's row (ON CONFLICT) rather than forking a divergent one.
    This is the invariant the sentinel-keyed draft violated."""
    monkeypatch.setenv("EXTERNAL_OFFER_DUAL_WRITE_ENABLED", "1")
    fake = FakeDB(
        seed={"id": "s1", "external_product_id": "ext1", "price_amount": 5.0},
        mirror={"product_key": _REAL_PK, "merchant_id": _REAL_SELLER},
    )
    monkeypatch.setattr(mod, "database", fake)
    await mod.sync_offer_for_seed("s1")
    params = fake.executed[0]["params"]
    # offer_id + sku_key are the deterministic functions of the mirror's key
    assert params["offer_id"] == mod.derive_mirror_offer_id(_REAL_PK)
    assert params["sku_key"] == mod.derive_mirror_sku_key(_REAL_PK)


@pytest.mark.asyncio
async def test_self_seed_projects_brand_direct_first_party(monkeypatch):
    """A seed_kind='self' crawl seed is the brand selling its OWN product (D2C),
    so its projected offer is brand_direct / first-party. This closes the gap where
    mirror-only paths left brand-owned offers at offer_type=NULL / is_first_party
    =false (the 2026-07-12 backfill of 344 Paul Mitchell / FORBEAUT / etc. offers)."""
    fake = FakeDB()
    monkeypatch.setattr(mod, "database", fake)
    await mod.upsert_catalog_offer_from_seed_row(
        _REAL_PK,
        {"id": "s1", "external_product_id": "ext1", "seed_kind": "Self", "price_amount": 12.0},
        merchant_id=_REAL_SELLER,
    )
    params = fake.executed[0]["params"]
    assert params["offer_type"] == "brand_direct"
    assert params["is_first_party"] is True
    # ON CONFLICT is non-clobbering: only fills a NULL offer_type, is_first_party sticks.
    sql = fake.executed[0]["sql"]
    assert "offer_type = COALESCE(catalog_offers.offer_type, EXCLUDED.offer_type)" in sql
    assert "is_first_party = catalog_offers.is_first_party OR EXCLUDED.is_first_party" in sql


@pytest.mark.asyncio
@pytest.mark.parametrize("seed_kind", [None, "", "retailer", "reseller", "marketplace"])
async def test_non_self_seed_leaves_offer_untyped(monkeypatch, seed_kind):
    """A non-self seed (retailer/reseller/unknown) must NOT be auto-marked
    brand_direct — it stays offer_type=NULL / is_first_party=false (the prior
    default), so onboard or a human types it deliberately."""
    fake = FakeDB()
    monkeypatch.setattr(mod, "database", fake)
    row = {"id": "s1", "external_product_id": "ext1", "price_amount": 12.0}
    if seed_kind is not None:
        row["seed_kind"] = seed_kind
    await mod.upsert_catalog_offer_from_seed_row(_REAL_PK, row, merchant_id=_REAL_SELLER)
    params = fake.executed[0]["params"]
    assert params["offer_type"] is None
    assert params["is_first_party"] is False


@pytest.mark.asyncio
async def test_upsert_refuses_banned_bucket():
    """ADR-009 D2: an offer may never be written under the 'external_seed'
    sentinel — the upsert raises rather than corrupt seller attribution."""
    with pytest.raises(ValueError):
        await mod.upsert_catalog_offer_from_seed_row(
            _REAL_PK, {"id": "s1"}, merchant_id="external_seed"
        )
    with pytest.raises(ValueError):
        await mod.upsert_catalog_offer_from_seed_row(_REAL_PK, {"id": "s1"}, merchant_id="")


@pytest.mark.asyncio
async def test_sync_is_fail_soft(monkeypatch):
    monkeypatch.setenv("EXTERNAL_OFFER_DUAL_WRITE_ENABLED", "1")

    class BoomDB:
        async def fetch_one(self, *a, **k):
            raise RuntimeError("db down")

    monkeypatch.setattr(mod, "database", BoomDB())
    assert (await mod.sync_offer_for_seed("s1"))["status"] == "error"  # never raises
