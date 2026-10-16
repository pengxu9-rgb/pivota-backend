"""scripts/identity_engine.py: what an executed apply or revert rebuilds, and that it never writes
without the dry run's digest."""
from __future__ import annotations

from typing import Dict, List

import pytest

from scripts import identity_engine as cli


def test_moved_details_are_the_attaches_and_merges_a_revert_did_not_skip():
    details = [{"attached": "l", "from_content_key": "a", "to_content_key": "b"},
               {"merged": True, "from_content_key": "c", "to_content_key": "d"},
               {"merged": True, "from_content_key": "e", "to_content_key": "f", "reverted": False},
               {"suppressed": 2}]
    assert [d.get("from_content_key") for d in cli.moved_details(details)] == ["a", "c"]


@pytest.mark.asyncio
async def test_a_merge_rebuilds_the_losing_key_first(monkeypatch):
    import services.agent_pdp_view_assembler as apv
    import services.index_pipeline_state_service as ips
    from services.identity_brand_link import refresh_after_move

    seen: List[str] = []

    async def refresh(ck, **kw):
        seen.append(ck)
        return True

    async def nothing(ck, **kw):
        return False

    monkeypatch.setattr(apv, "refresh_agent_pdp_view_for_content_key", refresh)
    monkeypatch.setattr(apv, "delete_agent_pdp_view_if_orphaned", nothing)
    monkeypatch.setattr(ips, "recompute_serving_eligibility", nothing)
    merged: Dict = {"merged": True, "from_content_key": "ck_loser", "to_content_key": "ck_winner"}
    await refresh_after_move(cli.moved_details([merged]), source="t")
    assert seen == ["ck_loser", "ck_winner"]
    seen.clear()
    await refresh_after_move(cli.moved_details([{**merged, "reverted": True}]), source="t")
    assert seen == ["ck_winner", "ck_loser"]


@pytest.mark.asyncio
async def test_execute_without_the_dry_runs_digest_is_refused_before_connecting(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit):
        await cli.main(["apply", "--strategy", "s", "--max-proposals", "5", "--execute"])
