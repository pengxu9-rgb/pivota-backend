"""One-off: prove Pivota can reach Visa's Developer Platform with each configured auth mode.

Calls GET /vdp/helloworld once per configured mode (two-way SSL, X-Pay) through
services/payment_orchestration/visa_vdp_client and prints ONE JSON line (a Cloud Run job's stdout
lands in jsonPayload). The line carries only shape: environment, mode, status, outcome, elapsed time
and Visa's correlation id, never a body, credential or certificate.

`"gate": "OPEN"` appears only when every configured mode answered 2xx; the exit code is 0 then and
1 otherwise (2 when the client is not configured at all), so a caller gates on the printed gate and
not on a pipe.

    VISA_VDP_ENABLED=on VISA_VDP_ENV=sandbox python -m jobs.visa_vdp_hello_world
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any, Dict, List

from services.payment_orchestration import visa_vdp_client as vdp


async def run() -> Dict[str, Any]:
    try:
        client = vdp.from_env()
    except vdp.VisaVdpConfigError as exc:
        return {"job": "visa_vdp_hello_world", "gate": "CLOSED", "error": "config", "detail": str(exc)}
    if client is None:
        return {"job": "visa_vdp_hello_world", "gate": "CLOSED", "error": "not_configured"}
    checks: List[Dict[str, Any]] = []
    for mode in client.auth_modes:
        result = await client.hello_world(auth=mode)
        checks.append({
            "mode": mode,
            "status": result.status,
            "outcome": result.outcome,
            "ok": result.ok,
            "elapsed_ms": result.elapsed_ms,
            "correlation_id": result.correlation_id,
        })
    gate = "OPEN" if checks and all(c["ok"] for c in checks) else "CLOSED"
    return {"job": "visa_vdp_hello_world", "env": client.environment, "gate": gate, "checks": checks}


def main() -> int:
    # httpx logs every request URL at INFO, and X-Pay's API key is in the query. The client already
    # redacts it in those records; this job also keeps both loggers quiet, so an operator who turns
    # INFO on for this process sees the job's own line and nothing else. The prior levels are
    # restored on the way out, so calling main() in-process (tests, a runner) leaves logging as it was.
    quiet = [logging.getLogger(name) for name in ("httpx", "httpcore")]
    prior = [lg.level for lg in quiet]
    for lg in quiet:
        lg.setLevel(logging.WARNING)
    try:
        summary = asyncio.run(run())
    finally:
        for lg, level in zip(quiet, prior):
            lg.setLevel(level)
    print(json.dumps(summary, sort_keys=True))
    if summary.get("error") == "not_configured":
        return 2
    return 0 if summary.get("gate") == "OPEN" else 1


if __name__ == "__main__":
    sys.exit(main())
