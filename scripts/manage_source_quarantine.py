#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.source_quarantine import (  # noqa: E402
    MATCH_TYPE_DOMAIN,
    QUARANTINE_COLUMNS,
    VALID_MATCH_TYPES,
    create_quarantine,
    bare_domain,
    revoke_quarantine,
    sql_bare_domain,
)

CONFIRM_CREATE = "SOURCE_QUARANTINE_CREATE"
CONFIRM_REVOKE = "SOURCE_QUARANTINE_REVOKE"
SAMPLE_LIMIT = 10


class PsycopgDatabase:
    def __init__(self, database_url: str):
        self.database_url = _normalize_database_url(database_url)
        self.conn = None

    async def __aenter__(self) -> "PsycopgDatabase":
        import psycopg2
        import psycopg2.extras

        self._extras = psycopg2.extras
        self.conn = psycopg2.connect(self.database_url, cursor_factory=psycopg2.extras.RealDictCursor)
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.conn is None:
            return
        if exc_type is None:
            self.conn.commit()
        else:
            self.conn.rollback()
        self.conn.close()
        self.conn = None

    async def fetch_all(self, query: str, values: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
        assert self.conn is not None
        with self.conn.cursor() as cur:
            cur.execute(_to_psycopg_query(query), _coerce_values(values or {}, self._extras))
            return [dict(row) for row in cur.fetchall()]

    async def fetch_one(self, query: str, values: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
        assert self.conn is not None
        with self.conn.cursor() as cur:
            cur.execute(_to_psycopg_query(query), _coerce_values(values or {}, self._extras))
            row = cur.fetchone()
            return dict(row) if row else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage catalog source quarantine overlay rows.")
    parser.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_PUBLIC_URL") or os.getenv("DATABASE_URL") or "",
        help="Postgres connection string. Defaults to DATABASE_PUBLIC_URL or DATABASE_URL.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create")
    create.add_argument("--match-type", required=True, choices=sorted(VALID_MATCH_TYPES))
    create.add_argument("--match-value", required=True)
    create.add_argument("--reason", required=True)
    create.add_argument("--created-by", required=True)
    create.add_argument("--expires-at")
    create.add_argument("--apply", action="store_true")
    create.add_argument("--confirm", default="")

    revoke = subparsers.add_parser("revoke")
    revoke.add_argument("--quarantine-id", required=True, type=int)
    revoke.add_argument("--revoked-by", required=True)
    revoke.add_argument("--apply", action="store_true")
    revoke.add_argument("--confirm", default="")

    list_cmd = subparsers.add_parser("list")
    list_cmd.add_argument("--state", choices=["active", "revoked", "expired"])
    list_cmd.add_argument("--match-type", choices=sorted(VALID_MATCH_TYPES))

    dry_run = subparsers.add_parser("dry-run")
    dry_run.add_argument("--quarantine-id", required=True, type=int)

    dry_run_proposed = subparsers.add_parser("dry-run-proposed")
    dry_run_proposed.add_argument("--match-type", required=True, choices=sorted(VALID_MATCH_TYPES))
    dry_run_proposed.add_argument("--match-value", required=True)

    return parser


def main(
    argv: Optional[list[str]] = None,
    *,
    db_factory: Callable[[str], Any] = PsycopgDatabase,
) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(_main_async(args, db_factory=db_factory))


async def _main_async(args: argparse.Namespace, *, db_factory: Callable[[str], Any]) -> int:
    if not args.database_url:
        raise SystemExit("ERROR: missing --database-url (or env DATABASE_PUBLIC_URL / DATABASE_URL)")

    async with _open_db(db_factory, args.database_url) as db:
        if args.command == "create":
            report = await _handle_create(args, db)
        elif args.command == "revoke":
            report = await _handle_revoke(args, db)
        elif args.command == "list":
            report = await _handle_list(args, db)
        elif args.command == "dry-run":
            report = await _handle_dry_run(args, db)
        elif args.command == "dry-run-proposed":
            report = await dry_run_impact(
                db,
                match_type=args.match_type,
                match_value=args.match_value,
            )
        else:
            raise SystemExit(f"unsupported command: {args.command}")

    print(json.dumps(_json_safe(report), indent=2, sort_keys=True))
    return 0


async def _handle_create(args: argparse.Namespace, db: Any) -> dict[str, Any]:
    expires_at = _parse_datetime(args.expires_at)
    requested = {
        "match_type": args.match_type,
        "match_value": args.match_value,
        "reason": args.reason,
        "created_by": args.created_by,
        "expires_at": expires_at,
    }
    if args.match_type == MATCH_TYPE_DOMAIN:
        # Echo what will actually be STORED, not what was typed. create_quarantine
        # canonicalises domains, so `--match-value www.mintree.us` is written as
        # `mintree.us`; a dry-run that echoes the raw input shows the operator a
        # value that will never appear in the table.
        requested["match_value_as_stored"] = bare_domain(args.match_value)
    if not args.apply:
        return {"action": "create", "dry_run": True, "would_create": requested}
    _require_confirm(args.confirm, CONFIRM_CREATE)
    row = await create_quarantine(
        match_type=args.match_type,
        match_value=args.match_value,
        reason=args.reason,
        created_by=args.created_by,
        expires_at=expires_at,
        db=db,
    )
    return {"action": "create", "dry_run": False, "created": asdict(row)}


async def _handle_revoke(args: argparse.Namespace, db: Any) -> dict[str, Any]:
    existing = await _fetch_quarantine(db, args.quarantine_id)
    if existing is None:
        raise SystemExit(f"ERROR: quarantine_id not found: {args.quarantine_id}")
    if not args.apply:
        return {
            "action": "revoke",
            "dry_run": True,
            "would_revoke": existing,
            "revoked_by": args.revoked_by,
        }
    _require_confirm(args.confirm, CONFIRM_REVOKE)
    row = await revoke_quarantine(
        quarantine_id=args.quarantine_id,
        revoked_by=args.revoked_by,
        db=db,
    )
    return {"action": "revoke", "dry_run": False, "revoked": asdict(row)}


async def _handle_list(args: argparse.Namespace, db: Any) -> dict[str, Any]:
    where = ["1 = 1"]
    values: dict[str, Any] = {}
    if args.state:
        where.append("state = :state")
        values["state"] = args.state
    if args.match_type:
        where.append("match_type = :match_type")
        values["match_type"] = args.match_type
    rows = await db.fetch_all(
        f"""
        SELECT {QUARANTINE_COLUMNS}
        FROM catalog_source_quarantine
        WHERE {" AND ".join(where)}
        ORDER BY created_at DESC, quarantine_id DESC
        """,
        values,
    )
    return {"rows": rows, "count": len(rows)}


async def _handle_dry_run(args: argparse.Namespace, db: Any) -> dict[str, Any]:
    row = await _fetch_quarantine(db, args.quarantine_id)
    if row is None:
        raise SystemExit(f"ERROR: quarantine_id not found: {args.quarantine_id}")
    impact = await dry_run_impact(db, match_type=row["match_type"], match_value=row["match_value"])
    impact["quarantine"] = row
    return impact


async def dry_run_impact(
    db: Any,
    *,
    match_type: str,
    match_value: str,
    sample_limit: int = SAMPLE_LIMIT,
) -> dict[str, Any]:
    if match_type not in VALID_MATCH_TYPES:
        raise SystemExit(f"ERROR: invalid match_type: {match_type}")

    product_clause = _product_match_clause(match_type)
    values = {"match_value": match_value, "limit": sample_limit}

    products = await _catalog_table_impact(db, "catalog_products", "p", product_clause, values)
    skus = await _joined_table_impact(db, "catalog_skus", "s", product_clause, values)
    offers = await _joined_table_impact(db, "catalog_offers", "o", product_clause, values)
    seeds = {"count": 0, "sample": []}
    if match_type == MATCH_TYPE_DOMAIN:
        seeds = await _external_seed_domain_impact(db, values)

    return {
        "match": {"match_type": match_type, "match_value": match_value},
        "tables": {
            "catalog_products": products,
            "catalog_skus": skus,
            "catalog_offers": offers,
            "external_product_seeds": seeds,
        },
    }


async def _catalog_table_impact(
    db: Any,
    table_name: str,
    alias: str,
    product_clause: str,
    values: dict[str, Any],
) -> dict[str, Any]:
    count_row = await db.fetch_one(
        f"SELECT COUNT(*)::int AS count FROM {table_name} {alias} WHERE {product_clause}",
        values,
    )
    sample = await db.fetch_all(
        f"""
        SELECT
          p.product_key,
          p.merchant_id,
          p.platform,
          p.source_domain,
          p.source_system,
          p.source_ref,
          p.title,
          p.brand,
          pil.identity_status,
          ips.serving_eligible
        FROM catalog_products p
        LEFT JOIN pdp_identity_listing pil
          ON pil.source_listing_ref = p.merchant_id || ':' || p.source_product_id
        LEFT JOIN index_pipeline_state ips
          ON ips.content_key = p.content_key
        WHERE {product_clause}
        ORDER BY p.updated_at DESC NULLS LAST, p.product_key
        LIMIT :limit
        """,
        values,
    )
    return {"count": _row_count(count_row), "sample": sample}


async def _joined_table_impact(
    db: Any,
    table_name: str,
    alias: str,
    product_clause: str,
    values: dict[str, Any],
) -> dict[str, Any]:
    count_row = await db.fetch_one(
        f"""
        SELECT COUNT(*)::int AS count
        FROM {table_name} {alias}
        JOIN catalog_products p
          ON p.product_key = {alias}.product_key
        WHERE {product_clause}
        """,
        values,
    )
    sample = await db.fetch_all(
        f"""
        SELECT
          p.product_key,
          p.merchant_id,
          p.platform,
          p.source_domain,
          p.source_system,
          p.source_ref,
          p.title,
          p.brand,
          pil.identity_status,
          ips.serving_eligible
        FROM {table_name} {alias}
        JOIN catalog_products p
          ON p.product_key = {alias}.product_key
        LEFT JOIN pdp_identity_listing pil
          ON pil.source_listing_ref = p.merchant_id || ':' || p.source_product_id
        LEFT JOIN index_pipeline_state ips
          ON ips.content_key = p.content_key
        WHERE {product_clause}
        ORDER BY p.updated_at DESC NULLS LAST, p.product_key
        LIMIT :limit
        """,
        values,
    )
    return {"count": _row_count(count_row), "sample": sample}


async def _external_seed_domain_impact(db: Any, values: dict[str, Any]) -> dict[str, Any]:
    count_row = await db.fetch_one(
        """
        SELECT COUNT(*)::int AS count
        FROM external_product_seeds e
        WHERE {seed_domain_match}
        """.format(seed_domain_match=_seed_domain_match()),
        values,
    )
    sample = await db.fetch_all(
        """
        SELECT
          COALESCE(e.attached_product_key, e.external_product_id, e.id) AS product_key,
          'external_seed' AS merchant_id,
          'external_seed' AS platform,
          e.domain AS source_domain,
          'external_product_seeds' AS source_system,
          e.id AS source_ref,
          e.title,
          COALESCE(
            e.seed_data #>> '{brand,name}',
            e.seed_data->>'brand',
            e.seed_data->>'brand_name',
            e.seed_data->>'vendor'
          ) AS brand,
          pil.identity_status,
          ips.serving_eligible
        FROM external_product_seeds e
        LEFT JOIN catalog_products p
          ON p.product_key = e.attached_product_key
        LEFT JOIN pdp_identity_listing pil
          ON pil.source_listing_ref = 'external_seed:' || e.external_product_id
        LEFT JOIN index_pipeline_state ips
          ON ips.content_key = p.content_key
        WHERE {seed_domain_match}
        ORDER BY e.updated_at DESC NULLS LAST, e.id
        LIMIT :limit
        """,
        values,
    )
    return {"count": _row_count(count_row), "sample": sample}


def _seed_domain_match() -> str:
    """Same normalisation the WRITE side uses. See _product_match_clause."""
    return f'{sql_bare_domain("e.domain")} = {sql_bare_domain(":match_value")}'


def _product_match_clause(match_type: str) -> str:
    if match_type == "domain":
        # The dry-run is the operator's ONLY preview of a destructive action, so
        # it must use the same rule as the write. With a bare lower() it did not:
        # `--dry-run-proposed --match-value www.mintree.us` reported 0 impact
        # while `create` canonicalised to `mintree.us` and blocked 120 products.
        # A preview that under-reports blast radius by 100% is worse than none.
        return f'{sql_bare_domain("p.source_domain")} = {sql_bare_domain(":match_value")}'
    if match_type == "merchant_platform":
        return "p.merchant_id || ':' || p.platform = :match_value"
    if match_type == "source_system_ref":
        return "p.source_system || ':' || p.source_ref = :match_value"
    raise ValueError(f"unsupported match_type: {match_type}")


async def _fetch_quarantine(db: Any, quarantine_id: int) -> Optional[dict[str, Any]]:
    return await db.fetch_one(
        f"""
        SELECT {QUARANTINE_COLUMNS}
        FROM catalog_source_quarantine
        WHERE quarantine_id = :quarantine_id
        """,
        {"quarantine_id": int(quarantine_id)},
    )


@asynccontextmanager
async def _open_db(db_factory: Callable[[str], Any], database_url: str):
    db = db_factory(database_url)
    if hasattr(db, "__aenter__"):
        async with db as opened:
            yield opened
    else:
        yield db


def _require_confirm(actual: str, expected: str) -> None:
    if actual != expected:
        raise SystemExit(f"ERROR: write requires --confirm {expected}")


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    normalized = value.strip().replace("Z", "+00:00")
    return datetime.fromisoformat(normalized)


def _row_count(row: Optional[dict[str, Any]]) -> int:
    if not row:
        return 0
    return int(row.get("count") or 0)


def _normalize_database_url(raw: str) -> str:
    value = str(raw or "").strip()
    if value.startswith("postgres://"):
        value = value.replace("postgres://", "postgresql://", 1)
    return value


def _to_psycopg_query(query: str) -> str:
    return re.sub(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)", r"%(\1)s", query)


def _coerce_values(values: dict[str, Any], extras: Any) -> dict[str, Any]:
    coerced = {}
    for key, value in values.items():
        if isinstance(value, (dict, list)):
            coerced[key] = extras.Json(value)
        else:
            coerced[key] = value
    return coerced


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    return value


if __name__ == "__main__":
    raise SystemExit(main())
