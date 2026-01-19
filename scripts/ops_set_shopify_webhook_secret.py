#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from typing import Any, Dict, Optional

import psycopg2
import psycopg2.extras


def _fp(value: str) -> Optional[str]:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _read_database_url_from_file(path: str) -> str:
    raw = open(path, "rb").read()
    text = raw.decode("utf-8", errors="ignore")
    m = re.search(r"(postgres(?:ql)?://[^\s\"']+)", text)
    if not m:
        raise SystemExit(f"Failed to find postgres url in {path}")
    url = m.group(1)
    url = re.sub(r"[^A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+$", "", url).rstrip("}").strip()
    return url


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


def main() -> int:
    parser = argparse.ArgumentParser(description="Set Shopify webhook secret for a merchant store (prod ops).")
    parser.add_argument("--merchant-id", required=True)
    parser.add_argument("--shop-domain", required=True, help="Shopify myshopify domain saved in merchant_stores.domain")
    parser.add_argument("--webhook-secret", required=True, help="Shopify webhook signing secret (do NOT paste into logs)")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL", ""))
    parser.add_argument("--database-url-file", default=os.getenv("DATABASE_URL_FILE", ""))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    database_url = (args.database_url or "").strip()
    if not database_url and args.database_url_file:
        database_url = _read_database_url_from_file(args.database_url_file)
    if not database_url:
        raise SystemExit("ERROR: missing --database-url (or env DATABASE_URL / --database-url-file)")

    merchant_id = args.merchant_id.strip()
    shop_domain = args.shop_domain.strip().lower()
    webhook_secret = (args.webhook_secret or "").strip()
    if not webhook_secret:
        raise SystemExit("ERROR: empty --webhook-secret")

    conn = psycopg2.connect(database_url)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """
            SELECT store_id, domain, api_key
            FROM merchant_stores
            WHERE merchant_id=%s AND platform='shopify' AND lower(domain)=lower(%s)
              AND status IN ('active','connected')
            ORDER BY connected_at DESC NULLS LAST, created_at DESC
            LIMIT 1
            """,
            (merchant_id, shop_domain),
        )
        row = cur.fetchone()
        if not row:
            cur.execute(
                """
                SELECT store_id, domain, status
                FROM merchant_stores
                WHERE merchant_id=%s AND platform='shopify'
                ORDER BY connected_at DESC NULLS LAST, created_at DESC
                LIMIT 10
                """,
                (merchant_id,),
            )
            candidates = [dict(r) for r in (cur.fetchall() or [])]
            print(
                json.dumps(
                    {
                        "status": "not_found",
                        "merchant_id": merchant_id,
                        "shop_domain": shop_domain,
                        "candidates": candidates,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 1

        store_id = str(row.get("store_id") or "")
        api_key_raw = str(row.get("api_key") or "")
        token_blob = _parse_token_blob(api_key_raw)

        access_token = str(
            token_blob.get("access_token") or token_blob.get("api_key") or ""
        ).strip()
        token_blob["access_token"] = access_token
        token_blob["webhook_secret"] = webhook_secret
        token_json = json.dumps(token_blob, ensure_ascii=False)

        if not args.dry_run:
            cur.execute(
                """
                UPDATE merchant_stores
                SET api_key=%s
                WHERE store_id=%s
                """,
                (token_json, store_id),
            )
            conn.commit()

        print(
            json.dumps(
                {
                    "status": "success",
                    "dry_run": bool(args.dry_run),
                    "merchant_id": merchant_id,
                    "store_id": store_id,
                    "domain": str(row.get("domain") or ""),
                    "access_token_len": len(access_token),
                    "access_token_fp": _fp(access_token),
                    "webhook_secret_len": len(webhook_secret),
                    "webhook_secret_fp": _fp(webhook_secret),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    finally:
        try:
            cur.close()
        finally:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
