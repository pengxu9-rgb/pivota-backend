"""SerpAPI search client — deterministic web retrieval for the BD
social-intel probes.

Replaces Gemini's discretionary `google_search` grounding, which a
2026-05-14 prod diagnostic proved returns 0 grounding chunks for 12/15
social calls even though the model issues good queries every time
(`~/tmp/social-grounding-check/verdict.md`). The social probes now run
real search queries here, then hand the results to a Gemini extraction
call (`bd_brand_signals._gemini_extract_call`). Grounding becomes
deterministic: "did the search return results?" instead of "did the
model decide to search, and did Gemini's grounding pipeline return
anything?".

Honesty contract: no API key, or a search failure, returns NO results —
never fabricated ones. The caller's PR-9 honesty gate then nulls or
suppresses the affected social fields.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)

# Telemetry identifiers — provider/scan_mode values are lowercase
# snake_case per the llm_probe_runs convention. The `bd_search` empty-rate
# is the replacement KPI for the retired `ungrounded` rate. Originally
# named `bd_social_search` when only the 3 social probes used it (#535);
# renamed when the 4 non-social probes joined (#536 follow-up).
_SEARCH_PROVIDER = "serpapi"
_SEARCH_SCAN_MODE = "bd_search"

_SEARCH_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=5.0, pool=5.0)
# Mirror bd_brand_signals._GEMINI_TRANSPORT_RETRY_EXCS — transient
# transport/timeout failures get one bounded retry; everything else fails fast.
_SEARCH_TRANSPORT_RETRY_EXCS = (
    httpx.TimeoutException,
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.ConnectError,
    httpx.NetworkError,
)
_SEARCH_RETRY_BACKOFF_S = 0.5
_SEARCH_RESULT_CAP = 8

# Bounds per-report search fan-out. `infer_social_intelligence` gathers
# ~9 probes, each firing ~3 queries (~28 SerpAPI calls/report); this
# semaphore keeps concurrent SerpAPI requests bounded so one report can't
# open ~28 sockets at once. Size to the SerpAPI plan's rate limit.
_SEARCH_CONCURRENCY = 4
_search_semaphore: Optional[asyncio.Semaphore] = None

# search status tokens — the caller maps these onto its _FR_* tokens.
_STATUS_OK = "ok"                   # >=1 query returned >=1 usable result
_STATUS_EMPTY = "empty"             # queries ran, all returned 0 results
_STATUS_TRANSPORT = "transport_error"  # the search API call(s) failed
_STATUS_NO_KEY = "no_key"           # SERPAPI_API_KEY unset


def _get_semaphore() -> asyncio.Semaphore:
    """Lazy one-shot init so tests can adjust _SEARCH_CONCURRENCY first."""
    global _search_semaphore
    if _search_semaphore is None:
        _search_semaphore = asyncio.Semaphore(max(1, _SEARCH_CONCURRENCY))
    return _search_semaphore


def _reset_search_state_for_test() -> None:
    """Test hook — drop the lazily-created semaphore."""
    global _search_semaphore
    _search_semaphore = None


def _resolve_search_api_key() -> Optional[str]:
    key = (settings.serpapi_api_key or "").strip()
    return key or None


async def _record_search_telemetry(
    *,
    status: str,
    started_at_perf: float,
    error_message: Optional[str] = None,
) -> None:
    """Best-effort telemetry for one SerpAPI call. SerpAPI cost is not
    metered here (cost_usd=None); the row captures latency + status + the
    empty/transport signal. The `bd_search` empty-rate
    (error_message='empty_search') is the replacement health KPI for the
    retired `ungrounded` rate."""
    try:
        from db.llm_probe_runs import record_probe_run
        from services.audit_telemetry_context import current_audit_context

        ctx = current_audit_context()
        latency_ms = int((time.perf_counter() - started_at_perf) * 1000.0)
        await record_probe_run(
            provider=_SEARCH_PROVIDER,
            scan_mode=_SEARCH_SCAN_MODE,
            status=status,
            merchant_id=ctx.merchant_id,
            audit_run_id=ctx.audit_run_id,
            latency_ms=latency_ms,
            input_tokens=None,
            output_tokens=None,
            cost_usd=None,
            error_message=error_message,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "_record_search_telemetry suppressed error: %s", str(exc)[:200],
        )


def _normalize_results(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    """SerpAPI response body → `[{title, url, snippet}]`, capped at
    `_SEARCH_RESULT_CAP`. The `answer_box` and `knowledge_graph` blocks
    (where an exact follower count often lives) are folded in FIRST as
    high-priority pseudo-results, ahead of the organic results."""
    out: List[Dict[str, str]] = []

    # answer_box — Google's direct-answer block, highest signal.
    ab = payload.get("answer_box")
    if isinstance(ab, dict):
        ab_text = " ".join(
            str(ab.get(k)) for k in ("answer", "result", "snippet", "title")
            if ab.get(k)
        ).strip()
        if ab_text:
            out.append({
                "title": "Google answer box",
                "url": str(ab.get("link") or ab.get("displayed_link") or ""),
                "snippet": ab_text[:600],
            })

    # knowledge_graph — entity facts (scalar fields only; skip nested junk).
    kg = payload.get("knowledge_graph")
    if isinstance(kg, dict):
        kg_bits: List[str] = []
        for k, v in kg.items():
            if k in ("kgmid", "knowledge_graph_search_link", "serpapi_link"):
                continue
            if isinstance(v, (str, int, float)) and str(v).strip():
                kg_bits.append(f"{k}: {v}")
        kg_text = " | ".join(kg_bits).strip()
        if kg_text:
            out.append({
                "title": "Google knowledge graph",
                "url": str(kg.get("website") or ""),
                "snippet": kg_text[:600],
            })

    # organic results.
    for r in payload.get("organic_results") or []:
        if not isinstance(r, dict):
            continue
        title = str(r.get("title") or "").strip()
        url = str(r.get("link") or "").strip()
        snippet = str(r.get("snippet") or "").strip()
        if not (title or snippet):
            continue
        out.append({"title": title, "url": url, "snippet": snippet})

    return out[:_SEARCH_RESULT_CAP]


async def search_web(query: str) -> Tuple[List[Dict[str, str]], str]:
    """Run ONE SerpAPI query. Returns `(results, status)` where status is
    one of `ok` / `empty` / `transport_error` / `no_key`.

    Never raises, never fabricates:
      - no API key                         -> ([], "no_key")
      - transport failure (after 1 retry)  -> ([], "transport_error")
      - non-200 / SerpAPI error body       -> ([], "transport_error")
      - 200 with 0 usable results          -> ([], "empty")
      - 200 with results                   -> (results, "ok")
    """
    api_key = _resolve_search_api_key()
    if not api_key:
        return ([], _STATUS_NO_KEY)

    started = time.perf_counter()
    url = f"{settings.serpapi_base_url.rstrip('/')}/search.json"
    params = {
        "q": query,
        "api_key": api_key,
        "engine": "google",
        "num": 10,
        "hl": "en",
        "gl": "us",
    }
    r: Optional[httpx.Response] = None
    last_exc: Optional[BaseException] = None
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=_SEARCH_TIMEOUT) as client:
                r = await client.get(url, params=params)
            break
        except _SEARCH_TRANSPORT_RETRY_EXCS as exc:
            last_exc = exc
            logger.warning(
                "social_search: transport error (attempt %s/2) q=%r: %s",
                attempt, query[:80], exc,
            )
            if attempt == 2:
                break
            await asyncio.sleep(_SEARCH_RETRY_BACKOFF_S)
        except httpx.RequestError as exc:
            # Non-retryable httpx error — fail fast.
            last_exc = exc
            logger.warning("social_search: HTTP error q=%r: %s", query[:80], exc)
            break

    if r is None:
        await _record_search_telemetry(
            status="failed", started_at_perf=started,
            error_message=(
                f"transport: {type(last_exc).__name__}"
                if last_exc is not None else "transport: unknown"
            ),
        )
        return ([], _STATUS_TRANSPORT)

    if r.status_code == 429:
        await _record_search_telemetry(
            status="rate_limited", started_at_perf=started,
            error_message="http_429",
        )
        return ([], _STATUS_TRANSPORT)
    if r.status_code != 200:
        await _record_search_telemetry(
            status="failed", started_at_perf=started,
            error_message=f"http_{r.status_code}",
        )
        return ([], _STATUS_TRANSPORT)

    try:
        payload = r.json()
    except (ValueError, json.JSONDecodeError):
        await _record_search_telemetry(
            status="failed", started_at_perf=started,
            error_message="non_json_response",
        )
        return ([], _STATUS_TRANSPORT)

    # SerpAPI can return 200 with an error body.
    if isinstance(payload, dict) and payload.get("error"):
        await _record_search_telemetry(
            status="failed", started_at_perf=started,
            error_message=f"serpapi_error: {str(payload['error'])[:80]}",
        )
        return ([], _STATUS_TRANSPORT)

    results = _normalize_results(payload if isinstance(payload, dict) else {})
    if not results:
        # 200 but nothing usable — the replacement signal for "ungrounded".
        await _record_search_telemetry(
            status="succeeded", started_at_perf=started,
            error_message="empty_search",
        )
        return ([], _STATUS_EMPTY)

    await _record_search_telemetry(status="succeeded", started_at_perf=started)
    return (results, _STATUS_OK)


async def search_web_many(
    queries: List[str],
) -> Tuple[List[Dict[str, str]], str]:
    """Run `queries` concurrently (bounded by `_SEARCH_CONCURRENCY`),
    dedupe results by url, return `(combined_results, status)`.

    status aggregation:
      - no_key          : SERPAPI_API_KEY unset (one mock_fallback row emitted)
      - ok              : >=1 query returned >=1 result
      - transport_error : 0 results AND >=1 query failed at transport
      - empty           : 0 results AND every query ran clean (data not found)
    """
    queries = [q for q in (queries or []) if q and q.strip()]
    if not queries:
        return ([], _STATUS_EMPTY)

    if not _resolve_search_api_key():
        # One mock_fallback row, not one per query — honest no-op.
        await _record_search_telemetry(
            status="mock_fallback", started_at_perf=time.perf_counter(),
            error_message="no_api_key",
        )
        return ([], _STATUS_NO_KEY)

    sem = _get_semaphore()

    async def _bounded(q: str) -> Tuple[List[Dict[str, str]], str]:
        async with sem:
            return await search_web(q)

    pairs = await asyncio.gather(*[_bounded(q) for q in queries])
    statuses = [s for _, s in pairs]

    combined: List[Dict[str, str]] = []
    seen: set = set()
    for results, _ in pairs:
        for item in results:
            key = item.get("url") or item.get("title")
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            combined.append(item)
    combined = combined[:_SEARCH_RESULT_CAP]

    if combined:
        return (combined, _STATUS_OK)
    # 0 results — distinguish a transport problem (retry-worthy) from a
    # genuine "the data isn't on the web" empty. If ANY query hit a
    # transport error, treat the whole batch as transport_error: better to
    # signal "we couldn't fully check" than "nothing found".
    if any(s == _STATUS_TRANSPORT for s in statuses):
        return ([], _STATUS_TRANSPORT)
    return ([], _STATUS_EMPTY)
