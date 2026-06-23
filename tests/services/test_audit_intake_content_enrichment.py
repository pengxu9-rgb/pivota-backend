"""ARO flywheel (slice 3): the merchant-paid audit enriches its OBSERVED index
seed with description + image, kept un-served (pdp_lifecycle_stage stays NULL) and
without touching the live connected-merchant ingest path."""

from services.audit_index_intake import (
    _CATALOG_INSERT_COLUMNS,
    _audit_description,
    _audit_image_url,
    _strip_html,
    audit_product_to_index_fields,
)


def test_strip_html():
    assert _strip_html("<p>Hello <b>world</b></p>") == "Hello world"
    assert _strip_html("  <div>  a\n\nb </div> ") == "a b"
    assert _strip_html(None) is None
    assert _strip_html("<br>") is None  # only tags -> empty -> None


def test_audit_description_prefers_structured_then_body_html():
    assert _audit_description({"description": "Clean desc"}) == "Clean desc"
    assert _audit_description({"body_html": "<p>From HTML</p>"}) == "From HTML"
    assert (
        _audit_description({"description": "", "body_html": "<p>fallback</p>"})
        == "fallback"
    )
    assert _audit_description({}) is None
    # capped at 5000 so a giant body_html can't bloat the row
    assert len(_audit_description({"body_html": "<p>" + "x" * 6000 + "</p>"})) == 5000


def test_audit_image_url_dict_and_list_forms():
    assert (
        _audit_image_url({"images": {"first_url": "https://x/a.jpg"}})
        == "https://x/a.jpg"
    )
    assert _audit_image_url({"images": ["https://x/b.jpg"]}) == "https://x/b.jpg"
    assert _audit_image_url({"images": [{"src": "https://x/c.jpg"}]}) == "https://x/c.jpg"
    assert _audit_image_url({"images": {}}) is None
    assert _audit_image_url({}) is None


def test_mapping_includes_description_and_image():
    audit = {
        "title": "Anua Heartleaf Toner",
        "vendor": "Anua",
        "pdp_url": "https://anua.com/products/toner",
        "attributes_raw": {
            "description": "77% heartleaf soothing toner",
            "images": {"first_url": "https://anua.com/img/toner.jpg"},
        },
    }
    fields = audit_product_to_index_fields("m1", audit)
    assert fields["description"] == "77% heartleaf soothing toner"
    assert fields["image_url"] == "https://anua.com/img/toner.jpg"
    # both must be in the persisted set so they actually reach catalog_products
    assert "description" in _CATALOG_INSERT_COLUMNS
    assert "image_url" in _CATALOG_INSERT_COLUMNS


def test_un_served_invariant_preserved():
    # The enrichment must NOT add any lifecycle/serving column — the seed stays
    # observed + un-served until the brand claims (consent gate).
    assert "pdp_lifecycle_stage" not in _CATALOG_INSERT_COLUMNS
    assert "sync_status" not in _CATALOG_INSERT_COLUMNS
    assert "serving_eligible" not in _CATALOG_INSERT_COLUMNS
