"""W1 RunFacts phase-1 tests.

Three contracts under test:
  1. PARITY — compute_run_facts' rollups equal the legacy implementations
     (extract_cited_hosts / _own_url_cited_runs) on the same runs, since
     phase 1 must change no displayed number.
  2. STAMP — build_structured_report stamps run_facts and the stamped values
     agree with the legacy numbers it rendered from.
  3. W7 — _inv_counters_match_run_facts flags incoherent/mutated stamps and
     stays silent on payloads without stamps (pre-W1) and on coherent ones.
"""
from __future__ import annotations

import logging

from services.audit_facts import (
    LEGACY_CITEDNESS_SITES,
    QUERY_CLASS_BRANDED,
    QUERY_CLASS_CATEGORY,
    aggregate_run_facts,
    compute_run_facts,
    own_url_cited_runs_any,
    parity_check,
    parity_measure,
)


def _run(
    query: str,
    sources,
    provider: str = "gemini",
    axis: str | None = None,
):
    run = {"query": query, "grounding_sources": sources, "_provider": provider}
    if axis:
        run["axis_metadata"] = {"axis": axis}
    return run


# Grounding-source shapes: a real-URI citation (OpenAI style) and a Vertex
# redirector whose title carries the real source (Gemini style).
OWN_SOURCE = {"uri": "https://glowbrand.com/products/serum", "title": "Glow Brand Serum"}
RETAILER_SOURCE = {"uri": "https://sephora.com/p/serum", "title": "Sephora"}
EDITORIAL_NAMING_SOURCE = {
    "uri": "https://byrdie.com/best-serums",
    "title": "Best serums: Glow Brand tops our list",
}
REDIRECTOR_RETAILER = {
    "uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc",
    "title": "oliveyoung.com",
}

# The branded runs carry their real stamped axis ("intent" / "review"). They
# used to carry NO axis and leaned on the classifier's fail-BRANDED default,
# which made the branded assertions below vacuous — every unstamped run was
# branded. That default is now fail-UNBRANDED, so the fixture states the axis
# the probe pipeline actually stamps for these query shapes.
RUNS = [
    # branded query, gemini: own page cited (T1) + retailer listing
    _run("where to buy glow brand serum", [OWN_SOURCE, RETAILER_SOURCE], "gemini", axis="intent"),
    # category query, gemini: editorial host NAMES the brand (T2+T3), no own page
    _run("best vitamin c serum", [EDITORIAL_NAMING_SOURCE], "gemini", axis="category"),
    # category query, openai: only a redirector-resolved retailer, brand absent
    _run("best vitamin c serum", [REDIRECTOR_RETAILER], "openai", axis="category"),
    # branded query, openai: no citations at all
    _run("glow brand serum reviews", [], "openai", axis="review"),
]

IDENTITY = dict(
    merchant_host="glowbrand.com",
    merchant_brand="Glow Brand",
    merchant_vendors=("Glow Brand",),
)


def test_parity_with_legacy_implementations():
    """The whole point of phase 1: RunFacts rollups == legacy numbers."""
    # T1's legacy oracle now lives in audit_facts (removed from the god module's
    # import surface once the report path read RunFacts exclusively). T3's
    # extract_cited_hosts stays in the god module — it still supplies the
    # competitor rollup, so the parity net imports it from there.
    from services.agent_center_bd_report_service import extract_cited_hosts
    from services.audit_facts import _own_url_cited_runs

    facts = compute_run_facts(RUNS, **IDENTITY)
    _, legacy_cited, legacy_any = extract_cited_hosts(RUNS, **IDENTITY)
    legacy_own = _own_url_cited_runs(RUNS, merchant_host="glowbrand.com")

    assert facts.brand_mentioned_runs == legacy_cited
    assert facts.runs_with_citations == legacy_any
    assert facts.own_url_cited_runs == legacy_own


def test_tier_semantics():
    facts = compute_run_facts(RUNS, **IDENTITY)

    # T1: only the run citing glowbrand.com itself.
    assert facts.own_url_cited_runs == 1
    # T3: own-page run (title carries brand) + editorial run naming the brand.
    assert facts.brand_mentioned_runs == 2
    # T2: byrdie.com endorses (editorial + names the brand); oliveyoung is a
    # listing that never names the brand; sephora never names the brand.
    assert facts.endorsement_hosts == ("byrdie.com",)
    # 3 of 4 runs had >=1 identified source.
    assert facts.runs_with_citations == 3
    assert facts.run_count == 4

    by_query = {p.query: p for p in facts.prompts}
    assert len(facts.prompts) == 3  # two category runs share one prompt group
    cat = by_query["best vitamin c serum"]
    assert cat.cls == QUERY_CLASS_CATEGORY
    assert cat.run_count == 2
    assert cat.brand_mentioned is True  # via gemini's editorial citation
    assert cat.own_url_cited is False
    assert cat.provider_verdicts["gemini"]["brand_mentioned"] is True
    assert cat.provider_verdicts["openai"]["brand_mentioned"] is False
    assert "oliveyoung.com" in cat.cited_hosts  # redirector resolved via title

    branded = by_query["where to buy glow brand serum"]
    assert branded.cls == QUERY_CLASS_BRANDED
    assert branded.own_url_cited is True

    assert facts.prompt_count_by_class == {
        QUERY_CLASS_BRANDED: 2,
        QUERY_CLASS_CATEGORY: 1,
    }
    assert facts.provider_coverage == {"gemini": 2, "openai": 2}


def test_no_merchant_host_yields_none_not_zero():
    """Mirrors _own_url_cited_runs: no host to match -> None (never a false 0)."""
    facts = compute_run_facts(RUNS, merchant_host=None, merchant_brand="Glow Brand")
    assert facts.own_url_cited_runs is None
    # T3 still computable from the brand name.
    assert facts.brand_mentioned_runs == 2


def test_aggregate_run_facts_folds_counters():
    a = compute_run_facts(RUNS[:2], **IDENTITY).to_dict()
    b = compute_run_facts(RUNS[2:], **IDENTITY).to_dict()
    fold = aggregate_run_facts([a, b], identity={"host": "glowbrand.com"})

    assert fold["own_url_cited_runs"] == 1
    assert fold["brand_mentioned_runs"] == 2
    assert fold["run_count"] == 4
    assert fold["runs_with_citations"] == 3
    assert fold["prompt_count"] == 4  # 2 + 2 (the shared query is per-product)
    assert fold["endorsement_hosts"] == ["byrdie.com"]
    assert fold["provider_coverage"] == {"gemini": 2, "openai": 2}
    assert fold["aggregated_from"] == 2
    # None-aware: children without a matchable host don't fabricate a 0.
    no_host = compute_run_facts(RUNS[:1], merchant_host=None).to_dict()
    assert aggregate_run_facts([no_host])["own_url_cited_runs"] is None


def _errored_run(query: str, provider: str = "gemini", *, via_key: bool = False):
    """A gateway run that carried an upstream error instead of an answer —
    HTTP 200 with `raw` prefixed `__error__:` (or an explicit `error` key),
    mirroring tests/services/test_agent_center_bd_per_sku._chatgpt_all_429_probe_runs."""
    run = {"query": query, "_provider": provider, "grounding_sources": []}
    if via_key:
        run["error"] = "upstream 429"
    else:
        run["raw"] = (
            "__error__:429 You exceeded your current quota, please check "
            "your plan and billing details."
        )
    return run


def test_run_errored_predicate():
    from services.audit_facts import run_errored

    assert run_errored(_errored_run("q")) is True
    assert run_errored(_errored_run("q", via_key=True)) is True
    assert run_errored(_run("q", [])) is False
    assert run_errored({"raw": "a real answer"}) is False
    assert run_errored(None) is False
    assert run_errored("not a mapping") is False


def test_errored_runs_excluded_from_all_rollups():
    """The 2026-07-21 scale-smoke shape (run 509cf81c): a provider lane errors
    wholesale. Errored runs are no evidence in either direction, so they must
    not inflate run_count / provider_coverage / prompt groups — the facts over
    a polluted set equal the facts over the clean set."""
    errored = [
        # Same query as a clean run — must not inflate that prompt group.
        _errored_run("best vitamin c serum", "gemini"),
        # Errored-only query — must not yield a false not-cited PromptFacts row.
        _errored_run("glow brand vs competitors", "gemini"),
        # Wholesale-errored provider — must not appear in provider_coverage.
        _errored_run("best vitamin c serum", "perplexity"),
        _errored_run("glow brand serum reviews", "perplexity", via_key=True),
    ]
    clean = compute_run_facts(RUNS, **IDENTITY)
    polluted = compute_run_facts(RUNS + errored, **IDENTITY)

    assert polluted.to_dict() == clean.to_dict()
    assert polluted.run_count == 4
    assert polluted.provider_coverage == {"gemini": 2, "openai": 2}
    assert "glow brand vs competitors" not in {p.query for p in polluted.prompts}
    by_query = {p.query: p for p in polluted.prompts}
    assert by_query["best vitamin c serum"].run_count == 2
    assert "perplexity" not in by_query["best vitamin c serum"].provider_verdicts


def test_all_errored_runs_yield_empty_facts():
    """A run set that errored wholesale measured nothing: zero denominators and
    no prompt rows, never a plausible-looking all-zeros verdict table."""
    facts = compute_run_facts(
        [_errored_run("best vitamin c serum"), _errored_run("q2", via_key=True)],
        **IDENTITY,
    )
    assert facts.run_count == 0
    assert facts.prompts == ()
    assert facts.provider_coverage == {}
    assert facts.brand_mentioned_runs == 0
    assert facts.runs_with_citations == 0
    assert facts.own_url_cited_runs == 0  # host present -> 0, not None


def test_parity_anchors_hold_with_errored_runs():
    """The docstring's parity anchors compare cited-run NUMERATORS; an errored
    run has no grounding sources, so legacy (unfiltered) and RunFacts
    (filtered) stay equal even on a polluted run set."""
    from services.agent_center_bd_report_service import extract_cited_hosts
    from services.audit_facts import _own_url_cited_runs

    polluted = RUNS + [
        _errored_run("best vitamin c serum", "gemini"),
        _errored_run("glow brand vs competitors", "perplexity"),
    ]
    facts = compute_run_facts(polluted, **IDENTITY)
    _, legacy_cited, legacy_any = extract_cited_hosts(polluted, **IDENTITY)
    legacy_own = _own_url_cited_runs(polluted, merchant_host="glowbrand.com")

    assert facts.brand_mentioned_runs == legacy_cited
    assert facts.runs_with_citations == legacy_any
    assert facts.own_url_cited_runs == legacy_own


def test_parity_check_logs_only_on_drift(caplog):
    with caplog.at_level(logging.WARNING, logger="services.audit_facts"):
        assert parity_check("site.x", 3, 3) is True
        assert "RUNFACTS_PARITY_DRIFT" not in caplog.text
        assert parity_check("site.x", 3, 4, context={"m": "glow"}) is False
        assert "RUNFACTS_PARITY_DRIFT" in caplog.text
        assert "site.x" in caplog.text


def test_parity_check_drift_alerts_once_per_site():
    """W7 C3: a parity_check drift (Type-A invariant violation) flags the site for a
    push alert — once per process, so hot per-prompt loops don't spam."""
    import services.audit_facts as af

    af._ALERTED_PARITY_SITES.discard("site.c3")
    assert "site.c3" not in af._ALERTED_PARITY_SITES
    parity_check("site.c3", 1, 2)                      # drift → alert flagged
    assert "site.c3" in af._ALERTED_PARITY_SITES
    parity_check("site.c3", 5, 9)                      # second drift → no re-flag/spam
    assert "site.c3" in af._ALERTED_PARITY_SITES


def test_parity_check_equal_does_not_alert():
    import services.audit_facts as af

    af._ALERTED_PARITY_SITES.discard("site.c3b")
    parity_check("site.c3b", 7, 7)                     # equal → no alert
    assert "site.c3b" not in af._ALERTED_PARITY_SITES


def test_parity_measure_drift_does_not_alert():
    """parity_measure is Type-B (definitions differ by design) — drift is EXPECTED
    and must never page."""
    from services.audit_facts import parity_measure
    import services.audit_facts as af

    af._ALERTED_PARITY_SITES.discard("site.measure")
    parity_measure("site.measure", 3, 5)              # a real gap, but not an alarm
    assert "site.measure" not in af._ALERTED_PARITY_SITES


def test_legacy_site_registry_covers_all_12():
    assert [s["id"] for s in LEGACY_CITEDNESS_SITES] == list(range(1, 13))
    # The registry is the cutover checklist — every entry states tier + mode.
    assert all(s.get("tier") and s.get("site") for s in LEGACY_CITEDNESS_SITES)
    assert all(s.get("mode") in ("drift", "measure") for s in LEGACY_CITEDNESS_SITES)
    # Phase-2: sites 1-9 instrumented, and site 11 (proof-of-done) rewired to the
    # site-8 endorsement set. Sites 10 (defer) + 12 (keep-variant) carry scoping
    # notes instead of a cutover.
    by_id = {s["id"]: s for s in LEGACY_CITEDNESS_SITES}
    assert all(by_id[i]["instrumented"] for i in list(range(1, 10)) + [11])
    assert all(not by_id[i]["instrumented"] and by_id[i].get("notes") for i in (10, 12))


def test_own_url_cited_runs_any_multi_host():
    # Same runs, richer identity: the Pivota-canonical host counts too.
    canonical_run = _run(
        "glow brand serum",
        [{"uri": "https://pdp.pivota.cc/sig_abc", "title": "Glow Brand Serum"}],
    )
    runs = RUNS + [canonical_run]
    # Single host — matches _own_url_cited_runs semantics on the same input.
    from services.audit_facts import _own_url_cited_runs

    assert own_url_cited_runs_any(runs, ["glowbrand.com"]) == _own_url_cited_runs(
        runs, merchant_host="glowbrand.com"
    ) == 1
    # Multi-host T1 picks up the canonical citation as first-party.
    assert own_url_cited_runs_any(
        runs, ["https://glowbrand.com/products/serum", "https://pdp.pivota.cc/x"]
    ) == 2
    # No resolvable hosts -> None (no false zero), same contract as legacy.
    assert own_url_cited_runs_any(runs, []) is None
    assert own_url_cited_runs_any(runs, [None, ""]) is None


def test_parity_measure_always_logs_info(caplog):
    with caplog.at_level(logging.INFO, logger="services.audit_facts"):
        parity_measure("site.m", 5, 5, context={"sku": "x"})  # agreement logs too
        parity_measure("site.m", 5, 7)
    lines = [r for r in caplog.records if "RUNFACTS_PARITY_MEASURE" in r.getMessage()]
    assert len(lines) == 2
    assert "equal=True" in lines[0].getMessage()
    assert "equal=False" in lines[1].getMessage()
    # Measurement is INFO — it must never land in the WARNING drift channel.
    assert all(r.levelno == logging.INFO for r in lines)


def test_build_structured_report_stamps_run_facts():
    """STAMP contract: the payload carries run_facts and it reconciles with the
    legacy numbers the same report rendered (phase 1 = zero drift here)."""
    from services.agent_center_bd_report_service import build_structured_report

    report = build_structured_report(
        merchant_name="Glow Brand",
        merchant_pdp_url="https://glowbrand.com/products/serum",
        product_title="Glow Brand Serum",
        product_vendor="Glow Brand",
        product_type="serum",
        visibility_result={"provider": "gemini", "scores": {"visibility_score": 50}, "raw_runs": []},
        attribution_result={"provider": "gemini", "scores": {"visibility_score": 50}, "raw_runs": RUNS},
        provider="gemini",
    )
    rf = report["run_facts"]
    assert rf["schema_version"] == 1
    assert rf["run_count"] == 4
    assert rf["own_url_cited_runs"] == 1
    assert rf["identity"]["host"] == "glowbrand.com"
    # The stamped facts agree with what the legacy path rendered.
    assert rf["brand_mentioned_runs"] == report["attribution"]["merchant_cited_runs"]
    assert rf["runs_with_citations"] == report["attribution"]["runs_with_any_citation"]


def _payload_with_stamps():
    child_a = compute_run_facts(RUNS[:2], **IDENTITY).to_dict()
    child_b = compute_run_facts(RUNS[2:], **IDENTITY).to_dict()
    rollup = aggregate_run_facts([child_a, child_b])
    return {
        "brand_report": {
            "merchant_domain": "https://glowbrand.com",
            "run_facts": rollup,
            "per_product": [{"run_facts": child_a}, {"run_facts": child_b}],
        }
    }


def test_w7_invariant_silent_on_coherent_and_absent_stamps():
    from services.audit_invariants import check_audit_invariants

    ok = check_audit_invariants(_payload_with_stamps())
    assert not [v for v in ok.violations if v.code.startswith("RUNFACTS_")]
    # Pre-W1 payloads (no stamps anywhere) yield no RunFacts violations.
    legacy = check_audit_invariants({"brand_report": {"per_product": [{}]}})
    assert not [v for v in legacy.violations if v.code.startswith("RUNFACTS_")]


def test_w7_invariant_flags_mutated_rollup_and_overflow():
    from services.audit_invariants import check_audit_invariants

    payload = _payload_with_stamps()
    payload["brand_report"]["run_facts"]["brand_mentioned_runs"] += 5  # the 51-vs-28 class
    payload["brand_report"]["per_product"][0]["run_facts"]["own_url_cited_runs"] = 99
    report = check_audit_invariants(payload)
    codes = {v.code for v in report.violations}
    assert "RUNFACTS_ROLLUP_MISMATCH" in codes
    assert "RUNFACTS_COUNTER_OVERFLOW" in codes
    # Phase-1 posture: WARN only — a drift must never degrade a surface.
    assert not [
        v for v in report.violations
        if v.code.startswith("RUNFACTS_") and v.severity != "warn"
    ]


# ---- W1 cutover: observable parity (drift snapshot) --------------------------

def test_parity_snapshot_tracks_checks_and_drifts():
    import services.audit_facts as af
    # isolate: snapshot the baseline for our unique sites
    af.parity_check("test.site.agree", 7, 7)
    af.parity_check("test.site.agree", 7, 7)
    af.parity_check("test.site.drift", 7, 6)
    af.parity_measure("test.site.measure", ["a"], ["a", "b"])

    snap = af.parity_stats_snapshot()
    assert snap["test.site.agree"] == {
        "checks": 2, "drifts": 0, "drift_rate": 0.0, "last_drift": None,
    }
    drift = snap["test.site.drift"]
    assert drift["checks"] == 1 and drift["drifts"] == 1 and drift["drift_rate"] == 1.0
    assert drift["last_drift"] == {"legacy": "7", "run_facts": "6"}
    # parity_measure feeds the same rollup (a measure disagreement counts as drift)
    assert snap["test.site.measure"]["drifts"] == 1


def test_parity_recording_never_raises_on_bad_input():
    import services.audit_facts as af

    class _Unhashable:
        def __eq__(self, other):
            raise RuntimeError("boom")

    # parity_check swallows the comparison error; snapshot stays usable
    assert af.parity_check("test.site.bad", _Unhashable(), 1) is False
    assert isinstance(af.parity_stats_snapshot(), dict)
