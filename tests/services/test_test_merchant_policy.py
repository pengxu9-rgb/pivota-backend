"""Test/demo merchant exclusion — the serving-side rig gate.

Covers the id denylist, the additive-only env hatch, the fail-soft cached
demo-domain resolver, and the product filter (dict + object, missing id kept).
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from services import test_merchant_policy as pol


@pytest.fixture(autouse=True)
def _clear_cache():
    pol.reset_cache()
    yield
    pol.reset_cache()


class _FakeDB:
    """Minimal async database stub: fetch_all returns preset rows or raises."""

    def __init__(self, rows: List[Dict[str, Any]] | None = None, raise_exc: bool = False):
        self._rows = rows or []
        self._raise = raise_exc
        self.calls = 0

    async def fetch_all(self, query, values=None):
        self.calls += 1
        if self._raise:
            raise RuntimeError("db down")
        return self._rows


def test_static_set_bakes_in_all_four_rigs():
    ids = pol.static_test_merchant_ids({})
    assert "merch_efbc46b4619cfbdf" in ids
    assert "merch_shopify_0584b37f7a8be00a5223" in ids
    assert "merch_shopify_00d4a720d67d96c5dcba" in ids
    assert "merch_bbd34645bc1950cc" in ids


def test_static_set_bakes_in_the_ownist_fixture_merchant():
    """ADR-018 census (#1595): a fixture catalog with 4 serving_eligible rows
    and NO merchant_stores row, so the demo-domain resolver can never reach it
    — the id denylist is the only leg that excludes it."""
    assert "merch_test_ownist_001" in pol.static_test_merchant_ids({})


async def test_ownist_is_excluded_without_any_store_row():
    # Resolver returns nothing (no merchant_stores row for this rig at all);
    # the exclusion must still hold from the static denylist.
    db = _FakeDB(rows=[])
    ids = await pol.get_excluded_merchant_ids(db, env={})
    assert "merch_test_ownist_001" in ids


def test_env_hatch_is_additive_only():
    ids = pol.static_test_merchant_ids({"PIVOTA_TEST_MERCHANT_IDS": "merch_new, merch_two"})
    assert "merch_new" in ids and "merch_two" in ids
    # cannot un-exclude a baked-in id
    assert "merch_efbc46b4619cfbdf" in pol.static_test_merchant_ids({"PIVOTA_TEST_MERCHANT_IDS": ""})


async def test_resolver_unions_demo_domain_merchants():
    db = _FakeDB(rows=[{"merchant_id": "merch_reconnected_demo"}])
    ids = await pol.get_excluded_merchant_ids(db, env={})
    assert "merch_reconnected_demo" in ids          # resolved by domain
    assert "merch_efbc46b4619cfbdf" in ids          # still has the static set


async def test_resolver_is_fail_soft_on_db_error():
    db = _FakeDB(raise_exc=True)
    ids = await pol.get_excluded_merchant_ids(db, env={})
    # DB blew up → only the static denylist, never a crash, never an empty leak
    assert "merch_efbc46b4619cfbdf" in ids
    assert "merch_reconnected_demo" not in ids


async def test_resolver_caches_within_ttl():
    db = _FakeDB(rows=[{"merchant_id": "m_demo"}])
    await pol.get_excluded_merchant_ids(db, env={}, now=1000.0)
    await pol.get_excluded_merchant_ids(db, env={}, now=1100.0)  # < 300s later
    assert db.calls == 1                                          # served from cache
    await pol.get_excluded_merchant_ids(db, env={}, now=1400.0)  # > 300s later
    assert db.calls == 2


def test_none_db_returns_static_only():
    import asyncio

    ids = asyncio.get_event_loop().run_until_complete(
        pol.get_excluded_merchant_ids(None, env={})
    )
    assert ids == pol.static_test_merchant_ids({})


def test_filter_drops_rigs_keeps_real_and_unscoped():
    excluded = {"merch_efbc46b4619cfbdf"}
    products = [
        {"merchant_id": "merch_efbc46b4619cfbdf", "title": "Winona test"},
        {"merchant_id": "merch_obs_cosrx", "title": "real"},
        {"title": "external_seed canonical (no merchant scope)"},
    ]
    kept = pol.filter_out_test_merchants(products, excluded)
    titles = [p.get("title") for p in kept]
    assert "Winona test" not in titles
    assert "real" in titles
    assert "external_seed canonical (no merchant scope)" in titles


def test_filter_handles_object_products():
    class _P:
        def __init__(self, mid):
            self.merchant_id = mid

    kept = pol.filter_out_test_merchants(
        [_P("merch_efbc46b4619cfbdf"), _P("merch_real")],
        {"merch_efbc46b4619cfbdf"},
    )
    assert len(kept) == 1 and kept[0].merchant_id == "merch_real"


def test_filter_empty_input():
    assert pol.filter_out_test_merchants(None, {"x"}) == []
    assert pol.filter_out_test_merchants([], {"x"}) == []
