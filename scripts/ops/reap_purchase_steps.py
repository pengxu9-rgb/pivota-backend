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
  undo that does not exist.

  `poll` needs no flag because it creates NOTHING AT THE PARTNER -- it is two GETs. It is not
  "read-only" without qualification, and the docstring used to say so: it rewrites the state
  file with the statuses it saw. Nothing there is destructive, but an operator who read
  "read-only" and pointed two polls at one state file would have been told something untrue.

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
    """Write the state file 0600, owner-only.

    It holds no credential and no card data, but it does hold a buyer's enrollment and checkout
    ids and our own client reference for them, and it is written by an operator on a shared box
    at whatever umask that box happens to have. `os.open` with the mode set is used rather than a
    `chmod` after the fact, so the file is never briefly world-readable between the two calls.
    """
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.chmod(path, 0o600)   # in case the file already existed with looser bits
    print(f"\nstate -> {path}")


# --- shared preamble ------------------------------------------------------------------------


def _checked_id(rc, value, *, what, uuid=False):
    """Validate an id the way the client will, but HERE, where the failure is a message.

    The client validates ids and raises ReapRequestError; this script called those functions
    without catching it, so `--attempt-id 'a/b'`, a 65-character attempt id, a malformed
    `--enrollment-id`, and a malformed id read out of the STATE FILE each ended in a traceback.
    A traceback is the wrong output for an operator error: the exception already carries a
    sentence written for a human, and a stack buries it.

    Returns the id, or exits 2 with that sentence. The try/except around each call stays as the
    backstop -- this is the one that fires first and says which flag was wrong.
    """
    try:
        return rc._path_id(value, what=what, uuid=uuid)
    except rc.ReapRequestError as exc:
        print(f"\nBAD {what.upper()} ID: {exc}")
        print("Nothing was sent.")
        raise SystemExit(2)


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
    """Everything we are willing to say about a failure. The body is NOT one of those things.

    This used to call `explain_refusal(..., detail_code=...)`, which takes no such argument --
    so EVERY failure path in this script raised TypeError, and the paths that report a failure
    are exactly the ones nobody exercises until something has already gone wrong. It then
    iterated the result, which is a string, and would have printed it one character per line.
    Two explainers, two calls, both printed as strings.
    """
    print(f"\nok     : {result.ok}")
    print(f"status : {result.status}")
    print(f"error  : {result.error}")
    print(f"code   : {result.error_code}        (error.code)")
    print(f"detail : {result.error_detail_code}  (error.detail.code)")
    text = rc.explain_refusal(result.error)
    if text:
        print("\n  " + text.replace("\n", "\n  "))
    # BOTH fields. Since the 2026-09-25 spec the 409 conflicts (QUOTE_EXPIRED,
    # ENROLLMENT_NOT_ACTIVE, IDEMPOTENCY_REQUEST_IN_PROGRESS) carry their code at `error.code`,
    # where the older 400s carried it at `error.detail.code`; asking for one of them is how the
    # explanation an operator needs goes unprinted.
    for code in (result.error_detail_code, result.error_code):
        detail = rc.explain_detail_code(code)
        if detail:
            print("\n  " + detail.replace("\n", "\n  "))
            break
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
    # VALIDATED BEFORE THE DRY RUN, not inside the --apply branch where it used to live. A dry
    # run exists to tell an operator their request is wrong before they send one; one that
    # prints a confident body and defers the id check to `--apply` tells them the opposite.
    attempt_id = args.attempt_id or f"ops-{int(time.time())}"
    attempt_id = _checked_id(rc, attempt_id, what="enrollment attempt")
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
        print(f"\nattempt id : {attempt_id}   (idempotency material; NOT sent in the body)")
        print("\nDRY RUN. Nothing was sent. Re-run with --apply to create this enrollment.")
        return 0

    print(f"\nattempt id : {attempt_id}   (idempotency material; NOT sent in the body)")
    print("A NEW attempt id means a NEW enrollment. Reuse one only to retry an attempt whose\n"
          "outcome you never saw -- the key is not time-bucketed, so reusing it tomorrow would\n"
          "replay today's enrollment and its long-expired hosted link.")
    result = asyncio.run(rc.create_enrollment(
        owner_id=args.owner_id, return_url=args.return_url, email=args.email,
        attempt_id=attempt_id,
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
    state["attempt_id"] = attempt_id
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
    enrollment_id = _checked_id(rc, enrollment_id, what="enrollment", uuid=True)
    quote_id = _checked_id(rc, args.quote_id, what="quote")

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
            quote_id=quote_id, enrollment_id=enrollment_id, return_url=args.return_url
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
        quote_id=quote_id, enrollment_id=enrollment_id, return_url=args.return_url
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
    state["quote_id"] = quote_id
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
    # Checked even when they came from the STATE FILE: that file is edited by hand between runs,
    # and "it was in the json" is not provenance.
    if enrollment_id:
        enrollment_id = _checked_id(rc, enrollment_id, what="enrollment", uuid=True)
    if checkout_id:
        checkout_id = _checked_id(rc, checkout_id, what="checkout")

    changed = False
    failed = False
    if enrollment_id:
        result = asyncio.run(rc.get_enrollment(enrollment_id))
        print("\n--- ENROLLMENT " + "-" * 56)
        if not result.ok:
            _report_failure(rc, result)
            failed = True
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
            failed = True
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
    # N4. `poll` used to exit 0 on EVERY failure -- a 400 ENROLLMENT_NOT_ACTIVE and a 500 both
    # printed their explanation and then reported success. That is the shape that makes a poll
    # loop in a shell script spin forever on a dead enrollment, and it disagreed with `enroll`
    # and `checkout`, which both return 4. The explanation is still printed; the exit code now
    # matches it.
    return 4 if failed else 0


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
    enroll.add_argument("--email", required=True,
                        help="REQUIRED by Reap's spec since 2026-09-25 (owner.email). Real buyer "
                             "PII; the builder refuses an empty or malformed one before egress.")
    enroll.add_argument("--attempt-id",
                        help="opaque id for THIS attempt ([A-Za-z0-9_-]{1,64}); in production "
                             "our ledger's enrollment row id. Defaults to a timestamp, which is "
                             "right for a probe and wrong for anything that needs to retry.")
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

    poll = sub.add_parser("poll", help="read the enrollment and/or checkout; creates nothing at "
                                       "the partner, but does update the state file")
    poll.add_argument("--enrollment-id")
    poll.add_argument("--checkout-id")
    poll.set_defaults(func=cmd_poll)

    args = ap.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
