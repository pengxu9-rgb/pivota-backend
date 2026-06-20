"""DeepSeek answer-quality verify must run even when the merchant selects
GENERATION providers explicitly.

The explicit/single coverage paths return verify_providers=[] (they bound the
generation providers); the merchant "Models to run" UI always sends an explicit
list, so without a default the verify pass was silently disabled on every merchant
audit ("verify on none"). _resolve_audit_verify_providers defaults to the
verify-supported set in that case, while still respecting a caller override.
"""

from services.agent_center_bd_report_service import _resolve_audit_verify_providers
from services.coverage_profiles import verify_supported_providers


def test_explicit_coverage_defaults_to_verify_supported():
    # explicit/single coverage path: verify_providers == [] -> default to deepseek
    out = _resolve_audit_verify_providers({"verify_providers": []}, None)
    assert out == verify_supported_providers()  # ["deepseek"]
    assert "deepseek" in out


def test_profile_verify_providers_used_when_present():
    out = _resolve_audit_verify_providers({"verify_providers": ["deepseek"]}, None)
    assert out == ["deepseek"]


def test_caller_override_wins():
    out = _resolve_audit_verify_providers({"verify_providers": []}, ["DeepSeek"])
    assert out == ["deepseek"]  # normalized


def test_caller_empty_list_disables_deliberately():
    # an explicit [] from the caller = "no verify" — respected, never defaulted
    assert _resolve_audit_verify_providers({"verify_providers": ["deepseek"]}, []) == []


def test_missing_coverage_key_defaults():
    assert _resolve_audit_verify_providers({}, None) == verify_supported_providers()


# The launch ENDPOINT (routes/audit_runs_routes.py) is the layer that previously
# DEFEATED the default: it resolved verify from coverage.get("verify_providers")
# directly (== [] for the explicit path) and stored that [], so the worker passed
# an explicit [] and the helper respected it ("verify on none" — proven live on a
# merchant audit). The fix routes the launch through the SAME helper with caller
# None, so the explicit path requests deepseek and the cost preview matches.
_EXPLICIT_COVERAGE = {
    "profile": "explicit",
    "providers": ["gemini"],
    "verify_providers": [],
    "explicit_provider_override": True,
}


def test_launch_enables_verify_for_explicit_path(monkeypatch):
    from routes import audit_runs_routes as ar
    monkeypatch.setattr(ar.settings, "deepseek_api_key", "test-key", raising=False)
    runnable, skipped = ar._runnable_verify_providers(
        ar._resolve_audit_verify_providers(_EXPLICIT_COVERAGE, None)
    )
    assert runnable == ["deepseek"]
    assert skipped == []


def test_launch_preflight_skips_verify_when_key_missing(monkeypatch):
    # With the default applied, a missing key is caught at pre-flight: verify is
    # stripped from the billable set and stamped skipped (no bill for work that
    # won't run) — instead of silently never being requested.
    from routes import audit_runs_routes as ar
    monkeypatch.setattr(ar.settings, "deepseek_api_key", "", raising=False)
    runnable, skipped = ar._runnable_verify_providers(
        ar._resolve_audit_verify_providers(_EXPLICIT_COVERAGE, None)
    )
    assert runnable == []
    assert skipped and skipped[0]["reason"] == "missing_deepseek_api_key"
