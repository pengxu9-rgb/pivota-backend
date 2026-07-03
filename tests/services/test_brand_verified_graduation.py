"""ADR-008 SLICE 2 — verify-to-serve graduation (pure, no DB).

Proves the graduation primitive + its wiring into verify_brand_claim:

  * scope is resolved NEUTRALLY (pdp_scope='merchant_owned', never the boosted
    multi_merchant_canonical) and live,
  * NO offer is minted (catalog_skus / catalog_offers never touched) — so a
    graduated product is index_eligible (citation floor) but serving/transact
    stay false,
  * the existing scorer is run on the brand's own content (writes
    product_quality_snapshot at the key the eligibility gate reads),
  * recompute is fired so index_eligible can flip TRUE,
  * the step is BEST-EFFORT (a failure never raises / never breaks verify),
  * flag OFF => verify behaves exactly as today (no graduation).

The quality-score behavior is asserted against the REAL deterministic scorer
(product_quality_service.preview_quality) to document the one dependency: a bare
brand-authored row (no enrichment) scores BELOW the 65 threshold; an enriched
one clears it. Graduation wires scoring correctly either way — the score is the
product's, not faked.
"""

from __future__ import annotations

import asyncio

import pytest

import services.brand_verified_graduation as grad
from services.index_pipeline_state_service import QUALITY_SCORE_THRESHOLD
from services.product_quality_service import build_quality_payload, preview_quality


# ---------------------------------------------------------------------------
# Fake DB: records UPDATE/SELECT against catalog_products. Never touches
# catalog_skus / catalog_offers (proving no offer is minted).
# ---------------------------------------------------------------------------

class _FakeDB:
    def __init__(self, rows):
        self._rows = rows
        self.executes = []

    async def fetch_all(self, query, params=None):
        self.executes.append(("fetch_all", query, params))
        if "FROM catalog_products" in query:
            return self._rows
        return []

    async def execute(self, query, params=None):
        self.executes.append(("execute", query, params))
        return 1


_GLOW_ROW = {
    "product_key": "pk_glow",
    "source_product_id": "ba-glow-serum-abc123",
    "content_key": "ck_glow",
    "title": "Glow Brightening Vitamin C Serum",
    "description": (
        "A lightweight brightening serum with vitamin C that fades dark "
        "spots and evens skin tone over time."
    ),
    "image_url": "https://img.example/glow.jpg",
    "brand": "Glow Co",
    "product_type": "serum",
    "category": "skincare",
}


def _patch_db_and_collaborators(monkeypatch, *, rows, enrichment=None):
    """Patch the DB + the three downstream collaborators graduation calls,
    capturing what each was invoked with."""
    db = _FakeDB(rows)
    monkeypatch.setattr(grad, "database", db)

    captured = {"scored": [], "apv": [], "recompute": []}

    # Scorer: capture the merchant/platform/product_id + payload, run the REAL
    # preview to record the resulting content_quality_score (no fake score).
    import services.product_quality_service as pqs

    async def _fake_full_quality_eval(**kw):
        captured["scored"].append(kw)
        return preview_quality(kw["payload"])

    monkeypatch.setattr(pqs, "full_quality_eval", _fake_full_quality_eval)

    # APV refresh: build-from-catalog path (no seed). Just record the call.
    import services.agent_pdp_view_assembler as apv

    async def _fake_apv(content_key, *, refresh_source, db=None):
        captured["apv"].append((content_key, refresh_source))
        return True

    monkeypatch.setattr(apv, "refresh_agent_pdp_view_for_content_key", _fake_apv)

    # Recompute: record content_key + reason.
    import services.index_pipeline_state_service as ips

    async def _fake_recompute(content_key, *, reason=None):
        captured["recompute"].append((content_key, reason))
        return True

    monkeypatch.setattr(ips, "recompute_serving_eligibility", _fake_recompute)

    # Enrichment overlay loader.
    import db.product_enrichment as dbe

    async def _fake_get_enrichment(merchant_id, platform, ppid, geo_code="default"):
        return enrichment

    monkeypatch.setattr(dbe, "get_enrichment", _fake_get_enrichment)

    return db, captured


# ---------------------------------------------------------------------------
# Core mechanics
# ---------------------------------------------------------------------------

def test_graduation_resolves_scope_neutral_merchant_owned(monkeypatch):
    db, captured = _patch_db_and_collaborators(monkeypatch, rows=[_GLOW_ROW])
    summary = asyncio.run(grad.graduate_brand_authored_products("merch_brand_x"))

    assert summary["products"] == 1
    assert summary["scope_resolved"] == 1

    # The scope UPDATE set merchant_owned + live, scoped to this product.
    scope_updates = [
        (q, p) for kind, q, p in db.executes
        if kind == "execute" and "UPDATE catalog_products" in q
    ]
    assert scope_updates, "expected a scope UPDATE"
    q, p = scope_updates[0]
    assert "pdp_scope = 'merchant_owned'" in q
    assert "sync_status = 'live'" in q
    # NEUTRALITY: never the +200-boosted scope.
    assert "multi_merchant_canonical" not in q
    assert p["source_product_id"] == "ba-glow-serum-abc123"


def test_graduation_does_not_mint_offer_or_set_group_id(monkeypatch):
    # HARD CONSTRAINT: no offer minted, no GTIN mis-merge. Graduation must never
    # touch catalog_skus / catalog_offers, and must never write product_group_id.
    db, _ = _patch_db_and_collaborators(monkeypatch, rows=[_GLOW_ROW])
    asyncio.run(grad.graduate_brand_authored_products("merch_brand_x"))

    all_sql = " ".join(q for _, q, _ in db.executes)
    assert "catalog_skus" not in all_sql
    assert "catalog_offers" not in all_sql
    assert "product_group_id" not in all_sql
    assert "product_group_members" not in all_sql


def test_graduation_runs_scorer_on_brand_content(monkeypatch):
    db, captured = _patch_db_and_collaborators(monkeypatch, rows=[_GLOW_ROW])
    asyncio.run(grad.graduate_brand_authored_products("merch_brand_x"))

    assert len(captured["scored"]) == 1
    kw = captured["scored"][0]
    # Snapshot lands at exactly the key the eligibility pqs join reads.
    assert kw["merchant_id"] == "merch_brand_x"
    assert kw["platform"] == "brand_authored"
    assert kw["platform_product_id"] == "ba-glow-serum-abc123"
    assert kw["geo_code"] == "default"
    # The payload carries the brand's own title/description.
    assert kw["payload"]["title_canonical"] == _GLOW_ROW["title"]


def test_graduation_fires_apv_and_recompute(monkeypatch):
    db, captured = _patch_db_and_collaborators(monkeypatch, rows=[_GLOW_ROW])
    asyncio.run(grad.graduate_brand_authored_products("merch_brand_x"))

    assert captured["apv"] == [("ck_glow", "brand_verified")]
    assert captured["recompute"] == [("ck_glow", "brand_verified")]


# ---------------------------------------------------------------------------
# Quality-score dependency (asserted against the REAL scorer — no fake score)
# ---------------------------------------------------------------------------

def test_bare_brand_authored_scores_below_threshold():
    # A bare brand-authored row (title+desc+image+brand+category, NO price, NO
    # enrichment) scores BELOW the index_eligible quality floor. Documents the
    # one dependency: graduation wires scoring correctly, but a thin product
    # won't clear the gate until enriched.
    payload = build_quality_payload(
        {
            "title": _GLOW_ROW["title"],
            "description": _GLOW_ROW["description"],
            "image_url": _GLOW_ROW["image_url"],
            "brand": _GLOW_ROW["brand"],
            "product_type": _GLOW_ROW["product_type"],
        },
        None,
    )
    score = preview_quality(payload)["content_quality_score"]
    assert score < QUALITY_SCORE_THRESHOLD


def test_enriched_brand_authored_clears_threshold():
    # The SAME product WITH a product_enrichment overlay (summary + bullets)
    # clears the 65 floor -> index_eligible can flip TRUE post-recompute.
    enrichment = {
        "summary_short": "A brightening vitamin C serum for dark spots and uneven tone.",
        "bullet_points": [
            "Fades dark spots",
            "Evens skin tone",
            "Lightweight, fast-absorbing",
        ],
        "usage_scenarios": ["morning skincare routine"],
    }
    payload = build_quality_payload(
        {
            "title": _GLOW_ROW["title"],
            "description": _GLOW_ROW["description"],
            "image_url": _GLOW_ROW["image_url"],
            "brand": _GLOW_ROW["brand"],
            "product_type": _GLOW_ROW["product_type"],
        },
        enrichment,
    )
    score = preview_quality(payload)["content_quality_score"]
    assert score >= QUALITY_SCORE_THRESHOLD


# ---------------------------------------------------------------------------
# Best-effort + idempotent
# ---------------------------------------------------------------------------

def test_graduation_is_best_effort_per_step(monkeypatch):
    # A failing scorer must NOT raise and must NOT block scope/apv/recompute.
    db, captured = _patch_db_and_collaborators(monkeypatch, rows=[_GLOW_ROW])

    import services.product_quality_service as pqs

    async def _boom(**kw):
        raise RuntimeError("scorer unavailable")

    monkeypatch.setattr(pqs, "full_quality_eval", _boom)

    summary = asyncio.run(grad.graduate_brand_authored_products("merch_brand_x"))
    assert summary["scope_resolved"] == 1
    assert summary["scored"] == 0  # the failing step didn't count
    # Downstream steps still ran.
    assert captured["apv"] == [("ck_glow", "brand_verified")]
    assert captured["recompute"] == [("ck_glow", "brand_verified")]


def test_graduation_no_merchant_is_noop():
    summary = asyncio.run(grad.graduate_brand_authored_products(""))
    assert summary["products"] == 0


def test_graduation_handles_multiple_products(monkeypatch):
    rows = [
        dict(_GLOW_ROW),
        {
            "product_key": "pk_cream",
            "source_product_id": "ba-rich-cream-def456",
            "content_key": "ck_cream",
            "title": "Rich Repair Night Cream",
            "description": "A nourishing night cream for dry, mature skin.",
            "image_url": "https://img.example/cream.jpg",
            "brand": "Glow Co",
            "product_type": "moisturizer",
            "category": "skincare",
        },
    ]
    db, captured = _patch_db_and_collaborators(monkeypatch, rows=rows)
    summary = asyncio.run(grad.graduate_brand_authored_products("merch_brand_x"))
    assert summary["products"] == 2
    assert summary["scope_resolved"] == 2
    assert len(captured["recompute"]) == 2
    assert {ck for ck, _ in captured["recompute"]} == {"ck_glow", "ck_cream"}
