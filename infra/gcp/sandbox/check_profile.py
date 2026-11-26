#!/usr/bin/env python3
"""Static guard for the partner sandbox's env profiles and secret lists.

    python3 infra/gcp/sandbox/check_profile.py                  # check the committed templates
    python3 infra/gcp/sandbox/check_profile.py --render OUTDIR \
        --web-url https://web-123.us-west1.run.app \
        --gateway-host sandbox.mcp.pivota.cc --agenpaz-host sandbox.agenpaz.com \
        --sandbox-project pivota-sandbox
                                                                # render + check (deploy_sandbox.sh)

Exit 0 = every rule holds. Any violation prints one line per problem and exits 1, and
deploy_sandbox.sh will not deploy. This is the infra half of "the sandbox can never move money,
fetch a store, or hold a prod credential"; the runtime half is the services' own startup check
(SANDBOX_STARTUP_CHECK=enforce, owned by the sandbox-backend / sandbox-gateway work).

Rules, each one a test in tests/test_sandbox_infra.py:
  R1  every profile sets PIVOTA_ENV=sandbox (the backend's and the gateway's boot checks key off it)
      and GOOGLE_CLOUD_PROJECT=@@SANDBOX_PROJECT@@, rendered to the sandbox project id: two
      independent signals, both read back off each new revision by deploy_sandbox.sh
  R2  no key starting with A2A_ anywhere (the real-money A2A rail is not configured at all)
  R3  web/worker: REAP_API_BASE_URL is exactly the Reap SANDBOX host, and
      REAP_AGENTIC_SIMULATE_CHECKOUT=COMPLETED
  R4  gateway/agenpaz: AGENPAS_RAILS is exactly "reap" and AGENPAS_CONSENT_ENABLED=true (without
      consent the gateway drops reap and mints a2a links)
  R5  every switch in MUST_BE_OFF is present and off (live reads, store egress, schedulers, sends,
      other payment paths). Present, not merely absent: several default ON in code. And
      gateway/agenpaz pin SKIP_DB_MIGRATIONS=true + DB_AUTO_MIGRATE=false: no public request may
      start a schema migration.
  R6  every URL or host in a value is on the sandbox allowlist; prod/staging/Railway hosts and any
      run.app host other than the sandbox's own web are refused
  R7  no secret-shaped key as a plain env var (secrets come only from secrets/*.list)
  R8  secret lists name only ALLOWED_SECRETS, and no env name twice; a secret in SECRET_ONLY_IN is
      mounted nowhere else, and one in SECRET_REQUIRED_IN by every service listed there
  R9  worker.yaml equals web.yaml except WORKER_ONLY_DIFFERENCES
  R10 a file is exactly `KEY: "value"` lines (+ comments); no duplicate keys (gcloud's
      --env-vars-file silently keeps the FIRST duplicate, infra/gcp/deploy_backend.sh)
  R11 after --render, no @@TOKEN@@ is left
  R13 web/worker: ISSUING_AGENT_EXCLUDED_AGENT_IDS and ISSUING_ASSERTION_VOUCHER_AGENT_IDS both name
      exactly the sandbox gateway's service agent (config.sh SANDBOX_GATEWAY_AGENT_ID)
  R12 gateway/agenpaz: no NODE_ENV/APP_ENV of production or test (the gateway's boot check refuses
      both: production would relabel the service, test disables auth)
  R6 also, in EVERY value of every service (URLs, bare hosts, `//host`, emails): a host under
      pivota.cc, agenpaz.com, agenpas.com or reap.global must carry a label containing "sandbox",
      as both boot checks require. Exceptions are exact: web/worker AGENPAS_TERMS_URL and
      AGENPAS_PRIVACY_URL at their two exact values (sandbox-backend), and AGENPAS_LEGAL_HOSTS
      listing only pivota.cc / merchant.pivota.cc (sandbox-gateway). No value may contain
      pivota-prod or pivota-staging. Every run.app host must be the sandbox's own web, so all
      run.app URLs share one project part (the gateway's allow-list rule).
  R14 no value, under ANY name, looks like a live credential: sk_/rk_/pk_live_, an ak_ agent key
      (live or test), whsec_, a private key block, an AWS/Google/GitHub/Slack key or token, or a
      DSN with a password
  R15 no value holds an IPv4 literal at all: the DB and Redis arrive only through mounted secrets,
      so any address in a profile is a mistake (and the public repo carries no private IP)
  R16 secret lists: every mounted ENV name is secret-shaped, is not A2A_*, NODE_ENV/APP_ENV or an
      ISSUING_* id, and does not shadow a key the service's profile sets or the rules pin
  R17 PRICE_CACHE_FIRESTORE_PROJECT / PIVOTA_AGENT_CENTER_GEMINI_VERTEX_PROJECT, when set, equal
      GOOGLE_CLOUD_PROJECT; gateway/agenpaz AURORA_ROUTES_FAIL_CLOSED / AURORA_BFF_FAIL_CLOSED are
      unset or off (the Aurora BFF is not loaded in the sandbox)
  R7 also refuses plain PGHOST / PGHOSTADDR / REDIS_HOST: the DB and Redis come only from secrets.
"""
from __future__ import annotations

import argparse
import ipaddress
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
SERVICES = ("web", "worker", "gateway", "agenpaz")
LINE_RE = re.compile(r'^([A-Za-z][A-Za-z0-9_]*): "([^"\\]*)"$')
TOKEN_RE = re.compile(r"@@([A-Z_]+)@@")
TOKENS = {"WEB_URL", "GATEWAY_HOST", "AGENPAZ_HOST", "AGENPAZ_PUBLIC_URL", "SANDBOX_PROJECT"}
# The same rule config.sh applies to SANDBOX_PROJECT (format first, then no environment word).
PROJECT_ID_RE = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")
ENV_WORDS = {"prod", "production", "staging", "stage", "stg", "shared"}


def valid_sandbox_project(pid: str) -> bool:
    return bool(PROJECT_ID_RE.fullmatch(pid or "")) and not (set(pid.split("-")) & ENV_WORDS)

REAP_SANDBOX_BASE_URL = "https://sandbox.api.reap.global"
# An empty value is NOT off: several of these switches default ON in code when empty (review S7).
OFF = {"0", "false", "off"}

# R5: present AND off. Per service.
_BACKEND_OFF = [
    "AGENT_ACP_ALLOW_LIVE_CAPTURE", "AGENT_ACP_TEST_CAPTURE", "ALLOW_TEST_PSP_PROBE", "SUBMIT_PAYMENT",
    "ALLOW_SHAKEOUT_ON_PROD", "AGENT_CARD_ISSUANCE_ENABLED", "REAP_EXTERNAL_AUTH_ENABLED",
    "FF_ENABLE_REFUND_AUTO_RETRY", "FF_ENABLE_PLATFORM_SYNC_OUTBOUND", "FF_ENABLE_PLATFORM_WEBHOOK_REFUND",
    "ORDER_CREATE_LIVE_QUOTE_REVALIDATION_ENABLED",
    "LIVE_OFFER_VERIFICATION_ENABLED", "LIVE_OFFER_VERIFICATION_ALLOW_REQUEST_PATH_EGRESS",
    "CHECKOUT_PREFLIGHT_MODE", "CHECKOUT_PREFLIGHT_ALLOW_EGRESS", "OFFICIAL_DOMAIN_LIVENESS_ENABLED",
    "OUTBOUND_WARM_HANDOFF_ENABLED", "AGENT_CHECKOUT_UCP_DISCOVERY_ENABLED", "SERVE_PDP_ENRICHMENT_ON_WRITE",
    "CATALOG_ONBOARD_ENABLED", "INDEXNOW_ENABLED", "GSC_INTEGRATION_ENABLED", "WEBHOOK_DELIVERY_ENABLED",
    "ORDER_CONFIRMATION_EMAIL_ENABLED", "VERTEX_AI_ENABLED",
    "REAP_AGENTIC_CART_LINK_ENRICHMENT_ENABLED", "REAP_AGENTIC_PREFLIGHT_MODE", "REAP_AGENTIC_PRICE_WRITEBACK",
    "REVIEWS_INVITATION_WORKER_ENABLED", "EXTERNAL_CONVERSION_POLLER_ENABLED", "PAYMENT_RECONCILE_SWEEP_ENABLED",
    "ENABLE_IDENTITY_RECONCILE_SWEEP", "QUALITY_BACKFILL_WORKER_ENABLED", "CATALOG_INVARIANT_SWEEP_ENABLED",
    "COMMERCE_INDEX_INSIGHT_REFRESH_ENABLED", "AGENT_CARD_REVOCATION_SWEEP_ENABLED",
    "STORE_AUDIT_UCP_PROBE_RECEIPT_ENABLED", "STORE_AUDIT_COMMERCE_PROBE_RECEIPT_ENABLED",
    "STORE_AUDIT_UCP_REPROBE_SCHEDULER_ENABLED", "STORE_AUDIT_COMMERCE_REPROBE_SCHEDULER_ENABLED",
]
MUST_BE_OFF = {
    "web": _BACKEND_OFF + ["AUDIT_WORKER_ENABLED"],
    "worker": _BACKEND_OFF,
    "gateway": [
        "AGENT_CHECKOUT_ALLOW_TEST_PSP", "AGENT_CHECKOUT_ALLOW_TEST_IDENTITY", "AGENT_CHECKOUT_MCP_ENABLE_AP2_MANDATE",
        "AGENT_CHECKOUT_UCP_DISCOVERY_ENABLED", "AGENT_CHECKOUT_UCP_TOOL_DOOR_ENABLED",
        "AGENT_CHECKOUT_UCP_ESCALATION_ENABLED", "AGENT_CHECKOUT_UCP_MERCHANT_PRICING_ENABLED",
        "AGENT_CHECKOUT_UCP_SELLER_DISCOUNT_CODES_ENABLED", "AGENT_CHECKOUT_HOSTED_LINK_ENABLED",
        "AGENT_CHECKOUT_ACP_FEED_ENABLED", "ACP_PUBLIC_FEED", "UCP_WARM_HANDOFF_ENABLED",
        "UCP_WARM_HANDOFF_INTERNAL_ROUTE_ENABLED", "PUBLIC_READ_MCP_ENABLED", "MCP_OAUTH_ENABLED",
        "SERVE_LIVE_MERCHANT_PRICE", "PDP_IDENTITY_GRAPH_AUTO_ENABLE_LIVE", "PDP_IDENTITY_AUTO_RESOLVE_ENABLED",
        "PDP_CORE_PREWARM_ENABLED", "AURORA_BFF_PDP_HOTSET_PREWARM_ENABLED", "CREATOR_CATALOG_AUTO_SYNC_ENABLED",
        "CATALOG_IMAGE_CACHE_BOOTSTRAP_ENABLED", "CATALOG_IMAGE_CACHE_JOB_RUNNER_ENABLED",
        "AURORA_TRAVEL_WEATHER_LIVE_ENABLED", "VERTEX_AI_ENABLED", "PIVOTA_INTENT_LLM_ENABLED",
        "REAP_AGENTIC_CART_LINK_ENRICHMENT_ENABLED", "DB_AUTO_MIGRATE",
    ],
    "agenpaz": ["AGENPAS_COLLECT_SHIPPING"],
}
# The gateway boot check's live-read list (sandbox-gateway's src/config/sandbox.js).
_GATEWAY_LIVE_OFF = ["UCP_LIVE_OFFER_READ", "UCP_LIVE_SEARCH_READ", "UCP_LIVE_PRODUCT_READ", "UCP_LIVE_PDP_READ",
                     "GET_OFFERS_LIVE_CART", "CAFE24_CATALOG_SEARCH", "MERCHANT_VARIANT_SOURCING_ENABLED",
                     "SERVE_LIVE_MERCHANT_PRICE", "PDP_IDENTITY_GRAPH_AUTO_ENABLE_LIVE",
                     "CATALOG_IMAGE_CACHE_BOOTSTRAP_ENABLED", "CATALOG_IMAGE_CACHE_JOB_RUNNER_ENABLED"]
for _svc in ("gateway", "agenpaz"):
    MUST_BE_OFF[_svc] += [k for k in _GATEWAY_LIVE_OFF + ["VERTEX_AI_ENABLED"] if k not in MUST_BE_OFF[_svc]]
# The backend guard's "unset or off" names (sandbox-backend's config/sandbox_guard.py), set explicitly here.
for _svc in ("web", "worker"):
    MUST_BE_OFF[_svc] += ["LEGACY_SETTLEMENT_LIVE", "SETTLEMENT_TRANSFER_ALLOWED_ON_STAGING", "ENABLE_INTERNAL_DEMO_FIXTURES"]
# Not "off" but must hold exactly this value.
MUST_EQUAL = {
    "web": {"REAP_API_BASE_URL": REAP_SANDBOX_BASE_URL, "REAP_AGENTIC_SIMULATE_CHECKOUT": "COMPLETED",
            "SCHEDULER_JOB_ALLOWLIST": ",", "DISABLE_WEBHOOK_OUTBOX": "true"},
    "worker": {"REAP_API_BASE_URL": REAP_SANDBOX_BASE_URL, "REAP_AGENTIC_SIMULATE_CHECKOUT": "COMPLETED",
               "SCHEDULER_JOB_ALLOWLIST": "reap_agentic_purchase_poll,agent_purchase_events",
               "DISABLE_WEBHOOK_OUTBOX": "true"},
    "gateway": {"AGENPAS_RAILS": "reap", "AGENPAS_CONSENT_ENABLED": "true", "LLM_KILL_SWITCH": "true",
                "SKIP_DB_MIGRATIONS": "true", "DB_AUTO_MIGRATE": "false"},
    "agenpaz": {"AGENPAS_RAILS": "reap", "AGENPAS_CONSENT_ENABLED": "true", "LLM_KILL_SWITCH": "true",
                "SKIP_DB_MIGRATIONS": "true", "DB_AUTO_MIGRATE": "false",
                # These two make the AgenPaz host EXCLUSIVE (PIVOTA-Agent src/agenpas/router.js host
                # guard, mounted first): only the buyer pages and the consent/address posts are served,
                # so agenpaz never runs a partner route and never needs the assertion key (S9 review).
                # Off, the same image would serve every gateway route on that host, unsigned.
                # (AGENPAS_PUBLIC_BASE_URL, the other one, is checked in check_service: it is rendered.)
                "AGENPAS_ENABLED": "1"},
}
WORKER_ONLY_DIFFERENCES = {"AUDIT_WORKER_ENABLED", "SCHEDULER_JOB_ALLOWLIST",
                           "DB_STATEMENT_TIMEOUT_SECONDS", "REAP_AGENTIC_CONTACT_MAX_AGE_SECONDS"}

# R6. Hosts any value may name. The sandbox's own web run.app host is added at render time.
ALLOWED_HOSTS = {"sandbox.api.reap.global", "sandbox.collect.prava.space", "pivota.cc", "merchant.pivota.cc"}
DENIED_HOST_PATTERNS = [
    re.compile(p) for p in (
        r"(^|\.)railway\.(app|internal)$", r"staging", r"(^|\.)agenzo\.com$",
        r"^(api|gateway|mcp|commerce\.mcp|ucp|acp|agent|agents)\.pivota\.cc$",
        r"^pay\.agenpa[sz]\.com$", r"^api\.reap\.global$",
    )
]
# R7. Plain env names that look secret-shaped but are not secrets.
SECRET_SHAPE = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE|PEM|JWK|CREDENTIAL|VERIFIER|DATABASE_URL|REDIS_URL|DSN)", re.I)
NOT_SECRET = {"AGENT_MAX_TOOL_CONTENT_CHARS"}

# R8. The sandbox's whole secret inventory (secrets/README.md).
ALLOWED_SECRETS = {
    "DATABASE_URL", "DATABASE_URL_NOVERIFY", "SCHEMA_APPLY_DATABASE_URL", "SCHEMA_APPLY_ROLE_VERIFIER", "REAP_API_KEY",
    "AGENT_AUTH_INTROSPECT_INTERNAL_KEY", "SHOP_GATEWAY_AGENT_API_KEY", "JWT_SECRET_KEY",
    "EXTERNAL_OFFERS_INTERNAL_KEY", "AGENPAS_LINK_SECRET", "AGENPAS_INTERNAL_READ_KEY",
    "AGENPAS_INTERNAL_CONSENT_KEY", "AGENPAS_CONSENT_OFFER_SECRET", "CHECKOUT_TOKEN_SECRET",
    "CHECKOUT_UI_KEY", "ORDER_TRACK_TOKEN_SECRET", "ADMIN_API_KEY", "CONNECTOR_CREDENTIALS_KEY",
    "METRICS_BEARER_TOKEN", "AGENT_CHECKOUT_CONFIRMATION_SECRET", "CONFIRMATION_SECRET",
    # the gateway's own service key: an ak_test_<64 hex> key minted in the sandbox (its hash is
    # registered as the gateway's service agent in the sandbox DB, RUNBOOK.md step 14)
    "PIVOTA_API_KEY",
    # review S9: the HMAC key of the issuing-agent assertion (X-Pivota-Issuing-Agent). The gateway
    # signs the partner it verified, web verifies it (services/issuing_agent_assertion.py), and only
    # then is a link/order credited to the partner instead of to nobody. Minted in the sandbox.
    "ISSUING_AGENT_ASSERTION_SECRET",
}
# Not a mount (no service reads it), but it lives in the project: the Cloud SQL user's password.
PROJECT_ONLY_SECRETS = {"pivota-db-password"}

# Backend-guard host rule (R6, web/worker): hosts under these zones need a "sandbox" label.
BRAND_ZONES = ("pivota.cc", "agenpaz.com", "agenpas.com", "reap.global")
# The buyer-facing consent links (BRAND_PAIR_EXCEPTIONS below): sandbox-backend allows exactly
# those two (setting, value) pairs; reported on every run until that commit is in the release.
NODE_ENV_KEYS = ("NODE_ENV", "APP_ENV")
GATEWAY_AGENT_KEYS = ("ISSUING_AGENT_EXCLUDED_AGENT_IDS", "ISSUING_ASSERTION_VOUCHER_AGENT_IDS")
# R6 brand-zone exceptions, exact: (key, value) pairs for web/worker, and the legal-host list.
BRAND_PAIR_EXCEPTIONS = {("AGENPAS_TERMS_URL", "https://pivota.cc/terms"),
                         ("AGENPAS_PRIVACY_URL", "https://merchant.pivota.cc/privacy")}
LEGAL_HOSTS_KEY, LEGAL_HOSTS_ALLOWED = "AGENPAS_LEGAL_HOSTS", {"pivota.cc", "merchant.pivota.cc"}
REFUSED_SUBSTRINGS = ("pivota-prod", "pivota-staging")
# Hosts anywhere in a value: a lowercase dotted name whose last label is letters (so 0.35 or 2.5
# never match). `@` may precede, so emails count, and so may a dot: `.api.reap.global/v1` is that
# host once joined onto a base (review); findall is leftmost-first, so a full name is never cut into
# a partial label. A word char may not precede.
DOMAIN_RE = re.compile(r"(?<![a-z0-9_-])((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63})(?![a-z0-9_-])")
IPV4_RE = re.compile(r"(?<![0-9.])(\d{1,3}(?:\.\d{1,3}){3})(?![0-9.])")

# R14: credential-shaped VALUES, whatever the key is called.
LIVE_SECRET_PATTERNS = [re.compile(p) for p in (
    r"\b(?:sk|rk|pk)_live_[0-9A-Za-z]{8,}", r"\bak_(?:live_|test_)?[0-9a-f]{64}\b", r"\bwhsec_[0-9A-Za-z]{16,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----", r"\bAKIA[0-9A-Z]{16}\b", r"\bAIza[0-9A-Za-z_-]{35}\b",
    r"\bya29\.[0-9A-Za-z_-]{20,}", r"\bgh[pousr]_[0-9A-Za-z]{30,}", r"\bxox[abposr]-[0-9A-Za-z-]{10,}",
    r"\b(?:postgres(?:ql)?|redis|rediss|mysql)(?:\+\w+)?://[^\s:/@]+:[^\s@/]+@",
)]
# R7: the DB and Redis are reached only through mounted secrets.
DB_HOST_KEYS = {"PGHOST", "PGHOSTADDR", "REDIS_HOST"}
# R17
PROJECT_EQUAL_KEYS = ("PRICE_CACHE_FIRESTORE_PROJECT", "PIVOTA_AGENT_CENTER_GEMINI_VERTEX_PROJECT")
AURORA_OFF_KEYS = ("AURORA_ROUTES_FAIL_CLOSED", "AURORA_BFF_FAIL_CLOSED")
# R16: model-provider credentials have no place in the sandbox (no LLM calls), under any mount
# name, even when the secret behind it is an allowed one (review: OPENAI_API_KEY=ADMIN_API_KEY passed).
#
# The gateway's own boot check refuses these by name (PIVOTA-Agent src/config/sandbox.js
# LLM_KEY_SETTING_RE and VERTEX_PATH_SETTING_RE). Their sources are pinned here VERBATIM, so a
# setting the gateway would refuse at boot fails here first; tests/test_sandbox_infra.py keeps the
# provider list equal to the pinned source, and compares the pin with the gateway's origin/main
# when GATEWAY_REPO_DIR is set.
GATEWAY_LLM_KEY_SETTING_RE_SOURCE = (
    r"^(?:[A-Z0-9_]*_)?(?:GEMINI|OPENAI|AZURE_OPENAI|ANTHROPIC|CLAUDE|LLM|GENAI|GOOGLE_GENERATIVE_AI|GENERATIVE_AI|"
    r"VERTEX|OPENROUTER|DASHSCOPE|QWEN|DEEPSEEK|MOONSHOT|KIMI|ZHIPU|MINIMAX|MISTRAL|COHERE|GROQ|PERPLEXITY|XAI|"
    r"TOGETHER|FIREWORKS|REPLICATE|HUGGINGFACE|BEDROCK|VOYAGE|JINA)(?:_[A-Z0-9]+)*_?(?:(?:API_?)?KEYS?|TOKEN|SECRET|"
    r"ACCESS_KEY)(?:_\d+)?$|^GOOGLE_API_?KEYS?(?:_\d+)?$")
GATEWAY_VERTEX_PATH_SETTING_RE_SOURCE = r"(^|_)VERTEX_(PROJECT|REGION|LOCATION)$"
GATEWAY_LLM_KEY_SETTING_RE = re.compile(GATEWAY_LLM_KEY_SETTING_RE_SOURCE)
GATEWAY_VERTEX_PATH_SETTING_RE = re.compile(GATEWAY_VERTEX_PATH_SETTING_RE_SOURCE)
# The gateway's providers, read out of the pinned source (one list, not two to keep in step) ...
GATEWAY_LLM_PROVIDERS = tuple(re.search(r"\(\?:(GEMINI\|[A-Z_|]+)\)", GATEWAY_LLM_KEY_SETTING_RE_SOURCE).group(1).split("|"))
# ... plus the sandbox's own extras, which the gateway does not list: Google's other API-key names, a
# short Hugging Face prefix, the search API the backend calls, and the look-replicator's key.
SANDBOX_EXTRA_LLM_PROVIDERS = ("GOOGLE_API", "GOOGLE_AI", "HF", "SERPAPI", "LOOK_REPLICATOR")
# Matched as a whole `_`-separated part ANYWHERE in a MOUNT name, so it is wider than the gateway's
# (PIVOTA_GEMINI_API_KEY, AURORA_SKIN_GEMINI_API_KEY, and any name the gateway's shape would miss).
LLM_KEY_NAME_RE = re.compile(r"(^|_)(" + "|".join(GATEWAY_LLM_PROVIDERS + SANDBOX_EXTRA_LLM_PROVIDERS) + r")(_|$)")


def is_llm_key_name(name: str) -> bool:
    """R16 for a mount name: the wider part match OR the gateway's own rule, so every name the gateway
    refuses at boot is refused here too, by construction (GOOGLE_APIKEYS has no `_` after GOOGLE_API)."""
    return bool(LLM_KEY_NAME_RE.search(name) or GATEWAY_LLM_KEY_SETTING_RE.search(name))
# R6 escape hatch, narrow and literal: values of these keys may be file or model names
# (`face.parsing.onnx`, `weights.v2.json`) that the bare-host scan would read as hosts. The hatch
# exempts ONLY a bare-host candidate whose last label is a file extension below, and only when the
# value has no `@` and no `//`. Everything else is always checked: URL hosts, any other bare host
# (X_VERSION=api.reap.global is a host), IP literals (R15 never skips: X_FILE=<IP> is an IP), the
# credential shapes and the prod/staging substrings (review). NOT `_PATH`: a path is joined onto a
# base URL by the code that reads it (REAP_ISSUE_CARD_PATH=@api.reap.global/... would reach prod Reap).
NOT_A_HOST_KEY_RE = re.compile(r"(_FILE|_FILENAME|_MODEL|_MODEL_NAME|_VERSION)$")
HATCH_FILE_EXTENSIONS = frozenset((
    "json", "onnx", "bin", "pth", "safetensors", "gguf", "ckpt", "tflite", "pb", "h5", "pkl", "npz", "npy",
    "txt", "csv", "yaml", "yml", "xml", "gz", "tar", "parquet", "model", "vocab", "spm"))  # none is a TLD (.zip is: left out)


# A *_PATH value is joined onto a base URL by the code that reads it, so it must be a plain absolute
# path: "/" first, then path characters only; no "@", no "//", no ":" (review: a leading-dot or
# bare-host value turns https://<base>{path} into a request to another host).
PATH_KEY_RE = re.compile(r"_PATH$")
# `{name}` placeholders are allowed (the real default REAP_REVOKE_CARD_PATH=/v1/cards/{card_id}/cancel);
# a brace anywhere else is not.
PATH_VALUE_RE = re.compile(r"^/(?!/)(?:[A-Za-z0-9._~%/+-]|\{[a-z_][a-z0-9_]*\})*$")


def _hatch(key: str, value: str) -> bool:
    return bool(NOT_A_HOST_KEY_RE.search(key)) and "@" not in value and "//" not in value


def sandbox_gateway_agent_id() -> str:
    m = re.search(r"^SANDBOX_GATEWAY_AGENT_ID=(agent_[0-9a-f]{16})$", (HERE / "config.sh").read_text(), re.M)
    return m.group(1) if m else ""


def parse_profile(path: Path, problems: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for n, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = LINE_RE.match(line)
        if not m:
            problems.append(f"R10 {path.name}:{n}: not a KEY: \"value\" line")
            continue
        k, v = m.group(1), m.group(2)
        if k in out:
            problems.append(f"R10 {path.name}:{n}: duplicate key {k} (gcloud would keep the FIRST one)")
        out[k] = v
    return out


# R8: secrets only the gateway (and the one-off ops jobs) may hold. PIVOTA_API_KEY is the gateway's
# own service key; DATABASE_URL_NOVERIFY is the node-pg form of the DB URL.
SECRET_ONLY_IN = {"PIVOTA_API_KEY": {"gateway", "ops"}, "DATABASE_URL_NOVERIFY": {"gateway", "ops"},
                  # the schema-apply role's DSN: only its own job and the role job (RUNBOOK 10a-sandbox)
                  "SCHEMA_APPLY_DATABASE_URL": {"schema-apply", "ops"},
                  # its SCRAM verifier: only the role job
                  "SCHEMA_APPLY_ROLE_VERIFIER": {"ops"},
                  # S9: the signer (gateway) and the verifier (web) only; worker and agenpaz neither
                  # sign nor verify it, so they do not hold it
                  "ISSUING_AGENT_ASSERTION_SECRET": {"web", "gateway"}}
# R8, the other direction: a secret that only works as a PAIR must be mounted by every member, AND
# under the one env name the code reads, or the feature silently degrades (S9: with the key on one
# side only, or under another name, or with that name fed by another secret, every assertion fails
# to verify and partners are never credited, with no error anywhere; review).
#   secret -> (the env name it must land in, the services that must mount it)
# Everywhere: the secret is mounted ONLY under that env name, and that env name is fed ONLY by it.
SECRET_REQUIRED_IN = {"ISSUING_AGENT_ASSERTION_SECRET": ("ISSUING_AGENT_ASSERTION_SECRET", {"web", "gateway"})}


def parse_secrets(path: Path, problems: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for n, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*=[A-Za-z][A-Za-z0-9_-]*", line):
            problems.append(f"R8 {path.name}:{n}: not ENV_NAME=SECRET_NAME")
            continue
        env, sec = line.split("=", 1)
        if env in out:
            problems.append(f"R8 {path.name}:{n}: {env} mounted twice")
        if sec not in ALLOWED_SECRETS:
            problems.append(f"R8 {path.name}:{n}: secret {sec} is not in the sandbox inventory")
        if sec in SECRET_ONLY_IN and path.stem not in SECRET_ONLY_IN[sec]:
            problems.append(f"R8 {path.name}:{n}: secret {sec} may only be mounted by {sorted(SECRET_ONLY_IN[sec])}")
        out[env] = sec
    return out


def _hosts_in(key: str, value: str) -> list[str]:
    """Every host a value names: URL hosts, bare hosts, `//host`, email domains (review S7)."""
    hosts: list[str] = []
    for part in value.split(","):
        part = part.strip()
        if "://" in part:
            hosts.append((urlsplit(part).hostname or "").lower())
    bare = DOMAIN_RE.findall(value.lower())
    if _hatch(key, value):
        bare = [h for h in bare if h.rsplit(".", 1)[-1] not in HATCH_FILE_EXTENSIONS]
    hosts += bare
    return list(dict.fromkeys(h for h in hosts if h))


def check_service(svc: str, env: dict[str, str], problems: list[str], *, rendered: bool,
                  allowed_hosts: set[str], sandbox_project: str = "", agenpaz_host: str = "") -> None:
    p = f"{svc}.yaml"
    if env.get("PIVOTA_ENV") != "sandbox":
        problems.append(f"R1 {p}: PIVOTA_ENV must be 'sandbox' (got {env.get('PIVOTA_ENV')!r})")
    gcp = env.get("GOOGLE_CLOUD_PROJECT")
    if not rendered and gcp != "@@SANDBOX_PROJECT@@":
        problems.append(f"R1 {p}: GOOGLE_CLOUD_PROJECT must be the token @@SANDBOX_PROJECT@@ (got {gcp!r})")
    if rendered and (gcp != sandbox_project or not valid_sandbox_project(gcp or "")):
        problems.append(f"R1 {p}: GOOGLE_CLOUD_PROJECT must be the sandbox project {sandbox_project!r} (got {gcp!r})")
    if svc in ("web", "worker"):
        want_id = sandbox_gateway_agent_id()
        for k in GATEWAY_AGENT_KEYS:
            if not want_id or env.get(k) != want_id:
                problems.append(f"R13 {p}: {k} must be exactly the sandbox gateway agent {want_id or '(config.sh has none)'}")
    if svc in ("gateway", "agenpaz"):
        for k in NODE_ENV_KEYS:
            if env.get(k, "").strip().lower() in ("production", "test"):
                problems.append(f"R12 {p}: {k}={env[k]!r} is refused by the gateway boot check")
    gcp_value = env.get("GOOGLE_CLOUD_PROJECT")
    for k in PROJECT_EQUAL_KEYS:
        if k in env and env[k] != gcp_value:
            problems.append(f"R17 {p}: {k} must equal GOOGLE_CLOUD_PROJECT (got {env[k]!r})")
    if svc in ("gateway", "agenpaz"):
        for k in AURORA_OFF_KEYS:
            if k in env and env[k].strip().lower() not in OFF:
                problems.append(f"R17 {p}: {k} must be unset or off (the Aurora BFF is not loaded in the sandbox)")
    for k in env:
        if k.startswith("A2A_"):
            problems.append(f"R2 {p}: {k} - no A2A_* setting may exist in the sandbox")
    if svc in ("gateway", "agenpaz"):
        for k in env:
            if GATEWAY_LLM_KEY_SETTING_RE.search(k):
                problems.append(f"R16 {p}: {k} - the gateway's boot check refuses an LLM key setting in the sandbox")
            elif GATEWAY_VERTEX_PATH_SETTING_RE.search(k):
                problems.append(f"R16 {p}: {k} - the gateway's boot check refuses a Vertex path setting in the sandbox")
    if svc == "agenpaz":
        base = env.get("AGENPAS_PUBLIC_BASE_URL", "")
        host = (urlsplit(base).hostname or "") if base.startswith("https://") else ""
        # Rendered: exactly the AgenPaz host's origin. Another allowed host (the gateway's) would make
        # THAT host exclusive and leave the AgenPaz host serving every gateway route.
        ok = (base == "@@AGENPAZ_PUBLIC_URL@@") if not rendered else (
            bool(agenpaz_host) and host == agenpaz_host.lower() and host in allowed_hosts
            and base.rstrip("/") == f"https://{host}")
        if not ok:
            problems.append(f"R4 {p}: AGENPAS_PUBLIC_BASE_URL must be the AgenPaz host's https origin (it makes that host "
                            f"exclusive; without it agenpaz serves every gateway route), got {base!r}")
    for k, want in MUST_EQUAL.get(svc, {}).items():
        if env.get(k) != want:
            tag = "R3" if k.startswith("REAP_") else "R4" if k.startswith("AGENPAS_") else "R5"
            problems.append(f"{tag} {p}: {k} must be {want!r} (got {env.get(k)!r})")
    for k in MUST_BE_OFF.get(svc, []):
        if k not in env:
            problems.append(f"R5 {p}: {k} must be set explicitly to off")
        elif env[k].strip().lower() not in OFF:
            problems.append(f"R5 {p}: {k} must be off (got {env[k]!r})")
    for k, v in env.items():
        if SECRET_SHAPE.search(k) and k not in NOT_SECRET:
            problems.append(f"R7 {p}: {k} looks like a secret; mount it from secrets/{svc}.list instead")
        if k in DB_HOST_KEYS:
            problems.append(f"R7 {p}: {k} may not be set; the DB/Redis come only from mounted secrets")
        for rx in LIVE_SECRET_PATTERNS:
            if rx.search(v):
                problems.append(f"R14 {p}: {k} holds a credential-shaped value ({rx.pattern[:24]}...)")
                break
        for sub in REFUSED_SUBSTRINGS:
            if sub in v.lower():
                problems.append(f"R6 {p}: {k} names {sub!r}")
        if PATH_KEY_RE.search(k) and v and (not PATH_VALUE_RE.match(v) or "//" in v):
            problems.append(f"R6 {p}: {k} must be a plain absolute path ('/' first, no '@', '//' or ':'), not {v!r}")
        for ip in IPV4_RE.findall(v):   # never skipped, not even under the R6 hatch (review)
            try:
                addr = ipaddress.ip_address(ip)
            except ValueError:
                problems.append(f"R15 {p}: {k} holds a malformed IPv4 literal {ip!r}")
                continue
            problems.append(f"R15 {p}: {k} names IP {addr}; a profile holds no IP address (the DB and Redis come from secrets)")
        if rendered and TOKEN_RE.search(v):
            problems.append(f"R11 {p}: {k} still holds {TOKEN_RE.search(v).group(0)}")
        if not rendered:
            for t in TOKEN_RE.findall(v):
                if t not in TOKENS:
                    problems.append(f"R11 {p}: {k} uses unknown token @@{t}@@")
            v = TOKEN_RE.sub("token.invalid", v)
        for h in _hosts_in(k, v):
            if h == "token.invalid" and not rendered:
                continue
            if any(rx.search(h) for rx in DENIED_HOST_PATTERNS):
                problems.append(f"R6 {p}: {k} names a prod/staging/real-money host {h!r}")
            elif h.endswith(".run.app") and h not in allowed_hosts:
                problems.append(f"R6 {p}: {k} names a run.app host {h!r} that is not the sandbox's own web")
            elif h not in allowed_hosts:
                problems.append(f"R6 {p}: {k} names host {h!r}, not on the sandbox allowlist")
            elif (any(h == z or h.endswith("." + z) for z in BRAND_ZONES)
                  and not any("sandbox" in label for label in h.split("."))
                  and not (svc in ("web", "worker") and (k, v) in BRAND_PAIR_EXCEPTIONS)
                  and not (k == LEGAL_HOSTS_KEY and h in LEGAL_HOSTS_ALLOWED)):
                problems.append(f"R6 {p}: {k} names {h!r}; both boot checks refuse a brand host without a sandbox label")


def check_all(env_dir: Path, secrets_dir: Path, *, rendered: bool = False,
              extra_hosts: set[str] | None = None, sandbox_project: str = "", agenpaz_host: str = "") -> list[str]:
    problems: list[str] = []
    allowed = set(ALLOWED_HOSTS) | set(extra_hosts or ())
    profiles: dict[str, dict[str, str]] = {}
    for svc in SERVICES:
        path = env_dir / f"{svc}.yaml"
        if not path.exists():
            problems.append(f"R10 missing {path}")
            continue
        profiles[svc] = parse_profile(path, problems)
        check_service(svc, profiles[svc], problems, rendered=rendered, allowed_hosts=allowed,
                      sandbox_project=sandbox_project, agenpaz_host=agenpaz_host)
    if "web" in profiles and "worker" in profiles:
        web, wk = profiles["web"], profiles["worker"]
        for k in sorted(set(web) | set(wk)):
            if k in WORKER_ONLY_DIFFERENCES:
                continue
            if web.get(k) != wk.get(k):
                problems.append(f"R9 worker.yaml: {k} differs from web.yaml ({wk.get(k)!r} vs {web.get(k)!r})")
    pinned = {"PIVOTA_ENV", "GOOGLE_CLOUD_PROJECT", *NODE_ENV_KEYS, *GATEWAY_AGENT_KEYS}
    for svc_rules in (MUST_EQUAL, MUST_BE_OFF):
        for keys in svc_rules.values():
            pinned |= set(keys)
    all_mounts = {lst.stem: parse_secrets(lst, []) for lst in sorted(secrets_dir.glob("*.list"))}
    for sec, (env_pin, need) in SECRET_REQUIRED_IN.items():
        for svc in sorted(need):
            if all_mounts.get(svc, {}).get(env_pin) != sec:
                problems.append(f"R8 {svc}.list: must hold exactly {env_pin}={sec} (it works only on {sorted(need)} together)")
        for svc, mounts in all_mounts.items():
            for env_name, mounted in mounts.items():
                if mounted == sec and env_name != env_pin:
                    problems.append(f"R8 {svc}.list: {sec} may only be mounted as {env_pin}, not as {env_name}")
                if env_name == env_pin and mounted != sec:
                    problems.append(f"R8 {svc}.list: {env_pin} may only be fed by the secret {sec}, not by {mounted}")
    for lst in sorted(secrets_dir.glob("*.list")):
        mounts = parse_secrets(lst, problems)
        profile = profiles.get(lst.stem, {})
        for env_name in mounts:
            why = None
            if env_name.startswith("A2A_"):
                why = "no A2A_* setting may exist"
            elif is_llm_key_name(env_name):
                why = "a model-provider credential name (the sandbox makes no LLM calls)"
            elif env_name in pinned:
                why = "a secret may not set a key the rules pin"
            elif env_name in profile:
                why = f"it would shadow {lst.stem}.yaml's own {env_name}"
            elif not SECRET_SHAPE.search(env_name):
                why = "a mounted name must be secret-shaped (a flag belongs in the profile)"
            if why:
                problems.append(f"R16 {lst.name}: {env_name}: {why}")
    return problems


def render(env_dir: Path, out_dir: Path, values: dict[str, str]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for svc in SERVICES:
        text = (env_dir / f"{svc}.yaml").read_text()
        text = TOKEN_RE.sub(lambda m: values.get(m.group(1), m.group(0)), text)
        (out_dir / f"{svc}.yaml").write_text(text)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-dir", type=Path, default=HERE / "env")
    ap.add_argument("--secrets-dir", type=Path, default=HERE / "secrets")
    ap.add_argument("--render", type=Path, metavar="OUTDIR")
    ap.add_argument("--web-url")
    ap.add_argument("--gateway-host")
    ap.add_argument("--agenpaz-host")
    ap.add_argument("--sandbox-project")
    a = ap.parse_args(argv)

    problems = check_all(a.env_dir, a.secrets_dir)
    if a.render:
        if not (a.web_url and a.gateway_host and a.agenpaz_host and a.sandbox_project):
            print("--render needs --web-url, --gateway-host, --agenpaz-host and --sandbox-project", file=sys.stderr)
            return 2
        if not valid_sandbox_project(a.sandbox_project):
            print(f"--sandbox-project {a.sandbox_project!r} is not a valid sandbox project id", file=sys.stderr)
            return 2
        web_host = (urlsplit(a.web_url).hostname or "").lower()
        if not re.fullmatch(r"web-[0-9]+\.us-west1\.run\.app", web_host) or not a.web_url.startswith("https://"):
            print(f"--web-url must be the sandbox web's https run.app URL (got {a.web_url!r})", file=sys.stderr)
            return 2
        values = {"WEB_URL": a.web_url.rstrip("/"), "GATEWAY_HOST": a.gateway_host,
                  "AGENPAZ_HOST": a.agenpaz_host, "AGENPAZ_PUBLIC_URL": f"https://{a.agenpaz_host}",
                  "SANDBOX_PROJECT": a.sandbox_project}
        render(a.env_dir, a.render, values)
        problems += check_all(a.render, a.secrets_dir, rendered=True,
                              extra_hosts={web_host, a.gateway_host.lower(), a.agenpaz_host.lower()},
                              sandbox_project=a.sandbox_project, agenpaz_host=a.agenpaz_host)
    for p in problems:
        print(p)
    print("note: allowed as exact pairs by sandbox-backend bd2663741; confirm it is in the release: "
          + ", ".join(sorted(k for k, _ in BRAND_PAIR_EXCEPTIONS)), file=sys.stderr)
    if problems:
        print(f"FAIL: {len(problems)} problem(s)", file=sys.stderr)
        return 1
    print("OK: sandbox profiles and secret lists pass R1-R17" + (" (rendered)" if a.render else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
