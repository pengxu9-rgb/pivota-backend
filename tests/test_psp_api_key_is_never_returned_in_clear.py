"""The PSP api_key must not leave the server, and masking it must not destroy it.

THE DEFECT. `/psps/all` returned every merchant's live PSP api_key in clear
text — `"api_key": row["api_key"],  # Include for Configure form` — sitting
directly beside `secret_key`, which the very same loop masked. Employee/admin
auth limits who can ask, but the credential still left the server on every load
of the PSP list, for every merchant at once, and landed anywhere a response
goes: browser memory, the disk cache, devtools, a HAR attached to a support
ticket.

WHY THIS NEEDED TWO CHANGES, NOT ONE. Masking the read ALONE would have been
worse than the leak. The employee portal's `UpdatePSPForm` seeds its api_key
input from this exact field:

    const [apiKey, setApiKey] = useState(psp.api_key || '');

and posts it back verbatim on save, with `canSave = apiKey && ...` requiring it
to be non-empty. So an operator who opens the form to change `account_id` and
hits Save would have written `****abcd` over the real key. The PSP keeps working
until its next API call, then fails to authenticate, and the original is gone.

So `/admin/psp/connect` now treats a masked api_key as "keep the stored one".
The two changes are a pair, and the round-trip test below is the one that
proves the pair holds — it replays exactly what the unmodified portal does.

WHY MASKED RATHER THAN OMITTED: that same `canSave` check disables Save when
the field is empty, so dropping the key would lock operators out of editing
`account_id` on an existing PSP. Masking keeps the form usable while the real
credential stays server-side.

NOT CLAIMED: that this was exploited, or that employee/admin auth was ever
bypassed. What is removed is a live credential crossing the wire with no need
to.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from utils.encryption import is_masked_credential, mask_credential


REAL_API_KEY = "sk_live_51ABCdefGHIjklMNOpqr"
REAL_SECRET = "whsec_ZYXwvuTSRqponMLK"


def _psp_row(**over):
    row = {
        "psp_id": "psp_1",
        "provider": "stripe",
        "name": "Stripe main",
        "status": "active",
        "merchant_id": "m1",
        "connected_at": datetime(2026, 5, 1, 12, 0),
        "capabilities": "cards,refunds",
        "api_key": REAL_API_KEY,
        "account_id": "acct_123",
        "secret_key": REAL_SECRET,
        "merchant_name": "Acme",
    }
    row.update(over)
    return row


class PspListSpy:
    """Serves `/psps/all`: the PSP rows, then per-row order stats."""

    def __init__(self, rows):
        self.rows = rows

    async def fetch_all(self, query, values=None):
        return self.rows

    async def fetch_one(self, query, values=None):
        return {"transaction_count": 0, "total_volume": 0, "successful_count": 0}


@contextmanager
def _list_client(spy, *, role: str = "employee"):
    import routes.employee_dashboard_routes as dash
    from main import app

    async def fake_current_user():
        return {"role": role, "email": "e@example.com", "user_id": "u1"}

    app.dependency_overrides[dash.get_current_user] = fake_current_user
    try:
        with TestClient(app) as client:
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(dash, "database", spy)
                yield client
    finally:
        app.dependency_overrides.pop(dash.get_current_user, None)


# ===========================================================================
# The read side
# ===========================================================================


def test_the_raw_api_key_never_appears_in_the_response() -> None:
    """The load-bearing assertion, made against the RAW RESPONSE TEXT.

    Checking the parsed `api_key` field alone would miss the credential
    reappearing under any other key — and "Include for Configure form" is
    exactly the kind of comment that comes back attached to a new field name.
    """
    with _list_client(PspListSpy([_psp_row()])) as client:
        resp = client.get("/psps/all")

    assert resp.status_code == 200, resp.json()
    assert REAL_API_KEY not in resp.text, (
        "the live PSP api_key is being shipped to the browser again")
    assert REAL_SECRET not in resp.text, "the secret_key leaked"


def test_the_api_key_is_masked_and_keeps_its_last_four_characters() -> None:
    """Masked rather than blanked: the last four are what lets an operator tell
    two keys apart, which is the only legitimate reason the field was ever
    displayed.

    The expected value is a LITERAL. An earlier version asserted
    `psp["api_key"] == mask_credential(REAL_API_KEY)` — comparing the
    endpoint's output to the very function under test, so it could not detect
    that function being wrong. Review proved the gap: replacing
    `mask_credential`'s body with `return "*" + value[1:]` shipped
    `*k_live_51ABCdefGHIjklMNOpqr` from the endpoint and every test still
    passed. Never pin a security primitive to itself.
    """
    with _list_client(PspListSpy([_psp_row()])) as client:
        psp = client.get("/psps/all").json()["psps"][0]

    assert psp["api_key"] == "************Opqr", (
        "the masked api_key changed shape — check it is not disclosing more")
    assert is_masked_credential(psp["api_key"])
    assert psp["api_key"].endswith(REAL_API_KEY[-4:])
    assert psp["api_key_present"] is True


def test_mask_credential_never_discloses_more_than_four_characters() -> None:
    """THE PROPERTY, checked against literals and independently of the endpoint.

    This is what actually kills the "leak all but the first character" class of
    mutation: whatever the implementation does, the output must share at most 4
    trailing characters with the input and nothing else.
    """
    assert mask_credential("sk_live_51ABCdefGHIjklMNOpqr") == "************Opqr"
    assert mask_credential("whsec_ZYXwvuTSRqponMLK") == "************nMLK"

    # A REALISTICALLY LONG credential, and this is not decoration. Re-review
    # found three surviving mutations that all hid behind the fixtures being
    # 22-27 characters — shorter than any real PSP key. One added
    # `if len(value) > 40: return value[:12] + value[-4:]`, leaking the first
    # twelve characters of every live Stripe key while every test stayed green,
    # because no test ever masked anything longer than 27 characters. Real
    # `sk_live_` keys run past 100.
    long_key = "sk_live_51NxwXyZ" + "AbCdEf0123456789" * 6 + "TAIL"
    assert len(long_key) > 100
    assert mask_credential(long_key) == "************TAIL"

    for secret in (
        "sk_live_51ABCdefGHIjklMNOpqr",
        "whsec_ZYXwvuTSRqponMLK",
        long_key,
    ):
        masked = mask_credential(secret)
        # Every revealed character must come from the last 4.
        revealed = [c for c in masked if c != "*"]
        assert "".join(revealed) == secret[-4:], (
            f"mask revealed {revealed!r}, expected only {secret[-4:]!r}")
        # And no interior run of the original may survive.
        assert secret[:-4] not in masked
        for start in range(0, len(secret) - 8):
            assert secret[start:start + 8] not in masked, (
                f"an 8-char run of the credential survived masking at {start}")


def test_mask_credential_does_not_disclose_the_credential_length() -> None:
    """A per-length mask publishes the exact credential length, which
    fingerprints the issuing provider and narrows an attacker's search space.
    Two credentials of very different lengths must mask to the same width."""
    short_masked = mask_credential("a" * 12 + "TAIL")
    long_masked = mask_credential("a" * 120 + "TAIL")
    assert len(short_masked) == len(long_masked), (
        "the mask width tracks the credential length — it leaks the length")
    # Width parity alone let a mutation survive that changed the SHORT branch to
    # `"*" * len(value)`, re-introducing length disclosure below 8 characters.
    assert len(mask_credential("abc")) == len(mask_credential("abcdefg")), (
        "the sub-8 mask width tracks the credential length")


@pytest.mark.parametrize("tiny", ["a", "abc", "abcd", "abcde", "1234567"])
def test_a_short_credential_reveals_nothing(tiny: str) -> None:
    """`/admin/psp/connect` enforces `len >= 8`, but other writers do not
    (`PUT /merchant/integrations/psp/{psp_id}` only requires non-empty), so
    short values do reach the mask. Revealing 4 of 5 characters is not masking.
    """
    masked = mask_credential(tiny)
    assert set(masked) == {"*"}, f"{tiny!r} masked to {masked!r}, revealing content"


@pytest.mark.parametrize("bad", [123, True, None, [], {}, b"bytes"])
def test_mask_credential_returns_none_for_non_strings_instead_of_raising(bad) -> None:
    """Defensive on purpose. The only caller sits in a loop inside `/psps/all`,
    whose outer handler converts any exception into `{"status": "success",
    "psps": []}` — so one malformed row would have silently emptied the entire
    PSP list rather than degrading one field. A fabrication-shaped failure."""
    assert mask_credential(bad) is None


def test_a_psp_with_no_stored_key_is_distinguishable_from_a_hidden_one() -> None:
    """`api_key_present` exists so "we are not showing you this" and "there is
    nothing to show" stop rendering identically — the same distinction the
    fabrication fixes elsewhere in this sweep turned on."""
    with _list_client(PspListSpy([_psp_row(api_key=None, secret_key=None)])) as client:
        psp = client.get("/psps/all").json()["psps"][0]

    assert psp["api_key"] is None
    assert psp["api_key_present"] is False
    assert psp["secret_key_present"] is False


def test_account_id_is_still_returned_in_full() -> None:
    """`account_id` is an identifier, not a credential — masking it would break
    the Configure form for no security gain."""
    with _list_client(PspListSpy([_psp_row()])) as client:
        psp = client.get("/psps/all").json()["psps"][0]

    assert psp["account_id"] == "acct_123"


# ===========================================================================
# The write side — without this, masking the read DESTROYS credentials
# ===========================================================================


@contextmanager
def _connect_client(existing_row, captured):
    """Drive `/admin/psp/connect`, capturing the api_key it would persist."""
    import routes.admin_api as admin
    from main import app

    async def fake_admin():
        return {"role": "admin", "email": "a@example.com", "user_id": "admin1"}

    class ConnectSpy:
        # Records the SQL, because a spy answers whatever it is asked and
        # cannot tell you WHICH COLUMNS the handler requested. A test that only
        # reads the spy's reply is blind to a column being dropped from the
        # SELECT — which is exactly how the secret_key preserve broke on the
        # merchant_id path.
        async def fetch_one(self, query, values=None):
            captured.setdefault("queries", []).append(query)
            if "FROM merchant_psps" in query and existing_row:
                return dict(existing_row)
            return None

        async def fetch_all(self, query, values=None):
            captured.setdefault("queries", []).append(query)
            return [dict(existing_row)] if existing_row else []

        async def execute(self, query, values=None):
            return None

        def transaction(self):
            class _T:
                async def __aenter__(self_inner):
                    return None

                async def __aexit__(self_inner, *a):
                    return False
            return _T()

    async def fake_persist(**kwargs):
        captured["api_key"] = kwargs.get("api_key")
        captured["secret_key"] = kwargs.get("secret_key")
        captured["merchant_id"] = kwargs.get("merchant_id")
        return {"psp_id": kwargs.get("psp_id") or "psp_1"}

    app.dependency_overrides[admin.require_admin] = fake_admin
    try:
        with TestClient(app) as client:
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(admin, "database", ConnectSpy())
                mp.setattr(admin, "persist_canonical_merchant_psp", fake_persist)
                yield client
    finally:
        app.dependency_overrides.pop(admin.require_admin, None)


def test_a_masked_api_key_preserves_the_stored_credential() -> None:
    """THE test. Without this branch, masking the read is a destruction bug."""
    captured = {}
    existing = {
        "psp_id": "psp_1", "merchant_id": "m1", "provider": "stripe",
        "api_key": REAL_API_KEY, "account_id": "acct_123",
        "environment": "live", "provider_config": None,
    }

    with _connect_client(existing, captured) as client:
        resp = client.post("/admin/psp/connect", json={
            "provider": "stripe",
            "merchant_id": "m1",
            "psp_id": "psp_1",
            "api_key": mask_credential(REAL_API_KEY),
            "account_id": "acct_456",
        })

    assert resp.status_code == 200, resp.text
    assert captured["api_key"] == REAL_API_KEY, (
        "a masked api_key was written to the database, destroying the real "
        "credential — this is what masking the read causes without this branch")


def test_a_genuinely_new_api_key_still_replaces_the_stored_one() -> None:
    """The other half. Preserving on a mask is worthless if it also swallows a
    real rotation — an operator pasting a new key must land it."""
    captured = {}
    existing = {
        "psp_id": "psp_1", "merchant_id": "m1", "provider": "stripe",
        "api_key": REAL_API_KEY, "account_id": "acct_123",
        "environment": "live", "provider_config": None,
    }
    rotated = "sk_live_99ZZZrotatedKEY123"

    with _connect_client(existing, captured) as client:
        resp = client.post("/admin/psp/connect", json={
            "provider": "stripe",
            "merchant_id": "m1",
            "psp_id": "psp_1",
            "api_key": rotated,
        })

    assert resp.status_code == 200, resp.text
    assert captured["api_key"] == rotated, "a credential rotation was swallowed"


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_a_blank_api_key_on_an_update_preserves_the_stored_credential(blank) -> None:
    """The honest form of "I am not changing the key", and the one the portal's
    form now sends.

    It matters that BLANK and MASKED resolve identically: the portal and the
    backend live in separate repos and cannot deploy in lockstep, so during any
    skew one build sends a mask and the other sends nothing. Both must preserve.
    """
    captured = {}
    existing = {
        "psp_id": "psp_1", "merchant_id": "m1", "provider": "stripe",
        "api_key": REAL_API_KEY, "account_id": "acct_123",
        "environment": "live", "provider_config": None,
    }

    payload = {
        "provider": "stripe",
        "merchant_id": "m1",
        "psp_id": "psp_1",
        "account_id": "acct_456",
    }
    if blank is not None:
        payload["api_key"] = blank

    with _connect_client(existing, captured) as client:
        resp = client.post("/admin/psp/connect", json=payload)

    assert resp.status_code == 200, resp.text
    assert captured["api_key"] == REAL_API_KEY, (
        "a blank api_key wiped the stored credential instead of preserving it")


def test_a_blank_api_key_on_a_NEW_connection_is_still_rejected() -> None:
    """Deferring the required-check must not delete it.

    A new PSP with no stored key has nothing to preserve, so a blank key is a
    genuine error — the same 400 the original `len < 8` check gave, just moved
    to the point where "is there a stored key?" can actually be answered.
    """
    captured = {}

    with _connect_client(None, captured) as client:
        resp = client.post("/admin/psp/connect", json={
            "provider": "stripe",
            "merchant_id": "m1",
            "api_key": "",
        })

    assert resp.status_code == 400
    assert "api_key" not in captured, "a blank key reached the persistence layer"


def test_a_short_api_key_is_still_rejected_as_malformed() -> None:
    """The length check still applies to a key that WAS supplied — deferring
    the missing-value case must not turn `len < 8` into dead code."""
    captured = {}
    existing = {
        "psp_id": "psp_1", "merchant_id": "m1", "provider": "stripe",
        "api_key": REAL_API_KEY, "account_id": "acct_123",
        "environment": "live", "provider_config": None,
    }

    with _connect_client(existing, captured) as client:
        resp = client.post("/admin/psp/connect", json={
            "provider": "stripe",
            "merchant_id": "m1",
            "psp_id": "psp_1",
            "api_key": "short",
        })

    assert resp.status_code == 400
    assert "api_key" not in captured


def test_a_masked_key_with_nothing_stored_is_rejected() -> None:
    """A mask with no stored key behind it is not a credential and must not be
    persisted as one. Rejecting beats writing asterisks."""
    captured = {}

    with _connect_client(None, captured) as client:
        resp = client.post("/admin/psp/connect", json={
            "provider": "stripe",
            "merchant_id": "m1",
            "api_key": mask_credential(REAL_API_KEY),
        })

    assert resp.status_code == 400
    assert "api_key" not in captured, "a masked value reached the persistence layer"


# ===========================================================================
# The two halves together
# ===========================================================================


def test_the_portal_round_trip_leaves_the_credential_intact() -> None:
    """Replays what the UNMODIFIED employee portal does, end to end.

    Read `/psps/all` → seed the form from `psp.api_key` → change `account_id`
    → POST the form back. The stored credential must survive, because this is
    the sequence an operator performs and the frontend cannot be deployed in
    lockstep with this change.
    """
    with _list_client(PspListSpy([_psp_row()])) as client:
        listed = client.get("/psps/all").json()["psps"][0]

    # Exactly what UpdatePSPForm does: useState(psp.api_key || '')
    form_api_key = listed["api_key"] or ""
    assert form_api_key, "Save would be disabled — the form needs a non-empty value"

    captured = {}
    existing = {
        "psp_id": "psp_1", "merchant_id": "m1", "provider": "stripe",
        "api_key": REAL_API_KEY, "account_id": "acct_123",
        "environment": "live", "provider_config": None,
    }

    # merchant_id IS included here, and that is a documented limitation rather
    # than a convenience: see
    # `test_the_real_portal_payload_is_rejected_before_reaching_this_logic`
    # below. The portal's UpdatePSPForm sends no merchant_id and is therefore
    # rejected upstream, so the preserve logic cannot be reached through that
    # form today. This test pins the preserve behaviour for callers that DO
    # send merchant_id — every other consumer of this endpoint, and the form
    # once its 400 is fixed.
    with _connect_client(existing, captured) as client:
        resp = client.post("/admin/psp/connect", json={
            "provider": "stripe",
            "merchant_id": "m1",
            "psp_id": "psp_1",
            "api_key": form_api_key,
            "account_id": "acct_456",
        })

    assert resp.status_code == 200, resp.text
    assert captured["api_key"] == REAL_API_KEY, (
        "the round trip overwrote the live key")


def test_the_real_portal_payload_is_rejected_before_reaching_this_logic() -> None:
    """PINS A KNOWN LIMITATION rather than leaving it to be rediscovered.

    The portal's UpdatePSPForm posts `{provider, psp_id, account_id}` and no
    merchant_id — its own comment reads "Pass psp_id instead of merchant_id for
    update" — and `admin_connect_psp` rejects a missing merchant_id BEFORE the
    psp_id lookup. So every save from that form 400s, and the
    credential-destruction scenario this file exists to prevent is unreachable
    through it.

    That is NOT fixed here. Deferring the merchant_id check so a psp_id can
    supply it was attempted and withdrawn: it interacts with the PayPal secret
    gate and with `persist_canonical_merchant_psp` preserving from a PARTIAL
    `existing_row`, and review found it created a false 400 on one path and a
    bypass on another. It belongs in its own change alongside the root cause
    (six columns, not just secret_key, are degraded by that partial dict).

    The masking in `/psps/all` is correct and complete regardless: the leak was
    real whether or not any form could save. This preserve branch is the
    defence for callers that DO reach it.
    """
    captured = {}
    existing = {
        "psp_id": "psp_1", "merchant_id": "m1", "provider": "stripe",
        "api_key": REAL_API_KEY, "account_id": "acct_123",
        "environment": "live", "provider_config": None,
    }

    with _connect_client(existing, captured) as client:
        resp = client.post("/admin/psp/connect", json={
            "provider": "stripe",
            "psp_id": "psp_1",              # exactly what the portal sends
            "account_id": "acct_456",
        })

    assert resp.status_code == 400
    assert "merchant_id" in resp.text
    assert "api_key" not in captured, "it reached the persistence layer"


def test_an_empty_credential_masks_to_nothing_not_to_a_fake_mask() -> None:
    """`""` must yield None, not `"************"`.

    A fake mask over an empty credential reads as "a secret is stored and we
    are hiding it" when nothing is stored — and it round-trips as a masked
    value, so the write path would then "preserve" a credential that does not
    exist. Mutation-found: dropping the `or not value` guard survived every
    other test in this file.
    """
    assert mask_credential("") is None
    assert mask_credential("   ") is not None  # whitespace IS content; only empty is nothing

