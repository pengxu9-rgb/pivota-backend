"""W1 site-5 backfill tests — repair of stored payloads whose channels table
undercounted own-site citations (services/audit_backfill.py)."""
from __future__ import annotations

import asyncio
import copy
from typing import Any, Dict, List

import services.audit_backfill as backfill_mod
from services.audit_backfill import backfill_channel_own_site


def _probe_runs(own_cited_queries: List[str], other_queries: List[str]):
    """One gemini probe whose runs cite the merchant's own page (label names
    the brand -> the OLD builder undercounted) or a retailer."""
    runs = []
    for q in own_cited_queries:
        runs.append({
            "query": q,
            "grounding_sources": [
                {"uri": "https://damdamtokyo.com/products/shiso", "title": "DAMDAM Shiso"},
            ],
        })
    for q in other_queries:
        runs.append({
            "query": q,
            "grounding_sources": [{"uri": "https://sephora.com/p/1", "title": "Sephora"}],
        })
    return [{"provider": "gemini", "raw_runs": runs}]


def _stored_run(run_id: str) -> Dict[str, Any]:
    """A pre-fix stored payload: own page actually cited on q1+q2, but the
    channels row displays 0 (the undercount) and no run_facts stamp exists."""
    per_prompt = [
        {"query": q, "normalized_query": q, "axis": "head",
         "source_summary": {"merchant_cited_runs": 1, "top_cited_hosts": [
             {"host": "sephora.com", "times_cited": 1}]}}
        for q in ("q1", "q2", "q3")
    ]
    return {
        "run_id": run_id,
        "status": "succeeded",
        "report_jsonb": {
            "merchant_name": "DAMDAM",
            "merchant_domain": "https://damdamtokyo.com",
            "per_sku_reports": [{
                "sku_key": "urlwedge:abc",
                "opportunity": {"per_prompt": per_prompt},
                "channel_appearance": {
                    "own_site_host": "damdamtokyo.com",
                    "own_site_cited": False,
                    "own_site_cited_count": 0,
                    "total_queries": 3,
                    "channels": [
                        {"host": "damdamtokyo.com", "is_own_site": True,
                         "cited_query_count": 0, "total_queries": 3},
                        {"host": "sephora.com", "is_own_site": False,
                         "is_your_listing": False, "cited_query_count": 3},
                    ],
                },
            }],
            "brand_rollup": {},
        },
    }


class _FakeDB:
    def __init__(self, rows):
        self.rows = rows
        self.executed: List[Any] = []

    async def fetch_all(self, query):
        return self.rows

    async def execute(self, query):
        self.executed.append(query)
        return 1


def _patch(monkeypatch, db, probe_runs):
    monkeypatch.setattr(backfill_mod, "database", db)

    async def fake_ensure():
        return None

    async def fake_load(sku_key, merchant_id, run_id):
        return probe_runs

    monkeypatch.setattr(backfill_mod, "ensure_merchant_audit_runs_table", fake_ensure)
    monkeypatch.setattr(backfill_mod, "load_per_sku_probe_runs", fake_load)


def test_dry_run_reports_undercount_without_writing(monkeypatch):
    db = _FakeDB([_stored_run("r1")])
    _patch(monkeypatch, db, _probe_runs(["q1", "q2"], ["q3"]))
    out = asyncio.run(backfill_channel_own_site(merchant_id="m1", dry_run=True))
    assert out["runs_scanned"] == 1
    assert out["runs_changed"] == 1
    assert out["runs_written"] == 0
    assert db.executed == []
    change = out["changes"][0]["skus"][0]
    assert change["own_site_cited_count"] == {"old": 0, "new": 2}
    assert change["run_facts_stamped"] is True


def test_write_patches_channels_stamps_facts_and_marks_provenance(monkeypatch):
    row = _stored_run("r1")
    db = _FakeDB([row])
    _patch(monkeypatch, db, _probe_runs(["q1", "q2"], ["q3"]))
    out = asyncio.run(backfill_channel_own_site(merchant_id="m1", dry_run=False))
    assert out["runs_written"] == 1 and len(db.executed) == 1

    rep = row["report_jsonb"]  # mutated in place before persist
    sku = rep["per_sku_reports"][0]
    assert sku["channel_appearance"]["own_site_cited_count"] == 2
    assert sku["channel_appearance"]["own_site_cited"] is True
    # Third-party channel evidence intact.
    assert any(c["host"] == "sephora.com" for c in sku["channel_appearance"]["channels"])
    # Backfilled fact stamp + brand fold + provenance marker.
    assert sku["run_facts"]["backfilled"] is True
    assert sku["run_facts"]["own_url_cited_runs"] == 2
    assert rep["brand_rollup"]["run_facts"]["backfilled"] is True
    assert rep["own_site_backfill"]["skus"] == ["urlwedge:abc"]


def test_idempotent_after_repair(monkeypatch):
    row = _stored_run("r1")
    db = _FakeDB([row])
    _patch(monkeypatch, db, _probe_runs(["q1", "q2"], ["q3"]))
    asyncio.run(backfill_channel_own_site(merchant_id="m1", dry_run=False))
    repaired = copy.deepcopy(row)
    db2 = _FakeDB([repaired])
    _patch(monkeypatch, db2, _probe_runs(["q1", "q2"], ["q3"]))
    again = asyncio.run(backfill_channel_own_site(merchant_id="m1", dry_run=False))
    assert again["runs_changed"] == 0
    assert db2.executed == []


def test_missing_probe_runs_skips_never_guesses(monkeypatch):
    db = _FakeDB([_stored_run("r1")])
    _patch(monkeypatch, db, [])
    out = asyncio.run(backfill_channel_own_site(merchant_id="m1", dry_run=False))
    assert out["runs_changed"] == 0
    assert out["skipped"] and "not patched" in out["skipped"][0]["skipped"]
    assert db.executed == []


def test_run_ids_scope(monkeypatch):
    db = _FakeDB([_stored_run("r1"), _stored_run("r2")])
    _patch(monkeypatch, db, _probe_runs(["q1"], ["q2", "q3"]))
    out = asyncio.run(
        backfill_channel_own_site(merchant_id="m1", run_ids=["r2"], dry_run=True)
    )
    assert out["runs_scanned"] == 1
    assert out["changes"][0]["run_id"] == "r2"
