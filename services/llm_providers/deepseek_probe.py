"""Deepseek V4 ungrounded-probe client (PR-3a).

Backend-direct integration with the Deepseek chat completions API for
multi-LLM audit coverage. Bypasses the upstream PIVOTA-Agent codex
stack so we can ship Deepseek before ChatGPT/Claude (which are
gated on codex work).

**Probe modes** (mirror the Gemini probe modes from PIVOTA-Agent's
agentCenterLlmProbe.js):

  - `open_product_visibility_test` — "where can I buy {product}?"-
    style queries. Output schema: `{product_visible, competitors_listed,
    evidence_excerpt}`.
  - `merchant_store_attribution_test` — same queries scored against
    whether the merchant's verified URL appears in the response.
    Output schema: `{merchant_url_found, evidence_excerpt}`.
  - `category_visibility_test` — category-open queries that don't name
    the brand. Output schema: `{brand_appears, competitors_appearing,
    evidence_excerpt}`.

**Output normalization.** Every probe returns the V1 result shape that
upstream PIVOTA-Agent emits — `{scan_mode, provider, runs_count,
scores, findings, usage, raw_runs}`. Per-run shape matches the Gemini
runs: `{query, raw, parsed, grounding_chunks, grounding_sources,
url_match}`. This means downstream code (score_category_visibility,
the BD report builder, the merchant_view assembler) consumes Deepseek
results identically to Gemini.

**Grounding boundary.** Deepseek chat does not provide server-side web
search on this API path; `tools` only accepts function tools, so this
client never sends `tools=[{"type": "web_search"}]` or `enable_search`.
Deepseek runs as an ungrounded answer-quality/verify signal. Grounded
probing must use Gemini or ChatGPT. If a response payload includes
citation metadata anyway, it is normalized into grounding_sources
matching the Gemini chunk shape; otherwise grounding_chunks/sources
land as empty arrays.

**Cost guard.** Reuses the per-merchant + global semaphore from
agent_center_llm_client. Caller must wrap probe calls in those
semaphores (the dispatcher in agent_center_llm_client.probe() does
this automatically).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Mapping, Optional

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)


ANSWER_QUALITY_VERIFY_SCAN_MODE = "answer_quality_verify"


# Match the issue-type taxonomy the Gemini probe + downstream scorers
# expect. Keeping this constant in lockstep with PIVOTA-Agent's
# PRIMARY_ISSUE_TYPE_BY_SCAN_MODE means the backend's report builder
# treats Deepseek findings identically to Gemini findings.
PRIMARY_ISSUE_TYPE_BY_SCAN_MODE: Dict[str, str] = {
    "open_product_visibility_test": "ai_visibility_loss",
    "merchant_store_attribution_test": "merchant_store_attribution_gap",
    "category_visibility_test": "category_discoverability_gap",
}


class DeepseekProbeError(RuntimeError):
    """Raised when the Deepseek probe fails irrecoverably (5xx, timeout,
    auth failure, etc.). Caller in agent_center_llm_client maps this
    to AgentCenterLlmClientError so the route layer surfaces a 502."""


def _build_query_strings(
    *,
    scan_mode: str,
    product_title: Optional[str] = None,
    product_type: Optional[str] = None,
    merchant_brand: Optional[str] = None,
    verify_query: Optional[str] = None,
    max_runs: int,
) -> List[str]:
    """Generate the query set per scan mode, mirroring how PIVOTA-Agent
    constructs queries for the Gemini probe. Capped at max_runs.
    """
    pt = (product_title or "").strip()
    cat = (product_type or "").strip()
    brand = (merchant_brand or "").strip()
    if scan_mode == "open_product_visibility_test":
        # Buyer-intent variations naming the product.
        templates = [
            f"where can I buy {pt}",
            f"shop {pt} online",
            f"{pt} for sale",
        ]
    elif scan_mode == "merchant_store_attribution_test":
        # Same buyer-intent queries; scored on URL appearance instead
        # of brand visibility. Generate the same query set so the two
        # tests are paired.
        templates = [
            f"where can I buy {pt}",
            f"shop {pt} online",
            f"{pt} for sale",
        ]
    elif scan_mode == "category_visibility_test":
        # Category-open queries that DON'T name the brand. Tests
        # whether Deepseek surfaces the brand organically when asked
        # category questions.
        if not cat:
            return []  # caller must skip if no product_type
        templates = [
            f"best {cat} 2026",
            f"top {cat} this year",
            f"best {cat} under $50",
        ]
    elif scan_mode == ANSWER_QUALITY_VERIFY_SCAN_MODE:
        query = (verify_query or pt or "answer quality verification").strip()
        templates = [query] if query else []
    else:
        return []
    return [t for t in templates if t.strip()][:max_runs]


def _build_system_prompt(scan_mode: str) -> str:
    """Per-scan-mode system prompt directing Deepseek to emit
    structured JSON matching the field set the downstream scorer
    expects.
    """
    ungrounded_primary_instruction = (
        "You do not have live web search in this Deepseek path. Answer "
        "from your model knowledge only, be conservative when evidence "
        "is uncertain, and do not claim to have searched or retrieved "
        "live source citations. "
    )
    if scan_mode == "open_product_visibility_test":
        return (
            ungrounded_primary_instruction +
            "You are a shopping assistant. When asked about a product, "
            "respond ONLY with a JSON object of this exact shape: "
            '{"product_visible": <bool>, "competitors_listed": '
            '[<brand strings>], "evidence_excerpt": "<short quoted '
            'reason from your answer>"}. Set product_visible=true only '
            "when you can identify the specific product the user named. "
            "List competitor brand names in competitors_listed. Quote "
            "the most relevant text from your answer in evidence_excerpt "
            "(under 300 characters). Do not include any text outside "
            "the JSON object."
        )
    if scan_mode == "merchant_store_attribution_test":
        return (
            ungrounded_primary_instruction +
            "You are a shopping assistant. When asked where to buy a "
            "product, respond ONLY with a JSON object of this exact "
            'shape: {"merchant_url_found": <bool>, "evidence_excerpt": '
            '"<short quoted text from your answer>"}. Set '
            "merchant_url_found=true only when the user's specified "
            "merchant URL appears in your answer. Quote the most "
            "relevant text in evidence_excerpt (under 300 characters). "
            "Do not include any text outside the JSON object."
        )
    if scan_mode == "category_visibility_test":
        return (
            ungrounded_primary_instruction +
            "You are a shopping research assistant. When asked about "
            "the best products in a category, respond ONLY with a "
            'JSON object of this exact shape: {"brand_appears": '
            '<bool>, "competitors_appearing": [<brand strings>], '
            '"evidence_excerpt": "<short quoted text from your '
            'answer>"}. Set brand_appears=true only when the brand '
            "named in the user's prompt appears in your answer. List "
            "all competitor brand names in competitors_appearing. "
            "Quote the most relevant text in evidence_excerpt (under "
            "300 characters). Do not include any text outside the "
            "JSON object."
        )
    if scan_mode == ANSWER_QUALITY_VERIFY_SCAN_MODE:
        return (
            "You are an ungrounded answer-quality verifier. You do not "
            "fetch URLs and you must not judge whether citation URLs "
            "exist. Read only the supplied grounded answer text, evidence "
            "excerpt, SKU title, and shopper intent. Respond ONLY with a "
            "JSON object of this exact shape: "
            '{"supports_recommendation": <bool>, "misstates_facts": '
            '<bool>, "note": "<short reason under 200 characters>"}. '
            "Set supports_recommendation=true only when the supplied "
            "answer substantively supports recommending the named SKU for "
            "the intent. Set misstates_facts=true when the answer "
            "contradicts or invents a core fact about the SKU. Do not "
            "include any text outside the JSON object."
        )
    return "Respond with a JSON object describing the user's query."


def _build_user_message(
    *,
    scan_mode: str,
    query: str,
    merchant_brand: Optional[str] = None,
    merchant_pdp_url: Optional[str] = None,
    product_title: Optional[str] = None,
    verify_answer_text: Optional[str] = None,
    verify_evidence_excerpt: Optional[str] = None,
    verify_intent: Optional[str] = None,
) -> str:
    """Per-scan-mode user-message text. Embeds the brand/URL the
    scorer needs to match against."""
    if scan_mode == ANSWER_QUALITY_VERIFY_SCAN_MODE:
        sku_title = (product_title or "").strip()
        intent = (verify_intent or query or "").strip()
        answer_text = (verify_answer_text or "").strip()
        evidence = (verify_evidence_excerpt or "").strip()
        return (
            f"SKU title: {sku_title}\n"
            f"Shopper intent/query: {intent}\n\n"
            "Grounded answer text from the citation provider:\n"
            f"{answer_text[:4000]}\n\n"
            "Evidence excerpt/chunks already supplied by that provider:\n"
            f"{evidence[:2000]}\n\n"
            "Judge answer quality and consistency only. Do not verify URL "
            "existence or perform external lookup."
        )
    if scan_mode == "merchant_store_attribution_test" and merchant_pdp_url:
        return (
            f"{query}. Please indicate whether {merchant_pdp_url} "
            "is included in your answer."
        )
    if scan_mode == "category_visibility_test" and merchant_brand:
        return (
            f"{query}. Please include any mention of {merchant_brand} "
            "if it appears in your recommendations."
        )
    return query


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _parse_deepseek_response(raw_text: str) -> Optional[Dict[str, Any]]:
    """Extract the JSON object from Deepseek's response. Handles three
    common formats: bare JSON, fenced ```json...``` block, JSON
    embedded mid-text. Returns None when nothing parseable is found
    (run is treated as upstream-failed by the scorer)."""
    if not raw_text or not raw_text.strip():
        return None
    text = raw_text.strip()
    # Try bare JSON first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Try fenced JSON block
    match = _JSON_FENCE_RE.search(text)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    # Last attempt: find the first {...} substring
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    return None


def _extract_grounding_sources(
    response_payload: Dict[str, Any],
) -> List[Dict[str, str]]:
    """Normalize citation metadata from a Deepseek response, if present,
    to the Gemini grounding-source shape `{uri, title}`.

    This client does not request web search. Citation metadata is still
    parsed defensively when present in captured or future payloads:
      - `choices[0].message.annotations` for citation annotations
        (OpenAI-compatible format)
      - Top-level `citations` field

    Defensive: when no grounding is surfaced, returns an empty list,
    which the downstream scorer correctly reads as "no grounded
    evidence" — does NOT credit the run with first-party attribution.
    """
    sources: List[Dict[str, str]] = []
    seen_uris: set = set()

    # Shape 1: top-level citations field (newer Deepseek search API)
    top_level_citations = response_payload.get("citations") or []
    if isinstance(top_level_citations, list):
        for c in top_level_citations:
            if isinstance(c, str):
                if c not in seen_uris:
                    seen_uris.add(c)
                    # Strip protocol + path to get a hostname-like label
                    label = re.sub(r"^https?://(www\.)?", "", c).split("/", 1)[0]
                    sources.append({"uri": c, "title": label or c})
            elif isinstance(c, dict):
                uri = c.get("url") or c.get("uri") or ""
                title = c.get("title") or c.get("name") or ""
                if uri and uri not in seen_uris:
                    seen_uris.add(uri)
                    sources.append({
                        "uri": uri,
                        "title": title or re.sub(
                            r"^https?://(www\.)?", "", uri,
                        ).split("/", 1)[0],
                    })

    # Shape 2: message.annotations (OpenAI-compatible citation format)
    choices = response_payload.get("choices") or []
    if choices and isinstance(choices, list):
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") or {}
        annotations = message.get("annotations") or []
        if isinstance(annotations, list):
            for a in annotations:
                if not isinstance(a, dict):
                    continue
                # OpenAI format: {type: "url_citation", url_citation: {url, title}}
                if a.get("type") == "url_citation":
                    uc = a.get("url_citation") or {}
                    uri = uc.get("url") or ""
                    title = uc.get("title") or ""
                    if uri and uri not in seen_uris:
                        seen_uris.add(uri)
                        sources.append({
                            "uri": uri,
                            "title": title or re.sub(
                                r"^https?://(www\.)?", "", uri,
                            ).split("/", 1)[0],
                        })
    return sources


def _build_url_match(
    *,
    scan_mode: str,
    parsed: Optional[Dict[str, Any]],
    merchant_brand: Optional[str],
    merchant_pdp_url: Optional[str],
    grounding_sources: List[Dict[str, str]],
    raw_text: str,
) -> Optional[Dict[str, Any]]:
    """Build the url_match shape downstream scorers consume. Mirrors
    the Gemini probe's url_match: in_grounding (URL appears in
    grounding chunks), in_text (URL appears in raw response text),
    llm_self_report (LLM's structured judgment about the brand)."""
    if scan_mode == "merchant_store_attribution_test":
        if not merchant_pdp_url:
            return None
        url_lower = merchant_pdp_url.lower()
        # Strip protocol + www so "gruns.co/p/x" in raw text matches
        # "https://gruns.co/p/x" url_lower (LLMs frequently drop the
        # protocol when echoing URLs in prose).
        url_no_proto = re.sub(r"^https?://(www\.)?", "", url_lower)
        in_grounding = any(
            url_lower in (s.get("uri") or "").lower()
            or url_no_proto in (s.get("uri") or "").lower()
            for s in grounding_sources
        )
        raw_lower = (raw_text or "").lower()
        in_text = url_lower in raw_lower or url_no_proto in raw_lower
        llm_self_report = bool(
            (parsed or {}).get("merchant_url_found")
        )
        return {
            "target_url": merchant_pdp_url,
            "in_grounding": in_grounding,
            "in_text": in_text,
            "llm_self_report": llm_self_report,
        }
    if scan_mode == "category_visibility_test":
        if not merchant_brand:
            return None
        return {
            "target_brand": merchant_brand,
            "target_url": merchant_pdp_url,
            "in_grounding": False,  # category test doesn't probe URL
            "in_text": False,
            "llm_self_report": (parsed or {}).get("brand_appears"),
        }
    # open_product_visibility_test has no url_match in upstream Gemini
    # output — return None to keep parity.
    return None


async def _call_deepseek_chat(
    *,
    api_key: str,
    base_url: str,
    model: str,
    system_prompt: str,
    user_message: str,
    timeout_s: float,
    enable_web_search: bool = True,
) -> Dict[str, Any]:
    """Single-shot Deepseek chat completion call. Returns the full
    response payload. Raises DeepseekProbeError on transport / 5xx
    failures so the caller can decide whether to mark the run as
    upstream-failed (vs failing the whole audit)."""
    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    body: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        # Force JSON output. Deepseek supports OpenAI-compatible
        # response_format for structured outputs.
        "response_format": {"type": "json_object"},
        # Conservative temperature — we want consistent structured
        # output, not creative variation.
        "temperature": 0.2,
        # Hard cap on output tokens. Evidence excerpts are <300 chars;
        # full response with competitor list is well under 1k tokens.
        "max_tokens": 800,
    }
    # Retained for caller compatibility; Deepseek chat rejects web_search as a
    # non-function tool, so this path must remain ungrounded.
    _ = enable_web_search

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            response = await client.post(url, json=body, headers=headers)
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        raise DeepseekProbeError(
            f"Deepseek API transport failure: {exc}"
        ) from exc
    if response.status_code >= 500:
        raise DeepseekProbeError(
            f"Deepseek API {response.status_code}: {response.text[:200]}"
        )
    if response.status_code == 401:
        raise DeepseekProbeError(
            "Deepseek API auth failure (401) — check DEEPSEEK_API_KEY"
        )
    if response.status_code >= 400:
        # 4xx — request shape issue. Not transient.
        raise DeepseekProbeError(
            f"Deepseek API client error {response.status_code}: "
            f"{response.text[:200]}"
        )
    return response.json()


async def answer_grounded_question(
    *,
    system_prompt: str,
    user_message: str,
    timeout_s: float = 30.0,
) -> str:
    """Answer a freeform merchant question grounded in supplied audit
    context, using Deepseek chat.

    Deepseek runs UNGROUNDED here (no live web search), which is exactly
    what we want for the "Ask about this product" box: the model must
    reason only over the audit slice we pass, never the open web — that
    keeps the answer faithful to the report and unable to invent outside
    facts. Reuses `_call_deepseek_chat`, which forces JSON output, so the
    caller's prompt should ask for `{"answer": "..."}` and we unwrap it
    here (falling back to the raw text if the model returns bare prose).

    Returns the plain-text answer (possibly empty if the model returned
    nothing usable). Raises DeepseekProbeError on missing key / transport
    / 4xx-5xx so the route can degrade to an honest "try again".
    """
    api_key = (settings.deepseek_api_key or "").strip()
    if not api_key:
        raise DeepseekProbeError(
            "DEEPSEEK_API_KEY is not configured; cannot answer the question."
        )
    payload = await _call_deepseek_chat(
        api_key=api_key,
        base_url=settings.deepseek_api_base_url,
        model=settings.deepseek_model,
        system_prompt=system_prompt,
        user_message=user_message,
        timeout_s=timeout_s,
        enable_web_search=False,
    )
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise DeepseekProbeError(
            "Deepseek response missing choices[0].message.content"
        ) from exc
    if not isinstance(content, str) or not content.strip():
        return ""
    try:
        parsed = json.loads(content)
    except (ValueError, TypeError):
        # Model ignored response_format and returned bare prose.
        return content.strip()
    if isinstance(parsed, dict):
        return str(parsed.get("answer") or "").strip()
    return content.strip()


async def _run_single_query(
    *,
    api_key: str,
    base_url: str,
    model: str,
    scan_mode: str,
    query: str,
    product_title: Optional[str],
    merchant_brand: Optional[str],
    merchant_pdp_url: Optional[str],
    verify_answer_text: Optional[str],
    verify_evidence_excerpt: Optional[str],
    verify_intent: Optional[str],
    timeout_s: float,
) -> Dict[str, Any]:
    """Probe one query. Returns the per-run shape downstream scorers
    expect (matches Gemini probe runs): `{query, raw, parsed,
    grounding_chunks, grounding_sources, url_match}`.

    Best-effort: any exception or unparseable response is captured
    as an upstream-failed run (raw="", parsed=None) — same way the
    Gemini probe handles transient failures."""
    system_prompt = _build_system_prompt(scan_mode)
    user_message = _build_user_message(
        scan_mode=scan_mode,
        query=query,
        merchant_brand=merchant_brand,
        merchant_pdp_url=merchant_pdp_url,
        product_title=product_title,
        verify_answer_text=verify_answer_text,
        verify_evidence_excerpt=verify_evidence_excerpt,
        verify_intent=verify_intent,
    )
    try:
        api_response = await _call_deepseek_chat(
            api_key=api_key,
            base_url=base_url,
            model=model,
            system_prompt=system_prompt,
            user_message=user_message,
            timeout_s=timeout_s,
            enable_web_search=scan_mode != ANSWER_QUALITY_VERIFY_SCAN_MODE,
        )
    except DeepseekProbeError as exc:
        logger.warning(
            "deepseek_probe single-query failed scan_mode=%s query=%r: %s",
            scan_mode, query[:80], exc,
        )
        # Return upstream-failed shape so downstream scorer excludes
        # from denominator (per PR-433 semantics).
        return {
            "query": query,
            "raw": "",
            "parsed": None,
            "grounding_chunks": [],
            "grounding_sources": [],
            "url_match": None,
            "_usage": {"input_tokens": None, "output_tokens": None},
        }
    choices = api_response.get("choices") or []
    raw_text = ""
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message") or {}
        raw_text = message.get("content") or ""
    parsed = _parse_deepseek_response(raw_text)
    grounding_sources = _extract_grounding_sources(api_response)
    grounding_chunks = [s.get("uri", "") for s in grounding_sources if s.get("uri")]
    url_match = _build_url_match(
        scan_mode=scan_mode,
        parsed=parsed,
        merchant_brand=merchant_brand,
        merchant_pdp_url=merchant_pdp_url,
        grounding_sources=grounding_sources,
        raw_text=raw_text,
    )
    # P2.5b: extract usage tokens for cost telemetry. Deepseek's
    # OpenAI-compatible response uses prompt_tokens + completion_tokens.
    usage_block = api_response.get("usage") or {}
    return {
        "query": query,
        "raw": raw_text,
        "parsed": parsed,
        "grounding_chunks": grounding_chunks,
        "grounding_sources": grounding_sources,
        "url_match": url_match,
        **({"provider": "deepseek", "role": "verify"} if scan_mode == ANSWER_QUALITY_VERIFY_SCAN_MODE else {}),
        # Per-run usage so the outer probe_one_scan_mode can sum.
        # None values when Deepseek omits the block (rare but possible
        # on rate-limit / partial responses).
        "_usage": {
            "input_tokens": usage_block.get("prompt_tokens"),
            "output_tokens": usage_block.get("completion_tokens"),
        },
    }


def _compute_scores_from_runs(
    *,
    scan_mode: str,
    runs: List[Dict[str, Any]],
) -> Dict[str, int]:
    """Mirror the upstream Gemini probe's score arithmetic so the
    Deepseek probe's `scores` block matches the V1 schema. The
    backend re-scores category_visibility from raw_runs anyway (in
    services.agent_center_bd_report_service.score_category_visibility),
    so this is mostly a stub to keep schema parity for renderers
    that consume the upstream-emitted score directly.

    Conservative: visibility_score is the % of runs where the LLM
    self-reported positively; attribution_echo_rate is unused by
    Deepseek (Gemini-specific concept).

    Upstream-failed runs (parsed=None — HTTP/parse errors caught in
    `_run_single_query`) are EXCLUDED from the denominator. Same
    convention as the canonical scorer in
    services.agent_center_bd_report_service.score_category_visibility
    (see PR-433). Counting a transient probe failure as a negative
    would drag the score down for what is not a visibility miss.
    """
    if not runs:
        return {"visibility_score": 0, "attribution_echo_rate": 0}
    scoreable = [r for r in runs if r.get("parsed") is not None]
    if not scoreable:
        return {"visibility_score": 0, "attribution_echo_rate": 0}
    if scan_mode == "open_product_visibility_test":
        positive = sum(
            1 for r in scoreable
            if (r.get("parsed") or {}).get("product_visible")
        )
    elif scan_mode == "merchant_store_attribution_test":
        positive = sum(
            1 for r in scoreable
            if (r.get("parsed") or {}).get("merchant_url_found")
        )
    elif scan_mode == "category_visibility_test":
        positive = sum(
            1 for r in scoreable
            if (r.get("parsed") or {}).get("brand_appears")
        )
    elif scan_mode == ANSWER_QUALITY_VERIFY_SCAN_MODE:
        positive = sum(
            1 for r in scoreable
            if (r.get("parsed") or {}).get("supports_recommendation") is True
            and (r.get("parsed") or {}).get("misstates_facts") is not True
        )
    else:
        positive = 0
    score = round((positive / len(scoreable)) * 100)
    scores = {"visibility_score": score, "attribution_echo_rate": 0}
    if scan_mode == ANSWER_QUALITY_VERIFY_SCAN_MODE:
        scores["supports_recommendation_rate"] = score
    return scores


def _build_findings(
    *,
    scan_mode: str,
    runs: List[Dict[str, Any]],
    visibility_score: int,
) -> List[Dict[str, Any]]:
    """Match the V1 finding shape PIVOTA-Agent emits — the BD report
    builder consumes these via the `findings` field. Single primary
    finding per scan_mode.
    """
    issue_type = PRIMARY_ISSUE_TYPE_BY_SCAN_MODE.get(scan_mode)
    if not issue_type:
        return []
    return [{
        "issue_type": issue_type,
        "severity": "high" if visibility_score < 30 else "medium",
        "evidence": {
            "visibility_score": visibility_score,
            "runs": runs,
        },
    }]


async def probe_one_scan_mode(
    *,
    scan_mode: str,
    product_title: str,
    product_type: Optional[str] = None,
    merchant_brand: Optional[str] = None,
    merchant_pdp_url: Optional[str] = None,
    verify_query: Optional[str] = None,
    verify_answer_text: Optional[str] = None,
    verify_evidence_excerpt: Optional[str] = None,
    verify_intent: Optional[str] = None,
    max_runs: int = 3,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    timeout_s: float = 30.0,
) -> Dict[str, Any]:
    """Run one scan_mode against Deepseek. Returns the V1 result shape
    matching what PIVOTA-Agent's Gemini probe emits.

    Caller is expected to wrap this in the per-merchant + global
    semaphores (the dispatcher in agent_center_llm_client does so).
    """
    import time as _time
    _telemetry_started_at = _time.perf_counter()
    api_key = (api_key or settings.deepseek_api_key or "").strip()
    if not api_key:
        raise DeepseekProbeError(
            "DEEPSEEK_API_KEY is not configured; cannot probe Deepseek."
        )
    base_url = base_url or settings.deepseek_api_base_url
    model = model or settings.deepseek_model
    queries = _build_query_strings(
        scan_mode=scan_mode,
        product_title=product_title,
        product_type=product_type,
        merchant_brand=merchant_brand,
        verify_query=verify_query,
        max_runs=max_runs,
    )
    if not queries:
        # No queries to run (e.g., category_visibility with no
        # product_type). Return an empty-but-shaped result so the
        # caller's pipeline doesn't blow up.
        await _record_deepseek_telemetry(
            scan_mode=scan_mode, status="succeeded",
            started_at_perf=_telemetry_started_at,
            input_tokens=0, output_tokens=0,
            model=model,
        )
        return {
            "scan_mode": scan_mode,
            "provider": "deepseek",
            **({"role": "verify"} if scan_mode == ANSWER_QUALITY_VERIFY_SCAN_MODE else {}),
            "runs_count": 0,
            "scores": {"visibility_score": 0, "attribution_echo_rate": 0},
            "findings": [],
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "raw_runs": [],
        }
    raw_runs: List[Dict[str, Any]] = []
    for query in queries:
        run_result = await _run_single_query(
            api_key=api_key,
            base_url=base_url,
            model=model,
            scan_mode=scan_mode,
            query=query,
            product_title=product_title,
            merchant_brand=merchant_brand,
            merchant_pdp_url=merchant_pdp_url,
            verify_answer_text=verify_answer_text,
            verify_evidence_excerpt=verify_evidence_excerpt,
            verify_intent=verify_intent,
            timeout_s=timeout_s,
        )
        raw_runs.append(run_result)
    scores = _compute_scores_from_runs(scan_mode=scan_mode, runs=raw_runs)
    findings = _build_findings(
        scan_mode=scan_mode,
        runs=raw_runs,
        visibility_score=scores["visibility_score"],
    )
    # P2.5b: aggregate per-run token usage so the outer `usage` block
    # has real numbers + cost telemetry can be recorded. None values
    # in per-run _usage (rate-limit responses, etc.) are treated as
    # zero rather than skewing the row count.
    total_in = sum(
        int((r.get("_usage") or {}).get("input_tokens") or 0)
        for r in raw_runs
    )
    total_out = sum(
        int((r.get("_usage") or {}).get("output_tokens") or 0)
        for r in raw_runs
    )
    succeeded_runs = sum(
        1 for r in raw_runs if (r.get("parsed") is not None)
    )
    await _record_deepseek_telemetry(
        scan_mode=scan_mode,
        # `succeeded` here means we got a response back (even if some
        # individual queries within failed). `failed` would only fire
        # on uncaught exceptions, which are caught + returned as
        # upstream-failed shapes by _run_single_query above.
        status="succeeded" if succeeded_runs > 0 else "failed",
        started_at_perf=_telemetry_started_at,
        input_tokens=total_in,
        output_tokens=total_out,
        error_message=(
            None if succeeded_runs > 0
            else "all_runs_returned_upstream_failed_shape"
        ),
        model=model,
    )
    return {
        "scan_mode": scan_mode,
        "provider": "deepseek",
        **({"role": "verify"} if scan_mode == ANSWER_QUALITY_VERIFY_SCAN_MODE else {}),
        "runs_count": len(raw_runs),
        "scores": scores,
        "findings": findings,
        "usage": {
            "input_tokens": total_in,
            "output_tokens": total_out,
        },
        "raw_runs": raw_runs,
    }


async def _record_deepseek_telemetry(
    *,
    scan_mode: str,
    status: str,
    started_at_perf: float,
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    error_message: Optional[str] = None,
    model: Optional[str] = None,
) -> None:
    """P2.5b: best-effort telemetry record for one Deepseek probe
    call. Pulls audit_run_id + merchant_id from the audit_telemetry
    context; computes cost via provider_registry rates. When `model`
    is supplied, per-model rates (set on the provider's `model_rates`)
    are used so overriding DEEPSEEK_MODEL to a non-Flash SKU does not
    silently undercount cost."""
    try:
        import time as _time
        from db.llm_probe_runs import (
            compute_cost_usd, record_probe_run,
        )
        from services.audit_telemetry_context import (
            current_audit_context,
        )
        from services.llm_providers.provider_registry import (
            get_provider,
        )
        ctx = current_audit_context()
        latency_ms = int(
            (_time.perf_counter() - started_at_perf) * 1000.0,
        )
        cost_usd = None
        try:
            p = get_provider("deepseek")
            if p is not None:
                rates = p.rate_for_model(model)
                cost_usd = compute_cost_usd(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_per_1k_input_tokens_usd=rates["input_per_1k"],
                    cost_per_1k_output_tokens_usd=rates["output_per_1k"],
                )
        except Exception:  # noqa: BLE001
            pass
        await record_probe_run(
            provider="deepseek",
            scan_mode=scan_mode,
            status=status,
            merchant_id=ctx.merchant_id,
            audit_run_id=ctx.audit_run_id,
            latency_ms=latency_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            error_message=error_message,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "_record_deepseek_telemetry suppressed error: %s",
            str(exc)[:200],
        )
