"""ADR-011 intake identity contract — resolve-or-attach before every mint.

Every intake door that writes catalog_products MUST pass this one shared
primitive before inserting a row (R1). It composes EXISTING machinery — no new
matching invention:

  - services.catalog_identity.make_content_key / normalize_gtin (the minter),
  - the audit ER gate's exact matchers (services.pdp_matcher.deterministic:
    canonical_url_match / source_product_id_match — the same Tier-0 matchers
    services.audit_index_intake routes through route_audit_match),
  - the deposit gate (services.catalog_identity.resolve_deposit_content_key,
    annotating how strongly the resolved key is grounded),
  - the ADR-008 / P1.4 brand-fragmentation guard
    (services.audit_index_intake.apply_intake_brand_fragmentation_guard).

IDENTITY MODEL (SPU — Amazon ASIN / Dewu SPU; founder direction 2026-07-09):
GTIN is a MATCH ATTRIBUTE, never key-material. There is ONE canonical family
identity per product — `content_key = make_content_key(brand, title)`, GTIN-less
— and the barcode lives beside it in `catalog_products.gtin`. Many products have
no standard GTIN, so a GTIN can never be *required* to have an identity; and a
product seen with-then-without a barcode must converge on ONE identity, not
fragment. The primitive therefore:

  - mints/keys purely on brand+title (the always-available signal),
  - uses GTIN as the STRONGEST Tier-0 matcher (GS1 GTIN = same physical product
    across merchants), attaching to whatever identity already carries it,
  - keeps the two-grain split (ADR-010, R7): content_key is the FAMILY grain;
    the GTIN attribute is the variant/buy-box discriminator downstream. Two
    genuinely-different products that collide on the deliberately non-unique
    brand+title key are FLAGged (never silently forked — the deterministic hash
    can't fork anyway) and told apart downstream by their distinct gtin
    attribute + the deposit gate.

Tier-0 EXACT matches only (ADR-010's auto tier): GTIN attribute, canonical_url,
source_product_id. Fuzzy/attribute matching stays propose-only elsewhere.

ATTACH semantics (ADR-011, review-verified): a catalog_products row IS still
inserted by the door — it reuses the resolved content_key / product_group_id
instead of minting fresh ones. This is NOT P1.3's seed-detach ATTACH (nothing
here touches external_product_seeds.attached_product_key) and there is no
serving change of any kind.

R3 disagreement semantics (FLAG, never a silent second identity):
  - same GTIN, different brand+title family → GTIN is authoritative: ATTACH to
    the GTIN's identity, FLAG the title/brand drift for review;
  - same brand+title, different GTIN → two distinct products colliding on the
    family key → FLAG for disambiguation (the row still lands under the shared
    family key, discriminated by its own gtin attribute downstream).

Rollout: per-door enable flags, default OFF (mirror-then-sync enable order).
Fail-open: any internal error degrades to MINT (today's behavior) — the
primitive must never block intake on its own account.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# --- Actions --------------------------------------------------------------------

ACTION_ATTACH = "ATTACH"  # reuse the resolved content_key/pg for the new listing
ACTION_MINT = "MINT"      # no exact match — mint fresh, exactly as today
ACTION_FLAG = "FLAG"      # proceed, but a conflict was enqueued for review
ACTION_SKIP = "SKIP"      # do not insert (observed-data doors only)


# --- Doors + per-door rollout flags (default OFF) --------------------------------

DOOR_CATALOG_SYNC = "catalog_sync"
DOOR_EXTERNAL_SEED_MIRROR = "external_seed_mirror"
DOOR_BRAND_AUTHORED = "brand_authored"
DOOR_CATALOG_ENRICHMENT = "catalog_enrichment"
DOOR_URL_AUDIT = "url_audit_intake"

_DOOR_FLAG_ENV = {
    DOOR_CATALOG_SYNC: "ENABLE_INTAKE_IDENTITY_SYNC",
    DOOR_EXTERNAL_SEED_MIRROR: "ENABLE_INTAKE_IDENTITY_MIRROR",
    DOOR_BRAND_AUTHORED: "ENABLE_INTAKE_IDENTITY_BRAND_AUTHORED",
    DOOR_CATALOG_ENRICHMENT: "ENABLE_INTAKE_IDENTITY_ENRICHMENT",
    DOOR_URL_AUDIT: "ENABLE_INTAKE_IDENTITY_AUDIT",
}

# ADR-008 door semantics: first-party doors (connected sync, the merchant's own
# manual authoring) are NEVER blocked — a brand conflict FLAGs and the record
# proceeds (reconcile-at-connect). Observed-data doors (mirror, retailer crawl,
# audit) SKIP the orphan mint; review is enqueued either way.
_DOOR_BLOCKS_ON_BRAND_CONFLICT = {
    DOOR_CATALOG_SYNC: False,
    DOOR_BRAND_AUTHORED: False,
    DOOR_EXTERNAL_SEED_MIRROR: True,
    DOOR_CATALOG_ENRICHMENT: True,
    DOOR_URL_AUDIT: True,
}


def intake_identity_enabled(door: str) -> bool:
    """Per-door rollout flag for the ADR-011 primitive. Default OFF for every
    door; enable mirror-then-sync (the ADR's staged order)."""
    env = _DOOR_FLAG_ENV.get(door)
    if not env:
        return False
    return os.getenv(env, "").strip().lower() in {"1", "true", "yes", "on"}


def canonical_gtin(value: Optional[str]) -> Optional[str]:
    """GS1-canonical GTIN-14 for storage + matching, or None. Only a clean
    14-digit form is a reliable identifier — normalize_gtin passes 15+ digit
    malformed inputs through unchanged, so we drop those rather than store a
    junk match key (mirrors agent_pdp_view_assembler.pick_gtin13)."""
    from services.catalog_identity import normalize_gtin

    norm = normalize_gtin(value)
    if not norm or len(norm) != 14:
        return None
    # ALL-ZERO IS A SENTINEL, NOT AN IDENTIFIER. `normalize_gtin` zero-pads to
    # 14, so a source field holding '0' — or '', or '000' — becomes
    # '00000000000000', which is structurally a valid GTIN-14 and then flows
    # into Tier-0 exact matching. That matcher is GLOBAL by design: a GS1 GTIN
    # identifies one physical product across every merchant. So one placeholder
    # zero would ATTACH unrelated products to each other, across merchants,
    # with the highest-confidence matcher in the system.
    #
    # Guarded HERE and not inside `normalize_gtin`: `make_content_key` folds
    # that function's output, so changing it would silently re-key every row
    # already minted with a zero GTIN. The consumer drops it; the key stays.
    if set(norm) == {"0"}:
        return None
    return norm


# --- DB lookups (each one small, exact, and monkeypatch-friendly) ----------------

_ROW_COLUMNS = (
    "product_key, merchant_id, platform, source_product_id, canonical_url, "
    "title, brand, content_key, gtin, pivota_signature_id, pivota_canonical_url"
)


async def _rows_by_gtin(
    gtin14: str, prefer_merchant_id: Optional[str]
) -> List[Dict[str, Any]]:
    """Existing listings carrying this GTIN attribute (the authoritative Tier-0
    matcher). GLOBAL scope — a GS1 GTIN identifies one physical product across
    every merchant. Same-merchant rows first, then oldest (the original
    identity), suppressed rows excluded."""
    if not gtin14:
        return []
    from db.database import database

    rows = await database.fetch_all(
        f"""
        SELECT {_ROW_COLUMNS}
        FROM catalog_products
        WHERE gtin = :gtin
          AND suppression_reason IS NULL
        ORDER BY (merchant_id = :merchant_id) DESC, created_at ASC
        LIMIT 5
        """,
        {"gtin": gtin14, "merchant_id": prefer_merchant_id or ""},
    )
    return [dict(row) for row in rows or []]


async def _rows_by_content_key(
    content_key: str, prefer_merchant_id: Optional[str]
) -> List[Dict[str, Any]]:
    """Existing listings on this brand+title FAMILY key. Same-merchant rows
    first, then oldest, suppressed rows excluded."""
    if not content_key:
        return []
    from db.database import database

    rows = await database.fetch_all(
        f"""
        SELECT {_ROW_COLUMNS}
        FROM catalog_products
        WHERE content_key = :content_key
          AND suppression_reason IS NULL
        ORDER BY (merchant_id = :merchant_id) DESC, created_at ASC
        LIMIT 5
        """,
        {"content_key": content_key, "merchant_id": prefer_merchant_id or ""},
    )
    return [dict(row) for row in rows or []]


async def _candidates_by_canonical_url(url_path_fragment: str) -> List[Dict[str, Any]]:
    """LIKE prefilter on the URL path (catches scheme/www drift); the pure
    matcher does the exact normalized-equality check. Unlike the pdp_matcher
    runner's candidate fetch, intake attaches against ANY live listing — no
    pdp_scope restriction (most catalog rows are merchant_owned/unverified)."""
    if not url_path_fragment:
        return []
    from db.database import database

    rows = await database.fetch_all(
        f"""
        SELECT {_ROW_COLUMNS}
        FROM catalog_products
        WHERE canonical_url IS NOT NULL
          AND LOWER(canonical_url) LIKE :url_like
          AND suppression_reason IS NULL
        LIMIT 25
        """,
        {"url_like": f"%{url_path_fragment.lower()}%"},
    )
    return [dict(row) for row in rows or []]


async def _candidates_by_source_id(
    source_product_id: str, merchant_id: str
) -> List[Dict[str, Any]]:
    """source_product_id is only identity evidence WITHIN a merchant (two
    Shopify stores can both have product id 12345) — same-merchant scope."""
    if not source_product_id or not merchant_id:
        return []
    from db.database import database

    rows = await database.fetch_all(
        f"""
        SELECT {_ROW_COLUMNS}
        FROM catalog_products
        WHERE source_product_id = :source_product_id
          AND merchant_id = :merchant_id
          AND suppression_reason IS NULL
        LIMIT 25
        """,
        {"source_product_id": source_product_id, "merchant_id": merchant_id},
    )
    return [dict(row) for row in rows or []]


async def _existing_pg_for_listing(row: Dict[str, Any]) -> Optional[str]:
    """The product_group the matched listing already belongs to (curated or
    singleton). Falls back to the deterministic singleton pg for the resolved
    content_key when no membership row exists yet."""
    from db.database import database

    member = await database.fetch_one(
        """
        SELECT product_group_id FROM product_group_members
        WHERE merchant_id = :merchant_id
          AND platform = :platform
          AND platform_product_id = :platform_product_id
        """,
        {
            "merchant_id": str(row.get("merchant_id") or ""),
            "platform": str(row.get("platform") or ""),
            "platform_product_id": str(row.get("source_product_id") or ""),
        },
    )
    return member["product_group_id"] if member else None


async def _write_provenance(provenance: Dict[str, Any]) -> None:
    """R1's provenance requirement: every outcome writes {door, action, matcher,
    evidence} (feeds ADR-010 D-2 + gold-label capture). Best-effort — a
    provenance failure must never block intake (the structured log line below
    is the always-on fallback)."""
    try:
        from db.database import database

        await database.execute(
            """
            INSERT INTO intake_identity_events
              (door, action, matcher, merchant_id, product_key, content_key,
               product_group_id, evidence)
            VALUES
              (:door, :action, :matcher, :merchant_id, :product_key, :content_key,
               :product_group_id, CAST(:evidence AS jsonb))
            """,
            {
                "door": provenance.get("door"),
                "action": provenance.get("action"),
                "matcher": provenance.get("matcher"),
                "merchant_id": provenance.get("merchant_id"),
                "product_key": provenance.get("product_key"),
                "content_key": provenance.get("content_key"),
                "product_group_id": provenance.get("product_group_id"),
                "evidence": json.dumps(
                    provenance.get("evidence") or {}, ensure_ascii=False, default=str
                ),
            },
        )
    except Exception as exc:  # noqa: BLE001 — provenance is best-effort
        logger.warning("intake_identity provenance write failed: %s", str(exc)[:200])


# --- Result assembly --------------------------------------------------------------


def _singleton_pg(content_key: Optional[str]) -> Optional[str]:
    if not content_key:
        return None
    from services.product_group_autogrouper import make_singleton_product_group_id

    return make_singleton_product_group_id(content_key)


def _deposit_basis(brand, title, gtin, content_key) -> str:
    """Deposit-gate annotation (catalog_identity.resolve_deposit_content_key):
    how strongly the resolved key is grounded (GTIN-backed identities are
    well-grounded even though our KEY no longer folds the GTIN in). Evidence
    only — deposits themselves stay gated at their own call sites."""
    from services.catalog_identity import resolve_deposit_content_key

    return resolve_deposit_content_key(
        brand=brand, title=title, gtin=gtin, existing_content_key=content_key
    ).basis


async def _finish(
    *,
    action: str,
    content_key: Optional[str],
    product_group_id: Optional[str],
    matcher: Optional[str],
    door: str,
    merchant_ctx: Dict[str, Any],
    detail: Dict[str, Any],
    gtin: Optional[str],
    attach: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    provenance = {
        "door": door,
        "action": action,
        "matcher": matcher,
        "merchant_id": merchant_ctx.get("merchant_id"),
        "product_key": merchant_ctx.get("product_key"),
        "content_key": content_key,
        "product_group_id": product_group_id,
        "evidence": detail,
    }
    logger.info(
        "intake_identity.resolve door=%s action=%s matcher=%s merchant=%s ck=%s pg=%s",
        door, action, matcher, merchant_ctx.get("merchant_id"),
        content_key, product_group_id,
    )
    await _write_provenance(provenance)
    return {
        "content_key": content_key,
        "product_group_id": product_group_id,
        "action": action,
        "gtin": gtin,
        "evidence": provenance,
        "attach": attach,
    }


def _attach_info(row: Dict[str, Any], merchant_id: Optional[str]) -> Dict[str, Any]:
    """What the door needs to reuse the matched listing's identity. R4 /
    ADR-010 D-6: on a SAME-merchant attach, the audit door must resolve to the
    listing's existing source_product_id/sig instead of minting a URL-fresh
    one — `same_merchant` is that signal."""
    return {
        "product_key": row.get("product_key"),
        "merchant_id": row.get("merchant_id"),
        "platform": row.get("platform"),
        "source_product_id": row.get("source_product_id"),
        "pivota_signature_id": row.get("pivota_signature_id"),
        "pivota_canonical_url": row.get("pivota_canonical_url"),
        "same_merchant": bool(
            merchant_id and str(row.get("merchant_id") or "") == str(merchant_id)
        ),
    }


async def _attach_pg(row: Dict[str, Any], content_key: Optional[str]) -> Optional[str]:
    try:
        existing = await _existing_pg_for_listing(row)
    except Exception as exc:  # noqa: BLE001 — pg lookup is best-effort
        logger.warning("intake_identity pg lookup failed: %s", str(exc)[:200])
        existing = None
    return existing or _singleton_pg(content_key)


async def _flag_review(
    door: str,
    merchant_ctx: Dict[str, Any],
    content_key: Optional[str],
    matcher: str,
    detail: Dict[str, Any],
) -> None:
    """FLAG outcomes ride the SAME review rail as the ER gate / P1.4 guard
    (pdp_review_tasks, module 'identity') so reconciliation sees one queue."""
    from services.audit_index_intake import enqueue_audit_identity_review

    await enqueue_audit_identity_review(
        {
            "product_key": merchant_ctx.get("product_key"),
            "content_key": content_key,
        },
        {
            "product_key": detail.get("conflict_product_key"),
            "matcher": matcher,
            "confidence": None,
            "evidence": {**detail, "door": door},
        },
    )


# --- The primitive ---------------------------------------------------------------


async def resolve_or_attach_content_identity(
    brand: Optional[str],
    title: Optional[str],
    gtin: Optional[str] = None,
    canonical_url: Optional[str] = None,
    source_product_id: Optional[str] = None,
    door: str = "",
    merchant_ctx: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Resolve-or-attach the content identity for one incoming catalog row.

    Returns {content_key, product_group_id, action, gtin, evidence, attach}:
      - ATTACH: an existing identity matched Tier-0 exactly (GTIN attribute,
        content_key, canonical_url, or same-merchant source_product_id) — the
        door inserts its row REUSING content_key/product_group_id (and,
        same-merchant at the audit door, the matched listing's
        source_product_id/sig — R4).
      - MINT:   no exact match — content_key is the GTIN-less family key
        make_content_key(brand, title) and product_group_id its deterministic
        singleton pg. `gtin` (canonicalized) is returned for the door to persist
        as the match attribute.
      - FLAG:   a conflict (GTIN/brand-title disagreement, or a
        brand-fragmentation conflict at a first-party door) was enqueued for
        identity review; the door PROCEEDS with the returned identity — flagged,
        not silent.
      - SKIP:   observed-data doors only — a brand-fragmentation conflict; the
        door must not insert (review enqueued).

    Fail-open by construction: any internal error returns MINT with today's
    brand+title identity. Never raises.
    """
    ctx = dict(merchant_ctx or {})
    merchant_id = str(ctx.get("merchant_id") or "") or None

    from services.catalog_identity import make_content_key

    gtin14 = canonical_gtin(gtin)
    content_key = make_content_key(brand, title)  # single canonical FAMILY key

    def _base_detail() -> Dict[str, Any]:
        return {
            "brand": brand,
            "title": title,
            "gtin": gtin14,
            "canonical_url": canonical_url,
            "source_product_id": source_product_id,
            "content_key": content_key,
        }

    try:
        # -- Tier-0a: GTIN attribute (authoritative, cross-merchant). A GS1 GTIN
        # identifies one physical product regardless of who sells it or how the
        # title is phrased, so it OUTRANKS brand+title.
        if gtin14:
            rows = await _rows_by_gtin(gtin14, merchant_id)
            if rows:
                row = rows[0]
                matched_ck = row.get("content_key") or content_key
                distinct_cks = {r.get("content_key") for r in rows if r.get("content_key")}
                drift = bool(content_key and matched_ck != content_key)
                if drift or len(distinct_cks) > 1:
                    # Same GTIN under a different brand+title family (or split
                    # across families). GTIN is authoritative → ATTACH to it,
                    # but FLAG the metadata drift. We never fork a new identity,
                    # so this is non-silent-attach, not a second identity.
                    detail = {
                        **_base_detail(),
                        "reason": "gtin_match_brand_title_drift",
                        "conflict_product_key": row.get("product_key"),
                        "matched_content_key": matched_ck,
                        "incoming_content_key": content_key,
                        "distinct_content_keys": sorted(c for c in distinct_cks if c),
                        "deposit_basis": _deposit_basis(brand, title, gtin, matched_ck),
                    }
                    await _flag_review(
                        door, ctx, matched_ck, "gtin_match_brand_title_drift", detail
                    )
                    return await _finish(
                        action=ACTION_FLAG, content_key=matched_ck,
                        product_group_id=await _attach_pg(row, matched_ck),
                        matcher="gtin_match_brand_title_drift", door=door,
                        merchant_ctx=ctx, detail=detail, gtin=gtin14,
                        attach=_attach_info(row, merchant_id),
                    )
                return await _finish(
                    action=ACTION_ATTACH, content_key=matched_ck,
                    product_group_id=await _attach_pg(row, matched_ck),
                    matcher="gtin_match", door=door, merchant_ctx=ctx,
                    detail={
                        **_base_detail(),
                        "matched_product_key": row.get("product_key"),
                        "deposit_basis": _deposit_basis(brand, title, gtin, matched_ck),
                    },
                    gtin=gtin14, attach=_attach_info(row, merchant_id),
                )

        if not content_key:
            # No brand+title identity and no GTIN attach — honest absence (the
            # door keeps today's content_key-NULL behavior; pg stays NULL). We
            # never mint an identity from a GTIN alone.
            return await _finish(
                action=ACTION_MINT, content_key=None, product_group_id=None,
                matcher=None, door=door, merchant_ctx=ctx, gtin=gtin14,
                detail={**_base_detail(), "reason": "no_identity_inputs"},
            )

        # -- Tier-0b: content_key (brand+title FAMILY key).
        rows = await _rows_by_content_key(content_key, merchant_id)
        if rows:
            known_gtins = {r.get("gtin") for r in rows if r.get("gtin")}
            if gtin14 and known_gtins and gtin14 not in known_gtins:
                # Same brand+title, but a DIFFERENT GTIN already lives on this
                # family key → two distinct products colliding on the
                # deliberately non-unique family key (ADR-010 two-grain:
                # family=content_key, variant=gtin). The deterministic hash
                # can't fork, so the row still lands under this family key,
                # discriminated by its own gtin attribute + the deposit gate
                # downstream. FLAG for disambiguation — never silent.
                detail = {
                    **_base_detail(),
                    "reason": "brand_title_collision_distinct_gtin",
                    "conflict_product_key": rows[0].get("product_key"),
                    "existing_gtins": sorted(g for g in known_gtins if g),
                }
                await _flag_review(
                    door, ctx, content_key, "brand_title_collision", detail
                )
                return await _finish(
                    action=ACTION_FLAG, content_key=content_key,
                    product_group_id=_singleton_pg(content_key),
                    matcher="brand_title_collision", door=door, merchant_ctx=ctx,
                    detail=detail, gtin=gtin14,
                )
            row = rows[0]
            ck = row.get("content_key") or content_key
            return await _finish(
                action=ACTION_ATTACH, content_key=ck,
                product_group_id=await _attach_pg(row, ck),
                matcher="content_key", door=door, merchant_ctx=ctx,
                detail={
                    **_base_detail(),
                    "matched_product_key": row.get("product_key"),
                    "deposit_basis": _deposit_basis(brand, title, gtin, ck),
                },
                gtin=gtin14, attach=_attach_info(row, merchant_id),
            )

        # -- Tier-0c: canonical_url exact (the ER gate's matcher, unchanged:
        # unique normalized-equality hit or nothing).
        if canonical_url:
            from urllib.parse import urlparse

            from services.pdp_matcher.deterministic import (
                canonical_url_match,
                normalize_canonical_url,
            )

            normalized = normalize_canonical_url(canonical_url)
            path_only = (urlparse(normalized).path or "") if normalized else ""
            candidates = (
                await _candidates_by_canonical_url(path_only) if path_only else []
            )
            match = (
                canonical_url_match(
                    seed={"canonical_url": canonical_url}, candidates=candidates
                )
                if candidates
                else None
            )
            if match:
                row = next(
                    c for c in candidates
                    if c.get("product_key") == match.get("product_key")
                )
                if row.get("content_key"):
                    ck = row["content_key"]
                    return await _finish(
                        action=ACTION_ATTACH, content_key=ck,
                        product_group_id=await _attach_pg(row, ck),
                        matcher="canonical_url_match", door=door,
                        merchant_ctx=ctx,
                        detail={
                            **_base_detail(),
                            "matched_product_key": row.get("product_key"),
                            "matcher_evidence": match.get("evidence"),
                            "deposit_basis": _deposit_basis(brand, title, gtin, ck),
                        },
                        gtin=gtin14, attach=_attach_info(row, merchant_id),
                    )

        # -- Tier-0d: source_product_id exact, SAME-merchant scope only.
        if source_product_id and merchant_id:
            from services.pdp_matcher.deterministic import source_product_id_match

            candidates = await _candidates_by_source_id(
                str(source_product_id), merchant_id
            )
            match = (
                source_product_id_match(
                    seed={"external_product_id": source_product_id},
                    candidates=candidates,
                )
                if candidates
                else None
            )
            if match:
                row = next(
                    c for c in candidates
                    if c.get("product_key") == match.get("product_key")
                )
                if row.get("content_key"):
                    ck = row["content_key"]
                    return await _finish(
                        action=ACTION_ATTACH, content_key=ck,
                        product_group_id=await _attach_pg(row, ck),
                        matcher="source_product_id_match", door=door,
                        merchant_ctx=ctx,
                        detail={
                            **_base_detail(),
                            "matched_product_key": row.get("product_key"),
                            "matcher_evidence": match.get("evidence"),
                            "deposit_basis": _deposit_basis(brand, title, gtin, ck),
                        },
                        gtin=gtin14, attach=_attach_info(row, merchant_id),
                    )

        # -- No exact match → ADR-008 / P1.4 brand-fragmentation guard, now
        # uniform across ALL five doors (extends the guard to doors 3/4).
        # merchant_ctx["brand_guard_memo"] (a set) keeps door 1's once-per-
        # brand-per-run economy.
        guard_action = "proceed"
        guard_detail: Dict[str, Any] = {}
        memo = ctx.get("brand_guard_memo")
        brand_key = str(brand or "").strip().lower()
        memo_hit = isinstance(memo, set) and brand_key and brand_key in memo
        if merchant_id and brand_key and not memo_hit:
            if isinstance(memo, set):
                memo.add(brand_key)
            from services.audit_index_intake import (
                apply_intake_brand_fragmentation_guard,
            )

            guard = await apply_intake_brand_fragmentation_guard(
                merchant_id,
                {
                    "product_key": ctx.get("product_key"),
                    "brand": brand,
                    "source_domain": ctx.get("source_domain"),
                    "canonical_url": canonical_url,
                    "content_key": content_key,
                },
                door=door,
                block_on_conflict=_DOOR_BLOCKS_ON_BRAND_CONFLICT.get(door, True),
            )
            guard_action = guard.get("action") or "proceed"
            guard_detail = {
                "conflict_product_key": guard.get("conflict_product_key"),
                "conflict_merchant_id": guard.get("conflict_merchant_id"),
            }

        if guard_action == "skip":
            return await _finish(
                action=ACTION_SKIP, content_key=content_key,
                product_group_id=None, matcher="brand_host_fragmentation",
                door=door, merchant_ctx=ctx, gtin=gtin14,
                detail={**_base_detail(), "reason": "brand_fragmentation", **guard_detail},
            )
        if guard_action == "flag":
            return await _finish(
                action=ACTION_FLAG, content_key=content_key,
                product_group_id=_singleton_pg(content_key),
                matcher="brand_host_fragmentation", door=door, merchant_ctx=ctx,
                gtin=gtin14,
                detail={**_base_detail(), "reason": "brand_fragmentation", **guard_detail},
            )

        return await _finish(
            action=ACTION_MINT, content_key=content_key,
            product_group_id=_singleton_pg(content_key), matcher=None,
            door=door, merchant_ctx=ctx, gtin=gtin14,
            detail={
                **_base_detail(),
                "deposit_basis": _deposit_basis(brand, title, gtin, content_key),
            },
        )
    except Exception as exc:  # noqa: BLE001 — fail-open: never block intake
        logger.warning(
            "resolve_or_attach_content_identity failed door=%s merchant=%s: %s",
            door, merchant_id, str(exc)[:300],
        )
        return {
            "content_key": content_key,
            "product_group_id": None,
            "action": ACTION_MINT,
            "gtin": gtin14,
            "evidence": {
                "door": door,
                "action": ACTION_MINT,
                "matcher": None,
                "merchant_id": merchant_id,
                "evidence": {"reason": "error", "error": str(exc)[:300]},
            },
            "attach": None,
        }
