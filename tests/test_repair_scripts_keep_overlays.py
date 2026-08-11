"""The `--apply` repair scripts must not strip the overlays they rewrite.

Mutation testing found these two functions had **no coverage at all**: restoring
the exact evidence-less `assemble_row` + full UPSERT they used to run produced
zero failures across the entire 10,592-test suite. Nothing in `tests/` even named
`_refresh_agent_pdp_view`. The only thing preventing reintroduction was a code
comment — on the two scripts an operator points at production.

Covered here:
  * scripts/source_pdp_content_repair.py  `_refresh_agent_pdp_view`
  * scripts/source_pdp_offer_image_repair.py  `_refresh_agent_pdp_view`
  * scripts/repair_external_seed_offer_mainline.py `_build_apv_offer_field_update`

The third is a different shape and was wrongly cleared twice — by a reviewer and
then by me repeating it. It does not run the full UPSERT, so "it only touches
offer fields" reads as safe. But its UPDATE assigns `offers = CAST(:offers AS
jsonb)`, replacing the whole array, and the W8 seller-trust envelope rides INSIDE
each offer object. A narrow UPDATE is not an overlay-free one.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import agent_pdp_view_assembler as apv  # noqa: E402

_CONTENT_KEY = "ck-repair-1"

_PRODUCT = {
    "product_key": "pk-1", "merchant_id": "m1", "platform": "shopify",
    "source_product_id": "sp-1", "title": "Glow Serum",
    "description": "Raw storefront description.", "brand": "AuraGlow",
}
_OFFER = {
    "offer_id": "of-1", "sku_key": "sk-1", "product_key": "pk-1",
    "merchant_id": "m1", "availability": "in_stock", "currency": "USD",
    "list_price": "42.00", "merchant_effective_price": "39.00",
    "estimated_best_price": None, "merchant_name": "AuraGlow Store",
}
_SELLER_TRUST = {"m1": {"grade": "A", "n": 12}}
_ENRICHMENT = {
    "merchant_id": "m1", "platform": "shopify", "platform_product_id": "sp-1",
    "geo_code": "default", "description_markdown": "Curated Pivota copy.",
    "bullet_points": ["Niacinamide 5%"], "usage_scenarios": ["Morning routine"],
}
_EVIDENCE = {
    "evidence_profile": {"substantiated_claims": [{"claim": "fragrance free"}]},
    "required_disclaimers": ["Not evaluated by the FDA."],
}


class _RecordingDB:
    is_connected = True

    def __init__(self) -> None:
        self.executes: List[Dict[str, Any]] = []

    async def fetch_one(self, sql: str, params: Dict[str, Any] = None):
        return None

    async def fetch_all(self, sql: str, params: Dict[str, Any] = None):
        return []

    async def execute(self, sql: str, params: Dict[str, Any]) -> None:
        self.executes.append({"sql": sql, "params": params})


@pytest.fixture
def enriched(monkeypatch: pytest.MonkeyPatch) -> _RecordingDB:
    db = _RecordingDB()

    async def products(content_key: str, *, db: Any = None):
        return [dict(_PRODUCT)]

    async def empty(product_keys, *, db: Any = None):
        return []

    async def one_offer(product_keys, *, db: Any = None):
        return [dict(_OFFER)]

    async def seed(product_keys, *, db: Any = None):
        return None

    async def evidence(product_keys, *, db: Any = None):
        return dict(_EVIDENCE)

    async def enrichment(products_):
        return dict(_ENRICHMENT)

    async def trust(merchant_ids):
        return dict(_SELLER_TRUST)

    monkeypatch.setattr(apv, "fetch_products_for_key", products)
    monkeypatch.setattr(apv, "fetch_skus_for_keys", empty)
    monkeypatch.setattr(apv, "fetch_offers_for_keys", one_offer)
    monkeypatch.setattr(apv, "fetch_external_seed_for_keys", seed)
    monkeypatch.setattr(apv, "fetch_evidence_for_keys", evidence)
    monkeypatch.setattr(apv, "_fetch_enrichment_for_canonical", enrichment)
    monkeypatch.setattr(apv, "database", db)
    import services.outcome_aggregation_service as oas
    monkeypatch.setattr(oas, "seller_trust_bulk", trust)
    return db


def _offers_of(params: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = params.get("offers")
    return json.loads(raw) if isinstance(raw, str) else (raw or [])


@pytest.mark.asyncio
async def test_content_repair_keeps_the_overlays(
    monkeypatch: pytest.MonkeyPatch, enriched: _RecordingDB
) -> None:
    import scripts.source_pdp_content_repair as script

    monkeypatch.setattr(script, "database", enriched)
    assert await script._refresh_agent_pdp_view(_CONTENT_KEY) is True

    assert len(enriched.executes) == 1
    params = enriched.executes[0]["params"]
    assert params["bullet_points"], "content repair stripped bullet_points"
    assert params["evidence_profile"], "content repair stripped evidence_profile"
    assert params["description"] == _ENRICHMENT["description_markdown"]
    assert _offers_of(params)[0].get("seller_trust") == _SELLER_TRUST["m1"]


@pytest.mark.asyncio
async def test_offer_image_repair_keeps_the_overlays(
    monkeypatch: pytest.MonkeyPatch, enriched: _RecordingDB
) -> None:
    import scripts.source_pdp_offer_image_repair as script

    monkeypatch.setattr(script, "database", enriched)
    assert await script._refresh_agent_pdp_view(_CONTENT_KEY) is True

    assert len(enriched.executes) == 1
    params = enriched.executes[0]["params"]
    assert params["bullet_points"], "offer/image repair stripped bullet_points"
    assert params["evidence_profile"], "offer/image repair stripped evidence_profile"
    assert params["description"] == _ENRICHMENT["description_markdown"]
    assert _offers_of(params)[0].get("seller_trust") == _SELLER_TRUST["m1"]


@pytest.mark.asyncio
async def test_both_repair_scripts_return_false_when_there_is_nothing_to_build(
    monkeypatch: pytest.MonkeyPatch, enriched: _RecordingDB
) -> None:
    """Their callers branch on this bool; the old predicate was 'no products OR
    no product_keys OR no row'."""
    import scripts.source_pdp_content_repair as content
    import scripts.source_pdp_offer_image_repair as image

    async def no_products(content_key: str, *, db: Any = None):
        return []

    monkeypatch.setattr(apv, "fetch_products_for_key", no_products)
    monkeypatch.setattr(content, "database", enriched)
    monkeypatch.setattr(image, "database", enriched)

    assert await content._refresh_agent_pdp_view(_CONTENT_KEY) is False
    assert await image._refresh_agent_pdp_view(_CONTENT_KEY) is False
    assert enriched.executes == []


@pytest.mark.asyncio
async def test_the_offer_mainline_repair_keeps_seller_trust_on_its_narrow_update(
    monkeypatch: pytest.MonkeyPatch, enriched: _RecordingDB
) -> None:
    """The one that was cleared as 'fine' twice.

    It never runs the full UPSERT — but `offers = CAST(:offers AS jsonb)` replaces
    the published array, and seller_trust lives inside each offer object.
    """
    import scripts.repair_external_seed_offer_mainline as script

    # Patch the SCRIPT's bound names, not the assembler's. This module does
    # `from services.agent_pdp_view_assembler import fetch_products_for_key, ...`
    # at import time, so patching `apv.<fn>` only reaches it if this test module
    # happens to import the script AFTER the fixture ran. It did when this file
    # was run alone and did not once a sibling imported the script first — the
    # test passed for a reason that had nothing to do with the behaviour it
    # claims to pin. The other two scripts are unaffected: they now go through
    # refresh_agent_pdp_view_for_content_key, which resolves the fetch helpers
    # from the assembler's own module globals at call time.
    async def products(content_key: str, *, db: Any = None):
        return [dict(_PRODUCT)]

    async def empty(product_keys, *, db: Any = None):
        return []

    async def one_offer(product_keys, *, db: Any = None):
        return [dict(_OFFER)]

    async def seed(product_keys, *, db: Any = None):
        return None

    monkeypatch.setattr(script, "fetch_products_for_key", products)
    monkeypatch.setattr(script, "fetch_skus_for_keys", empty)
    monkeypatch.setattr(script, "fetch_offers_for_keys", one_offer)
    monkeypatch.setattr(script, "fetch_external_seed_for_keys", seed)
    monkeypatch.setattr(script, "database", enriched)
    out = await script._build_apv_offer_field_update(_CONTENT_KEY, db=enriched)

    assert out is not None
    offers = json.loads(out["offers"]) if isinstance(out["offers"], str) else out["offers"]
    assert offers, "no offers built — the assertion below cannot fail"
    assert offers[0].get("seller_trust") == _SELLER_TRUST["m1"], (
        "the offer-mainline repair strips seller_trust from every offer it writes"
    )
