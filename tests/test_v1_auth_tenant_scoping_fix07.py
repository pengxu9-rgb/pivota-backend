from fastapi import FastAPI
from fastapi.testclient import TestClient


def _build_onboarding_client(user=None):
    import routes.merchant_onboarding_routes as module

    app = FastAPI()
    app.include_router(module.router)

    if user is not None:
        async def fake_current_user():
            return user

        app.dependency_overrides[module.get_current_user] = fake_current_user

    return TestClient(app), module


def _build_universal_sync_client(user):
    import routes.universal_product_sync as module

    app = FastAPI()
    app.include_router(module.router)

    async def fake_current_user():
        return user

    app.dependency_overrides[module.get_current_user] = fake_current_user
    return TestClient(app), module


def _build_payouts_client(user):
    import routes.merchant_payouts as module

    app = FastAPI()
    app.include_router(module.router)

    async def fake_current_user():
        return user

    app.dependency_overrides[module.get_current_user] = fake_current_user
    return TestClient(app), module


def test_psp_setup_requires_authentication(monkeypatch) -> None:
    client, module = _build_onboarding_client()

    async def fail_if_called(_merchant_id):
        raise AssertionError("tenant guard should run before merchant lookup")

    monkeypatch.setattr(module, "get_merchant_onboarding", fail_if_called)

    response = client.post(
        "/merchant/onboarding/psp/setup",
        json={
            "merchant_id": "merch_a",
            "psp_type": "stripe",
            "psp_key": "sk_test_secret",
        },
    )

    assert response.status_code in {401, 403}
    assert "api_key" not in response.json()


def test_psp_setup_rejects_cross_tenant_before_db_read(monkeypatch) -> None:
    client, module = _build_onboarding_client(
        {"role": "merchant", "merchant_id": "merch_a"}
    )

    async def fail_if_called(_merchant_id):
        raise AssertionError("tenant guard should run before merchant lookup")

    monkeypatch.setattr(module, "get_merchant_onboarding", fail_if_called)

    response = client.post(
        "/merchant/onboarding/psp/setup",
        json={
            "merchant_id": "merch_b",
            "psp_type": "stripe",
            "psp_key": "sk_test_secret",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "merchant_id does not match authenticated user"


def test_psp_setup_allows_same_tenant(monkeypatch) -> None:
    client, module = _build_onboarding_client(
        {"role": "merchant", "merchant_id": "merch_a"}
    )

    async def fake_get_merchant_onboarding(merchant_id):
        assert merchant_id == "merch_a"
        return {
            "merchant_id": "merch_a",
            "status": "approved",
            "auto_approved": False,
            "psp_connected": False,
        }

    async def fake_validate_psp_credentials(psp_type, psp_key):
        assert psp_type == "stripe"
        assert psp_key == "sk_test_secret"
        return True, ""

    async def fake_setup_psp_connection(merchant_id, psp_type, psp_key):
        assert merchant_id == "merch_a"
        return {
            "merchant_id": merchant_id,
            "api_key": "pk_live_generated",
            "psp_type": psp_type,
        }

    async def fake_register_merchant_psp_route(**kwargs):
        assert kwargs["merchant_id"] == "merch_a"

    monkeypatch.setattr(module, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(module, "validate_psp_credentials", fake_validate_psp_credentials)
    monkeypatch.setattr(module, "setup_psp_connection", fake_setup_psp_connection)
    monkeypatch.setattr(module, "register_merchant_psp_route", fake_register_merchant_psp_route)

    response = client.post(
        "/merchant/onboarding/psp/setup",
        json={
            "merchant_id": "merch_a",
            "psp_type": "stripe",
            "psp_key": "sk_test_secret",
        },
    )

    assert response.status_code == 200
    assert response.json()["api_key"] == "pk_live_generated"


def test_psp_setup_already_connected_does_not_return_api_key(monkeypatch) -> None:
    client, module = _build_onboarding_client(
        {"role": "merchant", "merchant_id": "merch_a"}
    )

    async def fake_get_merchant_onboarding(merchant_id):
        assert merchant_id == "merch_a"
        return {
            "merchant_id": "merch_a",
            "status": "approved",
            "auto_approved": False,
            "psp_connected": True,
            "api_key": "pk_live_existing",
            "psp_type": "stripe",
        }

    monkeypatch.setattr(module, "get_merchant_onboarding", fake_get_merchant_onboarding)

    response = client.post(
        "/merchant/onboarding/psp/setup",
        json={
            "merchant_id": "merch_a",
            "psp_type": "stripe",
            "psp_key": "sk_test_secret",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["message"] == "PSP already connected"
    assert "api_key" not in body


def test_psp_setup_allows_admin_cross_tenant(monkeypatch) -> None:
    client, module = _build_onboarding_client({"role": "admin"})

    async def fake_get_merchant_onboarding(merchant_id):
        assert merchant_id == "merch_b"
        return {
            "merchant_id": "merch_b",
            "status": "approved",
            "auto_approved": False,
            "psp_connected": True,
            "api_key": "pk_live_existing",
            "psp_type": "stripe",
        }

    monkeypatch.setattr(module, "get_merchant_onboarding", fake_get_merchant_onboarding)

    response = client.post(
        "/merchant/onboarding/psp/setup",
        json={
            "merchant_id": "merch_b",
            "psp_type": "stripe",
            "psp_key": "sk_test_secret",
        },
    )

    assert response.status_code == 200
    assert "api_key" not in response.json()


def test_universal_product_sync_rejects_cross_tenant_before_db_read(monkeypatch) -> None:
    client, module = _build_universal_sync_client(
        {"role": "merchant", "merchant_id": "merch_a"}
    )

    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("tenant guard should run before merchant lookup")

    monkeypatch.setattr(module, "get_merchant_onboarding", fail_if_called)

    response = client.post(
        "/products/sync-universal/",
        json={"merchant_id": "merch_b", "limit": 5},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "cannot sync products for another merchant"


def test_universal_product_sync_allows_admin_cross_tenant(monkeypatch) -> None:
    client, module = _build_universal_sync_client({"role": "super_admin"})

    async def fake_get_merchant_onboarding(_merchant_id):
        return None

    async def fake_find_connected_store(**_kwargs):
        return None

    monkeypatch.setattr(module, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(module, "find_connected_store", fake_find_connected_store)

    response = client.post(
        "/products/sync-universal/",
        json={"merchant_id": "merch_b", "limit": 5},
    )

    assert response.status_code == 200
    assert response.json()["merchant_id"] == "merch_b"


# The legacy commission endpoints were retired (non-custodial direction:
# "legacy commission endpoints retired; use Stage-1 monetization endpoints").
# They now tombstone with 410 for EVERY caller — merchant, cross-tenant, or
# admin — and must never reach the commission lookup. These tests pin the
# tombstone so a resurrected handler (the tenant-scoping bug class fix07
# guarded against) shows up as a red build.

def test_pending_commissions_retired_returns_410_before_db_read(monkeypatch) -> None:
    client, module = _build_payouts_client(
        {"role": "merchant", "merchant_id": "merch_a"}
    )

    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("retired endpoint must not reach the commission lookup")

    monkeypatch.setattr(module, "_fetch_unpaid_commission_entries", fail_if_called)

    response = client.get("/merchants/merch_b/payouts/pending-commissions")

    assert response.status_code == 410
    assert response.json()["detail"]["status"] == "gone"


def test_generate_from_commissions_retired_returns_410_before_db_read(monkeypatch) -> None:
    client, module = _build_payouts_client(
        {"role": "merchant", "merchant_id": "merch_a"}
    )

    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("retired endpoint must not reach the commission lookup")

    monkeypatch.setattr(module, "_fetch_unpaid_commission_entries", fail_if_called)

    response = client.post("/merchants/merch_b/payouts/generate-from-commissions")

    assert response.status_code == 410
    assert response.json()["detail"]["status"] == "gone"


def test_merchant_payouts_retired_even_for_admin(monkeypatch) -> None:
    client, module = _build_payouts_client({"role": "admin"})

    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("retired endpoint must not reach the commission lookup")

    monkeypatch.setattr(module, "_fetch_unpaid_commission_entries", fail_if_called)

    pending_response = client.get("/merchants/merch_b/payouts/pending-commissions")
    generate_response = client.post("/merchants/merch_b/payouts/generate-from-commissions")

    assert pending_response.status_code == 410
    assert generate_response.status_code == 410
