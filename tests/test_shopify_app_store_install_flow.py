import hashlib
import hmac
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
from starlette.requests import Request


@pytest.mark.asyncio
async def test_shopify_app_store_install_starts_public_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.merchant_store_connections as module

    captured_state = {}

    async def fake_ensure_shell_merchant(domain: str) -> str:
        assert domain == "demo-shop.myshopify.com"
        return "merch_shopify_public"

    async def fake_insert_state(**kwargs):
        captured_state.update(kwargs)

    monkeypatch.setattr(module.settings, "shopify_client_id", "shopify_client_id")
    monkeypatch.setattr(module.settings, "shopify_client_secret", "shopify_secret")
    monkeypatch.setattr(module.settings, "shopify_redirect_uri", "https://api.example.com/integrations/shopify/oauth/callback")
    monkeypatch.setattr(module.settings, "shopify_scopes", "read_products,write_webhooks")
    monkeypatch.setattr(module.settings, "merchant_portal_base_url", "https://merchant.example.com")
    monkeypatch.setattr(module, "_ensure_shopify_marketplace_shell_merchant", fake_ensure_shell_merchant)
    monkeypatch.setattr(module, "_insert_shopify_oauth_state", fake_insert_state)

    response = await module.shopify_app_store_install(
        request=object(),
        shop="https://demo-shop.myshopify.com/admin",
        host="admin-host",
        embedded="1",
        redirect=False,
    )

    assert response["status"] == "success"
    assert response["merchant_id"] == "merch_shopify_public"
    assert response["shop_domain"] == "demo-shop.myshopify.com"
    assert response["install_source"] == "app_store"
    assert response["authorization_url"].startswith("https://demo-shop.myshopify.com/admin/oauth/authorize?")
    assert captured_state["merchant_id"] == "merch_shopify_public"
    assert captured_state["shop_domain"] == "demo-shop.myshopify.com"
    assert captured_state["install_source"] == "app_store"
    assert captured_state["return_to"] == "https://merchant.example.com/app/install/success"
    assert captured_state["host"] == "admin-host"


@pytest.mark.asyncio
async def test_shopify_app_store_callback_redirect_includes_merchant_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.merchant_store_connections as module
    import services.shopify_integration_verify as verify_module

    secret = "shopify_secret"
    state = "oauth-state"
    state_sha = hashlib.sha256(state.encode("utf-8")).hexdigest()
    state_row = {
        "merchant_id": "merch_shopify_public",
        "shop_domain": "demo-shop.myshopify.com",
        "expires_at": module.datetime.now(module.timezone.utc) + module.timedelta(minutes=10),
        "used_at": None,
        "install_source": "app_store",
        "return_to": "https://merchant.example.com/app/install/success",
        "host": "admin-host",
    }

    class FakeDatabase:
        async def fetch_one(self, query: str, values: dict):
            assert values["state_sha256"] == state_sha
            if "SELECT merchant_id" in query:
                return state_row
            if "UPDATE shopify_oauth_states" in query:
                return {"merchant_id": "merch_shopify_public"}
            raise AssertionError(f"unexpected query: {query}")

    class FakeResponse:
        def __init__(self, status_code: int, payload: dict):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url: str, json: dict):
            assert url == "https://demo-shop.myshopify.com/admin/oauth/access_token"
            assert json["code"] == "oauth-code"
            return FakeResponse(200, {"access_token": "admin-token"})

        async def get(self, url: str, headers: dict):
            assert url == "https://demo-shop.myshopify.com/admin/api/2025-10/shop.json"
            assert headers["X-Shopify-Access-Token"] == "admin-token"
            return FakeResponse(
                200,
                {"shop": {"myshopify_domain": "demo-shop.myshopify.com", "name": "Demo Shop"}},
            )

    async def fake_ensure_tables():
        return None

    async def fake_create_storefront_token(**kwargs):
        return "storefront-token"

    async def fake_upsert_store(**kwargs):
        assert kwargs["merchant_id"] == "merch_shopify_public"
        assert kwargs["myshopify_domain"] == "demo-shop.myshopify.com"
        return "store_demo"

    async def fake_register_webhooks_best_effort(**kwargs):
        return {"created": [], "existing": []}

    monkeypatch.setattr(module.settings, "shopify_client_id", "shopify_client_id")
    monkeypatch.setattr(module.settings, "shopify_client_secret", secret)
    monkeypatch.setattr(module.settings, "shopify_redirect_uri", "https://api.example.com/integrations/shopify/oauth/callback")
    monkeypatch.setattr(module, "database", FakeDatabase())
    monkeypatch.setattr(module, "_ensure_shopify_oauth_tables", fake_ensure_tables)
    monkeypatch.setattr(module, "_create_storefront_access_token_best_effort", fake_create_storefront_token)
    monkeypatch.setattr(module, "_upsert_shopify_store_credentials", fake_upsert_store)
    monkeypatch.setattr(module.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(verify_module, "register_webhooks_best_effort", fake_register_webhooks_best_effort)

    params = {
        "shop": "demo-shop.myshopify.com",
        "code": "oauth-code",
        "state": state,
    }
    message = "&".join(f"{key}={value}" for key, value in sorted(params.items()))
    params["hmac"] = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/integrations/shopify/oauth/callback",
            "headers": [],
            "query_string": urlencode(params).encode("utf-8"),
            "server": ("api.example.com", 443),
            "scheme": "https",
        }
    )

    response = await module.shopify_oauth_callback(request)

    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("https://merchant.example.com/app/install/success?")
    query = parse_qs(urlparse(location).query)
    assert query["installed"] == ["shopify"]
    assert query["merchant_id"] == ["merch_shopify_public"]
    assert query["shop"] == ["demo-shop.myshopify.com"]
    assert query["store_id"] == ["store_demo"]
    assert query["status"] == ["success"]


@pytest.mark.asyncio
async def test_shopify_store_credential_upsert_handles_mapping_rows_without_get(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.merchant_store_connections as module

    class RowWithoutCallableGet(dict):
        get = None

    executed = {}

    class FakeDatabase:
        async def fetch_one(self, query: str, values: dict):
            assert values == {
                "merchant_id": "merch_shopify_public",
                "domain": "demo-shop.myshopify.com",
            }
            return RowWithoutCallableGet(
                {
                    "store_id": "store_demo",
                    "api_key": '{"storefront_access_token":"existing-storefront-token"}',
                }
            )

        async def execute(self, query: str, values: dict):
            executed.update(values)

    monkeypatch.setattr(module, "database", FakeDatabase())

    store_id = await module._upsert_shopify_store_credentials(
        merchant_id="merch_shopify_public",
        myshopify_domain="demo-shop.myshopify.com",
        shop_name="Demo Shop",
        access_token="new-admin-token",
        storefront_token=None,
        install_source="app_store",
    )

    assert store_id == "store_demo"
    assert executed["store_id"] == "store_demo"
    token_blob = module.json.loads(executed["api_key"])
    assert token_blob["access_token"] == "new-admin-token"
    assert token_blob["storefront_access_token"] == "existing-storefront-token"
    assert token_blob["install_source"] == "app_store"


def test_resolve_shopify_app_routes_by_install_source(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.merchant_store_connections as module

    s = module.settings
    monkeypatch.setattr(s, "shopify_appstore_client_id", "A_id")
    monkeypatch.setattr(s, "shopify_appstore_client_secret", "A_secret")
    monkeypatch.setattr(s, "shopify_appstore_redirect_uri", "https://api.pivota.cc/cb")
    monkeypatch.setattr(
        s, "shopify_appstore_scopes",
        "read_products,read_orders,read_fulfillments,read_discounts,write_webhooks",
    )
    monkeypatch.setattr(s, "shopify_headless_client_id", "B_id")
    monkeypatch.setattr(s, "shopify_headless_client_secret", "B_secret")
    monkeypatch.setattr(s, "shopify_headless_redirect_uri", "https://api.pivota.cc/cb")
    monkeypatch.setattr(
        s, "shopify_headless_scopes",
        "read_products,read_orders,read_fulfillments,read_discounts,write_webhooks,write_orders",
    )

    # Every OAuth install source resolves to App A (public, read-only). The
    # write-scoped headless app must never be reachable over OAuth.
    for src in ("app_store", "merchant_portal"):
        a = module.resolve_shopify_app(src)
        assert a.label == "appstore"
        assert a.client_id == "A_id"
        assert a.client_secret == "A_secret"
        assert "write_orders" not in a.scopes

    # Non-OAuth / unknown sources fall through to the custom-token headless app.
    for src in ("", None, "whatever"):
        b = module.resolve_shopify_app(src)
        assert b.label == "headless"
        assert b.client_id == "B_id"
        assert b.client_secret == "B_secret"
        assert "write_orders" in b.scopes


def test_resolve_shopify_app_falls_back_to_single_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.merchant_store_connections as module

    s = module.settings
    for field in (
        "shopify_appstore_client_id", "shopify_appstore_client_secret", "shopify_appstore_redirect_uri",
        "shopify_headless_client_id", "shopify_headless_client_secret", "shopify_headless_redirect_uri",
    ):
        monkeypatch.setattr(s, field, None)
    monkeypatch.setattr(s, "shopify_client_id", "single_id")
    monkeypatch.setattr(s, "shopify_client_secret", "single_secret")
    monkeypatch.setattr(s, "shopify_redirect_uri", "https://api.pivota.cc/cb")

    a = module.resolve_shopify_app("app_store")
    assert a.client_id == "single_id" and a.client_secret == "single_secret"
    b = module.resolve_shopify_app("merchant_portal")
    assert b.client_id == "single_id" and b.client_secret == "single_secret"


def _callback_request(query: str) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/integrations/shopify/oauth/callback",
        "query_string": query.encode("utf-8"),
        "headers": [],
    }
    return Request(scope)


@pytest.mark.asyncio
async def test_shopify_oauth_callback_redirects_instead_of_rendering_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The callback is loaded by a browser, so a failure must never render JSON.

    Shopify review flags a raw/pretty-printed JSON page as a display error (2.1.1);
    every failure path must 302 to the portal's install-error page instead.
    """
    import routes.merchant_store_connections as module
    from fastapi import HTTPException
    from fastapi.responses import RedirectResponse

    monkeypatch.setattr(module.settings, "merchant_portal_base_url", "https://merchant.example.com")

    # Each internal failure -> its own non-leaky reason slug on a 302.
    cases = [
        (HTTPException(status_code=400, detail="Invalid or expired OAuth state (reason=state_not_found)"), "state_not_found"),
        (HTTPException(status_code=400, detail="Invalid or expired OAuth state (reason=state_already_used)"), "state_already_used"),
        (HTTPException(status_code=401, detail="Invalid Shopify OAuth signature"), "invalid_signature"),
        (HTTPException(status_code=400, detail="Failed to exchange Shopify access token"), "token_exchange_failed"),
        (HTTPException(status_code=500, detail="Shopify OAuth is not configured"), "not_configured"),
        (RuntimeError("boom"), "install_failed"),  # unexpected crash must also redirect
    ]

    for raised, expected_reason in cases:
        async def fake_impl(_request, _raised=raised):
            raise _raised

        monkeypatch.setattr(module, "_shopify_oauth_callback_impl", fake_impl)

        response = await module.shopify_oauth_callback(
            _callback_request("shop=demo-shop.myshopify.com&code=c&state=s")
        )

        assert isinstance(response, RedirectResponse), f"{expected_reason} did not redirect"
        assert response.status_code == 302
        location = response.headers["location"]
        assert location.startswith("https://merchant.example.com/app/install/error?"), location
        assert f"reason={expected_reason}" in location, location
        # The internal detail must not leak into the browser URL.
        assert "Shopify" not in location and "OAuth" not in location


@pytest.mark.asyncio
async def test_shopify_app_store_install_redirects_instead_of_rendering_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The app entrypoint is loaded by a browser (Shopify appends ?shop=), so a
    missing/malformed shop must never render a raw JSON validation error (2.1.1)
    — it must 302 to the portal's install-error page like the OAuth callback.
    """
    import routes.merchant_store_connections as module
    from fastapi.responses import RedirectResponse

    monkeypatch.setattr(module.settings, "merchant_portal_base_url", "https://merchant.example.com")

    cases = [
        (None, "missing_shop"),
        ("", "missing_shop"),
        ("   ", "missing_shop"),
        ("not-a-shop", "invalid_shop"),
        ("https://evil.example.com/", "invalid_shop"),
    ]

    for shop, expected_reason in cases:
        response = await module.shopify_app_store_install(
            request=object(), shop=shop, host=None, embedded=None, redirect=None
        )

        assert isinstance(response, RedirectResponse), f"shop={shop!r} did not redirect"
        assert response.status_code == 302
        location = response.headers["location"]
        assert location.startswith("https://merchant.example.com/app/install/error?"), location
        assert f"reason={expected_reason}" in location, location
        # The internal detail must not leak into the browser URL.
        assert "myshopify" not in location and "required" not in location


@pytest.mark.asyncio
async def test_shopify_app_store_install_crash_redirects_to_error_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected crash on the entrypoint must also 302, never render JSON."""
    import routes.merchant_store_connections as module
    from fastapi.responses import RedirectResponse

    monkeypatch.setattr(module.settings, "merchant_portal_base_url", "https://merchant.example.com")

    async def boom(domain: str) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(module, "_ensure_shopify_marketplace_shell_merchant", boom)

    response = await module.shopify_app_store_install(
        request=object(), shop="demo-shop.myshopify.com", host=None, embedded=None, redirect=None
    )

    assert isinstance(response, RedirectResponse)
    assert response.status_code == 302
    assert response.headers["location"] == (
        "https://merchant.example.com/app/install/error?reason=install_failed"
    )


@pytest.mark.asyncio
async def test_shopify_app_store_install_default_redirects_to_shopify_oauth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a valid shop and no ?redirect= override, the entrypoint must 302 to
    Shopify's OAuth authorize URL (the normal App Store install path)."""
    import routes.merchant_store_connections as module
    from fastapi.responses import RedirectResponse

    async def fake_ensure_shell_merchant(domain: str) -> str:
        return "merch_shopify_public"

    async def fake_insert_state(**kwargs):
        return None

    monkeypatch.setattr(module.settings, "shopify_client_id", "shopify_client_id")
    monkeypatch.setattr(module.settings, "shopify_client_secret", "shopify_secret")
    monkeypatch.setattr(module.settings, "shopify_redirect_uri", "https://api.example.com/integrations/shopify/oauth/callback")
    monkeypatch.setattr(module.settings, "shopify_scopes", "read_products,write_webhooks")
    monkeypatch.setattr(module.settings, "merchant_portal_base_url", "https://merchant.example.com")
    monkeypatch.setattr(module, "_ensure_shopify_marketplace_shell_merchant", fake_ensure_shell_merchant)
    monkeypatch.setattr(module, "_insert_shopify_oauth_state", fake_insert_state)

    response = await module.shopify_app_store_install(
        request=object(), shop="demo-shop.myshopify.com", host=None, embedded=None, redirect=None
    )

    assert isinstance(response, RedirectResponse)
    assert response.status_code == 302
    assert response.headers["location"].startswith(
        "https://demo-shop.myshopify.com/admin/oauth/authorize?"
    )


def test_wants_shopify_oauth_redirect_is_lenient() -> None:
    """A malformed ?redirect= value must default to redirecting, not 422."""
    import routes.merchant_store_connections as module

    assert module._wants_shopify_oauth_redirect(None) is True
    assert module._wants_shopify_oauth_redirect(True) is True
    assert module._wants_shopify_oauth_redirect(False) is False
    assert module._wants_shopify_oauth_redirect("false") is False
    assert module._wants_shopify_oauth_redirect("0") is False
    assert module._wants_shopify_oauth_redirect("no") is False
    assert module._wants_shopify_oauth_redirect("true") is True
    assert module._wants_shopify_oauth_redirect("banana") is True


@pytest.mark.asyncio
async def test_claim_token_roundtrip_and_tamper_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    """The claim token must be unforgeable — it is the only proof of install."""
    import routes.merchant_store_connections as module
    from fastapi import HTTPException

    monkeypatch.setattr(module.settings, "jwt_secret_key", "test-secret")

    token = module._sign_claim_token(
        {
            "typ": "pivota_shopify_claim",
            "jti": "jti-1",
            "merchant_id": "merch_shopify_abc",
            "shop_domain": "demo-shop.myshopify.com",
            "iat": 0,
            "exp": 9999999999,
        }
    )
    payload = module._verify_claim_token(token)
    assert payload["merchant_id"] == "merch_shopify_abc"
    assert payload["shop_domain"] == "demo-shop.myshopify.com"

    # Flipping the payload without re-signing must fail.
    msg, sig = token.split(".")
    forged_msg = module._b64url(
        b'{"typ":"pivota_shopify_claim","jti":"jti-1","merchant_id":"merch_victim",'
        b'"shop_domain":"demo-shop.myshopify.com","iat":0,"exp":9999999999}'
    )
    with pytest.raises(HTTPException) as exc:
        module._verify_claim_token(f"{forged_msg}.{sig}")
    assert exc.value.status_code == 401

    # A token signed with a different key must fail.
    monkeypatch.setattr(module.settings, "jwt_secret_key", "other-secret")
    with pytest.raises(HTTPException):
        module._verify_claim_token(token)

    # A non-claim token type must fail.
    monkeypatch.setattr(module.settings, "jwt_secret_key", "test-secret")
    wrong_type = module._sign_claim_token({"typ": "something_else", "jti": "x"})
    with pytest.raises(HTTPException):
        module._verify_claim_token(wrong_type)


@pytest.mark.asyncio
async def test_claim_rejects_replayed_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """A consumed claim token cannot re-bind the store (the UPDATE is the lock)."""
    import routes.merchant_store_connections as module
    from fastapi import HTTPException

    monkeypatch.setattr(module.settings, "jwt_secret_key", "test-secret")

    class FakeDatabase:
        async def fetch_one(self, query: str, values: dict):
            if "UPDATE shopify_store_claim_tokens" in query:
                return None  # already used / expired -> no row returned
            raise AssertionError(f"unexpected query before token consumption: {query}")

        async def execute(self, *args, **kwargs):
            raise AssertionError("must not write anything when the token is spent")

    async def fake_ensure_tables():
        return None

    monkeypatch.setattr(module, "database", FakeDatabase())
    monkeypatch.setattr(module, "_ensure_shopify_oauth_tables", fake_ensure_tables)

    token = module._sign_claim_token(
        {
            "typ": "pivota_shopify_claim",
            "jti": "jti-used",
            "merchant_id": "merch_shopify_abc",
            "shop_domain": "demo-shop.myshopify.com",
            "iat": 0,
            "exp": 9999999999,
        }
    )

    with pytest.raises(HTTPException) as exc:
        await module.claim_shopify_store(
            module.ShopifyStoreClaimRequest(
                claim_token=token,
                email="reviewer@example.com",
                password="a-good-password",
            )
        )
    assert exc.value.status_code == 400
    assert "already been used" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_claim_creates_account_and_binds_shell_merchant(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.merchant_store_connections as module

    monkeypatch.setattr(module.settings, "jwt_secret_key", "test-secret")

    executed: list = []

    class FakeDatabase:
        async def fetch_one(self, query: str, values: dict):
            if "UPDATE shopify_store_claim_tokens" in query:
                return {"merchant_id": "merch_shopify_abc"}
            if "FROM users WHERE email" in query:
                return None  # brand-new account
            raise AssertionError(f"unexpected query: {query}")

        async def execute(self, query: str, values: dict = None):
            executed.append((query, values or {}))

    async def fake_ensure_tables():
        return None

    monkeypatch.setattr(module, "database", FakeDatabase())
    monkeypatch.setattr(module, "_ensure_shopify_oauth_tables", fake_ensure_tables)

    token = module._sign_claim_token(
        {
            "typ": "pivota_shopify_claim",
            "jti": "jti-fresh",
            "merchant_id": "merch_shopify_abc",
            "shop_domain": "demo-shop.myshopify.com",
            "iat": 0,
            "exp": 9999999999,
        }
    )

    result = await module.claim_shopify_store(
        module.ShopifyStoreClaimRequest(
            claim_token=token,
            email="Reviewer@Example.com",
            password="a-good-password",
            full_name="Shopify Reviewer",
        )
    )

    assert result["status"] == "success"
    assert result["token"]
    assert result["user"]["merchant_id"] == "merch_shopify_abc"
    assert result["user"]["email"] == "reviewer@example.com"  # normalised

    inserts = [q for q, _ in executed if "INSERT INTO users" in q]
    assert len(inserts) == 1, "should create exactly one user row"
    # The placeholder @pivota.invalid contact must be replaced with the real email.
    contact_updates = [v for q, v in executed if "UPDATE merchant_onboarding" in q]
    assert contact_updates and contact_updates[0]["email"] == "reviewer@example.com"

    # The stored password must be hashed, never the plaintext.
    user_values = [v for q, v in executed if "INSERT INTO users" in q][0]
    assert user_values["password_hash"] != "a-good-password"
    assert module.verify_bcrypt_password("a-good-password", user_values["password_hash"])


@pytest.mark.asyncio
async def test_claim_with_existing_merchant_moves_store_not_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Someone who already runs a merchant keeps it and just gains the store."""
    import routes.merchant_store_connections as module

    monkeypatch.setattr(module.settings, "jwt_secret_key", "test-secret")
    existing_hash = module.hash_password("a-good-password")
    reassigned: dict = {}

    class FakeDatabase:
        async def fetch_one(self, query: str, values: dict):
            if "UPDATE shopify_store_claim_tokens" in query:
                return {"merchant_id": "merch_shopify_abc"}
            if "FROM users WHERE email" in query:
                return {
                    "id": 7,
                    "email": "owner@example.com",
                    "password_hash": existing_hash,
                    "full_name": "Owner",
                    "role": "merchant",
                    "active": True,
                    "merchant_id": "merch_real_123",
                }
            raise AssertionError(f"unexpected query: {query}")

        async def execute(self, query: str, values: dict = None):
            if "UPDATE users" in query:
                raise AssertionError("must not repoint an existing account at the shell merchant")

    async def fake_ensure_tables():
        return None

    async def fake_reassign(*, from_merchant_id, to_merchant_id, shop_domain):
        reassigned.update(
            {"from": from_merchant_id, "to": to_merchant_id, "shop": shop_domain}
        )

    monkeypatch.setattr(module, "database", FakeDatabase())
    monkeypatch.setattr(module, "_ensure_shopify_oauth_tables", fake_ensure_tables)
    monkeypatch.setattr(module, "_reassign_store_to_merchant", fake_reassign)

    token = module._sign_claim_token(
        {
            "typ": "pivota_shopify_claim",
            "jti": "jti-existing",
            "merchant_id": "merch_shopify_abc",
            "shop_domain": "demo-shop.myshopify.com",
            "iat": 0,
            "exp": 9999999999,
        }
    )

    result = await module.claim_shopify_store(
        module.ShopifyStoreClaimRequest(
            claim_token=token,
            email="owner@example.com",
            password="a-good-password",
        )
    )

    assert result["user"]["merchant_id"] == "merch_real_123"
    assert reassigned == {
        "from": "merch_shopify_abc",
        "to": "merch_real_123",
        "shop": "demo-shop.myshopify.com",
    }


@pytest.mark.asyncio
async def test_claim_rejects_wrong_password_for_existing_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The claim token proves the install, NOT ownership of an arbitrary account."""
    import routes.merchant_store_connections as module
    from fastapi import HTTPException

    monkeypatch.setattr(module.settings, "jwt_secret_key", "test-secret")

    class FakeDatabase:
        async def fetch_one(self, query: str, values: dict):
            if "UPDATE shopify_store_claim_tokens" in query:
                return {"merchant_id": "merch_shopify_abc"}
            if "FROM users WHERE email" in query:
                return {
                    "id": 7,
                    "email": "victim@example.com",
                    "password_hash": module.hash_password("the-real-password"),
                    "full_name": "Victim",
                    "role": "merchant",
                    "active": True,
                    "merchant_id": "merch_victim",
                }
            raise AssertionError(f"unexpected query: {query}")

        async def execute(self, *args, **kwargs):
            raise AssertionError("must not mutate anything on a failed auth")

    async def fake_ensure_tables():
        return None

    monkeypatch.setattr(module, "database", FakeDatabase())
    monkeypatch.setattr(module, "_ensure_shopify_oauth_tables", fake_ensure_tables)

    token = module._sign_claim_token(
        {
            "typ": "pivota_shopify_claim",
            "jti": "jti-x",
            "merchant_id": "merch_shopify_abc",
            "shop_domain": "demo-shop.myshopify.com",
            "iat": 0,
            "exp": 9999999999,
        }
    )

    with pytest.raises(HTTPException) as exc:
        await module.claim_shopify_store(
            module.ShopifyStoreClaimRequest(
                claim_token=token,
                email="victim@example.com",
                password="not-the-password",
            )
        )
    assert exc.value.status_code == 401
