"""The last copy of the Shopify host rule, and the one webhook site that spends a credential.

`routes/webhook_routes.py:846` held a byte-identical private copy of `_canonicalize_shop_domain`,
untouched by #2075 which unified the rule everywhere else. Two definitions of "which host may
receive an Admin token" are free to drift, and this file pins the merge.

WHAT IS AND IS NOT PINNED, because the distinction is the whole design:

  * SIX call sites use the canonicaliser. FIVE of them COMPARE hosts -- the untrusted
    `X-Shopify-Shop-Domain` header against the stores connected to a merchant, and an
    `app/uninstalled` UPDATE keyed on the domain. Those are deliberately NOT pinned to
    `*.myshopify.com`. Pinning the allowlist at webhook_routes.py:3714 would make any store whose
    stored domain is not canonical stop matching its own webhooks -- Shopify would keep delivering
    and we would keep 404ing, silently, which is a worse outcome than the thing being prevented and
    is not what the credential guard is for. `test_allowlist_still_matches_a_non_canonical_store`
    pins that non-pinning so a later "tighten everything" pass cannot quietly break deliveries.

  * ONE call site turns a stored domain into an Admin API URL: the webhook registration loop POSTs
    `X-Shopify-Access-Token` to `{domain}/admin/api/2025-10/webhooks.json` once per topic -- about
    twenty requests -- and `resolve_shopify_admin_access_token` POSTs client credentials to
    `{domain}/admin/oauth/access_token` before that. THAT one is pinned, and pinned above the
    resolver, which previously received the raw column.

REAL SOCKETS: a listening socket counts connection attempts whatever HTTP client made them, so
these cannot be satisfied later by swapping clients. Bound on `::` -- a v4-only bind cannot observe
a dial to `[::1]`, it just gets ECONNREFUSED, so an IPv6 case would assert `count == 0` against code
that connected perfectly well.
"""
from __future__ import annotations

import socket
import threading
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

from services.shopify_domain import canonicalize_shop_domain
import routes.webhook_routes as wr

MERCHANT = "merchant_webhookpin"


class CountingListener:
    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("::", 0))
        self._sock.listen(16)
        self.port = self._sock.getsockname()[1]
        self.count = 0
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

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


def _auth() -> Dict[str, str]:
    """EMPLOYEE, not merchant. The route is gated on get_current_employee -- it points a store's
    full-PII order webhooks at a caller-supplied callback_base_url, so it is ops-only by design."""
    from utils.auth import create_access_token

    return {
        "Authorization": "Bearer "
        + create_access_token({"sub": "u-ops", "email": "ops@example.com", "role": "employee"})
    }


# --------------------------------------------------------------------------------------
# 1. One definition of the rule.
# --------------------------------------------------------------------------------------

def test_webhook_routes_uses_the_shared_canonicaliser():
    """Not a style assertion. Two copies of the rule that decides where a credential may be sent is
    the drift this merge exists to end, and identity is the only way to state that in a test."""
    assert wr._canonicalize_shop_domain is canonicalize_shop_domain


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("shop.myshopify.com", "shop.myshopify.com"),
        ("SHOP.MyShopify.Com", "shop.myshopify.com"),
        ("https://shop.myshopify.com/admin", "shop.myshopify.com"),
        # Canonicalising is NOT pinning: a non-myshopify host still canonicalises, because the
        # comparison sites need it to. The pin lives at the one site that builds a URL.
        ("shop.brand-example.com", "shop.brand-example.com"),
        ("", None),
        (None, None),
    ],
)
def test_canonicaliser_behaviour_is_unchanged(raw, expected):
    assert wr._canonicalize_shop_domain(raw) == expected


# --------------------------------------------------------------------------------------
# 2. The site that spends a credential.
# --------------------------------------------------------------------------------------

def _register_route(client, listener, stored, monkeypatch):
    async def _fake_active_stores(_merchant_id):
        return [{
            "store_id": "store_x",
            "platform": "shopify",
            "domain": stored,
            "status": "active",
            "api_key": "shpat_TEST_TOKEN_SENTINEL",
            "api_key_raw": "shpat_TEST_TOKEN_SENTINEL",
        }]

    async def _fake_onboarding(_merchant_id):
        return {"merchant_id": MERCHANT, "business_name": "Test"}

    monkeypatch.setattr(wr, "get_merchant_active_stores", _fake_active_stores, raising=True)
    if hasattr(wr, "get_merchant_onboarding"):
        monkeypatch.setattr(wr, "get_merchant_onboarding", _fake_onboarding, raising=True)

    # callback_base_url is a bare `str` parameter on the handler, so FastAPI reads it from the
    # QUERY STRING, not the body. Sending it as JSON yields a 422 before the handler ever runs.
    return client.post(
        f"/webhooks/register/shopify/{MERCHANT}",
        params={"callback_base_url": "https://api.example"},
        headers=_auth(),
    )


@pytest.mark.parametrize(
    "stored_tpl",
    ["127.0.0.1:{port}", "[::1]:{port}", "evil.example", "shop.myshopify.com.evil.example"],
)
def test_webhook_registration_refuses_a_hostile_stored_domain(
    client, listener, monkeypatch, stored_tpl
):
    stored = stored_tpl.format(port=listener.port)
    resp = _register_route(client, listener, stored, monkeypatch)

    # THE SOCKET FIRST. Ordered after a status assertion this is never reached on a build that
    # connects, and the connection -- the thing under test -- goes unreported.
    assert listener.count == 0, "the registration loop dialled the stored host"
    assert resp.status_code == 400
    assert "myshopify.com" in str(resp.json().get("detail", ""))
    # The untrusted value must not be echoed back to the caller.
    assert stored not in str(resp.json())


def test_webhook_registration_still_reaches_a_valid_shop(client, listener, monkeypatch):
    """Positive counterpart. Without it every refusal above is satisfied by a route that refuses
    everything, which would take webhook registration offline for every real merchant."""
    seen: List[str] = []

    class _Resp:
        status_code = 201

        @staticmethod
        def json():
            return {"webhook": {"id": 1}}

    async def _fake_post(self, url, *a, **k):  # noqa: ANN001
        seen.append(url)
        return _Resp()

    async def _fake_resolve(**_kwargs):
        return "shpat_TEST_TOKEN_SENTINEL", {}

    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post, raising=True)
    monkeypatch.setattr(wr, "resolve_shopify_admin_access_token", _fake_resolve, raising=True)

    resp = _register_route(client, listener, "cosrx-renewal.myshopify.com", monkeypatch)

    assert resp.status_code == 200, resp.text
    assert seen, "the validated domain never reached a Shopify request"
    # Exact URL, not merely the host: a mutant keeping the host but moving the path would survive.
    assert set(seen) == {"https://cosrx-renewal.myshopify.com/admin/api/2025-10/webhooks.json"}
    assert listener.count == 0


def test_the_token_resolver_receives_the_pinned_host_not_the_raw_column(client, monkeypatch):
    """The resolver POSTs client credentials to {domain}/admin/oauth/access_token, so it must be
    handed the pinned value. Before this change it was called with the raw column and the
    canonicalisation happened afterwards."""
    got: Dict[str, Any] = {}

    class _Resp:
        status_code = 201

        @staticmethod
        def json():
            return {"webhook": {"id": 1}}

    async def _fake_post(self, url, *a, **k):  # noqa: ANN001
        return _Resp()

    async def _fake_resolve(**kwargs):
        got.update(kwargs)
        return "shpat_TEST_TOKEN_SENTINEL", {}

    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post, raising=True)
    monkeypatch.setattr(wr, "resolve_shopify_admin_access_token", _fake_resolve, raising=True)

    _register_route(client, None, "HTTPS://CosRx-Renewal.MyShopify.Com/admin", monkeypatch)

    assert got.get("shop_domain") == "cosrx-renewal.myshopify.com"


# --------------------------------------------------------------------------------------
# 3. The non-pinning that has to stay non-pinned.
# --------------------------------------------------------------------------------------

def test_allowlist_still_matches_a_non_canonical_store():
    """Guards the deliberate gap. If someone later "tightens" the comparison sites to
    *.myshopify.com, a store connected under any other domain stops matching its own webhook
    deliveries and we 404 Shopify silently. Canonicalising must keep accepting such a host."""
    for host in ("shop.brand-example.com", "checkout.example.co.uk", "legacy-store.example"):
        assert wr._canonicalize_shop_domain(host) == host
        assert wr._canonicalize_shop_domain(f"https://{host}/") == host
