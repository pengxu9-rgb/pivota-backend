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

import hashlib
import logging
import os
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as _pg_insert

from services.catalog_identity import make_content_key
from services.intake_identity import canonical_gtin

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
    """Stable, URL-SAFE source_product_id for a URL-sourced SKU: scheme/query/
    fragment/trailing-slash stripped + lowercased, then keyed as host + a short
    hash of the path. Re-auditing the same URL yields the SAME id (dedup →
    updates the same row).

    Slash-free ON PURPOSE: this id flows into a URL PATH SEGMENT — both the
    `/merchant/products/{platform}/{platform_product_id}/evidence` route and the
    per-SKU report's pipe product_key (`merchant|url_audit|<id>`). A raw host+path
    id contains '/', and the proxy/ASGI stack decodes %2F→/ before routing, so a
    slashed id breaks single-segment matching → 404 (confirmed live). Hashing the
    path (not `/`→`_`) keeps it collision-safe: source_product_id feeds the
    product_key row identity, and an identity mis-merge is the index's worst
    failure — a lossy substitution could merge two distinct products."""
    if not pdp_url:
        return ""
    parsed = urlparse(pdp_url.strip())
    host = _host(pdp_url)
    path = (parsed.path or "").rstrip("/").lower()
    if not host:
        return "url~" + hashlib.sha1(pdp_url.strip().lower().encode()).hexdigest()[:16]
    if not path:
        return host
    return f"{host}~{hashlib.sha1(path.encode()).hexdigest()[:12]}"


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


def _audit_gtin(audit_product: Dict[str, Any]) -> Optional[str]:
    """R3 (ADR-011): read a source barcode/GTIN from the fetched PDP's
    structured-data attributes, so it can plumb through as the identity
    match-attribute instead of being dropped. Best-effort."""
    attrs = audit_product.get("attributes_raw")
    if not isinstance(attrs, dict):
        return None
    for key in ("gtin", "gtin13", "gtin14", "barcode", "upc", "ean"):
        value = str(attrs.get(key) or "").strip()
        if value:
            return value
    return None


def audit_product_to_index_fields(
    merchant_id: str, audit_product: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Map a fetched audit-product dict -> canonical catalog_products fields.

    Returns None when there's not enough to mint an identity (no title, or no
    resolvable URL identity). content_key is the GTIN-less brand+title FAMILY
    key (ADR-011 SPU model); any source barcode rides the separate `gtin`
    match-attribute column, de-conflated downstream by the identity gate."""
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
    from services.catalog_sync_service import (
        make_catalog_product_key,
        make_pivota_canonical_fields,
    )

    # W5 P3 (ADR-007): mint the Pivota canonical PDP identity at seed time so the
    # seed can be served on the OFFER-FREE citation floor (index_eligible), not
    # the full transact gate. The sig is deterministic from the SAME identity
    # tuple (merchant, platform, source_id) that keys product_key, so re-auditing
    # the same URL yields the same sig (idempotent — the upsert preserves it, see
    # _CATALOG_INSERT_COLUMNS + the on-conflict COALESCE below).
    pivota_fields = make_pivota_canonical_fields(
        merchant_id, PLATFORM_URL_AUDIT, source_id
    )

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
        # ADR-011: GTIN as a match-attribute (canonicalized), never folded into
        # content_key. Populated even when the identity flag is off so the
        # match corpus builds ahead of rollout.
        "gtin": canonical_gtin(_audit_gtin(audit_product)),
        "canonical_url": pdp_url,
        "source_domain": _host(pdp_url) or None,
        "product_type": (audit_product.get("product_type") or "").strip() or None,
        # OBSERVED content from the merchant-paid audit (the flywheel's exhaust):
        # enriches the seed so it's rich WHEN claimed. Adds no serving — the row
        # stays un-served (pdp_lifecycle_stage NULL). The Pivota canonical sig/URL
        # DO get minted (below): the citation floor is offer-free, so a
        # rich-enough seed becomes citable even before it's claimed/transactable.
        "description": _audit_description(audit_product.get("attributes_raw") or {}),
        "image_url": _audit_image_url(audit_product.get("attributes_raw") or {}),
        "raw_title": audit_product.get("raw_title"),
        "attributes_raw": audit_product.get("attributes_raw") or {},
        # Pivota canonical PDP fields (sig_id + agent.pivota.cc URL + minted_at).
        "pivota_signature_id": pivota_fields["pivota_signature_id"],
        "pivota_canonical_url": pivota_fields["pivota_canonical_url"],
        "pivota_signature_minted_at": pivota_fields["pivota_signature_minted_at"],
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


# Only the columns we set; everything else (pdp_scope='unverified',
# sync_status='live', created_at, …) takes its server_default — and
# pdp_lifecycle_stage stays NULL, so a seed is NOT recalled on the transact
# lane until it graduates or is claimed. description + image_url are OBSERVED
# content fields (the audit's exhaust) — they enrich the seed for when it's
# claimed and add NO transact serving. The pivota_signature_* fields (W5 P3)
# DO mint the offer-free citation identity: the by-signature PDP read serves
# this seed once index_pipeline_state.index_eligible flips true (which only
# happens when the seed passes the quality gates — a thin seed stays
# ineligible until E1 enrichment upgrades it).
#
# Convergence Phase 1.1: the tier triple is stamped EXPLICITLY. The DB
# server-defaults (internal_merchant/primary/commerce_ready, db/catalog.py)
# describe a FIRST-PARTY SYNCED product; an audit seed is an OBSERVED,
# unclaimed record and must carry the same honest triple as the external-seed
# mirror door (external_referral/observed/referral_only). Insert-only: the
# ON CONFLICT branch never re-asserts tiers, so a row a future graduation
# ladder has advanced is never downgraded by a re-audit.
_AUDIT_SEED_CATALOG_TRACK = "external_referral"
_AUDIT_SEED_TRUTH_TIER = "observed"
_AUDIT_SEED_READINESS_TIER = "referral_only"

_CATALOG_INSERT_COLUMNS = (
    "product_key", "merchant_id", "platform", "source_product_id",
    "title", "brand", "content_key", "gtin", "canonical_url", "source_domain",
    "product_type", "description", "image_url",
    "pivota_signature_id", "pivota_canonical_url", "pivota_signature_minted_at",
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


# --- Brand-fragmentation guard (ADR-008) --------------------------------------
# The ER gate above dedups at the SKU level (exact URL / source-id). It does NOT
# catch BRAND-level fragmentation: a genuinely NEW SKU of a brand that already has
# a published canonical presence under a DIFFERENT merchant (e.g. an external_seed
# brand) gets minted as an orphan under the audit/wedge merchant, splitting the
# brand's identity + citation signal across merchant_ids (the live ANUKO case:
# the URL-wedge minted a shampoo row under the demo merchant while the brand was
# already canonical under external_seed). This guard detects that collision and
# routes it to identity review instead of minting the orphan. Flag-gated + dark +
# best-effort — it may only SKIP a mint, never write across identities itself
# (that reconciliation is a separate, reviewed step — see ADR-008).


def audit_brand_fragmentation_guard_disabled() -> bool:
    """Explicit OPT-OUT for the ADR-008 brand-fragmentation guard. Default false.
    Set DISABLE_AUDIT_BRAND_FRAGMENTATION_GUARD=1 only to force the guard OFF for a
    merchant that intake is otherwise enabled for — a canary escape hatch, not the
    normal enablement lever."""
    return os.getenv("DISABLE_AUDIT_BRAND_FRAGMENTATION_GUARD", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def audit_brand_fragmentation_guard_enabled(merchant_id: Optional[str] = None) -> bool:
    """The ADR-008 brand-fragmentation guard runs UNCONDITIONALLY. Now that
    audit-index intake is the unconditional main line (W5 P2 removed
    ENABLE_AUDIT_INDEX_INTAKE), the guard is the correctness protection that
    REPLACES the flag — it must run for every seed. The opt-out env
    DISABLE_AUDIT_BRAND_FRAGMENTATION_GUARD (default false) can force it off as a
    canary escape hatch."""
    if audit_brand_fragmentation_guard_disabled():
        return False
    return True


async def _existing_brand_canonical_conflict(
    merchant_id: str, fields: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Return an existing PUBLISHED canonical row for the SAME brand + host under a
    DIFFERENT merchant, or None. Conservative (case-insensitive exact brand + host
    match, published-only) to keep false-positives near-zero — a false skip would
    drop a legitimately new brand's seed."""
    brand = str(fields.get("brand") or "").strip()
    host = str(fields.get("source_domain") or "").strip() or _host(fields.get("canonical_url"))
    if not brand or not host:
        return None
    from db.database import database

    row = await database.fetch_one(
        """
        SELECT product_key, merchant_id, content_key, pivota_signature_id
        FROM catalog_products
        WHERE lower(btrim(brand)) = lower(btrim(:brand))
          AND merchant_id <> :merchant_id
          AND (source_domain = :host OR canonical_url ILIKE :host_like)
          AND (pivota_signature_id IS NOT NULL OR merchant_id = 'external_seed')
          AND suppression_reason IS NULL
        LIMIT 1
        """,
        {
            "brand": brand,
            "merchant_id": merchant_id,
            "host": host,
            "host_like": f"%{host}%",
        },
    )
    return dict(row) if row else None


async def apply_intake_brand_fragmentation_guard(
    merchant_id: str,
    fields: Dict[str, Any],
    *,
    door: str = "url_audit_intake",
    block_on_conflict: bool = True,
) -> Dict[str, Any]:
    """ADR-008 prevent-at-intake, shared by ALL intake doors (convergence
    P1.4 — previously only the audit door ran it). Returns {action, ...}:
      - 'proceed' : flag off, no conflict, or any error (fail-open — never
                    block a record on the guard's account).
      - 'skip'    : (block_on_conflict=True; observed-data doors: audit,
                    seed mirror) a same-brand+host canonical exists under
                    another merchant; a review task was enqueued and the
                    orphan mint is suppressed.
      - 'flag'    : (block_on_conflict=False; the FIRST-PARTY sync door) the
                    conflict was enqueued for reconciliation but the record
                    PROCEEDS — a connected merchant's own catalog is the
                    higher-truth source and must never be blocked by an
                    observed row (ADR-008 reconcile-at-connect, not
                    block-at-connect).
    """
    if not audit_brand_fragmentation_guard_enabled(merchant_id):
        return {"action": "proceed", "reason": "disabled"}
    try:
        conflict = await _existing_brand_canonical_conflict(merchant_id, fields)
    except Exception as exc:  # noqa: BLE001 — never block the record on a guard error
        logger.warning(
            "%s.brand_guard lookup failed for %s: %s",
            door, fields.get("product_key"), str(exc)[:200],
        )
        return {"action": "proceed", "reason": "error"}
    if not conflict:
        return {"action": "proceed", "reason": "no_conflict"}
    # Reuse the ER-gate's review machinery so reconciliation can attach it.
    await enqueue_audit_identity_review(
        fields,
        {
            "product_key": conflict.get("product_key"),
            "matcher": "brand_host_fragmentation",
            "confidence": None,
            "evidence": {
                "reason": "brand_already_canonical_under_other_merchant",
                "door": door,
                "conflict_merchant_id": conflict.get("merchant_id"),
                "conflict_content_key": conflict.get("content_key"),
                "brand": fields.get("brand"),
                "host": fields.get("source_domain"),
            },
        },
    )
    return {
        "action": "skip" if block_on_conflict else "flag",
        "reason": "brand_fragmentation",
        "conflict_product_key": conflict.get("product_key"),
        "conflict_merchant_id": conflict.get("merchant_id"),
    }


async def apply_audit_brand_fragmentation_guard(
    merchant_id: str, fields: Dict[str, Any]
) -> Dict[str, Any]:
    """Audit-door wrapper (original call-site contract): conflict → 'skip'."""
    return await apply_intake_brand_fragmentation_guard(
        merchant_id, fields, door="url_audit_intake", block_on_conflict=True
    )


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
    from services.intake_identity import (
        ACTION_SKIP,
        DOOR_URL_AUDIT,
        intake_identity_enabled,
        resolve_or_attach_content_identity,
    )

    if intake_identity_enabled(DOOR_URL_AUDIT):
        # ADR-011 resolve-or-attach (flag-gated; replaces the legacy ER gate +
        # standalone brand guard below when ON — the primitive composes both).
        ident = await resolve_or_attach_content_identity(
            brand=fields.get("brand"),
            title=fields.get("title"),
            gtin=fields.get("gtin"),
            canonical_url=fields.get("canonical_url"),
            source_product_id=fields.get("source_product_id"),
            door=DOOR_URL_AUDIT,
            merchant_ctx={
                "merchant_id": merchant_id,
                "platform": PLATFORM_URL_AUDIT,
                "source_domain": fields.get("source_domain"),
                "product_key": fields.get("product_key"),
            },
        )
        if ident.get("action") == ACTION_SKIP:
            logger.info(
                "audit_intake.identity_skip pk=%s brand=%r host=%r",
                fields.get("product_key"), fields.get("brand"),
                fields.get("source_domain"),
            )
            return None
        fields["content_key"] = ident.get("content_key") or fields.get("content_key")
        attach = ident.get("attach") or None
        if attach and attach.get("same_merchant"):
            # R4 / ADR-010 D-6 at intake: the audited URL resolved Tier-0 to a
            # listing THIS merchant already has — re-key the upsert onto that
            # listing's existing (platform, source_product_id, product_key)
            # identity instead of minting a URL-fresh sibling row + sig. The
            # on-conflict branch preserves the existing sig (write-once), so
            # a re-audit at a new URL updates the one listing, never mints a
            # second public PDP for it.
            from services.catalog_sync_service import make_catalog_product_key

            fields["source_product_id"] = attach["source_product_id"]
            fields["platform"] = attach.get("platform") or fields.get("platform")
            fields["product_key"] = attach.get("product_key") or make_catalog_product_key(
                merchant_id, fields["platform"], fields["source_product_id"]
            )
            if attach.get("pivota_signature_id"):
                fields["pivota_signature_id"] = attach["pivota_signature_id"]
                fields["pivota_canonical_url"] = attach.get("pivota_canonical_url")
    else:
        # Entity-resolution gate (flag-gated): an EXACT match re-aligns the seed's
        # content_key to the existing canonical entity (dedup); a FUZZY match goes to
        # human review. Best-effort — content_key falls back to the original.
        gate_action = "none"
        try:
            gate = await apply_audit_er_gate(fields)
            fields["content_key"] = gate.get("content_key") or fields.get("content_key")
            gate_action = gate.get("action") or "none"
        except Exception as exc:  # noqa: BLE001 — the gate must never break the seed
            logger.warning(
                "upsert_audited_sku_to_index: ER gate failed for %s: %s",
                fields.get("product_key"), str(exc)[:200],
            )
        # Brand-fragmentation guard (ADR-008): only when the ER gate found no exact
        # SKU dedup — a real same-SKU 'align' is the correct outcome and proceeds. If
        # the brand is already canonical under another merchant, SKIP the orphan mint
        # (review was enqueued). Best-effort: on any failure the seed proceeds.
        if gate_action != "align":
            try:
                guard = await apply_audit_brand_fragmentation_guard(merchant_id, fields)
                if guard.get("action") == "skip":
                    logger.info(
                        "audit_intake.brand_guard_skip pk=%s brand=%r host=%r "
                        "conflict_pk=%s conflict_merchant=%s",
                        fields.get("product_key"), fields.get("brand"),
                        fields.get("source_domain"), guard.get("conflict_product_key"),
                        guard.get("conflict_merchant_id"),
                    )
                    return None
            except Exception as exc:  # noqa: BLE001 — guard must never break the seed
                logger.warning(
                    "upsert_audited_sku_to_index: brand guard failed for %s: %s",
                    fields.get("product_key"), str(exc)[:200],
                )
    # W5 P3 (serve chain): the by-signature PDP read INNER-JOINs catalog_merchants
    # (indexable + status='active'). URL-tier merchants have no synced storefront,
    # so no catalog_merchants row exists — without one the seed's canonical PDP
    # would 404 even once it's index_eligible. Upsert a minimal, indexable row
    # (indexable defaults true; a NEW row is minted status='active') so the
    # citation read resolves. Best-effort — never break the seed.
    #
    # Deliberately passes NO `status`: an audit is a content signal, not a
    # lifecycle one. `catalog_merchants.status` is owned by
    # services/store_lifecycle_service.py, and re-asserting 'active' here undid
    # the one transition that has no reconciliation backstop — the merchant who
    # detached their LAST store (PR #1852). See upsert_catalog_merchant.
    try:
        from services.catalog_sync_service import upsert_catalog_merchant

        await upsert_catalog_merchant(
            merchant_id=merchant_id,
            merchant_name=None,
            primary_platform=PLATFORM_URL_AUDIT,
            source_system="url_audit_intake",
            source_ref=fields.get("source_domain"),
            metadata_json={"ingested_from": "url_audit_intake"},
        )
    except Exception as exc:  # noqa: BLE001 — merchant upsert is best-effort
        logger.warning(
            "upsert_audited_sku_to_index: catalog_merchant upsert failed for %s: %s",
            merchant_id, str(exc)[:200],
        )
    # Convergence P1.2 (ADR-009 D3): seller-of-record on the CANONICAL row.
    # Audit-door records have no external_product_seeds row, so without this
    # the attribution closure has no seller_ref source and stamps
    # seller_ref_missing.
    #
    # CRITICAL: we resolve the seller from the DESTINATION (brand + audited
    # domain) via the SAME claim-aware primitive the crawl door uses
    # (ensure_observed_seller), NOT by treating the auditing merchant as an
    # "anchor that owns the domain". The audit path calls upsert_catalog_merchant
    # ABOVE, which overwrites catalog_merchants[merchant_id].source_ref with the
    # AUDITED domain — so an anchor-owns-domain check reads that back and would
    # tautologically resolve 'self' for EVERY audit, mis-attributing a
    # competitor's product to the auditing merchant (write-once, never
    # corrected). ensure_observed_seller instead reads verified `brand_claims`:
    #   - destination is a VERIFIED-claimed domain → that tenant is the seller
    #     (→ seed_kind 'self' iff it IS the auditing merchant, else 'cross');
    #   - otherwise → a per-brand observed seller (→ 'cross').
    # underivable (no brand / non-registrable domain / error) → NULL/NULL, the
    # honest legacy state that A9-4 backfills; NEVER assume 'self'.
    seller_ref: Optional[str] = None
    seed_kind: Optional[str] = None
    try:
        from services.seller_identity import ensure_observed_seller, etld1

        _dest = fields.get("source_domain") or fields.get("canonical_url")
        _brand = str(fields.get("brand") or "").strip()
        if _brand and etld1(_dest):
            resolved_seller = await ensure_observed_seller(
                brand=_brand,
                domain=_dest,
                source_system="url_audit_intake",
                primary_platform=PLATFORM_URL_AUDIT,
            )
            seller_ref = resolved_seller
            seed_kind = "self" if resolved_seller == merchant_id else "cross"
    except Exception as exc:  # noqa: BLE001 — must never break the audit seed
        seller_ref, seed_kind = None, None
        logger.warning(
            "upsert_audited_sku_to_index: seller derivation failed for %s: %s",
            fields.get("product_key"), str(exc)[:200],
        )

    try:
        from db.catalog import catalog_products
        from db.database import database

        values = {k: fields.get(k) for k in _CATALOG_INSERT_COLUMNS}
        # Phase 1.1: honest tier triple on INSERT only (see comment at
        # _AUDIT_SEED_* above) — never in the ON CONFLICT set_.
        values["catalog_track"] = _AUDIT_SEED_CATALOG_TRACK
        values["truth_tier"] = _AUDIT_SEED_TRUTH_TIER
        values["readiness_tier"] = _AUDIT_SEED_READINESS_TIER
        # Phase 1.2: seller identity rides the canonical row.
        values["seller_ref"] = seller_ref
        values["seed_kind"] = seed_kind
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
                # W5 P3: the Pivota canonical sig is WRITE-ONCE — on a re-audit
                # (conflict on product_key) preserve the EXISTING sig/URL/minted_at
                # so a second audit never mints a second signature and the arc
                # phase stays honest. `existing` first, `excluded` second only
                # backfills rows that predate sig-minting (their sig was NULL).
                "pivota_signature_id": func.coalesce(
                    catalog_products.c.pivota_signature_id,
                    stmt.excluded.pivota_signature_id,
                ),
                "pivota_canonical_url": func.coalesce(
                    catalog_products.c.pivota_canonical_url,
                    stmt.excluded.pivota_canonical_url,
                ),
                "pivota_signature_minted_at": func.coalesce(
                    catalog_products.c.pivota_signature_minted_at,
                    stmt.excluded.pivota_signature_minted_at,
                ),
                # P1.2: seller identity is WRITE-ONCE (existing first) — a
                # re-audit backfills NULL legacy rows but never re-keys a
                # seller already derived (identity stability, ADR-009).
                "seller_ref": func.coalesce(
                    catalog_products.c.seller_ref, stmt.excluded.seller_ref
                ),
                "seed_kind": func.coalesce(
                    catalog_products.c.seed_kind, stmt.excluded.seed_kind
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
        # ADR-009 ratified decision 1 (no-fallback): stamp the deterministic
        # SINGLETON product_group_id so this audit-sourced product carries a pg
        # (offer path keys on pg with zero branching). ON CONFLICT DO NOTHING —
        # never overwrites a real/curated group (no auto-merge). Best-effort like
        # the rest of this intake: a failure is logged loudly, never silently
        # absorbed, and the backfill remains the safety net.
        try:
            from services.product_group_autogrouper import (
                ensure_singleton_group_membership,
            )

            await ensure_singleton_group_membership(
                merchant_id=str(fields.get("merchant_id") or ""),
                platform=str(fields.get("platform") or ""),
                source_product_id=str(fields.get("source_product_id") or ""),
                content_key=content_key,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort, never break intake
            logger.warning(
                "upsert_audited_sku_to_index: singleton pg mint failed for %s: %s",
                fields.get("product_key"), str(exc)[:200],
            )
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
        # W5 P3 (enrich→eligible graduation): recompute index_pipeline_state for
        # this content_key through the REAL classifier — never force-set
        # eligibility. A thin seed fails the quality gates (description length,
        # etc.) and stays index_ineligible; once E1 (canonical_pdp_enrichment)
        # fattens the PDP and re-runs this recompute, it graduates to
        # index_eligible and the offer-free citation read starts serving it. Runs
        # AFTER the agent_pdp_view refresh so the classifier reads fresh content.
        # Best-effort — a recompute failure leaves the nightly job as the safety
        # net and never breaks the seed.
        try:
            from services.index_pipeline_state_service import (
                recompute_serving_eligibility,
            )

            await recompute_serving_eligibility(
                content_key, reason="url_audit_intake"
            )
        except Exception as exc:  # noqa: BLE001 — recompute is best-effort
            logger.warning(
                "upsert_audited_sku_to_index: eligibility recompute failed for "
                "%s: %s",
                content_key, str(exc)[:200],
            )
    return content_key or fields.get("product_key")
