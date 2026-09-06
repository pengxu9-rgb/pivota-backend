"""The host a Shopify Admin token may be sent to, and the host that may be STORED.

WHY THIS FILE EXISTS. `merchant_stores.domain` becomes `https://{domain}/admin/api/...` carrying a
live `X-Shopify-Access-Token` in ~30 places in this repo and in the PIVOTA-Agent gateway. The OAuth
entry point validated the caller's INPUT with `_validate_myshopify_domain`, but the value actually
PERSISTED was `shop.json`'s `myshopify_domain` taken straight from the upstream response and
re-validated by neither side. Validating one string and storing a different one is not a guard.

Three separate holes are pinned here, and they fail independently:

  1. `POST /integrations/shopify/connect` took `shop_domain` from the REQUEST BODY, checked only
     that it was non-empty -- under a comment claiming to validate it -- and then sent the
     client-credentials exchange and an Admin-token `GET .../shop.json` to that host. An
     authenticated merchant could aim this service's egress at any host or port and read the
     outcome from the status echoed back in the 400 detail.
  2. Both OAuth paths persisted the upstream `myshopify_domain` unchecked.
  3. `verify_shopify_integration` read `merchant_stores.domain` straight out of the table and built
     four Admin API URLs from it -- the read-path twin of the gateway hole closed in
     PIVOTA-Agent #2145.

REAL SOCKETS, NOT ONLY MOCK ASSERTIONS. httpx opens connections through layers a module-level spy
does not necessarily observe, and the property under test is that NO PACKET LEAVES -- not that some
particular function was not called. A listening socket counts connection attempts whatever made
them, so these tests cannot be satisfied later by swapping HTTP clients. The listener binds `::`
rather than 127.0.0.1: a v4-only bind cannot observe a dial to `[::1]`, it just gets ECONNREFUSED,
so an IPv6 case would assert `count == 0` against code that connected perfectly well.
"""
from __future__ import annotations

import asyncio
import socket
import threading
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

from services.shopify_domain import normalize_myshopify_domain

MERCHANT = "merchant_hostpin"


class CountingListener:
    """A bare TCP listener that counts connection ATTEMPTS. A completed TLS handshake is not what is
    being measured: reaching the port at all is the SSRF."""

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("::", 0))
        self._sock.listen(16)
        self.port = self._sock.getsockname()[1]
        self.count = 0
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            self.count += 1
            try:
                conn.close()
            except OSError:
                pass

    def close(self) -> None:
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass


@pytest.fixture()
def listener():
    lis = CountingListener()
    try:
        yield lis
    finally:
        lis.close()


@pytest.fixture(scope="module")
def client() -> TestClient:
    import main

    return TestClient(main.app)


def _auth(merchant_id: str = MERCHANT) -> Dict[str, str]:
    from utils.auth import create_access_token

    return {
        "Authorization": "Bearer "
        + create_access_token(
            {
                "sub": f"u-{merchant_id}",
                "email": f"{merchant_id}@example.com",
                "role": "merchant",
                "merchant_id": merchant_id,
            }
        )
    }


# --------------------------------------------------------------------------------------
# 1. The shared contract itself.
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value",
    [
        # Private and link-local literals -- the classic SSRF targets.
        "127.0.0.1", "127.0.0.1:8080", "169.254.169.254", "10.0.0.9", "192.168.1.1",
        "[::1]", "[::1]:8080", "metadata.google.internal",
        # A PUBLIC host is refused just as hard. A private-range check alone would still hand the
        # credential to an attacker-owned domain, which is why this pins the SHAPE instead.
        "evil.example", "8.8.8.8",
        # Suffix and authority confusion.
        "notmyshopify.com", "shop.myshopify.com.evil.example", "myshopify.com",
        "shop.myshopify.com.", "a.b.myshopify.com", "-shop.myshopify.com",
        "shop.myshopify.com@evil.example", "evil.example/#.myshopify.com",
        "https://evil.example/@shop.myshopify.com",
        # Encoding tricks. `。` (U+3002) is the one that separates this from the JS side: WHATWG
        # `new URL()` applies IDNA and maps it onto a real dot, so the gateway had to avoid URL
        # parsing entirely. Python does not, but it is pinned here so the two stay in agreement.
        "shop。myshopify.com", "shop%2emyshopify.com", "ѕhop.myshopify.com",
        "", "   ", None,
    ],
)
def test_refuses_everything_that_is_not_an_admin_host(value):
    assert normalize_myshopify_domain(value) is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("shop.myshopify.com", "shop.myshopify.com"),
        ("Shop.MyShopify.Com", "shop.myshopify.com"),
        ("  shop.myshopify.com  ", "shop.myshopify.com"),
        ("https://shop.myshopify.com", "shop.myshopify.com"),
        ("https://shop.myshopify.com/admin", "shop.myshopify.com"),
        ("cosrx-renewal.myshopify.com", "cosrx-renewal.myshopify.com"),
        ("92sfrj-bi.myshopify.com", "92sfrj-bi.myshopify.com"),
    ],
)
def test_accepts_and_canonicalises_exactly(raw, expected):
    # The EXACT output is the contract, not merely "not None": a mutant returning the raw input
    # would satisfy every refusal above while storing whatever it was handed.
    assert normalize_myshopify_domain(raw) == expected


# --------------------------------------------------------------------------------------
# 2. POST /integrations/shopify/connect -- the request-supplied host.
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "shop_domain_tpl",
    [
        "127.0.0.1:{port}",
        "[::1]:{port}",
        "evil.example",
        "shop.myshopify.com@evil.example",
        "shop。myshopify.com",
    ],
)
def test_connect_refuses_a_hostile_shop_domain_before_any_connection(
    client, listener, shop_domain_tpl
):
    body = {
        "merchant_id": MERCHANT,
        "shop_domain": shop_domain_tpl.format(port=listener.port),
        "access_token": "shpat_TEST_TOKEN_SENTINEL",
    }
    resp = client.post("/integrations/shopify/connect", json=body, headers=_auth())

    # THE SOCKET FIRST, deliberately. pytest reports the first failing assertion, and this is the
    # one signal that survives a client no spy watches. Ordered after the status assertions, it was
    # never reached against the unfixed build: those failed on `500 != 400` and the connection this
    # exists to catch went unreported.
    assert listener.count == 0, "a packet reached the listener before the host was validated"
    assert resp.status_code == 400
    assert "myshopify.com" in resp.json().get("detail", "")


def test_connect_still_accepts_a_real_shop_domain(client, listener, monkeypatch):
    """The positive counterpart. Without it every refusal above is satisfied by a handler that
    rejects everything, which would break every real merchant install while looking green."""
    seen: List[str] = []

    async def _fake_get(self, url, *args, **kwargs):  # noqa: ANN001
        seen.append(url)
        raise RuntimeError("stop after the host check")

    monkeypatch.setattr("httpx.AsyncClient.get", _fake_get, raising=True)

    body = {
        "merchant_id": MERCHANT,
        "shop_domain": "cosrx-renewal.myshopify.com",
        "access_token": "shpat_TEST_TOKEN_SENTINEL",
    }
    resp = client.post("/integrations/shopify/connect", json=body, headers=_auth())

    # It got PAST the shape check -- it reached the Shopify call and failed there instead.
    assert resp.status_code != 400 or "myshopify.com" not in resp.json().get("detail", "")
    assert seen, "the validated domain never reached the Shopify request"
    assert seen[0] == "https://cosrx-renewal.myshopify.com/admin/api/2025-10/shop.json"
    assert listener.count == 0


# --------------------------------------------------------------------------------------
# 3. verify_shopify_integration -- the host read back OUT of merchant_stores.
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "stored_tpl",
    ["127.0.0.1:{port}", "[::1]:{port}", "evil.example", "shop.myshopify.com.evil.example"],
)
async def test_verify_refuses_a_hostile_stored_domain_before_any_connection(
    listener, monkeypatch, stored_tpl
):
    import services.shopify_integration_verify as svc

    stored = stored_tpl.format(port=listener.port)

    async def _fake_primary_store(_merchant_id):
        return {
            "store_id": "store_x",
            "platform": "shopify",
            "domain": stored,
            "api_key": "shpat_TEST_TOKEN_SENTINEL",
            "api_key_raw": "shpat_TEST_TOKEN_SENTINEL",
        }

    async def _fake_resolve(**_kwargs):
        return "shpat_TEST_TOKEN_SENTINEL", {}

    # Patched on the MODULE that imported them, not on their defining module -- the names were
    # bound at import time, so patching the source has no effect here.
    monkeypatch.setattr(svc, "get_primary_store", _fake_primary_store, raising=True)
    monkeypatch.setattr(svc, "resolve_shopify_admin_access_token", _fake_resolve, raising=True)

    # Catching BROADLY on purpose. Against the unfixed build this raises httpx.ConnectError from
    # deep inside the client, which would escape `pytest.raises(ValueError)` and end the test before
    # the socket assertion below ever ran -- reporting a connection error instead of the connection.
    raised: Optional[BaseException] = None
    try:
        await svc.verify_shopify_integration(
            merchant_id=MERCHANT, callback_base_url="https://api.example"
        )
    except BaseException as exc:  # noqa: BLE001
        raised = exc

    assert listener.count == 0, "a packet reached the listener before the stored host was validated"
    assert isinstance(raised, ValueError), f"expected a local refusal, got {raised!r}"
    assert "myshopify.com" in str(raised)
    # The untrusted value must not be echoed into the error, which reaches logs and API surfaces.
    assert stored not in str(raised)


async def test_verify_does_not_persist_a_hostile_upstream_myshopify_domain(monkeypatch):
    """Hole 2, at the verify service: the canonical domain came from the upstream response and was
    written to merchant_stores.domain unchecked. A bad value must mean 'do not canonicalise'."""
    import services.shopify_integration_verify as svc

    writes: List[str] = []

    async def _fake_primary_store(_merchant_id):
        return {
            "store_id": "store_x",
            "platform": "shopify",
            "domain": "good-shop.myshopify.com",
            "api_key": "shpat_TEST_TOKEN_SENTINEL",
            "api_key_raw": "shpat_TEST_TOKEN_SENTINEL",
        }

    async def _fake_resolve(**_kwargs):
        return "shpat_TEST_TOKEN_SENTINEL", {}

    async def _fake_get_json(*, url, access_token, timeout_s=12.0):  # noqa: ANN001
        # The upstream says the canonical host is somewhere else entirely.
        return {"shop": {"myshopify_domain": "evil.example", "name": "Shop"}}

    class _DB:
        async def execute(self, query, values=None):
            if values and "domain" in (values or {}):
                writes.append(values["domain"])
            return None

        async def fetch_one(self, *a, **k):
            return None

        async def fetch_all(self, *a, **k):
            return []

    monkeypatch.setattr(svc, "get_primary_store", _fake_primary_store, raising=True)
    monkeypatch.setattr(svc, "resolve_shopify_admin_access_token", _fake_resolve, raising=True)
    monkeypatch.setattr(svc, "_shopify_get_json", _fake_get_json, raising=True)
    monkeypatch.setattr(svc, "database", _DB(), raising=True)

    try:
        await svc.verify_shopify_integration(
            merchant_id=MERCHANT, callback_base_url="https://api.example"
        )
    except Exception:
        # Downstream steps (scopes, webhooks, policies) are not this test's business; the domain
        # decision is made before them and the recorded writes are what is asserted.
        pass

    assert "evil.example" not in writes, "a hostile upstream domain reached merchant_stores.domain"

# --------------------------------------------------------------------------------------
# 4. The two derivation sites in routes/merchant_store_connections.py.
#
# Added after review: the original file asserted these holes "fail independently" and they did not.
# Reverting BOTH re-checks -- the two lines that close the reported write path -- left all 43 tests
# green, because the connect route's own input guard refused the hostile value first and the
# derivation never ran on anything bad. To reach the derivation the INPUT must be valid and only the
# UPSTREAM response hostile, which is exactly the real-world shape: Shopify is reached correctly and
# answers with a myshopify_domain we then store.
# --------------------------------------------------------------------------------------

def _shop_json(myshopify_domain):
    return {"shop": {"myshopify_domain": myshopify_domain, "name": "A Shop"}}


def test_connect_does_not_store_a_hostile_upstream_myshopify_domain(client, monkeypatch):
    """Mutant that survived review: `shop_info.get("myshopify_domain") or shop_domain` unchecked."""
    stored: List[Dict[str, Any]] = []

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return _shop_json("evil.example")

    async def _fake_get(self, url, *a, **k):  # noqa: ANN001
        return _Resp()

    # POST must be stubbed too. The handler best-effort creates a storefront token and registers
    # webhooks; leaving those unstubbed sent REAL requests to the live shop from a unit test and the
    # insert under assertion was never reached.
    async def _fake_post(self, url, *a, **k):  # noqa: ANN001
        return _Resp()

    async def _fake_execute(query, values=None):
        if values and isinstance(values, dict) and "domain" in values:
            stored.append(values["domain"])
        return None

    async def _fake_fetch_one(*a, **k):
        return None

    async def _fake_fetch_all(*a, **k):
        return []

    import routes.merchant_store_connections as msc

    monkeypatch.setattr("httpx.AsyncClient.get", _fake_get, raising=True)
    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post, raising=True)
    monkeypatch.setattr(msc.database, "execute", _fake_execute, raising=False)
    monkeypatch.setattr(msc.database, "fetch_one", _fake_fetch_one, raising=False)
    monkeypatch.setattr(msc.database, "fetch_all", _fake_fetch_all, raising=False)

    body = {
        "merchant_id": MERCHANT,
        # VALID input -- the guard at the top of the handler must not be what refuses here.
        "shop_domain": "cosrx-renewal.myshopify.com",
        "access_token": "shpat_TEST_TOKEN_SENTINEL",
        # Required alongside a static token since the token-refresh change; without them the handler
        # 400s on "requires client_id+client_secret" and the derivation under test never runs.
        "client_id": "test_client_id",
        "client_secret": "test_client_secret",
    }
    client.post("/integrations/shopify/connect", json=body, headers=_auth())

    assert "evil.example" not in stored, (
        "the upstream myshopify_domain reached merchant_stores.domain unchecked"
    )
    # Positive counterpart: something WAS written, and it was the validated fallback. Without this
    # the assertion above is satisfied by a handler that stores nothing at all.
    assert stored, "no domain was written; the assertion above would be vacuous"
    assert set(stored) == {"cosrx-renewal.myshopify.com"}


async def test_verify_persists_a_valid_but_different_upstream_domain(monkeypatch):
    """Positive counterpart for the persist test. Setting `canonical_myshopify = ""` deletes the
    whole canonicalisation write -- which keeps merchant_stores.domain matching the
    X-Shopify-Shop-Domain webhook header -- and survived every test before this one existed."""
    import services.shopify_integration_verify as svc

    writes: List[str] = []

    async def _fake_primary_store(_merchant_id):
        return {
            "store_id": "store_x",
            "platform": "shopify",
            "domain": "old-handle.myshopify.com",
            "api_key": "shpat_TEST_TOKEN_SENTINEL",
            "api_key_raw": "shpat_TEST_TOKEN_SENTINEL",
        }

    async def _fake_resolve(**_kwargs):
        return "shpat_TEST_TOKEN_SENTINEL", {}

    async def _fake_get_json(*, url, access_token, timeout_s=12.0):  # noqa: ANN001
        return _shop_json("new-handle.myshopify.com")

    class _DB:
        async def execute(self, query, values=None):
            if values and "domain" in (values or {}):
                writes.append(values["domain"])
            return None

        async def fetch_one(self, *a, **k):
            return None

        async def fetch_all(self, *a, **k):
            return []

    monkeypatch.setattr(svc, "get_primary_store", _fake_primary_store, raising=True)
    monkeypatch.setattr(svc, "resolve_shopify_admin_access_token", _fake_resolve, raising=True)
    monkeypatch.setattr(svc, "_shopify_get_json", _fake_get_json, raising=True)
    monkeypatch.setattr(svc, "database", _DB(), raising=True)

    try:
        await svc.verify_shopify_integration(
            merchant_id=MERCHANT, callback_base_url="https://api.example"
        )
    except Exception:
        pass

    assert writes == ["new-handle.myshopify.com"], (
        "a valid canonical domain must still be persisted; this is the write the "
        "X-Shopify-Shop-Domain webhook match depends on"
    )


# --------------------------------------------------------------------------------------
# 5. The two sibling read paths in the same file, found by review.
#
# Both read merchant_stores.domain and build Admin API URLs from it, exactly as
# verify_shopify_integration did. Proven to reach a listener before the guard was added.
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("stored_tpl", ["127.0.0.1:{port}", "[::1]:{port}", "evil.example"])
def test_token_diagnostic_refuses_a_hostile_stored_domain(client, listener, monkeypatch, stored_tpl):
    import routes.merchant_store_connections as msc

    stored = stored_tpl.format(port=listener.port)

    async def _fake_primary_store(_merchant_id):
        return {
            "store_id": "store_x",
            "platform": "shopify",
            "domain": stored,
            "api_key": "shpat_TEST_TOKEN_SENTINEL",
            "api_key_raw": "shpat_TEST_TOKEN_SENTINEL",
        }

    monkeypatch.setattr(msc, "get_primary_store", _fake_primary_store, raising=True)

    resp = client.get(
        "/integrations/shopify/token/diagnostic",
        params={"merchant_id": MERCHANT},
        headers=_auth(),
    )

    assert listener.count == 0, "the token diagnostic dialled the stored host"
    assert resp.status_code == 400
    assert "myshopify.com" in resp.json().get("detail", "")


@pytest.mark.parametrize("stored_tpl", ["127.0.0.1:{port}", "[::1]:{port}", "evil.example"])
def test_products_sync_refuses_a_hostile_stored_domain(client, listener, monkeypatch, stored_tpl):
    import routes.merchant_store_connections as msc

    stored = stored_tpl.format(port=listener.port)

    # This route reads merchant_stores with database.fetch_one directly -- NOT get_primary_store.
    # Mocking the wrong seam made the first version of this test 500 on "no such table" while
    # asserting nothing about the guard.
    async def _fake_fetch_one(query, values=None):
        if "api_key" in str(query):
            return {"api_key": "shpat_TEST_TOKEN_SENTINEL"}
        return {
            "store_id": "store_x",
            "platform": "shopify",
            "domain": stored,
            "status": "active",
        }

    monkeypatch.setattr(msc.database, "fetch_one", _fake_fetch_one, raising=False)

    resp = client.post(
        "/integrations/shopify/products/sync",
        json={"merchant_id": MERCHANT},
        headers=_auth(),
    )

    assert listener.count == 0, "the product sync dialled the stored host"
    assert resp.status_code == 400

def test_oauth_callback_does_not_store_a_hostile_upstream_myshopify_domain(client, monkeypatch):
    """The OAuth callback is the PRIMARY install path and the one the reported hole named, yet its
    re-check survived every test until this one: the connect route's own input guard refused hostile
    values before any derivation ran, so reverting BOTH derivations left the suite green.

    HMAC is stubbed deliberately -- the property under test is what happens to the domain the
    upstream returns, not signature verification, which has its own coverage."""
    import routes.merchant_store_connections as msc

    stored: List[str] = []

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return _shop_json("evil.example")

    class _TokenResp:
        status_code = 200

        @staticmethod
        def json():
            return {"access_token": "shpat_TEST_TOKEN_SENTINEL", "scope": "read_orders"}

    async def _fake_post(self, url, *a, **k):  # noqa: ANN001
        return _TokenResp() if "access_token" in str(url) else _Resp()

    async def _fake_get(self, url, *a, **k):  # noqa: ANN001
        return _Resp()

    async def _fake_fetch_one(query, values=None):
        q = " ".join(str(query).split())
        if "shopify_oauth_states" in q and q.upper().startswith("UPDATE"):
            return {"merchant_id": MERCHANT}          # the anti-replay consumption
        if "shopify_oauth_states" in q:
            return {                                   # the state lookup
                "merchant_id": MERCHANT,
                "shop_domain": "cosrx-renewal.myshopify.com",
                "used_at": None,
                "expires_at": None,
                "install_source": "test",
                "return_to": "",
            }
        return None

    async def _fake_execute(query, values=None):
        if values and isinstance(values, dict) and "domain" in values:
            stored.append(values["domain"])
        return None

    async def _fake_fetch_all(*a, **k):
        return []

    # The token exchange refuses with 500 "not configured" unless a client id/secret resolve.
    monkeypatch.setattr(msc.settings, "shopify_client_id", "test_client_id", raising=False)
    monkeypatch.setattr(msc.settings, "shopify_client_secret", "test_client_secret", raising=False)
    monkeypatch.setattr(msc, "_shopify_oauth_verify_hmac", lambda **k: True, raising=True)
    monkeypatch.setattr("httpx.AsyncClient.get", _fake_get, raising=True)
    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post, raising=True)
    monkeypatch.setattr(msc.database, "fetch_one", _fake_fetch_one, raising=False)
    monkeypatch.setattr(msc.database, "execute", _fake_execute, raising=False)
    monkeypatch.setattr(msc.database, "fetch_all", _fake_fetch_all, raising=False)

    client.get(
        "/integrations/shopify/oauth/callback",
        params={
            "shop": "cosrx-renewal.myshopify.com",
            "code": "abc123",
            "state": "s" * 24,
            "hmac": "deadbeef",
        },
        follow_redirects=False,
    )

    assert "evil.example" not in stored, (
        "the OAuth callback stored the upstream myshopify_domain unchecked"
    )
    assert stored, "no domain was written; the assertion above would be vacuous"
    assert set(stored) == {"cosrx-renewal.myshopify.com"}

