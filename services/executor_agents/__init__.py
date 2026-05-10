"""PR-4: executor agents — close-the-loop fixes for audit findings.

Each agent in this package implements BaseExecutorAgent and ships a
specific automated remediation. Today's agents:
  - GscUrlSubmissionAgent: polls Pivota canonical PDPs, submits to
    Google indexing API for any not yet indexed.

Future agents (PR-4b/c):
  - SitemapFreshnessMonitor: detects merchant sitemap drift, auto-
    republishes Pivota-managed sitemaps, queues human task for
    merchant-managed.
  - ContentBriefGenerator: generates Markdown briefs for category
    visibility failures.

Common contract via BaseExecutorAgent. The orchestrator iterates over
registered agents and dispatches each one's `should_run` + `execute`.
"""

from services.executor_agents.base import (
    BaseExecutorAgent,
    ExecutorContext,
    ExecutorResult,
)
from services.executor_agents.content_brief import ContentBriefGeneratorAgent
from services.executor_agents.gsc_url_submission import GscUrlSubmissionAgent
from services.executor_agents.sitemap_freshness import SitemapFreshnessAgent

__all__ = [
    "BaseExecutorAgent",
    "ExecutorContext",
    "ExecutorResult",
    "ContentBriefGeneratorAgent",
    "GscUrlSubmissionAgent",
    "SitemapFreshnessAgent",
]
