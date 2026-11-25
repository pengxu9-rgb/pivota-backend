"""scripts/sandbox/register_gateway_service_agent.py: where it may run (no database here)."""

from __future__ import annotations

import pytest

from scripts.sandbox import register_gateway_service_agent as R
from tests.test_sandbox_guard import GOOD, _clean_platform_env  # noqa: F401 (autouse fixture)

SANDBOX_DB = "postgresql://pivota:pw@10.61.0.5:5432/pivota"


AGENT = "agent_0123456789abcdef"


def _env(**changes):
    env = dict(GOOD, DATABASE_URL=SANDBOX_DB, CLOUD_RUN_JOB="register-gateway-service-agent",
               ISSUING_AGENT_EXCLUDED_AGENT_IDS="agent_other," + AGENT,
               ISSUING_ASSERTION_VOUCHER_AGENT_IDS=AGENT)
    env.pop("K_SERVICE", None)
    env.pop("K_REVISION", None)
    for key, value in changes.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def test_the_sandbox_with_its_own_db_passes():
    R.check_environment(_env())
    R.check_environment(_env(DATABASE_URL="postgresql://u:p@/pivota?host=/cloudsql/pivota-sandbox:us-west1:pivota-pg"))


@pytest.mark.parametrize("changes,match", [
    ({"PIVOTA_ENV": "staging"}, "only in the sandbox"),
    ({"PIVOTA_ENV": "production"}, "only in the sandbox"),
    ({"PIVOTA_ENV": None}, "only in the sandbox"),
    ({"DATABASE_URL": "postgresql://u:p@10.25.0.2:5432/pivota"}, "sandbox guard"),
    ({"DATABASE_URL": "postgresql://u:p@10.70.0.5:5432/pivota"}, "private range"),
    ({"DATABASE_URL": "postgresql://u:p@db.example.com:5432/pivota"}, "private range"),
    ({"DATABASE_URL": "postgresql://u:p@/pivota?host=/cloudsql/other-project:us-west1:pg"}, "socket"),
    ({"DATABASE_URL": None}, "no host"),
    ({"GOOGLE_CLOUD_PROJECT": "pivota-sandbox-2"}, "GOOGLE_CLOUD_PROJECT"),
    ({"REAP_API_BASE_URL": "https://prod.api.reap.global"}, "sandbox guard"),
])
def test_it_refuses_anywhere_but_the_sandbox_db(changes, match):
    with pytest.raises(R.Refused, match=match) as excinfo:
        R.check_environment(_env(**changes))
    assert "pw" not in str(excinfo.value) and ":p@" not in str(excinfo.value)


def test_main_refuses_with_exit_2_before_reading_any_secret(monkeypatch, capsys):
    monkeypatch.setenv("PIVOTA_ENV", "staging")

    def boom():
        raise AssertionError("read the secret outside the sandbox")

    monkeypatch.setattr(R, "fetch_key_from_secret_manager", boom)
    assert R.main(["--agent-id", "agent_982b1ea2df866206"]) == 2
    assert "REFUSED" in capsys.readouterr().err


def test_the_agent_id_must_be_in_both_service_agent_lists():
    R.check_agent_id(AGENT, _env())
    with pytest.raises(R.Refused, match="ISSUING_ASSERTION_VOUCHER_AGENT_IDS"):
        R.check_agent_id(AGENT, _env(ISSUING_ASSERTION_VOUCHER_AGENT_IDS="agent_other"))
    with pytest.raises(R.Refused, match="ISSUING_AGENT_EXCLUDED_AGENT_IDS"):
        R.check_agent_id(AGENT, _env(ISSUING_AGENT_EXCLUDED_AGENT_IDS=None))
    with pytest.raises(R.Refused, match="ISSUING_AGENT_EXCLUDED_AGENT_IDS"):
        R.check_agent_id("agent_982b1ea2df866206", _env())  # prod's default id is not in the sandbox lists
    with pytest.raises(R.Refused, match="agent-id"):
        R.check_agent_id("robot", _env())


def test_main_checks_the_id_before_reading_the_secret(monkeypatch, capsys):
    for key, value in _env().items():
        monkeypatch.setenv(key, value)

    def boom():
        raise AssertionError("read the secret for an unlisted id")

    monkeypatch.setattr(R, "fetch_key_from_secret_manager", boom)
    assert R.main(["--agent-id", "agent_not_in_lists"]) == 2
    assert "not listed" in capsys.readouterr().err


def test_main_checks_the_environment_before_reading_the_secret_even_with_a_listed_id(monkeypatch, capsys):
    for key, value in _env(PIVOTA_ENV="staging").items():
        monkeypatch.setenv(key, value)

    def boom():
        raise AssertionError("read the secret outside the sandbox")

    monkeypatch.setattr(R, "fetch_key_from_secret_manager", boom)
    assert R.main(["--agent-id", AGENT]) == 2
    assert "only in the sandbox" in capsys.readouterr().err
