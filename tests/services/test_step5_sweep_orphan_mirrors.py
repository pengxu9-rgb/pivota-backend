"""Step-5 orphan-mirror sweep — guard-rail and summary tests (no DB).

The sweep's selection is imported from step5_working_set (single source of
truth); these tests pin the safety properties of the UPDATE and the dry-run
summary shape.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from scripts.step5_sweep_orphan_mirrors import (  # noqa: E402
    SUPPRESSION_REASON,
    UPDATE_SQL,
    build_metadata,
    summarize,
)
from scripts.step5_working_set import ORPHAN_MIRRORS_SQL  # noqa: E402


def _row(**overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "product_key": "prod::external_seed::external_seed::1",
        "content_key": "ck_1",
        "source_ref": "seed_1",
        "canonical_url": "https://brand.example/products/x",
        "pivota_signature_id": None,
        "seed_status": "inactive",
    }
    base.update(overrides)
    return base


class TestUpdateGuards:
    def test_idempotency_guard(self):
        assert "suppression_reason IS NULL" in UPDATE_SQL

    def test_platform_scoped(self):
        assert "platform = 'external_seed'" in UPDATE_SQL

    def test_recheck_orphan_condition_against_reactivated_seed(self):
        # The UPDATE must re-verify no active seed backs the row, so a seed
        # reactivated between select and apply is left untouched.
        assert "NOT EXISTS" in UPDATE_SQL
        assert "= 'active'" in UPDATE_SQL

    def test_selection_excludes_already_suppressed(self):
        assert "suppression_reason IS NULL" in ORPHAN_MIRRORS_SQL

    def test_bidirectional_seed_linkage(self):
        # Enrichment-door rows have NO source_ref; their seed is linked via
        # external_product_seeds.attached_product_key. Both the selection and
        # the apply re-check must honor both directions, or live products get
        # tombstoned (the first prod dry-run surfaced 482 such false orphans).
        for sql in (ORPHAN_MIRRORS_SQL, UPDATE_SQL):
            assert "source_ref" in sql
            assert "attached_product_key" in sql


class TestSummarize:
    def test_counts_by_seed_status_and_signature(self):
        rows = [
            _row(seed_status="inactive"),
            _row(seed_status="inactive", product_key="p2"),
            _row(seed_status="missing", product_key="p3",
                 pivota_signature_id="sig_1"),
        ]
        s = summarize(rows)
        assert s["rows"] == 3
        assert s["by_seed_status"] == {"inactive": 2, "missing": 1}
        assert s["with_signature"] == 1
        assert s["signature_product_keys"] == ["p3"]

    def test_empty(self):
        s = summarize([])
        assert s == {
            "rows": 0,
            "by_seed_status": {},
            "with_signature": 0,
            "signature_product_keys": [],
        }


class TestMetadata:
    def test_metadata_carries_run_id_for_revert(self):
        meta = json.loads(build_metadata("20260710T000000Z"))
        assert meta["run_id"] == "20260710T000000Z"
        assert meta["script"] == "step5_sweep_orphan_mirrors"
        assert SUPPRESSION_REASON == "step5_orphan_seed_mirror"
