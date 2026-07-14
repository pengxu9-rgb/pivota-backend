"""Fix 4 — per-SKU win-plan builder.

Exercises the core synthesis on a fixture mirroring the real Aruen
"best collagen cream" shape: a losing category query whose grounded answer is
built from independent editorial hosts the merchant isn't cited in. Asserts the
uri-join recovers the per-query targets, the Fix-2 endorsement filter excludes
findability, the outreach state degrades honestly off the REAL BD registry, and
the guardrails hold (no fabrication, no inflation).
"""

from __future__ import annotations

from typing import Any, Dict, List

from services.win_plan_builder import (
    OUTREACH_DRAFT_READY,
    OUTREACH_SUBMISSION_ONLY,
    OUTREACH_TARGET_ONLY,
    build_win_plan,
)


def _host_row(host: str, role: str, uris: List[str]) -> Dict[str, Any]:
    return {"host": host, "citation_role": role, "evidence_urls": list(uris)}


def _src(uri: str) -> Dict[str, Any]:
    return {"uri": uri, "title": uri}


# --- a realistic per-SKU report + authority_map keyed by the shared raw uri ----

def _aruen_fixture() -> Dict[str, Any]:
    per_sku_reports = [
        {
            "sku_key": "collagen",
            "sku_title": "Tofu Collagen Jelly Cream",
            "failing_prompts": [
                {
                    "query": "best collagen cream",
                    "axis": "category",
                    "grounding_sources": [
                        _src("vx://forbes-1"),
                        _src("vx://byrdie-1"),
                        _src("vx://gh-1"),
                    ],
                    "competitors_named": ["Vital Proteins", "Ancient Nutrition"],
                },
                {
                    # Loses, but AI grounded it only in the merchant's own / retail
                    # listings — no independent target to name (no inflation).
                    "query": "best collagen for sleep",
                    "axis": "category",
                    "grounding_sources": [_src("vx://aruen-1"), _src("vx://ebay-1")],
                    "competitors_named": [],
                },
                {
                    # Branded axis — not a category recommendation to win; excluded.
                    "query": "Aruen collagen review",
                    "axis": "branded",
                    "grounding_sources": [_src("vx://forbes-1")],
                    "competitors_named": [],
                },
            ],
        },
        {
            # Only a branded failing prompt -> contributes no win-plan.
            "sku_key": "serum",
            "sku_title": "Vitamin C Serum",
            "failing_prompts": [
                {
                    "query": "Aruen serum",
                    "axis": "branded",
                    "grounding_sources": [_src("vx://forbes-1")],
                    "competitors_named": [],
                },
            ],
        },
    ]
    authority_map = {
        "skus": [
            {
                "sku_key": "collagen",
                "authority_hosts": [
                    _host_row("forbes.com", "editorial_review", ["vx://forbes-1"]),
                    _host_row("byrdie.com", "editorial_review", ["vx://byrdie-1"]),
                    _host_row("goodhousekeeping.com", "editorial_review", ["vx://gh-1"]),
                    _host_row("aruen.us", "own_domain", ["vx://aruen-1"]),
                    _host_row("ebay.com", "marketplace_self_listing", ["vx://ebay-1"]),
                ],
            },
            {"sku_key": "serum", "authority_hosts": []},
        ]
    }
    return {"per_sku_reports": per_sku_reports, "authority_map": authority_map}


def _collagen_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    return next(p for p in plan["sku_plans"] if p["sku_key"] == "collagen")


def _query(plan_sku: Dict[str, Any], text: str) -> Dict[str, Any]:
    return next(q for q in plan_sku["losing_queries"] if q["query"] == text)


def test_rederives_per_query_endorsement_targets():
    fx = _aruen_fixture()
    plan = build_win_plan(**fx)
    assert plan["available"] is True

    collagen = _collagen_plan(plan)
    # Only the two category queries — the branded one is excluded.
    assert {q["query"] for q in collagen["losing_queries"]} == {
        "best collagen cream",
        "best collagen for sleep",
    }

    q = _query(collagen, "best collagen cream")
    hosts = [t["host"] for t in q["grounds_in"]]
    # The independent endorsement hosts AI grounded the answer in — the targets.
    assert set(hosts) == {"byrdie.com", "forbes.com", "goodhousekeeping.com"}
    # All three are endorsement role; findability is never a target.
    assert all(t["role"] == "editorial_review" for t in q["grounds_in"])


def test_outreach_state_degrades_off_real_registry():
    fx = _aruen_fixture()
    plan = build_win_plan(**fx)
    q = _query(_collagen_plan(plan), "best collagen cream")
    state_by_host = {t["host"]: t["outreach"]["state"] for t in q["grounds_in"]}
    # Honest degrade off the REAL cited-host registry (no fabrication of a path):
    assert state_by_host["forbes.com"] == OUTREACH_DRAFT_READY          # has pitch email
    assert state_by_host["goodhousekeeping.com"] == OUTREACH_SUBMISSION_ONLY  # form only
    assert state_by_host["byrdie.com"] == OUTREACH_TARGET_ONLY          # no recipient yet
    # draft_ready means a real recipient email is present.
    forbes = next(t for t in q["grounds_in"] if t["host"] == "forbes.com")
    assert forbes["outreach"]["recipient"]["email"]
    byrdie = next(t for t in q["grounds_in"] if t["host"] == "byrdie.com")
    assert byrdie["outreach"]["recipient"] is None


def test_targets_ordered_tier_then_host():
    fx = _aruen_fixture()
    plan = build_win_plan(**fx)
    q = _query(_collagen_plan(plan), "best collagen cream")
    hosts = [t["host"] for t in q["grounds_in"]]
    # tier-1 publishers lead (byrdie/forbes, alpha within tier), tier-3 GH last.
    assert hosts.index("byrdie.com") < hosts.index("goodhousekeeping.com")
    assert hosts.index("forbes.com") < hosts.index("goodhousekeeping.com")


def test_competitor_benchmark_and_win_condition():
    fx = _aruen_fixture()
    plan = build_win_plan(**fx)
    q = _query(_collagen_plan(plan), "best collagen cream")
    assert q["competitor_benchmark"] == ["Vital Proteins", "Ancient Nutrition"]
    assert q["win_condition"] is not None
    assert "best collagen cream" in q["win_condition"]
    for h in ("byrdie.com", "forbes.com"):
        assert h in q["win_condition"]
    assert q["limit"] is None


def test_findability_only_query_names_no_target_no_inflation():
    fx = _aruen_fixture()
    plan = build_win_plan(**fx)
    q = _query(_collagen_plan(plan), "best collagen for sleep")
    # Own / retail listings are not a win — no fabricated target, honest limit.
    assert q["grounds_in"] == []
    assert q["limit"] is not None
    assert "aruen.us" in q["limit"] and "ebay.com" in q["limit"]
    assert "own" in q["limit"].lower()
    # No publisher target is NOT a dead end anymore: the lane routes to the
    # own-content play (AI already grounds in the merchant's own listings, so
    # strengthening that exact content is the win). Still no invented hosts —
    # the no-inflation contract lives in grounds_in/limit above.
    assert q["win_path"] == "own_content"
    assert q["win_condition"] and "your own" in q["win_condition"].lower()


def test_unresolvable_source_is_never_fabricated():
    # A losing category query whose only source uri is absent from the authority
    # map (the report couldn't resolve it) -> named no target, stated limit.
    per_sku_reports = [
        {
            "sku_key": "x",
            "sku_title": "X",
            "failing_prompts": [
                {
                    "query": "best x",
                    "axis": "category",
                    "grounding_sources": [_src("vx://unknown-1")],
                    "competitors_named": [],
                }
            ],
        }
    ]
    authority_map = {"skus": [{"sku_key": "x", "authority_hosts": []}]}
    plan = build_win_plan(per_sku_reports=per_sku_reports, authority_map=authority_map)
    q = plan["sku_plans"][0]["losing_queries"][0]
    assert q["grounds_in"] == []
    assert q["limit"] is not None
    assert "no resolvable independent source" in q["limit"]


def test_coverage_and_rollup_counts():
    fx = _aruen_fixture()
    plan = build_win_plan(**fx)
    collagen = _collagen_plan(plan)
    cov = collagen["coverage"]
    assert cov["losing_category_queries"] == 2
    assert cov["queries_with_grounded_target"] == 1            # only the cream query
    assert cov["queries_with_draft_ready_target"] == 1         # forbes on that query
    roll = plan["rollup"]
    assert roll["losing_category_queries"] == 2
    assert roll["independent_hosts_to_win"] == [
        "byrdie.com",
        "forbes.com",
        "goodhousekeeping.com",
    ]
    assert roll["draft_ready_hosts"] == ["forbes.com"]


def test_pitch_draft_rendered_for_emailable_target_keyed_to_query():
    # Phase 2: an emailable target with a matching playbook template gets a
    # rendered one-click pitch, keyed to THIS losing query + its competitors.
    fx = _aruen_fixture()
    plan = build_win_plan(merchant_name="Aruen", **fx)
    q = _query(_collagen_plan(plan), "best collagen cream")
    by_host = {t["host"]: t["outreach"] for t in q["grounds_in"]}

    forbes = by_host["forbes.com"]
    assert forbes["state"] == OUTREACH_DRAFT_READY
    draft = forbes["pitch_draft"]
    assert draft is not None
    assert draft["recipient_email"]                      # real registry email
    assert "Vital Proteins" in draft["body"]             # this query's benchmark
    assert "Aruen" in draft["body"]                      # the merchant

    # P3-8: a submission_only target now renders a paste-ready draft on the
    # submission_form channel (never a fake mailto — recipient_email stays
    # None, the published form URL rides along). target_only stays None.
    gh_draft = by_host["goodhousekeeping.com"]["pitch_draft"]
    assert gh_draft is not None
    assert gh_draft["channel"] == "submission_form"
    assert gh_draft["recipient_email"] is None
    assert gh_draft["submission_url"]
    assert "Aruen" in gh_draft["body"]
    assert by_host["byrdie.com"]["pitch_draft"] is None


def test_rollup_pitch_ready_distinct_from_emailable():
    fx = _aruen_fixture()
    plan = build_win_plan(merchant_name="Aruen", **fx)
    roll = plan["rollup"]
    # forbes is emailable AND renders a draft -> both lists. P3-8:
    # goodhousekeeping's submission_form draft makes it pitch-READY (a rendered
    # draft = act now) without being draft_ready (that stays a registry
    # emailability fact). byrdie (no recipient at all) is in neither.
    assert roll["draft_ready_hosts"] == ["forbes.com"]
    assert roll["pitch_ready_hosts"] == ["forbes.com", "goodhousekeeping.com"]


def test_no_losing_category_queries_is_unavailable():
    per_sku_reports = [
        {
            "sku_key": "s",
            "sku_title": "S",
            "failing_prompts": [
                {"query": "brand q", "axis": "branded", "grounding_sources": [], "competitors_named": []}
            ],
        }
    ]
    plan = build_win_plan(per_sku_reports=per_sku_reports, authority_map={"skus": []})
    assert plan["available"] is False
    assert plan["sku_plans"] == []
    assert plan["note"]


def test_defensive_on_garbage_input():
    # None / wrong types must degrade, never raise.
    assert build_win_plan(per_sku_reports=None, authority_map=None)["available"] is False
    assert build_win_plan(
        per_sku_reports=[None, {"sku_key": "a"}, 7],
        authority_map={"skus": [None, 3]},
    )["available"] is False


def _allure_fixture() -> Dict[str, Any]:
    """A losing category query grounded in allure.com — an editorial beauty host
    whose pitch_recipient was populated in the 2026-07-14 registry fill (the
    Best of Beauty submission form). Editorial beauty maps to a playbook with a
    pitch_template, so the win plan renders a paste-ready submission draft.
    Exercises the exact production path off the REAL registry."""
    per_sku_reports = [
        {
            "sku_key": "collagen",
            "sku_title": "Tofu Collagen Jelly Cream",
            "failing_prompts": [
                {
                    "query": "best collagen cream for glow",
                    "axis": "category",
                    "grounding_sources": [_src("vx://allure-1")],
                    "competitors_named": ["Vital Proteins"],
                },
            ],
        }
    ]
    authority_map = {
        "skus": [
            {
                "sku_key": "collagen",
                "authority_hosts": [
                    _host_row("allure.com", "editorial_review", ["vx://allure-1"]),
                ],
            }
        ]
    }
    return {"per_sku_reports": per_sku_reports, "authority_map": authority_map}


def test_newly_filled_editorial_host_flips_ladder_and_renders_draft():
    # Regression for the 2026-07-14 pitch_recipient fill: allure.com was
    # target_only; it must now reach submission_only off the REAL registry and
    # render a paste-ready pitch_draft via the playbook engine
    # (build_pitch_draft_for_host) on the honest submission_form channel —
    # never a fabricated mailto (recipient_email stays None).
    fx = _allure_fixture()
    plan = build_win_plan(merchant_name="Aruen", merchant_category="beauty", **fx)
    q = _query(_collagen_plan(plan), "best collagen cream for glow")
    allure = next(t["outreach"] for t in q["grounds_in"] if t["host"] == "allure.com")

    assert allure["state"] == OUTREACH_SUBMISSION_ONLY
    assert allure["recipient"]["submission_url"]        # real registry form URL
    assert allure["recipient"]["email"] is None         # no fabricated email
    draft = allure["pitch_draft"]
    assert draft is not None                             # rendered via playbook
    assert draft["channel"] == "submission_form"
    assert draft["recipient_email"] is None
    assert draft["submission_url"] == allure["recipient"]["submission_url"]
    assert "Aruen" in draft["body"]                      # keyed to the merchant
    assert "Vital Proteins" in draft["body"]             # this query's benchmark

    # Rollup: a rendered draft makes allure pitch-ready without being emailable
    # (draft_ready stays an email-only registry fact).
    roll = plan["rollup"]
    assert "allure.com" in roll["pitch_ready_hosts"]
    assert "allure.com" not in roll["draft_ready_hosts"]


def test_newly_filled_retailer_host_reaches_submission_only_without_fabrication():
    # The retailer fills (e.g. oliveyoung.co.kr vendor-inquiry form) flip the
    # outreach ladder to submission_only off the REAL registry via classify_host,
    # with no fabricated email. The wholesale playbook carries no pitch_template,
    # so build_pitch_draft_for_host honestly renders no one-click draft — the
    # ladder still advances past target_only, which is the win-plan gate.
    from services.cited_host_classifier import classify_host, reset_registry_cache
    from services.audit_playbook_engine import build_pitch_draft_for_host
    from services.win_plan_builder import _outreach_from_meta

    reset_registry_cache()
    meta = classify_host("oliveyoung.co.kr", merchant_category="beauty")
    recipient = meta.get("pitch_recipient")
    assert recipient and recipient.get("submission_url")   # real vendor form
    assert recipient.get("email") is None                  # no fabricated email

    outreach = _outreach_from_meta(meta)
    assert outreach["state"] == OUTREACH_SUBMISSION_ONLY    # flipped off target_only

    # No pitch_template for the wholesale playbook -> honest "no draft".
    assert build_pitch_draft_for_host(
        meta, merchant_name="Aruen", merchant_category="beauty",
        example_query="best k-beauty collagen", competitors_named=["Vital Proteins"],
        allow_submission_channel=True,
    ) is None


def test_batch2_glowpick_reaches_submission_only_and_renders_review_aggregator_draft():
    # Batch-2 fill (2026-07-14): glowpick.com — the Hwahae-peer Korean review
    # aggregator — carries a pitch_recipient pointing at Glowpick's official
    # product-DB registration guide (listing registration, not a press pitch).
    # It must flip to submission_only off the REAL registry with no fabricated
    # email, and the review_aggregator playbook has a pitch_template, so a
    # submission_form draft renders for the merchant to adapt.
    from services.cited_host_classifier import classify_host, reset_registry_cache
    from services.audit_playbook_engine import build_pitch_draft_for_host
    from services.win_plan_builder import _outreach_from_meta

    reset_registry_cache()
    meta = classify_host("glowpick.com", merchant_category="beauty")
    recipient = meta.get("pitch_recipient")
    assert recipient and recipient.get("submission_url")   # real DB-registration guide
    assert recipient.get("email") is None                  # no fabricated email
    assert "2026-07-14" in (recipient.get("note") or "")   # dated verification carried

    outreach = _outreach_from_meta(meta)
    assert outreach["state"] == OUTREACH_SUBMISSION_ONLY   # flipped off target_only

    draft = build_pitch_draft_for_host(
        meta, merchant_name="Aruen", merchant_category="beauty",
        example_query="best k-beauty collagen cream", competitors_named=["Vital Proteins"],
        allow_submission_channel=True,
    )
    assert draft is not None                               # review_aggregator playbook renders
    assert draft["channel"] == "submission_form"
    assert draft["recipient_email"] is None                # honest: form/guide channel, no mailto
    assert draft["submission_url"] == recipient["submission_url"]


def test_batch2_dermstore_reaches_submission_only_via_wholesale_channel():
    # Batch-2 fill (2026-07-14): dermstore.com publishes no vendor page; the
    # public path is a RangeMe supplier profile (Dermstore is a listed RangeMe
    # retail buyer). Retailer-type host -> wholesale playbook: the ladder
    # advances to submission_only, and build_pitch_draft_for_host honestly
    # renders no one-click draft (wholesale playbooks carry no pitch_template).
    from services.cited_host_classifier import classify_host, reset_registry_cache
    from services.audit_playbook_engine import build_pitch_draft_for_host
    from services.win_plan_builder import _outreach_from_meta

    reset_registry_cache()
    meta = classify_host("dermstore.com", merchant_category="beauty")
    assert meta.get("type") == "retailer"                  # routes to wholesale playbook
    recipient = meta.get("pitch_recipient")
    assert recipient and recipient.get("submission_url")   # RangeMe supplier intake
    assert recipient.get("email") is None                  # no fabricated email
    assert "2026-07-14" in (recipient.get("note") or "")   # dated verification carried

    outreach = _outreach_from_meta(meta)
    assert outreach["state"] == OUTREACH_SUBMISSION_ONLY

    assert build_pitch_draft_for_host(
        meta, merchant_name="Aruen", merchant_category="beauty",
        example_query="best vitamin c serum", competitors_named=["Skinceuticals"],
        allow_submission_channel=True,
    ) is None


def test_batch2_no_channel_hosts_stay_target_only_with_dated_hint():
    # Honesty guard: hosts whose research found NO public intake path must stay
    # target_only (no recipient invented) and carry the finding, dated, in the
    # outreach_hint so the narrative stops implying a pitch is possible.
    from services.cited_host_classifier import classify_host, reset_registry_cache
    from services.win_plan_builder import _outreach_from_meta

    reset_registry_cache()
    for host in ("sokoglam.com", "jolse.com", "stylevana.com", "stylekorean.com"):
        meta = classify_host(host, merchant_category="beauty")
        assert meta.get("pitch_recipient") is None, host
        assert _outreach_from_meta(meta)["state"] == OUTREACH_TARGET_ONLY, host
        hint = meta.get("outreach_hint") or ""
        assert "2026-07-14" in hint, host                  # dated no-channel verification
