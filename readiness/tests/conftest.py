from __future__ import annotations

import json
import os
import sys
from copy import deepcopy
from pathlib import Path

_repo_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_repo_root))

os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/pivota_test")
os.environ.setdefault("READINESS_ALLOW_UNAUTHED_DEV", "true")


def load_real_merchant_fixture() -> dict:
    fixture_path = _repo_root / "readiness" / "fixtures" / "real_merchant_alpha_shopify.json"
    return json.loads(fixture_path.read_text(encoding="utf-8"))


def build_live_shopify_products() -> list:
    from models.standard_product import StandardProduct

    fixture = load_real_merchant_fixture()
    products = []
    for row in fixture["products_cache_rows"]:
        product_data = deepcopy(row["product_data"])
        products.append(StandardProduct(**product_data))
    return products
