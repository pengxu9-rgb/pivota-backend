"""LLM-driven product-identity reviewer (DeepSeek).

The commerce index resolves most merchant listings automatically, but real-world
title cosmetics (e.g. a Shopify title "Triple Shine Grape - Ownist" vs the
canonical "Triple Shine Grape") fork the content_key / soft cluster, so the
listing lands at identity_status='review_required' (~0.62) and can't deposit
into the index. Human review of every such case doesn't scale.

This reviewer is the scalable matching layer: for each review_required merchant
listing it finds the APPROVED canonical candidate(s) of the same brand, asks
DeepSeek "is this the same physical product?", and on a confident YES records a
`force_exact_group` override (created_by='llm:deepseek') + recomputes
catalog_row_trust. The existing override pipeline (catalog_trust_policy.derive_trust
honors force_exact_group → confidence 1.0) then promotes the listing so the audit
deposit gate accepts it (basis identity_high_conf) — no change to the deposit path.

Dry-run by default (judges + prints, writes nothing). --apply records overrides +
recomputes trust.

Local run against prod (public proxy + small pool):
  railway run bash -lc 'DB_POOL_MIN_SIZE=1 DB_POOL_MAX_SIZE=1 \
    DB_POOL_ACQUIRE_TIMEOUT_SECONDS=30 DATABASE_URL="$DATABASE_PUBLIC_URL" \
    .venv/bin/python scripts/llm_identity_review.py --brand Ownist'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402
from services.catalog_row_trust_upserter import upsert_catalog_row_trust  # noqa: E402
from services.llm_providers.deepseek_probe import _call_deepseek_chat  # noqa: E402

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are a product-identity adjudicator for a commerce index. You decide "
    "whether a MERCHANT listing and a CANONICAL catalog entry refer to the SAME "
    "physical product (same brand, same item, ignoring cosmetic title differences "
    "like the brand name appended/prepended, retailer suffixes, or word order). "
    "Different size/shade/flavor variants of the same line are NOT the same exact "
    "item unless the variant matches. Be conservative: if unsure, say not a match. "
    'Respond ONLY as JSON: {"match_index": <int index of the matching candidate or '
    '-1 if none>, "same_product": <bool>, "confidence": <0.0-1.0>, "reason": '
    '"<one sentence>"}.'
)


def _deepseek_cfg() -> Dict[str, str]:
    try:
        from config.settings import settings  # type: ignore
        api_key = getattr(settings, "deepseek_api_key", None) or os.getenv("DEEPSEEK_API_KEY", "")
        base_url = getattr(settings, "deepseek_api_base_url", None) or os.getenv(
            "DEEPSEEK_API_BASE_URL", "https://api.deepseek.com"
        )
        model = getattr(settings, "deepseek_model", None) or os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    except Exception:  # noqa: BLE001
        api_key = os.getenv("DEEPSEEK_API_KEY", "")
        base_url = os.getenv("DEEPSEEK_API_BASE_URL", "https://api.deepseek.com")
        model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    return {"api_key": api_key, "base_url": base_url, "model": model}


async def _review_required_listings(brand: str, limit: int) -> List[Dict[str, Any]]:
    """review_required (or unresolved, non-depositable) merchant listings for a brand,
    excluding the external_seed canonical source."""
    rows = await database.fetch_all(
        """
        SELECT cp.product_key, cp.merchant_id, cp.brand, cp.title, cp.content_key,
               crt.source_listing_ref, crt.identity_status, crt.identity_confidence
        FROM catalog_products cp
        JOIN catalog_row_trust crt ON crt.product_key = cp.product_key
        WHERE cp.brand ILIKE :brand
          AND cp.merchant_id <> 'external_seed'
          AND COALESCE(crt.identity_confidence, 0) < 0.85
          AND crt.source_listing_ref IS NOT NULL
          -- Only override a merchant's OWN listing. Some rows point at an
          -- external_seed ref (seed-attached products); an override there would
          -- wrongly mutate the canonical seed, not the merchant listing.
          AND crt.source_listing_ref NOT LIKE 'external_seed:%'
        ORDER BY cp.merchant_id, cp.title
        LIMIT :limit
        """,
        {"brand": brand, "limit": limit},
    )
    return [dict(r) for r in (rows or [])]


async def _approved_candidates(brand: str) -> List[Dict[str, Any]]:
    """Approved canonical entries of the same brand (the match targets)."""
    rows = await database.fetch_all(
        """
        SELECT cp.product_key, cp.brand, cp.title, cp.content_key,
               crt.matched_sellable_item_group_id AS sig, crt.identity_confidence
        FROM catalog_products cp
        JOIN catalog_row_trust crt ON crt.product_key = cp.product_key
        WHERE cp.brand ILIKE :brand
          AND crt.identity_status = 'approved'
        ORDER BY cp.title
        """,
        {"brand": brand},
    )
    return [dict(r) for r in (rows or [])]


async def _judge(listing: Dict[str, Any], candidates: List[Dict[str, Any]], cfg: Dict[str, str]) -> Dict[str, Any]:
    cand_lines = "\n".join(
        f"  [{i}] brand={c['brand']!r} title={c['title']!r}" for i, c in enumerate(candidates)
    )
    user_message = (
        f"MERCHANT listing:\n  brand={listing['brand']!r} title={listing['title']!r}\n\n"
        f"CANDIDATE canonical entries:\n{cand_lines}\n\n"
        "Which candidate (if any) is the SAME physical product as the merchant listing?"
    )
    resp = await _call_deepseek_chat(
        api_key=cfg["api_key"], base_url=cfg["base_url"], model=cfg["model"],
        system_prompt=_SYSTEM_PROMPT, user_message=user_message,
        timeout_s=30.0, enable_web_search=False,
    )
    content = resp["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    usage = resp.get("usage") or {}
    parsed["_tokens"] = {"in": usage.get("prompt_tokens"), "out": usage.get("completion_tokens")}
    return parsed


async def _apply_override(listing: Dict[str, Any], canonical: Dict[str, Any], judgment: Dict[str, Any]) -> None:
    """Record a force_exact_group override + recompute the trust row."""
    override_id = "ov_" + uuid.uuid4().hex[:24]
    payload = {
        "matched_content_key": canonical.get("content_key"),
        "target_sellable_item_group_id": canonical.get("sig"),
        "matched_product_key": canonical.get("product_key"),
        "llm_reason": judgment.get("reason"),
        "llm_confidence": judgment.get("confidence"),
        "reviewer": "deepseek",
    }
    await database.execute(
        """
        INSERT INTO pdp_identity_override
          (id, source_listing_ref, action_type, payload, created_by, active, created_at, updated_at)
        VALUES (:id, :ref, 'force_exact_group', CAST(:payload AS JSONB), 'llm:deepseek', TRUE, now(), now())
        ON CONFLICT (id) DO NOTHING
        """,
        {"id": override_id, "ref": listing["source_listing_ref"], "payload": json.dumps(payload)},
    )
    await upsert_catalog_row_trust(db=database, product_key=listing["product_key"])


async def _drive(args: argparse.Namespace) -> Dict[str, Any]:
    await database.connect()
    try:
        cfg = _deepseek_cfg()
        if not cfg["api_key"]:
            return {"error": "DEEPSEEK_API_KEY not configured"}
        listings = await _review_required_listings(args.brand, args.limit)
        candidates = await _approved_candidates(args.brand)
        report: Dict[str, Any] = {
            "apply": bool(args.apply), "brand": args.brand,
            "listings_reviewed": 0, "matched": 0, "approved_written": 0,
            "no_match": 0, "decisions": [],
        }
        if not candidates:
            report["error"] = f"no approved canonical candidates for brand {args.brand!r}"
            return report
        for listing in listings:
            report["listings_reviewed"] += 1
            try:
                j = await _judge(listing, candidates, cfg)
            except Exception as exc:  # noqa: BLE001
                report["decisions"].append({"listing": listing["title"], "error": str(exc)[:160]})
                continue
            idx = j.get("match_index", -1)
            is_match = bool(j.get("same_product")) and isinstance(idx, int) and 0 <= idx < len(candidates) \
                and float(j.get("confidence") or 0) >= args.min_confidence
            decision = {
                "merchant_id": listing["merchant_id"],
                "listing_title": listing["title"],
                "source_listing_ref": listing["source_listing_ref"],
                "current_confidence": float(listing["identity_confidence"] or 0),
                "matched_canonical": candidates[idx]["title"] if is_match else None,
                "same_product": j.get("same_product"),
                "llm_confidence": j.get("confidence"),
                "reason": j.get("reason"),
                "tokens": j.get("_tokens"),
            }
            if is_match:
                report["matched"] += 1
                if args.apply:
                    await _apply_override(listing, candidates[idx], j)
                    decision["override"] = "written (force_exact_group)"
                    report["approved_written"] += 1
            else:
                report["no_match"] += 1
            report["decisions"].append(decision)
        return report
    finally:
        await database.disconnect()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--brand", required=True, help="brand to review (ILIKE match)")
    ap.add_argument("--apply", action="store_true", help="write overrides (default: dry-run)")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--min-confidence", type=float, default=0.75, help="min LLM confidence to approve")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    print(json.dumps(asyncio.run(_drive(args)), indent=2, default=str))


if __name__ == "__main__":
    main()
