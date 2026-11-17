from hashlib import sha256
import json

import pytest

from services import exact_retailer_cohorts as cohorts


def _gtin(number):
    body = f"{number:013d}"
    total = sum(int(digit) * (3 if i % 2 == 0 else 1) for i, digit in enumerate(reversed(body)))
    return f"{body}{-total % 10}"


LISTING_KEY = "ext:retailer:" + "a" * 32


@pytest.fixture
def manifest(monkeypatch):
    for setting in (cohorts.COHORT_C_MANIFEST, cohorts.COHORT_C_MANIFEST_SHA256,
                    cohorts.COHORT_C_FLAG, cohorts.COHORT_C_WRITE_FLAG):
        monkeypatch.delenv(setting, raising=False)
    variants = {str(71000000000000 + i): [str(72000000000000 + i), _gtin(10000000 + i)] for i in range(8)}
    return {
        "version": 1,
        "canonical_product_key": "ext:fixture-maker-lip-color::1234abcd",
        "canonical_merchant_id": "merch_obs_" + "1" * 16,
        "canonical_group_id": "pg_" + "2" * 32,
        "canonical_content_key": "ck_" + "2" * 32,
        "canonical_signature_id": "sig_" + "3" * 32,
        "listings": {
            LISTING_KEY: {
                "merchant_id": "merch_obs_" + "4" * 16,
                "host": "example-retailer.example",
                "native_product_id": "73000000000000",
                "product_path": "/products/lip-color",
                "variants": variants,
            },
        },
        "refresh": {
            "storefront": "www.example-retailer.example",
            "vendor": "Fixture Maker",
            "retailer_name": "Example Retailer",
            "category_path": "beauty/makeup/lip/lipstick",
            "sku_by_variant": {native: f"SKU{i}" for i, native in enumerate(variants)},
        },
    }


def _configure(monkeypatch, tmp_path, source):
    path = tmp_path / "cohort.json"
    path.write_bytes(source)
    monkeypatch.setenv(cohorts.COHORT_C_MANIFEST, str(path))
    monkeypatch.setenv(cohorts.COHORT_C_MANIFEST_SHA256, sha256(source).hexdigest())
    return path


def test_private_manifest_inactive_without_settings(manifest):
    assert cohorts.load_private_manifest() is None


@pytest.mark.parametrize("flag", (cohorts.COHORT_C_FLAG, cohorts.COHORT_C_WRITE_FLAG))
def test_private_manifest_required_when_enabled(manifest, monkeypatch, flag):
    monkeypatch.setenv(flag, "1")
    with pytest.raises(ValueError, match="path and SHA-256 pin"):
        cohorts.load_private_manifest()


def test_private_manifest_keeps_reviewed_variant_order(manifest, monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, json.dumps(manifest).encode())
    loaded = cohorts.load_private_manifest()
    assert loaded == manifest
    assert tuple(loaded["listings"][LISTING_KEY]["variants"]) == tuple(manifest["refresh"]["sku_by_variant"])


def test_private_manifest_refuses_changed_bytes(manifest, monkeypatch, tmp_path):
    path = _configure(monkeypatch, tmp_path, json.dumps(manifest).encode())
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="unreadable or invalid"):
        cohorts.load_private_manifest()


@pytest.mark.parametrize("invalid", ("unsafe_key", "wrong_host", "duplicate_gtin", "missing_sku", "duplicate_field"))
def test_private_manifest_refuses_invalid_data(manifest, monkeypatch, tmp_path, invalid):
    listing = manifest["listings"][LISTING_KEY]
    first, second = list(listing["variants"])[:2]
    if invalid == "unsafe_key":
        manifest["listings"] = {"ext:retailer:a'": listing}
    elif invalid == "wrong_host":
        manifest["refresh"]["storefront"] = "other-retailer.example"
    elif invalid == "duplicate_gtin":
        listing["variants"][first][1] = listing["variants"][second][1]
    elif invalid == "missing_sku":
        del manifest["refresh"]["sku_by_variant"][first]
    source = json.dumps(manifest).encode()
    if invalid == "duplicate_field":
        source = source.replace(b'"version": 1', b'"version": 2, "version": 1')
    _configure(monkeypatch, tmp_path, source)
    with pytest.raises(ValueError, match="unreadable or invalid"):
        cohorts.load_private_manifest()
