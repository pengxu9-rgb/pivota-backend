#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse


REQUIRED_TABLES = [
    "catalog_merchants",
    "catalog_products",
    "catalog_skus",
    "catalog_offers",
    "catalog_field_facts",
    "catalog_sync_events",
    "catalog_sync_jobs",
    "catalog_quote_snapshots",
    "catalog_payment_incentives",
    "beauty_product_profiles",
]

REQUIRED_INDEXES = [
    "idx_catalog_products_source_identity",
    "idx_catalog_skus_source_identity",
    "idx_catalog_offers_merchant_track",
    "idx_catalog_quote_snapshots_quote_id",
    "idx_beauty_product_profiles_merchant_id",
]

REQUIRED_COLUMN_DEFAULTS = {
    ("catalog_inventory_snapshots", "id"): "nextval(",
    ("catalog_price_snapshots", "id"): "nextval(",
}

MIGRATION_PATH = Path(__file__).resolve().parents[1] / "db" / "migrations" / "058_catalog_core.sql"


def _normalize_database_url(raw: str) -> str:
    value = str(raw or "").strip()
    if value.startswith("postgres://"):
        return value.replace("postgres://", "postgresql://", 1)
    return value


def _sqlite_path_from_url(database_url: str) -> str:
    if database_url.startswith("sqlite+aiosqlite:////"):
        return "/" + database_url[len("sqlite+aiosqlite:////") :]
    if database_url.startswith("sqlite:////"):
        return "/" + database_url[len("sqlite:////") :]
    if database_url.startswith("sqlite+aiosqlite:///"):
        return database_url[len("sqlite+aiosqlite:///") :]
    if database_url.startswith("sqlite:///"):
        return database_url[len("sqlite:///") :]
    parsed = urlparse(database_url)
    if parsed.scheme.startswith("sqlite"):
        return parsed.path or ""
    raise ValueError(f"Unsupported sqlite database url: {database_url}")


def _sqlite_compatible_sql(raw_sql: str) -> str:
    sql = raw_sql.replace("JSONB", "JSON")
    return re.sub(r"DO \$\$.*?END \$\$;", "", sql, flags=re.DOTALL)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply or verify catalog migration 058 against DATABASE_URL."
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
        "# Catalog Migration 058 Report",
        "",
        f"- mode: `{report['mode']}`",
        f"- database_kind: `{report['database_kind']}`",
        f"- migration_path: `{report['migration_path']}`",
        f"- success: `{report['success']}`",
        "",
        "## Verification",
        "",
        f"- missing_tables_count: `{report['verification']['missing_tables_count']}`",
        f"- missing_indexes_count: `{report['verification']['missing_indexes_count']}`",
        f"- missing_column_defaults_count: `{report['verification']['missing_column_defaults_count']}`",
        "",
    ]
    if report["verification"]["missing_tables"]:
        lines.append("### Missing Tables")
        lines.append("")
        for name in report["verification"]["missing_tables"]:
            lines.append(f"- `{name}`")
        lines.append("")
    if report["verification"]["missing_indexes"]:
        lines.append("### Missing Indexes")
        lines.append("")
        for name in report["verification"]["missing_indexes"]:
            lines.append(f"- `{name}`")
        lines.append("")
    if report["verification"]["missing_column_defaults"]:
        lines.append("### Missing Column Defaults")
        lines.append("")
        for item in report["verification"]["missing_column_defaults"]:
            lines.append(
                f"- `{item['table']}.{item['column']}` expected_contains=`{item['expected_contains']}` actual=`{item['actual'] or 'null'}`"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def _verify_sqlite(database_url: str) -> Dict[str, Any]:
    db_path = _sqlite_path_from_url(database_url)
    conn = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
    finally:
        conn.close()
    missing_tables = [name for name in REQUIRED_TABLES if name not in tables]
    missing_indexes = [name for name in REQUIRED_INDEXES if name not in indexes]
    return {
        "existing_tables_sample": sorted(list(tables))[:50],
        "existing_indexes_sample": sorted(list(indexes))[:50],
        "missing_tables": missing_tables,
        "missing_indexes": missing_indexes,
        "missing_column_defaults": [],
        "missing_tables_count": len(missing_tables),
        "missing_indexes_count": len(missing_indexes),
        "missing_column_defaults_count": 0,
    }


def _apply_sqlite(database_url: str, raw_sql: str) -> Dict[str, Any]:
    db_path = _sqlite_path_from_url(database_url)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_sqlite_compatible_sql(raw_sql))
        conn.commit()
    finally:
        conn.close()
    return {"applied": True}


def _verify_postgres(database_url: str) -> Dict[str, Any]:
    import psycopg2

    conn = psycopg2.connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            tables = {row[0] for row in cur.fetchall()}
            cur.execute("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
            indexes = {row[0] for row in cur.fetchall()}
            cur.execute(
                """
                SELECT table_name, column_name, column_default
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND (
                    (table_name = 'catalog_inventory_snapshots' AND column_name = 'id')
                    OR (table_name = 'catalog_price_snapshots' AND column_name = 'id')
                  )
                """
            )
            column_defaults = {
                (row[0], row[1]): str(row[2] or "")
                for row in cur.fetchall()
            }
    finally:
        conn.close()
    missing_tables = [name for name in REQUIRED_TABLES if name not in tables]
    missing_indexes = [name for name in REQUIRED_INDEXES if name not in indexes]
    missing_column_defaults = []
    for key, expected_contains in REQUIRED_COLUMN_DEFAULTS.items():
        actual = column_defaults.get(key, "")
        if expected_contains not in actual:
            missing_column_defaults.append(
                {
                    "table": key[0],
                    "column": key[1],
                    "expected_contains": expected_contains,
                    "actual": actual,
                }
            )
    return {
        "existing_tables_sample": sorted(list(tables))[:100],
        "existing_indexes_sample": sorted(list(indexes))[:100],
        "missing_tables": missing_tables,
        "missing_indexes": missing_indexes,
        "missing_column_defaults": missing_column_defaults,
        "missing_tables_count": len(missing_tables),
        "missing_indexes_count": len(missing_indexes),
        "missing_column_defaults_count": len(missing_column_defaults),
    }


def _apply_postgres(database_url: str, raw_sql: str) -> Dict[str, Any]:
    import psycopg2

    conn = psycopg2.connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(raw_sql)
        conn.commit()
    finally:
        conn.close()
    return {"applied": True}


def _run(mode: str, database_url: str) -> Dict[str, Any]:
    normalized_url = _normalize_database_url(database_url)
    if not normalized_url:
        raise SystemExit("ERROR: missing --database-url (or env DATABASE_URL)")

    raw_sql = MIGRATION_PATH.read_text(encoding="utf-8")
    is_sqlite = normalized_url.startswith("sqlite")
    database_kind = "sqlite" if is_sqlite else "postgres"

    apply_result: Optional[Dict[str, Any]] = None
    if mode in {"apply", "apply-verify"}:
        apply_result = _apply_sqlite(normalized_url, raw_sql) if is_sqlite else _apply_postgres(normalized_url, raw_sql)

    verification = _verify_sqlite(normalized_url) if is_sqlite else _verify_postgres(normalized_url)
    success = (
        verification["missing_tables_count"] == 0
        and verification["missing_indexes_count"] == 0
        and verification["missing_column_defaults_count"] == 0
    )
    if mode == "apply":
        success = bool(apply_result) and success

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
