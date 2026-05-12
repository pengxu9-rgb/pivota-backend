"""Tests for scripts/backfill_content_key.py.

The backfill is the one-off mechanism that turns legacy catalog_products
rows (predating mig 083) into content_key-keyed rows. Tests pin:
  - SELECT shape (stable product_key window; UPDATE guards NULL rows)
  - LIMIT / OFFSET binding discipline
  - Dry-run does not call execute
  - Apply skips rows where brand or title are empty (make_content_key returns None)
  - Apply does NOT touch seed_data / product_payload
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import backfill_content_key as backfill  # noqa: E402


def _ns(**kw) -> SimpleNamespace:
    base = {"apply": False, "limit": 200, "offset": 0}
    base.update(kw)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# SELECT SQL shape
# ---------------------------------------------------------------------------


def test_select_sql_uses_stable_product_key_window() -> None:
    """The SELECT must page over a stable all-row window. Filtering to
    NULL content_key rows before OFFSET would skip rows after previous
    chunks update content_key."""
    sql = backfill._select_sql(limit=100, offset=0)
    assert "cp.content_key" in sql
    assert "WHERE cp.content_key IS NULL" not in sql
    assert "ORDER BY cp.product_key ASC" in sql


def test_select_sql_limit_clause_only_when_positive() -> None:
    """limit=0 means 'all rows' — must omit the LIMIT clause
    (Postgres rejects LIMIT 0 as a literal)."""
    assert "LIMIT :limit" in backfill._select_sql(limit=100, offset=0)
    no_limit = backfill._select_sql(limit=0, offset=0)
    # LIMIT clause absent when limit=0
    assert "LIMIT :limit" not in no_limit


def test_select_sql_offset_clause_only_when_positive() -> None:
    """OFFSET appears only when offset>0 — same param-bind discipline
    as the audit worker fix (PR #423)."""
    assert "OFFSET" not in backfill._select_sql(limit=100, offset=0)
    assert "OFFSET :offset" in backfill._select_sql(limit=100, offset=200)


# ---------------------------------------------------------------------------
# _fetch_candidates param-bind discipline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_candidates_binds_limit_and_offset_only_when_positive(monkeypatch) -> None:
    """Avoids the SQL parser rejecting extra binds — same trap PR #423
    fixed for the audit worker."""
    captured: list = []

    async def fake_fetch_all(sql, params):
        captured.append({"sql": str(sql), "params": dict(params)})
        return []

    monkeypatch.setattr(backfill.database, "fetch_all", fake_fetch_all)

    await backfill._fetch_candidates(_ns(limit=200, offset=0))
    assert captured[0]["params"] == {"limit": 200}

    captured.clear()
    await backfill._fetch_candidates(_ns(limit=200, offset=400))
    assert captured[0]["params"] == {"limit": 200, "offset": 400}

    captured.clear()
    await backfill._fetch_candidates(_ns(limit=0, offset=0))
    assert captured[0]["params"] == {}


# ---------------------------------------------------------------------------
# _drive — dry run vs apply, no-op cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drive_dry_run_does_not_execute_updates(monkeypatch) -> None:
    """Default invocation must NEVER call database.execute. Operators
    expect --dry-run reporting to be side-effect free."""
    rows = [
        {"product_key": "p1", "content_key": None, "brand": "Glow Recipe",
         "title": "Plum Plump Hyaluronic Serum", "gtin": None},
        {"product_key": "p2", "content_key": None, "brand": "MAC",
         "title": "Lipstick Russian Red", "gtin": "0773602443796"},
    ]
    execute_calls: list = []

    async def fake_fetch_all(_sql, _params):
        return rows

    async def fake_execute(*args, **_kwargs):
        execute_calls.append(args)

    async def fake_connect():
        return None

    monkeypatch.setattr(backfill.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(backfill.database, "execute", fake_execute)
    monkeypatch.setattr(backfill.database, "is_connected", True)

    report = await backfill._drive(_ns(apply=False))
    assert execute_calls == []
    assert report["outcome_counts"]["considered"] == 2
    assert report["outcome_counts"]["key_computed"] == 2
    assert report["outcome_counts"]["skipped_no_op_in_dry_run"] == 2
    assert report["outcome_counts"]["updated"] == 0


@pytest.mark.asyncio
async def test_drive_apply_executes_one_update_per_eligible_row(monkeypatch) -> None:
    """--apply: each computed-key row gets one UPDATE. The UPDATE SQL
    only touches the content_key column (NOT seed_data / product_payload)."""
    rows = [
        {"product_key": "p1", "content_key": None, "brand": "Glow Recipe",
         "title": "Plum Plump Serum", "gtin": None},
        {"product_key": "p2", "content_key": None, "brand": "MAC",
         "title": "Lipstick", "gtin": "0773602443796"},
    ]
    executed: list = []

    async def fake_fetch_all(_sql, _params):
        return rows

    async def fake_execute(sql, params):
        executed.append({"sql": str(sql), "params": params})
        return 1

    monkeypatch.setattr(backfill.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(backfill.database, "execute", fake_execute)
    monkeypatch.setattr(backfill.database, "is_connected", True)

    report = await backfill._drive(_ns(apply=True))
    assert len(executed) == 2
    # Both UPDATEs target content_key only
    for call in executed:
        assert "UPDATE catalog_products" in call["sql"]
        assert "SET content_key" in call["sql"]
        assert "seed_data" not in call["sql"]
        assert "product_payload" not in call["sql"]
        # Idempotency guard: only UPDATEs when content_key IS NULL
        assert "content_key IS NULL" in call["sql"]
        assert "WHERE product_key = :product_key" in call["sql"]
    assert report["outcome_counts"]["updated"] == 2


@pytest.mark.asyncio
async def test_drive_skips_rows_with_empty_brand_or_title(monkeypatch) -> None:
    """make_content_key returns None when brand or title is empty;
    backfill counts these as 'key_null_brand_or_title_missing' and
    skips the UPDATE. These rows stay NULL on content_key — Stage 2's
    auto-grouper will skip them too."""
    rows = [
        {"product_key": "p1", "content_key": None, "brand": "", "title": "Title",
         "gtin": None},
        {"product_key": "p2", "content_key": None, "brand": None, "title": "T2",
         "gtin": None},
        {"product_key": "p3", "content_key": None, "brand": "Brand", "title": "",
         "gtin": None},
    ]
    executed: list = []

    async def fake_fetch_all(_sql, _params):
        return rows

    async def fake_execute(sql, params):
        executed.append({"sql": str(sql), "params": params})
        return 1

    monkeypatch.setattr(backfill.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(backfill.database, "execute", fake_execute)
    monkeypatch.setattr(backfill.database, "is_connected", True)

    report = await backfill._drive(_ns(apply=True))
    assert executed == []
    assert report["outcome_counts"]["key_null_brand_or_title_missing"] == 3
    assert report["outcome_counts"]["updated"] == 0


@pytest.mark.asyncio
async def test_drive_report_includes_sample_outcomes(monkeypatch) -> None:
    """Operators want to eyeball a few examples before flipping to
    --apply across the full table. Sample size capped at 5."""
    rows = [
        {"product_key": f"p{i}", "content_key": None, "brand": "B",
         "title": f"T{i}", "gtin": None}
        for i in range(20)
    ]

    async def fake_fetch_all(_sql, _params):
        return rows

    async def fake_execute(*args, **_kwargs):
        return None

    monkeypatch.setattr(backfill.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(backfill.database, "execute", fake_execute)
    monkeypatch.setattr(backfill.database, "is_connected", True)

    report = await backfill._drive(_ns(apply=False))
    assert len(report["samples"]) == 5
    # Each sample carries the computed key + the input fields the
    # operator needs to spot-check normalization decisions
    for s in report["samples"]:
        assert "content_key" in s and s["content_key"].startswith("ck_")
        assert "brand" in s and "title" in s


@pytest.mark.asyncio
async def test_drive_skips_already_keyed_rows_in_stable_window(monkeypatch) -> None:
    """Stable-window pagination reads all rows; rows already keyed must
    not be updated again."""
    rows = [
        {"product_key": "p1", "content_key": "ck_" + "1" * 32, "brand": "B", "title": "T1", "gtin": None},
        {"product_key": "p2", "content_key": None, "brand": "B", "title": "T2", "gtin": None},
    ]
    executed: list = []

    async def fake_fetch_all(_sql, _params):
        return rows

    async def fake_execute(sql, params):
        executed.append({"sql": str(sql), "params": params})
        return 1

    monkeypatch.setattr(backfill.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(backfill.database, "execute", fake_execute)
    monkeypatch.setattr(backfill.database, "is_connected", True)

    report = await backfill._drive(_ns(apply=True))

    assert report["outcome_counts"]["considered"] == 2
    assert report["outcome_counts"]["skipped_already_keyed"] == 1
    assert report["outcome_counts"]["key_computed"] == 1
    assert len(executed) == 1
    assert executed[0]["params"]["product_key"] == "p2"


# ---------------------------------------------------------------------------
# Integration with make_content_key
# ---------------------------------------------------------------------------


def test_backfill_uses_make_content_key_function() -> None:
    """Source-level check: the script imports make_content_key and uses
    it for key computation. If someone refactors and inlines a different
    hash, content_key drifts from new-write content_key — and Stage 2
    auto-grouping breaks silently."""
    src = (Path(__file__).resolve().parents[1] / "scripts" / "backfill_content_key.py").read_text()
    assert "from services.catalog_identity import make_content_key" in src
    assert "make_content_key(" in src
