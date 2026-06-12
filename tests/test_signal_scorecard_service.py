"""Tests for the controlled-signal scorecard service."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import signal_scorecard_service as svc  # noqa: E402


class _ScriptedDB:
    """fetch_one returns the next scalar from a per-substring script. Each entry
    maps a SQL substring → value (or an Exception to raise)."""

    def __init__(self, script: List[tuple]) -> None:
        self._script = script

    async def fetch_one(self, sql: str, params: Optional[Dict[str, Any]] = None):
        for needle, value in self._script:
            if needle in sql:
                if isinstance(value, Exception):
                    raise value
                return {"v": value}
        return {"v": 0}


@pytest.mark.asyncio
async def test_scorecard_aggregates_and_computes_pct() -> None:
    db = _ScriptedDB([
        ("co.suppressed_at IS NULL", 20),  # offers
        ("catalog_row_trust", 10),                                              # trust
        ("FROM index_pipeline_state WHERE serving_eligible IS TRUE AND content_quality_score", 80),
        ("ORDER BY EXTRACT(EPOCH FROM (now() - last_consolidated_at))", 3),
        ("SELECT count(*) FROM index_pipeline_state WHERE serving_eligible IS TRUE", 100),
        ("SELECT count(*) FROM index_pipeline_state", 250),
        ("count(DISTINCT anchor_ref)", 46),
        ("FROM product_relationship_edges", 5185),
        ("FROM aurora_product_intel_kb", 1170),
        ("ORDER BY updated_at", 12),
        ("SELECT count(*) FROM agent_decision_funnel_links", 7),
        ("count(DISTINCT decision_id)", 5),
        ("ORDER BY created_at", 1),
    ])
    out = await svc.compute_scorecard(db=db)
    assert out["serving_eligible_universe"] == 100
    by = {s["signal"]: s for s in out["signals"]}

    # catalog_base: eligible / total
    assert by["catalog_base"]["coverage_pct"] == 40.0  # 100/250
    assert by["catalog_base"]["exposed_to_agent"] == "live"

    # offers coverage over eligible universe
    assert by["offers"]["covered"] == 20
    assert by["offers"]["coverage_pct"] == 20.0
    assert by["offers"]["exposed_to_agent"] == "gated"

    # quality_meta carries freshness
    assert by["quality_meta"]["freshness_median_age_days"] == 3.0
    assert by["quality_meta"]["exposed_to_agent"] == "internal"

    # outcomes: no denominator (pct None), but counts present
    assert by["outcomes"]["covered"] == 7
    assert by["outcomes"]["coverage_pct"] is None
    assert by["outcomes"]["distinct_decisions_linked"] == 5

    # every signal carries an exposure state
    for s in out["signals"]:
        assert s["exposed_to_agent"] in {"live", "gated", "internal", "none", "unknown"}


@pytest.mark.asyncio
async def test_one_signal_failure_does_not_sink_scorecard() -> None:
    db = _ScriptedDB([
        ("SELECT count(*) FROM index_pipeline_state WHERE serving_eligible IS TRUE", 50),
        ("FROM product_relationship_edges", RuntimeError("relation does not exist")),
        # everything else returns 0
    ])
    out = await svc.compute_scorecard(db=db)
    by = {s["signal"]: s for s in out["signals"]}
    # The relationship/alternatives signal degrades to unavailable...
    assert by["alternatives"]["available"] is False
    assert "error" in by["alternatives"]
    # ...but the rest still computed.
    assert by["catalog_base"]["available"] is True
    assert len(out["signals"]) == len(svc._SIGNALS)


def test_exposure_registry_covers_every_signal() -> None:
    keys = {fn.__name__.lstrip("_") for fn in svc._SIGNALS}
    assert keys <= set(svc.EXPOSURE.keys())
