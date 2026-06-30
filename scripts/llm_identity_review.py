"""LLM-driven product-identity reviewer (DeepSeek).

The commerce index resolves most merchant listings automatically, but real-world
title cosmetics (e.g. a Shopify title "Triple Shine Grape - Ownist" vs the
canonical "Triple Shine Grape") fork the content_key / soft cluster, so the
listing lands at identity_status='review_required' (~0.62) and is queued in
pdp_identity_review_queue without ever depositing into the index. Human review of
every such case doesn't scale.

This reviewer is the scalable matching layer. For each review_required merchant
listing it finds the APPROVED canonical candidate(s) of the same brand, asks
DeepSeek "is this the same physical product?" (with brand / title / url / image /
description evidence), and on a confident YES records a `force_exact_group`
override (created_by='llm:deepseek') + recomputes catalog_row_trust. The existing
override pipeline (catalog_trust_policy.derive_trust honors force_exact_group →
confidence 1.0) then promotes the listing so the audit deposit gate accepts it —
no change to the deposit path.

Abstain policy: approve ONLY when same_product AND llm_confidence >= --min-confidence.
Same-brand-no-candidate → 'llm_no_candidate'; matched-but-unsure → 'llm_uncertain'
(escalate to a human); not-a-match → 'resolved_llm_rejected'. Nothing is force-
approved on thin evidence.

Modes:
  --queue            drain pending pdp_identity_review_queue rows (all brands)
  --brand NAME       targeted: review review_required listings of one brand

Robustness: DB work is front-loaded (fetch) and back-loaded (apply) around the
long LLM phase, so the flaky Railway public proxy can't drop the connection mid-run.

Dry-run by default. --apply writes overrides + recomputes trust + updates the queue.

Local run against prod (DeepSeek key lives on the `web` service):
  railway run --service web bash -lc 'DB_POOL_MIN_SIZE=1 DB_POOL_MAX_SIZE=1 \
    DB_POOL_ACQUIRE_TIMEOUT_SECONDS=30 DATABASE_URL="$DATABASE_PUBLIC_URL" \
    .venv/bin/python scripts/llm_identity_review.py --queue --limit 30'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from collections import defaultdict
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402
from services.catalog_row_trust_upserter import upsert_catalog_row_trust  # noqa: E402
from services.llm_providers.deepseek_probe import _call_deepseek_chat  # noqa: E402

logger = logging.getLogger(__name__)

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

_SEED_PREFIX = "external_seed:"


def _deepseek_cfg() -> Dict[str, str]:
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


_LISTING_COLS = """
  crt.source_listing_ref, cp.product_key, cp.merchant_id, cp.brand, cp.title,
  cp.content_key, cp.image_url, cp.canonical_url, LEFT(cp.description, 240) AS description,
  crt.identity_confidence
"""


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


async def _apply_decision(d: Dict[str, Any]) -> None:
    """Write the force_exact_group override + recompute trust for an approved match."""
    listing, canonical, j = d["listing"], d["canonical"], d["judgment"]
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
    await upsert_catalog_row_trust(db=database, product_key=listing["product_key"])


async def _set_queue_status(queue_id: Optional[str], status: str, note: Optional[str]) -> None:
    if not queue_id:
        return
    await database.execute(
        "UPDATE pdp_identity_review_queue SET status=:s, review_notes=:n, updated_at=now() WHERE id=:i",
        {"s": status, "n": (note or "")[:500], "i": queue_id},
    )


async def _drive(args: argparse.Namespace) -> Dict[str, Any]:
    cfg = _deepseek_cfg()
    if not cfg["api_key"]:
        return {"error": "DEEPSEEK_API_KEY not configured"}

    # ---- Phase 1: fetch (short DB session) ----
    await database.connect()
    try:
        if args.queue:
            listings = await _fetch_queue_listings(args.limit, args.offset)
        else:
            listings = await _fetch_brand_listings(args.brand, args.limit)
        brand_norms = sorted({_norm_brand(l["brand"]) for l in listings if l.get("brand")})
        candidates_by_brand = await _fetch_candidates(brand_norms)
    finally:
        await database.disconnect()

    report: Dict[str, Any] = {
        "apply": bool(args.apply), "mode": "queue" if args.queue else f"brand:{args.brand}",
        "listings": len(listings), "no_candidate": 0, "judged": 0,
        "approved": 0, "rejected": 0, "uncertain": 0, "errors": 0,
        "tokens_out": 0, "samples": [],
    }
    pending_writes: List[Dict[str, Any]] = []
    status_updates: List[Dict[str, Any]] = []

    # ---- Phase 2: judge (LLM only, NO DB) ----
    for listing in listings:
        cands = candidates_by_brand.get(_norm_brand(listing["brand"]), [])
        if not cands:
            report["no_candidate"] += 1
            status_updates.append({"queue_id": listing.get("queue_id"), "status": "llm_no_candidate",
                                    "note": "no approved same-brand canonical to match against"})
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
        sample = {
            "merchant_id": listing.get("merchant_id"), "title": listing.get("title"),
            "matched": cands[idx]["title"] if same else None,
            "same_product": j.get("same_product"), "confidence": conf, "reason": j.get("reason"),
        }
        if same and conf >= args.min_confidence:
            report["approved"] += 1
            pending_writes.append({"listing": listing, "canonical": cands[idx], "judgment": j})
            status_updates.append({"queue_id": listing.get("queue_id"), "status": "resolved_llm_approved",
                                   "note": j.get("reason")})
        elif same:  # matched but below confidence bar → escalate
            report["uncertain"] += 1
            status_updates.append({"queue_id": listing.get("queue_id"), "status": "llm_uncertain",
                                   "note": f"conf {conf} < {args.min_confidence}: {j.get('reason')}"})
        else:
            report["rejected"] += 1
            status_updates.append({"queue_id": listing.get("queue_id"), "status": "resolved_llm_rejected",
                                   "note": j.get("reason")})
        if len(report["samples"]) < (args.samples or 8):
            report["samples"].append(sample)

    # ---- Phase 3: apply (short DB session) ----
    if args.apply and (pending_writes or status_updates):
        await database.connect()
        try:
            for w in pending_writes:
                try:
                    await _apply_decision(w)
                except Exception as exc:  # noqa: BLE001
                    report["errors"] += 1
                    logger.warning("apply failed for %s: %s", w["listing"].get("source_listing_ref"), str(exc)[:160])
            for u in status_updates:
                try:
                    await _set_queue_status(u["queue_id"], u["status"], u["note"])
                except Exception:  # noqa: BLE001
                    pass
        finally:
            await database.disconnect()
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--queue", action="store_true", help="drain pending pdp_identity_review_queue")
    src.add_argument("--brand", help="targeted: review review_required listings of one brand")
    ap.add_argument("--apply", action="store_true", help="write overrides + queue updates (default: dry-run)")
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--min-confidence", type=float, default=0.85, help="min LLM confidence to auto-approve")
    ap.add_argument("--samples", type=int, default=8)
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    print(json.dumps(asyncio.run(_drive(args)), indent=2, default=str))


if __name__ == "__main__":
    main()
