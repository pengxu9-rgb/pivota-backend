"""Employee-facing product, offer, and legacy-seed operations.

The original ``/search`` and triplet-detail endpoints are intentionally kept
for support of the cache-backed merchant-sync tools.  They are *not* the
canonical product read model: employee product discovery now uses the
content-key catalogue endpoints below, whose offers come from
``catalog_offers``.
"""

from typing import Any, Dict, List, Optional, Tuple
import asyncio
import csv
import io
import json
import hashlib
import logging
import time
import math
import re
from urllib.parse import urlparse
from urllib.parse import parse_qsl, urlencode, urlunparse
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
import uuid

from db._ddl_guard import apply_ddl_statements
from db.database import database
from db.products import products_cache
from db.product_enrichment import get_enrichment, upsert_enrichment
from models.standard_product import StandardProduct
from utils.auth import get_current_employee, require_employee_permissions

from services import external_seed_destination_liveness as destination_liveness
from services.external_offers_service import ExternalOfferUnavailable, resolve_external_offer
from services.outbound_links_service import (
    DEFAULT_DISCLOSURE_TEXT,
    DEFAULT_UTM_TEMPLATE,
    _is_domain_allowed,
    apply_utm,
    make_redirect_token,
)
from services.seed_content_audit import audit_seed_data
from services.external_seed_audit import (
    MARKET_LOCALE_SEGMENT,
    audit_external_seed_row,
    audit_item_sort_key,
    audit_result_matches_filters,
    build_external_seed_audit_item,
    detect_language,
    get_primary_description,
    parse_locale_segment,
    should_suppress_stale_description_fallback,
    summarize_audit_results,
)
from services.external_referral_readiness import (
    fetch_merchant_referral_inventory,
    filter_referral_inventory_rows,
    get_merchant_referral_domains,
)
from services.pci_kb_scope_review import (
    REVIEW_DECISION_RESOLVED,
    build_queue_items,
    delete_pci_kb_rows_sync,
    extract_seed_id_from_sku_key,
    fetch_pci_kb_rows_sync,
    filter_queue_items,
    summarize_filtered_queue,
)
from db.reviews_center import product_reviews
from services.reviews_service import GLOBAL_IMPORT_MERCHANT_ID, build_product_key, build_sku_key
from utils.availability_vocabulary import normalize_availability

router = APIRouter(prefix="/employee/products", tags=["employee-products"])

logger = logging.getLogger(__name__)

_EXTERNAL_SEEDS_TABLE_READY = False
_EXTERNAL_SEEDS_TABLE_LOCK = asyncio.Lock()

_EXTERNAL_SEED_IMPORT_TASKS_TABLE_READY = False
_EXTERNAL_SEED_IMPORT_TASKS_TABLE_LOCK = asyncio.Lock()

# The whole DDL pass for employee_external_seed_import_tasks: create, then
# backfill the columns that deployments predating them are missing, then the
# indexes. Per-statement tolerant via db/_ddl_guard.py, so one failure must
# not abort the rest, and the guard's return value gates memoization — a
# swallowed ALTER used to leave the column permanently absent while the module
# reported ready. On the hermetic SQLite envs some tests run against, all
# eleven fail: `DEFAULT NOW()` is a syntax error there, so the CREATE dies
# first and the rest cascade to "no such table". These endpoints have never
# worked on SQLite; the change is that the failure is now logged and retried
# rather than raised.
#
# Every statement belongs in this list, including the CREATE ones. The guard
# retries without a cap, so anything left outside it would re-run on every
# accessor call for as long as one statement keeps failing, and on Postgres
# `CREATE TABLE`/`CREATE INDEX ... IF NOT EXISTS` take the table lock BEFORE
# evaluating IF NOT EXISTS. Inside the list they are paced by the guard's
# cooldown and dropped from the retry set as soon as they succeed.
_EXTERNAL_SEED_IMPORT_TASKS_DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS employee_external_seed_import_tasks (
      id TEXT PRIMARY KEY,
      status TEXT NOT NULL DEFAULT 'pending',
      market TEXT NOT NULL,
      tool TEXT NOT NULL,
      mode TEXT NOT NULL,
      created_by_employee_id TEXT NULL,
      created_count INTEGER NOT NULL DEFAULT 0,
      updated_count INTEGER NOT NULL DEFAULT 0,
      errors TEXT NOT NULL DEFAULT '[]',
      seed_ids TEXT NOT NULL DEFAULT '[]',
      stats TEXT NOT NULL DEFAULT '{}',
      error TEXT NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      finished_at TIMESTAMPTZ NULL
    );
    """,
    "ALTER TABLE employee_external_seed_import_tasks ADD COLUMN IF NOT EXISTS created_by_employee_id TEXT;",
    "ALTER TABLE employee_external_seed_import_tasks ADD COLUMN IF NOT EXISTS created_count INTEGER NOT NULL DEFAULT 0;",
    "ALTER TABLE employee_external_seed_import_tasks ADD COLUMN IF NOT EXISTS updated_count INTEGER NOT NULL DEFAULT 0;",
    "ALTER TABLE employee_external_seed_import_tasks ADD COLUMN IF NOT EXISTS errors TEXT NOT NULL DEFAULT '[]';",
    "ALTER TABLE employee_external_seed_import_tasks ADD COLUMN IF NOT EXISTS seed_ids TEXT NOT NULL DEFAULT '[]';",
    "ALTER TABLE employee_external_seed_import_tasks ADD COLUMN IF NOT EXISTS stats TEXT NOT NULL DEFAULT '{}';",
    "ALTER TABLE employee_external_seed_import_tasks ADD COLUMN IF NOT EXISTS error TEXT;",
    "ALTER TABLE employee_external_seed_import_tasks ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ;",
    "CREATE INDEX IF NOT EXISTS idx_employee_external_seed_import_tasks_status ON employee_external_seed_import_tasks(status);",
    "CREATE INDEX IF NOT EXISTS idx_employee_external_seed_import_tasks_updated_at ON employee_external_seed_import_tasks(updated_at DESC);",
]

_EMPLOYEE_PCI_KB_SCOPE_REVIEWS_TABLE_READY = False
_EMPLOYEE_PCI_KB_SCOPE_REVIEWS_TABLE_LOCK = asyncio.Lock()

EXTERNAL_SEED_MERCHANT_ID = "external_seed"
EXTERNAL_SEED_PLATFORM = "external"

def _to_iso(val: Any) -> Optional[str]:
    if val is None:
        return None
    try:
        iso = getattr(val, "isoformat", None)
        if callable(iso):
            return iso()
    except Exception:
        pass
    try:
        return str(val)
    except Exception:
        return None


def _ensure_dict(val: Any) -> Dict[str, Any]:
    """
    products_cache.product_data is expected to be JSON, but some environments may store it as a JSON string.
    Normalize to a dict so downstream parsing doesn't 500.
    """
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return {}
        try:
            parsed = json.loads(s)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}

async def _fetch_latest_products_cache_row(
    *,
    merchant_id: str,
    platform: str,
    platform_product_id: str,
    include_expired: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    Fetch the newest products_cache row for a (merchant_id, platform, platform_product_id).

    Defense-in-depth:
    - Prefer non-expired rows (expires_at NULL or > now) unless include_expired=True.
    - Always order by cached_at/id desc so in the presence of legacy duplicates we
      pick the freshest row deterministically.
    """
    base = (
        products_cache.select()
        .where(
            (products_cache.c.merchant_id == merchant_id)
            & (products_cache.c.platform == platform)
            & (products_cache.c.platform_product_id == platform_product_id)
        )
        .order_by(products_cache.c.cached_at.desc(), products_cache.c.id.desc())
        .limit(1)
    )

    if not include_expired:
        now = datetime.now()
        active = base.where(
            products_cache.c.expires_at.is_(None) | (products_cache.c.expires_at > now)
        )
        row = await database.fetch_one(active)
        if row:
            return dict(row)

    row = await database.fetch_one(base)
    return dict(row) if row else None


def _is_group_by_product_group(group_by: Optional[str]) -> bool:
    v = (group_by or "").strip().lower()
    return v in {"product_group", "product_group_id", "pg", "group", "true", "1"}


def _as_product_card(row: Dict[str, Any]) -> Dict[str, Any]:
    merchant_id = row.get("merchant_id")
    platform = row.get("platform")
    platform_product_id = row.get("platform_product_id")
    product_data = _ensure_dict(row.get("product_data"))

    try:
        sp = StandardProduct.model_validate(product_data)
        title = sp.title
        image_url = sp.image_url or (sp.images[0] if sp.images else None)
        product_id = sp.product_id or sp.id or platform_product_id
        variants = sp.variants or []
        currency = getattr(sp, "currency", None) or product_data.get("currency")
        price = getattr(sp, "price", None) if hasattr(sp, "price") else product_data.get("price")
    except Exception:
        title = product_data.get("title") or product_data.get("name") or platform_product_id
        image_url = product_data.get("image_url") or None
        product_id = product_data.get("product_id") or product_data.get("id") or platform_product_id
        variants = product_data.get("variants") or []
        currency = product_data.get("currency")
        price = product_data.get("price")

    return {
        "product_key": f"{merchant_id}|{platform}|{platform_product_id}",
        "merchant_id": merchant_id,
        "platform": platform,
        "platform_product_id": platform_product_id,
        "product_id": product_id,
        "title": title,
        "image_url": image_url,
        "variants_count": len(variants) if isinstance(variants, list) else 0,
        "price": {"value": price, "currency": currency},
        "cached_at": _to_iso(row.get("cached_at")),
        "expires_at": _to_iso(row.get("expires_at")),
    }


def _extract_product_summary(product_data: Dict[str, Any], platform_product_id: str) -> Dict[str, Any]:
    try:
        sp = StandardProduct.model_validate(product_data)
        title = sp.title
        image_url = sp.image_url or (sp.images[0] if sp.images else None)
        product_id = sp.product_id or sp.id or platform_product_id
        variants = sp.variants or []
        currency = getattr(sp, "currency", None) or product_data.get("currency")
        price = getattr(sp, "price", None)
        availability = getattr(sp, "in_stock", None)
    except Exception:
        title = product_data.get("title") or product_data.get("name") or platform_product_id
        image_url = product_data.get("image_url") or None
        product_id = product_data.get("product_id") or product_data.get("id") or platform_product_id
        variants = product_data.get("variants") or []
        currency = product_data.get("currency")
        price = product_data.get("price")
        availability = product_data.get("in_stock") if "in_stock" in product_data else product_data.get("availability")

    return {
        "title": title,
        "image_url": image_url,
        "product_id": product_id,
        "variants": variants if isinstance(variants, list) else [],
        "currency": currency,
        "price": price,
        "availability": availability,
    }

async def _ensure_external_seeds_table() -> None:
    """
    Minimal storage for employee-managed external seeds.
    We intentionally keep this as runtime DDL for MVP to avoid blocking on migration runners.
    """
    global _EXTERNAL_SEEDS_TABLE_READY
    if _EXTERNAL_SEEDS_TABLE_READY:
        return
    async with _EXTERNAL_SEEDS_TABLE_LOCK:
        if _EXTERNAL_SEEDS_TABLE_READY:
            return
    await database.execute(
        """
        CREATE TABLE IF NOT EXISTS external_product_seeds (
          id TEXT PRIMARY KEY,
          external_product_id TEXT NULL,
          market TEXT NOT NULL,
          tool TEXT NOT NULL DEFAULT '*',
          utm_template TEXT NULL,
          partner_type TEXT NULL,
          disclosure_text TEXT NULL,
          destination_url TEXT NOT NULL,
          canonical_url TEXT NULL,
          domain TEXT NULL,
          title TEXT NULL,
          image_url TEXT NULL,
          price_amount DOUBLE PRECISION NULL,
          price_currency TEXT NULL,
          availability TEXT NULL,
          seed_data JSONB NOT NULL DEFAULT '{}'::jsonb,
          status TEXT NOT NULL DEFAULT 'active',
          notes TEXT NULL,
          created_by_employee_id TEXT NULL,
          attached_product_key TEXT NULL,
          attached_variant_id TEXT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    # Backfill new columns for older deployments (best-effort).
    try:
        await database.execute(
            "ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS seed_data JSONB NOT NULL DEFAULT '{}'::jsonb;"
        )
    except Exception:
        # In case the DB doesn't support JSONB (unlikely in prod), keep the table usable.
        try:
            await database.execute(
                "ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS seed_data TEXT;"
            )
        except Exception:
            pass
    await database.execute(
        "ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS utm_template TEXT;"
    )
    await database.execute(
        "ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS partner_type TEXT;"
    )
    await database.execute(
        "ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS disclosure_text TEXT;"
    )
    await database.execute(
        "ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS external_product_id TEXT;"
    )
    # Content freshness (migration 202 / schema_guard). THIS FUNCTION IS THE ONLY THING THAT
    # CREATES THIS TABLE -- it is absent from SQLAlchemy metadata -- so a column declared in the
    # migration and in schema_guard but NOT here does not exist on any database bootstrapped
    # through this path. `ALTER TABLE IF EXISTS ... ADD COLUMN` in the guard is a silent no-op
    # when the table is missing, and the selector references `last_crawl_attempt_at`
    # unconditionally, so omitting it here is a hard error on first boot, not a degraded sort.
    # Migration 200's columns were added here for exactly this reason; 202's belong here too.
    await database.execute(
        "ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS last_crawled_at TIMESTAMPTZ;"
    )
    await database.execute(
        "ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS last_crawl_attempt_at TIMESTAMPTZ;"
    )
    # Destination liveness (migration 200 / schema_guard). The refresh below writes
    # these on every completed fetch, so this table cannot be usable without them.
    await database.execute(
        "ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS destination_checked_at TIMESTAMPTZ;"
    )
    await database.execute(
        "ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS destination_http_status INTEGER;"
    )
    await database.execute(
        "ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS destination_verdict TEXT;"
    )
    await database.execute(
        "ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS destination_failure_streak INTEGER NOT NULL DEFAULT 0;"
    )
    # The CHECK and the two partial indexes travel WITH the columns. Migration 199 and
    # db/schema_guard.py both create all four things; a table bootstrapped only through this
    # runtime path used to get the columns and neither, which is a third and quietly different
    # declaration of the same schema — and the one that decides whether an unknown verdict is
    # rejected or silently stored.
    try:
        await database.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'ck_external_product_seeds_destination_verdict'
                ) THEN
                    ALTER TABLE external_product_seeds
                        ADD CONSTRAINT ck_external_product_seeds_destination_verdict
                        CHECK (
                            destination_verdict IS NULL
                            OR destination_verdict IN (
                                'live',
                                'live_delisted',
                                'redirected_to_product',
                                'redirected_off_product',
                                'dead_404',
                                'unverifiable'
                            )
                        );
                END IF;
            END $$;
            """
        )
    except Exception:
        # SQLite (the test harness) has no DO blocks and no pg_constraint. The vocabulary is
        # additionally enforced in Python by `classify_destination`, which is the only producer.
        pass
    await database.execute(
        "CREATE INDEX IF NOT EXISTS idx_external_product_seeds_destination_checked "
        "ON external_product_seeds(destination_checked_at);"
    )
    await database.execute(
        "CREATE INDEX IF NOT EXISTS idx_external_product_seeds_destination_verdict "
        "ON external_product_seeds(destination_verdict);"
    )
    await database.execute(
        "CREATE INDEX IF NOT EXISTS idx_external_product_seeds_status ON external_product_seeds(status);"
    )
    await database.execute(
        "CREATE INDEX IF NOT EXISTS idx_external_product_seeds_attached ON external_product_seeds(attached_product_key, attached_variant_id);"
    )
    await database.execute(
        "CREATE INDEX IF NOT EXISTS idx_external_product_seeds_domain ON external_product_seeds(domain);"
    )
    await database.execute(
        "CREATE INDEX IF NOT EXISTS idx_external_product_seeds_created_at ON external_product_seeds(created_at DESC);"
    )
    await database.execute(
        "CREATE INDEX IF NOT EXISTS idx_external_product_seeds_external_product_id ON external_product_seeds(external_product_id);"
    )
    await database.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_external_product_seeds_active_unique ON external_product_seeds(market, tool, external_product_id) WHERE status = 'active' AND external_product_id IS NOT NULL;"
    )
    _EXTERNAL_SEEDS_TABLE_READY = True


async def _ensure_external_seed_import_tasks_table() -> None:
    """
    Track async CSV imports so the UI can poll status without holding an HTTP connection open.
    Runtime DDL keeps MVP deploys unblocked.
    """
    global _EXTERNAL_SEED_IMPORT_TASKS_TABLE_READY
    if _EXTERNAL_SEED_IMPORT_TASKS_TABLE_READY:
        return
    async with _EXTERNAL_SEED_IMPORT_TASKS_TABLE_LOCK:
        if _EXTERNAL_SEED_IMPORT_TASKS_TABLE_READY:
            return
        # The guard's return value gates memoization: the column backfill
        # used to sit in a bare `except Exception: pass`, so a swallowed
        # ALTER left the column permanently absent while this module
        # reported ready for the rest of the process lifetime.
        _EXTERNAL_SEED_IMPORT_TASKS_TABLE_READY = await apply_ddl_statements(
            _EXTERNAL_SEED_IMPORT_TASKS_DDL_STATEMENTS,
            label="_ensure_external_seed_import_tasks_table",
            logger=logger,
            execute=database.execute,
        )


async def _ensure_employee_pci_kb_scope_reviews_table() -> None:
    global _EMPLOYEE_PCI_KB_SCOPE_REVIEWS_TABLE_READY
    if _EMPLOYEE_PCI_KB_SCOPE_REVIEWS_TABLE_READY:
        return
    async with _EMPLOYEE_PCI_KB_SCOPE_REVIEWS_TABLE_LOCK:
        if _EMPLOYEE_PCI_KB_SCOPE_REVIEWS_TABLE_READY:
            return
        await database.execute(
            """
            CREATE TABLE IF NOT EXISTS employee_pci_kb_scope_reviews (
              sku_key TEXT PRIMARY KEY,
              decision TEXT NOT NULL,
              notes TEXT NULL,
              reviewed_by_employee_id TEXT NULL,
              reviewed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              external_seed_id TEXT NULL,
              brand TEXT NULL,
              product_name TEXT NULL,
              scope_decision TEXT NULL,
              scope_reason TEXT NULL,
              source_ref TEXT NULL,
              canonical_url TEXT NULL,
              market TEXT NULL,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        await database.execute(
            "CREATE INDEX IF NOT EXISTS idx_employee_pci_kb_scope_reviews_decision ON employee_pci_kb_scope_reviews(decision);"
        )
        await database.execute(
            "CREATE INDEX IF NOT EXISTS idx_employee_pci_kb_scope_reviews_reviewed_at ON employee_pci_kb_scope_reviews(reviewed_at DESC);"
        )
        _EMPLOYEE_PCI_KB_SCOPE_REVIEWS_TABLE_READY = True


async def _ensure_primary_offers_table() -> None:
    """
    Primary offer selection per product_key (employee curation).
    Runtime DDL keeps MVP deploys unblocked.
    """
    await database.execute(
        """
        CREATE TABLE IF NOT EXISTS employee_product_primary_offers (
          product_key TEXT PRIMARY KEY,
          offer_id TEXT NOT NULL,
          offer_type TEXT NOT NULL,
          created_by_employee_id TEXT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    await database.execute(
        "CREATE INDEX IF NOT EXISTS idx_employee_product_primary_offers_type ON employee_product_primary_offers(offer_type);"
    )
    await database.execute(
        "CREATE INDEX IF NOT EXISTS idx_employee_product_primary_offers_updated ON employee_product_primary_offers(updated_at DESC);"
    )


def _stable_external_product_id(url: str) -> str:
    u = str(url or "").strip()
    if not u:
        return ""
    return "ext_" + hashlib.sha256(u.encode("utf-8")).hexdigest()[:24]


def _ensure_json_obj(val: Any) -> Dict[str, Any]:
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return {}
        try:
            parsed = json.loads(s)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


# Fields that codex's seed-content-audit + seed-correction skills
# write into external_product_seeds.seed_data after a manual review.
# When catalog-intelligence re-extracts a product (via storefront-seed
# backfill, manual edits, etc.) we MUST NOT clobber these — otherwise
# the cycle is "codex reviews → backend backfills → review wiped" and
# all curated data quality work is lost on the next refresh.
#
# Found from prod data audit 2026-05-09: 253 rows have `review_summary`,
# 21 have `reviewed_product_specs_v1`, 12 have `reviewed_ingredient_ids`,
# 10 have `audit`, 8 have `review_status`, 4 have `audit_quarantine`.
# All sourced from codex skills per docs/CODEX_SKILLS.md.
_SEED_DATA_REVIEW_PRESERVE_KEYS: Tuple[str, ...] = (
    "review_summary",
    "reviewed_ingredient_ids",
    "reviewed_product_specs_v1",
    "review_status",
    "audit",
    "audit_quarantine",
)


def _preserve_seed_data_review_fields(
    new_seed_data: Dict[str, Any],
    existing_seed_data: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Copy codex-written review/audit fields from `existing_seed_data`
    into `new_seed_data`. Existing values win — re-extraction never
    has these fields and shouldn't overwrite them with null.

    Returns the same `new_seed_data` dict (mutated in place + returned
    for ergonomic chaining)."""
    if not isinstance(existing_seed_data, dict):
        return new_seed_data
    for key in _SEED_DATA_REVIEW_PRESERVE_KEYS:
        existing_val = existing_seed_data.get(key)
        if existing_val is None:
            continue
        # Skip if the new payload already has a non-null value for this key
        # (rare, but allows an explicit re-review to win).
        if new_seed_data.get(key) is not None:
            continue
        new_seed_data[key] = existing_val
    return new_seed_data


def _ensure_json_list(val: Any) -> List[Any]:
    if isinstance(val, list):
        return val
    if isinstance(val, tuple):
        return list(val)
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _external_seed_import_task_id() -> str:
    return f"esit_{uuid.uuid4().hex}"


_SEED_DATA_FORCE_TEXT = False


def _seed_data_payload(seed_data: Dict[str, Any]) -> Any:
    """Final funnel for every seed_data persistence path in this file.

    All 9 call sites that build a `values["seed_data"]` row for
    INSERT/UPDATE go through this function. Hooking the deterministic
    auditor (services.seed_content_audit.audit_seed_data) here means
    EVERY seed_data write is auto-cleaned — HTML entities decoded,
    Fenty-style shade-name prefix stripped from INCI lists, audit
    stamp written into seed_data.review_summary.

    Without this funnel, codex's external-seeds-backfill / seed-correction
    skills can call any of the 9 endpoints (CSV catalog import, v1
    CSV path, manual edit, bulk update, storefront-seed candidate,
    etc.) and reintroduce dirty content even though the storefront-seed
    path was already audited (PR #412). PR #412 fixed one of nine
    paths; this fix closes the remaining eight.

    The auditor is deterministic — no LLM calls, microseconds per row."""
    if not isinstance(seed_data, dict):
        # Defensive: legacy callers might pass already-stringified JSON.
        # Pass through unchanged rather than crash; the audit covers the
        # main code paths that build dict shapes.
        if _SEED_DATA_FORCE_TEXT and not isinstance(seed_data, str):
            return json.dumps(seed_data)
        return seed_data

    cleaned, audit_summary = audit_seed_data(seed_data)
    cleaned["review_summary"] = audit_summary

    if _SEED_DATA_FORCE_TEXT:
        return json.dumps(cleaned)
    return cleaned


async def _execute_seed_data_stmt(query: str, values: Dict[str, Any]) -> None:
    global _SEED_DATA_FORCE_TEXT
    try:
        await database.execute(query, values)
    except Exception as exc:
        msg = str(exc)
        seed_data = values.get("seed_data")
        if (
            not _SEED_DATA_FORCE_TEXT
            and isinstance(seed_data, dict)
            and ("expected str" in msg or "got dict" in msg or "query argument" in msg)
        ):
            _SEED_DATA_FORCE_TEXT = True
            retry_values = dict(values)
            retry_values["seed_data"] = json.dumps(seed_data or {})
            await database.execute(query, retry_values)
            return
        raise


def _seed_variants(seed_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    variants = seed_data.get("variants")
    if isinstance(variants, list):
        return [v for v in variants if isinstance(v, dict)]
    return []


def _set_manual_seed_description_override(seed_data: Dict[str, Any], description: Optional[str]) -> None:
    manual_overrides = _ensure_json_obj(seed_data.get("manual_overrides"))
    normalized = (description or "").strip()
    if normalized:
        manual_overrides["description"] = normalized
        manual_overrides["source"] = "employee_review"
        manual_overrides["updated_at"] = _to_iso(datetime.now(timezone.utc))
        seed_data["manual_overrides"] = manual_overrides
    else:
        manual_overrides.pop("description", None)
        manual_overrides.pop("source", None)
        manual_overrides.pop("updated_at", None)
        if manual_overrides:
            seed_data["manual_overrides"] = manual_overrides
        else:
            seed_data.pop("manual_overrides", None)


_SIZE_LIKE_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:ml|mL|l|L|oz|fl\s*oz|g|kg|lb|lbs|cm|mm|in|inch|inches)\b",
    re.IGNORECASE,
)


def _normalize_variant_title(title: Any) -> str:
    if title is None:
        return ""
    s = str(title).strip()
    return re.sub(r"\s+", " ", s)


def _variant_title_score(*, title: Any, product_title: Optional[str]) -> int:
    t = _normalize_variant_title(title)
    if not t:
        return 0
    if product_title and t.lower() == str(product_title).strip().lower():
        return 0
    if "http://" in t or "https://" in t:
        return 0
    if _SIZE_LIKE_RE.search(t):
        return 3
    if len(t) <= 12:
        return 2
    if len(t) <= 32:
        return 1
    return 0


def _best_variant_title_score(variants: List[Dict[str, Any]], product_title: Optional[str]) -> int:
    best = 0
    for v in variants:
        if not isinstance(v, dict):
            continue
        best = max(best, _variant_title_score(title=v.get("title"), product_title=product_title))
    return best


def _distinct_variant_titles(variants: List[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    for v in variants:
        if not isinstance(v, dict):
            continue
        t = _normalize_variant_title(v.get("title"))
        if t and t not in out:
            out.append(t)
    return out


def _distinct_variant_ids(variants: List[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    for v in variants:
        if not isinstance(v, dict):
            continue
        raw = v.get("variant_id") or v.get("variantId") or v.get("id") or v.get("sku") or v.get("sku_id")
        if raw is None:
            continue
        s = str(raw).strip()
        if s and s not in out:
            out.append(s)
    return out


def _should_overwrite_seed_variants(
    *,
    existing: List[Dict[str, Any]],
    incoming: List[Dict[str, Any]],
    product_title: Optional[str],
) -> bool:
    if not incoming:
        return False
    if not existing:
        return True

    existing_score = _best_variant_title_score(existing, product_title)
    incoming_score = _best_variant_title_score(incoming, product_title)
    if incoming_score < existing_score:
        return False

    # Overwrite only when existing titles are weak or degenerate.
    if existing_score <= 1 and incoming_score >= 2:
        return True

    existing_titles = _distinct_variant_titles(existing)
    incoming_titles = _distinct_variant_titles(incoming)
    if len(incoming_titles) > len(existing_titles):
        return True

    existing_ids = _distinct_variant_ids(existing)
    incoming_ids = _distinct_variant_ids(incoming)
    if len(incoming_ids) > len(existing_ids):
        return True

    return False


def _seed_variant_description_map(variants: List[Dict[str, Any]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for idx, variant in enumerate(variants or []):
        if not isinstance(variant, dict):
            continue
        variant_id = str(
            variant.get("variant_id")
            or variant.get("variantId")
            or variant.get("id")
            or variant.get("sku")
            or variant.get("sku_id")
            or f"variant-{idx + 1}"
        ).strip()
        description = str(variant.get("description") or "").strip()
        if variant_id and description:
            out[variant_id] = description
    return out


def _should_replace_localized_copy(
    *,
    existing_text: Optional[str],
    incoming_text: Optional[str],
    market: Optional[str],
    previous_canonical_url: Optional[str],
    refreshed_canonical_url: Optional[str],
) -> bool:
    existing = str(existing_text or "").strip()
    incoming = str(incoming_text or "").strip()
    if not existing or not incoming or existing == incoming:
        return False

    market_normalized = str(market or "").strip().upper()
    expected_locale = MARKET_LOCALE_SEGMENT.get(market_normalized, "")
    previous_locale = parse_locale_segment(str(previous_canonical_url or ""))
    refreshed_locale = parse_locale_segment(str(refreshed_canonical_url or ""))
    locale_corrected = bool(expected_locale and refreshed_locale == expected_locale and previous_locale and previous_locale != refreshed_locale)

    existing_language = detect_language(existing)
    incoming_language = detect_language(incoming)
    incoming_looks_english = incoming_language is None
    existing_is_non_english = existing_language in {"de", "fr", "es"}

    if locale_corrected and incoming_looks_english:
        return True
    if market_normalized == "US" and existing_is_non_english and incoming_looks_english:
        return True
    return False


def _should_replace_seed_variant_content(
    *,
    existing: List[Dict[str, Any]],
    incoming: List[Dict[str, Any]],
    market: Optional[str],
    previous_canonical_url: Optional[str],
    refreshed_canonical_url: Optional[str],
) -> bool:
    if not incoming:
        return False
    existing_map = _seed_variant_description_map(existing)
    incoming_map = _seed_variant_description_map(incoming)
    if not existing_map or not incoming_map:
        return False

    shared_ids = [variant_id for variant_id in incoming_map.keys() if variant_id in existing_map]
    if not shared_ids:
        return False

    return any(
        _should_replace_localized_copy(
            existing_text=existing_map.get(variant_id),
            incoming_text=incoming_map.get(variant_id),
            market=market,
            previous_canonical_url=previous_canonical_url,
            refreshed_canonical_url=refreshed_canonical_url,
        )
        for variant_id in shared_ids
    )


def _seed_primary_price(seed_row: Dict[str, Any], seed_data: Dict[str, Any]) -> Dict[str, Any]:
    variants = _seed_variants(seed_data)
    for v in variants:
        amt = v.get("price_amount")
        cur = v.get("price_currency") or v.get("currency")
        if amt is not None:
            return {"amount": amt, "currency": cur}
    return {"amount": seed_row.get("price_amount"), "currency": seed_row.get("price_currency")}


def _is_external_seed_product_key(*, merchant_id: str, platform: str) -> bool:
    return merchant_id == EXTERNAL_SEED_MERCHANT_ID and platform == EXTERNAL_SEED_PLATFORM


async def _fetch_external_seed_rows_by_external_product_id(
    *,
    external_product_id: str,
    limit: int = 50,
    allow_attached_product_key: Optional[str] = None,
) -> List[Dict[str, Any]]:
    await _ensure_external_seeds_table()
    values: Dict[str, Any] = {"external_product_id": external_product_id, "limit": limit}
    attached_clause = ""
    if allow_attached_product_key:
        values["allow_attached_product_key"] = allow_attached_product_key
        attached_clause = " AND (attached_product_key IS NULL OR attached_product_key = :allow_attached_product_key)"
    try:
        rows = await database.fetch_all(
            """
            SELECT *
            FROM external_product_seeds
            WHERE status = 'active'
              AND (
                external_product_id = :external_product_id
                OR (seed_data->>'external_product_id') = :external_product_id
              )
            """
            + attached_clause
            + """
            ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
            LIMIT :limit
            """,
            values,
        )
    except Exception:
        rows = await database.fetch_all(
            """
            SELECT *
            FROM external_product_seeds
            WHERE status = 'active'
              AND external_product_id = :external_product_id
            """
            + attached_clause
            + """
            ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
            LIMIT :limit
            """,
            values,
        )
    return [dict(r) for r in (rows or [])]


_SHOPIFY_SIZE_SUFFIX_RE = re.compile(r"_(\d{2,4})x(\d{0,4})?(?=\.(?:jpe?g|png|webp|gif|avif))", re.IGNORECASE)


def _seed_image_dedupe_key(url: str) -> str:
    s = str(url or "").strip()
    if not s:
        return ""
    try:
        parsed = urlparse(s)
    except Exception:
        return s
    netloc = (parsed.hostname or parsed.netloc or "").lower()
    path = parsed.path or ""
    if "cdn.shopify.com" in netloc:
        path = _SHOPIFY_SIZE_SUFFIX_RE.sub("", path)
    qs = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in {"width", "w", "height", "h", "dpr", "quality", "q"}
    ]
    query = urlencode(sorted(qs, key=lambda kv: (kv[0].lower(), kv[1])), doseq=True)
    if query:
        return f"{netloc}{path}?{query}"
    return f"{netloc}{path}"


def _seed_image_resolution_score(url: str) -> float:
    s = str(url or "").strip()
    if not s:
        return 0.0
    try:
        parsed = urlparse(s)
    except Exception:
        return 0.0

    width: Optional[int] = None
    height: Optional[int] = None
    dpr: Optional[float] = None
    for k, v in parse_qsl(parsed.query, keep_blank_values=True):
        kl = k.lower()
        if width is None and kl in {"width", "w"}:
            try:
                width = int(re.sub(r"[^0-9]", "", v) or "0") or None
            except Exception:
                width = None
        elif height is None and kl in {"height", "h"}:
            try:
                height = int(re.sub(r"[^0-9]", "", v) or "0") or None
            except Exception:
                height = None
        elif dpr is None and kl == "dpr":
            try:
                dpr = float(re.sub(r"[^0-9.]", "", v) or "0") or None
            except Exception:
                dpr = None

    score = 0.0
    if width is not None and height is not None:
        score = float(width * height)
    elif width is not None:
        score = float(width)
    elif height is not None:
        score = float(height)

    m = _SHOPIFY_SIZE_SUFFIX_RE.search(parsed.path or "")
    if m:
        try:
            w = int(m.group(1) or "0") or 0
        except Exception:
            w = 0
        try:
            h = int(m.group(2) or "0") if m.group(2) else 0
        except Exception:
            h = 0
        if w and h:
            score = max(score, float(w * h))
        elif w:
            score = max(score, float(w))

    if score and dpr and dpr > 0:
        score *= float(dpr * dpr)
    return score


def _dedupe_seed_image_urls(urls: List[str], *, limit: int = 20) -> List[str]:
    out: List[str] = []
    idx_by_key: Dict[str, int] = {}
    score_by_key: Dict[str, float] = {}
    for u in urls or []:
        if not isinstance(u, str):
            continue
        uu = u.strip()
        if not uu or not uu.startswith(("http://", "https://")):
            continue
        key = _seed_image_dedupe_key(uu) or uu
        score = _seed_image_resolution_score(uu)
        if key in idx_by_key:
            if score > score_by_key.get(key, -1.0):
                out[idx_by_key[key]] = uu
                score_by_key[key] = score
            continue
        if len(out) >= limit:
            continue
        idx_by_key[key] = len(out)
        score_by_key[key] = score
        out.append(uu)
    return out


def _normalize_seed_image_urls(*, seed_data: Dict[str, Any], row: Dict[str, Any]) -> List[str]:
    candidates: List[str] = []
    raw_list = seed_data.get("image_urls")
    if isinstance(raw_list, list):
        candidates.extend([str(u) for u in raw_list if isinstance(u, str)])
    for u in [seed_data.get("image_url"), row.get("image_url")]:
        if isinstance(u, str):
            candidates.append(u)

    return _dedupe_seed_image_urls(candidates, limit=20)


def _normalize_seed_brand(seed_data: Dict[str, Any]) -> Optional[str]:
    raw = seed_data.get("brand") or seed_data.get("product", {}).get("brand")
    if isinstance(raw, dict):
        raw = raw.get("name") or raw.get("brand")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _seed_variant_to_raw_row(
    *,
    v: Dict[str, Any],
    idx: int,
    external_product_id: str,
    default_currency: str,
    fallback_price_amount: Optional[float],
) -> Dict[str, Any]:
    variant_id = str(v.get("variant_id") or v.get("id") or v.get("sku") or f"{external_product_id}:{idx + 1}").strip()
    title = v.get("title") or v.get("name") or f"Variant {idx + 1}"
    raw_amount = v.get("price_amount")
    if raw_amount is None:
        raw_amount = v.get("price") or v.get("amount") or v.get("value")
    price_amount: Optional[float] = None
    if raw_amount is not None and raw_amount != "":
        try:
            price_amount = float(raw_amount)
        except Exception:
            price_amount = None
    if price_amount is None:
        price_amount = fallback_price_amount

    raw_currency = v.get("price_currency") or v.get("currency") or default_currency
    price_currency = str(raw_currency or default_currency).strip().upper() or default_currency

    availability = _normalize_seed_availability(v.get("availability"))
    available: Optional[bool] = None
    if availability in (None, "in_stock"):
        available = True
    elif availability == "out_of_stock":
        available = False

    image_url = v.get("image_url") or v.get("imageUrl") or v.get("imageURL") or v.get("image")
    if image_url is None:
        imgs = v.get("image_urls") or v.get("images")
        if isinstance(imgs, list) and imgs:
            image_url = imgs[0]
    if isinstance(image_url, dict):
        image_url = image_url.get("url") or image_url.get("image_url") or image_url.get("src")
    if not isinstance(image_url, str):
        image_url = None
    else:
        image_url = image_url.strip() or None

    label_image_url = v.get("label_image_url") or v.get("swatch_image_url") or v.get("thumbnail_url") or v.get("thumbnailUrl")
    if isinstance(label_image_url, dict):
        label_image_url = label_image_url.get("url") or label_image_url.get("image_url") or label_image_url.get("src")
    if not isinstance(label_image_url, str):
        label_image_url = None
    else:
        label_image_url = label_image_url.strip() or None

    out: Dict[str, Any] = {
        "variant_id": variant_id,
        "title": title,
        "price_amount": price_amount,
        "price_currency": price_currency,
        **({"availability": availability} if availability is not None else {}),
        **({"available": available} if available is not None else {}),
        **({"image_url": image_url} if image_url else {}),
        **({"label_image_url": label_image_url} if label_image_url else {}),
    }
    return out


def _seed_variant_to_standard_variant(
    *,
    raw_row: Dict[str, Any],
    fallback_price: float,
) -> Dict[str, Any]:
    vid = str(raw_row.get("variant_id") or "").strip() or "∅"
    title = str(raw_row.get("title") or "Variant").strip() or "Variant"
    price_amount = raw_row.get("price_amount")
    try:
        price = float(price_amount) if price_amount is not None else float(fallback_price)
    except Exception:
        price = float(fallback_price)
    image_url = raw_row.get("image_url")
    if not isinstance(image_url, str) or not image_url.strip():
        image_url = None
    return {
        "id": vid,
        "variant_id": vid,
        "title": title,
        "sku": vid,
        "price": price,
        "inventory_quantity": 0,
        **({"image_url": image_url} if image_url else {}),
    }


async def _build_external_seed_product_view(
    *,
    external_product_id: str,
    allow_attached_product_key: Optional[str] = None,
) -> Dict[str, Any]:
    rows = await _fetch_external_seed_rows_by_external_product_id(
        external_product_id=external_product_id,
        allow_attached_product_key=allow_attached_product_key,
    )
    if not rows:
        raise HTTPException(status_code=404, detail="PRODUCT_NOT_FOUND")

    primary_row = rows[0]
    seed_data = _ensure_json_obj(primary_row.get("seed_data"))

    title = (
        seed_data.get("title")
        or primary_row.get("title")
        or primary_row.get("canonical_url")
        or primary_row.get("destination_url")
        or external_product_id
    )
    description = get_primary_description(primary_row)

    primary_price = _seed_primary_price(primary_row, seed_data)
    raw_amount = primary_price.get("amount")
    try:
        price = float(raw_amount) if raw_amount is not None else 0.0
    except Exception:
        price = 0.0
    currency = str(primary_price.get("currency") or "USD").strip().upper() or "USD"

    image_urls = _normalize_seed_image_urls(seed_data=seed_data, row=primary_row)
    image_url = image_urls[0] if image_urls else None

    seed_variants = _seed_variants(seed_data)
    raw_variants: List[Dict[str, Any]] = []
    std_variants: List[Dict[str, Any]] = []
    for idx, v in enumerate(seed_variants):
        if not isinstance(v, dict):
            continue
        raw_row = _seed_variant_to_raw_row(
            v=v,
            idx=idx,
            external_product_id=external_product_id,
            default_currency=currency,
            fallback_price_amount=(float(raw_amount) if raw_amount is not None else None),
        )
        raw_variants.append(raw_row)
        std_variants.append(_seed_variant_to_standard_variant(raw_row=raw_row, fallback_price=price))
        if len(raw_variants) >= 50:
            break

    if not std_variants:
        std_variants = [
            {
                "id": external_product_id,
                "variant_id": external_product_id,
                "title": "Default",
                "sku": external_product_id,
                "price": price,
                "inventory_quantity": 0,
                **({"image_url": image_url} if image_url else {}),
            }
        ]
        raw_variants = [
            {
                "variant_id": "∅",
                "title": "Default (no variants)",
                "price_amount": (price if raw_amount is not None else None),
                "price_currency": currency,
                **({"image_url": image_url} if image_url else {}),
            }
        ]

    vendor = _normalize_seed_brand(seed_data)

    product_data: Dict[str, Any] = {
        "id": external_product_id,
        "product_id": external_product_id,
        "platform": EXTERNAL_SEED_PLATFORM,
        "merchant_id": EXTERNAL_SEED_MERCHANT_ID,
        "title": title,
        "description": description,
        "vendor": vendor,
        "product_type": "external",
        "tags": [],
        "price": price,
        "currency": currency,
        "inventory_quantity": 0,
        "orderable": False,
        "image_url": image_url,
        "images": image_urls,
        "variants": std_variants,
        "updated_at": primary_row.get("updated_at") or primary_row.get("created_at"),
        "platform_metadata": {
            "source": "external_seed",
            "external_product_id": external_product_id,
            "domain": primary_row.get("domain"),
            "canonical_url": primary_row.get("canonical_url"),
            "destination_url": primary_row.get("destination_url"),
            "seed_ids": [str(r.get("id")) for r in rows if r.get("id")],
        },
    }

    raw: Dict[str, Any] = {
        "title": title,
        "description": description,
        "image_url": image_url,
        "images": image_urls,
        "variants": raw_variants,
        "source": "external_seed",
        "external_product_id": external_product_id,
        "seed_primary": {
            "id": primary_row.get("id"),
            "market": primary_row.get("market"),
            "tool": primary_row.get("tool"),
            "utm_template": primary_row.get("utm_template") or seed_data.get("utm_template"),
            "partner_type": primary_row.get("partner_type") or seed_data.get("partner_type"),
            "disclosure_text": primary_row.get("disclosure_text")
            or seed_data.get("disclosure_text")
            or DEFAULT_DISCLOSURE_TEXT,
            "destination_url": primary_row.get("destination_url"),
            "canonical_url": primary_row.get("canonical_url"),
            "domain": primary_row.get("domain"),
            "price_amount": primary_row.get("price_amount"),
            "price_currency": primary_row.get("price_currency"),
            "availability": primary_row.get("availability"),
            "updated_at": _to_iso(primary_row.get("updated_at") or primary_row.get("created_at")),
        },
        "seed_data": seed_data,
    }

    cached_at = primary_row.get("updated_at") or primary_row.get("created_at")
    return {"product_data": product_data, "raw": raw, "cached_at": cached_at}


def _seed_merchant_display_name(seed_data: Dict[str, Any], domain: Optional[str]) -> Optional[str]:
    val = seed_data.get("merchant_display_name") or seed_data.get("brand") or domain
    if isinstance(val, str):
        val = val.strip()
        return val or None
    return None


def _normalize_market(market: Optional[str]) -> str:
    m = str(market or "").strip().upper()
    return m or "US"


def _normalize_tool(tool: Optional[str]) -> str:
    t = str(tool or "").strip()
    return t or "*"


# Market → expected purchase currency. CSV imports historically wrote
# whatever currency the source spreadsheet contained, even when it
# contradicted the market (e.g., 469 KraveBeauty seeds with currency=EUR
# under market=US). The mismatch surfaced as "$0 / EUR price for US
# users" in chat. This map is the source of truth for the consistency
# check; rows whose currency disagrees with the market's expected
# currency are dropped at import (or have currency normalized).
MARKET_EXPECTED_CURRENCY: Dict[str, str] = {
    "US": "USD",
    "CA": "CAD",
    "GB": "GBP",
    "UK": "GBP",
    "EU": "EUR",
    "DE": "EUR",
    "FR": "EUR",
    "ES": "EUR",
    "IT": "EUR",
    "JP": "JPY",
    "KR": "KRW",
    "CN": "CNY",
    "SG": "SGD",
    "AU": "AUD",
    "NZ": "NZD",
    "MX": "MXN",
    "BR": "BRL",
    "IN": "INR",
    "TH": "THB",
}


# Domain TLDs that strongly imply a non-US storefront. If a CSV row
# has market=US but the product_url's host ends in one of these,
# the row is almost certainly mislabelled — silently importing it
# yields wrong-language PDPs in US recall (12 .kr-domain seeds were
# the trigger for adding this check).
_NON_US_TLD_PATTERNS: Tuple[str, ...] = (
    ".kr", ".co.kr",
    ".jp", ".co.jp",
    ".de", ".fr", ".es", ".it", ".eu",
    ".cn", ".com.cn",
    ".tw", ".com.tw",
    ".hk", ".com.hk",
)


def validate_market_currency(market: Optional[str], currency: Optional[str]) -> Optional[str]:
    """Return None if the (market, currency) pair is consistent or the
    row should be allowed; return a short error tag if the currency
    contradicts the market.

    Permissive cases (return None):
      - currency is null/empty (will be filled from market default downstream)
      - market not in MARKET_EXPECTED_CURRENCY (unknown market — don't block)
      - currency matches the expected currency for the market
    """
    cur_norm = str(currency or "").strip().upper()
    if not cur_norm:
        return None
    market_norm = _normalize_market(market)
    expected = MARKET_EXPECTED_CURRENCY.get(market_norm)
    if not expected:
        return None
    if cur_norm == expected:
        return None
    return f"market_currency_mismatch_market_{market_norm}_currency_{cur_norm}"


def validate_market_domain(market: Optional[str], product_url: Optional[str]) -> Optional[str]:
    """Return None if the URL host is consistent with the market; return
    a short error tag if a strongly non-US TLD appears under market=US
    (or symmetric mismatches in the future). Conservative — only
    blocks the obvious .kr/.jp/.de/etc. cases that have been observed
    causing wrong-language PDPs in recall."""
    if not product_url:
        return None
    market_norm = _normalize_market(market)
    if market_norm != "US":
        # v1: only validate US-market mismatches. Non-US markets often
        # legitimately import from .com domains (Sephora.fr → market=FR
        # buying from sephora.com), so the check would over-reject.
        return None
    try:
        from urllib.parse import urlparse
        host = (urlparse(str(product_url).strip()).hostname or "").lower()
    except Exception:
        return None
    if not host:
        return None
    for tld in _NON_US_TLD_PATTERNS:
        if host.endswith(tld):
            return f"market_domain_mismatch_market_US_host_{host}"
    return None


def _require_http_url(url: str) -> str:
    u = str(url or "").strip()
    if not (u.startswith("http://") or u.startswith("https://")):
        raise HTTPException(status_code=400, detail="INVALID_URL")
    return u


def _normalize_external_seed_domain(raw_domain: Any) -> str:
    candidate = str(raw_domain or "").strip().lower()
    if not candidate:
        return ""
    if "://" in candidate:
        candidate = (urlparse(candidate).hostname or "").strip().lower()
    else:
        candidate = candidate.split("/", 1)[0].strip().lower()
    candidate = candidate.strip(".")
    if candidate.startswith("www."):
        candidate = candidate[4:]
    return candidate


def _build_external_seed_domain_where_clause(
    *,
    raw_domain: Any,
    include_subdomains: bool = True,
    param_prefix: str = "domain_filter",
) -> tuple[str, Dict[str, Any], str]:
    normalized = _normalize_external_seed_domain(raw_domain)
    if not normalized:
        raise HTTPException(status_code=400, detail="INVALID_DOMAIN")

    values: Dict[str, Any] = {
        f"{param_prefix}_exact": normalized,
        f"{param_prefix}_www": f"www.{normalized}",
        f"{param_prefix}_url_like": f"%{normalized}%",
    }
    clauses = [
        f"LOWER(COALESCE(domain, '')) = :{param_prefix}_exact",
        f"LOWER(COALESCE(domain, '')) = :{param_prefix}_www",
        f"LOWER(COALESCE(destination_url, '')) LIKE :{param_prefix}_url_like",
        f"LOWER(COALESCE(canonical_url, '')) LIKE :{param_prefix}_url_like",
    ]
    if include_subdomains:
        values[f"{param_prefix}_subdomain"] = f"%.{normalized}"
        clauses.append(f"LOWER(COALESCE(domain, '')) LIKE :{param_prefix}_subdomain")
    return "(" + " OR ".join(clauses) + ")", values, normalized


def _request_base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _seed_id() -> str:
    return f"eps_{uuid.uuid4().hex[:24]}"


async def _make_redirect_url(
    *,
    request: Request,
    market: str,
    tool: str,
    destination_url: str,
    utm_template: Optional[str],
    ctx: Dict[str, Any],
) -> str:
    dest_with_utm = apply_utm(destination_url, utm_template or DEFAULT_UTM_TEMPLATE, {"market": market, "tool": tool})
    if not await _is_domain_allowed(market=market, destination_url=dest_with_utm):
        raise HTTPException(status_code=400, detail="DOMAIN_NOT_ALLOWED")
    token = make_redirect_token(
        {
            "market": market,
            "tool": tool,
            "dest": dest_with_utm,
            "ctx": ctx,
        }
    )
    return f"{_request_base_url(request)}/r?token={token}"


class CreateExternalSeedRequest(BaseModel):
    destination_url: str = Field(..., min_length=1)
    market: Optional[str] = None
    tool: Optional[str] = None
    notes: Optional[str] = None
    attach_product_key: Optional[str] = None
    attach_variant_id: Optional[str] = None
    utm_template: Optional[str] = None
    partner_type: Optional[str] = None
    disclosure_text: Optional[str] = None
    # Optional manual overrides / richer product seed fields (for employee curation).
    merchant_display_name: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    image_url: Optional[str] = None
    image_urls: Optional[List[str]] = None
    product_id: Optional[str] = None
    brand: Optional[str] = None
    category: Optional[str] = None
    price_amount: Optional[float] = None
    price_currency: Optional[str] = None
    availability: Optional[str] = None
    variants: Optional[List[Dict[str, Any]]] = None


class UpdateExternalSeedRequest(BaseModel):
    status: Optional[str] = None
    notes: Optional[str] = None
    market: Optional[str] = None
    tool: Optional[str] = None
    destination_url: Optional[str] = None
    canonical_url: Optional[str] = None
    merchant_display_name: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    image_url: Optional[str] = None
    image_urls: Optional[List[str]] = None
    product_id: Optional[str] = None
    brand: Optional[str] = None
    category: Optional[str] = None
    price_amount: Optional[float] = None
    price_currency: Optional[str] = None
    availability: Optional[str] = None
    variants: Optional[List[Dict[str, Any]]] = None
    utm_template: Optional[str] = None
    partner_type: Optional[str] = None
    disclosure_text: Optional[str] = None


class HardDeleteExternalSeedsByDomainRequest(BaseModel):
    domain: str = Field(..., min_length=3)
    include_subdomains: bool = True
    dry_run: bool = False
    sample_limit: int = Field(default=20, ge=1, le=200)


class PreviewExternalSeedRequest(BaseModel):
    destination_url: str = Field(..., min_length=1)
    market: Optional[str] = None
    force_refresh: bool = False


class BackfillStorefrontExternalSeedsRequest(BaseModel):
    merchant_id: str = Field(..., min_length=1)
    market: Optional[str] = None
    limit: int = Field(default=50, ge=1, le=200)
    dry_run: bool = False


class ExternalSeedsCsvImportResponse(BaseModel):
    created: int
    updated: int = 0
    errors: List[str] = Field(default_factory=list)
    seedIds: List[str] = Field(default_factory=list)
    taskId: Optional[str] = None


class UpdatePciKbScopeReviewRequest(BaseModel):
    decision: str = Field(..., min_length=1)
    notes: Optional[str] = None


def _parse_float(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        return float(s)
    except Exception:
        return None


_CSV_KEY_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


def _normalize_csv_key(raw: Any) -> str:
    """
    Normalize CSV header keys so we can accept multiple upload formats.
    Examples:
      - "Product URL" -> "product_url"
      - "priceAmount" -> "priceamount"
    """
    s = str(raw or "").strip().lower()
    if not s:
        return ""
    s = _CSV_KEY_NORMALIZE_RE.sub("_", s)
    return s.strip("_")


def _normalize_seed_url_for_id(url: str) -> str:
    """
    Make external_product_id stable across common tracking params.
    Mirrors `services.external_offers_service._normalize_url` semantics (subset).
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(status_code=400, detail="INVALID_URL")
    qs = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if not k.lower().startswith("utm_")]
    qs = [(k, v) for k, v in qs if k.lower() not in {"fbclid", "gclid", "yclid", "msclkid"}]
    query = urlencode(qs, doseq=True)
    return urlunparse(parsed._replace(fragment="", query=query))


def _normalize_seed_availability(raw: Any) -> Optional[str]:
    """Canonicalise a seed availability string, keeping this call site's passthrough contract.

    The vocabulary itself now lives in utils.availability_vocabulary so every reader shares
    one mapping — see that module for why four divergent copies was a defect. The PASSTHROUGH
    for unrecognised values is preserved deliberately: the caller above treats a passthrough
    value as "unknown" (leaves `available` as None), and collapsing those to None here would
    instead make them read as AVAILABLE.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    canonical = normalize_availability(s)
    if canonical is not None:
        return canonical
    return s.lower().replace(" ", "_")


_CURRENCY_TO_REFERRAL_MARKET = {
    "USD": "US",
    "SGD": "SG",
    "JPY": "JP",
    "EUR": "EU-DE",
}


def _extract_storefront_handle(product_data: Dict[str, Any]) -> Optional[str]:
    raw = _ensure_dict(product_data.get("raw"))
    platform_metadata = _ensure_dict(product_data.get("platform_metadata"))
    for candidate in (
        product_data.get("handle"),
        raw.get("handle"),
        platform_metadata.get("handle"),
    ):
        value = str(candidate or "").strip().strip("/")
        if value:
            return value
    return None


def _extract_storefront_description(product_data: Dict[str, Any]) -> Optional[str]:
    raw = _ensure_dict(product_data.get("raw"))
    for candidate in (
        product_data.get("description"),
        product_data.get("description_raw"),
        product_data.get("body_html"),
        raw.get("body_html"),
        raw.get("bodyHtml"),
        raw.get("description"),
    ):
        value = str(candidate or "").strip()
        if value:
            return value
    return None


def _extract_storefront_image_urls(product_data: Dict[str, Any]) -> List[str]:
    raw = _ensure_dict(product_data.get("raw"))
    candidates: List[str] = []
    image_url = product_data.get("image_url")
    if isinstance(image_url, str) and image_url.strip():
        candidates.append(image_url.strip())
    images = product_data.get("images")
    if isinstance(images, list):
        candidates.extend(str(item).strip() for item in images if isinstance(item, str) and str(item).strip())
    raw_image = _ensure_dict(raw.get("image"))
    raw_image_src = str(raw_image.get("src") or "").strip()
    if raw_image_src:
        candidates.append(raw_image_src)
    raw_images = raw.get("images")
    if isinstance(raw_images, list):
        for item in raw_images:
            if not isinstance(item, dict):
                continue
            src = str(item.get("src") or item.get("url") or "").strip()
            if src:
                candidates.append(src)
    return _dedupe_seed_image_urls(candidates, limit=20)


def _availability_from_product_summary(value: Any) -> Optional[str]:
    if isinstance(value, bool):
        return "in_stock" if value else "out_of_stock"
    return _normalize_seed_availability(value)


def _build_storefront_seed_variants(
    *,
    product_data: Dict[str, Any],
    product_summary: Dict[str, Any],
    fallback_image_url: Optional[str],
) -> List[Dict[str, Any]]:
    variants: List[Dict[str, Any]] = []
    try:
        sp = StandardProduct.model_validate(product_data)
        for variant in sp.variants or []:
            inventory_quantity = int(variant.inventory_quantity or 0)
            availability = "in_stock" if inventory_quantity > 0 else "out_of_stock"
            variants.append(
                {
                    "variant_id": str(variant.variant_id or variant.id),
                    "id": str(variant.id),
                    "sku": str(variant.sku or "").strip() or None,
                    "title": str(variant.title or "").strip() or "Default",
                    "price_amount": float(variant.price) if variant.price is not None else None,
                    "currency": str(sp.currency or "").strip() or None,
                    "availability": availability,
                    "inventory_quantity": inventory_quantity,
                    "image_url": str(variant.image_url or fallback_image_url or "").strip() or None,
                }
            )
    except Exception:
        variants = []

    if variants:
        return variants

    fallback_price = product_summary.get("price")
    if fallback_price is None:
        return []

    availability = _availability_from_product_summary(product_summary.get("availability")) or "in_stock"
    variant_id = (
        str(product_data.get("variant_id") or "").strip()
        or str(product_data.get("product_id") or product_data.get("id") or "").strip()
        or "default"
    )
    return [
        {
            "variant_id": variant_id,
            "id": variant_id,
            "sku": str(product_data.get("sku") or "").strip() or None,
            "title": "Default",
            "price_amount": fallback_price,
            "currency": product_summary.get("currency"),
            "availability": availability,
            "inventory_quantity": int(product_data.get("inventory_quantity") or 0),
            "image_url": str(fallback_image_url or "").strip() or None,
        }
    ]


def _infer_storefront_referral_market(candidates: List[Dict[str, Any]], explicit_market: Optional[str]) -> str:
    if explicit_market:
        return _normalize_market(explicit_market)
    counts: Dict[str, int] = {}
    for candidate in candidates:
        currency = str(candidate.get("price_currency") or "").strip().upper()
        market = _CURRENCY_TO_REFERRAL_MARKET.get(currency)
        if not market:
            continue
        counts[market] = int(counts.get(market) or 0) + 1
    if not counts:
        return "US"
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]


async def _fetch_storefront_referral_seed_candidates(
    *,
    merchant_id: str,
    limit: int,
    market: Optional[str],
) -> Dict[str, Any]:
    matched_domains = await get_merchant_referral_domains(merchant_id)
    primary_domain = matched_domains[0] if matched_domains else None
    if not primary_domain:
        return {
            "merchant_id": merchant_id,
            "matched_domains": matched_domains,
            "primary_domain": None,
            "market": _normalize_market(market),
            "candidates": [],
        }

    row_limit = max(int(limit or 50) * 8, 200)
    latest_rows = await database.fetch_all(
        """
        SELECT merchant_id, platform, platform_product_id, product_data, cached_at, expires_at
        FROM (
          SELECT DISTINCT ON (platform, platform_product_id)
            merchant_id,
            platform,
            platform_product_id,
            product_data,
            cached_at,
            expires_at,
            id
          FROM products_cache
          WHERE merchant_id = :merchant_id
            AND platform = 'shopify'
          ORDER BY platform, platform_product_id, cached_at DESC NULLS LAST, id DESC NULLS LAST
        ) latest
        ORDER BY cached_at DESC NULLS LAST
        LIMIT :limit
        """,
        {"merchant_id": merchant_id, "limit": row_limit},
    )

    candidates: List[Dict[str, Any]] = []
    for row in latest_rows or []:
        row_dict = dict(row or {})
        product_data = _ensure_dict(row_dict.get("product_data"))
        handle = _extract_storefront_handle(product_data)
        if not handle:
            continue
        attached_product_key = f"{merchant_id}|shopify|{row_dict.get('platform_product_id')}"
        storefront_url = _normalize_seed_url_for_id(f"https://{primary_domain}/products/{handle}")
        product_summary = _extract_product_summary(product_data, str(row_dict.get("platform_product_id") or ""))
        image_urls = _extract_storefront_image_urls(product_data)
        image_url = image_urls[0] if image_urls else product_summary.get("image_url")
        variants = _build_storefront_seed_variants(
            product_data=product_data,
            product_summary=product_summary,
            fallback_image_url=image_url,
        )
        if not variants:
            continue
        title = str(product_summary.get("title") or "").strip()
        if not title:
            continue
        availability = (
            _availability_from_product_summary(product_summary.get("availability"))
            or variants[0].get("availability")
            or "in_stock"
        )
        candidates.append(
            {
                "merchant_id": merchant_id,
                "platform": "shopify",
                "platform_product_id": str(row_dict.get("platform_product_id") or "").strip(),
                "attached_product_key": attached_product_key,
                "storefront_url": storefront_url,
                "domain": primary_domain,
                "title": title,
                "description": _extract_storefront_description(product_data),
                "image_url": image_url,
                "image_urls": image_urls,
                "price_amount": product_summary.get("price"),
                "price_currency": str(product_summary.get("currency") or "").strip().upper() or None,
                "availability": availability,
                "variants": variants,
                "merchant_display_name": str(product_data.get("vendor") or "").strip() or None,
                "brand": str(product_data.get("vendor") or "").strip() or None,
                "category": str(product_data.get("product_type") or "").strip() or None,
                "product_id": str(product_summary.get("product_id") or row_dict.get("platform_product_id") or "").strip() or None,
            }
        )
        if len(candidates) >= int(limit or 50):
            break

    resolved_market = _infer_storefront_referral_market(candidates, market)
    for candidate in candidates:
        candidate["market"] = resolved_market
        candidate["external_product_id"] = _stable_external_product_id(candidate["storefront_url"])

    return {
        "merchant_id": merchant_id,
        "matched_domains": matched_domains,
        "primary_domain": primary_domain,
        "market": resolved_market,
        "candidates": candidates,
    }


async def _derive_seed_seller_columns(
    *,
    attached_product_key: Optional[str],
    brand: Optional[str],
    destination: Optional[str],
    source_system: str,
) -> Tuple[Optional[str], Optional[str]]:
    """ADR-009 D3 (docs/adr/ADR-009-seller-of-record-identity.md; IDENTITY_
    REFERENCE §4): derive `(seller_ref, seed_kind)` for a NEW employee/ops-created
    seed. A storefront seed pointing at the merchant's own store resolves SELF
    (destination belongs to the anchor); a referral to another seller resolves
    CROSS. Unresolvable → `(None, None)` (honest pre-A9-4 NULL; never 'self')."""
    from services.seller_identity import (
        anchor_merchant_from_product_key,
        derive_seed_seller,
    )

    return await derive_seed_seller(
        anchor_merchant_id=anchor_merchant_from_product_key(attached_product_key),
        brand=brand,
        destination_domain=destination,
        source_system=source_system,
    )


async def _upsert_storefront_referral_seed_candidate(
    candidate: Dict[str, Any],
    *,
    employee_id: Optional[str],
) -> Dict[str, Any]:
    attached_product_key = str(candidate.get("attached_product_key") or "").strip()
    storefront_url = str(candidate.get("storefront_url") or "").strip()
    external_product_id = str(candidate.get("external_product_id") or "").strip()
    market = _normalize_market(candidate.get("market"))
    tool = "*"

    existing_row = await database.fetch_one(
        """
        SELECT *
        FROM external_product_seeds
        WHERE status = 'active'
          AND (
            attached_product_key = :attached_product_key
            OR external_product_id = :external_product_id
            OR canonical_url = :canonical_url
            OR destination_url = :canonical_url
          )
        ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
        LIMIT 1
        """,
        {
            "attached_product_key": attached_product_key,
            "external_product_id": external_product_id,
            "canonical_url": storefront_url,
        },
    )

    seed_data = {
        "external_product_id": external_product_id,
        "merchant_display_name": candidate.get("merchant_display_name"),
        "product_id": candidate.get("product_id"),
        "brand": candidate.get("brand"),
        "category": candidate.get("category"),
        "title": candidate.get("title"),
        "description": candidate.get("description"),
        "image_url": candidate.get("image_url"),
        "image_urls": _dedupe_seed_image_urls(list(candidate.get("image_urls") or []), limit=20),
        "availability": candidate.get("availability"),
        "variants": list(candidate.get("variants") or []),
        "utm_template": DEFAULT_UTM_TEMPLATE,
        "partner_type": "merchant_storefront",
        "disclosure_text": DEFAULT_DISCLOSURE_TEXT,
        "source": "employee_storefront_seed_backfill",
        "product": {
            "product_id": candidate.get("product_id"),
            "brand": candidate.get("brand"),
            "category": candidate.get("category"),
            "platform": candidate.get("platform"),
            "platform_product_id": candidate.get("platform_product_id"),
        },
        "snapshot": {
            "canonical_url": storefront_url,
            "destination_url": storefront_url,
            "domain": candidate.get("domain"),
            "title": candidate.get("title"),
            "description": candidate.get("description"),
            "image_url": candidate.get("image_url"),
            "image_urls": _dedupe_seed_image_urls(list(candidate.get("image_urls") or []), limit=20),
            "price_amount": candidate.get("price_amount"),
            "price_currency": candidate.get("price_currency"),
            "availability": candidate.get("availability"),
            "variants": list(candidate.get("variants") or []),
        },
    }

    # Preserve codex-curated review/audit fields when re-mirroring an
    # existing seed. Without this, the catalog-intelligence backfill
    # cycle wipes review_summary / reviewed_ingredient_ids / audit /
    # etc. on every run — the user-reported "codex reviewed and
    # re-backfilled again and again" symptom traces back to this code
    # path overwriting `seed_data` wholesale. See
    # _preserve_seed_data_review_fields docstring + audit 2026-05-09.
    if existing_row:
        existing_seed_data = _ensure_json_obj(dict(existing_row).get("seed_data"))
        seed_data = _preserve_seed_data_review_fields(seed_data, existing_seed_data)

    # Note: the actual auto-audit step lives inside `_seed_data_payload`
    # (the funnel through which every seed_data write in this file
    # passes). PR #412 originally added an explicit audit call here; it
    # was removed in favour of the funnel approach so all 9 write paths
    # in this file get audited centrally without per-call-site repetition.

    values = {
        "external_product_id": external_product_id,
        "market": market,
        "tool": tool,
        "utm_template": DEFAULT_UTM_TEMPLATE,
        "partner_type": "merchant_storefront",
        "disclosure_text": DEFAULT_DISCLOSURE_TEXT,
        "destination_url": storefront_url,
        "canonical_url": storefront_url,
        "domain": candidate.get("domain"),
        "title": candidate.get("title"),
        "image_url": candidate.get("image_url"),
        "price_amount": candidate.get("price_amount"),
        "price_currency": candidate.get("price_currency"),
        "availability": candidate.get("availability"),
        "seed_data": _seed_data_payload(seed_data),
        "notes": "storefront_seed_backfill",
        "created_by_employee_id": str(employee_id) if employee_id else None,
        "attached_product_key": attached_product_key,
        "attached_variant_id": None,
    }

    if existing_row:
        row = dict(existing_row)
        if row.get("attached_product_key") and row.get("attached_product_key") == attached_product_key:
            existing_url = str(row.get("canonical_url") or row.get("destination_url") or "").strip()
            if existing_url and existing_url != storefront_url:
                return {
                    "action": "skipped",
                    "reason": "custom_attached_seed_destination",
                    "seed_id": row.get("id"),
                    "external_product_id": external_product_id,
                    "attached_product_key": attached_product_key,
                    "canonical_url": existing_url,
                    "title": row.get("title") or candidate.get("title"),
                }

        await _execute_seed_data_stmt(
            """
            UPDATE external_product_seeds
            SET external_product_id = :external_product_id,
                market = :market,
                tool = :tool,
                utm_template = :utm_template,
                partner_type = :partner_type,
                disclosure_text = :disclosure_text,
                destination_url = :destination_url,
                canonical_url = :canonical_url,
                domain = :domain,
                title = :title,
                image_url = :image_url,
                price_amount = :price_amount,
                price_currency = :price_currency,
                availability = :availability,
                seed_data = :seed_data,
                notes = :notes,
                created_by_employee_id = :created_by_employee_id,
                attached_product_key = :attached_product_key,
                attached_variant_id = :attached_variant_id,
                updated_at = NOW()
            WHERE id = :id
            """,
            {
                **values,
                "id": row.get("id"),
            },
        )
        seed_id = str(row.get("id") or "").strip()
        action = "updated"
    else:
        seed_id = _seed_id()
        # ADR-009 D3: a storefront seed's destination IS the merchant's own store
        # → derive resolves SELF (seller_ref = the anchor merchant). NULL only if
        # unmintable (logged loudly). New-row derivation only; the UPDATE branch
        # above leaves an existing row's seller_ref untouched (A9-4 backfills).
        seller_ref, seed_kind = await _derive_seed_seller_columns(
            attached_product_key=attached_product_key or None,
            brand=candidate.get("brand"),
            destination=values.get("domain") or storefront_url,
            source_system="employee_storefront_seed",
        )
        await _execute_seed_data_stmt(
            """
            INSERT INTO external_product_seeds (
              id, external_product_id, market, tool, utm_template, partner_type, disclosure_text,
              destination_url, canonical_url, domain, title, image_url,
              price_amount, price_currency, availability,
              seed_data,
              status, notes, created_by_employee_id, attached_product_key, attached_variant_id,
              seller_ref, seed_kind
            ) VALUES (
              :id, :external_product_id, :market, :tool, :utm_template, :partner_type, :disclosure_text,
              :destination_url, :canonical_url, :domain, :title, :image_url,
              :price_amount, :price_currency, :availability,
              :seed_data,
              'active', :notes, :created_by_employee_id, :attached_product_key, :attached_variant_id,
              :seller_ref, :seed_kind
            )
            """,
            {
                **values,
                "id": seed_id,
                "seller_ref": seller_ref,
                "seed_kind": seed_kind,
            },
        )
        action = "created"

    await database.execute(
        """
        UPDATE external_product_seeds
        SET status = 'disabled',
            notes = COALESCE(notes, '') || :note,
            updated_at = NOW()
        WHERE id <> :id
          AND status = 'active'
          AND (
            attached_product_key = :attached_product_key
            OR canonical_url = :canonical_url
            OR destination_url = :canonical_url
          )
        """,
        {
            "id": seed_id,
            "attached_product_key": attached_product_key,
            "canonical_url": storefront_url,
            "note": f" superseded_by:{seed_id}",
        },
    )

    return {
        "action": action,
        "seed_id": seed_id,
        "external_product_id": external_product_id,
        "attached_product_key": attached_product_key,
        "canonical_url": storefront_url,
        "title": candidate.get("title"),
    }


async def _run_external_seed_import_task(
    *,
    task_id: str,
    csv_text: str,
    current_user: dict,
    market: str,
    tool: str,
    mode: str,
) -> None:
    started_at = datetime.now(timezone.utc)
    try:
        await _ensure_external_seed_import_tasks_table()
        await database.execute(
            """
            UPDATE employee_external_seed_import_tasks
            SET status = 'running',
                updated_at = NOW()
            WHERE id = :id
            """,
            {"id": task_id},
        )

        result = await _import_external_seeds_csv_text(
            text=csv_text,
            current_user=current_user,
            market=market,
            tool=tool,
            mode=mode,
        )

        duration_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
        stats = {
            "duration_ms": duration_ms,
            "created": result.created,
            "updated": result.updated,
            "errors_count": len(result.errors or []),
            "seed_ids_count": len(result.seedIds or []),
        }

        await database.execute(
            """
            UPDATE employee_external_seed_import_tasks
            SET status = 'success',
                created_count = :created_count,
                updated_count = :updated_count,
                errors = :errors,
                seed_ids = :seed_ids,
                stats = :stats,
                finished_at = NOW(),
                updated_at = NOW()
            WHERE id = :id
            """,
            {
                "id": task_id,
                "created_count": int(result.created or 0),
                "updated_count": int(result.updated or 0),
                "errors": json.dumps(list(result.errors or [])[:500]),
                "seed_ids": json.dumps(list(result.seedIds or [])[:5000]),
                "stats": json.dumps(stats),
            },
        )
    except Exception as exc:
        try:
            duration_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
            await database.execute(
                """
                UPDATE employee_external_seed_import_tasks
                SET status = 'error',
                    error = :error,
                    stats = :stats,
                    finished_at = NOW(),
                    updated_at = NOW()
                WHERE id = :id
                """,
                {
                    "id": task_id,
                    "error": str(exc)[:800],
                    "stats": json.dumps({"duration_ms": duration_ms}),
                },
            )
        except Exception:
            pass


@router.get("/external-seeds/import-tasks/{task_id}")
async def get_external_seed_import_task(
    task_id: str,
    current_user: dict = Depends(get_current_employee),
):
    await _ensure_external_seed_import_tasks_table()
    row = await database.fetch_one(
        "SELECT * FROM employee_external_seed_import_tasks WHERE id = :id",
        {"id": task_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="TASK_NOT_FOUND")
    data = dict(row)
    return {
        "status": "success",
        "task": {
            "id": data.get("id"),
            "status": data.get("status"),
            "market": data.get("market"),
            "tool": data.get("tool"),
            "mode": data.get("mode"),
            "created": int(data.get("created_count") or 0),
            "updated": int(data.get("updated_count") or 0),
            "errors": [str(x) for x in _ensure_json_list(data.get("errors")) if str(x)],
            "seedIds": [str(x) for x in _ensure_json_list(data.get("seed_ids")) if str(x)],
            "error": data.get("error"),
            "stats": _ensure_json_obj(data.get("stats")),
            "created_at": _to_iso(data.get("created_at")),
            "updated_at": _to_iso(data.get("updated_at")),
            "finished_at": _to_iso(data.get("finished_at")),
        },
    }


@router.post("/external-seeds/import-csv", response_model=ExternalSeedsCsvImportResponse)
async def import_external_seeds_csv(
    req: Request,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_employee),
    market: str = Query("US"),
    tool: str = Query("*"),
    mode: str = Query("upsert"),
    async_import: bool = Query(False, alias="async"),
) -> ExternalSeedsCsvImportResponse:
    raw = await req.body()
    text = raw.decode("utf-8", errors="replace")
    if not text.strip():
        raise HTTPException(status_code=400, detail="EMPTY_CSV")

    employee_id = current_user.get("employee_id") or current_user.get("employeeId")
    created_by = str(current_user.get("email") or employee_id or "")

    if async_import:
        await _ensure_external_seed_import_tasks_table()
        task_id = _external_seed_import_task_id()
        await database.execute(
            """
            INSERT INTO employee_external_seed_import_tasks (
              id, status, market, tool, mode, created_by_employee_id,
              created_count, updated_count, errors, seed_ids, stats,
              created_at, updated_at
            ) VALUES (
              :id, 'pending', :market, :tool, :mode, :created_by_employee_id,
              0, 0, :errors, :seed_ids, :stats,
              NOW(), NOW()
            )
            """,
            {
                "id": task_id,
                "market": _normalize_market(market),
                "tool": _normalize_tool(tool),
                "mode": str(mode or "upsert").strip().lower(),
                "created_by_employee_id": created_by,
                "errors": "[]",
                "seed_ids": "[]",
                "stats": json.dumps({"input_bytes": len(raw)}),
            },
        )
        background_tasks.add_task(
            _run_external_seed_import_task,
            task_id=task_id,
            csv_text=text,
            current_user=current_user,
            market=market,
            tool=tool,
            mode=mode,
        )
        return ExternalSeedsCsvImportResponse(created=0, updated=0, errors=[], seedIds=[], taskId=task_id)

    return await _import_external_seeds_csv_text(
        text=text,
        current_user=current_user,
        market=market,
        tool=tool,
        mode=mode,
    )


async def _import_external_seeds_csv_text(
    *,
    text: str,
    current_user: dict,
    market: str,
    tool: str,
    mode: str,
) -> ExternalSeedsCsvImportResponse:
    """
    CSV import for external seeds / external products.

    Content-Type: text/csv (body is raw CSV text).

    Required columns:
      - destination_url (or url)
    Optional columns:
      - market, tool, title, image_url, price_amount, price_currency, availability
      - utm_template, partner_type, disclosure_text, notes
      - attached_product_key, attached_variant_id
      - status (active|disabled)
    """
    await _ensure_external_seeds_table()

    if not text.strip():
        raise HTTPException(status_code=400, detail="EMPTY_CSV")

    default_market = _normalize_market(market)
    default_tool = _normalize_tool(tool)
    mode_norm = str(mode or "upsert").strip().lower()
    if mode_norm not in ("create", "upsert"):
        raise HTTPException(status_code=400, detail="INVALID_MODE")

    reader = csv.DictReader(io.StringIO(text))
    fieldnames_norm = {_normalize_csv_key(x) for x in (reader.fieldnames or [])}
    is_catalog_export = (
        "product_url" in fieldnames_norm
        and "destination_url" not in fieldnames_norm
        and "url" not in fieldnames_norm
    )

    created = 0
    updated = 0
    errors: List[str] = []
    seed_ids: List[str] = []

    employee_id = current_user.get("employee_id") or current_user.get("employeeId")
    created_by = str(current_user.get("email") or employee_id or "")

    if is_catalog_export:
        # Catalog-style import (one row per variant, grouped into a single seed per Product URL).
        # Example: Tom Ford export CSV.
        groups: Dict[str, Dict[str, Any]] = {}
        group_meta: Dict[str, Dict[str, Any]] = {}

        for idx, row in enumerate(reader, start=2):
            try:
                nrow = { _normalize_csv_key(k): v for k, v in (row or {}).items() if k is not None }

                product_url_raw = str(nrow.get("product_url") or "").strip()
                if not product_url_raw:
                    raise ValueError("MISSING_PRODUCT_URL")
                product_url = _normalize_seed_url_for_id(_require_http_url(product_url_raw))

                row_market = _normalize_market(nrow.get("market") or default_market)
                row_tool = _normalize_tool(nrow.get("tool") or default_tool)

                product_title = str(nrow.get("product_title") or nrow.get("title") or "").strip() or None
                brand = str(nrow.get("brand") or "").strip() or None
                description = str(nrow.get("ai_merged_description") or nrow.get("description") or "").strip() or None

                variant_sku = str(nrow.get("sku") or nrow.get("option_value") or "").strip() or None
                variant_id_raw = str(nrow.get("variant_id") or "").strip() or None
                variant_id = variant_sku or variant_id_raw or f"v_{idx}"

                option_name = str(nrow.get("option_name") or "").strip() or None
                option_value = str(nrow.get("option_value") or "").strip() or None

                deep_link = str(nrow.get("deep_link") or nrow.get("variant_url") or "").strip() or None
                if deep_link and not deep_link.startswith(("http://", "https://")):
                    deep_link = None

                price_amount = _parse_float(nrow.get("price") or nrow.get("price_amount") or nrow.get("priceamount"))
                price_currency = str(nrow.get("currency") or nrow.get("price_currency") or nrow.get("pricecurrency") or "").strip().upper() or None
                availability = _normalize_seed_availability(nrow.get("availability"))

                variant_image_url = str(nrow.get("variant_image_url") or "").strip() or None
                if variant_image_url and not variant_image_url.startswith(("http://", "https://")):
                    variant_image_url = None

                label_image_url = str(
                    nrow.get("variant_label_image_url")
                    or nrow.get("swatch_image_url")
                    or nrow.get("label_image_url")
                    or nrow.get("variant_swatch_image_url")
                    or ""
                ).strip() or None
                if label_image_url and not label_image_url.startswith(("http://", "https://")):
                    label_image_url = None

                external_product_id = (
                    str(nrow.get("external_product_id") or "").strip()
                    or _stable_external_product_id(product_url)
                )
                if not external_product_id:
                    raise ValueError("MISSING_EXTERNAL_PRODUCT_ID")

                # Market consistency checks. CSV imports historically wrote
                # whatever (market, currency, url) tuple appeared in the
                # spreadsheet, even when fields contradicted — surfacing as
                # "$0 / EUR price for US users" + "Korean PDP in US recall"
                # in the shopping-agent chat. Reject the row at import so
                # bad data never reaches catalog_products.
                cur_err = validate_market_currency(row_market, price_currency)
                if cur_err is not None:
                    raise ValueError(cur_err)
                domain_err = validate_market_domain(row_market, product_url)
                if domain_err is not None:
                    raise ValueError(domain_err)

                group_key = f"{row_market}|{row_tool}|{external_product_id}"
                groups.setdefault(group_key, {"variants": [], "image_urls": []})
                gm = group_meta.setdefault(
                    group_key,
                    {
                        "market": row_market,
                        "tool": row_tool,
                        "external_product_id": external_product_id,
                        "destination_url": product_url,
                        "canonical_url": product_url,
                        "title": product_title,
                        "brand": brand,
                        "description": description,
                    },
                )

                # Merge group-level fields (first non-empty wins).
                if not gm.get("title") and product_title:
                    gm["title"] = product_title
                if not gm.get("brand") and brand:
                    gm["brand"] = brand
                if not gm.get("description") and description:
                    gm["description"] = description

                options: Dict[str, str] = {}
                if option_name and option_value:
                    options[option_name] = option_value

                variant_title = option_value or variant_sku or variant_id
                variant_payload: Dict[str, Any] = {
                    "variant_id": variant_id,
                    "title": _normalize_variant_title(variant_title),
                    **({"sku": variant_sku} if variant_sku else {}),
                    **({"price_amount": price_amount} if price_amount is not None else {}),
                    **({"price_currency": price_currency} if price_currency else {}),
                    **({"currency": price_currency} if price_currency else {}),
                    **({"availability": availability} if availability else {}),
                    **({"image_url": variant_image_url} if variant_image_url else {}),
                    **({"label_image_url": label_image_url} if label_image_url else {}),
                    **({"options": options} if options else {}),
                    **({"destination_url": deep_link} if deep_link else {}),
                }

                groups[group_key]["variants"].append(variant_payload)
                if variant_image_url:
                    groups[group_key]["image_urls"].append(variant_image_url)
            except HTTPException as exc:
                errors.append(f"Row {idx}: {exc.detail}")
            except Exception as exc:
                errors.append(f"Row {idx}: {str(exc)}")

        # Upsert each grouped product seed
        for group_key, g in groups.items():
            try:
                gm = group_meta.get(group_key) or {}
                row_market = gm.get("market") or default_market
                row_tool = gm.get("tool") or default_tool
                external_product_id = gm.get("external_product_id") or ""
                dest = gm.get("destination_url") or ""
                canonical_url = gm.get("canonical_url") or None

                if not external_product_id or not dest:
                    raise ValueError("INVALID_GROUP")

                # Deduplicate + sort variants so primary price is stable (min price first).
                seen_variant_ids: set[str] = set()
                variants: List[Dict[str, Any]] = []
                for v in g.get("variants") or []:
                    if not isinstance(v, dict):
                        continue
                    vid = str(v.get("variant_id") or "").strip()
                    if not vid or vid in seen_variant_ids:
                        continue
                    seen_variant_ids.add(vid)
                    variants.append(v)

                def _variant_price_key(v: Dict[str, Any]) -> float:
                    raw = v.get("price_amount")
                    if raw is None:
                        raw = v.get("price") or v.get("amount") or v.get("value")
                    try:
                        return float(raw)
                    except Exception:
                        return float("inf")

                variants.sort(key=_variant_price_key)

                image_urls = _dedupe_seed_image_urls([str(u) for u in (g.get("image_urls") or []) if isinstance(u, str)], limit=20)

                title = gm.get("title") or None
                brand = gm.get("brand") or None
                description = gm.get("description") or None
                image_url = image_urls[0] if image_urls else None

                # Summary price: min variant price when present.
                price_amount = None
                price_currency = None
                for v in variants:
                    if v.get("price_amount") is None:
                        continue
                    price_amount = v.get("price_amount")
                    price_currency = v.get("price_currency") or v.get("currency")
                    break
                if price_currency is not None:
                    price_currency = str(price_currency).strip().upper() or None

                # Summary availability: in_stock if any variant is in_stock.
                availability = None
                if variants:
                    any_in_stock = False
                    for v in variants:
                        a = _normalize_seed_availability(v.get("availability"))
                        if a == "in_stock" or a is None:
                            any_in_stock = True
                            break
                    availability = "in_stock" if any_in_stock else "out_of_stock"

                domain = None
                try:
                    domain = (urlparse(canonical_url or dest).hostname or "").lower() or None
                except Exception:
                    domain = None

                existing_row = None
                if mode_norm == "upsert":
                    existing_row = await database.fetch_one(
                        """
                        SELECT *
                        FROM external_product_seeds
                        WHERE market = :market
                          AND tool = :tool
                          AND external_product_id = :external_product_id
                        ORDER BY updated_at DESC, created_at DESC
                        LIMIT 1
                        """,
                        {"market": row_market, "tool": row_tool, "external_product_id": external_product_id},
                    )
                    if not existing_row:
                        match_url = canonical_url or dest
                        existing_row = await database.fetch_one(
                            """
                            SELECT *
                            FROM external_product_seeds
                            WHERE market = :market
                              AND tool = :tool
                              AND (canonical_url = :match_url OR destination_url = :match_url)
                            ORDER BY updated_at DESC, created_at DESC
                            LIMIT 1
                            """,
                            {"market": row_market, "tool": row_tool, "match_url": match_url},
                        )

                if existing_row:
                    existing = dict(existing_row)
                    seed_id = str(existing.get("id"))
                    seed_data = _ensure_json_obj(existing.get("seed_data"))
                    seed_data["external_product_id"] = seed_data.get("external_product_id") or external_product_id
                    seed_data["source"] = seed_data.get("source") or "employee_seed_csv_catalog"
                    if title is not None:
                        seed_data["title"] = title
                    if brand is not None:
                        seed_data["brand"] = brand
                    if description is not None:
                        seed_data["description"] = description
                    if image_url is not None:
                        seed_data["image_url"] = image_url
                    if image_urls:
                        seed_data["image_urls"] = image_urls
                    if availability is not None:
                        seed_data["availability"] = availability
                    if variants:
                        seed_data["variants"] = variants

                    canonical_url_update = canonical_url if canonical_url is not None else existing.get("canonical_url")
                    domain_update = domain if domain is not None else existing.get("domain")

                    update_values: Dict[str, Any] = {
                        "id": seed_id,
                        "external_product_id": external_product_id,
                        "destination_url": dest,
                        "canonical_url": canonical_url_update,
                        "domain": domain_update,
                        "seed_data": _seed_data_payload(seed_data),
                        "created_by_employee_id": str(employee_id) if employee_id else None,
                    }
                    set_clauses = [
                        "external_product_id = :external_product_id",
                        "destination_url = :destination_url",
                        "canonical_url = :canonical_url",
                        "domain = :domain",
                        "seed_data = :seed_data",
                        "updated_at = NOW()",
                        "created_by_employee_id = :created_by_employee_id",
                    ]

                    if title is not None:
                        update_values["title"] = title
                        set_clauses.append("title = :title")
                    if image_url is not None:
                        update_values["image_url"] = image_url
                        set_clauses.append("image_url = :image_url")
                    if price_amount is not None:
                        update_values["price_amount"] = price_amount
                        set_clauses.append("price_amount = :price_amount")
                    if price_currency is not None:
                        update_values["price_currency"] = price_currency
                        set_clauses.append("price_currency = :price_currency")
                    if availability is not None:
                        update_values["availability"] = availability
                        set_clauses.append("availability = :availability")

                    await _execute_seed_data_stmt(
                        f"UPDATE external_product_seeds SET {', '.join(set_clauses)} WHERE id = :id",
                        update_values,
                    )
                    updated += 1
                    seed_ids.append(seed_id)
                    continue

                seed_id = _seed_id()
                seed_data: Dict[str, Any] = {
                    "external_product_id": external_product_id,
                    "title": title,
                    "brand": brand,
                    "description": description,
                    "image_url": image_url,
                    "image_urls": image_urls,
                    "availability": availability,
                    "variants": variants,
                    "disclosure_text": DEFAULT_DISCLOSURE_TEXT,
                    "source": "employee_seed_csv_catalog",
                }

                # ADR-009 D3: CSV-catalog seed has no anchor (attached_product_key
                # NULL) → derive resolves CROSS to an observed seller (brand +
                # destination domain). NULL only if unmintable (logged loudly).
                seller_ref, seed_kind = await _derive_seed_seller_columns(
                    attached_product_key=None,
                    brand=brand,
                    destination=domain or dest,
                    source_system="employee_seed_csv_catalog",
                )
                await _execute_seed_data_stmt(
                    """
                    INSERT INTO external_product_seeds (
                      id, external_product_id, market, tool,
                      utm_template, partner_type, disclosure_text,
                      destination_url, canonical_url, domain,
                      title, image_url, price_amount, price_currency, availability,
                      seed_data, status, notes, created_by_employee_id, attached_product_key, attached_variant_id,
                      seller_ref, seed_kind,
                      created_at, updated_at
                    ) VALUES (
                      :id, :external_product_id, :market, :tool,
                      :utm_template, :partner_type, :disclosure_text,
                      :destination_url, :canonical_url, :domain,
                      :title, :image_url, :price_amount, :price_currency, :availability,
                      :seed_data, :status, :notes, :created_by_employee_id, :attached_product_key, :attached_variant_id,
                      :seller_ref, :seed_kind,
                      NOW(), NOW()
                    )
                    """,
                    {
                        "id": seed_id,
                        "external_product_id": external_product_id,
                        "market": row_market,
                        "tool": row_tool,
                        "utm_template": None,
                        "partner_type": None,
                        "disclosure_text": DEFAULT_DISCLOSURE_TEXT,
                        "destination_url": dest,
                        "canonical_url": canonical_url,
                        "domain": domain,
                        "title": title,
                        "image_url": image_url,
                        "price_amount": price_amount,
                        "price_currency": price_currency,
                        "availability": availability,
                        "seed_data": _seed_data_payload(seed_data),
                        "status": "active",
                        "notes": None,
                        "created_by_employee_id": created_by,
                        "attached_product_key": None,
                        "attached_variant_id": None,
                        "seller_ref": seller_ref,
                        "seed_kind": seed_kind,
                    },
                )
                created += 1
                seed_ids.append(seed_id)
            except HTTPException as exc:
                errors.append(f"Group {group_key}: {exc.detail}")
            except Exception as exc:
                errors.append(f"Group {group_key}: {str(exc)}")
    else:
        for idx, row in enumerate(reader, start=2):
            try:
                destination_url = str(row.get("destination_url") or row.get("url") or "").strip()
                if not destination_url:
                    raise ValueError("MISSING_DESTINATION_URL")
                dest = _normalize_seed_url_for_id(_require_http_url(destination_url))

                row_market = _normalize_market(row.get("market") or default_market)
                row_tool = _normalize_tool(row.get("tool") or default_tool)

                title = str(row.get("title") or "").strip() or None
                description = str(row.get("description") or row.get("product_details") or "").strip() or None
                image_url = str(row.get("image_url") or row.get("imageUrl") or "").strip() or None
                availability = _normalize_seed_availability(row.get("availability"))
                utm_template = str(row.get("utm_template") or row.get("utmTemplate") or "").strip() or None
                partner_type = str(row.get("partner_type") or row.get("partnerType") or "").strip() or None
                disclosure_text = str(row.get("disclosure_text") or row.get("disclosureText") or "").strip() or None
                notes = str(row.get("notes") or "").strip() or None
                status = str(row.get("status") or "").strip().lower() or None

                attached_product_key = str(row.get("attached_product_key") or row.get("product_key") or "").strip() or None
                attached_variant_id = str(row.get("attached_variant_id") or row.get("variant_id") or "").strip() or None
                if attached_variant_id == "":
                    attached_variant_id = None
                if attached_variant_id is not None and attached_variant_id != "∅":
                    attached_variant_id = attached_variant_id
                if attached_variant_id is None and attached_product_key:
                    attached_variant_id = "∅"

                price_amount = _parse_float(row.get("price_amount") or row.get("price") or row.get("priceAmount"))
                price_currency = str(row.get("price_currency") or row.get("currency") or row.get("priceCurrency") or "").strip() or None

                canonical_url = str(row.get("canonical_url") or row.get("canonicalUrl") or "").strip() or None
                if canonical_url:
                    canonical_url = _normalize_seed_url_for_id(_require_http_url(canonical_url))
                domain = None
                try:
                    domain = (urlparse(canonical_url or dest).hostname or "").lower() or None
                except Exception:
                    domain = None

                external_product_id = str(row.get("external_product_id") or "").strip() or _stable_external_product_id(canonical_url or dest)
                if not external_product_id:
                    raise ValueError("MISSING_EXTERNAL_PRODUCT_ID")

                # Same market consistency checks as the catalog-export
                # branch above. Reject mismatched (market, currency) and
                # (market, domain) at import so bad data doesn't reach
                # external_product_seeds → catalog_products → recall.
                cur_err = validate_market_currency(row_market, price_currency)
                if cur_err is not None:
                    raise ValueError(cur_err)
                domain_err = validate_market_domain(row_market, canonical_url or dest)
                if domain_err is not None:
                    raise ValueError(domain_err)

                existing_row = None
                if mode_norm == "upsert":
                    existing_row = await database.fetch_one(
                        """
                        SELECT *
                        FROM external_product_seeds
                        WHERE market = :market
                          AND tool = :tool
                          AND external_product_id = :external_product_id
                        ORDER BY updated_at DESC, created_at DESC
                        LIMIT 1
                        """,
                        {"market": row_market, "tool": row_tool, "external_product_id": external_product_id},
                    )
                    if not existing_row:
                        match_url = canonical_url or dest
                        existing_row = await database.fetch_one(
                            """
                            SELECT *
                            FROM external_product_seeds
                            WHERE market = :market
                              AND tool = :tool
                              AND (canonical_url = :match_url OR destination_url = :match_url)
                            ORDER BY updated_at DESC, created_at DESC
                            LIMIT 1
                            """,
                            {"market": row_market, "tool": row_tool, "match_url": match_url},
                        )

                if existing_row:
                    existing = dict(existing_row)
                    seed_id = str(existing.get("id"))
                    seed_data = _ensure_json_obj(existing.get("seed_data"))
                    seed_data["external_product_id"] = seed_data.get("external_product_id") or external_product_id
                    seed_data["source"] = seed_data.get("source") or "employee_seed_csv"

                    if title is not None:
                        seed_data["title"] = title
                    if description is not None:
                        seed_data["description"] = description
                    if image_url is not None:
                        seed_data["image_url"] = image_url
                    if availability is not None:
                        seed_data["availability"] = availability
                    if utm_template is not None:
                        seed_data["utm_template"] = utm_template
                    if partner_type is not None:
                        seed_data["partner_type"] = partner_type
                    if disclosure_text is not None:
                        seed_data["disclosure_text"] = disclosure_text

                    canonical_url_update = canonical_url if canonical_url is not None else existing.get("canonical_url")
                    domain_update = domain if domain is not None else existing.get("domain")

                    update_values: Dict[str, Any] = {
                        "id": seed_id,
                        "external_product_id": external_product_id,
                        "destination_url": dest,
                        "canonical_url": canonical_url_update,
                        "domain": domain_update,
                        "seed_data": _seed_data_payload(seed_data),
                        "created_by_employee_id": str(employee_id) if employee_id else None,
                    }
                    set_clauses = [
                        "external_product_id = :external_product_id",
                        "destination_url = :destination_url",
                        "canonical_url = :canonical_url",
                        "domain = :domain",
                        "seed_data = :seed_data",
                        "updated_at = NOW()",
                        "created_by_employee_id = :created_by_employee_id",
                    ]

                    if title is not None:
                        update_values["title"] = title
                        set_clauses.append("title = :title")
                    if image_url is not None:
                        update_values["image_url"] = image_url
                        set_clauses.append("image_url = :image_url")
                    if price_amount is not None:
                        update_values["price_amount"] = price_amount
                        set_clauses.append("price_amount = :price_amount")
                    if price_currency is not None:
                        update_values["price_currency"] = price_currency
                        set_clauses.append("price_currency = :price_currency")
                    if availability is not None:
                        update_values["availability"] = availability
                        set_clauses.append("availability = :availability")
                    if utm_template is not None:
                        update_values["utm_template"] = utm_template
                        set_clauses.append("utm_template = :utm_template")
                    if partner_type is not None:
                        update_values["partner_type"] = partner_type
                        set_clauses.append("partner_type = :partner_type")
                    if disclosure_text is not None:
                        update_values["disclosure_text"] = disclosure_text
                        set_clauses.append("disclosure_text = :disclosure_text")
                    if notes is not None:
                        update_values["notes"] = notes
                        set_clauses.append("notes = :notes")
                    if status is not None:
                        if status not in ("active", "disabled"):
                            raise ValueError("INVALID_STATUS")
                        update_values["status"] = status
                        set_clauses.append("status = :status")
                    if attached_product_key is not None:
                        update_values["attached_product_key"] = attached_product_key
                        set_clauses.append("attached_product_key = :attached_product_key")
                        update_values["attached_variant_id"] = attached_variant_id or "∅"
                        set_clauses.append("attached_variant_id = :attached_variant_id")

                    await _execute_seed_data_stmt(
                        f"UPDATE external_product_seeds SET {', '.join(set_clauses)} WHERE id = :id",
                        update_values,
                    )
                    updated += 1
                    seed_ids.append(seed_id)
                    continue

                # Create
                seed_id = _seed_id()
                seed_data: Dict[str, Any] = {
                    "external_product_id": external_product_id,
                    "title": title,
                    "description": description,
                    "image_url": image_url,
                    "availability": availability,
                    "utm_template": utm_template,
                    "partner_type": partner_type,
                    "disclosure_text": disclosure_text or DEFAULT_DISCLOSURE_TEXT,
                    "source": "employee_seed_csv",
                }

                # ADR-009 D3: a partner-referral CSV seed. If it attaches to a
                # merchant's own store (attached_product_key owns the domain) it
                # resolves SELF; otherwise CROSS to an observed seller. With no
                # brand on this CSV shape a CROSS seed is UNRESOLVABLE → NULL +
                # loud log (honest gap; A9-4 backfills). Never assumed 'self'.
                seller_ref, seed_kind = await _derive_seed_seller_columns(
                    attached_product_key=attached_product_key,
                    brand=seed_data.get("brand"),
                    destination=domain or dest,
                    source_system="employee_seed_csv",
                )
                await _execute_seed_data_stmt(
                    """
                    INSERT INTO external_product_seeds (
                      id, external_product_id, market, tool,
                      utm_template, partner_type, disclosure_text,
                      destination_url, canonical_url, domain,
                      title, image_url, price_amount, price_currency, availability,
                      seed_data, status, notes, created_by_employee_id, attached_product_key, attached_variant_id,
                      seller_ref, seed_kind,
                      created_at, updated_at
                    ) VALUES (
                      :id, :external_product_id, :market, :tool,
                      :utm_template, :partner_type, :disclosure_text,
                      :destination_url, :canonical_url, :domain,
                      :title, :image_url, :price_amount, :price_currency, :availability,
                      :seed_data, :status, :notes, :created_by_employee_id, :attached_product_key, :attached_variant_id,
                      :seller_ref, :seed_kind,
                      NOW(), NOW()
                    )
                    """,
                    {
                        "id": seed_id,
                        "external_product_id": external_product_id,
                        "market": row_market,
                        "tool": row_tool,
                        "utm_template": utm_template,
                        "partner_type": partner_type,
                        "disclosure_text": disclosure_text or DEFAULT_DISCLOSURE_TEXT,
                        "destination_url": dest,
                        "canonical_url": canonical_url,
                        "domain": domain,
                        "title": title,
                        "image_url": image_url,
                        "price_amount": price_amount,
                        "price_currency": price_currency,
                        "availability": availability,
                        "seed_data": _seed_data_payload(seed_data),
                        "status": status if status in ("active", "disabled") else "active",
                        "notes": notes,
                        "created_by_employee_id": created_by,
                        "attached_product_key": attached_product_key,
                        "attached_variant_id": attached_variant_id or ("∅" if attached_product_key else None),
                        "seller_ref": seller_ref,
                        "seed_kind": seed_kind,
                    },
                )
                created += 1
                seed_ids.append(seed_id)
            except HTTPException as exc:
                errors.append(f"Row {idx}: {exc.detail}")
            except Exception as exc:
                errors.append(f"Row {idx}: {str(exc)}")

    return ExternalSeedsCsvImportResponse(created=created, updated=updated, errors=errors, seedIds=seed_ids)


@router.post("/external-seeds/preview")
async def preview_external_seed(
    body: PreviewExternalSeedRequest,
    current_user: dict = Depends(get_current_employee),
):
    market = _normalize_market(body.market)
    dest = _require_http_url(body.destination_url)

    snapshot = None
    try:
        snapshot = await resolve_external_offer(market=market, url=dest, force_refresh=bool(body.force_refresh))
    except Exception as exc:
        parsed = urlparse(dest)
        domain = parsed.hostname or None
        path_tail = (parsed.path or "").rstrip("/").split("/")[-1]
        guess = path_tail.replace("-", " ").replace("_", " ").strip()
        guess_title = guess.title() if guess else None
        return {
            "status": "degraded",
            "error": f"snapshot_failed: {str(exc)[:200]}",
            "preview": {
                "url": dest,
                "canonical_url": dest,
                "domain": domain,
                "external_product_id": _stable_external_product_id(dest),
                "title": guess_title,
                "description": None,
                "image_url": None,
                "image_urls": [],
                "price_amount": None,
                "price_currency": None,
                "availability": None,
                "variants": [],
                "domain_allowed": True,
            },
        }

    canonical_url = getattr(snapshot, "canonical_url", None) or dest
    domain = getattr(snapshot, "domain", None)

    # Preview is best-effort and does not persist anything.
    domain_allowed = True
    try:
        domain_allowed = await _is_domain_allowed(market=market, destination_url=canonical_url)
    except Exception:
        domain_allowed = True

    variants = []
    image_urls: list[str] = []
    description = None
    evidence = getattr(snapshot, "evidence", None)
    if isinstance(evidence, dict):
        raw_variants = evidence.get("variants")
        if isinstance(raw_variants, list):
            variants = raw_variants
        raw_images = evidence.get("image_urls") or evidence.get("images")
        if isinstance(raw_images, list):
            image_urls = [str(u).strip() for u in raw_images if isinstance(u, str) and str(u).strip()]
        raw_desc = evidence.get("description")
        if isinstance(raw_desc, str) and raw_desc.strip():
            description = raw_desc.strip()

    return {
        "status": "success",
        "preview": {
            "url": dest,
            "canonical_url": canonical_url,
            "domain": domain,
            "external_product_id": _stable_external_product_id(canonical_url),
            "title": getattr(snapshot, "title", None),
            "description": description,
            "image_url": getattr(snapshot, "image_url", None),
            "image_urls": image_urls,
            "price_amount": getattr(snapshot, "price_amount", None),
            "price_currency": getattr(snapshot, "price_currency", None),
            "availability": getattr(snapshot, "availability", None),
            "variants": variants,
            "domain_allowed": domain_allowed,
        },
    }


def _build_external_seed_audit_where_clauses(
    *,
    q: Optional[str],
    status: str,
    attached: Optional[bool],
    domain: Optional[str],
    market: Optional[str],
    seed_id: Optional[str],
) -> tuple[list[str], Dict[str, Any]]:
    where = ["status = :status"]
    values: Dict[str, Any] = {"status": status}

    if attached is True:
        where.append("attached_product_key IS NOT NULL")
    elif attached is False:
        where.append("attached_product_key IS NULL")

    normalized_domain = (domain or "").strip().lower()
    if normalized_domain:
        values["domain"] = normalized_domain
        values["domain_like"] = f"%.{normalized_domain}"
        where.append("(LOWER(domain) = :domain OR LOWER(domain) LIKE :domain_like)")

    normalized_market = (market or "").strip().upper()
    if normalized_market:
        values["market"] = normalized_market
        where.append("UPPER(market) = :market")

    normalized_seed_id = (seed_id or "").strip()
    if normalized_seed_id:
        values["seed_id"] = normalized_seed_id
        where.append("id = :seed_id")

    normalized_q = (q or "").strip()
    if normalized_q:
        values["q"] = normalized_q
        values["q_like"] = f"%{normalized_q}%"
        where.append(
            "("
            "destination_url ILIKE :q_like"
            " OR canonical_url ILIKE :q_like"
            " OR domain ILIKE :q_like"
            " OR title ILIKE :q_like"
            " OR id = :q"
            " OR external_product_id = :q"
            ")"
        )

    return where, values


async def _fetch_external_seed_rows_for_audit(
    *,
    q: Optional[str],
    status: str,
    attached: Optional[bool],
    domain: Optional[str],
    market: Optional[str],
    seed_id: Optional[str],
    limit: int,
) -> List[Dict[str, Any]]:
    where, values = _build_external_seed_audit_where_clauses(
        q=q,
        status=status,
        attached=attached,
        domain=domain,
        market=market,
        seed_id=seed_id,
    )
    values["limit"] = limit

    rows = await database.fetch_all(
        f"""
        SELECT
          id, external_product_id, market, tool, utm_template, partner_type, disclosure_text,
          destination_url, canonical_url, domain, title, image_url,
          price_amount, price_currency, availability,
          seed_data,
          status, notes, created_by_employee_id,
          attached_product_key, attached_variant_id,
          created_at, updated_at
        FROM external_product_seeds
        WHERE {" AND ".join(where)}
        ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
        LIMIT :limit
        """,
        values,
    )
    return [dict(row) for row in rows]


def _build_external_seed_audit_response_item(row: Dict[str, Any]) -> Dict[str, Any]:
    audit_result = audit_external_seed_row(row)
    item = build_external_seed_audit_item(row, audit_result)
    seed = item.get("seed") or {}
    if row.get("created_at"):
        seed["created_at"] = _to_iso(row.get("created_at"))
    if row.get("updated_at"):
        seed["updated_at"] = _to_iso(row.get("updated_at"))
    return item


def _employee_id_from_user(current_user: Dict[str, Any]) -> str:
    return str(
        current_user.get("employee_id")
        or current_user.get("id")
        or current_user.get("user_id")
        or current_user.get("email")
        or ""
    ).strip()


async def _fetch_external_seed_rows_by_ids(seed_ids: List[str]) -> List[Dict[str, Any]]:
    normalized_ids = [str(seed_id).strip() for seed_id in seed_ids if str(seed_id).strip()]
    if not normalized_ids:
        return []
    rows = await database.fetch_all(
        """
        SELECT
          id, external_product_id, market, tool, utm_template, partner_type, disclosure_text,
          destination_url, canonical_url, domain, title, image_url,
          price_amount, price_currency, availability,
          seed_data,
          status, notes, created_by_employee_id,
          attached_product_key, attached_variant_id,
          created_at, updated_at
        FROM external_product_seeds
        WHERE id = ANY(:seed_ids)
        """,
        {"seed_ids": normalized_ids},
    )
    return [dict(row) for row in rows]


async def _fetch_employee_pci_kb_scope_review_rows() -> List[Dict[str, Any]]:
    await _ensure_employee_pci_kb_scope_reviews_table()
    rows = await database.fetch_all(
        """
        SELECT
          sku_key,
          decision,
          notes,
          reviewed_by_employee_id,
          reviewed_at,
          external_seed_id,
          brand,
          product_name,
          scope_decision,
          scope_reason,
          source_ref,
          canonical_url,
          market,
          created_at,
          updated_at
        FROM employee_pci_kb_scope_reviews
        ORDER BY reviewed_at DESC NULLS LAST, updated_at DESC NULLS LAST
        """
    )
    return [dict(row) for row in rows]


async def _upsert_employee_pci_kb_scope_review_row(
    *,
    sku_key: str,
    decision: str,
    notes: Optional[str],
    current_user: Dict[str, Any],
    item: Dict[str, Any],
) -> None:
    await _ensure_employee_pci_kb_scope_reviews_table()
    await database.execute("DELETE FROM employee_pci_kb_scope_reviews WHERE sku_key = :sku_key", {"sku_key": sku_key})
    await database.execute(
        """
        INSERT INTO employee_pci_kb_scope_reviews (
          sku_key,
          decision,
          notes,
          reviewed_by_employee_id,
          reviewed_at,
          external_seed_id,
          brand,
          product_name,
          scope_decision,
          scope_reason,
          source_ref,
          canonical_url,
          market,
          created_at,
          updated_at
        ) VALUES (
          :sku_key,
          :decision,
          :notes,
          :reviewed_by_employee_id,
          NOW(),
          :external_seed_id,
          :brand,
          :product_name,
          :scope_decision,
          :scope_reason,
          :source_ref,
          :canonical_url,
          :market,
          NOW(),
          NOW()
        )
        """,
        {
          "sku_key": sku_key,
          "decision": decision,
          "notes": notes,
          "reviewed_by_employee_id": _employee_id_from_user(current_user),
          "external_seed_id": item.get("external_seed_id"),
          "brand": item.get("brand"),
          "product_name": item.get("product_name"),
          "scope_decision": item.get("scope_decision"),
          "scope_reason": item.get("scope_reason"),
          "source_ref": item.get("source_ref"),
          "canonical_url": item.get("canonical_url"),
          "market": item.get("market"),
        },
    )


async def _delete_employee_pci_kb_scope_review_row(sku_key: str) -> None:
    await _ensure_employee_pci_kb_scope_reviews_table()
    await database.execute("DELETE FROM employee_pci_kb_scope_reviews WHERE sku_key = :sku_key", {"sku_key": sku_key})


@router.get("/external-seeds/audit-queue")
async def list_external_seed_audit_queue(
    q: Optional[str] = Query(default=None),
    attached: Optional[bool] = Query(default=None),
    status: str = Query(default="active"),
    merchant_id: Optional[str] = Query(default=None),
    domain: Optional[str] = Query(default=None),
    market: Optional[str] = Query(default=None),
    severity: Optional[str] = Query(default=None),
    anomaly_type: Optional[str] = Query(default=None),
    flagged_only: bool = Query(default=True),
    seed_id: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=200),
    current_user: dict = Depends(get_current_employee),
):
    await _ensure_external_seeds_table()
    if merchant_id:
        inventory = await fetch_merchant_referral_inventory(merchant_id=merchant_id, status=status)
        rows = filter_referral_inventory_rows(
            inventory["rows"],
            q=q,
            attached=attached,
            domain=domain,
            market=market,
            seed_id=seed_id,
            limit=limit,
        )
    else:
        rows = await _fetch_external_seed_rows_for_audit(
            q=q,
            status=status,
            attached=attached,
            domain=domain,
            market=market,
            seed_id=seed_id,
            limit=limit,
        )

    results = [audit_external_seed_row(row) for row in rows]
    filtered_pairs = [
        (row, result)
        for row, result in zip(rows, results)
        if audit_result_matches_filters(
            result,
            severity=severity or "",
            anomaly_type=anomaly_type or "",
            flagged_only=bool(flagged_only),
        )
    ]

    items = [_build_external_seed_audit_response_item(row) for row, _ in filtered_pairs]
    items.sort(key=audit_item_sort_key)

    return {
        "status": "success",
        "summary": summarize_audit_results([result for _, result in filtered_pairs]),
        "meta": {
            "returned": len(items),
            "limit": limit,
            "filters": {
                "q": q,
                "attached": attached,
                "status": status,
                "merchant_id": merchant_id,
                "domain": domain,
                "market": market,
                "severity": severity,
                "anomaly_type": anomaly_type,
                "flagged_only": bool(flagged_only),
                "seed_id": seed_id,
            },
        },
        "items": items,
    }


@router.get("/external-seeds/{seed_id}/audit")
async def get_external_seed_audit(
    seed_id: str,
    current_user: dict = Depends(get_current_employee),
):
    await _ensure_external_seeds_table()
    rows = await _fetch_external_seed_rows_for_audit(
        q=None,
        status="active",
        attached=None,
        domain=None,
        market=None,
        seed_id=seed_id,
        limit=1,
    )
    if not rows:
        row = await database.fetch_one(
            "SELECT * FROM external_product_seeds WHERE id = :id",
            {"id": seed_id},
        )
        if not row:
            raise HTTPException(status_code=404, detail="SEED_NOT_FOUND")
        rows = [dict(row)]

    item = _build_external_seed_audit_response_item(rows[0])
    return {
        "status": "success",
        "item": item,
    }


@router.post("/external-seeds/backfill-storefront")
async def backfill_storefront_external_seeds(
    body: BackfillStorefrontExternalSeedsRequest,
    current_user: dict = Depends(get_current_employee),
):
    await _ensure_external_seeds_table()
    merchant_id = str(body.merchant_id or "").strip()
    if not merchant_id:
        raise HTTPException(status_code=400, detail="MERCHANT_ID_REQUIRED")

    candidate_bundle = await _fetch_storefront_referral_seed_candidates(
        merchant_id=merchant_id,
        limit=body.limit,
        market=body.market,
    )
    candidates = list(candidate_bundle.get("candidates") or [])
    if body.dry_run:
        return {
            "status": "success",
            "merchant_id": merchant_id,
            "market": candidate_bundle.get("market"),
            "primary_domain": candidate_bundle.get("primary_domain"),
            "matched_domains": candidate_bundle.get("matched_domains") or [],
            "dry_run": True,
            "candidate_count": len(candidates),
            "created": 0,
            "updated": 0,
            "skipped": 0,
            "items": [
                {
                    "action": "preview",
                    "attached_product_key": candidate.get("attached_product_key"),
                    "canonical_url": candidate.get("storefront_url"),
                    "title": candidate.get("title"),
                    "external_product_id": candidate.get("external_product_id"),
                }
                for candidate in candidates[:20]
            ],
        }

    employee_id = _employee_id_from_user(current_user)
    created = 0
    updated = 0
    skipped = 0
    items: List[Dict[str, Any]] = []
    for candidate in candidates:
        result = await _upsert_storefront_referral_seed_candidate(candidate, employee_id=employee_id)
        items.append(result)
        action = str(result.get("action") or "")
        if action == "created":
            created += 1
        elif action == "updated":
            updated += 1
        else:
            skipped += 1

    return {
        "status": "success",
        "merchant_id": merchant_id,
        "market": candidate_bundle.get("market"),
        "primary_domain": candidate_bundle.get("primary_domain"),
        "matched_domains": candidate_bundle.get("matched_domains") or [],
        "dry_run": False,
        "candidate_count": len(candidates),
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "items": items[:20],
    }


@router.get("/pci-kb-scope-reviews")
async def list_pci_kb_scope_reviews(
    q: Optional[str] = Query(default=None),
    brand: Optional[str] = Query(default=None),
    domain: Optional[str] = Query(default=None),
    review_priority: Optional[str] = Query(default=None),
    scope_reason: Optional[str] = Query(default=None),
    decision: Optional[str] = Query(default=None),
    unresolved_only: bool = Query(default=True),
    limit: int = Query(default=200, ge=1, le=500),
    current_user: dict = Depends(get_current_employee),
):
    await _ensure_external_seeds_table()
    await _ensure_employee_pci_kb_scope_reviews_table()
    try:
        kb_rows = await asyncio.to_thread(fetch_pci_kb_rows_sync)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    seed_ids = list({extract_seed_id_from_sku_key(row.get("sku_key")) for row in kb_rows if extract_seed_id_from_sku_key(row.get("sku_key"))})
    seed_rows = await _fetch_external_seed_rows_by_ids(seed_ids)
    review_rows = await _fetch_employee_pci_kb_scope_review_rows()

    queue = build_queue_items(kb_rows, seed_rows, review_rows)
    filtered_items = filter_queue_items(
        queue["items"],
        q=q or "",
        brand=brand or "",
        domain=domain or "",
        review_priority=review_priority or "",
        scope_reason=scope_reason or "",
        decision=decision or "",
        unresolved_only=bool(unresolved_only),
    )[:limit]

    return {
        "status": "success",
        "summary": {
            **queue["summary"],
            **summarize_filtered_queue(filtered_items),
        },
        "meta": {
            "returned": len(filtered_items),
            "limit": limit,
            "filters": {
                "q": q,
                "brand": brand,
                "domain": domain,
                "review_priority": review_priority,
                "scope_reason": scope_reason,
                "decision": decision,
                "unresolved_only": bool(unresolved_only),
            },
        },
        "items": filtered_items,
    }


@router.patch("/pci-kb-scope-reviews/{sku_key:path}")
async def update_pci_kb_scope_review(
    sku_key: str,
    body: UpdatePciKbScopeReviewRequest,
    current_user: dict = Depends(get_current_employee),
):
    await _ensure_external_seeds_table()
    await _ensure_employee_pci_kb_scope_reviews_table()

    decision = str(body.decision or "").strip()
    allowed_decisions = {
        "keep_in_kb",
        "remove_from_kb",
        "needs_seed_rebuild",
        "needs_policy_review",
        "reopen",
    }
    if decision not in allowed_decisions:
        raise HTTPException(status_code=400, detail="INVALID_DECISION")

    try:
        kb_rows = await asyncio.to_thread(fetch_pci_kb_rows_sync)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    seed_ids = list({extract_seed_id_from_sku_key(row.get("sku_key")) for row in kb_rows if extract_seed_id_from_sku_key(row.get("sku_key"))})
    seed_rows = await _fetch_external_seed_rows_by_ids(seed_ids)
    review_rows = await _fetch_employee_pci_kb_scope_review_rows()
    queue = build_queue_items(kb_rows, seed_rows, review_rows)
    item = next((entry for entry in queue["items"] if str(entry.get("sku_key")) == sku_key), None)
    if not item and decision != "remove_from_kb":
        raise HTTPException(status_code=404, detail="PCI_KB_SCOPE_ITEM_NOT_FOUND")

    if decision == "reopen":
        await _delete_employee_pci_kb_scope_review_row(sku_key)
    else:
        if decision == "remove_from_kb":
            await asyncio.to_thread(delete_pci_kb_rows_sync, [sku_key])
        review_item = item or {
            "sku_key": sku_key,
            "external_seed_id": extract_seed_id_from_sku_key(sku_key),
            "brand": None,
            "product_name": None,
            "scope_decision": "review",
            "scope_reason": "manual_removed",
            "source_ref": None,
            "canonical_url": None,
            "market": None,
        }
        await _upsert_employee_pci_kb_scope_review_row(
            sku_key=sku_key,
            decision=decision,
            notes=body.notes,
            current_user=current_user,
            item=review_item,
        )

    return {
        "status": "success",
        "decision": decision,
        "resolved": REVIEW_DECISION_RESOLVED.get(decision, False),
        "sku_key": sku_key,
    }


@router.get("/external-seeds")
async def list_external_seeds(
    q: Optional[str] = Query(default=None),
    attached: Optional[bool] = Query(default=None),
    status: str = Query(default="active"),
    merchant_id: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: dict = Depends(get_current_employee),
):
    await _ensure_external_seeds_table()
    rows: List[Dict[str, Any]]
    if merchant_id:
        inventory = await fetch_merchant_referral_inventory(merchant_id=merchant_id, status=status)
        rows = filter_referral_inventory_rows(
            inventory["rows"],
            q=q,
            attached=attached,
            limit=limit,
        )
    else:
        where = ["status = :status"]
        values: Dict[str, Any] = {"status": status, "limit": limit}
        if attached is True:
            where.append("attached_product_key IS NOT NULL")
        elif attached is False:
            where.append("attached_product_key IS NULL")
        if q:
            q = q.strip()
            if q:
                values["q"] = q
                values["q_like"] = f"%{q}%"
                where.append(
                    "("
                    "destination_url ILIKE :q_like"
                    " OR canonical_url ILIKE :q_like"
                    " OR domain ILIKE :q_like"
                    " OR title ILIKE :q_like"
                    " OR id = :q"
                    " OR external_product_id = :q"
                    " OR seed_data->>'external_product_id' = :q"
                    " OR seed_data->>'product_id' = :q"
                    " OR seed_data->'product'->>'product_id' = :q"
                    " OR EXISTS ("
                    "   SELECT 1"
                    "   FROM jsonb_array_elements("
                    "     CASE"
                    "       WHEN jsonb_typeof(seed_data->'variants') = 'array' THEN seed_data->'variants'"
                    "       ELSE '[]'::jsonb"
                    "     END"
                    "   ) AS v"
                    "   WHERE (v->>'variant_id' = :q OR v->>'id' = :q OR v->>'sku' = :q)"
                    " )"
                    ")"
                )

        try:
            fetched_rows = await database.fetch_all(
                f"""
                SELECT
                  id, external_product_id, market, tool, utm_template, partner_type, disclosure_text,
                  destination_url, canonical_url, domain, title, image_url,
                  price_amount, price_currency, availability,
                  seed_data,
                  status, notes, created_by_employee_id,
                  attached_product_key, attached_variant_id,
                  created_at, updated_at
                FROM external_product_seeds
                WHERE {" AND ".join(where)}
                ORDER BY created_at DESC
                LIMIT :limit
                """,
                values,
            )
        except Exception:
            # Fallback for non-Postgres environments: drop JSONB variant/id filters.
            where_fallback = ["status = :status"]
            values_fallback: Dict[str, Any] = {"status": status, "limit": limit}
            if attached is True:
                where_fallback.append("attached_product_key IS NOT NULL")
            elif attached is False:
                where_fallback.append("attached_product_key IS NULL")
            if q:
                q2 = q.strip()
                if q2:
                    where_fallback.append(
                        "(destination_url ILIKE :q_like OR canonical_url ILIKE :q_like OR domain ILIKE :q_like OR title ILIKE :q_like)"
                    )
                    values_fallback["q_like"] = f"%{q2}%"

            fetched_rows = await database.fetch_all(
                f"""
                SELECT
                  id, external_product_id, market, tool, utm_template, partner_type, disclosure_text,
                  destination_url, canonical_url, domain, title, image_url,
                  price_amount, price_currency, availability,
                  seed_data,
                  status, notes, created_by_employee_id,
                  attached_product_key, attached_variant_id,
                  created_at, updated_at
                FROM external_product_seeds
                WHERE {" AND ".join(where_fallback)}
                ORDER BY created_at DESC
                LIMIT :limit
                """,
                values_fallback,
            )
        rows = [dict(row) for row in fetched_rows or []]
    items = []
    for r in rows:
        r = dict(r)
        seed_data = _ensure_json_obj(r.get("seed_data"))
        merchant_display_name = _seed_merchant_display_name(seed_data, r.get("domain"))
        external_product_id = (
            r.get("external_product_id")
            or seed_data.get("external_product_id")
            or _stable_external_product_id(r.get("canonical_url") or r.get("destination_url") or "")
        )
        items.append(
            {
                "id": r.get("id"),
                "external_product_id": external_product_id,
                "market": r.get("market"),
                "tool": r.get("tool"),
                "utm_template": r.get("utm_template") or seed_data.get("utm_template"),
                "partner_type": r.get("partner_type") or seed_data.get("partner_type"),
                "disclosure_text": r.get("disclosure_text") or seed_data.get("disclosure_text") or DEFAULT_DISCLOSURE_TEXT,
                "destination_url": r.get("destination_url"),
                "canonical_url": r.get("canonical_url"),
                "domain": r.get("domain"),
                "title": seed_data.get("title") or r.get("title"),
                "image_url": seed_data.get("image_url") or r.get("image_url"),
                "merchant_display_name": merchant_display_name,
                "price": _seed_primary_price(r, seed_data),
                "availability": seed_data.get("availability") or r.get("availability"),
                "product": {
                    "product_id": seed_data.get("product_id") or seed_data.get("product", {}).get("product_id"),
                    "brand": seed_data.get("brand") or seed_data.get("product", {}).get("brand"),
                    "category": seed_data.get("category") or seed_data.get("product", {}).get("category"),
                    "external_product_id": external_product_id,
                },
                "variants_count": len(_seed_variants(seed_data)),
                "status": r.get("status"),
                "notes": r.get("notes"),
                "created_by_employee_id": r.get("created_by_employee_id"),
                "attached_product_key": r.get("attached_product_key"),
                "attached_variant_id": r.get("attached_variant_id"),
                "created_at": r.get("created_at").isoformat() if r.get("created_at") else None,
                "updated_at": r.get("updated_at").isoformat() if r.get("updated_at") else None,
            }
        )
    return {"status": "success", "items": items}


@router.post("/external-seeds")
async def create_external_seed(
    body: CreateExternalSeedRequest,
    request: Request,
    current_user: dict = Depends(get_current_employee),
):
    await _ensure_external_seeds_table()
    market = _normalize_market(body.market)
    tool = _normalize_tool(body.tool)
    dest = _require_http_url(body.destination_url)

    employee_id = current_user.get("employee_id") or current_user.get("employeeId")
    attached_product_key = (body.attach_product_key or "").strip() or None
    attached_variant_id = (body.attach_variant_id or "").strip() or None
    if attached_variant_id == "∅":
        attached_variant_id = "∅"

    snapshot = None
    try:
        snapshot = await resolve_external_offer(market=market, url=dest, force_refresh=False)
    except Exception:
        snapshot = None

    canonical_url = getattr(snapshot, "canonical_url", None) if snapshot else None
    domain = getattr(snapshot, "domain", None) if snapshot else None
    snap_title = getattr(snapshot, "title", None) if snapshot else None
    snap_image_url = getattr(snapshot, "image_url", None) if snapshot else None
    snap_price_amount = getattr(snapshot, "price_amount", None) if snapshot else None
    snap_price_currency = getattr(snapshot, "price_currency", None) if snapshot else None
    snap_availability = getattr(snapshot, "availability", None) if snapshot else None
    snap_image_urls: list[str] = []
    snap_description: Optional[str] = None
    snap_variants: Optional[List[Dict[str, Any]]] = None
    evidence = getattr(snapshot, "evidence", None) if snapshot else None
    if isinstance(evidence, dict):
        raw_images = evidence.get("image_urls") or evidence.get("imageUrls") or evidence.get("images")
        if isinstance(raw_images, list):
            snap_image_urls = [str(u).strip() for u in raw_images if isinstance(u, str) and str(u).strip()]
            snap_image_urls = _dedupe_seed_image_urls(snap_image_urls, limit=20)
        raw_desc = evidence.get("description")
        if isinstance(raw_desc, str) and raw_desc.strip():
            snap_description = raw_desc.strip()
        raw_variants = evidence.get("variants")
        if isinstance(raw_variants, list) and raw_variants:
            snap_variants = [v for v in raw_variants if isinstance(v, dict)]

    match_url = canonical_url or dest
    existing_row = await database.fetch_one(
        """
        SELECT *
        FROM external_product_seeds
        WHERE status = 'active'
          AND market = :market
          AND tool = :tool
          AND (canonical_url = :match_url OR destination_url = :match_url)
        ORDER BY updated_at DESC, created_at DESC
        LIMIT 1
        """,
        {"market": market, "tool": tool, "match_url": match_url},
    )

    # Merge: employee-provided fields override snapshot-derived values.
    title = (body.title or "").strip() or snap_title
    description = (body.description or "").strip() or snap_description
    image_url = (body.image_url or "").strip() or snap_image_url
    if not image_url and body.image_urls:
        cleaned = [str(u).strip() for u in body.image_urls if isinstance(u, str) and str(u).strip()]
        cleaned = _dedupe_seed_image_urls(cleaned, limit=20)
        if cleaned:
            image_url = cleaned[0]
    if not image_url and snap_image_urls:
        image_url = snap_image_urls[0]
    price_amount = body.price_amount if body.price_amount is not None else snap_price_amount
    price_currency = (body.price_currency or "").strip() or snap_price_currency
    availability = (body.availability or "").strip() or snap_availability
    utm_template = (body.utm_template or "").strip() or None
    partner_type = (body.partner_type or "").strip() or None
    disclosure_text = (body.disclosure_text or "").strip() or DEFAULT_DISCLOSURE_TEXT

    if existing_row:
        row = dict(existing_row)
        seed_id = row.get("id")
        existing_seed_data = _ensure_json_obj(row.get("seed_data"))
        canonical_url = canonical_url or row.get("canonical_url")
        domain = domain or row.get("domain")

        title = title or existing_seed_data.get("title") or row.get("title")
        description = description or existing_seed_data.get("description")
        image_url = image_url or existing_seed_data.get("image_url") or row.get("image_url")
        if price_amount is None:
            price_amount = row.get("price_amount")
        if not price_currency:
            price_currency = row.get("price_currency")
        availability = availability or existing_seed_data.get("availability") or row.get("availability")
        utm_template = utm_template or row.get("utm_template") or existing_seed_data.get("utm_template")
        partner_type = partner_type or row.get("partner_type") or existing_seed_data.get("partner_type")
        disclosure_text = (
            disclosure_text
            or row.get("disclosure_text")
            or existing_seed_data.get("disclosure_text")
            or DEFAULT_DISCLOSURE_TEXT
        )

        attached_product_key = attached_product_key or row.get("attached_product_key")
        attached_variant_id = attached_variant_id or row.get("attached_variant_id")
        notes = body.notes if body.notes is not None else row.get("notes")
        market = market or row.get("market")
        tool = tool or row.get("tool")

        seed_data = dict(existing_seed_data)
        external_product_id = (
            row.get("external_product_id")
            or seed_data.get("external_product_id")
            or _stable_external_product_id(canonical_url or dest)
        )
        seed_data["external_product_id"] = external_product_id
        if body.merchant_display_name is not None:
            seed_data["merchant_display_name"] = (body.merchant_display_name or "").strip() or None
        if body.product_id is not None:
            seed_data["product_id"] = (body.product_id or "").strip() or None
        if body.brand is not None:
            seed_data["brand"] = (body.brand or "").strip() or None
        if body.category is not None:
            seed_data["category"] = (body.category or "").strip() or None
        seed_data["title"] = title
        seed_data["description"] = description
        seed_data["image_url"] = image_url
        if body.image_urls is not None:
            cleaned = [str(u).strip() for u in body.image_urls if isinstance(u, str) and str(u).strip()]
            seed_data["image_urls"] = _dedupe_seed_image_urls(cleaned, limit=20)
        elif snap_image_urls:
            seed_data["image_urls"] = snap_image_urls
        seed_data["availability"] = availability
        seed_data["utm_template"] = utm_template
        seed_data["partner_type"] = partner_type
        seed_data["disclosure_text"] = disclosure_text
        seed_data["source"] = seed_data.get("source") or "employee_seed"
        seed_data.setdefault("snapshot", {})
        seed_data["snapshot"].update(
            {
                "canonical_url": canonical_url,
                "domain": domain,
                "title": snap_title,
                "image_url": snap_image_url,
                "image_urls": snap_image_urls,
                "price_amount": snap_price_amount,
                "price_currency": snap_price_currency,
                "availability": snap_availability,
            }
        )
        if snap_description:
            seed_data["snapshot"]["description"] = snap_description
        if body.variants is not None and body.variants:
            seed_data["variants"] = body.variants
        elif snap_variants:
            existing_variants = _seed_variants(seed_data)
            if _should_overwrite_seed_variants(existing=existing_variants, incoming=snap_variants, product_title=title):
                seed_data["variants"] = snap_variants
            elif not existing_variants:
                seed_data["variants"] = snap_variants

        match_url = canonical_url or dest

        await _execute_seed_data_stmt(
            """
            UPDATE external_product_seeds
            SET external_product_id = :external_product_id,
                market = :market,
                tool = :tool,
                utm_template = :utm_template,
                partner_type = :partner_type,
                disclosure_text = :disclosure_text,
                destination_url = :destination_url,
                canonical_url = :canonical_url,
                domain = :domain,
                title = :title,
                image_url = :image_url,
                price_amount = :price_amount,
                price_currency = :price_currency,
                availability = :availability,
                seed_data = :seed_data,
                notes = :notes,
                created_by_employee_id = :created_by_employee_id,
                attached_product_key = :attached_product_key,
                attached_variant_id = :attached_variant_id,
                updated_at = NOW()
            WHERE id = :id
            """,
            {
                "id": seed_id,
                "external_product_id": external_product_id,
                "market": market,
                "tool": tool,
                "utm_template": utm_template,
                "partner_type": partner_type,
                "disclosure_text": disclosure_text,
                "destination_url": dest,
                "canonical_url": canonical_url,
                "domain": domain,
                "title": title,
                "image_url": image_url,
                "price_amount": price_amount,
                "price_currency": price_currency,
                "availability": availability,
                "seed_data": _seed_data_payload(seed_data),
                "notes": notes,
                "created_by_employee_id": str(employee_id) if employee_id else None,
                "attached_product_key": attached_product_key,
                "attached_variant_id": attached_variant_id,
            },
        )
        await database.execute(
            """
            UPDATE external_product_seeds
            SET status = 'disabled',
                notes = COALESCE(notes, '') || :note,
                updated_at = NOW()
            WHERE id <> :id
              AND status = 'active'
              AND market = :market
              AND tool = :tool
              AND (canonical_url = :match_url OR destination_url = :match_url)
            """,
            {
                "id": seed_id,
                "market": market,
                "tool": tool,
                "match_url": match_url,
                "note": f" superseded_by:{seed_id}",
            },
        )
    else:
        seed_id = _seed_id()
        external_product_id = _stable_external_product_id(match_url)
        seed_data: Dict[str, Any] = {
            "external_product_id": external_product_id,
            "merchant_display_name": (body.merchant_display_name or "").strip() or None,
            "product_id": (body.product_id or "").strip() or None,
            "brand": (body.brand or "").strip() or None,
            "category": (body.category or "").strip() or None,
            "title": title,
            "description": description,
            "image_url": image_url,
            "image_urls": [str(u).strip() for u in (body.image_urls or []) if isinstance(u, str) and str(u).strip()]
            if body.image_urls is not None
            else snap_image_urls,
            "availability": availability,
            "variants": body.variants or (snap_variants or []),
            "utm_template": utm_template,
            "partner_type": partner_type,
            "disclosure_text": disclosure_text,
            "source": "employee_seed",
            "snapshot": {
                "canonical_url": canonical_url,
                "domain": domain,
                "title": snap_title,
                "description": snap_description,
                "image_url": snap_image_url,
                "image_urls": snap_image_urls,
                "price_amount": snap_price_amount,
                "price_currency": snap_price_currency,
                "availability": snap_availability,
            },
        }

        # ADR-009 D3: derive the seller-of-record for this employee-created seed.
        # SELF when it attaches to the merchant's own store (attached_product_key
        # owns the destination); CROSS to an observed seller otherwise. NULL only
        # if unmintable (logged loudly) — never assumed 'self'.
        seller_ref, seed_kind = await _derive_seed_seller_columns(
            attached_product_key=attached_product_key,
            brand=(body.brand or "").strip() or None,
            destination=domain or dest,
            source_system="employee_seed",
        )
        await _execute_seed_data_stmt(
            """
            INSERT INTO external_product_seeds (
              id, external_product_id, market, tool, utm_template, partner_type, disclosure_text,
              destination_url, canonical_url, domain, title, image_url,
              price_amount, price_currency, availability,
              seed_data,
              status, notes, created_by_employee_id, attached_product_key, attached_variant_id,
              seller_ref, seed_kind
            ) VALUES (
              :id, :external_product_id, :market, :tool, :utm_template, :partner_type, :disclosure_text,
              :destination_url, :canonical_url, :domain, :title, :image_url,
              :price_amount, :price_currency, :availability,
              :seed_data,
              'active', :notes, :created_by_employee_id, :attached_product_key, :attached_variant_id,
              :seller_ref, :seed_kind
            )
            """,
            {
                "id": seed_id,
                "external_product_id": external_product_id,
                "market": market,
                "tool": tool,
                "utm_template": utm_template,
                "partner_type": partner_type,
                "disclosure_text": disclosure_text,
                "destination_url": dest,
                "canonical_url": canonical_url,
                "domain": domain,
                "title": title,
                "image_url": image_url,
                "price_amount": price_amount,
                "price_currency": price_currency,
                "availability": availability,
                "seed_data": _seed_data_payload(seed_data),
                "notes": body.notes,
                "created_by_employee_id": str(employee_id) if employee_id else None,
                "attached_product_key": attached_product_key,
                "attached_variant_id": attached_variant_id,
                "seller_ref": seller_ref,
                "seed_kind": seed_kind,
            },
        )

    redirect_url = await _make_redirect_url(
        request=request,
        market=market,
        tool=tool,
        destination_url=canonical_url or dest,
        utm_template=utm_template,
        ctx={
            "seedId": seed_id,
            **({"productKey": attached_product_key} if attached_product_key else {}),
            **({"variantId": attached_variant_id} if attached_variant_id else {}),
        },
    )

    return {
        "status": "success",
        "seed": {
            "id": seed_id,
            "external_product_id": seed_data.get("external_product_id"),
            "market": market,
            "tool": tool,
            "utm_template": utm_template,
            "partner_type": partner_type,
            "disclosure_text": disclosure_text,
            "destination_url": dest,
            "canonical_url": canonical_url or dest,
            "domain": domain,
            "title": title,
            "image_url": image_url,
            "merchant_display_name": _seed_merchant_display_name(seed_data, domain),
            "price": {"amount": price_amount, "currency": price_currency},
            "availability": availability,
            "notes": notes if existing_row else body.notes,
            "attached_product_key": attached_product_key,
            "attached_variant_id": attached_variant_id,
            "product": {
                "external_product_id": seed_data.get("external_product_id"),
                "product_id": seed_data.get("product_id"),
                "brand": seed_data.get("brand"),
                "category": seed_data.get("category"),
            },
            "variants": _seed_variants(seed_data),
        },
        "action": {"type": "redirect", "redirect_url": redirect_url, "disclosure_text": disclosure_text},
    }


@router.post("/external-seeds/hard-delete-by-domain")
async def hard_delete_external_seeds_by_domain(
    body: HardDeleteExternalSeedsByDomainRequest,
    current_user: dict = Depends(get_current_employee),
):
    await _ensure_external_seeds_table()
    where_clause, where_values, normalized_domain = _build_external_seed_domain_where_clause(
        raw_domain=body.domain,
        include_subdomains=bool(body.include_subdomains),
    )

    count_row = await database.fetch_one(
        f"SELECT COUNT(*) AS n FROM external_product_seeds WHERE {where_clause}",
        where_values,
    )
    match_count = int((dict(count_row) if count_row else {}).get("n") or 0)

    sample_rows = await database.fetch_all(
        f"""
        SELECT id, external_product_id, status, domain, title, destination_url, canonical_url
        FROM external_product_seeds
        WHERE {where_clause}
        ORDER BY created_at DESC
        LIMIT :sample_limit
        """,
        {**where_values, "sample_limit": int(body.sample_limit)},
    )
    sample = [dict(row) for row in sample_rows]

    if body.dry_run or match_count <= 0:
        return {
            "status": "success",
            "domain": normalized_domain,
            "dry_run": bool(body.dry_run),
            "match_count": match_count,
            "deleted_count": 0,
            "sample": sample,
        }

    deleted_rows = await database.fetch_all(
        f"""
        DELETE FROM external_product_seeds
        WHERE {where_clause}
        RETURNING id, external_product_id, status, domain, title, destination_url, canonical_url
        """,
        where_values,
    )
    deleted = [dict(row) for row in deleted_rows]

    return {
        "status": "success",
        "domain": normalized_domain,
        "dry_run": False,
        "match_count": match_count,
        "deleted_count": len(deleted),
        "deleted_ids": [str(row.get("id")) for row in deleted if row.get("id")],
        "sample": deleted[: min(len(deleted), int(body.sample_limit))],
    }


@router.get("/external-seeds/{seed_id}")
async def get_external_seed(
    seed_id: str,
    request: Request,
    current_user: dict = Depends(get_current_employee),
):
    await _ensure_external_seeds_table()
    row = await database.fetch_one(
        "SELECT * FROM external_product_seeds WHERE id = :id",
        {"id": seed_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="SEED_NOT_FOUND")
    row = dict(row)
    seed_data = _ensure_json_obj(row.get("seed_data"))
    external_product_id = (
        row.get("external_product_id")
        or seed_data.get("external_product_id")
        or _stable_external_product_id(row.get("canonical_url") or row.get("destination_url") or "")
    )
    redirect_url = await _make_redirect_url(
        request=request,
        market=row.get("market"),
        tool=row.get("tool"),
        destination_url=row.get("canonical_url") or row.get("destination_url"),
        utm_template=row.get("utm_template") or seed_data.get("utm_template"),
        ctx={
            "seedId": row.get("id"),
            **({"productKey": row.get("attached_product_key")} if row.get("attached_product_key") else {}),
            **({"variantId": row.get("attached_variant_id")} if row.get("attached_variant_id") else {}),
        },
    )
    disclosure_text = row.get("disclosure_text") or seed_data.get("disclosure_text") or DEFAULT_DISCLOSURE_TEXT
    return {
        "status": "success",
        "seed": {
            "id": row.get("id"),
            "external_product_id": external_product_id,
            "market": row.get("market"),
            "tool": row.get("tool"),
            "utm_template": row.get("utm_template") or seed_data.get("utm_template"),
            "partner_type": row.get("partner_type") or seed_data.get("partner_type"),
            "disclosure_text": disclosure_text,
            "destination_url": row.get("destination_url"),
            "canonical_url": row.get("canonical_url"),
            "domain": row.get("domain"),
            "title": seed_data.get("title") or row.get("title"),
            "image_url": seed_data.get("image_url") or row.get("image_url"),
            "merchant_display_name": _seed_merchant_display_name(seed_data, row.get("domain")),
            "price": _seed_primary_price(row, seed_data),
            "availability": seed_data.get("availability") or row.get("availability"),
            "status": row.get("status"),
            "notes": row.get("notes"),
            "attached_product_key": row.get("attached_product_key"),
            "attached_variant_id": row.get("attached_variant_id"),
            "created_at": row.get("created_at").isoformat() if row.get("created_at") else None,
            "updated_at": row.get("updated_at").isoformat() if row.get("updated_at") else None,
            "seed_data": seed_data,
            "variants": _seed_variants(seed_data),
            "variants_count": len(_seed_variants(seed_data)),
            "product": {
                "external_product_id": external_product_id,
                "product_id": seed_data.get("product_id") or seed_data.get("product", {}).get("product_id"),
                "brand": seed_data.get("brand") or seed_data.get("product", {}).get("brand"),
                "category": seed_data.get("category") or seed_data.get("product", {}).get("category"),
            },
        },
        "action": {"type": "redirect", "redirect_url": redirect_url, "disclosure_text": disclosure_text},
    }


@router.delete("/external-seeds/{seed_id}")
async def delete_external_seed(
    seed_id: str,
    current_user: dict = Depends(get_current_employee),
):
    await _ensure_external_seeds_table()
    row = await database.fetch_one(
        """
        SELECT id, external_product_id, status, domain, title, destination_url, canonical_url
        FROM external_product_seeds
        WHERE id = :id
        """,
        {"id": seed_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="SEED_NOT_FOUND")

    deleted_rows = await database.fetch_all(
        """
        DELETE FROM external_product_seeds
        WHERE id = :id
        RETURNING id, external_product_id, status, domain, title, destination_url, canonical_url
        """,
        {"id": seed_id},
    )
    deleted = [dict(item) for item in deleted_rows]
    if not deleted:
        deleted = [dict(row)]

    return {
        "status": "success",
        "deleted_count": len(deleted),
        "deleted_ids": [str(item.get("id")) for item in deleted if item.get("id")],
        "items": deleted,
    }


@router.patch("/external-seeds/{seed_id}")
async def update_external_seed(
    seed_id: str,
    body: UpdateExternalSeedRequest,
    current_user: dict = Depends(get_current_employee),
):
    await _ensure_external_seeds_table()
    row = await database.fetch_one("SELECT * FROM external_product_seeds WHERE id = :id", {"id": seed_id})
    if not row:
        raise HTTPException(status_code=404, detail="SEED_NOT_FOUND")
    row = dict(row)
    seed_data = _ensure_json_obj(row.get("seed_data"))
    external_product_id = (
        row.get("external_product_id")
        or seed_data.get("external_product_id")
        or _stable_external_product_id(row.get("canonical_url") or row.get("destination_url") or "")
    )
    seed_data["external_product_id"] = seed_data.get("external_product_id") or external_product_id
    seed_data.setdefault("snapshot", {})

    if body.title is not None:
        seed_data["title"] = body.title
        seed_data["snapshot"]["title"] = body.title
    if body.description is not None:
        seed_data["description"] = body.description
        seed_data["snapshot"]["description"] = body.description
        _set_manual_seed_description_override(seed_data, body.description)
    if body.image_url is not None:
        seed_data["image_url"] = body.image_url
        seed_data["snapshot"]["image_url"] = body.image_url
    if body.image_urls is not None:
        cleaned = [str(u).strip() for u in body.image_urls if isinstance(u, str) and str(u).strip()]
        deduped = _dedupe_seed_image_urls(cleaned, limit=20)
        seed_data["image_urls"] = deduped
        seed_data["snapshot"]["image_urls"] = deduped
        if body.image_url is None and deduped:
            seed_data["image_url"] = deduped[0]
    if body.availability is not None:
        seed_data["availability"] = body.availability
    if body.product_id is not None:
        seed_data["product_id"] = body.product_id
    if body.brand is not None:
        seed_data["brand"] = body.brand
    if body.category is not None:
        seed_data["category"] = body.category
    if body.variants is not None:
        seed_data["variants"] = body.variants
    if body.utm_template is not None:
        seed_data["utm_template"] = (body.utm_template or "").strip() or None
    if body.partner_type is not None:
        seed_data["partner_type"] = (body.partner_type or "").strip() or None
    if body.disclosure_text is not None:
        seed_data["disclosure_text"] = (body.disclosure_text or "").strip() or DEFAULT_DISCLOSURE_TEXT

    updates: Dict[str, Any] = {"id": seed_id}
    set_clauses: List[str] = []

    if external_product_id:
        updates["external_product_id"] = external_product_id
        set_clauses.append("external_product_id = :external_product_id")

    if body.market is not None:
        updates["market"] = _normalize_market(body.market)
        set_clauses.append("market = :market")
    if body.tool is not None:
        updates["tool"] = _normalize_tool(body.tool)
        set_clauses.append("tool = :tool")
    next_destination_url = row.get("destination_url")
    next_canonical_url = row.get("canonical_url")
    if body.destination_url is not None:
        next_destination_url = _require_http_url(body.destination_url)
        updates["destination_url"] = next_destination_url
        set_clauses.append("destination_url = :destination_url")
        seed_data["destination_url"] = next_destination_url
        seed_data["snapshot"]["destination_url"] = next_destination_url
    if body.canonical_url is not None:
        next_canonical_url = _require_http_url(body.canonical_url) if str(body.canonical_url).strip() else None
        updates["canonical_url"] = next_canonical_url
        set_clauses.append("canonical_url = :canonical_url")
        seed_data["canonical_url"] = next_canonical_url
        seed_data["snapshot"]["canonical_url"] = next_canonical_url
    if body.destination_url is not None or body.canonical_url is not None:
        next_url_for_domain = next_canonical_url or next_destination_url
        try:
            updates["domain"] = (urlparse(next_url_for_domain or "").hostname or "").lower() or None
        except Exception:
            updates["domain"] = None
        set_clauses.append("domain = :domain")
    if body.notes is not None:
        updates["notes"] = body.notes
        set_clauses.append("notes = :notes")
    if body.status is not None:
        updates["status"] = str(body.status).strip() or "active"
        set_clauses.append("status = :status")
    if body.merchant_display_name is not None:
        seed_data["merchant_display_name"] = (body.merchant_display_name or "").strip() or None
    if body.utm_template is not None:
        updates["utm_template"] = (body.utm_template or "").strip() or None
        set_clauses.append("utm_template = :utm_template")
    if body.partner_type is not None:
        updates["partner_type"] = (body.partner_type or "").strip() or None
        set_clauses.append("partner_type = :partner_type")
    if body.disclosure_text is not None:
        updates["disclosure_text"] = (body.disclosure_text or "").strip() or DEFAULT_DISCLOSURE_TEXT
        set_clauses.append("disclosure_text = :disclosure_text")

    # Compatibility: keep summary columns in sync for existing list views.
    if body.title is not None:
        updates["title"] = body.title
        set_clauses.append("title = :title")
    if body.image_url is not None:
        updates["image_url"] = body.image_url
        set_clauses.append("image_url = :image_url")
    elif body.image_urls is not None:
        deduped = seed_data.get("image_urls")
        if isinstance(deduped, list) and deduped:
            updates["image_url"] = str(deduped[0]).strip()
            set_clauses.append("image_url = :image_url")
    if body.price_amount is not None:
        updates["price_amount"] = body.price_amount
        set_clauses.append("price_amount = :price_amount")
    if body.price_currency is not None:
        updates["price_currency"] = body.price_currency
        set_clauses.append("price_currency = :price_currency")
    if body.availability is not None:
        updates["availability"] = body.availability
        set_clauses.append("availability = :availability")

    updates["seed_data"] = _seed_data_payload(seed_data)
    set_clauses.append("seed_data = :seed_data")
    set_clauses.append("updated_at = NOW()")

    await _execute_seed_data_stmt(
        f"UPDATE external_product_seeds SET {', '.join(set_clauses)} WHERE id = :id",
        updates,
    )
    return {"status": "success"}


def _same_destination(fetched: Optional[str], served: Optional[str]) -> bool:
    """Is the URL this refresh fetched the same one the serving lane hands out?

    Compared after trimming and dropping a trailing slash — the two columns are written by
    different code paths and differ cosmetically far more often than they differ in substance.
    Anything beyond that (query-string order, case) is deliberately NOT normalised: on a
    storefront a differing query string can select a different variant, so treating those as
    the same URL would reintroduce exactly the mis-attribution this guard exists to prevent.
    """
    a = str(fetched or "").strip().rstrip("/")
    b = str(served or "").strip().rstrip("/")
    return bool(a) and a == b


# A price reading that we actually STORED. `unavailable` means we read no price at all, and
# each `skipped_*` means we read something and REFUSED it (a 0-price broken-offer shape, an
# amount with no currency, a currency that disagrees with the stored one). In every refused
# case the row keeps its PREVIOUS amount, so the number we quote was not re-read and must not
# be described as freshly extracted. `unchanged` counts: we read the page and it still says
# what we store.
_PRICE_STATUSES_THAT_RE_READ_THE_STORED_PRICE = frozenset({"applied", "filled", "unchanged"})


async def _project_refreshed_seed_to_serving_surfaces(seed_id: str) -> Dict[str, int]:
    """Push a freshly re-read seed onto the surfaces a BUYER reads.

    THE BUG THIS CLOSES. `_refresh_external_seed_by_id` writes `external_product_seeds` and
    nothing else. The search/offers lane reads the seed, so it saw fresh prices — but the PDP
    (`agent_pdp_view`, via `agent_pdp_view_assembler.fetch_offers_for_keys`) and the index's
    `serving_eligible.has_price` gate both read `catalog_offers`, and NOTHING re-projected a
    seed that was already mirrored. Measured on prod 2026-09-06: 1,321 of 5,316 live products
    (25%) served a different price on the PDP than in search, 917 with a seed read inside 7 days.

    GATED AS A WHOLE, and that is a correction. The first version relied on
    `sync_offer_for_seed` self-gating on EXTERNAL_OFFER_DUAL_WRITE_ENABLED and left
    `refresh_agent_pdp_view_for_seed` UNGATED — which has no flag of its own. With the flag off
    that meant every refreshed seed (~2,000/night) still ran `build_agent_pdp_view_row`
    (~1s/key; the reconciler caps ITSELF at 300 keys/6h for that reason), the agent_pdp_view
    upsert and `recompute_serving_eligibility` — inside the sequential 3,300s crawl budget,
    while `catalog_offers` stayed untouched. All of the cost, none of the fix, and it would have
    made `stopped_early` near-certain, slowing rotation and making prices STALER. The claim
    "inert until armed" has to be true of the whole helper, so the flag is now checked here.

    Called for `unchanged` as well as `applied`/`filled` ON PURPOSE: re-projecting a price that
    did not move is what makes the nightly rotation self-healing for rows that already drifted,
    instead of needing a separate backfill pass.

    Returns counters so the batch can tell "healed 2,000" from "healed 0" — `sync_offer_for_seed`
    has seven statuses and six of them mean "did nothing" (`no_mirror_product` is expected to be
    common: the mirror is insert-only and matches on `source_ref = seed_id`). Dropping that dict
    is how a run that projected nothing would still have reported success.

    Best-effort and non-raising, mirroring the `seed_data_writer` hooks it stands in for: the
    seed row is the committed source of truth, so a projection failure must never turn a good
    price read into a failed refresh.
    """
    counts: Dict[str, Any] = {
        "attempted": 0, "projected": 0, "skipped": 0, "errored": 0, "seconds": 0.0,
    }
    if not seed_id:
        return counts
    _started = time.monotonic()
    from services.external_offer_dual_write import dual_write_enabled

    if not dual_write_enabled():
        # Inert, and cheap: no offer write AND no view rebuild. See the gating note above.
        return counts

    counts["attempted"] = 1
    try:
        from services.external_offer_dual_write import sync_offer_for_seed

        from services.external_offer_dual_write import (
            OFFER_SYNC_ERROR_STATUSES,
            OFFER_SYNC_WRITTEN_STATUSES,
        )

        outcome = await sync_offer_for_seed(seed_id)
        status = str((outcome or {}).get("status") or "").strip().lower()
        # Derived from the writer, never restated here. The first version guessed
        # {"synced","inserted","updated","ok"} — three statuses it cannot emit — and the tests
        # stubbed "ok", so the whole positive path was calibrated against a value production
        # never produces.
        if status in OFFER_SYNC_WRITTEN_STATUSES:
            counts["projected"] = 1
        elif status in OFFER_SYNC_ERROR_STATUSES:
            # The writer swallowed an exception. That is an error, not a skip: a skip means
            # "nothing to do", and counting a failed write as one hides it from the summary.
            counts["errored"] = 1
            counts["skip_" + (status or "unknown")] = 1
        else:
            counts["skipped"] = 1
            counts["skip_" + (status or "unknown")] = 1
    except Exception as exc:  # noqa: BLE001 - a cache write must not break the source of truth
        counts["errored"] = 1
        logger.warning(
            "external seed refresh: catalog_offers projection failed seed_id=%s err=%s",
            seed_id, str(exc)[:200],
        )
    try:
        from services.seed_data_writer import refresh_agent_pdp_view_for_seed

        await refresh_agent_pdp_view_for_seed(
            seed_id=seed_id, proposal_id=None, refresh_source="external_referral_refresh",
        )
    except Exception as exc:  # noqa: BLE001 - same isolation the assembler documents
        counts["errored"] = 1
        logger.warning(
            "external seed refresh: agent_pdp_view projection failed seed_id=%s err=%s",
            seed_id, str(exc)[:200],
        )
    counts["seconds"] = round(time.monotonic() - _started, 4)
    return counts


async def _stamp_crawl_attempt(seed_id: str) -> None:
    """Record that we TRIED to re-read this seed, whatever the outcome.

    THIS IS NOT A FRESHNESS SIGNAL and must never be read as one -- that is `last_crawled_at`,
    which only a fetch that reached the origin may set. This column exists solely to order the
    refresh QUEUE, and the two questions genuinely differ for the rows that matter most.

    Without it the queue deadlocks on its own failures. `get_external_referral_refresh_candidate_seed_ids`
    orders `last_crawled_at ASC NULLS FIRST`, and a seed that 404s, is bot-challenged, or is
    disallowed by robots never gets stamped -- so it stays NULL, stays first, and is retried
    every single run, forever, in front of the rows that could actually be corrected. On the
    dead-PDP audit's own numbers (10.4% of measurable seeds already broken) that is most of a
    nightly batch spent re-fetching URLs we have already proven are gone.

    Advancing the attempt clock on a failure does NOT claim the price is fresh. It claims only
    that we have already spent this round's request on this row, which is true.
    """
    await _execute_seed_data_stmt(
        "UPDATE external_product_seeds SET last_crawl_attempt_at = NOW() WHERE id = :id",
        {"id": seed_id},
    )


async def _refresh_external_seed_by_id(
    seed_id: str, *, max_wait: Optional[float] = None
) -> Dict[str, Any]:
    """Re-read one seed's price/availability from the origin.

    `max_wait` is the CALLER'S PATIENCE and the two callers want opposite things. The employee
    route behind this is interactive: a human is waiting, so the default ceiling
    (CRAWL_MAX_WAIT_SECONDS) is right and a throttled host should fail fast. The nightly batch
    is not: it must pass 0 (unbounded) or `crawl_politeness` refuses any slot beyond ~10s,
    which is most of the backoff curve, and every remaining row on that host burns in
    milliseconds looking like the host was down. Passing it per-call rather than reading an
    env var keeps one function honest for both.
    """
    await _ensure_external_seeds_table()
    row = await database.fetch_one("SELECT * FROM external_product_seeds WHERE id = :id", {"id": seed_id})
    if not row:
        raise HTTPException(status_code=404, detail="SEED_NOT_FOUND")
    row = dict(row)

    market = row.get("market")
    tool = row.get("tool")
    dest = row.get("destination_url")
    # The host, resolved BEFORE any degraded return can fire. `domain` further down is read off
    # the snapshot, which by definition does not exist on the paths that degrade — so the
    # summary's `top_degraded_hosts` was structurally always {"unknown": N}. Prefer the seed's
    # own column, fall back to the destination URL's netloc.
    report_host = str(row.get("domain") or "").strip().lower()
    if not report_host and dest:
        try:
            report_host = (urlparse(str(dest)).hostname or "").strip().lower()
        except Exception:  # noqa: BLE001 - a malformed URL must not break the refresh
            report_host = ""
    previous_canonical_url = row.get("canonical_url")
    if not dest:
        # Stamp FIRST. The candidate query orders by `last_crawl_attempt_at ASC NULLS FIRST`,
        # so a row that raises before the stamp keeps a null attempt time, heads the queue
        # again tomorrow, and every night after — a permanent backlog that crowds out rows
        # that could actually be read.
        await _stamp_crawl_attempt(seed_id)
        raise HTTPException(status_code=400, detail="INVALID_URL")

    # THE REFRESH USED TO BE BLIND TO A DEAD LINK, and worse than blind: `raise_for_status()`
    # threw, `resolve_external_offer` caught it and returned the CACHED snapshot, this function
    # wrote that snapshot and set `updated_at = NOW()` — so fetching a 404 made the row look
    # FRESHER to the `stale_snapshot` gate. `raise_on_unavailable=True` is what stops that.
    # `observed` carries the status and the FINAL url of the request that actually left, which
    # is the only way to tell a product page from a 301 onto a collection.
    # See docs/external-seed-dead-pdp-link-audit.md §4.2 and §5.2.
    snapshot = None
    observed: Dict[str, Any] = {}
    try:
        snapshot = await resolve_external_offer(
            market=market,
            url=dest,
            force_refresh=True,
            raise_on_unavailable=True,
            observed=observed,
            max_wait=max_wait,
        )
    except ExternalOfferUnavailable as exc:
        if not _same_destination(dest, destination_liveness.destination_of(row)):
            # See the note at the success path: a verdict about a URL we do not serve is worse
            # than no verdict at all.
            await _stamp_crawl_attempt(seed_id)
            return {
                "status": "degraded",
                "error": f"destination_unavailable: http {exc.status_code}",
                "domain": report_host or None,
                "destination_refresh": {"status": "not_observed", "reason": "not_the_served_url"},
            }
        observation = destination_liveness.classify_destination(
            requested_url=dest,
            status_code=exc.status_code,
            final_url=exc.final_url,
            bot_challenged=bool(observed.get("bot_challenged")),
        )
        destination_refresh = await destination_liveness.record_destination_observation(
            seed_id, observation
        )
        # THIS PATH RECORDS, IT NEVER RETIRES. A refresh sees one URL and one status code;
        # it has not read the brand's catalogue, so it cannot tell a deleted product from a
        # WAF that answers 404 to an unfamiliar client. `record_destination_observation`
        # enforces that (an uncorroborated verdict holds the streak), and the retirement call
        # is deliberately absent here rather than left in behind a condition that cannot fire
        # — an unreachable retirement reads like a live one to the next person.
        # Retirement belongs to jobs/external_seed_destination_sweep, which has stage 1.
        await _stamp_crawl_attempt(seed_id)
        return {
            "status": "degraded",
            "error": f"destination_unavailable: http {exc.status_code}",
            "domain": report_host or None,
            "destination_refresh": destination_refresh,
        }
    except Exception as exc:
        # We never reached the origin (timeout, TLS, DNS, robots). That is NOT evidence about
        # the product, so no observation is recorded and the failure streak does not move.
        await _stamp_crawl_attempt(seed_id)
        return {
            "status": "degraded",
            "error": f"snapshot_failed: {str(exc)[:200]}",
            "domain": report_host or None,
        }

    canonical_url = getattr(snapshot, "canonical_url", None) if snapshot else None
    domain = getattr(snapshot, "domain", None) if snapshot else None
    snap_title = getattr(snapshot, "title", None) if snapshot else None
    snap_image_url = getattr(snapshot, "image_url", None) if snapshot else None
    snap_price_amount = getattr(snapshot, "price_amount", None) if snapshot else None
    snap_price_currency = getattr(snapshot, "price_currency", None) if snapshot else None
    snap_availability = getattr(snapshot, "availability", None) if snapshot else None

    snap_image_urls: list[str] = []
    snap_description: Optional[str] = None
    snap_variants: Optional[List[Dict[str, Any]]] = None
    evidence = getattr(snapshot, "evidence", None)
    if isinstance(evidence, dict):
        raw_images = evidence.get("image_urls") or evidence.get("images")
        if isinstance(raw_images, list):
            snap_image_urls = [str(u).strip() for u in raw_images if isinstance(u, str) and str(u).strip()]
            snap_image_urls = _dedupe_seed_image_urls(snap_image_urls, limit=20)
        raw_desc = evidence.get("description")
        if isinstance(raw_desc, str) and raw_desc.strip():
            snap_description = raw_desc.strip()
        raw_variants = evidence.get("variants")
        if isinstance(raw_variants, list) and raw_variants:
            snap_variants = [v for v in raw_variants if isinstance(v, dict)]

    seed_data = _ensure_json_obj(row.get("seed_data"))
    seed_data.setdefault("snapshot", {})
    seed_data["snapshot"].update(
        {
            "canonical_url": canonical_url,
            "domain": domain,
            "title": snap_title,
            "description": snap_description,
            "image_url": snap_image_url,
            "image_urls": snap_image_urls,
            "price_amount": snap_price_amount,
            "price_currency": snap_price_currency,
            "availability": snap_availability,
            # `snapshot.fetched_at` DOES NOT EXIST. ExternalOfferSnapshot's fields are
            # (..., last_checked_at, evidence, override_*) -- so this getattr has always
            # returned None and this key has always been written as null. Nothing read it,
            # which is why it went unnoticed; `extracted_at` below is the field the
            # `stale_snapshot` gate actually reads. Use the real field, falling back to now.
            "refreshed_at": _to_iso(getattr(snapshot, "last_checked_at", None))
            or _to_iso(datetime.now(timezone.utc)),
        }
    )
    # Only overwrite curated fields if they are missing.
    if _should_replace_localized_copy(
        existing_text=seed_data.get("description"),
        incoming_text=snap_description,
        market=market,
        previous_canonical_url=previous_canonical_url,
        refreshed_canonical_url=canonical_url,
    ):
        seed_data["description"] = snap_description
    if not seed_data.get("title"):
        seed_data["title"] = snap_title
    if not seed_data.get("description"):
        if snap_description:
            seed_data["description"] = snap_description
    if not seed_data.get("image_url"):
        seed_data["image_url"] = snap_image_url
    if snap_image_urls:
        seed_data["image_urls"] = snap_image_urls
    if not seed_data.get("availability"):
        seed_data["availability"] = snap_availability
    if snap_variants:
        existing_variants = _seed_variants(seed_data)
        product_title = seed_data.get("title") or snap_title
        if _should_overwrite_seed_variants(existing=existing_variants, incoming=snap_variants, product_title=product_title):
            seed_data["variants"] = snap_variants
        elif _should_replace_seed_variant_content(
            existing=existing_variants,
            incoming=snap_variants,
            market=market,
            previous_canonical_url=previous_canonical_url,
            refreshed_canonical_url=canonical_url,
        ):
            seed_data["variants"] = snap_variants
        elif not existing_variants:
            seed_data["variants"] = snap_variants

    pending_row = dict(row)
    pending_row["seed_data"] = seed_data
    if should_suppress_stale_description_fallback(pending_row):
        seed_data.pop("description", None)
        for variant in _seed_variants(seed_data):
            if isinstance(variant, dict):
                variant.pop("description", None)

    # PRICE AND AVAILABILITY ARE FACTS WITH A SHELF LIFE, NOT CURATED COPY.
    #
    # This block used to be `price_amount = COALESCE(price_amount, :price_amount)`
    # in the SQL — "write the fresh value only if the stored one is NULL". Since a
    # seed almost always HAS a price, the refresh re-fetched the live page, wrote
    # the new price into seed_data["snapshot"], and then discarded it for the column
    # every consumer reads. Measured 2026-08-21 over 81 live K-beauty PDPs: 12.3%
    # price drift, 11.1% listed-in-stock-but-live-out-of-stock, none of it fixable
    # by any number of refresh runs.
    #
    # 🚨 WHAT THE FIRST VERSION OF THIS FIX GOT WRONG, because the shape of the
    # correction is dictated by it. It gated on `snap_price_currency is None` and on
    # `snap_availability is None`, having read `_extract_from_html`. Neither is ever
    # None by the time it arrives here — the CALLER post-processes both:
    #   * services/external_offers_service.resolve_external_offer FABRICATES a
    #     currency when extraction found none: `"JPY" if market=="JP" else "USD"`.
    #     `_detect_currency_from_text` has no `₩`/KRW case, so a Korean page priced
    #     ₩24,000 arrives as 24000.0 **USD**. The old COALESCE kept the stored KRW;
    #     a naive fix writes the fabricated USD and reports it as a correction.
    #   * availability is stored as `extracted.get("availability") or "unknown"`, so
    #     "no observation" arrives as the literal string "unknown" — and
    #     services/beauty_external_ranking maps anything outside
    #     {out_of_stock, outofstock, sold_out} to inventory 999, i.e. IN STOCK. So
    #     writing "unknown" over a known out_of_stock serves it as purchasable: the
    #     exact defect this change exists to remove.
    # Read the producer, not just the extractor.
    #
    # Resolved in Python rather than SQL so the decision is visible to callers (the
    # `price_refresh` / `availability_refresh` reports the sweep counts) and
    # reachable by a test. The statement below is asserted COALESCE-free for these
    # columns in tests/test_external_seed_refresh_price.py — the harness stubs the
    # executor, so without that assertion a re-COALESCE mutant stays green.
    def _as_price(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        # NaN survives float() and then defeats every comparison below: `nan != prev`
        # is True but `abs(nan - prev) >= 0.005` is False, so it would be written to a
        # DOUBLE PRECISION column and reported "unchanged". Treat it as no reading.
        return parsed if math.isfinite(parsed) else None

    prev_amount = _as_price(row.get("price_amount"))
    prev_currency = (str(row.get("price_currency") or "").strip().upper() or None)
    fresh_amount = _as_price(snap_price_amount)
    fresh_currency = (str(snap_price_currency or "").strip().upper() or None)

    # A SCRAPED REFRESH MAY CORRECT AN AMOUNT. IT MAY NOT REDENOMINATE AN OFFER.
    #
    # Because the currency reaching us may be a market-derived default rather than an
    # observation (see above), a disagreement between stored and fresh currency is not
    # evidence the merchant changed currency — it is more likely evidence we failed to
    # read one. Refusing the whole pair is the only safe reading, and it is reported
    # rather than swallowed so the rate is measurable. A genuine merchant currency
    # change therefore needs a human, which is the right cost: the alternative is
    # silently restating a ₩24,000 product as $24,000.
    if fresh_amount is None:
        next_amount, next_currency = prev_amount, prev_currency
        price_status = "unavailable"
    elif fresh_amount <= 0:
        # `price: 0` IS A DOCUMENTED BROKEN-OFFER SHAPE IN THIS CATALOG, not a free
        # product. `_parse_price` strips every non-digit, so an unrenderable or
        # sold-out PDP that emits `product:price:amount = 0` (or a currency glyph with
        # no number) arrives here as a clean 0.0 — and "$0 / EUR price for US users"
        # is recorded a few hundred lines up as an OBSERVED production symptom, not a
        # hypothetical. The old COALESCE preserved the stored price in this cell, so
        # writing it would be a regression introduced by the very change meant to make
        # prices trustworthy — and it lands hardest on the out-of-stock cohort this
        # work targets. Negative is unreachable (the minus sign is stripped upstream)
        # but is covered by the same comparison rather than left to be discovered.
        next_amount, next_currency = prev_amount, prev_currency
        price_status = "skipped_non_positive"
    elif fresh_currency is None:
        next_amount, next_currency = prev_amount, prev_currency
        price_status = "skipped_incomplete_pair"
    elif prev_currency is not None and fresh_currency != prev_currency:
        next_amount, next_currency = prev_amount, prev_currency
        price_status = "skipped_currency_mismatch"
    else:
        next_amount, next_currency = fresh_amount, fresh_currency
        if prev_amount is None:
            # First fill is not drift. Counting it as such would inflate the staleness
            # rate the sweep reports, which is the number this whole lane is judged on.
            price_status = "filled"
        elif abs(fresh_amount - prev_amount) >= 0.005:
            price_status = "applied"
        else:
            # Currency case-normalization ('usd' -> 'USD') lands here on purpose: the
            # WRITE is desirable, but it is not a price change and must not be counted.
            price_status = "unchanged"

    # KNOWN BEHAVIOUR CHANGE, stated rather than discovered later. The PATCH route
    # above lets an employee set price_amount/price_currency/availability directly.
    # Under the old COALESCE those edits were permanent, because the refresh could
    # never overwrite a non-NULL value — a side effect of the bug, not a designed
    # override. They are now superseded by the next successful refresh. That is the
    # intended direction for a perishable fact (a hand-typed price goes stale like any
    # other), but there is no real override mechanism to fall back on: unlike the
    # description, which has _set_manual_seed_description_override, price has none, and
    # external_offer_snapshots.override_price_amount is applied only in to_public().
    # Follow-up: give price a first-class override flag the refresh honours.
    # Availability: "unknown" is the producer's way of saying it saw nothing, so it is
    # NOT an observation and must never overwrite a known state (see the block above).
    _NO_AVAILABILITY_OBSERVATION = {"", "unknown"}
    prev_availability = (str(row.get("availability") or "").strip() or None)
    _fresh_availability_raw = str(snap_availability or "").strip()
    fresh_availability = (
        _fresh_availability_raw
        if _fresh_availability_raw.lower() not in _NO_AVAILABILITY_OBSERVATION
        else None
    )
    next_availability = fresh_availability or prev_availability
    if fresh_availability is None:
        availability_status = "unavailable"
    elif prev_availability is None:
        availability_status = "filled"
    elif fresh_availability != prev_availability:
        availability_status = "applied"
    else:
        availability_status = "unchanged"

    availability_refresh = {
        "status": availability_status,
        "changed": availability_status == "applied",
        "previous": prev_availability,
        "availability": next_availability,
    }

    price_refresh = {
        "status": price_status,
        "changed": price_status == "applied",
        "previous_amount": prev_amount,
        "previous_currency": prev_currency,
        "amount": next_amount,
        "currency": next_currency,
    }

    # HOISTED so the stamp and the liveness observation below are gated on ONE decision.
    # Deriving "did we reach the origin" twice is the twin-implementation drift this file
    # keeps paying for, and here the two copies would disagree about whether a row is fresh.
    #
    # REACHING THIS LINE IS NOT EVIDENCE OF A FETCH. `resolve_external_offer` honours
    # `raise_on_unavailable` only in its `except ExternalOfferUnavailable` arm; a timeout, a
    # TLS/DNS error, a `RobotsDisallowed` (a bare RuntimeError) or a failure inside the
    # extractor all land in its generic `except Exception`, which returns the CACHED snapshot.
    # The success path then runs normally. `observed["status_code"]` is the only proof that
    # anything actually left the process -- and `_same_destination` is the only proof it went
    # to the URL we serve rather than a legacy `destination_url` we do not.
    served_url = destination_liveness.destination_of(row)
    # ONE CLASSIFICATION, read three times: the freshness stamp, the staleness gate, and the
    # liveness verdict below all derive from THIS. `classify_destination` is pure, so hoisting
    # it costs nothing and removes the twin-implementation drift this file keeps paying for.
    #
    # `status_code is not None` ALONE IS NOT A READING, and three separate cases prove it:
    #   * `from_cache` -- `_fetch_html` stamps `observed` BEFORE returning, so a failure after
    #     the response (extractor, snapshot upsert, post-write re-read) hands back the CACHED
    #     row with `status_code` set. `resolve_external_offer` now says so explicitly.
    #   * `final_url` -- a 301 onto a collection, or onto a DIFFERENT product handle, answers
    #     200 for a page that is not the one we serve. Only the verdict looks at where the
    #     request ended; `_same_destination` compares two STORED urls and cannot see it.
    #   * `bot_challenged` -- a cf-mitigated 200 is `unverifiable`, and the liveness writer
    #     already refuses to stamp `destination_checked_at` for it. Anything claiming parity
    #     with that column has to refuse for the same reason.
    served_url = destination_liveness.destination_of(row)
    observation = None
    if observed.get("status_code") is not None and _same_destination(dest, served_url):
        observation = destination_liveness.classify_destination(
            requested_url=dest,
            status_code=int(observed["status_code"]),
            final_url=observed.get("final_url"),
            bot_challenged=bool(observed.get("bot_challenged")),
        )
    read_the_served_product = (
        observation is not None
        and observation.verdict == destination_liveness.VERDICT_LIVE
        and not observed.get("from_cache")
    )

    # CLEAR THE BLOCKER THIS REFRESH IS THE RECOMMENDED FIX FOR.
    #
    # `stale_snapshot` (services/external_referral_readiness) is raised when the CONTENT is
    # older than EXTERNAL_REFERRAL_STALE_DAYS, it is marked `auto_fixable: True`, and its
    # `recommended_action` is literally "Refresh the seed snapshot". It reads
    # `snapshot.extracted_at` via `get_content_extracted_at`, which has NO fallback by
    # design -- and until now this function wrote only `snapshot.refreshed_at`. So the gate
    # recommended an action that could not clear it, and `auto_fixable` was untrue: a seed
    # could be refreshed successfully every night and stay blocked forever.
    #
    # GATED TWICE, because the failure mode of getting this wrong is the one this whole lane
    # exists to prevent -- a stale price presented as freshly verified:
    #   * `reached_served_origin` -- a cached-snapshot fallback (timeout, TLS, robots) runs
    #     this same success path without contacting anyone, and must never clear a blocker;
    #   * the price status -- we only claim an extraction when the amount we now STORE came
    #     off the page. Refusing to write a reading and then calling the row fresh would
    #     vouch for the previous price.
    # A row we can never read a price from therefore stays blocked, which is the correct
    # direction to be wrong in: it withholds a row rather than quoting a number we did not
    # re-read.
    if read_the_served_product and price_status in _PRICE_STATUSES_THAT_RE_READ_THE_STORED_PRICE:
        seed_data["snapshot"]["extracted_at"] = _to_iso(datetime.now(timezone.utc))

    await _execute_seed_data_stmt(
        """
        UPDATE external_product_seeds
        SET canonical_url = :canonical_url,
            domain = :domain,
            title = COALESCE(title, :title),
            image_url = COALESCE(image_url, :image_url),
            price_amount = :price_amount,
            price_currency = :price_currency,
            availability = :availability,
            seed_data = :seed_data,
            updated_at = NOW(),
            -- WE SPENT THIS ROUND'S REQUEST ON THIS ROW. Advances on every terminal outcome
            -- (see _stamp_crawl_attempt), which is what keeps a permanently-dead seed from
            -- sitting at the head of the queue forever. Orders the queue; proves nothing.
            last_crawl_attempt_at = NOW(),
            -- WE ACTUALLY READ THE PRICE, FROM THE URL WE SERVE. Gated on exactly the
            -- predicate `destination_checked_at` is gated on, because it is the same claim
            -- about the same fetch. A cached-snapshot fallback keeps the OLD value rather
            -- than taking NOW() -- if it took NOW(), the hosts we can never read would be
            -- stamped fresh on every run and sorted to the BACK of the queue permanently,
            -- which is the starvation this column exists to remove, wearing a new hat.
            last_crawled_at = CASE
                WHEN CAST(:read_the_served_product AS BOOLEAN) THEN NOW()
                ELSE last_crawled_at
            END
        WHERE id = :id
        """,
        {
            "id": seed_id,
            "read_the_served_product": bool(read_the_served_product),
            "canonical_url": canonical_url,
            "domain": domain,
            "title": snap_title,
            "image_url": snap_image_url,
            "price_amount": next_amount,
            "price_currency": next_currency,
            "availability": next_availability,
            "seed_data": _seed_data_payload(seed_data),
        },
    )

    # THE FETCH REACHED THE ORIGIN, so it is an observation and it may stamp the liveness
    # columns — the ONLY writer allowed to, which is what makes `destination_checked_at`
    # answerable ("when did we last see this URL") where `updated_at` never was.
    #
    # Classified from `observed`, the real HTTP status and FINAL url, NOT from
    # `snapshot.canonical_url`: the page's self-declared canonical can point at a sibling
    # handle for perfectly live products, and a `redirected_off_product` verdict is one of the
    # two that can eventually retire a seed. A guess must never be able to do that.
    destination_refresh: Dict[str, Any] = {"status": "not_observed"}
    # ONLY JUDGE THE URL WE ACTUALLY SERVE. This function fetches `destination_url`, but the
    # readiness gate and the sweep both resolve a seed to `canonical_url or destination_url`
    # — and this very function writes `canonical_url` from the fetched page a few lines up, so
    # the two drift apart by design. Recording a verdict from the wrong one is how a seed whose
    # served link is healthy gets marked dead because a legacy `destination_url` 404s.
    # When they differ, the sweep observes the served URL; saying nothing here is correct.
    # `served_url` and `observation` are the HOISTED values from above; recomputing them here
    # is what would let the stamp and the verdict disagree.
    if observed.get("status_code") is None:
        # The fetch never reached the origin (or came from the snapshot cache), so there is
        # nothing to record. Distinct from the case below, and the drift report says which.
        destination_refresh = {"status": "not_observed", "reason": "no_origin_response"}
    elif not _same_destination(dest, served_url):
        destination_refresh = {"status": "not_observed", "reason": "not_the_served_url"}
    else:
        assert observation is not None  # same branch conditions as the hoist above
        destination_refresh = await destination_liveness.record_destination_observation(
            seed_id, observation
        )
        # THIS PATH RECORDS, IT NEVER RETIRES. A refresh sees one URL and one status code;
        # it has not read the brand's catalogue, so it cannot tell a deleted product from a
        # WAF that answers 404 to an unfamiliar client. `record_destination_observation`
        # enforces that (an uncorroborated verdict holds the streak), and the retirement call
        # is deliberately absent here rather than left in behind a condition that cannot fire
        # — an unreachable retirement reads like a live one to the next person.
        # Retirement belongs to jobs/external_seed_destination_sweep, which has stage 1.

    # The seed is committed; now make the buyer-facing surfaces agree with it. See the helper
    # for why this is gated on a price we actually re-read, and why `unchanged` is included.
    projection: Dict[str, int] = {}
    # BOTH conjuncts, matching the `extracted_at` stamp above. A cache-served fallback (a quarter
    # of rows on 09-05) also yields `unchanged`, and projecting it would stamp
    # catalog_offers.updated_at = NOW() on a row nobody re-read — claiming a freshness we did not
    # earn, which is exactly what the projection exists to stop.
    if read_the_served_product and price_status in _PRICE_STATUSES_THAT_RE_READ_THE_STORED_PRICE:
        projection = await _project_refreshed_seed_to_serving_surfaces(seed_id)

    return {
        "status": "success",
        "projection": projection,
        "seed_id": seed_id,
        "market": market,
        "tool": tool,
        "dest": dest,
        "canonical_url": canonical_url,
        "domain": domain,
        "seed_data": seed_data,
        # DID THIS "SUCCESS" ACTUALLY CONTACT THE ORIGIN? Often not, and the status alone
        # cannot say. `resolve_external_offer` honours `raise_on_unavailable` ONLY in its
        # `except ExternalOfferUnavailable` arm; anything else — a timeout, TLS, robots, and
        # now a `CrawlDelayTooLong` skip — falls into its generic `except Exception`, which
        # hands back the CACHED snapshot. This whole success path then runs having contacted
        # nobody, and the batch counts the row as `refreshed`.
        #
        # The freshness guards already hold (`observed["status_code"] is None` withholds
        # `last_crawled_at`, `destination_checked_at` and the verdict), so nothing is
        # fabricated. What was missing is any signal an OPERATOR can see: a run in which a
        # hostile robots.txt voided every row reported exactly like a complete one. Same
        # reasoning as `stopped_early`/`skipped_for_budget` on the batch summary.
        "snapshot_from_cache": bool(observed.get("from_cache")),
        # Drift report. The batch runner sums these so "we re-crawled N seeds" can
        # be stated as "N re-crawled, M prices actually moved" — the difference is
        # the whole point of the refresh existing.
        "price_refresh": price_refresh,
        "availability_refresh": availability_refresh,
        "destination_refresh": destination_refresh,
        "attached_product_key": row.get("attached_product_key"),
        "attached_variant_id": row.get("attached_variant_id"),
        "disclosure_text": row.get("disclosure_text") or seed_data.get("disclosure_text") or DEFAULT_DISCLOSURE_TEXT,
        "utm_template": row.get("utm_template") or seed_data.get("utm_template"),
    }


@router.post("/external-seeds/{seed_id}/refresh")
async def refresh_external_seed(
    seed_id: str,
    request: Request,
    current_user: dict = Depends(get_current_employee),
):
    refreshed = await _refresh_external_seed_by_id(seed_id)
    if refreshed.get("status") == "degraded":
        return refreshed
    redirect_url = await _make_redirect_url(
        request=request,
        market=refreshed["market"],
        tool=refreshed["tool"],
        destination_url=refreshed["canonical_url"] or refreshed["dest"],
        utm_template=refreshed["utm_template"],
        ctx={
            "seedId": seed_id,
            **({"productKey": refreshed["attached_product_key"]} if refreshed["attached_product_key"] else {}),
            **({"variantId": refreshed["attached_variant_id"]} if refreshed["attached_variant_id"] else {}),
        },
    )
    return {
        "status": "success",
        "action": {
            "type": "redirect",
            "redirect_url": redirect_url,
            "disclosure_text": refreshed["disclosure_text"],
        },
    }


class AttachSeedRequest(BaseModel):
    product_key: str = Field(..., min_length=3)
    variant_id: Optional[str] = None


@router.post("/external-seeds/{seed_id}/attach")
async def attach_external_seed(
    seed_id: str,
    body: AttachSeedRequest,
    current_user: dict = Depends(get_current_employee),
):
    await _ensure_external_seeds_table()
    pk = (body.product_key or "").strip()
    if pk.count("|") != 2:
        raise HTTPException(status_code=400, detail="INVALID_PRODUCT_KEY")
    vid = (body.variant_id or "").strip() or "∅"
    await database.execute(
        """
        UPDATE external_product_seeds
        SET attached_product_key = :pk,
            attached_variant_id = :vid,
            updated_at = NOW()
        WHERE id = :id
        """,
        {"pk": pk, "vid": vid, "id": seed_id},
    )
    return {"status": "success"}


@router.post("/external-seeds/{seed_id}/detach")
async def detach_external_seed(
    seed_id: str,
    current_user: dict = Depends(get_current_employee),
):
    await _ensure_external_seeds_table()
    row = await database.fetch_one("SELECT id FROM external_product_seeds WHERE id = :id", {"id": seed_id})
    if not row:
        raise HTTPException(status_code=404, detail="SEED_NOT_FOUND")
    await database.execute(
        """
        UPDATE external_product_seeds
        SET attached_product_key = NULL,
            attached_variant_id = NULL,
            updated_at = NOW()
        WHERE id = :id
        """,
        {"id": seed_id},
    )
    return {"status": "success"}


@router.get("/external-seeds/{seed_id}/resolve-subject")
async def resolve_external_seed_subject(
    seed_id: str,
    current_user: dict = Depends(get_current_employee),
):
    """
    Convenience helper for the Employee Portal to resolve the canonical PDP subject for an external seed.

    Returns:
    - product_key (when attached)
    - product_group_id (best-effort; may be null when grouping table is unavailable)
    """
    await _ensure_external_seeds_table()
    row = await database.fetch_one(
        "SELECT id, attached_product_key, attached_variant_id FROM external_product_seeds WHERE id = :id",
        {"id": seed_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="SEED_NOT_FOUND")
    r = dict(row)
    product_key = (r.get("attached_product_key") or "").strip() or None
    attached_variant_id = (r.get("attached_variant_id") or "").strip() or None
    if product_key:
        attached_variant_id = attached_variant_id or "∅"

    product_group_id: Optional[str] = None
    debug_errors: List[str] = []
    if product_key and product_key.count("|") == 2:
        try:
            parts = [p.strip() for p in product_key.split("|")]
            merchant_id, platform, platform_product_id = parts
            pg_row = await database.fetch_one(
                """
                SELECT product_group_id
                FROM product_group_members
                WHERE merchant_id = :merchant_id
                  AND platform = :platform
                  AND platform_product_id = :platform_product_id
                LIMIT 1
                """,
                {
                    "merchant_id": merchant_id,
                    "platform": platform,
                    "platform_product_id": platform_product_id,
                },
            )
            if pg_row and pg_row.get("product_group_id"):
                product_group_id = str(pg_row["product_group_id"])
        except Exception as exc:
            debug_errors.append(f"resolve_product_group_failed: {str(exc)[:200]}")

    return {
        "status": "degraded" if debug_errors else "success",
        "seed_id": seed_id,
        "product_key": product_key,
        "attached_variant_id": attached_variant_id,
        "product_group_id": product_group_id,
        **({"debug_errors": debug_errors} if debug_errors else {}),
    }


@router.get("/{product_key}/external-links")
async def list_attached_external_links(
    product_key: str,
    request: Request,
    variant_id: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: dict = Depends(get_current_employee),
):
    await _ensure_external_seeds_table()
    parts = [p.strip() for p in (product_key or "").split("|")]
    if len(parts) != 3:
        raise HTTPException(status_code=400, detail="INVALID_PRODUCT_KEY")

    merchant_id, platform, platform_product_id = parts

    values: Dict[str, Any]
    where: str

    if _is_external_seed_product_key(merchant_id=merchant_id, platform=platform):
        values = {"external_product_id": platform_product_id, "pk": product_key, "limit": limit}
        where = (
            "status = 'active' AND (external_product_id = :external_product_id OR (seed_data->>'external_product_id') = :external_product_id)"
        )
        where += " AND (attached_product_key IS NULL OR attached_product_key = :pk)"
        if variant_id:
            values["vid"] = str(variant_id).strip()
            where += " AND COALESCE(attached_variant_id, '∅') = :vid"
        try:
            rows = await database.fetch_all(
                f"""
                SELECT id, market, tool, destination_url, canonical_url, domain, title, image_url,
                       price_amount, price_currency, availability,
                       utm_template, partner_type, disclosure_text,
                       seed_data,
                       notes, attached_variant_id, created_at
                FROM external_product_seeds
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT :limit
                """,
                values,
            )
        except Exception:
            where = "status = 'active' AND external_product_id = :external_product_id"
            where += " AND (attached_product_key IS NULL OR attached_product_key = :pk)"
            if variant_id:
                where += " AND COALESCE(attached_variant_id, '∅') = :vid"
            rows = await database.fetch_all(
                f"""
                SELECT id, market, tool, destination_url, canonical_url, domain, title, image_url,
                       price_amount, price_currency, availability,
                       utm_template, partner_type, disclosure_text,
                       seed_data,
                       notes, attached_variant_id, created_at
                FROM external_product_seeds
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT :limit
                """,
                values,
            )
    else:
        values = {"pk": product_key, "limit": limit}
        where = "attached_product_key = :pk AND status = 'active'"
        if variant_id:
            values["vid"] = str(variant_id).strip()
            where += " AND attached_variant_id = :vid"

        rows = await database.fetch_all(
            f"""
            SELECT id, market, tool, destination_url, canonical_url, domain, title, image_url,
                   price_amount, price_currency, availability,
                   utm_template, partner_type, disclosure_text,
                   seed_data,
                   notes, attached_variant_id, created_at
            FROM external_product_seeds
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT :limit
            """,
            values,
        )

    items = []
    for r in rows:
        r = dict(r)
        seed_data = _ensure_json_obj(r.get("seed_data"))
        merchant_display_name = _seed_merchant_display_name(seed_data, r.get("domain"))
        redirect_url = await _make_redirect_url(
            request=request,
            market=r.get("market"),
            tool=r.get("tool"),
            destination_url=r.get("canonical_url") or r.get("destination_url"),
            utm_template=r.get("utm_template"),
            ctx={
                "seedId": r.get("id"),
                "productKey": product_key,
                "variantId": r.get("attached_variant_id") or "∅",
            },
        )
        disclosure_text = r.get("disclosure_text") or DEFAULT_DISCLOSURE_TEXT
        items.append(
            {
                "id": r.get("id"),
                "market": r.get("market"),
                "tool": r.get("tool"),
                "utm_template": r.get("utm_template"),
                "partner_type": r.get("partner_type"),
                "disclosure_text": disclosure_text,
                "destination_url": r.get("destination_url"),
                "canonical_url": r.get("canonical_url"),
                "domain": r.get("domain"),
                "title": r.get("title"),
                "image_url": r.get("image_url"),
                "merchant_display_name": merchant_display_name,
                "price": _seed_primary_price(r, seed_data),
                "availability": r.get("availability"),
                "notes": r.get("notes"),
                "attached_variant_id": r.get("attached_variant_id") or "∅",
                "created_at": r.get("created_at").isoformat() if r.get("created_at") else None,
                "action": {"type": "redirect", "redirect_url": redirect_url, "disclosure_text": disclosure_text},
            }
        )
    return {"status": "success", "items": items}


class CreateAttachedExternalLinkRequest(BaseModel):
    destination_url: str = Field(..., min_length=1)
    market: Optional[str] = None
    tool: Optional[str] = None
    variant_id: Optional[str] = None
    notes: Optional[str] = None
    utm_template: Optional[str] = None
    partner_type: Optional[str] = None
    disclosure_text: Optional[str] = None


@router.post("/{product_key}/external-links")
async def create_attached_external_link(
    product_key: str,
    body: CreateAttachedExternalLinkRequest,
    request: Request,
    current_user: dict = Depends(get_current_employee),
):
    pk = (product_key or "").strip()
    if pk.count("|") != 2:
        raise HTTPException(status_code=400, detail="INVALID_PRODUCT_KEY")
    return await create_external_seed(
        CreateExternalSeedRequest(
            destination_url=body.destination_url,
            market=body.market,
            tool=body.tool,
            notes=body.notes,
            utm_template=body.utm_template,
            partner_type=body.partner_type,
            disclosure_text=body.disclosure_text,
            attach_product_key=pk,
            attach_variant_id=(body.variant_id or "∅"),
        ),
        request=request,
        current_user=current_user,
    )


class SetPrimaryOfferRequest(BaseModel):
    offer_id: str = Field(..., min_length=1)
    offer_type: str = Field(..., min_length=1)


def _build_merchant_offer(
    *,
    product_key: str,
    merchant_id: str,
    platform: str,
    platform_product_id: str,
    product_data: Dict[str, Any],
) -> Dict[str, Any]:
    summary = _extract_product_summary(product_data, platform_product_id)
    variants = summary.get("variants") or []
    return {
        "id": product_key,
        "source": "merchant_product",
        "product_key": product_key,
        "merchant_id": merchant_id,
        "platform": platform,
        "platform_product_id": platform_product_id,
        "product_id": summary.get("product_id") or platform_product_id,
        "title": summary.get("title"),
        "image_url": summary.get("image_url"),
        "price": {"amount": summary.get("price"), "currency": summary.get("currency")},
        "availability": summary.get("availability"),
        "variants_count": len(variants),
    }


async def _build_external_seed_offers(
    *,
    product_key: str,
    request: Request,
    variant_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    await _ensure_external_seeds_table()
    values: Dict[str, Any] = {"pk": product_key}
    where = "attached_product_key = :pk AND status = 'active'"
    if variant_id:
        values["vid"] = str(variant_id).strip()
        where += " AND attached_variant_id = :vid"

    rows = await database.fetch_all(
        f"""
        SELECT
          id, market, tool, destination_url, canonical_url, domain, title, image_url,
          price_amount, price_currency, availability,
          utm_template, partner_type, disclosure_text,
          seed_data,
          attached_variant_id, created_at, updated_at
        FROM external_product_seeds
        WHERE {where}
        ORDER BY created_at DESC
        """,
        values,
    )

    offers: List[Dict[str, Any]] = []
    for r in rows:
        r = dict(r)
        seed_data = _ensure_json_obj(r.get("seed_data"))
        redirect_url = await _make_redirect_url(
            request=request,
            market=r.get("market"),
            tool=r.get("tool"),
            destination_url=r.get("canonical_url") or r.get("destination_url"),
            utm_template=r.get("utm_template") or seed_data.get("utm_template"),
            ctx={
                "seedId": r.get("id"),
                "productKey": product_key,
                "variantId": r.get("attached_variant_id") or "∅",
            },
        )
        disclosure_text = r.get("disclosure_text") or seed_data.get("disclosure_text") or DEFAULT_DISCLOSURE_TEXT
        offers.append(
            {
                "id": r.get("id"),
                "source": "external_seed",
                "seed_id": r.get("id"),
                "market": r.get("market"),
                "tool": r.get("tool"),
                "utm_template": r.get("utm_template") or seed_data.get("utm_template"),
                "partner_type": r.get("partner_type") or seed_data.get("partner_type"),
                "disclosure_text": disclosure_text,
                "destination_url": r.get("destination_url"),
                "canonical_url": r.get("canonical_url"),
                "domain": r.get("domain"),
                "title": seed_data.get("title") or r.get("title"),
                "image_url": seed_data.get("image_url") or r.get("image_url"),
                "merchant_display_name": _seed_merchant_display_name(seed_data, r.get("domain")),
                "price": _seed_primary_price(r, seed_data),
                "availability": seed_data.get("availability") or r.get("availability"),
                "variants_count": len(_seed_variants(seed_data)),
                "attached_variant_id": r.get("attached_variant_id") or "∅",
                "action": {"type": "redirect", "redirect_url": redirect_url, "disclosure_text": disclosure_text},
            }
        )
    return offers


async def _build_external_seed_offers_for_external_product_id(
    *,
    external_product_id: str,
    product_key_for_ctx: str,
    request: Request,
    variant_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    rows = await _fetch_external_seed_rows_by_external_product_id(
        external_product_id=external_product_id,
        limit=200,
        allow_attached_product_key=product_key_for_ctx,
    )
    offers: List[Dict[str, Any]] = []
    for r in rows:
        r = dict(r)
        vid = str(r.get("attached_variant_id") or "∅").strip() or "∅"
        if variant_id is not None and str(variant_id).strip() != vid:
            continue

        seed_data = _ensure_json_obj(r.get("seed_data"))
        redirect_url = await _make_redirect_url(
            request=request,
            market=r.get("market"),
            tool=r.get("tool"),
            destination_url=r.get("canonical_url") or r.get("destination_url"),
            utm_template=r.get("utm_template") or seed_data.get("utm_template"),
            ctx={
                "seedId": r.get("id"),
                "productKey": product_key_for_ctx,
                "variantId": vid,
            },
        )
        disclosure_text = r.get("disclosure_text") or seed_data.get("disclosure_text") or DEFAULT_DISCLOSURE_TEXT
        offers.append(
            {
                "id": r.get("id"),
                "source": "external_seed",
                "seed_id": r.get("id"),
                "market": r.get("market"),
                "tool": r.get("tool"),
                "utm_template": r.get("utm_template") or seed_data.get("utm_template"),
                "partner_type": r.get("partner_type") or seed_data.get("partner_type"),
                "disclosure_text": disclosure_text,
                "destination_url": r.get("destination_url"),
                "canonical_url": r.get("canonical_url"),
                "domain": r.get("domain"),
                "title": seed_data.get("title") or r.get("title"),
                "image_url": seed_data.get("image_url") or r.get("image_url"),
                "merchant_display_name": _seed_merchant_display_name(seed_data, r.get("domain")),
                "price": _seed_primary_price(r, seed_data),
                "availability": seed_data.get("availability") or r.get("availability"),
                "variants_count": len(_seed_variants(seed_data)),
                "attached_variant_id": vid,
                "notes": r.get("notes"),
                "created_at": _to_iso(r.get("created_at")),
                "updated_at": _to_iso(r.get("updated_at")),
                "action": {"type": "redirect", "redirect_url": redirect_url, "disclosure_text": disclosure_text},
            }
        )
    return offers


async def _get_primary_offer(product_key: str) -> Optional[Dict[str, Any]]:
    await _ensure_primary_offers_table()
    row = await database.fetch_one(
        "SELECT product_key, offer_id, offer_type, created_by_employee_id, updated_at FROM employee_product_primary_offers WHERE product_key = :pk",
        {"pk": product_key},
    )
    return dict(row) if row else None


async def _set_primary_offer(
    *,
    product_key: str,
    offer_id: str,
    offer_type: str,
    employee_id: Optional[str],
) -> None:
    await _ensure_primary_offers_table()
    await database.execute(
        """
        INSERT INTO employee_product_primary_offers (product_key, offer_id, offer_type, created_by_employee_id)
        VALUES (:pk, :offer_id, :offer_type, :employee_id)
        ON CONFLICT (product_key)
        DO UPDATE SET
          offer_id = EXCLUDED.offer_id,
          offer_type = EXCLUDED.offer_type,
          created_by_employee_id = EXCLUDED.created_by_employee_id,
          updated_at = NOW()
        """,
        {
            "pk": product_key,
            "offer_id": offer_id,
            "offer_type": offer_type,
            "employee_id": employee_id,
        },
    )


async def _compute_product_metrics(
    *,
    merchant_id: str,
    platform: str,
    platform_product_id: str,
    product_data: Dict[str, Any],
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    last_7d = now - timedelta(days=7)
    last_30d = now - timedelta(days=30)
    currency = product_data.get("currency") or "USD"
    product_id = product_data.get("product_id") or product_data.get("id") or platform_product_id
    debug_errors: List[str] = []

    sales_7d = 0
    sales_30d = 0
    gmv_7d = 0.0
    gmv_30d = 0.0

    try:
        currency_clause = " AND o.currency = :currency" if currency else ""
        row = await database.fetch_one(
            f"""
            SELECT
              COUNT(DISTINCT o.order_id) FILTER (
                WHERE o.created_at >= :last_7d AND o.payment_status = 'paid'
              )::bigint AS sales_7d,
              COUNT(DISTINCT o.order_id) FILTER (
                WHERE o.created_at >= :last_30d AND o.payment_status = 'paid'
              )::bigint AS sales_30d,
              COALESCE(SUM(CASE WHEN o.created_at >= :last_7d AND o.payment_status = 'paid' THEN o.total ELSE 0 END), 0) AS gmv_7d,
              COALESCE(SUM(CASE WHEN o.created_at >= :last_30d AND o.payment_status = 'paid' THEN o.total ELSE 0 END), 0) AS gmv_30d
            FROM orders o
            WHERE (o.is_deleted IS NULL OR o.is_deleted = FALSE)
              AND o.merchant_id = :merchant_id
              {currency_clause}
              AND EXISTS (
                SELECT 1
                FROM jsonb_array_elements(COALESCE(o.items::jsonb, '[]'::jsonb)) item
                WHERE (item->>'product_id' = :product_id OR item->>'product_id' = :platform_product_id)
              )
            """,
            {
                "last_7d": last_7d,
                "last_30d": last_30d,
                "merchant_id": merchant_id,
                "product_id": product_id,
                "platform_product_id": platform_product_id,
                "currency": currency,
            },
        )
        if row:
            sales_7d = int(row.get("sales_7d") or 0)
            sales_30d = int(row.get("sales_30d") or 0)
            gmv_7d = float(row.get("gmv_7d") or 0)
            gmv_30d = float(row.get("gmv_30d") or 0)
    except Exception as exc:
        debug_errors.append(f"orders metrics failed: {str(exc)[:200]}")
        # Fallback: fetch recent orders and filter in Python (MVP-safe, slower).
        try:
            currency_clause = " AND currency = :currency" if currency else ""
            rows = await database.fetch_all(
                f"""
                SELECT order_id, created_at, payment_status, total, currency, items
                FROM orders
                WHERE (is_deleted IS NULL OR is_deleted = FALSE)
                  AND merchant_id = :merchant_id
                  AND created_at >= :last_30d
                  {currency_clause}
                """,
                {
                    "merchant_id": merchant_id,
                    "last_30d": last_30d,
                    "currency": currency,
                },
            )
            sales_7d = 0
            sales_30d = 0
            gmv_7d = 0.0
            gmv_30d = 0.0
            for row in rows or []:
                r = dict(row)
                if (r.get("payment_status") or "").lower() != "paid":
                    continue
                items = r.get("items")
                if isinstance(items, str):
                    try:
                        items = json.loads(items)
                    except Exception:
                        items = None
                if isinstance(items, dict):
                    items = [items]
                if not isinstance(items, list):
                    continue
                match = False
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    pid = item.get("product_id")
                    if pid == product_id or pid == platform_product_id:
                        match = True
                        break
                if not match:
                    continue
                created_at = r.get("created_at")
                total = r.get("total") or 0
                try:
                    total_f = float(total)
                except Exception:
                    total_f = 0.0
                if created_at and created_at >= last_7d:
                    sales_7d += 1
                    gmv_7d += total_f
                sales_30d += 1
                gmv_30d += total_f
            debug_errors = []
        except Exception as fallback_exc:
            debug_errors.append(f"orders metrics fallback failed: {str(fallback_exc)[:200]}")

    merchants_selling = 1
    try:
        pg_row = await database.fetch_one(
            """
            SELECT product_group_id
            FROM product_group_members
            WHERE merchant_id = :merchant_id
              AND platform = :platform
              AND platform_product_id = :platform_product_id
            LIMIT 1
            """,
            {
                "merchant_id": merchant_id,
                "platform": platform,
                "platform_product_id": platform_product_id,
            },
        )
        product_group_id = str(pg_row["product_group_id"]) if pg_row and pg_row["product_group_id"] else None
        if product_group_id:
            count_row = await database.fetch_one(
                """
                SELECT COUNT(DISTINCT merchant_id)::int AS sellers_count
                FROM product_group_members
                WHERE product_group_id = :product_group_id
                """,
                {"product_group_id": product_group_id},
            )
            if count_row:
                merchants_selling = max(1, int(count_row["sellers_count"] or 0))
    except Exception:
        merchants_selling = 1

    metrics = {
        "sales_7d": sales_7d,
        "sales_30d": sales_30d,
        "gmv_7d": {"currency": currency, "amount": gmv_7d},
        "gmv_30d": {"currency": currency, "amount": gmv_30d},
        "merchants_selling": merchants_selling,
    }
    if debug_errors:
        metrics["debug_errors"] = debug_errors
    return metrics


@router.get("/{product_key}/offers")
async def list_product_offers(
    product_key: str,
    request: Request,
    variant_id: Optional[str] = Query(default=None),
    current_user: dict = Depends(get_current_employee),
):
    parts = [p.strip() for p in (product_key or "").split("|")]
    if len(parts) != 3:
        raise HTTPException(status_code=400, detail="INVALID_PRODUCT_KEY")

    merchant_id, platform, platform_product_id = parts

    if _is_external_seed_product_key(merchant_id=merchant_id, platform=platform):
        offers = await _build_external_seed_offers_for_external_product_id(
            external_product_id=platform_product_id,
            product_key_for_ctx=product_key,
            request=request,
            variant_id=variant_id,
        )

        primary = await _get_primary_offer(product_key)
        if primary:
            primary_offer = {
                "offer_id": primary.get("offer_id"),
                "offer_type": primary.get("offer_type"),
                "updated_at": _to_iso(primary.get("updated_at")),
            }
        else:
            primary_offer = {
                "offer_id": offers[0].get("id") if offers else None,
                "offer_type": "external_seed" if offers else None,
            }

        return {
            "status": "success",
            "product_group_id": None,
            "items": offers,
            "primary": primary_offer,
        }
    row = await _fetch_latest_products_cache_row(
        merchant_id=merchant_id,
        platform=platform,
        platform_product_id=platform_product_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="PRODUCT_NOT_FOUND")

    product_data = _ensure_dict(row.get("product_data"))

    product_group_id: Optional[str] = None
    try:
        pg_row = await database.fetch_one(
            """
            SELECT product_group_id
            FROM product_group_members
            WHERE merchant_id = :merchant_id
              AND platform = :platform
              AND platform_product_id = :platform_product_id
            LIMIT 1
            """,
            {
                "merchant_id": merchant_id,
                "platform": platform,
                "platform_product_id": platform_product_id,
            },
        )
        product_group_id = str(pg_row["product_group_id"]) if pg_row and pg_row["product_group_id"] else None
    except Exception:
        product_group_id = None

    merchant_offers: List[Dict[str, Any]] = []
    if product_group_id:
        try:
            members = await database.fetch_all(
                """
                SELECT
                  pgm.merchant_id,
                  pgm.platform,
                  pgm.platform_product_id,
                  pgm.is_primary,
                  pc.product_data
                FROM product_group_members pgm
                JOIN products_cache pc
                  ON pc.merchant_id = pgm.merchant_id
                 AND pc.platform = pgm.platform
                 AND pc.platform_product_id = pgm.platform_product_id
                WHERE pgm.product_group_id = :product_group_id
                ORDER BY pgm.is_primary DESC, pgm.updated_at DESC
                """,
                {"product_group_id": product_group_id},
            )
            for m in members or []:
                m = dict(m)
                mk = f"{m.get('merchant_id')}|{m.get('platform')}|{m.get('platform_product_id')}"
                md = _ensure_dict(m.get("product_data"))
                merchant_offers.append(
                    _build_merchant_offer(
                        product_key=mk,
                        merchant_id=str(m.get("merchant_id")),
                        platform=str(m.get("platform")),
                        platform_product_id=str(m.get("platform_product_id")),
                        product_data=md,
                    )
                )
        except Exception:
            merchant_offers = []

    if not merchant_offers:
        merchant_offers = [
            _build_merchant_offer(
                product_key=product_key,
                merchant_id=merchant_id,
                platform=platform,
                platform_product_id=platform_product_id,
                product_data=product_data,
            )
        ]

    offers = merchant_offers
    external_offers = await _build_external_seed_offers(
        product_key=product_key, request=request, variant_id=variant_id
    )
    offers.extend(external_offers)

    primary = await _get_primary_offer(product_key)
    if primary:
        primary_offer = {
            "offer_id": primary.get("offer_id"),
            "offer_type": primary.get("offer_type"),
            "updated_at": _to_iso(primary.get("updated_at")),
        }
    else:
        primary_offer = {
            "offer_id": product_key if offers else None,
            "offer_type": "merchant_product" if offers else None,
        }

    return {
        "status": "success",
        "product_group_id": product_group_id,
        "items": offers,
        "primary": primary_offer,
    }


@router.post("/{product_key}/offers/primary")
async def set_primary_offer(
    product_key: str,
    body: SetPrimaryOfferRequest,
    current_user: dict = Depends(get_current_employee),
):
    offer_type_raw = (body.offer_type or "").strip()
    offer_id = (body.offer_id or "").strip()
    if offer_type_raw in {"merchant", "merchant_product"}:
        offer_type = "merchant_product"
    elif offer_type_raw in {"external", "external_seed"}:
        offer_type = "external_seed"
    else:
        raise HTTPException(status_code=400, detail="INVALID_OFFER_TYPE")

    if offer_type == "merchant_product":
        offer_id = product_key
    else:
        await _ensure_external_seeds_table()
        row = await database.fetch_one(
            "SELECT id FROM external_product_seeds WHERE id = :id AND status = 'active'",
            {"id": offer_id},
        )
        if not row:
            raise HTTPException(status_code=404, detail="OFFER_NOT_FOUND")

    employee_id = current_user.get("employee_id") or current_user.get("employeeId")
    await _set_primary_offer(
        product_key=product_key,
        offer_id=offer_id,
        offer_type=offer_type,
        employee_id=str(employee_id) if employee_id else None,
    )
    return {"status": "success"}


@router.get("/{product_key}/metrics")
async def get_product_metrics(
    product_key: str,
    current_user: dict = Depends(get_current_employee),
):
    parts = [p.strip() for p in (product_key or "").split("|")]
    if len(parts) != 3:
        raise HTTPException(status_code=400, detail="INVALID_PRODUCT_KEY")

    merchant_id, platform, platform_product_id = parts
    row = await _fetch_latest_products_cache_row(
        merchant_id=merchant_id,
        platform=platform,
        platform_product_id=platform_product_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="PRODUCT_NOT_FOUND")

    product_data = _ensure_dict(row.get("product_data"))
    metrics = await _compute_product_metrics(
        merchant_id=merchant_id,
        platform=platform,
        platform_product_id=platform_product_id,
        product_data=product_data,
    )
    status = "degraded" if metrics.get("debug_errors") else "success"
    return {"status": status, "metrics": metrics}


@router.get("/search")
async def search_products(
    q: Optional[str] = Query(default=None, description="Search by product_id/platform_product_id/title (best-effort)"),
    merchant_id: Optional[str] = Query(default=None),
    platform: Optional[str] = Query(default=None),
    limit: int = Query(default=30, ge=1, le=100),
    after_id: Optional[int] = Query(default=None, description="Cursor: return rows with id < after_id"),
    group_by: Optional[str] = Query(
        default=None,
        description="Optional grouping mode. Use `product_group` to group by product_group_members.product_group_id when available.",
    ),
    include_expired: bool = Query(default=False, description="Include expired cache rows"),
    current_user: dict = Depends(get_current_employee),
):
    """
    Employee-facing product search over products_cache.

    v0 behavior:
    - Sort: most recently inserted cache rows (id desc).
    - Pagination: keyset on products_cache.id (after_id).
    - Search: best-effort exact id match + title ILIKE when supported.
    """
    use_product_groups = _is_group_by_product_group(group_by)

    where = []
    where_group = []
    values: Dict[str, Any] = {"limit": limit}

    if merchant_id:
        where.append("merchant_id = :merchant_id")
        where_group.append("pc.merchant_id = :merchant_id")
        values["merchant_id"] = merchant_id
    if platform:
        where.append("platform = :platform")
        where_group.append("pc.platform = :platform")
        values["platform"] = platform
    if after_id is not None:
        where.append("id < :after_id")
        where_group.append("pc.id < :after_id")
        values["after_id"] = after_id
    if not include_expired:
        where.append("(expires_at IS NULL OR expires_at > NOW())")
        where_group.append("(pc.expires_at IS NULL OR pc.expires_at > NOW())")

    debug_errors: List[str] = []

    if use_product_groups:
        try:
            # Select one representative row per product_group_id (fallback to per-product key when missing).
            group_key = "COALESCE(pgm.product_group_id, pc.merchant_id || '|' || pc.platform || '|' || pc.platform_product_id)"
            base = f"""
            SELECT *
            FROM (
              SELECT DISTINCT ON ({group_key})
                pc.id,
                pc.merchant_id,
                pc.platform,
                pc.platform_product_id,
                pc.product_data,
                pc.cached_at,
                pc.expires_at,
                pgm.product_group_id AS product_group_id
              FROM products_cache pc
              LEFT JOIN product_group_members pgm
                ON pgm.merchant_id = pc.merchant_id
               AND pgm.platform = pc.platform
               AND pgm.platform_product_id = pc.platform_product_id
            """
            clause = f" WHERE {' AND '.join(where_group)}" if where_group else ""
            # Order within group: prefer primary member, then newest cache row.
            order_limit = f" ORDER BY {group_key}, pgm.is_primary DESC NULLS LAST, pc.id DESC"

            rows: List[Dict[str, Any]] = []
            if q:
                q = q.strip()
            if q:
                # Best-effort: attempt title ILIKE + JSON product_id match (Postgres).
                try:
                    q_clause = (
                        " (pc.platform_product_id = :q"
                        " OR pc.product_data->>'product_id' = :q"
                        " OR pc.product_data->>'id' = :q"
                        " OR pc.product_data->>'title' ILIKE :q_like"
                        " OR pc.product_data->>'name' ILIKE :q_like"
                        " OR EXISTS ("
                        "   SELECT 1"
                        "   FROM jsonb_array_elements("
                        "     CASE"
                        "       WHEN jsonb_typeof(pc.product_data::jsonb->'variants') = 'array' THEN pc.product_data::jsonb->'variants'"
                        "       ELSE '[]'::jsonb"
                        "     END"
                        "   ) AS v"
                        "   WHERE (v->>'variant_id' = :q OR v->>'id' = :q OR v->>'sku' = :q)"
                        " ))"
                    )
                    values["q"] = q
                    values["q_like"] = f"%{q}%"
                    rows = await database.fetch_all(
                        f"{base}{clause}{' AND ' if clause else ' WHERE '}{q_clause}{order_limit}"
                        f"\n            ) t\n            ORDER BY t.id DESC LIMIT :limit",
                        values,
                    )
                except Exception:
                    q_clause = (
                        " (pc.platform_product_id = :q"
                        " OR pc.product_data->>'product_id' = :q"
                        " OR pc.product_data->>'id' = :q)"
                    )
                    values["q"] = q
                    rows = await database.fetch_all(
                        f"{base}{clause}{' AND ' if clause else ' WHERE '}{q_clause}{order_limit}"
                        f"\n            ) t\n            ORDER BY t.id DESC LIMIT :limit",
                        values,
                    )
            else:
                rows = await database.fetch_all(
                    f"{base}{clause}{order_limit}\n            ) t\n            ORDER BY t.id DESC LIMIT :limit",
                    values,
                )

            seller_counts: Dict[str, int] = {}
            group_ids = sorted({str(dict(r).get("product_group_id")) for r in rows if dict(r).get("product_group_id")})
            if group_ids:
                try:
                    counts = await database.fetch_all(
                        """
                        SELECT product_group_id, COUNT(DISTINCT merchant_id)::int AS sellers_count
                        FROM product_group_members
                        WHERE product_group_id = ANY(:group_ids)
                        GROUP BY product_group_id
                        """,
                        {"group_ids": group_ids},
                    )
                    for cr in counts or []:
                        seller_counts[str(cr["product_group_id"])] = int(cr["sellers_count"] or 0)
                except Exception as exc:
                    debug_errors.append(f"group_sellers_count_failed: {str(exc)[:200]}")

            cards: List[Dict[str, Any]] = []
            for r in rows:
                try:
                    rr = dict(r)
                    card = _as_product_card(rr)
                    pgid = rr.get("product_group_id")
                    card["product_group_id"] = pgid
                    if pgid:
                        card["sellers_count"] = max(1, int(seller_counts.get(str(pgid)) or 0))
                    else:
                        card["sellers_count"] = 1
                    cards.append(card)
                except Exception as exc:
                    debug_errors.append(f"card_parse_failed: {str(exc)}")

            next_after_id = int(rows[-1]["id"]) if rows else None
            return {
                "status": "degraded" if debug_errors else "success",
                "items": cards,
                "next": {"after_id": next_after_id},
                **({"debug_errors": debug_errors[:10]} if debug_errors else {}),
            }
        except Exception as exc:
            # Degrade gracefully when the grouping table is unavailable or query fails.
            debug_errors.append(f"group_by_failed: {str(exc)[:200]}")
            use_product_groups = False

    base = "SELECT id, merchant_id, platform, platform_product_id, product_data, cached_at, expires_at FROM products_cache"
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    order_limit = " ORDER BY id DESC LIMIT :limit"

    rows: List[Dict[str, Any]] = []
    if q:
        q = q.strip()

    if q:
        # Best-effort: attempt title ILIKE + JSON product_id match (Postgres).
        try:
            q_clause = (
                " (platform_product_id = :q"
                " OR product_data->>'product_id' = :q"
                " OR product_data->>'id' = :q"
                " OR product_data->>'title' ILIKE :q_like"
                " OR product_data->>'name' ILIKE :q_like"
                " OR EXISTS ("
                "   SELECT 1"
                "   FROM jsonb_array_elements("
                "     CASE"
                "       WHEN jsonb_typeof(product_data::jsonb->'variants') = 'array' THEN product_data::jsonb->'variants'"
                "       ELSE '[]'::jsonb"
                "     END"
                "   ) AS v"
                "   WHERE (v->>'variant_id' = :q OR v->>'id' = :q OR v->>'sku' = :q)"
                " ))"
            )
            values["q"] = q
            values["q_like"] = f"%{q}%"
            rows = await database.fetch_all(
                f"{base}{clause}{' AND ' if clause else ' WHERE '}{q_clause}{order_limit}",
                values,
            )
        except Exception:
            # Fallback: exact matches only.
            q_clause = " (platform_product_id = :q OR product_data->>'product_id' = :q OR product_data->>'id' = :q)"
            values["q"] = q
            rows = await database.fetch_all(
                f"{base}{clause}{' AND ' if clause else ' WHERE '}{q_clause}{order_limit}",
                values,
            )
    else:
        rows = await database.fetch_all(f"{base}{clause}{order_limit}", values)

    cards: List[Dict[str, Any]] = []
    seen_keys: set[str] = set()
    for r in rows:
        try:
            card = _as_product_card(dict(r))
            key = card.get("product_key")
            if key and key in seen_keys:
                continue
            if key:
                seen_keys.add(key)
            cards.append(card)
        except Exception as exc:
            debug_errors.append(f"card_parse_failed: {str(exc)}")
    next_after_id = int(rows[-1]["id"]) if rows else None

    return {
        "status": "degraded" if debug_errors else "success",
        "items": cards,
        "next": {"after_id": next_after_id},
        **({"debug_errors": debug_errors[:10]} if debug_errors else {}),
    }


def _catalog_number(value: Any) -> Optional[float]:
    """Serialize Decimal/Numeric values without leaking driver-specific types."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@router.get("/canonical")
async def list_canonical_products(
    q: Optional[str] = Query(default=None, description="Search content key, sig, brand, or title"),
    merchant_id: Optional[str] = Query(default=None, description="Find content clusters containing this merchant"),
    limit: int = Query(default=50, ge=1, le=100),
    after_content_key: Optional[str] = Query(default=None, description="Keyset cursor returned by this endpoint"),
    current_user: dict = Depends(get_current_employee),
):
    """List the canonical employee product catalogue at the content-key grain.

    A content key is the identity/aggregation grain.  The selected canonical
    ``sig_*`` remains a public-PDP URL identifier, while merchant buyability is
    read exclusively from unsuppressed ``catalog_offers``.  External seeds are
    deliberately absent: they are an audit/legacy compatibility data source,
    not offers of record.
    """
    normalized_q = (q or "").strip()
    normalized_merchant_id = (merchant_id or "").strip()
    cursor = (after_content_key or "").strip()
    values: Dict[str, Any] = {
        "q": normalized_q or None,
        "q_like": f"%{normalized_q}%" if normalized_q else None,
        "merchant_id": normalized_merchant_id or None,
        "after_content_key": cursor or None,
        "limit": limit + 1,
    }

    rows = await database.fetch_all(
        """
        WITH matching_keys AS (
          SELECT DISTINCT cp.content_key
          FROM catalog_products cp
          WHERE cp.content_key IS NOT NULL
            AND cp.suppressed_at IS NULL
            -- Bind every optional string explicitly.  asyncpg cannot infer a
            -- type for a NULL-only named bind in the first page request.
            AND (CAST(:merchant_id AS text) IS NULL OR cp.merchant_id = CAST(:merchant_id AS text))
            AND (
              CAST(:q AS text) IS NULL
              OR cp.content_key = CAST(:q AS text)
              OR cp.pivota_signature_id = CAST(:q AS text)
              OR cp.title ILIKE CAST(:q_like AS text)
              OR cp.brand ILIKE CAST(:q_like AS text)
            )
            AND (CAST(:after_content_key AS text) IS NULL
                 OR cp.content_key > CAST(:after_content_key AS text))
        ),
        cluster_products AS (
          SELECT cp.*,
                 ROW_NUMBER() OVER (
                   PARTITION BY cp.content_key
                   ORDER BY
                     (cp.pivota_signature_id IS NOT NULL) DESC,
                     cp.content_changed_at DESC NULLS LAST,
                     cp.product_key ASC
                 ) AS representative_rank
          FROM catalog_products cp
          JOIN matching_keys mk ON mk.content_key = cp.content_key
          WHERE cp.suppressed_at IS NULL
        ),
        offer_stats AS (
          SELECT
            cp.content_key,
            COUNT(DISTINCT cp.product_key)::int AS product_count,
            COUNT(DISTINCT cp.merchant_id)::int AS merchant_count,
            COUNT(DISTINCT o.offer_id)::int AS offer_count,
            COUNT(DISTINCT o.merchant_id)::int AS offer_merchant_count,
            MIN(COALESCE(o.merchant_effective_price, o.estimated_best_price, o.list_price)) AS lowest_price,
            MIN(o.currency) FILTER (WHERE o.currency IS NOT NULL) AS currency
          FROM cluster_products cp
          LEFT JOIN catalog_offers o
            ON o.product_key = cp.product_key
           AND o.suppressed_at IS NULL
          GROUP BY cp.content_key
        )
        SELECT
          cp.content_key,
          cp.product_key AS representative_product_key,
          cp.title,
          cp.brand,
          cp.image_url,
          cp.pivota_signature_id AS representative_sig_id,
          cp.pivota_canonical_url,
          COALESCE(cce.canonical_sig_id, cp.pivota_signature_id) AS canonical_sig_id,
          COALESCE(ips.serving_eligible, FALSE) AS serving_eligible,
          ips.index_eligible,
          ips.blocker_code,
          os.product_count,
          os.merchant_count,
          os.offer_count,
          os.offer_merchant_count,
          os.lowest_price,
          os.currency
        FROM cluster_products cp
        JOIN offer_stats os ON os.content_key = cp.content_key
        LEFT JOIN index_pipeline_state ips ON ips.content_key = cp.content_key
        LEFT JOIN content_canonical_election cce ON cce.content_key = cp.content_key
        WHERE cp.representative_rank = 1
        ORDER BY cp.content_key ASC
        LIMIT :limit
        """,
        values,
    )

    has_more = len(rows or []) > limit
    page_rows = [dict(row) for row in (rows or [])[:limit]]
    items = [
        {
            **row,
            "serving_eligible": bool(row.get("serving_eligible")),
            "index_eligible": bool(row.get("index_eligible")),
            "lowest_price": _catalog_number(row.get("lowest_price")),
        }
        for row in page_rows
    ]
    return {
        "status": "success",
        "items": items,
        "next": {"after_content_key": items[-1]["content_key"] if has_more and items else None},
    }


@router.get("/canonical/{content_key}")
async def get_canonical_product(
    content_key: str,
    current_user: dict = Depends(get_current_employee),
):
    """Return one content-key cluster and its merchant offers of record."""
    ck = (content_key or "").strip()
    if not ck.startswith("ck_"):
        raise HTTPException(status_code=400, detail="INVALID_CONTENT_KEY")

    products = await database.fetch_all(
        """
        SELECT
          cp.product_key, cp.merchant_id, cp.platform, cp.source_product_id,
          cp.title, cp.brand, cp.image_url, cp.pivota_signature_id,
          cp.pivota_canonical_url, cp.pdp_scope, cp.sync_status,
          cp.content_changed_at,
          COALESCE(cm.merchant_name, cp.merchant_id) AS merchant_name
        FROM catalog_products cp
        LEFT JOIN catalog_merchants cm ON cm.merchant_id = cp.merchant_id
        WHERE cp.content_key = :content_key
          AND cp.suppressed_at IS NULL
        ORDER BY (cp.pivota_signature_id IS NOT NULL) DESC,
                 cp.content_changed_at DESC NULLS LAST,
                 cp.product_key ASC
        """,
        {"content_key": ck},
    )
    if not products:
        raise HTTPException(status_code=404, detail="CANONICAL_PRODUCT_NOT_FOUND")

    offers = await database.fetch_all(
        """
        SELECT
          o.offer_id, o.product_key, o.merchant_id,
          COALESCE(cm.merchant_name, o.merchant_id) AS merchant_name,
          o.market, o.channel, o.offer_mode, o.offer_type, o.is_first_party,
          o.availability, o.currency, o.list_price,
          o.merchant_effective_price, o.estimated_best_price,
          o.price_confidence, o.source_domain, o.updated_at
        FROM catalog_offers o
        JOIN catalog_products cp ON cp.product_key = o.product_key
        LEFT JOIN catalog_merchants cm ON cm.merchant_id = o.merchant_id
        WHERE cp.content_key = :content_key
          AND cp.suppressed_at IS NULL
          AND o.suppressed_at IS NULL
        ORDER BY
          COALESCE(o.merchant_effective_price, o.estimated_best_price, o.list_price) ASC NULLS LAST,
          o.merchant_id ASC,
          o.offer_id ASC
        """,
        {"content_key": ck},
    )
    state = await database.fetch_one(
        """
        SELECT serving_eligible, index_eligible, blocker_code, blocker_detail
        FROM index_pipeline_state
        WHERE content_key = :content_key
        """,
        {"content_key": ck},
    )
    election = await database.fetch_one(
        "SELECT canonical_sig_id FROM content_canonical_election WHERE content_key = :content_key",
        {"content_key": ck},
    )

    product_items = [dict(row) for row in products]
    offer_items = []
    for row in offers or []:
        item = dict(row)
        for field in ("list_price", "merchant_effective_price", "estimated_best_price", "price_confidence"):
            item[field] = _catalog_number(item.get(field))
        item["is_first_party"] = bool(item.get("is_first_party"))
        offer_items.append(item)
    representative = product_items[0]
    canonical_sig_id = (dict(election).get("canonical_sig_id") if election else None) or representative.get("pivota_signature_id")

    return {
        "status": "success",
        "content_key": ck,
        "canonical_sig_id": canonical_sig_id,
        "canonical_url": representative.get("pivota_canonical_url"),
        "serving": dict(state) if state else {"serving_eligible": False, "index_eligible": False},
        "representative": representative,
        "products": product_items,
        "offers": offer_items,
    }


@router.get("/{product_key}")
async def get_product_by_key(
    product_key: str,
    current_user: dict = Depends(get_current_employee),
):
    """
    Product detail by product_key, where product_key = "{merchant_id}|{platform}|{platform_product_id}".
    """
    parts = [p.strip() for p in (product_key or "").split("|")]
    if len(parts) != 3:
        raise HTTPException(status_code=400, detail="INVALID_PRODUCT_KEY")

    merchant_id, platform, platform_product_id = parts

    # External-only canonical product, synthesized from external_product_seeds.
    if _is_external_seed_product_key(merchant_id=merchant_id, platform=platform):
        view = await _build_external_seed_product_view(
            external_product_id=platform_product_id,
            allow_attached_product_key=product_key,
        )
        product_data = _ensure_dict(view.get("product_data"))
        cached_at = view.get("cached_at")
        raw_payload = _ensure_dict(view.get("raw"))

        try:
            sp = StandardProduct.model_validate(product_data)
            normalized = sp.model_dump()
        except Exception:
            normalized = None

        metrics = {
            "sales_7d": None,
            "sales_30d": None,
            "gmv_7d": None,
            "gmv_30d": None,
            "merchants_selling": 0,
            "debug": {"product_type": "external_seed"},
        }

        enrichment_row: Optional[Dict[str, Any]] = None
        enrichment: Dict[str, Any] = {}
        enrichment_meta: Dict[str, Any] = {"status": "missing"}
        try:
            enrichment_row = await get_enrichment(
                merchant_id=merchant_id,
                platform=platform,
                platform_product_id=platform_product_id,
                geo_code="default",
            )
            if enrichment_row:
                enrichment = {
                    "title_override": enrichment_row.get("title_override"),
                    "summary_short": enrichment_row.get("summary_short"),
                    "description_markdown": enrichment_row.get("description_markdown"),
                    "bullet_points": enrichment_row.get("bullet_points"),
                    "usage_scenarios": enrichment_row.get("usage_scenarios"),
                    "audience_tags": enrichment_row.get("audience_tags"),
                    "topic_tags": enrichment_row.get("topic_tags"),
                    "regulatory_disclaimer_local": enrichment_row.get("regulatory_disclaimer_local"),
                    "extra_images": enrichment_row.get("extra_images"),
                    "llm_readability_score": enrichment_row.get("llm_readability_score"),
                    "llm_safety_flags": enrichment_row.get("llm_safety_flags"),
                }
                enrichment_meta = {
                    "status": "ready",
                    "geo_code": enrichment_row.get("geo_code") or "default",
                    "updated_at": enrichment_row.get("updated_at").isoformat() if enrichment_row.get("updated_at") else None,
                    "updated_by_employee_id": enrichment_row.get("updated_by_employee_id"),
                    "updated_by_email": enrichment_row.get("updated_by_email"),
                }
        except Exception as exc:
            enrichment = {}
            enrichment_meta = {"status": "degraded", "error": str(exc)[:200]}

        return {
            "status": "success",
            "product_key": product_key,
            "merchant_id": merchant_id,
            "platform": platform,
            "platform_product_id": platform_product_id,
            "cached_at": _to_iso(cached_at),
            "expires_at": None,
            "product": normalized,
            "raw": raw_payload,
            "metrics": metrics,
            "enrichment": enrichment,
            "enrichment_meta": enrichment_meta,
        }

    row = await _fetch_latest_products_cache_row(
        merchant_id=merchant_id,
        platform=platform,
        platform_product_id=platform_product_id,
        include_expired=True,
    )
    if not row:
        raise HTTPException(status_code=404, detail="PRODUCT_NOT_FOUND")

    product_data = _ensure_dict(row.get("product_data"))

    # Parse best-effort StandardProduct for normalized fields, but return the raw JSON as well.
    try:
        sp = StandardProduct.model_validate(product_data)
        normalized = sp.model_dump()
    except Exception:
        normalized = None

    metrics = await _compute_product_metrics(
        merchant_id=merchant_id,
        platform=platform,
        platform_product_id=platform_product_id,
        product_data=product_data,
    )

    enrichment_row: Optional[Dict[str, Any]] = None
    enrichment: Dict[str, Any] = {}
    enrichment_meta: Dict[str, Any] = {"status": "missing"}
    try:
        enrichment_row = await get_enrichment(
            merchant_id=merchant_id,
            platform=platform,
            platform_product_id=platform_product_id,
            geo_code="default",
        )
        if enrichment_row:
            enrichment = {
                "title_override": enrichment_row.get("title_override"),
                "summary_short": enrichment_row.get("summary_short"),
                "description_markdown": enrichment_row.get("description_markdown"),
                "bullet_points": enrichment_row.get("bullet_points"),
                "usage_scenarios": enrichment_row.get("usage_scenarios"),
                "audience_tags": enrichment_row.get("audience_tags"),
                "topic_tags": enrichment_row.get("topic_tags"),
                "regulatory_disclaimer_local": enrichment_row.get("regulatory_disclaimer_local"),
                "extra_images": enrichment_row.get("extra_images"),
                "llm_readability_score": enrichment_row.get("llm_readability_score"),
                "llm_safety_flags": enrichment_row.get("llm_safety_flags"),
            }
            enrichment_meta = {
                "status": "ready",
                "geo_code": enrichment_row.get("geo_code") or "default",
                "updated_at": enrichment_row.get("updated_at").isoformat() if enrichment_row.get("updated_at") else None,
                "updated_by_employee_id": enrichment_row.get("updated_by_employee_id"),
                "updated_by_email": enrichment_row.get("updated_by_email"),
            }
    except Exception as exc:
        enrichment = {}
        enrichment_meta = {"status": "degraded", "error": str(exc)[:200]}

    return {
        "status": "success",
        "product_key": product_key,
        "merchant_id": merchant_id,
        "platform": platform,
        "platform_product_id": platform_product_id,
        "cached_at": _to_iso(row.get("cached_at")),
        "expires_at": _to_iso(row.get("expires_at")),
        "product": normalized,
        "raw": product_data,
        "metrics": metrics,
        "enrichment": enrichment,
        "enrichment_meta": enrichment_meta,
    }

@router.get("/{merchant_id}/{platform}/{platform_product_id}")
async def get_product_by_triplet(
    merchant_id: str,
    platform: str,
    platform_product_id: str,
    current_user: dict = Depends(get_current_employee),
):
    """
    Product detail by explicit triplet path params.

    This is a convenience wrapper for the employee portal to avoid embedding the composite
    `product_key` (merchant|platform|platform_product_id) into a single URL segment.
    """
    product_key = f"{merchant_id}|{platform}|{platform_product_id}"
    return await get_product_by_key(product_key=product_key, current_user=current_user)


class UpdateProductEnrichmentRequest(BaseModel):
    summary_short: Optional[str] = None
    description_markdown: Optional[str] = None


@router.patch("/{product_key}/enrichment")
async def update_product_enrichment(
    product_key: str,
    body: UpdateProductEnrichmentRequest,
    current_user: dict = Depends(get_current_employee),
):
    parts = [p.strip() for p in (product_key or "").split("|")]
    if len(parts) != 3:
        raise HTTPException(status_code=400, detail="INVALID_PRODUCT_KEY")

    merchant_id, platform, platform_product_id = parts

    patch: Dict[str, Any] = {}
    if body.summary_short is not None:
        patch["summary_short"] = (body.summary_short or "").strip() or None
    if body.description_markdown is not None:
        patch["description_markdown"] = (body.description_markdown or "").strip() or None

    if not patch:
        raise HTTPException(status_code=400, detail="NO_FIELDS_TO_UPDATE")

    employee_id = (
        current_user.get("employee_id")
        or current_user.get("employeeId")
        or current_user.get("user_id")
        or current_user.get("sub")
    )
    email = current_user.get("email")
    patch["updated_by_employee_id"] = str(employee_id) if employee_id else None
    patch["updated_by_email"] = str(email) if email else None

    await upsert_enrichment(
        merchant_id=merchant_id,
        platform=platform,
        platform_product_id=platform_product_id,
        geo_code="default",
        data=patch,
    )

    # B① publish bridge: propagate the employee-curated enrichment to the served
    # agent_pdp_view (flag-gated, best-effort — mirrors the pipeline path).
    from services.agent_pdp_view_assembler import (
        refresh_agent_pdp_view_for_enrichment_write,
    )

    await refresh_agent_pdp_view_for_enrichment_write(
        merchant_id, platform, platform_product_id
    )

    row = await get_enrichment(
        merchant_id=merchant_id,
        platform=platform,
        platform_product_id=platform_product_id,
        geo_code="default",
    )
    if not row:
        return {"status": "degraded", "enrichment": None}

    return {
        "status": "success",
        "enrichment": {
            "title_override": row.get("title_override"),
            "summary_short": row.get("summary_short"),
            "description_markdown": row.get("description_markdown"),
            "bullet_points": row.get("bullet_points"),
            "usage_scenarios": row.get("usage_scenarios"),
            "audience_tags": row.get("audience_tags"),
            "topic_tags": row.get("topic_tags"),
            "regulatory_disclaimer_local": row.get("regulatory_disclaimer_local"),
            "extra_images": row.get("extra_images"),
            "llm_readability_score": row.get("llm_readability_score"),
            "llm_safety_flags": row.get("llm_safety_flags"),
        },
        "enrichment_meta": {
            "status": "ready",
            "geo_code": row.get("geo_code") or "default",
            "updated_at": row.get("updated_at").isoformat() if row.get("updated_at") else None,
            "updated_by_employee_id": row.get("updated_by_employee_id"),
            "updated_by_email": row.get("updated_by_email"),
        },
    }


class CreateManualReviewRequest(BaseModel):
    rating: int = Field(..., ge=1, le=5)
    title: Optional[str] = Field(default=None, max_length=200)
    body: Optional[str] = Field(default=None, max_length=5000)
    variant_id: Optional[str] = None
    status: Optional[str] = None


@router.post("/{product_key}/reviews")
async def create_manual_review(
    product_key: str,
    body: CreateManualReviewRequest,
    actor: Dict[str, Any] = Depends(require_employee_permissions(["reviews.create.manual"])),
):
    parts = [p.strip() for p in (product_key or "").split("|")]
    if len(parts) != 3:
        raise HTTPException(status_code=400, detail="INVALID_PRODUCT_KEY")

    merchant_id, platform, platform_product_id = parts
    status = (body.status or "under_review").strip().lower()
    if status not in {"active", "folded", "removed", "under_review"}:
        raise HTTPException(status_code=400, detail="INVALID_STATUS")

    variant_id = (body.variant_id or "").strip() or None
    pk = build_product_key(
        merchant_id=merchant_id,
        platform=platform,
        platform_product_id=platform_product_id,
    )
    sk = build_sku_key(
        merchant_id=merchant_id,
        platform=platform,
        platform_product_id=platform_product_id,
        variant_id=variant_id,
    )

    now = datetime.now(timezone.utc)
    ext_review_id = f"manual:{uuid.uuid4().hex}"

    review_id = await database.execute(
        product_reviews.insert().values(
            product_key=pk,
            sku_key=sk,
            merchant_id=merchant_id,
            platform=platform,
            platform_product_id=platform_product_id,
            variant_id=variant_id,
            group_id=None,
            author_user_id=None,
            source_type="manual",
            source_system="employee",
            external_review_id=ext_review_id,
            dedupe_key=ext_review_id,
            verification="unverified",
            rating=int(body.rating),
            title=(body.title or "").strip() or None,
            body=(body.body or "").strip() or None,
            media_count=0,
            risk_flags=None,
            status=status,
            created_at=now,
            updated_at=now,
        )
    )

    return {
        "status": "success",
        "review_id": int(review_id) if review_id is not None else None,
    }


@router.get("/{product_key}/reviews")
async def list_product_reviews(
    product_key: str,
    variant_id: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: dict = Depends(get_current_employee),
):
    """
    List reviews for a product_key (merchant/platform/platform_product_id).

    Notes:
    - Uses Reviews Center table `product_reviews` when present.
    - If the table is missing in an environment, returns an empty list (degraded) instead of 500.
    """
    parts = [p.strip() for p in (product_key or "").split("|")]
    if len(parts) != 3:
        raise HTTPException(status_code=400, detail="INVALID_PRODUCT_KEY")

    merchant_id, platform, platform_product_id = parts
    values: Dict[str, Any] = {
        "merchant_id": merchant_id,
        "global_merchant_id": GLOBAL_IMPORT_MERCHANT_ID,
        "platform": platform,
        "platform_product_id": platform_product_id,
        "limit": limit,
    }
    variant_clause = ""
    if variant_id:
        values["variant_id"] = str(variant_id).strip()
        variant_clause = " AND (variant_id = :variant_id)"

    try:
        rows = await database.fetch_all(
            f"""
            SELECT
              id,
              merchant_id,
              platform,
              platform_product_id,
              variant_id,
              rating,
              title,
              body,
              body_redacted,
              status,
              risk_flags,
              media_count,
              created_at,
              updated_at
            FROM product_reviews
            WHERE merchant_id IN (:merchant_id, :global_merchant_id)
              AND platform = :platform
              AND platform_product_id = :platform_product_id
              {variant_clause}
            ORDER BY created_at DESC
            LIMIT :limit
            """,
            values,
        )
    except Exception as exc:
        # Degrade gracefully in deployments where Reviews Center tables are not present.
        msg = str(exc)
        if "product_reviews" in msg and ("does not exist" in msg or "UndefinedTable" in msg):
            return {
                "status": "degraded",
                "items": [],
                "debug_errors": ["product_reviews table missing"],
            }
        return {
            "status": "degraded",
            "items": [],
            "debug_errors": [f"reviews query failed: {msg[:200]}"],
        }

    items = []
    status_counts: Dict[str, int] = {}
    for r in rows:
        r = dict(r)
        st = (r.get("status") or "unknown").strip().lower()
        status_counts[st] = status_counts.get(st, 0) + 1
        items.append(
            {
                "id": int(r["id"]),
                "merchant_id": r.get("merchant_id"),
                "platform": r.get("platform"),
                "platform_product_id": r.get("platform_product_id"),
                "variant_id": r.get("variant_id"),
                "rating": r.get("rating"),
                "title": r.get("title"),
                "body": r.get("body_redacted") or r.get("body"),
                "status": r.get("status"),
                "media_count": r.get("media_count") or 0,
                "risk_flags": r.get("risk_flags"),
                "created_at": r.get("created_at").isoformat() if r.get("created_at") else None,
                "updated_at": r.get("updated_at").isoformat() if r.get("updated_at") else None,
            }
        )

    return {
        "status": "success",
        "items": items,
        "counts": {
            "total": len(items),
            "by_status": status_counts,
        },
    }
