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
