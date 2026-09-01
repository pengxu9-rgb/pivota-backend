"""The issuer must CONFIRM the constraints, not merely accept them.

THE HOLE THIS CLOSES. `_build_payload` and `_parse_response` had zero coverage, and `issue()`
accepted any 2xx while reading only the card id and reveal handle. A REST API that does not
recognise a field name ignores it and answers 2xx — so a wrong spelling of `spend_limit` or
`merchant_restriction` minted an UNCAPPED, UNLOCKED card, we wrote `amount_cap_minor` into
`agent_issued_cards`, and every downstream alarm keyed on that cap stayed silent. The wire
format is explicitly unverified against Reap, so that was likely rather than hypothetical.

Contrast with the `get_checkout` defect fixed alongside this: that one failed LOUDLY (the
merchant refused the call). This one succeeded while dropping the constraint — the dangerous
direction, and the reason absence is refused here and not just contradiction.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services.card_issuers import CardIssuerError, IssueRequest
from services.card_issuers.reap_issuer import ReapIssuer

CAP = 2317  # odd and unique, so a hardcoded-cap mutant cannot pass by coincidence
DOMAIN = "cosrx.com"


def _issuer():
    return ReapIssuer({"REAP_API_BASE": "https://api.reap.test", "REAP_API_KEY": "k_test"})


def _request(**kw):
    base = dict(
        card_id="card_abc",
        amount_cap_minor=CAP,
        currency="USD",
        merchant_domain=DOMAIN,
        single_use=True,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=60),
        metadata={},
    )
    base.update(kw)
    return IssueRequest(**base)


def _confirming_body(**overrides):
    """What a correctly-honouring issuer echoes back."""
    card = {
        "id": "reap_card_1",
        "reveal_url": "https://reap.test/reveal/xyz",
        "single_use": True,
        "spend_limit": {"amount": CAP, "currency": "USD"},
        "merchant_restriction": {"domains": [DOMAIN]},
    }
    card.update(overrides)
    return {"card": card}


class _Resp:
    status_code = 200

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


def _install(monkeypatch, body):
    """Capture the outbound payload and replay `body`."""
    sent = {}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            sent["url"] = url
            sent["json"] = json
            sent["headers"] = headers or {}
            return _Resp(body)

    import services.card_issuers.reap_issuer as mod

    monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)
    return sent


# --------------------------------------------------------------------------------------
# The request shape — previously untested entirely
# --------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_constraints_are_actually_on_the_wire(monkeypatch):
    sent = _install(monkeypatch, _confirming_body())

    await _issuer().issue(_request())

    payload = sent["json"]
    assert payload["spend_limit"] == {"amount": CAP, "currency": "USD"}
    assert payload["merchant_restriction"] == {"domains": [DOMAIN]}
    assert payload["single_use"] is True
    assert payload["metadata"]["pivota_card_id"] == "card_abc"
    assert sent["headers"]["Authorization"] == "Bearer k_test"
    assert sent["url"] == "https://api.reap.test/v1/cards"


@pytest.mark.asyncio
async def test_a_confirming_response_yields_the_card(monkeypatch):
    _install(monkeypatch, _confirming_body())

    issued = await _issuer().issue(_request())

    assert issued.issuer_card_ref == "reap_card_1"
    assert issued.reveal_handle == "https://reap.test/reveal/xyz"


# --------------------------------------------------------------------------------------
# Silence is refused — the actual hole
# --------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_issuer_that_SILENTLY_DROPS_the_cap_is_refused(monkeypatch):
    """The exact failure the old code could not see: 2xx, valid card, no cap anywhere."""
    body = {"card": {"id": "reap_card_1", "reveal_url": "https://reap.test/r/1"}}
    _install(monkeypatch, body)

    with pytest.raises(CardIssuerError) as excinfo:
        await _issuer().issue(_request())

    assert excinfo.value.code == "REAP_CONSTRAINTS_UNCONFIRMED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides,constraint",
    [
        ({"spend_limit": None}, "spend cap"),
        ({"spend_limit": {"currency": "USD"}}, "spend cap"),
        ({"spend_limit": {"amount": CAP}}, "cap currency"),
        ({"merchant_restriction": None}, "merchant restriction"),
        ({"merchant_restriction": {"domains": []}}, "merchant restriction"),
        ({"single_use": None}, "single-use scope"),
    ],
)
async def test_each_unconfirmed_constraint_is_refused(monkeypatch, overrides, constraint):
    _install(monkeypatch, _confirming_body(**overrides))

    with pytest.raises(CardIssuerError) as excinfo:
        await _issuer().issue(_request())

    assert excinfo.value.code == "REAP_CONSTRAINTS_UNCONFIRMED"
    assert constraint in str(excinfo.value)


# --------------------------------------------------------------------------------------
# Contradiction is refused, and distinguishably so
# --------------------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"spend_limit": {"amount": CAP + 1, "currency": "USD"}},   # cap raised
        {"spend_limit": {"amount": CAP * 100, "currency": "USD"}}, # unit confusion
        {"spend_limit": {"amount": CAP, "currency": "EUR"}},       # different currency
        {"merchant_restriction": {"domains": ["evil.example"]}},   # locked elsewhere
    ],
)
async def test_a_contradicted_constraint_is_refused(monkeypatch, overrides):
    _install(monkeypatch, _confirming_body(**overrides))

    with pytest.raises(CardIssuerError) as excinfo:
        await _issuer().issue(_request())

    assert excinfo.value.code == "REAP_CONSTRAINTS_MISMATCH"


@pytest.mark.asyncio
async def test_a_lock_that_also_admits_other_merchants_is_not_our_lock(monkeypatch):
    """Containing our domain is not the same as being restricted to it."""
    _install(monkeypatch, _confirming_body(
        merchant_restriction={"domains": [DOMAIN, "somewhere.else"]}
    ))

    with pytest.raises(CardIssuerError) as excinfo:
        await _issuer().issue(_request())

    assert excinfo.value.code == "REAP_CONSTRAINTS_MISMATCH"


@pytest.mark.asyncio
async def test_the_two_verdicts_are_distinguishable(monkeypatch):
    """They need different operator responses: 'we cannot tell' vs 'it did something else'."""
    _install(monkeypatch, _confirming_body(spend_limit=None))
    with pytest.raises(CardIssuerError) as unconfirmed:
        await _issuer().issue(_request())

    _install(monkeypatch, _confirming_body(spend_limit={"amount": 999999, "currency": "USD"}))
    with pytest.raises(CardIssuerError) as mismatch:
        await _issuer().issue(_request())

    assert unconfirmed.value.code != mismatch.value.code


# --------------------------------------------------------------------------------------
# Coercion and disclosure rules
# --------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_decimal_string_cap_is_NOT_guessed_at(monkeypatch):
    """"23.00" could mean 23 or 2300. On a spending cap that ambiguity is not resolved by
    guessing — the same rule to_minor_units follows on the way in."""
    _install(monkeypatch, _confirming_body(spend_limit={"amount": "23.17", "currency": "USD"}))

    with pytest.raises(CardIssuerError) as excinfo:
        await _issuer().issue(_request())

    assert excinfo.value.code == "REAP_CONSTRAINTS_UNCONFIRMED"


@pytest.mark.asyncio
async def test_a_digit_string_cap_that_matches_is_accepted(monkeypatch):
    _install(monkeypatch, _confirming_body(spend_limit={"amount": str(CAP), "currency": "usd"}))

    issued = await _issuer().issue(_request())
    assert issued.issuer_card_ref == "reap_card_1"


@pytest.mark.asyncio
async def test_a_boolean_is_not_an_amount(monkeypatch):
    """`True == 1` in Python; a bool must never satisfy a cap comparison."""
    _install(monkeypatch, _confirming_body(spend_limit={"amount": True, "currency": "USD"}))

    with pytest.raises(CardIssuerError) as excinfo:
        await _issuer().issue(_request(amount_cap_minor=1))

    assert excinfo.value.code == "REAP_CONSTRAINTS_UNCONFIRMED"


@pytest.mark.asyncio
async def test_nothing_from_the_issuer_body_reaches_the_exception(monkeypatch):
    """Module rule 1: the body can carry a PAN, so it is parsed by allowlist and dropped.

    Amounts included — a mis-mapped field is exactly how a PAN ends up where an integer was
    expected, and digits are what we would be tempted to print.
    """
    pan = "4111111111111111"
    _install(monkeypatch, _confirming_body(
        spend_limit={"amount": pan, "currency": "USD"}, pan=pan, cvc="737"
    ))

    with pytest.raises(CardIssuerError) as excinfo:
        await _issuer().issue(_request())

    assert pan not in str(excinfo.value)
    assert "737" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_single_use_is_only_asserted_when_we_asked_for_it(monkeypatch):
    """A card we never scoped single-use has no confirmation to give; demanding one would
    refuse a correct response."""
    body = _confirming_body()
    body["card"].pop("single_use")
    _install(monkeypatch, body)

    issued = await _issuer().issue(_request(single_use=False))
    assert issued.issuer_card_ref == "reap_card_1"


@pytest.mark.asyncio
async def test_a_flat_body_without_the_card_envelope_still_verifies(monkeypatch):
    """`_parse_response` already tolerates both shapes; verification must read the same one."""
    flat = _confirming_body()["card"]
    _install(monkeypatch, flat)

    issued = await _issuer().issue(_request())
    assert issued.issuer_card_ref == "reap_card_1"


@pytest.mark.asyncio
async def test_the_orphan_card_id_is_logged_when_verification_refuses(monkeypatch):
    """A refusal here does not un-mint the card, and the caller never stores the ref — so the
    log is the only handle left for revoking it.

    Asserted on the logger the module actually calls, not through caplog/capsys/capfd. All three
    fixtures observe an EMPTY string here (utils.logger installs its own handler and does not
    propagate), so each would have passed vacuously with the logging deleted outright — the
    default failure mode of an assertion that cannot fail.
    """
    import services.card_issuers.reap_issuer as mod

    recorded = []

    class _Recorder:
        def error(self, msg, *args):
            recorded.append(msg % args if args else msg)

        def warning(self, *a, **k):
            pass

    monkeypatch.setattr(mod, "logger", _Recorder())
    _install(monkeypatch, _confirming_body(spend_limit=None))

    with pytest.raises(CardIssuerError):
        await _issuer().issue(_request())

    assert len(recorded) == 1, "exactly one alarm, so it cannot be lost in noise"
    line = recorded[0]
    assert "reap_card_1" in line, "the orphan card ref must survive the refusal"
    assert "card_abc" in line
    assert "REAP_CONSTRAINTS_UNCONFIRMED" in line


# --------------------------------------------------------------------------------------
# The orphan handoff: the ref must survive the refusal, on the exception, not just in a log
# --------------------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides,code",
    [
        ({"spend_limit": None}, "REAP_CONSTRAINTS_UNCONFIRMED"),
        ({"spend_limit": {"amount": 999999, "currency": "USD"}}, "REAP_CONSTRAINTS_MISMATCH"),
    ],
)
async def test_a_constraint_refusal_carries_the_orphan_ref(monkeypatch, overrides, code):
    """The card is REAL. The ref on the exception is what lets the route persist it and the
    sweep find it — a log line is not a work queue."""
    _install(monkeypatch, _confirming_body(**overrides))

    with pytest.raises(CardIssuerError) as excinfo:
        await _issuer().issue(_request())

    assert excinfo.value.code == code
    assert excinfo.value.issuer_card_ref == "reap_card_1"


@pytest.mark.asyncio
async def test_failures_with_NO_card_carry_no_ref(monkeypatch):
    """A non-2xx minted nothing, so there is no orphan and the sweep must not chase one."""
    class _Refused:
        status_code = 500

        def json(self):
            return {}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            return _Refused()

    import services.card_issuers.reap_issuer as mod

    monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)

    with pytest.raises(CardIssuerError) as excinfo:
        await _issuer().issue(_request())

    assert excinfo.value.code == "REAP_REFUSED"
    assert excinfo.value.issuer_card_ref is None


# --------------------------------------------------------------------------------------
# revoke(): confirmation required, and a wrong path must not read as success
# --------------------------------------------------------------------------------------

def _revoke_client(monkeypatch, status, body):
    seen = {}

    class _R:
        status_code = status

        def json(self):
            if body is None:
                raise ValueError("not json")
            return body

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            seen["url"] = url
            return _R()

    import services.card_issuers.reap_issuer as mod

    monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["revoked", "cancelled", "canceled", "TERMINATED", "closed"])
async def test_revoke_accepts_an_affirmative_dead_state(monkeypatch, state):
    seen = _revoke_client(monkeypatch, 200, {"card": {"id": "reap_1", "status": state}})

    await _issuer().revoke("reap_1")

    assert seen["url"] == "https://api.reap.test/v1/cards/reap_1/cancel"


@pytest.mark.asyncio
async def test_a_404_is_NOT_treated_as_already_revoked(monkeypatch):
    """The path is unverified. If it is wrong every call 404s, and calling that success would
    mark the whole orphan backlog revoked while killing nothing."""
    _revoke_client(monkeypatch, 404, {})

    with pytest.raises(CardIssuerError) as excinfo:
        await _issuer().revoke("reap_1")

    assert excinfo.value.code == "REAP_REVOKE_REFUSED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"card": {"id": "reap_1"}},                      # says nothing about state
        {"card": {"id": "reap_1", "status": "active"}},  # says it is still ALIVE
        {"ok": True},                                    # a cheerful no-op
    ],
)
async def test_a_2xx_without_a_dead_state_is_unconfirmed(monkeypatch, body):
    _revoke_client(monkeypatch, 200, body)

    with pytest.raises(CardIssuerError) as excinfo:
        await _issuer().revoke("reap_1")

    assert excinfo.value.code == "REAP_REVOKE_UNCONFIRMED"


@pytest.mark.asyncio
async def test_an_unparseable_revoke_response_is_unconfirmed(monkeypatch):
    _revoke_client(monkeypatch, 200, None)

    with pytest.raises(CardIssuerError) as excinfo:
        await _issuer().revoke("reap_1")

    assert excinfo.value.code == "REAP_REVOKE_UNCONFIRMED"


@pytest.mark.asyncio
async def test_revoke_refuses_an_empty_ref_before_calling_out(monkeypatch):
    seen = _revoke_client(monkeypatch, 200, {"card": {"status": "revoked"}})

    with pytest.raises(CardIssuerError):
        await _issuer().revoke("")

    assert "url" not in seen, "an empty ref must not produce a request at all"
