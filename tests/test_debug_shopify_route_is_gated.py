"""`GET /debug/test-shopify/{merchant_id}` was mounted with no authentication at all.

It selects any merchant's active Shopify row by a caller-supplied `merchant_id`, resolves that
merchant's stored Admin token, spends it against `{domain}/admin/api/2025-10/products.json`, and
returns `shop_domain`, `api_response_code`, `products_sample` (two real products) and, on a non-200,
the upstream `response.text[:500]`.

Two exposures, and the FIRST needs no hostile row:

  1. an unauthenticated caller could read ANY merchant's Shopify catalogue by enumerating
     merchant_id -- no SSRF required, just the absence of a dependency;
  2. if a stored domain were ever not a myshopify host, that merchant's Admin token went to it with
     a read-back oracle attached.

Verified live on api.pivota.cc before the fix: an unauthenticated GET reached the handler (500 from
its own error path, not 401/403). Locally with a stubbed row and no Authorization header it returned
200 and the token went to the row's host.

The three tests below pin three separate defects: the missing dependency, the unpinned host, and a
bare `except Exception` that swallowed the handler's own HTTPExceptions into 500s.
"""
from __future__ import annotations

import socket
import threading
from typing import Any, Dict, Optional

import pytest
from fastapi.testclient import TestClient

import routes.debug_shopify_api as dbg

MERCHANT = "merchant_debugroute"


class CountingListener:
    """Counts connection ATTEMPTS. Bound on `::` -- a v4-only bind cannot observe a dial to
    `[::1]`, so an IPv6 case would assert `count == 0` against code that connected."""

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


def _token(role: str, merchant_id: Optional[str] = None) -> Dict[str, str]:
    from utils.auth import create_access_token

    claims: Dict[str, Any] = {"sub": f"u-{role}", "email": f"{role}@example.com", "role": role}
    if merchant_id:
        claims["merchant_id"] = merchant_id
    return {"Authorization": "Bearer " + create_access_token(claims)}


def _store_row(domain: str):
    async def _fetch_one(query, values=None):
        return {
            "store_id": "store_x",
            "domain": domain,
            "api_key": "shpat_VICTIM_TOKEN_SENTINEL",
            "status": "active",
            "connected_at": None,
        }

    return _fetch_one


# --------------------------------------------------------------------------------------
# 1. The missing dependency.
# --------------------------------------------------------------------------------------

def test_an_unauthenticated_caller_is_refused(client, listener, monkeypatch):
    """The defect exactly as it stood: no header at all, and the handler ran."""
    monkeypatch.setattr(dbg.database, "fetch_one", _store_row("victim.myshopify.com"), raising=False)

    resp = client.get(f"/debug/test-shopify/{MERCHANT}")

    # No packet, whatever the status: an unauthenticated request must not spend the token.
    assert listener.count == 0
    assert resp.status_code in (401, 403), (
        f"unauthenticated caller reached the handler and got {resp.status_code}"
    )


@pytest.mark.parametrize("role", ["merchant", "employee", "outsourced"])
def test_a_non_admin_caller_is_refused(client, monkeypatch, role):
    """Positive-ish counterpart to the 401: the gate is ADMIN, not merely 'authenticated'. A
    merchant token must not read another merchant's catalogue through this route."""
    monkeypatch.setattr(dbg.database, "fetch_one", _store_row("victim.myshopify.com"), raising=False)

    resp = client.get(f"/debug/test-shopify/{MERCHANT}", headers=_token(role, "some_other_merchant"))

    assert resp.status_code in (401, 403)


def test_an_admin_caller_still_reaches_the_handler(client, monkeypatch):
    """Without this, every assertion above is satisfied by a route that refuses everyone -- which
    would look identical to a correct gate while having deleted the endpoint."""
    seen = []

    class _Resp:
        status_code = 200
        text = "{}"

        @staticmethod
        def json():
            return {"products": [{"id": 1}, {"id": 2}, {"id": 3}]}

    async def _fake_get(self, url, *a, **k):  # noqa: ANN001
        seen.append(url)
        return _Resp()

    async def _fake_resolve(**_kwargs):
        return "shpat_VICTIM_TOKEN_SENTINEL", {}

    monkeypatch.setattr(dbg.database, "fetch_one", _store_row("victim.myshopify.com"), raising=False)
    monkeypatch.setattr(dbg, "resolve_shopify_admin_access_token", _fake_resolve, raising=True)
    monkeypatch.setattr("httpx.AsyncClient.get", _fake_get, raising=True)

    resp = client.get(f"/debug/test-shopify/{MERCHANT}", headers=_token("admin"))

    assert resp.status_code == 200, resp.text
    assert seen == ["https://victim.myshopify.com/admin/api/2025-10/products.json"]
    assert resp.json()["product_count"] == 3


# --------------------------------------------------------------------------------------
# 2. The unpinned host -- this route is one of the credential-sending sites.
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("stored_tpl", ["127.0.0.1:{port}", "[::1]:{port}", "evil.example"])
def test_a_hostile_stored_domain_is_refused_before_the_token_is_spent(
    client, listener, monkeypatch, stored_tpl
):
    stored = stored_tpl.format(port=listener.port)
    monkeypatch.setattr(dbg.database, "fetch_one", _store_row(stored), raising=False)

    resp = client.get(f"/debug/test-shopify/{MERCHANT}", headers=_token("admin"))

    # Socket first: it is the signal that survives a transport no spy watches.
    assert listener.count == 0, "the debug route dialled the stored host"
    assert resp.status_code == 400
    assert "myshopify.com" in str(resp.json().get("detail", ""))
    assert stored not in str(resp.json())


# --------------------------------------------------------------------------------------
# 3. The catch-all that swallowed the handler's own refusals.
# --------------------------------------------------------------------------------------

def test_a_missing_store_answers_404_not_500(client, monkeypatch):
    """`except Exception -> HTTPException(500, detail=str(e))` caught this handler's OWN
    HTTPException(404) and re-raised it as a 500 carrying str(e). That is why probing production
    with a nonexistent merchant_id returned 500 -- which also made the missing auth harder to
    notice, since 500 reads like a broken endpoint rather than an open one."""
    async def _none(query, values=None):
        return None

    monkeypatch.setattr(dbg.database, "fetch_one", _none, raising=False)

    resp = client.get(f"/debug/test-shopify/{MERCHANT}", headers=_token("admin"))

    assert resp.status_code == 404
    assert "not found" in str(resp.json().get("detail", "")).lower()


def test_an_unexpected_error_does_not_echo_its_message(client, monkeypatch):
    """The catch-all returned `str(e)` to the caller. Whatever it carries -- a DSN, a host, a driver
    message -- is not the caller's business on a debug route."""
    async def _boom(query, values=None):
        raise RuntimeError("connection to server at 10.0.0.9 port 5432 failed: secret detail")

    monkeypatch.setattr(dbg.database, "fetch_one", _boom, raising=False)

    resp = client.get(f"/debug/test-shopify/{MERCHANT}", headers=_token("admin"))

    assert resp.status_code == 500
    body = str(resp.json())
    assert "10.0.0.9" not in body
    assert "secret detail" not in body
