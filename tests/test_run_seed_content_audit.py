"""Tests for scripts/run_seed_content_audit.py.

Worker orchestrates the audit: SELECT candidates → run audit_seed_data
→ UPDATE external_product_seeds + catalog_products. Tests cover the
SELECT shape (force flag, limit), the dry-run no-write guarantee, the
apply path executing the right UPDATEs, and skip-on-already-audited.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import run_seed_content_audit as worker  # noqa: E402


def _ns(**kwargs) -> SimpleNamespace:
    base = {"limit": 0, "apply": False, "force": False}
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_select_sql_default_skips_already_audited_rows() -> None:
    """Default invocation (force=False) MUST filter out rows whose
    seed_data.review_summary.auditor matches the current auditor
    version. Otherwise re-runs would clobber every row's review
    fields with a fresh timestamp."""
    sql = worker._select_sql(force=False, limit=100)
    assert "review_summary" in sql
    assert "auditor" in sql
    assert ":auditor_version" in sql
    assert "LIMIT :limit" in sql


def test_select_sql_force_processes_all_rows() -> None:
    """--force must skip the audited filter so a new auditor version
    can re-process old auto_corrected rows."""
    sql = worker._select_sql(force=True, limit=100)
    # No auditor filter when forced
    assert ":auditor_version" not in sql
    assert "review_summary" not in sql


def test_select_sql_omits_limit_clause_when_zero() -> None:
    """limit=0 means 'all rows' — the LIMIT clause must be skipped
    entirely (Postgres doesn't accept LIMIT 0 as 'no limit')."""
    sql = worker._select_sql(force=False, limit=0)
    assert "LIMIT" not in sql.upper().replace("LIMIT :LIMIT", "")


def test_coerce_seed_data_handles_dict_string_and_garbage() -> None:
    """asyncpg returns JSONB columns as either dict or JSON-string
    depending on codec path; the worker must accept both."""
    assert worker._coerce_seed_data({"a": 1}) == {"a": 1}
    assert worker._coerce_seed_data('{"a": 1}') == {"a": 1}
    assert worker._coerce_seed_data("not json") is None
    assert worker._coerce_seed_data("[1,2,3]") is None  # list, not dict
    assert worker._coerce_seed_data(None) is None
    assert worker._coerce_seed_data(123) is None


@pytest.mark.asyncio
async def test_drive_dry_run_does_not_call_db_execute(monkeypatch) -> None:
    """Default invocation must NEVER call database.execute. The
    histogram + sample rows still populate."""
    rows = [
        {
            "id": "eps_1",
            "external_product_id": "ext_1",
            "seed_data": {
                "description": "It&rsquo;s nice",
                "pdp_ingredients_raw": "AQUA, GLYCERIN",
            },
            "updated_at": None,
        },
    ]

    db_calls: list = []

    async def fake_fetch_all(_sql, _params):
        return rows

    async def fake_execute(*args, **_kwargs):
        db_calls.append(args)

    monkeypatch.setattr(worker.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(worker.database, "execute", fake_execute)

    report = await worker._drive(_ns(apply=False))

    assert db_calls == []
    assert report["candidate_count"] == 1
    assert report["applied_count"] == 0
    # Issue/fix counters still capture what would be done
    assert report["issue_counts"].get("html_entities_in_description") == 1


@pytest.mark.asyncio
async def test_drive_apply_writes_seed_and_catalog_products_when_fixes_applied(monkeypatch) -> None:
    """When fixes are applied, both UPDATEs fire: external_product_seeds
    (always) + catalog_products (because the cleaned content needs to
    propagate to chat without waiting for the mirror)."""
    rows = [
        {
            "id": "eps_1",
            "external_product_id": "ext_1",
            "seed_data": {
                "description": "It&rsquo;s nice",
                "pdp_ingredients_raw": "TWO'LIP KISS, RIRI: AQUA, GLYCERIN",
            },
            "updated_at": None,
        },
    ]

    executed_sqls: list = []

    async def fake_fetch_all(_sql, _params):
        return rows

    async def fake_execute(sql, params):
        executed_sqls.append({"sql": str(sql), "params": dict(params)})
        return 1  # rowcount-equivalent

    monkeypatch.setattr(worker.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(worker.database, "execute", fake_execute)

    report = await worker._drive(_ns(apply=True))

    assert report["applied_count"] == 1
    # Two UPDATEs per fixed row: external_product_seeds + catalog_products
    assert len(executed_sqls) == 2
    sqls_joined = "\n".join(s["sql"] for s in executed_sqls)
    assert "UPDATE external_product_seeds" in sqls_joined
    assert "UPDATE catalog_products" in sqls_joined
    # The seed_data param carries cleaned content + review_summary
    seed_update = next(s for s in executed_sqls if "external_product_seeds" in s["sql"])
    import json
    parsed = json.loads(seed_update["params"]["seed_data"])
    assert "review_summary" in parsed
    assert parsed["review_summary"]["review_status"] == "auto_corrected"
    assert parsed["description"] == "It’s nice"
    assert parsed["pdp_ingredients_raw"] == "AQUA, GLYCERIN"


@pytest.mark.asyncio
async def test_drive_apply_writes_seed_only_when_no_fix_applied(monkeypatch) -> None:
    """When the audit finds no issues (clean row), we still want to
    write the auditor stamp to external_product_seeds (so future runs
    skip it via the audited filter), but NOT touch catalog_products
    (no content changed)."""
    rows = [
        {
            "id": "eps_clean",
            "external_product_id": "ext_clean",
            "seed_data": {
                "description": "Clean text",
                "pdp_ingredients_raw": "AQUA, GLYCERIN",
            },
            "updated_at": None,
        },
    ]

    executed_sqls: list = []

    async def fake_fetch_all(_sql, _params):
        return rows

    async def fake_execute(sql, params):
        executed_sqls.append({"sql": str(sql), "params": dict(params)})
        return 1

    monkeypatch.setattr(worker.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(worker.database, "execute", fake_execute)

    report = await worker._drive(_ns(apply=True))

    # external_product_seeds always written (auditor stamp)
    assert any("external_product_seeds" in s["sql"] for s in executed_sqls)
    # catalog_products NOT written (no fixes applied)
    assert not any("UPDATE catalog_products" in s["sql"] for s in executed_sqls)
    assert report["status_counts"]["no_issues_found"] == 1
    assert report["catalog_products_updated"] == 0


@pytest.mark.asyncio
async def test_drive_skips_unparseable_seed_data(monkeypatch) -> None:
    """Defensive: legacy rows might have seed_data stored as a
    non-JSON string. The worker must skip them, not crash."""
    rows = [
        {
            "id": "eps_bad",
            "external_product_id": "ext_bad",
            "seed_data": "not valid json",
            "updated_at": None,
        },
    ]

    async def fake_fetch_all(_sql, _params):
        return rows

    executed: list = []

    async def fake_execute(sql, params):
        executed.append((sql, params))

    monkeypatch.setattr(worker.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(worker.database, "execute", fake_execute)

    report = await worker._drive(_ns(apply=True))

    assert executed == []
    assert report["status_counts"]["skipped_seed_data_unparseable"] == 1
    assert report["applied_count"] == 0
