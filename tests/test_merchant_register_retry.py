"""
register_merchant transaction-abort retry path.

Regression: the retry insert (after DB reconnect) rebuilt merchant_dict from
merchant_data.dict() WITHOUT popping 'password'. merchant_onboarding has no
password column, so the recovery insert failed with an unknown-column error
exactly when it was supposed to recover. The primary insert paths already
pop it; the retry path must too.

Style mirrors tests/test_merchant_declared_mode.py: call the route coroutine
directly with monkeypatched DB + helpers (no live DB).
"""

import pytest

import routes.merchant_onboarding_routes as module
from routes.merchant_onboarding_routes import MerchantRegisterRequest


class _DummyBackgroundTasks:
    def add_task(self, *args, **kwargs):
        pass


def _patch_common(monkeypatch, captured):
    """Stub the shared onboarding side-effects so register_merchant runs DB-free."""

    async def fake_execute(*args, **kwargs):
        return None

    async def fake_fetch_one(*args, **kwargs):
        return None

    async def fake_get_active_onboarding_by_email(email):
        return None

    async def fake_get_user_auth_binding(email):
        return None

    async def fake_resolve_identity_merge(**kwargs):
        return None, None

    async def fake_get_merchant_onboarding(merchant_id):
        return None

    async def fake_update_kyc_status(merchant_id, status, *a, **k):
        return True

    async def fake_sync_user(**kwargs):
        return None

    async def fake_disconnect():
        captured.setdefault("db_cycles", []).append("disconnect")

    async def fake_connect():
        captured.setdefault("db_cycles", []).append("connect")

    async def fake_sleep(seconds):
        captured["slept"] = seconds

    monkeypatch.setattr(module.database, "execute", fake_execute)
    monkeypatch.setattr(module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(module.database, "disconnect", fake_disconnect)
    monkeypatch.setattr(module.database, "connect", fake_connect)
    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(module, "get_active_onboarding_by_email", fake_get_active_onboarding_by_email)
    monkeypatch.setattr(module, "get_user_auth_binding", fake_get_user_auth_binding)
    monkeypatch.setattr(module, "resolve_public_merchant_identity_merge", fake_resolve_identity_merge)
    monkeypatch.setattr(module, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(module, "update_kyc_status", fake_update_kyc_status)
    monkeypatch.setattr(module, "sync_merchant_auth_user", fake_sync_user)


@pytest.mark.asyncio
async def test_transaction_abort_retry_pops_password(monkeypatch):
    captured = {}
    _patch_common(monkeypatch, captured)

    call_dicts = []

    async def flaky_create(merchant_dict):
        call_dicts.append(dict(merchant_dict))
        if len(call_dicts) == 1:
            raise Exception("current transaction is aborted, commands ignored until end of transaction block")
        return "merch_test_retry"

    monkeypatch.setattr(module, "create_merchant_onboarding", flaky_create)

    req = MerchantRegisterRequest(
        business_name="Retry Brand Co",
        region="US",
        contact_email="retry@example.com",
        password="hunter2hunter2",
        operating_mode="store_less",
    )

    resp = await module.register_merchant(req, _DummyBackgroundTasks())

    # Retry path ran: two insert attempts around a DB reconnect cycle.
    assert len(call_dicts) == 2
    assert captured.get("db_cycles") == ["disconnect", "connect"]

    # Primary path already pops password; the retry insert must too —
    # merchant_onboarding has no password column.
    assert "password" not in call_dicts[0]
    assert "password" not in call_dicts[1]

    assert resp["status"] == "success"
    assert resp["merchant_id"] == "merch_test_retry"
