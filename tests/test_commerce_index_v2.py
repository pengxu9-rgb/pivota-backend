from datetime import datetime, timezone

from services.commerce_index_v2 import (
    FieldObservation,
    commerce_index_v2_enabled,
    commerce_index_v2_enabled_for_merchant,
    plan_field_change,
    source_kind_for_system,
)


def _observation(*, family: str, source_kind: str = "merchant_api", value=None) -> FieldObservation:
    return FieldObservation(
        entity_type="sku",
        entity_id="sku_123",
        field_family=family,
        field_key="value",
        value=value if value is not None else {"value": "new"},
        source_system="test_source",
        source_kind=source_kind,
        observed_at=datetime(2026, 8, 22, tzinfo=timezone.utc),
        confidence=0.95,
    )


def test_unchanged_field_does_not_create_publication_work() -> None:
    observation = _observation(family="taxonomy", value={"category": "serum"})

    plan = plan_field_change(observation, previous_value={"category": "serum"})

    assert plan.changed is False
    assert plan.publication_targets == ()
    assert plan.reason == "value_unchanged"


def test_taxonomy_change_updates_search_graph_and_insights() -> None:
    plan = plan_field_change(
        _observation(family="taxonomy", value={"category": "moisturizer"}),
        previous_value={"category": "serum"},
    )

    assert plan.changed is True
    assert plan.review_required is False
    assert plan.publication_targets == ("search_index", "relation_graph", "product_insights")


def test_authorized_price_change_requires_checkout_validation_not_graph_rebuild() -> None:
    plan = plan_field_change(
        _observation(family="price", value={"currency": "USD", "amount": "25.00"}),
        previous_value={"currency": "USD", "amount": "30.00"},
    )

    assert plan.review_required is False
    assert plan.publication_targets == ("search_index", "checkout_validation", "product_insights")


def test_public_crawl_price_is_held_for_review() -> None:
    plan = plan_field_change(
        _observation(
            family="price",
            source_kind="public_crawl",
            value={"currency": "USD", "amount": "25.00"},
        ),
        previous_value={"currency": "USD", "amount": "30.00"},
    )

    assert plan.changed is True
    assert plan.review_required is True
    assert plan.publication_targets == ()
    assert plan.reason == "checkout_sensitive_source_below_authority_threshold"


def test_unknown_source_is_conservative_and_feature_gate_is_opt_in(monkeypatch) -> None:
    monkeypatch.delenv("COMMERCE_INDEX_V2_ENABLED", raising=False)

    assert commerce_index_v2_enabled() is False
    assert source_kind_for_system("unverified_partner") == "public_crawl"
    assert source_kind_for_system("shopify_products_sync") == "merchant_api"

    monkeypatch.setenv("COMMERCE_INDEX_V2_ENABLED", "true")
    assert commerce_index_v2_enabled() is True
    assert commerce_index_v2_enabled_for_merchant("merchant_123") is False
    monkeypatch.setenv("COMMERCE_INDEX_V2_MERCHANT_ALLOWLIST", "merchant_123, merchant_456")
    assert commerce_index_v2_enabled_for_merchant("merchant_123") is True
    assert commerce_index_v2_enabled_for_merchant("merchant_999") is False
