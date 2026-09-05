"""The invariant the migration judgment asked for: every stage claim cites a row.

THE DEFECT THIS EXISTS FOR. `extract_findings` read `brand_report["aggregate"]`
and `["per_product"]` — the LEGACY report shape. Production writes
`brand_rollup` / `brand_verdict_label` and has neither key, so on every real run
the extractor fell through every branch, returned [], and
`build_revenue_recovery_projection` rendered that as `NO_FINDINGS` — "we checked
and found nothing".

Confirmed against a live run on 2026-09-05: get_selected and get_cited both said
NO_FINDINGS while the same audit scored the brand 1.6/10 and reported identity,
routability and content_richness all BLOCKED.

The whole suite was green because every fixture fed the legacy shape. So the
fixture here is a VERBATIM slice of a real production report
(tests/fixtures_real_per_sku_brand_report.json) — not a hand-written dict, which
is what let the defect through the first time.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.audit_evidence_builder import extract_findings
from services.audit_projection_builder import build_revenue_recovery_projection

_FIXTURE = Path(__file__).with_name("fixtures_real_per_sku_brand_report.json")


@pytest.fixture(scope="module")
def real_report():
    return json.loads(_FIXTURE.read_text())


def _project(findings):
    return build_revenue_recovery_projection(
        evidence=[], findings=findings, actions=[],
        audit_run_row={"run_id": "run-1"},
    )


# ---------------------------------------------------------------------
# The regression
# ---------------------------------------------------------------------


def test_the_production_report_shape_yields_findings(real_report):
    """The exact assertion that was missing. Against the shape production
    writes, the extractor must not come back empty."""
    findings = extract_findings(real_report)

    assert findings, "extract_findings returned [] for a REAL production report"
    types = {f["finding_type"] for f in findings}
    # The rollup called these blocked; the projection must not be silent on them.
    assert "product_identity_unresolvable" in types
    assert "content_too_thin_to_cite" in types


def test_no_stage_claims_an_all_clear_on_the_real_report(real_report):
    """The invariant: a stage may only say NO_FINDINGS when the report was read
    AND nothing was found. Here plenty was found, so nothing may claim it."""
    proj = _project(extract_findings(real_report))

    for stage in proj["stages"]:
        if stage["status"] == "NO_FINDINGS":
            pytest.fail(
                f"{stage['stage']} claimed NO_FINDINGS on a run whose rollup "
                f"reported blocked dimensions"
            )


def test_every_stage_claim_cites_a_row_that_exists_in_the_report(real_report):
    """'Every stage claim must cite a row.'

    The earlier version of this test asserted MEASURED implies non-empty
    findings — which is VACUOUS: MEASURED is assigned by `"MEASURED" if
    stages[name]["findings"] else "NO_FINDINGS"`, so no input can fail it. It
    passed against the broken build too. What actually needed pinning is that
    the cited rows trace back to the report: a stage citing a finding the
    rollup never produced is a fabricated claim, which is the same defect
    class as the false all-clear.
    """
    report_dims = set(real_report["brand_rollup"]["dimensions"])
    findings = extract_findings(real_report)
    proj = _project(findings)

    emitted = {f["finding_type"]: f for f in findings}
    cited = 0
    for stage in proj["stages"]:
        assert (stage["status"] == "MEASURED") == bool(stage["findings"])
        for f in stage["findings"]:
            assert f["type"] in emitted, (
                f"{stage['stage']} cites {f['type']}, which the extractor "
                f"never emitted for this report"
            )
            payload = emitted[f["type"]].get("payload") or {}
            assert payload.get("dimension") in report_dims, (
                f"{f['type']} cites a dimension absent from the rollup"
            )
            cited += 1
    assert cited, "no stage cited anything on a report with blocked dimensions"


def test_findings_reach_the_stage_their_meaning_belongs_to(real_report):
    """Unmapped finding types default to GET SELECTED, so forgetting to map a
    new type files every citation problem under selection — silently."""
    proj = _project(extract_findings(real_report))
    by_stage = {
        s["stage"]: {f["type"] for f in s["findings"]} for s in proj["stages"]
    }

    assert "product_identity_unresolvable" in by_stage["get_selected"]
    assert "content_too_thin_to_cite" in by_stage["get_selected"]
    assert "category_citation_weak" in by_stage["get_cited"]
    # routability is NOT convert_sales: that stage means a real purchase path
    # was exercised, and only the browser lane can say that.
    assert not by_stage["convert_sales"]


# ---------------------------------------------------------------------
# The ambiguity that caused it
# ---------------------------------------------------------------------


def test_an_unrecognised_report_is_a_finding_not_a_silence():
    """[] because nothing is wrong and [] because we could not read it are
    different facts. Only one is an all-clear."""
    findings = extract_findings({"some": "shape", "we": "do not know"})

    assert [f["finding_type"] for f in findings] == ["report_shape_unreadable"]
    assert findings[0]["severity"] == "high"


def test_an_unrecognised_report_leaves_every_stage_unverified():
    """Including the stages the finding did not land in: nothing about this
    merchant was established, so no stage may claim an all-clear."""
    proj = _project(extract_findings({"unknown": "shape"}))

    assert [s["status"] for s in proj["stages"]] == ["UNVERIFIED"] * 3

    by_stage = {s["stage"]: s for s in proj["stages"]}
    # The two stages that WOULD have been readable say why they were not read.
    for name in ("get_selected", "get_cited"):
        assert "established nothing" in by_stage[name]["unverified_reason"]
        assert "not an all-clear" in by_stage[name]["unverified_reason"]
    # convert_sales keeps its own, more specific reason — it has never run at
    # all, which is a stronger statement than "we could not read the report",
    # and replacing it would lose information.
    assert "never run against a live store" in (
        by_stage["convert_sales"]["unverified_reason"]
    )


def test_the_legacy_shape_still_works():
    """The old shape has to keep working — some runs still produce it, and a fix
    that traded one silent shape for another would be no fix."""
    legacy = {
        "aggregate": {
            "avg_visibility": 5, "avg_attribution": 3,
            "avg_category_visibility": 12, "products_succeeded": 3,
            "brand_verdict_label": "VISIBLE VIA RETAILERS",
        },
        "per_product": [],
    }
    types = {f["finding_type"] for f in extract_findings(legacy)}

    assert "merchant_visible_via_retailers_only" in types
    assert "category_visibility_low" in types
    assert "report_shape_unreadable" not in types


def test_every_producible_type_has_a_stage_and_vice_versa():
    """A type the extractor can emit but the projection cannot place lands in
    GET SELECTED by default — which is how a citation finding gets filed as a
    selection one. Keep the two tables in step."""
    from services import audit_projection_builder as b

    from services import audit_evidence_builder as e

    unmapped = (
        b._PRODUCIBLE_FINDING_TYPES
        - set(b._STAGE_FOR_FINDING_TYPE)
        - b._META_FINDING_TYPES
    )
    assert not unmapped, f"producible but unmapped to a stage: {sorted(unmapped)}"

    # The "vice versa" the name promised and the body never checked: a stage
    # mapping for a type nothing emits makes _stage_is_measurable say a stage
    # has a producer when it has none — which is how a stage claims
    # NO_FINDINGS for something that can never produce one.
    unproducible = set(b._STAGE_FOR_FINDING_TYPE) - b._PRODUCIBLE_FINDING_TYPES
    assert not unproducible, (
        f"mapped to a stage but no producer emits it: {sorted(unproducible)}"
    )

    # And the third table: every dimension the evidence builder maps must be a
    # type the projection knows. A rename on one side alone is the whole
    # failure mode here.
    assert set(e._ROLLUP_DIMENSION_FINDING.values()) <= (
        b._PRODUCIBLE_FINDING_TYPES
    )


def test_a_stage_is_never_unverified_while_carrying_findings(real_report):
    """The contradiction the producible-set gate can create.

    `_stage_is_measurable` is checked BEFORE the findings list, so a stage whose
    producers are all missing from _PRODUCIBLE_FINDING_TYPES renders UNVERIFIED
    — "nothing can be measured here" — while displaying the findings it just
    collected. Dropping a rollup type from that set is a one-line edit and left
    the whole suite green.
    """
    proj = _project(extract_findings(real_report))

    for stage in proj["stages"]:
        if stage["status"] == "UNVERIFIED":
            assert not stage["findings"], (
                f"{stage['stage']} says nothing can be measured while citing "
                f"{len(stage['findings'])} finding(s)"
            )


# ---------------------------------------------------------------------
# The headline. `headline_score` was `visibility_score_avg` — a bare integer
# with no n and no interval. Rule 1 forbids exactly that: "no stage scores.
# Distributions only. A 'CAPTURE INTENT 41%' implies a formula. Ours moves
# 5.6x on denominator choice."
# ---------------------------------------------------------------------


def test_the_revenue_recovery_surface_reports_no_bare_score(real_report):
    """The regression. A single integer here is the defect, whatever it is
    named — so this asserts the ABSENCE of the shape, not of one key."""
    out = _project(extract_findings(real_report))

    assert "headline_score" not in out
    for key, value in out.items():
        if key in ("headline", "stages", "audit_run_id", "audience",
                   "builder_version"):
            continue
        assert not isinstance(value, (int, float)) or isinstance(value, bool), (
            f"{key} is a bare number on the merchant headline surface"
        )
    assert out["headline"]["kind"] == "distribution"


def test_every_headline_number_travels_with_its_n(real_report):
    """A band over 3 SKUs is not a band over 300. Rule 2."""
    dims = _project(extract_findings(real_report))["headline"]["dimensions"]

    assert dims, "the real report banded four dimensions; the headline shows none"
    for dim in dims:
        assert dim["n"] is not None, f"{dim['dimension']} carries a band with no n"
        assert dim["median"] is not None
        # The interval, not just the point. A median with no spread is the
        # single number Rule 1 removes wearing a different name.
        assert dim["p25"] is not None and dim["p75"] is not None


def test_every_number_in_the_headline_is_traceable_to_one_row(real_report):
    """No composite. identity 16 and citation 48 average to something that
    describes neither, so no key here may hold a cross-dimension figure.

    Asserted structurally rather than by value: `round(23.25)` collides with
    content_richness's own median, so "the mean does not appear" is not a test
    a composite would fail. "Every number sits inside a dimension row or a
    split bucket" is — a new `headline_score` breaks it whatever it holds.
    """
    head = _project(extract_findings(real_report))["headline"]

    # Counts of what was measured, not measurements. Named so that adding a
    # third scalar is a decision someone has to make here.
    allowed_scalars = {"dimensions_considered", "skus_audited"}
    stray: list[str] = []

    def walk(node, path):
        if isinstance(node, bool):
            return
        if isinstance(node, (int, float)):
            head_key = path[0] if path else ""
            if head_key in ("dimensions", "prompt_split"):
                return
            if head_key in allowed_scalars and len(path) == 1:
                return
            stray.append(".".join(str(p) for p in path) + f" = {node}")
            return
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, path + [k])
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, path + [i])

    walk(head, [])
    assert not stray, f"headline carries untraceable number(s): {stray}"


def test_the_headline_shows_dimensions_that_passed_too(real_report):
    """The denominator. Findings are emitted only for blocked/partial, so a
    headline built from findings alone could not tell four-of-four from
    four-of-nine — it would show the failures and imply they were all of it."""
    report = json.loads(json.dumps(real_report))
    dims = report["brand_rollup"]["dimensions"]
    passing_key = "content_richness"
    dims[passing_key]["band"] = "strong"

    findings = extract_findings(report)
    out = build_revenue_recovery_projection(
        evidence=[], findings=findings, actions=[],
        audit_run_row={"run_id": "run-1"},
    )

    # It emits no problem finding — the merchant is not told to fix it.
    assert not any(
        f["finding_type"] == "content_too_thin_to_cite" for f in findings
    )
    for stage in out["stages"]:
        assert all(
            f["type"] != "content_too_thin_to_cite" for f in stage["findings"]
        )
    # But it still counts, and it still shows.
    head = out["headline"]
    shown = {d["dimension"]: d for d in head["dimensions"]}
    assert passing_key in shown, "a passing dimension vanished from the headline"
    assert shown[passing_key]["band"] == "strong"
    assert head["dimensions_considered"] == len(dims)


# ---------------------------------------------------------------------
# The branded/unbranded split — §6's definition of done for this surface.
# ---------------------------------------------------------------------


def test_the_branded_unbranded_split_is_present_with_n(real_report):
    split = _project(extract_findings(real_report))["headline"]["prompt_split"]

    assert split is not None, "the §6 split is missing from a report that has it"
    for side in ("branded", "unbranded"):
        bucket = split[side]
        assert bucket["answered"], f"{side} rate carries no denominator"
        assert bucket["cited"] <= bucket["answered"]
        assert bucket["rate"] == round(
            bucket["cited"] / bucket["answered"], 3,
        )
    # Comparing across a mix version change is invalid; the version must be
    # legible to whoever renders or diffs this.
    assert split["prompt_mix_version"] is not None


def test_the_split_classifies_by_the_reports_own_branded_axes(real_report):
    """Not by a list of our own. The report says which axes are branded; a
    hard-coded copy here would drift the first time #1521 is revised."""
    mix = real_report["brand_rollup"]["prompt_mix"]
    split = _project(extract_findings(real_report))["headline"]["prompt_split"]

    assert set(split["branded"]["axes"]) == set(mix["branded_axes"])
    assert set(split["unbranded"]["axes"]) == (
        set(real_report["brand_rollup"]["citation_by_intent"])
        - set(mix["branded_axes"])
    )
    assert split["unclassified_axes"] == []
    assert split["branded_axes_without_citation_data"] == []


def test_an_axis_the_report_never_scored_is_named_not_dropped(real_report):
    """A branded axis with no citation row would silently shrink the branded
    denominator and make the split look better than it is."""
    report = json.loads(json.dumps(real_report))
    branded = report["brand_rollup"]["prompt_mix"]["branded_axes"]
    report["brand_rollup"]["citation_by_intent"].pop(branded[0])

    split = _project(extract_findings(report))["headline"]["prompt_split"]

    assert branded[0] in split["branded_axes_without_citation_data"]


def test_a_missing_split_reads_as_unknown_not_as_parity(real_report):
    report = json.loads(json.dumps(real_report))
    report["brand_rollup"].pop("citation_by_intent")

    head = _project(extract_findings(report))["headline"]

    assert head["prompt_split"] is None
    assert "unknown" in head["prompt_split_unavailable_reason"].lower()
    # It must still show the dimensions it DID measure.
    assert head["dimensions"]


def test_a_run_with_no_distribution_says_so_rather_than_showing_a_clean_sheet():
    """An empty dimension list rendered as a headline reads as a clean sheet.
    That is the same false all-clear this file exists for, moved one level up."""
    head = build_revenue_recovery_projection(
        evidence=[], findings=[], actions=[], audit_run_row={"run_id": "r"},
    )["headline"]

    assert head["dimensions"] == []
    reason = head["unavailable_reason"].lower()
    assert "not a score of zero" in reason and "not a pass" in reason


def test_a_side_with_no_answered_prompts_has_no_rate(real_report):
    """0/0 is not 0%. A branded rate of 0.0 says the brand was asked for and
    never cited; `None` says it was never asked. Rendering the second as the
    first is the false zero this whole file exists to stop, moved into the
    split — and it survived a mutant until this test existed.
    """
    report = json.loads(json.dumps(real_report))
    by_intent = report["brand_rollup"]["citation_by_intent"]
    for axis in report["brand_rollup"]["prompt_mix"]["branded_axes"]:
        by_intent[axis]["cited"] = 0
        by_intent[axis]["total"] = 0

    split = _project(extract_findings(report))["headline"]["prompt_split"]

    assert split["branded"]["answered"] == 0
    assert split["branded"]["rate"] is None, "0/0 rendered as a rate"
    # The other side still reports normally — this is not a global blank.
    assert split["unbranded"]["rate"] is not None


# ---------------------------------------------------------------------
# Review findings, 2026-09-05. Each of these pins a defect the first cut of
# this fix shipped.
# ---------------------------------------------------------------------


def test_routability_is_never_a_merchant_facing_visibility_claim(real_report):
    """It measures OUR readiness, not the brand's.

    Every routability bucket reads Pivota data and only Pivota data:
    serving_eligibility (30) is our index pipeline state, offer_orderability
    (25) and price_currency_confidence (15) read our `catalog_offers`, the last
    10 read our merchant record. Not one reads a model's answer. The writer
    already demoted it for exactly this reason (_INTERNAL_STATE_GAPS, #1504) —
    "conflating 'Pivota hasn't served this' with 'brand isn't AI-visible'".

    The first cut emitted it as a HIGH finding, `destination_unroutable`, in
    the CITATION stage, with the summary "AI has no buyable offer to route a
    shopper to". On this run routability banded `blocked` at median 6 — the
    merchant would have read our own un-ingested catalog as their AI-visibility
    failure, with nothing they could do about it.
    """
    findings = extract_findings(real_report)
    routability = [
        f for f in findings
        if (f.get("payload") or {}).get("dimension") == "routability"
    ]

    assert routability, "routability stopped being measured entirely"
    for f in routability:
        assert f["severity"] == "low", (
            "routability is high-severity again; it will reach the merchant"
        )
    assert not any(
        f["finding_type"] == "destination_unroutable" for f in findings
    )

    # And it reaches no merchant stage list.
    for stage in _project(findings)["stages"]:
        for shown in stage["findings"]:
            assert shown["type"] != "pivota_serving_not_ready"

    # It is still measured and still visible in the headline, labelled for
    # what it is.
    head = _project(findings)["headline"]
    row = {d["dimension"]: d for d in head["dimensions"]}["routability"]
    assert row["measures"] == "pivota_readiness"
    assert row["band"] == "blocked"


def test_no_raw_band_enum_reaches_merchant_copy(real_report):
    """The writer's contract: the band enum is "INTERNAL vocabulary that must
    never reach a merchant as a raw token"; `band_label` exists for this. The
    first cut interpolated `dim["band"]`, so `findings_summary[].summary` on
    the merchant projection read "Identity: blocked"."""
    import services.audit_projection_builder as apb
    import db.audit_evidence as ae

    findings = extract_findings(real_report)
    merchant = apb.build_projection(
        audience=ae.AUDIENCE_MERCHANT, evidence=[], findings=findings,
        actions=[], audit_run_row={"run_id": "r"},
    )
    shown = " ".join(
        str(f["summary"]) for f in merchant["findings_summary"]
    ) + " ".join(str(f["short_summary"]) for f in findings)

    for token in ("blocked", "partial", "agent_ready", "unscored"):
        assert token not in shown.lower(), (
            f"the raw band token {token!r} reached merchant copy"
        )
    # The merchant-safe rendering is what shipped instead.
    assert "Needs work" in shown or "Not yet visible" in shown


def test_a_rollup_that_scored_nothing_is_unmeasured_not_empty(real_report):
    """The residual false all-clear. `_dimension_band(None)` is "unscored",
    which is what every dimension gets when each SKU hit missing_inputs. That
    rollup is still the modern shape, so it passed the shape check, produced no
    band finding, and rendered NO_FINDINGS — "checked, found nothing" — for a
    run that measured nothing at all."""
    report = json.loads(json.dumps(real_report))
    for dim in report["brand_rollup"]["dimensions"].values():
        dim["band"] = "unscored"

    findings = extract_findings(report)
    proj = _project(findings)

    assert any(f["finding_type"] == "no_dimension_was_scored" for f in findings)
    assert [s["status"] for s in proj["stages"]] == ["UNVERIFIED"] * 3
    by_stage = {s["stage"]: s for s in proj["stages"]}
    for name in ("get_selected", "get_cited"):
        assert "not an all-clear" in by_stage[name]["unverified_reason"]
    # convert_sales keeps its own, stronger reason — it has never run at all.
    assert "never run against a live store" in (
        by_stage["convert_sales"]["unverified_reason"]
    )


def test_a_rollup_with_no_dimensions_key_is_unmeasured_too(real_report):
    report = json.loads(json.dumps(real_report))
    report["brand_rollup"].pop("dimensions")

    proj = _project(extract_findings(report))

    assert [s["status"] for s in proj["stages"]] == ["UNVERIFIED"] * 3


def test_a_meta_finding_never_lands_in_a_stages_findings():
    """Commit 2's own invariant, violated by commit 1 and invisible to its
    test because that test only ran on the real report.
    `_stage_for("report_shape_unreadable")` falls to the get_selected default,
    severity high, so the stage rendered UNVERIFIED *while citing it*."""
    proj = _project(extract_findings({"unknown": "shape"}))

    for stage in proj["stages"]:
        assert stage["status"] == "UNVERIFIED"
        assert not stage["findings"], (
            f"{stage['stage']} is UNVERIFIED and cites {stage['findings']}"
        )


def test_a_meta_finding_is_not_sold_as_a_locked_finding():
    """public_anonymous counted it, so the paywall teaser advertised
    "1 finding waiting" for a run in which nothing was read."""
    import services.audit_projection_builder as apb
    import db.audit_evidence as ae

    findings = extract_findings({"unknown": "shape"})
    assert findings, "the meta-finding stopped being emitted"

    public = apb.build_projection(
        audience=ae.AUDIENCE_PUBLIC_ANONYMOUS, evidence=[], findings=findings,
        actions=[], audit_run_row={"run_id": "r"},
    )
    assert public["locked"]["findings"] == 0


def test_a_meta_finding_is_not_told_to_the_merchant_or_to_agents():
    """It is evidence about US. The merchant projection rendered "The findings
    extractor did not recognise this report's shape" to the store owner as a
    high-severity finding about their store, and the agent feed published it as
    a claim at confidence 85 for external agents to ingest as fact."""
    import services.audit_projection_builder as apb
    import db.audit_evidence as ae

    findings = extract_findings({"unknown": "shape"})

    merchant = apb.build_projection(
        audience=ae.AUDIENCE_MERCHANT, evidence=[], findings=findings,
        actions=[], audit_run_row={"run_id": "r"},
    )
    assert merchant["findings_summary"] == []

    feed = apb.build_projection(
        audience=ae.AUDIENCE_FRONTEND_AGENT_FEED, evidence=[],
        findings=findings, actions=[], audit_run_row={"run_id": "r"},
    )
    assert feed["claims"] == []


def test_the_builder_version_moved_so_cached_projections_are_re_rendered():
    """Every run that completed before this fix has a cached revenue_recovery
    projection holding the NO_FINDINGS all-clear and the bare headline_score —
    including the live run this whole branch was opened for. Serving those
    unchanged is the defect still shipping."""
    from services import audit_projection_builder as b

    assert b._BUILDER_VERSION != "1.0.0"
