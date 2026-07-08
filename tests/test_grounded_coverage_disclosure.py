"""Honesty gate (Principle 4): non-INCI-grounded categories disclose it."""
import services.agent_center_bd_report_service as R
from services.vertical_profiles import get_profile


def test_disclosure_present_only_off_beauty():
    assert get_profile("beauty").grounded_coverage_disclosure is None          # INCI-grounded
    assert "unavailable" in get_profile("electronics").grounded_coverage_disclosure
    assert "unavailable" in get_profile("other").grounded_coverage_disclosure   # generic


def test_merchant_profile_carries_disclosure():
    elec = R._merchant_profile_from_reports([{"product_type": "Headphones", "title": "Purra Swim"}])
    assert elec.grounded_coverage_disclosure and "unavailable" in elec.grounded_coverage_disclosure
    beauty = R._merchant_profile_from_reports([{"product_type": "Supplements", "title": "Collagen"}])
    assert beauty.grounded_coverage_disclosure is None                          # -> report omits the key


def test_report_emission_shape():
    # The report emits the key ONLY when the merchant profile has a disclosure,
    # so a beauty report is byte-identical (no new key). Mirror the inline logic.
    def emit(profile):
        return (
            {"grounded_coverage_disclosure": profile.grounded_coverage_disclosure}
            if getattr(profile, "grounded_coverage_disclosure", None)
            else {}
        )
    assert emit(get_profile("beauty")) == {}
    assert emit(get_profile("electronics")) == {
        "grounded_coverage_disclosure": "grounded-evidence dimensions are unavailable for this category"
    }
