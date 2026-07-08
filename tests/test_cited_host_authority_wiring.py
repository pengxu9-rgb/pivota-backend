"""Phase 1c — electronics-audio authority hosts wired into the cited-host classifier."""
import services.cited_host_classifier as C
from services.vertical_profiles import get_profile


def test_audio_authority_hosts_are_recognized_review_sites():
    for host in ["rtings.com", "soundguys.com", "head-fi.org", "whathifi.com"]:
        r = C.classify_host(host)
        assert r["type"] == "editorial", host
        assert r.get("subtype") == "review_site", host
        assert r.get("ai_grounding_weight") == "medium", host


def test_profile_is_the_source_of_truth():
    # every electronics_audio authority host classifies as a known review site
    for host in get_profile("electronics").authority_hosts:
        assert C.classify_host(host)["type"] == "editorial", host


def test_existing_higher_weight_not_downgraded_and_beauty_unaffected():
    assert C.classify_host("wirecutter.com").get("ai_grounding_weight") == "high"
    assert C.classify_host("allure.com").get("ai_grounding_weight") == "medium"
