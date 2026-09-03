"""Every module that sends text to a model either fences its third-party input or
is named here with the reason it does not. The allowlist can only shrink.

Why a ratchet and not a sweep: the lanes were built one at a time and each one
inlines merchant, crawled, or shopper text into its own prompt. Fencing them all
in one change is not reviewable; fencing new ones as they appear is. So this
test fails a PR that adds a model call without a fence, and fails when an
allowlisted module is fenced or removed, so the list cannot drift from the code.

Detection is by the transport calls this repo uses, not by module name, so a
new lane on any provider is caught. A module counts as fenced when it imports
``services.llm_fence``; whether it applies the fence to the right string is
the lane's own test's job (``tests/services/test_llm_fence_wiring.py``).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The calls through which a prompt leaves this process.
_MODEL_CALL = re.compile(
    r"generate_structured\(|\bsynthesize\(|generate_content_url\(|chat/completions|messages\.create\("
)
_FENCE_IMPORT = re.compile(r"^\s*from services\.llm_fence import|^\s*import services\.llm_fence", re.M)

# Modules that make a model call and do not import the fence, each with why.
# Remove an entry when its module is fenced; the test insists on it.
UNFENCED_MODEL_CALLERS: dict[str, str] = {
    # Transport: these carry whatever the caller built and add no text of their own.
    "services/llm_io.py": "transport; the caller owns the prompt",
    "services/llm_synthesis.py": "transport; the caller owns the prompt",
    "services/vertex_gemini.py": "transport; the caller owns the prompt",
    "services/llm_providers/deepseek_probe.py": "transport probe; sends a fixed string",
    # Lanes not yet fenced as of 2026-09-03. Fence when touched; then delete the row.
    "services/agent_center_bd_report_service.py": "unfenced; brand and search-snippet text",
    "services/bd_brand_category_inferrer.py": "unfenced; product titles",
    "services/bd_brand_signals.py": "unfenced; web search snippets",
    "services/catalog_enrichment_agent/gemini_url_validator.py": "unfenced; page text",
    "services/citation_draft_service.py": "unfenced; merchant copy",
    "services/competitor_audit_orchestrator.py": "unfenced; competitor page text",
    "services/evidence_extraction.py": "unfenced; merchant copy",
    "services/executor_agents/canonical_pdp_enrichment.py": "unfenced; PDP text",
    "services/executor_agents/competitor_insights.py": "unfenced; competitor page text",
    "services/executor_agents/content_brief.py": "unfenced; PDP text",
    "services/fashion_field_extractor.py": "unfenced; product copy",
    "services/identity_tier3_judge.py": "unfenced; candidate PDP text",
    "services/pdp_label_agent.py": "unfenced; PDP text",
    "services/pdp_matcher/llm_match.py": "unfenced; candidate PDP titles",
    "services/product_identity_i18n.py": "unfenced; product titles",
    "services/report_deck_builder.py": "unfenced; report text",
    "services/retailer_ingest/official_match_judge.py": "unfenced; retailer listing text",
    "services/strategic_brief.py": "unfenced; merchant and competitor text",
}


def _model_callers() -> dict[str, str]:
    """Repo-relative path -> source, for every module that makes a model call."""
    found: dict[str, str] = {}
    for base in ("services", "routes"):
        for path in sorted((ROOT / base).rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            if _MODEL_CALL.search(source):
                found[path.relative_to(ROOT).as_posix()] = source
    return found


def test_every_model_caller_is_fenced_or_named_here():
    callers = _model_callers()
    assert callers, "the detector found no model callers; its patterns are stale"
    unfenced = {p for p, src in callers.items() if not _FENCE_IMPORT.search(src)}
    new = sorted(unfenced - set(UNFENCED_MODEL_CALLERS))
    assert not new, (
        "model callers without a fence and without an allowlist row (fence the prompt "
        "with services.llm_fence, or add a row with a reason): " + ", ".join(new)
    )


def test_the_allowlist_names_only_modules_that_still_need_it():
    callers = _model_callers()
    stale = sorted(p for p in UNFENCED_MODEL_CALLERS if p not in callers)
    assert not stale, "allowlisted modules that no longer make a model call: " + ", ".join(stale)
    fenced = sorted(
        p for p in UNFENCED_MODEL_CALLERS if p in callers and _FENCE_IMPORT.search(callers[p])
    )
    assert not fenced, "allowlisted modules that are now fenced; delete their rows: " + ", ".join(fenced)
