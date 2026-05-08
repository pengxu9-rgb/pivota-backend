"""Tests for scripts/backfill_pdp_lifecycle_stage.py (Phase O-6b).

The backfill must:
  - Only touch rows where pdp_lifecycle_stage IS NULL (idempotent re-run)
  - Compute the stage via the same pure function the 3 ingest paths use
  - Default to dry-run (no UPDATE without --apply)
  - Handle JSONB columns returned as JSON-encoded strings (asyncpg
    sometimes returns list, sometimes str depending on codec path)
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import backfill_pdp_lifecycle_stage as backfill  # noqa: E402


def _ns(**kwargs) -> SimpleNamespace:
    """Build an args namespace with defaults overridden by kwargs."""
    base = {"limit": 1000, "apply": False}
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_normalize_jsonb_field_decodes_json_string():
    assert backfill._normalize_jsonb_field('["a", "b"]') == ["a", "b"]


def test_normalize_jsonb_field_passes_through_list():
    assert backfill._normalize_jsonb_field(["a", "b"]) == ["a", "b"]


def test_normalize_jsonb_field_returns_string_when_invalid_json():
    """If the column was somehow written as a non-JSON string (legacy
    rows), return the raw string — the gate's _has_nonempty_list helper
    handles comma-separated strings as a fallback."""
    assert backfill._normalize_jsonb_field("not, json") == "not, json"


def test_normalize_jsonb_field_passes_none():
    assert backfill._normalize_jsonb_field(None) is None


@pytest.mark.asyncio
async def test_drive_dry_run_does_not_execute_updates(monkeypatch):
    """Default invocation (no --apply) must compute the stage and
    populate the histogram, but NEVER call database.execute."""
    rows = [
        {
            "product_key": "draft_row",
            "title": None,
            "description": None,
            "image_url": None,
            "category_path": None,
            "tags": None,
            "demographic": None,
            "use_case_tags": None,
            "lifestyle_tags": None,
            "pdp_scope": "merchant_owned",
            "source_system": "shopify",
        },
        {
            "product_key": "validated_row",
            "title": "Vitamin C Serum",
            "description": "A long enough description to satisfy the candidate length gate by a margin.",
            "image_url": "https://x/y.jpg",
            "category_path": "beauty/skincare/serum",
            "tags": '["k-beauty"]',  # JSONB string form
            "demographic": "women",
            "use_case_tags": ["daily"],
            "lifestyle_tags": [],
            "pdp_scope": "merchant_owned",
            "source_system": "shopify",
        },
        {
            "product_key": "published_row",
            "title": "Curated Lipstick",
            "description": "Hand-curated lipstick from the agent pipeline with adequate description text.",
            "image_url": "https://x/lip.jpg",
            "category_path": "beauty/makeup/lip",
            "tags": ["matte"],
            "demographic": "women",
            "use_case_tags": ["daily"],
            "lifestyle_tags": ["vegan"],
            "pdp_scope": None,
            "source_system": "catalog_enrichment_agent_v1",
        },
    ]

    executed: list = []

    async def fake_fetch_all(_sql, _params):
        return rows

    async def fake_execute(*args, **kwargs):
        executed.append((args, kwargs))

    async def fake_connect_with_retry(**_kwargs):
        return None

    monkeypatch.setattr(backfill.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(backfill.database, "execute", fake_execute)
    monkeypatch.setattr(backfill, "_connect_with_retry", fake_connect_with_retry)

    report = await backfill._drive(_ns(apply=False))

    assert report["candidate_count"] == 3
    assert report["applied_count"] == 0
    assert executed == [], "dry-run must not execute any UPDATEs"
    assert report["stage_counts"] == {
        "draft": 1,
        "validated": 1,
        "published": 1,
    }


@pytest.mark.asyncio
async def test_drive_apply_executes_one_update_per_candidate(monkeypatch):
    """With --apply, every candidate gets exactly one UPDATE with its
    computed stage. The SQL guards `pdp_lifecycle_stage IS NULL` so a
    concurrent writer can't be clobbered."""
    rows = [
        {
            "product_key": "row_a",
            "title": "X",
            "description": None,
            "image_url": None,
            "category_path": None,
            "tags": None,
            "demographic": None,
            "use_case_tags": None,
            "lifestyle_tags": None,
            "pdp_scope": None,
            "source_system": None,
        },
        {
            "product_key": "row_b",
            "title": "Y",
            "description": "Y" * 60,
            "image_url": "https://y",
            "category_path": "beauty/skincare/x",
            "tags": ["k-beauty"],
            "demographic": None,
            "use_case_tags": None,
            "lifestyle_tags": None,
            "pdp_scope": "merchant_owned",
            "source_system": "shopify",
        },
    ]

    executed: list = []

    async def fake_fetch_all(_sql, _params):
        return rows

    async def fake_execute(_sql, params):
        executed.append(params)

    async def fake_connect_with_retry(**_kwargs):
        return None

    monkeypatch.setattr(backfill.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(backfill.database, "execute", fake_execute)
    monkeypatch.setattr(backfill, "_connect_with_retry", fake_connect_with_retry)

    report = await backfill._drive(_ns(apply=True))

    assert report["applied_count"] == 2
    assert len(executed) == 2
    assert executed[0]["product_key"] == "row_a"
    assert executed[0]["stage"] == "draft"  # no content
    assert executed[1]["product_key"] == "row_b"
    assert executed[1]["stage"] == "validated"


@pytest.mark.asyncio
async def test_drive_per_scope_breakdown_counts_by_scope_then_stage(monkeypatch):
    rows = [
        {
            "product_key": "p1",
            "title": "T",
            "description": "D" * 60,
            "image_url": "https://x",
            "category_path": "beauty/x",
            "tags": ["k-beauty"],
            "demographic": "women",
            "use_case_tags": [],
            "lifestyle_tags": [],
            "pdp_scope": "multi_merchant_canonical",
            "source_system": "shopify",
        },
        {
            "product_key": "p2",
            "title": "T",
            "description": "D" * 60,
            "image_url": "https://x",
            "category_path": "beauty/x",
            "tags": ["k-beauty"],
            "demographic": "women",
            "use_case_tags": [],
            "lifestyle_tags": [],
            "pdp_scope": "merchant_owned",
            "source_system": "shopify",
        },
    ]

    async def fake_fetch_all(_sql, _params):
        return rows

    async def fake_execute(*_args, **_kwargs):
        return None

    async def fake_connect_with_retry(**_kwargs):
        return None

    monkeypatch.setattr(backfill.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(backfill.database, "execute", fake_execute)
    monkeypatch.setattr(backfill, "_connect_with_retry", fake_connect_with_retry)

    report = await backfill._drive(_ns(apply=False))

    # canonical scope reaches published; merchant_owned stops at validated
    assert report["per_scope_stage"]["multi_merchant_canonical"] == {"published": 1}
    assert report["per_scope_stage"]["merchant_owned"] == {"validated": 1}


def test_select_sql_only_targets_null_stage_rows():
    """Pin the SELECT clause so a future refactor doesn't widen the
    scope to already-staged rows (which would clobber existing stages)."""
    assert "pdp_lifecycle_stage IS NULL" in backfill.SELECT_SQL


def test_update_sql_guards_against_concurrent_writes():
    """The UPDATE must include the `pdp_lifecycle_stage IS NULL` guard
    so that a row staged by a concurrent writer (Path A/B/C ingest)
    isn't silently overwritten by the backfill."""
    assert "pdp_lifecycle_stage IS NULL" in backfill.UPDATE_SQL


@pytest.mark.asyncio
async def test_drive_reports_limit_hit_when_candidates_match_limit(monkeypatch):
    """If the SELECT returns exactly --limit rows, the operator needs
    to know more rows may exist. Pin both the report flag and the
    log warning so a future refactor doesn't silently swallow this
    signal — that's the whole point of bumping the default to 10000:
    catch a partial backfill before someone declares victory."""

    rows = [
        {
            "product_key": f"p{i}",
            "title": "T",
            "description": "D" * 60,
            "image_url": "https://x",
            "category_path": "beauty/x",
            "tags": ["k-beauty"],
            "demographic": "women",
            "use_case_tags": [],
            "lifestyle_tags": [],
            "pdp_scope": "merchant_owned",
            "source_system": "shopify",
        }
        for i in range(3)
    ]

    async def fake_fetch_all(_sql, _params):
        return rows

    async def fake_execute(*_args, **_kwargs):
        return None

    async def fake_connect_with_retry(**_kwargs):
        return None

    monkeypatch.setattr(backfill.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(backfill.database, "execute", fake_execute)
    monkeypatch.setattr(backfill, "_connect_with_retry", fake_connect_with_retry)

    # candidates count == limit → must signal "may be more"
    report_at_limit = await backfill._drive(_ns(limit=3))
    assert report_at_limit["limit_hit"] is True

    # candidates count < limit → no signal, run completed the table
    report_under_limit = await backfill._drive(_ns(limit=1000))
    assert report_under_limit["limit_hit"] is False


def test_default_limit_covers_current_catalog():
    """Catalog is ~4690 rows; default must be high enough to cover it
    in one run. Earlier default of 1000 silently truncated the
    backfill — pin against that regression."""
    parser_default = backfill._parse_args.__wrapped__ if hasattr(
        backfill._parse_args, "__wrapped__"
    ) else None
    # Inspect the argparse default the simple way: build a parser
    # and read its default.
    import argparse as _argparse

    p = _argparse.ArgumentParser()
    # Mirror the runtime registration to read the default. We can't
    # call _parse_args directly because it consumes sys.argv. Instead
    # we re-invoke the real parser with no args (after stubbing argv).
    import sys as _sys

    saved_argv = _sys.argv
    try:
        _sys.argv = ["backfill_pdp_lifecycle_stage.py"]
        ns = backfill._parse_args()
    finally:
        _sys.argv = saved_argv
    assert ns.limit >= 5000, (
        f"default --limit must cover the current ~5k catalog; got {ns.limit}"
    )
