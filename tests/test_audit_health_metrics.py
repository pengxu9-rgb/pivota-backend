"""W7 audit-health metrics — the pure folding + breach logic."""

from __future__ import annotations

import services.audit_health_metrics as ahm


def _report(*outcomes: str) -> dict:
    """A report_jsonb with one per-SKU next_best_action.brief_debug per outcome."""
    return {
        "report_jsonb": {
            "per_sku_reports": [
                {"next_best_action": {"brief_debug": {"outcome": o}}}
                for o in outcomes
            ]
        }
    }


def test_walk_finds_outcomes_in_brand_and_single_shapes() -> None:
    brand = _report("llm", "unavailable_llm_error")
    single = {"report_jsonb": {"next_best_action": {"brief_debug": {"outcome": "llm"}}}}
    rates = ahm.brief_outcome_rates([brand, single])
    assert rates["brief_attempts"] == 3
    assert rates["honest_failures"] == 1
    assert rates["honest_failure_rate"] == round(1 / 3, 4)


def test_not_attempted_states_excluded_from_denominator() -> None:
    # none_disabled / none_no_key are "never ran", not failures.
    rates = ahm.brief_outcome_rates([_report("none_disabled", "none_no_key", "llm")])
    assert rates["brief_attempts"] == 1        # only the llm one was attempted
    assert rates["honest_failures"] == 0
    assert rates["honest_failure_rate"] == 0.0


def test_attach_exception_counts_as_honest_failure() -> None:
    rates = ahm.brief_outcome_rates([_report("llm", "attach_exception", "unavailable_after_rejects")])
    assert rates["brief_attempts"] == 3
    assert rates["honest_failures"] == 2


def test_no_attempts_yields_none_rate() -> None:
    rates = ahm.brief_outcome_rates([_report("none_disabled")])
    assert rates["brief_attempts"] == 0
    assert rates["honest_failure_rate"] is None


def test_breach_run_failure_rate_above_threshold(monkeypatch) -> None:
    monkeypatch.setattr(ahm, "MIN_OBSERVATIONS", 10)
    monkeypatch.setattr(ahm, "RUN_FAILURE_RATE_ALERT", 0.30)
    breaches = ahm.evaluate_breaches({
        "total_runs": 50, "run_failure_rate": 0.4,
        "brief_attempts": 0, "honest_failure_rate": None,
    })
    assert [b["metric"] for b in breaches] == ["run_failure_rate"]


def test_no_breach_below_observation_floor(monkeypatch) -> None:
    monkeypatch.setattr(ahm, "MIN_OBSERVATIONS", 10)
    # 100% failure but only 3 runs — too few to page.
    breaches = ahm.evaluate_breaches({
        "total_runs": 3, "run_failure_rate": 1.0,
        "brief_attempts": 3, "honest_failure_rate": 1.0,
    })
    assert breaches == []


def test_breach_honest_failure_rate(monkeypatch) -> None:
    monkeypatch.setattr(ahm, "MIN_OBSERVATIONS", 10)
    monkeypatch.setattr(ahm, "HONEST_FAILURE_RATE_ALERT", 0.20)
    breaches = ahm.evaluate_breaches({
        "total_runs": 0, "run_failure_rate": None,
        "brief_attempts": 40, "honest_failure_rate": 0.35,
    })
    assert [b["metric"] for b in breaches] == ["honest_failure_rate"]
