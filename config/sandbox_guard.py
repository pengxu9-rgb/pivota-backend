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

WHAT IT DOES NOT CLAIM. The flag lists are the flags this codebase has today; a store fetch behind a
flag of a different shape, or behind no flag at all, is not caught here. The second lock is the
data: the catalog copy (jobs/sandbox_catalog_copy.py) carries no store credential (no Shopify /
Cafe24 / WooCommerce token, no webhook secret), so a store API call from the sandbox has nothing to
authenticate with. Unauthenticated public page fetches are what the flags above and the scheduler
allowlist exist to stop.
"""

from __future__ import annotations

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
    "STORE_LIFECYCLE_RECONCILE_ENABLED",
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
#: Private IPs of prod's Cloud SQL and Redis and staging's Cloud SQL (infra/gcp/README.md).
_PROD_PRIVATE_IPS = ("10.25.0.2", "10.25.7.196", "10.122.0.3")
_PROD_IP_RE = re.compile(
    r"(?<![0-9.])(" + "|".join(re.escape(ip) for ip in _PROD_PRIVATE_IPS) + r")(?![0-9])"
)
#: Domains whose hosts must be sandbox hosts (some label contains "sandbox").
_GUARDED_ZONES = ("pivota.cc", "agenpaz.com", "agenpas.com", "reap.global")
#: Railway was prod's platform; the sandbox never runs there or talks to it.
_RAILWAY_ZONES = ("railway.app", "railway.internal", "rlwy.net")
#: Live credentials by prefix: Stripe live secret / restricted keys, Pivota live agent keys.
_LIVE_KEY_PREFIXES = ("sk_live_", "rk_live_", "ak_live_")
_LIVE_KEY_RE = re.compile(
    r"(?<![a-z0-9])(" + "|".join(re.escape(p) for p in _LIVE_KEY_PREFIXES) + r")"
)
_HOST_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+")


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


def _prod_reference_rule(value: str) -> Optional[str]:
    """Which prod-reference rule ``value`` breaks, or None. The rule text never quotes the value."""
    low = value.lower()
    if _PROD_PROJECT_RE.search(low):
        return "names a prod or staging GCP project"
    if _PROD_IP_RE.search(low):
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

    # Prod references, in any setting's value.
    for name in sorted(env):
        value = env.get(name) or ""
        if not value.strip():
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
