"""Phase 1c — electronics-audio authority hosts wired into the cited-host classifier."""
import services.cited_host_classifier as C
from services.vertical_profiles import get_profile


def test_audio_authority_hosts_are_recognized_review_sites():
    for host in ["rtings.com", "soundguys.com", "whathifi.com"]:
        r = C.classify_host(host)
        assert r["type"] == "editorial", host
        assert r.get("subtype") == "review_site", host
        assert r.get("ai_grounding_weight") == "medium", host
    # P3-8: head-fi.org is registry-classified for what it actually is — a
    # forum, not an editorial review site (the profile still lists it as an
    # authority host; the grounding weight registration is type-agnostic).
    hf = C.classify_host("head-fi.org")
    assert hf["type"] == "community"
    assert hf.get("subtype") == "forum"
    assert hf.get("ai_grounding_weight") == "medium"


def test_profile_is_the_source_of_truth():
    # every electronics_audio authority host classifies as a KNOWN host — an
    # editorial review site, or (per the registry's finer knowledge, P3-8) a
    # community forum like head-fi.org. Never unclassified.
    for host in get_profile("electronics").authority_hosts:
        assert C.classify_host(host)["type"] in {"editorial", "community"}, host


def test_existing_higher_weight_not_downgraded_and_beauty_unaffected():
    assert C.classify_host("wirecutter.com").get("ai_grounding_weight") == "high"
    assert C.classify_host("allure.com").get("ai_grounding_weight") == "medium"


# --- Phase 1c retailer half: profile retailer_tokens wired into classify_host ---

def test_profile_retailer_hosts_classify_as_retailer():
    # bestbuy/newegg/crutchfield are electronics profile retailer_tokens and
    # absent from the BD registry — before this wiring they came back
    # unclassified/tier:null (and bestbuy.com got mis-flagged as a competitor
    # host on the Mojawa pilot).
    for host in ["bestbuy.com", "newegg.com", "crutchfield.com"]:
        r = C.classify_host(host)
        assert r["type"] == "retailer", host
        assert r.get("subtype") == "vertical_retailer", host
        assert r.get("tier") is None, host  # tier is editorial-only
        assert r.get("expected_outreach_cycle_weeks") == [12, 26], host


def test_retailer_token_matches_subdomain_and_cctld():
    assert C.classify_host("shop.bestbuy.com")["type"] == "retailer"
    assert C.classify_host("bestbuy.com.au")["type"] == "retailer"


def test_registry_and_editorial_defaults_still_win_over_token():
    # walmart.com has a real registry entry; rtings.com is an editorial
    # default — the token fallback must not shadow either path.
    assert C.classify_host("walmart.com")["type"] in {"retailer", "marketplace"}
    assert C.classify_host("rtings.com")["type"] == "editorial"


def test_unknown_hosts_stay_unclassified():
    # Negative control for the retailer-token rule above: a host carrying no
    # retail token must fall all the way through to `unclassified`, in the bare
    # and the cc-TLD form alike. The host is synthetic on purpose — a real
    # retailer would be typed the moment it landed in the registry or in an
    # ELECTRONICS profile retailer_token, and this assert would die for reasons
    # that have nothing to do with the rule it is guarding.
    for host in ["unknown-fixture-host.example", "unknown-fixture-host.com.au"]:
        r = C.classify_host(host)
        assert r["type"] == "unclassified", host
        assert r["confidence"] == "fallback", host  # pins the fallback path


def test_is_profile_retailer_name():
    assert C.is_profile_retailer_name("Best Buy")
    assert C.is_profile_retailer_name("Walmart (Refurbished)") is False  # whole name, not alias
    assert C.is_profile_retailer_name("walmart")
    assert C.is_profile_retailer_name("Shokz") is False
    assert C.is_profile_retailer_name("") is False


def test_run_competitor_aliases_drop_retailer_names():
    from services.agent_center_bd_report_service import _run_competitor_aliases

    aliases = _run_competitor_aliases(
        ["Best Buy", "Walmart (Refurbished)", "NTUC FairPrice", "Shokz OpenRun"],
        frozenset(),
    )
    joined = " ".join(sorted(aliases))
    assert "bestbuy" not in aliases
    assert "walmart" not in aliases
    assert not any(a.startswith("walmart") and a == "walmart" for a in aliases)
    assert any("shokz" in a for a in aliases), joined


def test_source_roles_mark_merchant_own_domain():
    from services.sku_opportunity import _source_roles_for_runs

    runs = [{
        "grounding_sources": [
            {"uri": "https://mojawa.com/products/purra-run", "title": "mojawa.com"},
            {"uri": "https://bestbuy.com/site/mojawa", "title": "bestbuy.com"},
        ],
    }]
    sku_ctx = {"canonical_url": "https://mojawa.com/products/purra-run"}
    rows = {r["host"]: r for r in _source_roles_for_runs(runs, "electronics", sku_ctx)}
    assert rows["mojawa.com"]["role"] == "own_site"
    assert rows["mojawa.com"].get("first_party") is True
    assert rows["bestbuy.com"]["role"] == "retailer"
    # legacy call shape (no sku_ctx) keeps the old behavior
    legacy = {r["host"]: r for r in _source_roles_for_runs(runs, "electronics")}
    assert legacy["mojawa.com"]["role"] == "unclassified"


# --- P3-8: verified pitch recipients for the pilot authority hosts ---

def test_pilot_authority_hosts_have_pitch_recipients():
    # Registry data verified against each site's own pages (2026-07-11). A
    # removed recipient silently regresses every win-plan target back to
    # target_only — the dead-end state the operator review flagged.
    email_hosts = {"soundguys.com": "pr@soundguys.com", "runnersworld.com": "RWgear@hearst.com"}
    for host, email in email_hosts.items():
        rec = C.classify_host(host).get("pitch_recipient") or {}
        assert rec.get("email") == email, host
    for host in ["rtings.com", "techradar.com", "tomsguide.com", "theverge.com",
                 "cnet.com", "wirecutter.com", "hwahae.com", "audiosciencereview.com"]:
        rec = C.classify_host(host).get("pitch_recipient") or {}
        assert rec.get("submission_url"), host


def test_submission_only_host_renders_paste_draft_for_win_plan():
    from services.audit_playbook_engine import build_pitch_draft_for_host

    rtings = C.classify_host("rtings.com", merchant_category="electronics")
    # ai-readiness surface (default): email-only contract preserved — no draft.
    assert build_pitch_draft_for_host(rtings, merchant_name="Mojawa") is None
    # win-plan surface: paste-ready submission_form draft.
    draft = build_pitch_draft_for_host(
        rtings, merchant_name="Mojawa", merchant_category="electronics",
        example_query="best bone conduction headphones",
        allow_submission_channel=True,
    )
    assert draft is not None
    assert draft["channel"] == "submission_form"
    assert draft["recipient_email"] is None
    assert draft["submission_url"] == "https://www.rtings.com/tv/suggestions"
    assert "Mojawa" in draft["body"]


# --- Electronics/sport-audio pilot batch 2: hosts left `unclassified` in the
#     Mojawa deposit run (69782ea5) that the registry now classifies. ---

def test_electronics_editorial_review_hosts_classified():
    # Tier-1 / niche tech-review sites cited in the electronics pilot but absent
    # from the registry until now — they came back type="unclassified" so their
    # citations counted toward neither findability nor endorsement.
    for host in [
        "pcmag.com", "headphonesaddict.com", "headfonia.com",
        "techgearlab.com", "techhive.com",
    ]:
        r = C.classify_host(host)
        assert r["type"] == "editorial", host
        assert r.get("subtype") == "review_site", host


def test_sport_review_hosts_classified_as_editorial_endorsement():
    # A bone-conduction / sport-audio brand (Mojawa) is recommended in running /
    # cycling / tri / swim gear roundups; those niche sport-review hosts must
    # classify as editorial so the recommendation reads as an endorsement, not
    # an unclassified "neither" signal.
    from services.cited_host_classifier import (
        merchant_relative_role, is_endorsement_role,
    )
    for host in [
        "believeintherun.com", "bikeradar.com", "cyclingweekly.com", "road.cc",
        "220triathlon.com", "swimswam.com", "mensfitness.co.uk",
        "velo.outsideonline.com", "averagejoecyclist.com",
    ]:
        r = C.classify_host(host)
        assert r["type"] == "editorial", host
        role = merchant_relative_role(r["type"], first_party=False, is_competitor=False)
        assert is_endorsement_role(role), host


def test_officedepot_classifies_as_retailer():
    r = C.classify_host("officedepot.com")
    assert r["type"] == "retailer", r


def test_competitor_brand_storefronts_flag_is_competitor():
    # Brand-owned competitor storefronts (Shokz/Soundcore/Underwater Audio/
    # Voistek) now carry registry type="brand", so the deposit path's FIRST pass
    # (`_host_is_competitor`) flags them — no longer dependent on the engine
    # happening to NAME the brand as a competitor in that run.
    from services.agent_center_bd_report_service import (
        _host_is_competitor, _classify_authority_host, _citation_role,
    )
    from services.cited_host_classifier import ROLE_COMPETITOR
    for host in ["shokz.com", "soundcore.com", "underwateraudio.com", "voistek.com"]:
        raw = (C.classify_host(host).get("type") or "").lower()
        assert raw == "brand", host
        assert _host_is_competitor(raw, first_party=False) is True, host
        # And the folded citation role is `competitor` (counts toward neither
        # findability nor endorsement — a rival's store is not the merchant's).
        role = _citation_role(_classify_authority_host(host), False, True)
        assert role == ROLE_COMPETITOR, host


def test_competitor_brand_not_flagged_when_first_party():
    # The merchant auditing its OWN brand store is never flagged a competitor:
    # `_host_is_competitor` short-circuits on first_party. Guards against a Shokz
    # audit tagging shokz.com as its own competitor.
    from services.agent_center_bd_report_service import _host_is_competitor
    raw = (C.classify_host("shokz.com").get("type") or "").lower()
    assert _host_is_competitor(raw, first_party=True) is False


def test_registry_additions_present_with_valid_shape():
    # Sanity that the hand-formatted registry still parses after the batch-2
    # additions and the new hosts are present with a documented type. (The
    # insertions-only guarantee itself is enforced by the git numstat, not here.)
    import json
    from pathlib import Path
    doc = json.loads(
        (Path(__file__).resolve().parent.parent / "data" / "cited_host_registry.json").read_text()
    )
    hosts = doc["hosts"]
    for h in ["shokz.com", "soundcore.com", "pcmag.com", "officedepot.com", "swimswam.com"]:
        assert h in hosts, h
        assert hosts[h]["type"] in {
            "editorial", "retailer", "marketplace", "video", "community",
            "forum", "social", "brand", "cdn", "unclassified",
        }, h
