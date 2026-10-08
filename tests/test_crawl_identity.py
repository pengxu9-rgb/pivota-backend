"""services/crawl_identity.py + routes/crawl_identity.py: Web Bot Auth signing of crawl requests.

The bytes are pinned against draft-meunier-web-bot-auth-architecture-05 Appendix A.2 (Ed25519,
RFC 9421 test-key-ed25519), not against our own reading of the spec:

  A.2.1  "@authority" only                     -> signature reproduced EXACTLY (Ed25519 is deterministic)
  A.2.3  Signature-Agent as an sf-string        -> headers and signature reproduced EXACTLY
         (Cloudflare's required form, our default)
  A.2.2  Signature-Agent as a dictionary member -> our signature BASE equals the draft's printed
         base and verifies. The draft's PUBLISHED signature for A.2.2 does not verify against its
         own printed base under any reading we tried, so it cannot be a byte target.
"""
import base64
import logging

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services import crawl_identity as ci

# RFC 9421 Appendix B.1.4, test-key-ed25519 (PKCS#8 PEM).
RFC_TEST_KEY = (
    "-----BEGIN PRIVATE KEY-----\n"
    "MC4CAQAwBQYDK2VwBCIEIJ+DYvh6SEqVTm50DFtMDoQikTmiCqirVv9mWG9qfSnF\n"
    "-----END PRIVATE KEY-----\n"
)
DRAFT_KEYID = "poqkLGiymh_W0uP6PZFw-dvez3QJT5SolqXBCW38r0U"
DRAFT_AGENT = "https://signature-agent.test"


def _pem(key) -> str:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


def _verify(key: Ed25519PrivateKey, base: str, signature_header: str) -> bool:
    raw = base64.b64decode(signature_header.split("=:", 1)[1].rstrip(":"))
    try:
        key.public_key().verify(raw, base.encode())
        return True
    except Exception:
        return False


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in (ci.FLAG_ENV, ci.KEY_ENV, ci.AGENT_ENV, ci.FORMAT_ENV):
        monkeypatch.delenv(name, raising=False)
    ci.reset_for_tests()
    yield
    ci.reset_for_tests()


# ── bytes against the draft ──────────────────────────────────────────────────────────────────────

def test_keyid_is_the_drafts_thumbprint_and_the_jwk_carries_no_private_member():
    key = ci.load_private_key(RFC_TEST_KEY)
    assert ci.jwk_thumbprint(key) == DRAFT_KEYID
    jwk = ci.public_jwk(key)
    assert jwk == {"kty": "OKP", "crv": "Ed25519", "x": "JrQLj5P_89iXES9-vFgrIy29clF9CC_oPPsw3c5D0bs",
                   "kid": DRAFT_KEYID}
    assert "d" not in jwk


def test_a21_authority_only_signature_is_reproduced_exactly():
    key = ci.load_private_key(RFC_TEST_KEY)
    comps = [ci.Component("@authority")]
    params = ci.serialize_signature_params(comps, [
        ("created", 1735689600), ("keyid", DRAFT_KEYID), ("alg", "ed25519"), ("expires", 4889289600),
        ("nonce", "g0iqFa9e1ffijlyOScDkXpfSmTbYpRNSGPJrQ1It20ahwgzB3jOUcdgLgFxUg7RMtW4V8IILaKKtA+YuSyIgJQ=="),
        ("tag", "web-bot-auth")])
    base = ci.signature_base([(comps[0], "example.com")], params)
    assert base64.b64encode(key.sign(base)).decode() == (
        "FFASViSdcgsyaqqYiCnkHreeZzbNKcTzDvZC5uVlP/dn9IbWj8j0o4wKFTH3rBnUiSUBduwm1Gp5VlIPCp01Ag==")


def test_a23_string_form_headers_and_signature_are_reproduced_exactly():
    signer = ci.Signer(ci.load_private_key(RFC_TEST_KEY), signature_agent=DRAFT_AGENT, agent_format="string")
    nonce = "e8N7S2MFd/qrd6T2R3tdfAuuANngKI7LFtKYI/vowzk4lAZYadIX6wW25MwG7DCT9RUKAJ0qVkU0mEeLElW1qg=="
    headers = signer.request_headers(httpx.URL("https://example.com/"), created=1735689600,
                                     expires=1735693200, nonce=nonce)
    assert headers == {
        "Signature-Agent": '"https://signature-agent.test"',
        "Signature-Input": (
            'sig1=("@authority" "signature-agent");created=1735689600'
            f';keyid="{DRAFT_KEYID}";alg="ed25519";expires=1735693200;nonce="{nonce}";tag="web-bot-auth"'),
        "Signature": "sig1=:jdq0SqOwHdyHr9+r5jw3iYZH6aNGKijYp/EstF4RQTQdi5N5YYKrD+mCT1HA1nZDsi6nJKuHxUi/5Syp3rLWBA==:",
    }


def test_a22_dictionary_form_base_matches_the_draft_and_verifies():
    key = ci.load_private_key(RFC_TEST_KEY)
    signer = ci.Signer(key, signature_agent=DRAFT_AGENT, agent_format="dictionary", agent_label="agent2")
    nonce = "XeP72svPKNiGEg3aDE7WJuTpN69H08oMFqC8NLFy1MptpENAT3WZTYwK+MYdsFMlaqHCJGo9ZAhqer1NWY9Epg=="
    headers = signer.request_headers(httpx.URL("https://example.com/"), created=1735689600,
                                     expires=4889289600, nonce=nonce)
    draft_base = (
        '"@authority": example.com\n'
        '"signature-agent";key="agent2": "https://signature-agent.test"\n'
        '"@signature-params": ("@authority" "signature-agent";key="agent2");created=1735689600'
        f';keyid="{DRAFT_KEYID}";alg="ed25519";expires=4889289600;nonce="{nonce}";tag="web-bot-auth"')
    assert headers["Signature-Agent"] == 'agent2="https://signature-agent.test"'
    assert headers["Signature-Input"] == "sig1=" + draft_base.split('"@signature-params": ', 1)[1]
    assert _verify(key, draft_base, headers["Signature"])


@pytest.mark.parametrize("url,authority", [
    ("https://Example.COM/products.json?limit=1", "example.com"),
    ("https://example.com:443/x", "example.com"),
    ("https://example.com:8443/x", "example.com:8443"),
    ("http://example.com:80/x", "example.com"),
])
def test_authority_is_lowercased_and_drops_only_the_default_port(url, authority):
    assert ci.authority_of(httpx.URL(url)) == authority


@pytest.mark.parametrize("bad", ['https://a.test"', "https://a.test\x00", "http://a.test", "https://ä.test",
                                 "https://a.test/path", "https://u@a.test", "https://A.test", "https://a.test?x=1"])
def test_a_signature_agent_the_header_cannot_carry_is_refused(bad):
    with pytest.raises(ValueError):
        ci.Signer(Ed25519PrivateKey.generate(), signature_agent=bad)


@pytest.mark.parametrize("bad", ["caf\u00e9", "line\nbreak", "nul\x00", "del\x7f"])
def test_sf_string_refuses_what_a_header_cannot_carry(bad):
    with pytest.raises(ValueError):
        ci.sf_string(bad)


def test_sf_string_escapes_quote_and_backslash():
    assert ci.sf_string('a"b\\c') == '"a\\"b\\\\c"'


# ── the transport ───────────────────────────────────────────────────────────────────────────────

async def test_every_request_including_each_redirect_hop_is_signed_for_its_own_host():
    key = Ed25519PrivateKey.generate()
    signer = ci.Signer(key, clock=lambda: 1_800_000_000)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "a.test":
            return httpx.Response(301, headers={"location": "https://b.test/products.json"})
        return httpx.Response(200, json={"products": []})

    transport = ci.SigningTransport(httpx.MockTransport(handler), signer)
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        # A stale signature the caller left on must be replaced, not appended to.
        r = await client.get("https://a.test/products.json", headers={"Signature": "sig1=:stale:"})
    assert r.status_code == 200 and [q.url.host for q in seen] == ["a.test", "b.test"]
    for request in seen:
        sig_input = request.headers["signature-input"]
        assert request.headers["signature-agent"] == '"https://api.pivota.cc"'
        assert ";created=1800000000;" in sig_input and ";expires=1800000060;" in sig_input
        assert sig_input.endswith(';tag="web-bot-auth"') and 'alg="ed25519"' in sig_input
        assert request.headers.get_list("signature") == [request.headers["signature"]]
        base = (f'"@authority": {request.url.host}\n"signature-agent": "https://api.pivota.cc"\n'
                f'"@signature-params": {sig_input[len("sig1="):]}')
        assert _verify(key, base, request.headers["signature"]), request.url.host
    nonces = [q.headers["signature-input"].split(';nonce="', 1)[1].split('"', 1)[0] for q in seen]
    assert len(set(nonces)) == 2 and all(len(base64.b64decode(n)) == 64 for n in nonces)


# ── configuration: dark by default, never fails a crawl ─────────────────────────────────────────

def test_flag_off_changes_nothing(monkeypatch):
    monkeypatch.setenv(ci.KEY_ENV, _pem(Ed25519PrivateKey.generate()))
    inner = httpx.MockTransport(lambda r: httpx.Response(200))
    assert ci.status() == "off"
    assert ci.transport_kwargs() == {}
    assert ci.crawl_transport(inner) is inner and ci.crawl_transport() is None


def test_flag_on_with_a_key_wraps_the_default_transport(monkeypatch):
    monkeypatch.setenv(ci.FLAG_ENV, "1")
    monkeypatch.setenv(ci.KEY_ENV, _pem(Ed25519PrivateKey.generate()))
    assert ci.status() == "signed"
    kwargs = ci.transport_kwargs()
    assert isinstance(kwargs["transport"], ci.SigningTransport)
    first, _ = ci.configured_signer()
    again, _ = ci.configured_signer()
    assert first is again  # parsed once per process, not once per fetch


@pytest.mark.parametrize("pem,why", [
    ("", "no_key"),
    ("not a pem", "bad_key"),
    (None, "bad_key"),  # an RSA key: the wrong type, not the wrong text
])
def test_flag_on_without_a_usable_key_sends_unsigned_and_says_so_once(monkeypatch, caplog, pem, why):
    if pem is None:
        pem = _pem(generate_private_key(public_exponent=65537, key_size=2048))
    monkeypatch.setenv(ci.FLAG_ENV, "true")
    monkeypatch.setenv(ci.KEY_ENV, pem)
    with caplog.at_level(logging.ERROR, logger=ci.__name__):
        assert ci.transport_kwargs() == {}
        assert ci.transport_kwargs() == {}
        assert ci.status() == f"unsigned_{why}"
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert 1 <= len(errors) <= 2  # the key complaint and the "going unsigned" line, each once
    assert all("PRIVATE KEY" not in r.getMessage() and "not a pem" not in r.getMessage() for r in errors)


@pytest.mark.parametrize("fmt,expect", [("dictionary", 'agent1="https://crawl.test"'), ("nonsense", None)])
def test_format_and_agent_come_from_the_environment(monkeypatch, fmt, expect):
    monkeypatch.setenv(ci.FLAG_ENV, "1")
    monkeypatch.setenv(ci.KEY_ENV, _pem(Ed25519PrivateKey.generate()))
    monkeypatch.setenv(ci.AGENT_ENV, "https://crawl.test")
    monkeypatch.setenv(ci.FORMAT_ENV, fmt)
    signer, status = ci.configured_signer()
    if expect is None:
        assert signer is None and status == "bad_config" and ci.transport_kwargs() == {}
    else:
        assert signer.request_headers(httpx.URL("https://x.test/"))["Signature-Agent"] == expect


# ── the directory route ─────────────────────────────────────────────────────────────────────────

def _directory_client() -> TestClient:
    from routes.crawl_identity import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_directory_is_404_until_a_key_is_configured():
    r = _directory_client().get(ci.DIRECTORY_PATH, headers={"host": "api.pivota.cc"})
    assert r.status_code == 404


@pytest.mark.parametrize("host,authority", [("api.pivota.cc", "api.pivota.cc"), ("API.pivota.cc:443", "api.pivota.cc")])
def test_directory_serves_the_public_key_signed_over_the_request_authority(monkeypatch, host, authority):
    key = Ed25519PrivateKey.generate()
    monkeypatch.setenv(ci.KEY_ENV, _pem(key))  # the crawl flag is NOT needed to publish the key
    r = _directory_client().get(ci.DIRECTORY_PATH, headers={"host": host})
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/http-message-signatures-directory+json"
    assert r.headers["cache-control"] == "max-age=3600"
    body = r.json()
    assert body == {"keys": [ci.public_jwk(key)]} and "d" not in body["keys"][0]
    sig_input = r.headers["signature-input"]
    assert sig_input.startswith('sig1=("@authority";req);created=')
    assert f';keyid="{ci.jwk_thumbprint(key)}";' in sig_input and sig_input.endswith(';tag="http-message-signatures-directory"')
    created = int(sig_input.split(";created=", 1)[1].split(";", 1)[0])
    expires = int(sig_input.split(";expires=", 1)[1].split(";", 1)[0])
    assert expires - created == 86400
    base = f'"@authority";req: {authority}\n"@signature-params": {sig_input[len("sig1="):]}'
    assert _verify(key, base, r.headers["signature"])


def test_our_own_directory_check_passes_the_served_directory_and_catches_tampering(monkeypatch):
    key = Ed25519PrivateKey.generate()
    monkeypatch.setenv(ci.KEY_ENV, _pem(key))
    r = _directory_client().get(ci.DIRECTORY_PATH, headers={"host": "api.pivota.cc"})
    headers = dict(r.headers)
    assert ci.verify_directory(r.content, headers, authority="api.pivota.cc") == {
        "ok": True, "problems": [], "keyids": [ci.jwk_thumbprint(key)]}
    assert "signature does not verify" in ci.verify_directory(r.content, headers, authority="evil.test")["problems"]
    other = Ed25519PrivateKey.generate()
    swapped = ('{"keys":[%s]}' % __import__("json").dumps(ci.public_jwk(other))).encode()
    assert not ci.verify_directory(swapped, headers, authority="api.pivota.cc")["ok"]
    wrong_type = {**headers, "content-type": "application/json"}
    assert ci.verify_directory(r.content, wrong_type, authority="api.pivota.cc")["problems"] == [
        "content-type is 'application/json'"]
    assert "signature expired" in ci.verify_directory(r.content, headers, authority="api.pivota.cc",
                                                      now=10**12)["problems"]


# ── the lanes ───────────────────────────────────────────────────────────────────────────────────

async def test_external_offer_fetch_signs_when_on_and_is_unchanged_when_off(monkeypatch):
    from services import external_offers_service as eos

    async def _no_wait(*a, **k):
        return None

    monkeypatch.setattr(eos.crawl_politeness, "before_request", _no_wait)
    monkeypatch.setattr(eos.crawl_politeness, "note_response", lambda *a, **k: None)
    monkeypatch.setattr(eos.shopify_edge_pacer, "learn_from_response", lambda *a, **k: None)
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, html="<html></html>", headers={"content-type": "text/html"})

    real_transport = httpx.AsyncHTTPTransport
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda *a, **k: httpx.MockTransport(handler))
    monkeypatch.setenv(ci.KEY_ENV, _pem(Ed25519PrivateKey.generate()))

    monkeypatch.setenv(ci.FLAG_ENV, "1")
    await eos._fetch_html("https://shop.test/products/x")
    assert seen[-1].headers["signature-agent"] == '"https://api.pivota.cc"'
    assert seen[-1].headers["user-agent"] == eos.DEFAULT_UA

    monkeypatch.setenv(ci.FLAG_ENV, "0")
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", real_transport)
    calls = []

    class _Recorder(httpx.AsyncClient):
        def __init__(self, *a, **k):
            calls.append(k)
            super().__init__(*a, **{**k, "transport": httpx.MockTransport(handler)})

    monkeypatch.setattr(eos.httpx, "AsyncClient", _Recorder)
    await eos._fetch_html("https://shop.test/products/x")
    assert "transport" not in calls[-1]  # flag off: the client is built exactly as before
    assert "signature" not in seen[-1].headers


def test_enrichment_client_signs_when_on_and_keeps_a_callers_transport(monkeypatch):
    from jobs import enrichment_cart_variant_proof as job

    monkeypatch.setenv(ci.FLAG_ENV, "1")
    monkeypatch.setenv(ci.KEY_ENV, _pem(Ed25519PrivateKey.generate()))
    assert isinstance(job.no_cookie_client()._transport, ci.SigningTransport)
    mine = httpx.MockTransport(lambda r: httpx.Response(200))
    assert job.no_cookie_client(transport=mine)._transport is mine
    monkeypatch.setenv(ci.FLAG_ENV, "0")
    assert not isinstance(job.no_cookie_client()._transport, ci.SigningTransport)


@pytest.mark.parametrize("path,needle", [
    ("jobs/reap_cart_proof_refresh.py", "httpx.AsyncClient(**crawl_identity.transport_kwargs()) as raw_client"),
    ("scripts/backfill_shopify_variant_ids.py", "httpx.AsyncClient(**crawl_identity.transport_kwargs()) as client"),
])
def test_the_mirror_lane_clients_take_the_signing_transport(path, needle):
    # The mirror lane's two client constructions live inside a job runner and a CLI main; their
    # behaviour is the transport's (tested above), so this pins only that they ask for it.
    from pathlib import Path

    assert needle in (Path(__file__).resolve().parents[1] / path).read_text()
