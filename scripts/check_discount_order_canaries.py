#!/usr/bin/env python3
"""Read-only canary audit for discounted order reconciliation and refunds."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

if os.getenv("SHOPIFY_DISCOUNT_CANARY_DATABASE_URL"):
    os.environ["DATABASE_URL"] = os.getenv("SHOPIFY_DISCOUNT_CANARY_DATABASE_URL", "")

from db.database import database
from scripts.validate_shopify_discounts import _now_slug, _redact


@dataclass(frozen=True)
class CanaryFinding:
    order_id: str
    severity: str
    check: str
    detail: str


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only discounted order/refund canary audit.")
    parser.add_argument("--merchant-id", default=os.getenv("SHOPIFY_DISCOUNT_TEST_MERCHANT_ID"))
    parser.add_argument("--limit", type=int, default=int(os.getenv("SHOPIFY_DISCOUNT_CANARY_LIMIT", "100")))
    parser.add_argument("--include-undiscounted", action="store_true")
    parser.add_argument("--output-dir", default=os.getenv("SHOPIFY_DISCOUNT_CANARY_OUTPUT_DIR"))
    parser.add_argument("--fail-on-warning", action="store_true")
    return parser.parse_args()


def _money(value: Any) -> Decimal:
    try:
        return Decimal(str(value if value is not None else "0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:
        return Decimal("0.00")


def _coerce_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _pricing_quote_meta(order: Dict[str, Any]) -> Dict[str, Any]:
    metadata = _coerce_dict(order.get("metadata"))
    pricing_quote = metadata.get("pricing_quote")
    return pricing_quote if isinstance(pricing_quote, dict) else {}


def _discount_total_from_quote(pricing_quote: Dict[str, Any]) -> Decimal:
    pricing = pricing_quote.get("pricing") if isinstance(pricing_quote, dict) else None
    if isinstance(pricing, dict):
        total = _money(pricing.get("discount_total"))
        if total > 0:
            return total

    total = Decimal("0.00")
    for line in pricing_quote.get("promotion_lines") or []:
        if isinstance(line, dict):
            total += _money(line.get("amount")).copy_abs()
    evidence = pricing_quote.get("discount_evidence")
    if isinstance(evidence, dict):
        for app in evidence.get("applications") or []:
            if isinstance(app, dict):
                total += _money(app.get("amount")).copy_abs()
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _pricing_confidence(pricing_quote: Dict[str, Any]) -> str:
    evidence = pricing_quote.get("discount_evidence") if isinstance(pricing_quote, dict) else None
    if not isinstance(evidence, dict):
        return ""
    return str(evidence.get("pricing_confidence") or "").strip().lower()


def _is_discounted_order(order: Dict[str, Any]) -> bool:
    pricing_quote = _pricing_quote_meta(order)
    if _discount_total_from_quote(pricing_quote) > 0:
        return True
    evidence = pricing_quote.get("discount_evidence") if isinstance(pricing_quote, dict) else None
    if isinstance(evidence, dict):
        if evidence.get("applications"):
            return True
        for code in evidence.get("codes") or []:
            if isinstance(code, dict) and code.get("applicable") is True:
                return True
    return False


def _refund_totals(refunds: Iterable[Dict[str, Any]]) -> Dict[str, Decimal]:
    completed = Decimal("0.00")
    ignored = Decimal("0.00")
    failed = Decimal("0.00")
    pending = Decimal("0.00")
    for refund in refunds:
        amount = _money(refund.get("amount"))
        status = str(refund.get("status") or "").strip().lower()
        if status == "completed":
            completed += amount
        elif status == "ignored":
            ignored += amount
        elif status == "failed":
            failed += amount
        else:
            pending += amount
    return {
        "completed": completed.quantize(Decimal("0.01")),
        "ignored": ignored.quantize(Decimal("0.01")),
        "failed": failed.quantize(Decimal("0.01")),
        "pending": pending.quantize(Decimal("0.01")),
    }


def _audit_order(order: Dict[str, Any], refunds: List[Dict[str, Any]]) -> List[CanaryFinding]:
    findings: List[CanaryFinding] = []
    order_id = str(order.get("order_id") or "")
    total = _money(order.get("total"))
    total_refunded = _money(order.get("total_refunded"))
    pricing_quote = _pricing_quote_meta(order)
    discount_total = _discount_total_from_quote(pricing_quote)
    payment_status = str(order.get("payment_status") or "").strip().lower()
    psp_used = str(order.get("psp_used") or "").strip().lower()
    shopify_order_id = str(order.get("shopify_order_id") or "").strip()

    if payment_status == "paid" and discount_total > 0 and not shopify_order_id:
        findings.append(
            CanaryFinding(order_id, "fail", "missing_shopify_order_link", "paid discounted order has no Shopify order id")
        )

    if payment_status == "paid" and discount_total > 0 and _pricing_confidence(pricing_quote) != "authoritative":
        findings.append(
            CanaryFinding(
                order_id,
                "fail",
                "non_authoritative_discount_pricing",
                f"discounted paid order has pricing_confidence={_pricing_confidence(pricing_quote) or 'missing'}",
            )
        )

    if total_refunded > total:
        findings.append(
            CanaryFinding(
                order_id,
                "fail",
                "order_total_refunded_exceeds_total",
                f"total_refunded={total_refunded} exceeds total={total}",
            )
        )

    refund_totals = _refund_totals(refunds)
    if refund_totals["completed"] > total:
        findings.append(
            CanaryFinding(
                order_id,
                "fail",
                "completed_refund_ledger_exceeds_total",
                f"completed refund ledger={refund_totals['completed']} exceeds total={total}",
            )
        )

    if total_refunded != refund_totals["completed"]:
        findings.append(
            CanaryFinding(
                order_id,
                "warn",
                "order_refund_total_differs_from_completed_ledger",
                f"orders.total_refunded={total_refunded}; completed refund ledger={refund_totals['completed']}",
            )
        )

    external_psp = psp_used and psp_used not in {"shopify", "shopify_payments", "shop_pay"}
    for refund in refunds:
        source = str(refund.get("source") or "").strip().lower()
        status = str(refund.get("status") or "").strip().lower()
        platform_type = str(refund.get("platform_type") or "").strip().lower()
        if external_psp and source == "platform_webhook" and platform_type == "shopify" and status != "ignored":
            findings.append(
                CanaryFinding(
                    order_id,
                    "fail",
                    "shopify_refund_webhook_mutated_external_psp_order",
                    f"platform webhook refund status={status}; expected ignored for external PSP {psp_used}",
                )
            )

    return findings


async def _fetch_orders(*, merchant_id: Optional[str], limit: int) -> List[Dict[str, Any]]:
    where = ["COALESCE(is_deleted, false) = false"]
    params: Dict[str, Any] = {"limit": max(1, min(limit, 1000))}
    if merchant_id:
        where.append("merchant_id = :merchant_id")
        params["merchant_id"] = merchant_id
    query = f"""
        SELECT order_id, merchant_id, customer_email, total, total_refunded, currency,
               payment_status, status, shopify_order_id, psp_used, metadata, created_at, paid_at
        FROM orders
        WHERE {' AND '.join(where)}
        ORDER BY created_at DESC
        LIMIT :limit
    """
    return [dict(row) for row in await database.fetch_all(query, params)]


async def _fetch_refunds(order_id: str) -> List[Dict[str, Any]]:
    query = """
        SELECT refund_id, order_id, merchant_id, amount, currency, source, status,
               platform_type, platform_refund_id, psp_type, psp_refund_id,
               idempotency_key, error_message, created_at
        FROM refund_records
        WHERE order_id = :order_id
        ORDER BY created_at ASC
    """
    return [dict(row) for row in await database.fetch_all(query, {"order_id": order_id})]


async def _run(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir or f"artifacts/shopify-discount-validation/{_now_slug()}/order-canaries")
    output_dir.mkdir(parents=True, exist_ok=True)

    should_disconnect = False
    orders: List[Dict[str, Any]] = []
    checked: List[Dict[str, Any]] = []
    findings: List[CanaryFinding] = []
    try:
        if not database.is_connected:
            await database.connect()
            should_disconnect = True

        orders = await _fetch_orders(merchant_id=args.merchant_id, limit=args.limit)
        for order in orders:
            if not args.include_undiscounted and not _is_discounted_order(order):
                continue
            refunds = await _fetch_refunds(str(order.get("order_id")))
            order_findings = _audit_order(order, refunds)
            findings.extend(order_findings)
            pricing_quote = _pricing_quote_meta(order)
            checked.append(
                {
                    "order_id": order.get("order_id"),
                    "merchant_id": order.get("merchant_id"),
                    "payment_status": order.get("payment_status"),
                    "shopify_order_id": order.get("shopify_order_id"),
                    "psp_used": order.get("psp_used"),
                    "total": str(_money(order.get("total"))),
                    "total_refunded": str(_money(order.get("total_refunded"))),
                    "discount_total": str(_discount_total_from_quote(pricing_quote)),
                    "pricing_confidence": _pricing_confidence(pricing_quote),
                    "refund_totals": {k: str(v) for k, v in _refund_totals(refunds).items()},
                    "finding_count": len(order_findings),
                }
            )
    except Exception as exc:
        summary = {
            "merchant_id": args.merchant_id,
            "orders_scanned": 0,
            "discounted_orders_checked": 0,
            "fail_count": 1,
            "warn_count": 0,
            "checked_orders": [],
            "findings": [
                {
                    "order_id": "",
                    "severity": "fail",
                    "check": "canary_query_failed",
                    "detail": str(exc),
                }
            ],
        }
        summary_path = output_dir / "summary.json"
        summary_path.write_text(json.dumps(_redact(summary), indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        csv_path = output_dir / "findings.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["order_id", "severity", "check", "detail"])
            writer.writeheader()
            writer.writerows(summary["findings"])
        print(json.dumps({"output_dir": str(output_dir), "summary": str(summary_path), "csv": str(csv_path)}, indent=2))
        return 1
    finally:
        if should_disconnect and database.is_connected:
            await database.disconnect()

    finding_rows = [finding.__dict__ for finding in findings]
    summary = {
        "merchant_id": args.merchant_id,
        "orders_scanned": len(orders),
        "discounted_orders_checked": len(checked),
        "fail_count": sum(1 for f in findings if f.severity == "fail"),
        "warn_count": sum(1 for f in findings if f.severity == "warn"),
        "checked_orders": checked,
        "findings": finding_rows,
    }
    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(_redact(summary), indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")

    csv_path = output_dir / "findings.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["order_id", "severity", "check", "detail"])
        writer.writeheader()
        writer.writerows(finding_rows)

    print(json.dumps({"output_dir": str(output_dir), "summary": str(summary_path), "csv": str(csv_path)}, indent=2))
    if summary["fail_count"] > 0:
        return 1
    if args.fail_on_warning and summary["warn_count"] > 0:
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
