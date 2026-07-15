#!/usr/bin/env python3
import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

import psycopg2


@dataclass(frozen=True)
class OrderRow:
    order_id: str
    merchant_id: str
    store_id: Optional[str]
    shopify_order_id: Optional[str]
    status: Optional[str]
    payment_status: Optional[str]
    fulfillment_status: Optional[str]
    tracking_number: Optional[str]


@dataclass(frozen=True)
class ShopifyStore:
    shop_domain: str
    access_token: str
    webhook_secret: str = ""


def _die(msg: str) -> None:
    raise SystemExit(msg)


def _read_database_url(path: str) -> str:
    raw = open(path, "rb").read()
    text = raw.decode("utf-8", errors="ignore")
    m = re.search(r"(postgres(?:ql)?://[^\s\"']+)", text)
    if not m:
        _die("ERROR: could not find postgresql://... in database url file")
    url = m.group(1).strip().strip('"').strip("'")
    # Trim trailing junk (common when copy/paste from rich text)
    url = re.sub(r"[^A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+$", "", url).rstrip("}").strip()
    return url


def _fp(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _pg_fetch_one(conn, query: str, params: tuple[Any, ...]) -> Optional[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(query, params)
        row = cur.fetchone()
        if not row:
            return None
        cols = [d.name for d in cur.description]
        return {cols[i]: row[i] for i in range(len(cols))}


def _load_order(conn, order_id: str) -> OrderRow:
    row = _pg_fetch_one(
        conn,
        """
        SELECT
          order_id,
          merchant_id,
          store_id,
          shopify_order_id,
          status,
          payment_status,
          fulfillment_status,
          tracking_number
        FROM orders
        WHERE order_id = %s
        LIMIT 1
        """,
        (order_id,),
    )
    if not row:
        _die(f"ERROR: order not found: {order_id}")
    return OrderRow(
        order_id=str(row.get("order_id") or ""),
        merchant_id=str(row.get("merchant_id") or ""),
        store_id=(str(row.get("store_id")) if row.get("store_id") else None),
        shopify_order_id=(str(row.get("shopify_order_id")) if row.get("shopify_order_id") else None),
        status=(str(row.get("status")) if row.get("status") is not None else None),
        payment_status=(str(row.get("payment_status")) if row.get("payment_status") is not None else None),
        fulfillment_status=(str(row.get("fulfillment_status")) if row.get("fulfillment_status") is not None else None),
        tracking_number=(str(row.get("tracking_number")) if row.get("tracking_number") is not None else None),
    )


def _load_shopify_store(conn, store_id: str) -> ShopifyStore:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name='merchant_stores'
            """,
        )
        cols = {str(r[0]) for r in (cur.fetchall() or [])}
    domain_col = "shop_domain" if "shop_domain" in cols else ("domain" if "domain" in cols else None)
    if not domain_col:
        _die("ERROR: merchant_stores has neither shop_domain nor domain column")

    row = _pg_fetch_one(
        conn,
        f"""
        SELECT {domain_col} AS shop_domain, api_key
        FROM merchant_stores
        WHERE store_id = %s
        LIMIT 1
        """,
        (store_id,),
    )
    if not row:
        _die(f"ERROR: store not found: {store_id}")
    shop_domain = (row.get("shop_domain") or "").strip()
    if not shop_domain:
        _die(f"ERROR: store has empty shop_domain: {store_id}")

    api_key = row.get("api_key")
    if isinstance(api_key, str):
        try:
            api_key = json.loads(api_key)
        except Exception:
            api_key = {}
    if not isinstance(api_key, dict):
        api_key = {}
    access_token = str(api_key.get("access_token") or "").strip()
    if not access_token:
        _die(f"ERROR: store has no access_token configured: {store_id}")
    webhook_secret = str(api_key.get("webhook_secret") or "").strip()

    return ShopifyStore(shop_domain=shop_domain, access_token=access_token, webhook_secret=webhook_secret)

def _pick_shopify_store_for_merchant(conn, merchant_id: str) -> ShopifyStore:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name='merchant_stores'
            """,
        )
        cols = {str(r[0]) for r in (cur.fetchall() or [])}
    domain_col = "shop_domain" if "shop_domain" in cols else ("domain" if "domain" in cols else None)
    if not domain_col:
        _die("ERROR: merchant_stores has neither shop_domain nor domain column")

    row = _pg_fetch_one(
        conn,
        f"""
        SELECT store_id, {domain_col} AS shop_domain, api_key
        FROM merchant_stores
        WHERE merchant_id=%s AND platform='shopify' AND status IN ('active','connected')
        ORDER BY connected_at DESC NULLS LAST, created_at DESC
        LIMIT 1
        """,
        (merchant_id,),
    )
    if not row:
        _die(f"ERROR: no active Shopify store for merchant_id={merchant_id}")
    store_id = str(row.get("store_id") or "").strip() or "<unknown>"
    store = _load_shopify_store(conn, store_id) if store_id != "<unknown>" else ShopifyStore(shop_domain=str(row.get("shop_domain") or ""), access_token="")
    if not store.access_token:
        api_key = row.get("api_key")
        if isinstance(api_key, str):
            try:
                api_key = json.loads(api_key)
            except Exception:
                api_key = {}
        if not isinstance(api_key, dict):
            api_key = {}
        access_token = str(api_key.get("access_token") or "").strip()
        if not access_token:
            _die(f"ERROR: store has no access_token configured (picked store_id={store_id})")
        store = ShopifyStore(shop_domain=str(row.get("shop_domain") or ""), access_token=access_token)
    return store


def _shopify_get_order(store: ShopifyStore, shopify_order_id: str, api_version: str) -> dict[str, Any]:
    url = f"https://{store.shop_domain}/admin/api/{api_version}/orders/{shopify_order_id}.json"
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "X-Shopify-Access-Token": store.access_token,
            "Accept": "application/json",
            "User-Agent": "pivota-ops/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            data = json.loads(body)
            order = data.get("order")
            if not isinstance(order, dict):
                _die("ERROR: unexpected Shopify response: missing order")
            return order
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
        _die(f"ERROR: Shopify GET order failed http={e.code} body={raw[:400]}")


def _shopify_hmac_sha256_base64(secret: str, body_bytes: bytes) -> str:
    mac = hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).digest()
    return base64.b64encode(mac).decode("ascii")


def _post_shopify_webhook(
    base_url: str,
    merchant_id: str,
    shop_domain: str,
    topic: str,
    secret: str,
    payload_obj: dict[str, Any],
) -> tuple[int, str]:
    payload_bytes = json.dumps(payload_obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    sig = _shopify_hmac_sha256_base64(secret, payload_bytes)
    url = base_url.rstrip("/") + f"/webhooks/shopify/{merchant_id}"
    req = urllib.request.Request(
        url,
        method="POST",
        data=payload_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Topic": topic,
            "X-Shopify-Shop-Domain": shop_domain,
            "X-Shopify-Hmac-Sha256": sig,
            "X-Shopify-Webhook-Id": f"ops_sim_{int(time.time())}",
            "X-Shopify-Triggered-At": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "User-Agent": "pivota-ops/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return int(resp.status), body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
        return int(e.code), body


def main() -> int:
    ap = argparse.ArgumentParser(description="Simulate Shopify orders/updated webhook using real Shopify order payload.")
    ap.add_argument("--base-url", required=True, help="Backend base URL, e.g. https://api.pivota.cc")
    ap.add_argument("--order-id", required=True, help="Pivota order id, e.g. ORD_...")
    ap.add_argument("--database-url", default=os.getenv("DATABASE_URL", ""), help="Postgres DATABASE_URL (overrides --database-url-file)")
    ap.add_argument("--database-url-file", default=os.getenv("DATABASE_URL_FILE", ""), help="File containing postgresql://... (can include extra text)")
    ap.add_argument("--api-version", default="2025-10", help="Shopify Admin API version (default: 2025-10)")
    ap.add_argument("--client-secret", default="", help="Shopify app client secret (fallback: env SHOPIFY_CLIENT_SECRET)")
    args = ap.parse_args()

    client_secret = (args.client_secret or os.getenv("SHOPIFY_CLIENT_SECRET") or "").strip()

    database_url = (args.database_url or "").strip()
    if not database_url:
        if not args.database_url_file:
            _die("ERROR: missing database url (set --database-url or --database-url-file)")
        database_url = _read_database_url(args.database_url_file)
    conn = psycopg2.connect(database_url)
    conn.autocommit = True
    try:
        before = _load_order(conn, args.order_id)
        if not before.merchant_id:
            _die("ERROR: order has empty merchant_id (unexpected)")
        if not before.shopify_order_id:
            _die("ERROR: order has empty shopify_order_id (Shopify order id required)")

        store = _load_shopify_store(conn, before.store_id) if before.store_id else _pick_shopify_store_for_merchant(conn, before.merchant_id)
        secret = (store.webhook_secret or client_secret or "").strip()
        if not secret:
            _die(
                "ERROR: no webhook secret available. Set per-store webhook_secret in merchant_stores.api_key JSON "
                "or pass --client-secret / env SHOPIFY_CLIENT_SECRET."
            )

        print(f"order_id={before.order_id} merchant_id={before.merchant_id} store_id={before.store_id}")
        print(f"pivota_before: status={before.status} payment={before.payment_status} fulfillment={before.fulfillment_status} tracking={bool(before.tracking_number)}")
        print(
            f"shop_domain={store.shop_domain} access_token_len={len(store.access_token)} "
            f"access_token_fp={_fp(store.access_token)} webhook_secret_present={bool(store.webhook_secret)}"
        )

        shopify_order = _shopify_get_order(store, before.shopify_order_id, args.api_version)
        print(
            "shopify_order: id=%s financial_status=%s fulfillment_status=%s fulfillments=%d"
            % (
                shopify_order.get("id"),
                shopify_order.get("financial_status"),
                shopify_order.get("fulfillment_status"),
                len(shopify_order.get("fulfillments") or []),
            )
        )

        http_status, body = _post_shopify_webhook(
            base_url=args.base_url,
            merchant_id=before.merchant_id,
            shop_domain=store.shop_domain,
            topic="orders/updated",
            secret=secret,
            payload_obj=shopify_order,
        )
        print(f"webhook_http={http_status}")
        try:
            parsed = json.loads(body) if body else {}
        except Exception:
            parsed = {"raw": (body[:400] if body else "")}
        print(f"webhook_resp_keys={list(parsed.keys())}")

        after = _load_order(conn, args.order_id)
        print(f"pivota_after: status={after.status} payment={after.payment_status} fulfillment={after.fulfillment_status} tracking={bool(after.tracking_number)}")
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
