"""GET /agent/v1/products/external_seed/{id} must not publish the stored seed_data blob.

seed_data is the external-seed row's internal working state: price-read bookkeeping, variant refresh
state, canonical resets, read provenance, review aggregates, recall internals. The partner-key detail
route served it whole on the product (`product.seed_data`). The response now carries only the
allowlisted projection (services/seed_data_egress_projection.py); the stored row and anything cached
from it are never touched.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Dict
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from main import app


def _internal_seed_data() -> Dict[str, Any]:
    """A seed_data shaped like a refreshed production row: allowlisted fields next to internal ones."""
    return {
        # --- allowlisted ---
        "image_url": "https://example.com/img_0.jpg",
        "image_urls": ["https://example.com/img_1.jpg", "https://example.com/img_2.jpg"],
        "snapshot": {
            "description": "A light gel moisturiser.",
            "image_url": "https://example.com/snap.jpg",
            "variants": [
                {
                    "variant_id": "v1",
                    "sku": "SKU-1",
                    "option_name": "50ml",
                    "stock": 7,
                    "availability": "in_stock",
                    # internal, inside an allowlisted container
                    "price_amount": 12.34,
                    "price_read": {"at": "2026-11-06T00:00:00Z", "source": "page"},
                    "canonical_reset": True,
                },
            ],
            # internal snapshot fields
            "price_read_source": "jsonld",
            "variant_refresh_status": "stale",
            "raw_html_hash": "abc123",
        },
        "derived": {
            "recall": {
                "retrieval_summary": "Gel moisturiser for dry skin.",
                "embedding_version": "v9",
                "terms": ["gel", "moisturiser"],
            },
            "price_truth": {"status": "verified"},
        },
        "variants": [
            {
                "variant_id": "v1",
                "title": "50ml",
                "price_amount": 12.34,
                "price_currency": "USD",
                "availability": "in_stock",
                "stock": {"qty": 7, "source": "page"},  # non-scalar -> dropped
                "variant_gid": "gid://shop/ProductVariant/1",
                "variant_refresh_last_write": "2026-11-06T00:00:00Z",
            },
            {
                "variant_id": "v2",
                "title": "100ml",
                "price_amount": 19.99,
                "price_currency": "USD",
                "availability": "in_stock",
            },
        ],
        # --- internal ---
        "price_read": {"amount": 12.34, "currency": "USD", "at": "2026-11-06T00:00:00Z"},
        "price_read_source": "jsonld",
        "variant_refresh": {"status": "ok", "last_run": "2026-11-06T00:00:00Z"},
        "variant_refresh_status": "stale",
        "canonical_reset": {"at": "2026-11-01T00:00:00Z", "reason": "drift"},
        "read_provenance": {"read_at": "2026-11-06T00:00:00Z", "via": "handleless"},
        "refresh_status": "dead_page",
        "review_summary": {"rating": 4.5, "count": 120},
        "manual_overrides": {"price": True},
        "utm_template": "utm_source=pivota",
        "seller_ref": "seller_internal_1",
        "description": "top-level description",
    }


_ALLOWED_TOP = {"image_url", "imageUrl", "image", "images", "image_urls", "snapshot", "derived", "variants"}
_ALLOWED_SNAPSHOT = {"image_url", "imageUrl", "image", "images", "image_urls", "description", "variants"}
_ALLOWED_VARIANT = {
    "variant_id",
    "variant_gid",
    "selected_variant_id",
    "id",
    "sku",
    "option_name",
    "stock",
    "availability",
    "availability_status",
}


def _seed_row(seed_data: Any) -> Dict[str, Any]:
    return {
        "id": "eps_test_1",
        "external_product_id": "ext_test_1",
        "market": "US",
        "tool": "*",
        "utm_template": None,
        "partner_type": None,
        "disclosure_text": None,
        "destination_url": "https://example.com/product/1",
        "canonical_url": None,
        "domain": "example.com",
        "title": "Example External Product",
        "image_url": None,
        "price_amount": 12.34,
        "price_currency": "USD",
        "availability": "in_stock",
        "seed_data": seed_data,
        "status": "active",
        "notes": None,
        "created_by_employee_id": None,
        "attached_product_key": None,
        "attached_variant_id": None,
        "created_at": None,
        "updated_at": None,
    }


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _detail_response(monkeypatch: pytest.MonkeyPatch, client: TestClient, row: Dict[str, Any]):
    import routes.agent_api as agent_api_module

    async def fake_fetch_one(query: str, values=None):
        if "FROM external_product_seeds" in str(query):
            return row
        return None

    monkeypatch.setattr(agent_api_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(agent_api_module, "is_destination_domain_allowed", lambda *_a, **_k: True)
    monkeypatch.setattr(
        agent_api_module,
        "should_block_external_referral_runtime",
        AsyncMock(return_value=(False, None)),
    )
    return client.get("/agent/v1/products/external_seed/ext_test_1", headers={"X-API-Key": "test-api-key"})


def _get_detail(monkeypatch: pytest.MonkeyPatch, client: TestClient, row: Dict[str, Any]) -> Dict[str, Any]:
    res = _detail_response(monkeypatch, client, row)
    assert res.status_code == 200, res.text
    return res.json()["product"]


def _assert_only_allowlisted(seed_data: Dict[str, Any]) -> None:
    assert set(seed_data) <= _ALLOWED_TOP, set(seed_data) - _ALLOWED_TOP
    snapshot = seed_data.get("snapshot") or {}
    assert set(snapshot) <= _ALLOWED_SNAPSHOT, set(snapshot) - _ALLOWED_SNAPSHOT
    derived = seed_data.get("derived")
    if derived is not None:
        assert derived == {"recall": {"retrieval_summary": derived["recall"]["retrieval_summary"]}}
    for variants in (seed_data.get("variants") or [], snapshot.get("variants") or []):
        for v in variants:
            assert set(v) <= _ALLOWED_VARIANT, set(v) - _ALLOWED_VARIANT
            for value in v.values():
                assert isinstance(value, (str, int, float, bool)), value


def test_detail_route_serves_only_the_allowlisted_seed_data(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    product = _get_detail(monkeypatch, client, _seed_row(_internal_seed_data()))

    seed_data = product["seed_data"]
    _assert_only_allowlisted(seed_data)
    # Exactly what a consumer reads survives.
    assert seed_data == {
        "image_url": "https://example.com/img_0.jpg",
        "image_urls": ["https://example.com/img_1.jpg", "https://example.com/img_2.jpg"],
        "variants": [
            {"variant_id": "v1", "availability": "in_stock", "variant_gid": "gid://shop/ProductVariant/1"},
            {"variant_id": "v2", "availability": "in_stock"},
        ],
        "snapshot": {
            "image_url": "https://example.com/snap.jpg",
            "description": "A light gel moisturiser.",
            "variants": [
                {
                    "variant_id": "v1",
                    "sku": "SKU-1",
                    "option_name": "50ml",
                    "stock": 7,
                    "availability": "in_stock",
                }
            ],
        },
        "derived": {"recall": {"retrieval_summary": "Gel moisturiser for dry skin."}},
    }
    # No internal word anywhere in the projected blob.
    wire = json.dumps(seed_data)
    for word in (
        "price_read",
        "variant_refresh",
        "canonical_reset",
        "read_provenance",
        "refresh_status",
        "review_summary",
        "manual_overrides",
        "seller_internal_1",
        "embedding_version",
        "price_truth",
        "raw_html_hash",
        "price_amount",
        "dead_page",
        "handleless",
    ):
        assert word not in wire, word


def test_detail_route_product_fields_built_from_seed_data_are_unchanged(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """The projection runs after the builder: prices, variants, images and description are built from
    the full stored blob exactly as before."""
    product = _get_detail(monkeypatch, client, _seed_row(_internal_seed_data()))
    assert product["description"] == "top-level description"
    assert product["image_urls"] == ["https://example.com/img_1.jpg", "https://example.com/img_2.jpg"]
    assert [(v["variant_id"], v["price"]) for v in product["variants"]] == [("v1", 12.34), ("v2", 19.99)]


def test_detail_route_never_mutates_the_stored_row(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    seed_data = _internal_seed_data()
    row = _seed_row(seed_data)
    before = copy.deepcopy(row)

    _get_detail(monkeypatch, client, row)
    _get_detail(monkeypatch, client, row)  # a second read sees the same full blob

    assert row == before
    assert row["seed_data"] is seed_data


@pytest.mark.parametrize(
    "stored",
    [
        {"price_read": {"amount": 1}, "variant_refresh": {"status": "ok"}},  # nothing allowlisted
        {},
        None,
    ],
)
def test_detail_route_drops_seed_data_when_nothing_allowlisted_remains(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, stored: Any
) -> None:
    product = _get_detail(monkeypatch, client, _seed_row(stored))
    assert "seed_data" not in product


def test_detail_route_seed_data_stored_as_text_is_projected_too(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """Some deployments keep seed_data as TEXT; the builder parses it, and the projection applies to the
    parsed object."""
    product = _get_detail(monkeypatch, client, _seed_row(json.dumps(_internal_seed_data())))
    _assert_only_allowlisted(product["seed_data"])
    assert "price_read" not in json.dumps(product)


# --- the projection itself -------------------------------------------------------------------------

from services.seed_data_egress_projection import (  # noqa: E402
    project_product_seed_data,
    project_seed_data,
)


def test_projection_never_mutates_its_input_and_shares_no_containers() -> None:
    stored = _internal_seed_data()
    before = copy.deepcopy(stored)

    projected = project_seed_data(stored)

    assert stored == before
    assert projected is not stored
    assert projected["snapshot"] is not stored["snapshot"]
    assert projected["variants"][0] is not stored["variants"][0]
    # Editing the projection cannot reach the stored blob either.
    projected["snapshot"]["description"] = "changed"
    projected["variants"][0]["variant_id"] = "changed"
    assert stored == before


def test_projection_keeps_image_objects_only_as_their_url_strings() -> None:
    projected = project_seed_data(
        {
            "images": [
                {"url": "https://example.com/a.jpg", "alt": "x", "source": "crawl", "w": 100},
                {"width": 10},  # no URL -> dropped
                "https://example.com/b.jpg",
                ["nested"],  # not an image -> dropped
            ],
            "image": {"src": "https://example.com/c.jpg", "read_at": "2026-11-06"},
            "image_url": {"nested": {"url": "x"}},  # no string URL key -> dropped
            "imageUrl": "",
        }
    )
    assert projected == {
        "images": [{"url": "https://example.com/a.jpg"}, "https://example.com/b.jpg"],
        "image": {"src": "https://example.com/c.jpg"},
    }


def test_projection_text_fields_must_be_strings() -> None:
    assert (
        project_seed_data(
            {
                "snapshot": {"description": {"html": "<p>x</p>"}},
                "derived": {"recall": {"retrieval_summary": ["a", "b"]}},
            }
        )
        is None
    )


@pytest.mark.parametrize("stored", [None, "a string", ["a", "list"], 7, {}, {"price_read": {"a": 1}}])
def test_projection_of_nothing_allowlisted_is_none(stored: Any) -> None:
    assert project_seed_data(stored) is None


def test_product_without_seed_data_is_returned_as_the_same_object() -> None:
    product = {"id": "p1", "title": "x"}
    assert project_product_seed_data(product) is product


def test_product_projection_is_a_copy_and_drops_an_empty_projection() -> None:
    stored = {"price_read": {"a": 1}}
    product = {"id": "p1", "seed_data": stored}
    out = project_product_seed_data(product)
    assert out == {"id": "p1"}
    assert product == {"id": "p1", "seed_data": {"price_read": {"a": 1}}}
    assert product["seed_data"] is stored


# --- review follow-ups (be#2726 P3s) ------------------------------------------------------------------


def test_variant_fields_with_list_values_are_dropped() -> None:
    """S4: every allowlisted variant field must be a scalar; a list-valued one is dropped, and a variant
    left with nothing is dropped too."""
    projected = project_seed_data(
        {
            "variants": [
                {"variant_id": ["v1", "v2"], "sku": ["SKU-1"], "stock": [7], "availability": "in_stock"},
                {"variant_id": ["v3"], "title": "Retinol 0.3%"},
            ]
        }
    )
    assert projected == {"variants": [{"availability": "in_stock"}]}


def test_variant_identity_fields_each_survive_alone() -> None:
    """Every identity/stock name on the allowlist narrows to itself; a near-name does not ride along."""
    for field in ("selected_variant_id", "id", "availability_status", "variant_gid", "option_name", "sku"):
        projected = project_seed_data({"variants": [{field: "x", f"{field}_raw": "internal"}]})
        assert projected == {"variants": [{field: "x"}]}, field


def test_image_field_names_and_object_url_keys() -> None:
    """imageUrl at the top level and on the snapshot; image objects keep url / image_url / imageUrl / src
    and nothing else (I7: a non-string URL is dropped)."""
    projected = project_seed_data(
        {
            "imageUrl": "https://example.com/top.jpg",
            "snapshot": {"imageUrl": "https://example.com/snap.jpg"},
            "images": [
                {"image_url": "https://example.com/a.jpg", "width": 10},
                {"imageUrl": "https://example.com/b.jpg", "source": "crawl"},
                {"url": 123},
                {"url": ["https://example.com/c.jpg"]},
                {"src": {"href": "https://example.com/d.jpg"}},
            ],
        }
    )
    assert projected == {
        "imageUrl": "https://example.com/top.jpg",
        "images": [{"image_url": "https://example.com/a.jpg"}, {"imageUrl": "https://example.com/b.jpg"}],
        "snapshot": {"imageUrl": "https://example.com/snap.jpg"},
    }


def test_image_projection_leaves_its_input_unchanged() -> None:
    """C6."""
    stored = {
        "images": [{"url": "https://example.com/a.jpg", "alt": "x"}, "https://example.com/b.jpg"],
        "image": {"src": "https://example.com/c.jpg", "read_at": "2026-11-06"},
    }
    before = copy.deepcopy(stored)
    projected = project_seed_data(stored)
    assert stored == before
    assert projected["images"][0] is not stored["images"][0]
    projected["images"][0]["url"] = "changed"
    projected["image"]["src"] = "changed"
    assert stored == before


def test_a_snapshot_stored_as_json_text_is_parsed_then_projected() -> None:
    snapshot = {
        "description": "A light gel moisturiser.",
        "image_url": "https://example.com/snap.jpg",
        "variants": [{"variant_id": "v1", "title": "50ml", "price_amount": 12.0}],
        "price_read_source": "jsonld",
    }
    projected = project_seed_data({"snapshot": json.dumps(snapshot)})
    assert projected == {
        "snapshot": {
            "image_url": "https://example.com/snap.jpg",
            "description": "A light gel moisturiser.",
            "variants": [{"variant_id": "v1"}],
        }
    }
    for not_an_object in ("not json", "[1, 2]", '"a string"', "", "   "):
        assert project_seed_data({"snapshot": not_an_object}) is None, not_an_object


def test_a_pathologically_deep_text_snapshot_is_dropped_not_raised() -> None:
    """P3-4: json.loads raises RecursionError on very deep text; one bad row must not fail the page."""
    deep = '{"a":' + "[" * 5000 + "]" * 5000 + "}"
    assert project_seed_data({"title": "x", "snapshot": deep}) is None


def test_detail_route_projects_a_text_snapshot(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    seed_data = _internal_seed_data()
    seed_data["snapshot"] = json.dumps(seed_data["snapshot"])
    product = _get_detail(monkeypatch, client, _seed_row(seed_data))
    _assert_only_allowlisted(product["seed_data"])
    assert product["seed_data"]["snapshot"]["description"] == "A light gel moisturiser."
    assert "price_read_source" not in json.dumps(product["seed_data"])


def test_detail_route_fails_safe_when_the_projection_raises(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """A raise in the projection is a 500 that carries no seed_data, never the unprojected product."""
    import routes.agent_api as agent_api_module

    def boom(_product: Any) -> Any:
        raise RuntimeError("projection failed")

    monkeypatch.setattr(agent_api_module, "serve_detail_product", boom)
    res = _detail_response(monkeypatch, client, _seed_row(_internal_seed_data()))
    assert res.status_code == 500
    for word in ("seed_data", "price_read", "variant_refresh", "seller_internal_1"):
        assert word not in res.text, word


def test_detail_route_with_a_lone_surrogate_in_a_text_snapshot_still_renders(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """be#2730 review P2-1: the kept snapshot.description carried a raw lone surrogate and the response
    failed to encode (500). Kept strings replace it with U+FFFD."""
    seed_data = _internal_seed_data()
    seed_data["snapshot"] = json.dumps({"description": "Glow \ud83d", "image_url": "https://example.com/\udc00.jpg"})
    res = _detail_response(monkeypatch, client, _seed_row(seed_data))
    assert res.status_code == 200, res.text[:300]
    snapshot = res.json()["product"]["seed_data"]["snapshot"]
    assert snapshot == {"description": "Glow \ufffd", "image_url": "https://example.com/\ufffd.jpg"}


def test_detail_projection_replaces_lone_surrogates_in_every_kept_string() -> None:
    projected = project_seed_data(
        {
            "image_urls": ["https://example.com/\ud83d.jpg"],
            "images": [{"url": "https://example.com/\udc00.jpg"}],
            "variants": [{"variant_id": "v\ud83d", "option_name": "Shade \udc00"}],
            "derived": {"recall": {"retrieval_summary": "Gel \ud83d"}},
        }
    )
    wire = json.dumps(projected, ensure_ascii=False)
    wire.encode("utf-8")  # encodes
    assert wire.count("\ufffd") == 5
