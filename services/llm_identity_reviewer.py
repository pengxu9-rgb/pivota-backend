"""LLM-driven product-identity reviewer (DeepSeek) — reusable core + scheduler tick.

The commerce index resolves most merchant listings automatically, but real-world
title cosmetics (a Shopify title "Triple Shine Grape - Ownist" vs the canonical
"Triple Shine Grape") fork the content_key / soft cluster, so the listing lands at
identity_status='review_required' (~0.62), is queued in pdp_identity_review_queue,
and never deposits into the index. Human review of every case doesn't scale.

This is the scalable matching layer. For each review_required merchant listing it
finds the APPROVED canonical candidate(s) of the same brand, asks DeepSeek "is this
the same physical product?" (brand / title / url / image / description evidence),
and on a confident YES records a `force_exact_group` override (created_by=
'llm:deepseek') + recomputes catalog_row_trust. The existing override pipeline
(catalog_trust_policy.derive_trust honors force_exact_group → confidence 1.0) then
promotes the listing so the audit deposit gate accepts it — no deposit-path change.

Abstain policy: auto-approve ONLY when same_product AND llm_confidence >=
min_confidence. Matched-but-unsure → 'llm_uncertain' (human escalation); not a
match → 'resolved_llm_rejected'; same-brand-no-canonical (e.g. brandless AliExpress
products, which have no brand anchor at all) → 'llm_no_candidate'. Nothing is
force-approved on thin evidence.

The scheduler tick is gated OFF by default (LLM_IDENTITY_REVIEW_ENABLED) — like the
other autonomous-mutation features — so deploying the code starts nothing.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from collections import defaultdict
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from db.database import database
from services.catalog_row_trust_upserter import upsert_catalog_row_trust
from services.llm_providers.deepseek_probe import _call_deepseek_chat

logger = logging.getLogger(__name__)


async def _regroup_content_key(product_key: str, old_ck: Optional[str], new_ck: Optional[str]) -> bool:
    """Cross-seller grouping: re-point a listing's content_key to the canonical so it
    groups with it in agent_pdp_view (which groups strictly by content_key), then
    cascade the served PDP + serving eligibility for both the new and old clusters.

    Durable counterpart: catalog_sync_service consults the same override on every
    sync, so this survives re-ingest. No-op when there's nothing to change."""
    if not new_ck or not old_ck or old_ck == new_ck:
        return False
    await database.execute(
        "UPDATE catalog_products SET content_key=:new WHERE product_key=:pk",
        {"new": new_ck, "pk": product_key},
    )
    await upsert_catalog_row_trust(db=database, product_key=product_key)
    from services.agent_pdp_view_assembler import refresh_agent_pdp_view_for_content_key
    from services.index_pipeline_state_service import recompute_serving_eligibility
    for ck in (new_ck, old_ck):
        try:
            await refresh_agent_pdp_view_for_content_key(ck, refresh_source="llm_identity_grouping", db=database)
        except Exception:  # noqa: BLE001
            pass
        try:
            await recompute_serving_eligibility(ck, reason="llm_identity_grouping")
        except Exception:  # noqa: BLE001
            pass
    return True


_SEED_PREFIX = "external_seed:"

_SYSTEM_PROMPT = (
    "You are a product-identity adjudicator for a commerce index. You decide "
    "whether a MERCHANT listing and one of several CANONICAL catalog entries refer "
    "to the SAME physical product: same brand, same item, and same variant "
    "(size/shade/flavor/scent). Ignore cosmetic title differences — brand name "
    "appended/prepended, retailer suffixes, word order, punctuation. A DIFFERENT "
    "variant of the same product line is NOT a match. Be conservative: if the "
    "evidence is thin or ambiguous, do NOT claim a match. "
    'Respond ONLY as JSON: {"match_index": <int index of the matching candidate, or '
    '-1 if none match>, "same_product": <bool>, "confidence": <0.0-1.0>, "reason": '
    '"<one short sentence>"}.'
)

_LISTING_COLS = """
  crt.source_listing_ref, cp.product_key, cp.merchant_id, cp.brand, cp.title,
  cp.content_key, cp.image_url, cp.canonical_url, LEFT(cp.description, 240) AS description,
  crt.identity_confidence
"""


def deepseek_cfg() -> Dict[str, str]:
    try:
        from config.settings import settings  # type: ignore
        api_key = getattr(settings, "deepseek_api_key", None) or os.getenv("DEEPSEEK_API_KEY", "")
        base_url = getattr(settings, "deepseek_api_base_url", None) or os.getenv(
            "DEEPSEEK_API_BASE_URL", "https://api.deepseek.com")
        model = getattr(settings, "deepseek_model", None) or os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    except Exception:  # noqa: BLE001
        api_key = os.getenv("DEEPSEEK_API_KEY", "")
        base_url = os.getenv("DEEPSEEK_API_BASE_URL", "https://api.deepseek.com")
        model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    return {"api_key": api_key, "base_url": base_url, "model": model}


def _norm_brand(b: Optional[str]) -> str:
    return (b or "").strip().lower()


async def _fetch_queue_listings(limit: int, offset: int) -> List[Dict[str, Any]]:
    rows = await database.fetch_all(
        f"""
        SELECT q.id AS queue_id, {_LISTING_COLS}
        FROM pdp_identity_review_queue q
        JOIN catalog_row_trust crt ON crt.source_listing_ref = q.source_listing_ref
        JOIN catalog_products cp ON cp.product_key = crt.product_key
        WHERE q.status = 'pending'
          AND q.source_listing_ref NOT LIKE :seed
          AND cp.merchant_id <> 'external_seed'
          AND COALESCE(crt.identity_confidence, 0) < 0.85
        ORDER BY q.created_at
        LIMIT :limit OFFSET :offset
        """,
        {"seed": _SEED_PREFIX + "%", "limit": limit, "offset": offset},
    )
    return [dict(r) for r in (rows or [])]


async def _fetch_brand_listings(brand: str, limit: int) -> List[Dict[str, Any]]:
    rows = await database.fetch_all(
        f"""
        SELECT NULL AS queue_id, {_LISTING_COLS}
        FROM catalog_products cp
        JOIN catalog_row_trust crt ON crt.product_key = cp.product_key
        WHERE cp.brand ILIKE :brand
          AND cp.merchant_id <> 'external_seed'
          AND COALESCE(crt.identity_confidence, 0) < 0.85
          AND crt.source_listing_ref IS NOT NULL
          AND crt.source_listing_ref NOT LIKE :seed
        ORDER BY cp.merchant_id, cp.title
        LIMIT :limit
        """,
        {"brand": brand, "seed": _SEED_PREFIX + "%", "limit": limit},
    )
    return [dict(r) for r in (rows or [])]


async def _fetch_candidates(brand_norms: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    """Approved canonical entries grouped by normalized brand (the match targets)."""
    if not brand_norms:
        return {}
    rows = await database.fetch_all(
        """
        SELECT cp.brand, cp.product_key, cp.title, cp.content_key,
               cp.image_url, cp.canonical_url, LEFT(cp.description, 240) AS description,
               crt.matched_sellable_item_group_id AS sig
        FROM catalog_products cp
        JOIN catalog_row_trust crt ON crt.product_key = cp.product_key
        WHERE crt.identity_status = 'approved'
          AND lower(btrim(cp.brand)) = ANY(:brands)
        ORDER BY cp.title
        """,
        {"brands": brand_norms},
    )
    out: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows or []:
        out[_norm_brand(r["brand"])].append(dict(r))
    return out


def _fmt_product(p: Dict[str, Any]) -> str:
    bits = [f"brand={p.get('brand')!r}", f"title={p.get('title')!r}"]
    if p.get("canonical_url"):
        bits.append(f"url={p['canonical_url']}")
    if p.get("image_url"):
        bits.append(f"image={p['image_url']}")
    if p.get("description"):
        bits.append(f"desc={p['description']!r}")
    return " ".join(bits)


async def _judge(listing: Dict[str, Any], candidates: List[Dict[str, Any]], cfg: Dict[str, str]) -> Dict[str, Any]:
    cand_lines = "\n".join(f"  [{i}] {_fmt_product(c)}" for i, c in enumerate(candidates[:8]))
    user_message = (
        f"MERCHANT listing:\n  {_fmt_product(listing)}\n\n"
        f"CANDIDATE canonical entries:\n{cand_lines}\n\n"
        "Which candidate (if any) is the SAME physical product (same variant) as the "
        "merchant listing? If none clearly match, return match_index -1."
    )
    resp = await _call_deepseek_chat(
        api_key=cfg["api_key"], base_url=cfg["base_url"], model=cfg["model"],
        system_prompt=_SYSTEM_PROMPT, user_message=user_message,
        timeout_s=30.0, enable_web_search=False,
    )
    parsed = json.loads(resp["choices"][0]["message"]["content"])
    usage = resp.get("usage") or {}
    parsed["_tokens"] = {"in": usage.get("prompt_tokens"), "out": usage.get("completion_tokens")}
    return parsed


async def _apply_decision(listing: Dict[str, Any], canonical: Dict[str, Any], j: Dict[str, Any]) -> None:
    payload = {
        "matched_content_key": canonical.get("content_key"),
        "target_sellable_item_group_id": canonical.get("sig"),
        "matched_product_key": canonical.get("product_key"),
        "llm_reason": j.get("reason"),
        "llm_confidence": j.get("confidence"),
        "reviewer": "deepseek",
    }
    await database.execute(
        """
        INSERT INTO pdp_identity_override
          (id, source_listing_ref, action_type, payload, created_by, active, created_at, updated_at)
        VALUES (:id, :ref, 'force_exact_group', CAST(:payload AS JSONB), 'llm:deepseek', TRUE, now(), now())
        ON CONFLICT (id) DO NOTHING
        """,
        {"id": "ov_" + uuid.uuid4().hex[:24], "ref": listing["source_listing_ref"], "payload": json.dumps(payload)},
    )
    # Cross-seller grouping: adopt the canonical content_key now (immediate effect);
    # the sync hook keeps it durable across re-ingest. _regroup_content_key also
    # recomputes trust (so the override's force_exact_group → confidence 1.0 lands)
    # and cascades the served PDP. If nothing to regroup, fall back to a plain trust
    # recompute so the promotion still applies.
    if not await _regroup_content_key(
        listing["product_key"], listing.get("content_key"), canonical.get("content_key")
    ):
        await upsert_catalog_row_trust(db=database, product_key=listing["product_key"])


async def reconcile_grouping(*, limit: int, apply: bool) -> Dict[str, Any]:
    """Apply cross-seller grouping to listings whose active force_exact_group override
    names a canonical content_key they don't yet carry (e.g. overrides written before
    grouping shipped). Re-points content_key + cascades. Dry-run unless apply=True."""
    rows = await database.fetch_all(
        """
        SELECT crt.product_key, cp.merchant_id, cp.title,
               cp.content_key AS old_ck, o.payload->>'matched_content_key' AS new_ck
        FROM pdp_identity_override o
        JOIN catalog_row_trust crt ON crt.source_listing_ref = o.source_listing_ref
        JOIN catalog_products cp ON cp.product_key = crt.product_key
        WHERE o.active AND o.action_type = 'force_exact_group'
          AND o.payload->>'matched_content_key' IS NOT NULL
          AND cp.content_key <> o.payload->>'matched_content_key'
        ORDER BY cp.merchant_id
        LIMIT :limit
        """,
        {"limit": limit},
    )
    report: Dict[str, Any] = {"apply": bool(apply), "to_regroup": len(rows or []), "regrouped": 0, "samples": []}
    for r in rows or []:
        if apply:
            try:
                if await _regroup_content_key(r["product_key"], r["old_ck"], r["new_ck"]):
                    report["regrouped"] += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("reconcile_grouping failed for %s: %s", r["product_key"], str(exc)[:160])
        if len(report["samples"]) < 12:
            report["samples"].append({"merchant_id": r["merchant_id"], "title": r["title"],
                                      "old_ck": r["old_ck"], "new_ck": r["new_ck"]})
    return report


async def _set_queue_status(queue_id: Optional[str], status: str, note: Optional[str]) -> None:
    if not queue_id:
        return
    await database.execute(
        "UPDATE pdp_identity_review_queue SET status=:s, review_notes=:n, updated_at=now() WHERE id=:i",
        {"s": status, "n": (note or "")[:500], "i": queue_id},
    )


_FIRST_PARTY_SYSTEM_PROMPT = (
    "You are a product-identity adjudicator. A merchant sells a product under a brand "
    "on their OWN store, and that brand is NOT yet a canonical in our index. Decide "
    "whether the merchant is the FIRST-PARTY OWNER of the brand — i.e. it's their own "
    "brand that they created and sell (common for small DTC sellers who source a "
    "product, e.g. from AliExpress, and put their own brand on it) — as opposed to a "
    "RESELLER / dropshipper of someone else's brand, or a generic product with no real "
    "brand. Signals: the brand is distinctive (not a generic English word), appears in "
    "the title, and the store URL corresponds to the brand. Be conservative: if it "
    "looks like a reseller of a recognizable third-party brand, or a no-name generic "
    "product, say first_party=false. "
    'Respond ONLY as JSON: {"first_party": <bool>, "confidence": <0.0-1.0>, "reason": "<one sentence>"}.'
)


def _domain_contains_brand(canonical_url: Optional[str], brand: Optional[str]) -> bool:
    """Inference signal (mirrors chooseSourceTier): does the listing's own store
    domain correspond to its brand? True for a brand selling on its own domain
    (ownist.com / ownist.myshopify.com); False for a multi-brand reseller whose
    domain doesn't reflect the brand."""
    b = re.sub(r"[^a-z0-9]", "", (brand or "").lower())
    if not b or not canonical_url:
        return False
    try:
        host = urlparse(canonical_url if "://" in canonical_url else "https://" + canonical_url).netloc.lower()
    except Exception:  # noqa: BLE001
        return False
    host = re.sub(r"[^a-z0-9]", "", host.split(":")[0])
    return bool(host) and b in host


async def _fetch_owned_brands(merchant_ids: List[str]) -> Dict[str, set]:
    """Declared brand ownership per merchant (decision: declaration is the durable
    upgrade). v1 has no onboarding capture, so this reads an OPTIONAL
    metadata_json->'owned_brands' and is empty for ~all merchants today — the
    declared-auto path is wired but dormant until capture lands."""
    out: Dict[str, set] = {}
    if not merchant_ids:
        return out
    try:
        rows = await database.fetch_all(
            "SELECT merchant_id, metadata_json->'owned_brands' AS owned "
            "FROM catalog_merchants WHERE merchant_id = ANY(:ids)",
            {"ids": merchant_ids},
        )
        for r in rows or []:
            owned = r["owned"]
            if isinstance(owned, str):
                try:
                    owned = json.loads(owned)
                except Exception:  # noqa: BLE001
                    owned = None
            if isinstance(owned, list):
                out[r["merchant_id"]] = {_norm_brand(str(b)) for b in owned if b}
    except Exception as exc:  # noqa: BLE001
        logger.warning("_fetch_owned_brands failed: %s", str(exc)[:160])
    return out


async def _judge_first_party(listing: Dict[str, Any], cfg: Dict[str, str]) -> Dict[str, Any]:
    user_message = (
        f"MERCHANT listing (brand has no canonical yet):\n  {_fmt_product(listing)}\n\n"
        "Is the merchant the first-party owner of this brand (their own brand), or a "
        "reseller / generic? Note: the store domain appears to correspond to the brand."
    )
    resp = await _call_deepseek_chat(
        api_key=cfg["api_key"], base_url=cfg["base_url"], model=cfg["model"],
        system_prompt=_FIRST_PARTY_SYSTEM_PROMPT, user_message=user_message,
        timeout_s=30.0, enable_web_search=False,
    )
    parsed = json.loads(resp["choices"][0]["message"]["content"])
    parsed["_tokens"] = {"out": (resp.get("usage") or {}).get("completion_tokens")}
    return parsed


async def _apply_first_party(listing: Dict[str, Any], basis: str, confidence: Any, reason: Optional[str]) -> None:
    """Record an approve_first_party_canonical override (CREATE): the merchant's own
    listing becomes the approved canonical for its brand. Recompute trust so the
    deposit gate accepts it (derive_trust → approved/0.9)."""
    # Basis-aware brand-tier floor: declared ownership is self-asserted-strong;
    # inferred+LLM is weaker (just clears the 0.85 gate). derive_trust reads this
    # from the payload (clamped to [0.85, 0.95]).
    fp_confidence = 0.92 if basis == "declared" else 0.87
    payload = {
        "basis": basis, "first_party_confidence": fp_confidence,
        "llm_confidence": confidence, "llm_reason": reason, "reviewer": "deepseek",
    }
    await database.execute(
        """
        INSERT INTO pdp_identity_override
          (id, source_listing_ref, action_type, payload, created_by, active, created_at, updated_at)
        VALUES (:id, :ref, 'approve_first_party_canonical', CAST(:payload AS JSONB), 'llm:deepseek', TRUE, now(), now())
        ON CONFLICT (id) DO NOTHING
        """,
        {"id": "ov_" + uuid.uuid4().hex[:24], "ref": listing["source_listing_ref"], "payload": json.dumps(payload)},
    )
    await upsert_catalog_row_trust(db=database, product_key=listing["product_key"])


async def review_listings(
    listings: List[Dict[str, Any]],
    *,
    min_confidence: float,
    apply: bool,
    samples: int = 8,
) -> Dict[str, Any]:
    """Judge a batch of review_required listings and (optionally) apply approvals.

    DB work is phased: candidates are fetched up front, the LLM phase touches no DB,
    and overrides + queue updates are applied at the end — so a flaky DB connection
    can't drop mid-run. The caller owns connect()/disconnect().
    """
    cfg = deepseek_cfg()
    if not cfg["api_key"]:
        return {"error": "DEEPSEEK_API_KEY not configured"}

    brand_norms = sorted({_norm_brand(l["brand"]) for l in listings if l.get("brand")})
    candidates_by_brand = await _fetch_candidates(brand_norms)
    owned_brands = await _fetch_owned_brands(sorted({l["merchant_id"] for l in listings if l.get("merchant_id")}))

    report: Dict[str, Any] = {
        "apply": bool(apply), "listings": len(listings), "no_candidate": 0, "judged": 0,
        "approved": 0, "rejected": 0, "uncertain": 0, "errors": 0,
        "first_party_created": 0, "tokens_out": 0, "samples": [],
    }
    writes: List[Dict[str, Any]] = []
    fp_writes: List[Dict[str, Any]] = []
    status_updates: List[Dict[str, Any]] = []

    for listing in listings:
        cands = candidates_by_brand.get(_norm_brand(listing["brand"]), [])
        if not cands:
            # No existing canonical for this brand → first-party CREATE candidate.
            # The "no canonical" condition IS the guardrail: a dropshipper of a known
            # brand can't reach here (that brand would already have a canonical → MATCH).
            brand_n = _norm_brand(listing.get("brand"))
            declared = brand_n in owned_brands.get(listing.get("merchant_id"), set())
            inferred = _domain_contains_brand(listing.get("canonical_url"), listing.get("brand"))
            if not brand_n or (not declared and not inferred):
                report["no_candidate"] += 1
                status_updates.append({"queue_id": listing.get("queue_id"), "status": "llm_no_candidate",
                                       "note": "no canonical; not a first-party brand owner (no declaration / no store-domain match)"})
                continue
            if declared:
                # Decision 4: auto-CREATE for declared brand ownership (no LLM).
                report["first_party_created"] += 1
                fp_writes.append({"listing": listing, "basis": "declared", "confidence": 1.0,
                                  "reason": "merchant-declared brand ownership"})
                status_updates.append({"queue_id": listing.get("queue_id"), "status": "resolved_first_party",
                                       "note": "declared brand ownership"})
                continue
            # Decision 4: inferred-only (store-domain match) → LLM-assisted confirm.
            try:
                fj = await _judge_first_party(listing, cfg)
            except Exception as exc:  # noqa: BLE001
                report["errors"] += 1
                report["samples"].append({"title": listing.get("title"), "error": str(exc)[:140]})
                continue
            report["tokens_out"] += (fj.get("_tokens") or {}).get("out") or 0
            fp = bool(fj.get("first_party")) and float(fj.get("confidence") or 0) >= min_confidence
            if fp:
                report["first_party_created"] += 1
                fp_writes.append({"listing": listing, "basis": "inferred_llm",
                                  "confidence": fj.get("confidence"), "reason": fj.get("reason")})
                status_updates.append({"queue_id": listing.get("queue_id"), "status": "resolved_first_party",
                                       "note": fj.get("reason")})
            else:
                report["no_candidate"] += 1
                status_updates.append({"queue_id": listing.get("queue_id"), "status": "first_party_rejected",
                                       "note": fj.get("reason")})
            if len(report["samples"]) < samples:
                report["samples"].append({"merchant_id": listing.get("merchant_id"), "title": listing.get("title"),
                                          "first_party": fj.get("first_party"), "confidence": fj.get("confidence"),
                                          "reason": fj.get("reason")})
            continue
        try:
            j = await _judge(listing, cands, cfg)
        except Exception as exc:  # noqa: BLE001
            report["errors"] += 1
            report["samples"].append({"title": listing.get("title"), "error": str(exc)[:140]})
            continue
        report["judged"] += 1
        report["tokens_out"] += (j.get("_tokens") or {}).get("out") or 0
        idx = j.get("match_index", -1)
        conf = float(j.get("confidence") or 0)
        same = bool(j.get("same_product")) and isinstance(idx, int) and 0 <= idx < len(cands)
        if same and conf >= min_confidence:
            report["approved"] += 1
            writes.append({"listing": listing, "canonical": cands[idx], "judgment": j})
            status_updates.append({"queue_id": listing.get("queue_id"), "status": "resolved_llm_approved",
                                   "note": j.get("reason")})
        elif same:
            report["uncertain"] += 1
            status_updates.append({"queue_id": listing.get("queue_id"), "status": "llm_uncertain",
                                   "note": f"conf {conf} < {min_confidence}: {j.get('reason')}"})
        else:
            report["rejected"] += 1
            status_updates.append({"queue_id": listing.get("queue_id"), "status": "resolved_llm_rejected",
                                   "note": j.get("reason")})
        if len(report["samples"]) < samples:
            report["samples"].append({
                "merchant_id": listing.get("merchant_id"), "title": listing.get("title"),
                "matched": cands[idx]["title"] if same else None,
                "same_product": j.get("same_product"), "confidence": conf, "reason": j.get("reason"),
            })

    if apply and (writes or fp_writes or status_updates):
        for w in writes:
            try:
                await _apply_decision(w["listing"], w["canonical"], w["judgment"])
            except Exception as exc:  # noqa: BLE001
                report["errors"] += 1
                logger.warning("llm_identity_reviewer apply failed for %s: %s",
                               w["listing"].get("source_listing_ref"), str(exc)[:160])
        for w in fp_writes:
            try:
                await _apply_first_party(w["listing"], w["basis"], w["confidence"], w["reason"])
            except Exception as exc:  # noqa: BLE001
                report["errors"] += 1
                logger.warning("llm_identity_reviewer first-party apply failed for %s: %s",
                               w["listing"].get("source_listing_ref"), str(exc)[:160])
        for u in status_updates:
            try:
                await _set_queue_status(u["queue_id"], u["status"], u["note"])
            except Exception:  # noqa: BLE001
                pass
    return report


async def drain_review_queue(*, limit: int, offset: int = 0, min_confidence: float, apply: bool) -> Dict[str, Any]:
    listings = await _fetch_queue_listings(limit, offset)
    out = await review_listings(listings, min_confidence=min_confidence, apply=apply)
    out["mode"] = "queue"
    return out


async def review_brand(*, brand: str, limit: int, min_confidence: float, apply: bool) -> Dict[str, Any]:
    listings = await _fetch_brand_listings(brand, limit)
    out = await review_listings(listings, min_confidence=min_confidence, apply=apply)
    out["mode"] = f"brand:{brand}"
    return out


# --------------------------------------------------------------------------- #
# Scheduler tick — gated OFF by default.
# --------------------------------------------------------------------------- #

def _tick_enabled() -> bool:
    return (os.getenv("LLM_IDENTITY_REVIEW_ENABLED") or "").strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


async def run_llm_identity_review_tick() -> None:
    """APScheduler-callable. No-op unless LLM_IDENTITY_REVIEW_ENABLED is set.

    Drains a bounded batch from pdp_identity_review_queue each fire. Autonomous, so
    it runs a STRICTER confidence bar than the manual CLI (default 0.9). Best-effort:
    failures are logged, never raised (a scheduler tick must not crash the loop)."""
    if not _tick_enabled():
        return
    cfg = deepseek_cfg()
    if not cfg["api_key"]:
        logger.warning("llm_identity_review_tick: DEEPSEEK_API_KEY unset; skipping")
        return
    batch = _env_int("LLM_IDENTITY_REVIEW_BATCH", 25)
    min_conf = _env_float("LLM_IDENTITY_REVIEW_MIN_CONFIDENCE", 0.9)
    try:
        report = await drain_review_queue(limit=batch, min_confidence=min_conf, apply=True)
        if report.get("listings"):
            logger.info("llm_identity_review_tick: %s", {k: v for k, v in report.items() if k != "samples"})
    except Exception:  # noqa: BLE001
        logger.exception("llm_identity_review_tick failed")
