"""services/store_audit_probe_identity.py: the product a commerce checkout probe is keyed to.

The catalog branch runs on real Postgres in tests/test_store_audit_scheduled_commerce_receipt_postgres.py;
here the database is a fake, so the rule itself is pinned without one.
"""

from __future__ import annotations

import hashlib

import pytest

from services import store_audit_probe_identity as identity


@pytest.mark.parametrize("raw, normalized", [
    ("https://Shop.Example/products/x/", "https://shop.example/products/x"),
    ("https://shop.example/products/x?variant=1#reviews", "https://shop.example/products/x"),
    ("https://shop.example:443/products/x", "https://shop.example/products/x"),
    ("https://shop.example:8443/products/x", "https://shop.example:8443/products/x"),
    ("https://shop.example", "https://shop.example/"),
    ("https://shop.example/", "https://shop.example/"),
])
def test_the_probe_url_is_normalized_like_the_route_endpoint(raw, normalized):
    assert identity.probe_url(raw) == normalized


def test_the_probe_url_matches_execution_routes_normalization():
    # The worker is handed endpoint_normalized as target_url; Store Readiness submits the raw URL.
    from db.audit_evidence import normalize_execution_route_identity

    for raw in ("https://Shop.Example/products/x/", "https://shop.example/", "https://shop.example:8443/a/b/",
                "https://shop.example:443/a"):
        _, _, endpoint = normalize_execution_route_identity(
            normalized_domain="shop.example", route_kind="storefront", endpoint=raw,
        )
        assert identity.probe_url(raw) == endpoint


class _Record:
    """Like a databases Postgres Record: indexable, and no dict .get."""

    def __init__(self, values):
        self._values = values

    def __getitem__(self, key):
        return self._values[key]


async def test_a_catalog_product_at_the_url_is_the_key(monkeypatch):
    seen = {}

    async def fetch_one(_query, values):
        seen.update(values)
        return _Record({"product_key": "sacr_product_1"})

    monkeypatch.setattr(identity.database, "fetch_one", fetch_one)

    assert await identity.probe_product_key(merchant_id="merchant:sacr-a", url="https://sacr-a.example/products/x/") == "sacr_product_1"
    # Matched with and without the trailing slash, on the normalized URL.
    assert seen["url"] == "https://sacr-a.example/products/x"
    assert seen["url_slash"] == "https://sacr-a.example/products/x/"
    assert seen["merchant_id"] == "merchant:sacr-a"


@pytest.mark.parametrize("row", [None, _Record({"product_key": None}), _Record({"product_key": ""})])
async def test_without_a_catalog_product_the_key_is_the_url_probe_digest(monkeypatch, row):
    async def fetch_one(_query, _values):
        return row

    monkeypatch.setattr(identity.database, "fetch_one", fetch_one)
    expected = "url_probe:" + hashlib.sha256(b"merchant:sacr-a|https://sacr-a.example/products/x").hexdigest()[:24]

    # Both spellings of one product URL reach one key (the two lanes used to disagree on the slash).
    for url in ("https://sacr-a.example/products/x", "https://SACR-A.example/products/x/?variant=2"):
        assert await identity.probe_product_key(merchant_id="merchant:sacr-a", url=url) == expected
    other = await identity.probe_product_key(merchant_id="merchant:sacr-b", url="https://sacr-a.example/products/x")
    assert other != expected


async def test_a_catalog_lookup_error_falls_back_to_the_url_probe_key(monkeypatch):
    async def fetch_one(_query, _values):
        raise RuntimeError("relation catalog_products does not exist")

    monkeypatch.setattr(identity.database, "fetch_one", fetch_one)
    key = await identity.probe_product_key(merchant_id="merchant:sacr-a", url="https://sacr-a.example/")
    assert key.startswith("url_probe:") and len(key) == len("url_probe:") + 24


def test_store_readiness_uses_this_rule():
    # tests/test_store_readiness.py patches sr._product_identity, so pin the alias here.
    from routes import store_readiness as sr

    assert sr._product_identity is identity.probe_product_key


def test_the_store_readiness_idempotency_key_is_what_the_receipt_recognises():
    key = identity.store_readiness_idempotency_key("audit-123")
    assert identity.is_store_readiness_probe(key)
    # The scheduled selector's real key, so a change to either side shows here.
    from datetime import datetime, timezone

    from jobs.scheduled_commerce_checkout_reprobe_job import merchant_reprobe_idempotency_key

    scheduled = merchant_reprobe_idempotency_key(
        merchant_id="merchant:sacr-a", scheduled_at=datetime(2026, 11, 4, 3, 45, tzinfo=timezone.utc),
    )
    assert not identity.is_store_readiness_probe(scheduled)
    assert not identity.is_store_readiness_probe(None)
