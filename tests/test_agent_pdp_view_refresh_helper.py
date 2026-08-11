"""Tests for refresh_agent_pdp_view_for_content_key — the canonical
fetch->assemble->upsert orchestration used by the catalog_sync auto-servable
hook (so a fresh internal-merchant SKU gets an APV row before recompute).

🚨 WHAT THE _FakeDB CASES BELOW CANNOT PROVE. `_FakeDB.fetch_one` returns a
canned row whatever SQL it is handed, so the enrichment-write cases assert only
the CONTROL FLOW around the bridge — the flag gate, the identity guard, the
best-effort swallow. They cannot see whether the statement is valid, and they
did not: all four passed for weeks while the bridge queried
`catalog_products.platform_product_id`, a column that does not exist, so every
real call raised UndefinedColumn into the swallow and the bridge was dead.

The statement itself is constrained in two other places, and both are load
bearing: tests/test_enrichment_bridge_and_cohort_postgres.py EXECUTES it against
a real catalog_products row (reverting the column turns it red), and the
repo-wide prepare gate plans it. Do not add a case here that claims the bridge
"works" — this file cannot support that claim."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import agent_pdp_view_assembler as apv  # noqa: E402

_PRODUCT_FOR_TRISTATE = {
    "product_key": "pk-1", "merchant_id": "m1", "platform": "shopify",
    "source_product_id": "sp-1", "title": "Glow Serum",
    "description": "A long enough description for the agent PDP view row.",
    "brand": "AuraGlow",
}


class _FakeDB:
    def __init__(self, fetch_one_result: Optional[Dict[str, Any]] = None) -> None:
        self.executes: List[Dict[str, Any]] = []
        self.fetch_ones: List[Dict[str, Any]] = []
        self._fetch_one_result = fetch_one_result

    async def execute(self, sql: str, params: Dict[str, Any]) -> None:
        self.executes.append({"sql": sql, "params": params})

    async def fetch_one(self, sql: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        self.fetch_ones.append({"sql": sql, "params": params})
        return self._fetch_one_result


def _patch_fetches(
    monkeypatch: pytest.MonkeyPatch,
    *,
    products: List[Dict[str, Any]],
) -> None:
    async def fake_products(content_key: str, *, db: Any = None):
        return products

    async def fake_skus(product_keys, *, db: Any = None):
        return []

    async def fake_offers(product_keys, *, db: Any = None):
        return []

    async def fake_seed(product_keys, *, db: Any = None):
        return None

    async def fake_evidence(product_keys, *, db: Any = None):
        return {}

    monkeypatch.setattr(apv, "fetch_products_for_key", fake_products)
    monkeypatch.setattr(apv, "fetch_skus_for_keys", fake_skus)
    monkeypatch.setattr(apv, "fetch_offers_for_keys", fake_offers)
    monkeypatch.setattr(apv, "fetch_external_seed_for_keys", fake_seed)
    monkeypatch.setattr(apv, "fetch_evidence_for_keys", fake_evidence)


@pytest.mark.asyncio
async def test_refresh_builds_and_upserts_when_title_present(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_fetches(
        monkeypatch,
        products=[{
            "product_key": "pk-1",
            "merchant_id": "m1",
            "platform": "shopify",
            "source_product_id": "sp-1",
            "title": "Glow Serum",
            "description": "A long enough description for the agent PDP view row.",
            "brand": "AuraGlow",
            "image_url": "https://img.example/serum.jpg",
        }],
    )
    db = _FakeDB()
    built = await apv.refresh_agent_pdp_view_for_content_key(
        "ck-1", refresh_source="catalog_sync", db=db
    )
    assert built is True
    assert len(db.executes) == 1
    assert db.executes[0]["sql"] is apv.UPSERT_SQL
    assert db.executes[0]["params"]["content_key"] == "ck-1"
    assert db.executes[0]["params"]["refresh_source"] == "catalog_sync"


@pytest.mark.asyncio
async def test_refresh_projects_evidence_into_agent_pdp_view(monkeypatch: pytest.MonkeyPatch) -> None:
    # Graded claims authored on the canonical record must reach the agent view.
    _patch_fetches(
        monkeypatch,
        products=[{
            "product_key": "pk-1", "merchant_id": "m1", "platform": "shopify",
            "source_product_id": "sp-1", "title": "Glow Serum",
            "description": "A long enough description for the agent PDP view row.",
            "brand": "AuraGlow",
        }],
    )
    profile = {"claims": [{"claim_text": "Helps brighten", "source_type": "ingredient_mechanism",
                           "substantiation_status": "substantiated"}], "review_state": "observed"}
    disclaimers = [{"text": "FDA disclaimer"}]

    async def fake_evidence(product_keys, *, db: Any = None):
        return {"evidence_profile": profile, "required_disclaimers": disclaimers}

    monkeypatch.setattr(apv, "fetch_evidence_for_keys", fake_evidence)
    db = _FakeDB()
    built = await apv.refresh_agent_pdp_view_for_content_key("ck-1", refresh_source="catalog_sync", db=db)
    assert built is True
    params = db.executes[0]["params"]
    # JSONB columns are serialized to a JSON string by row_to_upsert_params.
    assert params["evidence_profile"] == apv.to_jsonb(profile)
    assert params["required_disclaimers"] == apv.to_jsonb(disclaimers)
    assert "Helps brighten" in params["evidence_profile"]


@pytest.mark.asyncio
async def test_refresh_overlays_enrichment_description_markdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """E2 publish bridge: refresh fetches the product_enrichment overlay and the
    generated description_markdown reaches the upserted agent_pdp_view.description
    — the wire that lets enriched copy reach the served PDP AND the
    serving-eligibility gate (which reads that stored description)."""
    _patch_fetches(
        monkeypatch,
        products=[{
            "product_key": "pk-1", "merchant_id": "m1", "platform": "shopify",
            "source_product_id": "sp-1", "title": "Good Night Collagen",
            "description": "thin raw storefront description",
            "brand": "BB Lab",
        }],
    )

    async def fake_enrichment(products: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        return {"description_markdown": "Low-molecular collagen, 30 sticks. Halal-certified."}

    monkeypatch.setattr(apv, "_fetch_enrichment_for_canonical", fake_enrichment)
    db = _FakeDB()
    built = await apv.refresh_agent_pdp_view_for_content_key(
        "ck-1", refresh_source="canonical_pdp_enrichment", db=db
    )
    assert built is True
    assert (
        db.executes[0]["params"]["description"]
        == "Low-molecular collagen, 30 sticks. Halal-certified."
    )


@pytest.mark.asyncio
async def test_refresh_returns_false_when_no_products(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_fetches(monkeypatch, products=[])
    db = _FakeDB()
    built = await apv.refresh_agent_pdp_view_for_content_key(
        "ck-missing", refresh_source="catalog_sync", db=db
    )
    assert built is False
    assert db.executes == []


@pytest.mark.asyncio
async def test_refresh_returns_false_when_row_too_thin(monkeypatch: pytest.MonkeyPatch) -> None:
    # Products exist but no title → assemble_row returns None → no upsert.
    _patch_fetches(
        monkeypatch,
        products=[{
            "product_key": "pk-2",
            "merchant_id": "m1",
            "platform": "shopify",
            "source_product_id": "sp-2",
            "title": None,
        }],
    )
    db = _FakeDB()
    built = await apv.refresh_agent_pdp_view_for_content_key(
        "ck-2", refresh_source="catalog_sync", db=db
    )
    assert built is False
    assert db.executes == []


# ── B① write-triggered enrichment propagation ──────────────────────────────


@pytest.mark.asyncio
async def test_enrichment_write_refresh_noop_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    # Default OFF: no content_key resolution, no view rebuild — current behavior.
    monkeypatch.delenv("SERVE_PDP_ENRICHMENT_ON_WRITE", raising=False)
    db = _FakeDB(fetch_one_result={"content_key": "ck-1"})
    out = await apv.refresh_agent_pdp_view_for_enrichment_write(
        "m1", "shopify", "sp-1", db=db
    )
    assert out is False
    assert db.fetch_ones == []  # didn't even resolve the content_key
    assert db.executes == []


@pytest.mark.asyncio
async def test_enrichment_write_refresh_false_on_missing_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVE_PDP_ENRICHMENT_ON_WRITE", "1")
    db = _FakeDB(fetch_one_result={"content_key": "ck-1"})
    out = await apv.refresh_agent_pdp_view_for_enrichment_write("m1", None, "sp-1", db=db)
    assert out is False
    assert db.fetch_ones == []


@pytest.mark.asyncio
async def test_enrichment_write_refresh_false_when_no_catalog_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVE_PDP_ENRICHMENT_ON_WRITE", "1")
    db = _FakeDB(fetch_one_result=None)  # no catalog_products row maps to a content_key
    out = await apv.refresh_agent_pdp_view_for_enrichment_write(
        "m1", "shopify", "sp-1", db=db
    )
    assert out is False
    assert len(db.fetch_ones) == 1  # tried to resolve
    assert db.executes == []


@pytest.mark.asyncio
async def test_enrichment_write_refresh_rebuilds_view_when_flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    # Flag on + content_key resolves → the helper rebuilds the served view via
    # refresh_agent_pdp_view_for_content_key (one upsert with the resolved key).
    monkeypatch.setenv("SERVE_PDP_ENRICHMENT_ON_WRITE", "1")
    _patch_fetches(
        monkeypatch,
        products=[{
            "product_key": "pk-1", "merchant_id": "m1", "platform": "shopify",
            "source_product_id": "sp-1", "title": "Glow Serum",
            "description": "A long enough description for the agent PDP view row.",
            "brand": "AuraGlow",
        }],
    )
    db = _FakeDB(fetch_one_result={"content_key": "ck-resolved"})
    out = await apv.refresh_agent_pdp_view_for_enrichment_write(
        "m1", "shopify", "sp-1", db=db
    )
    assert out is True
    assert len(db.fetch_ones) == 1
    assert len(db.executes) == 1
    assert db.executes[0]["params"]["content_key"] == "ck-resolved"
    assert db.executes[0]["params"]["refresh_source"] == "enrichment_write"


@pytest.mark.asyncio
async def test_enrichment_write_refresh_swallows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    # Best-effort: a DB error during resolution must never raise into the writer.
    monkeypatch.setenv("SERVE_PDP_ENRICHMENT_ON_WRITE", "1")

    class _BoomDB:
        async def fetch_one(self, sql: str, params: Dict[str, Any]):
            raise RuntimeError("db down")

    out = await apv.refresh_agent_pdp_view_for_enrichment_write(
        "m1", "shopify", "sp-1", db=_BoomDB()
    )
    assert out is False


# ---------------------------------------------------------------------------
# tri-state overlay fetch
# ---------------------------------------------------------------------------
# A failed overlay READ and a successful read that finds NOTHING both used to
# arrive as None, so the write could not tell "preserve what is published" from
# "the operator deleted it". These pin the distinction at the point it is made;
# tests/test_agent_pdp_view_overlay_preservation_postgres.py proves the UPSERT
# then honours it against real Postgres.

@pytest.mark.asyncio
async def test_a_failed_enrichment_fetch_asks_the_write_to_preserve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fetches(monkeypatch, products=[dict(_PRODUCT_FOR_TRISTATE)])

    async def boom(products):
        return apv.FETCH_FAILED

    monkeypatch.setattr(apv, "_fetch_enrichment_for_canonical", boom)
    row = await apv.build_agent_pdp_view_row("ck-1", refresh_source="t", db=_FakeDB())
    assert row is not None
    assert row["preserve_enrichment"] is True
    assert row["preserve_evidence"] is False


@pytest.mark.asyncio
async def test_a_successful_fetch_finding_nothing_does_not_preserve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The half that keeps this a tri-state rather than a never-downgrade rule:
    a genuine removal must still reach the served row."""
    _patch_fetches(monkeypatch, products=[dict(_PRODUCT_FOR_TRISTATE)])

    async def absent(products):
        return None

    monkeypatch.setattr(apv, "_fetch_enrichment_for_canonical", absent)
    row = await apv.build_agent_pdp_view_row("ck-1", refresh_source="t", db=_FakeDB())
    assert row is not None
    assert row["preserve_enrichment"] is False


@pytest.mark.asyncio
async def test_a_failed_evidence_fetch_preserves_evidence_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fetches(monkeypatch, products=[dict(_PRODUCT_FOR_TRISTATE)])

    async def boom(product_keys, *, db=None):
        raise RuntimeError("evidence store down")

    async def absent(products):
        return None

    monkeypatch.setattr(apv, "fetch_evidence_for_keys", boom)
    monkeypatch.setattr(apv, "_fetch_enrichment_for_canonical", absent)
    row = await apv.build_agent_pdp_view_row("ck-1", refresh_source="t", db=_FakeDB())
    assert row is not None
    assert row["preserve_evidence"] is True
    assert row["preserve_enrichment"] is False


@pytest.mark.asyncio
async def test_the_sentinel_is_reached_through_the_real_swallow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive the REAL _fetch_enrichment_for_canonical, not a stub of it.

    The per-member `except ... continue` inside it is the actual source of the
    ambiguity; a test that replaces the whole function cannot show that a raising
    get_enrichment now yields FETCH_FAILED rather than None.
    """
    import db.product_enrichment as pe_module

    async def boom(*args, **kwargs):
        raise RuntimeError("connection reset by peer")

    monkeypatch.setattr(pe_module, "get_enrichment", boom)
    result = await apv._fetch_enrichment_for_canonical([dict(_PRODUCT_FOR_TRISTATE)])
    assert result is apv.FETCH_FAILED

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(pe_module, "get_enrichment", nothing)
    assert await apv._fetch_enrichment_for_canonical([dict(_PRODUCT_FOR_TRISTATE)]) is None
