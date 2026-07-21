"""ADR-010 D-2 engine — proposal builders, guards, apply/revert flow (no DB)."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from services.identity_resolution import (  # noqa: E402
    DEACTIVATE_SEEDS_SQL,
    KEEPER_ORPHANED_CHECK_SQL,
    REVERT_ROWS_SQL,
    SUPPRESS_SQL,
    apply_approved,
    member_fingerprint,
    new_proposal,
    proposal_key,
    revert_run,
    suppression_metadata,
)


class TestProposalBuilders:
    def test_fingerprint_is_order_insensitive(self):
        assert member_fingerprint(["b", "a"]) == member_fingerprint(["a", "b"])
        assert member_fingerprint(["a", "b"]) != member_fingerprint(["a", "b", "c"])

    def test_proposal_key_changes_with_member_set(self):
        k1 = proposal_key("same_url_dup", "m", "ck", member_fingerprint(["a", "b"]))
        k2 = proposal_key("same_url_dup", "m", "ck", member_fingerprint(["a", "b", "c"]))
        assert k1 != k2

    def test_new_proposal_is_deterministic(self):
        a = new_proposal(kind="suppress_dup", strategy="same_url_dup",
                         subject_product_keys=["b", "a"], keeper_product_key="a",
                         merchant_id="m", content_key="ck")
        b = new_proposal(kind="suppress_dup", strategy="same_url_dup",
                         subject_product_keys=["a", "b"], keeper_product_key="a",
                         merchant_id="m", content_key="ck")
        assert a["proposal_id"] == b["proposal_id"]
        assert a["subject_product_keys"] == ["a", "b"]

    def test_suppress_dup_requires_keeper_in_members(self):
        with pytest.raises(ValueError):
            new_proposal(kind="suppress_dup", strategy="s",
                         subject_product_keys=["a", "b"], keeper_product_key="z")

    def test_suppress_dup_requires_two_members(self):
        with pytest.raises(ValueError):
            new_proposal(kind="suppress_dup", strategy="s",
                         subject_product_keys=["a"], keeper_product_key="a")

    def test_unknown_kind_rejected(self):
        with pytest.raises(ValueError):
            new_proposal(kind="obliterate", strategy="s", subject_product_keys=["a", "b"])

    def test_metadata_carries_the_revert_handles(self):
        p = new_proposal(kind="suppress_dup", strategy="same_url_dup",
                         subject_product_keys=["a", "b"], keeper_product_key="a")
        meta = json.loads(suppression_metadata(p, "RUN1"))
        assert meta["run_id"] == "RUN1"
        assert meta["proposal_id"] == p["proposal_id"]
        assert meta["keeper_product_key"] == "a"


class TestGuardSQL:
    def test_suppress_excludes_keeper_in_statement(self):
        assert "cp.product_key <> $4" in SUPPRESS_SQL
        assert "suppression_reason IS NULL" in SUPPRESS_SQL

    def test_seed_deactivation_bidirectional_and_keeper_safe(self):
        assert "attached_product_key" in DEACTIVATE_SEEDS_SQL
        assert "id = ANY" in DEACTIVATE_SEEDS_SQL
        assert "attached_product_key <> $3" in DEACTIVATE_SEEDS_SQL
        assert "id IS DISTINCT FROM $4" in DEACTIVATE_SEEDS_SQL

    def test_keeper_orphan_check_is_bidirectional(self):
        assert "source_ref" in KEEPER_ORPHANED_CHECK_SQL
        assert "attached_product_key" in KEEPER_ORPHANED_CHECK_SQL

    def test_revert_only_touches_engine_tombstones(self):
        assert "d2\\_%" in REVERT_ROWS_SQL
        assert "run_id" in REVERT_ROWS_SQL


class FakeTx:
    def __init__(self):  # noqa: D401
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeConn:
    """Scripted asyncpg stand-in: routes queries by leading keyword of the
    SQL against per-table state good enough for the engine flow."""

    def __init__(self, live_groups: Dict[tuple, List[str]],
                 source_refs: Optional[Dict[str, str]] = None,
                 approved: Optional[List[Dict[str, Any]]] = None):
        self.live_groups = live_groups          # (merchant, ck) -> live product_keys
        self.source_refs = source_refs or {}
        self.approved = approved or []
        self.suppressed: List[str] = []
        self.deactivated: List[str] = []
        self.reactivated: List[str] = []
        self.events: List[tuple] = []
        self.marked: List[tuple] = []
        self.orphaned_keepers = 0
        self.reverted_rows: List[str] = []

    def transaction(self):
        return FakeTx()

    async def fetch(self, sql, *args):
        s = " ".join(sql.split())
        if "FROM identity_resolution_proposals WHERE status = 'approved'" in s:
            return self.approved
        if "SELECT product_key FROM catalog_products WHERE merchant_id" in s:
            keys = self.live_groups.get((args[0], args[1]), [])
            return [{"product_key": k} for k in keys if k not in self.suppressed]
        if "SELECT product_key, source_ref FROM catalog_products WHERE product_key = ANY" in s:
            return [{"product_key": k, "source_ref": self.source_refs.get(k)} for k in args[0]]
        if "SET status = 'inactive'" in s:
            refs, losers, keeper_key, keeper_ref = args
            ids = [r for r in refs if r and r != keeper_ref]
            self.deactivated.extend(ids)
            return [{"id": i} for i in ids]
        if "SET status = 'active'" in s:
            self.reactivated.extend(args[0])
            return [{"id": i} for i in args[0]]
        if "suppression_metadata->>'run_id'" in s:
            return [{"product_key": k} for k in self.reverted_rows]
        if "FROM identity_resolution_events" in s:
            return [{"proposal_id": "irp_x", "detail": json.dumps(
                {"deactivated_seed_ids": self.deactivated})}]
        if "SET status = 'reverted'" in s:
            return [{"proposal_id": "irp_x"}]
        raise AssertionError(f"unscripted fetch: {s[:90]}")

    async def fetchrow(self, sql, *args):
        s = " ".join(sql.split())
        if "WHERE product_key = $1" in s:
            return {"product_key": args[0], "source_ref": self.source_refs.get(args[0])}
        raise AssertionError(f"unscripted fetchrow: {s[:90]}")

    async def fetchval(self, sql, *args):
        s = " ".join(sql.split())
        if "SELECT COUNT(*) FROM catalog_products WHERE merchant_id" in s:
            keys = self.live_groups.get((args[0], args[1]), [])
            return len([k for k in keys if k not in self.suppressed])
        if "NOT EXISTS" in s:
            return self.orphaned_keepers
        raise AssertionError(f"unscripted fetchval: {s[:90]}")

    async def execute(self, sql, *args):
        s = " ".join(sql.split())
        if "UPDATE catalog_products" in s and "suppression_reason = $2" in s:
            losers = [k for k in args[0] if k != args[3] and k not in self.suppressed]
            self.suppressed.extend(losers)
            return f"UPDATE {len(losers)}"
        if "INSERT INTO identity_resolution_events" in s:
            self.events.append((args[0], args[1], args[2]))
            return "INSERT 0 1"
        if "SET status = 'applied'" in s:
            self.marked.append(args)
            return "UPDATE 1"
        raise AssertionError(f"unscripted execute: {s[:90]}")


def _approved(members: List[str], keeper: str) -> Dict[str, Any]:
    p = new_proposal(kind="suppress_dup", strategy="same_url_dup",
                     subject_product_keys=members, keeper_product_key=keeper,
                     merchant_id="m1", content_key="ck1")
    p.update({"status": "approved", "evidence": "{}"})
    return p


class TestApplyFlow:
    @pytest.mark.asyncio
    async def test_happy_path_suppresses_losers_and_records_event(self):
        conn = FakeConn(live_groups={("m1", "ck1"): ["a", "b", "c"]},
                        source_refs={"b": "eps_b", "c": "eps_c"},
                        approved=[_approved(["a", "b", "c"], "a")])
        result = await apply_approved(conn, run_id="RUN1")
        assert result["skipped"] == []
        assert len(result["applied"]) == 1
        assert sorted(conn.suppressed) == ["b", "c"]
        assert sorted(conn.deactivated) == ["eps_b", "eps_c"]
        assert conn.events and conn.events[0][1] == "applied"

    @pytest.mark.asyncio
    async def test_member_drift_skips_and_leaves_proposal_approved(self):
        # Live group gained a row since propose time.
        conn = FakeConn(live_groups={("m1", "ck1"): ["a", "b", "x"]},
                        approved=[_approved(["a", "b"], "a")])
        result = await apply_approved(conn, run_id="RUN1")
        assert result["applied"] == []
        assert result["skipped"][0][1] == "member_set_drift"
        assert conn.suppressed == [] and conn.marked == []

    @pytest.mark.asyncio
    async def test_keeper_shared_seed_ref_is_not_deactivated(self):
        # Loser 'b' carries the SAME source_ref seed that backs the keeper.
        conn = FakeConn(live_groups={("m1", "ck1"): ["a", "b"]},
                        source_refs={"a": "eps_shared", "b": "eps_shared"},
                        approved=[_approved(["a", "b"], "a")])
        result = await apply_approved(conn, run_id="RUN1")
        assert len(result["applied"]) == 1
        assert conn.deactivated == []  # shared seed spared by keeper_ref guard

    @pytest.mark.asyncio
    async def test_post_check_failure_raises(self):
        conn = FakeConn(live_groups={("m1", "ck1"): ["a", "b"]},
                        approved=[_approved(["a", "b"], "a")])
        conn.orphaned_keepers = 1
        with pytest.raises(RuntimeError):
            await apply_approved(conn, run_id="RUN1")

    @pytest.mark.asyncio
    async def test_strategy_filter(self):
        conn = FakeConn(live_groups={("m1", "ck1"): ["a", "b"]},
                        approved=[_approved(["a", "b"], "a")])
        result = await apply_approved(conn, run_id="RUN1", strategies=["junk_url"])
        assert result["applied"] == [] and conn.suppressed == []


class TestRevertFlow:
    @pytest.mark.asyncio
    async def test_revert_restores_rows_and_seeds(self):
        conn = FakeConn(live_groups={})
        conn.reverted_rows = ["b", "c"]
        conn.deactivated = ["eps_b", "eps_c"]
        result = await revert_run(conn, "RUN1")
        assert result["restored_rows"] == ["b", "c"]
        assert result["reactivated_seeds"] == ["eps_b", "eps_c"]
        assert conn.events and conn.events[-1][1] == "reverted"
