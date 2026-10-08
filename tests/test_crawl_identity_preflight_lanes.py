"""Web Bot Auth on the Tier B / purchasability-sweep preflight lanes (services/crawl_identity.py).

Flag off: the transports and headers these lanes build are exactly today's (desktop-Chrome UA,
plain AsyncHTTPTransport). Flag on with a key: every request is signed INNERMOST (under the pacer,
so a pacing wait cannot lapse the signature) and carries the declared PivotaBot UA, which is what
Shopify's Web Bot Auth registration names.
"""
import base64

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from services import crawl_identity as ci


def _pem() -> str:
    return Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in (ci.FLAG_ENV, ci.KEY_ENV, ci.AGENT_ENV, ci.FORMAT_ENV):
        monkeypatch.delenv(name, raising=False)
    ci.reset_for_tests()
    yield
    ci.reset_for_tests()


def _sign_on(monkeypatch):
    monkeypatch.setenv(ci.FLAG_ENV, "1")
    monkeypatch.setenv(ci.KEY_ENV, _pem())


CHROME = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/128.0"


async def test_a_signed_request_declares_pivotabot_and_an_unsigned_one_keeps_its_lane_ua():
    seen = []
    signed = ci.SigningTransport(httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200)),
                                 ci.Signer(Ed25519PrivateKey.generate()))
    async with httpx.AsyncClient(transport=signed, headers={"User-Agent": CHROME}) as client:
        await client.get("https://shop.test/products.json")
    assert seen[-1].headers["user-agent"] == ci.DECLARED_USER_AGENT and "signature" in seen[-1].headers

    class _Broken(ci.Signer):
        def sign(self, request):
            request.headers["User-Agent"] = "half-written"
            raise RuntimeError("boom")

    failing = ci.SigningTransport(httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200)),
                                  _Broken(Ed25519PrivateKey.generate()))
    async with httpx.AsyncClient(transport=failing, headers={"User-Agent": CHROME}) as client:
        await client.get("https://shop.test/products.json")
    assert seen[-1].headers["user-agent"] == CHROME and "signature" not in seen[-1].headers


def test_the_preflights_own_client_is_signed_only_when_on(monkeypatch):
    from services import shopify_cart_link_preflight as pf

    assert pf._own_client_transport() == {}
    _sign_on(monkeypatch)
    assert isinstance(pf._own_client_transport()["transport"], ci.SigningTransport)


@pytest.mark.parametrize("factory", [
    lambda: __import__("jobs.tierb_cart_link_eligibility", fromlist=["x"])._default_inner_transport(),
    lambda: __import__("jobs.merchant_purchasability_sweep", fromlist=["x"])._inner_transport(None),
])
def test_the_inner_transports_sign_only_when_on(monkeypatch, factory):
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    off = factory()
    assert isinstance(off, httpx.AsyncHTTPTransport) and not isinstance(off, ci.SigningTransport)
    _sign_on(monkeypatch)
    assert isinstance(factory(), ci.SigningTransport)


async def test_signing_is_innermost_so_the_signature_is_made_after_the_pacing_wait(monkeypatch):
    from jobs.tierb_cart_link_eligibility import PacedTransport

    now = {"t": 1_800_000_000.0}

    class _WaitingPacer:
        async def acquire(self):
            now["t"] += 600  # a long pacing wait: ten times the signature's 60 s lifetime
            return now["t"]

        def remaining(self):
            return 10_000

    seen = []
    signer = ci.Signer(Ed25519PrivateKey.generate(), clock=lambda: now["t"])
    inner = ci.SigningTransport(httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200)), signer)
    async with httpx.AsyncClient(transport=PacedTransport(inner, _WaitingPacer())) as client:
        await client.get("https://shop.test/cart/1:1")
    sig_input = seen[0].headers["signature-input"]
    created = int(sig_input.split(";created=", 1)[1].split(";", 1)[0])
    assert created == int(now["t"])  # signed AFTER the wait, so still valid when it leaves


def test_the_sweeps_proxy_vantage_is_never_signed(monkeypatch):
    """It stands in for a buyer's network and leaves from an unregistered address."""
    from jobs import merchant_purchasability_sweep as sweep

    _sign_on(monkeypatch)
    via = sweep._inner_transport("http://vantage.test:3128")
    assert isinstance(via, httpx.AsyncHTTPTransport) and not isinstance(via, ci.SigningTransport)


def _mock_http(monkeypatch, handler):
    """Every httpx.AsyncHTTPTransport the code under test builds becomes a mock, so the signing
    wrapper the code itself adds around it is what is exercised."""
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda *a, **k: httpx.MockTransport(handler))


def _assert_every_request_signed_as_pivotabot(seen):
    assert seen, "no request went out"
    for request in seen:
        assert request.headers["user-agent"] == ci.DECLARED_USER_AGENT, request.url
        assert request.headers["signature"].startswith("sig1=:") and "signature-input" in request.headers, request.url


async def test_a_real_tierb_run_signs_every_request_it_sends(monkeypatch):
    """The real job.run and the real preflight, flag on: every request, redirect hops included."""
    from jobs import tierb_cart_link_eligibility as job
    from services.tierb_cart_link_merchants import Merchant

    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path.startswith("/cart/"):
            return httpx.Response(302, headers={"location": "https://judydoll.com/checkouts/cn/T"})
        return httpx.Response(200, text="<html></html>")

    _mock_http(monkeypatch, handler)
    _sign_on(monkeypatch)
    now = {"t": 1000.0}

    async def _sleep(s):
        now["t"] += s

    await job.run(environ={job.GATE_ENV: "true"}, dry_run=True, merchants=[Merchant("judydoll.com", "US", "49819267301653")],
                  clock=lambda: now["t"], sleep=_sleep, emit=lambda line: None, stamp="T", retry_delay_s=0)
    _assert_every_request_signed_as_pivotabot(seen)


async def test_the_preflight_without_a_client_signs_through_its_own(monkeypatch):
    from services import shopify_cart_link_preflight as pf

    seen = []
    _mock_http(monkeypatch, lambda r: seen.append(r) or httpx.Response(200, text="<html></html>"))
    _sign_on(monkeypatch)
    await pf.preflight("judydoll.com", market="US", variant_id="49819267301653", click_id="c_test")
    _assert_every_request_signed_as_pivotabot(seen)
