"""CanonicalPdpEnrichmentAgent (E1) — the first real auto-dispatch node.

The audit measures that a merchant's products are content-thin ("AI can't
recommend a product it can't read"). For products on a Pivota-OWNED canonical
PDP (agent.pivota.cc/products/sig_*), Pivota can close that gap end-to-end —
no merchant action, no writing to the merchant's store:

  1. Find the merchant's canonical PDPs that are content-thin AND not yet
     enriched (respecting any existing merchant/employee copy).
  2. Generate citable copy with a frontier LLM (Gemini grounded search) so the
     facts are accurate, not invented.
  3. Compliance-guard it (no medical-cure / financial-return claims) — reusing
     the same guard the enrichment pipeline uses.
  4. Persist to product_enrichment (description_markdown + structured fields).
  5. Publish: refresh agent_pdp_view so the enriched description reaches the
     served PDP (via the E2 bridge) AND the serving-eligibility gate (which
     reads agent_pdp_view.description). The next audit / nightly index-health
     pass re-measures.

This is the executor the report's "Pivota handles this — Automatic" card
promises. Scope is the canonical PDP ONLY — a surface Pivota owns — NOT the
merchant's Shopify PDP (that stays a chat / copy-back handoff). The frontier
model is the commodity content engine; the moat is the grounded product context
+ the owned publish surface + the audit's re-measurement proof.

Cost: 1 Gemini grounded call per SKU enriched, capped at _MAX_ENRICH_PER_RUN
per run. Dispatched on audit completion via services.executor_agents.dispatcher.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional

import httpx

from services.executor_agents.base import (
    BaseExecutorAgent,
    ExecutorContext,
    ExecutorResult,
)
# Single source of truth for the Gemini key resolution (GEMINI_API_KEY /
# PIVOTA_GEMINI_API_KEY) — shared with the content-brief executor.
from services.executor_agents.content_brief import _resolve_gemini_api_key

logger = logging.getLogger(__name__)

# How many thin canonical PDPs we enrich per run. Each is one grounded Gemini
# call; 5 keeps the per-audit cost bounded. Re-audits pick up the next batch.
_MAX_ENRICH_PER_RUN = 5

# Candidate threshold: a product whose served description is shorter than this
# is "content-thin" and benefits from enrichment. The serving gate blocks at
# <50 chars; <200 means "thin enough to be worth generating real copy for".
_THIN_DESCRIPTION_CHARS = 200

# A SKU the audit scored below this content-richness band ("ready" starts at 70)
# is thin enough that Pivota should enrich its canonical PDP. Uses the audit's
# content signal — more accurate than a raw char count (a SKU can have a long-but-
# thin description the char gate misses, or a short-but-complete one it over-flags).
_CONTENT_THIN_SCORE = 70

# Reject too-short LLM output — a 3-word "description" isn't citable and won't
# clear the serving gate. The prompt asks for 600-1200 chars; require >=200.
_MIN_GENERATED_DESCRIPTION_CHARS = 200

_GEMINI_MODEL = "gemini-2.5-flash"
_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
_GEMINI_TIMEOUT_S = 30.0

async def _fetch_thin_canonical_pdps(
    merchant_id: str, *, cap: int
) -> List[Dict[str, Any]]:
    """The merchant's products on a minted Pivota canonical PDP whose served
    copy is thin AND that have no existing enrichment description_markdown
    (so we never clobber merchant/employee-authored copy). Returns the rows
    needed to build the enrichment prompt + publish. Best-effort: any failure
    degrades to no candidates rather than breaking dispatch."""
    from db.database import database

    try:
        rows = await database.fetch_all(
            """
            SELECT cp.merchant_id, cp.platform, cp.source_product_id,
                   cp.content_key, cp.title, cp.description, cp.brand,
                   cp.product_type, cp.category
            FROM catalog_products cp
            LEFT JOIN product_enrichment pe
              ON pe.merchant_id = cp.merchant_id
             AND pe.platform = cp.platform
             AND pe.platform_product_id = cp.source_product_id
             AND pe.geo_code = 'default'
            WHERE cp.merchant_id = :merchant_id
              AND cp.pivota_signature_id IS NOT NULL
              AND cp.sync_status = 'live'
              AND cp.content_key IS NOT NULL
              AND cp.source_product_id IS NOT NULL
              AND COALESCE(LENGTH(cp.description), 0) < :thin_chars
              AND (pe.description_markdown IS NULL OR pe.description_markdown = '')
            ORDER BY cp.created_at DESC NULLS LAST
            LIMIT :lim
            """,
            {
                "merchant_id": merchant_id,
                "thin_chars": _THIN_DESCRIPTION_CHARS,
                "lim": cap,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "canonical_pdp_enrichment: candidate fetch failed for %s: %s",
            merchant_id, str(exc)[:200],
        )
        return []
    return [dict(r) for r in (rows or [])]


def _decode_report(audit_report: Any) -> Dict[str, Any]:
    """The worker may hand the report back as a JSON string (report_jsonb under
    the codec-registration race). Decode defensively before reading it."""
    if isinstance(audit_report, str):
        try:
            audit_report = json.loads(audit_report)
        except (json.JSONDecodeError, ValueError):
            return {}
    return audit_report if isinstance(audit_report, dict) else {}


def _audit_thin_content_keys(audit_report: Any) -> List[str]:
    """The content_keys of the SKUs THIS audit flagged as content-thin and that
    have a minted canonical PDP — i.e. the products the merchant just measured and
    saw "needs work". This is what E1 should enrich, instead of an unrelated
    "newest 5 in the catalog" set (the bug a live re-audit surfaced: enrichment
    never reached the audited SKUs). Reads each per-SKU report's content_richness
    score + indexing_arc (present iff the SKU has a canonical PDP)."""
    rep = _decode_report(audit_report)
    out: List[str] = []
    seen: set = set()
    for r in rep.get("per_sku_reports") or []:
        if not isinstance(r, dict):
            continue
        ck = r.get("content_key")
        if not ck or ck in seen:
            continue
        if not r.get("indexing_arc"):  # no canonical PDP -> E1 can't enrich it
            continue
        score = (((r.get("scores") or {}).get("content_richness") or {}).get("score"))
        if isinstance(score, (int, float)) and score < _CONTENT_THIN_SCORE:
            seen.add(ck)
            out.append(ck)
    return out


async def _fetch_canonical_pdps_by_content_keys(
    merchant_id: str, content_keys: List[str], *, cap: int
) -> List[Dict[str, Any]]:
    """Fetch enrichable canonical PDPs for specific content_keys (the audit-flagged
    thin SKUs). Same guards as the catalog scan (minted sig, live, has
    source_product_id, not already enriched) but NO char-count / newest-N limit —
    the audit already determined these need work. Best-effort."""
    keys = [k for k in (content_keys or []) if k]
    if not keys:
        return []
    from db.database import database

    try:
        rows = await database.fetch_all(
            """
            SELECT cp.merchant_id, cp.platform, cp.source_product_id,
                   cp.content_key, cp.title, cp.description, cp.brand,
                   cp.product_type, cp.category
            FROM catalog_products cp
            LEFT JOIN product_enrichment pe
              ON pe.merchant_id = cp.merchant_id
             AND pe.platform = cp.platform
             AND pe.platform_product_id = cp.source_product_id
             AND pe.geo_code = 'default'
            WHERE cp.merchant_id = :merchant_id
              AND cp.content_key = ANY(:keys)
              AND cp.pivota_signature_id IS NOT NULL
              AND cp.sync_status = 'live'
              AND cp.source_product_id IS NOT NULL
              AND (pe.description_markdown IS NULL OR pe.description_markdown = '')
            ORDER BY cp.created_at DESC NULLS LAST
            LIMIT :lim
            """,
            {"merchant_id": merchant_id, "keys": keys, "lim": cap},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "canonical_pdp_enrichment: audit-driven fetch failed for %s: %s",
            merchant_id, str(exc)[:200],
        )
        return []
    return [dict(r) for r in (rows or [])]


async def _resolve_candidates(
    context: ExecutorContext, *, cap: int
) -> List[Dict[str, Any]]:
    """Candidates to enrich, AUDIT-FIRST: the thin canonical SKUs this audit
    flagged (so the loop is honest — "merchant audits X -> Pivota enriches X ->
    re-audit shows X improved"), then top up from the newest-thin catalog scan as
    a fallback (legacy / no-audit-report dispatch). Deduped by product identity,
    capped. Each candidate is tagged with `_candidate_source` for evidence."""
    merchant_id = context.merchant_id
    if not merchant_id:
        return []
    candidates: List[Dict[str, Any]] = []
    seen: set = set()

    def _add(rows: List[Dict[str, Any]], source: str) -> None:
        for r in rows:
            key = (r.get("merchant_id"), r.get("platform"), r.get("source_product_id"))
            if key in seen or len(candidates) >= cap:
                continue
            seen.add(key)
            r["_candidate_source"] = source
            candidates.append(r)

    audit_keys = _audit_thin_content_keys(context.audit_report)
    if audit_keys:
        _add(
            await _fetch_canonical_pdps_by_content_keys(merchant_id, audit_keys, cap=cap),
            "audit",
        )
    if len(candidates) < cap:
        _add(await _fetch_thin_canonical_pdps(merchant_id, cap=cap), "catalog")
    return candidates[:cap]


def _build_enrichment_prompt(candidate: Dict[str, Any]) -> str:
    title = str(candidate.get("title") or "").strip() or "(untitled product)"
    brand = str(candidate.get("brand") or "").strip()
    category = str(
        candidate.get("category") or candidate.get("product_type") or ""
    ).strip()
    raw = str(candidate.get("description") or "").strip() or "(none provided)"
    by_brand = f' by {brand}' if brand else ""
    cat_line = f"\nCategory: {category}" if category else ""
    return f"""You are writing the official product-detail-page copy for "{title}"{by_brand}, so AI shopping agents (Gemini, ChatGPT) can accurately find, understand, and recommend it.{cat_line}

Use grounded search to confirm REAL facts about this exact product. Write accurate, specific, non-promotional copy. Do NOT invent specs, ingredients, certifications, or any medical/health/financial claims. If you cannot verify a fact, omit it.

OUTPUT FORMAT — strict:
- Reply with a bare JSON object starting with {{ and ending with }}
- Do NOT wrap in markdown fences (```)
- Do NOT add prose before or after

Schema:
{{
  "description_markdown": "A thorough, factual product description in plain markdown, 600-1200 characters: what it is, key features/ingredients/materials, who it's for, how to use it, and the facts a buyer needs to decide. No hype, no unverifiable claims.",
  "summary_short": "One factual sentence (<=160 chars)",
  "bullet_points": ["3-6 concrete, specific selling points, each a short phrase"],
  "usage_scenarios": ["2-4 short when/how-to-use scenarios"],
  "audience_tags": ["2-5 short who-it's-for tags"],
  "title_override": "A clean, specific product title (<=120 chars); repeat the given title if it is already clean"
}}

Rules:
- description_markdown MUST be 600-1200 chars and factual.
- NEVER include medical cure/treatment claims or financial-return promises.
- Given title: "{title}"
- Given description (may be thin/empty): "{raw}"
"""


def _clean_str_list(value: Any, *, cap: int) -> Optional[List[str]]:
    if not isinstance(value, list):
        return None
    out = [s.strip() for s in value if isinstance(s, str) and s.strip()]
    return out[:cap] or None


def _parse_enrichment_response(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract + validate the enrichment JSON from a Gemini response. Returns
    None when there is no usable description_markdown (the field E2 publishes)."""
    candidates = payload.get("candidates") or []
    if not candidates:
        return None
    parts = (((candidates[0] or {}).get("content") or {}).get("parts") or [])
    text = "\n".join(
        p.get("text", "") for p in parts if isinstance(p, dict)
    ).strip()
    if not text:
        return None
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
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
    if not isinstance(parsed, dict):
        return None

    desc = parsed.get("description_markdown")
    if not isinstance(desc, str) or len(desc.strip()) < _MIN_GENERATED_DESCRIPTION_CHARS:
        return None
    desc = desc.strip()

    summary = parsed.get("summary_short")
    summary = summary.strip() if isinstance(summary, str) and summary.strip() else None
    title_override = parsed.get("title_override")
    title_override = (
        title_override.strip()
        if isinstance(title_override, str) and title_override.strip()
        else None
    )

    return {
        "description_markdown": desc,
        "summary_short": summary,
        "bullet_points": _clean_str_list(parsed.get("bullet_points"), cap=6),
        "usage_scenarios": _clean_str_list(parsed.get("usage_scenarios"), cap=4),
        "audience_tags": _clean_str_list(parsed.get("audience_tags"), cap=5),
        "title_override": title_override,
    }


async def _generate_enrichment(
    candidate: Dict[str, Any],
    api_key: str,
    *,
    timeout_s: float = _GEMINI_TIMEOUT_S,
) -> Optional[Dict[str, Any]]:
    """Single Gemini grounded call → parsed enrichment payload, or None on any
    failure (HTTP error, non-200, unparseable, too-short description)."""
    prompt = _build_enrichment_prompt(candidate)
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 2048},
        "tools": [{"google_search": {}}],
    }
    url = f"{_GEMINI_BASE_URL}/models/{_GEMINI_MODEL}:generateContent"
    headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            r = await client.post(url, headers=headers, json=body)
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        logger.warning(
            "canonical_pdp_enrichment: HTTP error for %s: %s",
            candidate.get("source_product_id"), exc,
        )
        return None
    if r.status_code != 200:
        logger.warning(
            "canonical_pdp_enrichment: non-200 (%s) for %s",
            r.status_code, candidate.get("source_product_id"),
        )
        return None
    try:
        payload = r.json()
    except json.JSONDecodeError:
        return None
    return _parse_enrichment_response(payload)


class CanonicalPdpEnrichmentAgent(BaseExecutorAgent):
    """Auto-generate + publish citable enrichment for content-thin Pivota
    canonical PDPs (the surface Pivota owns)."""

    name = "canonical_pdp_enrichment"

    async def should_run(self, context: ExecutorContext) -> bool:
        """Run when merchant_id is set, a Gemini key is configured, and there is
        >=1 enrichable candidate (an audit-flagged thin canonical SKU, else a
        newest-thin catalog candidate). Cheap — capped at 1, no LLM call."""
        if not context.merchant_id:
            return False
        if not _resolve_gemini_api_key():
            return False
        candidates = await _resolve_candidates(context, cap=1)
        return len(candidates) > 0

    async def execute(self, context: ExecutorContext) -> ExecutorResult:
        if not context.merchant_id:
            return ExecutorResult(
                status="skipped", error_message="merchant_id required"
            )
        api_key = _resolve_gemini_api_key()
        if not api_key:
            return ExecutorResult(
                status="skipped", error_message="no GEMINI_API_KEY configured"
            )

        candidates = await _resolve_candidates(context, cap=_MAX_ENRICH_PER_RUN)
        if not candidates:
            return ExecutorResult(
                status="skipped",
                evidence={"reason": "no thin canonical PDPs to enrich"},
            )

        # Generate in parallel — each is a single grounded Gemini call.
        gens = await asyncio.gather(
            *[_generate_enrichment(c, api_key) for c in candidates],
            return_exceptions=True,
        )

        # Lazy imports (match the executor convention; keep module import light).
        from db.product_enrichment import upsert_enrichment
        from services.agent_pdp_view_assembler import (
            refresh_agent_pdp_view_for_content_key,
        )
        from services.product_enrichment_pipeline import _simple_compliance_check

        enriched: List[Dict[str, Any]] = []
        blocked: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []

        for cand, gen in zip(candidates, gens):
            ident = {
                "merchant_id": cand.get("merchant_id"),
                "platform": cand.get("platform"),
                "source_product_id": cand.get("source_product_id"),
            }
            if isinstance(gen, Exception) or gen is None:
                failed.append({**ident, "reason": "gemini_call_failed_or_unparseable"})
                continue

            # Compliance guard — same check the enrichment pipeline uses. Never
            # publish medical-cure / financial-return copy to the served PDP.
            compliance = _simple_compliance_check({
                "description": gen.get("description_markdown") or "",
                "summary": gen.get("summary_short") or "",
                "bullets": " ".join(gen.get("bullet_points") or []),
            })
            if not compliance["allowed"]:
                blocked.append({**ident, "reason": compliance["reason"]})
                continue

            # Persist to the enrichment overlay (candidate filter already
            # excluded rows with existing description_markdown, so no clobber).
            try:
                await upsert_enrichment(
                    cand["merchant_id"],
                    cand["platform"],
                    cand["source_product_id"],
                    "default",
                    {
                        "description_markdown": gen["description_markdown"],
                        "summary_short": gen.get("summary_short"),
                        "bullet_points": gen.get("bullet_points"),
                        "usage_scenarios": gen.get("usage_scenarios"),
                        "audience_tags": gen.get("audience_tags"),
                        "title_override": gen.get("title_override"),
                        "llm_safety_flags": {"auto_generated_by": self.name},
                    },
                )
            except Exception as exc:  # noqa: BLE001
                failed.append({**ident, "reason": f"upsert_failed: {exc!r}"})
                continue

            # Publish: refresh the served view so the enriched description
            # reaches the PDP + the serving gate (E2 bridge). Best-effort — the
            # enrichment is already persisted; a refresh failure just defers the
            # publish to the next catalog_sync / nightly pass.
            published = False
            content_key = cand.get("content_key")
            if content_key:
                try:
                    published = await refresh_agent_pdp_view_for_content_key(
                        content_key, refresh_source=self.name
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "canonical_pdp_enrichment: APV refresh failed for %s: %s",
                        content_key, str(exc)[:200],
                    )

            enriched.append({
                **ident,
                "content_key": content_key,
                "description_chars": len(gen["description_markdown"]),
                "published": published,
                "source": cand.get("_candidate_source"),
            })

        evidence = {
            "candidates_total": len(candidates),
            "audit_driven_count": sum(
                1 for c in candidates if c.get("_candidate_source") == "audit"
            ),
            "enriched_count": len(enriched),
            "blocked_count": len(blocked),
            "failed_count": len(failed),
            "enriched": enriched,
            "blocked": blocked,
            "failed": failed,
        }
        # Succeeded when at least one PDP was enriched + persisted — the agent
        # did real work. All-failed (every Gemini call failed) is a failure so
        # the worker retries.
        return ExecutorResult(
            status="succeeded" if enriched else "failed",
            evidence=evidence,
            error_message=None if enriched else "no products enriched",
        )
