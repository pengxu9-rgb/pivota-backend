"""CompetitorInsightsAgent (C4a) — WHY the competitors AI names win.

The audit captures WHO wins each losing query (competitor names) + the AI's
verbatim answer excerpt, but never WHY — the attribute/claim that earned the
recommendation. C4a runs one LLM pass over the already-captured excerpts and turns
"MDhair Marine Collagen, Vida Glow" into "they win on marine-collagen + glycine/
melatonin for sleep — to match, lead with your sleep actives."

No new probes: it reads context.audit_report (per-SKU per-prompt cited_evidence).
Surfaced as a merchant task (like the content-brief executor). Cost: 1 LLM call
per run (no grounded search — it summarizes the provided answer text).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

import httpx

from services.executor_agents.base import (
    BaseExecutorAgent,
    ExecutorContext,
    ExecutorResult,
    RESULT_TYPE_HUMAN_TASK_RECOMMENDED,
)
# Single source of truth for the Gemini key resolution.
from services.executor_agents.content_brief import _resolve_gemini_api_key
from services import vertex_gemini

logger = logging.getLogger(__name__)

# Cap the losing queries fed to the LLM — bounds the prompt size + the one call's
# cost. The audit surfaces the highest-opportunity lanes first.
_MAX_QUERIES = 8
_MAX_COMPETITORS_PER_QUERY = 5

_GEMINI_MODEL = "gemini-2.5-flash"
_GEMINI_TIMEOUT_S = 25.0


def _decode_report(audit_report: Any) -> Dict[str, Any]:
    """The worker may hand the report back as a JSON string (report_jsonb under
    the codec-registration race). Decode defensively before reading it."""
    if isinstance(audit_report, str):
        try:
            audit_report = json.loads(audit_report)
        except (json.JSONDecodeError, ValueError):
            return {}
    return audit_report if isinstance(audit_report, dict) else {}


def _collect_competitor_evidence(audit_report: Any) -> List[Dict[str, Any]]:
    """From the audit's per-SKU per-prompt lanes, the losing queries where AI named
    competitors AND gave a verbatim answer — (query, excerpt, competitors). Deduped
    by query, capped. This is the already-captured signal C4a explains."""
    rep = _decode_report(audit_report)
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for r in rep.get("per_sku_reports") or []:
        if not isinstance(r, dict):
            continue
        opp = r.get("opportunity") if isinstance(r.get("opportunity"), dict) else {}
        for pp in opp.get("per_prompt") or []:
            if not isinstance(pp, dict):
                continue
            ce = pp.get("cited_evidence") if isinstance(pp.get("cited_evidence"), dict) else {}
            excerpt = str(ce.get("excerpt") or "").strip()
            competitors = [
                str(c).strip()
                for c in (pp.get("competitors") or ce.get("competitors_named") or [])
                if str(c or "").strip()
            ][:_MAX_COMPETITORS_PER_QUERY]
            query = str(pp.get("normalized_query") or pp.get("query") or "").strip()
            if not (excerpt and competitors and query) or query in seen:
                continue
            seen.add(query)
            out.append({"query": query, "excerpt": excerpt, "competitors": competitors})
            if len(out) >= _MAX_QUERIES:
                return out
    return out


def _build_prompt(rows: List[Dict[str, Any]]) -> str:
    blocks = []
    for r in rows:
        blocks.append(
            f'Query: "{r["query"]}"\n'
            f'Competitors named: {", ".join(r["competitors"])}\n'
            f'AI answer: "{r["excerpt"]}"'
        )
    data = "\n\n".join(blocks)
    return f"""You are a competitive analyst. Below are buyer queries where AI shopping agents recommended COMPETITORS (not the merchant), each with the AI's verbatim answer and the competitors it named. For each competitor, extract from the ANSWER why it wins — the specific attribute/claim — plus a short "how to compete".

OUTPUT FORMAT — strict:
- Reply with a bare JSON object starting with {{ and ending with }}
- No markdown fences (```), no prose before/after

Schema:
{{
  "insights": [
    {{"competitor": "<name>", "query": "<the query>", "why_wins": "<=14 words: the attribute/claim FROM the answer", "how_to_compete": "<=14 words: a concrete move for the merchant"}}
  ]
}}

Rules:
- why_wins MUST be grounded in the provided answer text — never invented. If the answer gives no reason, set why_wins to "named without a stated reason".
- One entry per (competitor, query). Skip generic ingredient/category words that aren't a brand/product.

Data:
{data}
"""


def _parse_insights(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    candidates = payload.get("candidates") or []
    if not candidates:
        return []
    parts = (((candidates[0] or {}).get("content") or {}).get("parts") or [])
    text = "\n".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
    if not text:
        return []
    # W3: shared tolerant parser (bare/fence/substring), one implementation.
    from services.llm_io import parse_llm_object

    parsed = parse_llm_object(text, label="competitor_insights")
    if parsed is None:
        return []
    out: List[Dict[str, Any]] = []
    for item in parsed.get("insights") or []:
        if not isinstance(item, dict):
            continue
        comp = str(item.get("competitor") or "").strip()
        why = str(item.get("why_wins") or "").strip()
        if not comp or not why:
            continue
        out.append({
            "competitor": comp,
            "query": str(item.get("query") or "").strip() or None,
            "why_wins": why,
            "how_to_compete": str(item.get("how_to_compete") or "").strip() or None,
        })
    return out


async def _extract_win_reasons(
    rows: List[Dict[str, Any]], api_key: str, *, timeout_s: float = _GEMINI_TIMEOUT_S
) -> List[Dict[str, Any]]:
    """Single Gemini call (no grounded search — it summarizes the provided answer
    text) → structured why-they-win per competitor. [] on any failure."""
    body = {
        "contents": [{"role": "user", "parts": [{"text": _build_prompt(rows)}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 2048},
    }
    url = vertex_gemini.generate_content_url(_GEMINI_MODEL)
    headers = await vertex_gemini.auth_headers(api_key)
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            r = await client.post(url, headers=headers, json=body)
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        logger.warning("competitor_insights: HTTP error: %s", exc)
        return []
    if r.status_code != 200:
        logger.warning("competitor_insights: non-200 (%s)", r.status_code)
        return []
    try:
        payload = r.json()
    except json.JSONDecodeError:
        return []
    return _parse_insights(payload)


def _task_body(insights: List[Dict[str, Any]]) -> str:
    lines = [
        "Pivota analyzed the AI answers where competitors win the queries you're "
        "losing — here's WHY each wins and how to match:",
        "",
    ]
    for ins in insights[:8]:
        q = f' ("{ins["query"]}")' if ins.get("query") else ""
        compete = f" → {ins['how_to_compete']}" if ins.get("how_to_compete") else ""
        lines.append(f"• {ins['competitor']}{q}: {ins['why_wins']}{compete}")
    lines.append("")
    lines.append(
        "Use this to sharpen your product claims + feed the canonical-PDP "
        "enrichment so AI can ground the same strengths in your listing."
    )
    return "\n".join(lines)


class CompetitorInsightsAgent(BaseExecutorAgent):
    """Explain WHY the competitors AI names win, from the captured answer excerpts."""

    name = "competitor_insights"

    async def should_run(self, context: ExecutorContext) -> bool:
        if not context.merchant_id:
            return False
        if not _resolve_gemini_api_key():
            return False
        return len(_collect_competitor_evidence(context.audit_report)) > 0

    async def execute(self, context: ExecutorContext) -> ExecutorResult:
        if not context.merchant_id:
            return ExecutorResult(status="skipped", error_message="merchant_id required")
        api_key = _resolve_gemini_api_key()
        if not api_key:
            return ExecutorResult(
                status="skipped", error_message="no GEMINI_API_KEY configured"
            )
        rows = _collect_competitor_evidence(context.audit_report)
        if not rows:
            return ExecutorResult(
                status="skipped",
                evidence={"reason": "no competitor evidence to analyze"},
            )
        insights = await _extract_win_reasons(rows, api_key)
        if not insights:
            return ExecutorResult(
                status="failed",
                error_message="win-reason extraction failed",
                evidence={"queries_analyzed": len(rows)},
            )
        return ExecutorResult(
            status="succeeded",
            evidence={"queries_analyzed": len(rows), "insights": insights},
            result_type=RESULT_TYPE_HUMAN_TASK_RECOMMENDED,
            task_title="Why competitors win — and how to match",
            task_body=_task_body(insights),
            task_severity="medium",
            task_owner="merchant_growth_team",
            task_lever="competitive_intel",
        )
