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


# --- M3 review: the CLI's own governance --------------------------------------------------------------


class _Conn:
    def __init__(self, rows=()):
        self.rows = list(rows)

    async def fetch(self, sql, *args):
        return self.rows

    async def close(self):
        return None


@pytest.fixture
def engine(monkeypatch):
    """asyncpg and the engine stubbed: records how each entry point was called."""
    import asyncpg

    calls: Dict[str, list] = {"apply": [], "revert": [], "approve": []}
    conn = _Conn()

    async def connect(*a, **k):
        return conn

    async def apply(c, **kw):
        calls["apply"].append(kw)
        return {"run_id": "R", "dry_run": kw.get("dry_run")}

    async def revert(c, pid, **kw):
        calls["revert"].append(kw)
        return {"proposal_id": pid, "dry_run": kw.get("dry_run")}

    async def approve(c, ids, by):
        calls["approve"].append(ids)
        return ids

    monkeypatch.setenv("DATABASE_URL", "postgresql://stub/none")
    monkeypatch.setattr(asyncpg, "connect", connect)
    monkeypatch.setattr(cli, "apply_approved", apply)
    monkeypatch.setattr(cli, "revert_proposal", revert)
    monkeypatch.setattr(cli, "approve_proposals", approve)
    return calls, conn


@pytest.mark.asyncio
async def test_every_write_is_a_dry_run_unless_execute(engine):
    calls, conn = engine
    await cli.main(["apply", "--strategy", "cross_merchant_same_size", "--max-proposals", "5"])
    await cli.main(["revert-proposal", "irp_x"])
    conn.rows = [{"proposal_id": "irp_x", "strategy": "cross_merchant_same_size", "kind": "merge_content_key",
                  "status": "proposed"}]
    await cli.main(["approve", "irp_x", "--by", "peng"])
    assert calls["apply"][0]["dry_run"] is True and calls["revert"][0]["dry_run"] is True
    assert calls["approve"] == []
    await cli.main(["approve", "irp_x", "--by", "peng", "--execute"])
    assert calls["approve"] == [["irp_x"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("argv", [
    ["apply", "--strategy", "same_url_dup", "--max-proposals", "5"],
    ["apply", "--strategy", "retailer_listing_brand_prefix", "--max-proposals", "5"],
    ["propose-merge", "--from-ck", "a", "--to-ck", "b", "--keeper", "k", "--strategy", "junk_url"],
    ["propose-merge", "--from-ck", "a", "--to-ck", "b", "--keeper", "k", "--strategy", "variant_barcode_match"],
])
async def test_a_reserved_strategy_name_is_refused_before_connecting(engine, argv):
    calls, _ = engine
    with pytest.raises(SystemExit):
        await cli.main(argv)
    assert calls["apply"] == []


@pytest.mark.asyncio
async def test_approving_a_reserved_strategys_proposal_is_refused(engine):
    calls, conn = engine
    conn.rows = [{"proposal_id": "irp_s", "strategy": "same_url_dup", "kind": "merge_content_key",
                  "status": "proposed"}]
    with pytest.raises(SystemExit):
        await cli.main(["approve", "irp_s", "--by", "peng", "--execute"])
    assert calls["approve"] == []


def test_every_strategy_another_path_applies_by_name_is_reserved():
    """Pinned against the modules that write or auto-apply strategies, so a new one cannot slip by."""
    import re
    from pathlib import Path

    from services import identity_barcode_link, identity_brand_link
    from services.identity_reconcile_sweep import AUTO_APPROVE_STRATEGIES, JUDGE_STRATEGY

    root = Path(__file__).resolve().parents[2] / "services"
    written = set()
    for name in ("identity_resolution_strategies.py", "identity_tier3_judge.py", "identity_reconcile_sweep.py"):
        written |= set(re.findall(r'strategy\s*=\s*"([a-z0-9_]+)"', (root / name).read_text()))
    written |= {identity_brand_link.STRATEGY, identity_barcode_link.STRATEGY, JUDGE_STRATEGY,
                *AUTO_APPROVE_STRATEGIES}
    assert written and written <= cli.RESERVED_STRATEGIES, written - cli.RESERVED_STRATEGIES
