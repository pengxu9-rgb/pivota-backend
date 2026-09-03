"""GET /api/merchant-center/audit/funnel-checks — what a claim finally buys.

Claiming a funnel run used to be an ownership stamp with nothing behind it:
the public read stops answering once a run has an owner, and
recent_runs_for_merchant excludes this lane because it feeds trends and
history that a score-less run would corrupt. A merchant claimed their check
and could then see it nowhere. This endpoint is the surface that makes the
claim mean something.

It reports the SAME deterministic tier the visitor saw before registering.
The owner sees no more than the anonymous visitor did, which is correct —
nothing else was ever measured for a funnel run.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.store_audit_public_intake as sap
from utils.auth import get_current_merchant


def _now():
    return datetime.now(timezone.utc)


def _row(*, run_id: str, merchant_id: str, domain: str, claimed_ago_h: int = 1):
    return {
        "run_id": run_id,
        "merchant_id": merchant_id,
        "subject_type": "public_funnel",
        "status": "succeeded",
        "requested_at": _now() - timedelta(hours=claimed_ago_h + 1),
        "merchant_claimed_at": _now() - timedelta(hours=claimed_ago_h),
        "partial_result_jsonb": {"funnel": {"domain": domain}},
    }


@pytest.fixture()
def wired(monkeypatch):
    state: Dict[str, Any] = {"rows": [], "evidence": {}, "asked_for": []}

    async def fake_rows(*, merchant_id: str, limit: int = 10):
        state["asked_for"].append(merchant_id)
        return [r for r in state["rows"] if r["merchant_id"] == merchant_id]

    async def fake_evidence(*, audit_run_id: str):
        return state["evidence"].get(audit_run_id, [])

    import db.merchant_audit_runs as mar
    import db.audit_evidence as ae
    monkeypatch.setattr(mar, "list_claimed_funnel_runs_for_merchant", fake_rows)
    monkeypatch.setattr(ae, "list_evidence_for_run", fake_evidence)
    monkeypatch.setattr(sap, "_enabled", lambda: True)
    return state


def _client(merchant_id: str = "m-1") -> TestClient:
    app = FastAPI()
    app.include_router(sap.claim_router)
    app.dependency_overrides[get_current_merchant] = lambda: merchant_id
    return TestClient(app)


def test_a_claimed_check_is_visible_to_its_owner(wired):
    """The whole point. Before this, a claimed run was readable nowhere."""
    wired["rows"] = [_row(run_id="r-1", merchant_id="m-1", domain="anua.com")]
    wired["evidence"]["r-1"] = [
        {"evidence_type": "acceptance_signal", "evidence_level": "tested",
         "payload_jsonb": {"probe_id": "SECRET-PROBE"}},
    ]
    body = _client().get("/api/merchant-center/audit/funnel-checks").json()
    assert len(body["checks"]) == 1
    check = body["checks"][0]
    assert check["audit_run_id"] == "r-1"
    assert check["domain"] == "anua.com"
    # DISTINCT values, not just truthy: asserting both are set let a mutant
    # that swaps checked_at and claimed_at pass. The check happened BEFORE the
    # claim, always — the run exists before anyone can own it.
    assert check["checked_at"] < check["claimed_at"]
    assert check["observed_signals"] == [
        {"signal": "acceptance_signal", "evidence_level": "tested"}
    ]


def test_it_never_republishes_the_evidence_payload(wired):
    """Being allowed to report a signal is not being allowed to reprint it —
    payloads carry endpoint URLs and probe ids."""
    wired["rows"] = [_row(run_id="r-1", merchant_id="m-1", domain="anua.com")]
    wired["evidence"]["r-1"] = [
        {"evidence_type": "acceptance_signal", "evidence_level": "tested",
         "payload_jsonb": {"probe_id": "SECRET-PROBE",
                           "signal": {"endpoint": "SECRET-ENDPOINT"}}},
    ]
    raw = _client().get("/api/merchant-center/audit/funnel-checks").text
    assert "SECRET-PROBE" not in raw and "SECRET-ENDPOINT" not in raw


def test_model_derived_evidence_never_appears(wired):
    """The deterministic tier is the whole contract. A funnel run should never
    hold grounding output, but the filter is what makes that a guarantee
    rather than an assumption about who writes what."""
    wired["rows"] = [_row(run_id="r-1", merchant_id="m-1", domain="anua.com")]
    wired["evidence"]["r-1"] = [
        {"evidence_type": "acceptance_signal", "evidence_level": "tested",
         "payload_jsonb": {}},
        {"evidence_type": "grounding_chunk", "evidence_level": "tested",
         "payload_jsonb": {"excerpt_text": "SECRET-EXCERPT"}},
        {"evidence_type": "competitor_mention", "payload_jsonb": {
            "competitor": "SECRET-RIVAL"}},
        {"evidence_type": "some_future_type", "evidence_level": "tested",
         "payload_jsonb": {"x": "SECRET-FUTURE"}},
    ]
    body = _client().get("/api/merchant-center/audit/funnel-checks")
    assert "SECRET-EXCERPT" not in body.text
    assert "SECRET-RIVAL" not in body.text
    assert "SECRET-FUTURE" not in body.text
    assert [s["signal"] for s in body.json()["checks"][0]["observed_signals"]] == [
        "acceptance_signal"
    ]


def test_the_allowlist_is_the_projection_builders_own(wired):
    """Imported, not restated. Two copies of an evidence allowlist drift, and
    the drift is silent because both look reviewed."""
    import inspect
    from services.audit_projection_builder import _DETERMINISTIC_EVIDENCE_TYPES

    src = inspect.getsource(sap.list_claimed_funnel_checks)
    assert "_DETERMINISTIC_EVIDENCE_TYPES" in src
    assert "acceptance_signal" not in src.split('"""')[2], (
        "the allowlist must be imported, not re-listed in this route"
    )
    assert "acceptance_signal" in _DETERMINISTIC_EVIDENCE_TYPES


def test_one_merchant_never_sees_anothers_check(wired):
    """The reader is merchant-scoped in SQL; this pins that the route passes
    the AUTHENTICATED id and not something from the request."""
    wired["rows"] = [
        _row(run_id="r-mine", merchant_id="m-1", domain="mine.com"),
        _row(run_id="r-theirs", merchant_id="m-2", domain="theirs.com"),
    ]
    body = _client("m-1").get("/api/merchant-center/audit/funnel-checks").json()
    assert [c["audit_run_id"] for c in body["checks"]] == ["r-mine"]
    assert "theirs.com" not in str(body)
    assert wired["asked_for"] == ["m-1"]


def test_a_check_with_no_evidence_yet_still_appears(wired):
    """The probe drains on a ~5-minute cadence, so a freshly claimed run
    routinely has no evidence. It must still be listed — "we checked this, and
    have not heard back yet" is the honest state, and hiding it would look
    like the claim failed."""
    wired["rows"] = [_row(run_id="r-1", merchant_id="m-1", domain="anua.com")]
    body = _client().get("/api/merchant-center/audit/funnel-checks").json()
    assert len(body["checks"]) == 1
    assert body["checks"][0]["observed_signals"] == []


def test_an_evidence_read_failure_does_not_sink_the_list(wired, monkeypatch):
    """One unreadable run must not blank the merchant's whole panel."""
    wired["rows"] = [_row(run_id="r-1", merchant_id="m-1", domain="anua.com")]

    async def boom(**_kw):
        raise RuntimeError("db down")

    import db.audit_evidence as ae
    monkeypatch.setattr(ae, "list_evidence_for_run", boom)
    body = _client().get("/api/merchant-center/audit/funnel-checks").json()
    assert len(body["checks"]) == 1
    assert body["checks"][0]["observed_signals"] == []


def test_duplicate_signals_collapse_to_the_NEWEST(wired):
    """The reprobe job deposits onto the same run with a fresh idempotency key,
    so a run can hold two acceptance_signal rows. list_evidence_for_run returns
    oldest first, so first-wins would show the merchant the ORIGINAL probe's
    level forever — a store whose second probe upgraded detected -> tested
    would keep reading "detected"."""
    wired["rows"] = [_row(run_id="r-1", merchant_id="m-1", domain="anua.com")]
    wired["evidence"]["r-1"] = [
        {"evidence_type": "acceptance_signal", "evidence_level": "detected",
         "payload_jsonb": {}},   # older
        {"evidence_type": "acceptance_signal", "evidence_level": "tested",
         "payload_jsonb": {}},   # newer
    ]
    signals = _client().get(
        "/api/merchant-center/audit/funnel-checks").json()["checks"][0][
            "observed_signals"]
    assert signals == [{"signal": "acceptance_signal",
                        "evidence_level": "tested"}]


def test_an_unknown_evidence_level_is_dropped(wired):
    wired["rows"] = [_row(run_id="r-1", merchant_id="m-1", domain="anua.com")]
    wired["evidence"]["r-1"] = [
        {"evidence_type": "acceptance_signal",
         "evidence_level": "SECRET-PROSE", "payload_jsonb": {}},
    ]
    body = _client().get("/api/merchant-center/audit/funnel-checks")
    assert "SECRET-PROSE" not in body.text
    assert body.json()["checks"][0]["observed_signals"][0]["evidence_level"] is None


def test_the_flag_gates_this_surface_too(wired, monkeypatch):
    """Same flag as the rest of the lane: a dark funnel is dark everywhere."""
    monkeypatch.setattr(sap, "_enabled", lambda: False)
    assert _client().get("/api/merchant-center/audit/funnel-checks").status_code == 404


# The QUERY itself is exercised in tests/test_claimed_funnel_checks_postgres.py.
# It cannot live here: the reader selects DateTime(timezone=True) columns, and
# SQLite cannot round-trip those ("Couldn't parse datetime string"), so a
# SQLite version would fail for a dialect reason and prove nothing about the
# tenancy filter it is meant to guard.


def test_the_domain_is_re_normalized_on_read(wired):
    """funnel_domain_of re-runs normalize_store_domain rather than trusting
    the stored value: it is echoed to the merchant and is the claim gate's
    authorization key, so reading it straight out of partial_result_jsonb
    would hand back whatever a bad writer left there."""
    row = _row(run_id="r-1", merchant_id="m-1", domain="anua.com")
    row["partial_result_jsonb"] = {
        "funnel": {"domain": "  HTTPS://WWW.Anua.com/p/1  "}
    }
    wired["rows"] = [row]
    body = _client().get("/api/merchant-center/audit/funnel-checks").json()
    assert body["checks"][0]["domain"] == "anua.com"

