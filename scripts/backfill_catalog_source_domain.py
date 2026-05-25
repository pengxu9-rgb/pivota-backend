#!/usr/bin/env python3
"""Backfill catalog source_domain values from historical provenance.

Resolution rules by source_system:

- external_product_seeds_mirror_v1:
  catalog_products.source_ref stores external_product_seeds.id. Use that
  seed's domain. SKUs/offers inherit through product_key after the product
  mapping is resolved.

- shopify_products_sync:
  Prefer a row-embedded store domain under product_payload source/store/
  platform_metadata shapes. If absent, look for a known merchant_stores.domain
  inside source_ref or the payload text. If the merchant has exactly one
  historical Shopify store, use that domain. Multi-store rows with no row-level
  clue stay NULL.

- universal_product_sync:
  Same as Shopify, scoped to the row platform. Wix may store a site_id instead
  of a hostname, so site_id clues are matched against merchant_stores.domain,
  store_id, and credential JSON in memory; only the store domain is written.

- catalog_enrichment_agent_v1:
  Enrichment runs are not store-scoped; leave NULL and report.

- external_seed_catalog_mirror_v1:
  Legacy external-seed mirror. Resolve by source_ref -> seed id first, then
  attached_product_key -> product_key if source_ref is not usable.

Dry-run/report are read-only. Apply is idempotent and updates only rows where
source_domain IS NULL. Production apply requires both the normal confirmation
token and an explicit production authorization token; this dispatch must not
use the production token.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlparse


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


CONFIRM_TOKEN = "BACKFILL_CATALOG_SOURCE_DOMAIN"
PROD_CONFIRM_TOKEN = "I_HAVE_USER_AUTHORIZATION"
WRITER_NAME = "backfill_catalog_source_domain"
SAMPLE_LIMIT = 10
CHYDAN_MERCHANT_ID = "merch_efbc46b4619cfbdf"

SOURCE_EXTERNAL_SEEDS = "external_product_seeds_mirror_v1"
SOURCE_SHOPIFY = "shopify_products_sync"
SOURCE_UNIVERSAL = "universal_product_sync"
SOURCE_ENRICHMENT = "catalog_enrichment_agent_v1"
SOURCE_LEGACY_EXTERNAL_SEED = "external_seed_catalog_mirror_v1"

KNOWN_SOURCES = [
    SOURCE_EXTERNAL_SEEDS,
    SOURCE_SHOPIFY,
    SOURCE_UNIVERSAL,
    SOURCE_ENRICHMENT,
    SOURCE_LEGACY_EXTERNAL_SEED,
]
LOW_RECOVERY_EXEMPT_SOURCES = {SOURCE_ENRICHMENT}


@dataclass(frozen=True)
class ProductResolution:
    product_key: str
    merchant_id: str
    platform: str
    source_system: str
    source_ref: Optional[str]
    current_source_domain: Optional[str]
    resolved_source_domain: Optional[str]
    resolution_rule: str
    unresolved_reason: Optional[str] = None

    @property
    def recoverable(self) -> bool:
        return bool(self.resolved_source_domain)


@dataclass(frozen=True)
class StoreDomain:
    merchant_id: str
    platform: str
    domain: str
    status: Optional[str] = None
    store_id: Optional[str] = None
    site_ids: tuple[str, ...] = ()


class PsycopgDatabase:
    def __init__(self, database_url: str):
        self.database_url = _normalize_database_url(database_url)
        self.conn = None

    async def __aenter__(self) -> "PsycopgDatabase":
        import psycopg2
        import psycopg2.extras

        self._extras = psycopg2.extras
        self.conn = psycopg2.connect(self.database_url, cursor_factory=psycopg2.extras.RealDictCursor)
        self.conn.autocommit = False
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

    async def execute(self, query: str, values: Optional[dict[str, Any]] = None) -> int:
        assert self.conn is not None
        with self.conn.cursor() as cur:
            cur.execute(_to_psycopg_query(query), _coerce_values(values or {}, self._extras))
            return int(cur.rowcount or 0)


class CatalogSourceDomainBackfill:
    def __init__(self, db: Any, *, sample_limit: int = SAMPLE_LIMIT):
        self.db = db
        self.sample_limit = int(sample_limit)
        self._store_domains: Optional[dict[tuple[str, str], list[StoreDomain]]] = None

    async def resolve_source(self, source_system: str) -> list[ProductResolution]:
        if source_system == SOURCE_EXTERNAL_SEEDS:
            return await self.resolve_external_product_seeds_mirror()
        if source_system == SOURCE_SHOPIFY:
            return await self.resolve_shopify_products_sync()
        if source_system == SOURCE_UNIVERSAL:
            return await self.resolve_universal_product_sync()
        if source_system == SOURCE_ENRICHMENT:
            return await self.resolve_catalog_enrichment_agent()
        if source_system == SOURCE_LEGACY_EXTERNAL_SEED:
            return await self.resolve_external_seed_catalog_mirror()
        raise ValueError(f"unsupported source_system: {source_system}")

    async def resolve_external_product_seeds_mirror(self) -> list[ProductResolution]:
        rows = await self.db.fetch_all(
            """
            SELECT
              cp.product_key,
              cp.merchant_id,
              cp.platform,
              cp.source_system,
              cp.source_ref,
              cp.source_domain AS current_source_domain,
              COALESCE(cp.source_domain, NULLIF(BTRIM(eps.domain), '')) AS resolved_source_domain,
              CASE
                WHEN cp.source_domain IS NOT NULL THEN 'existing_product_source_domain'
                ELSE 'external_product_seeds.id'
              END AS resolution_rule
            FROM catalog_products cp
            LEFT JOIN external_product_seeds eps
              ON cp.source_ref = eps.id::text
            WHERE cp.source_system = :source_system
              AND (
                cp.source_domain IS NULL
                OR EXISTS (
                  SELECT 1 FROM catalog_skus s
                  WHERE s.product_key = cp.product_key
                    AND s.source_domain IS NULL
                )
                OR EXISTS (
                  SELECT 1 FROM catalog_offers o
                  WHERE o.product_key = cp.product_key
                    AND o.source_domain IS NULL
                )
              )
            ORDER BY cp.product_key
            """,
            {"source_system": SOURCE_EXTERNAL_SEEDS},
        )
        return [
            ProductResolution(
                product_key=str(row["product_key"]),
                merchant_id=str(row.get("merchant_id") or ""),
                platform=str(row.get("platform") or ""),
                source_system=SOURCE_EXTERNAL_SEEDS,
                source_ref=_clean_optional(row.get("source_ref")),
                current_source_domain=_clean_optional(row.get("current_source_domain")),
                resolved_source_domain=_normalize_domain(row.get("resolved_source_domain")),
                resolution_rule=str(row.get("resolution_rule") or "external_product_seeds.id"),
                unresolved_reason=None
                if _normalize_domain(row.get("resolved_source_domain"))
                else "seed_missing_or_empty_domain",
            )
            for row in rows
        ]

    async def resolve_shopify_products_sync(self) -> list[ProductResolution]:
        return await self._resolve_store_sync_source(
            source_system=SOURCE_SHOPIFY,
            platform_hint="shopify",
        )

    async def resolve_universal_product_sync(self) -> list[ProductResolution]:
        return await self._resolve_store_sync_source(
            source_system=SOURCE_UNIVERSAL,
            platform_hint=None,
        )

    async def resolve_catalog_enrichment_agent(self) -> list[ProductResolution]:
        rows = await self._fetch_source_candidate_products(SOURCE_ENRICHMENT)
        return [
            ProductResolution(
                product_key=str(row["product_key"]),
                merchant_id=str(row.get("merchant_id") or ""),
                platform=str(row.get("platform") or ""),
                source_system=SOURCE_ENRICHMENT,
                source_ref=_clean_optional(row.get("source_ref")),
                current_source_domain=_clean_optional(row.get("current_source_domain")),
                resolved_source_domain=_normalize_domain(row.get("current_source_domain")),
                resolution_rule="existing_product_source_domain"
                if _normalize_domain(row.get("current_source_domain"))
                else "not_store_scoped",
                unresolved_reason=None
                if _normalize_domain(row.get("current_source_domain"))
                else "enrichment_not_store_scoped",
            )
            for row in rows
        ]

    async def resolve_external_seed_catalog_mirror(self) -> list[ProductResolution]:
        rows = await self.db.fetch_all(
            """
            SELECT
              cp.product_key,
              cp.merchant_id,
              cp.platform,
              cp.source_system,
              cp.source_ref,
              cp.source_domain AS current_source_domain,
              COALESCE(cp.source_domain, NULLIF(BTRIM(COALESCE(eps_ref.domain, eps_attached.domain)), '')) AS resolved_source_domain,
              CASE
                WHEN cp.source_domain IS NOT NULL THEN 'existing_product_source_domain'
                WHEN eps_ref.id IS NOT NULL THEN 'external_product_seeds.id'
                WHEN eps_attached.id IS NOT NULL THEN 'external_product_seeds.attached_product_key'
                ELSE 'legacy_external_seed_unresolved'
              END AS resolution_rule
            FROM catalog_products cp
            LEFT JOIN external_product_seeds eps_ref
              ON cp.source_ref = eps_ref.id::text
            LEFT JOIN external_product_seeds eps_attached
              ON eps_attached.attached_product_key = cp.product_key
            WHERE cp.source_system = :source_system
              AND (
                cp.source_domain IS NULL
                OR EXISTS (
                  SELECT 1 FROM catalog_skus s
                  WHERE s.product_key = cp.product_key
                    AND s.source_domain IS NULL
                )
                OR EXISTS (
                  SELECT 1 FROM catalog_offers o
                  WHERE o.product_key = cp.product_key
                    AND o.source_domain IS NULL
                )
              )
            ORDER BY cp.product_key
            """,
            {"source_system": SOURCE_LEGACY_EXTERNAL_SEED},
        )
        return [
            ProductResolution(
                product_key=str(row["product_key"]),
                merchant_id=str(row.get("merchant_id") or ""),
                platform=str(row.get("platform") or ""),
                source_system=SOURCE_LEGACY_EXTERNAL_SEED,
                source_ref=_clean_optional(row.get("source_ref")),
                current_source_domain=_clean_optional(row.get("current_source_domain")),
                resolved_source_domain=_normalize_domain(row.get("resolved_source_domain")),
                resolution_rule=str(row.get("resolution_rule") or "legacy_external_seed_unresolved"),
                unresolved_reason=None
                if _normalize_domain(row.get("resolved_source_domain"))
                else "seed_missing_or_empty_domain",
            )
            for row in rows
        ]

    async def dry_run(self, source_systems: list[str]) -> dict[str, Any]:
        per_source: dict[str, Any] = {}
        headline = {
            "total_null_rows": 0,
            "total_recoverable": 0,
            "total_unrecoverable": 0,
            "table_null_rows": {"catalog_products": 0, "catalog_skus": 0, "catalog_offers": 0},
            "recoverable_table_rows": {"catalog_products": 0, "catalog_skus": 0, "catalog_offers": 0},
        }

        for source_system in source_systems:
            resolutions = await self.resolve_source(source_system)
            table_null_rows = await self._table_null_counts(source_system)
            recoverable_table_rows = await self._recoverable_table_counts(resolutions)
            source_report = self._source_report(
                source_system,
                resolutions,
                table_null_rows=table_null_rows,
                recoverable_table_rows=recoverable_table_rows,
            )
            per_source[source_system] = source_report
            headline["total_null_rows"] += source_report["total_null_rows"]
            headline["total_recoverable"] += source_report["recoverable_rows"]
            headline["total_unrecoverable"] += source_report["unrecoverable_rows"]
            for table_name, count in table_null_rows.items():
                headline["table_null_rows"][table_name] += count
            for table_name, count in recoverable_table_rows.items():
                headline["recoverable_table_rows"][table_name] += count

        report = {
            "mode": "dry-run",
            "target": None,
            "headline": headline,
            "sources": per_source,
            "other_null_source_systems": await self._other_null_source_systems(source_systems),
            "chydan_shopify_breakout": await self._chydan_shopify_breakout(),
        }
        report["dry_run_report_hash"] = _report_hash(report)
        return report

    async def apply(self, *, source_system: str, target: str, actor: str) -> dict[str, Any]:
        resolutions = await self.resolve_source(source_system)
        dry_run_source = self._source_report(
            source_system,
            resolutions,
            table_null_rows=await self._table_null_counts(source_system),
            recoverable_table_rows=await self._recoverable_table_counts(resolutions),
        )
        _raise_if_low_recovery(source_system, dry_run_source)
        await self._raise_if_store_domain_mismatch(source_system, resolutions)

        mappings = [
            {"product_key": row.product_key, "source_domain": row.resolved_source_domain}
            for row in resolutions
            if row.recoverable
        ]

        batch_id = f"{target}:{source_system}:{uuid.uuid4()}"
        applied = {"catalog_products": 0, "catalog_skus": 0, "catalog_offers": 0}
        if mappings:
            mapping_json = json.dumps(mappings, sort_keys=True)
            applied["catalog_products"] = await self._update_table_from_mapping(
                "catalog_products",
                key_column="product_key",
                mapping_json=mapping_json,
            )
            applied["catalog_skus"] = await self._update_table_from_mapping(
                "catalog_skus",
                key_column="product_key",
                mapping_json=mapping_json,
            )
            applied["catalog_offers"] = await self._update_table_from_mapping(
                "catalog_offers",
                key_column="product_key",
                mapping_json=mapping_json,
            )

        applied_rows = sum(applied.values())
        reasons = {
            "source_system": source_system,
            "target": target,
            "product_null_rows": dry_run_source["total_null_rows"],
            "product_recoverable_rows": dry_run_source["recoverable_rows"],
            "product_unrecoverable_rows": dry_run_source["unrecoverable_rows"],
            "applied_catalog_products": applied["catalog_products"],
            "applied_catalog_skus": applied["catalog_skus"],
            "applied_catalog_offers": applied["catalog_offers"],
            "resolution_rules": dry_run_source["resolution_rules"],
            "unresolved_reasons": dry_run_source["unresolved_reasons"],
        }
        skipped_rows = int(dry_run_source["unrecoverable_rows"])
        await self._write_audit_log(
            batch_id=batch_id,
            dry_run_report_hash=_report_hash(dry_run_source),
            applied_rows=applied_rows,
            skipped_rows=skipped_rows,
            reasons=reasons,
            actor=actor,
        )
        return {
            "mode": "apply",
            "target": target,
            "source_system": source_system,
            "batch_id": batch_id,
            "dry_run_report_hash": _report_hash(dry_run_source),
            "rows_updated": applied,
            "applied_rows": applied_rows,
            "skipped_product_rows": skipped_rows,
            "audit_log": {
                "writer_name": WRITER_NAME,
                "batch_id": batch_id,
                "applied_rows": applied_rows,
                "skipped_rows": skipped_rows,
            },
        }

    async def report(self, source_systems: list[str], *, audit_limit: int = 20) -> dict[str, Any]:
        dry_run = await self.dry_run(source_systems)
        dry_run["mode"] = "report"
        dry_run["recent_audit_log"] = await self.db.fetch_all(
            """
            SELECT writer_name, batch_id, dry_run_report_hash, applied_rows,
                   skipped_rows, reasons, actor, applied_at
            FROM writer_audit_log
            WHERE writer_name = :writer_name
            ORDER BY applied_at DESC, id DESC
            LIMIT :limit
            """,
            {"writer_name": WRITER_NAME, "limit": int(audit_limit)},
        )
        return dry_run

    async def _resolve_store_sync_source(
        self,
        *,
        source_system: str,
        platform_hint: Optional[str],
    ) -> list[ProductResolution]:
        rows = await self._fetch_source_candidate_products(source_system)
        stores = await self._load_store_domains()
        out: list[ProductResolution] = []
        for row in rows:
            platform = str(platform_hint or row.get("platform") or "").strip().lower()
            source_ref = _clean_optional(row.get("source_ref"))
            current_domain = _normalize_domain(row.get("current_source_domain"))
            if current_domain:
                domain, rule, reason = current_domain, "existing_product_source_domain", None
            else:
                row_stores = stores.get((str(row.get("merchant_id") or ""), platform), [])
                payload = _coerce_json(row.get("product_payload"))
                domain, rule, reason = _resolve_store_domain_for_row(
                    payload=payload,
                    source_ref=source_ref,
                    stores=row_stores,
                    platform=platform,
                )
            out.append(
                ProductResolution(
                    product_key=str(row["product_key"]),
                    merchant_id=str(row.get("merchant_id") or ""),
                    platform=platform,
                    source_system=source_system,
                    source_ref=source_ref,
                    current_source_domain=_clean_optional(row.get("current_source_domain")),
                    resolved_source_domain=domain,
                    resolution_rule=rule,
                    unresolved_reason=reason,
                )
            )
        return out

    async def _fetch_source_candidate_products(self, source_system: str) -> list[dict[str, Any]]:
        return await self.db.fetch_all(
            """
            SELECT product_key, merchant_id, platform, source_system, source_ref,
                   source_domain AS current_source_domain, product_payload
            FROM catalog_products
            WHERE source_system = :source_system
              AND (
                source_domain IS NULL
                OR EXISTS (
                  SELECT 1 FROM catalog_skus s
                  WHERE s.product_key = catalog_products.product_key
                    AND s.source_domain IS NULL
                )
                OR EXISTS (
                  SELECT 1 FROM catalog_offers o
                  WHERE o.product_key = catalog_products.product_key
                    AND o.source_domain IS NULL
                )
              )
            ORDER BY product_key
            """,
            {"source_system": source_system},
        )

    async def _load_store_domains(self) -> dict[tuple[str, str], list[StoreDomain]]:
        if self._store_domains is not None:
            return self._store_domains
        rows = await self.db.fetch_all(
            """
            SELECT merchant_id, lower(platform) AS platform, domain, status, store_id, api_key
            FROM merchant_stores
            WHERE domain IS NOT NULL
              AND BTRIM(domain) <> ''
            ORDER BY merchant_id, lower(platform), domain
            """
        )
        stores: dict[tuple[str, str], list[StoreDomain]] = defaultdict(list)
        seen: set[tuple[str, str, str]] = set()
        for row in rows:
            merchant_id = str(row.get("merchant_id") or "")
            platform = str(row.get("platform") or "").lower()
            domain = _normalize_domain(row.get("domain"))
            if not merchant_id or not platform or not domain:
                continue
            key = (merchant_id, platform, domain)
            if key in seen:
                continue
            seen.add(key)
            site_ids = _extract_site_ids(
                {
                    "domain": row.get("domain"),
                    "store_id": row.get("store_id"),
                    "api_key": _coerce_json(row.get("api_key")),
                }
            )
            stores[(merchant_id, platform)].append(
                StoreDomain(
                    merchant_id=merchant_id,
                    platform=platform,
                    domain=domain,
                    status=_clean_optional(row.get("status")),
                    store_id=_clean_optional(row.get("store_id")),
                    site_ids=tuple(sorted(site_ids)),
                )
            )
        self._store_domains = dict(stores)
        return self._store_domains

    async def _table_null_counts(self, source_system: str) -> dict[str, int]:
        values = {"source_system": source_system}
        products = await self.db.fetch_one(
            """
            SELECT COUNT(*)::int AS count
            FROM catalog_products cp
            WHERE cp.source_system = :source_system
              AND cp.source_domain IS NULL
            """,
            values,
        )
        skus = await self.db.fetch_one(
            """
            SELECT COUNT(*)::int AS count
            FROM catalog_skus s
            JOIN catalog_products cp ON cp.product_key = s.product_key
            WHERE cp.source_system = :source_system
              AND s.source_domain IS NULL
            """,
            values,
        )
        offers = await self.db.fetch_one(
            """
            SELECT COUNT(*)::int AS count
            FROM catalog_offers o
            JOIN catalog_products cp ON cp.product_key = o.product_key
            WHERE cp.source_system = :source_system
              AND o.source_domain IS NULL
            """,
            values,
        )
        return {
            "catalog_products": _row_count(products),
            "catalog_skus": _row_count(skus),
            "catalog_offers": _row_count(offers),
        }

    async def _recoverable_table_counts(self, resolutions: list[ProductResolution]) -> dict[str, int]:
        mappings = [
            {"product_key": row.product_key, "source_domain": row.resolved_source_domain}
            for row in resolutions
            if row.recoverable
        ]
        if not mappings:
            return {"catalog_products": 0, "catalog_skus": 0, "catalog_offers": 0}
        mapping_json = json.dumps(mappings, sort_keys=True)
        values = {"mappings": mapping_json}
        products = await self.db.fetch_one(
            """
            WITH mapping AS (
              SELECT product_key, source_domain
              FROM jsonb_to_recordset(CAST(:mappings AS jsonb))
                AS m(product_key text, source_domain text)
            )
            SELECT COUNT(*)::int AS count
            FROM catalog_products cp
            JOIN mapping m ON m.product_key = cp.product_key
            WHERE cp.source_domain IS NULL
            """,
            values,
        )
        skus = await self.db.fetch_one(
            """
            WITH mapping AS (
              SELECT product_key, source_domain
              FROM jsonb_to_recordset(CAST(:mappings AS jsonb))
                AS m(product_key text, source_domain text)
            )
            SELECT COUNT(*)::int AS count
            FROM catalog_skus s
            JOIN mapping m ON m.product_key = s.product_key
            WHERE s.source_domain IS NULL
            """,
            values,
        )
        offers = await self.db.fetch_one(
            """
            WITH mapping AS (
              SELECT product_key, source_domain
              FROM jsonb_to_recordset(CAST(:mappings AS jsonb))
                AS m(product_key text, source_domain text)
            )
            SELECT COUNT(*)::int AS count
            FROM catalog_offers o
            JOIN mapping m ON m.product_key = o.product_key
            WHERE o.source_domain IS NULL
            """,
            values,
        )
        return {
            "catalog_products": _row_count(products),
            "catalog_skus": _row_count(skus),
            "catalog_offers": _row_count(offers),
        }

    def _source_report(
        self,
        source_system: str,
        resolutions: list[ProductResolution],
        *,
        table_null_rows: dict[str, int],
        recoverable_table_rows: dict[str, int],
    ) -> dict[str, Any]:
        product_null_rows = [row for row in resolutions if not row.current_source_domain]
        recoverable = [row for row in product_null_rows if row.recoverable]
        unrecoverable = [row for row in product_null_rows if not row.recoverable]
        mapping_rows = [row for row in resolutions if row.recoverable]
        return {
            "source_system": source_system,
            "total_null_rows": len(product_null_rows),
            "recoverable_rows": len(recoverable),
            "unrecoverable_rows": len(unrecoverable),
            "recovery_rate": _safe_rate(len(recoverable), len(product_null_rows)),
            "mapping_product_rows": len(mapping_rows),
            "table_null_rows": table_null_rows,
            "recoverable_table_rows": recoverable_table_rows,
            "resolution_rules": dict(Counter(row.resolution_rule for row in mapping_rows)),
            "unresolved_reasons": dict(Counter(row.unresolved_reason or "unresolved" for row in unrecoverable)),
            "sample_recoverable_mappings": [
                {
                    "product_key": row.product_key,
                    "current_source_domain": row.current_source_domain,
                    "resolved_source_domain": row.resolved_source_domain,
                    "resolution_rule": row.resolution_rule,
                }
                for row in recoverable[: self.sample_limit]
            ],
            "sample_existing_product_mappings": [
                {
                    "product_key": row.product_key,
                    "current_source_domain": row.current_source_domain,
                    "resolved_source_domain": row.resolved_source_domain,
                    "resolution_rule": row.resolution_rule,
                }
                for row in mapping_rows
                if row.current_source_domain
            ][: self.sample_limit],
            "sample_unrecoverable_rows": [
                {
                    "product_key": row.product_key,
                    "merchant_id": row.merchant_id,
                    "source_ref": row.source_ref,
                    "unresolved_reason": row.unresolved_reason,
                }
                for row in unrecoverable[: self.sample_limit]
            ],
        }

    async def _other_null_source_systems(self, selected_sources: list[str]) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all(
            """
            SELECT COALESCE(source_system, '<NULL>') AS source_system,
                   COUNT(*)::int AS null_products
            FROM catalog_products
            WHERE source_domain IS NULL
            GROUP BY COALESCE(source_system, '<NULL>')
            ORDER BY null_products DESC, source_system
            """
        )
        selected = set(selected_sources)
        return [row for row in rows if row.get("source_system") not in selected]

    async def _chydan_shopify_breakout(self) -> dict[str, Any]:
        if self._store_domains is None:
            await self._load_store_domains()
        rows = await self.db.fetch_all(
            """
            SELECT product_key, merchant_id, platform, source_system, source_ref,
                   source_domain AS current_source_domain, product_payload
            FROM catalog_products
            WHERE merchant_id = :merchant_id
              AND source_system = :source_system
            ORDER BY product_key
            """,
            {"merchant_id": CHYDAN_MERCHANT_ID, "source_system": SOURCE_SHOPIFY},
        )
        stores = (self._store_domains or {}).get((CHYDAN_MERCHANT_ID, "shopify"), [])
        domains = [store.domain for store in stores]
        resolved_counter: Counter[str] = Counter()
        for row in rows:
            if _normalize_domain(row.get("current_source_domain")):
                resolved_counter[_normalize_domain(row.get("current_source_domain")) or "unknown"] += 1
                continue
            domain, _rule, _reason = _resolve_store_domain_for_row(
                payload=_coerce_json(row.get("product_payload")),
                source_ref=_clean_optional(row.get("source_ref")),
                stores=stores,
                platform="shopify",
            )
            resolved_counter[domain or "ambiguous"] += 1
        known_domain_counts = {domain: resolved_counter.get(domain, 0) for domain in domains}
        third_store_domains = [domain for domain in domains if domain not in {"92sfrj-bi.myshopify.com", "jwx893-fz.myshopify.com"}]
        return {
            "merchant_id": CHYDAN_MERCHANT_ID,
            "total_shopify_rows": len(rows),
            "known_store_domains": [
                {"domain": store.domain, "status": store.status, "store_id": store.store_id}
                for store in stores
            ],
            "domain_counts": known_domain_counts,
            "92sfrj-bi.myshopify.com": resolved_counter.get("92sfrj-bi.myshopify.com", 0),
            "jwx893-fz.myshopify.com": resolved_counter.get("jwx893-fz.myshopify.com", 0),
            "third_store_counts": {
                domain: resolved_counter.get(domain, 0) for domain in third_store_domains
            },
            "ambiguous": resolved_counter.get("ambiguous", 0),
            "other_resolved": {
                domain: count
                for domain, count in sorted(resolved_counter.items())
                if domain not in set(domains) | {"ambiguous"}
            },
        }

    async def _raise_if_store_domain_mismatch(
        self,
        source_system: str,
        resolutions: list[ProductResolution],
    ) -> None:
        if source_system not in {SOURCE_SHOPIFY, SOURCE_UNIVERSAL}:
            return
        stores = await self._load_store_domains()
        mismatches = []
        for row in resolutions:
            if not row.recoverable:
                continue
            known = {store.domain for store in stores.get((row.merchant_id, row.platform), [])}
            if row.resolved_source_domain not in known:
                mismatches.append(
                    {
                        "product_key": row.product_key,
                        "merchant_id": row.merchant_id,
                        "platform": row.platform,
                        "resolved_source_domain": row.resolved_source_domain,
                        "known_domains": sorted(known),
                    }
                )
        if mismatches:
            raise SystemExit(
                "ERROR: resolved store domains not present in merchant_stores: "
                + json.dumps(mismatches[: self.sample_limit], sort_keys=True)
            )

    async def _update_table_from_mapping(self, table_name: str, *, key_column: str, mapping_json: str) -> int:
        if table_name not in {"catalog_products", "catalog_skus", "catalog_offers"}:
            raise ValueError(f"unsafe table: {table_name}")
        if key_column != "product_key":
            raise ValueError(f"unsafe key column: {key_column}")
        row = await self.db.fetch_one(
            f"""
            WITH mapping AS (
              SELECT product_key, source_domain
              FROM jsonb_to_recordset(CAST(:mappings AS jsonb))
                AS m(product_key text, source_domain text)
            ),
            updated AS (
              UPDATE {table_name} target
              SET source_domain = mapping.source_domain,
                  updated_at = NOW()
              FROM mapping
              WHERE target.{key_column} = mapping.product_key
                AND target.source_domain IS NULL
              RETURNING target.{key_column}
            )
            SELECT COUNT(*)::int AS count FROM updated
            """,
            {"mappings": mapping_json},
        )
        return _row_count(row)

    async def _write_audit_log(
        self,
        *,
        batch_id: str,
        dry_run_report_hash: str,
        applied_rows: int,
        skipped_rows: int,
        reasons: dict[str, Any],
        actor: str,
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO writer_audit_log (
              writer_name, batch_id, dry_run_report_hash,
              applied_rows, skipped_rows, reasons, actor
            ) VALUES (
              :writer_name, :batch_id, :dry_run_report_hash,
              :applied_rows, :skipped_rows, CAST(:reasons AS jsonb), :actor
            )
            """,
            {
                "writer_name": WRITER_NAME,
                "batch_id": batch_id,
                "dry_run_report_hash": dry_run_report_hash,
                "applied_rows": int(applied_rows),
                "skipped_rows": int(skipped_rows),
                "reasons": json.dumps(reasons, sort_keys=True),
                "actor": actor,
            },
        )


def _resolve_store_domain_for_row(
    *,
    payload: Any,
    source_ref: Optional[str],
    stores: list[StoreDomain],
    platform: str,
) -> tuple[Optional[str], str, Optional[str]]:
    known_domains = {store.domain for store in stores if store.domain}
    if not stores:
        return None, "no_merchant_store", "no_known_store_domain"

    for candidate in _payload_explicit_domain_candidates(payload, platform=platform):
        domain = _normalize_domain(candidate)
        if domain and domain in known_domains:
            return domain, "payload_domain", None
        if domain:
            return None, "payload_domain_not_known_store", "payload_domain_not_known_store"

    text_sources = [source_ref or ""]
    text_sources.extend(_payload_string_values(payload))
    for source in text_sources:
        matched = _domain_mentioned_in_text(source, known_domains)
        if matched:
            rule = "source_ref_domain" if source == (source_ref or "") else "payload_known_store_domain"
            return matched, rule, None

    if platform == "wix":
        row_site_ids = _extract_site_ids({"payload": payload, "source_ref": source_ref})
        if row_site_ids:
            matched_domains = sorted(
                {
                    store.domain
                    for store in stores
                    if row_site_ids.intersection(set(store.site_ids) | {store.domain, store.store_id or ""})
                }
            )
            if len(matched_domains) == 1:
                return matched_domains[0], "payload_site_id", None
            if len(matched_domains) > 1:
                return None, "payload_site_id_ambiguous", "payload_site_id_ambiguous"

    single_domain = sorted(known_domains)
    if len(single_domain) == 1:
        return single_domain[0], "single_historical_store", None
    return None, "multi_store_ambiguous", "multi_store_ambiguous"


def _payload_explicit_domain_candidates(payload: Any, *, platform: str) -> list[str]:
    paths = [
        ("source", "domain"),
        ("source", "shop_domain"),
        ("source", "shopDomain"),
        ("source", "store_domain"),
        ("source", "storeDomain"),
        ("store", "domain"),
        ("store", "shop_domain"),
        ("store", "shopDomain"),
        ("domain",),
        ("shop_domain",),
        ("shopDomain",),
        ("store_domain",),
        ("storeDomain",),
        ("platform_metadata", "domain"),
        ("platform_metadata", "shop_domain"),
        ("platform_metadata", "shopDomain"),
        ("platform_metadata", "store_domain"),
        ("platform_metadata", "storeDomain"),
        ("raw", "domain"),
        ("raw", "shop_domain"),
        ("raw", "shopDomain"),
    ]
    if platform == "wix":
        paths.extend(
            [
                ("wix_domain",),
                ("platform_metadata", "wix_domain"),
                ("raw", "wix_domain"),
            ]
        )
    out: list[str] = []
    for path in paths:
        value = _nested_value(payload, path)
        if value is not None:
            out.append(str(value))
    return out


def _payload_string_values(value: Any, *, max_values: int = 400) -> list[str]:
    out: list[str] = []

    def walk(item: Any) -> None:
        if len(out) >= max_values:
            return
        if isinstance(item, str):
            if item.strip():
                out.append(item)
            return
        if isinstance(item, Mapping):
            for nested in item.values():
                walk(nested)
            return
        if isinstance(item, (list, tuple)):
            for nested in item:
                walk(nested)

    walk(value)
    return out


def _domain_mentioned_in_text(text: Any, known_domains: set[str]) -> Optional[str]:
    lowered = str(text or "").lower()
    if not lowered:
        return None
    matches = sorted(domain for domain in known_domains if domain and domain.lower() in lowered)
    return matches[0] if len(matches) == 1 else None


def _extract_site_ids(value: Any) -> set[str]:
    loaded = _coerce_json(value)
    out: set[str] = set()

    def add(candidate: Any) -> None:
        text = _clean_optional(candidate)
        if not text:
            return
        # Wix site ids in this codebase are commonly UUID-like strings, but
        # older store rows may use an opaque site id. Keep only bounded tokens.
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]{5,80}", text):
            if "." in token:
                continue
            out.add(token.lower())

    def walk(item: Any, key_hint: str = "") -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                key_text = str(key or "").lower()
                if key_text in {"site_id", "siteid", "wix_site_id", "wixsiteid", "instance_id", "instanceid"}:
                    add(nested)
                walk(nested, key_text)
            return
        if isinstance(item, list):
            for nested in item:
                walk(nested, key_hint)
            return
        if key_hint in {"domain", "store_id", "source_ref", "api_key"}:
            add(item)

    walk(loaded)
    return out


def _nested_value(value: Any, path: tuple[str, ...]) -> Any:
    cur = _coerce_json(value)
    for key in path:
        if not isinstance(cur, Mapping):
            return None
        cur = cur.get(key)
    return cur


def _coerce_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if raw and raw[0] in "{[":
            try:
                return json.loads(raw)
            except Exception:
                return value
    return value


def _normalize_domain(value: Any) -> Optional[str]:
    raw = _clean_optional(value)
    if not raw:
        return None
    if raw.startswith("{") or raw.startswith("["):
        return None
    raw = raw.replace("\\/", "/")
    if "://" in raw:
        parsed = urlparse(raw)
        host = parsed.netloc or parsed.path
    elif raw.startswith("//"):
        host = urlparse(raw).netloc
    else:
        host = raw
    host = host.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if "@" in host:
        host = host.rsplit("@", 1)[-1]
    host = host.split(":", 1)[0]
    host = host.strip().strip(".").lower()
    return host or None


def _clean_optional(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _row_count(row: Optional[Mapping[str, Any]]) -> int:
    if not row:
        return 0
    return int(row.get("count") or 0)


def _safe_rate(numerator: int, denominator: int) -> Optional[float]:
    if denominator <= 0:
        return None
    return round(float(numerator) / float(denominator), 4)


def _raise_if_low_recovery(source_system: str, source_report: Mapping[str, Any]) -> None:
    total = int(source_report.get("total_null_rows") or 0)
    if total <= 0 or source_system in LOW_RECOVERY_EXEMPT_SOURCES:
        return
    recoverable = int(source_report.get("recoverable_rows") or 0)
    if recoverable / total < 0.5:
        raise SystemExit(
            "ERROR: recovery rate below 50% for "
            f"{source_system}: {recoverable}/{total}. Refine resolver before staging apply."
        )


def _report_hash(report: Any) -> str:
    payload = json.dumps(_json_safe(report), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _json_safe(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return value


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


def _actor() -> str:
    return (
        os.getenv("PIPELINE_ACTOR")
        or os.getenv("USER")
        or Path(sys.argv[0] or "backfill_catalog_source_domain.py").name
    )


def _selected_source_arg(args: argparse.Namespace) -> Optional[str]:
    global_source = getattr(args, "source", None)
    command_source = getattr(args, "command_source", None)
    if global_source and command_source and global_source != command_source:
        raise SystemExit("ERROR: conflicting --source values")
    return global_source or command_source


def _source_list(source: Optional[str]) -> list[str]:
    if not source:
        return list(KNOWN_SOURCES)
    if source not in KNOWN_SOURCES:
        raise SystemExit(f"ERROR: unsupported --source {source!r}; expected one of {', '.join(KNOWN_SOURCES)}")
    return [source]


def _require_apply_confirm(args: argparse.Namespace) -> None:
    if args.confirm != CONFIRM_TOKEN:
        raise SystemExit(f"ERROR: apply requires --confirm {CONFIRM_TOKEN}")
    if args.target == "production" and args.prod_confirm != PROD_CONFIRM_TOKEN:
        raise SystemExit(f"ERROR: production apply requires --prod-confirm {PROD_CONFIRM_TOKEN}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_PUBLIC_URL") or os.getenv("DATABASE_URL") or "",
        help="Postgres URL. Defaults to DATABASE_PUBLIC_URL or DATABASE_URL.",
    )
    parser.add_argument(
        "--source",
        choices=KNOWN_SOURCES,
        help="Limit to one catalog_products.source_system.",
    )
    parser.add_argument("--sample-limit", type=int, default=SAMPLE_LIMIT)
    subparsers = parser.add_subparsers(dest="command", required=True)

    dry_run_cmd = subparsers.add_parser("dry-run", help="Resolve domains and print a read-only report.")
    dry_run_cmd.add_argument(
        "--source",
        dest="command_source",
        choices=KNOWN_SOURCES,
        help="Limit to one catalog_products.source_system.",
    )

    apply_cmd = subparsers.add_parser("apply", help="Apply idempotent updates and write one audit row.")
    apply_cmd.add_argument(
        "--source",
        dest="command_source",
        choices=KNOWN_SOURCES,
        help="Limit to one catalog_products.source_system.",
    )
    apply_cmd.add_argument("--target", choices=["staging", "production"], default="staging")
    apply_cmd.add_argument("--confirm", default="")
    apply_cmd.add_argument("--prod-confirm", default="")

    report_cmd = subparsers.add_parser("report", help="Print current dry-run plus recent audit rows.")
    report_cmd.add_argument(
        "--source",
        dest="command_source",
        choices=KNOWN_SOURCES,
        help="Limit to one catalog_products.source_system.",
    )
    report_cmd.add_argument("--audit-limit", type=int, default=20)
    return parser


async def _main_async(args: argparse.Namespace, *, db_factory: Callable[[str], Any]) -> dict[str, Any]:
    if not args.database_url:
        raise SystemExit("ERROR: missing --database-url (or env DATABASE_PUBLIC_URL / DATABASE_URL)")
    sources = _source_list(_selected_source_arg(args))
    async with _open_db(db_factory, args.database_url) as db:
        backfill = CatalogSourceDomainBackfill(db, sample_limit=args.sample_limit)
        if args.command == "dry-run":
            return await backfill.dry_run(sources)
        if args.command == "report":
            return await backfill.report(sources, audit_limit=args.audit_limit)
        if args.command == "apply":
            _require_apply_confirm(args)
            if len(sources) != 1:
                raise SystemExit("ERROR: apply requires --source <source_system> so each phase has its own audit row")
            return await backfill.apply(source_system=sources[0], target=args.target, actor=_actor())
    raise SystemExit(f"unsupported command: {args.command}")


@asynccontextmanager
async def _open_db(db_factory: Callable[[str], Any], database_url: str):
    db = db_factory(database_url)
    if hasattr(db, "__aenter__"):
        async with db as opened:
            yield opened
    else:
        yield db


def main(argv: Optional[list[str]] = None, *, db_factory: Callable[[str], Any] = PsycopgDatabase) -> int:
    args = build_parser().parse_args(argv)
    import asyncio

    report = asyncio.run(_main_async(args, db_factory=db_factory))
    print(json.dumps(_json_safe(report), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
