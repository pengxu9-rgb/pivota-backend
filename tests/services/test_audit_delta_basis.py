"""audit_delta must not call a measurement change 'your movement'.

`_measurement_basis` decided comparability from the pinned prompt set ALONE,
and `build_reaudit_delta` uses that verdict to pick between
MATERIAL_SCORE_DELTA (15) and MATERIAL_SCORE_DELTA_SAME_BASIS (5). The prompt
set says WHAT was asked and nothing about which model answered, against which
official-domain set, or with what tier mix — so a model swap reported same=True
and TIGHTENED the mask to 5 points, the direction that manufactures movement.

Measured 2026-09-01: a model generation moved No-Destination 20.9% -> 0.0% and
multi-host 50% -> 86% with no merchant behaviour change at all.
"""
import services.audit_delta as ad


def _report(prompt_set_id, score=50):
    return {
        "prompt_basis": {"selected_set_id": prompt_set_id},
        "scores": {"visibility_score": score},
    }


def _basis(**over):
    base = {
        "methodology_version": "1",
        "providers_and_models": {"gemini": {"model_id": "gemini-2.5-flash"}},
        "primary_destination_version": 1,
        "prompt_set_id": "ps_1",
        "selected_set_id": "sel_1",
        "official_domains": ["brand.com"],
        "tier_mix": {"category_head": 10},
        "market": "US",
        "language": "en",
    }
    base.update(over)
    return base


def test_same_prompt_set_and_same_basis_is_a_real_comparison():
    """Positive counterpart: the tightened mask must still be reachable."""
    verdict = ad.measurement_basis_between(
        _report("sel_1"), _report("sel_1"), _basis(), _basis()
    )
    assert verdict["same"] is True


def test_a_model_swap_is_not_your_movement():
    verdict = ad.measurement_basis_between(
        _report("sel_1"), _report("sel_1"),
        _basis(providers_and_models={"gemini": {"model_id": "gemini-3-flash-preview"}}),
        _basis(),
    )
    assert verdict["same"] is False
    assert verdict.get("basis_divergence") == "measurement_basis"
    assert "prompt set changed" not in verdict["note"], (
        "the prompt set did NOT change — saying so would be a false explanation"
    )


def test_an_added_official_domain_is_not_your_movement():
    """The case that moves 'AI sends buyers to your own store' by config alone."""
    verdict = ad.measurement_basis_between(
        _report("sel_1"), _report("sel_1"),
        _basis(official_domains=["brand.com", "brand.co.uk"]), _basis(),
    )
    assert verdict["same"] is False


def test_a_changed_tier_mix_is_not_your_movement():
    verdict = ad.measurement_basis_between(
        _report("sel_1"), _report("sel_1"),
        _basis(tier_mix={"trust": 10}), _basis(),
    )
    assert verdict["same"] is False


def test_absent_bases_are_unknown():
    """Runs predating audit_basis carry no evidence of a model change either
    way. Failing them closed would silently desensitise every merchant's next
    re-audit, so the change is strictly additive."""
    assert ad.measurement_basis_between(_report("sel_1"), _report("sel_1"))["same"] is None
    assert ad.measurement_basis_between(_report("sel_1"), _report("sel_2"))["same"] is False


def test_one_sided_basis_is_unknown():
    assert ad.measurement_basis_between(
        _report("sel_1"), _report("sel_1"), _basis(), None
    )["same"] is None


def test_a_different_prompt_set_still_wins_and_keeps_its_own_note():
    """A prompt-set change must not be relabelled as a basis divergence."""
    verdict = ad.measurement_basis_between(
        _report("sel_1"), _report("sel_2"), _basis(), _basis(selected_set_id="sel_2")
    )
    assert verdict["same"] is False
    assert "prompt set changed" in verdict["note"]
    assert "basis_divergence" not in verdict


def test_the_threshold_actually_loosens_when_the_basis_diverges():
    """The verdict is only useful if build_reaudit_delta acts on it: a 6-point
    move is material at 5 and immaterial at 15."""
    cur, prior = _report("sel_1", score=56), _report("sel_1", score=50)
    same = ad.build_reaudit_delta(
        current_report=cur, prior_report=prior, prior_row=None, days_since=30,
        current_basis=_basis(), prior_basis=_basis(),
    )
    swapped = ad.build_reaudit_delta(
        current_report=cur, prior_report=prior, prior_row=None, days_since=30,
        current_basis=_basis(providers_and_models={"gemini": {"model_id": "x"}}),
        prior_basis=_basis(),
    )
    assert same["measurement_basis"]["same"] is True
    assert swapped["measurement_basis"]["same"] is False
    assert len(swapped["movements"]) <= len(same["movements"]), (
        "a diverged basis must not report MORE movement than a matched one"
    )


# ---------------------------------------------------------------------------
# The WIRING. Everything above tests the verdict; none of it tests that the
# caller actually hands the bases over. A mutant making _basis_pair_for_delta
# return (None, None) left 166 tests green — the fix would have shipped inert,
# which is exactly the hole this workstream keeps finding.
# ---------------------------------------------------------------------------
import services.agent_center_bd_report_service as acbd  # noqa: E402


async def test_the_caller_supplies_both_bases(monkeypatch):
    prior_row = {"methodology_version": "1", "providers_and_models": {}}

    async def _get_basis(run_id):
        assert run_id == "prior-run"
        return prior_row

    async def _build(*, audit_run_id, brand_report, merchant_id, persist):
        assert persist is False, (
            "the CURRENT run's basis must be built in memory — its row is "
            "written later, by the worker, so a read would always be None"
        )
        return {
            "methodology_version": "1",
            "providers_and_models": {"gemini": {"model_id": "g"}},
            "selected_set_id": "sel_current",
        }

    import db.audit_basis as ab
    import services.audit_evidence_builder as eb
    monkeypatch.setattr(ab, "get_basis_for_run", _get_basis)
    monkeypatch.setattr(eb, "record_audit_basis", _build)

    current, prior = await acbd._basis_pair_for_delta({}, "m1", "prior-run")
    assert prior is prior_row
    # Distinguishable on purpose: a mutant returning (prior, prior) — comparing
    # the prior run against ITSELF, so `same` is always True and the feature is
    # inert — satisfied the old `current is not None` assertion.
    assert current is not prior_row
    assert current["selected_set_id"] == "sel_current"


async def test_a_prior_run_with_no_basis_falls_through(monkeypatch):
    """Runs predating audit_basis must keep today's verdict, not fail closed."""
    import db.audit_basis as ab

    async def _none(run_id):
        return None

    monkeypatch.setattr(ab, "get_basis_for_run", _none)
    assert await acbd._basis_pair_for_delta({}, "m1", "prior-run") == (None, None)


async def test_missing_ids_fall_through():
    assert await acbd._basis_pair_for_delta({}, None, "prior-run") == (None, None)
    assert await acbd._basis_pair_for_delta({}, "m1", None) == (None, None)


async def test_a_basis_lookup_failure_never_sinks_the_audit(monkeypatch):
    import db.audit_basis as ab

    async def _boom(run_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(ab, "get_basis_for_run", _boom)
    assert await acbd._basis_pair_for_delta({}, "m1", "prior-run") == (None, None)


# ---------------------------------------------------------------------------
# The two blockers review found, each with the test that would have caught it.
# ---------------------------------------------------------------------------
async def test_the_comparability_path_can_build_a_basis_without_a_run_id():
    """BLOCKER 1. record_audit_basis guarded `if not audit_run_id ... return
    None` — above the persist=False branch — and the comparability path calls it
    with "" by design, because the current run has no row to write under yet. So
    the current basis was ALWAYS None and the whole feature was inert while
    every test passed."""
    import services.audit_evidence_builder as eb

    payload = await eb.record_audit_basis(
        audit_run_id="", brand_report={}, merchant_id="m1", persist=False,
    )
    assert payload is not None, "an empty run id must not defeat the build-only path"
    assert payload["methodology_version"]


async def test_persisting_still_requires_a_run_id():
    """Positive counterpart: relaxing the guard must not let a WRITE through
    without the id it writes under."""
    import services.audit_evidence_builder as eb

    assert await eb.record_audit_basis(
        audit_run_id="", brand_report={}, merchant_id="m1", persist=True,
    ) is None


async def test_a_run_is_comparable_with_its_own_stored_basis():
    """BLOCKER 2. The current basis is built in memory; the prior one is read
    back from the DB. If those shapes do not compare equal, the check fires on
    EVERY re-audit and every merchant is told their basis changed — the same
    defect as the bug being fixed, pointing the other way.

    official_domains was the live vector: record_basis stores sorted({lower}),
    the in-memory path returned list_official_domains order verbatim, and that
    query has no ORDER BY.
    """
    import db.audit_basis as ab

    await ab.ensure_audit_basis_table()
    payload = {
        "providers_and_models": {"gemini": {"model_id": "gemini-2.5-flash"}},
        "prompt_set_id": "ps_rt", "selected_set_id": "sel_rt",
        "tier_mix": {"category_head": 2},
        "official_domains": ["shop.anua.com", "anua.com"],  # NOT alphabetical
        "primary_destination_version": 1,
        "market": "US", "language": "en", "currency": None,
    }
    await ab.record_basis(audit_run_id="rt-self", merchant_id="m1", **payload)
    stored = await ab.get_basis_for_run("rt-self")
    in_memory = dict(payload, methodology_version=ab.METHODOLOGY_VERSION)

    assert ab.bases_are_comparable(in_memory, stored) is True, (
        "a run must be comparable with its own stored basis"
    )


async def test_a_genuinely_different_domain_set_is_still_not_comparable():
    """Positive counterpart: order-insensitivity must not blunt the check."""
    import db.audit_basis as ab

    base = {
        "methodology_version": ab.METHODOLOGY_VERSION,
        "providers_and_models": {"gemini": {"model_id": "g"}},
        "primary_destination_version": 1,
        "prompt_set_id": "p", "selected_set_id": "s",
        "official_domains": ["a.com", "b.com"],
        "tier_mix": {"category_head": 1}, "market": "US", "language": "en",
    }
    assert ab.bases_are_comparable(base, dict(base, official_domains=["a.com"])) is False


async def test_the_built_payload_normalises_domains_like_the_writer_does(monkeypatch):
    """Both halves of the ordering fix are pinned, not just the comparison one.

    bases_are_comparable now sorts lists, so it compensates for an unnormalised
    payload — which means a mutant dropping the normalisation HERE survives
    unless the payload shape is asserted directly. Two writers emitting the same
    set in different shapes is the defect; keeping them identical at the source
    is the fix, and the sort in the comparison is the belt.
    """
    import services.audit_evidence_builder as eb
    import db.merchant_official_domains as mod

    async def _domains(merchant_id):
        return [
            {"domain": "shop.anua.com", "liveness_status": "live"},
            {"domain": "ANUA.com", "liveness_status": "unchecked"},
            {"domain": "anua.us", "liveness_status": "dead"},  # excluded
        ]

    monkeypatch.setattr(mod, "list_official_domains", _domains)
    payload = await eb.record_audit_basis(
        audit_run_id="", brand_report={}, merchant_id="m1", persist=False,
    )
    assert payload["official_domains"] == ["anua.com", "shop.anua.com"], (
        "sorted, lower-cased, dead excluded — exactly what record_basis stores"
    )


# ---------------------------------------------------------------------------
# NULL == NULL is not evidence of the same pinned set.
# ---------------------------------------------------------------------------


def _complete_basis(**overrides):
    basis = {
        "methodology_version": "2",
        "providers_and_models": {"gemini": {"model_id": "g"}},
        "primary_destination_version": 1,
        "prompt_set_id": "p",
        "selected_set_id": "s",
        "official_domains": ["brand.com"],
        "tier_mix": {"category": 10},
        "market": "US",
        "language": "en",
    }
    basis.update(overrides)
    return basis


def test_two_bases_with_no_set_identity_at_all_are_not_comparable():
    """The set identity is the PAIR (selected_set_id, prompt_set_id) and
    `audit_delta._basis_id` reads the stronger of the two, so "we know which
    set was probed" means at least one is present. With neither, two bases
    matched NULL against NULL on both fields and came back comparable on no
    evidence. audit_delta gates this today, but that gate lives in another
    module and this function's docstring already promised False."""
    from db.audit_basis import bases_are_comparable

    blank = _complete_basis(prompt_set_id=None, selected_set_id=None)
    assert bases_are_comparable(blank, dict(blank)) is False
    assert bases_are_comparable(
        _complete_basis(prompt_set_id="", selected_set_id=""),
        _complete_basis(prompt_set_id="", selected_set_id=""),
    ) is False


def test_a_pre_w2_1_run_carrying_only_a_prompt_set_id_is_still_comparable():
    """`_pinned_set_ids` records the id it has on purpose: a run predating
    W2.1 has a prompt_set_id and no selected_set_id. Requiring BOTH would make
    every such run permanently non-comparable."""
    from db.audit_basis import bases_are_comparable

    w2 = _complete_basis(selected_set_id=None)
    assert bases_are_comparable(w2, dict(w2)) is True
    w2_1 = _complete_basis(prompt_set_id=None)
    assert bases_are_comparable(w2_1, dict(w2_1)) is True


async def test_the_writer_records_the_set_id_the_reader_resolves(monkeypatch):
    """THE ASYMMETRY THAT MADE THE ABOVE REACHABLE. `_pinned_set_ids` read only
    `per_sku_reports` rows while `audit_delta._prompt_set_id` also accepts a
    root-level `prompt_basis` (the legacy build_structured_report shape) and
    `per_product` rows — so for those shapes the writer stored NULL ids for a
    run whose id the delta could read straight off the same report.

    THE ids ARE DISTINCT PER SHAPE, and the last two reports carry TWO blocks
    each. The first cut of this test put the SAME id in every position, so it
    could only catch a writer that found NOTHING — a writer that found the
    WRONG block passed it. Both sides then had a precedence, they were written
    independently, and they disagreed: on a report carrying a root
    `prompt_basis` beside nested per-SKU rows the writer recorded the nested
    id into an immutable basis row while the delta resolved the root's. Now
    both come from `audit_delta.prompt_basis_blocks`, and a reversal in either
    one moves the id the other reports.
    """
    import services.audit_evidence_builder as eb
    import db.merchant_official_domains as mod
    from services.audit_delta import measurement_basis_between

    async def _domains(merchant_id):
        return []

    monkeypatch.setattr(mod, "list_official_domains", _domains)

    def _block(set_id):
        return {"prompt_basis": {"selected_set_id": set_id}}

    shapes = {
        # One block, one answer: the writer must find each shape at all.
        "root": ({"prompt_basis": {"selected_set_id": "sel_root_only"}},
                 "sel_root_only"),
        "per_product": ({"per_product": [_block("sel_per_product")]},
                        "sel_per_product"),
        "per_sku_reports": ({"per_sku_reports": [_block("sel_per_sku")]},
                            "sel_per_sku"),
        "nested": ({"brand_report": {"per_sku_reports": [_block("sel_nested")]}},
                   "sel_nested"),
        # TWO blocks, one answer. The primary report is the report ITSELF
        # (it carries no rows of its own), so its root block wins over the
        # rows hanging off the nested brand_report.
        "root_beside_nested_rows": (
            {"prompt_basis": {"selected_set_id": "sel_the_root"},
             "brand_report": {"per_sku_reports": [_block("sel_a_nested_row")]}},
            "sel_the_root",
        ),
        # ...and the other way round: when the top-level report DOES carry
        # rows, the first row is the primary report and its block wins over
        # the root block beside it.
        "rows_beside_root": (
            {"prompt_basis": {"selected_set_id": "sel_the_root"},
             "per_sku_reports": [_block("sel_the_first_row")]},
            "sel_the_first_row",
        ),
    }
    for name, (report, expected) in shapes.items():
        payload = await eb.record_audit_basis(
            audit_run_id="", brand_report=report, merchant_id="m1",
            persist=False,
        )
        assert payload["selected_set_id"] == expected, name
        # ...and it is the SAME id the delta side resolves for that report.
        assert measurement_basis_between(
            report, report, payload, payload,
        )["prompt_set_id"] == expected, name
