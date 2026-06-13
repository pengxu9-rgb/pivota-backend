"""Read-time enrichment in _fetch_beauty_vertical_payload.

The agent-decision-grade `find` dimension needs category_kind + fit attributes
(concerns / key actives). 2,195 of 2,198 categorized products have NO
beauty_product_profiles row, and the payload used to be FROM bpp -> the whole
payload (incl. category_kind, which actually lives on catalog_products) was
dropped for them. Now the payload anchors on catalog_products and derives
concerns + key actives from product text the same deterministic way the
skincare_format / spf fields are already derived. These tests assert the
no-bpp-row path surfaces those fields.
"""

import pytest

import services.pivot_query_service as module

pytestmark = pytest.mark.asyncio


def _profile_row_cp_only() -> dict:
    """A joined row where the bpp side is all NULL (no profile) and only the
    catalog_products columns are present — the 99.9% case."""
    return {
        "taxonomy_json": None,
        "concerns_json": None,
        "claims_json": None,
        "routine_phase": None,
        "benefits_json": None,
        "profile_payload": None,
        "evidence_profile": None,
        "required_disclaimers": None,
        "category_kind": "skincare",
        "cp_title": "Brightening Vitamin C Serum",
        "cp_product_type": "Serum",
        "cp_category_path": "beauty/skincare/serums",
        "cp_description": "A niacinamide serum that brightens dull skin.",
    }


def _install_db(monkeypatch, *, profile_row, ingredient_row=None):
    async def fake_fetch_one(query: str, params: dict):
        if "beauty_product_profiles" in query:
            return profile_row
        if "beauty_sku_ingredients" in query:
            return ingredient_row
        return None  # usage guide

    async def fake_fetch_all(query: str, params: dict):
        return []

    monkeypatch.setattr(module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(module.database, "fetch_all", fake_fetch_all)


async def test_category_kind_surfaces_without_a_profile_row(monkeypatch) -> None:
    _install_db(monkeypatch, profile_row=_profile_row_cp_only())
    payload = await module._fetch_beauty_vertical_payload("pk1", "sk1")
    # Was None before the catalog_products-anchored fix (bpp row absent).
    assert payload["category_kind"] == "skincare"


async def test_concerns_derived_from_text_when_unprofiled(monkeypatch) -> None:
    _install_db(monkeypatch, profile_row=_profile_row_cp_only())
    payload = await module._fetch_beauty_vertical_payload("pk1", "sk1")
    # "brightens" -> dullness, "dull" -> dullness, "niacinamide serum" is text.
    assert "dullness" in payload["concerns"]


async def test_key_actives_derived_from_text_when_unprofiled(monkeypatch) -> None:
    _install_db(monkeypatch, profile_row=_profile_row_cp_only())
    payload = await module._fetch_beauty_vertical_payload("pk1", "sk1")
    labels = [a.get("label", "").lower() for a in payload["active_ingredients"]]
    assert any("niacinamide" in lbl for lbl in labels)
    # No INCI -> derived from product text.
    assert all(a.get("source") == "text" for a in payload["active_ingredients"])


async def test_authored_profile_values_win_over_derivation(monkeypatch) -> None:
    """When a bpp row DOES carry concerns, they are used as-is (no override)."""
    row = _profile_row_cp_only()
    row["concerns_json"] = ["sensitivity"]
    _install_db(monkeypatch, profile_row=row)
    payload = await module._fetch_beauty_vertical_payload("pk1", "sk1")
    assert payload["concerns"] == ["sensitivity"]


async def test_structured_ingredient_row_wins_over_derivation(monkeypatch) -> None:
    row_ing = {
        "raw_inci": None,
        "normalized_ingredients_json": None,
        "active_ingredients_json": [{"label": "Retinol", "source": "inci"}],
        "concentration_notes_json": None,
    }
    _install_db(monkeypatch, profile_row=_profile_row_cp_only(), ingredient_row=row_ing)
    payload = await module._fetch_beauty_vertical_payload("pk1", "sk1")
    labels = [a.get("label") for a in payload["active_ingredients"]]
    assert "Retinol" in labels  # structured row used; not overwritten by text derivation


async def test_uncategorized_product_derives_nothing(monkeypatch) -> None:
    """No category_kind -> no concern/active derivation (we never guess a category)."""
    row = _profile_row_cp_only()
    row["category_kind"] = None
    _install_db(monkeypatch, profile_row=row)
    payload = await module._fetch_beauty_vertical_payload("pk1", "sk1")
    assert payload["category_kind"] is None
    assert payload["concerns"] == []
    assert payload["active_ingredients"] == []


async def test_supplement_category_kind_surfaces_without_a_profile_row(monkeypatch) -> None:
    """The Ownist supplement repro: supplements have no FIT-concern vocabulary, so
    enrichment never writes a beauty_product_profiles row for them. category_kind
    must still surface from catalog_products so the decision-grade `find` dimension
    passes -- before the catalog_products-anchored fix it came back
    'uncategorized'/find=fail. Concerns stay empty (the supplement concern vocab is
    intentionally empty pending the supplement module -- we never fabricate one)."""
    row = _profile_row_cp_only()
    row["category_kind"] = "supplement"
    row["cp_title"] = "Triple Collagen Orange"
    row["cp_product_type"] = "Supplement"
    row["cp_category_path"] = None
    row["cp_description"] = "Marine collagen peptides for skin elasticity."
    _install_db(monkeypatch, profile_row=row)
    payload = await module._fetch_beauty_vertical_payload(
        "prod::external_seed::external_seed::ownist-triple-collagen-orange", None
    )
    # Non-empty payload: category_kind surfaces even with no bpp row.
    assert payload["category_kind"] == "supplement"
    # No supplement concern vocabulary -> no concerns, and we never guess.
    assert payload["concerns"] == []
