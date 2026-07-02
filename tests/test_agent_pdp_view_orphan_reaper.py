"""Tests for the agent_pdp_view orphan reaper — delete_agent_pdp_view_if_orphaned
(the inline re-key reap in catalog_sync) and reap_orphaned_agent_pdp_view_rows
(the catch-all sweep script).

An orphan is an agent_pdp_view row whose content_key no longer exists in
catalog_products (product re-keyed on re-sync; the old view row was left behind,
squatting on the still-live pivota_signature_id). The invariant under test: reap
IFF unreferenced — a content_key still present in catalog_products is never removed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import agent_pdp_view_assembler as apv  # noqa: E402


class _FakeDB:
    """In-memory stand-in that answers exactly the queries the reaper issues.

    view:    {content_key: {"pivota_signature_id", "title", "refresh_source",
                            "has_evidence"}}
    catalog: set of content_keys present in catalog_products.
    """

    def __init__(self, view: Dict[str, Dict[str, Any]], catalog: set) -> None:
        self.view = dict(view)
        self.catalog = set(catalog)

    async def fetch_val(self, sql: str, params: Dict[str, Any]) -> Any:
        # SELECT EXISTS(view WHERE ck) AND NOT EXISTS(catalog WHERE ck)
        ck = params["ck"]
        return (ck in self.view) and (ck not in self.catalog)

    async def fetch_all(self, sql: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        # Orphan select: view rows whose content_key is absent from catalog.
        orphans = [
            {
                "content_key": ck,
                "pivota_signature_id": row.get("pivota_signature_id"),
                "title": row.get("title"),
                "refresh_source": row.get("refresh_source"),
                "has_evidence": bool(row.get("has_evidence")),
            }
            for ck, row in self.view.items()
            if ck not in self.catalog
        ]
        limit = params.get("limit")
        return orphans[:limit] if limit else orphans

    async def execute(self, sql: str, params: Dict[str, Any]) -> None:
        # Guarded delete: remove the view row only if unreferenced by catalog.
        assert "DELETE FROM agent_pdp_view" in sql
        ck = params["content_key"]
        if ck in self.view and ck not in self.catalog:
            del self.view[ck]


@pytest.mark.asyncio
async def test_reaps_a_true_orphan() -> None:
    db = _FakeDB(view={"ck-orphan": {"title": "Old", "pivota_signature_id": "sig-1"}}, catalog=set())
    deleted = await apv.delete_agent_pdp_view_if_orphaned("ck-orphan", db=db)
    assert deleted is True
    assert "ck-orphan" not in db.view


@pytest.mark.asyncio
async def test_never_reaps_a_live_content_key() -> None:
    # content_key present in BOTH view and catalog (the legitimate multi-seller /
    # not-yet-rekeyed case) must survive.
    db = _FakeDB(view={"ck-live": {"title": "Live"}}, catalog={"ck-live"})
    deleted = await apv.delete_agent_pdp_view_if_orphaned("ck-live", db=db)
    assert deleted is False
    assert "ck-live" in db.view


@pytest.mark.asyncio
async def test_noop_when_no_view_row() -> None:
    db = _FakeDB(view={}, catalog={"ck-x"})
    assert await apv.delete_agent_pdp_view_if_orphaned("ck-x", db=db) is False


@pytest.mark.asyncio
async def test_empty_content_key_is_noop() -> None:
    db = _FakeDB(view={"": {"title": "weird"}}, catalog=set())
    assert await apv.delete_agent_pdp_view_if_orphaned("", db=db) is False
    assert "" in db.view  # untouched


@pytest.mark.asyncio
async def test_sweep_dry_run_reports_without_deleting() -> None:
    db = _FakeDB(
        view={
            "ck-orphan-1": {"title": "O1", "has_evidence": True},
            "ck-orphan-2": {"title": "O2"},
            "ck-live": {"title": "L"},
        },
        catalog={"ck-live"},
    )
    report = await apv.reap_orphaned_agent_pdp_view_rows(db=db, dry_run=True)
    assert report["orphans"] == 2
    assert report["with_evidence"] == 1
    assert report["deleted"] == 0
    assert set(db.view) == {"ck-orphan-1", "ck-orphan-2", "ck-live"}  # nothing removed


@pytest.mark.asyncio
async def test_sweep_apply_deletes_only_orphans() -> None:
    db = _FakeDB(
        view={
            "ck-orphan-1": {"title": "O1"},
            "ck-orphan-2": {"title": "O2"},
            "ck-live": {"title": "L"},
        },
        catalog={"ck-live"},
    )
    report = await apv.reap_orphaned_agent_pdp_view_rows(db=db, dry_run=False)
    assert report["orphans"] == 2
    assert report["deleted"] == 2
    assert set(db.view) == {"ck-live"}  # live row survives


@pytest.mark.asyncio
async def test_sweep_respects_limit() -> None:
    db = _FakeDB(
        view={f"ck-orphan-{i}": {"title": str(i)} for i in range(5)},
        catalog=set(),
    )
    report = await apv.reap_orphaned_agent_pdp_view_rows(db=db, limit=2, dry_run=False)
    assert report["orphans"] == 2
    assert report["deleted"] == 2
    assert len(db.view) == 3  # only the limited batch removed
