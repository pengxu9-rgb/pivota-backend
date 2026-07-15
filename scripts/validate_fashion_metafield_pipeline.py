#!/usr/bin/env python3
"""End-to-end validation for the Phase O-5b fashion metafield pipeline.

What this exercises:

  Shopify Admin metafield (set by THIS script)
    → ShopifyProductAdapter.fetch_products (with SHOPIFY_METAFIELD_INGEST_ENABLED=true)
    → catalog_sync_service.ingest_standard_products
    → catalog_products.material / material_source / material_confidence
    → PIVOTA-Agent canonicalCatalogSearch SELECT
    → product.fashion_meta (provenance-tagged shape)
    → pdpBuilder.pickFashionMeta confidence gate
    → merchant-facing PDP response

Run AFTER:
  - PRs #540/#541/#542/#543/#544/#545/#546 (this repo) all merged + deployed
  - PIVOTA-Agent PRs #1391/#1393 merged + deployed
  - mig 094 applied (verified via schema_guard auto-apply)

Usage:
    # 1) Set SHOPIFY_METAFIELD_INGEST_ENABLED=true in the deployed env
    #    (use Railway dashboard or `railway variables --set`).
    # 2) Pick a real Shopify merchant + product:
    python scripts/validate_fashion_metafield_pipeline.py \\
        --merchant-id merch_xxxxxxxxxxxxxxxx \\
        --product-id 10064562225449 \\
        --material '100% test linen (PIPELINE VALIDATION)' \\
        --gateway-base https://agent.pivota.cc

    # 3) Default behavior is dry-clean: removes the test metafield on exit.
    #    Pass --no-cleanup to leave it in place for manual inspection.

Exit codes:
    0  = all 5 checks passed (PIPELINE LIVE)
    1  = pipeline broken — see step-by-step diagnostic
    2  = setup error (creds, network, args)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database  # noqa: E402
from services.shopify_products_sync import (  # noqa: E402
    _get_shopify_store_credentials,
    sync_shopify_products_for_merchant,
)

logger = logging.getLogger("validate_fashion_metafield_pipeline")

# Sentinel value so the script's writes are obvious in any audit. Prefix
# also lets the cleanup step pattern-match what to delete if the script
# is interrupted mid-run.
_VALIDATION_PREFIX = "VALIDATION_RUN"


def _make_validation_value(prefix: str, base: str) -> str:
    return f"{prefix}: {base}"


def _bold(s: str) -> str:
    return f"\033[1m{s}\033[0m"


def _green(s: str) -> str:
    return f"\033[32m{s}\033[0m"


def _red(s: str) -> str:
    return f"\033[31m{s}\033[0m"


def _yellow(s: str) -> str:
    return f"\033[33m{s}\033[0m"


# ============================================================
# Step 1 — set the metafield via Shopify Admin REST
# ============================================================

async def _shopify_upsert_metafield(
    *,
    shop_domain: str,
    access_token: str,
    product_id: str,
    namespace: str,
    key: str,
    value: str,
) -> Tuple[bool, Optional[int]]:
    """POST /admin/api/.../products/{id}/metafields.json to create the
    metafield, OR PUT to update if it already exists. Returns
    (ok, metafield_id)."""
    base = f"https://{shop_domain}/admin/api/2025-10"
    headers = {"X-Shopify-Access-Token": access_token, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        # See if it already exists.
        list_resp = await client.get(f"{base}/products/{product_id}/metafields.json", headers=headers)
        existing_id: Optional[int] = None
        if list_resp.status_code == 200:
            for mf in (list_resp.json() or {}).get("metafields", []):
                if mf.get("namespace") == namespace and mf.get("key") == key:
                    existing_id = int(mf["id"])
                    break
        body = {
            "metafield": {
                "namespace": namespace,
                "key": key,
                "value": value,
                "type": "single_line_text_field",
            }
        }
        if existing_id is not None:
            url = f"{base}/metafields/{existing_id}.json"
            resp = await client.put(url, headers=headers, json={"metafield": {"id": existing_id, "value": value, "type": "single_line_text_field"}})
        else:
            url = f"{base}/products/{product_id}/metafields.json"
            resp = await client.post(url, headers=headers, json=body)
        if resp.status_code not in (200, 201):
            logger.error("metafield upsert failed: %d %s", resp.status_code, resp.text[:300])
            return False, existing_id
        try:
            mf_id = int(resp.json()["metafield"]["id"])
            return True, mf_id
        except Exception:
            return True, existing_id


async def _shopify_delete_metafield(
    *,
    shop_domain: str,
    access_token: str,
    metafield_id: int,
) -> bool:
    headers = {"X-Shopify-Access-Token": access_token}
    url = f"https://{shop_domain}/admin/api/2025-10/metafields/{metafield_id}.json"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.delete(url, headers=headers)
    return resp.status_code in (200, 204)


# ============================================================
# Step 3/4 — verify catalog_products row
# ============================================================

async def _query_catalog_product(merchant_id: str, source_product_id: str) -> Optional[Dict[str, Any]]:
    if not getattr(database, "is_connected", False):
        await database.connect()
    row = await database.fetch_one(
        """
        SELECT product_key, pivota_signature_id, title, category_path,
               material, material_source, material_confidence,
               care, care_source, care_confidence,
               size_guide, size_guide_source, size_guide_confidence
        FROM catalog_products
        WHERE merchant_id = :merchant_id
          AND platform = 'shopify'
          AND source_product_id = :source_product_id
        LIMIT 1
        """,
        {"merchant_id": merchant_id, "source_product_id": source_product_id},
    )
    return dict(row) if row else None


# ============================================================
# Step 5 — gateway PDP roundtrip
# ============================================================

async def _gateway_get_pdp_v2(
    *,
    gateway_base: str,
    product_id: str,
    merchant_id: Optional[str] = None,
) -> Tuple[int, Dict[str, Any]]:
    """Hit the PIVOTA-Agent gateway /agent/shop/v1/invoke endpoint with
    get_pdp_v2. Returns (status_code, response_json)."""
    body: Dict[str, Any] = {
        "operation": "get_pdp_v2",
        "payload": {
            "product_ref": {"product_id": product_id},
            "include": ["offers", "product_overview"],
        },
    }
    if merchant_id:
        body["payload"]["product_ref"]["merchant_id"] = merchant_id
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{gateway_base.rstrip('/')}/agent/shop/v1/invoke", json=body)
    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, {"raw": resp.text[:500]}


def _read_fashion_meta_material(pdp_response: Dict[str, Any]) -> Optional[Any]:
    """Drill into the get_pdp_v2 response to find product.fashion_meta.material."""
    modules = pdp_response.get("modules") or []
    canonical = next(
        (m for m in modules if isinstance(m, dict) and m.get("type") == "canonical"),
        None,
    )
    if not canonical:
        return None
    pdp_payload = (canonical.get("data") or {}).get("pdp_payload") or {}
    product = pdp_payload.get("product") or {}
    fashion_meta = product.get("fashion_meta") or {}
    return fashion_meta.get("material")


# ============================================================
# Main orchestration
# ============================================================

async def _run(args: argparse.Namespace) -> int:
    print(_bold("\n=== Phase O-5b Fashion Metafield Pipeline Validation ===\n"))
    print(f"  merchant_id    : {args.merchant_id}")
    print(f"  product_id     : {args.product_id}")
    print(f"  namespace/key  : {args.namespace}/{args.key}")
    print(f"  metafield value: {args.material!r}")
    print(f"  gateway base   : {args.gateway_base}")
    print(f"  cleanup on exit: {not args.no_cleanup}")
    print()

    test_value = _make_validation_value(_VALIDATION_PREFIX, args.material)
    flag_state = os.getenv("SHOPIFY_METAFIELD_INGEST_ENABLED", "")
    if flag_state.lower() not in ("1", "true", "yes", "on"):
        print(_yellow(
            f"⚠️  SHOPIFY_METAFIELD_INGEST_ENABLED='{flag_state}' "
            f"in the LOCAL env. The deployed env is what matters for the sync; "
            f"this script doesn't check that. Confirm the flag is on in Railway "
            f"variables before running."
        ))
        print()

    # ---- Step 0: resolve credentials ----
    print(_bold("[Step 0] Resolve Shopify credentials"))
    try:
        creds = await _get_shopify_store_credentials(args.merchant_id)
        shop_domain = creds["shop_domain"]
        access_token = creds["access_token"]
        print(_green(f"  ✓ shop_domain={shop_domain}"))
    except Exception as exc:
        print(_red(f"  ✗ Could not resolve credentials: {exc}"))
        return 2

    metafield_id: Optional[int] = None
    try:
        # ---- Step 1: set the metafield ----
        print(_bold("\n[Step 1] Set Shopify metafield (POST/PUT)"))
        ok, metafield_id = await _shopify_upsert_metafield(
            shop_domain=shop_domain,
            access_token=access_token,
            product_id=args.product_id,
            namespace=args.namespace,
            key=args.key,
            value=test_value,
        )
        if not ok:
            print(_red("  ✗ Metafield upsert failed (see logs)."))
            return 1
        print(_green(f"  ✓ metafield_id={metafield_id} value={test_value!r}"))

        # ---- Step 2: trigger re-sync ----
        print(_bold("\n[Step 2] Trigger catalog sync (limit=5, ingest_catalog=True)"))
        sync_result = await sync_shopify_products_for_merchant(
            merchant_id=args.merchant_id,
            limit=5,
            per_page=5,
            max_pages=1,
            ingest_catalog=True,
        )
        sync_summary = {
            k: sync_result.get(k)
            for k in ("status", "fetched_count", "ingested_count", "catalog_stats")
            if k in sync_result
        }
        print(_green(f"  ✓ sync done: {json.dumps(sync_summary, default=str)[:300]}"))

        # Tiny buffer for any downstream async catalog event-handlers.
        await asyncio.sleep(2)

        # ---- Step 3: verify catalog_products row ----
        print(_bold("\n[Step 3] Verify catalog_products row"))
        row = await _query_catalog_product(args.merchant_id, args.product_id)
        if not row:
            print(_red("  ✗ No catalog_products row found for this merchant+product."))
            return 1
        target_col = args.key if args.key in ("material", "care", "size_guide") else "material"
        actual_value = row.get(target_col)
        actual_source = row.get(f"{target_col}_source")
        actual_confidence = row.get(f"{target_col}_confidence")
        print(f"  product_key            : {row.get('product_key')}")
        print(f"  pivota_signature_id    : {row.get('pivota_signature_id')}")
        print(f"  {target_col:<22} : {actual_value!r}")
        print(f"  {target_col}_source        : {actual_source!r}")
        print(f"  {target_col}_confidence    : {actual_confidence!r}")
        if actual_value != test_value:
            print(_red(f"  ✗ EXPECTED value={test_value!r} but found {actual_value!r}"))
            print(_yellow(
                "    Probable causes: "
                "(a) SHOPIFY_METAFIELD_INGEST_ENABLED not set true in the deployed env, "
                "(b) sync didn't re-touch this product because nothing changed (try --limit=250), "
                "(c) consumer extractor namespace/key mismatch — see services/fashion_field_payload_extractor.py."
            ))
            return 1
        if actual_source != "merchant_payload":
            print(_red(f"  ✗ EXPECTED source='merchant_payload' but found {actual_source!r}"))
            return 1
        if actual_confidence != 1.0:
            print(_red(f"  ✗ EXPECTED confidence=1.0 but found {actual_confidence!r}"))
            return 1
        print(_green(f"  ✓ catalog_products column populated correctly"))

        # ---- Step 4: gateway PDP roundtrip ----
        sig = row.get("pivota_signature_id")
        if not sig:
            print(_red("  ✗ Row has no pivota_signature_id; cannot hit gateway."))
            return 1
        print(_bold(f"\n[Step 4] Gateway get_pdp_v2 for sig={sig}"))
        status, pdp_response = await _gateway_get_pdp_v2(
            gateway_base=args.gateway_base, product_id=sig,
        )
        if status != 200:
            print(_red(f"  ✗ Gateway returned HTTP {status}"))
            print(json.dumps(pdp_response, default=str)[:400])
            return 1
        gateway_material = _read_fashion_meta_material(pdp_response)
        if gateway_material is None:
            print(_red("  ✗ product.fashion_meta.material is missing from PDP response"))
            print(_yellow(
                "    Probable causes: "
                "(a) gateway hasn't redeployed PRs #1391/#1393 yet, "
                "(b) trust gate dropped the value (check confidence)."
            ))
            return 1
        print(f"  fashion_meta.{target_col}: {gateway_material!r}")
        if isinstance(gateway_material, str):
            # Legacy flat-string shape (back-compat path).
            if gateway_material != test_value:
                print(_red(f"  ✗ EXPECTED {test_value!r} but found {gateway_material!r}"))
                return 1
        elif isinstance(gateway_material, dict):
            if gateway_material.get("value") != test_value:
                print(_red(f"  ✗ EXPECTED value={test_value!r} but found {gateway_material.get('value')!r}"))
                return 1
            if gateway_material.get("source") != "merchant_payload":
                print(_red(f"  ✗ EXPECTED source='merchant_payload' but found {gateway_material.get('source')!r}"))
                return 1
        else:
            print(_red(f"  ✗ Unexpected shape: {type(gateway_material).__name__}"))
            return 1
        print(_green("  ✓ gateway PDP carries the merchant_payload value end-to-end"))

        print(_bold(_green("\n=== PIPELINE LIVE — all 4 checks passed ===\n")))
        return 0

    finally:
        if metafield_id and not args.no_cleanup:
            print(_bold(f"\n[Cleanup] Deleting test metafield {metafield_id}"))
            ok = await _shopify_delete_metafield(
                shop_domain=shop_domain, access_token=access_token, metafield_id=metafield_id,
            )
            print(_green("  ✓ deleted") if ok else _yellow("  ⚠ delete failed; remove manually in Shopify Admin"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--merchant-id", required=True, help="Pivota merchant_id (e.g. merch_xxxxxxxxxxxxxxxx)")
    parser.add_argument("--product-id", required=True, help="Shopify product_id (numeric, e.g. 10064562225449)")
    parser.add_argument("--material", default="100% test linen", help="Material text the script will set in Shopify")
    parser.add_argument("--namespace", default="custom", help="Shopify metafield namespace (default 'custom')")
    parser.add_argument("--key", default="material", choices=("material", "care_instructions", "size_guide"),
                        help="Shopify metafield key + target catalog_products column root")
    parser.add_argument("--gateway-base", default="https://agent.pivota.cc",
                        help="PIVOTA-Agent gateway base URL")
    parser.add_argument("--no-cleanup", action="store_true",
                        help="Leave the test metafield in place after the run (default: delete on exit)")
    args = parser.parse_args()
    # Normalize key alias: catalog column for 'care_instructions' is 'care'.
    if args.key == "care_instructions":
        args.key = "care"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
