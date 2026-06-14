"""Phase 4: niche-target outcome tracking — did we win the targeted niches?"""

from __future__ import annotations

import pytest

from services import niche_outcomes as no
from services.agent_center_bd_report_service import (
    attach_niche_movement,
    build_where_you_can_win,
)


class _FakeDB:
    def __init__(self, rows=None):
        self.executes = []
        self._rows = rows or []

    async def execute(self, sql, params):
        self.executes.append(params)

    async def fetch_all(self, sql, params):
        self.last = params
        return self._rows


def test_movement_classification():
    assert no._movement("open-lane", "merchant-owned") == "won"
    assert no._movement(None, "merchant-owned") == "won"
    assert no._movement("merchant-owned", "merchant-owned") == "holding"
    assert no._movement("merchant-owned", "competitor-owned") == "lost"
    assert no._movement("open-lane", "open-lane") == "still_open"
    assert no._movement(None, "open-lane") == "new"


@pytest.mark.asyncio
async def test_record_extracts_meaningful_dedupes_by_query():
    db = _FakeDB()
    reports = [{
        "opportunity": {"per_prompt": [
            {"normalized_query": "vegan collagen", "ownership_state": "open-lane",
             "opportunity_score": 40, "open_lane": True},
            {"normalized_query": "vegan collagen", "ownership_state": "open-lane",
             "opportunity_score": 70, "open_lane": True},   # higher score wins dedupe
            {"normalized_query": "best collagen", "ownership_state": "competitor-owned",
             "opportunity_score": 10},
            {"normalized_query": "noise", "ownership_state": "no-demand"},  # excluded
        ]},
    }]
    n = await no.record_niche_outcomes(
        per_sku_reports=reports, merchant_id="m1", audit_run_id="run-2", db=db)
    assert n == 2  # vegan collagen (deduped) + best collagen; no-demand excluded
    recorded = {(e["q"], e["score"]) for e in db.executes}
    assert ("vegan collagen", 70.0) in recorded


@pytest.mark.asyncio
async def test_movement_compares_latest_two_audits():
    # newest-first rows: prior audit had it open-lane, current audit owns it
    db = _FakeDB(rows=[
        {"normalized_query": "vegan collagen", "ownership_state": "merchant-owned",
         "audit_run_id": "run-2", "seen_at": "2026-06-14T02:00:00Z"},
        {"normalized_query": "vegan collagen", "ownership_state": "open-lane",
         "audit_run_id": "run-1", "seen_at": "2026-06-13T02:00:00Z"},
    ])
    out = await no.niche_movement_for_queries("m1", ["Vegan Collagen"], db=db)
    assert out["vegan collagen"]["movement"] == "won"
    assert out["vegan collagen"]["current"] == "merchant-owned"
    assert out["vegan collagen"]["prior"] == "open-lane"


@pytest.mark.asyncio
async def test_attach_movement_skips_new(monkeypatch):
    wycw = build_where_you_can_win([{
        "sku_key": "s", "identity": {"name": "A"},
        "opportunity": {"per_prompt": [
            {"query": "won q", "normalized_query": "won q", "open_lane": True,
             "opportunity_score": 80, "attribute_basis": ["x"]},
            {"query": "new q", "normalized_query": "new q", "open_lane": True,
             "opportunity_score": 70, "attribute_basis": ["y"]},
        ]},
    }])

    async def fake_movement(merchant_id, queries, *, db=None):
        return {"won q": {"movement": "won", "current": "merchant-owned", "prior": "open-lane"},
                "new q": {"movement": "new", "current": "open-lane", "prior": None}}

    monkeypatch.setattr("services.niche_outcomes.niche_movement_for_queries", fake_movement)
    out = await attach_niche_movement(wycw, merchant_id="m1", db=None)
    by_q = {t["normalized_query"]: t for t in out["targets"]}
    assert by_q["won q"]["movement"] == "won"
    assert "movement" not in by_q["new q"]  # 'new' has no prior to compare — omitted
