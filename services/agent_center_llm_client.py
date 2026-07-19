"""
Async client for the PIVOTA-Agent `/internal/agent-center/llm-probe`
endpoint. Wraps the V1 contract documented in
`PIVOTA-Agent/src/internal/agentCenterLlmProbe.js`.

Two production modes:

  - **Configured** (`PIVOTA_AGENT_INTERNAL_API_KEY` is set): make a real
    HTTP call to the configured `pivota_agent_internal_url`, returning the
    structured `result` block.
  - **Unconfigured** (key absent — typical for local dev / CI): return a
    deterministic local mock matching the same response schema, so the
    Demand Test pipeline stays end-to-end testable without network.

Either way the caller gets a `Dict[str, Any]` with the V1 keys
(`scan_mode`, `provider`, `runs_count`, `scores`, `findings`, `usage`,
`raw_runs`). The caller is responsible for translating findings into
`agent_center_issues` rows and `usage` into `agent_center_usage_events`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)


def _summarize_error_reasons(
    error_reasons: Any, *, max_len: int = 1000,
) -> Optional[str]:
    """Join the gateway's deduped `error_reasons` array into a single
    error_message string, truncated to the column width (1000 chars in
    db/llm_probe_runs.py). Defensive against shapes older/odd gateways
    might emit: a list, a bare string, or nothing.

    Returns a non-empty string when any reason text is present, else a
    stable "all_runs_failed" sentinel so a downgraded-to-failed row
    never has a null error_message."""
    text = ""
    if isinstance(error_reasons, (list, tuple)):
        text = "; ".join(str(r).strip() for r in error_reasons if r)
    elif error_reasons:
        text = str(error_reasons).strip()
    text = text or "all_runs_failed"
    return text[:max_len]


def _grounded_call_count(
    result: Optional[Dict[str, Any]],
    usage: Optional[Mapping[str, Any]],
) -> int:
    """Number of grounded provider calls one probe response represents.

    A single gateway probe bundles up to `runs_count` runs (see
    _dispatch's max_runs cap), and `usage.input_tokens` is SUMMED across
    them — so a flat per-call grounding surcharge must scale by the run
    count, not be charged once. Prefer `succeeded_runs` (grounded calls
    that actually returned; failed runs are left uncharged), falling back
    to `runs_count`, then 1 for older gateway shapes that omit both."""
    usage_block = usage if isinstance(usage, Mapping) else {}
    res = result if isinstance(result, dict) else {}
    for candidate in (usage_block.get("succeeded_runs"), res.get("succeeded_runs")):
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 0:
            return candidate
    try:
        runs_count = int(res.get("runs_count") or 0)
    except (TypeError, ValueError):
        runs_count = 0
    if runs_count > 0:
        return runs_count
    return 1


def _add_grounding_surcharge(
    cost_usd: Optional[Decimal],
    provider: str,
    result: Optional[Dict[str, Any]],
    usage: Optional[Mapping[str, Any]],
) -> Optional[Decimal]:
    """Add the server-side grounding surcharge to a token-based probe cost.

    #1505: Gemini bills `google_search` grounding as a flat per-request
    surcharge (~$0.035/grounded call) that is NOT captured in measured
    tokens — unlike ChatGPT/Claude web_search, whose retrieved content
    lands in input_tokens (so those must NOT get a separate fee, or COGS
    double-counts). #1802 made the Agent count Gemini's tokens, but the
    surcharge is still missing, understating Gemini per-provider cost ~50x.

    Config drives the decision: only providers flagged
    `grounding_fee_billed_separately` (Gemini today) get the additive fee,
    applied once per grounded run. Token counts are never touched. Returns
    cost_usd unchanged when there's no cost to add onto (None), the provider
    isn't a separately-billed grounded lane, or no grounded run happened."""
    if cost_usd is None:
        return cost_usd
    try:
        from services.provider_credit_rates import (
            provider_default_grounded,
            provider_grounding_billed_separately,
            provider_grounding_fee_usd_per_call,
        )
        if not (
            provider_default_grounded(provider)
            and provider_grounding_billed_separately(provider)
        ):
            return cost_usd
        fee_per_call = provider_grounding_fee_usd_per_call(provider)
        if fee_per_call <= 0:
            return cost_usd
        grounded_calls = _grounded_call_count(result, usage)
        if grounded_calls <= 0:
            return cost_usd
        total = cost_usd + fee_per_call * Decimal(grounded_calls)
        # Match Numeric(10, 6) precision of llm_probe_runs.cost_usd.
        return total.quantize(Decimal("0.000001"))
    except Exception:  # noqa: BLE001 — surcharge is best-effort like token cost
        return cost_usd


async def _record_probe_telemetry(
    *,
    provider: str,
    scan_mode: str,
    status: str,
    started_at_perf: float,
    result: Optional[Dict[str, Any]] = None,
    error_message: Optional[str] = None,
    model: Optional[str] = None,
) -> None:
    """P2.5b: best-effort telemetry record after every probe call.
    Pulls audit_run_id + merchant_id from the audit_telemetry context
    so we don't have to thread them through every call signature.

    Token + cost extraction: result['usage'] from upstream PIVOTA-Agent
    contains input_tokens + output_tokens when available. Cost is
    computed via provider_registry rates — when `model` is supplied
    and the provider has per-model rates, those are preferred over
    the provider-level fallback so non-headline SKUs (e.g.
    deepseek-reasoner vs deepseek-chat) aren't silently undercounted.
    When result/usage is missing (mock fallback, error path, partial
    response), the row is still inserted with None for the cost
    fields — telemetry coverage beats perfect cost data.
    """
    try:
        from db.llm_probe_runs import (
            STATUS_FAILED, STATUS_SUCCEEDED, compute_cost_usd,
            record_probe_run,
        )
        from services.audit_telemetry_context import (
            current_audit_context,
        )
        ctx = current_audit_context()
        latency_ms = int(
            (time.perf_counter() - started_at_perf) * 1000.0,
        )
        usage = (result or {}).get("usage") if isinstance(result, dict) else None
        input_tokens = None
        output_tokens = None
        cost_usd = None
        # --- un-metered-ChatGPT-COGS guard --------------------------------
        # A gateway HTTP-200 carrying a `result` block used to be recorded
        # as status="succeeded" even when every grounded run inside it
        # failed (e.g. OpenAI 429 quota-exceeded). Those runs persist 0
        # tokens / $0, making a fully-failed probe indistinguishable from a
        # free success. The gateway now surfaces per-run health as
        # succeeded_runs / failed_runs (both top-level and inside `usage`)
        # plus a deduped `error_reasons` array. When the caller reported
        # success but every run actually failed, downgrade to "failed" and
        # attach the upstream reasons. Older gateway responses lack these
        # fields → the values stay None → current behaviour is preserved.
        if status == STATUS_SUCCEEDED and isinstance(result, dict):
            usage_block = usage if isinstance(usage, dict) else {}
            succeeded_runs = usage_block.get("succeeded_runs")
            if succeeded_runs is None:
                succeeded_runs = result.get("succeeded_runs")
            failed_runs = usage_block.get("failed_runs")
            if failed_runs is None:
                failed_runs = result.get("failed_runs")
            try:
                runs_count = int(result.get("runs_count") or 0)
            except (TypeError, ValueError):
                runs_count = 0
            if runs_count > 0 and succeeded_runs == 0:
                # All runs failed — this is the un-metered-COGS case.
                status = STATUS_FAILED
                if not error_message:
                    error_message = _summarize_error_reasons(
                        result.get("error_reasons")
                    )
            elif (
                isinstance(failed_runs, int)
                and 0 < failed_runs < runs_count
                and result.get("error_reasons")
            ):
                # Partial failure: some runs produced real tokens/cost, so
                # keep status="succeeded" for accurate cost accounting, but
                # surface the upstream reasons so partial degradation is
                # visible in telemetry rather than silently dropped.
                if not error_message:
                    error_message = _summarize_error_reasons(
                        result.get("error_reasons")
                    )
        if isinstance(usage, dict):
            input_tokens = usage.get("input_tokens")
            output_tokens = usage.get("output_tokens")
            try:
                from services.llm_providers.provider_registry import (
                    get_provider,
                )
                p = get_provider(provider)
                if p is not None:
                    rates = p.rate_for_model(model)
                    cost_usd = compute_cost_usd(
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cost_per_1k_input_tokens_usd=rates["input_per_1k"],
                        cost_per_1k_output_tokens_usd=rates["output_per_1k"],
                    )
                    cost_usd = _add_grounding_surcharge(
                        cost_usd, provider, result, usage,
                    )
            except Exception:  # noqa: BLE001
                pass  # cost is nice-to-have; row still records
        await record_probe_run(
            provider=provider,
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
    except Exception as exc:  # noqa: BLE001 — telemetry never fails calls
        logger.debug(
            "_record_probe_telemetry suppressed error: %s",
            str(exc)[:200],
        )


# Retry once on transport-layer errors (connection reset, read error,
# remote-protocol error). These are typical during deploy overlap on
# Railway — when the upstream container is rolling, in-flight TCP
# connections get reset and httpx surfaces an empty-message
# RemoteProtocolError. One retry buys us through a single rolling
# restart without making the user re-run a 30-second BD report.
#
# httpx.TimeoutException IS retried (ReadTimeout / ConnectTimeout /
# WriteTimeout / PoolTimeout all subclass it). The prior code
# excluded timeouts on the reasoning "a slow Gemini call shouldn't
# double the wall-clock cost" — but that conflates two things:
#   - a slow-but-SUCCESSFUL call: correct not to retry (we got a result)
#   - a ReadTimeout: the call FAILED — the window was spent for zero
#     result. Not retrying just drops the product from the audit.
# When a timeout fires we've already lost the time; a single retry
# has a real chance of producing an actual result, and a lost
# product is worse than a slow audit. The retry stays inside the
# already-held per-merchant + global semaphores (no concurrency
# blow-out) and is bounded at one extra attempt — this is NOT the
# per-query call multiplier the LLM-cost-safety rule guards against;
# the retry only fires when a call has already failed, never on the
# happy path. (Prod incident 2026-05-14: audit f33b6069 had many
# products fail mid-audit on `ReadTimeout('')`.)
_TRANSPORT_RETRY_EXCS = (
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.ConnectError,
    httpx.NetworkError,
    httpx.TimeoutException,
)
_RETRY_BACKOFF_S = 0.5


# Concurrency caps (Phase C prerequisite).
# Per feedback_llm_call_multipliers.md: PR #278 took the backend down
# when uncapped concurrent probes saturated the upstream LLM provider.
# Two semaphores: a single global cap (across all merchants) bounds
# overall LLM-provider load; a per-merchant lazy-init cap prevents
# any one merchant's audit from monopolizing the backend.
#
# Caps are read from settings on first use (one-shot evaluation) so
# tests + ops can monkeypatch the settings before any probes fire.
_GLOBAL_SEMAPHORE: Optional[asyncio.Semaphore] = None
_PER_MERCHANT_SEMAPHORES: Dict[str, asyncio.Semaphore] = {}
_PER_MERCHANT_LOCK = asyncio.Lock()


def _get_global_semaphore() -> asyncio.Semaphore:
    global _GLOBAL_SEMAPHORE
    if _GLOBAL_SEMAPHORE is None:
        raw = settings.llm_probe_global_max_concurrent
        # Treat None as default; `or 30` would also catch 0, masking
        # a misconfigured cap. Clamp 0/negative to 1 to avoid a
        # deadlock-on-every-probe footgun.
        cap = 30 if raw is None else int(raw)
        _GLOBAL_SEMAPHORE = asyncio.Semaphore(max(1, cap))
    return _GLOBAL_SEMAPHORE


async def _get_per_merchant_semaphore(merchant_id: str) -> asyncio.Semaphore:
    """Lazy-init per merchant_id. Bounded growth: each merchant gets
    one Semaphore; the dict grows once per active merchant. Acceptable
    for current scale (hundreds, not millions). If we ever hit
    pressure, an LRU eviction pass is the natural follow-up."""
    key = (merchant_id or "").strip() or "_unknown_"
    sem = _PER_MERCHANT_SEMAPHORES.get(key)
    if sem is not None:
        return sem
    async with _PER_MERCHANT_LOCK:
        # Re-check inside lock to avoid double-init on a race.
        sem = _PER_MERCHANT_SEMAPHORES.get(key)
        if sem is not None:
            return sem
        raw = settings.llm_probe_per_merchant_max_concurrent
        cap = 5 if raw is None else int(raw)
        sem = asyncio.Semaphore(max(1, cap))
        _PER_MERCHANT_SEMAPHORES[key] = sem
        return sem


def _reset_concurrency_caps_for_test() -> None:
    """Test hook — drop semaphore state between tests so monkeypatched
    cap settings take effect on the next probe."""
    global _GLOBAL_SEMAPHORE
    _GLOBAL_SEMAPHORE = None
    _PER_MERCHANT_SEMAPHORES.clear()


# Mirror of `PRIMARY_ISSUE_TYPE_BY_SCAN_MODE` in
# `PIVOTA-Agent/src/internal/agentCenterLlmProbe.js`. Used by the local-mock
# fallback so the pipeline still produces a sensible synthetic finding when
# the internal endpoint isn't reachable from this environment.
PRIMARY_ISSUE_TYPE_BY_SCAN_MODE: Dict[str, str] = {
    "open_product_visibility_test": "ai_visibility_loss",
    "merchant_store_attribution_test": "merchant_store_attribution_gap",
    "pivota_pdp_attribution_test": "pivota_pdp_attribution_gap",
    "search_grounded_product_discovery_test": "ai_visibility_loss",
}


class AgentCenterLlmClientError(RuntimeError):
    """Raised when the LLM probe call fails for a reason that should
    propagate to the caller as a 502/503-shaped service error rather than
    silently degrading to a mock response."""


def _resolve_endpoint_url() -> str:
    base = (settings.pivota_agent_internal_url or "").rstrip("/")
    return f"{base}/internal/agent-center/llm-probe"


def _local_mock_result(
    scan_mode: str,
    max_runs: int,
    *,
    note_provider: str = "local_mock_no_internal_key",
) -> Dict[str, Any]:
    """Return a deterministic stub matching the V1 response schema, used
    when no `PIVOTA_AGENT_INTERNAL_API_KEY` is configured."""
    issue_type = PRIMARY_ISSUE_TYPE_BY_SCAN_MODE.get(scan_mode)
    findings: List[Dict[str, Any]] = []
    if issue_type:
        findings.append(
            {
                "issue_type": issue_type,
                "severity": "medium",
                "evidence": {
                    "mock": True,
                    "note": (
                        "Synthesised by pivota-backend's local mock fallback "
                        "(no PIVOTA_AGENT_INTERNAL_API_KEY configured)."
                    ),
                },
            }
        )
    return {
        "scan_mode": scan_mode,
        "provider": note_provider,
        "runs_count": max(1, min(int(max_runs), 8)),
        "scores": {"visibility_score": 50, "attribution_echo_rate": 0},
        "findings": findings,
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "raw_runs": [],
    }


async def _probe_via_deepseek(
    *,
    scan_mode: str,
    context: Optional[Mapping[str, Any]],
    merchant_id: str,
    max_runs: int,
    timeout_s: Optional[float] = None,
) -> Dict[str, Any]:
    """PR-3a Deepseek dispatch — runs a Deepseek probe through the
    backend-direct client, applying the same per-merchant + global
    semaphores as the upstream-routed Gemini probe.

    Pulls product/brand/URL out of `context` (caller-supplied):
      - product_title (required for visibility/attribution scan modes)
      - product_type (required for category_visibility_test)
      - merchant_brand
      - merchant_pdp_url

    Returns the V1 result dict matching the upstream Gemini probe so
    the downstream BD report builder consumes it identically.
    """
    from services.llm_providers.deepseek_probe import (
        DeepseekProbeError, probe_one_scan_mode,
    )
    api_key = (settings.deepseek_api_key or "").strip()
    if not api_key:
        raise AgentCenterLlmClientError(
            "DEEPSEEK_API_KEY is not configured; cannot route "
            "provider='deepseek' probes."
        )
    ctx = dict(context or {})
    prod = ctx.get("product") or {}
    product_title = (
        ctx.get("product_title") or prod.get("title") or ""
    ).strip()
    product_type = ctx.get("product_type") or prod.get("product_type")
    merchant_brand = ctx.get("merchant_brand") or prod.get("vendor")
    merchant_pdp_url = ctx.get("merchant_pdp_url")
    if not product_title:
        # Without product context the probe queries are meaningless;
        # surface as caller error so the route layer maps to 422.
        raise ValueError(
            "context.product_title is required when provider='deepseek'"
        )
    timeout = float(
        timeout_s if timeout_s is not None
        else settings.agent_center_llm_probe_timeout_s
    )
    global_sem = _get_global_semaphore()
    per_merchant_sem = await _get_per_merchant_semaphore(merchant_id)
    async with per_merchant_sem:
        async with global_sem:
            try:
                return await probe_one_scan_mode(
                    scan_mode=scan_mode,
                    product_title=product_title,
                    product_type=product_type,
                    merchant_brand=merchant_brand,
                    merchant_pdp_url=merchant_pdp_url,
                    verify_query=ctx.get("verify_query"),
                    verify_answer_text=ctx.get("verify_answer_text"),
                    verify_evidence_excerpt=ctx.get("verify_evidence_excerpt"),
                    verify_intent=ctx.get("verify_intent"),
                    max_runs=max_runs,
                    api_key=api_key,
                    timeout_s=timeout,
                )
            except DeepseekProbeError as exc:
                raise AgentCenterLlmClientError(
                    f"Deepseek probe failed: {exc}"
                ) from exc


async def probe(
    *,
    scan_mode: str,
    scan_target_id: str,
    merchant_id: str,
    store_id: str,
    context: Optional[Mapping[str, Any]] = None,
    provider: str,
    max_runs: int = 3,
    model: Optional[str] = None,
    model_is_override: bool = False,
    timeout_s: Optional[float] = None,
    allow_local_mock: bool = False,
) -> Dict[str, Any]:
    """Run an LLM probe via PIVOTA-Agent.

    Returns the V1 `result` dict. On any unexpected upstream failure (HTTP
    >=500, timeout, network error) raises `AgentCenterLlmClientError`. On
    explicit caller-side misuse (the upstream returns 4xx) raises
    `ValueError` so the route layer maps it to 400/422.

    `provider` is required (was: defaulted to "mock", which was a footgun
    for callers that forgot to set it — they'd silently ask the upstream
    for synthetic data).

    `allow_local_mock=False` is the default so callers fail loudly when
    `PIVOTA_AGENT_INTERNAL_API_KEY` is unset. Demand-test runner opts
    in via `allow_local_mock=True` because its product surface
    deliberately uses stub responses (`stub_complete` status) on free-
    tier preview calls. Merchant audit + BD report MUST keep the
    default to never produce fabricated prose against synthetic data.

    **Provider dispatch (PR-3a):** `provider="deepseek"` routes to a
    backend-direct Deepseek client (services/llm_providers/
    deepseek_probe.py), bypassing the upstream PIVOTA-Agent codex
    stack. The result shape is normalized to V1 so downstream code
    (scorers, report builder) consumes Deepseek results identically.
    Other providers ("gemini", "mock", future "chatgpt"/"claude")
    continue to route through PIVOTA-Agent via HTTP.
    """
    # PR-7-orchestrator: when caller passes provider="auto" (or
    # "auto:strategy"), let the orchestrator pick the actual provider
    # based on scan_mode + cost + capability. Existing callers passing
    # a specific provider id ("gemini", "deepseek") bypass this and
    # route directly — backwards compatible.
    if provider.startswith("auto"):
        from services.llm_providers.orchestrator import (
            STRATEGY_SINGLE_BEST,
            parse_provider_spec,
            select_provider,
        )
        _, strategy_override = parse_provider_spec(provider)
        chosen_strategy = strategy_override or STRATEGY_SINGLE_BEST
        chosen_provider = select_provider(
            scan_mode=scan_mode,
            strategy=chosen_strategy,
            merchant_id=merchant_id,
        )
        logger.info(
            "orchestrator: selected provider=%s for scan_mode=%s "
            "strategy=%s merchant_id=%s",
            chosen_provider, scan_mode, chosen_strategy, merchant_id,
        )
        # Recurse with the resolved provider id; subsequent dispatch
        # logic (Deepseek branch + upstream HTTP fallthrough) handles
        # the actual call.
        return await probe(
            scan_mode=scan_mode,
            scan_target_id=scan_target_id,
            merchant_id=merchant_id,
            store_id=store_id,
            context=context,
            provider=chosen_provider,
            max_runs=max_runs,
            model=model,
            model_is_override=model_is_override,
            timeout_s=timeout_s,
            allow_local_mock=allow_local_mock,
        )

    # PR-3a Deepseek dispatch: backend-direct, no upstream HTTP call.
    if provider == "deepseek":
        return await _probe_via_deepseek(
            scan_mode=scan_mode,
            context=context,
            merchant_id=merchant_id,
            max_runs=max_runs,
            timeout_s=timeout_s,
        )

    body: Dict[str, Any] = {
        "scan_mode": scan_mode,
        "scan_target_id": scan_target_id,
        "merchant_id": merchant_id,
        "store_id": store_id,
        "context": dict(context or {}),
        "options": {"provider": provider, "max_runs": int(max_runs)},
    }
    resolved_model = str(model or "").strip() or None
    if resolved_model:
        body["options"]["model"] = resolved_model
    api_key = (settings.pivota_agent_internal_api_key or "").strip()
    if not api_key:
        if not allow_local_mock:
            # Fail loudly. Production must always configure the key.
            # Without this, callers that don't explicitly opt in to
            # mock would silently render audit/report prose against
            # synthetic data — fabricating user-facing content.
            # Name ALL accepted env vars (and the preferred one) — the key is
            # resolved from a priority chain in config.settings, so naming only
            # the legacy candidate sends operators to set the wrong var.
            raise AgentCenterLlmClientError(
                "No PIVOTA-Agent internal API key is configured — set "
                "PROMOTIONS_ADMIN_KEY (preferred; or AGENT_API_KEY / "
                "PIVOTA_AGENT_INTERNAL_API_KEY) on this service so audit "
                "probes can authenticate to the grounded-search gateway. "
                "Refusing to fall back to local mock data. Pass "
                "allow_local_mock=True only if your caller explicitly "
                "handles synthetic responses (e.g., the demand-test "
                "runner marking results as stub_complete)."
            )
        logger.error(
            "No PIVOTA-Agent internal API key configured (set "
            "PROMOTIONS_ADMIN_KEY / AGENT_API_KEY / "
            "PIVOTA_AGENT_INTERNAL_API_KEY); using local mock for "
            "scan_target=%s scan_mode=%s (allow_local_mock=True)",
            scan_target_id,
            scan_mode,
        )
        # P2.5b: record mock-fallback as a telemetry row so global
        # cost-cap accounting doesn't get fooled into thinking nothing
        # was spent. Latency for mock is negligible but not zero —
        # use 0 so the row is unambiguously distinguishable from real
        # provider calls.
        from services.audit_telemetry_context import (
            current_audit_context,
        )
        try:
            from db.llm_probe_runs import record_probe_run
            ctx = current_audit_context()
            await record_probe_run(
                provider="mock", scan_mode=scan_mode,
                status="mock_fallback",
                merchant_id=ctx.merchant_id,
                audit_run_id=ctx.audit_run_id,
                latency_ms=0,
            )
        except Exception:  # noqa: BLE001
            pass
        result = _local_mock_result(scan_mode, max_runs)
        if resolved_model:
            result["model"] = resolved_model
            result["model_is_override"] = bool(model_is_override)
        return result

    url = _resolve_endpoint_url()
    timeout = float(timeout_s if timeout_s is not None else settings.agent_center_llm_probe_timeout_s)
    headers = {
        "Content-Type": "application/json",
        "X-Pivota-Internal-Key": api_key,
    }

    # Concurrency caps: acquire per-merchant FIRST (lower priority,
    # so one merchant's wait doesn't block other merchants from
    # acquiring the global slot), then global. Both are released
    # after the HTTP call completes (incl. retries). Order matters
    # for fairness — global last → released first → other merchants
    # can move forward immediately after this probe finishes.
    global_sem = _get_global_semaphore()
    per_merchant_sem = await _get_per_merchant_semaphore(merchant_id)
    response = None
    last_exc: Optional[BaseException] = None
    # P2.5b: start latency timer BEFORE semaphore acquisition so wait
    # time for a busy global slot also counts toward observed latency
    # — matters for tuning the per-merchant + global concurrency caps.
    _telemetry_started_at = time.perf_counter()
    async with per_merchant_sem:
        async with global_sem:
            for attempt in (1, 2):
                try:
                    async with httpx.AsyncClient(timeout=timeout) as client:
                        response = await client.post(url, json=body, headers=headers)
                    break
                except _TRANSPORT_RETRY_EXCS as exc:
                    last_exc = exc
                    logger.warning(
                        "llm probe transport retry: scan_mode=%s attempt=%s/%s exc=%s(%r)",
                        scan_mode,
                        attempt,
                        2,
                        type(exc).__name__,
                        str(exc),
                    )
                    if attempt == 2:
                        break
                    await asyncio.sleep(_RETRY_BACKOFF_S)
                except httpx.HTTPError as exc:
                    # Genuinely non-retryable httpx error — e.g. DecodingError,
                    # TooManyRedirects. Timeouts + transport errors are caught
                    # by the _TRANSPORT_RETRY_EXCS branch above; only what
                    # falls through to here is unrecoverable. Surface
                    # immediately with the exception class named so logs
                    # aren't blank.
                    raise AgentCenterLlmClientError(
                        f"llm probe transport failed ({type(exc).__name__}): {exc!r}"
                    ) from exc
    if response is None:
        # All retries exhausted on retryable transport errors.
        await _record_probe_telemetry(
            provider=provider, scan_mode=scan_mode, status="failed",
            started_at_perf=_telemetry_started_at,
            model=resolved_model,
            error_message=(
                f"transport_failed: "
                f"{type(last_exc).__name__ if last_exc else 'unknown'}"
            ),
        )
        raise AgentCenterLlmClientError(
            f"llm probe transport failed after retry "
            f"({type(last_exc).__name__ if last_exc else 'unknown'}): {last_exc!r}"
        ) from last_exc

    if response.status_code >= 500:
        await _record_probe_telemetry(
            provider=provider, scan_mode=scan_mode, status="failed",
            started_at_perf=_telemetry_started_at,
            model=resolved_model,
            error_message=f"upstream_5xx_{response.status_code}",
        )
        raise AgentCenterLlmClientError(
            f"llm probe upstream {response.status_code}: {response.text[:200]}"
        )
    if response.status_code >= 400:
        # Upstream rejected the request (bad scan_mode / missing field /
        # auth mismatch). Surface as ValueError so the route layer turns
        # it into 400.
        await _record_probe_telemetry(
            provider=provider, scan_mode=scan_mode, status="failed",
            started_at_perf=_telemetry_started_at,
            model=resolved_model,
            error_message=f"upstream_4xx_{response.status_code}",
        )
        raise ValueError(
            f"llm probe rejected by upstream ({response.status_code}): "
            f"{response.text[:200]}"
        )

    try:
        payload = response.json()
    except Exception as exc:
        await _record_probe_telemetry(
            provider=provider, scan_mode=scan_mode, status="failed",
            started_at_perf=_telemetry_started_at,
            model=resolved_model,
            error_message=f"non_json_response: {exc}",
        )
        raise AgentCenterLlmClientError(f"llm probe non-JSON response: {exc}") from exc

    if not isinstance(payload, dict) or not payload.get("ok"):
        await _record_probe_telemetry(
            provider=provider, scan_mode=scan_mode, status="failed",
            started_at_perf=_telemetry_started_at,
            model=resolved_model,
            error_message="response_not_ok",
        )
        raise AgentCenterLlmClientError(
            f"llm probe response not ok: {str(payload)[:200]}"
        )

    result = payload.get("result")
    if not isinstance(result, dict):
        await _record_probe_telemetry(
            provider=provider, scan_mode=scan_mode, status="failed",
            started_at_perf=_telemetry_started_at,
            model=resolved_model,
            error_message="missing_result_object",
        )
        raise AgentCenterLlmClientError("llm probe response missing `result` object")

    if resolved_model:
        result.setdefault("model", resolved_model)
        result.setdefault("model_is_override", bool(model_is_override))

    # Success — record cost/usage telemetry then return.
    await _record_probe_telemetry(
        provider=provider, scan_mode=scan_mode, status="succeeded",
        started_at_perf=_telemetry_started_at,
        result=result,
        model=resolved_model,
    )
    return result
