#!/usr/bin/env python3
"""Wix order write-back smoke test against a real merchant's stored credentials.

Reads the merchant's Wix store row from `merchant_stores`, builds a synthetic
Pivota-shaped order, and exercises `adapters.wix_adapter.create_wix_order`.

Default mode is --dry-run: intercepts the upstream `httpx.AsyncClient.post`
call and reports the URL, headers (token redacted), and full JSON payload
that *would* have hit Wix Stores v2. No upstream side-effect.

With --live: actually POSTs to Wix Stores v2 and creates a real order in the
merchant's Wix dashboard. The synthetic order is tagged
`customer_email=ops+wix-writeback-canary@pivota.invalid` and channelInfo
externalOrderId starts with `pivota_smoke_` so it's easy to identify and
cancel from the Wix dashboard afterwards.

Run via Railway so DATABASE_URL + WIX_* env vars are injected:

    railway run python scripts/smoke_wix_order_writeback.py \\
        --merchant-id merch_efbc46b4619cfbdf

Add --live to make the real API call. Capture the returned `order_id`
(the Wix-issued order id) so the merchant can cancel it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _redact_token(token: str) -> str:
    if not token:
        return ""
    if len(token) <= 12:
        return "***"
    return f"{token[:6]}…{token[-4:]}"


def _redact_headers(headers: Dict[str, str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k, v in headers.items():
        if k.lower() == "authorization":
            parts = (v or "").split()
            tok = parts[-1] if parts else ""
            out[k] = f"Bearer {_redact_token(tok)}"
        elif k.lower() == "wix-site-id":
            out[k] = _redact_token(v)
        else:
            out[k] = v
    return out


async def _load_wix_credentials(merchant_id: str) -> Dict[str, Any]:
    from db.database import database

    if not database.is_connected:
        await database.connect()

    row = await database.fetch_one(
        """
        SELECT store_id, merchant_id, platform, domain, name, api_key,
               status, connected_at, last_sync
        FROM merchant_stores
        WHERE merchant_id = :merchant_id
          AND platform = 'wix'
          AND status = 'active'
        ORDER BY connected_at DESC NULLS LAST
        LIMIT 1
        """,
        {"merchant_id": merchant_id},
    )
    if not row:
        raise SystemExit(
            f"No active Wix store found for merchant_id={merchant_id}. "
            "Verify merchant_stores has a row with platform='wix' AND status='active'."
        )
    data = dict(row)
    api_key = (data.get("api_key") or "").strip()
    site_id = (data.get("domain") or "").strip()
    return {
        "store_id": data["store_id"],
        "merchant_id": data["merchant_id"],
        "name": data.get("name"),
        "connected_at": data.get("connected_at"),
        "last_sync": data.get("last_sync"),
        "access_token": api_key,
        "site_id": site_id,
    }


def _build_synthetic_order(
    *, merchant_id: str, creds: Dict[str, Any], amount_cents: int
) -> Dict[str, Any]:
    order_id = f"pivota_smoke_{uuid.uuid4().hex[:10]}"
    unit_price = f"{amount_cents / 100:.2f}"
    return {
        "order_id": order_id,
        "merchant_id": merchant_id,
        "customer_email": "ops+wix-writeback-canary@pivota.invalid",
        "customer_name": "Pivota Canary",
        "currency": "USD",
        "subtotal": unit_price,
        "shipping_fee": "0.00",
        "tax": "0.00",
        "total": unit_price,
        "payment_status": "paid",
        "payment_intent_id": f"pi_smoke_{int(time.time())}",
        "shipping_address": {
            "name": "Pivota Canary",
            "country": "US",
            "state": "CA",
            "city": "San Francisco",
            "postal_code": "94103",
            "address_line1": "1 Pivota Way",
            "phone": "+14155550100",
        },
        "items": [
            {
                "product_title": "Pivota Canary Test Item — do not fulfill",
                "sku": "PIVOTA-CANARY",
                "quantity": 1,
                "unit_price": unit_price,
            }
        ],
        "wix_credentials": {
            "access_token": creds["access_token"],
            "site_id": creds["site_id"],
        },
    }


def _install_dry_run_intercept(captured: Dict[str, Any]) -> None:
    """Monkeypatch httpx.AsyncClient.post so dry-run doesn't hit Wix."""
    import httpx

    class _DryResponse:
        status_code = 200
        text = '{"order": {"id": "DRY_RUN_NO_WIX_CALL"}}'

        def json(self) -> Dict[str, Any]:
            return {"order": {"id": "DRY_RUN_NO_WIX_CALL"}, "_dry_run": True}

    async def _fake_post(self, url, *, headers=None, json=None, **kw):  # noqa: A002
        captured["url"] = url
        captured["headers"] = dict(headers or {})
        captured["payload"] = json
        return _DryResponse()

    httpx.AsyncClient.post = _fake_post  # type: ignore[assignment]


async def _run(
    *, merchant_id: str, live: bool, amount_cents: int
) -> Dict[str, Any]:
    creds = await _load_wix_credentials(merchant_id)

    cred_summary = {
        "store_id": creds["store_id"],
        "name": creds.get("name"),
        "access_token_present": bool(creds["access_token"]),
        "access_token_preview": _redact_token(creds["access_token"]),
        "site_id_present": bool(creds["site_id"]),
        "site_id_preview": _redact_token(creds["site_id"]),
        "connected_at": str(creds.get("connected_at") or ""),
        "last_sync": str(creds.get("last_sync") or ""),
    }

    order = _build_synthetic_order(
        merchant_id=merchant_id, creds=creds, amount_cents=amount_cents
    )

    captured: Dict[str, Any] = {}
    if not live:
        _install_dry_run_intercept(captured)

    from adapters.wix_adapter import build_wix_order_payload, create_wix_order

    assembled_payload = build_wix_order_payload(order)

    result = await create_wix_order(merchant_id, order)

    summary: Dict[str, Any] = {
        "mode": "live" if live else "dry-run",
        "merchant_id": merchant_id,
        "credentials": cred_summary,
        "synthetic_order_id": order["order_id"],
        "amount_cents": amount_cents,
        "currency": order["currency"],
        "adapter_result": result,
        "assembled_payload_preview": {
            "lineItems": assembled_payload.get("lineItems"),
            "billingInfo": {
                "email": assembled_payload["billingInfo"].get("email"),
                "paymentMethod": assembled_payload["billingInfo"].get("paymentMethod"),
            },
            "paymentStatus": assembled_payload.get("paymentStatus"),
            "fulfillmentStatus": assembled_payload.get("fulfillmentStatus"),
            "currency": assembled_payload.get("currency"),
            "channelInfo": assembled_payload.get("channelInfo"),
        },
    }
    if not live:
        summary["intercepted_request"] = {
            "url": captured.get("url"),
            "headers": _redact_headers(captured.get("headers") or {}),
            "payload": captured.get("payload"),
        }
    return summary


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Wix order write-back smoke test against a real merchant.",
    )
    parser.add_argument("--merchant-id", required=True)
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "Actually POST to Wix Stores v2 (creates a real order in the "
            "merchant's Wix dashboard). Without this flag, runs in dry-run "
            "mode and intercepts the httpx call."
        ),
    )
    parser.add_argument(
        "--amount-cents",
        type=int,
        default=100,
        help="Synthetic line-item unit price in cents (default 100 = $1.00).",
    )
    parser.add_argument("--output-json", default=None)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not os.environ.get("DATABASE_URL"):
        print(
            "ERROR: DATABASE_URL not set. Run via `railway run python "
            "scripts/smoke_wix_order_writeback.py ...` so prod env is injected.",
            file=sys.stderr,
        )
        return 2

    summary = asyncio.run(
        _run(
            merchant_id=args.merchant_id,
            live=args.live,
            amount_cents=args.amount_cents,
        )
    )

    rendered = json.dumps(summary, indent=2, default=str)
    print(rendered)
    if args.output_json:
        Path(args.output_json).write_text(rendered)

    result = summary["adapter_result"]
    ok = isinstance(result, dict) and result.get("status") != "error"
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
