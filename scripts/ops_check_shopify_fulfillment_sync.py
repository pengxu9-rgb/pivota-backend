#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import psycopg2
import psycopg2.extras


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _redact_database_url(url: str) -> str:
    return re.sub(r"(postgres(?:ql)?://[^:]+:)[^@]+(@)", r"\1<REDACTED>\2", url)


def _read_database_url_from_file(path: str) -> str:
    raw = open(path, "rb").read()
    text = raw.decode("utf-8", errors="ignore")
    m = re.search(r"(postgres(?:ql)?://[^\s\"']+)", text)
    if not m:
        raise SystemExit(f"Failed to find postgres url in {path}")
    url = m.group(1)
    # trim trailing junk (common when copy/paste from rich text)
    url = re.sub(r"[^A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+$", "", url).rstrip("}")
    return url


def _parse_store_access_token(api_key_raw: str) -> str:
    raw = (api_key_raw or "").strip()
    if not raw:
        return ""
    if raw.startswith("{"):
        try:
            d = json.loads(raw)
            if isinstance(d, dict):
                return str(d.get("access_token") or d.get("api_key") or "")
        except Exception:
            return raw
    return raw


def _parse_token_blob(api_key_raw: str) -> Dict[str, Any]:
    raw = (api_key_raw or "").strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        try:
            d = json.loads(raw)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}
    return {"access_token": raw}


def _fetch_shopify_order(
    *, shop_domain: str, access_token: str, api_version: str, shopify_order_id: str
) -> Dict[str, Any]:
    url = (
        f"https://{shop_domain}/admin/api/{api_version}/orders/{shopify_order_id}.json"
        "?fields=id,order_number,financial_status,fulfillment_status,fulfillments,updated_at,processed_at"
    )
    req = urllib.request.Request(url, headers={"X-Shopify-Access-Token": access_token})
    with urllib.request.urlopen(req, timeout=25) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    return json.loads(body).get("order") or {}


def _slim_shopify_fulfillment(f: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": f.get("id"),
        "status": f.get("status"),
        "created_at": f.get("created_at"),
        "updated_at": f.get("updated_at"),
        "tracking_company": f.get("tracking_company"),
        "tracking_number_present": bool(f.get("tracking_number")),
        "tracking_url_present": bool(f.get("tracking_url")),
    }


def _fp(value: str) -> Optional[str]:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]

def _detect_merchant_stores_domain_column(cur) -> str:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name='merchant_stores'
        """,
    )
    cols = {str(r["column_name"]) for r in (cur.fetchall() or [])}
    if "shop_domain" in cols:
        return "shop_domain"
    if "domain" in cols:
        return "domain"
    raise SystemExit("ERROR: merchant_stores has neither shop_domain nor domain column")


def main() -> int:
    parser = argparse.ArgumentParser(description="Check whether Shopify fulfillment is syncing into Pivota.")
    parser.add_argument("--order-id", required=True)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL", ""))
    parser.add_argument("--database-url-file", default=os.getenv("DATABASE_URL_FILE", ""))
    parser.add_argument("--shopify-api-version", default="2025-10")
    parser.add_argument("--merchant-id", default="")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    database_url = (args.database_url or "").strip()
    if not database_url and args.database_url_file:
        database_url = _read_database_url_from_file(args.database_url_file)
    if not database_url:
        print("ERROR: missing --database-url (or env DATABASE_URL / --database-url-file)", file=sys.stderr)
        return 2

    order_id = args.order_id.strip()
    merchant_id_override = args.merchant_id.strip() or None

    conn = psycopg2.connect(database_url)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    domain_col = _detect_merchant_stores_domain_column(cur)

    cur.execute(
        """
        SELECT order_id, merchant_id, store_id, shopify_order_id, status, payment_status,
               fulfillment_status, shipped_at, tracking_number, carrier
        FROM orders
        WHERE order_id = %s
        """,
        (order_id,),
    )
    order = cur.fetchone()
    if not order:
        print(json.dumps({"status": "not_found", "order_id": order_id}))
        return 1

    merchant_id = merchant_id_override or (order.get("merchant_id") or "")
    shopify_order_id = str(order.get("shopify_order_id") or "").strip()
    out: Dict[str, Any] = {
        "now_utc": _utcnow().isoformat(),
        "order": {k: (str(v) if isinstance(v, datetime) else v) for k, v in order.items() if k != "merchant_id"},
        "merchant_id": merchant_id,
    }

    store_id = str(order.get("store_id") or "").strip() or None
    if store_id:
        cur.execute(
            f"""
            SELECT {domain_col} AS shop_domain, api_key
            FROM merchant_stores
            WHERE store_id=%s
            LIMIT 1
            """,
            (store_id,),
        )
    else:
        cur.execute(
            f"""
            SELECT {domain_col} AS shop_domain, api_key
            FROM merchant_stores
            WHERE merchant_id=%s AND platform='shopify' AND status IN ('active','connected')
            ORDER BY connected_at DESC NULLS LAST, created_at DESC
            LIMIT 1
            """,
            (merchant_id,),
        )
    store = cur.fetchone() or {}
    shop_domain = (store.get("shop_domain") or "").strip()
    token_blob = _parse_token_blob(str(store.get("api_key") or ""))
    access_token = _parse_store_access_token(str(store.get("api_key") or ""))
    webhook_secret = str(token_blob.get("webhook_secret") or "").strip()
    out["shopify_store"] = {
        "shop_domain": shop_domain or None,
        "access_token_len": len(access_token),
        "access_token_fp": _fp(access_token),
        "webhook_secret_present": bool(webhook_secret),
        "webhook_secret_fp": _fp(webhook_secret),
    }

    # Shopify order status (read-only)
    if shop_domain and access_token and shopify_order_id:
        try:
            sj = _fetch_shopify_order(
                shop_domain=shop_domain,
                access_token=access_token,
                api_version=args.shopify_api_version,
                shopify_order_id=shopify_order_id,
            )
            fulfillments = sj.get("fulfillments") or []
            out["shopify_order"] = {
                "shopify_order_id": sj.get("id"),
                "financial_status": sj.get("financial_status"),
                "fulfillment_status": sj.get("fulfillment_status"),
                "processed_at": sj.get("processed_at"),
                "updated_at": sj.get("updated_at"),
                "fulfillments": [_slim_shopify_fulfillment(f) for f in fulfillments[:5]],
            }
        except Exception as e:
            out["shopify_order"] = {"error_type": type(e).__name__, "error": str(e)[:180]}
    else:
        out["shopify_order"] = {"skipped": True, "reason": "missing shop_domain/access_token/shopify_order_id"}

    # Webhook ingestion evidence (no payload)
    cur.execute(
        """
        SELECT id, shop_domain, topic, signature_verified, received_at, occurred_at
        FROM pcs_shopify_webhook_events
        WHERE merchant_id=%s
        ORDER BY received_at DESC
        LIMIT 20
        """,
        (merchant_id,),
    )
    events = cur.fetchall()
    out["recent_shopify_webhook_events"] = [
        {
            "id": e.get("id"),
            "shop_domain": e.get("shop_domain"),
            "topic": e.get("topic"),
            "signature_verified": bool(e.get("signature_verified")),
            "received_at": str(e.get("received_at")),
            "occurred_at": str(e.get("occurred_at")),
        }
        for e in (events or [])
    ]

    conn.close()

    # Heuristic diagnosis
    diagnosis = []
    s_order = out.get("shopify_order") or {}
    pivota_f = (order.get("fulfillment_status") or "").strip().lower()
    shopify_f = str(s_order.get("fulfillment_status") or "").strip().lower()
    if shopify_f in {"fulfilled", "partial"} and pivota_f not in {"shipped", "fulfilled"}:
        diagnosis.append("Shopify shows fulfilled but Pivota is not shipped -> webhook not ingested or processed.")
        if out.get("shopify_store", {}).get("webhook_secret_present"):
            diagnosis.append(
                "Webhook secret is configured for this store. Next: ensure backend is deployed with per-store webhook_secret support and check /metrics reviews_shopify_webhook_total{reason=...}."
            )
        else:
            diagnosis.append(
                "Next: configure webhook signing secret for this store (merchant_stores.api_key JSON webhook_secret) or use a single official Shopify app so global SHOPIFY_CLIENT_SECRET can verify."
            )
        diagnosis.append(
            "Also: update fulfillment tracking in Shopify to trigger a fresh fulfillments/update webhook (Shopify won't replay old events)."
        )
    out["diagnosis"] = diagnosis

    # Avoid printing full DATABASE_URL
    if args.verbose:
        out["database_url_redacted"] = _redact_database_url(database_url)

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
