import argparse
import asyncio
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import httpx


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ops-only PSP canary harness for merchant initiation and routing evidence."
    )
    parser.add_argument("--base-url", default="https://api.pivota.cc", help="Public API base URL.")
    parser.add_argument("--email", required=True, help="Merchant email for authenticated portal APIs.")
    parser.add_argument("--password", required=True, help="Merchant password for authenticated portal APIs.")
    parser.add_argument("--merchant-id", required=True, help="Merchant ID.")
    parser.add_argument("--api-key", help="Existing merchant API key. If omitted, the harness fetches one from /merchant/api-credentials.")
    parser.add_argument(
        "--mode",
        choices=("live", "test"),
        default="live",
        help="Use merchant-facing live execution or internal test-mode canary execution.",
    )
    parser.add_argument(
        "--internal-key",
        default=os.getenv("READINESS_INTERNAL_API_KEY") or os.getenv("UCP_INTERNAL_API_KEY"),
        help="X-Pivota-Internal-Key for ops-only test canary execution.",
    )
    parser.add_argument("--provider-order", help="Temporary routing order, e.g. stripe,adyen,checkout")
    parser.add_argument("--amount-minor", type=int, default=100, help="Payment amount in minor units.")
    parser.add_argument("--currency", default="USD", help="Currency code.")
    parser.add_argument("--order-id", help="Explicit order ID for the initiation call.")
    parser.add_argument("--output", help="Write evidence JSON to this file path.")
    parser.add_argument(
        "--stripe-checkout-canary",
        action="store_true",
        help=(
            "Request Stripe hosted checkout by passing metadata.psp_mode=stripe_checkout. "
            "Intended for ops canary proof only."
        ),
    )
    parser.add_argument(
        "--emit-merchant-webhook",
        action="store_true",
        help="Allow the canary execution to emit merchant outbound webhook events.",
    )
    parser.add_argument(
        "--keep-route",
        action="store_true",
        help="Do not restore the original routing after the canary run.",
    )
    return parser.parse_args()


async def _login(client: httpx.AsyncClient, email: str, password: str) -> Dict[str, Any]:
    response = await client.post("/api/auth/login", json={"email": email, "password": password})
    response.raise_for_status()
    payload = response.json()
    token = payload.get("token")
    if not token:
        raise RuntimeError("Login succeeded but no bearer token was returned")
    return {"token": token, "payload": payload}


def _parse_provider_order(raw: Optional[str]) -> Optional[list[dict[str, Any]]]:
    if not raw:
        return None
    providers = [item.strip().lower() for item in raw.split(",") if item.strip()]
    return [{"psp": provider, "priority": index + 1} for index, provider in enumerate(providers)]


def _build_payment_request(args: argparse.Namespace, order_id: str) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {
        "source": "ops_psp_canary_harness",
    }
    if args.stripe_checkout_canary:
        metadata["psp_mode"] = "stripe_checkout"

    return {
        "amount": args.amount_minor,
        "currency": args.currency,
        "order_id": order_id,
        "customer_email": args.email,
        "description": "ops_psp_canary_harness",
        "metadata": metadata,
    }


async def _run(args: argparse.Namespace) -> int:
    evidence: Dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "merchant_id": args.merchant_id,
        "base_url": args.base_url.rstrip("/"),
        "mode": args.mode,
        "stripe_checkout_canary": bool(args.stripe_checkout_canary),
    }

    async with httpx.AsyncClient(base_url=args.base_url.rstrip("/"), timeout=20.0) as client:
        login = await _login(client, args.email, args.password)
        token = login["token"]
        client.headers["Authorization"] = f"Bearer {token}"
        evidence["login"] = {"status": "success"}

        route_before_resp = await client.get("/merchant/integrations/routing")
        route_before_resp.raise_for_status()
        route_before = route_before_resp.json().get("data") or route_before_resp.json()
        evidence["routing_before"] = route_before

        psp_list_resp = await client.get(f"/merchant/{args.merchant_id}/psps")
        psp_list_resp.raise_for_status()
        evidence["psps"] = (psp_list_resp.json().get("data") or {}).get("psps", [])

        credentials_resp = await client.get("/merchant/api-credentials")
        credentials_resp.raise_for_status()
        credentials = credentials_resp.json().get("data") or credentials_resp.json()
        evidence["api_credentials"] = {
            "issued": credentials.get("issued"),
            "api_key_last4": credentials.get("api_key_last4"),
        }
        api_key = args.api_key or credentials.get("api_key")
        if args.mode == "live" and not api_key:
            raise RuntimeError("Merchant API key is not issued; issue a key before running the live canary harness")
        if args.mode == "test" and not args.internal_key:
            raise RuntimeError("READINESS_INTERNAL_API_KEY or --internal-key is required for test canary mode")

        original_route = route_before
        requested_route = _parse_provider_order(args.provider_order)
        if requested_route:
            route_update_resp = await client.put(
                "/merchant/integrations/routing",
                json={"psp_priority": requested_route},
            )
            route_update_resp.raise_for_status()
            evidence["routing_applied"] = route_update_resp.json().get("data") or route_update_resp.json()

        order_id = args.order_id or f"canary_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(4)}"
        payment_request = _build_payment_request(args, order_id)

        execute_headers: Dict[str, str] = {}
        execute_path = "/payment/execute"
        execute_body: Dict[str, Any] = dict(payment_request)

        if args.mode == "test":
            execute_path = f"/payment/internal/canary/merchants/{args.merchant_id}/execute"
            execute_headers["X-Pivota-Internal-Key"] = str(args.internal_key)
            execute_body["emit_merchant_webhook"] = bool(args.emit_merchant_webhook)
        else:
            execute_headers["X-Merchant-API-Key"] = str(api_key)

        execute_resp = await client.post(
            execute_path,
            headers=execute_headers,
            json=execute_body,
        )
        evidence["payment_execute"] = {
            "path": execute_path,
            "status_code": execute_resp.status_code,
            "body": execute_resp.json(),
        }

        deliveries_resp = await client.get("/merchant/webhooks/deliveries?limit=10")
        if deliveries_resp.status_code == 200:
            deliveries_payload = deliveries_resp.json().get("data") or deliveries_resp.json()
            evidence["merchant_webhook_deliveries"] = deliveries_payload

        if requested_route and not args.keep_route:
            restore_resp = await client.put(
                "/merchant/integrations/routing",
                json={
                    "psp_priority": (original_route or {}).get("psp_priority") or [],
                    "routing_strategy": (original_route or {}).get("routing_strategy") or "priority",
                    "max_retries": (original_route or {}).get("max_retries"),
                    "timeout_ms": (original_route or {}).get("timeout_ms"),
                },
            )
            restore_resp.raise_for_status()
            evidence["routing_restored"] = restore_resp.json().get("data") or restore_resp.json()

    evidence["completed_at"] = datetime.now(timezone.utc).isoformat()
    output = json.dumps(evidence, ensure_ascii=True, indent=2)
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
    else:
        print(output)
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_run(_parse_args())))


if __name__ == "__main__":
    main()
