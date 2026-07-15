#!/usr/bin/env python3
"""Read-only Shopify discount fixture preflight.

This script separates merchant/store setup blockers from Pivota discount
execution bugs before a live/dev-store validation run. It never creates orders,
confirms PSP payments, or creates/updates Shopify discounts.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.validate_shopify_discounts import (
    Scenario,
    _evaluate,
    _now_slug,
    _quote_request,
    _redact,
    _scenario_catalog,
)


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preflight Shopify discount fixtures without creating orders.")
    parser.add_argument(
        "--base-url",
        default=os.getenv("SHOPIFY_DISCOUNT_TEST_BASE_URL") or os.getenv("PIVOTA_BASE_URL", "http://127.0.0.1:8000"),
    )
    parser.add_argument("--merchant-id", default=os.getenv("SHOPIFY_DISCOUNT_TEST_MERCHANT_ID"))
    parser.add_argument("--agent-api-key", default=os.getenv("SHOPIFY_DISCOUNT_TEST_AGENT_API_KEY") or os.getenv("PIVOTA_AGENT_API_KEY"))
    parser.add_argument("--product-id", default=os.getenv("SHOPIFY_DISCOUNT_TEST_PRODUCT_ID", "shopify_test_product"))
    parser.add_argument("--variant-id", default=os.getenv("SHOPIFY_DISCOUNT_TEST_VARIANT_ID"))
    parser.add_argument("--customer-email", default=os.getenv("SHOPIFY_DISCOUNT_TEST_CUSTOMER_EMAIL", "shopify-discount-test@example.com"))
    parser.add_argument("--allow-dev-store", action="store_true", help="Acknowledge that only a dev/test Shopify store is targeted.")
    parser.add_argument(
        "--allow-live-readonly",
        action="store_true",
        help="Acknowledge live read-only quote/cart/admin metadata preflight is approved.",
    )
    parser.add_argument("--allow-remote", action="store_true", help="Allow non-localhost base URLs.")
    parser.add_argument(
        "--admin-key",
        default=(
            os.getenv("SHOPIFY_DISCOUNT_PREFLIGHT_ADMIN_KEY")
            or os.getenv("SHOPIFY_DISCOUNT_TEST_ADMIN_KEY")
            or os.getenv("PROMOTIONS_ADMIN_KEY")
            or os.getenv("ADMIN_API_KEY")
        ),
        help="Internal admin key used only for the read-only discountNodes access probe.",
    )
    parser.add_argument("--api-version", default=os.getenv("SHOPIFY_API_VERSION", "2025-10"))
    parser.add_argument("--output-dir", default=os.getenv("SHOPIFY_DISCOUNT_PREFLIGHT_OUTPUT_DIR"))
    parser.add_argument("--fail-on-blocked", action="store_true", help="Return non-zero when any check is blocked.")
    return parser.parse_args()


def _row(
    *,
    check_id: str,
    area: str,
    status: str,
    actual_result: str,
    evidence_artifact_path: str = "",
    recommended_action: str = "",
) -> Dict[str, str]:
    return {
        "check_id": check_id,
        "area": area,
        "status": status,
        "actual_result": actual_result,
        "evidence_artifact_path": evidence_artifact_path,
        "recommended_action": recommended_action,
    }


def _write_artifact(path: Path, payload: Dict[str, Any]) -> str:
    path.write_text(json.dumps(_redact(payload), indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return str(path)


def _pricing_confidence(payload: Dict[str, Any]) -> Optional[str]:
    evidence = payload.get("discount_evidence") if isinstance(payload, dict) else None
    if not isinstance(evidence, dict):
        return None
    value = evidence.get("pricing_confidence")
    return str(value) if value is not None else None


def _shipping_evidence_status(payload: Dict[str, Any]) -> Optional[str]:
    evidence = payload.get("discount_evidence") if isinstance(payload, dict) else None
    shipping = evidence.get("shipping_evidence") if isinstance(evidence, dict) else None
    if not isinstance(shipping, dict):
        return None
    value = shipping.get("status")
    return str(value) if value is not None else None


def _baseline_result(status_code: int, payload: Dict[str, Any]) -> tuple[str, str, str]:
    if status_code <= 0:
        return "fail", "request failed before receiving an HTTP response", "Fix backend URL/network access before discount validation."
    if status_code >= 400:
        return "fail", f"HTTP {status_code}", "Fix product/variant/merchant connectivity before discount validation."
    pricing = payload.get("pricing") if isinstance(payload, dict) else None
    if not isinstance(pricing, dict) or pricing.get("total") is None:
        return "fail", "quote response missing pricing.total", "Inspect quote preview and Storefront pricing logs."
    confidence = _pricing_confidence(payload)
    shipping_status = _shipping_evidence_status(payload)
    if confidence == "authoritative" and shipping_status == "authoritative":
        return "pass", "baseline quote priced with authoritative Shopify delivery evidence", ""
    if confidence == "authoritative":
        return (
            "blocked",
            f"baseline quote priced but shipping evidence is not authoritative; shipping_status={shipping_status}",
            "Confirm Markets, shipping zone, product weight, and delivery profile for the test address.",
        )
    return (
        "blocked",
        f"baseline quote priced but pricing_confidence={confidence}",
        "Resolve Storefront cart/delivery evidence before relying on final discount totals.",
    )


def _scenario_blocker(scenario: Scenario) -> Optional[str]:
    if scenario.env_required and not os.getenv(scenario.env_required):
        return f"missing fixture env {scenario.env_required}"
    submitted_codes = [code for code in scenario.discount_codes if code]
    if scenario.required_code_count and len(submitted_codes) < scenario.required_code_count:
        return f"missing discount code fixtures; expected {scenario.required_code_count}, got {len(submitted_codes)}"
    if not submitted_codes and scenario.expected not in {"automatic_discount", "customer_eligibility_evidence"}:
        return "missing discount code fixture"
    return None


async def _get_json(client: httpx.AsyncClient, path: str) -> tuple[int, Dict[str, Any]]:
    try:
        response = await client.get(path)
    except httpx.RequestError as exc:
        return 0, {"error": "request_error", "message": str(exc)}
    try:
        payload = response.json()
    except Exception:
        payload = {"raw_text": response.text}
    return response.status_code, payload


async def _post_json(client: httpx.AsyncClient, path: str, body: Dict[str, Any]) -> tuple[int, Dict[str, Any]]:
    try:
        response = await client.post(path, json=body)
    except httpx.RequestError as exc:
        return 0, {"error": "request_error", "message": str(exc)}
    try:
        payload = response.json()
    except Exception:
        payload = {"raw_text": response.text}
    return response.status_code, payload


async def _run(args: argparse.Namespace) -> int:
    if args.allow_dev_store and args.allow_live_readonly:
        raise SystemExit("Choose either --allow-dev-store or --allow-live-readonly, not both")
    if not args.allow_dev_store and not args.allow_live_readonly:
        raise SystemExit("--allow-dev-store or --allow-live-readonly is required")
    if not args.allow_remote and not args.base_url.startswith(("http://127.0.0.1", "http://localhost")):
        raise SystemExit("--allow-remote is required for non-local base URLs")

    output_dir = Path(args.output_dir or f"artifacts/shopify-discount-validation/{_now_slug()}/preflight")
    output_dir.mkdir(parents=True, exist_ok=True)

    rows: List[Dict[str, str]] = []
    missing_required = [
        name
        for name, value in {
            "SHOPIFY_DISCOUNT_TEST_MERCHANT_ID": args.merchant_id,
            "SHOPIFY_DISCOUNT_TEST_AGENT_API_KEY": args.agent_api_key,
            "SHOPIFY_DISCOUNT_TEST_VARIANT_ID": args.variant_id,
        }.items()
        if not value
    ]
    if missing_required:
        rows.append(
            _row(
                check_id="PREFLIGHT-ENV",
                area="configuration",
                status="blocked",
                actual_result=f"missing required values: {', '.join(missing_required)}",
                recommended_action="Set the missing env vars or CLI args; do not paste secrets into logs.",
            )
        )
    else:
        rows.append(
            _row(
                check_id="PREFLIGHT-ENV",
                area="configuration",
                status="pass",
                actual_result="required merchant, agent key, and variant identifiers are present",
            )
        )

    headers = {"X-API-Key": args.agent_api_key or ""}
    async with httpx.AsyncClient(base_url=args.base_url.rstrip("/"), headers=headers, timeout=30.0) as client:
        health_status, health_payload = await _get_json(client, "/health")
        health_artifact = _write_artifact(
            output_dir / "PREFLIGHT-HEALTH.json",
            {"status_code": health_status, "response": health_payload},
        )
        rows.append(
            _row(
                check_id="PREFLIGHT-HEALTH",
                area="pivota_api",
                status="pass" if 200 <= health_status < 400 else "fail",
                actual_result=f"HTTP {health_status}",
                evidence_artifact_path=health_artifact,
                recommended_action="" if 200 <= health_status < 400 else "Fix backend health before running discount validation.",
            )
        )

        if not missing_required:
            baseline = Scenario(
                "PREFLIGHT-QUOTE-BASELINE",
                "baseline quote without discount code",
                [],
                expected="record_only",
            )
            body = _quote_request(args, baseline)
            quote_status, quote_payload = await _post_json(client, "/agent/v1/quotes/preview", body)
            quote_artifact = _write_artifact(
                output_dir / "PREFLIGHT-QUOTE-BASELINE.json",
                {"request": body, "status_code": quote_status, "response": quote_payload},
            )
            status_value, actual, action = _baseline_result(quote_status, quote_payload)
            rows.append(
                _row(
                    check_id="PREFLIGHT-QUOTE-BASELINE",
                    area="storefront_pricing",
                    status=status_value,
                    actual_result=actual,
                    evidence_artifact_path=quote_artifact,
                    recommended_action=action,
                )
            )

            for scenario in _scenario_catalog():
                blocker = _scenario_blocker(scenario)
                if blocker:
                    rows.append(
                        _row(
                            check_id=scenario.scenario_id,
                            area="discount_fixture",
                            status="blocked",
                            actual_result=blocker,
                            recommended_action="Create or correct this Shopify-native fixture before rerunning live validation.",
                        )
                    )
                    continue

                body = _quote_request(args, scenario)
                status_code, payload = await _post_json(client, "/agent/v1/quotes/preview", body)
                artifact = _write_artifact(
                    output_dir / f"{scenario.scenario_id}.json",
                    {
                        "scenario": scenario.__dict__,
                        "request": body,
                        "status_code": status_code,
                        "response": payload,
                    },
                )
                status_value, actual = _evaluate(scenario, status_code, payload)
                action = "" if status_value == "pass" else "Fix the Shopify fixture or Pivota quote path before treating this scenario as rollout evidence."
                rows.append(
                    _row(
                        check_id=scenario.scenario_id,
                        area="discount_fixture",
                        status=status_value,
                        actual_result=actual,
                        evidence_artifact_path=artifact,
                        recommended_action=action,
                    )
                )

        if not args.admin_key:
            rows.append(
                _row(
                    check_id="PREFLIGHT-DISCOUNT-NODES",
                    area="admin_graphql",
                    status="blocked",
                    actual_result="missing admin key for read-only discountNodes access probe",
                    recommended_action=(
                        "Set SHOPIFY_DISCOUNT_PREFLIGHT_ADMIN_KEY or run the internal endpoint manually "
                        "after the merchant custom app Admin token is updated with read_discounts."
                    ),
                )
            )
        elif not args.merchant_id:
            rows.append(
                _row(
                    check_id="PREFLIGHT-DISCOUNT-NODES",
                    area="admin_graphql",
                    status="blocked",
                    actual_result="missing merchant id",
                    recommended_action="Set SHOPIFY_DISCOUNT_TEST_MERCHANT_ID.",
                )
            )
        else:
            admin_headers = {"X-ADMIN-KEY": args.admin_key}
            async with httpx.AsyncClient(base_url=args.base_url.rstrip("/"), headers=admin_headers, timeout=30.0) as admin_client:
                path = f"/agent/internal/shopify/promotions/preflight/{args.merchant_id}/discount-nodes?api_version={args.api_version}"
                status_code, payload = await _get_json(admin_client, path)
            artifact = _write_artifact(
                output_dir / "PREFLIGHT-DISCOUNT-NODES.json",
                {"status_code": status_code, "response": payload},
            )
            probe = payload.get("probe") if isinstance(payload, dict) else None
            if status_code <= 0 or status_code >= 400:
                rows.append(
                    _row(
                        check_id="PREFLIGHT-DISCOUNT-NODES",
                        area="admin_graphql",
                        status="fail",
                        actual_result=f"HTTP {status_code}",
                        evidence_artifact_path=artifact,
                        recommended_action="Deploy the preflight endpoint and verify the internal admin key.",
                    )
                )
            elif isinstance(probe, dict) and probe.get("discountNodesAccess") == "ok" and probe.get("hasReadDiscountsScope"):
                rows.append(
                    _row(
                        check_id="PREFLIGHT-DISCOUNT-NODES",
                        area="admin_graphql",
                        status="pass",
                        actual_result=f"discountNodes readable; sampleNodeCount={probe.get('sampleNodeCount')}",
                        evidence_artifact_path=artifact,
                    )
                )
            else:
                rows.append(
                    _row(
                        check_id="PREFLIGHT-DISCOUNT-NODES",
                        area="admin_graphql",
                        status="blocked",
                        actual_result=f"discountNodes not readable; probe={_redact(probe or payload)}",
                        evidence_artifact_path=artifact,
                        recommended_action=(
                            "Regenerate or update the merchant custom app Admin token with read_discounts, "
                            "update the stored Shopify credential, then rerun preflight."
                        ),
                    )
                )

    summary = {"base_url": args.base_url, "mode": "live_readonly" if args.allow_live_readonly else "dev_store", "rows": rows}
    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    csv_path = output_dir / "summary.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["check_id", "area", "status", "actual_result", "evidence_artifact_path", "recommended_action"],
        )
        writer.writeheader()
        writer.writerows(rows)

    print(json.dumps({"output_dir": str(output_dir), "summary": str(summary_path), "csv": str(csv_path)}, indent=2))
    if any(row["status"] == "fail" for row in rows):
        return 1
    if args.fail_on_blocked and any(row["status"] == "blocked" for row in rows):
        return 1
    return 0


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
