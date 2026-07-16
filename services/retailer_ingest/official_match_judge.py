"""DeepSeek judge lane — resolve retailer residue against the brand's ENUMERATED
official catalog, without web search.

WHY. For Shopify brands we hold the brand's complete official catalog for free
(`curated_brand_feed` / `/products.json`). Residue resolution is then not a web
search ("find the PDP") but a closed-set matching judgment ("is SK's 'Propolis
Synergy Toner 150ml' the same product as official 'Full Fit Propolis Synergy
Toner'?"). A grounded-search LLM (Gemini) is the wrong—and expensive—tool for a
closed candidate set; a cheap judge (DeepSeek) over a deterministic shortlist is
both cheaper and structurally safer: the URL/identity comes from the enumerated
feed, so fabrication is impossible. Gemini grounded search remains the lane for
brands with NO enumerable official catalog (see run_catalog_enrichment).

Doctrine: propose-then-attach. Verdicts at/above the auto threshold (aligned
with catalog_identity's identity_high_conf deposit basis, 0.85) may be applied
by the caller; everything else is emitted for HITL review. Never auto-merges
canonicals; the judge only pairs a retailer SKU with an official product.
"""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import httpx

from config.settings import settings
from services.pdp_matcher.retailer_match import retailer_match_key

logger = logging.getLogger(__name__)

# Aligned with services.catalog_identity.deposit_min_confidence default: the
# threshold at which the repo already treats an identity resolution as
# depositable (identity_high_conf).
AUTO_ATTACH_THRESHOLD = 0.85

_SHORTLIST_K = 5
_MIN_SHARED_TOKENS = 1

_SYSTEM_PROMPT = (
    "You match retail product listings to a brand's official catalog. "
    "You are given ONE retailer listing and a small numbered list of the brand's "
    "official products. Decide which official product, if any, is the SAME "
    "physical product as the retailer listing. Size/volume/pack-count and "
    "bundle packaging differences do NOT make products different. Different "
    "shades, strengths (e.g. vitamin C 13 vs 23), line variants (e.g. 'Light' "
    "vs original), or refill-only SKUs ARE different products. If none match, "
    "say so. Respond with JSON only."
)


def _title_tokens(brand: Optional[str], title: Optional[str]) -> frozenset:
    key = retailer_match_key(brand, title)
    _, _, t = key.partition("::")
    return frozenset(t.split())


def shortlist_candidates(
    brand: Optional[str],
    title: Optional[str],
    official_records: Sequence[Mapping[str, Any]],
    *,
    k: int = _SHORTLIST_K,
) -> List[Tuple[Mapping[str, Any], float]]:
    """Deterministic top-k official candidates by title-token Jaccard overlap.
    Pure; no LLM. Records with no shared tokens are excluded entirely."""
    src = _title_tokens(brand, title)
    if not src:
        return []
    scored: List[Tuple[Mapping[str, Any], float]] = []
    for rec in official_records:
        pdp = rec.get("pdp") or {}
        toks = _title_tokens(pdp.get("brand"), pdp.get("product_name"))
        shared = len(src & toks)
        if shared < _MIN_SHARED_TOKENS:
            continue
        union = len(src | toks) or 1
        scored.append((rec, shared / union))
    scored.sort(key=lambda p: (-p[1], str((p[0].get("pdp") or {}).get("product_name") or "")))
    return scored[:k]


def _clean(value: Any, cap: int = 200) -> str:
    """Flatten crawled text before embedding it in the prompt: collapse newlines/
    whitespace and cap length, so a hostile product title can't forge candidate
    lines or inject instructions. Blast radius is already bounded (index must be
    in the deterministic shortlist, confidence clamped, bad JSON -> review), but
    this closes the forging surface cheaply."""
    return re.sub(r"\s+", " ", str(value or "")).strip()[:cap]


def build_judge_message(
    sk_brand: Optional[str],
    sk_title: Optional[str],
    candidates: Sequence[Mapping[str, Any]],
) -> str:
    lines = [f"Retailer listing: [{_clean(sk_brand, 80)}] {_clean(sk_title)}", "", "Official catalog candidates:"]
    for i, rec in enumerate(candidates):
        pdp = rec.get("pdp") or {}
        lines.append(f"  {i}: {_clean(pdp.get('product_name'))}")
    lines += [
        "",
        'Return JSON only: {"match_index": <candidate number, or -1 if none is the '
        'same product>, "confidence": <float 0..1>, "reason": "<short>"}',
    ]
    return "\n".join(lines)


def parse_judge_response(payload: Any, n_candidates: int) -> Optional[Dict[str, Any]]:
    """Validate a judge JSON payload. Returns {'match_index','confidence','reason'}
    with match_index=None for a no-match verdict; None when unparseable."""
    if not isinstance(payload, dict):
        return None
    raw_idx = payload.get("match_index")
    # bool is an int subclass — reject True/False so they don't select candidate 1/0.
    if isinstance(raw_idx, bool):
        return None
    # a non-integer float (0.9) would truncate to a different candidate — reject.
    if isinstance(raw_idx, float) and not raw_idx.is_integer():
        return None
    try:
        idx = int(raw_idx)
    except (TypeError, ValueError):
        return None
    try:
        conf = float(payload.get("confidence"))
    except (TypeError, ValueError):
        return None
    # NaN/inf would survive min/max (nan comparisons are False) and auto-attach at
    # 1.0 — reject degenerate confidence outright.
    if not math.isfinite(conf):
        return None
    conf = max(0.0, min(1.0, conf))
    if idx < -1 or idx >= n_candidates:
        return None
    return {
        "match_index": None if idx == -1 else idx,
        "confidence": conf,
        "reason": str(payload.get("reason") or "")[:300],
    }


async def _call_deepseek_judge(user_message: str, *, timeout_s: float = 20.0) -> Optional[Dict[str, Any]]:
    """One judge call, following the repo's DeepSeek idiom
    (category_classifier_llm). Returns the raw parsed JSON dict or None."""
    api_key = (settings.deepseek_api_key or "").strip()
    if not api_key:
        logger.warning("official_match_judge.no_api_key")
        return None
    base_url = settings.deepseek_api_base_url.rstrip("/")
    body = {
        "model": settings.deepseek_model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
        "max_tokens": 150,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(
                f"{base_url}/v1/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            )
    except httpx.HTTPError as exc:
        # every transport failure (timeout, network, RemoteProtocolError, ...) is
        # an HTTPError — catch the base so one bad response can't abort the batch.
        logger.warning("official_match_judge.transport_fail err=%s", exc)
        return None
    if resp.status_code >= 400:
        logger.warning("official_match_judge.http_%d body=%s", resp.status_code, resp.text[:200])
        return None
    try:
        # content can be null on a malformed 200 -> json.loads(None) raises TypeError.
        return json.loads(resp.json()["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("official_match_judge.parse_fail err=%s", exc)
        return None


JudgeFn = Callable[[str], Awaitable[Optional[Dict[str, Any]]]]


async def judge_residue_items(
    items: Sequence[Mapping[str, Any]],
    official_records: Sequence[Mapping[str, Any]],
    *,
    threshold: float = AUTO_ATTACH_THRESHOLD,
    judge_fn: Optional[JudgeFn] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Judge each residue item ({'brand','title',...}) against the official
    catalog. Returns {'auto': [...], 'review': [...], 'no_match': [...],
    'no_candidates': [...]}.

    auto   — judge matched with confidence >= threshold (caller may attach)
    review — matched below threshold, or unparseable judge output (HITL)
    judge_fn is injectable for tests; defaults to the DeepSeek call."""
    call = judge_fn or _call_deepseek_judge
    out: Dict[str, List[Dict[str, Any]]] = {"auto": [], "review": [], "no_match": [], "no_candidates": []}
    for item in items:
        brand, title = item.get("brand"), item.get("title")
        shortlist = shortlist_candidates(brand, title, official_records)
        if not shortlist:
            out["no_candidates"].append({"item": dict(item)})
            continue
        cands = [rec for rec, _ in shortlist]
        raw = await call(build_judge_message(brand, title, cands))
        verdict = parse_judge_response(raw, len(cands))
        if verdict is None:
            out["review"].append({"item": dict(item), "candidates": [dict(c) for c in cands],
                                  "verdict": None, "note": "unparseable_judge_output"})
            continue
        if verdict["match_index"] is None:
            out["no_match"].append({"item": dict(item), "verdict": verdict})
            continue
        matched = dict(cands[verdict["match_index"]])
        row = {"item": dict(item), "official": matched, "verdict": verdict,
               "shortlist_score": shortlist[verdict["match_index"]][1]}
        (out["auto"] if verdict["confidence"] >= threshold else out["review"]).append(row)
    return out
