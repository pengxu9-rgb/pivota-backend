"""Audit -> commerce-index SKU intake (the AUTOMATIC index-population path).

When a merchant runs an audit on a product URL (the storefront-agnostic URL
audit), the fetched product info should generate/update a canonical SKU in the
commerce index as an OBSERVED, unclaimed seed. The brand can later CLAIM +
verify + attest (the manual / lab-evidence path) to upgrade it to
brand-attested.

This module maps a fetched audit-product dict (from
services.bd_cold_start_service.fetch_curated_audit_product:
``{title, raw_title, pdp_url, vendor, product_type, attributes_raw}``) into the
canonical catalog_products shape, keyed by content_key. The mapping here is
PURE + unit-tested; the DB upsert that consumes it (follow-up) must be
best-effort + flag-gated so it can never break a live audit.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as _pg_insert

from services.catalog_identity import make_content_key

logger = logging.getLogger(__name__)

# Synthetic platform for URL-audit-sourced SKUs (no real storefront sync) — keeps
# them identifiable + de-conflated from Shopify/marketplace-synced rows.
PLATFORM_URL_AUDIT = "url_audit"


def _host(url: Optional[str]) -> str:
    if not url:
        return ""
    netloc = (urlparse(url).netloc or "").lower()
    netloc = netloc.split("@")[-1].split(":")[0]
    return netloc[4:] if netloc.startswith("www.") else netloc


def stable_source_id(pdp_url: Optional[str]) -> str:
    """Stable source_product_id for a URL-sourced SKU: scheme/query/fragment
    stripped, lowercased host+path. Re-auditing the same URL updates the SAME
    row instead of duplicating it in the index."""
    if not pdp_url:
        return ""
    parsed = urlparse(pdp_url.strip())
    host = _host(pdp_url)
    path = (parsed.path or "").rstrip("/").lower()
    return f"{host}{path}" if host else pdp_url.strip().lower()


def _strip_html(html: Optional[str]) -> Optional[str]:
    """Crude tag-strip for a PDP body_html → plain description. Best-effort."""
    if not html:
        return None
    import re

    text = re.sub(r"<[^>]+>", " ", str(html))
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _audit_description(attrs: Dict[str, Any]) -> Optional[str]:
    """Prefer a clean structured description; fall back to stripped body_html.
    Capped so a giant body_html can't bloat the row."""
    desc = (str(attrs.get("description") or "").strip() or None) or _strip_html(
        attrs.get("body_html")
    )
    return desc[:5000] if desc else None


def _audit_image_url(attrs: Dict[str, Any]) -> Optional[str]:
    """First product image from the audit's structured-data read (dict or list)."""
    images = attrs.get("images")
    if isinstance(images, dict):
        return str(images.get("first_url") or "").strip() or None
    if isinstance(images, list) and images:
        first = images[0]
        if isinstance(first, str):
            return first.strip() or None
        if isinstance(first, dict):
            return str(first.get("url") or first.get("src") or "").strip() or None
    return None


def audit_product_to_index_fields(
    merchant_id: str, audit_product: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Map a fetched audit-product dict -> canonical catalog_products fields.

    Returns None when there's not enough to mint an identity (no title, or no
    resolvable URL identity). content_key is brand+title here (GTIN enrichment
    is a follow-up); the deliberately non-unique key is de-conflated downstream
    by the identity gate, exactly as for other seed sources."""
    if not merchant_id or not isinstance(audit_product, dict):
        return None
    title = (audit_product.get("title") or "").strip()
    if not title:
        return None
    pdp_url = (audit_product.get("pdp_url") or "").strip() or None
    source_id = stable_source_id(pdp_url)
    if not source_id:
        return None
    brand = (audit_product.get("vendor") or "").strip() or None
    # Canonical catalog product_key (prod::merchant::platform::source) — keeps
    # url_audit seeds parseable by any tool that splits product_key on the
    # `prod::` prefix / `::` separator, exactly like Shopify/marketplace rows.
    # (Lazy import keeps this pure mapping module import-light, like the db.*
    # imports in upsert_audited_sku_to_index below.)
    from services.catalog_sync_service import make_catalog_product_key

    return {
        "merchant_id": merchant_id,
        "platform": PLATFORM_URL_AUDIT,
        "source_product_id": source_id,
        "product_key": make_catalog_product_key(
            merchant_id, PLATFORM_URL_AUDIT, source_id
        ),
        "title": title,
        "brand": brand,
        "content_key": make_content_key(brand, title),
        "canonical_url": pdp_url,
        "source_domain": _host(pdp_url) or None,
        "product_type": (audit_product.get("product_type") or "").strip() or None,
        # OBSERVED content from the merchant-paid audit (the flywheel's exhaust):
        # enriches the seed so it's rich WHEN claimed. Adds no serving — the row
        # stays un-served (pdp_lifecycle_stage NULL).
        "description": _audit_description(audit_product.get("attributes_raw") or {}),
        "image_url": _audit_image_url(audit_product.get("attributes_raw") or {}),
        "raw_title": audit_product.get("raw_title"),
        "attributes_raw": audit_product.get("attributes_raw") or {},
    }


def resolve_seed_vendor(
    *,
    fetched_vendor: Optional[str],
    declared_brand: Optional[str],
    fallback_brand: Optional[str] = None,
) -> Optional[str]:
    """The brand to attribute to a URL-audit seed (and the audit's vendor-anchored
    query). Precedence:

      1. An EXPLICITLY-declared brand wins — a store-less brand pointing at a
         RETAILER PDP (Amazon / Olive Young) knows its own brand, whereas that
         page's JSON-LD `vendor` is often the retailer or marketplace seller, not
         the brand. Letting it win is what makes "index my product from where
         I'm listed" attribute to the right brand (and content_key).
      2. Else the fetched vendor (authoritative for the brand's own Shopify PDP).
      3. Else a resolved fallback brand (derived from domain / business name).

    None when nothing resolves."""
    declared = (declared_brand or "").strip()
    if declared:
        return declared
    fetched = (fetched_vendor or "").strip()
    if fetched:
        return fetched
    fallback = (fallback_brand or "").strip()
    return fallback or None


def audit_intake_enabled() -> bool:
    """Flag: auto-seed the commerce index from audits. Default OFF — ships dark,
    enabled per-env after canary. (The upsert is best-effort regardless, so it
    can never break an audit; the flag governs whether we attempt it at all.)"""
    return os.getenv("ENABLE_AUDIT_INDEX_INTAKE", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


# Only the columns we set; everything else (catalog_track, truth_tier,
# readiness_tier, pdp_scope='unverified', sync_status, created_at, …) takes its
# server_default — and pdp_lifecycle_stage stays NULL, so a seed is NOT recalled
# /served until it graduates or is claimed. description + image_url are OBSERVED
# content fields (the audit's exhaust) — they enrich the seed for when it's
# claimed and add NO serving; the lifecycle stage is still left NULL.
_CATALOG_INSERT_COLUMNS = (
    "product_key", "merchant_id", "platform", "source_product_id",
    "title", "brand", "content_key", "canonical_url", "source_domain",
    "product_type", "description", "image_url",
)


# --- Entity-resolution gate (CONSERVATIVE) -------------------------------------
# Dedup the audit seed against the existing index. Entity mis-merge is the index's
# single biggest risk (a no-GTIN content_key is brand+title, non-unique), so ONLY
# an EXACT deterministic match (canonical-URL / source-id) may AUTO-merge; a fuzzy
# (title+brand trigram) match routes to HUMAN REVIEW instead. No LLM tier yet.
# Flag-gated + dark by default + best-effort — must never block the audit seed.

_EXACT_MATCHERS = frozenset({"source_product_id_match", "canonical_url_match"})


def audit_er_gate_enabled() -> bool:
    """Flag: run the entity-resolution gate on audit seeds. Default OFF."""
    return os.getenv("ENABLE_AUDIT_ER_GATE", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def route_audit_match(match: Optional[Dict[str, Any]]) -> str:
    """Conservative routing of a matcher result:
      - exact deterministic match (URL / source-id) -> 'align' (auto-merge);
      - fuzzy match (title+brand trigram, LLM, anything else) -> 'review' (HITL);
      - no match -> 'none'.
    """
    if not match or not match.get("product_key"):
        return "none"
    return "align" if match.get("matcher") in _EXACT_MATCHERS else "review"


def _audit_seed_for_match(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Build the pdp_matcher seed shape from the audit index-fields."""
    return {
        "id": f"audit_{fields.get('product_key')}",
        "external_product_id": fields.get("source_product_id"),
        "title": fields.get("title"),
        "canonical_url": fields.get("canonical_url"),
        "destination_url": fields.get("canonical_url"),
        "domain": fields.get("source_domain"),
        "seed_data": {"brand": fields.get("brand")},
    }


async def _resolve_audit_identity(
    fields: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Deterministic-only cascade (NO LLM) of the audit seed against canonical
    catalog candidates. Reuses the pdp_matcher candidate fetch + matchers."""
    from services.pdp_matcher.deterministic import matches_for_seed
    from services.pdp_matcher.runner import _candidates_for_seed

    seed = _audit_seed_for_match(fields)
    candidates = await _candidates_for_seed(seed)
    if not candidates:
        return None
    return matches_for_seed(seed=seed, candidates=candidates)


async def _content_key_for_product_key(product_key: str) -> Optional[str]:
    """The content_key of an already-indexed product (the auto-merge target)."""
    if not product_key:
        return None
    from db.catalog import catalog_products
    from db.database import database

    row = await database.fetch_one(
        catalog_products.select().where(
            catalog_products.c.product_key == product_key
        )
    )
    return row["content_key"] if row else None


async def enqueue_audit_identity_review(
    fields: Dict[str, Any], match: Dict[str, Any]
) -> Optional[str]:
    """Best-effort: route a FUZZY audit-identity match to human review
    (pdp_review_tasks, module 'identity') instead of auto-merging. Never raises."""
    import uuid

    from db.database import database
    from db.pdp_governance import pdp_review_tasks

    task_id = f"pdptask_{uuid.uuid4().hex}"
    try:
        await database.execute(
            pdp_review_tasks.insert().values(
                id=task_id,
                pdp_id=str(fields.get("product_key") or "")[:96],
                module_key="identity",
                status="needs_review",
                priority="normal",
                checklist={
                    "source": "audit_intake",
                    "audit_product_key": fields.get("product_key"),
                    "audit_content_key": fields.get("content_key"),
                    "candidate_product_key": match.get("product_key"),
                    "matcher": match.get("matcher"),
                    "confidence": match.get("confidence"),
                    "evidence": match.get("evidence"),
                },
                policy_labels=["entity_resolution", "audit_intake"],
            )
        )
        return task_id
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning(
            "enqueue_audit_identity_review failed for %s: %s",
            fields.get("product_key"), str(exc)[:200],
        )
        return None


async def apply_audit_er_gate(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Conservative ER gate for an audit seed. Returns {content_key, action, ...}.
    Best-effort: ANY failure returns the seed's ORIGINAL content_key so the gate
    can never block the seed.
      - 'align'   : exact match -> content_key re-aligned to the matched entity.
      - 'review'  : fuzzy match -> HITL enqueued; content_key UNCHANGED.
      - 'none' / 'disabled' / 'error' : content_key UNCHANGED.
    """
    original_ck = fields.get("content_key")
    if not audit_er_gate_enabled():
        return {"content_key": original_ck, "action": "disabled"}
    try:
        match = await _resolve_audit_identity(fields)
    except Exception as exc:  # noqa: BLE001 — never block the seed
        logger.warning(
            "apply_audit_er_gate: resolve failed for %s: %s",
            fields.get("product_key"), str(exc)[:200],
        )
        return {"content_key": original_ck, "action": "error"}

    action = route_audit_match(match)
    if action == "align":
        try:
            matched_ck = await _content_key_for_product_key(match["product_key"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("apply_audit_er_gate: ck lookup failed: %s", str(exc)[:200])
            matched_ck = None
        if matched_ck:
            return {
                "content_key": matched_ck,
                "action": "align",
                "matched_product_key": match["product_key"],
                "matcher": match.get("matcher"),
            }
        return {"content_key": original_ck, "action": "none"}
    if action == "review":
        await enqueue_audit_identity_review(fields, match)
        return {"content_key": original_ck, "action": "review"}
    return {"content_key": original_ck, "action": "none"}


async def upsert_audited_sku_to_index(
    merchant_id: str, audit_product: Dict[str, Any]
) -> Optional[str]:
    """Best-effort: upsert one audited product into catalog_products (the
    canonical index entity) keyed on product_key, then refresh its agent_pdp_view
    row. An OBSERVED, unclaimed seed. Returns the content_key (or product_key) on
    success, None otherwise. NEVER raises — it must not break a live audit."""
    fields = audit_product_to_index_fields(merchant_id, audit_product)
    if not fields:
        return None
    # Entity-resolution gate (flag-gated): an EXACT match re-aligns the seed's
    # content_key to the existing canonical entity (dedup); a FUZZY match goes to
    # human review. Best-effort — content_key falls back to the original.
    try:
        gate = await apply_audit_er_gate(fields)
        fields["content_key"] = gate.get("content_key") or fields.get("content_key")
    except Exception as exc:  # noqa: BLE001 — the gate must never break the seed
        logger.warning(
            "upsert_audited_sku_to_index: ER gate failed for %s: %s",
            fields.get("product_key"), str(exc)[:200],
        )
    try:
        from db.catalog import catalog_products
        from db.database import database

        values = {k: fields.get(k) for k in _CATALOG_INSERT_COLUMNS}
        stmt = _pg_insert(catalog_products).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["product_key"],
            set_={
                "title": stmt.excluded.title,
                "brand": func.coalesce(stmt.excluded.brand, catalog_products.c.brand),
                "content_key": func.coalesce(
                    stmt.excluded.content_key, catalog_products.c.content_key
                ),
                "canonical_url": func.coalesce(
                    stmt.excluded.canonical_url, catalog_products.c.canonical_url
                ),
                "source_domain": func.coalesce(
                    stmt.excluded.source_domain, catalog_products.c.source_domain
                ),
                "product_type": func.coalesce(
                    stmt.excluded.product_type, catalog_products.c.product_type
                ),
                "description": func.coalesce(
                    stmt.excluded.description, catalog_products.c.description
                ),
                "image_url": func.coalesce(
                    stmt.excluded.image_url, catalog_products.c.image_url
                ),
                "updated_at": func.now(),
                "content_changed_at": func.now(),
            },
        )
        await database.execute(stmt)
    except Exception as exc:  # noqa: BLE001 — best-effort, never break the audit
        logger.warning(
            "upsert_audited_sku_to_index: catalog upsert failed for %s: %s",
            fields.get("product_key"), str(exc)[:200],
        )
        return None

    content_key = fields.get("content_key")
    if content_key:
        try:
            from services.agent_pdp_view_assembler import (
                refresh_agent_pdp_view_for_content_key,
            )

            await refresh_agent_pdp_view_for_content_key(
                content_key, refresh_source="url_audit_intake"
            )
        except Exception as exc:  # noqa: BLE001 — PDP refresh is best-effort
            logger.warning(
                "upsert_audited_sku_to_index: pdp refresh failed for %s: %s",
                content_key, str(exc)[:200],
            )
    return content_key or fields.get("product_key")
