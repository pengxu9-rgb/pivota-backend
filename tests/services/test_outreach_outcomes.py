"""services/outreach_outcomes.py — the audit→action→outcome loop.

Fixture pairs mirror the real per-SKU brand-report shape (win_plan targets +
authority_map host matrix + failing_prompts grounding), modeled on merchant
merch_924da2be8503e5f7's July 2026 runs (hwahae endorsement present, targets
varying run to run). Each outcome class, the not-comparable basis gate, and
the first-audit degrade get a fixture pair.
"""

from __future__ import annotations

from services.outreach_outcomes import build_outreach_outcomes

SAME_BASIS = {"same": True, "prompt_set_id": "sel_w2", "note": "same set"}
CHANGED_BASIS = {"same": False, "prompt_set_id": "sel_new", "note": "refreshed"}
UNKNOWN_BASIS = {"same": None, "prompt_set_id": None, "note": "predates pinning"}


def _host_row(
    host: str,
    *,
    cites_exact_sku: bool = False,
    cites_near_variant: bool = False,
    citation_role: str = "independent_endorsement",
    prompts_cited_count: int = 1,
) -> dict:
    return {
        "host": host,
        "citation_role": citation_role,
        "recommendation_class": "lists",
        "cited_on_category_query": True,
        "cites_exact_sku": cites_exact_sku,
        "cites_near_variant": cites_near_variant,
        "first_party": False,
        "is_competitor": False,
        "prompts_cited_count": prompts_cited_count,
    }


def _prior_report(
    *,
    target_hosts: list[str] | None = None,
    query: str = "best hair care",
    outreach_move_hosts: list[str] | None = None,
    closed_channel_hosts: list[str] | None = None,
    endorsement_hosts: list[str] | None = None,
) -> dict:
    targets = [
        {"host": h, "role": "independent_endorsement", "tier": 1,
         "outreach": {"state": "draft_ready"}}
        for h in (target_hosts if target_hosts is not None else ["hwahae.com"])
    ]
    return {
        "timestamp": "2026-07-08T00:00:00+00:00",
        "win_plan": {
            "available": True,
            "sku_plans": [
                {
                    "sku_key": "sku1",
                    "losing_queries": [
                        {
                            "query": query,
                            "axis": "category",
                            "grounds_in": targets,
                            "win_condition": f'Get cited for "{query}".',
                            "competitor_benchmark": ["Rival Beauty"],
                        }
                    ],
                }
            ],
        },
        "merchant_narrative": {
            "where_youre_losing": {
                "outreach_moves": [
                    {"host": h, "headline": f"Pitch {h}", "realism": "reachable"}
                    for h in (outreach_move_hosts or [])
                ],
                "closed_channels": [
                    {"host": h} for h in (closed_channel_hosts or [])
                ],
            }
        },
        "authority_map": {
            "hosts": [
                _host_row(h)
                for h in (target_hosts if target_hosts is not None else ["hwahae.com"])
            ]
            + [_host_row(h) for h in (outreach_move_hosts or [])],
            "host_attribution_summary": {
                "endorsement_hosts": list(endorsement_hosts or []),
                "endorsement_category_hosts": [],
            },
        },
    }


def _current_report(
    *,
    still_failing_query: str | None = "best hair care",
    grounding_hosts: list[str] | None = None,
    host_rows: list[dict] | None = None,
    endorsement_hosts: list[str] | None = None,
    endorsement_category_hosts: list[str] | None = None,
    probed_queries: list[str] | None = None,
) -> dict:
    grounding_hosts = (
        grounding_hosts if grounding_hosts is not None else ["hwahae.com"]
    )
    uris = [f"https://{h}/article" for h in grounding_hosts]
    failing_prompts = (
        [
            {
                "query": still_failing_query,
                "axis": "category",
                "grounding_sources": [{"uri": u} for u in uris],
            }
        ]
        if still_failing_query
        else []
    )
    authority_hosts = [
        {**_host_row(h), "evidence_urls": [f"https://{h}/article"]}
        for h in grounding_hosts
    ]
    report = {
        "timestamp": "2026-07-14T00:00:00+00:00",
        "per_sku_reports": [
            {
                "sku_key": "sku1",
                "failing_prompts": failing_prompts,
                **(
                    {
                        "opportunity": {
                            "per_prompt": [{"query": q} for q in probed_queries]
                        }
                    }
                    if probed_queries is not None
                    else {}
                ),
            }
        ],
        "authority_map": {
            "skus": [{"sku_key": "sku1", "authority_hosts": authority_hosts}],
            "hosts": (
                host_rows
                if host_rows is not None
                else [_host_row(h) for h in grounding_hosts]
            ),
            "host_attribution_summary": {
                "endorsement_hosts": list(endorsement_hosts or []),
                "endorsement_category_hosts": list(
                    endorsement_category_hosts or []
                ),
            },
        },
    }
    return report


def _target(outcomes: dict, host: str, query: str | None = "best hair care") -> dict:
    return next(
        t
        for t in outcomes["targets"]
        if t["host"] == host and t["query"] == query
    )


def test_first_audit_degrades_to_empty_baseline():
    outcomes = build_outreach_outcomes(
        current_report=_current_report(),
        prior_report=None,
        measurement_basis={"same": None, "note": "baseline"},
    )

    assert outcomes["is_first_audit"] is True
    assert outcomes["available"] is False
    assert outcomes["targets"] == []
    assert "next re-audit" in outcomes["note"]


def test_won_when_previously_losing_query_recovers():
    prior = _prior_report(target_hosts=["hwahae.com"])
    # Query gone from the failing set, and the probed-prompt list confirms it
    # WAS sampled this run — a real recovery, not a coverage artifact.
    current = _current_report(
        still_failing_query=None,
        grounding_hosts=["hwahae.com"],
        probed_queries=["best hair care"],
    )

    outcomes = build_outreach_outcomes(
        current_report=current, prior_report=prior, measurement_basis=SAME_BASIS
    )

    row = _target(outcomes, "hwahae.com")
    assert row["outcome"] == "won"
    assert row["reason"] == "query_now_cited"
    assert "best hair care" in row["what_changed"]
    assert outcomes["summary"]["won"] == 1


def test_won_when_target_host_enters_endorsement_set():
    """The specific query is still lost, but the target host now names the
    merchant on a category query — an endorsement transition is a win."""
    prior = _prior_report(target_hosts=["hwahae.com"])
    current = _current_report(
        still_failing_query="best hair care",
        grounding_hosts=["hwahae.com"],
        endorsement_hosts=["hwahae.com"],
        endorsement_category_hosts=["hwahae.com"],
    )

    outcomes = build_outreach_outcomes(
        current_report=current, prior_report=prior, measurement_basis=SAME_BASIS
    )

    row = _target(outcomes, "hwahae.com")
    assert row["outcome"] == "won"
    assert row["reason"] == "host_now_endorses"
    assert "hwahae.com now names you" in row["what_changed"]


def test_progress_when_host_cites_sku_but_query_still_lost():
    prior = _prior_report(target_hosts=["goodhousekeeping.com"])
    current = _current_report(
        still_failing_query="best hair care",
        grounding_hosts=["goodhousekeeping.com"],
        host_rows=[_host_row("goodhousekeeping.com", cites_exact_sku=True)],
    )

    outcomes = build_outreach_outcomes(
        current_report=current, prior_report=prior, measurement_basis=SAME_BASIS
    )

    row = _target(outcomes, "goodhousekeeping.com")
    assert row["outcome"] == "progress"
    assert row["signals"]["current"]["names_you"] is True
    assert row["signals"]["current"]["query_still_losing"] is True
    assert 'still lost' in row["what_changed"]


def test_no_change_when_host_still_grounds_query_without_naming():
    prior = _prior_report(target_hosts=["goodhousekeeping.com"])
    current = _current_report(
        still_failing_query="best hair care",
        grounding_hosts=["goodhousekeeping.com"],
    )

    outcomes = build_outreach_outcomes(
        current_report=current, prior_report=prior, measurement_basis=SAME_BASIS
    )

    row = _target(outcomes, "goodhousekeeping.com")
    assert row["outcome"] == "no_change"
    assert (
        row["what_changed"]
        == 'goodhousekeeping.com still grounds "best hair care" without naming you.'
    )


def test_no_longer_grounded_when_host_vanishes_from_query_grounding():
    """The host disappeared from THIS query's grounding — honestly neither a
    win nor a loss (the engine sampled different sources)."""
    prior = _prior_report(target_hosts=["hwahae.com"])
    current = _current_report(
        still_failing_query="best hair care",
        grounding_hosts=["allure.com"],  # hwahae absent this run
    )

    outcomes = build_outreach_outcomes(
        current_report=current, prior_report=prior, measurement_basis=SAME_BASIS
    )

    row = _target(outcomes, "hwahae.com")
    assert row["outcome"] == "no_longer_grounded"
    assert row["reason"] == "absent_from_query_grounding"
    assert "neither a win nor a loss" in row["what_changed"]


def test_query_absent_from_probe_set_is_not_claimed_as_won():
    """failing_prompts is capped upstream — a query missing from BOTH the
    failing set and the probed-prompt list is a coverage artifact, never a
    win claim."""
    prior = _prior_report(target_hosts=["hwahae.com"])
    current = _current_report(
        still_failing_query=None,
        grounding_hosts=["hwahae.com"],
        probed_queries=["a different query entirely"],
    )

    outcomes = build_outreach_outcomes(
        current_report=current, prior_report=prior, measurement_basis=SAME_BASIS
    )

    row = _target(outcomes, "hwahae.com")
    assert row["outcome"] == "no_longer_grounded"
    assert row["reason"] == "query_not_probed"
    assert outcomes["summary"]["won"] == 0


def test_changed_basis_gates_all_query_level_claims():
    prior = _prior_report(target_hosts=["hwahae.com", "goodhousekeeping.com"])
    current = _current_report(
        still_failing_query=None,  # would read as "won" on a same-basis run
        grounding_hosts=["goodhousekeeping.com"],
    )

    outcomes = build_outreach_outcomes(
        current_report=current, prior_report=prior, measurement_basis=CHANGED_BASIS
    )

    assert outcomes["comparable"] is False
    assert len(outcomes["targets"]) == 2
    assert all(t["outcome"] == "not_comparable" for t in outcomes["targets"])
    assert all(t["reason"] == "basis_changed" for t in outcomes["targets"])


def test_changed_basis_still_reports_endorsement_transition():
    """Basis-independent fact: the target host entered the endorsement set.
    That transition is reportable even when per-query claims are gated."""
    prior = _prior_report(target_hosts=["hwahae.com", "goodhousekeeping.com"])
    current = _current_report(
        still_failing_query="best hair care",
        grounding_hosts=["goodhousekeeping.com"],
        endorsement_hosts=["hwahae.com"],
    )

    outcomes = build_outreach_outcomes(
        current_report=current, prior_report=prior, measurement_basis=CHANGED_BASIS
    )

    assert _target(outcomes, "hwahae.com")["outcome"] == "won"
    assert _target(outcomes, "hwahae.com")["reason"] == "host_now_endorses"
    assert _target(outcomes, "goodhousekeeping.com")["outcome"] == "not_comparable"


def test_unknown_basis_is_as_conservative_as_changed_basis():
    prior = _prior_report(target_hosts=["hwahae.com"])
    current = _current_report(
        still_failing_query=None, grounding_hosts=["hwahae.com"]
    )

    outcomes = build_outreach_outcomes(
        current_report=current, prior_report=prior, measurement_basis=UNKNOWN_BASIS
    )

    assert _target(outcomes, "hwahae.com")["outcome"] == "not_comparable"


def test_host_only_outreach_move_targets_classify_without_query():
    prior = _prior_report(
        target_hosts=[],
        outreach_move_hosts=["reddit.com", "vanished.example"],
    )
    current = _current_report(
        still_failing_query="best hair care",
        grounding_hosts=["reddit.com"],
        host_rows=[_host_row("reddit.com")],
    )

    outcomes = build_outreach_outcomes(
        current_report=current, prior_report=prior, measurement_basis=SAME_BASIS
    )

    still = _target(outcomes, "reddit.com", query=None)
    assert still["target_source"] == "outreach_move"
    assert still["outcome"] == "no_change"
    gone = _target(outcomes, "vanished.example", query=None)
    assert gone["outcome"] == "no_longer_grounded"
    assert gone["reason"] == "absent_from_run_grounding"


def test_closed_channels_are_excluded_not_silently_dropped():
    prior = _prior_report(
        target_hosts=["hwahae.com"],
        outreach_move_hosts=["hair.com"],
        closed_channel_hosts=["hair.com"],
    )
    current = _current_report(grounding_hosts=["hwahae.com"])

    outcomes = build_outreach_outcomes(
        current_report=current, prior_report=prior, measurement_basis=SAME_BASIS
    )

    assert outcomes["closed_channels_excluded"] == ["hair.com"]
    assert all(t["host"] != "hair.com" for t in outcomes["targets"])


def test_prior_without_targets_degrades_honestly():
    prior = {"win_plan": {"available": False, "sku_plans": []}}
    outcomes = build_outreach_outcomes(
        current_report=_current_report(),
        prior_report=prior,
        measurement_basis=SAME_BASIS,
    )

    assert outcomes["available"] is False
    assert outcomes["targets"] == []
    assert "no outreach targets" in outcomes["note"]


def test_current_without_host_data_degrades_honestly():
    """A legacy per-product current report carries no authority_map — the
    section must say outcomes aren't measurable, never emit host claims."""
    prior = _prior_report(target_hosts=["hwahae.com"])
    current = {"merchant_view": {"headline": {}}}

    outcomes = build_outreach_outcomes(
        current_report=current, prior_report=prior, measurement_basis=SAME_BASIS
    )

    assert outcomes["available"] is False
    assert outcomes["targets"] == []
    assert "no host-level grounding data" in outcomes["note"]


def test_no_causation_copy_anywhere():
    prior = _prior_report(target_hosts=["hwahae.com"])
    current = _current_report(
        still_failing_query=None,
        grounding_hosts=["hwahae.com"],
        endorsement_hosts=["hwahae.com"],
        probed_queries=["best hair care"],
    )

    outcomes = build_outreach_outcomes(
        current_report=current, prior_report=prior, measurement_basis=SAME_BASIS
    )

    # The merchant-facing outcome copy must never claim causation. (The
    # top-level note is the explicit DISCLAIMER of causation, so it is scanned
    # separately.)
    copy = " ".join(t["what_changed"] for t in outcomes["targets"]).lower()
    for banned in ("your pitch worked", "caused", "because of your outreach", "thanks to"):
        assert banned not in copy
    assert "do not prove" in outcomes["note"]


def test_marked_done_task_is_surfaced_as_fact_not_causation():
    prior = _prior_report(target_hosts=["hwahae.com"])
    current = _current_report(
        still_failing_query=None,
        grounding_hosts=["hwahae.com"],
        probed_queries=["best hair care"],
    )

    outcomes = build_outreach_outcomes(
        current_report=current,
        prior_report=prior,
        measurement_basis=SAME_BASIS,
        completed_actions=[
            {
                "host": "hwahae.com",
                "title": "Pitch hwahae.com",
                "completed_at": "2026-07-10T00:00:00+00:00",
            }
        ],
    )

    action = _target(outcomes, "hwahae.com")["merchant_action"]
    assert action["days_before_current_run"] == 4
    assert "You marked" in action["note"]
    assert "caused" not in action["note"].lower()


def test_dual_provenance_marked_when_host_is_both_win_plan_and_move():
    prior = _prior_report(
        target_hosts=["hwahae.com"], outreach_move_hosts=["hwahae.com"]
    )
    current = _current_report(grounding_hosts=["hwahae.com"])

    outcomes = build_outreach_outcomes(
        current_report=current, prior_report=prior, measurement_basis=SAME_BASIS
    )

    row = _target(outcomes, "hwahae.com")
    assert row["target_source"] == "win_plan+outreach_move"
    # No duplicate host-only row for a host already query-keyed.
    assert len([t for t in outcomes["targets"] if t["host"] == "hwahae.com"]) == 1


def test_count_deltas_never_classify_on_one_cite_baselines():
    """A 1→2 prompts_cited_count move with no categorical transition stays
    no_change — count jitter is noise, only transitions are signal."""
    prior = _prior_report(target_hosts=["goodhousekeeping.com"])
    current = _current_report(
        still_failing_query="best hair care",
        grounding_hosts=["goodhousekeeping.com"],
        host_rows=[
            _host_row("goodhousekeeping.com", prompts_cited_count=2)
        ],
    )

    outcomes = build_outreach_outcomes(
        current_report=current, prior_report=prior, measurement_basis=SAME_BASIS
    )

    row = _target(outcomes, "goodhousekeeping.com")
    assert row["outcome"] == "no_change"
    assert row["signals"]["current"]["prompts_cited_count"] == 2
