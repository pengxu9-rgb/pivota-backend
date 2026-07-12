"""Register a merchant's protocol/checkout capability (Fix Plan A, option (ii)).

Dark provisioning helper: upserts a ``pcs_merchant_capabilities`` row for a pilot
merchant so the capability-based checkout gate
(``AGENT_CHECKOUT_CAPABILITY_GATE``, see services/merchant_capability_gate.py) can
recognize the merchant as transaction-capable WITHOUT a ``merchant_psps`` row.

Safe by construction:

* DRY-RUN by default. Nothing is written unless ``--apply`` is passed.
* Idempotent upsert (``ON CONFLICT (merchant_id) DO UPDATE``), same table/shape
  written by the Shopify integration-verify path.
* Registering a capability row alone changes NOTHING until the gate flag is on —
  the flag is the actual switch.

Usage (staging/prod are gated by the operator supplying DATABASE_URL):

    # preview only (no write)
    python -m scripts.register_merchant_protocol_capability --merchant-id merch_xxx

    # write the row
    python -m scripts.register_merchant_protocol_capability \
        --merchant-id merch_xxx --apply \
        --shopify-api-version 2024-10 --has-shopify-payments

    # list current capability rows (audit)
    python -m scripts.register_merchant_protocol_capability --list
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from typing import Any, Dict, List

from db.database import database

logger = logging.getLogger("register_merchant_protocol_capability")

UPSERT_SQL = """
    INSERT INTO pcs_merchant_capabilities
      (merchant_id, shopify_api_version, scopes_json, has_shopify_payments, has_returns_api, last_checked_at)
    VALUES
      (:merchant_id, :shopify_api_version, CAST(:scopes_json AS jsonb), :has_shopify_payments, :has_returns_api, NOW())
    ON CONFLICT (merchant_id) DO UPDATE SET
      shopify_api_version = EXCLUDED.shopify_api_version,
      scopes_json = EXCLUDED.scopes_json,
      has_shopify_payments = EXCLUDED.has_shopify_payments,
      has_returns_api = EXCLUDED.has_returns_api,
      last_checked_at = EXCLUDED.last_checked_at
"""


async def _connect() -> None:
    if not getattr(database, "is_connected", False):
        await database.connect()


async def _list_rows() -> List[Dict[str, Any]]:
    rows = await database.fetch_all(
        """
        SELECT merchant_id, shopify_api_version, has_shopify_payments,
               has_returns_api, last_checked_at
        FROM pcs_merchant_capabilities
        ORDER BY merchant_id
        """
    )
    return [dict(r) for r in rows or []]


async def _drive(args: argparse.Namespace) -> Dict[str, Any]:
    await _connect()

    if args.list:
        rows = await _list_rows()
        return {"action": "list", "count": len(rows), "rows": rows}

    if not args.merchant_id:
        raise SystemExit("--merchant-id is required (or use --list)")

    params = {
        "merchant_id": args.merchant_id.strip(),
        "shopify_api_version": args.shopify_api_version,
        "scopes_json": json.dumps(json.loads(args.scopes_json) if args.scopes_json else {}),
        "has_shopify_payments": bool(args.has_shopify_payments),
        "has_returns_api": bool(args.has_returns_api),
    }

    if not args.apply:
        return {
            "action": "dry_run",
            "would_upsert": params,
            "note": "no write performed; pass --apply to persist",
        }

    await database.execute(UPSERT_SQL, params)
    logger.info("upserted pcs_merchant_capabilities row for %s", params["merchant_id"])
    return {"action": "applied", "upserted": params}


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--merchant-id", dest="merchant_id", default=None)
    p.add_argument("--shopify-api-version", dest="shopify_api_version", default=None)
    p.add_argument("--scopes-json", dest="scopes_json", default=None,
                   help='JSON object of granted scopes, e.g. \'{"read_products": true}\'')
    p.add_argument("--has-shopify-payments", dest="has_shopify_payments", action="store_true")
    p.add_argument("--has-returns-api", dest="has_returns_api", action="store_true")
    p.add_argument("--list", dest="list", action="store_true",
                   help="list current capability rows and exit")
    p.add_argument("--apply", dest="apply", action="store_true",
                   help="actually write (default is dry-run)")
    return p.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()
    report = asyncio.run(_drive(args))
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
