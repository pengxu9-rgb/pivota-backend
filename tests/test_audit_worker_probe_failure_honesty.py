"""Worker honesty: an all-probes-failed run (e.g. probe-auth key missing on the
worker) must NOT finalize as a 'succeeded' empty audit that reads as
'merchant invisible'. Unit-tests the detection helpers."""
from __future__ import annotations

from services.audit_run_worker import (
    _all_per_sku_probes_failed,
    _first_probe_failure_reason,
)

_KEY_ERR = (
    "No PIVOTA-Agent internal API key is configured — set PROMOTIONS_ADMIN_KEY"
)


def _failed(provider):
    return {"provider": provider, "status": "probe_failed", "error": _KEY_ERR,
            "raw_runs": []}


def _ok(provider, n=2):
    return {"provider": provider, "status": "succeeded",
            "raw_runs": [{"query": f"q{i}", "parsed": {"product_visible": False}}
                         for i in range(n)]}


def test_all_failed_no_evidence_is_detected():
    runs = {"sku1": [_failed("gemini"), _failed("chatgpt")]}
    assert _all_per_sku_probes_failed(runs) is True
    assert "PROMOTIONS_ADMIN_KEY" in _first_probe_failure_reason(runs)


def test_partial_success_is_not_flagged():
    # One provider failed but another returned real runs — there IS evidence.
    runs = {"sku1": [_failed("gemini"), _ok("chatgpt")]}
    assert _all_per_sku_probes_failed(runs) is False


def test_all_succeeded_even_if_empty_is_not_flagged():
    # Probes ran and returned runs (product just not cited) — a real result.
    runs = {"sku1": [_ok("gemini"), _ok("chatgpt")]}
    assert _all_per_sku_probes_failed(runs) is False


def test_no_payloads_is_not_flagged():
    assert _all_per_sku_probes_failed({}) is False
    assert _all_per_sku_probes_failed({"sku1": []}) is False


def test_multi_sku_one_has_evidence_is_not_flagged():
    runs = {"sku1": [_failed("gemini")], "sku2": [_ok("gemini")]}
    assert _all_per_sku_probes_failed(runs) is False


def test_multi_sku_all_failed_is_flagged():
    runs = {"sku1": [_failed("gemini")], "sku2": [_failed("chatgpt")]}
    assert _all_per_sku_probes_failed(runs) is True
