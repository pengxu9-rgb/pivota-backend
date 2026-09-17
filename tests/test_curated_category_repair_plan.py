import pytest

from scripts.plan_curated_category_repair import plan_category_repair


def row():
    return {"product_key": "exact-key", "title": "Stored lip item", "product_type": "balm",
            "category_path": "beauty/makeup/lip/balm",
            "category_evidence": {"title": "Honey & Milk Lip Oil", "product_type": "Lip Care",
                                  "source_url": "https://eyurs.com/products/apieu-honey-milk-lip-oil",
                                  "observed_at": "2026-09-12T05:00:00Z"}}


def test_existing_leaf_requires_explicit_review_and_merchant_evidence():
    observed = row()
    assert plan_category_repair([observed])["proposed_changes"] == 0
    plan = plan_category_repair([observed], review_existing_leaves=True)
    assert plan["mode"] == "review_only"
    change = plan["changes"][0]
    assert change["expected_category_path"] == "beauty/makeup/lip/balm"
    assert change["proposed_category_path"] == "beauty/makeup/lip/oil"
    assert change["category_evidence"] == observed["category_evidence"]
    del observed["category_evidence"]
    with pytest.raises(ValueError, match="merchant category_evidence"):
        plan_category_repair([observed], review_existing_leaves=True)


def test_ambiguous_merchant_evidence_cannot_downgrade_a_leaf():
    observed = row()
    observed["category_evidence"]["product_type"] = "Lip & Cheek"
    assert plan_category_repair([observed], review_existing_leaves=True)["proposed_changes"] == 0
