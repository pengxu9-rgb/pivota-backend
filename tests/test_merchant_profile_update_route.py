"""PUT /merchant/profile — contact fields persist and login-email change
moves users.email + auth_identities together (the pre-fix stub returned
success without writing anything, silently breaking login-email changes)."""

import contextlib

import pytest
from fastapi import HTTPException


MERCHANT_ROW = {
    "merchant_id": "merch_upd_1",
    "business_name": "Glow Commerce",
    "contact_email": "old@example.com",
    "contact_phone": "+1-555-0100",
    "website": "https://glow.example",
}

CURRENT_USER = {
    "role": "merchant",
    "merchant_id": "merch_upd_1",
    "email": "old@example.com",
    "sub": "identity_123",
    "identity_id": "identity_123",
}


class FakeDB:
    def __init__(self, *, email_taken: bool = False):
        self.email_taken = email_taken
        self.executed = []

    async def fetch_one(self, query, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return dict(MERCHANT_ROW)
        if "FROM users" in q or "FROM auth_identities" in q:
            return {"id": 99} if self.email_taken else None
        return None

    async def execute(self, query, values=None):
        self.executed.append((str(query), dict(values or {})))

    def transaction(self):
        @contextlib.asynccontextmanager
        async def _txn():
            yield

        return _txn()


@pytest.fixture()
def module(monkeypatch: pytest.MonkeyPatch):
    import routes.merchant_api_extensions as module
    import db.auth_identity as auth_identity_module

    events = []

    async def fake_record_identity_event(**kwargs):
        events.append(kwargs)

    monkeypatch.setattr(auth_identity_module, "record_identity_event", fake_record_identity_event)
    module._test_identity_events = events
    return module


@pytest.mark.asyncio
async def test_contact_only_update_persists_without_touching_login(module, monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(module, "database", db)

    response = await module.update_merchant_profile(
        profile_data={
            "business_name": "Glow Commerce v2",
            "contact_email": "old@example.com",
            "contact_phone": "+1-555-0999",
            "website": "https://glow2.example",
        },
        current_user=dict(CURRENT_USER),
    )

    assert response["status"] == "success"
    assert response["login_email_changed"] is False
    assert response["data"]["business_name"] == "Glow Commerce v2"
    assert response["data"]["contact_phone"] == "+1-555-0999"

    queries = [q for q, _ in db.executed]
    assert any("UPDATE merchant_onboarding" in q for q in queries)
    assert not any("UPDATE users" in q for q in queries)
    assert not any("UPDATE auth_identities" in q for q in queries)
    assert module._test_identity_events == []


@pytest.mark.asyncio
async def test_email_change_updates_users_identities_and_onboarding(module, monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(module, "database", db)

    response = await module.update_merchant_profile(
        profile_data={
            "business_name": "Glow Commerce",
            "contact_email": "New@Example.org",
            "contact_phone": "+1-555-0100",
            "website": "https://glow.example",
        },
        current_user=dict(CURRENT_USER),
    )

    assert response["login_email_changed"] is True
    assert response["data"]["contact_email"] == "New@Example.org"

    onboarding = next(v for q, v in db.executed if "UPDATE merchant_onboarding" in q)
    assert onboarding["contact_email"] == "New@Example.org"

    users_update = next(v for q, v in db.executed if "UPDATE users" in q)
    assert users_update == {"new_email": "new@example.org", "old_email": "old@example.com"}

    identity_update = next(v for q, v in db.executed if "UPDATE auth_identities" in q)
    assert identity_update == {"new_email": "new@example.org", "old_email": "old@example.com"}

    assert len(module._test_identity_events) == 1
    event = module._test_identity_events[0]
    assert event["event_type"] == "login_email_changed"
    assert event["details"]["old_email"] == "old@example.com"


@pytest.mark.asyncio
async def test_email_change_conflict_returns_409_and_writes_nothing(module, monkeypatch):
    db = FakeDB(email_taken=True)
    monkeypatch.setattr(module, "database", db)

    with pytest.raises(HTTPException) as exc_info:
        await module.update_merchant_profile(
            profile_data={"contact_email": "taken@example.com"},
            current_user=dict(CURRENT_USER),
        )

    assert exc_info.value.status_code == 409
    assert db.executed == []


@pytest.mark.asyncio
async def test_invalid_new_email_rejected(module, monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(module, "database", db)

    with pytest.raises(HTTPException) as exc_info:
        await module.update_merchant_profile(
            profile_data={"contact_email": "not-an-email"},
            current_user=dict(CURRENT_USER),
        )

    assert exc_info.value.status_code == 422
    assert db.executed == []


@pytest.mark.asyncio
async def test_absent_keys_keep_existing_values(module, monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(module, "database", db)

    response = await module.update_merchant_profile(
        profile_data={"business_name": "Renamed Co"},
        current_user=dict(CURRENT_USER),
    )

    onboarding = next(v for q, v in db.executed if "UPDATE merchant_onboarding" in q)
    assert onboarding["contact_phone"] == "+1-555-0100"
    assert onboarding["website"] == "https://glow.example"
    assert onboarding["contact_email"] == "old@example.com"
    assert response["login_email_changed"] is False
