"""Drive one buyer-funded Reap purchase by hand: enroll, checkout, poll. Pivota moves no money.

RUN THIS FROM THE SESSION THAT HOLDS THE SANDBOX CREDENTIALS. It reads them from the environment
and never prints them, so nobody has to hand the key to anyone.

    export REAP_API_BASE_URL="https://sandbox.api.reap.global"
    export REAP_API_KEY="..."                 # never echoed, never logged, never in a URL
    S=~/.cache/pivota/reap_steps.json         # NOT a shared /tmp path: it holds ids, not secrets,
                                              # but two operators sharing one file is a bad day

    python3 scripts/ops/reap_purchase_steps.py enroll   --state "$S" --owner-id ops-probe-1 --apply
    #   -> prints a hosted URL. A HUMAN opens it and types THEIR OWN card. This script never does.
    python3 scripts/ops/reap_purchase_steps.py poll     --state "$S"            # is it ACTIVE yet?
    python3 scripts/ops/reap_purchase_steps.py checkout --state "$S" --quote-id <id> --apply
    python3 scripts/ops/reap_purchase_steps.py poll     --state "$S"            # did it complete?

WHAT THIS SCRIPT WILL NOT DO, AND WHY THAT IS THE DESIGN.

  It never enters card data. There is no flag for it and no code path to it. The card leg is
  between the BUYER and REAP on a page Reap hosts; that is the entire reason this rail is
  compatible with the 6 Sep constraint that Pivota never deposits, prefunds, custodies, or is
  liable for a balance. A script that could type a card number would make us a party to the money
  leg, which is the thing we are not.

  It never opens the hosted URL for you. It prints it. Opening it is a human act.

  `enroll` and `checkout` are DRY BY DEFAULT and need `--apply`. Both CREATE something at a
  partner: an enrollment is cheap, but a checkout against a live quote is the step where a real
  buyer's card gets charged, and a dry run that prints the exact body is worth more than an
  undo that does not exist. `poll` is read-only and needs no flag.

  It is not run by CI and it is not imported by anything. Live verification is a human sitting
  with the sandbox key, reading what came back.

WHAT THE STATE FILE IS FOR. The three steps happen minutes or hours apart -- a human has to open
a page in between -- so the enrollment id and checkout id have to outlive the process. It holds
ids and timestamps. It holds NO credential and no card data, and `checkout` refuses to run if the
enrollment it finds there is not ACTIVE, because `ENROLLMENT_NOT_ACTIVE` is the 400 this sequence
actually hits (`error.detail.code`, inside a 400 `AGENTIC_REQUEST_REJECTED`).

THE ORDER MATTERS AND IS NOT OBVIOUS. An enrollment is REQUIRES_ACTION the moment it is created
and stays that way until the buyer finishes the hosted page. There is no webhook -- agentic
resources have none -- so `poll` is not a convenience, it is the only way any outcome is ever
learned.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Any, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

#: Where the buyer's browser lands after the hosted page. Ours, and validated against
#: REAP_RETURN_URL_HOSTS by the client before it is ever sent.
DEFAULT_RETURN_URL = "https://agent.pivota.cc/reap/return"


# --- state ---------------------------------------------------------------------------------


def _load(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001
        print(f"state file {path} is unreadable ({type(exc).__name__}); refusing to overwrite it")
        raise SystemExit(2)
    return data if isinstance(data, dict) else {}


def _save(path: str, state: Dict[str, Any]) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(f"\nstate -> {path}")


# --- shared preamble ------------------------------------------------------------------------


def _client():
    from services import reap_agentic_client as rc

    print(f"Base URL set : {bool(rc.base_url())}")
    print(f"API key set  : {bool(os.getenv('REAP_API_KEY'))}   (value never printed)")
    if not rc.is_configured():
        print("\nNOT CONFIGURED — set REAP_API_BASE_URL and REAP_API_KEY. Nothing was sent.")
        raise SystemExit(1)
    try:
        rc.validate_base_url()
    except rc.ReapConfigError as exc:
        print(f"\nHOST REFUSED: {exc}")
        print(f"\nThe allowlist is {rc.ALLOWED_HOST_SUFFIXES}, taken from the spec's `servers`.\n"
              "DO NOT disable the check: a mistyped base URL delivers the API key to the typo.")
        raise SystemExit(2)
    print(f"Host check   : OK ({rc.base_url()})")
    print(f"returnUrl hosts allowed : {rc.return_url_hosts()}")
    return rc


def _report_failure(rc, result) -> None:
    """Everything we are willing to say about a failure. The body is NOT one of those things."""
    print(f"\nok     : {result.ok}")
    print(f"status : {result.status}")
    print(f"error  : {result.error}")
    print(f"code   : {result.error_code}        (error.code)")
    print(f"detail : {result.error_detail_code}  (error.detail.code)")
    for line in rc.explain_refusal(result.error, detail_code=result.error_detail_code):
        print(f"\n  {line}")
    print("\nThe response BODY is deliberately not captured — only the two machine-readable\n"
          "codes above. A partner's error payload can echo the request, and the request can\n"
          "carry a buyer's address.")


def _show_hosted(rc, payload: Dict[str, Any]) -> Optional[str]:
    action = rc.hosted_action(payload)
    if action is None:
        if isinstance(payload.get("nextAction"), dict):
            print("\nThere IS a nextAction, but its URL is not on an allowed host and was\n"
                  "therefore not shown. That is a refusal, not a display bug — this is the page\n"
                  "a buyer types a card into.")
        else:
            print("\nNo next action: there is nowhere to send a human right now.")
        return None
    url, expires = action
    print("\n" + "=" * 78)
    print("OPEN THIS IN A BROWSER, AS A HUMAN. The card is typed on REAP's page, not here.")
    print(f"\n    {url}\n")
    print(f"expires: {expires or 'not stated'}")
    print("=" * 78)
    return url


# --- subcommands ----------------------------------------------------------------------------


def cmd_enroll(args) -> int:
    rc = _client()
    try:
        body = rc.build_enrollment_request(
            owner_id=args.owner_id, return_url=args.return_url, email=args.email
        )
    except rc.ReapRequestError as exc:
        print(f"\nREFUSED BEFORE EGRESS: {exc}")
        return 3

    print("\n--- ENROLLMENT REQUEST " + "-" * 48)
    print("POST /agentic/enrollments")
    print(json.dumps(body, indent=2))
    print("\nNote `source: EXTERNAL`. REAP_CARD and BIN_SPONSOR enroll an ALREADY-ISSUED card —\n"
          "the Program-Funded rail, dormant by design — and the builder refuses to construct\n"
          "either of them, or any `cardId`, at all.")

    if not args.apply:
        print("\nDRY RUN. Nothing was sent. Re-run with --apply to create this enrollment.")
        return 0

    result = asyncio.run(rc.create_enrollment(
        owner_id=args.owner_id, return_url=args.return_url, email=args.email
    ))
    if not result.ok:
        _report_failure(rc, result)
        return 4

    payload = result.data
    print("\n--- ENROLLMENT " + "-" * 56)
    print(f"id     : {payload.get('id')}")
    print(f"status : {payload.get('status')}  ->  {rc.enrollment_state(payload)}")
    _show_hosted(rc, payload)

    state = _load(args.state)
    state["enrollment_id"] = payload.get("id")
    state["enrollment_status"] = payload.get("status")
    state["owner_id"] = args.owner_id
    state["enrolled_at"] = time.time()
    _save(args.state, state)
    print("\nNext: a human opens the URL above, then `poll` until the enrollment is `active`.")
    return 0


def cmd_checkout(args) -> int:
    rc = _client()
    state = _load(args.state)
    enrollment_id = args.enrollment_id or state.get("enrollment_id")
    if not enrollment_id:
        print("\nNo enrollment id — run `enroll` first, or pass --enrollment-id.")
        return 3

    if not args.skip_enrollment_check:
        current = asyncio.run(rc.get_enrollment(enrollment_id))
        if not current.ok:
            _report_failure(rc, current)
            return 4
        where = rc.enrollment_state(current.data)
        print(f"\nenrollment {enrollment_id} is {current.data.get('status')} -> {where}")
        if where != "active":
            print("\nREFUSING TO CREATE A CHECKOUT. An enrollment that is not ACTIVE produces a\n"
                  "400 AGENTIC_REQUEST_REJECTED with detail.code ENROLLMENT_NOT_ACTIVE, and\n"
                  "creating it again will not help.")
            if where == "pending":
                _show_hosted(rc, current.data)
                print("\nThe buyer has not finished the hosted card page. Send them back to it.")
            return 5
        method = current.data.get("paymentMethod") or {}
        if method:
            # last4 is the whole point of showing anything here: it is how a human confirms the
            # buyer enrolled the card they meant to. Nothing else about the card is read.
            print(f"card   : {method.get('network')} ****{method.get('last4')} "
                  f"exp {method.get('expiryMonth')}/{method.get('expiryYear')}")

    try:
        body = rc.build_checkout_request(
            quote_id=args.quote_id, enrollment_id=enrollment_id, return_url=args.return_url
        )
    except rc.ReapRequestError as exc:
        print(f"\nREFUSED BEFORE EGRESS: {exc}")
        return 3

    print("\n--- CHECKOUT REQUEST " + "-" * 50)
    print("POST /agentic/checkouts")
    print(json.dumps(body, indent=2))
    print("\nNote what is NOT here: there is no `owner` field any more. It was required as\n"
          "recently as the previous spec read and is now absent entirely — and `info.version`\n"
          "did not move. Reap drops unknown keys with a 200, so a body still sending it would\n"
          "look exactly like one that worked.")

    if not args.apply:
        print("\nDRY RUN. Nothing was sent. Re-run with --apply.")
        print("READ THE QUOTE TOTAL FIRST. --apply on a live quote is the step that leads to a\n"
              "real charge on a real buyer's card, on their approval, on Reap's page.")
        return 0

    result = asyncio.run(rc.create_checkout(
        quote_id=args.quote_id, enrollment_id=enrollment_id, return_url=args.return_url
    ))
    if not result.ok:
        _report_failure(rc, result)
        return 4

    payload = result.data
    print("\n--- CHECKOUT " + "-" * 58)
    print(f"id     : {payload.get('id')}")
    print(f"status : {payload.get('status')}  ->  {rc.checkout_state(payload)}")
    print(f"amount : {json.dumps(payload.get('amount'))}")
    _show_hosted(rc, payload)

    state["checkout_id"] = payload.get("id")
    state["quote_id"] = args.quote_id
    state["checkout_status"] = payload.get("status")
    state["checkout_created_at"] = time.time()
    _save(args.state, state)
    print("\nNext: the buyer approves on the URL above, then `poll`. There are NO WEBHOOKS on\n"
          "agentic resources — polling is the only way the outcome is ever learned.")
    return 0


def cmd_poll(args) -> int:
    rc = _client()
    state = _load(args.state)
    enrollment_id = args.enrollment_id or state.get("enrollment_id")
    checkout_id = args.checkout_id or state.get("checkout_id")
    if not enrollment_id and not checkout_id:
        print("\nNothing to poll — no ids in the state file and none passed.")
        return 3

    changed = False
    if enrollment_id:
        result = asyncio.run(rc.get_enrollment(enrollment_id))
        print("\n--- ENROLLMENT " + "-" * 56)
        if not result.ok:
            _report_failure(rc, result)
        else:
            payload = result.data
            print(f"id     : {payload.get('id')}")
            print(f"status : {payload.get('status')}  ->  {rc.enrollment_state(payload)}")
            print(f"updated: {payload.get('updatedAt')}")
            if rc.enrollment_state(payload) == "unknown":
                print("\nSTATUS NOT RECOGNISED. Reap has added a status this client does not map.\n"
                      "It is deliberately not folded into active or dead — say so to a human and\n"
                      "update ENROLLMENT_STATES rather than guessing which it resembles.")
            if rc.enrollment_state(payload) == "pending":
                _show_hosted(rc, payload)
            state["enrollment_status"] = payload.get("status")
            changed = True

    if checkout_id:
        result = asyncio.run(rc.get_checkout(checkout_id))
        print("\n--- CHECKOUT " + "-" * 58)
        if not result.ok:
            _report_failure(rc, result)
        else:
            payload = result.data
            where = rc.checkout_state(payload)
            print(f"id     : {payload.get('id')}")
            print(f"status : {payload.get('status')}  ->  {where}")
            print(f"order  : {payload.get('orderId')}")
            print(f"final  : {json.dumps(payload.get('finalAmount'))}")
            print(f"updated: {payload.get('updatedAt')}")
            print(f"terminal: {rc.checkout_is_terminal(payload)}")
            if where == "processing":
                print("\nPROCESSING IS NOT COMPLETED. The buyer approved and Reap is placing the\n"
                      "order; an orderId appears only on COMPLETED. Reporting a purchase here\n"
                      "reports one that may still fail.")
            if where == "awaiting_buyer":
                _show_hosted(rc, payload)
            state["checkout_status"] = payload.get("status")
            state["order_id"] = payload.get("orderId")
            changed = True

    if changed:
        state["polled_at"] = time.time()
        _save(args.state, state)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", required=True,
                    help="JSON file carrying the ids between steps. Give it a path of YOUR OWN "
                         "— never a shared /tmp path.")
    ap.add_argument("--return-url", default=DEFAULT_RETURN_URL,
                    help="where the buyer's browser lands afterwards; must be https and on "
                         "REAP_RETURN_URL_HOSTS")
    sub = ap.add_subparsers(dest="command", required=True)

    enroll = sub.add_parser("enroll", help="create an EXTERNAL enrollment (hosted card entry)")
    enroll.add_argument("--owner-id", required=True,
                        help="OUR opaque client reference for this buyer. Not an email, not a "
                             "name — it goes to a third party and comes back in a query string.")
    enroll.add_argument("--email", help="optional, prefills the hosted page. Real buyer PII.")
    enroll.add_argument("--apply", action="store_true", help="actually create it")
    enroll.set_defaults(func=cmd_enroll)

    checkout = sub.add_parser("checkout", help="create a checkout against a quote + enrollment")
    checkout.add_argument("--quote-id", required=True)
    checkout.add_argument("--enrollment-id", help="defaults to the one in the state file")
    checkout.add_argument("--skip-enrollment-check", action="store_true",
                          help="do not read the enrollment first. You will get "
                               "ENROLLMENT_NOT_ACTIVE instead of a clear refusal.")
    checkout.add_argument("--apply", action="store_true", help="actually create it")
    checkout.set_defaults(func=cmd_checkout)

    poll = sub.add_parser("poll", help="read the enrollment and/or checkout. Read-only.")
    poll.add_argument("--enrollment-id")
    poll.add_argument("--checkout-id")
    poll.set_defaults(func=cmd_poll)

    args = ap.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
