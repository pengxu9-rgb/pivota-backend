"""The reviewed ops jobs hold on any sha256 identity pin mismatch, before a database or HTTP call.

The jobs need no pin variables: each hashes what it already has (its DATABASE_URL, the connected
server, the storefront's shop id, the runtime service account) and compares the digest with a
constant in scripts/ops/reviewed_job_pins.py. A mismatch prints digests only, never the value.
"""

from datetime import datetime, timezone
import importlib
import json
import re
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from scripts.ops import reviewed_job_pins as pins
from services.exact_retailer_cohorts import COHORT_A_CONFIG, COHORT_B_CONFIG, RETAILER_COHORT_B

COHORT_A_JOB = importlib.import_module(COHORT_A_CONFIG["refresh"]["module"])
COHORT_B_JOB = importlib.import_module(COHORT_B_CONFIG["refresh"]["module"])
EXPANSION_JOB = importlib.import_module(COHORT_B_CONFIG["expansion_job"]["module"])

STAMP = "a" * 40
# Placeholder infrastructure: none of these hash to a reviewed pin.
UNPINNED_HOST = "db-host.invalid"
UNPINNED_DB = "unpinned_db"
UNPINNED_DSN = f"postgresql://job@{UNPINNED_HOST}:5432/{UNPINNED_DB}"


def test_pins_are_distinct_sha256_digests():
    values = [pins.PROD_DB_HOST_SHA256, pins.PROD_DB_NAME_SHA256, pins.STAGING_DB_HOST_SHA256,
              pins.STAGING_DB_NAME_SHA256, pins.PROD_SERVICE_ACCOUNT_SHA256, pins.STAGING_SERVICE_ACCOUNT_SHA256,
              pins.COHORT_B_SHOPIFY_SHOP_ID_SHA256]
    assert all(re.fullmatch(r"[0-9a-f]{64}", value) for value in values)
    assert len(set(values)) == len(values)


def test_a_mismatch_names_digests_never_values():
    with pytest.raises(pins.PinMismatch) as raised:
        pins.require_pins(db_host=(UNPINNED_HOST, pins.PROD_DB_HOST_SHA256), db_name=(None, pins.PROD_DB_NAME_SHA256))
    message = str(raised.value)
    assert f"db_host sha256={pins.digest(UNPINNED_HOST)}" in message and "db_name sha256=missing" in message
    assert UNPINNED_HOST not in message
    pins.require_pins(db_host=(UNPINNED_HOST, pins.digest(UNPINNED_HOST)))


@pytest.mark.parametrize(("path", "name"), [("/db", "db"), ("//db", None), ("/", None), ("", None)])
def test_dsn_database_name_takes_exactly_one_leading_slash(path, name):
    assert pins.dsn_database_name(path) == name


def _job_environment(monkeypatch, job_module, prefix, release_env=None, environment="production"):
    monkeypatch.setattr(job_module, "Path", lambda *_args: SimpleNamespace(read_text=lambda: STAMP + "\n"))
    monkeypatch.setenv("PIVOTA_ENV", environment)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "pivota-prod" if environment == "production" else "pivota-staging")
    monkeypatch.setenv("CLOUD_RUN_JOB", prefix + "test")
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", prefix + "test-x1")
    monkeypatch.setenv("DATABASE_URL", UNPINNED_DSN)
    if release_env:
        monkeypatch.setenv(release_env, STAMP)


def _forbid_database_and_http(monkeypatch, job_module):
    from db.database import database

    connect = AsyncMock(side_effect=AssertionError("a pin mismatch must hold before connecting"))
    monkeypatch.setattr(database, "connect", connect)
    storefront = AsyncMock(side_effect=AssertionError("a pin mismatch must hold before the storefront read"))
    monkeypatch.setattr(job_module, "fetch_storefront", storefront)
    return connect, storefront


@pytest.mark.parametrize("job_module,config", [(COHORT_A_JOB, COHORT_A_CONFIG), (COHORT_B_JOB, COHORT_B_CONFIG)],
                         ids=["cohort_a_refresh", "cohort_b_refresh"])
def test_refresh_job_holds_on_an_unpinned_database_with_no_db_or_http_call(monkeypatch, capsys, job_module, config):
    refresh = config["refresh"]
    _job_environment(monkeypatch, job_module, refresh["job_prefix"], refresh["release_commit_env"])
    connect, storefront = _forbid_database_and_http(monkeypatch, job_module)
    monkeypatch.setattr(sys, "argv", ["job", "plan"])
    with pytest.raises(SystemExit) as raised:
        job_module.main()
    assert raised.value.code == 1
    held = json.loads(capsys.readouterr().out)
    assert held["status"] == "held" and held["scope"] == refresh["scope"]
    assert f"db_host sha256={pins.digest(UNPINNED_HOST)}" in held["reason"]
    assert f"db_name sha256={pins.digest(UNPINNED_DB)}" in held["reason"]
    assert UNPINNED_HOST not in held["reason"] and UNPINNED_DB not in held["reason"]
    connect.assert_not_awaited()
    storefront.assert_not_awaited()


@pytest.mark.parametrize("job_module,config", [(COHORT_A_JOB, COHORT_A_CONFIG), (COHORT_B_JOB, COHORT_B_CONFIG)],
                         ids=["cohort_a_refresh", "cohort_b_refresh"])
def test_refresh_job_passes_a_database_that_hashes_to_its_pins(monkeypatch, job_module, config):
    refresh = config["refresh"]
    _job_environment(monkeypatch, job_module, refresh["job_prefix"], refresh["release_commit_env"])
    monkeypatch.setattr(job_module, "PROD_DB_HOST_SHA256", pins.digest(UNPINNED_HOST))
    monkeypatch.setattr(job_module, "PROD_DB_NAME_SHA256", pins.digest(UNPINNED_DB))
    assert job_module.check_environment(**({"write": False} if job_module is COHORT_B_JOB else {"mode": "plan"})) == STAMP


@pytest.mark.asyncio
@pytest.mark.parametrize("server", [
    {"host": UNPINNED_HOST, "name": UNPINNED_DB, "replica": False},
    {"host": None, "name": UNPINNED_DB, "replica": False},
    {"host": UNPINNED_HOST, "name": UNPINNED_DB, "replica": True},
], ids=["unpinned_primary", "socket_connection", "replica"])
async def test_refresh_job_holds_when_the_connected_server_is_not_the_pinned_primary(server):
    class DB:
        def __init__(self):
            self.statements = []

        def transaction(self):
            db = self

            class Transaction:
                async def __aenter__(self):
                    return db

                async def __aexit__(self, *_exc):
                    return False

            return Transaction()

        async def execute(self, sql, *_args):
            self.statements.append(sql)

        async def fetch_one(self, _sql, *_args):
            return server

        fetch_val = AsyncMock(side_effect=AssertionError("no query after a server pin mismatch"))
        fetch_all = AsyncMock(side_effect=AssertionError("no query after a server pin mismatch"))

    for run in (lambda db: COHORT_A_JOB.apply_cohort(db, [], mode="refresh"),
                lambda db: COHORT_B_JOB.refresh_existing(db, {}, write=True)):
        db = DB()
        with pytest.raises(ValueError, match="pin mismatch|did not reach the reviewed production primary"):
            await run(db)
        assert len(db.statements) == 1 and db.statements[0].startswith("SET TRANSACTION ISOLATION LEVEL")


def test_cohort_b_storefront_holds_on_an_unpinned_shop_id():
    (listing,) = RETAILER_COHORT_B.listings.values()
    shop = {"id": 1, "domain": listing.host, "myshopify_domain": COHORT_B_CONFIG["refresh"]["myshopify_domain"],
            "ships_to_countries": ["US"]}
    with pytest.raises(pins.PinMismatch, match=f"shopify_shop_id sha256={pins.digest('1')}"):
        COHORT_B_JOB.storefront_record(shop, {}, datetime.now(timezone.utc))


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_expansion_job_holds_on_an_unpinned_database_before_metadata_db_or_http(monkeypatch, environment):
    prefix = COHORT_B_CONFIG["expansion_job"]["job_prefixes"][environment]
    _job_environment(monkeypatch, EXPANSION_JOB, prefix, environment=environment)
    metadata = Mock(side_effect=AssertionError("a database pin mismatch must hold before the metadata server"))
    monkeypatch.setattr(EXPANSION_JOB, "metadata", metadata)
    connect = AsyncMock(side_effect=AssertionError("a pin mismatch must hold before connecting"))
    monkeypatch.setattr(EXPANSION_JOB.database, "connect", connect)
    storefront = AsyncMock(side_effect=AssertionError("a pin mismatch must hold before the storefront read"))
    monkeypatch.setattr(EXPANSION_JOB, "fetch_storefront", storefront)
    import asyncio

    with pytest.raises(pins.PinMismatch) as raised:
        asyncio.run(EXPANSION_JOB.run("plan", STAMP))
    assert UNPINNED_HOST not in str(raised.value) and UNPINNED_DB not in str(raised.value)
    metadata.assert_not_called()
    connect.assert_not_awaited()
    storefront.assert_not_awaited()


def test_expansion_job_holds_on_an_unpinned_service_account(monkeypatch):
    prefix = COHORT_B_CONFIG["expansion_job"]["job_prefixes"]["staging"]
    _job_environment(monkeypatch, EXPANSION_JOB, prefix, environment="staging")
    monkeypatch.setitem(EXPANSION_JOB.PROFILES, "staging", (
        "pivota-staging", pins.digest(UNPINNED_HOST), pins.digest(UNPINNED_DB), pins.STAGING_SERVICE_ACCOUNT_SHA256,
    ))
    answers = {"project/project-id": "pivota-staging",
               "instance/service-accounts/default/email": "someone@example.invalid"}
    monkeypatch.setattr(EXPANSION_JOB, "metadata", lambda key: answers[key])
    with pytest.raises(pins.PinMismatch, match="service_account sha256=") as raised:
        EXPANSION_JOB.check_environment(STAMP)
    assert "someone@example.invalid" not in str(raised.value)
    answers["instance/service-accounts/default/email"] = "reviewed@example.invalid"
    monkeypatch.setitem(EXPANSION_JOB.PROFILES, "staging", (
        "pivota-staging", pins.digest(UNPINNED_HOST), pins.digest(UNPINNED_DB), pins.digest("reviewed@example.invalid"),
    ))
    monkeypatch.setenv(COHORT_B_CONFIG["expansion_job"]["enable_flag"], "1")
    assert EXPANSION_JOB.check_environment(STAMP)[0] == "staging"
