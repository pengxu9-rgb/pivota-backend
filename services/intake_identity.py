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

Tier-0 EXACT matches only (ADR-010's auto tier): GTIN (via the content_key
forms), canonical_url, source_product_id. Fuzzy/attribute matching stays
propose-only elsewhere — never here.

ATTACH semantics (ADR-011, review-verified): a catalog_products row IS still
inserted by the door — it reuses the resolved content_key / product_group_id
instead of minting fresh ones. This is NOT P1.3's seed-detach ATTACH (nothing
here touches external_product_seeds.attached_product_key) and there is no
serving change of any kind.

R3 (GTIN with reconciliation): make_content_key(brand,title,gtin) differs from
the GTIN-less form and the legacy catalog is GTIN-less, so the GTIN'd form is
tried FIRST and the GTIN-less form is the fallback. A GTIN disagreement (same
GTIN, different brand+title — or same brand+title, different known GTIN) is a
FLAG, never a silent second identity.

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


# --- DB lookups (each one small, exact, and monkeypatch-friendly) ----------------

_ROW_COLUMNS = (
    "product_key, merchant_id, platform, source_product_id, canonical_url, "
    "title, brand, content_key, pivota_signature_id, pivota_canonical_url"
)


async def _rows_by_content_key(
    content_key: str, prefer_merchant_id: Optional[str]
) -> List[Dict[str, Any]]:
    """Existing listings carrying this EXACT content_key (the GTIN or GTIN-less
    form). Same-merchant rows first (R4 cares), then oldest (the original
    identity), suppressed rows excluded."""
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


async def _known_gtin13_for_content_key(content_key: str) -> Optional[str]:
    """The canonical GTIN-14 the existing identity is known by, from the
    denormalized agent_pdp_view (indexed; pick_gtin13 has already picked the
    modal barcode across the group's SKUs). None when unknown — absence of
    evidence is never a disagreement."""
    if not content_key:
        return None
    from db.database import database

    row = await database.fetch_one(
        "SELECT gtin13 FROM agent_pdp_view WHERE content_key = :ck",
        {"ck": content_key},
    )
    value = row["gtin13"] if row else None
    return str(value) if value else None


async def _content_key_for_gtin13(gtin13: str) -> Optional[Dict[str, Any]]:
    """Reverse lookup: is this GTIN already known under some OTHER content
    identity? Uses the partial index on agent_pdp_view.gtin13."""
    if not gtin13:
        return None
    from db.database import database

    row = await database.fetch_one(
        """
        SELECT content_key, brand, title FROM agent_pdp_view
        WHERE gtin13 = :gtin13 AND content_key IS NOT NULL
        LIMIT 1
        """,
        {"gtin13": gtin13},
    )
    return dict(row) if row else None


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
    how strongly the resolved key is grounded. Evidence-only here — deposits
    themselves stay gated at their own call sites."""
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

    Returns {content_key, product_group_id, action, evidence, attach}:
      - ATTACH: an existing identity matched Tier-0 exactly — the door inserts
        its row REUSING content_key/product_group_id (and, same-merchant at the
        audit door, the matched listing's source_product_id/sig — R4).
      - MINT:   no exact match — content_key is the fresh make_content_key
        (GTIN-aware when the door plumbed one through, R3) and
        product_group_id its deterministic singleton pg.
      - FLAG:   a conflict (GTIN disagreement, or a brand-fragmentation
        conflict at a first-party door) was enqueued for identity review; the
        door PROCEEDS with the returned (fresh) identity — flagged, not silent.
      - SKIP:   observed-data doors only — a brand-fragmentation conflict; the
        door must not insert (review enqueued).

    Fail-open by construction: any internal error returns MINT with today's
    hash-computed identity. Never raises.
    """
    ctx = dict(merchant_ctx or {})
    merchant_id = str(ctx.get("merchant_id") or "") or None

    from services.catalog_identity import make_content_key, normalize_gtin

    gtin_norm = normalize_gtin(gtin)
    ck_gtin = make_content_key(brand, title, gtin) if gtin_norm else None
    ck_plain = make_content_key(brand, title)
    ck_fresh = ck_gtin or ck_plain  # what MINT/FLAG hand back (R3 GTIN-aware)

    def _base_detail() -> Dict[str, Any]:
        return {
            "brand": brand,
            "title": title,
            "gtin13": gtin_norm or None,
            "canonical_url": canonical_url,
            "source_product_id": source_product_id,
            "ck_gtin_form": ck_gtin,
            "ck_plain_form": ck_plain,
        }

    if not ck_plain and not ck_gtin:
        # No brand/title identity to resolve — the door keeps today's
        # content_key-NULL behavior (honest absence; pg stays NULL too).
        return await _finish(
            action=ACTION_MINT, content_key=None, product_group_id=None,
            matcher=None, door=door, merchant_ctx=ctx,
            detail={**_base_detail(), "reason": "no_identity_inputs"},
        )

    try:
        # -- Tier-0a: GTIN, via the content_key forms (R3: GTIN'd form first,
        # GTIN-less fallback second so a GTIN'd source still finds its legacy
        # GTIN-less twin instead of minting a parallel identity).
        if ck_gtin:
            rows = await _rows_by_content_key(ck_gtin, merchant_id)
            if rows:
                row = rows[0]
                ck = row.get("content_key") or ck_gtin
                return await _finish(
                    action=ACTION_ATTACH, content_key=ck,
                    product_group_id=await _attach_pg(row, ck),
                    matcher="content_key_gtin", door=door, merchant_ctx=ctx,
                    detail={
                        **_base_detail(),
                        "matched_product_key": row.get("product_key"),
                        "deposit_basis": _deposit_basis(brand, title, gtin, ck),
                    },
                    attach=_attach_info(row, merchant_id),
                )

        if ck_plain:
            rows = await _rows_by_content_key(ck_plain, merchant_id)
            if rows:
                row = rows[0]
                ck = row.get("content_key") or ck_plain
                if gtin_norm:
                    # R3 disagreement check: the brand+title twin is already
                    # known under a DIFFERENT GTIN → two physical products
                    # colliding on brand+title. FLAG, never a silent attach.
                    known = await _known_gtin13_for_content_key(ck)
                    if known and known != gtin_norm:
                        detail = {
                            **_base_detail(),
                            "reason": "gtin_disagreement_same_brand_title",
                            "conflict_product_key": row.get("product_key"),
                            "conflict_content_key": ck,
                            "known_gtin13": known,
                        }
                        await _flag_review(
                            door, ctx, ck_fresh, "gtin_disagreement", detail
                        )
                        return await _finish(
                            action=ACTION_FLAG, content_key=ck_fresh,
                            product_group_id=_singleton_pg(ck_fresh),
                            matcher="gtin_disagreement", door=door,
                            merchant_ctx=ctx, detail=detail,
                        )
                return await _finish(
                    action=ACTION_ATTACH, content_key=ck,
                    product_group_id=await _attach_pg(row, ck),
                    matcher=(
                        "content_key_brand_title_gtin_fallback"
                        if gtin_norm else "content_key_brand_title"
                    ),
                    door=door, merchant_ctx=ctx,
                    detail={
                        **_base_detail(),
                        "matched_product_key": row.get("product_key"),
                        "deposit_basis": _deposit_basis(brand, title, gtin, ck),
                    },
                    attach=_attach_info(row, merchant_id),
                )

        # -- Tier-0a': reverse GTIN disagreement — no content_key hit, but the
        # GTIN is already known under some other brand+title identity. FLAG.
        if gtin_norm:
            other = await _content_key_for_gtin13(gtin_norm)
            if other and other.get("content_key") not in {ck_gtin, ck_plain}:
                detail = {
                    **_base_detail(),
                    "reason": "gtin_conflict_different_brand_title",
                    "conflict_content_key": other.get("content_key"),
                    "conflict_brand": other.get("brand"),
                    "conflict_title": other.get("title"),
                }
                await _flag_review(door, ctx, ck_fresh, "gtin_conflict", detail)
                return await _finish(
                    action=ACTION_FLAG, content_key=ck_fresh,
                    product_group_id=_singleton_pg(ck_fresh),
                    matcher="gtin_conflict", door=door, merchant_ctx=ctx,
                    detail=detail,
                )

        # -- Tier-0b: canonical_url exact (the ER gate's matcher, unchanged:
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
                        attach=_attach_info(row, merchant_id),
                    )

        # -- Tier-0c: source_product_id exact, SAME-merchant scope only.
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
                        attach=_attach_info(row, merchant_id),
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
                    "content_key": ck_fresh,
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
                action=ACTION_SKIP, content_key=ck_fresh,
                product_group_id=None, matcher="brand_host_fragmentation",
                door=door, merchant_ctx=ctx,
                detail={
                    **_base_detail(),
                    "reason": "brand_fragmentation",
                    **guard_detail,
                },
            )
        if guard_action == "flag":
            return await _finish(
                action=ACTION_FLAG, content_key=ck_fresh,
                product_group_id=_singleton_pg(ck_fresh),
                matcher="brand_host_fragmentation", door=door, merchant_ctx=ctx,
                detail={
                    **_base_detail(),
                    "reason": "brand_fragmentation",
                    **guard_detail,
                },
            )

        return await _finish(
            action=ACTION_MINT, content_key=ck_fresh,
            product_group_id=_singleton_pg(ck_fresh), matcher=None,
            door=door, merchant_ctx=ctx,
            detail={
                **_base_detail(),
                "deposit_basis": _deposit_basis(brand, title, gtin, ck_fresh),
            },
        )
    except Exception as exc:  # noqa: BLE001 — fail-open: never block intake
        logger.warning(
            "resolve_or_attach_content_identity failed door=%s merchant=%s: %s",
            door, merchant_id, str(exc)[:300],
        )
        return {
            "content_key": ck_fresh,
            "product_group_id": None,
            "action": ACTION_MINT,
            "evidence": {
                "door": door,
                "action": ACTION_MINT,
                "matcher": None,
                "merchant_id": merchant_id,
                "evidence": {"reason": "error", "error": str(exc)[:300]},
            },
            "attach": None,
        }
