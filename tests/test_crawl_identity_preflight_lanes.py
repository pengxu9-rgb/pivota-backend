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


def test_user_agent_switches_only_while_signing(monkeypatch):
    chrome = "Mozilla/5.0 (Macintosh) Chrome/128.0"
    assert ci.user_agent(chrome) == chrome
    monkeypatch.setenv(ci.FLAG_ENV, "1")  # on, no key: unsigned, so the old UA stays
    assert ci.user_agent(chrome) == chrome
    monkeypatch.setenv(ci.KEY_ENV, _pem())
    assert ci.user_agent(chrome) == ci.DECLARED_USER_AGENT
    assert "PivotaBot/1.0" in ci.DECLARED_USER_AGENT


def test_preflight_headers_are_unchanged_off_and_declared_when_signing(monkeypatch):
    from services import shopify_cart_link_preflight as pf

    assert pf._headers() == pf._HEADERS and "Chrome/128.0" in pf._headers()["User-Agent"]
    _sign_on(monkeypatch)
    assert pf._headers() == {**pf._HEADERS, "User-Agent": ci.DECLARED_USER_AGENT}


@pytest.mark.parametrize("factory", [
    lambda: __import__("jobs.tierb_cart_link_eligibility", fromlist=["x"])._default_inner_transport(),
    lambda: __import__("jobs.merchant_purchasability_sweep", fromlist=["x"])._inner_transport(None),
    lambda: __import__("jobs.merchant_purchasability_sweep", fromlist=["x"])._inner_transport("http://vantage.test:3128"),
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


def test_the_sweep_builds_its_headers_through_the_switch():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "jobs" / "merchant_purchasability_sweep.py").read_text()
    assert '"User-Agent": crawl_identity.user_agent(USER_AGENT)' in src
    assert '"User-Agent": USER_AGENT,' not in src
