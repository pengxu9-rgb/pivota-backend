"""PIVOTA_ENV=sandbox: the platform label and the startup guard (config/sandbox_guard.py).

Three things are pinned here:

1. ``sandbox`` is its own label: not production, not staging, deployed. Every other label answers
   exactly as before (a truth table, not "whatever the code says").
2. A sandbox process refuses to start on each bad setting, one test per refusal, and the refusal
   names the setting and never its value.
3. Production and staging are untouched: the SAME hostile settings that make a sandbox refuse make
   ``enforce_sandbox_guard`` a no-op there, and the lifespan boots past it.
"""

from __future__ import annotations

import os
import re

import pytest

from config import platform as P
from config import sandbox_guard as G
from tests.sandbox_test_addresses import stand_in_addresses  # noqa: F401 (autouse: hashed addresses -> RFC 5737 stand-ins)

# A value that must never appear in a refusal message. Used as a password / key in the cases below.
SECRET = "s3cr3t-value-never-printed"

#: Settings a real sandbox revision would carry; the guard passes on exactly these.
GOOD = {
    "PIVOTA_ENV": "sandbox",
    "K_SERVICE": "web",
    "K_REVISION": "web-00001-abc",
    "GOOGLE_CLOUD_PROJECT": "pivota-sandbox",
    "DATABASE_URL": "postgresql://pivota:%s@198.51.100.5:5432/pivota" % SECRET,
    "REAP_API_BASE_URL": "https://sandbox.api.reap.global",
    "REAP_API_KEY": SECRET,
    "REAP_AGENTIC_SIMULATE_CHECKOUT": "COMPLETED",
    "ORDER_CREATE_LIVE_QUOTE_REVALIDATION_ENABLED": "0",
    "PUBLIC_MCP_URL": "https://sandbox.mcp.pivota.cc/mcp",
    "AGENPAS_PUBLIC_BASE_URL": "https://sandbox.agenpaz.com",
    "CATALOG_COPY_GCS_URI": "gs://pivota-sandbox-catalog-drop/catalog/",
    "A2A_RAIL_ENABLED": "false",
    "AUDIT_WORKER_ENABLED": "true",
    "SCHEDULER_JOB_ALLOWLIST": "reap_agentic_purchase_poll,agent_purchase_events",
    "CATALOG_TRUST_CRAWL_FRESH_ENABLED": "false",
    # S1: the refund flags default ON, so they are set off; the two settings whose code default is a
    # prod gateway URL are set (empty = the feature has nowhere to go).
    "FF_ENABLE_INTERNAL_REFUND": "false",
    "FF_ENABLE_MERCHANT_ORDER_FAILURE_REFUND": "false",
    "PIVOTA_AGENT_INTERNAL_URL": "",
    "OUTBOUND_WARM_HANDOFF_RESOLVE_URL": "",
}

_PLATFORM_KEYS = (
    "PIVOTA_PLATFORM", "PIVOTA_ENV", "RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME",
    "RAILWAY_SERVICE_ID", "RAILWAY_SERVICE_NAME", "RAILWAY_PROJECT_ID", "RAILWAY_DEPLOYMENT_ID",
    "K_SERVICE", "K_REVISION", "K_CONFIGURATION", "CLOUD_RUN_JOB", "CLOUD_RUN_EXECUTION",
)


@pytest.fixture(autouse=True)
def _clean_platform_env(monkeypatch):
    for key in _PLATFORM_KEYS:
        monkeypatch.delenv(key, raising=False)
    P.reset_platform_state()
    yield
    P.reset_platform_state()


def _with(**changes):
    env = dict(GOOD)
    for key, value in changes.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


# ---------------------------------------------------------------------------------------------
# 1. the label
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["sandbox", "SANDBOX", "  Sandbox  "])
def test_sandbox_is_its_own_label(raw):
    for markers in ({}, {"K_SERVICE": "web"}, {"CLOUD_RUN_JOB": "catalog-import"}):
        env = {"PIVOTA_ENV": raw, **markers}
        assert P.platform_env(env) == "sandbox"
        assert P.is_sandbox(env) is True
        assert P.is_production(env) is False
        assert P.is_staging(env) is False
        assert P.is_development(env) is False
        # Deployed by definition, even with no platform marker: every is_deployed() gate is strict.
        assert P.is_deployed(env) is True
        assert P.pytest_bypass_allowed({**env, "PYTEST_CURRENT_TEST": "x"}) is False
        assert P.require_platform_env(env) == "sandbox"


@pytest.mark.parametrize("raw", ["sandbx", "sand box", "sandbox-1", "partner-sandbox"])
def test_a_near_miss_still_fails_closed_to_production(raw):
    env = {"PIVOTA_ENV": raw, "K_SERVICE": "web"}
    assert P.platform_env(env) == "production"
    assert P.is_sandbox(env) is False
    with pytest.raises(RuntimeError, match="PIVOTA_ENV"):
        P.require_platform_env(env)


def test_sandbox_is_accepted_only_from_pivota_env():
    # A Railway environment named "sandbox" keeps today's answer: fail closed.
    env = {"RAILWAY_ENVIRONMENT": "sandbox", "RAILWAY_SERVICE_ID": "x"}
    assert P.platform_env(env) == "production"
    assert P.is_sandbox(env) is False


#: (env, platform_env, is_production, is_staging, is_development, is_deployed). The answers every
#: existing label gave before the sandbox label existed; this table is the proof nothing moved.
_TRUTH_TABLE = [
    ({}, "development", False, False, True, False),
    ({"PIVOTA_ENV": "production"}, "production", True, False, False, False),
    ({"PIVOTA_ENV": "production", "K_SERVICE": "web"}, "production", True, False, False, True),
    ({"PIVOTA_ENV": "prod", "CLOUD_RUN_JOB": "j"}, "production", True, False, False, True),
    ({"PIVOTA_ENV": "staging"}, "staging", False, True, False, False),
    ({"PIVOTA_ENV": "staging", "K_SERVICE": "web"}, "staging", False, True, False, True),
    ({"PIVOTA_ENV": "preview", "K_SERVICE": "web"}, "staging", False, True, False, True),
    ({"PIVOTA_ENV": "development"}, "development", False, False, True, False),
    ({"PIVOTA_ENV": "test"}, "test", False, False, True, False),
    ({"K_SERVICE": "web"}, "production", True, False, False, True),
    ({"CLOUD_RUN_JOB": "j"}, "production", True, False, False, True),
    ({"PIVOTA_ENV": "bogus"}, "production", True, False, False, False),
    ({"RAILWAY_ENVIRONMENT": "staging"}, "staging", False, True, False, True),
    ({"RAILWAY_ENVIRONMENT": "production"}, "production", True, False, False, True),
]


@pytest.mark.parametrize("env,resolved,prod,staging,dev,deployed", _TRUTH_TABLE)
def test_every_existing_label_answers_as_before(env, resolved, prod, staging, dev, deployed):
    assert P.platform_env(env) == resolved
    assert P.is_production(env) is prod
    assert P.is_staging(env) is staging
    assert P.is_development(env) is dev
    assert P.is_deployed(env) is deployed
    assert P.is_sandbox(env) is False


def test_queue_worker_is_off_by_default_in_the_sandbox_and_unchanged_elsewhere(monkeypatch):
    from services import audit_scheduler

    monkeypatch.delenv("AUDIT_WORKER_ENABLED", raising=False)
    monkeypatch.delenv("PIVOTA_SERVICE_NAME", raising=False)
    monkeypatch.setenv("K_SERVICE", "worker")
    cases = {"production": True, "staging": False, "sandbox": False}
    for label, expected in cases.items():
        monkeypatch.setenv("PIVOTA_ENV", label)
        assert audit_scheduler._queue_worker_enabled() is expected, label
    monkeypatch.setenv("PIVOTA_ENV", "sandbox")
    monkeypatch.setenv("AUDIT_WORKER_ENABLED", "true")
    assert audit_scheduler._queue_worker_enabled() is True


# ---------------------------------------------------------------------------------------------
# 2. refusals
# ---------------------------------------------------------------------------------------------


def test_the_good_sandbox_profile_passes():
    assert G.sandbox_violations(GOOD) == []
    assert G.enforce_sandbox_guard(GOOD) is True


_REFUSALS = [
    # (case id, changes, setting the refusal must name)
    ("reap_base_unset", {"REAP_API_BASE_URL": None}, "REAP_API_BASE_URL"),
    ("reap_base_prod", {"REAP_API_BASE_URL": "https://prod.api.reap.global"}, "REAP_API_BASE_URL"),
    ("reap_base_sg_prod", {"REAP_API_BASE_URL": "https://sg.prod.api.reap.global"}, "REAP_API_BASE_URL"),
    ("reap_base_lookalike", {"REAP_API_BASE_URL": "https://x.sandbox.api.reap.global"}, "REAP_API_BASE_URL"),
    ("simulate_unset", {"REAP_AGENTIC_SIMULATE_CHECKOUT": None}, "REAP_AGENTIC_SIMULATE_CHECKOUT"),
    ("simulate_lower", {"REAP_AGENTIC_SIMULATE_CHECKOUT": "completed"}, "REAP_AGENTIC_SIMULATE_CHECKOUT"),
    ("a2a_enabled", {"A2A_RAIL_ENABLED": "true"}, "A2A_RAIL_ENABLED"),
    ("a2a_enabled_typo", {"A2A_RAIL_ENABLED": "flase"}, "A2A_RAIL_ENABLED"),
    ("a2a_create", {"A2A_RAIL_CREATE_ENABLED": "1"}, "A2A_RAIL_CREATE_ENABLED"),
    ("a2a_other", {"A2A_RAIL_PILOT_AGENT_IDS": "agent_1"}, "A2A_RAIL_PILOT_AGENT_IDS"),
    ("a2a_key", {"A2A_RAIL_API_KEY": SECRET}, "A2A_RAIL_API_KEY"),
    ("money_acp_live", {"AGENT_ACP_ALLOW_LIVE_CAPTURE": "true"}, "AGENT_ACP_ALLOW_LIVE_CAPTURE"),
    ("money_settlement", {"SETTLEMENT_TRANSFER_ALLOWED_ON_STAGING": "true"}, "SETTLEMENT_TRANSFER_ALLOWED_ON_STAGING"),
    ("demo_fixtures", {"ENABLE_INTERNAL_DEMO_FIXTURES": "true"}, "ENABLE_INTERNAL_DEMO_FIXTURES"),
    ("shakeout_token", {"SHAKEOUT_DEBUG_TOKEN": SECRET}, "SHAKEOUT_DEBUG_TOKEN"),
    ("prod_project", {"GOOGLE_CLOUD_PROJECT": "pivota-prod"}, "GOOGLE_CLOUD_PROJECT"),
    ("prod_sql_socket", {"DATABASE_URL": "postgresql://u:%s@/pivota?host=/cloudsql/pivota-prod:us-west1:pivota-pg" % SECRET}, "DATABASE_URL"),
    ("staging_project", {"SOME_BUCKET": "gs://pivota-staging-exports/x"}, "SOME_BUCKET"),
    ("prod_db_ip", {"DATABASE_URL": "postgresql://u:%s@192.0.2.25:5432/pivota" % SECRET}, "DATABASE_URL"),
    ("prod_redis_ip", {"REDIS_URL": "redis://:%s@192.0.2.196:6379" % SECRET}, "REDIS_URL"),
    ("staging_db_ip", {"DATABASE_URL": "postgresql://u:%s@192.0.2.3/pivota" % SECRET}, "DATABASE_URL"),
    ("prod_api_host", {"PIVOTA_API_BASE": "https://api.pivota.cc"}, "PIVOTA_API_BASE"),
    ("prod_mcp_host", {"PUBLIC_MCP_URL": "https://mcp.pivota.cc/mcp"}, "PUBLIC_MCP_URL"),
    ("prod_apex_in_list", {"CORS_ORIGINS": "https://sandbox.mcp.pivota.cc,https://pivota.cc"}, "CORS_ORIGINS"),
    ("prod_agenpaz", {"AGENPAS_PUBLIC_BASE_URL": "https://pay.agenpaz.com"}, "AGENPAS_PUBLIC_BASE_URL"),
    ("prod_reap_other_var", {"REAP_HOSTED_BASE": "https://prod.api.reap.global"}, "REAP_HOSTED_BASE"),
    # Any *.railway.app host is refused; not the legacy prod URL itself, which
    # tests/test_legacy_backend_url_guard.py keeps out of the tree.
    ("railway_host", {"UPSTREAM": "https://some-service-production.up.railway.app"}, "UPSTREAM"),
    ("railway_proxy", {"DATABASE_URL": "postgresql://u:%s@switchback.proxy.rlwy.net:1/db" % SECRET}, "DATABASE_URL"),
    ("stripe_live", {"STRIPE_SECRET_KEY": "sk_live_" + SECRET}, "STRIPE_SECRET_KEY"),
    ("agent_live_key", {"SHOP_GATEWAY_AGENT_API_KEY": "ak_live_" + SECRET}, "SHOP_GATEWAY_AGENT_API_KEY"),
    ("live_quote_default_on", {"ORDER_CREATE_LIVE_QUOTE_REVALIDATION_ENABLED": None}, "ORDER_CREATE_LIVE_QUOTE_REVALIDATION_ENABLED"),
    ("live_quote_empty", {"ORDER_CREATE_LIVE_QUOTE_REVALIDATION_ENABLED": ""}, "ORDER_CREATE_LIVE_QUOTE_REVALIDATION_ENABLED"),
    ("live_quote_on", {"ORDER_CREATE_LIVE_QUOTE_REVALIDATION_ENABLED": "1"}, "ORDER_CREATE_LIVE_QUOTE_REVALIDATION_ENABLED"),
    ("crawl_fresh", {"CATALOG_TRUST_CRAWL_FRESH_ENABLED": "true"}, "CATALOG_TRUST_CRAWL_FRESH_ENABLED"),
    ("liveness", {"OFFICIAL_DOMAIN_LIVENESS_ENABLED": "1"}, "OFFICIAL_DOMAIN_LIVENESS_ENABLED"),
    ("reprobe_armed", {"STORE_AUDIT_COMMERCE_REPROBE_ARMED": "true"}, "STORE_AUDIT_COMMERCE_REPROBE_ARMED"),
    ("cart_link_enrich", {"REAP_AGENTIC_CART_LINK_ENRICHMENT_ENABLED": "yes"}, "REAP_AGENTIC_CART_LINK_ENRICHMENT_ENABLED"),
    ("pattern_new_flag", {"SHOPIFY_LIVE_READ_ENABLED": "1"}, "SHOPIFY_LIVE_READ_ENABLED"),
    ("pattern_new_refresh", {"FOO_PRICE_REFRESH_ENABLED": "true"}, "FOO_PRICE_REFRESH_ENABLED"),
    ("worker_no_allowlist", {"SCHEDULER_JOB_ALLOWLIST": None}, "SCHEDULER_JOB_ALLOWLIST"),
    ("worker_star", {"SCHEDULER_JOB_ALLOWLIST": "*"}, "SCHEDULER_JOB_ALLOWLIST"),
    ("worker_extra_job", {"SCHEDULER_JOB_ALLOWLIST": "reap_agentic_purchase_poll,catalog_row_trust_crawl_fresh_recheck"}, "SCHEDULER_JOB_ALLOWLIST"),
    # S1: each gap the 5d8e3aa73 review found passing the old deny list, now refused by a positive rule.
    ("s1_live_offer", {"LIVE_OFFER_VERIFICATION_ENABLED": "1"}, "LIVE_OFFER_VERIFICATION_ENABLED"),
    ("s1_live_offer_egress", {"LIVE_OFFER_VERIFICATION_ALLOW_REQUEST_PATH_EGRESS": "1"}, "LIVE_OFFER_VERIFICATION_ALLOW_REQUEST_PATH_EGRESS"),
    ("s1_preflight_enforce", {"CHECKOUT_PREFLIGHT_MODE": "enforce"}, "CHECKOUT_PREFLIGHT_MODE"),
    ("s1_preflight_shadow", {"CHECKOUT_PREFLIGHT_MODE": "shadow"}, "CHECKOUT_PREFLIGHT_MODE"),
    ("s1_preflight_egress", {"CHECKOUT_PREFLIGHT_ALLOW_EGRESS": "1"}, "CHECKOUT_PREFLIGHT_ALLOW_EGRESS"),
    ("s1_any_egress", {"FOO_ALLOW_EGRESS": "true"}, "FOO_ALLOW_EGRESS"),
    ("s1_visa_env", {"VISA_VDP_ENV": "production"}, "VISA_VDP_ENV"),
    ("s1_visa_allow_prod", {"VISA_VDP_ALLOW_PRODUCTION": "yes"}, "VISA_VDP_ALLOW_PRODUCTION"),
    ("s1_sendgrid", {"SENDGRID_API_KEY": "SG.%s" % SECRET}, "SENDGRID_API_KEY"),
    ("s1_smtp2go", {"SMTP2GO_API_KEY": "api-%s" % SECRET}, "SMTP2GO_API_KEY"),
    ("s1_twilio", {"TWILIO_AUTH_TOKEN": SECRET}, "TWILIO_AUTH_TOKEN"),
    ("s1_adyen", {"ADYEN_API_KEY": "AQE%s" % SECRET}, "ADYEN_API_KEY"),
    ("s1_stripe_live", {"STRIPE_SECRET_KEY": "sk_live_%s" % SECRET}, "STRIPE_SECRET_KEY"),
    ("s1_stripe_unshaped", {"STRIPE_SECRET_KEY": SECRET}, "STRIPE_SECRET_KEY"),
    ("s1_aws_key", {"AWS_SECRET_ACCESS_KEY": SECRET}, "AWS_SECRET_ACCESS_KEY"),
    ("s1_reap_ext_auth", {"REAP_EXTERNAL_AUTH_ENABLED": "1"}, "REAP_EXTERNAL_AUTH_ENABLED"),
    ("s1_refund_unset", {"FF_ENABLE_INTERNAL_REFUND": None}, "FF_ENABLE_INTERNAL_REFUND"),
    ("s1_refund_on", {"FF_ENABLE_INTERNAL_REFUND": "true"}, "FF_ENABLE_INTERNAL_REFUND"),
    ("s1_order_failure_refund_unset", {"FF_ENABLE_MERCHANT_ORDER_FAILURE_REFUND": None}, "FF_ENABLE_MERCHANT_ORDER_FAILURE_REFUND"),
    ("s1_other_refund_flag", {"FF_ENABLE_REFUND_AUTO_RETRY": "true"}, "FF_ENABLE_REFUND_AUTO_RETRY"),
    ("s1_environment_prod", {"ENVIRONMENT": "production"}, "ENVIRONMENT"),
    ("s1_app_env_prod", {"APP_ENV": "production"}, "APP_ENV"),
    ("s1_project_unset", {"GOOGLE_CLOUD_PROJECT": None}, "GOOGLE_CLOUD_PROJECT"),
    ("s1_project_other", {"GOOGLE_CLOUD_PROJECT": "pivota-sandbox-2"}, "GOOGLE_CLOUD_PROJECT"),
    ("s1_db_unset", {"DATABASE_URL": None}, "DATABASE_URL"),
    ("s1_db_other_ip", {"DATABASE_URL": "postgresql://u:%s@203.0.113.70:5432/pivota" % SECRET}, "DATABASE_URL"),
    ("s1_db_hostname", {"DATABASE_URL": "postgresql://u:%s@db.example.com/pivota" % SECRET}, "DATABASE_URL"),
    ("s1_second_dsn", {"SHOPIFY_DISCOUNT_CANARY_DATABASE_URL": "postgresql://u:%s@203.0.113.70/x" % SECRET}, "SHOPIFY_DISCOUNT_CANARY_DATABASE_URL"),
    ("s1_redis", {"REDIS_URL": "redis://:%s@203.0.113.70:6379/0" % SECRET}, "REDIS_URL"),
    ("s1_redis_host", {"REDIS_HOST": "203.0.113.70"}, "REDIS_HOST"),
    ("s1_run_app", {"SOME_SERVICE_URL": "https://web-abc123-uw.a.run.app"}, "SOME_SERVICE_URL"),
    ("s1_url_in_any_name", {"FEATURE_TARGET": "https://api.pivota.cc/x"}, "FEATURE_TARGET"),
    ("s1_redis_scheme_elsewhere", {"CACHE_TARGET": "redis://cache.example.com:6379"}, "CACHE_TARGET"),
    ("s1_bare_host", {"INDEXNOW_HOST": "agent.pivota.cc"}, "INDEXNOW_HOST"),
    ("s1_bare_ip_host", {"UPSTREAM_HOST": "203.0.113.70:8080"}, "UPSTREAM_HOST"),
    ("s1_ip_anywhere", {"FOO_TARGETS": "a,203.0.113.70"}, "FOO_TARGETS"),
    ("s1_gs_other_bucket", {"CATALOG_COPY_GCS_URI": "gs://pivota-prod-backups/catalog"}, "CATALOG_COPY_GCS_URI"),
    ("s1_agent_url_unset", {"PIVOTA_AGENT_INTERNAL_URL": None}, "PIVOTA_AGENT_INTERNAL_URL"),
    ("s1_agent_url_prod", {"PIVOTA_AGENT_INTERNAL_URL": "https://gateway.pivota.cc"}, "PIVOTA_AGENT_INTERNAL_URL"),
    ("s1_warm_url_unset", {"OUTBOUND_WARM_HANDOFF_RESOLVE_URL": None}, "OUTBOUND_WARM_HANDOFF_RESOLVE_URL"),
    ("s1_store_token", {"SHOPIFY_ACCESS_TOKEN": "shpat_%s" % SECRET}, "SHOPIFY_ACCESS_TOKEN"),
    ("s1_cafe24_secret", {"CAFE24_CLIENT_SECRET": SECRET}, "CAFE24_CLIENT_SECRET"),
]


@pytest.mark.parametrize("case,changes,setting", _REFUSALS, ids=[c[0] for c in _REFUSALS])
def test_each_bad_setting_is_refused_by_name_never_by_value(case, changes, setting):
    env = _with(**changes)
    names = [v.setting for v in G.sandbox_violations(env)]
    assert setting in names, (case, names)
    with pytest.raises(G.SandboxGuardRefused) as excinfo:
        G.enforce_sandbox_guard(env)
    message = str(excinfo.value)
    assert setting in message
    assert SECRET not in message
    for value in changes.values():
        if value and value not in ("1", "true", "yes", "*", "completed", "flase"):
            assert value not in message, (case, "the refusal quoted a value")


@pytest.mark.parametrize("changes", [
    {"STRIPE_SECRET_KEY": "sk_test_abc", "STRIPE_PUBLISHABLE_KEY": "pk_test_abc"},
    {"STRIPE_REQUEST_TIMEOUT_SECONDS": "30", "STRIPE_MAX_NETWORK_RETRIES": "2"},
    {"VISA_VDP_ENV": "sandbox", "VISA_VDP_ALLOW_PRODUCTION": "false"},
    {"SENDGRID_API_KEY": "", "ADYEN_API_KEY": "  "},
    {"ENVIRONMENT": "sandbox", "APP_ENV": "sandbox"},
    {"CHECKOUT_PREFLIGHT_MODE": "off", "CHECKOUT_PREFLIGHT_ALLOW_EGRESS": "0"},
    {"FF_ENABLE_INTERNAL_REFUND": "0", "FF_ENABLE_REFUND_AUTO_RETRY": "false"},
    {"PIVOTA_AGENT_INTERNAL_URL": "https://sandbox.mcp.pivota.cc"},
    {"DATABASE_URL": "postgresql://u:p@/pivota?host=/cloudsql/pivota-sandbox:us-west1:pivota-pg"},
    {"DATABASE_URL_NOVERIFY": "postgresql://u:p@198.51.100.126:5432/pivota", "SHOPIFY_DISCOUNT_CANARY_DATABASE_URL": ""},
    {"REDIS_URL": "", "REAP_RETURN_URL_HOSTS": "sandbox.agenpaz.com,sandbox.mcp.pivota.cc"},
    {"PORT": "8080", "PYTHON_VERSION": "3.11.9", "DB_POOL_MAX_SIZE": "4"},
])
def test_the_safe_form_of_each_s1_setting_passes(changes):
    assert G.sandbox_violations(_with(**changes)) == []


def test_the_s1_positive_checks_are_in_addition_to_the_deny_list():
    # A prod project in a value is still refused by the old rule as well as the new identity rule.
    names = [v.setting for v in G.sandbox_violations(_with(GOOGLE_CLOUD_PROJECT="pivota-prod"))]
    assert names.count("GOOGLE_CLOUD_PROJECT") == 2


def test_a_star_allowlist_is_refused_as_all_jobs():
    rules = [v.rule for v in G.sandbox_violations(_with(SCHEDULER_JOB_ALLOWLIST="reap_agentic_purchase_poll,*"))]
    assert rules == ["'*' (all jobs) is refused in the sandbox"]


def test_only_the_bad_setting_is_named():
    env = _with(A2A_RAIL_ENABLED="true")
    assert [v.setting for v in G.sandbox_violations(env)] == ["A2A_RAIL_ENABLED"]


@pytest.mark.parametrize("value", ["", "0", "false", "off", "no", " FALSE "])
def test_off_values_are_accepted_for_the_a2a_dials(value):
    assert G.sandbox_violations(_with(A2A_RAIL_ENABLED=value, A2A_RAIL_CREATE_ENABLED=value)) == []


def test_an_inverse_polarity_flag_does_not_match_the_pattern():
    # *_DISABLED=true is the SAFE side; the catch-all must not trip on it.
    assert G.sandbox_violations(_with(IDENTITY_LIVE_READ_DISABLED="true")) == []


def test_the_two_consent_links_are_allowed_exactly():
    env = _with(AGENPAS_TERMS_URL="https://pivota.cc/terms",
                AGENPAS_PRIVACY_URL="https://merchant.pivota.cc/privacy")
    assert G.sandbox_violations(env) == []


@pytest.mark.parametrize("name,value", [
    ("AGENPAS_TERMS_URL", "https://pivota.cc/terms?x=1"),
    ("AGENPAS_TERMS_URL", "https://pivota.cc/terms/other"),
    ("AGENPAS_TERMS_URL", "https://api.pivota.cc/terms"),
    ("AGENPAS_PRIVACY_URL", "https://pivota.cc/terms"),
    ("SOME_OTHER_URL", "https://pivota.cc/terms"),
    ("AGENPAS_PRIVACY_URL", "https://merchant.pivota.cc/privacy,https://api.pivota.cc"),
])
def test_anything_but_the_exact_consent_pair_is_still_refused(name, value):
    assert name in [v.setting for v in G.sandbox_violations(_with(**{name: value}))]


def test_a_web_process_needs_no_allowlist():
    env = _with(AUDIT_WORKER_ENABLED=None, SCHEDULER_JOB_ALLOWLIST=None)
    assert G.sandbox_violations(env) == []


def test_only_the_listed_hosts_are_allowed_in_a_url():
    # S1: an allowlist. The two sandbox hosts and Reap's sandbox hosts pass ...
    env = _with(
        X1="https://sandbox.mcp.pivota.cc", X2="https://sandbox.agenpaz.com/x",
        X3="https://sg.sandbox.api.reap.global/v1", X4="gs://pivota-sandbox-catalog-drop/catalog",
    )
    assert G.sandbox_violations(env) == []
    # ... and a host that merely LOOKS like a sandbox host is refused: nobody listed it.
    for value in ("https://sandbox-developer.pivota.cc/x", "https://pay-sandbox.agenpaz.com",
                  "https://x.sandbox.mcp.pivota.cc", "https://sandbox.mcp.pivota.cc.evil.example"):
        assert [v.setting for v in G.sandbox_violations(_with(X=value))] == ["X"], value


def test_every_refusal_is_reported_at_once():
    env = _with(
        REAP_API_BASE_URL="https://prod.api.reap.global",
        A2A_RAIL_ENABLED="true",
        GOOGLE_CLOUD_PROJECT="pivota-prod",
    )
    names = {v.setting for v in G.sandbox_violations(env)}
    assert {"REAP_API_BASE_URL", "A2A_RAIL_ENABLED", "GOOGLE_CLOUD_PROJECT"} <= names


# ---------------------------------------------------------------------------------------------
# 3. production and staging are untouched
# ---------------------------------------------------------------------------------------------

#: Every refusal above at once, in one environment.
HOSTILE = {}
for _case, _changes, _setting in _REFUSALS:
    for _k, _v in _changes.items():
        if _v is not None:
            HOSTILE.setdefault(_k, _v)
HOSTILE.pop("PIVOTA_ENV", None)


@pytest.mark.parametrize("label", ["production", "staging", "development", "test", None])
def test_the_guard_is_a_no_op_outside_the_sandbox(label):
    env = dict(HOSTILE)
    env["K_SERVICE"] = "web"
    if label:
        env["PIVOTA_ENV"] = label
    else:
        env.pop("K_SERVICE")  # local: no marker, no label
    # The very settings that make a sandbox refuse do nothing here.
    assert G.enforce_sandbox_guard(env) is False


def test_the_guard_reads_nothing_but_the_platform_outside_the_sandbox():
    class Spy(dict):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.read = set()

        def get(self, key, default=None):
            self.read.add(key)
            return super().get(key, default)

        def __getitem__(self, key):
            self.read.add(key)
            return super().__getitem__(key)

        def __iter__(self):
            raise AssertionError("the guard enumerated settings outside the sandbox")

    for label in ("production", "staging"):
        spy = Spy(HOSTILE, PIVOTA_ENV=label, K_SERVICE="web")
        assert G.enforce_sandbox_guard(spy) is False
        assert spy.read <= set(_PLATFORM_KEYS) | {"PIVOTA_PLATFORM"}, spy.read - set(_PLATFORM_KEYS)


def test_the_lifespan_runs_the_guard(monkeypatch):
    """main.app_lifespan refuses a bad sandbox at boot, and only a sandbox."""
    from fastapi.testclient import TestClient

    from main import app

    for key in list(GOOD):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PIVOTA_ENV", "sandbox")
    monkeypatch.setenv("K_SERVICE", "web")
    monkeypatch.setenv("REAP_API_BASE_URL", "https://prod.api.reap.global")
    with pytest.raises(G.SandboxGuardRefused, match="REAP_API_BASE_URL"):
        with TestClient(app):
            pass


def test_the_lifespan_calls_the_guard_right_after_the_platform_check():
    import inspect

    import main

    source = inspect.getsource(main.app_lifespan)
    assert source.index("require_platform_env()") < source.index("enforce_sandbox_guard()")
    assert source.index("enforce_sandbox_guard()") < source.index("install_uvicorn_access_log_redaction()")


def test_this_file_does_not_leak_platform_env():
    assert "PIVOTA_ENV" not in os.environ or os.environ["PIVOTA_ENV"] != "sandbox"


# --- hashed addresses (the repo is public) -------------------------------------------------------
def test_the_prod_ips_are_held_only_as_three_sha256_digests():
    from tests.sandbox_test_addresses import REAL_PROD_IP_SHA256

    assert len(REAL_PROD_IP_SHA256) == 3
    assert all(re.fullmatch(r"[0-9a-f]{64}", d) for d in REAL_PROD_IP_SHA256)
    assert not hasattr(G, "_PROD_PRIVATE_IPS") and not hasattr(G, "_PROD_IP_RE")


@pytest.mark.parametrize("value,hit", [
    ("postgresql://u@192.0.2.25/db", True),
    ("redis://192.0.2.196:6379", True),
    ("host=192.0.2.3", True),
    ("::ffff:192.0.2.25", True),
    ("192.0.2.25.nip.io", True),
    ("192.0.2.250", False),      # a longer last octet is another address
    ("192.0.2.1960", False),     # a 4-digit last octet is no address, not 192.0.2.196
    ("1192.0.2.25", False),      # the token is 1192.0.2.25: not an address
    ("203.192.0.2.25", False),   # the token is 203.192.0.2: another address
    ("999.0.2.25 010.0.2.25", False),  # not addresses: ignored, never raises
    ("198.51.100.5", False),
])
def test_a_prod_ip_is_found_by_its_normalized_hash(value, hit):
    assert G._names_a_prod_ip(value) is hit


def test_no_sandbox_file_holds_a_real_prod_ip_or_the_real_sandbox_range():
    """Assert by hash: every dotted quad in the sandbox's code, docs and tests is hashed and compared
    with the real digests (the stand-in fixture does not touch these)."""
    import ipaddress
    from pathlib import Path

    from config.sandbox_identity import _masked_sha256
    from tests.sandbox_test_addresses import REAL_PROD_IP_SHA256, REAL_SANDBOX_DB_NETWORKS_SHA256, ip_sha256

    root = Path(__file__).resolve().parents[1]
    files = [p for pattern in ("config/sandbox_*.py", "config/platform.py", "jobs/sandbox_*.py", "scripts/sandbox/*",
                               "docs/sandbox/*", "tests/test_sandbox_*.py", "tests/test_register_gateway_service_agent*.py",
                               "tests/sandbox_test_addresses.py")
             for p in sorted(root.glob(pattern)) if p.is_file()]
    assert len(files) >= 15
    found = []
    for path in files:
        for token in G._IPV4_TOKEN_RE.findall(path.read_text(encoding="utf-8")):
            try:
                ip = ipaddress.ip_address(token)
            except ValueError:
                continue
            if ip_sha256(token) in REAL_PROD_IP_SHA256 or any(
                    _masked_sha256(ip, prefix) == digest for prefix, digest in REAL_SANDBOX_DB_NETWORKS_SHA256):
                found.append(path.relative_to(root).as_posix())  # the file, never the address
    assert not found, found
