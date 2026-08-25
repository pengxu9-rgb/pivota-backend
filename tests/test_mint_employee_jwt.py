from __future__ import annotations

import json
import sys

import jwt
import pytest

import scripts.mint_employee_jwt as module


def _run(monkeypatch, argv: list[str]) -> int:
    """Drive main() through the REAL parser.

    The previous version of these tests hand-built an argparse.Namespace, so
    every new flag had to be mirrored here or main() blew up with AttributeError
    on a field the parser would always have supplied. Parsing the real argv keeps
    the test measuring the script instead of a copy of it.
    """
    monkeypatch.setattr(sys, "argv", ["mint_employee_jwt.py", *argv])
    return module.main()


def test_mint_employee_jwt_uses_direct_secret(monkeypatch, capsys) -> None:
    exit_code = _run(
        monkeypatch,
        [
            "--email", "ops+audit@pivota.invalid",
            "--role", "admin",
            "--user-id", "audit-admin",
            "--employee-id", "emp_audit",
            "--expires-minutes", "30",
            "--jwt-secret", "test-secret",
        ],
    )

    assert exit_code == 0
    token = capsys.readouterr().out.strip()
    payload = jwt.decode(token, "test-secret", algorithms=["HS256"])
    assert payload["email"] == "ops+audit@pivota.invalid"
    assert payload["role"] == "admin"
    assert payload["employee_id"] == "emp_audit"


def test_mint_employee_jwt_can_load_secret_from_gcp(monkeypatch, capsys) -> None:
    """The production path: Secret Manager in pivota-prod."""
    seen: dict[str, str] = {}

    def _fake(secret: str, project: str) -> str:
        seen["secret"] = secret
        seen["project"] = project
        return "gcp-secret"

    monkeypatch.setattr(module, "_load_secret_from_gcp", _fake)

    exit_code = _run(
        monkeypatch,
        ["--email", "ops+audit@pivota.invalid", "--gcp-secret", "--format", "json"],
    )

    assert exit_code == 0
    # Defaults must name the secret the running service actually mounts
    # (web mounts JWT_SECRET_KEY from env-JWT_SECRET_KEY) and the prod project.
    assert seen == {"secret": "env-JWT_SECRET_KEY", "project": "pivota-prod"}
    token = json.loads(capsys.readouterr().out)["token"]
    assert jwt.decode(token, "gcp-secret", algorithms=["HS256"])["role"] == "admin"


def test_mint_employee_jwt_gcp_secret_name_is_overridable(monkeypatch, capsys) -> None:
    monkeypatch.setattr(module, "_load_secret_from_gcp", lambda secret, project: f"{secret}@{project}")

    exit_code = _run(
        monkeypatch,
        [
            "--email", "ops+audit@pivota.invalid",
            "--gcp-secret", "DATABASE_URL",
            "--gcp-project", "pivota-staging",
            "--format", "token",
        ],
    )

    assert exit_code == 0
    token = capsys.readouterr().out.strip()
    assert jwt.decode(token, "DATABASE_URL@pivota-staging", algorithms=["HS256"])


def test_mint_employee_jwt_can_load_secret_from_railway(monkeypatch, capsys) -> None:
    """Railway is the ROLLBACK, but the flag still has to work when aimed there."""
    monkeypatch.setattr(module, "_load_secret_from_railway", lambda service, environment: "rail-secret")

    exit_code = _run(
        monkeypatch,
        [
            "--email", "ops+audit@pivota.invalid",
            "--role", "employee",
            "--sub", "custom-sub",
            "--railway-service", "web",
            "--railway-environment", "production",
            "--format", "json",
        ],
    )

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    claims = output["claims"]
    decoded = jwt.decode(output["token"], "rail-secret", algorithms=["HS256"])
    assert claims["sub"] == "custom-sub"
    assert claims["role"] == "employee"
    assert decoded["sub"] == "custom-sub"
    assert decoded["user_id"] == "ops+audit@pivota.invalid"


def test_mint_employee_jwt_refuses_both_secret_sources(monkeypatch) -> None:
    """Production and the rollback sign with DIFFERENT keys.

    Letting one silently win would mint a token that authenticates against
    exactly one platform, with nothing in the output to say which.
    """
    monkeypatch.setattr(module, "_load_secret_from_gcp", lambda secret, project: "gcp-secret")
    monkeypatch.setattr(module, "_load_secret_from_railway", lambda service, environment: "rail-secret")

    with pytest.raises(SystemExit) as excinfo:
        _run(
            monkeypatch,
            [
                "--email", "ops+audit@pivota.invalid",
                "--gcp-secret",
                "--railway-service", "web",
            ],
        )
    assert "only one" in str(excinfo.value)


def test_mint_employee_jwt_requires_a_secret_source(monkeypatch) -> None:
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    with pytest.raises(SystemExit) as excinfo:
        _run(monkeypatch, ["--email", "ops+audit@pivota.invalid", "--jwt-secret", ""])
    message = str(excinfo.value)
    assert "--gcp-secret" in message and "--railway-service" in message


# ---------------------------------------------------------------------------
# `_load_secret_from_gcp` itself: the three tests above monkeypatch it, so
# nothing covered what it actually SHELLS OUT. Four mutations of its argv
# survived the original suite — swapping --secret/--project, pinning ":1"
# instead of "latest", and dropping the empty-value guard.
# ---------------------------------------------------------------------------


def test_gcp_loader_asks_for_the_right_secret_in_the_right_project(monkeypatch) -> None:
    seen: dict = {}

    def _fake(cmd, text=False):
        seen["cmd"] = cmd
        return "the-key"

    monkeypatch.setattr(module.subprocess, "check_output", _fake)

    assert module._load_secret_from_gcp("env-JWT_SECRET_KEY", "pivota-prod") == "the-key"
    assert seen["cmd"] == [
        "gcloud", "secrets", "versions", "access", "latest",
        "--secret=env-JWT_SECRET_KEY", "--project=pivota-prod",
    ]


def test_gcp_loader_does_not_strip_the_payload(monkeypatch) -> None:
    """Cloud Run mounts the payload verbatim and the app reads a bare os.getenv.

    A secret stored with a trailing newline is what the SERVICE verifies with, so
    stripping it here would sign with a different key and produce a 401 that
    looks like a broken account rather than a mangled key.
    """
    monkeypatch.setattr(module.subprocess, "check_output", lambda cmd, text=False: "the-key\n")

    assert module._load_secret_from_gcp("env-JWT_SECRET_KEY", "pivota-prod") == "the-key\n"


def test_gcp_loader_refuses_an_empty_payload(monkeypatch) -> None:
    monkeypatch.setattr(module.subprocess, "check_output", lambda cmd, text=False: "")

    with pytest.raises(RuntimeError) as excinfo:
        module._load_secret_from_gcp("env-JWT_SECRET_KEY", "pivota-prod")
    assert "empty" in str(excinfo.value)


def test_gcp_secret_does_not_silently_override_an_explicit_jwt_secret(monkeypatch) -> None:
    """B2: the old guard covered gcp-vs-railway only.

    `--jwt-secret abc --gcp-secret` signed with the GCP key and never said that
    `abc` had been discarded — the exact hazard the guard's own comment names.
    """
    monkeypatch.setattr(module, "_load_secret_from_gcp", lambda secret, project: "gcp-secret")

    with pytest.raises(SystemExit) as excinfo:
        _run(monkeypatch, ["--email", "a@b.c", "--jwt-secret", "abc", "--gcp-secret"])
    assert "only one" in str(excinfo.value)


def test_an_empty_gcp_secret_name_does_not_fall_through_to_railway(monkeypatch) -> None:
    """B3: a truthiness guard let `--gcp-secret=` skip both the check and the load."""
    monkeypatch.setattr(module, "_load_secret_from_railway", lambda service, environment: "rail-secret")

    with pytest.raises(SystemExit) as excinfo:
        _run(monkeypatch, ["--email", "a@b.c", "--gcp-secret=", "--railway-service", "web"])
    assert "only one" in str(excinfo.value)


def test_an_empty_gcp_secret_name_alone_is_refused(monkeypatch) -> None:
    with pytest.raises(SystemExit) as excinfo:
        _run(monkeypatch, ["--email", "a@b.c", "--gcp-secret="])
    assert "empty secret name" in str(excinfo.value)
