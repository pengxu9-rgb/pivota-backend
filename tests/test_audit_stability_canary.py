"""W7 stability canary — the pure same-basis |Δ| check."""

from __future__ import annotations

import services.audit_stability_canary as sc


from tests.basis_fixtures import with_provider_models, writer_basis  # noqa: E402


def _report(*, visibility, attribution, category_visibility, basis_id="sel_abc"):
    """Minimal report shape audit_delta._scores / _measurement_basis read.

    `provider_models` rides along because `providers_and_models` is an
    evidence-required comparability component: a basis built from a report
    without it cannot support any comparison, so the canary would read every
    pair as not-comparable for a reason unrelated to stability.
    """
    return with_provider_models({
        "prompt_basis": {"selected_set_id": basis_id} if basis_id else {},
        "verdict": {
            "visibility_score": visibility,
            "attribution_score": attribution,
            "category_visibility_score": category_visibility,
        },
        "merchant_view": {
            "headline": {
                "scores": {
                    "visibility": visibility,
                    "attribution": attribution,
                    "category_visibility": category_visibility,
                }
            }
        },
    })


async def _same_basis():
    """One writer-built basis payload, shared by both runs of a pair.

    Two runs measured the same way have the same recorded basis; the canary's
    whole subject is the residual delta when nothing about the measurement
    moved, so the basis is held identical and the SCORES are the only variable.
    """
    return await writer_basis(_report(visibility=0, attribution=0, category_visibility=0))


async def test_same_basis_within_tolerance_is_stable(monkeypatch):
    monkeypatch.setattr(sc, "STABILITY_TOLERANCE", 5)
    basis = await _same_basis()
    cur = _report(visibility=62, attribution=48, category_visibility=55)
    pri = _report(visibility=60, attribution=45, category_visibility=57)
    res = sc.stability_delta(cur, pri, current_basis=basis, prior_basis=basis)
    assert res is not None
    assert res["max_delta"] == 3
    assert res["breach"] is False


async def test_same_basis_beyond_tolerance_breaches(monkeypatch):
    monkeypatch.setattr(sc, "STABILITY_TOLERANCE", 5)
    basis = await _same_basis()
    cur = _report(visibility=80, attribution=48, category_visibility=55)
    pri = _report(visibility=60, attribution=45, category_visibility=57)
    res = sc.stability_delta(cur, pri, current_basis=basis, prior_basis=basis)
    assert res["max_delta"] == 20         # visibility 60->80
    assert res["breach"] is True
    assert res["deltas"]["visibility"] == 20


async def test_a_diverged_run_basis_is_not_a_canary_pair():
    """The pinned prompt set is necessary, not sufficient.

    Same questions, same 20-point residual — but the model changed between the
    two runs, which explains the delta. Paging on it would be a false alarm
    about our own measurement stability, so the pair is not comparable at all.
    """
    basis = await _same_basis()
    swapped = dict(basis, providers_and_models={
        "gemini": {"model_id": "gemini-3-flash-preview", "temperature": None}
    })
    cur = _report(visibility=80, attribution=48, category_visibility=55)
    pri = _report(visibility=60, attribution=45, category_visibility=57)
    assert sc.stability_delta(cur, pri, current_basis=swapped, prior_basis=basis) is None


def test_a_missing_run_basis_is_not_a_canary_pair():
    """And the shape that made this canary inert: no recorded bases at all.

    Identical pinned set, 20-point residual, nothing to establish the two runs
    were measured the same way → None. This is the reason `run_stability_canary`
    MUST fetch the rows; see test_the_scan_supplies_the_recorded_bases.
    """
    cur = _report(visibility=80, attribution=48, category_visibility=55)
    pri = _report(visibility=60, attribution=45, category_visibility=57)
    assert sc.stability_delta(cur, pri) is None


def test_different_basis_is_not_comparable():
    # Different pinned set → not a like-for-like comparison → None (never a false
    # "unstable" from measuring different questions).
    cur = _report(visibility=80, attribution=48, category_visibility=55, basis_id="sel_new")
    pri = _report(visibility=60, attribution=45, category_visibility=57, basis_id="sel_old")
    assert sc.stability_delta(cur, pri) is None


def test_unknown_basis_is_not_comparable():
    cur = _report(visibility=80, attribution=48, category_visibility=55, basis_id=None)
    pri = _report(visibility=60, attribution=45, category_visibility=57, basis_id=None)
    assert sc.stability_delta(cur, pri) is None


def test_configured_merchants_parsing(monkeypatch):
    monkeypatch.setenv("AUDIT_STABILITY_CANARY_MERCHANTS", " m1 , m2 ,")
    assert sc._configured_merchants() == ["m1", "m2"]
    monkeypatch.setenv("AUDIT_STABILITY_CANARY_MERCHANTS", "")
    assert sc._configured_merchants() == []


# --- auto-detect grouping + window gate (_stability_pairs) ----------------------

from datetime import datetime, timedelta, timezone  # noqa: E402

_T0 = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)


def _row(merchant_id, run_id, hours_ago, **scores):
    return {
        "merchant_id": merchant_id,
        "run_id": run_id,
        "requested_at": _T0 - timedelta(hours=hours_ago),
        "report_jsonb": _report(**scores),
    }


async def _bases_for(rows):
    """`{run_id: basis}` for every row — one shared writer-built payload, i.e.
    every run measured the same way. `_bases_by_run_id` returns this shape."""
    basis = await _same_basis()
    return {r["run_id"]: basis for r in rows}


async def test_pairs_auto_detects_all_merchants(monkeypatch):
    monkeypatch.setattr(sc, "STABILITY_TOLERANCE", 5)
    monkeypatch.setattr(sc, "STABILITY_WINDOW_HOURS", 48)
    rows = [
        # m1: stable pair (Δ3) within window
        _row("m1", "r1b", 1, visibility=63, attribution=45, category_visibility=55),
        _row("m1", "r1a", 5, visibility=60, attribution=45, category_visibility=57),
        # m2: breach pair (Δ20) within window
        _row("m2", "r2b", 2, visibility=80, attribution=45, category_visibility=55),
        _row("m2", "r2a", 6, visibility=60, attribution=45, category_visibility=55),
    ]
    out = {r["merchant_id"]: r
           for r in sc._stability_pairs(rows, set(), await _bases_for(rows))}
    assert out["m1"]["status"] == "stable"
    assert out["m2"]["status"] == "breach" and out["m2"]["max_delta"] == 20


def test_pairs_without_bases_yield_nothing():
    """The inert shape, pinned. `_stability_pairs` given no bases must produce
    an EMPTY result, not a silently-comparable one — so a caller that forgets
    the fetch fails loudly here rather than muting production alerting."""
    rows = [
        _row("m1", "r1b", 1, visibility=80, attribution=45, category_visibility=55),
        _row("m1", "r1a", 5, visibility=60, attribution=45, category_visibility=55),
    ]
    assert sc._stability_pairs(rows, set()) == []


async def test_the_scan_supplies_the_recorded_bases(monkeypatch):
    """THE WIRING. Every negative test in this file stays green when the canary
    compares nothing, so the scan's basis fetch needs its own positive: a mutant
    dropping `bases` from the `_stability_pairs` call, or returning `{}` from
    `_bases_by_run_id`, must break something.
    """
    monkeypatch.setattr(sc, "STABILITY_TOLERANCE", 5)
    monkeypatch.setattr(sc, "STABILITY_WINDOW_HOURS", 48)
    rows = [
        _row("m1", "r1b", 1, visibility=80, attribution=45, category_visibility=55),
        _row("m1", "r1a", 5, visibility=60, attribution=45, category_visibility=55),
    ]
    basis = await _same_basis()
    asked = []

    async def fake_get_basis(run_id):
        asked.append(run_id)
        return basis

    import db.audit_basis as ab
    import db.merchant_audit_runs as mar
    monkeypatch.setattr(ab, "get_basis_for_run", fake_get_basis)

    async def fake_rows(*, window_seconds):
        return rows

    monkeypatch.setattr(mar, "completed_runs_in_window", fake_rows)
    monkeypatch.setattr(sc, "_configured_merchants", lambda: [])

    # The breach path alerts; keep it off the DB and off Sentry.
    import services.agent_anomaly_detector as aad

    async def fake_alert(**kwargs):
        return None

    monkeypatch.setattr(aad, "create_alert", fake_alert)

    out = await sc.run_stability_canary()
    assert sorted(asked) == ["r1a", "r1b"], (
        "the scan must read the basis of BOTH runs it is about to pair"
    )
    assert out["pairs_evaluated"] == 1 and out["breaches"] == 1, (
        "the fetched bases must reach _stability_pairs; without them the pair "
        "is not comparable and the canary reports nothing at all"
    )


async def test_pairs_skips_time_separated_same_basis(monkeypatch):
    # Same basis, big delta, but 100h apart (> 48h window) → real trend, not a canary
    # breach → skipped entirely. Bases ARE supplied, so the skip can only be the
    # window (an empty result proves nothing when nothing was comparable anyway).
    monkeypatch.setattr(sc, "STABILITY_WINDOW_HOURS", 48)
    rows = [
        _row("m1", "b", 1, visibility=80, attribution=45, category_visibility=55),
        _row("m1", "a", 101, visibility=60, attribution=45, category_visibility=55),
    ]
    assert sc._stability_pairs(rows, set(), await _bases_for(rows)) == []


async def test_pairs_skips_single_run_and_different_basis(monkeypatch):
    monkeypatch.setattr(sc, "STABILITY_WINDOW_HOURS", 48)
    rows = [
        _row("solo", "x", 1, visibility=60, attribution=45, category_visibility=55),
        # different basis pair → not comparable
        {"merchant_id": "m2", "run_id": "p", "requested_at": _T0 - timedelta(hours=1),
         "report_jsonb": _report(visibility=80, attribution=45, category_visibility=55, basis_id="sel_new")},
        {"merchant_id": "m2", "run_id": "q", "requested_at": _T0 - timedelta(hours=2),
         "report_jsonb": _report(visibility=60, attribution=45, category_visibility=55, basis_id="sel_old")},
    ]
    assert sc._stability_pairs(rows, set(), await _bases_for(rows)) == []


async def test_pairs_allowlist_narrows(monkeypatch):
    monkeypatch.setattr(sc, "STABILITY_WINDOW_HOURS", 48)
    rows = [
        _row("m1", "b1", 1, visibility=80, attribution=45, category_visibility=55),
        _row("m1", "a1", 3, visibility=60, attribution=45, category_visibility=55),
        _row("m2", "b2", 1, visibility=80, attribution=45, category_visibility=55),
        _row("m2", "a2", 3, visibility=60, attribution=45, category_visibility=55),
    ]
    out = sc._stability_pairs(rows, {"m2"}, await _bases_for(rows))
    assert [r["merchant_id"] for r in out] == ["m2"]


def test_candidate_run_ids_are_bounded_to_the_two_most_recent(monkeypatch):
    """The basis fetch is one query per id, so it must not walk the window.
    A merchant with a single run yields nothing: it can form no pair."""
    rows = [
        _row("m1", "b", 1, visibility=80, attribution=45, category_visibility=55),
        _row("m1", "a", 3, visibility=60, attribution=45, category_visibility=55),
        _row("m1", "older", 5, visibility=60, attribution=45, category_visibility=55),
        _row("solo", "x", 1, visibility=60, attribution=45, category_visibility=55),
    ]
    assert sc._candidate_run_ids(rows, set()) == ["b", "a"]
    assert sc._candidate_run_ids(rows, {"solo"}) == []
