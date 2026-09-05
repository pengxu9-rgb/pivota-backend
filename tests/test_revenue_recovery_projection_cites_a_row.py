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
    assert "destination_unroutable" in types


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


def test_every_stage_that_claims_MEASURED_cites_at_least_one_finding(real_report):
    """'Every stage claim must cite a row.' MEASURED with an empty list is a
    claim with no evidence behind it."""
    proj = _project(extract_findings(real_report))

    for stage in proj["stages"]:
        if stage["status"] == "MEASURED":
            assert stage["findings"], (
                f"{stage['stage']} is MEASURED but cites no finding"
            )


def test_findings_reach_the_stage_their_meaning_belongs_to(real_report):
    """Unmapped finding types default to GET SELECTED, so forgetting to map a
    new type files every citation problem under selection — silently."""
    proj = _project(extract_findings(real_report))
    by_stage = {
        s["stage"]: {f["type"] for f in s["findings"]} for s in proj["stages"]
    }

    assert "product_identity_unresolvable" in by_stage["get_selected"]
    assert "category_citation_weak" in by_stage["get_cited"]
    assert "destination_unroutable" in by_stage["get_cited"]
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
        assert "did not recognise" in by_stage[name]["unverified_reason"]
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

    unmapped = b._PRODUCIBLE_FINDING_TYPES - set(b._STAGE_FOR_FINDING_TYPE) - {
        b._UNREADABLE_FINDING,
    }
    assert not unmapped, f"producible but unmapped to a stage: {sorted(unmapped)}"


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
