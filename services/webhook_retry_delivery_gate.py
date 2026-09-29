"""WEBHOOK_RETRY_DELIVERY_ENABLED: outbound webhook RETRIES deliver only in production by default.

WHY THIS EXISTS. `main.startup_event` starts `agent_webhook_retry_worker` and
`merchant_webhook_retry_worker` on EVERY process, `web` included, whatever AUDIT_WORKER_ENABLED
says, and `list_deliveries` (both services) runs `process_due_retries` inline on every read of
the delivery log. Staging runs on a RESTORED COPY of production data (infra/gcp/deploy_backend.sh),
so any `retrying` row copied from prod is due in staging too, and its `destination_url` is a REAL
agent or merchant endpoint. Nothing stopped staging from POSTing prod events, signed with prod
signing secrets, to real customers. Measured 2026-09-29: staging had 0 due rows, so this closes a
latent path rather than an active one. The next restore that copies in a `retrying` row would
open it.

THE CONTRACT (read per call, never cached):

  * unset / empty                      -> deliver only if `platform_env()` is production. Prod
    therefore behaves exactly as before, and staging, development and test do not deliver.
  * 1 / true / yes / on                -> deliver, in any environment. This is the explicit
    opt-in for a staging that needs retries, e.g. against a sandbox destination.
  * 0 / false / no / off               -> do not deliver, in any environment, production
    included. This is the kill switch.
  * anything else                      -> do NOT deliver, and log a WARNING. An operator who set
    the variable meant something, and the safe reading of a value we cannot parse is "off".

WHAT "NOT DELIVER" MEANS: no due-row SELECT and no HTTP. A `retrying` row is left exactly as it
was, with the same `attempt_count` and `next_retry_at`, so re-enabling delivers it and no
attempt is burned.

SCOPE. This gates the RETRY path only: the two boot loops and `process_due_retries`, including
the inline call in `list_deliveries`. First-attempt delivery (`emit_*_webhook_event`), an explicit
per-row `retry_delivery` from the portal, and test sends are request-triggered and are NOT gated
here.

It composes with SCHEDULER_JOB_ALLOWLIST (services/scheduler_job_allowlist.py, where present):
that decides whether the loop STARTS on this process, and this decides whether anything it or the
inline path finds is DELIVERED. Both must allow it.
"""

from __future__ import annotations

import logging
import os
from typing import Mapping, Optional

from config.platform import PRODUCTION, platform_env

logger = logging.getLogger(__name__)

ENV_VAR = "WEBHOOK_RETRY_DELIVERY_ENABLED"

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def retry_delivery_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """True if this process may deliver due webhook retries. See the module docstring."""
    source = os.environ if env is None else env
    raw = (source.get(ENV_VAR) or "").strip().lower()
    if not raw:
        return platform_env(env) == PRODUCTION
    if raw in _TRUE:
        return True
    if raw not in _FALSE:
        logger.warning(
            "%s=%r is not one of %s / %s; treating it as OFF, so no webhook retries are delivered.",
            ENV_VAR, raw, sorted(_TRUE), sorted(_FALSE),
        )
    return False


def describe(env: Optional[Mapping[str, str]] = None) -> str:
    """One line for the log: the resolved environment and the raw variable (ids only, no secrets)."""
    source = os.environ if env is None else env
    return "platform_env=%r %s=%r" % (platform_env(env), ENV_VAR, source.get(ENV_VAR))
