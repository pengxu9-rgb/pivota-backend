import json
import time

import pytest
import jwt
from jwt import algorithms
from cryptography.hazmat.primitives.asymmetric import rsa

from services.agent_user_jwt import AgentUserJwtError, verify_agent_user_jwt


def _mk_rsa_jwks(kid: str = "k1"):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return key, {"keys": [jwk]}


def test_verify_agent_user_jwt_accepts_rs256_and_prefixes_issuer(monkeypatch):
    private_key, jwks = _mk_rsa_jwks(kid="kid_1")
    monkeypatch.setenv("AGENT_USER_JWKS_JSON", json.dumps(jwks))
    monkeypatch.setenv("AGENT_USER_JWT_ISSUER", "https://agent.tools.example")
    monkeypatch.setenv("AGENT_USER_JWT_AUDIENCE", "pivota")

    now = int(time.time())
    token = jwt.encode(
        {"iss": "https://agent.tools.example", "sub": "user_123", "aud": "pivota", "iat": now, "exp": now + 60},
        key=private_key,
        algorithm="RS256",
        headers={"kid": "kid_1"},
    )

    ident = verify_agent_user_jwt(token)
    assert ident.agent_user_ref == "https://agent.tools.example:user_123"
    assert ident.issuer == "https://agent.tools.example"
    assert ident.subject == "user_123"


def test_verify_agent_user_jwt_rejects_wrong_audience(monkeypatch):
    private_key, jwks = _mk_rsa_jwks(kid="kid_1")
    monkeypatch.setenv("AGENT_USER_JWKS_JSON", json.dumps(jwks))
    monkeypatch.setenv("AGENT_USER_JWT_ISSUER", "https://agent.tools.example")
    monkeypatch.setenv("AGENT_USER_JWT_AUDIENCE", "pivota")

    now = int(time.time())
    token = jwt.encode(
        {"iss": "https://agent.tools.example", "sub": "user_123", "aud": "other", "iat": now, "exp": now + 60},
        key=private_key,
        algorithm="RS256",
        headers={"kid": "kid_1"},
    )

    with pytest.raises(AgentUserJwtError):
        verify_agent_user_jwt(token)

