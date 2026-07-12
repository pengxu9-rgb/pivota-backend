"""--no-serving (decision-grade-only) onboard: _onboard(serve=False) writes the
identity/mirror/category/INCI half but SKIPS the serving half
(_resolve_pdp_scope + make_external_seed_servable), so onboarded external-seed
rows never become servable. Default (serve=True) is unchanged — it still runs
both serving steps. See the electronics-pilot deposit-leg scope.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.onboard_external_brand_from_crawl as onboard  # noqa: E402

_MIRROR_ENV = "EXTERNAL_SEED_MIRROR_MAKE_SERVABLE"


class _FakeDB:
    async def execute(self, sql, params=None):
        return None

    async def fetch_one(self, sql, params=None):
        # _resolve_pdp_scope reads seller_count; a single-seller row is fine.
        return {"seller_count": 1}


_PK = "prod::merch_obs_deadbeefdeadbeef::external_seed::mojawa_us_1"
_MID = "merch_obs_deadbeefdeadbeef"
_COHORT = [{
    "external_product_id": "mojawa_us_1",
    "brand": "Mojawa",
    "title": "Purra Run Bone Conduction Headphones",
    "category_kind": "electronics",
    "destination_url": "https://mojawa.com/products/purra-run",
    "price_amount": 159.99,
    "raw_inci": "",
}]


@pytest.fixture
def spies(monkeypatch):
    """Stub every DB-touching dependency of _onboard and record which of the two
    serving steps get called."""
    calls = {"resolve_pdp_scope": 0, "make_servable": 0, "mirror": 0, "inci": 0,
             "mirror_env": "unset"}
    monkeypatch.setattr(onboard, "database", _FakeDB())
    # Baseline: prod has the mirror's serving pass ON. monkeypatch restores it
    # after the test, so the onboard's direct os.environ mutation can't leak.
    monkeypatch.setenv(_MIRROR_ENV, "1")

    async def _resolve_seller_and_key(p):
        return _MID, _PK

    async def _derive_seed_seller(**kwargs):
        return _MID, "self"

    async def _upsert_seed(p, *, seller_ref, seed_kind):
        return None

    async def _set_category_and_offer(p, pk, self_merchant_id):
        return None

    async def _suppress(dropped):
        return 0

    async def _mirror_apply(limit):
        calls["mirror"] += 1
        # Capture the mirror's serving-pass env AS THE MIRROR WOULD SEE IT — the
        # onboard must have disabled it before calling us under --no-serving.
        calls["mirror_env"] = os.environ.get(_MIRROR_ENV)
        return len(_COHORT)

    async def _ingest(items, dry_run, db):
        calls["inci"] += 1
        return {"inci_written": 0, "actives_filled": 0, "skipped": len(items)}

    async def _resolve_pdp_scope(p, pk, mid):
        calls["resolve_pdp_scope"] += 1

    async def _make_servable(**kwargs):
        calls["make_servable"] += 1
        return {"serving_eligible": True}

    monkeypatch.setattr(onboard, "_resolve_seller_and_key", _resolve_seller_and_key)
    monkeypatch.setattr(onboard, "derive_seed_seller", _derive_seed_seller)
    monkeypatch.setattr(onboard, "_upsert_seed", _upsert_seed)
    monkeypatch.setattr(onboard, "_set_category_and_offer", _set_category_and_offer)
    monkeypatch.setattr(onboard, "_suppress_dropped_listings", _suppress)
    monkeypatch.setattr(onboard, "mirror_apply", _mirror_apply)
    monkeypatch.setattr(onboard, "ingest_crawled_inci_items", _ingest)
    monkeypatch.setattr(onboard, "_resolve_pdp_scope", _resolve_pdp_scope)
    monkeypatch.setattr(onboard, "make_external_seed_servable", _make_servable)
    return calls


async def test_no_serving_skips_both_serving_steps(spies):
    await onboard._onboard(_COHORT, [], serve=False)
    # identity/mirror/INCI half still ran...
    assert spies["mirror"] == 1
    assert spies["inci"] == 1
    # ...but neither serving step fired.
    assert spies["resolve_pdp_scope"] == 0
    assert spies["make_servable"] == 0
    # ...and the mirror's OWN serving pass was disabled before it ran, so no
    # serving artifacts are minted by the mirror either (invariant by construction).
    assert spies["mirror_env"] == "0"


async def test_default_runs_both_serving_steps(spies):
    await onboard._onboard(_COHORT, [], serve=True)
    assert spies["mirror"] == 1
    assert spies["inci"] == 1
    # default posture still serves: one _resolve_pdp_scope + one make_servable per product.
    assert spies["resolve_pdp_scope"] == 1
    assert spies["make_servable"] == 1
    # default must NOT touch the mirror's serving-pass env (prod baseline "1" stands).
    assert spies["mirror_env"] == "1"
