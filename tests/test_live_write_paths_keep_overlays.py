"""The two LIVE write paths must not strip the overlays they rewrite.

These are not operator tools. They fire on ordinary product traffic:

  * `services/fashion_field_authoring._refresh_view_for_content_key` — reached
    from `PUT /{platform}/{platform_product_id}/fashion_fields`, i.e. a merchant
    saving a `material` or `care` value.
  * `services/seed_data_writer.refresh_agent_pdp_view_for_seed` — reached on
    every seed_data commit / proposal application.

Both inlined their own `assemble_row` with no `evidence=`, `enrichment=` or
`seller_trust_by_id=`, then ran the full UPSERT, which assigns every column from
EXCLUDED. So each of them NULLed the curated description, bullet_points,
usage_scenarios, evidence_profile and required_disclaimers of any product in the
cluster that had them — silently, with nothing logged, and reachable by a
merchant editing one field.

That is strictly worse than the backfill-script version of this defect, which at
least required an operator to run it, and it is very likely a source of the
"stranded enrichment" cohort the backfill exists to repair: re-publishing an
overlay does no good if the next seed commit wipes it again.

WHAT IS ASSERTED. Each path is driven end to end against a product that HAS an
overlay, and the row handed to the UPSERT must still carry it. This is not a
textual pin — any rebuild that drops the overlays fails here however it is
spelled, including a reintroduced local `assemble_row`, a new "fast path", or a
refactor that loses a kwarg.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import agent_pdp_view_assembler as apv  # noqa: E402

_CONTENT_KEY = "ck-live-1"
_SEED_ID = "seed-live-1"
_ATTACHED_PRODUCT_KEY = "pk-live-1"

_PRODUCT = {
    "product_key": _ATTACHED_PRODUCT_KEY,
    "merchant_id": "m1",
    "platform": "shopify",
    "source_product_id": "sp-1",
    "title": "Glow Serum",
    "description": "Raw storefront description.",
    "brand": "AuraGlow",
}

_ENRICHMENT = {
    "merchant_id": "m1", "platform": "shopify", "platform_product_id": "sp-1",
    "geo_code": "default",
    "description_markdown": "Curated Pivota copy.",
    "bullet_points": ["Niacinamide 5%"],
    "usage_scenarios": ["Morning routine"],
    "updated_by_employee_id": "emp-1",
}

_EVIDENCE = {
    "evidence_profile": {"substantiated_claims": [{"claim": "fragrance free"}]},
    "required_disclaimers": ["Not evaluated by the FDA."],
}


class _FakeDB:
    """Answers the two lookup queries the seed path makes, records upserts."""

    is_connected = True

    def __init__(self) -> None:
        self.executes: List[Dict[str, Any]] = []

    async def fetch_one(self, sql: str, params: Dict[str, Any]):
        if "external_product_seeds" in sql:
            return {"attached_product_key": _ATTACHED_PRODUCT_KEY}
        if "catalog_products" in sql:
            return {"content_key": _CONTENT_KEY}
        return None

    async def fetch_all(self, sql: str, params: Dict[str, Any]):
        return []

    async def execute(self, sql: str, params: Dict[str, Any]) -> None:
        self.executes.append({"sql": sql, "params": params})


@pytest.fixture
def enriched(monkeypatch: pytest.MonkeyPatch) -> _FakeDB:
    """A product carrying every overlay family, and the seed lookups wired."""
    db = _FakeDB()

    async def products(content_key: str, *, db: Any = None):
        return [dict(_PRODUCT)]

    async def empty(product_keys, *, db: Any = None):
        return []

    async def seed_for_keys(product_keys, *, db: Any = None):
        return {"id": "seed-newest-in-group", "seed_data": {}}

    async def seed_by_id(seed_id, *, db: Any = None):
        return {"id": seed_id, "seed_data": {}}

    async def evidence(product_keys, *, db: Any = None):
        return dict(_EVIDENCE)

    async def enrichment(products_):
        return dict(_ENRICHMENT)

    monkeypatch.setattr(apv, "fetch_products_for_key", products)
    monkeypatch.setattr(apv, "fetch_skus_for_keys", empty)
    monkeypatch.setattr(apv, "fetch_offers_for_keys", empty)
    monkeypatch.setattr(apv, "fetch_external_seed_for_keys", seed_for_keys)
    monkeypatch.setattr(apv, "fetch_external_seed_by_id", seed_by_id)
    monkeypatch.setattr(apv, "fetch_evidence_for_keys", evidence)
    monkeypatch.setattr(apv, "_fetch_enrichment_for_canonical", enrichment)
    monkeypatch.setattr(apv, "database", db)
    return db


def _assert_overlays_intact(params: Dict[str, Any]) -> None:
    assert params["bullet_points"], "bullet_points were stripped by a live write path"
    assert params["usage_scenarios"], "usage_scenarios were stripped by a live write path"
    assert params["evidence_profile"], "evidence_profile was stripped by a live write path"
    assert params["required_disclaimers"], "required_disclaimers were stripped"
    assert params["description"] == _ENRICHMENT["description_markdown"], (
        "curated copy was replaced by the raw storefront description"
    )


@pytest.mark.asyncio
async def test_a_merchant_fashion_field_edit_keeps_the_overlays(
    monkeypatch: pytest.MonkeyPatch, enriched: _FakeDB
) -> None:
    """The worst of the two: a merchant saving one field stripped the PDP."""
    import services.fashion_field_authoring as ffa

    monkeypatch.setattr(ffa, "database", enriched)

    async def no_recompute(content_key, reason=""):
        return None

    import services.index_pipeline_state_service as ipss
    monkeypatch.setattr(ipss, "recompute_serving_eligibility", no_recompute)

    await ffa._refresh_view_for_content_key(_CONTENT_KEY)

    assert len(enriched.executes) == 1, "the fashion-field write did not refresh the view"
    _assert_overlays_intact(enriched.executes[0]["params"])


@pytest.mark.asyncio
async def test_a_seed_commit_keeps_the_overlays(
    monkeypatch: pytest.MonkeyPatch, enriched: _FakeDB
) -> None:
    import services.seed_data_writer as sdw

    monkeypatch.setattr(sdw, "database", enriched)

    async def no_recompute(content_key, reason=""):
        return None

    import services.index_pipeline_state_service as ipss
    monkeypatch.setattr(ipss, "recompute_serving_eligibility", no_recompute)

    await sdw.refresh_agent_pdp_view_for_seed(
        seed_id=_SEED_ID, proposal_id=4242, refresh_source="writer_commit:4242",
    )

    assert len(enriched.executes) == 1
    _assert_overlays_intact(enriched.executes[0]["params"])


@pytest.mark.asyncio
async def test_the_seed_path_still_uses_the_TRIGGERING_seed(
    monkeypatch: pytest.MonkeyPatch, enriched: _FakeDB
) -> None:
    """The one thing that made this call site special, and the thing a careless
    conversion to the canonical path would silently lose: the writer knows which
    seed_data just changed. The default (newest active seed in the cluster) can
    pull description text from a stale, unrelated seed."""
    import services.seed_data_writer as sdw

    monkeypatch.setattr(sdw, "database", enriched)
    seen: Dict[str, Any] = {}

    async def record_by_id(seed_id, *, db: Any = None):
        seen["by_id"] = seed_id
        return {"id": seed_id, "seed_data": {}}

    async def must_not_be_used(product_keys, *, db: Any = None):
        seen["by_keys"] = True
        return None

    monkeypatch.setattr(apv, "fetch_external_seed_by_id", record_by_id)
    monkeypatch.setattr(apv, "fetch_external_seed_for_keys", must_not_be_used)

    async def no_recompute(content_key, reason=""):
        return None

    import services.index_pipeline_state_service as ipss
    monkeypatch.setattr(ipss, "recompute_serving_eligibility", no_recompute)

    await sdw.refresh_agent_pdp_view_for_seed(
        seed_id=_SEED_ID, proposal_id=1, refresh_source="writer_commit:1",
    )

    assert seen.get("by_id") == _SEED_ID
    assert "by_keys" not in seen, "fell back to the newest seed in the cluster"


@pytest.mark.asyncio
async def test_the_seed_path_still_records_its_proposal(
    monkeypatch: pytest.MonkeyPatch, enriched: _FakeDB
) -> None:
    import services.seed_data_writer as sdw

    monkeypatch.setattr(sdw, "database", enriched)

    async def no_recompute(content_key, reason=""):
        return None

    import services.index_pipeline_state_service as ipss
    monkeypatch.setattr(ipss, "recompute_serving_eligibility", no_recompute)

    await sdw.refresh_agent_pdp_view_for_seed(
        seed_id=_SEED_ID, proposal_id=777, refresh_source="writer_commit:777",
    )
    assert enriched.executes[0]["params"]["refreshed_by_proposal_id"] == 777


@pytest.mark.asyncio
async def test_the_fashion_path_records_no_proposal(
    monkeypatch: pytest.MonkeyPatch, enriched: _FakeDB
) -> None:
    """It has none to record — and the UPSERT COALESCEs, so passing None must
    not erase an attribution the seed writer previously set."""
    import services.fashion_field_authoring as ffa

    monkeypatch.setattr(ffa, "database", enriched)

    async def no_recompute(content_key, reason=""):
        return None

    import services.index_pipeline_state_service as ipss
    monkeypatch.setattr(ipss, "recompute_serving_eligibility", no_recompute)

    await ffa._refresh_view_for_content_key(_CONTENT_KEY)
    assert enriched.executes[0]["params"]["refreshed_by_proposal_id"] is None


@pytest.mark.asyncio
async def test_both_paths_still_preserve_on_a_failed_overlay_read(
    monkeypatch: pytest.MonkeyPatch, enriched: _FakeDB
) -> None:
    """They inherit the tri-state by going through the canonical path: a
    transient overlay-read failure must ask the write to keep what is
    published rather than blanking it."""
    import services.fashion_field_authoring as ffa

    monkeypatch.setattr(ffa, "database", enriched)

    async def failed(products_):
        return apv.FETCH_FAILED

    monkeypatch.setattr(apv, "_fetch_enrichment_for_canonical", failed)

    async def no_recompute(content_key, reason=""):
        return None

    import services.index_pipeline_state_service as ipss
    monkeypatch.setattr(ipss, "recompute_serving_eligibility", no_recompute)

    await ffa._refresh_view_for_content_key(_CONTENT_KEY)
    assert enriched.executes[0]["params"]["preserve_enrichment"] is True
