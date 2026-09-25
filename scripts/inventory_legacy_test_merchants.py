#!/usr/bin/env python3
"""Read-only inventory for legacy/test merchant rows in production DBs."""

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

# This is a short-lived operator script; keep the production pool tiny.
os.environ.setdefault("DB_POOL_MIN_SIZE", "1")
os.environ.setdefault("DB_POOL_MAX_SIZE", "2")
if os.getenv("DATABASE_PUBLIC_URL"):
    os.environ["DATABASE_URL"] = os.getenv("DATABASE_PUBLIC_URL", "")

from config.platform import (  # noqa: E402
    platform_metadata,
    raw_environment_label,
    service_name,
)
from db.database import IS_POSTGRES, database  # noqa: E402


KNOWN_LEGACY_MERCHANT_IDS = (
    "merch_208139f7600dbf42",
    "merch_6b90dc9838d5fd9c",
)

DEFAULT_MATCH_TERMS = (
    "chydan",
    "chydantest",
    "store_shopify_chydantest",
)

BROAD_REVIEW_TERMS = (
    "test",
    "demo",
    "sandbox",
)

RUNTIME_TABLE_PREFERENCE = (
    "merchant_stores",
    "catalog_merchants",
    "products_cache",
    "catalog_products",
    "catalog_skus",
    "catalog_offers",
    "catalog_offer_snapshots",
    "product_group_members",
    "pdp_identity_listing",
    "pdp_identity_review_queue",
)

DISCOVERY_TABLES = (
    "merchant_stores",
    "catalog_merchants",
)

SAMPLE_COLUMN_NAMES = (
    "id",
    "merchant_id",
    "store_id",
    "catalog_merchant_id",
    "name",
    "store_name",
    "display_name",
    "shop_domain",
    "domain",
    "platform",
    "status",
    "source_system",
    "source_kind",
    "created_at",
    "updated_at",
)

MATCH_COLUMN_HINTS = (
    "merchant",
    "store",
    "shop",
    "domain",
    "name",
    "email",
    "source",
    "platform",
)

ACTIVE_STATUSES = ("active", "connected", "enabled", "live")


def _ident(name: str) -> str:
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name or ""):
        raise ValueError(f"unsafe identifier: {name!r}")
    return f'"{name}"'


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


async def _fetch_columns() -> dict[str, dict[str, str]]:
    rows = await database.fetch_all(
        """
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'public'
        ORDER BY table_name, ordinal_position
        """
    )
    out: dict[str, dict[str, str]] = {}
    for row in rows:
        data = dict(row)
        out.setdefault(str(data["table_name"]), {})[str(data["column_name"])] = str(data["data_type"])
    return out


def _text_match_columns(columns: dict[str, str]) -> list[str]:
    out: list[str] = []
    for column, data_type in columns.items():
        lower = column.lower()
        if not any(hint in lower for hint in MATCH_COLUMN_HINTS):
            continue
        if data_type not in {"text", "character varying", "character", "uuid"}:
            continue
        out.append(column)
    return out


def _row_reasons(row: dict[str, Any], terms: list[str], known_ids: set[str]) -> list[str]:
    reasons: list[str] = []
    merchant_id = str(row.get("merchant_id") or "").strip()
    if merchant_id in known_ids:
        reasons.append("known_legacy_merchant_id")
    for key, value in row.items():
        text = str(value or "").lower()
        if not text:
            continue
        for term in terms:
            if term.lower() in text:
                reasons.append(f"{key}_contains_{term}")
    return sorted(set(reasons))


async def _discover_from_table(
    table: str,
    columns: dict[str, str],
    terms: list[str],
    known_ids: set[str],
    limit: int,
) -> list[dict[str, Any]]:
    if "merchant_id" not in columns:
        return []
    match_columns = _text_match_columns(columns)
    if not match_columns:
        return []

    params: dict[str, Any] = {}
    predicates: list[str] = []
    for idx, merchant_id in enumerate(sorted(known_ids)):
        key = f"known_{idx}"
        params[key] = merchant_id
        predicates.append(f"{_ident('merchant_id')}::text = :{key}")

    for column in match_columns:
        for idx, term in enumerate(terms):
            key = f"term_{column}_{idx}".replace("-", "_")
            params[key] = f"%{term.lower()}%"
            predicates.append(f"lower(coalesce({_ident(column)}::text, '')) LIKE :{key}")

    sample_columns = [col for col in SAMPLE_COLUMN_NAMES if col in columns]
    for column in match_columns:
        if column not in sample_columns:
            sample_columns.append(column)

    sql = f"""
        SELECT {", ".join(_ident(col) for col in sample_columns)}
        FROM {_ident(table)}
        WHERE {" OR ".join(predicates)}
        ORDER BY {_ident('merchant_id')} NULLS LAST
        LIMIT {int(limit)}
    """
    rows = await database.fetch_all(sql, params)
    out: list[dict[str, Any]] = []
    for row in rows:
        data = {key: _jsonable(value) for key, value in dict(row).items()}
        out.append(
            {
                "table": table,
                "merchant_id": str(data.get("merchant_id") or ""),
                "reasons": _row_reasons(data, terms, known_ids),
                "sample": data,
            }
        )
    return out


async def _count_table(table: str, columns: dict[str, str], merchant_id: str) -> dict[str, Any]:
    if "merchant_id" not in columns:
        return {"rows": None, "skipped": "missing_merchant_id"}
    params = {"merchant_id": merchant_id}
    total = await database.fetch_one(
        f"SELECT COUNT(*) AS n FROM {_ident(table)} WHERE {_ident('merchant_id')}::text = :merchant_id",
        params,
    )
    result: dict[str, Any] = {"rows": int(dict(total or {}).get("n") or 0)}
    if "status" in columns:
        active = await database.fetch_one(
            f"""
            SELECT COUNT(*) AS n
            FROM {_ident(table)}
            WHERE {_ident('merchant_id')}::text = :merchant_id
              AND lower(coalesce({_ident('status')}::text, '')) IN ('active', 'connected', 'enabled', 'live')
            """,
            params,
        )
        result["active_status_rows"] = int(dict(active or {}).get("n") or 0)
    if "is_active" in columns:
        active_bool = await database.fetch_one(
            f"""
            SELECT COUNT(*) AS n
            FROM {_ident(table)}
            WHERE {_ident('merchant_id')}::text = :merchant_id
              AND {_ident('is_active')} IS TRUE
            """,
            params,
        )
        result["is_active_rows"] = int(dict(active_bool or {}).get("n") or 0)
    return result


async def _sample_table(table: str, columns: dict[str, str], merchant_id: str, limit: int) -> list[dict[str, Any]]:
    if "merchant_id" not in columns:
        return []
    sample_columns = [col for col in SAMPLE_COLUMN_NAMES if col in columns]
    if not sample_columns:
        sample_columns = ["merchant_id"]
    rows = await database.fetch_all(
        f"""
        SELECT {", ".join(_ident(col) for col in sample_columns)}
        FROM {_ident(table)}
        WHERE {_ident('merchant_id')}::text = :merchant_id
        LIMIT {int(limit)}
        """,
        {"merchant_id": merchant_id},
    )
    return [{key: _jsonable(value) for key, value in dict(row).items()} for row in rows]


async def _drive(args: argparse.Namespace) -> None:
    if not IS_POSTGRES:
        raise SystemExit(
            "Refusing to inventory non-Postgres DATABASE_URL. Production is Cloud "
            "Run (pivota-prod/us-west1); run this inside it, which mounts the "
            "DATABASE_URL secret:\n"
            "  scripts/ops/run_oneoff_job.sh scripts/inventory_legacy_test_merchants.py"
        )

    known_ids = set(KNOWN_LEGACY_MERCHANT_IDS)
    known_ids.update(value.strip() for value in args.merchant_id if value.strip())
    terms = list(DEFAULT_MATCH_TERMS)
    if args.include_broad_review_terms:
        terms.extend(BROAD_REVIEW_TERMS)
    terms.extend(value.strip().lower() for value in args.term if value.strip())
    terms = sorted(set(terms))

    await database.connect()
    try:
        columns_by_table = await _fetch_columns()
        discovered: list[dict[str, Any]] = []
        for table in DISCOVERY_TABLES:
            columns = columns_by_table.get(table)
            if not columns:
                continue
            discovered.extend(await _discover_from_table(table, columns, terms, known_ids, args.discovery_limit))

        candidate_ids = {
            item["merchant_id"]
            for item in discovered
            if item.get("merchant_id")
        }
        candidate_ids.update(known_ids)

        merchant_tables = sorted(
            table for table, columns in columns_by_table.items() if "merchant_id" in columns
        )
        preferred = [table for table in RUNTIME_TABLE_PREFERENCE if table in columns_by_table]
        extra_tables = [table for table in merchant_tables if table not in preferred]
        count_tables = preferred + (extra_tables if args.include_all_merchant_tables else [])

        merchants: list[dict[str, Any]] = []
        for merchant_id in sorted(candidate_ids):
            table_counts: dict[str, Any] = {}
            for table in count_tables:
                counts = await _count_table(table, columns_by_table[table], merchant_id)
                if counts.get("rows") or counts.get("active_status_rows") or counts.get("is_active_rows"):
                    table_counts[table] = counts
            samples: dict[str, Any] = {}
            for table in ("merchant_stores", "catalog_merchants"):
                if table in columns_by_table:
                    rows = await _sample_table(table, columns_by_table[table], merchant_id, args.sample_limit)
                    if rows:
                        samples[table] = rows
            merchants.append(
                {
                    "merchant_id": merchant_id,
                    "table_counts": table_counts,
                    "samples": samples,
                }
            )

        result = {
            "ok": True,
            "mode": "read_only",
            # Key kept as "railway" so already-archived inventory JSON stays
            # comparable, but the VALUES now resolve through config.platform.
            # Read directly, these two were RAILWAY_SERVICE_NAME /
            # RAILWAY_ENVIRONMENT_NAME — both null on Cloud Run, so every
            # post-cutover audit artefact would have recorded "which host and
            # environment produced this" as null and nobody would have noticed
            # until they needed it.
            "railway": {
                "service": service_name(),
                "environment": raw_environment_label(),
            },
            "platform": platform_metadata(),
            "match_terms": terms,
            "known_legacy_merchant_ids": sorted(known_ids),
            "candidate_count": len(merchants),
            "discovered_rows": discovered,
            "candidate_merchants": merchants,
        }
        print(json.dumps(result, indent=2, sort_keys=True))
    finally:
        await database.disconnect()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,    formatter_class=argparse.RawDescriptionHelpFormatter,)
    parser.add_argument("--merchant-id", action="append", default=[], help="Additional merchant_id to inspect.")
    parser.add_argument("--term", action="append", default=[], help="Additional lowercase text term to match.")
    parser.add_argument("--include-broad-review-terms", action="store_true", help="Also match test/demo/sandbox.")
    parser.add_argument("--include-all-merchant-tables", action="store_true", help="Count every public table with merchant_id.")
    parser.add_argument("--discovery-limit", type=int, default=100)
    parser.add_argument("--sample-limit", type=int, default=3)
    return parser.parse_args()


def main() -> int:
    asyncio.run(_drive(_parse_args()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
