"""Does the A2A rail's PRODUCTION card list carry a four-digit last4? Read-only probe, counts and booleans only.

The AgenPas buyer page names the card an A2A purchase pays with from the rail's own card list (`payment.method-list`
methods[].payment_brand and .last4; db/a2a_rail_ledger.record_payment_card_as_holder, migration 298). That was measured
on the rail's TEST environment only. Before AGENPAS_PAYMENT_CARD_ENABLED is ever on for the A2A rail, this probe asks
the PRODUCTION rail, once, for ONE buyer's card list and reports whether each card carries a brand and a last4 of
exactly four digits.

WHAT IT SENDS. One token exchange for the member, then ONE read: `payment.method-view#submit` with every status listed,
in a fresh conversation of its own (never a purchase's). Exactly the read the A2A worker makes before each step. No
other action, nothing written anywhere, no database.

WHAT IT PRINTS. One `A2A_LAST4 {...}` line: how many methods, and per method three booleans (a brand present, a last4
of exactly four digits, status ACTIVE). Then `A2A_LAST4_DONE`. Never a last4, a brand, a card id, a member id, a token,
the key or a response body, in any output or log line. A failed read prints its kind only.

WHERE. Only the rail's PRODUCTION host. `--base` must be exactly that host (pinned here by digest,
PRODUCTION_HOST_SHA256, the way services/payment_orchestration/a2a_rail pins its allowed hosts), so the rail's test host
and every other host are refused before the key is read. The key comes from A2A_RAIL_API_KEY (the job mounts that one
secret and nothing else).

Run as a gated prod one-off (oneoff_gate.sh GATE_OPEN first), through the PR controller, with the owner's go:
  SECRETS=A2A_RAIL_API_KEY=A2A_RAIL_API_KEY:latest \\
    scripts/ops/run_oneoff_job.sh scripts/ops/a2a_method_list_last4_probe.py --base https://<prod host> --member <member id>

Exit: 0 read and reported (whatever the answer), 2 the read failed or gave no card list, 3 refused before any call.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import sys
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlparse

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from services.payment_orchestration import a2a_rail  # noqa: E402

#: SHA-256 of the rail's PRODUCTION host name (lower case), one of a2a_rail.ALLOWED_HOST_SHA256. The other digest there
#: is the rail's test host, which this probe refuses: it exists to measure production.
PRODUCTION_HOST_SHA256 = "c91cff4b3b7cf1ed9344a5334df60dc9751db61743bd7a41d082c35345097d7b"
VIEW_METHODS = ("payment.method-view", "submit")
LIST_ALL = "ACTIVE,PENDING,DISABLED,FAILED,EXPIRED"
_LAST4_RE = re.compile(r"^[0-9]{4}\Z")
_MEMBER_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}\Z")


class Refused(Exception):
    """Refused before any call; the message names the rule, never a value."""


def production_base(raw: str) -> str:
    """The production base URL, or Refused: https, a bare origin, and exactly the pinned production host."""
    value = str(raw or "").strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port is not None:
        raise Refused("base must be a bare https origin")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise Refused("base must be a bare https origin")
    host = (parsed.hostname or "").lower()
    if hashlib.sha256(host.encode("utf-8")).hexdigest() != PRODUCTION_HOST_SHA256:
        raise Refused("base is not the rail's production host")
    return a2a_rail.validate_base_url(value)


def summarize(methods: Sequence[Any]) -> Dict[str, Any]:
    """Counts and booleans only: never a value from a method."""
    per: List[Dict[str, bool]] = []
    for m in methods:
        if not isinstance(m, dict):
            per.append({"is_object": False, "brand": False, "last4_4_digits": False, "active": False})
            continue
        brand = m.get("payment_brand")
        last4 = m.get("last4")
        per.append({
            "is_object": True,
            "brand": isinstance(brand, str) and bool(brand.strip()),
            "last4_4_digits": isinstance(last4, str) and bool(_LAST4_RE.match(last4)),
            "active": m.get("status") == "ACTIVE",
        })
    return {
        "methods": len(per),
        "with_brand": sum(p["brand"] for p in per),
        "with_last4_4_digits": sum(p["last4_4_digits"] for p in per),
        "active": sum(p["active"] for p in per),
        "active_with_last4_4_digits": sum(p["active"] and p["last4_4_digits"] for p in per),
        "per_method": per,
    }


async def probe(base: str, api_key: str, member_id: str, *, transport: Any = None) -> Dict[str, Any]:
    client = a2a_rail.A2ARailClient(base, api_key, transport=transport)
    context = client.context_for(member_id, f"last4_probe_{secrets.token_hex(6)}")
    result = await client.send_action(
        member_id=member_id, context_id=context, component=VIEW_METHODS[0], action=VIEW_METHODS[1],
        payload={"list_statuses": LIST_ALL},
    )
    if not getattr(result, "ok", False):
        return {"read": "failed", "kind": str(getattr(result, "kind", "unknown"))[:40]}
    card = result.card("payment.method-list")
    methods = card.data.get("methods") if card is not None else None
    if not isinstance(methods, list):
        return {"read": "no_method_list"}
    return {"read": "ok", **summarize(methods)}


def main(argv: Optional[Sequence[str]] = None, *, transport: Any = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True, help="the rail's production origin (pinned by digest)")
    parser.add_argument("--member", required=True, help="the rail member whose card list is read (never printed)")
    args = parser.parse_args(argv)
    try:
        base = production_base(args.base)
        if not _MEMBER_RE.match(args.member or ""):
            raise Refused("member must be 1..128 of [A-Za-z0-9_.:-]")
        api_key = (os.getenv(a2a_rail.A2A_RAIL_API_KEY_ENV) or "").strip()
        if not api_key:
            raise Refused(f"{a2a_rail.A2A_RAIL_API_KEY_ENV} is not mounted")
    except Refused as exc:
        print(f"A2A_LAST4_REFUSED {exc}")
        return 3
    try:
        report = asyncio.run(probe(base, api_key, args.member, transport=transport))
    except Exception as exc:  # noqa: BLE001 - the error's type only: a message could carry a value
        report = {"read": "error", "error_type": type(exc).__name__}
    print("A2A_LAST4 " + json.dumps(report, sort_keys=True))
    print("A2A_LAST4_DONE")
    return 0 if report.get("read") == "ok" else 2


if __name__ == "__main__":
    sys.exit(main())
