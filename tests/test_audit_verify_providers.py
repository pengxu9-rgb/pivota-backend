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
