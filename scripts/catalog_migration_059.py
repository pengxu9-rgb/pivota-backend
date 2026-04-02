#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from scripts.catalog_migration_058 import _normalize_database_url


REQUIRED_INDEXES = [
    "idx_catalog_merchants_merchant_name_trgm",
    "idx_catalog_products_title_trgm",
    "idx_catalog_products_brand_trgm",
    "idx_catalog_products_source_product_id_lookup",
    "idx_catalog_skus_title_trgm",
    "idx_catalog_skus_sku_trgm",
    "idx_catalog_skus_source_variant_id_lookup",
]

MIGRATION_PATH = Path(__file__).resolve().parents[1] / "db" / "migrations" / "059_catalog_pivot_search_indexes.sql"

POSTGRES_STATEMENTS = [
    "CREATE EXTENSION IF NOT EXISTS pg_trgm",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_catalog_merchants_merchant_name_trgm ON catalog_merchants USING GIN (LOWER(COALESCE(merchant_name, '')) gin_trgm_ops)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_catalog_products_title_trgm ON catalog_products USING GIN (LOWER(COALESCE(title, '')) gin_trgm_ops)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_catalog_products_brand_trgm ON catalog_products USING GIN (LOWER(COALESCE(brand, '')) gin_trgm_ops)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_catalog_products_source_product_id_lookup ON catalog_products (source_product_id)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_catalog_skus_title_trgm ON catalog_skus USING GIN (LOWER(COALESCE(title, '')) gin_trgm_ops)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_catalog_skus_sku_trgm ON catalog_skus USING GIN (LOWER(COALESCE(sku, '')) gin_trgm_ops)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_catalog_skus_source_variant_id_lookup ON catalog_skus (source_variant_id)",
]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply or verify catalog migration 059 against DATABASE_URL."
    )
    parser.add_argument(
        "--mode",
        choices=("verify", "apply", "apply-verify"),
        default="verify",
    )
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL") or "")
    parser.add_argument("--output-json", default=None)
    parser.add_argument("--output-md", default=None)
    return parser.parse_args()


def _write_if_requested(path_str: Optional[str], content: str) -> None:
    if not path_str:
        return
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _render_markdown(report: Dict[str, Any]) -> str:
    lines = [
        "# Catalog Migration 059 Report",
        "",
        f"- mode: `{report['mode']}`",
        f"- database_kind: `{report['database_kind']}`",
        f"- migration_path: `{report['migration_path']}`",
        f"- success: `{report['success']}`",
        "",
        "## Verification",
        "",
        f"- missing_indexes_count: `{report['verification']['missing_indexes_count']}`",
        "",
    ]
    if report["verification"]["missing_indexes"]:
        lines.append("### Missing Indexes")
        lines.append("")
        for name in report["verification"]["missing_indexes"]:
            lines.append(f"- `{name}`")
        lines.append("")
    return "\n".join(lines) + "\n"


def _verify_sqlite() -> Dict[str, Any]:
    return {
        "existing_indexes_sample": [],
        "missing_indexes": [],
        "missing_indexes_count": 0,
        "skipped": True,
    }


def _apply_sqlite() -> Dict[str, Any]:
    return {"applied": False, "skipped": True}


def _verify_postgres(database_url: str) -> Dict[str, Any]:
    import psycopg2

    conn = psycopg2.connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
            indexes = {row[0] for row in cur.fetchall()}
    finally:
        conn.close()
    missing_indexes = [name for name in REQUIRED_INDEXES if name not in indexes]
    return {
        "existing_indexes_sample": sorted(list(indexes))[:100],
        "missing_indexes": missing_indexes,
        "missing_indexes_count": len(missing_indexes),
    }


def _apply_postgres(database_url: str) -> Dict[str, Any]:
    import psycopg2

    conn = psycopg2.connect(database_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            for statement in POSTGRES_STATEMENTS:
                cur.execute(statement)
    finally:
        conn.close()
    return {"applied": True, "statement_count": len(POSTGRES_STATEMENTS)}


def _run(mode: str, database_url: str) -> Dict[str, Any]:
    normalized_url = _normalize_database_url(database_url)
    if not normalized_url:
        raise SystemExit("ERROR: missing --database-url (or env DATABASE_URL)")

    is_sqlite = normalized_url.startswith("sqlite")
    database_kind = "sqlite" if is_sqlite else "postgres"

    apply_result: Optional[Dict[str, Any]] = None
    if mode in {"apply", "apply-verify"}:
        apply_result = _apply_sqlite() if is_sqlite else _apply_postgres(normalized_url)

    verification = _verify_sqlite() if is_sqlite else _verify_postgres(normalized_url)
    success = verification["missing_indexes_count"] == 0
    if mode == "apply":
        success = bool(apply_result is not None) and success

    return {
        "mode": mode,
        "database_kind": database_kind,
        "migration_path": str(MIGRATION_PATH),
        "apply": apply_result,
        "verification": verification,
        "success": success,
    }


def main() -> int:
    args = _parse_args()
    report = _run(args.mode, args.database_url)
    json_blob = json.dumps(report, indent=2, ensure_ascii=False)
    _write_if_requested(args.output_json, json_blob + "\n")
    _write_if_requested(args.output_md, _render_markdown(report))
    print(json_blob)
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
