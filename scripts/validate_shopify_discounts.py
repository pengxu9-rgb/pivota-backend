#!/usr/bin/env python3
"""Shopify discount validation harness.

This script drives Pivota's quote API and captures redacted evidence. It does
not use production credentials by default. Live quote/cart validation requires
an explicit flag, and order creation is disabled unless both an explicit CLI flag
and env gate are present for a dev/test target.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    description: str
    discount_codes: List[str]
    quantity: int = 1
    items_env: Optional[str] = None
    expected: str = "record_only"
    env_required: Optional[str] = None
    required_code_count: int = 0
    boundary: bool = False


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate Shopify discounts against an explicitly approved Pivota merchant.")
    parser.add_argument("--base-url", default=os.getenv("SHOPIFY_DISCOUNT_TEST_BASE_URL") or os.getenv("PIVOTA_BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--merchant-id", default=os.getenv("SHOPIFY_DISCOUNT_TEST_MERCHANT_ID"))
    parser.add_argument("--agent-api-key", default=os.getenv("SHOPIFY_DISCOUNT_TEST_AGENT_API_KEY") or os.getenv("PIVOTA_AGENT_API_KEY"))
    parser.add_argument("--product-id", default=os.getenv("SHOPIFY_DISCOUNT_TEST_PRODUCT_ID", "shopify_test_product"))
    parser.add_argument("--variant-id", default=os.getenv("SHOPIFY_DISCOUNT_TEST_VARIANT_ID"))
    parser.add_argument("--customer-email", default=os.getenv("SHOPIFY_DISCOUNT_TEST_CUSTOMER_EMAIL", "shopify-discount-test@example.com"))
    parser.add_argument("--currency", default=os.getenv("SHOPIFY_DISCOUNT_TEST_CURRENCY", "USD"))
    parser.add_argument("--allow-dev-store", action="store_true", help="Acknowledge that only a dev/test Shopify store is targeted.")
    parser.add_argument(
        "--allow-live-no-order",
        action="store_true",
        help="Acknowledge live quote/cart validation is approved. Blocks order creation and payment paths.",
    )
    parser.add_argument(
        "--allow-live-readonly",
        action="store_true",
        help="Alias for --allow-live-no-order.",
    )
    parser.add_argument("--allow-remote", action="store_true", help="Allow non-localhost base URLs.")
    parser.add_argument("--include-order-create", action="store_true", help="Also call agent order create for a successful quote. Dev/test only.")
    parser.add_argument("--output-dir", default=os.getenv("SHOPIFY_DISCOUNT_VALIDATION_OUTPUT_DIR"))
    return parser.parse_args()


def _now_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _redact_url_query(value: str) -> str:
    lowered = value.lower()
    if not any(marker in lowered for marker in ("?key=", "&key=", "?token=", "&token=", "access_token=")):
        return value
    try:
        parts = urlsplit(value)
    except Exception:
        return value
    if not parts.query:
        return value
    sensitive_keys = {"key", "token", "access_token", "checkout_token", "cart_token"}
    pairs = []
    changed = False
    for key, raw in parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() in sensitive_keys:
            pairs.append((key, "[redacted]"))
            changed = True
        else:
            pairs.append((key, raw))
    if not changed:
        return value
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs, doseq=True), parts.fragment))


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            key = str(k).lower()
            if any(token in key for token in ("token", "api_key", "authorization", "access_token", "secret")):
                out[k] = "[redacted]"
            elif key == "checkout_url" and isinstance(v, str):
                out[k] = "[redacted_checkout_url]"
            elif key == "engine_ref" and isinstance(v, str) and "cart" in v.lower():
                out[k] = "[redacted_cart_reference]"
            elif key in {"customer_email", "email"} and isinstance(v, str) and "@" in v:
                local, _, domain = v.partition("@")
                out[k] = f"{local[:2]}***@{domain}"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, str):
        return _redact_url_query(value)
    return value


def _shipping_address() -> Dict[str, Any]:
    return {
        "name": os.getenv("SHOPIFY_DISCOUNT_TEST_SHIPPING_NAME", "Shopify Discount Test"),
        "address_line1": os.getenv("SHOPIFY_DISCOUNT_TEST_SHIPPING_ADDRESS1", "150 Elgin St"),
        "address_line2": os.getenv("SHOPIFY_DISCOUNT_TEST_SHIPPING_ADDRESS2", ""),
        "city": os.getenv("SHOPIFY_DISCOUNT_TEST_SHIPPING_CITY", "Ottawa"),
        "state": os.getenv("SHOPIFY_DISCOUNT_TEST_SHIPPING_STATE", "ON"),
        "postal_code": os.getenv("SHOPIFY_DISCOUNT_TEST_SHIPPING_POSTAL_CODE", "K2P1L4"),
        "country": os.getenv("SHOPIFY_DISCOUNT_TEST_SHIPPING_COUNTRY", "CA"),
        "phone": os.getenv("SHOPIFY_DISCOUNT_TEST_SHIPPING_PHONE") or None,
    }


def _scenario_catalog() -> List[Scenario]:
    return [
        Scenario(
            "SFD-001",
            "valid amount-off code",
            [os.getenv("SHOPIFY_DISCOUNT_TEST_AMOUNT_CODE", "").strip()],
            expected="valid_code_discount",
            env_required="SHOPIFY_DISCOUNT_TEST_AMOUNT_CODE",
        ),
        Scenario(
            "SFD-002",
            "invalid code rejection",
            [os.getenv("SHOPIFY_DISCOUNT_TEST_INVALID_CODE", "PIVOTA_INVALID_TEST_CODE").strip()],
            expected="invalid_code_rejected",
        ),
        Scenario(
            "SFD-003",
            "automatic amount-off discount",
            [],
            items_env="SHOPIFY_DISCOUNT_TEST_AUTOMATIC_ITEMS_JSON",
            expected="automatic_discount",
            env_required="SHOPIFY_DISCOUNT_TEST_AUTOMATIC_ENABLED",
        ),
        Scenario(
            "SFD-004",
            "Buy X Get Y code",
            [os.getenv("SHOPIFY_DISCOUNT_TEST_BXGY_CODE", "").strip()],
            quantity=int(os.getenv("SHOPIFY_DISCOUNT_TEST_BXGY_QUANTITY", "2") or "2"),
            items_env="SHOPIFY_DISCOUNT_TEST_BXGY_ITEMS_JSON",
            expected="valid_code_discount",
            env_required="SHOPIFY_DISCOUNT_TEST_BXGY_CODE",
        ),
        Scenario(
            "SFD-005",
            "free shipping code with delivery evidence",
            [os.getenv("SHOPIFY_DISCOUNT_TEST_FREE_SHIPPING_CODE", "").strip()],
            expected="free_shipping_partial_or_applied",
            env_required="SHOPIFY_DISCOUNT_TEST_FREE_SHIPPING_CODE",
        ),
        Scenario(
            "SFD-006",
            "new-customer or segment eligibility",
            [os.getenv("SHOPIFY_DISCOUNT_TEST_NEW_CUSTOMER_CODE", "").strip()],
            items_env="SHOPIFY_DISCOUNT_TEST_NEW_CUSTOMER_ITEMS_JSON",
            expected="customer_eligibility_evidence",
            env_required=(
                "SHOPIFY_DISCOUNT_TEST_NEW_CUSTOMER_CODE"
                if os.getenv("SHOPIFY_DISCOUNT_TEST_NEW_CUSTOMER_CODE")
                else "SHOPIFY_DISCOUNT_TEST_NEW_CUSTOMER_ENABLED"
            ),
        ),
        Scenario(
            "SFD-007",
            "usage limit exhausted",
            [os.getenv("SHOPIFY_DISCOUNT_TEST_EXHAUSTED_CODE", "").strip()],
            expected="invalid_code_rejected",
            env_required="SHOPIFY_DISCOUNT_TEST_EXHAUSTED_CODE",
        ),
        Scenario(
            "SFD-008",
            "active date window active",
            [os.getenv("SHOPIFY_DISCOUNT_TEST_ACTIVE_WINDOW_CODE", "").strip()],
            expected="valid_code_discount",
            env_required="SHOPIFY_DISCOUNT_TEST_ACTIVE_WINDOW_CODE",
        ),
        Scenario(
            "SFD-009",
            "active date window inactive",
            [os.getenv("SHOPIFY_DISCOUNT_TEST_INACTIVE_WINDOW_CODE", "").strip()],
            expected="invalid_code_rejected",
            env_required="SHOPIFY_DISCOUNT_TEST_INACTIVE_WINDOW_CODE",
            boundary=True,
        ),
        Scenario(
            "SFD-010",
            "combinable discounts",
            [
                os.getenv("SHOPIFY_DISCOUNT_TEST_COMBINABLE_CODE_A", "").strip(),
                os.getenv("SHOPIFY_DISCOUNT_TEST_COMBINABLE_CODE_B", "").strip(),
            ],
            quantity=int(os.getenv("SHOPIFY_DISCOUNT_TEST_COMBINABLE_QUANTITY", "1") or "1"),
            items_env="SHOPIFY_DISCOUNT_TEST_COMBINABLE_ITEMS_JSON",
            expected="all_codes_applicable_discount",
            env_required="SHOPIFY_DISCOUNT_TEST_COMBINABLE_CODE_A",
            required_code_count=2,
        ),
        Scenario(
            "SFD-011",
            "non-combinable discount conflict",
            [
                os.getenv("SHOPIFY_DISCOUNT_TEST_NONCOMBINABLE_CODE_A", "").strip(),
                os.getenv("SHOPIFY_DISCOUNT_TEST_NONCOMBINABLE_CODE_B", "").strip(),
            ],
            quantity=int(
                os.getenv("SHOPIFY_DISCOUNT_TEST_NONCOMBINABLE_QUANTITY")
                or os.getenv("SHOPIFY_DISCOUNT_TEST_BXGY_QUANTITY", "2")
                or "2"
            ),
            items_env="SHOPIFY_DISCOUNT_TEST_NONCOMBINABLE_ITEMS_JSON",
            expected="conflict_recorded",
            env_required="SHOPIFY_DISCOUNT_TEST_NONCOMBINABLE_CODE_A",
            required_code_count=2,
        ),
    ]


def _items_from_env(env_name: Optional[str]) -> Optional[List[Dict[str, Any]]]:
    if not env_name:
        return None
    raw = (os.getenv(env_name) or "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{env_name} must be a JSON array of quote items: {exc}") from exc
    if not isinstance(parsed, list) or not parsed:
        raise SystemExit(f"{env_name} must be a non-empty JSON array of quote items")
    items: List[Dict[str, Any]] = []
    for idx, row in enumerate(parsed):
        if not isinstance(row, dict):
            raise SystemExit(f"{env_name}[{idx}] must be an object")
        product_id = str(row.get("product_id") or "").strip()
        variant_id = str(row.get("variant_id") or "").strip()
        try:
            quantity = int(row.get("quantity") or 0)
        except Exception as exc:
            raise SystemExit(f"{env_name}[{idx}].quantity must be an integer") from exc
        if not product_id or not variant_id or quantity <= 0:
            raise SystemExit(f"{env_name}[{idx}] must include product_id, variant_id, and quantity > 0")
        items.append({"product_id": product_id, "variant_id": variant_id, "quantity": quantity})
    return items


def _quote_request(args: argparse.Namespace, scenario: Scenario) -> Dict[str, Any]:
    items = _items_from_env(scenario.items_env) or [
        {
            "product_id": args.product_id,
            "variant_id": args.variant_id,
            "quantity": scenario.quantity,
        }
    ]
    return {
        "merchant_id": args.merchant_id,
        "customer_email": args.customer_email,
        "items": items,
        "discount_codes": [code for code in scenario.discount_codes if code],
        "shipping_address": _shipping_address(),
    }


def _code_applicability(payload: Dict[str, Any]) -> Dict[str, Optional[bool]]:
    evidence = payload.get("discount_evidence") if isinstance(payload, dict) else None
    out: Dict[str, Optional[bool]] = {}
    if isinstance(evidence, dict):
        for row in evidence.get("codes") or []:
            if isinstance(row, dict) and row.get("code"):
                out[str(row["code"]).upper()] = row.get("applicable")
    return out


def _discount_total(payload: Dict[str, Any]) -> float:
    try:
        pricing = payload.get("pricing") or {}
        return float(pricing.get("discount_total") or 0)
    except Exception:
        return 0.0


def _shipping_discount_total(payload: Dict[str, Any]) -> float:
    try:
        pricing = payload.get("pricing") or {}
        return float(pricing.get("shipping_discount_total") or 0)
    except Exception:
        return 0.0


def _discount_applications(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    evidence = payload.get("discount_evidence") if isinstance(payload, dict) else None
    if not isinstance(evidence, dict):
        return []
    return [row for row in evidence.get("applications") or [] if isinstance(row, dict)]


def _evaluate(scenario: Scenario, status_code: int, payload: Dict[str, Any]) -> tuple[str, str]:
    if status_code >= 400:
        return "fail", f"HTTP {status_code}"
    applicability = _code_applicability(payload)
    discount_total = _discount_total(payload)
    if scenario.expected == "valid_code_discount":
        code = next((c.upper() for c in scenario.discount_codes if c), "")
        if code and applicability.get(code) is True and discount_total > 0:
            return "pass", "applicable code produced a discount"
        return "fail", f"expected applicable discount for {code}; applicability={applicability}, discount_total={discount_total}"
    if scenario.expected == "all_codes_applicable_discount":
        codes = [c.upper() for c in scenario.discount_codes if c]
        missing = [code for code in codes if applicability.get(code) is not True]
        applications = _discount_applications(payload)
        applied_codes = {str(app.get("code") or "").upper() for app in applications if app.get("code")}
        if not missing and discount_total > 0 and applied_codes.intersection(codes):
            return "pass", f"all submitted codes applicable; discount_total={discount_total}"
        return (
            "fail",
            f"expected all codes applicable with discount evidence; missing={missing}, applicability={applicability}, "
            f"applied_codes={sorted(applied_codes)}, discount_total={discount_total}",
        )
    if scenario.expected == "invalid_code_rejected":
        code = next((c.upper() for c in scenario.discount_codes if c), "")
        if code and applicability.get(code) is False and discount_total <= 0:
            return "pass", "invalid/unavailable code rejected without discount"
        return "fail", f"expected rejected code and no discount; applicability={applicability}, discount_total={discount_total}"
    if scenario.expected == "automatic_discount":
        applications = _discount_applications(payload)
        has_auto = any(str(app.get("method") or "").lower() == "automatic" for app in applications)
        if discount_total > 0 and (has_auto or not applications):
            return "pass", "automatic discount observed"
        return "fail", f"no automatic discount observed; applications={applications}, discount_total={discount_total}"
    if scenario.expected == "customer_eligibility_evidence":
        evidence = payload.get("discount_evidence") or {}
        customer_evidence = evidence.get("customer_eligibility") if isinstance(evidence, dict) else None
        return ("pass", "customer eligibility evidence present") if customer_evidence else ("fail", "missing customer eligibility evidence")
    if scenario.expected == "free_shipping_partial_or_applied":
        evidence = payload.get("discount_evidence") or {}
        confidence = evidence.get("pricing_confidence") if isinstance(evidence, dict) else None
        code = next((c.upper() for c in scenario.discount_codes if c), "")
        shipping_discount = _shipping_discount_total(payload)
        if code and applicability.get(code) is True and confidence in {"authoritative", "partial"}:
            return (
                "pass",
                f"free-shipping code applicability captured; pricing_confidence={confidence}; shipping_discount_total={shipping_discount}",
            )
        return (
            "fail",
            f"free-shipping applicability not captured; applicability={applicability}, confidence={confidence}, "
            f"shipping_discount_total={shipping_discount}",
        )
    if scenario.expected == "conflict_recorded":
        codes = [c.upper() for c in scenario.discount_codes if c]
        applied = [code for code in codes if applicability.get(code) is True]
        rejected = [code for code in codes if applicability.get(code) is False]
        if applied and rejected and discount_total > 0:
            return "pass", f"conflict recorded; applied={applied}, rejected={rejected}, discount_total={discount_total}"
        return (
            "fail",
            f"expected one code applied and one rejected; applicability={applicability}, discount_total={discount_total}",
        )
    return "pass", "recorded for manual review"


async def _post_json(client: httpx.AsyncClient, path: str, body: Dict[str, Any]) -> tuple[int, Dict[str, Any]]:
    response = await client.post(path, json=body)
    try:
        payload = response.json()
    except Exception:
        payload = {"raw_text": response.text}
    return response.status_code, payload


async def _run(args: argparse.Namespace) -> int:
    allow_live_no_order = bool(args.allow_live_no_order or args.allow_live_readonly)
    if args.allow_dev_store and allow_live_no_order:
        raise SystemExit("Choose either --allow-dev-store or --allow-live-no-order, not both")
    if not args.allow_dev_store and not allow_live_no_order:
        raise SystemExit("--allow-dev-store or --allow-live-no-order is required")
    if allow_live_no_order and (args.include_order_create or os.getenv("SHOPIFY_DISCOUNT_TEST_ORDER_CREATE") == "1"):
        raise SystemExit("Live validation is quote/cart only; order creation is blocked with --allow-live-no-order")
    if not args.allow_remote and not args.base_url.startswith(("http://127.0.0.1", "http://localhost")):
        raise SystemExit("--allow-remote is required for non-local base URLs")
    if not args.merchant_id or not args.agent_api_key or not args.variant_id:
        raise SystemExit("merchant id, agent API key, and variant id are required")
    if args.include_order_create and os.getenv("SHOPIFY_DISCOUNT_TEST_ORDER_CREATE") != "1":
        raise SystemExit("SHOPIFY_DISCOUNT_TEST_ORDER_CREATE=1 is required with --include-order-create")

    validation_mode = "live_no_order" if allow_live_no_order else "dev_store"
    output_dir = Path(args.output_dir or f"artifacts/shopify-discount-validation/{_now_slug()}")
    output_dir.mkdir(parents=True, exist_ok=True)

    scenarios = _scenario_catalog()
    rows: List[Dict[str, Any]] = []
    headers = {"X-API-Key": args.agent_api_key}
    async with httpx.AsyncClient(base_url=args.base_url.rstrip("/"), headers=headers, timeout=30.0) as client:
        successful_quote_payload: Optional[Dict[str, Any]] = None
        successful_quote_request: Optional[Dict[str, Any]] = None
        for scenario in scenarios:
            if scenario.env_required and not os.getenv(scenario.env_required):
                rows.append(
                    {
                        "scenario_id": scenario.scenario_id,
                        "description": scenario.description,
                        "status": "blocked",
                        "actual_result": f"missing fixture env {scenario.env_required}",
                        "evidence_artifact_path": "",
                    }
                )
                continue
            submitted_codes = [code for code in scenario.discount_codes if code]
            if scenario.required_code_count and len(submitted_codes) < scenario.required_code_count:
                rows.append(
                    {
                        "scenario_id": scenario.scenario_id,
                        "description": scenario.description,
                        "status": "blocked",
                        "actual_result": f"missing discount code fixtures; expected {scenario.required_code_count}, got {len(submitted_codes)}",
                        "evidence_artifact_path": "",
                    }
                )
                continue
            if not any(scenario.discount_codes) and scenario.expected not in {"automatic_discount", "customer_eligibility_evidence"}:
                rows.append(
                    {
                        "scenario_id": scenario.scenario_id,
                        "description": scenario.description,
                        "status": "blocked",
                        "actual_result": "missing discount code fixture",
                        "evidence_artifact_path": "",
                    }
                )
                continue

            request_body = _quote_request(args, scenario)
            status_code, payload = await _post_json(client, "/agent/v1/quotes/preview", request_body)
            artifact = output_dir / f"{scenario.scenario_id}.json"
            artifact.write_text(
                json.dumps(
                    {
                        "scenario": scenario.__dict__,
                        "validation_mode": validation_mode,
                        "request": _redact(request_body),
                        "status_code": status_code,
                        "response": _redact(payload),
                    },
                    indent=2,
                    sort_keys=True,
                    default=str,
                )
                + "\n",
                encoding="utf-8",
            )
            status_value, actual = _evaluate(scenario, status_code, payload)
            rows.append(
                {
                    "scenario_id": scenario.scenario_id,
                    "description": scenario.description,
                    "status": status_value,
                    "actual_result": actual,
                    "evidence_artifact_path": str(artifact),
                }
            )
            if status_value == "pass" and not successful_quote_payload:
                successful_quote_payload = payload
                successful_quote_request = request_body

        if args.include_order_create:
            if not successful_quote_payload or not successful_quote_request:
                rows.append(
                    {
                        "scenario_id": "SFD-012",
                        "description": "quote -> order create -> Shopify reconciliation",
                        "status": "blocked",
                        "actual_result": "no successful quote available for order create",
                        "evidence_artifact_path": "",
                    }
                )
            else:
                order_body = {
                    "merchant_id": args.merchant_id,
                    "customer_email": args.customer_email,
                    "quote_id": successful_quote_payload.get("quote_id"),
                    "items": successful_quote_request["items"],
                    "shipping_address": _shipping_address(),
                    "currency": args.currency,
                    "discount_codes": successful_quote_request.get("discount_codes") or [],
                    "metadata": {"source": "shopify_discount_validation_harness"},
                }
                status_code, payload = await _post_json(client, "/agent/v1/orders/create", order_body)
                artifact = output_dir / "SFD-012.json"
                artifact.write_text(
                    json.dumps(
                        {
                            "validation_mode": validation_mode,
                            "request": _redact(order_body),
                            "status_code": status_code,
                            "response": _redact(payload),
                        },
                        indent=2,
                        sort_keys=True,
                        default=str,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                rows.append(
                    {
                        "scenario_id": "SFD-012",
                        "description": "quote -> order create -> Shopify reconciliation",
                        "status": "pass" if status_code < 400 else "fail",
                        "actual_result": f"HTTP {status_code}",
                        "evidence_artifact_path": str(artifact),
                    }
                )
        else:
            rows.append(
                {
                    "scenario_id": "SFD-012",
                    "description": "quote -> order create -> Shopify reconciliation",
                    "status": "blocked",
                    "actual_result": "order creation blocked for live quote/cart validation"
                    if allow_live_no_order
                    else "order creation intentionally gated; rerun with --include-order-create and SHOPIFY_DISCOUNT_TEST_ORDER_CREATE=1 in a dev store",
                    "evidence_artifact_path": "",
                }
            )

    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps({"validation_mode": validation_mode, "rows": rows}, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    csv_path = output_dir / "summary.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["scenario_id", "description", "status", "actual_result", "evidence_artifact_path"],
        )
        writer.writeheader()
        writer.writerows(rows)

    print(json.dumps({"output_dir": str(output_dir), "summary": str(summary_path), "csv": str(csv_path)}, indent=2))
    return 0 if all(row["status"] in {"pass", "blocked"} for row in rows) else 1


def main() -> int:
    try:
        import anyio
    except Exception:
        anyio = None
    if anyio:
        return anyio.run(_run, _args())
    import asyncio

    return asyncio.run(_run(_args()))


if __name__ == "__main__":
    sys.exit(main())
