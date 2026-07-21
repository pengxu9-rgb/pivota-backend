"""Step-5 Lane 2 — keeper policy, proposal building, drift matching (no DB)."""

from __future__ import annotations

import json
from typing import Any, Dict, List

from scripts.step5_lane2_same_url_dedup import (  # noqa: E402
    DEACTIVATE_SEEDS_SQL,
    SUPPRESS_SQL,
    SUPPRESSION_REASON,
    build_metadata,
    build_proposal,
    choose_keeper,
    match_proposal,
)


def _detail(pk: str, **overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "product_key": pk,
        "merchant_id": "external_seed",
        "content_key": "ck_1",
        "platform": "external_seed",
        "canonical_url": "https://brand.example/products/x",
        "source_ref": f"eps_{pk}",
        "pivota_signature_id": f"sig_{pk}",
        "created_at": "2026-06-01",
        "payload_bytes": 100,
        "group_is_primary": False,
    }
    base.update(overrides)
    return base


def _lane2_group(pks: List[str], merchant: str = "external_seed") -> Dict[str, Any]:
    return {
        "merchant_id": merchant,
        "content_key": "ck_1",
        "rows": [{"product_key": pk} for pk in pks],
    }


class TestKeeperPolicy:
    def test_is_primary_beats_signature_and_ordering(self):
        rows = [
            _detail("b"),
            _detail("a", group_is_primary=True, pivota_signature_id=None),
        ]
        assert choose_keeper(rows)["product_key"] == "a"

    def test_signature_beats_lowest_key(self):
        rows = [
            _detail("a", pivota_signature_id=None),
            _detail("b"),
        ]
        assert choose_keeper(rows)["product_key"] == "b"

    def test_all_equal_falls_back_to_lowest_product_key(self):
        rows = [_detail("c"), _detail("a"), _detail("b")]
        assert choose_keeper(rows)["product_key"] == "a"

    def test_url_audit_row_never_wins(self):
        rows = [
            _detail("a", platform="url_audit", group_is_primary=True),
            _detail("z"),
        ]
        assert choose_keeper(rows)["product_key"] == "z"


class TestBuildProposal:
    def test_keeper_excluded_from_losers(self):
        detail = {pk: _detail(pk) for pk in ("a", "b", "c")}
        proposal = build_proposal([_lane2_group(["a", "b", "c"])], detail)
        (g,) = proposal["groups"]
        assert g["keeper"]["product_key"] == "a"
        assert sorted(l["product_key"] for l in g["losers"]) == ["b", "c"]
        assert proposal["summary"] == {
            "groups": 1, "keepers": 1, "losers": 2, "skipped_inconsistent": 0,
        }

    def test_group_with_missing_detail_is_skipped_not_guessed(self):
        detail = {"a": _detail("a")}  # 'b' vanished between queries
        proposal = build_proposal([_lane2_group(["a", "b"])], detail)
        assert proposal["groups"] == []
        assert proposal["summary"]["skipped_inconsistent"] == 1


class TestMatchProposal:
    def _proposal(self, keeper: str, losers: List[str]) -> Dict[str, Any]:
        detail = {pk: _detail(pk) for pk in [keeper, *losers]}
        return build_proposal([_lane2_group([keeper, *losers])], detail)

    def test_unchanged_group_applies(self):
        reviewed = self._proposal("a", ["b"])
        fresh = self._proposal("a", ["b"])
        to_apply, drifted = match_proposal(fresh, reviewed)
        assert len(to_apply) == 1 and drifted == []

    def test_new_member_since_review_drifts(self):
        reviewed = self._proposal("a", ["b"])
        fresh = self._proposal("a", ["b", "c"])  # a new dup row appeared
        to_apply, drifted = match_proposal(fresh, reviewed)
        assert to_apply == [] and drifted == [("external_seed", "ck_1")]

    def test_group_gone_since_review_drifts(self):
        reviewed = self._proposal("a", ["b"])
        fresh = {"groups": []}
        to_apply, drifted = match_proposal(fresh, reviewed)
        assert to_apply == [] and drifted == [("external_seed", "ck_1")]


class TestApplyGuards:
    def test_suppress_sql_guards(self):
        assert "suppression_reason IS NULL" in SUPPRESS_SQL
        # The keeper is excluded in-statement, not just by list construction.
        assert "cp.product_key <> $4" in SUPPRESS_SQL

    def test_seed_deactivation_uses_both_linkages_and_only_active(self):
        assert "attached_product_key" in DEACTIVATE_SEEDS_SQL
        assert "id = ANY" in DEACTIVATE_SEEDS_SQL
        assert "= 'active'" in DEACTIVATE_SEEDS_SQL

    def test_seed_deactivation_never_touches_keeper_backing(self):
        # Seed<->row linkage is many-to-many: a seed can be a loser's
        # source_ref while its attached_product_key points at the KEEPER.
        # The first prod apply orphaned 411 keepers this way; the exclusion
        # must live in the deactivation statement itself.
        assert "attached_product_key <> $3" in DEACTIVATE_SEEDS_SQL
        assert "id IS DISTINCT FROM $4" in DEACTIVATE_SEEDS_SQL

    def test_metadata_traces_run_and_keeper(self):
        detail = {pk: _detail(pk) for pk in ("a", "b")}
        (g,) = build_proposal([_lane2_group(["a", "b"])], detail)["groups"]
        meta = json.loads(build_metadata("20260710T000000Z", g))
        assert meta["run_id"] == "20260710T000000Z"
        assert meta["keeper_product_key"] == "a"
        assert SUPPRESSION_REASON == "step5_same_merchant_same_url_dup"
