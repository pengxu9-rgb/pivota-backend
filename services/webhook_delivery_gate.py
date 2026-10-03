"""WEBHOOK_DELIVERY_ENABLED: outbound agent/merchant webhooks deliver only in production by default.

WHY THIS EXISTS. Staging runs on a RESTORED COPY of production data (infra/gcp/deploy_backend.sh),
so every webhook config in it carries a REAL agent or merchant `destination_url` and that
customer's prod signing secret. Outbound delivery could leave staging on four paths:

  * the retry loops `agent_webhook_retry_worker` / `merchant_webhook_retry_worker`, which
    `main.startup_event` starts on EVERY process, `web` included, whatever AUDIT_WORKER_ENABLED
    says, and `process_due_retries`, which `list_deliveries` also runs inline;
  * first attempts: `emit_*_webhook_event`, called from order, payment, refund and agent flows;
  * a user-clicked per-row retry (`retry_delivery`);
  * a test send (`send_test_webhook`).

All four end in `_attempt_delivery`, the one place in each service that POSTs. That is where
this gate is enforced. It is also checked earlier, in `process_due_retries` and
`_retry_worker_loop`, so a disabled process does not even select due rows or poll.

Measured 2026-09-29: staging had 0 due retry rows and had written no delivery since its restore,
so this closes a latent path rather than an active one.

THE CONTRACT (read per call, never cached):

  * unset / empty                      -> deliver only if `platform_env()` is production. Prod
    therefore behaves exactly as before, and staging, development and test do not deliver.
  * 1 / true / yes / on                -> deliver, in any environment. This is the explicit
    opt-in for a staging that needs webhooks, e.g. against a sandbox destination.
  * 0 / false / no / off               -> do not deliver, in any environment, production
    included. This is the kill switch.
  * anything else                      -> do NOT deliver, and log a WARNING. An operator who set
    the variable meant something, and the safe reading of a value we cannot parse is "off".

WHAT "NOT DELIVER" MEANS: no HTTP and no delivery row. `_attempt_delivery` returns the services'
existing skip shape (see `skipped_result`), which is what an unconfigured or unsubscribed
destination already returns. Callers in order and payment flows therefore see a result they
already handle. A `retrying` row keeps its `attempt_count` and `next_retry_at`, so re-enabling
delivers it and no attempt is burned.

It composes with SCHEDULER_JOB_ALLOWLIST (services/scheduler_job_allowlist.py, where present):
that decides whether the retry loops START on this process, and this decides whether anything
is DELIVERED. Both must allow it.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Mapping, Optional

from config.platform import PRODUCTION, platform_env

logger = logging.getLogger(__name__)

ENV_VAR = "WEBHOOK_DELIVERY_ENABLED"
SKIP_REASON = "delivery_disabled_in_this_environment"

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def delivery_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """True if this process may POST webhooks. See the module docstring."""
    source = os.environ if env is None else env
    raw = (source.get(ENV_VAR) or "").strip().lower()
    if not raw:
        return platform_env(env) == PRODUCTION
    if raw in _TRUE:
        return True
    if raw not in _FALSE:
        logger.warning(
            "%s=%r is not one of %s / %s; treating it as OFF, so no webhooks are delivered.",
            ENV_VAR, raw, sorted(_TRUE), sorted(_FALSE),
        )
    return False


def skipped_result(*, event_type: str, delivery_id: Optional[str]) -> Dict[str, Any]:
    """What `_attempt_delivery` returns instead of POSTing. It uses the same shape as the other skips."""
    return {
        "status": "skipped",
        "reason": SKIP_REASON,
        "event_type": event_type,
        "delivery_id": delivery_id,
    }


def describe(env: Optional[Mapping[str, str]] = None) -> str:
    """One line for the log: the resolved environment and the raw variable (ids only, no secrets)."""
    source = os.environ if env is None else env
    return "platform_env=%r %s=%r" % (platform_env(env), ENV_VAR, source.get(ENV_VAR))
