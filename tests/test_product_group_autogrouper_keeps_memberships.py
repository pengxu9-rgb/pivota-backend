"""ADR-032 M11 item 3: the autogrouper never moves a listing out of a group it is already in.

A listing with no membership, or already in its cluster's group (`pg_` + the content_key's hex),
joins; a listing in ANY other group (curated, engine-moved, a variant promoter's, the singleton of an
earlier content_key) stays there, is reported as kept, and is never picked as the cluster's primary.
A cluster with fewer than two free listings writes nothing. The upsert's `DO UPDATE ... WHERE` enforces
the rule in SQL (tests/test_product_group_autogrouper_keeps_memberships_postgres.py runs it on Postgres).
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List

import pytest

from services import product_group_autogrouper as autogrouper

CK = "ck_" + "a" * 32
PG = "pg_" + "a" * 32
OTHER_PG = "pg_curated_other"


def _db_row(pk, stage=None, group=None, minted=None):
    return {"content_key": CK, "product_key": pk, "merchant_id": f"m_{pk}", "platform": "shopify",
            "source_product_id": f"s_{pk}", "pdp_lifecycle_stage": stage,
            "pivota_signature_minted_at": minted, "current_group_id": group}


class _RollbackTxn:
    """Records whether the transaction ended in an exception (a rollback)."""

    def __init__(self, log):
        self.log = log

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, *exc):
        self.log.append("rollback" if exc_type else "commit")
        return False


class _Executed(list):
    """The writes, plus the primary checks (`checks`) and how each transaction ended (`ends`)."""


def _install(monkeypatch, rows, *, primary_landed=True):
    executed = _Executed()
    checks: List[Dict[str, Any]] = []
    ends: List[str] = []

    async def fetch_all(sql, params):
        return rows

    async def fetch_one(sql, params=None):
        checks.append({"sql": str(sql), "params": dict(params or {})})
        return {"?column?": 1} if primary_landed else None

    async def execute(sql, params=None):
        executed.append({"sql": str(sql), "params": dict(params or {})})

    monkeypatch.setattr(autogrouper.database, "fetch_all", fetch_all)
    monkeypatch.setattr(autogrouper.database, "fetch_one", fetch_one)
    monkeypatch.setattr(autogrouper.database, "execute", execute)
    monkeypatch.setattr(autogrouper.database, "transaction", lambda: _RollbackTxn(ends))
    executed.checks, executed.ends = checks, ends
    return executed


def _upserted(executed):
    return {e["params"]["merchant_id"]: e["params"]["is_primary"] for e in executed
            if "INSERT INTO product_group_members" in e["sql"]}


def test_the_cluster_read_returns_each_listings_current_group():
    for kw in ({"content_key": CK, "merchant_id": None}, {"content_key": None, "merchant_id": "m"},
               {"content_key": None, "merchant_id": None}):
        sql, _ = autogrouper._build_cluster_query(limit=10, **kw)
        assert "m.product_group_id AS current_group_id" in sql
        assert ("LEFT JOIN product_group_members m" in sql and "m.merchant_id = cp.merchant_id" in sql
                and "m.platform = cp.platform" in sql and "m.platform_product_id = cp.source_product_id" in sql)


def test_the_upsert_only_updates_a_row_already_in_the_same_group():
    sql = autogrouper._UPSERT_MEMBER_SQL
    assert "WHERE product_group_members.product_group_id = EXCLUDED.product_group_id" in sql
    assert "product_group_id = EXCLUDED.product_group_id,\n" not in sql, "the SET never moves the group"
    assert "is_primary = EXCLUDED.is_primary" in sql


@pytest.mark.asyncio
@pytest.mark.parametrize("apply", [True, False])
async def test_a_listing_in_another_group_is_kept_and_never_primary(monkeypatch, apply):
    rows = [_db_row("p1", stage="published", group=OTHER_PG),   # would win the primary pick
            _db_row("p2", stage="validated", group=PG),          # already a member
            _db_row("p3", stage="draft")]                         # no membership
    executed = _install(monkeypatch, rows)
    report = await autogrouper.autogroup_clusters(content_key=CK, apply=apply)
    assert report.clusters_grouped == 1 and report.members_kept_total == 1
    [out] = report.per_cluster
    assert out.primary_product_key == "p2"
    assert (out.member_count, out.members_kept_in_other_group, out.kept_product_keys) == (2, 1, ["p1"])
    if apply:
        assert _upserted(executed) == {"m_p2": True, "m_p3": False}, "p1 is not written"
        assert report.members_upserted_total == 2
    else:
        assert executed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("groups", [(OTHER_PG, OTHER_PG), (OTHER_PG, None), ("pg_x", "pg_y"), (OTHER_PG, PG)])
async def test_a_cluster_with_fewer_than_two_free_listings_writes_nothing(monkeypatch, groups):
    rows = [_db_row(f"p{i}", group=g) for i, g in enumerate(groups)]
    executed = _install(monkeypatch, rows)
    report = await autogrouper.autogroup_clusters(content_key=CK, apply=True)
    assert executed == [] and report.per_cluster == []
    assert (report.clusters_considered, report.clusters_grouped, report.clusters_left_alone) == (1, 0, 1)
    assert report.members_kept_total == sum(1 for g in groups if g not in (None, PG))


@pytest.mark.asyncio
async def test_rows_already_in_the_group_or_unclaimed_all_join_as_before(monkeypatch):
    t0 = dt.datetime(2026, 4, 1, tzinfo=dt.timezone.utc)
    rows = [_db_row("p1", group=PG, minted=t0), _db_row("p2", group=None, minted=t0 + dt.timedelta(days=1))]
    executed = _install(monkeypatch, rows)
    report = await autogrouper.autogroup_clusters(content_key=CK, apply=True)
    assert _upserted(executed) == {"m_p1": True, "m_p2": False}
    assert report.members_kept_total == 0 and report.per_cluster[0].kept_product_keys == []


def test_the_primary_check_reads_the_chosen_primary_in_the_group():
    sql = " ".join(autogrouper._PRIMARY_IN_GROUP_SQL.split())
    assert sql == ("SELECT 1 FROM product_group_members WHERE product_group_id = :product_group_id"
                   " AND merchant_id = :merchant_id AND platform = :platform"
                   " AND platform_product_id = :platform_product_id")


@pytest.mark.asyncio
async def test_a_landed_primary_commits_and_clears_the_others(monkeypatch):
    rows = [_db_row("p1", stage="published"), _db_row("p2")]
    executed = _install(monkeypatch, rows)
    report = await autogrouper.autogroup_clusters(content_key=CK, apply=True)
    [check] = executed.checks
    assert check["params"] == {"product_group_id": PG, "merchant_id": "m_p1", "platform": "shopify",
                               "platform_product_id": "s_p1"}
    assert executed.ends == ["commit"]
    assert [e for e in executed if "SET is_primary = FALSE" in e["sql"]], "the clear runs"
    [out] = report.per_cluster
    assert out.raced is False and out.primary_flag_writes == 1 and report.clusters_raced == 0


@pytest.mark.asyncio
async def test_a_primary_claimed_elsewhere_rolls_the_cluster_back(monkeypatch):
    rows = [_db_row("p1", stage="published"), _db_row("p2"), _db_row("p3")]
    executed = _install(monkeypatch, rows, primary_landed=False)
    report = await autogrouper.autogroup_clusters(content_key=CK, apply=True)
    assert executed.ends == ["rollback"]
    assert not [e for e in executed if "SET is_primary = FALSE" in e["sql"]], "no clear after a lost primary"
    [out] = report.per_cluster
    assert (out.raced, out.members_upserted, out.primary_flag_writes) == (True, 0, 0)
    assert (report.clusters_raced, report.clusters_grouped, report.members_upserted_total) == (1, 0, 0)
