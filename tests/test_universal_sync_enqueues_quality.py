"""Every portal sync must enqueue quality scoring — the drain tick cannot drain nothing.

The 30s `quality_backfill_drain_tick` only processes jobs someone enqueued.
`run_catalog_sync_job` has carried the enqueue hook since it existed, but
`universal_product_sync` — the path every portal sync button actually goes
through (wix/shopify/woocommerce/bigcommerce routes → sync_products → here) —
never got it. A portal-synced merchant's catalog was therefore born with ZERO
quality snapshots: every content_key stamped `low_quality` ("no quality
snapshot found"), serving_eligible false, and nothing scheduled to change it.

Measured on the 2026-07-29 Wix pilot (merch_e68c20b0189746d0): 20/20 rows
ingested, identity minted, IPS classified — `product_quality_backfill_jobs`
contained NOTHING for the merchant.

Structural guard, same pattern as test_mirror_source_backed_quality_signals:
driving the real route needs live store credentials and a database, so the
call node is pinned instead. If this becomes awkward, replace it with an
integration test — do not delete it, or the omission goes invisible again.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROUTE = Path(__file__).resolve().parents[1] / "routes" / "universal_product_sync.py"


def _calls(tree, name):
    return [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and (
            (isinstance(n.func, ast.Name) and n.func.id == name)
            or (isinstance(n.func, ast.Attribute) and n.func.attr == name)
        )
    ]


def test_universal_sync_enqueues_a_quality_backfill_job():
    tree = ast.parse(ROUTE.read_text())
    ingest = _calls(tree, "ingest_standard_products")
    enqueue = _calls(tree, "enqueue_quality_backfill_if_needed")
    assert ingest, "the canonical ingest call disappeared — rewrite this guard"
    assert enqueue, (
        "universal_product_sync no longer enqueues quality scoring after ingest. "
        "Without it every portal-synced merchant is born low_quality-blocked "
        "with nothing scheduled to ever score them."
    )
    kw = {k.arg for c in enqueue for k in c.keywords}
    assert "missing_only" in kw and "requested_by" in kw
    # The enqueue goes through the cooldown-aware helper and tells it whether a
    # person is waiting. Without `unattended` every request is treated as
    # attended and the gateway's auto-sync churn (20 snapshots per sync, up to
    # 16 syncs a day on one Wix store, measured 2026-09-02) comes straight back.
    assert "unattended" in kw, "forward request.unattended to the enqueue helper"


def test_the_internal_platform_sync_api_marks_its_requests_unattended():
    """routes/platform_products_sync_api is the gateway's scheduled auto-sync
    door (X-ADMIN-KEY, server-to-server, force_refresh=True). It must say so, or
    the cooldown never applies to the one caller it exists for."""
    api = Path(__file__).resolve().parents[1] / "routes" / "platform_products_sync_api.py"
    tree = ast.parse(api.read_text())
    reqs = _calls(tree, "UniversalSyncRequest")
    assert reqs, "the internal API no longer builds a UniversalSyncRequest — rewrite this guard"
    for call in reqs:
        kws = {k.arg: k.value for k in call.keywords}
        assert "unattended" in kws, "unattended is not passed"
        node = kws["unattended"]
        assert isinstance(node, ast.Constant) and node.value is True, (
            "the internal auto-sync API must construct its request with unattended=True"
        )


def test_enqueue_honours_the_caller_s_force_refresh():
    """Enqueuing is not enough — the job must be allowed to actually rescore.

    `force_refresh` was hardcoded False here while
    `UniversalSyncRequest.force_refresh` existed and was silently ignored, and
    both callers that set it (routes/wix_sync.py,
    routes/platform_products_sync_api.py) were downgraded on the way through.

    That made an entire CLASS of fix undeliverable. The backfill skips any row
    that already has a snapshot, so when a merchant's products newly GAIN a
    field, a re-sync writes the field, enqueues the job, skips every row, keeps
    the old score, and returns success. Concretely: Wix merchants stuck at 66.7
    for a missing category would have stayed at 66.7 after category mapping
    landed, with every signal reporting success.
    """
    tree = ast.parse(ROUTE.read_text())
    calls = _calls(tree, "enqueue_quality_backfill_if_needed")
    assert calls, "the enqueue call disappeared — rewrite this guard"
    for call in calls:
        kws = {k.arg: k.value for k in call.keywords}
        assert "force_refresh" in kws, "force_refresh is no longer passed at all"
        node = kws["force_refresh"]
        assert not (isinstance(node, ast.Constant) and node.value is False), (
            "force_refresh is hardcoded False again. The caller's force_refresh is "
            "then silently discarded and no already-scored row can ever be rescored "
            "— a re-sync that adds a field reports success and changes nothing."
        )
        assert "force_refresh" in ast.dump(node), (
            "force_refresh is not derived from the request; forward request.force_refresh."
        )


def test_the_skip_predicate_is_what_makes_force_refresh_load_bearing():
    """Pin the behaviour the test above exists to protect.

    Mirrors services/product_quality_backfill_service.py:102. If this predicate
    changes, revisit the reasoning above rather than deleting the guard.
    """
    from services.product_quality_backfill_service import quality_row_has_scores

    already_scored = {"content_quality_score": 66.7}
    assert quality_row_has_scores(already_scored) is True

    def row_skipped(missing_only: bool, force_refresh: bool) -> bool:
        return bool(
            missing_only and not force_refresh and quality_row_has_scores(already_scored)
        )

    assert row_skipped(True, False) is True, "a scored row is skipped without force_refresh"
    assert row_skipped(True, True) is False, "force_refresh is what unskips it"


def test_enqueue_failure_cannot_fail_the_sync():
    # The enqueue must sit inside its own try/except, same contract as the
    # sibling hook in run_catalog_sync_job: a scoring-enqueue failure must never
    # fail the sync that just succeeded.
    tree = ast.parse(ROUTE.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            body_src = ast.dump(node)
            if "enqueue_quality_backfill_if_needed" in body_src:
                assert node.handlers, "enqueue try-block has no except handler"
                return
    raise AssertionError("create_quality_backfill_job is not wrapped in try/except")
