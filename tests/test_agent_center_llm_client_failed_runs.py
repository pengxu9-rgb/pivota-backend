"""Tests for the un-metered-ChatGPT-COGS guard in agent_center_llm_client.

The motivating bug: the gateway returns HTTP 200 with a `result` block
even when every grounded run inside it failed (e.g. OpenAI 429
quota-exceeded). The old consumer recorded that as status="succeeded"
with 0 tokens / $0 — indistinguishable from a genuinely-free success, so
ChatGPT COGS went silently un-metered.

The gateway now surfaces per-run health: top-level + usage-level
`succeeded_runs` / `failed_runs`, plus a deduped `error_reasons` array
(see PIVOTA-Agent src/internal/agentCenterLlmProbe.js). The consumer
downgrades an all-runs-failed result to status="failed" with the upstream
reasons as error_message, while still recording genuine successes as
"succeeded" and tolerating older gateway responses that lack the fields.
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import patch

import httpx
import pytest


def _result(
    *,
    runs_count: int,
    succeeded_runs: Any,
    failed_runs: Any,
    error_reasons: Any = None,
    in_tokens: int = 0,
    out_tokens: int = 0,
    include_in_usage: bool = True,
    include_top_level: bool = True,
) -> Dict[str, Any]:
    """Build a V1 result dict matching the gateway shape, with control
    over whether the run-health fields appear top-level / in usage so we
    can exercise the fallback + older-gateway paths."""
    usage: Dict[str, Any] = {
        "input_tokens": in_tokens,
        "output_tokens": out_tokens,
    }
    if include_in_usage:
        usage["succeeded_runs"] = succeeded_runs
        usage["failed_runs"] = failed_runs
    result: Dict[str, Any] = {
        "scan_mode": "open_product_visibility_test",
        "provider": "chatgpt",
        "runs_count": runs_count,
        "scores": {"visibility_score": 0, "attribution_echo_rate": 0},
        "findings": [],
        "usage": usage,
        "raw_runs": [],
    }
    if include_top_level:
        result["succeeded_runs"] = succeeded_runs
        result["failed_runs"] = failed_runs
        if error_reasons is not None:
            result["error_reasons"] = error_reasons
    elif error_reasons is not None:
        # error_reasons only lives top-level in the gateway shape; allow
        # tests to attach it even when run counts are usage-only.
        result["error_reasons"] = error_reasons
    return result


@pytest.fixture
def capture_record(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """Patch db.llm_probe_runs.record_probe_run to capture the kwargs the
    telemetry helper passes — the import inside _record_probe_telemetry
    reads the module attribute at call time, so this patch takes effect."""
    import db.llm_probe_runs as lpr

    captured: Dict[str, Any] = {}

    async def fake_record(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "fake-probe-id"

    monkeypatch.setattr(lpr, "record_probe_run", fake_record)
    return captured


# ---------------------------------------------------------------------------
# _record_probe_telemetry — direct unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_runs_failed_records_failed_with_error_message(
    capture_record: Dict[str, Any],
) -> None:
    """succeeded_runs=0, failed_runs=N, error_reasons populated → the
    caller-declared "succeeded" is downgraded to "failed" with a non-null
    error_message drawn from the upstream reasons."""
    from services import agent_center_llm_client as client

    await client._record_probe_telemetry(
        provider="chatgpt",
        scan_mode="open_product_visibility_test",
        status="succeeded",
        started_at_perf=0.0,
        result=_result(
            runs_count=3,
            succeeded_runs=0,
            failed_runs=3,
            error_reasons=[
                "OpenAI 429 quota-exceeded",
                "OpenAI 429 quota-exceeded",  # de-dup happens upstream
                "OpenAI 500 server_error",
            ],
        ),
    )

    assert capture_record["status"] == "failed"
    assert capture_record["error_message"]  # non-null, non-empty
    assert "quota-exceeded" in capture_record["error_message"]
    assert "server_error" in capture_record["error_message"]


@pytest.mark.asyncio
async def test_all_runs_failed_uses_top_level_fields_as_fallback(
    capture_record: Dict[str, Any],
) -> None:
    """Run-health counts only at the top level (not inside usage) → the
    fallback read still detects the all-failed case."""
    from services import agent_center_llm_client as client

    await client._record_probe_telemetry(
        provider="chatgpt",
        scan_mode="open_product_visibility_test",
        status="succeeded",
        started_at_perf=0.0,
        result=_result(
            runs_count=2,
            succeeded_runs=0,
            failed_runs=2,
            error_reasons=["upstream timeout"],
            include_in_usage=False,
            include_top_level=True,
        ),
    )

    assert capture_record["status"] == "failed"
    assert "timeout" in capture_record["error_message"]


@pytest.mark.asyncio
async def test_all_runs_failed_without_reasons_uses_sentinel(
    capture_record: Dict[str, Any],
) -> None:
    """All runs failed but error_reasons is empty/missing → still record
    "failed" with a stable sentinel so the row never has a null
    error_message."""
    from services import agent_center_llm_client as client

    await client._record_probe_telemetry(
        provider="chatgpt",
        scan_mode="open_product_visibility_test",
        status="succeeded",
        started_at_perf=0.0,
        result=_result(
            runs_count=3,
            succeeded_runs=0,
            failed_runs=3,
            error_reasons=[],
        ),
    )

    assert capture_record["status"] == "failed"
    assert capture_record["error_message"] == "all_runs_failed"


@pytest.mark.asyncio
async def test_fully_successful_records_succeeded(
    capture_record: Dict[str, Any],
) -> None:
    """succeeded_runs=N, failed_runs=0 → status stays "succeeded" and the
    real token counts are recorded for cost accounting."""
    from services import agent_center_llm_client as client

    await client._record_probe_telemetry(
        provider="chatgpt",
        scan_mode="open_product_visibility_test",
        status="succeeded",
        started_at_perf=0.0,
        result=_result(
            runs_count=3,
            succeeded_runs=3,
            failed_runs=0,
            in_tokens=1200,
            out_tokens=400,
        ),
    )

    assert capture_record["status"] == "succeeded"
    assert capture_record["error_message"] is None
    assert capture_record["input_tokens"] == 1200
    assert capture_record["output_tokens"] == 400


@pytest.mark.asyncio
async def test_partial_failure_stays_succeeded_but_surfaces_reasons(
    capture_record: Dict[str, Any],
) -> None:
    """0 < failed_runs < runs_count → keep "succeeded" (some runs produced
    real tokens/cost) but attach the upstream reasons so partial
    degradation is visible in telemetry."""
    from services import agent_center_llm_client as client

    await client._record_probe_telemetry(
        provider="chatgpt",
        scan_mode="open_product_visibility_test",
        status="succeeded",
        started_at_perf=0.0,
        result=_result(
            runs_count=3,
            succeeded_runs=2,
            failed_runs=1,
            error_reasons=["OpenAI 429 quota-exceeded"],
            in_tokens=800,
            out_tokens=200,
        ),
    )

    assert capture_record["status"] == "succeeded"
    assert "quota-exceeded" in capture_record["error_message"]
    assert capture_record["input_tokens"] == 800


@pytest.mark.asyncio
async def test_older_gateway_without_run_health_keeps_succeeded(
    capture_record: Dict[str, Any],
) -> None:
    """An older gateway response with no succeeded_runs/failed_runs/
    error_reasons fields → values stay None → current behaviour is
    preserved (status "succeeded", no error_message)."""
    from services import agent_center_llm_client as client

    await client._record_probe_telemetry(
        provider="chatgpt",
        scan_mode="open_product_visibility_test",
        status="succeeded",
        started_at_perf=0.0,
        result={
            "scan_mode": "open_product_visibility_test",
            "provider": "chatgpt",
            "runs_count": 3,
            "scores": {"visibility_score": 50},
            "findings": [],
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "raw_runs": [],
        },
    )

    assert capture_record["status"] == "succeeded"
    assert capture_record["error_message"] is None
    assert capture_record["input_tokens"] == 100


@pytest.mark.asyncio
async def test_explicit_failed_status_is_not_resurrected(
    capture_record: Dict[str, Any],
) -> None:
    """A caller passing status="failed" (e.g. transport/5xx error paths)
    must never be flipped back to succeeded by the run-health logic, which
    only runs on the success path."""
    from services import agent_center_llm_client as client

    await client._record_probe_telemetry(
        provider="chatgpt",
        scan_mode="open_product_visibility_test",
        status="failed",
        started_at_perf=0.0,
        error_message="upstream_5xx_503",
    )

    assert capture_record["status"] == "failed"
    assert capture_record["error_message"] == "upstream_5xx_503"


# ---------------------------------------------------------------------------
# End-to-end through probe() — the gateway success path (~line 660) wires
# the result into _record_probe_telemetry(status="succeeded").
# ---------------------------------------------------------------------------


@pytest.fixture
def configured_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from services import agent_center_llm_client as llm_client

    monkeypatch.setattr(
        llm_client.settings, "pivota_agent_internal_api_key", "test-secret"
    )


@pytest.mark.asyncio
async def test_probe_all_failed_runs_records_failed_telemetry(
    configured_api_key: None, capture_record: Dict[str, Any],
) -> None:
    """A gateway HTTP-200 whose every run failed flows through probe() and
    is persisted as status="failed" — the un-metered-COGS bug is fixed
    end-to-end. probe() itself still returns the result dict (the caller
    decides what to do with a fully-failed probe)."""
    from services import agent_center_llm_client as llm_client

    async def _post(self, url, **kwargs):
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": _result(
                    runs_count=3,
                    succeeded_runs=0,
                    failed_runs=3,
                    error_reasons=["OpenAI 429 quota-exceeded"],
                ),
            },
        )

    with patch.object(httpx.AsyncClient, "post", _post):
        result = await llm_client.probe(
            scan_mode="open_product_visibility_test",
            scan_target_id="t1",
            merchant_id="m1",
            store_id="s1",
            provider="chatgpt",
            max_runs=3,
        )

    assert result["provider"] == "chatgpt"
    assert capture_record["status"] == "failed"
    assert "quota-exceeded" in capture_record["error_message"]


@pytest.mark.asyncio
async def test_probe_successful_runs_records_succeeded_telemetry(
    configured_api_key: None, capture_record: Dict[str, Any],
) -> None:
    """A genuinely successful probe still records status="succeeded"."""
    from services import agent_center_llm_client as llm_client

    async def _post(self, url, **kwargs):
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": _result(
                    runs_count=3,
                    succeeded_runs=3,
                    failed_runs=0,
                    in_tokens=900,
                    out_tokens=300,
                ),
            },
        )

    with patch.object(httpx.AsyncClient, "post", _post):
        result = await llm_client.probe(
            scan_mode="open_product_visibility_test",
            scan_target_id="t1",
            merchant_id="m1",
            store_id="s1",
            provider="chatgpt",
            max_runs=3,
        )

    assert result["provider"] == "chatgpt"
    assert capture_record["status"] == "succeeded"
    assert capture_record["error_message"] is None
    assert capture_record["input_tokens"] == 900
