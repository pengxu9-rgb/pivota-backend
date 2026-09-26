"""Sweep phase 3: the Shopify returns family.

`sync_shopify_returns_best_effort` and `probe_shopify_return_eligibility_best_effort` are the two
functions this module exports to other modules, and both took a `shop_domain` argument straight from
`merchant_stores.domain` with a live Admin token beside it. Four callers:

  * `readiness/service.py:1200` (sync) and `:1346` (probe) — FIVE call sites, not four. Both are
    fed by `_resolve_shopify_return_context`, which passes the raw host to
    `resolve_shopify_admin_access_token` one await EARLIER; that resolver POSTs the app's
    client_id + client_secret, which mint tokens for every shop the app is installed on. Pinning
    only these helpers left the stronger credential exposed on this path — see the chokepoint pin
    in `services/shopify_access_token_service.py` and the local pin at `readiness/service.py:1147`.
  * `routes/agent_commerce.py:511` — raw column AND the raw `api_key` column as the token, then the
    same raw host to the eligibility probe at :521
  * `routes/merchant_risk_api.py`  — raw column, same shape
  * `routes/ops_shopify_integration_routes.py` — pinned at the call site in #2086

Only TWO of them pass the raw `api_key` column as the token (`agent_commerce`, `merchant_risk_api`);
`readiness` passes a RESOLVED token. An earlier draft of this file said three, and lumping readiness
in with the raw-token callers is exactly what erased the resolver hop from view.

PINNED AT THE HELPER, not at the four callers. #2086 argued for exactly this and then applied it to
one helper and not this one, which is why the merchant_risk_api and agent_commerce twins stayed open
after it merged.

IT REFUSES BY RETURNING, NOT BY RAISING. `readiness/service.py` and `routes/agent_commerce.py` call
these with no exception handling, so a raise would convert a recoverable miss into an unhandled 500 —
the regression this sweep keeps checking other people's helpers for. `{"ok": False}` is the shape
this module already uses for failure, and no caller subscripts the result, so the refusal cannot
KeyError either.

Real listening socket, bound `::` so a dial to `[::1]` is observable, asserted BEFORE any return
value. httpx is left REAL in the refusal tests: nothing sits between these helpers and the socket.
"""
from __future__ import annotations

import socket
import threading
from typing import Any, Dict, List

import pytest

import services.shopify_returns_service as rs

MERCHANT = "merchant_returnspin"
TOKEN = "shpat_RETURNS_TOKEN_SENTINEL"


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


HOSTILE = ["127.0.0.1:{port}", "[::1]:{port}", "evil.example", "shop.myshopify.com.evil.example"]


# --------------------------------------------------------------------------------------
# 1. sync_shopify_returns_best_effort
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("host_tpl", HOSTILE)
async def test_sync_refuses_a_hostile_host_without_dialling(listener, host_tpl):
    host = host_tpl.format(port=listener.port)

    result = await rs.sync_shopify_returns_best_effort(
        merchant_id=MERCHANT,
        shop_domain=host,
        access_token=TOKEN,
        api_version="2025-10",
        limit=5,
    )

    # Socket first: it is the signal that survives a transport no spy watches.
    assert listener.count == 0, "the returns sync dialled the host it was handed"
    assert result["ok"] is False
    assert result["reason"] == "domain_not_a_myshopify_host"
    # The untrusted value must not come back to the caller, which puts it in an API response.
    assert host not in str(result)


@pytest.mark.parametrize("host_tpl", HOSTILE)
async def test_sync_refuses_by_RETURNING_not_raising(listener, host_tpl):
    """Separate from the assertion above on purpose. Two callers -- readiness/service.py:1200 and
    routes/agent_commerce.py:511 -- have no try/except, so a raise here is a 500 on a route that
    previously answered. The refusal must stay a value."""
    host = host_tpl.format(port=listener.port)

    try:
        result = await rs.sync_shopify_returns_best_effort(
            merchant_id=MERCHANT, shop_domain=host, access_token=TOKEN,
            api_version="2025-10", limit=5,
        )
    except BaseException as exc:  # noqa: BLE001
        pytest.fail(f"the pin raised instead of returning: {exc!r}")

    # The success-path keys are present, so a caller reading .get("fetched") sees 0 rather than None.
    assert result["fetched"] == 0
    assert result["upserted"] == 0
    assert listener.count == 0


async def test_sync_still_runs_for_a_real_shop(monkeypatch):
    """Positive counterpart. Without it every refusal above is satisfied by a helper that refuses
    everything, which would silently disable returns sync for every merchant."""
    seen: List[str] = []

    async def _fake_graphql(*, shop_domain, access_token, query, variables=None, api_version=None, **k):
        seen.append(shop_domain)
        return {"returns": {"nodes": []}}

    monkeypatch.setattr(rs, "shopify_admin_graphql", _fake_graphql, raising=True)

    result = await rs.sync_shopify_returns_best_effort(
        merchant_id=MERCHANT,
        # Deliberately not already canonical: scheme, case and path must be normalised, not refused.
        shop_domain="HTTPS://CosRx-Renewal.MyShopify.Com/admin",
        access_token=TOKEN,
        api_version="2025-10",
        limit=5,
    )

    assert result["ok"] is True
    assert seen, "the validated host never reached the GraphQL client"
    assert set(seen) == {"cosrx-renewal.myshopify.com"}


async def test_a_port_on_a_real_shop_is_stripped_not_refused(listener, monkeypatch):
    """A genuine shop carrying a port is CANONICALISED, not refused -- and the port must not survive
    into the request. Asserted as the host actually passed downstream, because "did not raise" would
    not distinguish the two outcomes."""
    seen: List[str] = []

    async def _fake_graphql(*, shop_domain, access_token, query, variables=None, api_version=None, **k):
        seen.append(shop_domain)
        return {"returns": {"nodes": []}}

    monkeypatch.setattr(rs, "shopify_admin_graphql", _fake_graphql, raising=True)

    await rs.sync_shopify_returns_best_effort(
        merchant_id=MERCHANT,
        shop_domain=f"cosrx-renewal.myshopify.com:{listener.port}",
        access_token=TOKEN,
        api_version="2025-10",
        limit=5,
    )

    assert seen == ["cosrx-renewal.myshopify.com"]
    assert str(listener.port) not in seen[0]
    assert listener.count == 0


# --------------------------------------------------------------------------------------
# 2. probe_shopify_return_eligibility_best_effort
#
# agent_commerce.py calls this with the SAME raw host immediately after the sync call, so pinning
# only the sync would have left the sibling open inside one handler.
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("host_tpl", HOSTILE)
async def test_probe_refuses_a_hostile_host_without_dialling(listener, host_tpl):
    host = host_tpl.format(port=listener.port)

    result = await rs.probe_shopify_return_eligibility_best_effort(
        shop_domain=host,
        access_token=TOKEN,
        api_version="2025-10",
        shopify_order_id="123",
    )

    assert listener.count == 0, "the eligibility probe dialled the host it was handed"
    assert result["ok"] is False
    assert result["reason"] == "domain_not_a_myshopify_host"
    assert host not in str(result)


@pytest.mark.parametrize(
    "given",
    [
        "cosrx-renewal.myshopify.com",
        # Non-canonical forms must be NORMALISED, not refused...
        "HTTPS://CosRx-Renewal.MyShopify.Com/admin",
        # ...and a port must not survive into the request. A mutant that validated the host but
        # dropped `shop_domain = pinned` passed all 16 tests before this parametrisation existed:
        # the NAME cleared the pin, and the raw host-with-port was what reached the Admin client
        # with a live token. The sync half had this test; the probe did not.
        "cosrx-renewal.myshopify.com:51285",
    ],
)
async def test_probe_still_runs_for_a_real_shop_and_normalises_the_host(monkeypatch, given):
    seen: List[str] = []

    async def _fake_introspect(*, shop_domain, access_token, api_version, type_name):
        seen.append(shop_domain)
        return ["returns", "returnableFulfillments"]

    async def _fake_graphql(*, shop_domain, access_token, query, variables=None, api_version=None, **k):
        seen.append(shop_domain)
        return {"order": {"id": "gid://shopify/Order/123"}}

    monkeypatch.setattr(rs, "_introspect_type_fields", _fake_introspect, raising=True)
    monkeypatch.setattr(rs, "shopify_admin_graphql", _fake_graphql, raising=True)

    result = await rs.probe_shopify_return_eligibility_best_effort(
        shop_domain=given,
        access_token=TOKEN,
        api_version="2025-10",
        shopify_order_id="123",
    )

    assert result["ok"] is True
    assert result["shopify_order_id"] == "123"
    assert seen, "the validated host never reached the Admin client"
    # The HOST THAT ARRIVED is the contract -- "did not raise" cannot distinguish a normalised host
    # from the raw one.
    assert set(seen) == {"cosrx-renewal.myshopify.com"}


# --------------------------------------------------------------------------------------
# 3. The callers the helper pin is supposed to cover.
# --------------------------------------------------------------------------------------

def test_every_external_caller_goes_through_a_pinned_entry_point():
    """The two functions other modules import are the two pinned here. If a third public entry point
    that takes a shop_domain is ever exported, this fails and the pin has to move with it -- which is
    the whole argument for pinning the helper rather than the four call sites."""
    import inspect

    exported_taking_a_host = {
        name
        for name, fn in vars(rs).items()
        if not name.startswith("_")
        and inspect.iscoroutinefunction(fn)
        and getattr(fn, "__module__", None) == rs.__name__
        and "shop_domain" in inspect.signature(fn).parameters
    }

    assert exported_taking_a_host == {
        "sync_shopify_returns_best_effort",
        "probe_shopify_return_eligibility_best_effort",
        "fetch_shopify_returns",
        "fetch_shopify_returns_via_shop",
    }, exported_taking_a_host

    # Of those four, only the first two are imported by other modules; the fetch_* pair is internal
    # to this file and is only ever reached through them. Each of the two carries the pin in its OWN
    # body -- checked per function rather than per file, since a single import at the top would
    # satisfy a file-level grep while one of them went unpinned.
    for pinned in ("sync_shopify_returns_best_effort", "probe_shopify_return_eligibility_best_effort"):
        body = inspect.getsource(getattr(rs, pinned))
        assert "normalize_myshopify_domain(shop_domain)" in body, f"{pinned} does not pin its host"
        # Before the FIRST await, not before a named callee: sync_shopify_returns_best_effort
        # reaches the network through internal helpers, so `shopify_admin_graphql` does not appear
        # in its own body. "Nothing is awaited before the host is pinned" is the property that
        # actually holds for both, and it is the one that matters.
        assert body.index("normalize_myshopify_domain(shop_domain)") < body.index("await "), (
            f"{pinned} awaits something before it pins its host"
        )


# --------------------------------------------------------------------------------------
# 4. The chokepoint, and the readiness path that reached it with a raw host.
#
# Found by review: pinning the two returns helpers was decorative on the readiness path, because
# `_resolve_shopify_return_context` hands the raw host to `resolve_shopify_admin_access_token` one
# await EARLIER, and that POSTs the app's client_id + client_secret -- credentials that mint tokens
# for EVERY shop the app is installed on, not just this one. Proven at a socket before the fix.
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("host_tpl", HOSTILE)
async def test_the_credential_exchange_itself_refuses_a_non_myshopify_host(listener, host_tpl):
    """The chokepoint. This is the only place in the repo that POSTs client_id + client_secret, and
    it is reached from ~27 call sites, so pinning it closes the credential-exchange leg everywhere
    at once rather than per caller. httpx is left REAL: nothing sits between this and the socket."""
    from services.shopify_access_token_service import exchange_shopify_client_credentials_token

    host = host_tpl.format(port=listener.port)

    token, expires_in, error = await exchange_shopify_client_credentials_token(
        shop_domain=host, client_id="cid", client_secret="csec",
    )

    assert listener.count == 0, "the client-credentials exchange dialled the host it was handed"
    assert token is None
    assert error == "shop_domain_not_myshopify"


async def test_the_credential_exchange_refuses_by_returning_not_raising(listener):
    """Its callers read the reason out of meta["refresh_error"] and carry on with the token they
    already had. A raise here would surface at ~27 sites, most of which have no handler."""
    from services.shopify_access_token_service import exchange_shopify_client_credentials_token

    try:
        token, _expires, error = await exchange_shopify_client_credentials_token(
            shop_domain=f"127.0.0.1:{listener.port}", client_id="cid", client_secret="csec",
        )
    except BaseException as exc:  # noqa: BLE001
        pytest.fail(f"the chokepoint pin raised instead of returning: {exc!r}")

    assert (token, error) == (None, "shop_domain_not_myshopify")
    assert listener.count == 0


async def test_the_credential_exchange_still_runs_for_a_real_shop(monkeypatch):
    """Positive counterpart: a refusal-everything chokepoint would silently stop every token
    refresh in the codebase, which no other test here would notice."""
    from services import shopify_access_token_service as sats

    seen = []

    class _Resp:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"access_token": "shpat_NEW", "expires_in": 3600}

    async def _fake_post(self, url, *a, **k):  # noqa: ANN001
        seen.append(url)
        return _Resp()

    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post, raising=True)

    token, _expires, error = await sats.exchange_shopify_client_credentials_token(
        shop_domain="HTTPS://CosRx-Renewal.MyShopify.Com/admin", client_id="cid", client_secret="csec",
    )

    assert error is None
    assert token == "shpat_NEW"
    # Normalised, not merely accepted.
    assert seen == ["https://cosrx-renewal.myshopify.com/admin/oauth/access_token"]


def test_a_refused_probe_is_audible_in_the_eligibility_summary():
    """The refusal shape omits every key the summary's warnings are derived from, so before this the
    surface answered "checked, found nothing" -- no warnings at all -- on a merchant whose returns
    were broken precisely because we never reached Shopify. That is the shape #2074 was merged to
    stop, and an operator hits this endpoint to find out WHY returns are broken."""
    import inspect
    import readiness.service as rsvc

    body = inspect.getsource(rsvc._build_return_eligibility_summary)
    assert 'platform_probe.get("ok") is False' in body, (
        "a refused probe produces no warning, so the summary reports a clean bill of health"
    )
    assert "shopify_return_probe_refused" in body


async def test_readiness_refuses_a_non_myshopify_store_before_building_a_context(monkeypatch):
    """The readiness local pin, which the chokepoint alone does NOT make observable.

    With only the chokepoint pinned, a hostile stored domain still flows into the returned context
    whenever `shopify_cfg` carries a fallback access_token: the resolver refuses, the fallback token
    fills in, and the raw host is handed onward to the returns helpers. Those helpers then refuse --
    so there is no leak either way -- but the operator gets an opaque `ok: False` from deep in the
    sync instead of this surface's own CHECKOUT_RETURN_SYNC_UNAVAILABLE, which
    routes/readiness_internal.py maps to a 409 naming the missing piece.

    That fallback-token case is the only one that distinguishes the two, which is why it is the one
    tested: without it the mutant that unpins readiness passes every other test in this file."""
    import readiness.service as rsvc

    class _Checkout:
        merchant_id = "m1"
        order_id = "o1"
        # The builder reads this before anything else; an empty payload takes its own early branch.
        session_payload = {"merchant_alpha_mode": "real_merchant_alpha"}

    class _Journal:
        async def get_checkout_session(self, checkout_id):
            return _Checkout()

    async def _order(_order_id):
        return {"id": "o1", "merchant_id": "m1", "shopify_order_id": "123"}

    async def _primary(_merchant_id):
        return {"store_id": "s1", "platform": "shopify", "domain": "evil.example",
                "api_key": "shpat_X", "api_key_raw": "shpat_X"}

    async def _cfg(_merchant_id):
        # A fallback token IS present -- the case that separates the local pin from the chokepoint.
        return {"access_token": "shpat_FALLBACK", "api_version": "2025-10"}

    monkeypatch.setattr(rsvc, "get_default_journal", lambda: _Journal(), raising=True)
    monkeypatch.setattr(rsvc, "get_order", _order, raising=True)
    monkeypatch.setattr(rsvc, "get_primary_store", _primary, raising=True)
    monkeypatch.setattr(rsvc, "_get_shopify_config_for_merchant", _cfg, raising=True)

    with pytest.raises(ValueError) as excinfo:
        await rsvc._resolve_shopify_return_context("m1", "c1")

    detail = excinfo.value.args[0]
    assert detail["code"] == "CHECKOUT_RETURN_SYNC_UNAVAILABLE"
    # The refusal names WHICH half was missing, and it is the host -- not the token, which was there.
    assert detail["shop_domain_present"] is False
    assert detail["access_token_present"] is True
    assert "evil.example" not in str(detail)
