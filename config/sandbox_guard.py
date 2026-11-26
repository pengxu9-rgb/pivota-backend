"""The partner sandbox's startup check: a PIVOTA_ENV=sandbox process refuses to serve unless its
settings prove it cannot move real money, fetch a store's site, or reach prod.

Design: claude.ai artifact N72GzumjqaW8h7SCE96jt7, build step 4 ("a startup check refuses to serve
if any prod host or credential is set"). Called from ``main.app_lifespan`` (web AND worker run that
same app) and from the sandbox catalog import job, right after ``require_platform_env()``.

WHEN IT RUNS. Only when ``config.platform.platform_env()`` resolves to ``sandbox``. For every other
environment ``enforce_sandbox_guard`` returns before it reads a single setting, so production,
staging, development and test boot exactly as before (tests/test_sandbox_guard.py pins that with
the same hostile settings that make a sandbox refuse). There is no switch to turn it off: a check
that one env value can disable is one console edit away from not existing.

WHAT IT REFUSES (each refusal names the SETTING and the RULE, never the value: values here include
DATABASE_URL and API keys):

  reap_base_url      REAP_API_BASE_URL's host is exactly one of Reap's sandbox hosts
                     (services.reap_agentic_client.REAP_SANDBOX_HOSTS, the one list the simulate
                     header and the poller already use).
  simulate_checkout  REAP_AGENTIC_SIMULATE_CHECKOUT is exactly COMPLETED.
  a2a_rail           A2A_RAIL_ENABLED and A2A_RAIL_CREATE_ENABLED are unset or an explicit off
                     value; every other A2A_RAIL_* is unset or empty. The A2A rail is real money.
  money_dial         the other real-money dials (live ACP capture, legacy settlement, the staging
                     settlement-transfer override) are unset or off.
  partner_surface    the internal surfaces that open outside production are shut: demo-fixture
                     logins (ENABLE_INTERNAL_DEMO_FIXTURES, known passwords) and the shakeout debug
                     endpoints (SHAKEOUT_DEBUG_TOKEN). The sandbox is reachable by outside partners.
  prod_reference     no setting's VALUE names a prod or staging project (pivota-prod*,
                     pivota-staging*), a prod/staging database or Redis private IP, a Railway host
                     (the old prod platform), a live payment or agent key prefix, or a host under
                     one of Pivota's / AgenPaz's / Reap's domains that is not a sandbox host (one of
                     its labels contains "sandbox"). Staging counts as prod here: it runs a
                     restored copy of prod's database, i.e. real people's data.
  store_fetch        every known crawl / refresh / live-read / store-probe flag is off, including
                     the ones that default ON when unset (those must be set to an explicit off
                     value); and any OTHER set variable whose name has the shape of such a flag
                     (CRAWL / REFRESH / LIVE_READ / LIVENESS / PROBE / SCRAPE / DISCOVERY ...
                     ending _ENABLED / _ARMED / _APPLY) is off too, so a new flag of that shape is
                     caught without editing this file.
  scheduler          if this process is a worker (AUDIT_WORKER_ENABLED true), SCHEDULER_JOB_ALLOWLIST
                     is set, is not "*", and names only jobs in SANDBOX_SCHEDULER_JOBS. Most crawls
                     and refreshes are scheduled jobs; this pins the sandbox worker to the purchase
                     poller whatever flags say.

POSITIVE CHECKS (S1: what a setting MUST be, not only what it must not be):

  identity           GOOGLE_CLOUD_PROJECT is exactly the sandbox's project, DATABASE_URL is set,
                     and every DSN-shaped setting (any name containing DATABASE_URL) connects only
                     to the sandbox's own database (config/sandbox_identity.dsn_refusal: asyncpg's
                     own parser, the sandbox range or socket). No Redis setting at all (v0 has none).
  url_allowlist      every scheme://host in ANY value names an allowed host: Reap's sandbox hosts or
                     the sandbox's two public hosts for http(s), the drop bucket for gs://, nothing
                     for any other scheme (DSN settings excepted, checked above). A host/URL-named
                     setting (*_URL, *_HOST, *_ORIGINS, ...) may name no other bare host either, and no
                     setting outside the DSNs may hold an IP address. The two consent links stay
                     exempt as exact pairs.
                     The URL rule is strict: no userinfo or backslash in an authority, no escaped
                     or percent-encoded URL, no base64 value that decodes to a URL, no //host, no
                     outbound proxy; every scheme-less host in any value (an email's domain too)
                     and every item of a *_HOST(S)/_DOMAIN(S)/_ORIGIN(S) setting must be allowed;
                     a *_URL setting holds URLs only; no dotted, IPv6 or hex address (a bare
                     integer cannot be told from a count, so it is not treated as one).
  prod_default       the settings whose CODE default is a prod gateway URL the server calls must be
                     SET (empty, or an allowed URL): unset means "call prod". The public base URL
                     the backend hands out (resolve_public_api_base_url, fallbacks included) must
                     resolve to https on the sandbox gateway host (SF2). Code that falls back to a
                     prod host even when its setting is empty is refused in the sandbox at the
                     call site instead (tests/test_sandbox_prod_default_hosts.py lists them all).
  flag_allowlist     a feature flag (*_ENABLED/_ENABLE/_ARMED/_APPLY/_LIVE) SET to anything but an off
                     value must be in _FLAGS_ALLOWED_ON (the flags the sandbox profile turns on);
                     flags that default ON when unset are listed and must be set off.
  ai_search          no LLM or search API credential, model dials off, the keyless FX lookup off
                     (SF5: nothing in sandbox v0 calls a paid or third-party model/search API).
  money_default_on   the refund dials that default ON when unset are set to an explicit off value;
                     every other FF_ENABLE_*REFUND*, REAP_EXTERNAL_AUTH_ENABLED and
                     VISA_VDP_ALLOW_PRODUCTION are off.
  live_read          the live-offer and checkout-preflight egress dials, and any *EGRESS* name, are
                     off; CHECKOUT_PREFLIGHT_MODE is unset or off; VISA_VDP_ENV is unset or sandbox;
                     ENVIRONMENT and APP_ENV (read directly by several routes and by Sentry) are unset
                     or sandbox.
  provider           email/SMS/payment-provider settings (SendGrid, SMTP2GO, Twilio, Adyen, Stripe,
                     Visa, AWS keys, ...) hold nothing but an off value, a number, or a test-shaped
                     value for that setting (sk_test_/pk_test_ for Stripe); store-platform
                     credentials (Shopify, Cafe24, Wix, ... *_TOKEN/*_SECRET/*_KEY) are unset.

WHAT IT DOES NOT CLAIM. The flag lists are the flags this codebase has today; a store fetch behind a
flag of a different shape, or behind no flag at all, is not caught here. The second lock is the
data: the catalog copy (jobs/sandbox_catalog_copy.py) carries no store credential (no Shopify /
Cafe24 / WooCommerce token, no webhook secret), so a store API call from the sandbox has nothing to
authenticate with. Unauthenticated public page fetches are what the flags above and the scheduler
allowlist exist to stop.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from dataclasses import dataclass
from typing import List, Mapping, Optional, Tuple

__all__ = [
    "SandboxGuardRefused",
    "Violation",
    "SANDBOX_SCHEDULER_JOBS",
    "sandbox_violations",
    "enforce_sandbox_guard",
]

#: The only values accepted as "off" for a dial. A whitelist, not "anything not truthy": a typo
#: ("ture", "flase") is refused rather than read either way.
_OFF_VALUES = frozenset({"", "0", "false", "off", "no"})
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})

# --- money ---------------------------------------------------------------------------------------
_A2A_PREFIX = "A2A_RAIL_"
#: May be present only with an explicit off value. Every other A2A_RAIL_* must be absent or empty.
_A2A_DIALS = ("A2A_RAIL_ENABLED", "A2A_RAIL_CREATE_ENABLED")
#: Other real-money dials. Unset or off.
_MONEY_DIALS = (
    "AGENT_ACP_ALLOW_LIVE_CAPTURE",
    "LEGACY_SETTLEMENT_LIVE",
    "SETTLEMENT_TRANSFER_ALLOWED_ON_STAGING",
)

# --- internal surfaces that open outside production (partner-facing hazards) -------------------
#: Must be unset or off: settings.enable_internal_demo_fixtures turns on demo employee logins and
#: demo merchant ids whenever is_production() is False (routes/auth.py, routes/auth_routes.py).
_PARTNER_SURFACE_DIALS = ("ENABLE_INTERNAL_DEMO_FIXTURES",)
#: Must be unset or empty: a configured token opens /__shakeout/* (routes/shakeout_debug.py).
_PARTNER_SURFACE_SECRETS = ("SHAKEOUT_DEBUG_TOKEN",)

# --- store fetches -------------------------------------------------------------------------------
#: Flags that DEFAULT ON when unset. The sandbox must set each to an explicit off value.
_STORE_FETCH_DEFAULT_ON = (
    # routes/order_routes.py: re-quotes against the merchant at order create; default "1".
    "ORDER_CREATE_LIVE_QUOTE_REVALIDATION_ENABLED",
    # services/store_lifecycle_service.py: _env_flag(..., True). It sat in the default-off list
    # below, so leaving it unset meant ON (found while auditing default-on flags for SF5).
    "STORE_LIFECYCLE_RECONCILE_ENABLED",
)
#: Flags that default OFF. Unset or an explicit off value.
_STORE_FETCH_FLAGS = (
    "CATALOG_TRUST_CRAWL_FRESH_ENABLED",
    "EXTERNAL_REFERRAL_REFRESH_CATALOGUE_ENABLED",
    "REVIEWED_RETAILER_REFRESH_WRITE_ENABLED",
    "OFFICIAL_DOMAIN_LIVENESS_ENABLED",
    "MERCHANT_PURCHASABILITY_SWEEP_ENABLED",
    "MERCHANT_PROTOCOL_PROFILE_SWEEP_ENABLED",
    "MERCHANT_UCP_ENDPOINT_DISCOVERY_ENABLED",
    "STORE_AUDIT_COMMERCE_PROBE_RECEIPT_ENABLED",
    "STORE_AUDIT_COMMERCE_REPROBE_ARMED",
    "STORE_AUDIT_COMMERCE_REPROBE_SCHEDULER_ENABLED",
    "STORE_AUDIT_UCP_PROBE_CHECKOUT_TIER_ENABLED",
    "STORE_AUDIT_UCP_PROBE_RECEIPT_ENABLED",
    "STORE_AUDIT_UCP_REPROBE_SCHEDULER_ENABLED",
    "CATALOG_SYNC_DRAIN_ENABLED",
    "SHOPIFY_DRAFT_ORDER_QUOTE_SYNC_ENABLED",
    "EMPLOYEE_SHOPIFY_ORDER_SYNC_ENABLED",
    "REAP_AGENTIC_CART_LINK_ENRICHMENT_ENABLED",
    "OUTBOUND_WARM_HANDOFF_ENABLED",
    "CAFE24_RECONCILIATION_ENABLED",
    "MERCHANT_ORDER_CREATE_RECONCILE_ENABLED",
    "CHECKOUT_CANCEL_UPSTREAM_ENABLED",
    "SERVE_PDP_ENRICHMENT_ON_WRITE",
)
#: The catch-all: a set variable whose NAME has a store-fetch keyword AND an enabling suffix.
#: Names ending _DISABLED are inverse polarity and deliberately do not match.
_STORE_FETCH_NAME = re.compile(
    r"(CRAWL|REFRESH|LIVE_READ|LIVENESS|LIVE_VERIF|LIVE_QUOTE|REPROBE|PROBE|SCRAPE|DISCOVERY|HARVEST)"
    r"[A-Z0-9_]*(_ENABLED|_ENABLE|_ARMED|_APPLY)$"
)

# --- scheduler -----------------------------------------------------------------------------------
#: The scheduled jobs a sandbox worker may run: the Reap purchase poller (drives a simulated
#: checkout to `completed`) and the purchase-event detector (webhooks to the partner's own agent).
SANDBOX_SCHEDULER_JOBS = frozenset({"reap_agentic_purchase_poll", "agent_purchase_events"})

# --- prod references -----------------------------------------------------------------------------
#: GCP projects. Matched as a token, so pivota-prod-migration and pivota-prod:us-west1:pivota-pg
#: match and pivota-sandbox does not.
_PROD_PROJECT_TOKENS = ("pivota-prod", "pivota-staging")
_PROD_PROJECT_RE = re.compile(
    r"(?<![a-z0-9])(" + "|".join(re.escape(t) for t in _PROD_PROJECT_TOKENS) + r")(?![a-z0-9])"
)
#: Private IPs of prod's Cloud SQL and Redis and staging's Cloud SQL, as sha256 of the normalized
#: address (str(ipaddress.ip_address(...))): the repo is public, so the addresses are not written here.
_PROD_PRIVATE_IP_SHA256 = frozenset({
    "4bffbd8da0d2a6414d0b853c39a53edc9b4afab5f48bc34402ec123de51b9549",  # prod Cloud SQL
    "7d34656101ab6b67ef3f9662be01e73cd2b048037baa6ac1f7e6fb5f67212c2e",  # prod Redis
    "f60ccb8adcd36703f02206ceb1b62ef031c07c21950aa06c789a735bd122606b",  # staging Cloud SQL
})
#: Every dotted-quad token in a value; each is normalized and hashed before the compare.
_IPV4_TOKEN_RE = re.compile(r"(?<![0-9.])([0-9]{1,3}(?:\.[0-9]{1,3}){3})(?![0-9])")
#: Domains whose hosts must be sandbox hosts (some label contains "sandbox").
_GUARDED_ZONES = ("pivota.cc", "agenpaz.com", "agenpas.com", "reap.global")
#: Railway was prod's platform; the sandbox never runs there or talks to it.
_RAILWAY_ZONES = ("railway.app", "railway.internal", "rlwy.net")
#: Live credentials by prefix: Stripe live secret / restricted keys, Pivota live agent keys.
_LIVE_KEY_PREFIXES = ("sk_live_", "rk_live_", "ak_live_")
_LIVE_KEY_RE = re.compile(
    r"(?<![a-z0-9])(" + "|".join(re.escape(p) for p in _LIVE_KEY_PREFIXES) + r")"
)
#: (setting, exact value) pairs exempt from the prod-reference rule: the buyer consent links. The
#: backend only validates, stores and SHOWS them (services/payment_orchestration/purchase_consent.py
#: config()); nothing fetches them, and the sandbox buyer agrees to the real legal text, which a
#: sandbox copy could drift from. Exact match only: another path, a query string, or the same URL
#: in any other setting is still refused.
_DISPLAY_ONLY_LINKS = {
    "AGENPAS_TERMS_URL": "https://pivota.cc/terms",
    "AGENPAS_PRIVACY_URL": "https://merchant.pivota.cc/privacy",
}
_HOST_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+")

# --- positive checks (S1): what a sandbox setting MUST be, not only what it must not be ----------
#: Identity: the project every sandbox process names (the deploy reads it back off each revision).
_IDENTITY_PROJECT_SETTING = "GOOGLE_CLOUD_PROJECT"
#: A setting is a Postgres DSN when its name contains this; every one must connect only to the
#: sandbox's own database (config/sandbox_identity.dsn_refusal). DATABASE_URL itself is required.
_DSN_NAME_RE = re.compile(r"DATABASE_URL|_DSN$")
#: No Redis in sandbox v0 (the assertion replay guard falls back in-process). A sandbox Redis is a
#: new range to add here first, never a URL that happens not to be prod's.
_REDIS_NAME_RE = re.compile(r"REDIS")
#: The ONLY hosts an http(s) URL in a sandbox setting may name: Reap's sandbox hosts and the
#: sandbox's own two public hosts (infra/gcp/sandbox/config.sh GATEWAY_HOST / AGENPAZ_HOST). Exact
#: strings, not "a label contains sandbox": an allowlist, so a host nobody listed is refused.
_SANDBOX_PUBLIC_HOSTS = frozenset({"sandbox.mcp.pivota.cc", "sandbox.agenpaz.com"})
#: The ONLY bucket a gs:// URL may name (config/sandbox_identity.SANDBOX_DROP_BUCKET).
_URL_RE = re.compile(r"([a-z][a-z0-9+.-]*)://([^/\s?#,;'\"]*)")
#: A scheme-relative URL (//host/...), which clients resolve against https.
_SCHEMELESS_URL_RE = re.compile(r"(?:^|[\s,;=\"'(])//([^/\s?#,;'\"]+)")
#: The only authority shape accepted: a host or bracketed IPv6, an optional port. No userinfo, no
#: backslash: `http://evil.example\@sandbox.mcp.pivota.cc` reads as the sandbox host to a naive split
#: but resolves to evil.example in requests (review nit).
_AUTHORITY_RE = re.compile(r"(?:[a-z0-9-]+(?:\.[a-z0-9-]+)*\.?|\[[0-9a-f:.]+\])(?::[0-9]{1,5})?\Z")
#: A URL hidden by escaping or percent-encoding (`https:\/\/`, `https%3A%2F%2F`).
_ESCAPED_URL_RE = re.compile(r":\\+/|%3a%2f%2f|%3a//|:%2f%2f")
#: A bare host anywhere in a value (no scheme): dotted labels ending in a letters-only label of 2-12
#: characters (a TLD shape); file names are not hosts.
_BARE_HOST_RE = re.compile(r"(?<![a-z0-9_.-])((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+([a-z]{2,12}))(?![a-z0-9_-])")
_FILE_SUFFIXES = frozenset({"json", "yaml", "yml", "txt", "csv", "py", "js", "ts", "html", "htm", "sql", "gz",
                            "md", "log", "xml", "pem", "crt", "png", "jpg", "jpeg", "svg", "webp", "gif", "zip"})
#: IPv6 and hex-encoded IPv4 literals (dotted IPv4 is _IPV4_TOKEN_RE). A bare decimal integer cannot
#: be told from a count, so it is not treated as an address outside a URL host.
_IPV6_TOKEN_RE = re.compile(r"(?<![0-9a-f:])([0-9a-f]{0,4}(?::[0-9a-f]{0,4}){2,7})(?![0-9a-f:])")
_HEX_IP_RE = re.compile(r"(?<![0-9a-z])0x[0-9a-f]{1,8}(?![0-9a-z])")
#: A value that is base64 and decodes to something holding a URL.
_BASE64_RE = re.compile(r"[A-Za-z0-9+/_-]{16,}={0,2}\Z")
#: Names whose value is a host or a list of hosts: EVERY item must be an allowed host.
_HOST_NAME_RE = re.compile(
    r"(_HOST|_HOSTS|_HOSTNAME|_DOMAIN|_DOMAINS|_ORIGIN|_ORIGINS|_ENDPOINT|_ENDPOINTS|_ADDR|_ADDRESS)$"
)
#: Names whose value is a URL or a list of URLs: every item must be a URL (on the allowlist).
_URL_NAME_RE = re.compile(r"(_URL|_URLS|_URI|_URIS|_BASE)$")
#: Outbound proxies: none in the sandbox (a proxy is a way around every host check here).
_PROXY_NAME_RE = re.compile(r"^(https?|all|ftp)_proxy$", re.IGNORECASE)
#: Settings whose CODE DEFAULT is a prod host the server calls (config/settings.py). Unset means
#: "call prod", so the sandbox must set each one: to empty (the feature has nowhere to go) or to an
#: allowed sandbox URL.
_PROD_DEFAULT_URL_SETTINGS = (
    "PIVOTA_AGENT_INTERNAL_URL",          # gateway.pivota.cc: search/detail proxies, LLM probe
    "OUTBOUND_WARM_HANDOFF_RESOLVE_URL",  # gateway.pivota.cc/internal/ucp/warm-handoff/resolve
)
#: The public base URL the backend writes into links it hands out and URLs it gives others to call
#: (managed-inbox webhook receivers, the MCP OAuth login redirect, OpenAPI servers):
#: config.settings.resolve_public_api_base_url falls back to prod's https://api.pivota.cc when
#: PUBLIC_API_BASE_URL (and PUBLIC_BASE_URL / APP_URL / BASE_URL) are unset or empty. In the sandbox
#: the RESOLVED URL must be https on the sandbox's public gateway host (the backend has no public
#: host of its own there).
_PUBLIC_API_BASE_HOSTS = frozenset({"sandbox.mcp.pivota.cc"})
#: Real-money dials that DEFAULT ON when unset (config/feature_flags.py): each must be SET to an
#: explicit off value. Any other FF_ENABLE_*REFUND* that is set must be off too.
_MONEY_DEFAULT_ON = ("FF_ENABLE_INTERNAL_REFUND", "FF_ENABLE_MERCHANT_ORDER_FAILURE_REFUND")
_REFUND_FLAG_RE = re.compile(r"^FF_ENABLE_[A-Z0-9_]*REFUND[A-Z0-9_]*$")
#: More real-money / live-network dials. Unset or off. MERCHANT_UCP_CHECKOUT_ENABLED drives a checkout
#: AT the merchant's own store (services/merchant_ucp_checkout.py): no catch-all name matched it.
_MORE_MONEY_DIALS = ("REAP_EXTERNAL_AUTH_ENABLED", "VISA_VDP_ALLOW_PRODUCTION", "VISA_VDP_ENABLED",
                     "MERCHANT_UCP_CHECKOUT_ENABLED", "ACP_SPT_CAPTURE_ENABLED", "A2A_SHIPPING_CAPTURE_ENABLED",
                     "PARTNER_PAYOUT_EXECUTION_ENABLED", "SETTLEMENT_FILE_TRANSFER_ENABLED")
#: Live-read dials whose names the store-fetch pattern does not catch. Unset or off.
_MORE_STORE_FETCH_FLAGS = (
    "LIVE_OFFER_VERIFICATION_ENABLED",
    "LIVE_OFFER_VERIFICATION_ALLOW_REQUEST_PATH_EGRESS",
    "CHECKOUT_PREFLIGHT_ALLOW_EGRESS",
)
#: Any set name with EGRESS in it allows outbound fetches: off (a *_DISABLED name is the safe side).
_EGRESS_NAME_RE = re.compile(r"EGRESS(?!.*_DISABLED$)")
#: Mode settings pinned to the values that are safe in the sandbox ("" = unset).
_PINNED_VALUES = {
    "CHECKOUT_PREFLIGHT_MODE": frozenset({"", "off"}),
    "VISA_VDP_ENV": frozenset({"", "sandbox"}),
    # Read directly by utils/runtime_safety.py, utils/service_token.py, routes/webhook_routes.py,
    # routes/payment_routes.py, routes/billing_routes.py, routes/shopify_setup.py, routes/photos.py,
    # routes/refund_webhook_routes.py, routes/agent_shop_gateway.py and Sentry's environment tag.
    "ENVIRONMENT": frozenset({"", "sandbox"}),
    "APP_ENV": frozenset({"", "sandbox"}),
}
#: Email, SMS and payment-provider settings: the sandbox sends no mail or SMS and has no PSP but
#: Reap's sandbox. Any non-empty value is refused unless it is an explicit off value, a number (a
#: timeout or retry count), or test-shaped for that setting (_PROVIDER_TEST_SHAPES).
_PROVIDER_NAME_RE = re.compile(
    r"^(SENDGRID|SMTP2GO|SMTP|MAILGUN|MAILJET|POSTMARK|MANDRILL|RESEND|SPARKPOST|BREVO|SES|AWS_SES"
    r"|TWILIO|VONAGE|NEXMO|PLIVO|MESSAGEBIRD|SINCH|TELNYX"
    r"|ADYEN|STRIPE|PAYPAL|BRAINTREE|KLARNA|AFTERPAY|EVONET|AIRWALLEX|RAPYD|SQUARE|WORLDPAY"
    r"|CYBERSOURCE|MOLLIE|RAZORPAY|CKO|CHECKOUT_COM|VISA|MASTERCARD|FEISHU)_"
    r"|^AWS_(ACCESS_KEY_ID|SECRET_ACCESS_KEY|SESSION_TOKEN)$"
)
_PROVIDER_TEST_SHAPES = {
    "STRIPE_SECRET_KEY": ("sk_test_",),
    "STRIPE_TEST_SECRET_KEY": ("sk_test_",),
    "STRIPE_PUBLISHABLE_KEY": ("pk_test_",),
    "VISA_VDP_ENV": ("sandbox",),
}
_NUMBER_RE = re.compile(r"^[0-9]+(\.[0-9]+)?$")
#: Store-platform credentials: with one, a store API call from the sandbox could authenticate. The
#: credential word may sit anywhere in the name (SHOPIFY_CREDENTIALS, SHOPIFY_ACCESS_TOKEN_JSON).
_STORE_CREDENTIAL_RE = re.compile(
    r"^(SHOPIFY|CAFE24|WIX|WOOCOMMERCE|WOO|AMAZON_SP_API|SQUARESPACE|BIGCOMMERCE|MAGENTO|SHOPLINE"
    r"|SHOPLAZZA)_[A-Z0-9_]*(TOKEN|SECRET|KEY|PASSWORD|CREDENTIAL|CLIENT_ID|AUTH)"
)
#: LLM and search API credentials (SF5): none is allowed in sandbox v0, whose profile carries none;
#: each would let the sandbox call a paid third-party service on Pivota's account.
_AI_SEARCH_CREDENTIAL_RE = re.compile(
    r"^(OPENAI|ANTHROPIC|CLAUDE|GEMINI|PIVOTA_GEMINI|GOOGLE_AI|GOOGLE_GENAI|GOOGLE_API|DEEPSEEK|SERPAPI"
    r"|SERPER|PERPLEXITY|MISTRAL|COHERE|GROQ|XAI|TOGETHER|FIREWORKS|OPENROUTER|BING|BRAVE|TAVILY|EXA|JINA"
    r"|FIRECRAWL|SCRAPINGBEE|APIFY|BROWSERLESS|CATALOG_INTELLIGENCE)_[A-Z0-9_]*(KEY|TOKEN|SECRET|CREDENTIAL)"
)
#: Model-provider dials: off (Vertex/Gemini authenticate with the service account, no key needed).
_AI_DIALS = ("VERTEX_AI_ENABLED", "GOOGLE_GENAI_USE_VERTEXAI")
#: Keyless third-party egress that DEFAULTS ON when unset: each must be SET to an explicit off value.
_EGRESS_DEFAULT_ON = (
    # routes/agent_shop_gateway.py: budget FX "latest" lookup to api.exchangerate.host (the static
    # FX table stays as the fallback).
    "AGENT_SHOP_BUDGET_FX_LATEST_FALLBACK_ENABLED",
    # services/grounding_redirect_resolver.py: follows grounding redirect URLs (default "true").
    "AUDIT_RESOLVE_GROUNDING_REDIRECTS",
)
#: POSITIVE flag rule: a feature flag (a name ending _ENABLED/_ENABLE/_ARMED/_APPLY/_LIVE) that is SET
#: to anything but an off value must be one reviewed for the sandbox. These are the flags the sandbox
#: profile turns on (infra/gcp/sandbox/env/web.yaml + worker.yaml), plus partner webhook delivery.
#: A flag nobody reviewed (e.g. a new *_CHECKOUT_ENABLED) is refused ON whatever its name says.
_FLAG_NAME_RE = re.compile(r"_(ENABLED|ENABLE|ARMED|APPLY|LIVE)$")
_FLAGS_ALLOWED_ON = frozenset({
    "AGENPAS_CONSENT_WRITE_ENABLED", "AGENPAS_INTERNAL_READ_ENABLED", "AGENT_EXTERNAL_SEED_CACHE_ENABLED",
    "AGENT_PURCHASE_LEDGER_ENABLED", "AGENT_SHOP_PIVOT_MULTI_SERVE_ENABLED", "AUDIT_WORKER_ENABLED",
    "PAYMENT_ORCHESTRATOR_ENABLED", "PROMETHEUS_METRICS_ENABLED", "REAP_AGENTIC_CART_LINK_ENABLED",
    "REAP_AGENTIC_CREATE_ENABLED", "REAP_AGENTIC_ENABLED", "REAP_AGENTIC_RECONCILE_ENABLED",
    # Purchase webhooks to the partner's own registered endpoint (off by default outside production).
    "WEBHOOK_DELIVERY_ENABLED",
})


@dataclass(frozen=True)
class Violation:
    setting: str
    rule: str

    def __str__(self) -> str:  # never the value
        return "%s: %s" % (self.setting, self.rule)


class SandboxGuardRefused(RuntimeError):
    def __init__(self, violations: List[Violation]):
        self.violations = list(violations)
        super().__init__(
            "sandbox guard: PIVOTA_ENV=sandbox but %d setting(s) break the sandbox's guarantees "
            "(no real money, no store fetch, no prod reference). Refusing to start. %s"
            % (len(self.violations), "; ".join(str(v) for v in self.violations))
        )


def _val(env: Mapping[str, str], name: str) -> str:
    return (env.get(name) or "").strip()


def _is_off(raw: str) -> bool:
    return raw.strip().lower() in _OFF_VALUES


def _host_in(host: str, zones: Tuple[str, ...]) -> Optional[str]:
    for zone in zones:
        if host == zone or host.endswith("." + zone):
            return zone
    return None


def _ip_sha256(token: str) -> Optional[str]:
    try:
        return hashlib.sha256(str(ipaddress.ip_address(token)).encode("ascii")).hexdigest()
    except ValueError:  # 999.1.1.1, 010.0.0.1: not an address
        return None


def _names_a_prod_ip(value: str) -> bool:
    return any(_ip_sha256(t) in _PROD_PRIVATE_IP_SHA256 for t in _IPV4_TOKEN_RE.findall(value))


def _prod_reference_rule(value: str) -> Optional[str]:
    """Which prod-reference rule ``value`` breaks, or None. The rule text never quotes the value."""
    low = value.lower()
    if _PROD_PROJECT_RE.search(low):
        return "names a prod or staging GCP project"
    if _names_a_prod_ip(low):
        return "names a prod or staging database/Redis private IP"
    if _LIVE_KEY_RE.search(low):
        return "holds a live payment or agent key"
    for host in _HOST_RE.findall(low):
        host = host.rstrip(".")
        if _host_in(host, _RAILWAY_ZONES):
            return "names a Railway host"
        zone = _host_in(host, _GUARDED_ZONES)
        if zone is not None:
            labels = host[: -len(zone)].rstrip(".").split(".") if host != zone else []
            if not any("sandbox" in label for label in labels if label):
                return "names a non-sandbox %s host" % zone
    return None


def _allowed_url_host(scheme: str, host: str) -> bool:
    from config.sandbox_identity import SANDBOX_DROP_BUCKET
    from services.reap_agentic_client import REAP_SANDBOX_HOSTS

    if scheme in ("http", "https"):
        return host in REAP_SANDBOX_HOSTS or host in _SANDBOX_PUBLIC_HOSTS
    if scheme == "gs":
        return host == SANDBOX_DROP_BUCKET
    return False  # redis://, postgres:// outside a DSN setting, amqp://, ftp://, ...


def _url_authority_host(authority: str) -> str:
    if authority.startswith("["):
        return authority[1:].split("]", 1)[0]
    return authority.split(":", 1)[0].rstrip(".")


def _url_rule(name: str, value: str) -> Optional[str]:
    """The rule a URL inside ``value`` breaks, or None: every scheme://host and //host must have a
    plain authority (no userinfo, no backslash) naming an allowed host."""
    low = value.lower()
    if _ESCAPED_URL_RE.search(low):
        return "holds an escaped or percent-encoded URL"
    urls = list(_URL_RE.findall(low)) + [("https", a) for a in _SCHEMELESS_URL_RE.findall(low)]
    for scheme, authority in urls:
        if not _AUTHORITY_RE.match(authority):
            return "names a URL with credentials or an unusual authority"
        if not _allowed_url_host(scheme, _url_authority_host(authority)):
            return "names a URL whose host is not on the sandbox's allowlist"
    return None


def _items(value: str) -> List[str]:
    return [i for i in re.split(r"[\s,;]+", value.strip().lower()) if i]


def _host_item_rule(item: str) -> Optional[str]:
    """One item of a host-named setting: an allowed host (optionally :port), or a URL (_url_rule)."""
    if "//" in item:
        return None  # a URL: _url_rule decides
    if not _AUTHORITY_RE.match(item) or not _allowed_url_host("https", _url_authority_host(item)):
        return "names a host that is not on the sandbox's allowlist"
    return None


def _address_rule(value: str) -> Optional[str]:
    low = value.lower()
    if any(_ip_sha256(t) for t in _IPV4_TOKEN_RE.findall(low)) or _HEX_IP_RE.search(low):
        return "holds an IP address (only DATABASE_URL may, and only the sandbox's)"
    for token in _IPV6_TOKEN_RE.findall(low):
        try:
            ipaddress.IPv6Address(token)
        except ValueError:
            continue
        return "holds an IP address (only DATABASE_URL may, and only the sandbox's)"
    return None


def _bare_host_rule(value: str) -> Optional[str]:
    """Any scheme-less host in any value must be an allowed host (an email's domain included)."""
    for host, tld in _BARE_HOST_RE.findall(value.lower()):
        if tld in _FILE_SUFFIXES:
            continue
        if not _allowed_url_host("https", host.rstrip(".")):
            return "names a host that is not on the sandbox's allowlist"
    return None


def _encoded_url_rule(value: str) -> Optional[str]:
    import base64
    import binascii

    for item in [value.strip()] + _items(value):
        if not _BASE64_RE.match(item):
            continue
        padded = item + "=" * (-len(item) % 4)
        for decode in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                decoded = decode(padded.encode("ascii"))
            except (binascii.Error, ValueError):
                continue
            if b"://" in decoded.lower():
                return "holds a base64-encoded URL"
    return None


def _provider_value_ok(name: str, raw: str) -> bool:
    value = raw.strip()
    if not value or _is_off(value) or _NUMBER_RE.match(value):
        return True
    return any(value.startswith(p) for p in _PROVIDER_TEST_SHAPES.get(name, ()))


def _worker_enabled(env: Mapping[str, str]) -> bool:
    return _val(env, "AUDIT_WORKER_ENABLED").lower() in _TRUE_VALUES


def sandbox_violations(env: Mapping[str, str]) -> List[Violation]:
    """Every rule ``env`` breaks, in a stable order. Pure: reads only ``env``."""
    out: List[Violation] = []

    # Reap: the sandbox host, simulated checkout.
    from services.reap_agentic_client import is_sandbox_base_url

    base = _val(env, "REAP_API_BASE_URL")
    if not base or not is_sandbox_base_url(base):
        out.append(Violation("REAP_API_BASE_URL", "must be a Reap sandbox host (REAP_SANDBOX_HOSTS)"))
    if _val(env, "REAP_AGENTIC_SIMULATE_CHECKOUT") != "COMPLETED":
        out.append(Violation("REAP_AGENTIC_SIMULATE_CHECKOUT", "must be exactly COMPLETED"))

    # A2A rail: off, and nothing else configured.
    for name in sorted(k for k in env if k.startswith(_A2A_PREFIX)):
        raw = env.get(name) or ""
        if name in _A2A_DIALS:
            if not _is_off(raw):
                out.append(Violation(name, "the A2A rail must be off in the sandbox"))
        elif raw.strip():
            out.append(Violation(name, "no A2A_RAIL_* setting may be set in the sandbox"))
    for name in _MONEY_DIALS:
        if name in env and not _is_off(env.get(name) or ""):
            out.append(Violation(name, "real-money dial must be off in the sandbox"))

    # Internal surfaces that open outside production.
    for name in _PARTNER_SURFACE_DIALS:
        if name in env and not _is_off(env.get(name) or ""):
            out.append(Violation(name, "internal demo surface must be off in the partner sandbox"))
    for name in _PARTNER_SURFACE_SECRETS:
        if _val(env, name):
            out.append(Violation(name, "must be unset in the partner sandbox (opens a debug surface)"))

    # Store fetches.
    for name in _STORE_FETCH_DEFAULT_ON:
        if name not in env or not _is_off(env.get(name) or "") or not _val(env, name):
            out.append(Violation(name, "defaults on; must be set to an explicit off value (0/false)"))
    named = set(_STORE_FETCH_FLAGS) | set(_STORE_FETCH_DEFAULT_ON)
    for name in _STORE_FETCH_FLAGS:
        if name in env and not _is_off(env.get(name) or ""):
            out.append(Violation(name, "store fetch / crawl / live-read flag must be off"))
    for name in sorted(env):
        if name in named or not _STORE_FETCH_NAME.search(name):
            continue
        if not _is_off(env.get(name) or ""):
            out.append(Violation(name, "looks like a crawl/refresh/live-read flag; must be off"))

    # Scheduler: a sandbox worker runs only the purchase jobs.
    if _worker_enabled(env):
        raw = _val(env, "SCHEDULER_JOB_ALLOWLIST")
        ids = [part.strip() for part in raw.split(",") if part.strip()]
        if not raw:
            out.append(Violation("SCHEDULER_JOB_ALLOWLIST", "a sandbox worker must name its jobs"))
        elif "*" in ids:
            out.append(Violation("SCHEDULER_JOB_ALLOWLIST", "'*' (all jobs) is refused in the sandbox"))
        else:
            extra = sorted(set(ids) - SANDBOX_SCHEDULER_JOBS)
            if extra:
                # Job ids are not secrets; naming them is what makes the refusal actionable.
                out.append(Violation(
                    "SCHEDULER_JOB_ALLOWLIST",
                    "names jobs outside SANDBOX_SCHEDULER_JOBS: %s" % ",".join(extra),
                ))

    # Identity: the sandbox's project, and every DSN the sandbox's own database.
    from config.sandbox_identity import SANDBOX_PROJECT, dsn_refusal

    if _val(env, _IDENTITY_PROJECT_SETTING) != SANDBOX_PROJECT:
        out.append(Violation(_IDENTITY_PROJECT_SETTING, "must be exactly %s" % SANDBOX_PROJECT))
    dsn_names = {name for name in env if _DSN_NAME_RE.search(name)} | {"DATABASE_URL"}
    for name in sorted(dsn_names):
        raw = env.get(name) or ""
        if name != "DATABASE_URL" and not raw.strip():
            continue
        refusal = dsn_refusal(raw, name)
        if refusal:
            out.append(Violation(name, refusal))

    # Redis: none in sandbox v0.
    for name in sorted(env):
        if _REDIS_NAME_RE.search(name) and (env.get(name) or "").strip() and not _is_off(env.get(name) or ""):
            out.append(Violation(name, "no Redis in the sandbox (v0): must be unset"))

    # URLs and hosts: on the allowlist, wherever they appear (DSNs are checked above instead).
    for name in sorted(env):
        value = env.get(name) or ""
        if not value.strip() or name in dsn_names or _DISPLAY_ONLY_LINKS.get(name) == value.strip():
            continue
        rule = _url_rule(name, value)
        if rule is None and _HOST_NAME_RE.search(name):
            rule = next((r for r in map(_host_item_rule, _items(value)) if r), None)
        if rule is None and _URL_NAME_RE.search(name):
            if any("//" not in item for item in _items(value)):
                rule = "must be a URL on the sandbox's allowlist (or empty)"
        if rule is None:
            rule = _address_rule(value) or _bare_host_rule(value) or _encoded_url_rule(value)
        if rule is None and _PROXY_NAME_RE.match(name):
            rule = "no outbound proxy in the sandbox"
        if rule:
            out.append(Violation(name, rule))
    for name in _PROD_DEFAULT_URL_SETTINGS:
        if name not in env:
            out.append(Violation(name, "defaults to a prod host when unset; set it to empty or a sandbox URL"))
    from urllib.parse import urlsplit

    from config.settings import resolve_public_api_base_url

    resolved = urlsplit(resolve_public_api_base_url(env))
    if resolved.scheme != "https" or (resolved.hostname or "") not in _PUBLIC_API_BASE_HOSTS:
        out.append(Violation(
            "PUBLIC_API_BASE_URL",
            "must resolve to https://%s (unset or empty resolves to prod's api.pivota.cc)"
            % "|".join(sorted(_PUBLIC_API_BASE_HOSTS)),
        ))

    # Money that defaults on, and every refund flag.
    for name in _MONEY_DEFAULT_ON:
        if not _val(env, name) or not _is_off(env.get(name) or ""):
            out.append(Violation(name, "defaults on; must be set to an explicit off value (0/false)"))
    for name in sorted(env):
        if name in _MONEY_DEFAULT_ON:
            continue
        if (_REFUND_FLAG_RE.match(name) or name in _MORE_MONEY_DIALS) and not _is_off(env.get(name) or ""):
            out.append(Violation(name, "real-money dial must be off in the sandbox"))

    # Live reads and egress the store-fetch pattern does not catch; pinned modes.
    for name in sorted(env):
        if name in _MORE_STORE_FETCH_FLAGS or _EGRESS_NAME_RE.search(name):
            if not _is_off(env.get(name) or ""):
                out.append(Violation(name, "live read / egress flag must be off"))
    for name, allowed in sorted(_PINNED_VALUES.items()):
        if _val(env, name).lower() not in allowed:
            out.append(Violation(name, "must be one of: %s" % ", ".join(sorted(v or "(unset)" for v in allowed))))

    # Every feature flag that is ON must be one reviewed for the sandbox.
    for name in sorted(env):
        if name.startswith(_A2A_PREFIX):
            continue  # the A2A rule above names these already
        if _FLAG_NAME_RE.search(name) and name not in _FLAGS_ALLOWED_ON and not _is_off(env.get(name) or ""):
            out.append(Violation(name, "feature flag ON but not reviewed for the sandbox (_FLAGS_ALLOWED_ON)"))

    # LLM / search credentials, model dials, keyless egress that defaults on (SF5).
    for name in sorted(env):
        if _AI_SEARCH_CREDENTIAL_RE.match(name) and (env.get(name) or "").strip():
            out.append(Violation(name, "LLM/search API credential: none is allowed in sandbox v0"))
    for name in _AI_DIALS:
        if name in env and not _is_off(env.get(name) or ""):
            out.append(Violation(name, "model-provider dial must be off in sandbox v0"))
    for name in _EGRESS_DEFAULT_ON:
        if not _val(env, name) or not _is_off(env.get(name) or ""):
            out.append(Violation(name, "defaults on (a third-party call); must be set to an explicit off value"))

    # Email, SMS and payment providers; store credentials.
    for name in sorted(env):
        raw = env.get(name) or ""
        if _PROVIDER_NAME_RE.search(name) and name not in _PINNED_VALUES and not _provider_value_ok(name, raw):
            out.append(Violation(name, "email/SMS/payment-provider setting: refused unless test-shaped"))
        elif _STORE_CREDENTIAL_RE.match(name) and raw.strip():
            out.append(Violation(name, "store credential: the sandbox holds none"))

    # Prod references, in any setting's value.
    for name in sorted(env):
        value = env.get(name) or ""
        if not value.strip():
            continue
        if _DISPLAY_ONLY_LINKS.get(name) == value.strip():
            continue
        rule = _prod_reference_rule(value)
        if rule:
            out.append(Violation(name, rule))
    return out


def enforce_sandbox_guard(env: Optional[Mapping[str, str]] = None) -> bool:
    """Raise :class:`SandboxGuardRefused` when this is a sandbox process with a bad setting.

    Returns True when the guard ran and passed, False when this is not a sandbox process (in which
    case nothing but PIVOTA_ENV and the platform markers was read).
    """
    import os

    from config.platform import is_sandbox

    source = os.environ if env is None else env
    if not is_sandbox(source):
        return False
    violations = sandbox_violations(source)
    if violations:
        raise SandboxGuardRefused(violations)
    return True
