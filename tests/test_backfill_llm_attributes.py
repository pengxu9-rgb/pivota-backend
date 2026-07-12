"""Tests for scripts/backfill_llm_attributes.py (Fix Plan G — T1 runner).

Pins the pilot-runner contract:
  - the cohort SELECT is beauty + live-non-demo + un-enriched, keyset-paginated;
  - the batch UPDATE writes ONLY llm_attributes and guards (IS NULL OR '{}')
    (idempotent, never clobbers an existing payload / extractor cache);
  - dry-run calls NO LLM and writes NOTHING but reports deterministic coverage +
    a cost estimate;
  - pilot writes the versioned envelope, records ACTUAL cost/parse-failure, and
    FAILS LOUDLY above the parse-failure threshold (the truncation-swallow guard);
  - the founder cost gate refuses an over-budget pilot.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from scripts import backfill_llm_attributes as bf


def _ns(**kw) -> SimpleNamespace:
    base = {
        "mode": "dry-run", "limit": 100, "max_pilot": 100,
        "i_understand_full_cost": False, "provider": "gemini",
        "model": "gemini-2.5-flash", "max_tokens": 512, "max_parse_fail_rate": 0.05,
        "batch_size": 200, "max_batches": 0, "sample_size": 5, "no_write": False,
        "max_retries": 3, "retry_base_delay": 0.0,
    }
    base.update(kw)
    return SimpleNamespace(**base)


class _FakeDB:
    """Honors the keyset SELECT window + guarded batch UPDATE. Rows are already
    the joined shape the SELECT would return."""

    def __init__(self, rows: List[Dict[str, Any]]):
        self.rows = {r["product_key"]: dict(r) for r in rows}
        self.is_connected = True
        self.updates: List[Dict[str, Any]] = []

    async def connect(self):  # pragma: no cover
        return None

    async def disconnect(self):  # pragma: no cover
        return None

    async def fetch_all(self, sql, params):
        cursor = params["cursor"]
        batch_size = params["batch_size"]
        eligible = [
            r for r in self.rows.values()
            if r["product_key"] > cursor
            and (r.get("llm_attributes") in (None, {}))
        ]
        eligible.sort(key=lambda r: r["product_key"])
        return eligible[:batch_size]

    async def execute(self, sql, params):
        self.updates.append({"sql": str(sql), "params": dict(params)})
        for pk, payload in zip(params["keys"], params["payloads"]):
            row = self.rows.get(pk)
            if row is not None and row.get("llm_attributes") in (None, {}):
                row["llm_attributes"] = json.loads(payload)
        return 1


def _beauty_row(pk: str, **over) -> Dict[str, Any]:
    base = {
        "product_key": pk, "merchant_id": "m1", "platform": "shopify",
        "source_product_id": pk, "title": "Snail Essence 100ml",
        "description": "Hydrating essence for dry skin. Fragrance-free.",
        "product_type": "Essence", "category": "Skincare",
        "category_path": "beauty/skincare/essence", "category_kind": "skincare",
        "tags": None, "concerns_json": None, "active_ingredients_json": None,
        "raw_inci": "Water, Snail Secretion Filtrate, Niacinamide",
        "concentration_notes_json": None, "seed_inci": None, "shade_json": None,
        "llm_attributes": None,
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------- #
# SQL contract (source-level)
# --------------------------------------------------------------------------- #

def test_cohort_sql_is_beauty_live_nondemo_unenriched_keyset():
    sql = bf._SELECT_SQL
    assert "resolved_vertical = 'beauty'" in sql
    assert "suppression_reason IS NULL" in sql
    assert "pivota-review-demo%" in sql
    assert "cp.merchant_id <> ALL(:demo_merchants)" in sql
    assert "llm_attributes IS NULL OR cp.llm_attributes = '{}'::jsonb" in sql
    assert "cp.product_key > :cursor" in sql
    assert "ORDER BY cp.product_key ASC" in sql


def test_update_sql_writes_only_llm_attributes_and_guards():
    sql = bf._UPDATE_BATCH_SQL
    assert "SET llm_attributes = CAST(v.payload AS jsonb)" in sql
    assert "c.llm_attributes IS NULL OR c.llm_attributes = '{}'::jsonb" in sql
    assert "unnest(" in sql.lower()
    for forbidden in ("resolved_vertical", "category", "product_payload", "title"):
        assert f"SET {forbidden}" not in sql


# --------------------------------------------------------------------------- #
# dry-run: no LLM, no writes, coverage + estimate
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_dry_run_no_llm_no_writes_reports_coverage():
    db = _FakeDB([_beauty_row("p1"), _beauty_row("p2")])
    report = await bf._drive(_ns(mode="dry-run"), db=db, synthesize=None)
    assert db.updates == []
    assert report["scanned"] == 2
    # volume + key_ingredients + concerns + format resolved deterministically
    cov = report["deterministic_field_coverage"]
    assert cov["volume"]["count"] == 2
    assert cov["key_ingredients"]["count"] == 2
    # skin_type/texture/finish are residual for these rows
    assert report["residual_field_frequency"].get("skin_type") == 2
    assert report["estimate"]["residual_llm_calls"] == 2
    assert report["estimate"]["est_cost_usd_for_scanned"] >= 0.0
    # rows untouched
    assert all(r["llm_attributes"] is None for r in db.rows.values())


# --------------------------------------------------------------------------- #
# pilot: writes versioned envelope, records cost + parse-fail
# --------------------------------------------------------------------------- #

def _synth_ok(**_kw):
    async def _run(**_k):
        return {"text": '{"skin_type":["dry"],"texture":"watery"}',
                "finish_reason": "stop", "usage": {"input_tokens": 100, "output_tokens": 15}}
    return _run()


@pytest.mark.asyncio
async def test_pilot_writes_versioned_envelope_and_costs():
    db = _FakeDB([_beauty_row("p1"), _beauty_row("p2")])

    async def synth(**_kw):
        return {"text": '{"skin_type":["dry"],"texture":"watery"}',
                "finish_reason": "stop", "usage": {"input_tokens": 100, "output_tokens": 15}}

    report = await bf._drive(_ns(mode="pilot", limit=2, batch_size=200), db=db, synthesize=synth)
    assert report["pilot"]["written"] == 2
    assert set(report["pilot"]["written_product_keys"]) == {"p1", "p2"}
    assert report["pilot"]["parse_fail_rate"] == 0.0
    assert report["pilot"]["actual_input_tokens"] == 200
    assert report["pilot"]["actual_cost_usd"] >= 0.0
    assert "FATAL" not in report
    # envelope written with schema_version + merged llm skin_type
    env = db.rows["p1"]["llm_attributes"]
    assert env["schema_version"].startswith("structural_depth")
    assert env["attributes"]["skin_type"] == ["dry"]
    assert env["attributes"]["volume"] == "100 ml"        # deterministic
    assert env["provenance"]["skin_type"].startswith("llm:")


@pytest.mark.asyncio
async def test_pilot_stops_at_limit():
    db = _FakeDB([_beauty_row(f"p{i:02d}") for i in range(10)])

    async def synth(**_kw):
        return {"text": "{}", "finish_reason": "stop", "usage": {}}

    report = await bf._drive(_ns(mode="pilot", limit=3, batch_size=2), db=db, synthesize=synth)
    assert report["scanned"] == 3
    assert report["pilot"]["written"] == 3


@pytest.mark.asyncio
async def test_pilot_fails_loudly_on_truncation():
    db = _FakeDB([_beauty_row(f"p{i:02d}") for i in range(4)])

    async def synth(**_kw):
        # Every call truncates -> 100% parse-failure -> FATAL.
        return {"text": '{"skin_type": ["dr', "finish_reason": "length", "usage": {}}

    report = await bf._drive(_ns(mode="pilot", limit=4, max_parse_fail_rate=0.05), db=db, synthesize=synth)
    assert report["pilot"]["parse_fail_rate"] == 1.0
    assert "FATAL" in report


@pytest.mark.asyncio
async def test_pilot_no_write_flag_skips_writes():
    db = _FakeDB([_beauty_row("p1")])

    async def synth(**_kw):
        return {"text": '{"skin_type":["dry"]}', "finish_reason": "stop", "usage": {}}

    report = await bf._drive(_ns(mode="pilot", limit=1, no_write=True), db=db, synthesize=synth)
    assert db.updates == []
    assert report["pilot"]["written"] == 0


# --------------------------------------------------------------------------- #
# founder cost gate
# --------------------------------------------------------------------------- #

def test_main_refuses_over_budget_pilot(monkeypatch):
    monkeypatch.setattr(
        bf, "_parse_args",
        lambda argv=None: _ns(mode="pilot", limit=9000, max_pilot=100,
                               i_understand_full_cost=False),
    )
    with pytest.raises(SystemExit) as exc:
        bf.main()
    assert "REFUSED" in str(exc.value)
