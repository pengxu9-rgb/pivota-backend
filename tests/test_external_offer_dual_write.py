"""Unit tests for the P1.6 external seed → catalog_offers projection.

Pure-logic + mocked-database tests (no real SQL execution). The offer field
mapping itself is asserted by the mirror test suite (which now patches this
module); here we cover the identity derivation + the sync_offer_for_seed control
flow and its dark-launch kill-switch.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import external_offer_dual_write as mod  # noqa: E402


class FakeDB:
    def __init__(self, *, seed=None, product_exists=False):
        self._seed = seed
        self._product_exists = product_exists
        self.executed = []

    async def fetch_one(self, sql, params=None):
        # Only the seed lookup uses fetch_one in this module.
        return dict(self._seed) if self._seed else None

    async def fetch_val(self, sql, params=None):
        return 1 if self._product_exists else None

    async def execute(self, sql, params=None):
        self.executed.append({"sql": str(sql), "params": dict(params or {})})


def test_mirror_product_key_format():
    assert mod.mirror_product_key("ext1") == "prod::external_seed::external_seed::ext1"


def test_derive_sku_key():
    pk = "prod::external_seed::external_seed::ext1"
    assert mod.derive_mirror_sku_key(pk) == f"{pk}::canonical"


def test_derive_offer_id_deterministic_and_prefixed():
    pk = "prod::external_seed::external_seed::ext1"
    a = mod.derive_mirror_offer_id(pk)
    b = mod.derive_mirror_offer_id(pk)
    assert a == b
    assert a.startswith(mod.OFFER_ID_PREFIX)
    assert mod.derive_mirror_offer_id("other") != a


@pytest.mark.asyncio
async def test_sync_disabled_is_noop(monkeypatch):
    monkeypatch.setenv("EXTERNAL_OFFER_DUAL_WRITE_ENABLED", "off")
    fake = FakeDB(seed={"id": "s1", "external_product_id": "ext1"}, product_exists=True)
    monkeypatch.setattr(mod, "database", fake)
    result = await mod.sync_offer_for_seed("s1")
    assert result["status"] == "disabled"
    assert fake.executed == []


@pytest.mark.asyncio
async def test_sync_seed_missing(monkeypatch):
    monkeypatch.setenv("EXTERNAL_OFFER_DUAL_WRITE_ENABLED", "true")
    fake = FakeDB(seed=None)
    monkeypatch.setattr(mod, "database", fake)
    result = await mod.sync_offer_for_seed("gone")
    assert result["status"] == "seed_missing"


@pytest.mark.asyncio
async def test_sync_no_external_product_id(monkeypatch):
    monkeypatch.setenv("EXTERNAL_OFFER_DUAL_WRITE_ENABLED", "true")
    fake = FakeDB(seed={"id": "s1", "external_product_id": None})
    monkeypatch.setattr(mod, "database", fake)
    result = await mod.sync_offer_for_seed("s1")
    assert result["status"] == "no_external_product_id"
    assert fake.executed == []


@pytest.mark.asyncio
async def test_sync_no_mirror_product_yet(monkeypatch):
    monkeypatch.setenv("EXTERNAL_OFFER_DUAL_WRITE_ENABLED", "true")
    fake = FakeDB(
        seed={"id": "s1", "external_product_id": "ext1"}, product_exists=False
    )
    monkeypatch.setattr(mod, "database", fake)
    result = await mod.sync_offer_for_seed("s1")
    assert result["status"] == "no_mirror_product"
    assert result["product_key"] == "prod::external_seed::external_seed::ext1"
    assert fake.executed == []  # never creates the product/offer chain itself


@pytest.mark.asyncio
async def test_sync_projects_offer_when_mirror_exists(monkeypatch):
    monkeypatch.setenv("EXTERNAL_OFFER_DUAL_WRITE_ENABLED", "true")
    seed = {
        "id": "s1",
        "external_product_id": "ext1",
        "price_amount": 19.99,
        "price_currency": "USD",
        "availability": "in_stock",
        "destination_url": "https://x.test/p",
        "canonical_url": "https://x.test/p",
        "domain": "x.test",
        "market": "US",
    }
    fake = FakeDB(seed=seed, product_exists=True)
    monkeypatch.setattr(mod, "database", fake)
    result = await mod.sync_offer_for_seed("s1")
    assert result["status"] == "synced"
    assert result["product_key"] == "prod::external_seed::external_seed::ext1"
    assert len(fake.executed) == 1
    params = fake.executed[0]["params"]
    assert "INSERT INTO catalog_offers" in fake.executed[0]["sql"]
    assert params["list_price"] == 19.99
    assert params["merchant_effective_price"] == 19.99
    assert params["estimated_best_price"] == 19.99
    assert params["catalog_track"] == "external_referral"
    assert params["truth_tier"] == "observed"
    assert params["readiness_tier"] == "referral_only"
    assert params["offer_mode"] == "redirect"
    assert params["source_ref"] == "s1"


@pytest.mark.asyncio
async def test_sync_is_fail_soft(monkeypatch):
    monkeypatch.setenv("EXTERNAL_OFFER_DUAL_WRITE_ENABLED", "true")

    class BoomDB:
        async def fetch_one(self, *a, **k):
            raise RuntimeError("db down")

    monkeypatch.setattr(mod, "database", BoomDB())
    result = await mod.sync_offer_for_seed("s1")
    assert result["status"] == "error"  # never raises into the caller
