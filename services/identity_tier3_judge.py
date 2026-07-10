"""ADR-010 D-2 Phase C — the Tier-3 batch adjudication judge (PROPOSE-ONLY).

An LLM judge for the ambiguous reconciliation tail (the groups the
deterministic strategies route to review): given a group of catalog listings
that share a family content_key, decide whether they are the SAME purchasable
listing duplicated (`collapse`) or legitimately separate (`keep_separate`).

Deployment gate (the phase plan §5 — non-negotiable): the judge adjudicates
NOTHING live until scripts/run_identity_tier3_eval.py shows, against the
step-5 gold-label fixture, a **zero mis-merge rate** — no keep_separate-
labeled group judged `collapse` at or above CONFIDENCE_FLOOR. Mis-merge is
worse than fragmentation; the judge earns trust per-version, recorded in the
eval report, and even then only ever emits proposals for human approval with
a standing spot-check sample.

Calibration — this judge's semantics INTENTIONALLY differ from
pdp_matcher/llm_match.py (which asks "same product?" for seed attachment):
here the question is "same LISTING to collapse?", and the step-5 lane-4
review verdicts define the truth:
  - different SIZES (50ml vs 100ml, 200ml vs 400ml) -> keep_separate
    (distinct sellable SKUs);
  - the same product on DIFFERENT seller/retailer/regional domains ->
    keep_separate (multi-seller observations feed the future buy-box);
  - distinct products behind one generic title -> keep_separate;
  - campaign/tracking/split-test slugs, re-seeds, junk copies of one PDP ->
    collapse.

HTTP shape mirrors pdp_matcher/llm_match.py (httpx + GEMINI_API_KEY) so the
LLM-touching modules age together. Offline (no key) the judge returns None —
it never guesses.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Sequence

import httpx

logger = logging.getLogger("identity_tier3_judge")

JUDGE_VERSION = "tier3.v2"  # v2: same-domain regional-slug rule (v1 under-covered)
DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_TIMEOUT_S = 30.0
CONFIDENCE_FLOOR = 0.7
VERDICTS = ("collapse", "keep_separate", "unsure")


def _resolve_api_key(provided: Optional[str] = None) -> Optional[str]:
    if provided is not None:
        return provided.strip() or None
    for var in ("GEMINI_API_KEY", "PIVOTA_GEMINI_API_KEY"):
        value = os.environ.get(var)
        if value and value.strip():
            return value.strip()
    return None


def _row_line(idx: int, row: Dict[str, Any]) -> str:
    return (
        f"  [{idx}] title={str(row.get('title') or '')[:120]!r} "
        f"brand={str(row.get('brand') or '')[:60]!r} "
        f"url={str(row.get('url') or row.get('canonical_url') or '')[:160]}"
    )


def build_judge_prompt(rows: Sequence[Dict[str, Any]]) -> str:
    listing_block = "\n".join(_row_line(i, r) for i, r in enumerate(rows))
    return f"""You are a product-catalog identity judge. The listings below share one
family identity key (same normalized brand+title). Decide whether they are
the SAME purchasable listing duplicated, or legitimately separate listings.

Listings:
{listing_block}

Reply with ONLY a JSON object:
{{
  "verdict": "collapse" | "keep_separate" | "unsure",
  "confidence": <0.0-1.0>,
  "reasoning": "<one short sentence>"
}}

Rules (these define the verdicts — follow them over your own instincts):
- "collapse" = the SAME product page seeded/cloned multiple times: campaign
  or split-test URL slugs (date codes, tracking suffixes, "-copy-3"),
  re-crawls of one PDP, junk duplicates. Collapsing keeps one row and
  tombstones the rest.
- Slugs on the SAME domain differing only by a region suffix ("-eu", "-ca",
  "-uk") are regional path clones of one storefront listing -> "collapse".
  This is different from separate regional DOMAINS (site.jp vs site.us),
  which are separate storefronts -> "keep_separate".
- A trailing NUMBER that expresses a spec (spf-45 vs spf-50, 100ml) is
  product identity, NOT a clone counter -> "keep_separate".
- "keep_separate" = legitimately distinct listings even though names match:
  * different SIZES or quantities (50ml vs 100ml) — distinct sellable SKUs;
  * the same product on DIFFERENT seller, retailer, or regional-storefront
    domains (brand site + retailer, .jp + .us) — multi-seller observations;
  * genuinely different products behind one generic title;
  * distinct shade/variant product pages.
- A WRONG "collapse" destroys a real listing and is far worse than a wrong
  "keep_separate". If the evidence is thin or mixed, answer "keep_separate"
  or "unsure" — never a low-conviction "collapse".
- confidence: 0.9+ unambiguous, 0.7-0.9 likely, below 0.7 unsure (callers
  ignore verdicts under 0.7).
"""


def parse_judge_response(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Gemini payload -> validated verdict dict, or None."""
    candidates = payload.get("candidates") or []
    if not candidates:
        return None
    parts = (((candidates[0] or {}).get("content") or {}).get("parts") or [])
    text = "\n".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return validate_verdict(parsed)


def validate_verdict(parsed: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(parsed, dict):
        return None
    verdict = str(parsed.get("verdict") or "").strip().lower()
    if verdict not in VERDICTS:
        return None
    try:
        confidence = float(parsed.get("confidence"))
    except (TypeError, ValueError):
        return None
    if not 0.0 <= confidence <= 1.0:
        return None
    return {
        "verdict": verdict,
        "confidence": confidence,
        "reasoning": str(parsed.get("reasoning") or "")[:500],
        "judge_version": JUDGE_VERSION,
    }


def judge_group(
    rows: Sequence[Dict[str, Any]],
    *,
    api_key: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Optional[Dict[str, Any]]:
    """Judge one group. Returns a validated verdict dict, or None (offline,
    HTTP failure, or unparseable reply — the judge never guesses)."""
    key = _resolve_api_key(api_key)
    if not key:
        return None
    prompt = build_judge_prompt(rows)
    url = f"{base_url}/models/{model}:generateContent"
    try:
        resp = httpx.post(
            url,
            params={"key": key},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.0, "maxOutputTokens": 4096},
            },
            timeout=timeout_s,
        )
        resp.raise_for_status()
        return parse_judge_response(resp.json())
    except Exception as exc:  # noqa: BLE001 — judge failure = defer, never guess
        logger.warning("tier3 judge call failed: %s", str(exc)[:200])
        return None


def eval_gate(results: List[Dict[str, Any]],
              confidence_floor: float = CONFIDENCE_FLOOR) -> Dict[str, Any]:
    """Pure: the deployment gate over eval results
    [{label, verdict, confidence}]. mis_merges MUST be zero before the judge
    adjudicates anything live."""
    keep = [r for r in results if r["label"] == "keep_separate"]
    coll = [r for r in results if r["label"] == "collapse"]

    def confident(r: Dict[str, Any], verdict: str) -> bool:
        return (r.get("verdict") == verdict
                and float(r.get("confidence") or 0) >= confidence_floor)

    mis_merges = [r for r in keep if confident(r, "collapse")]
    return {
        "judge_version": JUDGE_VERSION,
        "confidence_floor": confidence_floor,
        "n": len(results),
        "mis_merges": len(mis_merges),
        "mis_merge_rate": (len(mis_merges) / len(keep)) if keep else 0.0,
        "mis_merge_group_ids": [r.get("group_id") for r in mis_merges],
        "collapse_coverage": (
            sum(1 for r in coll if confident(r, "collapse")) / len(coll)
            if coll else 0.0),
        "keep_coverage": (
            sum(1 for r in keep if confident(r, "keep_separate")) / len(keep)
            if keep else 0.0),
        "unsure_or_failed": sum(
            1 for r in results
            if r.get("verdict") in (None, "unsure")
            or float(r.get("confidence") or 0) < confidence_floor),
        "gate_passed": not mis_merges,
    }
