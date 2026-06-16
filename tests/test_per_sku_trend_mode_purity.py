"""Step 3 mode-purity: a per_sku trend must compare only against prior per_sku
runs — legacy runs write different score semantics into the same columns, so
mixing them renders a misleading delta."""

from __future__ import annotations

import json

from db.merchant_audit_runs import _run_audit_mode
from services.agent_center_bd_report_service import (
    _legacy_prior_runs,
    _per_sku_prior_runs,
)


def test_legacy_prior_runs_excludes_per_sku():
    runs = [
        {"run_id": "legacy1", "audit_mode": None},  # legacy = untagged → kept
        {"run_id": "legacy2", "audit_mode": "legacy"},  # kept
        {"run_id": "psku", "audit_mode": "per_sku"},  # excluded
        "not-a-dict",
    ]
    assert [r["run_id"] for r in _legacy_prior_runs(runs)] == ["legacy1", "legacy2"]
    assert _legacy_prior_runs(None) == []


def test_filters_to_per_sku_only():
    runs = [
        {"run_id": "a", "audit_mode": "per_sku", "visibility_score_avg": 40},
        {"run_id": "b", "audit_mode": "legacy", "visibility_score_avg": 60},
        {"run_id": "c", "audit_mode": None, "visibility_score_avg": 50},  # unknown → excluded
        {"run_id": "d"},  # no audit_mode → excluded
        "not-a-dict",  # robustness
    ]
    out = _per_sku_prior_runs(runs)
    assert [r["run_id"] for r in out] == ["a"]


def test_empty_and_none():
    assert _per_sku_prior_runs(None) == []
    assert _per_sku_prior_runs([]) == []


def test_run_audit_mode_decodes_jsonb_string_prod_path():
    payload = {"launch": {"audit_mode": "per_sku", "custom_prompts": []}}
    # asyncpg returns JSONB as a JSON STRING, not a dict — must still resolve
    # (reading .get() on the raw string would silently empty the whole trend).
    assert _run_audit_mode(json.dumps(payload)) == "per_sku"
    # dict path (in-memory test fake)
    assert _run_audit_mode(payload) == "per_sku"


def test_run_audit_mode_none_legacy_and_garbage():
    assert _run_audit_mode(None) is None  # legacy run (no launch options)
    assert _run_audit_mode(json.dumps({"launch": {}})) is None  # no audit_mode
    assert _run_audit_mode("not-json") is None  # unparseable → not a dict
    assert _run_audit_mode({"launch": "weird"}) is None  # launch not a dict
