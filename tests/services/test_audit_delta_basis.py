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


def test_absent_bases_fall_through_to_todays_verdict():
    """Runs predating audit_basis carry no evidence of a model change either
    way. Failing them closed would silently desensitise every merchant's next
    re-audit, so the change is strictly additive."""
    assert ad.measurement_basis_between(_report("sel_1"), _report("sel_1"))["same"] is True
    assert ad.measurement_basis_between(_report("sel_1"), _report("sel_2"))["same"] is False


def test_one_sided_basis_also_falls_through():
    assert ad.measurement_basis_between(
        _report("sel_1"), _report("sel_1"), _basis(), None
    )["same"] is True


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
        return {"methodology_version": "1", "providers_and_models": {}}

    import db.audit_basis as ab
    import services.audit_evidence_builder as eb
    monkeypatch.setattr(ab, "get_basis_for_run", _get_basis)
    monkeypatch.setattr(eb, "record_audit_basis", _build)

    current, prior = await acbd._basis_pair_for_delta({}, "m1", "prior-run")
    assert current is not None and prior is prior_row


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
