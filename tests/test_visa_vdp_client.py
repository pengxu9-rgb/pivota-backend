"""services/payment_orchestration/visa_vdp_client.py and jobs/visa_vdp_hello_world.py, offline.

An httpx MockTransport stands in for Visa and records every request, so the tests assert both what
comes back and what was sent. Keys and certificates are generated per run; nothing here is a real
Visa credential. Card values are fake.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import hmac
import json
import logging
import os
import sys

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import services.payment_orchestration.visa_vdp_client as vdp  # noqa: E402
from jobs import visa_vdp_hello_world as job  # noqa: E402

DPAN = "4895370012003478"
CRYPTOGRAM = "AOHHzRvcAAAAAAAAAAAAAAAAAAAAAA=="
SIGNED_JWT = "eyJhbGciOiJSUzI1NiJ9.eyJ0b2tlbiI6IjQ4OTUzNzAwMTIwMDM0NzgifQ.c2ln"


def _keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "vdp-test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode(), key_pem


@pytest.fixture(scope="module")
def visa_mle():
    """Visa's MLE server cert + key (we encrypt to it) and our MLE client cert + key (Visa encrypts to it)."""
    server_cert, server_key = _keypair()
    client_cert, client_key = _keypair()
    return {"server_cert": server_cert, "server_key": server_key, "client_cert": client_cert, "client_key": client_key}


class Recorder:
    def __init__(self, responder):
        self.requests = []
        self._responder = responder

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responder(request)


def _client(responder, **kwargs):
    rec = Recorder(responder)
    kwargs.setdefault("mtls", vdp.MtlsAuth("CERT", "KEY", "user-1", "pw-1"))
    client = vdp.VisaVdpClient("sandbox", transport=httpx.MockTransport(rec), **kwargs)
    return client, rec


def _json(status, body, headers=None):
    return lambda request: httpx.Response(status, json=body, headers=headers or {})


# ── X-Pay ────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path,expected", [
    ("/vdp/helloworld", "helloworld"),
    ("/vacp/v2/credentials-for-instruction", "v2/credentials-for-instruction"),
    ("/vts/panEnrollments", "vts/panEnrollments"),
])
def test_xpay_resource_path_drops_the_vdp_or_vacp_prefix(path, expected):
    assert vdp.xpay_resource_path(path) == expected


def test_xpay_token_is_the_reference_hmac():
    token = vdp.xpay_token("s3cret", "/vdp/helloworld", "apikey=K", '{"a":1}', timestamp=1700000000)
    expected = hmac.new(b"s3cret", b'1700000000helloworldapikey=K{"a":1}', hashlib.sha256).hexdigest()
    assert token == f"xv2:1700000000:{expected}"


def test_xpay_request_carries_token_and_apikey_and_no_basic_auth():
    client, rec = _client(_json(200, {"message": "helloworld"}), mtls=None, xpay=vdp.XPayAuth("K1", "S1"))
    result = asyncio.run(client.hello_world())
    assert result.ok
    req = rec.requests[0]
    assert req.url.host == "sandbox.api.visa.com"
    assert req.url.path == "/vdp/helloworld"
    assert req.url.params["apikey"] == "K1"
    assert req.headers["x-pay-token"].startswith("xv2:")
    assert "authorization" not in req.headers


def test_mtls_request_sends_basic_auth_and_no_apikey():
    client, rec = _client(_json(200, {"message": "helloworld"}))
    assert asyncio.run(client.hello_world(auth="mtls")).ok
    req = rec.requests[0]
    assert req.headers["authorization"] == "Basic " + base64.b64encode(b"user-1:pw-1").decode()
    assert "apikey" not in req.url.params
    assert "x-pay-token" not in req.headers


# ── MLE ──────────────────────────────────────────────────────────────────────────────────────────


def test_mle_round_trip_and_header(visa_mle):
    jwe = vdp.mle_encrypt('{"x":1}', visa_mle["server_cert"], "kid-1", iat_ms=1700000000000)
    header = json.loads(vdp._b64u_decode(jwe.split(".")[0]))
    assert header == {"alg": "RSA-OAEP-256", "enc": "A128GCM", "kid": "kid-1", "iat": 1700000000000}
    assert vdp.mle_decrypt(jwe, visa_mle["server_key"]) == '{"x":1}'


def test_mle_rejects_wrong_key_and_tampering(visa_mle):
    jwe = vdp.mle_encrypt("hello", visa_mle["server_cert"], "kid-1")
    with pytest.raises(ValueError):
        vdp.mle_decrypt(jwe, visa_mle["client_key"])
    parts = jwe.split(".")
    parts[3] = vdp._b64u(b"\x00" + vdp._b64u_decode(parts[3])[1:])
    with pytest.raises(ValueError):
        vdp.mle_decrypt(".".join(parts), visa_mle["server_key"])
    with pytest.raises(ValueError):
        vdp.mle_decrypt("not.a.jwe", visa_mle["server_key"])


def test_encrypted_request_and_answer(visa_mle):
    keys = vdp.MleKeys("kid-1", visa_mle["server_cert"], visa_mle["client_key"])

    def visa(request):
        sent = json.loads(request.content)
        assert set(sent) == {"encData"}
        assert json.loads(vdp.mle_decrypt(sent["encData"], visa_mle["server_key"])) == {"q": "v"}
        answer = vdp.mle_encrypt(json.dumps({"status": "ACTIVE"}), visa_mle["client_cert"], "kid-1")
        return httpx.Response(200, json={"encData": answer})

    client, rec = _client(visa, mle=keys)
    result = asyncio.run(client.request("POST", "/vacp/v1/cards", {"q": "v"}, encrypt=True))
    assert result.ok and result.body == {"status": "ACTIVE"}
    assert rec.requests[0].headers["keyid"] == "kid-1"


def test_encrypted_answer_without_mle_key_is_an_error_not_a_body(visa_mle):
    answer = vdp.mle_encrypt("{}", visa_mle["client_cert"], "kid-1")
    client, _ = _client(_json(200, {"encData": answer}))
    result = asyncio.run(client.hello_world())
    assert result.outcome == "mle_error" and result.body is None and not result.ok


# ── credentials never leave the module ──────────────────────────────────────────────────────────


def test_vic_credential_payload_is_stripped_and_counted(caplog):
    body = {
        "instructionId": "ins_1",
        "clientReferenceId": "ref_1",
        "payload": {
            "network": "visa",
            "panLastFour": "3478",
            "signedPayload": SIGNED_JWT,
            "encryptedSignedPayload": "eyJhbGciOiJSU0EtT0FFUC0yNTYifQ.a.b.c.d",
            "paymentAccountReference": "V0010013018275372891234567890",
        },
        "tokenDetails": {"dpan": DPAN, "cryptogram": CRYPTOGRAM},
    }
    client, _ = _client(_json(200, body))
    with caplog.at_level(logging.INFO, logger=vdp.logger.name):
        result = asyncio.run(client.request("POST", "/vacp/v1/instructions/ins_1/credentials", {"a": 1}))
    flat = json.dumps(result.body)
    for secret in (SIGNED_JWT, DPAN, CRYPTOGRAM, "eyJhbGciOiJSU0EtT0FFUC0yNTYifQ"):
        assert secret not in flat
        assert secret not in caplog.text
    assert result.body["instructionId"] == "ins_1"
    assert result.body["payload"]["panLastFour"] == "3478"
    assert result.credential_fields_stripped >= 4
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_logs_carry_shape_only(caplog):
    client, _ = _client(_json(200, {"message": "helloworld", "timestamp": "2026-11-07T04:00:00"}))
    with caplog.at_level(logging.INFO, logger=vdp.logger.name):
        asyncio.run(client.hello_world())
    assert "helloworld" in caplog.text  # the path
    assert "pw-1" not in caplog.text and "user-1" not in caplog.text
    assert "2026-11-07T04:00:00" not in caplog.text


# ── transport hygiene ────────────────────────────────────────────────────────────────────────────


def test_unknown_environment_and_missing_auth_are_config_errors():
    with pytest.raises(vdp.VisaVdpConfigError):
        vdp.VisaVdpClient("staging", xpay=vdp.XPayAuth("K", "S"), transport=httpx.MockTransport(lambda r: None))
    with pytest.raises(vdp.VisaVdpConfigError):
        vdp.VisaVdpClient("sandbox", transport=httpx.MockTransport(lambda r: None))


@pytest.mark.parametrize("env,host", [("sandbox", "sandbox.api.visa.com"), ("cert", "cert.api.visa.com"),
                                      ("production", "api.visa.com")])
def test_each_environment_has_one_fixed_host(env, host):
    rec = Recorder(_json(200, {}))
    client = vdp.VisaVdpClient(env, xpay=vdp.XPayAuth("K", "S"), transport=httpx.MockTransport(rec))
    asyncio.run(client.hello_world())
    assert rec.requests[0].url.host == host and rec.requests[0].url.scheme == "https"


@pytest.mark.parametrize("path", ["vdp/helloworld", "//evil.test/x", "/vdp/helloworld?apikey=x", "/a#b"])
def test_paths_cannot_escape_the_host(path):
    client, _ = _client(_json(200, {}))
    with pytest.raises(ValueError):
        asyncio.run(client.request("GET", path))


def test_redirect_is_not_followed():
    client, rec = _client(lambda r: httpx.Response(302, headers={"Location": "https://evil.test/"}))
    result = asyncio.run(client.hello_world())
    assert len(rec.requests) == 1 and result.status == 302 and result.outcome == "http_error"


def test_oversized_and_unparseable_bodies_are_not_returned():
    big, _ = _client(lambda r: httpx.Response(200, content=b"x" * (vdp.MAX_BODY_BYTES + 1)))
    assert asyncio.run(big.hello_world()).outcome == "too_large"
    html, _ = _client(lambda r: httpx.Response(200, content=b"<html>" + DPAN.encode() + b"</html>"))
    result = asyncio.run(html.hello_world())
    assert result.outcome == "unparseable" and result.body is None


def test_timeout_is_outcome_unknown():
    def boom(request):
        raise httpx.ReadTimeout("slow", request=request)

    client, _ = _client(boom)
    result = asyncio.run(client.hello_world())
    assert result.outcome == "outcome_unknown" and result.status is None and not result.ok


def test_real_mtls_context_rejects_a_mismatched_pair(visa_mle):
    with pytest.raises(vdp.VisaVdpConfigError):
        vdp.VisaVdpClient("sandbox", mtls=vdp.MtlsAuth(visa_mle["server_cert"], visa_mle["client_key"], "u", "p"))


def test_real_mtls_context_loads_a_matching_pair(visa_mle):
    client = vdp.VisaVdpClient("sandbox", mtls=vdp.MtlsAuth(visa_mle["server_cert"], visa_mle["server_key"], "u", "p"))
    assert client._ssl is not None and client.auth_modes == ("mtls",)


# ── from_env and the one-off job ─────────────────────────────────────────────────────────────────

_ALL_ENV = [getattr(vdp, n) for n in dir(vdp) if n.startswith("VISA_VDP_") and n.endswith("_ENV")]


@pytest.fixture
def clean_env(monkeypatch):
    for name in _ALL_ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_from_env_is_dark_until_enabled(clean_env):
    clean_env.setenv(vdp.VISA_VDP_API_KEY_ENV, "K")
    clean_env.setenv(vdp.VISA_VDP_SHARED_SECRET_ENV, "S")
    assert vdp.from_env() is None
    clean_env.setenv(vdp.VISA_VDP_ENABLED_ENV, "on")
    client = vdp.from_env(transport=httpx.MockTransport(lambda r: None))
    assert client.environment == "sandbox" and client.auth_modes == ("xpay",)


def test_from_env_refuses_a_half_configured_mode(clean_env):
    clean_env.setenv(vdp.VISA_VDP_ENABLED_ENV, "on")
    clean_env.setenv(vdp.VISA_VDP_USER_ID_ENV, "u")
    with pytest.raises(vdp.VisaVdpConfigError) as err:
        vdp.from_env()
    assert "VISA_VDP_PASSWORD" in str(err.value)


def test_job_gate_open_only_when_every_mode_answers(clean_env, monkeypatch, capsys):
    clean_env.setenv(vdp.VISA_VDP_ENABLED_ENV, "on")
    clean_env.setenv(vdp.VISA_VDP_API_KEY_ENV, "K")
    clean_env.setenv(vdp.VISA_VDP_SHARED_SECRET_ENV, "S")
    real = vdp.from_env
    monkeypatch.setattr(vdp, "from_env", lambda: real(transport=httpx.MockTransport(_json(200, {"message": "ok"}))))
    assert job.main() == 0
    line = json.loads(capsys.readouterr().out)
    assert line["gate"] == "OPEN" and line["checks"][0]["mode"] == "xpay"
    monkeypatch.setattr(vdp, "from_env", lambda: real(transport=httpx.MockTransport(_json(401, {"x": 1}))))
    assert job.main() == 1
    assert json.loads(capsys.readouterr().out)["gate"] == "CLOSED"


def test_job_not_configured_exits_2(clean_env, capsys):
    assert job.main() == 2
    assert json.loads(capsys.readouterr().out) == {
        "job": "visa_vdp_hello_world", "gate": "CLOSED", "error": "not_configured"}
