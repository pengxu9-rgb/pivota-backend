"""Phase 2 of the Shopify credential-host sweep: the webhook helper and the ops routes.

`register_webhooks_best_effort` formatted `https://{shop_domain}/admin/api/{v}/webhooks.json` with
NO canonicalisation at all -- so unlike the caller-side handling it replaced, a PORT survived. Its
one pinned caller (`verify_shopify_integration`) pinned before calling in; the two ops routes did
not, and `/ops/v1/integrations/shopify/resubscribe-all` sweeps EVERY active Shopify row, so a single
bad row was exercised by a routine ops action rather than by an attacker finding a route.

WHY THE PIN SITS AT THE HELPER, not only at the three call sites: a helper that receives an unpinned
host from several callers is one fix, not several, and it covers the caller nobody has written yet.
The call sites are pinned TOO, because the helper pin cannot cover what happens BEFORE it --
`resolve_shopify_admin_access_token` POSTs client_id/client_secret to
`{domain}/admin/oauth/access_token`, and the returns service takes its own host argument.

Real listening socket, bound `::` so a dial to `[::1]` is observable, asserted BEFORE any status.
"""
from __future__ import annotations

import json
import socket
import threading
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

import routes.ops_shopify_integration_routes as ops
import services.shopify_integration_verify as siv

MERCHANT = "merchant_opspin"


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
    """Employee: these are ops routes, gated on get_current_employee."""
    from utils.auth import create_access_token

    return {
        "Authorization": "Bearer "
        + create_access_token({"sub": "u-ops", "email": "ops@example.com", "role": "employee"})
    }


def _store(domain: str) -> Dict[str, Any]:
    # A credentials blob, NOT a plain shpat_ string: with a plain token the resolver returns
    # without a packet, and the socket assertions below would be measuring nothing.
    blob = json.dumps({"client_id": "cid", "client_secret": "csec"})
    return {
        "store_id": "store_x",
        "merchant_id": MERCHANT,
        "platform": "shopify",
        "domain": domain,
        "status": "active",
        "api_key": blob,
        "api_key_raw": blob,
    }


# --------------------------------------------------------------------------------------
# 1. The helper itself.
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "host_tpl",
    ["127.0.0.1:{port}", "[::1]:{port}", "evil.example", "shop.myshopify.com.evil.example"],
)
async def test_register_webhooks_best_effort_refuses_a_non_myshopify_host(listener, host_tpl):
    host = host_tpl.format(port=listener.port)

    with pytest.raises(ValueError) as excinfo:
        await siv.register_webhooks_best_effort(
            shop_domain=host,
            access_token="shpat_TEST_TOKEN_SENTINEL",
            merchant_id=MERCHANT,
            callback_base_url="https://api.example",
            topics=["orders/create"],
        )

    assert listener.count == 0, "the webhook helper dialled the host it was handed"
    assert "myshopify.com" in str(excinfo.value)
    assert host not in str(excinfo.value)


async def test_register_webhooks_best_effort_still_posts_for_a_real_shop(monkeypatch):
    """Positive counterpart: the helper must still register for a valid shop, or this pin has
    silently disabled webhook subscription everywhere."""
    seen: List[str] = []

    class _Resp:
        status_code = 201
        text = ""

        @staticmethod
        def json():
            return {"webhook": {"id": 1}}

    async def _fake_post(self, url, *a, **k):  # noqa: ANN001
        seen.append(url)
        return _Resp()

    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post, raising=True)

    report = await siv.register_webhooks_best_effort(
        shop_domain="HTTPS://CosRx-Renewal.MyShopify.Com/admin",
        access_token="shpat_TEST_TOKEN_SENTINEL",
        merchant_id=MERCHANT,
        callback_base_url="https://api.example",
        topics=["orders/create", "orders/paid"],
    )

    assert len(report["created"]) == 2
    # Canonicalised, and every request went to the canonical host -- not just the first.
    assert set(seen) == {"https://cosrx-renewal.myshopify.com/admin/api/2025-10/webhooks.json"}
    assert len(seen) == 2


async def test_a_port_on_a_real_shop_is_stripped_rather_than_dialled(listener, monkeypatch):
    """The helper did NO canonicalisation, so a port reached the URL verbatim and a row storing
    `shop.myshopify.com:PORT` dialled that port. The pin does not REFUSE this -- the host is a
    genuine shop -- it CANONICALISES, so the port cannot survive into the request. Asserted as the
    URL that was built, because "no exception" would not have distinguished the two."""
    seen: List[str] = []

    class _Resp:
        status_code = 201
        text = ""

        @staticmethod
        def json():
            return {"webhook": {"id": 1}}

    async def _fake_post(self, url, *a, **k):  # noqa: ANN001
        seen.append(url)
        return _Resp()

    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post, raising=True)

    await siv.register_webhooks_best_effort(
        shop_domain=f"cosrx-renewal.myshopify.com:{listener.port}",
        access_token="shpat_TEST_TOKEN_SENTINEL",
        merchant_id=MERCHANT,
        callback_base_url="https://api.example",
        topics=["orders/create"],
    )

    assert seen == ["https://cosrx-renewal.myshopify.com/admin/api/2025-10/webhooks.json"]
    assert str(listener.port) not in seen[0], "the port survived into the Admin API URL"


# --------------------------------------------------------------------------------------
# 2. The ops routes, which the helper pin cannot fully cover.
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("stored_tpl", ["127.0.0.1:{port}", "[::1]:{port}", "evil.example"])
def test_resubscribe_refuses_before_the_credential_exchange(client, listener, monkeypatch, stored_tpl):
    """The route resolves a token BEFORE calling the helper, and that resolve POSTs the client
    secret to {domain}/admin/oauth/access_token. httpx is left REAL here, so the socket is the
    thing under test."""
    stored = stored_tpl.format(port=listener.port)

    async def _primary(_merchant_id):
        return _store(stored)

    monkeypatch.setattr(ops, "get_primary_store", _primary, raising=True)

    resp = client.post(
        f"/ops/v1/merchants/{MERCHANT}/integrations/shopify/resubscribe",
        json={"callback_base_url": "https://api.example"},
        headers=_auth(),
    )

    assert listener.count == 0, "the credential exchange dialled the stored host"
    assert resp.status_code == 400
    assert "myshopify.com" in str(resp.json().get("detail", ""))


def test_resubscribe_all_skips_a_bad_row_and_keeps_going(client, listener, monkeypatch):
    """The sweep touches EVERY active store. One unpinnable row must be recorded and skipped, not
    abort the sweep and not silently look like a token problem."""
    good = _store("cosrx-renewal.myshopify.com")
    good["merchant_id"] = "merchant_good"
    # A static token: _token_needs_refresh() is False, so the real resolver returns it immediately
    # and this row sends no packet either.
    good["api_key"] = "shpat_STATIC_TOKEN"
    good["api_key_raw"] = "shpat_STATIC_TOKEN"
    bad = _store(f"127.0.0.1:{listener.port}")
    bad["merchant_id"] = "merchant_bad"

    async def _fetch_all(query, values=None):
        return [bad, good]

    # The resolver is left REAL. Stubbing it made `listener.count == 0` below vacuous -- review
    # moved the credential exchange ABOVE the pin check and all 11 tests still passed, while the
    # sweep POSTed client_id/client_secret to the raw row's host for every unpinnable row. Only the
    # error string carried a kill. With the real resolver, the bad row's host is the listener, so
    # the socket assertion is what fails if the ordering is ever inverted again.
    #
    # The GOOD row must therefore not reach the resolver either, or it would dial a real shop from
    # a unit test: its credentials blob is absent, so the resolver returns without a packet.
    async def _fake_register(**kwargs):
        return {"created": [{"topic": "orders/create"}], "already_exists": [], "failed": []}

    monkeypatch.setattr(ops.database, "fetch_all", _fetch_all, raising=False)
    monkeypatch.setattr(ops, "register_webhooks_best_effort", _fake_register, raising=True)

    resp = client.post(
        "/ops/v1/integrations/shopify/resubscribe-all",
        json={"callback_base_url": "https://api.example", "limit": 10},
        headers=_auth(),
    )

    assert listener.count == 0
    assert resp.status_code == 200, resp.text
    results = {r["merchant_id"]: r for r in resp.json()["results"]}

    # The bad row is reported with its OWN reason, not "missing_credentials".
    assert results["merchant_bad"]["error"] == "domain_not_a_myshopify_host"
    # ...and the sweep carried on: the good row was still resubscribed.
    assert results["merchant_good"]["error"] is None
    assert results["merchant_good"]["created"]


def test_returns_sync_refuses_a_hostile_stored_domain(client, listener, monkeypatch):
    """The third ops site. It passes the stored domain AND the raw api_key column to the returns
    service, which builds its own Admin URLs."""
    async def _primary(_merchant_id):
        return _store(f"127.0.0.1:{listener.port}")

    monkeypatch.setattr(ops, "get_primary_store", _primary, raising=True)

    resp = client.post(
        f"/ops/v1/merchants/{MERCHANT}/integrations/shopify/returns/sync",
        json={"limit": 5},
        headers=_auth(),
    )

    assert listener.count == 0, "the returns sync dialled the stored host"
    assert resp.status_code == 400
