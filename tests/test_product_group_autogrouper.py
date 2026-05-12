"""Tests for services/product_group_autogrouper.py — Stage 2b-i.

The autogrouper turns content_key clusters into multi-seller groups.
Correctness affects what agent UI shows as "available from N sellers".
Tests pin:
  - Group ID derivation is deterministic (idempotent re-runs)
  - Primary selection policy: lifecycle > sig_minted_at > product_key
  - Cluster query: scope by content_key / merchant_id / unbounded
  - Apply path: every member upserted, primary flag set correctly,
    lingering primaries cleared (handles primary migration)
  - Dry-run path: never calls execute, still computes outcomes
  - NEVER writes to catalog_products / seed_data / product_payload
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import product_group_autogrouper as autogrouper  # noqa: E402
from services.product_group_autogrouper import (  # noqa: E402
    AutogroupReport,
    ClusterOutcome,
    ClusterRow,
    derive_product_group_id,
    pick_primary,
)


# ---------------------------------------------------------------------------
# derive_product_group_id
# ---------------------------------------------------------------------------


def test_derive_group_id_reuses_content_key_hex_suffix() -> None:
    """Deterministic + debuggable: group_id is ck → pg substitution.
    Operators can grep both tables on the same suffix."""
    ck = "ck_32de31827aded89c8d0339895b6a2786"
    assert derive_product_group_id(ck) == "pg_32de31827aded89c8d0339895b6a2786"


def test_derive_group_id_is_idempotent() -> None:
    """Re-running the autogrouper on the same content_key produces the
    same group_id. Critical — otherwise re-runs would fork groups."""
    ck = "ck_a363cbe4bc721b724168df4282713e6c"
    assert derive_product_group_id(ck) == derive_product_group_id(ck)


def test_derive_group_id_rejects_bad_content_key() -> None:
    """Defensive: a missing-prefix string is a programming bug, not a
    data condition. Raise so the caller sees it immediately."""
    with pytest.raises(ValueError):
        derive_product_group_id("not_a_content_key")
    with pytest.raises(ValueError):
        derive_product_group_id("")
    with pytest.raises(ValueError):
        derive_product_group_id(None)  # type: ignore


# ---------------------------------------------------------------------------
# pick_primary — the policy
# ---------------------------------------------------------------------------


def _row(
    pk: str = "p1",
    merchant: str = "m1",
    platform: str = "shopify",
    source_pid: str = "src1",
    stage: str = None,
    minted: dt.datetime = None,
) -> ClusterRow:
    return ClusterRow(
        product_key=pk,
        merchant_id=merchant,
        platform=platform,
        source_product_id=source_pid,
        pdp_lifecycle_stage=stage,
        pivota_signature_minted_at=minted,
    )


def test_pick_primary_prefers_published_over_validated() -> None:
    """Lifecycle hierarchy: published > validated > candidate > draft >
    NULL. Most-developed row wins as the canonical."""
    rows = [
        _row(pk="p_draft", stage="draft"),
        _row(pk="p_validated", stage="validated"),
        _row(pk="p_published", stage="published"),
    ]
    assert pick_primary(rows).product_key == "p_published"


def test_pick_primary_treats_null_lifecycle_as_lowest() -> None:
    """NULL pdp_lifecycle_stage ranks below any explicit stage.
    Avoids accidentally promoting a fresh-ingested row over a
    curator-validated one."""
    rows = [
        _row(pk="p_null", stage=None),
        _row(pk="p_candidate", stage="candidate"),
    ]
    assert pick_primary(rows).product_key == "p_candidate"


def test_pick_primary_tiebreaks_by_earlier_signature_minted_at() -> None:
    """When lifecycle is tied, earlier sig_minted_at wins — earlier
    means more-stable canonical URL (LLMs may already cite it)."""
    early = dt.datetime(2026, 4, 1, tzinfo=dt.timezone.utc)
    late = dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc)
    rows = [
        _row(pk="p_late", stage="validated", minted=late),
        _row(pk="p_early", stage="validated", minted=early),
    ]
    assert pick_primary(rows).product_key == "p_early"


def test_pick_primary_treats_null_minted_at_as_latest() -> None:
    """NULL minted_at ranks after any non-null timestamp. A row that
    never got a sig minted is less canonical than one that did,
    everything else equal."""
    early = dt.datetime(2026, 4, 1, tzinfo=dt.timezone.utc)
    rows = [
        _row(pk="p_with_sig", stage="validated", minted=early),
        _row(pk="p_no_sig", stage="validated", minted=None),
    ]
    assert pick_primary(rows).product_key == "p_with_sig"


def test_pick_primary_final_tiebreak_is_product_key_lex() -> None:
    """All else equal, lower product_key wins. Deterministic so re-runs
    don't churn primaries when lifecycle/sig_minted_at are identical."""
    rows = [
        _row(pk="p_b", stage="validated"),
        _row(pk="p_a", stage="validated"),
    ]
    assert pick_primary(rows).product_key == "p_a"


def test_pick_primary_raises_on_empty_cluster() -> None:
    with pytest.raises(ValueError):
        pick_primary([])


# ---------------------------------------------------------------------------
# Cluster query construction
# ---------------------------------------------------------------------------


def test_cluster_query_single_content_key_mode() -> None:
    """content_key set: query targets only that one cluster's rows.
    No HAVING filter needed."""
    sql, params = autogrouper._build_cluster_query(
        content_key="ck_x", merchant_id=None, limit=100,
    )
    assert "cp.content_key = :content_key" in sql
    assert "HAVING count" not in sql
    assert params == {"content_key": "ck_x"}


def test_cluster_query_merchant_scope_includes_having_filter() -> None:
    """merchant_id set + no content_key: only clusters where that
    merchant has ≥2 rows. Eligible CTE produces the cluster list."""
    sql, params = autogrouper._build_cluster_query(
        content_key=None, merchant_id="merch_moyu", limit=10,
    )
    assert "HAVING count(*) >= 2" in sql
    assert "AND merchant_id = :merchant_id" in sql
    assert params["merchant_id"] == "merch_moyu"
    assert params["limit_clusters"] == 10


def test_cluster_query_unbounded_mode() -> None:
    """No filter: every cluster with count >= 2."""
    sql, params = autogrouper._build_cluster_query(
        content_key=None, merchant_id=None, limit=50,
    )
    assert "HAVING count(*) >= 2" in sql
    # No merchant scope
    assert "merchant_id = :merchant_id" not in sql
    assert params == {"limit_clusters": 50}


# ---------------------------------------------------------------------------
# _apply_cluster — the write path
# ---------------------------------------------------------------------------


class _FakeTxn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _install_fake_db(monkeypatch) -> List[Dict[str, Any]]:
    executed: List[Dict[str, Any]] = []

    async def fake_execute(sql, params=None):
        executed.append({"sql": str(sql), "params": dict(params or {})})
        return None

    class _DB:
        def execute(self, sql, params=None):
            return fake_execute(sql, params)

        def transaction(self):
            return _FakeTxn()

    monkeypatch.setattr(autogrouper, "database", _DB())
    return executed


@pytest.mark.asyncio
async def test_apply_cluster_upserts_every_member_with_correct_primary(monkeypatch) -> None:
    """Three-row cluster: each row upserted, primary flag set on the
    chosen primary, others get is_primary=False."""
    executed = _install_fake_db(monkeypatch)
    rows = [
        _row(pk="p_a", merchant="merch_a", platform="shopify", source_pid="100", stage="published"),
        _row(pk="p_b", merchant="merch_b", platform="shopify", source_pid="200", stage="validated"),
        _row(pk="p_c", merchant="merch_c", platform="shopify", source_pid="300", stage="draft"),
    ]
    outcome = await autogrouper._apply_cluster(
        content_key="ck_abc", rows=rows,
    )
    assert outcome.member_count == 3
    assert outcome.members_upserted == 3
    assert outcome.primary_product_key == "p_a"  # published wins
    assert outcome.product_group_id == "pg_abc"

    # Find the 3 INSERTs (one per member) plus the clear-other-primaries UPDATE
    member_upserts = [e for e in executed if "INSERT INTO product_group_members" in e["sql"]]
    assert len(member_upserts) == 3
    # The 'published' row gets is_primary=True
    primary_writes = [e for e in member_upserts if e["params"]["is_primary"] is True]
    assert len(primary_writes) == 1
    assert primary_writes[0]["params"]["merchant_id"] == "merch_a"
    # Others get is_primary=False
    non_primary_writes = [e for e in member_upserts if e["params"]["is_primary"] is False]
    assert len(non_primary_writes) == 2


@pytest.mark.asyncio
async def test_apply_cluster_clears_lingering_other_primaries(monkeypatch) -> None:
    """Re-running with a different primary winner must un-flag the old
    primary in the same group. The clear UPDATE handles that."""
    executed = _install_fake_db(monkeypatch)
    rows = [
        _row(pk="p_x", merchant="m_x", platform="shopify", source_pid="x", stage="published"),
    ]
    await autogrouper._apply_cluster(content_key="ck_test", rows=rows)
    clears = [e for e in executed if "UPDATE product_group_members" in e["sql"]
              and "SET is_primary = FALSE" in e["sql"]]
    assert len(clears) == 1
    # Clear is scoped to OTHER members in the group (not the new primary)
    assert "NOT (merchant_id = :primary_merchant_id" in clears[0]["sql"]


@pytest.mark.asyncio
async def test_apply_cluster_never_touches_catalog_or_seed_tables(monkeypatch) -> None:
    """The autogrouper only writes to product_group_members. Defensive
    assertion against accidental drift in future refactors."""
    executed = _install_fake_db(monkeypatch)
    rows = [
        _row(pk="p1", merchant="m1", platform="shopify", source_pid="1"),
        _row(pk="p2", merchant="m2", platform="shopify", source_pid="2"),
    ]
    await autogrouper._apply_cluster(content_key="ck_x", rows=rows)
    sql_joined = "\n".join(e["sql"] for e in executed)
    assert "catalog_products" not in sql_joined
    assert "external_product_seeds" not in sql_joined
    assert "seed_data" not in sql_joined
    assert "product_payload" not in sql_joined


# ---------------------------------------------------------------------------
# autogroup_clusters — driver
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_autogroup_dry_run_does_not_execute(monkeypatch) -> None:
    """Default invocation (apply=False) never writes. Operators can
    eyeball outcomes before flipping --apply."""
    executed: List[Dict[str, Any]] = []

    async def fake_fetch_all(_sql, _params):
        return [
            {"content_key": "ck_a", "product_key": "p1",
             "merchant_id": "m1", "platform": "shopify", "source_product_id": "1",
             "pdp_lifecycle_stage": "validated",
             "pivota_signature_minted_at": dt.datetime(2026, 4, 1, tzinfo=dt.timezone.utc)},
            {"content_key": "ck_a", "product_key": "p2",
             "merchant_id": "m2", "platform": "shopify", "source_product_id": "2",
             "pdp_lifecycle_stage": "validated",
             "pivota_signature_minted_at": dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc)},
        ]

    async def fake_execute(*args, **kwargs):
        executed.append(args)

    monkeypatch.setattr(autogrouper.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(autogrouper.database, "execute", fake_execute)

    report = await autogrouper.autogroup_clusters(apply=False)
    assert executed == []
    assert report.clusters_considered == 1
    assert report.clusters_grouped == 1
    assert report.members_upserted_total == 0
    assert report.per_cluster[0].dry_run is True
    # Primary picked deterministically: same lifecycle, earlier minted_at wins
    assert report.per_cluster[0].primary_product_key == "p1"


@pytest.mark.asyncio
async def test_autogroup_skips_singleton_clusters(monkeypatch) -> None:
    """Defensive: if the fetch returns only one row for a content_key
    (CTE bug, race, whatever), skip without grouping. We never want a
    single-member 'group' polluting product_group_members."""
    executed: List[Dict[str, Any]] = []

    async def fake_fetch_all(_sql, _params):
        return [
            {"content_key": "ck_only_one", "product_key": "p1",
             "merchant_id": "m1", "platform": "shopify", "source_product_id": "1",
             "pdp_lifecycle_stage": None, "pivota_signature_minted_at": None},
        ]

    async def fake_execute(*args, **kwargs):
        executed.append(args)

    monkeypatch.setattr(autogrouper.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(autogrouper.database, "execute", fake_execute)

    report = await autogrouper.autogroup_clusters(apply=True)
    assert report.clusters_grouped == 0
    assert executed == []


@pytest.mark.asyncio
async def test_autogroup_real_world_moyu_cluster_shape(monkeypatch) -> None:
    """Anchor case from production data 2026-05-12: MOYU has 26
    'Foundation Brush' rows under one merchant, same shopify
    platform, different source_product_ids. Expected: one group with
    26 members, one primary."""
    rows_from_db = [
        {"content_key": "ck_32de31827aded89c8d0339895b6a2786",
         "product_key": f"p{i}",
         "merchant_id": "merch_efbc46b4619cfbdf",
         "platform": "shopify",
         "source_product_id": f"10064565{i:05d}",
         "pdp_lifecycle_stage": None,
         "pivota_signature_minted_at": None}
        for i in range(26)
    ]
    executed: List[Dict[str, Any]] = []

    async def fake_fetch_all(_sql, _params):
        return rows_from_db

    async def fake_execute(sql, params=None):
        executed.append({"sql": str(sql), "params": dict(params or {})})

    monkeypatch.setattr(autogrouper.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(autogrouper.database, "execute", fake_execute)
    monkeypatch.setattr(
        autogrouper.database, "transaction", lambda: _FakeTxn(),
    )

    report = await autogrouper.autogroup_clusters(
        merchant_id="merch_efbc46b4619cfbdf", apply=True,
    )
    assert report.clusters_grouped == 1
    outcome = report.per_cluster[0]
    assert outcome.content_key == "ck_32de31827aded89c8d0339895b6a2786"
    assert outcome.product_group_id == "pg_32de31827aded89c8d0339895b6a2786"
    assert outcome.member_count == 26
    assert outcome.members_upserted == 26
    # 26 INSERTs + 1 clear-other-primaries UPDATE = 27 writes
    assert len(executed) == 27
    # Exactly one is_primary=True among the inserts
    primary_count = sum(
        1 for e in executed
        if "INSERT INTO product_group_members" in e["sql"]
        and e["params"]["is_primary"] is True
    )
    assert primary_count == 1
