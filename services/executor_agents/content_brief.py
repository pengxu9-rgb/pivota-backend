"""ContentBriefGeneratorAgent (PR-4c) — drafts content briefs for
queries the merchant LOST in category_visibility audit.

Pivota CAN'T write content for the merchant (their voice, their team).
But we CAN draft a brief: target query + competitor articles currently
cited + suggested title + outline + word count + key talking points.
The merchant's content team takes it from there.

This is the third executor in the series. Like PR-4a (GSC) and PR-4b
(sitemap), it converts an advisory finding ("you're invisible on
query X") into a tangible artifact ("here's the brief — your team
writes the article").

Out of scope (defer):
  - Generating the full article (merchant voice + brand + legal
    review). Pivota stops at the brief.
  - Submitting the brief to the merchant's CMS / content team. Lands
    in evidence_jsonb; PR-6 task queue surfaces it as work.
  - Cold-start version (would be BD-pitch material). PR-4c only fires
    on merchant audits via the existing dispatcher.

Cost: 1 Gemini grounded call per brief generated, capped at 3 briefs
per agent run (so worst case +3 grounded Gemini calls per merchant
audit). Bounded by per-merchant semaphore.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

import httpx

from services.executor_agents.base import (
    BaseExecutorAgent,
    ExecutorContext,
    ExecutorResult,
)
from services import vertex_gemini

logger = logging.getLogger(__name__)


# How many failed category queries we attempt briefs for. Top-N by
# the order they appear in the audit (Gemini tends to surface the
# most-prominent category queries first). 3 keeps quota bounded;
# operators wanting more can re-run with manual triggers.
_MAX_BRIEFS_PER_RUN = 3

_GEMINI_MODEL = "gemini-2.5-flash"
_GEMINI_TIMEOUT_S = 25.0


def _resolve_gemini_api_key() -> Optional[str]:
    for var in ("GEMINI_API_KEY", "PIVOTA_GEMINI_API_KEY"):
        value = os.environ.get(var)
        if value and value.strip():
            return value.strip()
    return None


def _decode_jsonb(value: Any) -> Any:
    """Defensive JSONB reader: asyncpg / the `databases` library may hand
    back a JSONB column — OR a JSONB value nested inside one — as a raw
    JSON *string* instead of a parsed dict/list, depending on whether the
    JSONB codec was registered. When that happens, calling `.get()` (or
    iterating-as-dict) on the string raises
    ``AttributeError("'str' object has no attribute 'get'")`` — the same
    class of bug fixed in db.merchant_audit_runs._decode_jsonb_field for
    the per-SKU trend reader.

    Mirrors that helper but is shape-preserving: it returns parsed dicts
    AND lists (the content-brief walk reads array fields — per_product,
    queries, match_details — not just objects). Non-JSON strings (e.g.
    a literal "garbage" sentinel) are returned unchanged so downstream
    isinstance() guards still skip them rather than mis-parsing.
    """
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return value
    return value


def _extract_failed_category_queries(
    audit_report: Optional[Dict[str, Any]],
    *,
    cap: int,
) -> List[str]:
    """Walk the audit's per_product → category_visibility → match_details
    and return queries where the merchant's brand was NOT matched in
    grounded sources. Dedup across products. Return at most `cap`.

    Failure signal: match_details.matched is False (the strict signal —
    set by the upstream audit when the merchant brand wasn't found in
    grounding chunks for that query). Fallback when match_details
    missing: queries with cited_urls_count == 0 (Gemini returned no
    grounded sources at all for this query — both a failure and a
    content opportunity).
    """
    # JSONB-as-string defense: report_jsonb (and nested values) can come
    # back as a raw JSON string under the codec-registration race. Decode
    # before any .get()/iteration so we read the data instead of either
    # crashing (pre-#869) or silently skipping it (post-#869: the string
    # fell through every isinstance guard, yielding zero briefs).
    audit_report = _decode_jsonb(audit_report)
    if not isinstance(audit_report, dict):
        return []
    seen = set()
    out: List[str] = []
    per_product = _decode_jsonb(audit_report.get("per_product"))
    if not isinstance(per_product, list):
        per_product = []
    for product in per_product:
        product = _decode_jsonb(product)
        if not isinstance(product, dict):
            continue
        # Q-P1-7: defensive shape handling. `category_visibility` is a
        # dict in the canonical probe output, but production has
        # surfaced both shapes:
        #   - dict: {"queries": [...], "match_details": [...], ...}
        #   - list: [{"query": "...", "matched": False}, ...]  (legacy
        #     / partial-probe shape; behaves like a flat queries list
        #     with match_details inlined)
        # Pre-fix this crashed with AttributeError on the list shape;
        # it can also arrive as a JSON string (nested JSONB) — decode it.
        cv_raw = _decode_jsonb(product.get("category_visibility"))
        if isinstance(cv_raw, list):
            # List shape: each entry is already a query+match record.
            # Treat the list itself as both `queries` and inline
            # `match_details`.
            queries_iter = cv_raw
            match_details = cv_raw
        elif isinstance(cv_raw, dict):
            queries_iter = _decode_jsonb(cv_raw.get("queries"))
            match_details = _decode_jsonb(cv_raw.get("match_details"))
        else:
            continue
        if not isinstance(queries_iter, list):
            queries_iter = []
        if not isinstance(match_details, list):
            match_details = []
        # Index match_details by query for joining.
        md_by_query = {
            (md.get("query") or ""): md
            for md in match_details
            if isinstance(md, dict)
        }
        for q in queries_iter:
            if not isinstance(q, dict):
                continue
            qtext = q.get("query") or ""
            if not qtext or qtext in seen:
                continue
            md = md_by_query.get(qtext)
            if md is not None:
                # Use strict signal when match_details available.
                # In the list-shape case `md` and `q` are the same dict;
                # the `matched` field still drives the decision.
                is_failure = not bool(md.get("matched", False))
            else:
                # Fallback: query returned zero grounded sources.
                is_failure = (q.get("cited_urls_count") or 0) == 0
            if is_failure:
                seen.add(qtext)
                out.append(qtext)
                if len(out) >= cap:
                    return out
    return out


def _build_brief_prompt(brand: str, query: str) -> str:
    return f"""You are a content strategist helping the brand "{brand}" win the buyer query: "{query}".

Today, when consumers ask AI shopping agents (Gemini, ChatGPT) this query, the answers cite specific articles. Find the top 3 articles currently cited for this query — listicles, reviews, buying guides. For each: title, publication, what topics they cover.

Then propose a content brief the merchant could write to compete in this category.

OUTPUT FORMAT — strict:
- Reply with a bare JSON object starting with {{ and ending with }}
- Do NOT wrap in markdown fences (```)
- Do NOT add prose before or after

Schema:
{{
  "target_query": "{query}",
  "suggested_title": "Working title for the article (60-80 chars)",
  "suggested_word_count": 1500,
  "outline_h2_sections": ["H2 heading 1", "H2 heading 2", "..."],
  "key_talking_points": ["bullet 1", "bullet 2", "..."],
  "competitor_articles": [
    {{"title": "...", "publication": "...", "topics_covered": "1-line summary"}}
  ],
  "differentiation_angle": "1-2 sentences on what makes this brand's take distinct vs the cited competitors"
}}

Rules:
- outline_h2_sections: 3-6 items
- key_talking_points: 4-6 items, each a single sentence
- competitor_articles: up to 3; reflect what's actually cited in grounded sources, not made up
- suggested_word_count: round to 100
- If you can't find grounded competitor articles for this query, set competitor_articles=[] and base the outline on category-best-practice
"""


def _parse_brief_response(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract + validate the brief JSON from a Gemini response."""
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
        # Try to find the first balanced object.
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(parsed, dict):
        return None

    # Validate / coerce shape — drop briefs that don't have the
    # essentials (a title + outline). Other fields are nice-to-have.
    title = parsed.get("suggested_title")
    outline = parsed.get("outline_h2_sections")
    if not (isinstance(title, str) and title.strip()):
        return None
    if not (isinstance(outline, list) and outline):
        return None

    word_count = parsed.get("suggested_word_count")
    if not isinstance(word_count, int):
        try:
            word_count = int(word_count) if word_count is not None else None
        except (TypeError, ValueError):
            word_count = None

    return {
        "target_query": (
            parsed.get("target_query") if isinstance(parsed.get("target_query"), str) else None
        ),
        "suggested_title": title.strip(),
        "suggested_word_count": word_count,
        "outline_h2_sections": [
            s.strip() for s in outline if isinstance(s, str) and s.strip()
        ],
        "key_talking_points": [
            s.strip() for s in (parsed.get("key_talking_points") or [])
            if isinstance(s, str) and s.strip()
        ],
        "competitor_articles": [
            a for a in (parsed.get("competitor_articles") or [])
            if isinstance(a, dict)
        ],
        "differentiation_angle": (
            parsed.get("differentiation_angle")
            if isinstance(parsed.get("differentiation_angle"), str)
            else None
        ),
    }


def _brief_target_queries(briefs: List[Dict[str, Any]]) -> List[str]:
    """The real target queries from parsed briefs.

    Reads `target_query` — the key `_parse_brief_response` actually stores. The
    pre-fix title/body code read `query`, which is never present, so every run
    fell back to a generic ("category-visibility queries") phrase and an
    identical count-only title, making distinct runs look like duplicates.
    """
    out: List[str] = []
    for b in briefs or []:
        if isinstance(b, dict):
            q = str(b.get("target_query") or "").strip()
            if q:
                out.append(q)
    return out


def _content_brief_task_title(briefs: List[Dict[str, Any]]) -> str:
    """Target-specific task title so each run is scannable and re-audits of the
    same failing query collapse onto the same (merchant_id, lever, title)
    identity for supersession instead of stacking generic duplicates."""
    queries = _brief_target_queries(briefs)
    n = len(briefs or [])
    if len(queries) == 1:
        return f"Content brief: {queries[0]}"
    if queries:
        shown = ", ".join(queries[:2])
        extra = len(queries) - 2
        return f"{n} content briefs: {shown}" + (f", +{extra} more" if extra > 0 else "")
    return (
        f"Write {n} content brief{'s' if n != 1 else ''} "
        "for failed category-visibility queries"
    )


async def _generate_brief_for_query(
    brand: str,
    query: str,
    api_key: str,
    *,
    timeout_s: float = _GEMINI_TIMEOUT_S,
) -> Optional[Dict[str, Any]]:
    """Single Gemini grounded call; returns parsed brief or None on
    any failure."""
    prompt = _build_brief_prompt(brand, query)
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 2048},
        "tools": [{"google_search": {}}],
    }
    url = vertex_gemini.generate_content_url(_GEMINI_MODEL)
    headers = await vertex_gemini.auth_headers(api_key)
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            r = await client.post(url, headers=headers, json=body)
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        logger.warning(
            "content_brief: HTTP error for query=%r: %s", query, exc,
        )
        return None
    if r.status_code != 200:
        logger.warning(
            "content_brief: non-200 for query=%r: %s",
            query, r.status_code,
        )
        return None
    try:
        payload = r.json()
    except json.JSONDecodeError:
        return None
    return _parse_brief_response(payload)


def _derive_brand_from_audit(audit_report: Optional[Dict[str, Any]]) -> str:
    """Pull the brand name from the audit_report (uses
    `merchant_name` set by run_brand_report). Fallback to '(brand)'
    so prompt construction doesn't blow up when missing."""
    audit_report = _decode_jsonb(audit_report)
    if isinstance(audit_report, dict):
        mn = audit_report.get("merchant_name")
        if isinstance(mn, str) and mn.strip():
            return mn.strip()
    return "(brand)"


class ContentBriefGeneratorAgent(BaseExecutorAgent):
    """For each failed category_visibility query, draft a content brief
    the merchant's content team can act on."""

    name = "content_brief_generator"

    async def should_run(self, context: ExecutorContext) -> bool:
        """Run when:
          - merchant_id is set
          - audit_report has at least 1 failed category query
          - Gemini API key is configured
        """
        if not context.merchant_id:
            return False
        if not _resolve_gemini_api_key():
            return False
        failed = _extract_failed_category_queries(
            context.audit_report, cap=1,  # short-circuit: existence check only
        )
        return len(failed) > 0

    async def execute(self, context: ExecutorContext) -> ExecutorResult:
        if not context.merchant_id:
            return ExecutorResult(
                status="skipped", error_message="merchant_id required",
            )
        api_key = _resolve_gemini_api_key()
        if not api_key:
            return ExecutorResult(
                status="skipped", error_message="no GEMINI_API_KEY configured",
            )

        failed_queries = _extract_failed_category_queries(
            context.audit_report, cap=_MAX_BRIEFS_PER_RUN,
        )
        if not failed_queries:
            return ExecutorResult(
                status="skipped",
                evidence={"reason": "no failed category queries to brief"},
            )

        brand = _derive_brand_from_audit(context.audit_report)

        # Generate briefs in parallel — each is a single Gemini call.
        results = await asyncio.gather(
            *[
                _generate_brief_for_query(brand, q, api_key)
                for q in failed_queries
            ],
            return_exceptions=True,
        )

        briefs: List[Dict[str, Any]] = []
        failures: List[Dict[str, Any]] = []
        for query, result in zip(failed_queries, results):
            if isinstance(result, Exception):
                failures.append({
                    "query": query,
                    "reason": f"unexpected: {type(result).__name__}",
                })
            elif result is None:
                failures.append({
                    "query": query,
                    "reason": "gemini_call_failed_or_unparseable",
                })
            else:
                briefs.append(result)

        evidence = {
            "candidate_queries_total": len(failed_queries),
            "briefs_generated": len(briefs),
            "briefs_failed": len(failures),
            "briefs": briefs,
            "failures": failures,
        }

        # Even partial success counts — at least one brief means the
        # merchant has actionable artifact. Only fail when zero
        # briefs were generated.
        if briefs:
            # P1.1: a content brief is a deliverable for the merchant
            # content team to write the article from. Emit a human
            # task so it shows up in the merchant's task queue.
            from services.executor_agents.base import (
                RESULT_TYPE_HUMAN_TASK_RECOMMENDED,
            )
            # Read the real target queries (key is "target_query", not
            # "query") so each run names its queries and re-audits collapse
            # onto the same task identity for supersession.
            brief_queries = _brief_target_queries(briefs)
            queries_phrase = ", ".join(
                f'"{q}"' for q in brief_queries[:3]
            ) or "category-visibility queries"
            task_title = _content_brief_task_title(briefs)
            task_body = (
                f"Pivota's content brief generator produced "
                f"{len(briefs)} drafted brief"
                f"{'s' if len(briefs) != 1 else ''} for queries where "
                f"your brand is currently invisible in AI-channel "
                f"category answers (e.g. {queries_phrase}). Each "
                f"brief includes target query, top-cited competitors, "
                f"suggested headline, outline, and word count. Your "
                f"content team writes the article; Pivota submits it "
                f"to indexing in the next audit cycle."
            )
            return ExecutorResult(
                status="succeeded",
                evidence=evidence,
                result_type=RESULT_TYPE_HUMAN_TASK_RECOMMENDED,
                task_title=task_title,
                task_body=task_body,
                task_severity="medium",
                task_owner="merchant_brand_team",
                task_lever="content_creation",
            )
        return ExecutorResult(
            status="failed",
            error_message="no briefs generated (all Gemini calls failed)",
            evidence=evidence,
        )
