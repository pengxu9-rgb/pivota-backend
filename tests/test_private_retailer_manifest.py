from hashlib import sha256
import json
import pathlib

import pytest

from services import exact_retailer_cohorts as cohorts
from services.seller_identity import make_observed_retailer_id


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
                "merchant_id": make_observed_retailer_id("example-retailer.example"),
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


@pytest.mark.parametrize("invalid", ("unsafe_key", "wrong_host", "duplicate_gtin", "missing_sku", "duplicate_field",
                                     "not_the_host_retailer_id", "retailer_is_canonical_merchant"))
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
    elif invalid == "not_the_host_retailer_id":
        listing["merchant_id"] = "merch_obs_" + "4" * 16
    elif invalid == "retailer_is_canonical_merchant":
        manifest["canonical_merchant_id"] = listing["merchant_id"]
    source = json.dumps(manifest).encode()
    if invalid == "duplicate_field":
        source = source.replace(b'"version": 1', b'"version": 2, "version": 1')
    _configure(monkeypatch, tmp_path, source)
    with pytest.raises(ValueError, match="unreadable or invalid"):
        cohorts.load_private_manifest()



def _import_cohorts(env):
    # A fresh interpreter, so module identity in this test process is untouched.
    import os
    import subprocess
    import sys

    probe = (
        "import json, logging, sys\n"
        "logging.basicConfig(stream=sys.stderr, level=logging.ERROR)\n"
        "from services import exact_retailer_cohorts as c\n"
        "print(json.dumps({'cohort_c': c.RETAILER_COHORT_C is not None,"
        " 'listing_keys': sorted(c.EXACT_ONLY_LISTING_KEYS), 'family_urls': list(c.EXACT_ONLY_FAMILY_URLS)}))\n"
    )
    child_env = {key: value for key, value in os.environ.items()
                 if key not in (cohorts.COHORT_C_MANIFEST, cohorts.COHORT_C_MANIFEST_SHA256,
                                cohorts.COHORT_C_FLAG, cohorts.COHORT_C_WRITE_FLAG, "DATABASE_URL")}
    child_env.update(env)
    run = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, env=child_env,
                         cwd=str(pathlib.Path(cohorts.__file__).resolve().parents[1]), timeout=120)
    assert run.returncode == 0, run.stderr[-2000:]
    return json.loads(run.stdout.strip().splitlines()[-1]), run.stderr


def test_bad_manifest_at_import_disables_only_cohort_c(manifest, tmp_path):
    source = json.dumps(manifest).encode()
    path = tmp_path / "cohort.json"
    path.write_bytes(source + b" ")
    result, stderr = _import_cohorts({cohorts.COHORT_C_MANIFEST: str(path),
                                      cohorts.COHORT_C_MANIFEST_SHA256: sha256(source).hexdigest(),
                                      cohorts.COHORT_C_FLAG: "1"})
    assert result["cohort_c"] is False
    assert LISTING_KEY not in result["listing_keys"]
    assert "cohort C disabled" in stderr


def test_good_manifest_at_import_wires_cohort_c_exclusions(manifest, tmp_path):
    source = json.dumps(manifest).encode()
    path = tmp_path / "cohort.json"
    path.write_bytes(source)
    result, _ = _import_cohorts({cohorts.COHORT_C_MANIFEST: str(path),
                                 cohorts.COHORT_C_MANIFEST_SHA256: sha256(source).hexdigest()})
    assert result["cohort_c"] is True
    assert LISTING_KEY in result["listing_keys"]
    assert "https://example-retailer.example/products/lip-color" in result["family_urls"]
