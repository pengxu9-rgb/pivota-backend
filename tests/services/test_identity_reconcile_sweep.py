"""ADR-010 D-2 Phase B — sweep allowlist, alerting, review routing (no DB)."""

from __future__ import annotations

import json
import os

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")

from services.identity_reconcile_sweep import (  # noqa: E402
    AUTO_APPROVE_STRATEGIES,
    APPROVE_ALLOWLIST_SQL,
    REVIEW_CANDIDATES_SQL,
    detect_gauge_rise,
    identity_reconcile_sweep_enabled,
    review_task_row,
)


class TestAllowlist:
    def test_only_the_step5_proven_mechanical_strategies(self):
        # Widening this is a reviewed decision, not a config change. If this
        # test surprises you, read the phase plan §4 before touching it.
        assert AUTO_APPROVE_STRATEGIES == ("same_url_dup", "junk_url")

    def test_approve_sql_scopes_to_proposed_and_allowlist(self):
        assert "status = 'proposed'" in APPROVE_ALLOWLIST_SQL
        assert "strategy = ANY($1::text[])" in APPROVE_ALLOWLIST_SQL
        assert "sweep_auto_allowlist" in APPROVE_ALLOWLIST_SQL

    def test_review_candidates_exclude_the_allowlist(self):
        assert "NOT (strategy = ANY($1::text[]))" in REVIEW_CANDIDATES_SQL
        assert "campaign_clone_ambiguous" in REVIEW_CANDIDATES_SQL


class TestFlag:
    def test_default_off(self, monkeypatch):
        monkeypatch.delenv("ENABLE_IDENTITY_RECONCILE_SWEEP", raising=False)
        assert identity_reconcile_sweep_enabled() is False

    def test_on(self, monkeypatch):
        monkeypatch.setenv("ENABLE_IDENTITY_RECONCILE_SWEEP", "1")
        assert identity_reconcile_sweep_enabled() is True


class TestGaugeAlert:
    def test_no_previous_sweep_never_alerts(self):
        assert detect_gauge_rise(None, {"same_merchant_dup_keys": 999}) == []

    def test_fall_or_flat_is_quiet(self):
        prev = {"gauges": {"same_merchant_dup_keys": 280,
                           "cross_merchant_shared_keys": 10}}
        assert detect_gauge_rise(prev, {"same_merchant_dup_keys": 250,
                                        "cross_merchant_shared_keys": 10}) == []

    def test_rise_alerts_with_before_and_after(self):
        prev = {"gauges": {"same_merchant_dup_keys": 280}}
        alerts = detect_gauge_rise(prev, {"same_merchant_dup_keys": 300})
        assert alerts == ["same_merchant_dup_keys: 280 -> 300"]

    def test_new_gauge_name_does_not_false_alert(self):
        prev = {"gauges": {}}
        assert detect_gauge_rise(prev, {"brand_new_gauge": 5}) == []


class TestReviewTaskRow:
    def _proposal(self) -> dict:
        return {
            "proposal_id": "irp_abc123",
            "kind": "suppress_dup",
            "strategy": "campaign_clone",
            "content_key": "ck_1",
            "subject_product_keys": ["a", "b"],
            "keeper_product_key": "a",
            "confidence": 0.9,
            "evidence": {"rule": "all_campaign_marked"},
        }

    def test_task_id_is_deterministic_per_proposal(self):
        t1, _, _, _ = review_task_row(self._proposal())
        t2, _, _, _ = review_task_row(self._proposal())
        assert t1 == t2 == "pdptask_ir_irp_abc123"

    def test_checklist_carries_the_decision_material(self):
        _, pdp_id, checklist, labels = review_task_row(self._proposal())
        payload = json.loads(checklist)
        assert pdp_id == "a"
        assert payload["proposal_id"] == "irp_abc123"
        assert payload["subject_product_keys"] == ["a", "b"]
        assert payload["evidence"] == {"rule": "all_campaign_marked"}
        assert "identity_reconcile_sweep" in json.loads(labels)

    def test_label_only_proposal_uses_first_subject_as_pdp_id(self):
        p = self._proposal()
        p["keeper_product_key"] = None
        p["kind"] = "label_only"
        _, pdp_id, _, _ = review_task_row(p)
        assert pdp_id == "a"
