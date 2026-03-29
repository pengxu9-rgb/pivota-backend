from __future__ import annotations

import json

import jwt

import scripts.mint_employee_jwt as module


def test_mint_employee_jwt_uses_direct_secret(monkeypatch, capsys) -> None:
    args = module.argparse.Namespace(
        email="ops+audit@pivota.invalid",
        role="admin",
        sub=None,
        user_id="audit-admin",
        employee_id="emp_audit",
        merchant_id=None,
        agent_id=None,
        expires_minutes=30,
        jwt_secret="test-secret",
        railway_service=None,
        railway_environment=None,
        format="token",
    )
    monkeypatch.setattr(module, "_parse_args", lambda: args)

    exit_code = module.main()

    assert exit_code == 0
    token = capsys.readouterr().out.strip()
    payload = jwt.decode(token, "test-secret", algorithms=["HS256"])
    assert payload["email"] == "ops+audit@pivota.invalid"
    assert payload["role"] == "admin"
    assert payload["employee_id"] == "emp_audit"


def test_mint_employee_jwt_can_load_secret_from_railway(monkeypatch, capsys) -> None:
    args = module.argparse.Namespace(
        email="ops+audit@pivota.invalid",
        role="employee",
        sub="custom-sub",
        user_id=None,
        employee_id=None,
        merchant_id=None,
        agent_id=None,
        expires_minutes=60,
        jwt_secret="",
        railway_service="web",
        railway_environment="production",
        format="json",
    )
    monkeypatch.setattr(module, "_parse_args", lambda: args)
    monkeypatch.setattr(module, "_load_secret_from_railway", lambda service, environment: "rail-secret")

    exit_code = module.main()

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    token = output["token"]
    claims = output["claims"]
    decoded = jwt.decode(token, "rail-secret", algorithms=["HS256"])
    assert claims["sub"] == "custom-sub"
    assert claims["role"] == "employee"
    assert decoded["sub"] == "custom-sub"
    assert decoded["user_id"] == "ops+audit@pivota.invalid"
