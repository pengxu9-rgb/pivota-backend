"""config/sandbox_identity.py: is this database the sandbox's? Answered by asyncpg's own DSN parser."""

from __future__ import annotations

import ipaddress

import pytest

from config import sandbox_identity as I
from tests.sandbox_test_addresses import stand_in_addresses  # noqa: F401 (autouse: hashed addresses -> RFC 5737 stand-ins)

SOCKET = "/cloudsql/pivota-sandbox:us-west1:pivota-pg"


@pytest.fixture(autouse=True)
def _no_libpq_env(monkeypatch):
    for name in ("PGHOST", "PGHOSTADDR", "PGPORT", "PGSERVICE", "PGSERVICEFILE"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("dsn", [
    "postgresql://u:pw@198.51.100.5:5432/pivota",
    "postgresql://u:pw@198.51.100.126/pivota",
    "postgresql+asyncpg://u:pw@198.51.100.5/pivota",
    "postgresql://u:pw@/pivota?host=" + SOCKET,
])
def test_the_sandbox_database_passes(dsn):
    assert I.sandbox_database_refusal({"DATABASE_URL": dsn}) is None


@pytest.mark.parametrize("dsn,match", [
    # The controller's S7 reproduction: the netloc host wins over ?host=, so this is 203.0.113.7.
    ("postgresql://u:pw@203.0.113.7/pivota?host=" + SOCKET, "private range"),
    ("postgresql://u:pw@198.51.100.5,203.0.113.7/pivota", "private range"),
    ("postgresql://u:pw@198.51.100.200/pivota", "private range"),
    ("postgresql://u:pw@192.0.2.25/pivota", "private range"),
    ("postgresql://u:pw@db.example.com/pivota", "private range"),
    ("postgresql://u:pw@[::1]/pivota", "private range"),
    ("postgresql://u:pw@/pivota", "socket"),  # no host: libpq's default sockets / localhost
    ("postgresql://u:pw@/pivota?host=/cloudsql/pivota-prod:us-west1:pivota-pg", "socket"),
    ("postgresql://u:pw@/pivota?host=/cloudsql/pivota-sandbox-evil:x", "socket"),
    ("postgresql://u:pw@/pivota?host=/cloudsql/pivota-sandbox:x/../../../tmp", "socket"),
    ("postgresql://u:pw@/pivota?host=/cloudsql/pivota-sandbox:us-west1:pg/../../pivota-prod:us-west1:pivota-pg", "socket"),
    ("postgresql://u:pw@/pivota?host=/cloudsql/pivota-sandbox:us-west1:pivota-pg/sub", "socket"),
    ("postgresql://u:pw@/pivota?host=/cloudsql/pivota-sandbox:US-WEST1:pg", "socket"),
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
    env = {"DATABASE_URL": "postgresql://u:pw@198.51.100.5/pivota"}
    assert I.sandbox_database_refusal(dict(env, GOOGLE_CLOUD_PROJECT=I.SANDBOX_PROJECT)) is None
    assert "GOOGLE_CLOUD_PROJECT" in I.sandbox_database_refusal(dict(env, GOOGLE_CLOUD_PROJECT="pivota-prod"))


def test_a_changed_asyncpg_parser_is_a_refusal_not_a_pass(monkeypatch):
    from asyncpg import connect_utils

    monkeypatch.setattr(connect_utils, "_parse_connect_dsn_and_args", lambda *, dsn: ([("198.51.100.5", 5432)], None))
    assert "changed shape" in I.sandbox_database_refusal({"DATABASE_URL": "postgresql://u:pw@198.51.100.5/pivota"})


def test_one_source_for_every_sandbox_db_writer():
    """The register script, the schema apply and the catalog import all ask config.sandbox_identity."""
    import inspect

    from jobs import sandbox_catalog_copy, sandbox_schema_apply
    from scripts.sandbox import register_gateway_service_agent

    for fn in (register_gateway_service_agent.check_environment, sandbox_schema_apply.check_environment,
               sandbox_catalog_copy.check_import_environment):
        assert "sandbox_database_refusal(" in inspect.getsource(fn), fn.__qualname__
    assert register_gateway_service_agent.sandbox_database_refusal is I.sandbox_database_refusal


@pytest.mark.parametrize("addr,ok", [
    ("/cloudsql/pivota-sandbox:us-west1:pivota-pg/.s.PGSQL.5432", True),
    ("/cloudsql/pivota-sandbox:us-west1:pivota-pg/.s.PGSQL.5432\n", False),  # `$` would let this pass
    ("/cloudsql/pivota-sandbox:us-west1:pivota-pg/.s.PGSQL.5432/x", False),
    ("/cloudsql/pivota-sandbox:us-west1:pivota-pg/../../tmp/.s.PGSQL.5432", False),
])
def test_the_socket_path_shape_is_exact(addr, ok):
    assert I._address_ok(addr) is ok


# --- the range is held only as a hash (the repo is public) ---------------------------------------
def test_the_real_range_is_one_hashed_slash_20():
    import re

    from tests.sandbox_test_addresses import REAL_SANDBOX_DB_NETWORKS_SHA256

    assert len(REAL_SANDBOX_DB_NETWORKS_SHA256) == 1
    prefix, digest = REAL_SANDBOX_DB_NETWORKS_SHA256[0]
    assert prefix == 20 and re.fullmatch(r"[0-9a-f]{64}", digest)
    assert not hasattr(I, "SANDBOX_DB_NETWORKS")


def test_membership_is_the_hash_of_the_address_s_own_masked_network():
    from tests.sandbox_test_addresses import SANDBOX_NETWORK, network_sha256

    assert I._masked_sha256(ipaddress.ip_address("198.51.100.77"), 25) == network_sha256(SANDBOX_NETWORK)
    assert I._masked_sha256(ipaddress.ip_address("198.51.100.128"), 25) != network_sha256(SANDBOX_NETWORK)
    assert I._address_ok(("198.51.100.0", 5432)) and I._address_ok(("198.51.100.127", 5432))
    assert not I._address_ok(("198.51.100.128", 5432)) and not I._address_ok(("198.51.99.255", 5432))


def test_a_prefix_the_address_family_cannot_take_is_a_refusal_not_a_crash(monkeypatch):
    from tests.sandbox_test_addresses import network_sha256

    monkeypatch.setattr(I, "SANDBOX_DB_NETWORKS_SHA256", ((64, network_sha256("2001:db8::/64")),))
    assert "private range" in I.sandbox_database_refusal({"DATABASE_URL": "postgresql://u:pw@198.51.100.5/pivota"})
    assert I.sandbox_database_refusal({"DATABASE_URL": "postgresql://u:pw@[2001:db8::5]/pivota"}) is None
