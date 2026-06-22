"""P0.2 deposit-gate tests — services.catalog_identity.resolve_deposit_content_key.

The gate exists because content_key is nullable + DELIBERATELY non-unique
(db/migrations/083): no-GTIN SKUs collide on brand+title, so depositing
entity-scoped rows against an unresolved key cross-contaminates entities.
"""

from services.catalog_identity import (
    DEPOSIT_BASIS_GTIN,
    DEPOSIT_BASIS_IDENTITY,
    DEPOSIT_BASIS_REVIEWED,
    DEPOSIT_BASIS_UNRESOLVED,
    make_content_key,
    resolve_deposit_content_key,
    resolve_deposit_content_key_for_row,
)


def test_gtin_present_is_depositable_on_gtin_basis():
    r = resolve_deposit_content_key(
        brand="Anua", title="Heartleaf Toner", gtin="8809530160053"
    )
    assert r.basis == DEPOSIT_BASIS_GTIN
    assert r.is_depositable
    assert r.content_key == make_content_key("Anua", "Heartleaf Toner", "8809530160053")


def test_no_gtin_low_confidence_is_unresolved_and_not_depositable():
    r = resolve_deposit_content_key(
        brand="Anua", title="Heartleaf Toner", gtin=None, identity_confidence=0.4
    )
    assert r.basis == DEPOSIT_BASIS_UNRESOLVED
    assert not r.is_depositable


def test_no_gtin_high_confidence_identity_is_depositable():
    ck = make_content_key("Anua", "Heartleaf Toner")
    r = resolve_deposit_content_key(
        brand="Anua",
        title="Heartleaf Toner",
        gtin=None,
        existing_content_key=ck,
        identity_confidence=0.95,
    )
    assert r.basis == DEPOSIT_BASIS_IDENTITY
    assert r.is_depositable
    assert r.content_key == ck


def test_reviewed_state_is_depositable_without_gtin_or_confidence():
    ck = make_content_key("Anua", "Heartleaf Toner")
    r = resolve_deposit_content_key(
        brand="Anua",
        title="Heartleaf Toner",
        existing_content_key=ck,
        review_state="reviewed",
    )
    assert r.basis == DEPOSIT_BASIS_REVIEWED
    assert r.is_depositable


def test_two_distinct_products_same_brand_title_no_gtin_both_unresolved():
    # The cross-contamination scenario: collide on brand+title, no GTIN.
    a = resolve_deposit_content_key(
        brand="X", title="Serum", gtin=None, identity_confidence=0.2
    )
    b = resolve_deposit_content_key(
        brand="X", title="Serum", gtin=None, identity_confidence=0.2
    )
    assert not a.is_depositable and not b.is_depositable
    # They share a key — which is exactly why neither may deposit.
    assert a.content_key == b.content_key


def test_gtin_uses_existing_node_key_not_a_competing_mint():
    # Node identity graph is authoritative: when a content_key already exists,
    # hang the deposit on it rather than minting a divergent key.
    existing = "ck_deadbeefdeadbeefdeadbeefdeadbeef"
    r = resolve_deposit_content_key(
        brand="Anua",
        title="Heartleaf Toner",
        gtin="8809530160053",
        existing_content_key=existing,
    )
    assert r.basis == DEPOSIT_BASIS_GTIN
    assert r.content_key == existing


def test_threshold_is_env_overridable(monkeypatch):
    ck = make_content_key("Anua", "Heartleaf Toner")
    monkeypatch.setenv("CONTENT_KEY_DEPOSIT_MIN_CONFIDENCE", "0.99")
    r = resolve_deposit_content_key(
        brand="Anua",
        title="Heartleaf Toner",
        existing_content_key=ck,
        identity_confidence=0.95,  # below the raised 0.99 bar
    )
    assert r.basis == DEPOSIT_BASIS_UNRESOLVED


def test_empty_brand_or_title_yields_no_key_and_not_depositable():
    r = resolve_deposit_content_key(brand="", title="Serum", gtin="8809530160053")
    assert r.content_key is None
    assert not r.is_depositable


def test_row_wrapper_reads_common_fields():
    ck = make_content_key("Anua", "Heartleaf Toner")
    row = {
        "brand": "Anua",
        "title": "Heartleaf Toner",
        "content_key": ck,
        "gtin": "8809530160053",
    }
    r = resolve_deposit_content_key_for_row(row)
    assert r.is_depositable
    assert r.basis == DEPOSIT_BASIS_GTIN
    assert r.content_key == ck
