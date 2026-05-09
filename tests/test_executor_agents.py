"""PR-4a: executor agent dispatcher + GscUrlSubmissionAgent tests.

Pure-logic coverage:
  - BaseExecutorAgent contract
  - dispatcher iterates registry, gates on should_run, persists via
    record_executor_run_started/completed
  - GscUrlSubmissionAgent: should_run gating, success/failure result
    shapes (mocked submit_url_to_gsc to avoid real Google API calls)

DB-touching paths (executor_runs persistence) tested end-to-end on
staging — same rationale as scheduled_audit_job.
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock, patch

import pytest

from services.executor_agents.base import (
    BaseExecutorAgent,
    ExecutorContext,
    ExecutorResult,
)


# ---------------------------------------------------------------------------
# BaseExecutorAgent contract
# ---------------------------------------------------------------------------


class _NoopAgent(BaseExecutorAgent):
    name = "noop"

    def __init__(self, *, run_decision: bool = True, exec_status: str = "succeeded"):
        self._run = run_decision
        self._status = exec_status

    async def should_run(self, context):
        return self._run

    async def execute(self, context):
        return ExecutorResult(status=self._status, evidence={"agent": "noop"})


def test_base_agent_subclass_must_override_methods():
    """BaseExecutorAgent is abstract — direct instantiation fails."""
    with pytest.raises(TypeError):
        BaseExecutorAgent()  # type: ignore[abstract]


@pytest.mark.asyncio
async def test_executor_result_default_evidence():
    """ExecutorResult.evidence defaults to {} (dataclass default_factory)."""
    r = ExecutorResult(status="succeeded")
    assert r.evidence == {}
    assert r.error_message is None


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_skips_agent_when_should_run_false():
    """Agent with should_run=False is evaluated but not executed.
    Persistence should NOT be called for it."""
    from services.executor_agents import dispatcher
    started = AsyncMock(return_value="run-1")
    completed = AsyncMock()
    with patch.object(dispatcher, "_registry", lambda: [_NoopAgent(run_decision=False)]), \
         patch("db.executor_runs.record_executor_run_started", started), \
         patch("db.executor_runs.record_executor_run_completed", completed):
        result = await dispatcher.dispatch_agents(
            ExecutorContext(merchant_id="m1"),
        )
    assert result["agents_evaluated"] == 1
    assert result["agents_executed"] == 0
    assert result["results"] == []
    started.assert_not_called()
    completed.assert_not_called()


@pytest.mark.asyncio
async def test_dispatcher_executes_agent_when_should_run_true():
    """Agent with should_run=True gets started + completed via DB."""
    from services.executor_agents import dispatcher
    started = AsyncMock(return_value="run-2")
    completed = AsyncMock()
    with patch.object(dispatcher, "_registry", lambda: [_NoopAgent(run_decision=True, exec_status="succeeded")]), \
         patch("db.executor_runs.record_executor_run_started", started), \
         patch("db.executor_runs.record_executor_run_completed", completed):
        result = await dispatcher.dispatch_agents(
            ExecutorContext(merchant_id="m2", parent_audit_run_id="audit-x"),
        )
    assert result["agents_evaluated"] == 1
    assert result["agents_executed"] == 1
    assert result["results"][0]["status"] == "succeeded"
    started.assert_called_once_with(
        agent_name="noop",
        merchant_id="m2",
        parent_audit_run_id="audit-x",
    )
    completed.assert_called_once()
    completed_kwargs = completed.call_args.kwargs
    assert completed_kwargs["status"] == "succeeded"
    assert completed_kwargs["evidence_jsonb"] == {"agent": "noop"}


@pytest.mark.asyncio
async def test_dispatcher_records_failure_when_execute_raises():
    """Uncaught exception in execute() → status='failed' with diagnostic
    error_message instead of propagating."""
    from services.executor_agents import dispatcher

    class _BoomAgent(BaseExecutorAgent):
        name = "boom"
        async def should_run(self, context): return True
        async def execute(self, context): raise RuntimeError("kaboom")

    started = AsyncMock(return_value="run-3")
    completed = AsyncMock()
    with patch.object(dispatcher, "_registry", lambda: [_BoomAgent()]), \
         patch("db.executor_runs.record_executor_run_started", started), \
         patch("db.executor_runs.record_executor_run_completed", completed):
        result = await dispatcher.dispatch_agents(
            ExecutorContext(merchant_id="m3"),
        )
    assert result["results"][0]["status"] == "failed"
    assert "kaboom" in (result["results"][0]["error_message"] or "")
    completed_kwargs = completed.call_args.kwargs
    assert completed_kwargs["status"] == "failed"


@pytest.mark.asyncio
async def test_dispatcher_continues_when_should_run_raises():
    """should_run is meant to be cheap; if it raises (e.g. transient
    DB error), skip that agent silently and continue with the next.
    Lifecycle persistence does NOT run for the skipped agent."""
    from services.executor_agents import dispatcher

    class _BoomShouldRun(BaseExecutorAgent):
        name = "boom_should_run"
        async def should_run(self, context): raise ValueError("cant-decide")
        async def execute(self, context): return ExecutorResult(status="succeeded")

    started = AsyncMock(return_value="run-4")
    completed = AsyncMock()
    with patch.object(dispatcher, "_registry", lambda: [_BoomShouldRun(), _NoopAgent(run_decision=True)]), \
         patch("db.executor_runs.record_executor_run_started", started), \
         patch("db.executor_runs.record_executor_run_completed", completed):
        result = await dispatcher.dispatch_agents(
            ExecutorContext(merchant_id="m4"),
        )
    # 2 evaluated, 1 actually executed (the noop one)
    assert result["agents_evaluated"] == 2
    assert result["agents_executed"] == 1
    assert result["results"][0]["agent_name"] == "noop"


# ---------------------------------------------------------------------------
# GscUrlSubmissionAgent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gsc_agent_should_run_false_without_merchant_id():
    from services.executor_agents.gsc_url_submission import GscUrlSubmissionAgent
    agent = GscUrlSubmissionAgent()
    assert await agent.should_run(ExecutorContext(merchant_id=None)) is False


@pytest.mark.asyncio
async def test_gsc_agent_should_run_false_when_gsc_disabled():
    from services.executor_agents.gsc_url_submission import GscUrlSubmissionAgent
    agent = GscUrlSubmissionAgent()
    with patch("services.executor_agents.gsc_url_submission._gsc_enabled", return_value=False):
        assert await agent.should_run(ExecutorContext(merchant_id="m1")) is False


@pytest.mark.asyncio
async def test_gsc_agent_should_run_false_when_no_stale_urls():
    from services.executor_agents.gsc_url_submission import GscUrlSubmissionAgent
    agent = GscUrlSubmissionAgent()
    with patch("services.executor_agents.gsc_url_submission._gsc_enabled", return_value=True), \
         patch.object(agent, "_fetch_stale_unindexed", new=AsyncMock(return_value=[])):
        assert await agent.should_run(ExecutorContext(merchant_id="m1")) is False


@pytest.mark.asyncio
async def test_gsc_agent_should_run_true_when_candidates_exist():
    from services.executor_agents.gsc_url_submission import GscUrlSubmissionAgent
    agent = GscUrlSubmissionAgent()
    candidates = [{"url": "https://acme.co/p/1", "last_status": "pending", "last_status_at": "2026-04-01T00:00:00+00:00"}]
    with patch("services.executor_agents.gsc_url_submission._gsc_enabled", return_value=True), \
         patch.object(agent, "_fetch_stale_unindexed", new=AsyncMock(return_value=candidates)):
        assert await agent.should_run(ExecutorContext(merchant_id="m1")) is True


@pytest.mark.asyncio
async def test_gsc_agent_execute_no_candidates_returns_skipped():
    from services.executor_agents.gsc_url_submission import GscUrlSubmissionAgent
    agent = GscUrlSubmissionAgent()
    with patch.object(agent, "_fetch_stale_unindexed", new=AsyncMock(return_value=[])):
        result = await agent.execute(ExecutorContext(merchant_id="m1"))
    assert result.status == "skipped"
    assert result.evidence["reason"] == "no stale unindexed URLs"


@pytest.mark.asyncio
async def test_gsc_agent_execute_succeeds_with_partial_failures():
    """Some URLs submit OK, some return error responses → status=succeeded
    with both counts surfaced in evidence (caller can show '8 of 12
    submitted; 4 failed Google quota')."""
    from services.executor_agents.gsc_url_submission import GscUrlSubmissionAgent
    agent = GscUrlSubmissionAgent()
    candidates = [
        {"url": "https://acme.co/p/1", "last_status": "pending", "last_status_at": "2026-04-01"},
        {"url": "https://acme.co/p/2", "last_status": "pending", "last_status_at": "2026-04-01"},
        {"url": "https://acme.co/p/3", "last_status": "pending", "last_status_at": "2026-04-01"},
    ]

    async def _fake_submit(merchant_id, url, *, audit_run_id=None):
        if "p/2" in url:
            return {"status": "error", "message": "quota exceeded"}
        return {"status": "submitted", "message": "ok"}

    with patch.object(agent, "_fetch_stale_unindexed", new=AsyncMock(return_value=candidates)), \
         patch("services.gsc_integration.submit_url_to_gsc", new=AsyncMock(side_effect=_fake_submit)):
        result = await agent.execute(ExecutorContext(merchant_id="m1"))
    assert result.status == "succeeded"
    assert result.evidence["candidates_total"] == 3
    assert result.evidence["succeeded_count"] == 2
    assert result.evidence["failed_count"] == 1


@pytest.mark.asyncio
async def test_gsc_agent_execute_aborts_on_oauth_missing():
    """GscNotConfiguredError on the first URL aborts the loop —
    subsequent URLs would all hit the same error."""
    from services.executor_agents.gsc_url_submission import GscUrlSubmissionAgent
    from services.gsc_integration import GscNotConfiguredError
    agent = GscUrlSubmissionAgent()
    candidates = [{"url": "https://acme.co/p/1", "last_status": "pending", "last_status_at": "x"}]

    async def _fake_submit(merchant_id, url, *, audit_run_id=None):
        raise GscNotConfiguredError("no oauth")

    with patch.object(agent, "_fetch_stale_unindexed", new=AsyncMock(return_value=candidates)), \
         patch("services.gsc_integration.submit_url_to_gsc", new=AsyncMock(side_effect=_fake_submit)):
        result = await agent.execute(ExecutorContext(merchant_id="m1"))
    assert result.status == "failed"
    assert "gsc_oauth_missing" in (result.error_message or "")


@pytest.mark.asyncio
async def test_gsc_agent_caps_submits_per_run():
    """Even with 100 candidates, agent caps per-run submits at
    _MAX_SUBMITS_PER_RUN (25). Skipped count surfaced in evidence."""
    from services.executor_agents.gsc_url_submission import (
        GscUrlSubmissionAgent,
        _MAX_SUBMITS_PER_RUN,
    )
    agent = GscUrlSubmissionAgent()
    candidates = [
        {"url": f"https://acme.co/p/{i}", "last_status": "pending", "last_status_at": "x"}
        for i in range(100)
    ]
    async def _fake_submit(merchant_id, url, *, audit_run_id=None):
        return {"status": "submitted", "message": "ok"}
    with patch.object(agent, "_fetch_stale_unindexed", new=AsyncMock(return_value=candidates)), \
         patch("services.gsc_integration.submit_url_to_gsc", new=AsyncMock(side_effect=_fake_submit)):
        result = await agent.execute(ExecutorContext(merchant_id="m1"))
    assert result.status == "succeeded"
    assert result.evidence["submits_attempted"] == _MAX_SUBMITS_PER_RUN
    assert result.evidence["skipped_for_throttle"] == 100 - _MAX_SUBMITS_PER_RUN


# ---------------------------------------------------------------------------
# PR-4b: SitemapFreshnessAgent
# ---------------------------------------------------------------------------

from services.executor_agents.sitemap_freshness import (
    SitemapFreshnessAgent,
    _classify_severity,
    _compute_freshness_score,
    _derive_merchant_host,
    _normalize_url_for_diff,
    _parse_sitemap_xml,
)


def test_normalize_url_strips_scheme_www_trailing_slash_query():
    """All these URLs should normalize to the same key for diff."""
    variants = [
        "https://acme.co/products/x",
        "https://www.acme.co/products/x",
        "http://acme.co/products/x/",
        "https://acme.co/products/x?utm_source=email",
        "acme.co/products/x",
    ]
    norms = [_normalize_url_for_diff(v) for v in variants]
    assert len(set(norms)) == 1
    assert norms[0] == "acme.co/products/x"


def test_normalize_url_returns_none_for_garbage():
    assert _normalize_url_for_diff(None) is None
    assert _normalize_url_for_diff("") is None
    assert _normalize_url_for_diff("   ") is None
    assert _normalize_url_for_diff("not a url") is None or _normalize_url_for_diff("not a url") == "not a url/"


def test_normalize_url_handles_root_path():
    assert _normalize_url_for_diff("https://acme.co/") == "acme.co/"
    assert _normalize_url_for_diff("https://acme.co") == "acme.co/"


def test_derive_merchant_host_picks_most_common():
    urls = [
        "https://acme.co/p/1",
        "https://acme.co/p/2",
        "https://acme.co/p/3",
        "https://different.com/p/1",  # outlier
    ]
    assert _derive_merchant_host(urls) == "acme.co"


def test_derive_merchant_host_strips_www():
    urls = ["https://www.acme.co/p/1"]
    assert _derive_merchant_host(urls) == "acme.co"


def test_derive_merchant_host_returns_none_for_empty():
    assert _derive_merchant_host([]) is None
    assert _derive_merchant_host(["not a url at all"]) is None


def test_parse_sitemap_xml_handles_namespaces():
    xml = b"""<?xml version="1.0"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://acme.co/p/1</loc></url>
      <url><loc>https://acme.co/p/2</loc></url>
    </urlset>"""
    urls = _parse_sitemap_xml(xml)
    assert len(urls) == 2
    assert "https://acme.co/p/1" in urls


def test_parse_sitemap_xml_falls_back_to_regex_on_malformed():
    xml = b"<urlset><url><loc>https://acme.co/p/1</loc></url><url><loc>https://acme.co/p/2</loc></url"
    urls = _parse_sitemap_xml(xml)
    assert "https://acme.co/p/1" in urls


def test_compute_freshness_score_perfect_alignment():
    """Catalog and sitemap perfectly aligned → score 1.0."""
    assert _compute_freshness_score(catalog_count=10, missing_count=0, orphan_count=0) == 1.0


def test_compute_freshness_score_weighted_to_missing():
    """Missing-from-sitemap is weighted heavier than orphans (missing
    products = direct visibility loss; orphans = SEO noise)."""
    miss_only = _compute_freshness_score(catalog_count=10, missing_count=2, orphan_count=0)
    orphan_only = _compute_freshness_score(catalog_count=10, missing_count=0, orphan_count=2)
    assert miss_only < orphan_only  # same diff size, missing hurts more


def test_compute_freshness_score_clamps_at_zero():
    """Edge case: missing_count > catalog_count shouldn't go negative."""
    score = _compute_freshness_score(catalog_count=10, missing_count=20, orphan_count=20)
    assert 0.0 <= score <= 1.0


def test_compute_freshness_score_empty_catalog():
    """No catalog products → vacuous 1.0 (nothing to compare)."""
    assert _compute_freshness_score(catalog_count=0, missing_count=0, orphan_count=0) == 1.0


def test_classify_severity_low_for_minimal_drift():
    assert _classify_severity(missing_ratio=0.01, orphan_ratio=0.05) == "low"


def test_classify_severity_medium_for_moderate_drift():
    assert _classify_severity(missing_ratio=0.10, orphan_ratio=0.05) == "medium"


def test_classify_severity_high_for_major_drift():
    assert _classify_severity(missing_ratio=0.25, orphan_ratio=0.05) == "high"
    assert _classify_severity(missing_ratio=0.05, orphan_ratio=0.55) == "high"


@pytest.mark.asyncio
async def test_sitemap_agent_should_run_false_without_merchant_id():
    agent = SitemapFreshnessAgent()
    assert await agent.should_run(ExecutorContext(merchant_id=None)) is False


@pytest.mark.asyncio
async def test_sitemap_agent_should_run_false_without_catalog():
    agent = SitemapFreshnessAgent()
    with patch("services.executor_agents.sitemap_freshness._fetch_merchant_catalog_urls",
               new=AsyncMock(return_value=[])):
        assert await agent.should_run(ExecutorContext(merchant_id="m1")) is False


@pytest.mark.asyncio
async def test_sitemap_agent_should_run_true_when_catalog_exists():
    agent = SitemapFreshnessAgent()
    with patch("services.executor_agents.sitemap_freshness._fetch_merchant_catalog_urls",
               new=AsyncMock(return_value=["https://acme.co/p/1"])):
        assert await agent.should_run(ExecutorContext(merchant_id="m1")) is True


@pytest.mark.asyncio
async def test_sitemap_agent_execute_perfect_alignment():
    """Catalog and sitemap match exactly → succeeded with score 1.0
    + zero missing/orphan."""
    agent = SitemapFreshnessAgent()
    catalog_urls = ["https://acme.co/p/1", "https://acme.co/p/2", "https://acme.co/p/3"]
    sitemap_urls = list(catalog_urls)
    with patch("services.executor_agents.sitemap_freshness._fetch_merchant_catalog_urls",
               new=AsyncMock(return_value=catalog_urls)), \
         patch("services.executor_agents.sitemap_freshness._fetch_sitemap_urls_recursive",
               new=AsyncMock(return_value=(sitemap_urls, None))):
        result = await agent.execute(ExecutorContext(merchant_id="m1"))
    assert result.status == "succeeded"
    assert result.evidence["catalog_url_count"] == 3
    assert result.evidence["sitemap_url_count"] == 3
    assert result.evidence["missing_from_sitemap_count"] == 0
    assert result.evidence["orphan_in_sitemap_count"] == 0
    assert result.evidence["freshness_score"] == 1.0
    assert result.evidence["severity"] == "low"


@pytest.mark.asyncio
async def test_sitemap_agent_execute_detects_missing_products():
    """5 catalog products, sitemap only has 3 → 2 missing → high
    severity (40% missing ratio)."""
    agent = SitemapFreshnessAgent()
    catalog = [f"https://acme.co/p/{i}" for i in range(5)]
    sitemap = catalog[:3]  # missing p/3 and p/4
    with patch("services.executor_agents.sitemap_freshness._fetch_merchant_catalog_urls",
               new=AsyncMock(return_value=catalog)), \
         patch("services.executor_agents.sitemap_freshness._fetch_sitemap_urls_recursive",
               new=AsyncMock(return_value=(sitemap, None))):
        result = await agent.execute(ExecutorContext(merchant_id="m1"))
    assert result.evidence["missing_from_sitemap_count"] == 2
    assert result.evidence["severity"] == "high"
    # Sample contains the actual missing URLs
    missing_sample = result.evidence["missing_from_sitemap_sample"]
    assert any("p/3" in u for u in missing_sample)
    assert any("p/4" in u for u in missing_sample)


@pytest.mark.asyncio
async def test_sitemap_agent_execute_detects_orphans():
    """Sitemap has URLs that aren't in catalog → orphan_in_sitemap."""
    agent = SitemapFreshnessAgent()
    catalog = ["https://acme.co/p/1"]
    sitemap = ["https://acme.co/p/1", "https://acme.co/p/old-discontinued"]
    with patch("services.executor_agents.sitemap_freshness._fetch_merchant_catalog_urls",
               new=AsyncMock(return_value=catalog)), \
         patch("services.executor_agents.sitemap_freshness._fetch_sitemap_urls_recursive",
               new=AsyncMock(return_value=(sitemap, None))):
        result = await agent.execute(ExecutorContext(merchant_id="m1"))
    assert result.evidence["orphan_in_sitemap_count"] == 1
    assert any("old-discontinued" in u for u in result.evidence["orphan_in_sitemap_sample"])


@pytest.mark.asyncio
async def test_sitemap_agent_execute_normalization_handles_trailing_slash():
    """Catalog stores 'p/1'; sitemap publishes 'p/1/' — normalization
    treats them as same product."""
    agent = SitemapFreshnessAgent()
    with patch("services.executor_agents.sitemap_freshness._fetch_merchant_catalog_urls",
               new=AsyncMock(return_value=["https://acme.co/p/1"])), \
         patch("services.executor_agents.sitemap_freshness._fetch_sitemap_urls_recursive",
               new=AsyncMock(return_value=(["https://www.acme.co/p/1/?utm_source=email"], None))):
        result = await agent.execute(ExecutorContext(merchant_id="m1"))
    assert result.evidence["missing_from_sitemap_count"] == 0
    assert result.evidence["orphan_in_sitemap_count"] == 0


@pytest.mark.asyncio
async def test_sitemap_agent_execute_unreachable_sitemap_is_finding():
    """A 404 on sitemap.xml is itself a finding the merchant needs
    to fix — surface as failed result with diagnostic."""
    agent = SitemapFreshnessAgent()
    with patch("services.executor_agents.sitemap_freshness._fetch_merchant_catalog_urls",
               new=AsyncMock(return_value=["https://acme.co/p/1"])), \
         patch("services.executor_agents.sitemap_freshness._fetch_sitemap_urls_recursive",
               new=AsyncMock(return_value=([], "http_404"))):
        result = await agent.execute(ExecutorContext(merchant_id="m1"))
    assert result.status == "failed"
    assert "sitemap_unreachable" in (result.error_message or "")
    assert result.evidence["fetch_error"] == "http_404"
    assert result.evidence["sitemap_url"] == "https://acme.co/sitemap.xml"


@pytest.mark.asyncio
async def test_sitemap_agent_execute_no_merchant_id_returns_skipped():
    agent = SitemapFreshnessAgent()
    result = await agent.execute(ExecutorContext(merchant_id=None))
    assert result.status == "skipped"
