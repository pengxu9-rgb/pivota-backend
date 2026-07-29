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
    enqueue = _calls(tree, "create_quality_backfill_job")
    assert ingest, "the canonical ingest call disappeared — rewrite this guard"
    assert enqueue, (
        "universal_product_sync no longer enqueues quality scoring after ingest. "
        "Without it every portal-synced merchant is born low_quality-blocked "
        "with nothing scheduled to ever score them."
    )
    kw = {k.arg for c in enqueue for k in c.keywords}
    assert "missing_only" in kw and "requested_by" in kw


def test_enqueue_failure_cannot_fail_the_sync():
    # The enqueue must sit inside its own try/except, same contract as the
    # sibling hook in run_catalog_sync_job: a scoring-enqueue failure must never
    # fail the sync that just succeeded.
    tree = ast.parse(ROUTE.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            body_src = ast.dump(node)
            if "create_quality_backfill_job" in body_src:
                assert node.handlers, "enqueue try-block has no except handler"
                return
    raise AssertionError("create_quality_backfill_job is not wrapped in try/except")
