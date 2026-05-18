#!/usr/bin/env python3
"""Quarantine specific merchant store connections without disabling the merchant."""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("DB_POOL_MIN_SIZE", "1")
os.environ.setdefault("DB_POOL_MAX_SIZE", "2")
if os.getenv("DATABASE_PUBLIC_URL"):
    os.environ["DATABASE_URL"] = os.getenv("DATABASE_PUBLIC_URL", "")

from db.database import IS_POSTGRES, database  # noqa: E402


ACTIVE_STATUS_SQL = "lower(coalesce(status::text, '')) IN ('active', 'connected')"


def _ident(name: str) -> str:
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name or ""):
        raise ValueError(f"unsafe identifier: {name!r}")
    return f'"{name}"'


def _fmt(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("\n", " ").strip()


async def _columns(table: str) -> set[str]:
    rows = await database.fetch_all(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = :table
        """,
        {"table": table},
    )
    return {str(dict(row)["column_name"]) for row in rows}


async def _store_snapshot(store_id: str) -> dict[str, Any] | None:
    columns = await _columns("merchant_stores")
    selected = [
        column
        for column in (
            "store_id",
            "merchant_id",
            "platform",
            "domain",
            "name",
            "status",
            "connected_at",
            "last_sync",
            "created_at",
            "updated_at",
        )
        if column in columns
    ]
    row = await database.fetch_one(
        f"""
        SELECT {", ".join(_ident(column) for column in selected)}
        FROM merchant_stores
        WHERE store_id = :store_id
        """,
        {"store_id": store_id},
    )
    return dict(row) if row else None


async def _active_store_count(merchant_id: str) -> int:
    row = await database.fetch_one(
        f"""
        SELECT COUNT(*) AS n
        FROM merchant_stores
        WHERE merchant_id = :merchant_id
          AND {ACTIVE_STATUS_SQL}
        """,
        {"merchant_id": merchant_id},
    )
    return int(dict(row or {}).get("n") or 0)


async def _active_catalog_merchant_count(merchant_id: str) -> int:
    row = await database.fetch_one(
        """
        SELECT COUNT(*) AS n
        FROM catalog_merchants
        WHERE merchant_id = :merchant_id
          AND lower(coalesce(status::text, 'active')) = 'active'
        """,
        {"merchant_id": merchant_id},
    )
    return int(dict(row or {}).get("n") or 0)


async def _drive(args: argparse.Namespace) -> None:
    if not IS_POSTGRES:
        raise SystemExit("Refusing to quarantine non-Postgres DATABASE_URL; use Railway production env.")
    store_ids = [value.strip() for value in args.store_id if value.strip()]
    if not store_ids:
        raise SystemExit("--store-id is required")

    await database.connect()
    try:
        snapshots = []
        print("=== Store quarantine plan ===")
        for store_id in store_ids:
            store = await _store_snapshot(store_id)
            if not store:
                print(f"store_id={store_id} missing")
                continue
            merchant_id = str(store.get("merchant_id") or "")
            active_before = await _active_store_count(merchant_id)
            catalog_active_before = await _active_catalog_merchant_count(merchant_id)
            snapshots.append((store, active_before, catalog_active_before))
            print(
                "store_id={store_id} merchant_id={merchant_id} platform={platform} "
                "domain={domain} name={name} status={status} active_stores_before={active} "
                "active_catalog_merchants_before={catalog_active}".format(
                    store_id=_fmt(store.get("store_id")),
                    merchant_id=_fmt(merchant_id),
                    platform=_fmt(store.get("platform")),
                    domain=_fmt(store.get("domain")),
                    name=_fmt(store.get("name")),
                    status=_fmt(store.get("status")),
                    active=active_before,
                    catalog_active=catalog_active_before,
                )
            )

        if not args.apply:
            print("\nDRY-RUN - no writes. Re-run with --apply to mark these stores inactive.")
            return

        for store, _active_before, _catalog_active_before in snapshots:
            await database.execute(
                """
                UPDATE merchant_stores
                SET status = 'inactive'
                WHERE store_id = :store_id
                  AND lower(coalesce(status::text, '')) IN ('active', 'connected')
                """,
                {"store_id": store["store_id"]},
            )

        if args.inactivate_catalog_merchant_when_no_active_stores:
            merchant_ids = sorted({str(store.get("merchant_id") or "") for store, _, _ in snapshots if store.get("merchant_id")})
            for merchant_id in merchant_ids:
                remaining = await _active_store_count(merchant_id)
                if remaining == 0:
                    await database.execute(
                        """
                        UPDATE catalog_merchants
                        SET status = 'inactive',
                            updated_at = NOW()
                        WHERE merchant_id = :merchant_id
                          AND lower(coalesce(status::text, 'active')) = 'active'
                        """,
                        {"merchant_id": merchant_id},
                    )

        print("\nAPPLY complete.")
        print("\n=== Post-quarantine counts ===")
        merchant_ids = sorted({str(store.get("merchant_id") or "") for store, _, _ in snapshots if store.get("merchant_id")})
        for merchant_id in merchant_ids:
            print(
                f"merchant_id={merchant_id} active_stores={await _active_store_count(merchant_id)} "
                f"active_catalog_merchants={await _active_catalog_merchant_count(merchant_id)}"
            )
    finally:
        await database.disconnect()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store-id", action="append", default=[], help="Specific merchant_stores.store_id to quarantine.")
    parser.add_argument("--apply", action="store_true", help="Write quarantine updates. Default is dry-run.")
    parser.add_argument(
        "--inactivate-catalog-merchant-when-no-active-stores",
        action="store_true",
        help="After store quarantine, inactivate catalog_merchants only for merchants left with zero active stores.",
    )
    return parser.parse_args()


def main() -> int:
    asyncio.run(_drive(_parse_args()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
