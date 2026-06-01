"""Regression test: per-SKU probe runs must survive a JSONB-as-string read.

Background
----------
The second live Ownist pilot run (after PR #711) expanded SKUs correctly but
returned `citation.score = 0` / `citation_by_provider = {}` /
`missing_inputs: ["per_sku_audit.raw_runs"]` for every SKU, even though raw
Gemini runs existed in `partial_result_jsonb.per_sku_probe_runs`.

Root cause (same class as #706): asyncpg returns JSONB columns as STRINGS (no
global JSON codec — see db/database.py). `load_per_sku_probe_runs` reads
`partial_result_jsonb` via the service-local `_fetch_one_dict`/`_row_dict`,
which do a plain `dict(row)` with no decode. `_extract_probe_result_candidates`
then hit `if not isinstance(doc, dict): return []` and silently dropped every
probe run, so the citation scorer saw zero runs.

The fix decodes a JSON-string `doc` in `_extract_probe_result_candidates`.
Every existing test stores `partial_result_jsonb` as a real dict (the e2e
integration test included), so none of them exercise the string path — this
test does.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from databases import Database

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.agent_center_bd_report_service import (
    _extract_probe_result_candidates,
    _flatten_probe_runs,
    load_per_sku_probe_runs,
)

MERCHANT = "merch_test_probe_decode_001"
RUN_ID = "run_test_probe_decode_001"
SKU_KEY = "rk_test_p1::v::var1"


def _probe_container() -> dict:
    return {
        "per_sku_probe_runs": {
            SKU_KEY: [
                {
                    "provider": "gemini",
                    "probe_run_id": "pr_1",
                    "scan_mode": "per_sku_audit",
                    "sku_key": SKU_KEY,
                    "raw_runs": [
                        {"query": "buy Ownist Triple Shine Grape online",
                         "provider": "gemini", "sku_key": SKU_KEY},
                        {"query": "where to buy Ownist Triple Shine Grape",
                         "provider": "gemini", "sku_key": SKU_KEY},
                    ],
                }
            ]
        }
    }


def test_extract_candidates_decodes_jsonb_string() -> None:
    """The exact line that broke: a JSON-string container must still yield runs."""
    container = _probe_container()

    # dict path (what tests/GET provide) — baseline
    from_dict = _flatten_probe_runs(_extract_probe_result_candidates(container, SKU_KEY))
    assert len(from_dict) == 2

    # string path (what asyncpg returns on prod) — must now match, not drop to 0
    from_str = _flatten_probe_runs(
        _extract_probe_result_candidates(json.dumps(container), SKU_KEY)
    )
    assert len(from_str) == 2, "JSONB-string partial_result_jsonb dropped probe runs"


async def test_load_per_sku_probe_runs_decodes_string_partial_result(monkeypatch, tmp_path) -> None:
    """Drive the real load_per_sku_probe_runs with partial_result_jsonb stored as
    a STRING (as asyncpg returns it) and assert probe runs are recovered."""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'probe_decode_test.db'}")
    await db.connect()
    try:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS merchant_audit_runs ("
            " run_id TEXT, merchant_id TEXT, report_jsonb TEXT, "
            " partial_result_jsonb TEXT, cost_summary_jsonb TEXT)"
        )
        # Store partial_result_jsonb as a JSON STRING — the prod failure mode.
        await db.execute(
            "INSERT INTO merchant_audit_runs "
            "(run_id, merchant_id, report_jsonb, partial_result_jsonb, cost_summary_jsonb) "
            "VALUES (:rid, :m, :rep, :partial, :cost)",
            {"rid": RUN_ID, "m": MERCHANT, "rep": None,
             "partial": json.dumps(_probe_container()), "cost": None},
        )
        monkeypatch.setattr("db.database.database", db)

        candidates = await load_per_sku_probe_runs(SKU_KEY, MERCHANT, RUN_ID)
        assert candidates, "no probe candidates recovered from string partial_result_jsonb"
        assert len(_flatten_probe_runs(candidates)) == 2
    finally:
        await db.disconnect()
