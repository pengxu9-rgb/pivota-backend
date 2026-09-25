#!/usr/bin/env python3
"""Read-only store/source inventory for merchant cleanup decisions."""

from __future__ import annotations

import argparse
import asyncio
import json
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


SUMMARY_TABLES = (
    "products_cache",
    "catalog_products",
    "catalog_skus",
    "catalog_offers",
    "product_group_members",
    "pdp_identity_listing",
)


def _ident(name: str) -> str:
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name or ""):
        raise ValueError(f"unsafe identifier: {name!r}")
    return f'"{name}"'


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


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


async def _store_rows(merchant_id: str) -> list[dict[str, Any]]:
    columns = await _columns("merchant_stores")
    preferred = [
        "store_id",
        "merchant_id",
        "platform",
        "domain",
        "name",
        "status",
        "is_primary",
        "connected_at",
        "last_sync",
        "created_at",
        "updated_at",
    ]
    selected = [column for column in preferred if column in columns]
    rows = await database.fetch_all(
        f"""
        SELECT {", ".join(_ident(column) for column in selected)}
        FROM merchant_stores
        WHERE merchant_id = :merchant_id
        ORDER BY
          CASE WHEN lower(coalesce(status::text, '')) IN ('active', 'connected') THEN 0 ELSE 1 END,
          platform,
          connected_at DESC NULLS LAST,
          created_at DESC NULLS LAST
        """,
        {"merchant_id": merchant_id},
    )
    return [{key: _jsonable(value) for key, value in dict(row).items()} for row in rows]


async def _catalog_merchant(merchant_id: str) -> dict[str, Any] | None:
    columns = await _columns("catalog_merchants")
    if not columns:
        return None
    selected = [
        column
        for column in [
            "merchant_id",
            "merchant_name",
            "primary_platform",
            "status",
            "source_system",
            "source_ref",
            "last_full_sync_at",
            "created_at",
            "updated_at",
        ]
        if column in columns
    ]
    row = await database.fetch_one(
        f"""
        SELECT {", ".join(_ident(column) for column in selected)}
        FROM catalog_merchants
        WHERE merchant_id = :merchant_id
        """,
        {"merchant_id": merchant_id},
    )
    return {key: _jsonable(value) for key, value in dict(row).items()} if row else None


async def _group_counts(table: str, merchant_id: str) -> list[dict[str, Any]]:
    columns = await _columns(table)
    if "merchant_id" not in columns:
        return []
    dimensions = [column for column in ("platform", "source_system", "catalog_track", "offer_mode", "sync_status") if column in columns]
    if dimensions:
        group_expr = ", ".join(_ident(column) for column in dimensions)
        sql = f"""
            SELECT {group_expr}, COUNT(*) AS rows
            FROM {_ident(table)}
            WHERE merchant_id::text = :merchant_id
            GROUP BY {group_expr}
            ORDER BY rows DESC
            LIMIT 30
        """
    else:
        sql = f"""
            SELECT COUNT(*) AS rows
            FROM {_ident(table)}
            WHERE merchant_id::text = :merchant_id
        """
    rows = await database.fetch_all(sql, {"merchant_id": merchant_id})
    return [{key: _jsonable(value) for key, value in dict(row).items()} for row in rows]


async def _drive(args: argparse.Namespace) -> None:
    if not IS_POSTGRES:
        raise SystemExit(
            "Refusing to inspect non-Postgres DATABASE_URL. Production is Cloud "
            "Run (pivota-prod/us-west1); run this inside it, which mounts the "
            "DATABASE_URL secret:\n"
            "  scripts/ops/run_oneoff_job.sh scripts/inspect_merchant_store_sources.py"
        )
    merchant_ids = [value.strip() for value in args.merchant_id if value.strip()]
    if not merchant_ids:
        raise SystemExit("--merchant-id is required")

    await database.connect()
    try:
        merchants: list[dict[str, Any]] = []
        for merchant_id in merchant_ids:
            summaries = {}
            for table in SUMMARY_TABLES:
                summaries[table] = await _group_counts(table, merchant_id)
            merchants.append(
                {
                    "merchant_id": merchant_id,
                    "catalog_merchant": await _catalog_merchant(merchant_id),
                    "stores": await _store_rows(merchant_id),
                    "runtime_source_counts": summaries,
                }
            )
        print(json.dumps({"ok": True, "mode": "read_only", "merchants": merchants}, indent=2, sort_keys=True))
    finally:
        await database.disconnect()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,    formatter_class=argparse.RawDescriptionHelpFormatter,)
    parser.add_argument("--merchant-id", action="append", default=[], help="Merchant ID to inspect.")
    return parser.parse_args()


def main() -> int:
    asyncio.run(_drive(_parse_args()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
