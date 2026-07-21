"""#1505 — Gemini google_search grounding surcharge in llm_probe_runs.cost_usd.

Gemini bills its `google_search` grounding as a flat server-side surcharge per
grounded request (~$0.035/call), independent of tokens. #1802 made the Agent
count Gemini's tokens, but that per-call fee was still absent from the recorded
`cost_usd` — understating Gemini per-provider COGS ~50x in report.cost_summary.

The actual-usage cost path (services.agent_center_llm_client._record_probe_telemetry
-> compute_cost_usd) now adds the config-sourced surcharge, once per grounded run,
ONLY for providers flagged `grounding_fee_billed_separately` (Gemini). ChatGPT /
Claude web_search grounding is billed AS input tokens, so they must stay unchanged
(adding a fee there would double-count and regress #1507).
"""

from __future__ import annotations

import copy
from typing import Any, Dict

import pytest


def _result(
    *,
    provider: str,
    in_tokens: int,
    out_tokens: int,
    runs_count: int = 1,
    succeeded_runs: Any = None,
) -> Dict[str, Any]:
    """Gateway-shaped result dict. `usage.input_tokens` is the SUM across
    `runs_count` runs; `succeeded_runs` (when set) is the grounded-call count
    the surcharge scales by."""
    usage: Dict[str, Any] = {
        "input_tokens": in_tokens,
        "output_tokens": out_tokens,
    }
    if succeeded_runs is not None:
        usage["succeeded_runs"] = succeeded_runs
        usage["failed_runs"] = max(runs_count - int(succeeded_runs), 0)
    result: Dict[str, Any] = {
        "scan_mode": "open_product_visibility_test",
        "provider": provider,
        "runs_count": runs_count,
        "scores": {"visibility_score": 50},
        "findings": [],
        "usage": usage,
        "raw_runs": [],
    }
    if succeeded_runs is not None:
        result["succeeded_runs"] = succeeded_runs
        result["failed_runs"] = max(runs_count - int(succeeded_runs), 0)
    return result


@pytest.fixture
def capture_record(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    import db.llm_probe_runs as lpr

    captured: Dict[str, Any] = {}

    async def fake_record(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "fake-probe-id"

    monkeypatch.setattr(lpr, "record_probe_run", fake_record)
    return captured


def _config_fee(provider: str) -> float:
    from services.provider_credit_rates import provider_grounding_fee_usd_per_call

    return float(provider_grounding_fee_usd_per_call(provider))


def _token_cost(provider: str, in_tokens: int, out_tokens: int) -> float:
    from services.llm_providers.provider_registry import get_provider

    rates = get_provider(provider).rate_for_model(None)
    return (
        (in_tokens / 1000.0) * rates["input_per_1k"]
        + (out_tokens / 1000.0) * rates["output_per_1k"]
    )


@pytest.mark.asyncio
async def test_grounded_gemini_cost_is_tokens_plus_grounding_fee(
    capture_record: Dict[str, Any],
) -> None:
    """A grounded Gemini probe records cost = token COGS + grounding fee per
    grounded run (succeeded_runs)."""
    from services import agent_center_llm_client as client

    await client._record_probe_telemetry(
        provider="gemini",
        scan_mode="open_product_visibility_test",
        status="succeeded",
        started_at_perf=0.0,
        result=_result(
            provider="gemini",
            in_tokens=1000,
            out_tokens=200,
            runs_count=3,
            succeeded_runs=3,
        ),
    )

    fee = _config_fee("gemini")
    assert fee > 0
    expected = _token_cost("gemini", 1000, 200) + 3 * fee
    assert float(capture_record["cost_usd"]) == pytest.approx(expected)
    # Tokens themselves are untouched (surcharge is additive, not token-inflating).
    assert capture_record["input_tokens"] == 1000
    assert capture_record["output_tokens"] == 200


@pytest.mark.asyncio
async def test_grounding_fee_scales_by_grounded_call_count(
    capture_record: Dict[str, Any],
) -> None:
    """One row can bundle multiple grounded runs (input_tokens summed); the flat
    fee scales by succeeded_runs, not charged once."""
    from services import agent_center_llm_client as client

    await client._record_probe_telemetry(
        provider="gemini",
        scan_mode="open_product_visibility_test",
        status="succeeded",
        started_at_perf=0.0,
        result=_result(
            provider="gemini",
            in_tokens=350,
            out_tokens=120,
            runs_count=1,
            succeeded_runs=1,
        ),
    )
    single = float(capture_record["cost_usd"])

    capture_record.clear()
    await client._record_probe_telemetry(
        provider="gemini",
        scan_mode="open_product_visibility_test",
        status="succeeded",
        started_at_perf=0.0,
        result=_result(
            provider="gemini",
            in_tokens=350,
            out_tokens=120,
            runs_count=5,
            succeeded_runs=5,
        ),
    )
    five = float(capture_record["cost_usd"])

    fee = _config_fee("gemini")
    # 4 extra grounded runs => 4 extra flat fees (token portion identical here).
    assert five - single == pytest.approx(4 * fee)


@pytest.mark.asyncio
async def test_grounded_chatgpt_cost_unchanged_no_double_count(
    capture_record: Dict[str, Any],
) -> None:
    """ChatGPT web_search grounding is billed AS input tokens, so its cost must
    stay pure token COGS — NO separate surcharge (regression guard for #1507)."""
    from services import agent_center_llm_client as client

    await client._record_probe_telemetry(
        provider="chatgpt",
        scan_mode="open_product_visibility_test",
        status="succeeded",
        started_at_perf=0.0,
        result=_result(
            provider="chatgpt",
            in_tokens=15000,
            out_tokens=260,
            runs_count=3,
            succeeded_runs=3,
        ),
    )

    expected = _token_cost("chatgpt", 15000, 260)
    assert float(capture_record["cost_usd"]) == pytest.approx(expected)


@pytest.mark.asyncio
async def test_grounded_claude_cost_unchanged_no_double_count(
    capture_record: Dict[str, Any],
) -> None:
    """Claude web_search grounding is also billed as input tokens — no fee."""
    from services import agent_center_llm_client as client

    await client._record_probe_telemetry(
        provider="claude",
        scan_mode="open_product_visibility_test",
        status="succeeded",
        started_at_perf=0.0,
        result=_result(
            provider="claude",
            in_tokens=15000,
            out_tokens=260,
            runs_count=1,
            succeeded_runs=1,
        ),
    )

    expected = _token_cost("claude", 15000, 260)
    assert float(capture_record["cost_usd"]) == pytest.approx(expected)


@pytest.mark.asyncio
async def test_missing_usage_records_none_cost_no_crash(
    capture_record: Dict[str, Any],
) -> None:
    """No usage block => cost_usd stays None (surcharge never fabricates a cost
    out of nothing)."""
    from services import agent_center_llm_client as client

    await client._record_probe_telemetry(
        provider="gemini",
        scan_mode="open_product_visibility_test",
        status="succeeded",
        started_at_perf=0.0,
        result={
            "scan_mode": "open_product_visibility_test",
            "provider": "gemini",
            "runs_count": 1,
            "scores": {},
            "findings": [],
            "raw_runs": [],
        },
    )
    assert capture_record["cost_usd"] is None


@pytest.mark.asyncio
async def test_grounding_fee_is_sourced_from_config(
    capture_record: Dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Change the config grounding fee => recorded Gemini cost tracks it. Proves
    the surcharge is config-driven, not a hardcoded constant."""
    from services import agent_center_llm_client as client
    from services import provider_credit_rates as pcr

    base_config = copy.deepcopy(pcr.load_provider_credit_config())
    base_config["provider_credit_rates"]["gemini"][
        "grounding_cost_usd_per_call"
    ] = 0.05
    monkeypatch.setattr(pcr, "load_provider_credit_config", lambda: base_config)

    await client._record_probe_telemetry(
        provider="gemini",
        scan_mode="open_product_visibility_test",
        status="succeeded",
        started_at_perf=0.0,
        result=_result(
            provider="gemini",
            in_tokens=1000,
            out_tokens=200,
            runs_count=3,
            succeeded_runs=3,
        ),
    )

    expected = _token_cost("gemini", 1000, 200) + 3 * 0.05
    assert float(capture_record["cost_usd"]) == pytest.approx(expected)
