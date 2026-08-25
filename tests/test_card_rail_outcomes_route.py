"""POST /agent/v1/outcomes — validation, attribution, and what it refuses to guess.

The database CHECKs in migration 199 are the real contract (gated by
tests/test_card_rail_outcomes_postgres.py). These cover what only the route can do: stamp the
reporter from the token, split an unrecognised failure reason instead of losing it, and turn a
constraint violation into a 4xx that names the field rather than a 500 from Postgres.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from main import app
from routes.agent_auth import get_agent_context

# The app converts a 422 into 400 / INVALID_REQUEST on the way out
# (middleware/error_handler.py:146) — a house convention, not a quirk of this route. Asserting
# 422 here would test the raise site rather than what a caller actually receives.
INVALID = 400


class _Ctx:
    def __init__(self, agent_id: str = "agent_from_token") -> None:
        self.agent_id = agent_id
        self.agent_name = "Test Agent"

    def can_access_merchant(self, _merchant_id: str) -> bool:
        return True


@pytest.fixture
def client_and_writes(monkeypatch: pytest.MonkeyPatch):
    """A client with auth satisfied and the DB write captured rather than performed."""
    import routes.card_rail_outcomes as mod

    writes: List[Dict[str, Any]] = []

    async def fake_record(values: Dict[str, Any]):
        writes.append(values)
        return {"recommendation_id": values["recommendation_id"], "inserted": True}

    monkeypatch.setattr(mod, "record_outcome", fake_record)
    app.dependency_overrides[get_agent_context] = lambda: _Ctx()
    try:
        yield TestClient(app), writes
    finally:
        app.dependency_overrides.pop(get_agent_context, None)


def _post(client, **over):
    body = {"recommendation_id": "rec_abc123", "outcome": "completed"}
    body.update(over)
    return client.post("/agent/v1/outcomes", json=body)


# --- attribution ------------------------------------------------------------------------------

def test_the_agent_id_comes_from_the_TOKEN_and_a_body_field_cannot_override_it(client_and_writes):
    """THE security property of this endpoint. It exists to be evidence, and an agent that could
    name itself could attribute its own failures to a competitor — or credit itself with a
    completion it never made."""
    client, writes = client_and_writes
    res = _post(client, agent_id="somebody_else", merchant_domain="brand.example")
    assert res.status_code == 200, res.text
    assert writes[0]["agent_id"] == "agent_from_token"
    # repr, not json.dumps — the values dict holds datetimes and is not JSON-serialisable.
    assert "somebody_else" not in repr(writes[0]), (
        "a body-supplied agent id must not survive into ANY stored field"
    )


def test_the_endpoint_requires_authentication() -> None:
    """No override installed, so the real dependency runs. An unauthenticated writer could poison
    the measurement this table exists to produce."""
    with TestClient(app) as anon:
        res = anon.post(
            "/agent/v1/outcomes",
            json={"recommendation_id": "rec_x", "outcome": "completed"},
        )
    assert res.status_code in (401, 403), (
        f"expected an auth refusal, got {res.status_code}: {res.text[:200]}"
    )


# --- the vocabularies -------------------------------------------------------------------------

def test_an_unknown_outcome_is_a_422_that_names_the_allowed_values(client_and_writes):
    client, writes = client_and_writes
    res = _post(client, outcome="kinda_worked")
    assert res.status_code == INVALID
    assert "completed" in res.text and "aborted_on_mismatch" in res.text
    assert writes == [], "an invalid outcome must not reach the database"


def test_a_failure_without_a_reason_is_refused_BEFORE_the_database_sees_it(client_and_writes):
    """The CHECK would also catch it, but as a 500. A caller that gets a 422 naming the field can
    fix its report; a caller that gets a 500 files a bug against us."""
    client, writes = client_and_writes
    res = _post(client, outcome="failed")
    assert res.status_code == INVALID
    assert "failure_reason" in res.text
    assert "out_of_stock" in res.text, "the 422 should teach the vocabulary"
    assert writes == []


def test_an_unrecognised_failure_reason_is_KEPT_not_rejected_and_not_coerced(client_and_writes):
    """Rejecting would lose the outcome entirely and teach us nothing. Mapping it to 'other' would
    erase the evidence that our vocabulary is incomplete. It goes to the raw column, is still
    counted, and the response says so — so the caller learns immediately rather than from a
    dashboard three weeks later."""
    client, writes = client_and_writes
    res = _post(client, outcome="failed", failure_reason="captcha_wall")
    assert res.status_code == 200, res.text
    assert writes[0]["failure_reason"] is None, "an unknown value must not enter the typed column"
    assert writes[0]["failure_reason_raw"] == "captcha_wall"
    assert res.json()["failure_reason_unrecognised"] is True


@pytest.mark.parametrize("sent", ["out-of-stock", "OUT_OF_STOCK", "  price mismatch  "])
def test_a_known_reason_is_normalised_rather_than_treated_as_unknown(client_and_writes, sent):
    """A caller writing `out-of-stock` means the vocabulary term. Treating a hyphen as a new
    reason would fragment the exact metric this column exists to make countable."""
    client, writes = client_and_writes
    res = _post(client, outcome="failed", failure_reason=sent)
    assert res.status_code == 200, res.text
    assert writes[0]["failure_reason"] in ("out_of_stock", "price_mismatch")
    assert res.json()["failure_reason_unrecognised"] is False


def test_a_reporter_outside_the_vocabulary_is_refused(client_and_writes):
    client, writes = client_and_writes
    res = _post(client, reported_by="marketing")
    assert res.status_code == INVALID and writes == []


def test_reported_by_defaults_to_agent(client_and_writes):
    client, writes = client_and_writes
    assert _post(client).status_code == 200
    assert writes[0]["reported_by"] == "agent"


# --- money ------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [-1.0, 12.345678])
def test_a_total_we_cannot_represent_exactly_is_refused_rather_than_rounded(client_and_writes, bad):
    """This table's whole point is measuring the gap between what we quoted and what was charged.
    A rounding WE introduce would be indistinguishable from the merchant's error."""
    client, writes = client_and_writes
    res = _post(client, quoted_grand_total=bad)
    assert res.status_code == INVALID, res.text
    assert "quoted_grand_total" in res.text
    assert writes == []


def test_a_legitimate_total_is_kept_exactly(client_and_writes):
    client, writes = client_and_writes
    assert _post(client, quoted_grand_total=41.99, actual_grand_total=48.5).status_code == 200
    assert str(writes[0]["quoted_grand_total"]) == "41.99"
    assert str(writes[0]["actual_grand_total"]) == "48.5"


# --- the JSONB field is agent-supplied --------------------------------------------------------

def test_latency_keeps_numbers_and_drops_everything_else(client_and_writes):
    """`latency_ms` lands in JSONB straight from an agent, so it is the obvious place to smuggle a
    blob into our storage. Non-numeric values are DROPPED rather than rejected — a malformed
    latency should not cost us the outcome it was attached to."""
    client, writes = client_and_writes
    res = _post(client, latency_ms={
        "recommend": 120, "verify": 34.5,
        "note": "a string", "flag": True, "nested": {"a": 1}, "negative": -5,
    })
    assert res.status_code == 200, res.text
    stored = json.loads(writes[0]["latency_ms"])
    assert stored == {"recommend": 120.0, "verify": 34.5}


def test_latency_is_bounded_in_key_count(client_and_writes):
    client, writes = client_and_writes
    res = _post(client, latency_ms={f"k{i}": i for i in range(200)})
    assert res.status_code == 200
    assert len(json.loads(writes[0]["latency_ms"])) <= 16


def test_a_non_dict_latency_does_not_cost_the_outcome(client_and_writes):
    client, writes = client_and_writes
    res = client.post(
        "/agent/v1/outcomes",
        json={"recommendation_id": "rec_x", "outcome": "completed", "latency_ms": "nope"},
    )
    # Pydantic refuses the wrong type outright; either way the caller learns, and nothing
    # malformed reaches JSONB.
    assert res.status_code in (200, INVALID)
    if res.status_code == 200:
        assert json.loads(writes[0]["latency_ms"]) == {}


# --- honest reporting -------------------------------------------------------------------------

def test_a_write_that_did_not_land_is_reported_as_a_failure_not_an_ok(
    monkeypatch: pytest.MonkeyPatch,
):
    """The reason this hop is an inbound endpoint at all. The pivota-acp outbox failed quietly
    because its sender returned the same thing on success, on transport error and on an upstream
    500 — the caller could not tell delivered from rejected from never attempted. A silent `ok`
    here would reproduce exactly that."""
    import routes.card_rail_outcomes as mod

    async def fake_record(_values):
        return None

    monkeypatch.setattr(mod, "record_outcome", fake_record)
    app.dependency_overrides[get_agent_context] = lambda: _Ctx()
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            res = client.post(
                "/agent/v1/outcomes",
                json={"recommendation_id": "rec_x", "outcome": "completed"},
            )
        assert res.status_code == 500
        assert "not recorded" in res.text
    finally:
        app.dependency_overrides.pop(get_agent_context, None)


def test_the_response_says_whether_this_was_a_first_report_or_a_correction(
    monkeypatch: pytest.MonkeyPatch,
):
    """So a caller can tell them apart without a second round trip."""
    import routes.card_rail_outcomes as mod

    async def fake_record(values):
        return {"recommendation_id": values["recommendation_id"], "inserted": False}

    monkeypatch.setattr(mod, "record_outcome", fake_record)
    app.dependency_overrides[get_agent_context] = lambda: _Ctx()
    try:
        with TestClient(app) as client:
            res = client.post(
                "/agent/v1/outcomes",
                json={"recommendation_id": "rec_x", "outcome": "completed"},
            )
        assert res.status_code == 200
        assert res.json()["created"] is False
    finally:
        app.dependency_overrides.pop(get_agent_context, None)


def test_the_request_model_HAS_no_agent_id_field_at_all(client_and_writes):
    """The reason a body-supplied agent id cannot forge attribution.

    The route reads `context.agent_id`, but that alone is not what protects it — a mutation run
    showed that reading `body.agent_id or context.agent_id` passes every other test, because
    pydantic silently drops a field the model does not declare. The MODEL's shape is the guard, so
    it is asserted directly: add `agent_id` to CardRailOutcome and this fails immediately.
    """
    from routes.card_rail_outcomes import CardRailOutcome

    assert "agent_id" not in CardRailOutcome.model_fields, (
        "CardRailOutcome must not declare agent_id — attribution comes from the token"
    )
    parsed = CardRailOutcome(
        recommendation_id="rec_x", outcome="completed", agent_id="somebody_else"
    )
    assert not hasattr(parsed, "agent_id"), (
        "an undeclared agent_id must not survive parsing; if the model ever sets "
        "model_config extra='allow', this endpoint becomes forgeable"
    )
