"""config/sandbox_identity.py: is this database the sandbox's? Answered by asyncpg's own DSN parser."""

from __future__ import annotations

import pytest

from config import sandbox_identity as I

SOCKET = "/cloudsql/pivota-sandbox:us-west1:pivota-pg"


@pytest.fixture(autouse=True)
def _no_libpq_env(monkeypatch):
    for name in ("PGHOST", "PGHOSTADDR", "PGPORT", "PGSERVICE", "PGSERVICEFILE"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("dsn", [
    "postgresql://u:pw@10.61.0.5:5432/pivota",
    "postgresql://u:pw@10.61.15.254/pivota",
    "postgresql+asyncpg://u:pw@10.61.0.5/pivota",
    "postgresql://u:pw@/pivota?host=" + SOCKET,
])
def test_the_sandbox_database_passes(dsn):
    assert I.sandbox_database_refusal({"DATABASE_URL": dsn}) is None


@pytest.mark.parametrize("dsn,match", [
    # The controller's S7 reproduction: the netloc host wins over ?host=, so this is 203.0.113.7.
    ("postgresql://u:pw@203.0.113.7/pivota?host=" + SOCKET, "private range"),
    ("postgresql://u:pw@10.61.0.5,203.0.113.7/pivota", "private range"),
    ("postgresql://u:pw@10.62.0.1/pivota", "private range"),
    ("postgresql://u:pw@10.25.0.2/pivota", "private range"),
    ("postgresql://u:pw@db.example.com/pivota", "private range"),
    ("postgresql://u:pw@[::1]/pivota", "private range"),
    ("postgresql://u:pw@/pivota", "socket"),  # no host: libpq's default sockets / localhost
    ("postgresql://u:pw@/pivota?host=/cloudsql/pivota-prod:us-west1:pivota-pg", "socket"),
    ("postgresql://u:pw@/pivota?host=/cloudsql/pivota-sandbox-evil:x", "socket"),
    ("", "no host"),
    ("not a dsn", "could not be parsed"),
])
def test_anything_else_is_refused_without_quoting_the_dsn(dsn, match):
    refusal = I.sandbox_database_refusal({"DATABASE_URL": dsn})
    assert refusal and match in refusal
    assert "pw" not in refusal


def test_pghost_cannot_smuggle_a_host_in(monkeypatch):
    monkeypatch.setenv("PGHOST", "203.0.113.7")
    assert "private range" in I.sandbox_database_refusal({"DATABASE_URL": "postgresql://u:pw@/pivota"})


def test_the_project_must_be_the_sandbox_s():
    env = {"DATABASE_URL": "postgresql://u:pw@10.61.0.5/pivota"}
    assert I.sandbox_database_refusal(dict(env, GOOGLE_CLOUD_PROJECT=I.SANDBOX_PROJECT)) is None
    assert "GOOGLE_CLOUD_PROJECT" in I.sandbox_database_refusal(dict(env, GOOGLE_CLOUD_PROJECT="pivota-prod"))


def test_a_changed_asyncpg_parser_is_a_refusal_not_a_pass(monkeypatch):
    from asyncpg import connect_utils

    monkeypatch.setattr(connect_utils, "_parse_connect_dsn_and_args", lambda *, dsn: ([("10.61.0.5", 5432)], None))
    assert "changed shape" in I.sandbox_database_refusal({"DATABASE_URL": "postgresql://u:pw@10.61.0.5/pivota"})


def test_one_source_for_every_sandbox_db_writer():
    """The register script, the schema apply and the catalog import all ask config.sandbox_identity."""
    import inspect

    from jobs import sandbox_catalog_copy, sandbox_schema_apply
    from scripts.sandbox import register_gateway_service_agent

    for fn in (register_gateway_service_agent.check_environment, sandbox_schema_apply.check_environment,
               sandbox_catalog_copy.check_import_environment):
        assert "sandbox_database_refusal(" in inspect.getsource(fn), fn.__qualname__
    assert register_gateway_service_agent.sandbox_database_refusal is I.sandbox_database_refusal
