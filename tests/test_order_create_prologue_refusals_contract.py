"""The order-create refusals that are answered before any order can exist: a contract with the gateway.

The agent gateway holds a guard on a quote while it creates the order, and gives the guard back only
when the backend answers one of ten exact (status, code) pairs. That is safe only while each pair
is answered before any order row of the request can be written, and while nothing that can run
after an order row is written answers one of them. Nine are answered from the request's PROLOGUE,
strictly before the order write step (`create_new_order`) is first entered. One,
MERCHANT_NOT_ACCEPTING_ORDERS, is raised inside the write step's function, above its order write
(`create_order`), and the write step's in-request busy retry can never reach it after a row exists.
The list lives in contracts/order_create_prologue_refusals.json; the gateway vendors it.

    C-0  the list itself (ten pairs, sha256 pinned, no 503, no 429, none of this change's new refusals)
    C-1  where each listed code is written in this repository (an AST inventory, pinned), that the
         write step and everything it calls writes none of them but the merchant refusal, that the
         merchant refusal sits above the order write and after the quote's claim is read, that every
         call on the way from each route to it, and in every dependency of those functions, is a
         known read-only call (an allowlist), and that each create answers an order it already made,
         and a quote that already holds its order, before any refusal from a live read
    C-2  each pair, through the real route and the real error envelope, with the order write entered
         0 times (and, for the nine prologue pairs, the write step entered 0 times)
    C-3  an order row written, then every failure that can follow it: never a listed pair; the busy
         retry, a resend after the order while a live read now refuses it, and a create for a quote
         that holds its order outside the first create's replay scope
    C-4  the error envelope the gateway reads the code from
    C-5  the partner assertion is refused by the route's dependency, before the route body, and says
         why (a replayed assertion says `replay`)

The harness is tests/order_create_one_per_quote_cases.py (real routes, real order rows on SQLite).
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import secrets
import subprocess
import time
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from order_create_one_per_quote_cases import (  # noqa: F401
    AGENT_A,
    AGENT_B,
    BUSY,
    MERCHANT,
    Answer,
    World,
    _order_data,
    shipping_address,
    world,
)

REPO = Path(__file__).resolve().parents[1]
CONTRACT_PATH = REPO / "contracts" / "order_create_prologue_refusals.json"
NOTES_PATH = REPO / "contracts" / "order_create_prologue_refusals_notes.json"
ENVELOPE_PATH = REPO / "contracts" / "error_envelope_status_codes.json"

# The list file is BYTE-IDENTICAL to the gateway's copy: the gateway pins the same sha256. A change
# to it is a change to what the gateway may do with money and must land in both repositories. The
# notes file is this repository's own; the envelope table is vendored by the gateway's test fixture.
CONTRACT_SHA256 = "221f4bb28485caea22e7c0a84ae0c55e4f3ef644fca85a63ed3342adb80706a3"
NOTES_SHA256 = "b26315e9541eb2fffa97a05133b2f0038dc7c7d72731087067090a86b16ed27d"
ENVELOPE_SHA256 = "74e2875f61e8c3a96ba7a4a768731c7052d346d0ee3faec10c7c8954fdd1a3f5"

#: THE list. Every case below reads the pairs from the file; none is written out a second time.
CONTRACT_PAIRS: List[List[Any]] = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
NOTES = json.loads(NOTES_PATH.read_text(encoding="utf-8"))
ASSERTION_NOTES = NOTES["PARTNER_ASSERTION_REJECTED"]
ENVELOPE = json.loads(ENVELOPE_PATH.read_text(encoding="utf-8"))
LISTED: Set[Tuple[int, str]] = {(status, code) for status, code in CONTRACT_PAIRS}
LISTED_CODES: Set[str] = {code for _, code in LISTED}
CAPITAL_SNAKE = re.compile(r"^[A-Z][A-Z0-9_]+$")
GATEWAY = "agent_oopq_gateway"
ASSERTION_SECRET = "oopq_fixture_assertion_secret"


def pairs_of(answer: Answer) -> Set[Tuple[int, Any]]:
    """Every (status, code) the gateway could read from this answer: `error.details.error`, then
    `error.message`, then `error.code`."""
    err = answer.body.get("error") if isinstance(answer.body.get("error"), dict) else {}
    details = err.get("details") if isinstance(err.get("details"), dict) else {}
    return {(answer.status, details.get("error")), (answer.status, err.get("message")), (answer.status, err.get("code"))}


def assert_not_listed(answer: Answer) -> None:
    assert not (pairs_of(answer) & LISTED), (answer.status, answer.body)


# ---------------------------------------------------------------------------
# C-0: the list
# ---------------------------------------------------------------------------


def test_c0_the_list_file_is_byte_identical_to_the_gateways_copy() -> None:
    raw = CONTRACT_PATH.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == CONTRACT_SHA256, (
        "contracts/order_create_prologue_refusals.json no longer has the bytes the gateway pins. "
        "Change it only together with the gateway's copy, and update CONTRACT_SHA256 on both sides."
    )
    assert len(raw) == 347 and b"\r" not in raw and raw.endswith(b"]\n") and not raw.endswith(b"\n\n")
    assert hashlib.sha256(NOTES_PATH.read_bytes()).hexdigest() == NOTES_SHA256
    assert hashlib.sha256(ENVELOPE_PATH.read_bytes()).hexdigest() == ENVELOPE_SHA256, (
        "contracts/error_envelope_status_codes.json changed: update ENVELOPE_SHA256 and re-vendor it in the gateway"
    )


def test_c0_the_list_is_ten_distinct_pairs_sorted_by_status_then_code() -> None:
    assert len(CONTRACT_PAIRS) == 10 == len(LISTED)
    assert all(
        isinstance(pair, list) and len(pair) == 2 and type(pair[0]) is int and isinstance(pair[1], str)
        and CAPITAL_SNAKE.match(pair[1])
        for pair in CONTRACT_PAIRS
    )
    assert CONTRACT_PAIRS == sorted(CONTRACT_PAIRS)
    assert {status for status, _ in LISTED} == {400, 403, 404, 409}
    assert len(LISTED_CODES) == 10, "a code listed at two statuses: the inventory below reads codes, not pairs"


def test_c0_no_case_in_this_file_writes_the_list_out_a_second_time() -> None:
    """The pairs are read from the file. A listed code may be named in a case (the case that
    triggers it), but never next to its status as a literal pair, and LISTED is built from the file."""
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Tuple, ast.List)) and len(node.elts) == 2:
            first, second = node.elts
            if (
                isinstance(first, ast.Constant) and type(first.value) is int
                and isinstance(second, ast.Constant) and second.value in LISTED_CODES
                and (first.value, second.value) in LISTED
            ):
                # Allowed only as a membership question about LISTED (`(404, "X") in LISTED`).
                parent_ok = any(
                    isinstance(cmp, ast.Compare) and node is cmp.left
                    and isinstance(cmp.comparators[0], ast.Name) and cmp.comparators[0].id == "LISTED"
                    for cmp in ast.walk(tree)
                )
                assert parent_ok, f"a listed pair written out at line {node.lineno}"
    assigns = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == "LISTED"
    ]
    assert len(assigns) == 1 and "CONTRACT_PAIRS" in ast.unparse(assigns[0].value)


def test_c0_the_notes_say_where_the_code_and_the_reason_are() -> None:
    assert NOTES["envelope"]["code"] == "error.details.error"
    assert NOTES["envelope"]["reason"] == "error.details.reason"
    # The three a caller must keep its guard on, and the eight that say nothing was accepted.
    assert ASSERTION_NOTES["can_follow_an_accepted_earlier_copy"] == ["replay", "stale", "subject"]
    assert sorted(set(ASSERTION_NOTES["reasons"]) - set(ASSERTION_NOTES["can_follow_an_accepted_earlier_copy"])) == [
        "bad_mac", "body", "malformed", "method", "no_secret", "op", "path", "resource",
    ]
    assert ASSERTION_NOTES["only_across_a_change_of_the_signing_secret"] == ["bad_mac", "no_secret"]
    assert sorted(NOTES["MERCHANT_NOT_ACCEPTING_ORDERS"]) == ["after_a_written_row", "residual_race", "where"]
    assert "PREFLIGHT_FAILED" in NOTES["MERCHANT_NOT_ACCEPTING_ORDERS"]["residual_race"]
    assert "per-merchant lock" in NOTES["MERCHANT_NOT_ACCEPTING_ORDERS"]["residual_race"]
    assert "claim" in NOTES["MERCHANT_NOT_ACCEPTING_ORDERS"]["after_a_written_row"]
    from utils.transient_errors import COMMERCE_PATH_UNKNOWN

    assert COMMERCE_PATH_UNKNOWN in NOTES["UNSUPPORTED_COMMERCE_PATH"]
    assert "Residual race" in NOTES["UNSUPPORTED_COMMERCE_PATH"] and "PREFLIGHT_FAILED" in NOTES["UNSUPPORTED_COMMERCE_PATH"]
    assert "before the first copy's order row is written" in NOTES["MERCHANT_NOT_ACCEPTING_ORDERS"]["residual_race"]
    assert "reads as no holder" in NOTES["MERCHANT_NOT_ACCEPTING_ORDERS"]["residual_race"]
    assert COMMERCE_PATH_UNKNOWN in NOTES["not_listed_on_purpose"]["503 TEMPORARY_UNAVAILABLE"]
    rate = NOTES["not_listed_on_purpose"]["429"]
    assert "on both routes" in rate and "blocked or suspended" in rate and "403 (unlisted)" in rate
    assert "/agent/v1/orders/create" not in rate
    assert sorted(NOTES["not_listed_on_purpose"]) == ["409 QUOTE_CONSUMED", "429", "503 TEMPORARY_UNAVAILABLE"]


def test_c0_no_503_and_none_of_the_could_not_tell_refusals_is_listed() -> None:
    from utils.transient_errors import (
        ORDER_QUOTE_GUARD_UNAVAILABLE,
        ORDER_REPLAY_LOOKUP_UNAVAILABLE,
        db_busy_http_exception,
        order_existence_unknown_http_exception_fields,
    )

    assert not any(status >= 500 for status, _ in LISTED)
    for reason in (ORDER_REPLAY_LOOKUP_UNAVAILABLE, ORDER_QUOTE_GUARD_UNAVAILABLE):
        fields = order_existence_unknown_http_exception_fields(reason)
        assert (fields["status_code"], fields["detail"]["error"]) == (503, "TEMPORARY_UNAVAILABLE")
        assert (fields["status_code"], fields["detail"]["error"]) not in LISTED
        assert reason not in LISTED_CODES and fields["detail"]["reason"] == reason
    busy = db_busy_http_exception()
    assert (busy.status_code, busy.detail["error"]) not in LISTED
    from utils.transient_errors import COMMERCE_PATH_UNKNOWN, commerce_path_unknown_http_exception_fields

    unknown = commerce_path_unknown_http_exception_fields()
    assert (unknown["status_code"], unknown["detail"]["error"]) == (503, "TEMPORARY_UNAVAILABLE")
    assert unknown["detail"]["reason"] == COMMERCE_PATH_UNKNOWN not in LISTED_CODES
    assert (409, "QUOTE_CONSUMED") not in LISTED and "QUOTE_CONSUMED" not in LISTED_CODES


def test_c0_no_429_is_listed() -> None:
    """A resend of an accepted create can be answered 429 while its order exists (the cases at the
    end of this file), so no 429 is on the list, under any code."""
    assert not any(status == 429 for status, _ in LISTED)
    assert not ({"RATE_LIMITED", "RATE_LIMIT_EXCEEDED", "rate_limit_exceeded"} & LISTED_CODES)
    assert ENVELOPE["status_code"]["429"][0] not in LISTED_CODES


# ---------------------------------------------------------------------------
# C-1: where the codes are written (AST, not grep)
# ---------------------------------------------------------------------------


def _source_files() -> List[str]:
    tracked = subprocess.run(["git", "ls-files", "*.py"], cwd=REPO, capture_output=True, text=True, check=True).stdout.split()
    return sorted(
        f for f in tracked if not (f.startswith("tests/") or "/tests/" in f or Path(f).name.startswith("test_"))
    )


_TREES: Dict[str, ast.AST] = {}


def _tree(path: str) -> ast.AST:
    if path not in _TREES:
        _TREES[path] = ast.parse((REPO / path).read_text(encoding="utf-8"), filename=path)
    return _TREES[path]


def _scoped_constants(path: str) -> Iterator[Tuple[str, str]]:
    """(scope, string constant) for every string constant in the file. The scope is the dotted
    enclosing function/class, or `:NAME` for a module-level assignment to NAME, or `<module>`."""

    def walk(node: ast.AST, scope: List[str]) -> Iterator[Tuple[str, str]]:
        for child in ast.iter_child_nodes(node):
            inner = scope
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = scope + [child.name]
            elif isinstance(child, (ast.Assign, ast.AnnAssign)) and not scope:
                target = child.targets[0] if isinstance(child, ast.Assign) else child.target
                inner = [":" + (target.id if isinstance(target, ast.Name) else "?")]
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                yield (".".join(scope) or "<module>", child.value)
            yield from walk(child, inner)

    yield from walk(_tree(path), [])


#: Every place a listed code is written as a string constant, outside tests.
#: ON the order-create path of POST /agent/v2/orders (the prologue, the route dependency, and the
#: merchant refusal inside the write step's function, above its order write):
ON_PATH = {
    ("routes/order_routes.py", "create_new_order", "MERCHANT_NOT_ACCEPTING_ORDERS"),
    ("routes/agent_forwarded_owner.py", ":REJECTED_CODE", "PARTNER_ASSERTION_REJECTED"),
    ("services/quote_service.py", ":QUOTE_NOT_FOUND_DETAIL", "QUOTE_NOT_FOUND"),
    # The quote re-read. A QuoteError: every place that answers it writes 409 or 400, never 404.
    ("services/quote_service.py", "QuoteService.load_active_quote_or_raise", "QUOTE_NOT_FOUND"),
    ("routes/agent_v2.py", "_coerce_shipping_address", "INVALID_BUYER_CONTEXT"),
    ("routes/agent_v2.py", "create_order_v2", "INVALID_BUYER_CONTEXT"),
    ("routes/agent_v2.py", "create_order_v2", "INVALID_QUOTE"),
    ("routes/agent_api.py", "agent_create_order", "PREFLIGHT_FAILED"),
    ("routes/agent_api.py", "agent_create_order", "QUOTE_MERCHANT_MISMATCH"),
    ("routes/agent_api.py", "agent_create_order", "QUOTE_REQUIRED_BEFORE_PURCHASE"),
    ("routes/agent_api.py", "agent_create_order", "UNSUPPORTED_COMMERCE_PATH"),
    ("routes/agent_api.py", "agent_create_order", "VARIANT_REQUIRED"),
}
#: OFF that path: other routes, which the order create never calls.
OFF_PATH = {
    ("routes/agent_checkout_intents.py", "create_checkout_intent", "QUOTE_MERCHANT_MISMATCH"),
    ("routes/agent_checkout_intents.py", "create_checkout_intent", "QUOTE_REQUIRED_BEFORE_PURCHASE"),
    ("routes/agent_checkout_intents.py", "create_checkout_intent", "UNSUPPORTED_COMMERCE_PATH"),
    ("routes/agent_payment_sdk.py", "create_payment", "UNSUPPORTED_COMMERCE_PATH"),
    ("routes/agent_routes.py", ":_DEPRECATION_DETAIL", "QUOTE_REQUIRED_BEFORE_PURCHASE"),
    ("routes/offer_routes.py", "preflight_quote_offer", "PREFLIGHT_FAILED"),
    ("routes/offer_routes.py", "preflight_quote_offer", "QUOTE_MERCHANT_MISMATCH"),
}


def test_c1_every_place_a_listed_code_is_written_is_known() -> None:
    found = {
        (path, scope, value)
        for path in _source_files()
        for scope, value in _scoped_constants(path)
        if value in LISTED_CODES
    }
    assert found == ON_PATH | OFF_PATH, (
        "a listed code is written somewhere new (or no longer where it was). Before changing this "
        "set, show that the new place cannot answer the listed status after an order row exists.\n"
        f"new: {sorted(found - (ON_PATH | OFF_PATH))}\ngone: {sorted((ON_PATH | OFF_PATH) - found)}"
    )


def _first_call_line(fn: ast.AST, callee: str) -> int:
    lines = [
        call.lineno
        for call in ast.walk(fn)
        if isinstance(call, ast.Call)
        and (getattr(call.func, "id", None) == callee or getattr(call.func, "attr", None) == callee)
    ]
    assert lines, f"{callee} is no longer called here"
    return min(lines)


def listed_codes_at_or_after_the_write_step(source: str, function: str, callee: Optional[str]) -> List[Tuple[int, str]]:
    """(line, code) of every listed-code constant in `function` that is not lexically ABOVE the
    function's first call of `callee` (the order write step, or the call that leads to it). With
    `callee` None the whole function runs after an order can exist: every constant counts."""
    tree = ast.parse(source)
    fn = next(
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function
    )
    first = fn.lineno if callee is None else _first_call_line(fn, callee)
    return sorted(
        (const.lineno, const.value)
        for const in ast.walk(fn)
        if isinstance(const, ast.Constant) and isinstance(const.value, str) and const.value in LISTED_CODES
        and const.lineno >= first
    )


#: (file, the function that writes listed codes, the call after which an order row can exist)
_PROLOGUE_FUNCTIONS = [
    ("routes/agent_api.py", "agent_create_order", "create_new_order"),
    ("routes/agent_v2.py", "create_order_v2", "agent_v1_create_order"),
    # The write step itself: its merchant refusal sits above its order write (the claim and the row).
    ("routes/order_routes.py", "create_new_order", "create_order"),
]


@pytest.mark.parametrize("path, function, callee", _PROLOGUE_FUNCTIONS, ids=[f[1] for f in _PROLOGUE_FUNCTIONS])
def test_c1_every_listed_code_sits_above_the_first_call_of_the_write_step(path: str, function: str, callee: str) -> None:
    """The inventory above knows WHICH function writes a listed code, not WHERE in it. These
    functions hold both what runs before an order row can exist and everything after it is written,
    so each listed constant must also sit above the function's first call of the write step (for
    `create_new_order`, of the order write)."""
    source = (REPO / path).read_text(encoding="utf-8")
    assert listed_codes_at_or_after_the_write_step(source, function, callee) == []
    # ... and the function does write listed codes above it (the check is looking at something).
    fn = next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.AsyncFunctionDef) and n.name == function)
    above = [c for c in ast.walk(fn) if isinstance(c, ast.Constant) and c.value in LISTED_CODES]
    assert len(above) >= 2


@pytest.mark.parametrize(
    "path, function, callee, anchor",
    [
        # In the post-create tail, inside one of its `try ... except Exception: pass` blocks.
        ("routes/agent_api.py", "agent_create_order", "create_new_order",
         "        _post_create_new_order_tail_started = time.perf_counter()\n"),
        # Right after the create returned, in the v2 route.
        # v2's answer for an order that exists (made or replayed), at its first statement.
        ("routes/agent_v2.py", "_order_created_answer", None,
         '    order_id = str((response or {}).get("order_id") or "").strip()\n'),
        # Right after the order write, inside the write step.
        ("routes/order_routes.py", "create_new_order", "create_order",
         "        order_id = await create_order(order_data)\n"),
    ],
    ids=["agent_create_order_tail", "v2_answer_for_an_existing_order", "create_new_order_after_the_write"],
)
@pytest.mark.parametrize("pair", sorted(LISTED), ids=lambda pair: f"{pair[0]}_{pair[1]}")
def test_c1_a_listed_pair_raised_after_the_write_step_is_caught(
    path: str, function: str, callee: Optional[str], anchor: str, pair: Tuple[int, str]
) -> None:
    """The mutant, applied to the source text in memory: a listed pair raised below the first call
    of the write step, in a function the inventory already allows to write that code."""
    status, code = pair
    source = (REPO / path).read_text(encoding="utf-8")
    assert source.count(anchor) == 1
    indent = anchor[: len(anchor) - len(anchor.lstrip())]
    planted = (
        f"{indent}if order_request_is_odd:\n"
        f'{indent}    raise HTTPException(status_code={status}, detail={{"error": "{code}", "message": "planted"}})\n'
    )
    mutated = source.replace(anchor, anchor + planted)

    found = listed_codes_at_or_after_the_write_step(mutated, function, callee)

    assert [c for _, c in found] == [code]


#: Every call in the write step that writes the order or what hangs off it (the claim and the row,
#: their updates, the order's events and edges, the merchant's sync job). Matched by the callee's
#: name; `orders_table` (the orders table itself) is matched wherever it is read.
_ORDER_WRITE_CALLEES = {
    "create_order", "update_order_status", "update_payment_info", "mark_order_paid", "update_fulfillment_info",
    "mark_order_shipped", "update_order_row", "log_order_event", "_bg_log_order_event",
    "upsert_order_attribution_edge", "enqueue_merchant_order_create",
}


def _callee(call: ast.Call) -> Optional[str]:
    func = call.func
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None


def _merchant_refusal_raises(fn: ast.AST) -> List[ast.Raise]:
    """`raise HTTPException(status_code=403, detail={"error": MERCHANT_NOT_ACCEPTING_ORDERS, ...})`."""
    out = []
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) and _callee(node.exc) == "HTTPException"):
            continue
        kws = {kw.arg: kw.value for kw in node.exc.keywords}
        detail = kws.get("detail")
        if (
            isinstance(kws.get("status_code"), ast.Constant) and kws["status_code"].value == 403
            and isinstance(detail, ast.Dict)
            and any(isinstance(k, ast.Constant) and k.value == "error" and isinstance(v, ast.Constant) and v.value == "MERCHANT_NOT_ACCEPTING_ORDERS"
                    for k, v in zip(detail.keys, detail.values))
        ):
            out.append(node)
    return out


def _order_writes(fn: ast.AST) -> List[ast.AST]:
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and _callee(n) in _ORDER_WRITE_CALLEES]
    tables = [n for n in ast.walk(fn) if isinstance(n, ast.Name) and n.id == "orders_table"]
    return calls + tables


def _position(node: ast.AST) -> Tuple[int, int]:
    return (node.lineno, node.col_offset)


def merchant_refusals_not_above_the_first_order_write(source: str) -> List[str]:
    """What is wrong with the write step's merchant refusal, found by the AST: every place that
    writes MERCHANT_NOT_ACCEPTING_ORDERS is one of its 403 raises, and each raise ends before the
    first statement of the function that writes the order (or anything hung off it)."""
    fn = next(
        n for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "create_new_order"
    )
    raises = _merchant_refusal_raises(fn)
    writes = _order_writes(fn)
    problems: List[str] = []
    if not writes:
        return ["no order write found"]
    first_write = min(writes, key=_position)
    if not (isinstance(first_write, ast.Call) and _callee(first_write) == "create_order"):
        problems.append(f"the first order write is no longer create_order: {ast.unparse(first_write)[:80]}")
    for node in raises:
        if (node.end_lineno, node.end_col_offset) >= _position(first_write):
            problems.append(f"a merchant refusal at or below the first order write: line {node.lineno}")
    inside = {id(c) for r in raises for c in ast.walk(r)}
    for const in ast.walk(fn):
        if isinstance(const, ast.Constant) and const.value == "MERCHANT_NOT_ACCEPTING_ORDERS" and id(const) not in inside:
            problems.append(f"MERCHANT_NOT_ACCEPTING_ORDERS written outside a 403 raise: line {const.lineno}")
    return problems


def test_c1_both_merchant_refusals_are_raised_above_the_first_order_write() -> None:
    source = (REPO / "routes/order_routes.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.AsyncFunctionDef) and n.name == "create_new_order")
    assert len(_merchant_refusal_raises(fn)) == 2, "the write step's merchant refusal is raised at two places"
    assert merchant_refusals_not_above_the_first_order_write(source) == []


def _create_new_order_node(tree: ast.AST) -> ast.AsyncFunctionDef:
    return next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "create_new_order")


def _quote_claim_read_comes_first(source: str) -> List[str]:
    """The write step reads the quote's claim, and answers a quote that holds an order with
    OrderForQuoteExists, before either merchant refusal can be raised."""
    fn = _create_new_order_node(ast.parse(source))
    reads = [c for c in ast.walk(fn) if isinstance(c, ast.Call) and _callee(c) == "read_quote_claim_holder"]
    raises = _merchant_refusal_raises(fn)
    if len(reads) != 1 or not raises:
        return [f"claim reads {len(reads)}, merchant refusals {len(raises)}"]
    first_refusal = min(_position(r) for r in raises)
    holds = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call) and _callee(n.exc) == "OrderForQuoteExists"
        and _position(reads[0]) < _position(n) < first_refusal
    ]
    if len(holds) != 1:
        return ["the quote's claim is not read and answered before the merchant refusal"]
    return []


#: The route bodies on the way to the write step's merchant refusal, each up to the call that
#: leads on (for the write step itself: up to its second merchant refusal).
_PATH_TO_THE_MERCHANT_REFUSAL = [
    ("routes/agent_v2.py", "create_order_v2", "agent_v1_create_order"),
    ("routes/agent_api.py", "agent_create_order", "create_new_order"),
    ("routes/order_routes.py", "create_new_order", None),
]

_PURE = "in memory, no I/O"
_REFUSAL = "builds the refusal it raises"
_QUOTE_READ = "reads the quote (an expired quote is marked expired); writes no order"
#: EVERY call on that way, by the name it is called with (`<expr>.x` is a method of a computed
#: value). Each writes no order, no quote claim, no payment and no order event or edge; the ones
#: that write anything else say what. A call not named here fails C-1: add it only after reading it.
_CALLS_ON_THE_PATH: Dict[str, str] = {
    **{name: _PURE for name in (
        "any", "bool", "dict", "float", "getattr", "hasattr", "int", "isinstance", "len", "max", "str", "sum", "vars",
        "Decimal", "SimpleNamespace", "PolicyInput", "CreateOrderRequest", "QuoteService", "PaymentRoutingService",
        "<expr>.get", "<expr>.join", "<expr>.lower", "<expr>.strip", "<expr>.upper", "<expr>.timestamp",
        "<expr>.total_seconds", "agent_v2_meta.update", "body.request_context.model_dump", "brief_meta.get",
        "commerce_policy.as_dict", "commerce_policy.response_fields", "context.can_access_merchant",
        "create_new_order_params.values", "detail.update", "drift_fields.append", "idempotency_cache_value.get",
        "inventory_info.get", "li.get", "merchant.get", "meta.get", "metadata.get", "metadata.pop", "mvp_meta.update",
        "o.model_dump", "order_request.metadata.get", "order_request.metadata.pop", "order_request.metadata.setdefault",
        "order_request.metadata.update",
        "order_request.model_copy", "order_request.shipping_address.model_dump", "order_request_normalized.get", "p.get",
        "p.model_dump", "pricing.get", "pricing_quote_meta.get", "psp_type.lower", "quote.expires_at.isoformat",
        "quote_request_json.get", "quote_request_normalized.get", "replacement_quote.get", "replay_response.get",
        "request_context_meta.get", "request_json.get", "smeta.get", "snap.get", "snap_for_policy.get", "snap_pricing.get",
        "token_payload.get",
        "_build_quote_drift_normalized_request", "_coerce_shipping_address", "_coerce_utc_datetime",
        "_decision_layer_dict", "_default_order_create_idempotency_key", "_extract_checkout_decision_id_from_metadata",
        "_extract_decision_id_from_metadata", "_fresh_quote_validate_skip_seconds", "_is_checkout_ui_order_create",
        "_normalize_buyer_ref", "_normalize_order_provider_hint", "_order_create_replay_owner",
        "_order_defers_payment_surface", "_order_items_from_quote_request", "_order_live_quote_revalidation_enabled",
        "_pricing_quote_has_unverified_shipping", "_quote_order_items_unchanged", "_shopify_discount_reconciliation_mode",
        "attach_traffic_taxonomy", "build_offers_from_quote", "build_traffic_taxonomy", "compute_request_fingerprint",
        "governance.evaluate", "normalize_discount_codes", "parse_decimal_money", "partner_identity_metadata",
        "preflight_offers", "resolve_commerce_execution_policy",
    )},
    **{name: _REFUSAL for name in (
        "HTTPException", "QuoteError", "OrderForQuoteExists", "commerce_path_unknown_http_exception_fields",
    )},
    **{name: "logging, a clock, an id or the environment" for name in (
        "logger.info", "logger.warning", "logger.error", "time.perf_counter", "time.time", "datetime.utcnow",
        "datetime.now", "uuid.uuid4", "os.getenv", "inspect.signature", "partner_identity_forwarding_enabled",
    )},
    "asyncio.gather": "runs the calls passed to it, each of which is checked here",
    "_timed_get_primary_store_for_policy": "a local wrapper of get_primary_store; its body is checked here",
    "_timed_idempotency_get": "a local wrapper of _get_cached_agent_order_create_response; checked here",
    "_timed_load_replayable_agent_order_create_response": "a local wrapper of the replay lookup; checked here",
    "_set_request_taxonomy_state": "sets attributes on request.state",
    "_get_order_create_lock": "the in-process per-merchant lock",
    "validate_order_create_governance": "governance: reads the agent's policy (blocked, suspended), counts the request in memory",
    "emit_best_effort": "a best-effort telemetry event; no order",
    "require_quote_for_caller": "reads the quote's owner",
    "should_require_quote_for_order_create": "reads the merchant's quote-first setting",
    "get_primary_store": "reads the merchant's store",
    "get_latest_policy_hashes": "reads the merchant's policy hashes",
    "_get_cached_agent_order_create_response": "reads the response cache",
    "_load_replayable_agent_order_create_response": "the replay lookup: reads the orders table",
    "_cache_agent_order_create_response_best_effort": "writes the response cache for an order that EXISTS, then returns it",
    "ensure_database_ready": "probes the database connection",
    "get_merchant_onboarding": "reads the merchant",
    "check_inventory_availability": "reads stock",
    "read_quote_claim_holder": "reads the quote's claim and its order row",
    "answer_for_a_quote_that_holds_its_order": "the quote's order inside this caller's replay scope (the replay lookup; the cache write for that existing order) or the 409",
    "answer_for_a_create_whose_quote_holds_its_order": "reads the quote's claim, then as answer_for_a_quote_that_holds_its_order",
    "_order_created_answer": "reads the order row an answer names, and builds the v2 answer",
    "held_quote_response.get": _PURE,
    "type": _PURE,
    "_resolve_active_order_psp": "reads the merchant's payment provider",
    "routing_service.select_psp": "reads the merchant's payment routing (may create its default route row); no order",
    "service.load_active_quote_or_raise": _QUOTE_READ,
    "quote_service.load_active_quote_or_raise": _QUOTE_READ,
    "<expr>.load_active_quote_or_raise": _QUOTE_READ,
    "quote_service.validate_quote_snapshot_live": "re-prices the quote live (may write a replacement QUOTE); no order",
}

#: Names that never appear on that way, called or not (passed to a scheduler, aliased, ...).
_NEVER_ON_THE_PATH = {
    "create_order", "_claim_quote_for_order", "orders_table", "order_quote_claims", "database", "execute",
    "insert", "add_task", "create_task", "ensure_future",
}


def _call_key(call: ast.Call) -> str:
    func, parts = call.func, []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    parts.append(func.id if isinstance(func, ast.Name) else "<expr>")
    return ".".join(reversed(parts))


def _path_region(fn: ast.AST, stop: Optional[str]) -> Tuple[List[ast.AST], Tuple[int, int]]:
    """Every node of the function's BODY (not its decorators or defaults) that comes before the
    first call of `stop`, or (stop None, the write step) before the end of its last merchant refusal."""
    if stop is None:
        raises = _merchant_refusal_raises(fn)
        assert raises, "the write step has no merchant refusal"
        end = max((r.end_lineno, r.end_col_offset) for r in raises)
    else:
        calls = [c for c in ast.walk(fn) if isinstance(c, ast.Call) and _callee(c) == stop]
        assert calls, f"{stop} is no longer called"
        end = min(_position(c) for c in calls)
    nodes = [n for stmt in fn.body for n in ast.walk(stmt) if hasattr(n, "lineno") and _position(n) < end]
    return nodes, end


def _module_path(module: str) -> Optional[str]:
    path = module.replace(".", "/") + ".py"
    return path if (REPO / path).exists() else None


def _resolve_function(path: str, name: str, sources: Dict[str, str]) -> Optional[Tuple[str, ast.AST]]:
    """The def `name` refers to in module `path`: defined there, or imported into it by name."""
    tree = ast.parse(sources[path]) if path in sources else _tree(path)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return path, node
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if (alias.asname or alias.name) == name:
                    target = _module_path(node.module)
                    return _resolve_function(target, alias.name, sources) if target else None
    return None


#: FastAPI security schemes: they read a request header and nothing else.
_HEADER_SCHEMES = {"APIKeyHeader", "HTTPBearer"}


def _is_header_scheme(path: str, name: str) -> bool:
    for node in _tree(path).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return isinstance(node.value, ast.Call) and _call_key(node.value) in _HEADER_SCHEMES
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.ImportFrom) and node.module and any((a.asname or a.name) == name for a in node.names):
            target = _module_path(node.module)
            return bool(target) and _is_header_scheme(target, name)
    return False


def _dependency_targets(fn: ast.AST) -> List[ast.AST]:
    args = fn.args
    defaults = list(args.defaults) + [d for d in args.kw_defaults if d is not None]
    return [
        d.args[0] for d in defaults
        if isinstance(d, ast.Call) and _call_key(d) in ("Depends", "Security") and d.args
    ]


def route_dependencies(sources: Dict[str, str]) -> Tuple[List[Tuple[str, str, List[ast.AST]]], List[str]]:
    """Every dependency the three path functions declare, and theirs in turn (a factory's whole
    body, nested dependency included): (file, name, the nodes of its body), and what could not be read."""
    out: List[Tuple[str, str, List[ast.AST]]] = []
    problems: List[str] = []
    seen: Set[Tuple[str, str]] = set()
    queue: List[Tuple[str, ast.AST]] = []
    for path, function, _ in _PATH_TO_THE_MERCHANT_REFUSAL:
        fn = next(
            n for n in ast.walk(ast.parse(sources[path]))
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == function
        )
        queue.append((path, fn))
    while queue:
        path, fn = queue.pop()
        holders = [fn] + [n for n in ast.walk(fn) if n is not fn and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for holder in holders:
            for target in _dependency_targets(holder):
                if isinstance(target, ast.Lambda):
                    out.append((path, f"lambda@{target.lineno}", list(ast.walk(target.body))))
                    continue
                name = (
                    target.id if isinstance(target, ast.Name)
                    else target.attr if isinstance(target, ast.Attribute)
                    else _call_key(target).split(".")[-1] if isinstance(target, ast.Call) else None
                )
                if name and isinstance(target, ast.Name) and _is_header_scheme(path, name):
                    continue
                found = _resolve_function(path, name, sources) if name else None
                if not found:
                    problems.append(f"{path}: a dependency that cannot be read: {ast.unparse(target)[:60]}")
                    continue
                dep_path, dep = found
                if (dep_path, dep.name) in seen:
                    continue
                seen.add((dep_path, dep.name))
                out.append((dep_path, dep.name, [n for stmt in dep.body for n in ast.walk(stmt)]))
                queue.append((dep_path, dep))
    return out, problems


#: Every call in the bodies of those dependencies. They run before the route body; each writes no
#: order, claim, payment or order event, and the ones that write anything else say what.
_CALLS_IN_THE_DEPENDENCIES: Dict[str, str] = {
    **{name: _PURE for name in (
        "<expr>.strip", "AgentContext", "AgentUserContext", "ValueError", "agent.get",
        "auth_metrics.get", "bearer.scheme.lower", "bool", "context.agent.get", "current_user.get", "hasattr",
        "int", "isinstance", "max", "msg.lower", "re.match", "str", "secret_fingerprint",
        "_build_internal_trusted_agent", "_is_internal_trusted_api_key", "agent_is_active", "pytest_bypass_allowed",
    )},
    **{name: _REFUSAL for name in ("HTTPException", "db_busy_http_exception")},
    **{name: "logging, a clock or a pause" for name in (
        "logger.info", "logger.warning", "time.perf_counter", "time.time", "asyncio.sleep",
    )},
    "Depends": "declares the nested dependency, which is checked here",
    "adopt_forwarded_owner": "verifies the partner assertion and claims its nonce (the replay guard); no order",
    "get_agent_by_key": "reads the agent by its key",
    "resolve_agent_id_by_api_key": "reads the agent id by its key",
    "_get_agent_context_from_checkout_token": "verifies the checkout token and reads its agent",
    "check_rate_limit": "the agent's rate counter",
    "check_daily_quota": "the agent's daily counter",
    "update_agent_stats": "the agent's request counters",
    "asyncio.create_task": "schedules update_agent_stats only (checked)",
    "verify_agent_user_jwt_for_agent": "verifies a user token",
    "decode_token": "verifies a token",
    "logger.error": "logging",
}

#: What a dependency may hand to asyncio.create_task.
_SCHEDULED_BY_A_DEPENDENCY = {"update_agent_stats"}


def _unknown_on(nodes: List[ast.AST], allowed: Dict[str, str], where: str, *, scheduled_ok: Set[str] = frozenset()) -> List[str]:
    problems: List[str] = []
    # The routing service is built over the database handle; its one call is select_psp (allowed).
    handed_over = {
        id(arg) for n in nodes
        if isinstance(n, ast.Call) and _call_key(n) == "PaymentRoutingService" for arg in n.args
    }
    for n in nodes:
        if (
            isinstance(n, ast.Call) and _call_key(n) == "asyncio.create_task" and n.args
            and isinstance(n.args[0], ast.Call) and _call_key(n.args[0]) in scheduled_ok
        ):
            handed_over.add(id(n.func))
    for node in nodes:
        if isinstance(node, ast.Call) and _call_key(node) not in allowed:
            problems.append(f"{where}:{node.lineno} calls {_call_key(node)}, not known to be read-only")
        if isinstance(node, ast.Await) and not isinstance(node.value, ast.Call):
            problems.append(f"{where}:{node.lineno} awaits something that is not a call")
        name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None
        if name in _NEVER_ON_THE_PATH and id(node) not in handed_over:
            problems.append(f"{where}:{node.lineno} names {name}")
    return problems


def _path_nodes(sources: Dict[str, str]) -> List[Tuple[str, List[ast.AST]]]:
    out = []
    for path, function, stop in _PATH_TO_THE_MERCHANT_REFUSAL:
        fn = next(
            n for n in ast.walk(ast.parse(sources[path]))
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == function
        )
        out.append((f"{path} {function}", _path_region(fn, stop)[0]))
    return out


def calls_on_the_path_not_known(sources: Dict[str, str]) -> List[str]:
    problems: List[str] = []
    for where, nodes in _path_nodes(sources):
        problems += _unknown_on(nodes, _CALLS_ON_THE_PATH, where)
    dependencies, unreadable = route_dependencies(sources)
    problems += unreadable
    for path, name, nodes in dependencies:
        problems += _unknown_on(nodes, _CALLS_IN_THE_DEPENDENCIES, f"{path} {name}", scheduled_ok=_SCHEDULED_BY_A_DEPENDENCY)
    return problems


def _path_sources() -> Dict[str, str]:
    return {path: (REPO / path).read_text(encoding="utf-8") for path, _, _ in _PATH_TO_THE_MERCHANT_REFUSAL}


def test_c1_every_call_on_the_way_to_the_merchant_refusal_is_known_to_be_read_only() -> None:
    sources = _path_sources()
    assert calls_on_the_path_not_known(sources) == []
    used = {_call_key(n) for _, nodes in _path_nodes(sources) for n in nodes if isinstance(n, ast.Call)}
    assert set(_CALLS_ON_THE_PATH) == used, f"stale entries: {sorted(set(_CALLS_ON_THE_PATH) - used)}"
    dependencies, _ = route_dependencies(sources)
    used_by_dependencies = {_call_key(n) for _, _, nodes in dependencies for n in nodes if isinstance(n, ast.Call)}
    assert set(_CALLS_IN_THE_DEPENDENCIES) == used_by_dependencies, (
        f"stale: {sorted(set(_CALLS_IN_THE_DEPENDENCIES) - used_by_dependencies)}"
    )
    names = {(path.split("/")[-1], name) for path, name, _ in dependencies if not name.startswith("lambda@")}
    assert {("agent_forwarded_owner.py", "forwarded_owner_context"), ("agent_auth.py", "get_agent_context"),
            ("agent_user_auth.py", "get_agent_user_context"), ("auth.py", "require_admin")} <= names, names


def test_c1_a_writing_dependency_on_a_path_function_is_caught() -> None:
    """M7: a dependency added to the v2 route whose body writes an order row."""
    sources = _path_sources()
    tree = ast.parse(sources["routes/agent_v2.py"])
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "create_order_v2")
    tree.body.append(ast.parse(
        "async def _oopq_planted_dependency():\n"
        "    await database.execute(\"INSERT INTO orders (order_id) VALUES ('ORD_OOPQ_EARLY')\")\n"
    ).body[0])
    fn.args.kwonlyargs.append(ast.arg(arg="planted"))
    fn.args.kw_defaults.append(ast.parse("Depends(_oopq_planted_dependency)", mode="eval").body)
    mutated = {**sources, "routes/agent_v2.py": ast.unparse(tree)}

    problems = calls_on_the_path_not_known(mutated)

    assert any("_oopq_planted_dependency" in p and "database" in p for p in problems), problems


def test_c1_a_dependency_that_cannot_be_read_is_caught() -> None:
    sources = _path_sources()
    tree = ast.parse(sources["routes/agent_v2.py"])
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "create_order_v2")
    fn.args.kwonlyargs.append(ast.arg(arg="planted"))
    fn.args.kw_defaults.append(ast.parse("Depends(oopq_somewhere_else)", mode="eval").body)

    problems = calls_on_the_path_not_known({**sources, "routes/agent_v2.py": ast.unparse(tree)})

    assert any("cannot be read" in p for p in problems), problems


def test_c1_the_quote_claim_is_read_and_answered_before_the_merchant_refusal() -> None:
    assert _quote_claim_read_comes_first((REPO / "routes/order_routes.py").read_text(encoding="utf-8")) == []


def _statement_list_of(fn: ast.AST, stmt: ast.stmt) -> List[ast.stmt]:
    for node in ast.walk(fn):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if isinstance(block, list) and any(s is stmt for s in block):
                return block
    raise AssertionError("statement not found")


def _statement_holding(fn: ast.AST, inner: ast.AST) -> ast.stmt:
    """The innermost statement of `fn` that is, or contains, `inner` and sits directly in a block."""
    best: Optional[ast.stmt] = None
    for node in ast.walk(fn):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            for stmt in block if isinstance(block, list) else []:
                if isinstance(stmt, ast.stmt) and any(n is inner for n in ast.walk(stmt)):
                    if best is None or _position(stmt) >= _position(best):
                        best = stmt
    assert best is not None
    return best


def _insert_before(fn: ast.AST, anchor: ast.stmt, new: List[ast.stmt]) -> None:
    block = _statement_list_of(fn, anchor)
    i = next(k for k, s in enumerate(block) if s is anchor)
    block[i:i] = new


def _insert_after(fn: ast.AST, anchor: ast.stmt, new: List[ast.stmt]) -> None:
    block = _statement_list_of(fn, anchor)
    i = next(k for k, s in enumerate(block) if s is anchor)
    block[i + 1:i + 1] = new


def _remove(fn: ast.AST, stmt: ast.stmt) -> None:
    block = _statement_list_of(fn, stmt)
    block[:] = [s for s in block if s is not stmt] or [ast.Pass()]


_ESCAPES = {
    "a_raw_insert": "await database.execute(\"INSERT INTO orders (order_id) VALUES ('ORD_OOPQ_EARLY')\")",
    "the_claim_insert": "await _claim_quote_for_order({'agent_id': 'a', 'quote_id': 'q'}, 'ORD_OOPQ_EARLY')",
    "a_scheduled_write": "background_tasks.add_task(create_order, {})",
    "a_helper_that_writes": "await _write_order_early(order_request)",
    "an_insert_through_another_alias": "await orders_module_alias.orders.insert().values(order_id='ORD_OOPQ_EARLY')",
}


def _mutated_write_step(mutation: str) -> str:
    """The write step's source with one mutation applied to its AST (no source line is matched)."""
    tree = ast.parse((REPO / "routes/order_routes.py").read_text(encoding="utf-8"))
    fn = _create_new_order_node(tree)
    first, second = sorted(_merchant_refusal_raises(fn), key=_position)
    first_site, second_site = _statement_holding(fn, first), _statement_holding(fn, second)
    write_call = min(
        (c for c in ast.walk(fn) if isinstance(c, ast.Call) and _callee(c) == "create_order"), key=_position
    )
    write_stmt = _statement_holding(fn, write_call)
    claim_read = next(c for c in ast.walk(fn) if isinstance(c, ast.Call) and _callee(c) == "read_quote_claim_holder")
    claim_stmt = _statement_holding(fn, claim_read)
    planted_403 = ast.parse(
        'if merchant_status == "oopq_planted":\n'
        '    raise HTTPException(status_code=403, detail={"error": "MERCHANT_NOT_ACCEPTING_ORDERS", "message": "planted"})\n'
    ).body
    if mutation == "write_above_the_first_site":
        _remove(fn, write_stmt)
        _insert_before(fn, first_site, [write_stmt])
    elif mutation == "write_above_the_second_site":
        _remove(fn, write_stmt)
        _insert_before(fn, second_site, [write_stmt])
    elif mutation == "an_event_write_above_a_site":
        _insert_before(fn, second_site, ast.parse("await log_order_event('planted', 'planted', {})").body)
    elif mutation == "a_raise_below_the_write":
        _insert_after(fn, write_stmt, planted_403)
    elif mutation == "the_code_outside_a_raise":
        _insert_after(fn, write_stmt, ast.parse("order_metadata['refusal'] = 'MERCHANT_NOT_ACCEPTING_ORDERS'").body)
    elif mutation == "the_claim_read_below_the_first_site":
        _remove(fn, claim_stmt)
        _insert_after(fn, first_site, [claim_stmt])
    elif mutation in _ESCAPES:
        _insert_before(fn, first_site, ast.parse(_ESCAPES[mutation]).body)
    else:
        raise AssertionError(mutation)
    return ast.unparse(tree)


def _write_step_problems(source: str) -> List[str]:
    sources = {**_path_sources(), "routes/order_routes.py": source}
    return (
        merchant_refusals_not_above_the_first_order_write(source)
        + _quote_claim_read_comes_first(source)
        + calls_on_the_path_not_known(sources)
    )


def test_c1_the_unmutated_write_step_passes_the_same_checks_after_a_round_trip() -> None:
    """The mutants are compared with this: the write step re-rendered from its AST, unmutated."""
    tree = ast.parse((REPO / "routes/order_routes.py").read_text(encoding="utf-8"))
    assert _write_step_problems(ast.unparse(tree)) == []


@pytest.mark.parametrize(
    "mutation",
    ["write_above_the_first_site", "write_above_the_second_site", "an_event_write_above_a_site",
     "a_raise_below_the_write", "the_code_outside_a_raise", "the_claim_read_below_the_first_site", *_ESCAPES],
)
def test_c1_the_merchant_refusal_checks_catch(mutation: str) -> None:
    assert _write_step_problems(_mutated_write_step(mutation)) != []



def listed_codes_before_the_replay(source: str) -> Set[str]:
    """The listed codes `agent_create_order` writes ABOVE the point where it answers an order this
    create already made (the cached answer, then the replay lookup's)."""
    fn = next(
        n for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "agent_create_order"
    )
    replay_returns = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Return) and isinstance(n.value, ast.Name) and n.value.id in ("replay_response", "idempotency_cache_value")
    ]
    assert replay_returns, "agent_create_order no longer answers a replay"
    last = max(_position(r) for r in replay_returns if r.value.id == "replay_response")
    first_replay = min(_position(r) for r in replay_returns)
    assert first_replay <= last
    return {
        c.value for c in ast.walk(fn)
        if isinstance(c, ast.Constant) and isinstance(c.value, str) and c.value in LISTED_CODES and _position(c) < first_replay
    }


def test_c1_a_resend_gets_its_order_before_any_listed_refusal_that_reads_live_state() -> None:
    """Above the replay, agent_create_order writes only QUOTE_REQUIRED_BEFORE_PURCHASE (no quote:
    such a request never made an order). Everything it refuses from a live read (the store and the
    commerce path, the pre-flight, the quote's merchant, the variants) is below it, so the same
    create sent again after its order was made is answered with that order."""
    source = (REPO / "routes/agent_api.py").read_text(encoding="utf-8")
    assert listed_codes_before_the_replay(source) == {"QUOTE_REQUIRED_BEFORE_PURCHASE"}


def test_c1_a_listed_refusal_moved_above_the_replay_is_caught() -> None:
    tree = ast.parse((REPO / "routes/agent_api.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "agent_create_order")
    cache_read = next(
        n for n in ast.walk(fn)
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "idempotency_cache_value" for t in n.targets)
    )
    _insert_before(fn, cache_read, ast.parse(
        'if order_request_is_odd:\n'
        '    raise HTTPException(status_code=409, detail={"error": "UNSUPPORTED_COMMERCE_PATH", "message": "planted"})\n'
    ).body)

    assert "UNSUPPORTED_COMMERCE_PATH" in listed_codes_before_the_replay(ast.unparse(tree))



#: Where each create function answers a quote that already holds its order, and the listed codes it
#: may still raise above that point: no quote at all (no claim can exist), and the quote-ownership
#: refusal (it decides whose claim is read).
_HOLDER_ANSWERED_FIRST = {
    "routes/agent_api.py": ("agent_create_order", "answer_for_a_quote_that_holds_its_order",
                            {"QUOTE_REQUIRED_BEFORE_PURCHASE", "QUOTE_NOT_FOUND"}),
    "routes/agent_v2.py": ("create_order_v2", "answer_for_a_create_whose_quote_holds_its_order", {"QUOTE_NOT_FOUND"}),
}


def _codes_written_by(function_name: str) -> Set[str]:
    """The listed codes a function of the inventory writes (the quote re-read's QuoteError is answered
    409 by every caller, C-1 above, so it is not counted)."""
    return {
        code for path, scope, code in ON_PATH
        if scope.split(".")[-1] == function_name and scope != "QuoteService.load_active_quote_or_raise"
    } | ({"QUOTE_NOT_FOUND"} if function_name == "require_quote_for_caller" else set())


def listed_codes_above_the_holder_answer(path: str, source: str) -> Set[str]:
    function, answer, _ = _HOLDER_ANSWERED_FIRST[path]
    fn = next(
        n for n in ast.walk(ast.parse(source))
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == function
    )
    answers = [c for c in ast.walk(fn) if isinstance(c, ast.Call) and _callee(c) == answer]
    assert len(answers) == 1, f"{function} no longer answers a quote that holds its order once"
    at = _position(answers[0])
    above = {
        c.value for c in ast.walk(fn)
        if isinstance(c, ast.Constant) and isinstance(c.value, str) and c.value in LISTED_CODES and _position(c) < at
    }
    for call in ast.walk(fn):
        if isinstance(call, ast.Call) and _position(call) < at:
            above |= _codes_written_by(_callee(call) or "")
    return above


@pytest.mark.parametrize("path", sorted(_HOLDER_ANSWERED_FIRST))
def test_c1_a_quote_that_holds_its_order_is_answered_before_any_listed_refusal(path: str) -> None:
    source = (REPO / path).read_text(encoding="utf-8")
    # The route's dependencies (the partner assertion) run before its body and are not counted:
    # their refusals say why, and the gateway keeps its claim on the three that can follow an order.
    assert listed_codes_above_the_holder_answer(path, source) == _HOLDER_ANSWERED_FIRST[path][2]


def test_c1_the_order_create_reads_the_quote_claim_with_its_parallel_reads() -> None:
    fn = next(
        n for n in ast.walk(_tree("routes/agent_api.py"))
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "agent_create_order"
    )
    gathers = [c for c in ast.walk(fn) if isinstance(c, ast.Call) and _call_key(c) == "asyncio.gather"]
    first = min(gathers, key=_position)
    assert any(isinstance(a, ast.Call) and _callee(a) == "read_quote_claim_holder" for a in first.args)


@pytest.mark.parametrize("path", sorted(_HOLDER_ANSWERED_FIRST))
def test_c1_a_listed_refusal_above_the_holder_answer_is_caught(path: str) -> None:
    function, answer, _ = _HOLDER_ANSWERED_FIRST[path]
    tree = ast.parse((REPO / path).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == function)
    call = next(c for c in ast.walk(fn) if isinstance(c, ast.Call) and _callee(c) == answer)
    _insert_before(fn, _statement_holding(fn, call), ast.parse(
        'if order_request_is_odd:\n'
        '    raise HTTPException(status_code=409, detail={"error": "PREFLIGHT_FAILED", "message": "planted"})\n'
    ).body)

    assert "PREFLIGHT_FAILED" in listed_codes_above_the_holder_answer(path, ast.unparse(tree))



#: Governance runs before each create function answers a quote that holds its order.
_GOVERNANCE_FIRST = {
    "routes/agent_api.py": ("agent_create_order", "validate_order_create_governance", "answer_for_a_quote_that_holds_its_order"),
    "routes/agent_v2.py": ("create_order_v2", "validate_order_create_governance", "answer_for_a_create_whose_quote_holds_its_order"),
}


def governance_comes_first(path: str, source: str) -> List[str]:
    function, governance, answer = _GOVERNANCE_FIRST[path]
    fn = next(
        n for n in ast.walk(ast.parse(source))
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == function
    )
    gov = [c for c in ast.walk(fn) if isinstance(c, ast.Call) and _callee(c) == governance]
    ans = [c for c in ast.walk(fn) if isinstance(c, ast.Call) and _callee(c) == answer]
    if len(gov) != 1 or len(ans) != 1:
        return [f"{function}: governance calls {len(gov)}, holder answers {len(ans)}"]
    problems = []
    if not _position(gov[0]) < _position(ans[0]):
        problems.append(f"{function}: governance runs after the holder answer")
    replays = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Return) and isinstance(n.value, ast.Name)
        and n.value.id in ("replay_response", "idempotency_cache_value")
    ]
    if any(_position(r) < _position(gov[0]) for r in replays):
        problems.append(f"{function}: an order is replayed before governance")
    return problems


@pytest.mark.parametrize("path", sorted(_GOVERNANCE_FIRST))
def test_c1_governance_runs_before_a_held_quote_is_answered(path: str) -> None:
    """A blocked or suspended agent (governance's 403) is refused before an order its quote holds,
    or one this create already made (the cache and the replay lookup), is answered with its payment
    surface."""
    assert governance_comes_first(path, (REPO / path).read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("path", sorted(_GOVERNANCE_FIRST))
def test_c1_governance_moved_below_the_holder_answer_is_caught(path: str) -> None:
    function, governance, answer = _GOVERNANCE_FIRST[path]
    tree = ast.parse((REPO / path).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == function)
    gov_stmt = _statement_holding(fn, next(c for c in ast.walk(fn) if isinstance(c, ast.Call) and _callee(c) == governance))
    ans_stmt = _statement_holding(fn, next(c for c in ast.walk(fn) if isinstance(c, ast.Call) and _callee(c) == answer))
    _remove(fn, gov_stmt)
    _insert_after(fn, ans_stmt, [gov_stmt])

    assert governance_comes_first(path, ast.unparse(tree)) != []


#: (file, function, the holder answer, the name its result is bound to)
_HELD_BRANCHES = [
    ("routes/agent_api.py", "agent_create_order", "answer_for_a_quote_that_holds_its_order", "held_quote_response"),
    ("routes/agent_v2.py", "create_order_v2", "answer_for_a_create_whose_quote_holds_its_order", "held"),
]


def held_branch_problems(source: str, function: str, answer: str, name: str) -> List[str]:
    """Once a holder is known, the create may only answer that order. Every branch on the holder
    answer's result holds a bookkeeping assignment at most, then `return` of that result (as is, or
    through v2's `_order_created_answer`); and no listed code is written between the answer and the
    end of its branch."""
    fn = next(
        n for n in ast.walk(ast.parse(source))
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == function
    )
    problems: List[str] = []
    branches = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.If) and any(isinstance(x, ast.Name) and x.id == name for x in ast.walk(n.test))
    ]
    if len(branches) != 1:
        return [f"{function}: {len(branches)} branches on {name}"]
    branch = branches[0]
    if branch.orelse:
        problems.append(f"{function}: the held branch has an else")
    *head, last = branch.body
    for stmt in head:
        targets = stmt.targets if isinstance(stmt, ast.Assign) else []
        if not (len(targets) == 1 and isinstance(targets[0], ast.Name) and targets[0].id == "_agent_order_id_for_perf"):
            problems.append(f"{function}:{stmt.lineno} the held branch does more than answer: {ast.unparse(stmt)[:60]}")
    value = last.value if isinstance(last, ast.Return) else None
    plain = isinstance(value, ast.Name) and value.id == name
    via_v2 = (
        isinstance(value, ast.Await) and isinstance(value.value, ast.Call) and _callee(value.value) == "_order_created_answer"
        and isinstance(value.value.args[0], ast.Name) and value.value.args[0].id == name
    )
    if not (plain or via_v2):
        problems.append(f"{function}: the held branch does not end by answering the order")
    answer_at = min(_position(c) for c in ast.walk(fn) if isinstance(c, ast.Call) and _callee(c) == answer)
    end = (branch.end_lineno, branch.end_col_offset)
    for const in ast.walk(fn):
        if isinstance(const, ast.Constant) and const.value in LISTED_CODES and answer_at <= _position(const) <= end:
            problems.append(f"{function}:{const.lineno} {const.value} next to the holder answer")
    return problems


#: The functions that answer a quote that holds its order: none writes a listed code.
_HOLDER_ANSWER_FUNCTIONS = [
    ("routes/agent_api.py", "answer_for_a_quote_that_holds_its_order"),
    ("routes/agent_api.py", "answer_for_a_create_whose_quote_holds_its_order"),
    ("routes/agent_api.py", "_replay_order_that_holds_quote"),
    ("routes/agent_v2.py", "_order_created_answer"),
    ("db/orders.py", "read_quote_claim_holder"),
]


@pytest.mark.parametrize("path, function, answer, name", _HELD_BRANCHES, ids=[b[1] for b in _HELD_BRANCHES])
def test_c1_a_held_branch_only_answers_the_order(path: str, function: str, answer: str, name: str) -> None:
    assert held_branch_problems((REPO / path).read_text(encoding="utf-8"), function, answer, name) == []


@pytest.mark.parametrize("path, function", _HOLDER_ANSWER_FUNCTIONS, ids=[f[1] for f in _HOLDER_ANSWER_FUNCTIONS])
def test_c1_the_holder_answer_functions_write_no_listed_code(path: str, function: str) -> None:
    assert listed_codes_at_or_after_the_write_step((REPO / path).read_text(encoding="utf-8"), function, None) == []


@pytest.mark.parametrize("path, function, answer, name", _HELD_BRANCHES, ids=[b[1] for b in _HELD_BRANCHES])
def test_c1_a_listed_refusal_in_the_held_branch_is_caught(path: str, function: str, answer: str, name: str) -> None:
    """MG: a conditional listed refusal inside the branch that runs once an order is known."""
    tree = ast.parse((REPO / path).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == function)
    branch = next(n for n in ast.walk(fn) if isinstance(n, ast.If) and any(isinstance(x, ast.Name) and x.id == name for x in ast.walk(n.test)))
    branch.body[0:0] = ast.parse(
        f'if str({name}.get("currency") or "") != "oopq":\n'
        '    raise HTTPException(status_code=409, detail={"error": "PREFLIGHT_FAILED", "message": "planted"})\n'
    ).body

    assert held_branch_problems(ast.unparse(tree), function, answer, name) != []


#: Everything else that runs around these routes. Reviewed: none writes an order, a claim, a payment
#: or an order event. Adding one fails here until it is read and named.
_APP_MIDDLEWARE = {
    "UsageLoggerMiddleware", "RateLimitMiddleware", "StructuredLoggingMiddleware", "SyntheticCallMiddleware",
    "ErrorHandlerMiddleware", "SecurityHeadersMiddleware", "AP2SecurityMiddleware", "TieredCORSMiddleware",
    # Reviewed (affiliate registry, 2026-11-20): answers 404 for /employee/affiliate-programs* while its dial
    # is off and passes every other request through untouched; it reads no body and writes nothing.
    "RegistryDarkMiddleware",
}
_APP_HTTP_MIDDLEWARE_FUNCTIONS = {"add_service_version_headers", "cors_preflight_passthrough"}


def _keyword(call: ast.Call, name: str) -> Optional[ast.AST]:
    return next((kw.value for kw in call.keywords if kw.arg == name), None)


def test_c1_no_router_or_app_dependency_and_only_the_reviewed_middleware_around_the_routes() -> None:
    main = _tree("main.py")
    app_calls = [
        n for n in ast.walk(main)
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "app" for t in n.targets)
        and isinstance(n.value, ast.Call) and _call_key(n.value) == "FastAPI"
    ]
    assert len(app_calls) == 1 and _keyword(app_calls[0].value, "dependencies") is None
    for path in ("routes/agent_api.py", "routes/agent_v2.py"):
        routers = [
            n.value for n in _tree(path).body
            if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "router" for t in n.targets)
        ]
        assert len(routers) == 1 and _keyword(routers[0], "dependencies") is None, path
    includes = [
        n for n in ast.walk(main)
        if isinstance(n, ast.Call) and _call_key(n) == "app.include_router" and n.args
        and isinstance(n.args[0], ast.Name) and n.args[0].id in ("agent_api_router", "agent_v2_router")
    ]
    assert len(includes) == 2 and all(_keyword(c, "dependencies") is None for c in includes)
    added = {
        ast.unparse(n.args[0]) for n in ast.walk(main)
        if isinstance(n, ast.Call) and _call_key(n) == "app.add_middleware" and n.args
    }
    assert added == _APP_MIDDLEWARE, f"middleware to review: {sorted(added ^ _APP_MIDDLEWARE)}"
    http = {
        n.name for n in ast.walk(main)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(isinstance(d, ast.Call) and _call_key(d) == "app.middleware" for d in n.decorator_list)
    }
    assert http == _APP_HTTP_MIDDLEWARE_FUNCTIONS, f"middleware to review: {sorted(http ^ _APP_HTTP_MIDDLEWARE_FUNCTIONS)}"


def _functions_reading(path: str, name: str) -> Set[str]:
    """The functions of `path` that read the module-level name `name`."""
    out: Set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Load) for n in ast.walk(node)):
                out.add(node.name)
    return out


def test_c1_the_two_module_constants_have_one_user_each() -> None:
    assert _functions_reading("routes/agent_forwarded_owner.py", "REJECTED_CODE") == {"_reject"}
    assert _functions_reading("services/quote_service.py", "QUOTE_NOT_FOUND_DETAIL") == {"require_quote_for_caller"}
    # Nobody reaches them from another module either (by attribute or by import).
    for path in _source_files():
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Attribute) and node.attr in ("REJECTED_CODE", "QUOTE_NOT_FOUND_DETAIL"):
                raise AssertionError(f"{path} reads {node.attr} by attribute")
            if isinstance(node, ast.ImportFrom) and any(a.name in ("REJECTED_CODE", "QUOTE_NOT_FOUND_DETAIL") for a in node.names):
                raise AssertionError(f"{path} imports {[a.name for a in node.names]}")
    # `_reject` is called only by the dependency's own adoption function.
    callers = {
        node.name
        for node in ast.walk(_tree("routes/agent_forwarded_owner.py"))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "_reject" for c in ast.walk(node))
    }
    assert callers == {"adopt_forwarded_owner"}


def _call_first_arg_literals(callee: str) -> Set[Tuple[str, str]]:
    out: Set[Tuple[str, str]] = set()
    for path in _source_files():
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
            if name == callee and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                out.add((path, node.args[0].value))
    return out


def test_c1_no_error_enum_member_and_no_typed_error_carries_a_listed_code() -> None:
    from utils.error_codes import ErrorCode

    assert not ({member.value.code for member in ErrorCode} & LISTED_CODES)
    assert not ({member.name for member in ErrorCode} & LISTED_CODES)
    assert not ({code for _, code in _call_first_arg_literals("ShopifyPricingError")} & LISTED_CODES)
    quote_errors = {(path, code) for path, code in _call_first_arg_literals("QuoteError") if code in LISTED_CODES}
    assert quote_errors == {("services/quote_service.py", "QUOTE_NOT_FOUND")}


def _write_step_closure() -> Dict[Tuple[str, str, int], ast.AST]:
    """`order_routes.create_new_order` and every function it can call in routes/ and services/,
    followed by NAME through the AST (so it over-reaches: a call of `x.get(...)` follows every
    function named `get`). Over-reaching is the safe direction for this check."""
    defs: Dict[str, List[Tuple[str, ast.AST]]] = defaultdict(list)
    for path in _source_files():
        if not path.startswith(("routes/", "services/")):
            continue
        for node in ast.walk(_tree(path)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defs[node.name].append((path, node))
    start = [d for d in defs["create_new_order"] if d[0] == "routes/order_routes.py"]
    assert len(start) == 1
    seen: Dict[Tuple[str, str, int], ast.AST] = {}
    queue = list(start)
    while queue:
        path, node = queue.pop()
        key = (path, node.name, node.lineno)
        if key in seen:
            continue
        seen[key] = node
        for call in ast.walk(node):
            if isinstance(call, ast.Call):
                func = call.func
                name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
                queue.extend(defs.get(name or "", []))
    return seen


def test_c1_the_write_step_and_everything_it_calls_writes_no_listed_code_but_the_merchant_refusal() -> None:
    closure = _write_step_closure()
    assert len(closure) > 50, "the call graph collapsed; the walk is broken"
    names = {name for _, name, _ in closure}
    assert "create_new_order" in names and "load_active_quote_or_raise" in names
    assert not ({"agent_create_order", "create_order_v2", "_coerce_shipping_address", "_reject", "require_quote_for_caller"} & names), (
        "the write step can now reach a function that answers a listed pair"
    )
    hits = {
        (path, name, const.value)
        for (path, name, _), node in closure.items()
        for const in ast.walk(node)
        if isinstance(const, ast.Constant) and isinstance(const.value, str) and const.value in LISTED_CODES
    }
    # Two exceptions. The quote re-read's QuoteError("QUOTE_NOT_FOUND"): the write step answers
    # every QuoteError with 409 (C-3 drives it), and the listed pair is the 404. And the write step's
    # own merchant refusal, which sits above its order write (pinned above) and which its busy
    # retry cannot reach after a row exists (C-3).
    assert hits == {
        ("services/quote_service.py", "load_active_quote_or_raise", "QUOTE_NOT_FOUND"),
        ("routes/order_routes.py", "create_new_order", "MERCHANT_NOT_ACCEPTING_ORDERS"),
    }


def test_c1_the_write_step_answers_every_quote_error_with_409() -> None:
    """Inside `create_new_order`, each `except QuoteError` handler raises one HTTPException, 409."""
    tree = _tree("routes/order_routes.py")
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "create_new_order")
    handlers = [
        h
        for h in ast.walk(fn)
        if isinstance(h, ast.ExceptHandler) and isinstance(h.type, ast.Name) and h.type.id == "QuoteError"
    ]
    assert handlers
    for handler in handlers:
        statuses = [
            kw.value.value
            for call in ast.walk(handler)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "HTTPException"
            for kw in call.keywords
            if kw.arg == "status_code" and isinstance(kw.value, ast.Constant)
        ]
        assert statuses == [409], statuses
    # And no 404 in the whole function carries a dict detail (its one 404 is a plain string).
    for call in ast.walk(fn):
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "HTTPException":
            kws = {kw.arg: kw.value for kw in call.keywords}
            if isinstance(kws.get("status_code"), ast.Constant) and kws["status_code"].value == 404:
                assert isinstance(kws.get("detail"), ast.Constant) and kws["detail"].value == "Merchant not found"


def test_c1_a_listed_code_is_only_ever_written_as_the_error_member_of_a_detail() -> None:
    """The gateway gives its guard back only when `error.details.error` IS the code. So no raise
    site may put a listed code anywhere else: not as a bare text detail (which the envelope would
    copy into `message`), not under another key. Each on-path constant is the value of an "error"
    key of a dict literal; the one QuoteError code is that exception's first argument."""
    for path in sorted({path for path, _, _ in ON_PATH}):
        tree = _tree(path)
        parents: Dict[int, ast.AST] = {id(child): node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in LISTED_CODES):
                continue
            parent = parents[id(node)]
            if isinstance(parent, ast.Dict):
                keys = [k.value for k, v in zip(parent.keys, parent.values) if v is node and isinstance(k, ast.Constant)]
                assert keys == ["error"], f"{path}:{node.lineno}: {node.value} under key {keys}"
            elif isinstance(parent, ast.Call):
                assert getattr(parent.func, "id", None) == "QuoteError" and parent.args[0] is node, f"{path}:{node.lineno}"
            else:
                assert isinstance(parent, ast.Assign) and parent.targets[0].id == "REJECTED_CODE", f"{path}:{node.lineno}"
    # ... and REJECTED_CODE itself is used as the "error" member, in `_reject` only.
    owner = _tree("routes/agent_forwarded_owner.py")
    uses = [
        (k.value if isinstance(k, ast.Constant) else None)
        for d in ast.walk(owner) if isinstance(d, ast.Dict)
        for k, v in zip(d.keys, d.values) if isinstance(v, ast.Name) and v.id == "REJECTED_CODE"
    ]
    assert uses == ["error"]


def test_c1_the_assertion_reasons_in_the_contract_are_the_ones_the_code_can_give() -> None:
    reasons: Set[str] = set()
    for node in ast.walk(_tree("services/bound_issuing_assertion.py")):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "BoundVerdict":
            if len(node.args) == 2 and isinstance(node.args[0], ast.Constant) and node.args[0].value is None:
                assert isinstance(node.args[1], ast.Constant), "a refusal reason that is not a literal"
                reasons.add(node.args[1].value)
    # The two the route dependency adds itself: an unusable subject, and a nonce already used.
    owner_literals = {value for _, value in _scoped_constants("routes/agent_forwarded_owner.py")}
    assert {"subject", "replay"} <= owner_literals
    reasons |= {"subject", "replay"}
    assert sorted(reasons) == ASSERTION_NOTES["reasons"]
    assert len(reasons) == 11
    assert all(re.fullmatch(r"[a-z][a-z0-9_]*", r) for r in reasons)


# ---------------------------------------------------------------------------
# The contract world: the harness, with the real quote-ownership check and the real assertion check
# ---------------------------------------------------------------------------


@pytest.fixture
async def cw(world: World, monkeypatch: pytest.MonkeyPatch) -> World:
    import routes.agent_api as agent_api
    import routes.agent_auth as agent_auth
    import routes.agent_forwarded_owner as forwarded
    import routes.agent_v2 as agent_v2
    import services.issuing_agent_assertion as ia
    import services.quote_service as quote_service
    import services.shopify_policy_service as policy_service
    from services import assertion_replay_guard

    w = world
    w.quotes.consume_lands = False

    # The REAL ownership check (404 QUOTE_NOT_FOUND), over the in-memory quote store.
    async def owner_row(quote_id: str) -> Optional[Dict[str, Any]]:
        q = w.quotes.quotes.get(quote_id)
        return {"quote_id": quote_id, "agent_id": q["agent_id"], "snapshot_json": {}} if q else None

    monkeypatch.setattr(quote_service, "get_quote_owner_row", owner_row)
    monkeypatch.setattr(agent_api, "require_quote_for_caller", quote_service.require_quote_for_caller)
    w.body_entries = []
    real_require = quote_service.require_quote_for_caller

    async def body_entry(quote_id: Any, context: Any) -> Any:
        w.body_entries.append(getattr(context, "agent_id", None))  # create_order_v2's first statement
        return await real_require(quote_id, context)

    monkeypatch.setattr(agent_v2, "require_quote_for_caller", body_entry)

    # C-2 / C-3: the replay lookup reads the table and finds nothing.
    w.lookup_raises = False

    async def no_order(**_: Any) -> None:
        if w.lookup_raises:
            raise RuntimeError(BUSY)
        return None

    monkeypatch.setattr(agent_api, "find_replayable_order_for_create", no_order)

    async def no_policies(_merchant_id: str) -> List[Dict[str, Any]]:
        return []

    monkeypatch.setattr(policy_service, "get_latest_policy_hashes", no_policies)

    # The REAL partner-assertion check, with the caller taken as the vouching service.
    agents = {
        AGENT_A: {"agent_id": AGENT_A, "agent_name": "Fixture Partner", "is_active": True, "allowed_merchants": None},
        GATEWAY: {"agent_id": GATEWAY, "agent_name": "Fixture Gateway", "is_active": True, "allowed_merchants": None},
    }

    async def get_agent(agent_id: str) -> Optional[Dict[str, Any]]:
        return dict(agents[agent_id]) if agent_id in agents else None

    async def is_service(_api_key: Any) -> bool:
        return True

    async def active(agent_id: Any, _excluded: Any = None) -> Optional[str]:
        return agent_id if agent_id in agents else None

    async def within_limits(agent_id: str, *_: Any, **__: Any) -> Tuple[bool, int, int]:
        return True, 1, 1000

    async def no_stats(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(agent_auth, "get_agent", get_agent)
    monkeypatch.setattr(agent_auth, "_is_service_caller", is_service)
    monkeypatch.setattr(agent_auth, "check_rate_limit", within_limits)
    monkeypatch.setattr(agent_auth, "check_daily_quota", within_limits)
    monkeypatch.setattr(agent_auth, "update_agent_stats", no_stats)
    monkeypatch.setattr(ia, "_active_agent_id", active)
    monkeypatch.setattr("utils.redis_client.get_redis_client", lambda: None)
    monkeypatch.setenv("ISSUING_AGENT_ASSERTION_SECRET", ASSERTION_SECRET)
    monkeypatch.setenv("ISSUING_ASSERTION_VOUCHER_AGENT_IDS", GATEWAY)
    monkeypatch.setenv("ISSUING_AGENT_EXCLUDED_AGENT_IDS", GATEWAY)
    monkeypatch.setenv("PARTNER_IDENTITY_FORWARDING_ENABLED", "1")
    assertion_replay_guard.reset_local_store()
    w.nonce_claims = []
    real_claim = assertion_replay_guard.claim

    async def counted_claim(jti: str, ttl: int, **kwargs: Any) -> Any:
        w.nonce_claims.append(jti)
        return await real_claim(jti, ttl, **kwargs)

    monkeypatch.setattr(assertion_replay_guard, "claim", counted_claim)
    w.agents = agents
    outcomes = dict(forwarded.FORWARDED_OWNER_OUTCOMES)
    try:
        yield w
    finally:
        assertion_replay_guard.reset_local_store()
        forwarded.FORWARDED_OWNER_OUTCOMES.clear()
        forwarded.FORWARDED_OWNER_OUTCOMES.update(outcomes)


def v2_body(quote_id: Optional[str], *, email: Optional[str] = "buyer@example.com", **buyer: Any) -> Dict[str, Any]:
    body: Dict[str, Any] = {"buyer_context": {**({"customer_email": email} if email else {}), **buyer}}
    if quote_id is not None:
        body["quote_id"] = quote_id
    return body


def signed(body: Dict[str, Any], *, secret: str = ASSERTION_SECRET, **override: Any) -> Tuple[bytes, Dict[str, str]]:
    """The body's bytes and a v2 assertion bound to exactly this request, as the gateway signs it."""
    from services.bound_issuing_assertion import sign_bound_issuing_agent_assertion

    raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
    payload: Dict[str, Any] = {
        "v": 2, "kind": "agent", "sub": AGENT_A, "op": "create_order", "ts": int(time.time()),
        "jti": secrets.token_urlsafe(18), "m": "POST", "p": "/agent/v2/orders", "rid": body.get("quote_id"),
        "bh": hashlib.sha256(raw).hexdigest(),
    }
    payload.update(override)
    token = sign_bound_issuing_agent_assertion(payload, secret)
    return raw, {"X-Pivota-Issuing-Agent": token, "X-API-Key": "ak_live_" + "0c" * 32}


async def post_v2(w: World, body: Dict[str, Any], *, agent: str = AGENT_A, tag: str = "r1", **kwargs: Any) -> Answer:
    return await w._post("/agent/v2/orders", body, agent=agent, tag=tag, **kwargs)


def assert_listed_refusal(w: World, answer: Answer, status: int, code: str) -> None:
    assert (status, code) in LISTED
    assert answer.status == status, (answer.status, answer.body)
    err = answer.body["error"]
    assert err["details"]["error"] == code, answer.body
    assert err["message"] == code
    assert err["code"] == ENVELOPE["status_code"][str(status)][0]
    assert w.create_runs == [], "the write step was entered before a listed refusal"
    assert w.db.count("order_insert") == 0 and w.db.count("claim_insert") == 0


# ---------------------------------------------------------------------------
# C-2: each pair, through the real route, with the write step entered 0 times
# ---------------------------------------------------------------------------


async def test_c2_partner_assertion_rejected(cw: World) -> None:
    quote = cw.quotes.add()
    raw, headers = signed(v2_body(quote), secret="not the secret")

    answer = await post_v2(cw, None, agent=GATEWAY, content=raw, headers=headers)

    assert_listed_refusal(cw, answer, 409, "PARTNER_ASSERTION_REJECTED")
    assert answer.body["error"]["details"]["reason"] == "bad_mac"


@pytest.mark.parametrize("whose", ["absent", "another_agents"])
async def test_c2_quote_not_found(cw: World, whose: str) -> None:
    quote = cw.quotes.add(agent=AGENT_B) if whose == "another_agents" else "q_oopq_no_such_quote"

    answer = await post_v2(cw, v2_body(quote), agent=AGENT_A)

    assert_listed_refusal(cw, answer, 404, "QUOTE_NOT_FOUND")


async def test_c2_invalid_quote(cw: World) -> None:
    quote = cw.quotes.add()
    cw.quotes.reshape = lambda _id, snapshot, _n: snapshot.request_json.update(items=[])

    answer = await post_v2(cw, v2_body(quote))

    assert_listed_refusal(cw, answer, 400, "INVALID_QUOTE")


@pytest.mark.parametrize("what", ["no_email", "no_address", "address_missing_fields"])
async def test_c2_invalid_buyer_context(cw: World, what: str) -> None:
    quote = cw.quotes.add()
    body = v2_body(quote)
    if what == "no_email":
        cw.quotes.reshape = lambda _id, snapshot, _n: snapshot.request_json.pop("customer_email", None)
        body = v2_body(quote, email=None)
    elif what == "no_address":
        cw.quotes.reshape = lambda _id, snapshot, _n: snapshot.request_json.pop("shipping_address", None)
    else:
        body = v2_body(quote, shipping_address={"name": "Fixture Buyer"})

    answer = await post_v2(cw, body)

    assert_listed_refusal(cw, answer, 400, "INVALID_BUYER_CONTEXT")


async def test_c2_quote_required_before_purchase(cw: World) -> None:
    """Reachable only on the v1 route: the v2 body cannot be sent without a quote id."""
    payload = {
        "merchant_id": MERCHANT,
        "customer_email": "buyer@example.com",
        "items": [
            {"product_id": "prod_oopq_1", "product_title": "Fixture item", "variant_id": "var_oopq_1",
             "quantity": 1, "unit_price": "9.09", "subtotal": "9.09"}
        ],
        "shipping_address": shipping_address("US"),
        "currency": "USD",
        "metadata": {},
    }

    answer = await cw._post("/agent/v1/orders/create", payload, agent=AGENT_A, tag="r1")

    assert_listed_refusal(cw, answer, 400, "QUOTE_REQUIRED_BEFORE_PURCHASE")


async def test_c2_unsupported_commerce_path(cw: World, monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.agent_api as agent_api

    async def another_platform(_merchant_id: str) -> Dict[str, Any]:
        return {"platform": "woocommerce", "store_id": "store_oopq_other"}

    monkeypatch.setattr(agent_api, "get_primary_store", another_platform)
    quote = cw.quotes.add()

    answer = await post_v2(cw, v2_body(quote))

    assert_listed_refusal(cw, answer, 409, "UNSUPPORTED_COMMERCE_PATH")


@pytest.mark.parametrize("route", ["v2", "v1"])
async def test_c2_quote_merchant_mismatch(cw: World, route: str) -> None:
    """On v2 the order's merchant IS the quote's, so the mismatch needs the quote to read as another
    merchant's at the second read. On v1 the caller names the merchant."""
    quote = cw.quotes.add()
    if route == "v2":

        def other_merchant_on_the_second_read(_id: str, snapshot: SimpleNamespace, nth: int) -> None:
            if nth >= 2:
                snapshot.merchant_id = "merch_oopq_other"

        cw.quotes.reshape = other_merchant_on_the_second_read
        answer = await post_v2(cw, v2_body(quote))
    else:
        payload = {
            "merchant_id": "merch_oopq_other",
            "quote_id": quote,
            "customer_email": "buyer@example.com",
            "items": [{"product_id": "prod_oopq_1", "variant_id": "var_oopq_1", "quantity": 1}],
            "shipping_address": shipping_address("US"),
            "currency": "USD",
            "metadata": {},
        }
        answer = await cw._post("/agent/v1/orders/create", payload, agent=AGENT_A, tag="r1")

    assert_listed_refusal(cw, answer, 400, "QUOTE_MERCHANT_MISMATCH")


async def test_c2_preflight_failed(cw: World, monkeypatch: pytest.MonkeyPatch) -> None:
    import mvp.offer as offer_module

    real_preflight = offer_module.preflight_offers

    def failing_preflight(**kwargs: Any) -> Any:
        results = real_preflight(**kwargs)
        assert results, "the fixture quote produced no offer to pre-flight"
        return [r.model_copy(update={"status": "fail"}) for r in results]

    monkeypatch.setattr(offer_module, "preflight_offers", failing_preflight)
    monkeypatch.setenv("MVP_PREFLIGHT_ENFORCE", "true")
    quote = cw.quotes.add()

    answer = await post_v2(cw, v2_body(quote))

    assert_listed_refusal(cw, answer, 409, "PREFLIGHT_FAILED")


async def test_c2_variant_required(cw: World) -> None:
    quote = cw.quotes.add()

    def no_variant(_id: str, snapshot: SimpleNamespace, _n: int) -> None:
        snapshot.request_json["items"] = [{"product_id": "prod_oopq_1", "quantity": 1}]

    cw.quotes.reshape = no_variant

    answer = await post_v2(cw, v2_body(quote))

    assert_listed_refusal(cw, answer, 400, "VARIANT_REQUIRED")


async def test_c2_the_same_request_without_its_fault_reaches_the_write_step(cw: World) -> None:
    """The control: the fixture request these cases break is one that creates an order."""
    quote = cw.quotes.add()
    raw, headers = signed(v2_body(quote))

    answer = await post_v2(cw, None, agent=GATEWAY, content=raw, headers=headers)

    assert answer.status == 200, answer.body
    assert cw.create_runs == ["r1"] and cw.body_entries == [AGENT_A]
    assert len(await cw.orders_for(quote, AGENT_A)) == 1


# ---------------------------------------------------------------------------
# C-3: a row is written, then every failure that can follow it
# ---------------------------------------------------------------------------


def _quote_error_409(code: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"error": code, "message": "fixture", "debug_id": None})


def _guard_refusal() -> HTTPException:
    import routes.order_routes as order_routes

    return order_routes.OrderForQuoteExists("ORD_OOPQ_ELSEWHERE")


def _guard_unavailable() -> HTTPException:
    from utils.transient_errors import ORDER_QUOTE_GUARD_UNAVAILABLE, order_existence_unknown_http_exception_fields

    return HTTPException(**order_existence_unknown_http_exception_fields(ORDER_QUOTE_GUARD_UNAVAILABLE))


#: Everything `create_new_order` can raise that is not a busy error, as it raises it.
_WRITE_STEP_REFUSALS: List[Tuple[str, Any, int]] = [
    ("merchant_not_found", lambda: HTTPException(status_code=404, detail="Merchant not found"), 404),
    ("merchant_not_accepting", lambda: HTTPException(status_code=403, detail={"error": "MERCHANT_NOT_ACCEPTING_ORDERS", "message": "fixture"}), 403),
    ("checkout_quote_required", lambda: HTTPException(status_code=400, detail={"error": "CHECKOUT_QUOTE_REQUIRED", "message": "fixture"}), 400),
    ("quote_required", lambda: HTTPException(status_code=400, detail={"error": "QUOTE_REQUIRED", "message": "quote_id is required"}), 400),
    ("insufficient_inventory", lambda: HTTPException(status_code=400, detail={"message": "Insufficient inventory", "items": []}), 400),
    *[
        (f"quote_error_{code.lower()}", (lambda code=code: _quote_error_409(code)), 409)
        for code in (
            "QUOTE_NOT_FOUND", "QUOTE_EXPIRED", "QUOTE_CONSUMED", "QUOTE_MISMATCH", "QUOTE_STALE_REPRICE_REQUIRED",
            "QUOTE_SHIPPING_UNVERIFIED", "QUOTE_PERSISTENCE_FAILED", "SHOPIFY_PRICING_UNAVAILABLE", "OUT_OF_STOCK",
            "INSUFFICIENT_INVENTORY",
        )
    ],
    ("internal_error", lambda: HTTPException(status_code=500, detail="Order creation internal error: fixture"), 500),
    ("quote_already_has_its_order", _guard_refusal, 409),
    ("guard_could_not_tell", _guard_unavailable, 503),
]


def _busy_500() -> HTTPException:
    """A busy error as `create_new_order` wraps one."""
    try:
        raise RuntimeError(BUSY)
    except RuntimeError as busy:
        wrapped = HTTPException(status_code=500, detail=f"Order creation internal error: {busy}")
        wrapped.__cause__ = busy
        return wrapped


def _install_write_step_spy(w: World, monkeypatch: pytest.MonkeyPatch, quote: str, script: List[Any]) -> List[int]:
    """Replace the write step: its FIRST run writes the order row (through the real insert), and
    each run then raises (or returns) the next entry of `script`."""
    import routes.order_routes as order_routes

    runs: List[int] = []

    async def spy(order_request: Any, background_tasks: Any, **_: Any) -> Any:
        runs.append(len(runs) + 1)
        if len(runs) == 1:
            await w.orders_module.create_order(_order_data(quote))
        outcome = script[len(runs) - 1]()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(order_routes, "create_new_order", spy)
    return runs


@pytest.mark.parametrize("case", _WRITE_STEP_REFUSALS, ids=[c[0] for c in _WRITE_STEP_REFUSALS])
async def test_c3_busy_after_the_row_then_a_refusal_from_the_second_run_is_never_a_listed_pair(
    cw: World, monkeypatch: pytest.MonkeyPatch, case: Tuple[str, Any, int]
) -> None:
    _, second_run, status = case
    quote = cw.quotes.add()
    runs = _install_write_step_spy(cw, monkeypatch, quote, [_busy_500, second_run])

    answer = await post_v2(cw, v2_body(quote))

    assert len(await cw.orders_for(quote)) == 1, "the case must have an order row behind the answer"
    assert runs == [1, 2]
    # The refusal is raised while the first run's busy error is being handled, so it carries that
    # error as its context and the busy check reads it as busy: today every one of these leaves
    # as the busy 503. Either answer is acceptable here; a listed pair is not (the merchant
    # refusal, listed at 403, must therefore leave as the 503).
    assert answer.status in (status, 503), (answer.status, answer.body)
    if answer.status == 503:
        assert answer.body["error"]["details"]["error"] == "TEMPORARY_UNAVAILABLE"
    assert_not_listed(answer)


@pytest.mark.parametrize("case", _WRITE_STEP_REFUSALS, ids=[c[0] for c in _WRITE_STEP_REFUSALS])
async def test_c3_busy_after_the_row_with_an_unreadable_lookup_is_the_503_and_runs_the_write_step_once(
    cw: World, monkeypatch: pytest.MonkeyPatch, case: Tuple[str, Any, int]
) -> None:
    _, second_run, _ = case
    quote = cw.quotes.add()
    runs = _install_write_step_spy(cw, monkeypatch, quote, [_busy_500, second_run])

    async def lookup_fails_after_the_row(**_: Any) -> None:
        if runs:
            raise RuntimeError(BUSY)
        return None

    import routes.agent_api as agent_api

    monkeypatch.setattr(agent_api, "find_replayable_order_for_create", lookup_fails_after_the_row)

    answer = await post_v2(cw, v2_body(quote))

    assert runs == [1], "the write step ran again although the lookup could not tell"
    assert answer.status == 503
    assert answer.body["error"]["details"]["error"] == "TEMPORARY_UNAVAILABLE"
    assert answer.body["error"]["details"]["reason"] == "ORDER_REPLAY_LOOKUP_UNAVAILABLE"
    assert_not_listed(answer)


async def test_c3_busy_on_both_runs_is_the_503(cw: World, monkeypatch: pytest.MonkeyPatch) -> None:
    quote = cw.quotes.add()
    runs = _install_write_step_spy(cw, monkeypatch, quote, [_busy_500, _busy_500])

    answer = await post_v2(cw, v2_body(quote))

    assert runs == [1, 2] and answer.status == 503
    assert answer.retry_after == "1"
    assert_not_listed(answer)


class _BusyTotal:
    """What the write step returns, whose first read in the post-create tail meets a busy error."""

    order_id = "ORD_OOPQ_TAIL"

    @property
    def total(self) -> float:
        raise RuntimeError(BUSY)


@pytest.mark.parametrize("lookup", ["reads_nothing", "unreadable"])
async def test_c3_a_busy_error_in_the_post_create_tail_is_never_a_listed_pair(
    cw: World, monkeypatch: pytest.MonkeyPatch, lookup: str
) -> None:
    quote = cw.quotes.add()
    runs = _install_write_step_spy(cw, monkeypatch, quote, [_BusyTotal])
    if lookup == "unreadable":
        import routes.agent_api as agent_api

        async def unreadable(**_: Any) -> None:
            if runs:
                raise RuntimeError(BUSY)
            return None

        monkeypatch.setattr(agent_api, "find_replayable_order_for_create", unreadable)

    answer = await post_v2(cw, v2_body(quote))

    assert runs == [1] and len(await cw.orders_for(quote)) == 1
    assert answer.status == 503 and answer.body["error"]["details"]["error"] == "TEMPORARY_UNAVAILABLE"
    assert_not_listed(answer)


@pytest.mark.parametrize("reload", ["missing", "raises", "raises_busy"])
async def test_c3_a_failed_reload_after_the_create_is_never_a_listed_pair(
    cw: World, monkeypatch: pytest.MonkeyPatch, reload: str
) -> None:
    import routes.agent_v2 as agent_v2

    async def reload_fails(_order_id: str) -> Any:
        if reload == "missing":
            return None
        raise RuntimeError(BUSY if reload == "raises_busy" else "reload failed")

    monkeypatch.setattr(agent_v2, "get_order", reload_fails)
    quote = cw.quotes.add()

    answer = await post_v2(cw, v2_body(quote))

    assert cw.create_runs == ["r1"] and len(await cw.orders_for(quote)) == 1
    assert answer.status == (502 if reload == "missing" else 500), (answer.status, answer.body)
    assert_not_listed(answer)


@pytest.mark.parametrize("run", ["first_run", "second_run_after_a_written_row"])
async def test_c3_quote_not_found_from_inside_the_real_write_step_is_409_never_404(
    cw: World, monkeypatch: pytest.MonkeyPatch, run: str
) -> None:
    """The REAL write step says the quote is gone (its own re-check of the quote). On its first
    run that is 409 QUOTE_NOT_FOUND. On its second run in one request (the first wrote the row and
    then met a busy error) it is that 409 or the busy 503. It is never the listed 404."""
    import routes.order_routes as order_routes
    from services.quote_service import QuoteError

    quote = cw.quotes.add()
    gone_from = 1
    if run == "second_run_after_a_written_row":
        cw.after_row_busy_runs = {1}
        gone_from = 2
    checks: List[int] = []

    async def gone(self: Any, quote_snapshot: Any, **_: Any) -> Dict[str, Any]:
        checks.append(len(checks) + 1)
        if len(checks) >= gone_from:
            raise QuoteError("QUOTE_NOT_FOUND", "Quote not found")
        return {"status": "validated", "engine": quote_snapshot.engine, "engine_ref": quote_snapshot.engine_ref}

    monkeypatch.setattr(order_routes.QuoteService, "validate_quote_snapshot_live", gone)

    answer = await post_v2(cw, v2_body(quote))

    assert (404, "QUOTE_NOT_FOUND") in LISTED and (409, "QUOTE_NOT_FOUND") not in LISTED
    assert answer.status != 404
    assert_not_listed(answer)
    if run == "first_run":
        assert cw.create_runs == ["r1"], "this refusal must come from inside the write step"
        assert answer.status == 409 and answer.body["error"]["details"]["error"] == "QUOTE_NOT_FOUND"
        assert await cw.orders_for(quote) == []
    else:
        assert cw.create_runs == ["r1", "r1"] and len(await cw.orders_for(quote)) == 1
        assert answer.status in (409, 503), (answer.status, answer.body)


# ---------------------------------------------------------------------------
# C-4: the envelope
# ---------------------------------------------------------------------------


class _Body(BaseModel):
    quote_id: str


def _envelope_app() -> FastAPI:
    """A bare app with the service's own error middleware and validation handler, as main.py
    installs them, and one route per kind of `detail`."""
    from fastapi.exceptions import RequestValidationError

    from middleware.error_handler import ErrorHandlerMiddleware, request_validation_exception_handler

    app = FastAPI()
    app.add_middleware(ErrorHandlerMiddleware)
    app.add_exception_handler(RequestValidationError, request_validation_exception_handler)

    @app.get("/named/{status}")
    async def named(status: int) -> None:
        raise HTTPException(status_code=status, detail={"error": "INVALID_QUOTE", "message": "a sentence", "reason": "why"})

    @app.get("/unnamed/{status}")
    async def unnamed(status: int) -> None:
        raise HTTPException(status_code=status, detail={"message": "Insufficient inventory", "items": []})

    @app.get("/text/{status}")
    async def text(status: int) -> None:
        raise HTTPException(status_code=status, detail="Merchant not found")

    @app.post("/validated")
    async def validated(body: _Body) -> None:
        return None

    @app.get("/busy")
    async def busy() -> None:
        from utils.transient_errors import ORDER_REPLAY_LOOKUP_UNAVAILABLE, order_existence_unknown_http_exception_fields

        raise HTTPException(**order_existence_unknown_http_exception_fields(ORDER_REPLAY_LOOKUP_UNAVAILABLE))

    return app


@pytest.fixture(scope="module")
def envelope_client() -> Iterator[TestClient]:
    with TestClient(_envelope_app(), raise_server_exceptions=False) as client:
        yield client


_STATUSES = sorted(int(s) for s in ENVELOPE["status_code"])


@pytest.mark.parametrize("status", _STATUSES)
def test_c4_the_status_alone_chooses_the_outer_code_and_the_default_message(status: int) -> None:
    from middleware.error_handler import ErrorHandlerMiddleware

    code, message = ENVELOPE["status_code"][str(status)]
    error_code = ErrorHandlerMiddleware(None)._map_status_to_error_code(status)
    assert (error_code.value.code, error_code.value.default_message) == (code, message)
    assert not CAPITAL_SNAKE.match(message), "a default message the gateway would read as a code"
    assert code not in LISTED_CODES


def test_c4_a_status_the_table_does_not_name_gets_the_500_entry() -> None:
    from middleware.error_handler import ErrorHandlerMiddleware

    for status in (402, 405, 418, 423, 504):
        error_code = ErrorHandlerMiddleware(None)._map_status_to_error_code(status)
        assert [error_code.value.code, error_code.value.default_message] == ENVELOPE["status_code"]["500"]


@pytest.mark.parametrize("status", [400, 404, 409, 503])
def test_c4_a_detail_that_names_a_code(envelope_client: TestClient, status: int) -> None:
    res = envelope_client.get(f"/named/{status}")
    body = res.json()
    assert res.status_code == status
    assert body["status"] == "error"
    assert body["error"]["details"] == {"error": "INVALID_QUOTE", "message": "a sentence", "reason": "why"}
    assert body["error"]["message"] == "INVALID_QUOTE" == body["error"]["details"]["error"]
    assert body["error"]["code"] == ENVELOPE["status_code"][str(status)][0]
    assert body["detail"] == body["error"]["details"]


@pytest.mark.parametrize("status", [400, 404, 409, 503])
def test_c4_a_detail_that_names_no_code(envelope_client: TestClient, status: int) -> None:
    res = envelope_client.get(f"/unnamed/{status}")
    body = res.json()
    assert res.status_code == status
    assert "error" not in body["error"]["details"]
    assert body["error"]["message"] == ENVELOPE["status_code"][str(status)][1]
    assert not CAPITAL_SNAKE.match(body["error"]["message"])
    assert body["error"]["code"] == ENVELOPE["status_code"][str(status)][0]


@pytest.mark.parametrize("status", [404, 500, 502])
def test_c4_a_text_detail(envelope_client: TestClient, status: int) -> None:
    res = envelope_client.get(f"/text/{status}")
    body = res.json()
    assert res.status_code == status
    assert body["error"]["details"] == {"error": "Merchant not found"}
    assert body["error"]["message"] == "Merchant not found"
    assert not CAPITAL_SNAKE.match(body["error"]["message"])
    assert body["error"]["code"] == ENVELOPE["status_code"][str(status)][0]


def test_c4_a_body_that_fails_validation_is_a_400(envelope_client: TestClient) -> None:
    res = envelope_client.post("/validated", json={})
    body = res.json()
    assert res.status_code == 400
    assert body["error"]["code"] == ENVELOPE["status_code"]["400"][0]
    assert body["error"]["message"] == ENVELOPE["validation_message"]
    assert not CAPITAL_SNAKE.match(body["error"]["message"])
    assert "error" not in body["error"]["details"] and body["error"]["details"]["validation_errors"]
    assert not ({(400, body["error"]["message"]), (400, body["error"]["code"])} & LISTED)


def test_c4_the_retriable_503_keeps_its_retry_after_and_names_its_reason(envelope_client: TestClient) -> None:
    res = envelope_client.get("/busy")
    body = res.json()
    assert res.status_code == 503 and res.headers["retry-after"] == "1"
    assert body["error"]["code"] == "EXTERNAL_SERVICE_ERROR"
    assert body["error"]["message"] == "TEMPORARY_UNAVAILABLE" == body["error"]["details"]["error"]
    assert body["error"]["details"]["reason"] == "ORDER_REPLAY_LOOKUP_UNAVAILABLE"


# ---------------------------------------------------------------------------
# C-5: the partner assertion is refused before the route body
# ---------------------------------------------------------------------------

_BAD_ASSERTIONS: List[Tuple[str, Dict[str, Any], str]] = [
    ("another_secret", {"secret": "not the secret"}, "bad_mac"),
    ("another_operation", {"op": "submit_payment"}, "op"),
    ("another_quote", {"rid": "q_oopq_another"}, "resource"),
    ("another_body", {"bh": "0" * 64}, "body"),
    ("another_path", {"p": "/agent/v2/orders/x"}, "path"),
    ("another_method", {"m": "GET"}, "method"),
    ("long_ago", {"ts": 1_000_000_000}, "stale"),
    ("nobody", {"sub": "agent_oopq_nobody"}, "subject"),
    ("no_subject_kind", {"kind": "something_else"}, "subject"),
    ("not_version_two", {"v": 3}, "malformed"),
    ("a_nonce_too_short", {"jti": "short"}, "malformed"),
]


@pytest.mark.parametrize("case", _BAD_ASSERTIONS, ids=[c[0] for c in _BAD_ASSERTIONS])
async def test_c5_a_refused_assertion_never_enters_the_route_body_and_says_why(
    cw: World, case: Tuple[str, Dict[str, Any], str]
) -> None:
    _, override, reason = case
    quote = cw.quotes.add()
    raw, headers = signed(v2_body(quote), **override)

    answer = await post_v2(cw, None, agent=GATEWAY, content=raw, headers=headers)

    assert_listed_refusal(cw, answer, 409, "PARTNER_ASSERTION_REJECTED")
    assert cw.body_entries == [], "the route body ran before the assertion was refused"
    details = answer.body["error"]["details"]
    assert details["reason"] == reason
    assert details["reason"] in ASSERTION_NOTES["reasons"]
    assert details["reason"] in ASSERTION_NOTES["decided_before_the_nonce_is_claimed"]
    assert cw.nonce_claims == [], "the nonce was claimed for an assertion that was then refused"


async def test_c5_a_replayed_assertion_says_replay_and_the_first_copy_made_the_order(cw: World) -> None:
    """The exact request, delivered twice. The second copy is refused before the route body, with
    reason `replay` at error.details.reason: an order DOES exist, made by the first copy."""
    quote = cw.quotes.add()
    raw, headers = signed(v2_body(quote))

    first = await post_v2(cw, None, agent=GATEWAY, content=raw, headers=headers, tag="r1")
    again = await post_v2(cw, None, agent=GATEWAY, content=raw, headers=headers, tag="r2")

    assert first.status == 200, first.body
    assert again.status == 409
    details = again.body["error"]["details"]
    assert details["error"] == "PARTNER_ASSERTION_REJECTED"
    assert details["reason"] == "replay"
    assert details["reason"] in ASSERTION_NOTES["can_follow_an_accepted_earlier_copy"]
    assert len(cw.nonce_claims) == 2 and cw.nonce_claims[0] == cw.nonce_claims[1]
    assert cw.body_entries == [AGENT_A] and cw.create_runs == ["r1"]
    rows = await cw.orders_for(quote, AGENT_A)
    assert [r["order_id"] for r in rows] == [first.order_id]


async def test_c5_the_one_refusal_after_the_nonce_is_claimed_is_subject_and_it_is_still_before_the_body(cw: World) -> None:
    """The assertion verifies and its nonce is claimed; then the partner's agent record cannot be
    read at adoption. Refused with `subject`, with the route body not entered and nothing written."""
    import routes.agent_auth as agent_auth

    quote = cw.quotes.add()
    raw, headers = signed(v2_body(quote))

    async def unreadable(_agent_id: str) -> None:
        raise RuntimeError(BUSY)

    cw.monkeypatch.setattr(agent_auth, "get_agent", unreadable)

    answer = await post_v2(cw, None, agent=GATEWAY, content=raw, headers=headers)

    assert_listed_refusal(cw, answer, 409, "PARTNER_ASSERTION_REJECTED")
    assert answer.body["error"]["details"]["reason"] == "subject"
    assert len(cw.nonce_claims) == 1 and cw.body_entries == []
    assert ASSERTION_NOTES["subject_has_a_second_site"]


def test_c5_in_the_verifier_every_reason_but_replay_is_returned_before_the_nonce_can_be_claimed() -> None:
    """Static: the verifier never touches the nonce store, and the dependency claims the nonce in
    exactly one place, after the verifier's verdict and after the subject was resolved."""
    verifier = _tree("services/bound_issuing_assertion.py")
    assert not any(isinstance(n, ast.Name) and n.id == "assertion_replay_guard" for n in ast.walk(verifier))
    assert not any(isinstance(n, ast.Attribute) and n.attr == "claim" for n in ast.walk(verifier))
    owner = _tree("routes/agent_forwarded_owner.py")
    claims = [
        (fn.name, call.lineno)
        for fn in ast.walk(owner) if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        for call in ast.walk(fn)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "claim"
    ]
    assert [name for name, _ in claims] == ["_verify_bound"]
    fn = next(n for n in ast.walk(owner) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_verify_bound")
    claim_line = claims[0][1]
    line_of = lambda needle: next(  # noqa: E731
        n.lineno for n in ast.walk(fn)
        if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", None)) == needle
    )
    assert line_of("verify_bound_issuing_agent_assertion") < line_of("_subject_agent_id") < claim_line
    replay_returns = [
        n.lineno for n in ast.walk(fn)
        if isinstance(n, ast.Return) and any(isinstance(c, ast.Constant) and c.value == "replay" for c in ast.walk(n))
    ]
    assert len(replay_returns) == 1 and replay_returns[0] > claim_line
    assert sorted(set(ASSERTION_NOTES["reasons"]) - {"replay"}) == ASSERTION_NOTES["decided_before_the_nonce_is_claimed"]


@pytest.mark.parametrize("accepted_at", [-60, -17, 0, 31, 60], ids=lambda v: f"accepted_{v:+d}s")
async def test_c5_an_accepted_request_delivered_again_is_only_ever_told_replay_or_stale(accepted_at: int) -> None:
    """The nonce is kept longer than the timestamp window stays open. A request accepted at any
    moment of its window and delivered again one second later, then every second after that, is
    refused by its nonce until its timestamp refuses it: it is never accepted a second time.

    Explicit clocks on both functions (their `now` parameters); no global clock is touched. This
    holds for the store that claimed the nonce, with the secret and the request unchanged."""
    import hashlib as _hashlib

    from routes.agent_forwarded_owner import REPLAY_TTL_SECONDS
    from services import assertion_replay_guard
    from services.bound_issuing_assertion import (
        MAX_SKEW_SECONDS_V2,
        sign_bound_issuing_agent_assertion,
        verify_bound_issuing_agent_assertion,
    )

    assert REPLAY_TTL_SECONDS > 2 * MAX_SKEW_SECONDS_V2
    signed_at = 1_000_000  # a timestamp inside the token; never compared with a real clock
    body_hash = _hashlib.sha256(b"{}").hexdigest()
    jti = "oopq-fixture-nonce-" + secrets.token_urlsafe(12)
    token = sign_bound_issuing_agent_assertion(
        {"v": 2, "kind": "agent", "sub": AGENT_A, "op": "create_order", "ts": signed_at, "jti": jti,
         "m": "POST", "p": "/agent/v2/orders", "rid": "q_oopq_fixture", "bh": body_hash},
        ASSERTION_SECRET,
    )

    def verdict(now: float) -> str:
        return verify_bound_issuing_agent_assertion(
            token, ops=("create_order",), method="POST", path="/agent/v2/orders", resource_id="q_oopq_fixture",
            body_hash=body_hash, secret=ASSERTION_SECRET, now=now,
        ).reason

    assertion_replay_guard.reset_local_store()
    try:
        first = signed_at + accepted_at
        assert verdict(first) == "ok"
        assert (await assertion_replay_guard.claim(jti, REPLAY_TTL_SECONDS, now=first))[0] is True
        told = set()
        for later in range(first + 1, first + REPLAY_TTL_SECONDS + 3 * MAX_SKEW_SECONDS_V2):
            reason = verdict(later)
            if reason == "ok":
                first_use, _store = await assertion_replay_guard.claim(jti, REPLAY_TTL_SECONDS, now=later)
                assert first_use is False, f"accepted a second time {later - first}s after the first"
                reason = "replay"
            told.add(reason)
        assert told <= {"replay", "stale"}, told
        assert told <= set(ASSERTION_NOTES["can_follow_an_accepted_earlier_copy"])
        assert "stale" in told
    finally:
        assertion_replay_guard.reset_local_store()


async def test_c5_an_accepted_request_delivered_again_while_the_agent_cannot_be_read_says_subject(
    cw: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Why `subject` is a reason a caller keeps its guard on. The first copy is accepted and makes
    the order. The same bytes arrive again while the partner's agent record cannot be read: the
    subject check runs BEFORE the nonce is looked at, and a failed read counts as no agent, so the
    answer is `subject`, not `replay`. An order exists."""
    import services.issuing_agent_assertion as ia

    quote = cw.quotes.add()
    raw, headers = signed(v2_body(quote))
    first = await post_v2(cw, None, agent=GATEWAY, content=raw, headers=headers, tag="r1")
    assert first.status == 200, first.body

    async def unreadable(_agent_id: Any, _excluded: Any = None) -> Optional[str]:
        raise RuntimeError(BUSY)

    monkeypatch.setattr(ia, "_active_agent_id", unreadable)
    again = await post_v2(cw, None, agent=GATEWAY, content=raw, headers=headers, tag="r2")

    assert again.status == 409
    details = again.body["error"]["details"]
    assert (details["error"], details["reason"]) == ("PARTNER_ASSERTION_REJECTED", "subject")
    assert details["reason"] in ASSERTION_NOTES["can_follow_an_accepted_earlier_copy"]
    assert len(cw.nonce_claims) == 1, "the second copy was refused before its nonce was looked at"
    assert cw.body_entries == [AGENT_A] and cw.create_runs == ["r1"]
    assert [r["order_id"] for r in await cw.orders_for(quote, AGENT_A)] == [first.order_id]


# ---------------------------------------------------------------------------
# The merchant refusal: inside the write step's function, above its order write
# ---------------------------------------------------------------------------

MERCHANT_CODE = "MERCHANT_NOT_ACCEPTING_ORDERS"
MERCHANT_MESSAGE = "This merchant is not currently able to accept orders."
KEYED = "ak_live_" + "0d" * 32


def _count_order_writes(monkeypatch: pytest.MonkeyPatch) -> List[int]:
    """Every call of the order write itself (`db.orders.create_order`: the claim, then the row)."""
    import routes.order_routes as order_routes

    writes: List[int] = []
    real = order_routes.create_order

    async def counted(*args: Any, **kwargs: Any) -> Any:
        writes.append(1)
        return await real(*args, **kwargs)

    monkeypatch.setattr(order_routes, "create_order", counted)
    return writes


def _merchant_statuses(monkeypatch: pytest.MonkeyPatch, *statuses: str) -> List[str]:
    """The merchant's status as each read of it sees it; the last one repeats."""
    import routes.order_routes as order_routes

    seen: List[str] = []

    async def merchant(merchant_id: str) -> Dict[str, Any]:
        status = statuses[min(len(seen), len(statuses) - 1)]
        seen.append(status)
        return {"merchant_id": merchant_id, "status": status}

    monkeypatch.setattr(order_routes, "get_merchant_onboarding", merchant)
    return seen


def _real_replay_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real `find_replayable_order_for_create` (the contract world doubles it with "nothing")."""
    import db.orders as orders_module
    import routes.agent_api as agent_api

    monkeypatch.setattr(agent_api, "find_replayable_order_for_create", orders_module.find_replayable_order_for_create)


def _v1_payload(w: World, quote: str) -> Dict[str, Any]:
    q = w.quotes.quotes[quote]
    return {
        "merchant_id": MERCHANT,
        "quote_id": quote,
        "customer_email": "buyer@example.com",
        "items": [{"product_id": "prod_oopq_1", "variant_id": "var_oopq_1", "quantity": 1}],
        "shipping_address": shipping_address(q["country"]),
        "currency": q["currency"],
        "metadata": {"market": q["market"]},
    }


async def _post_route(w: World, route: str, quote: str, *, tag: str = "r1", **kwargs: Any) -> Answer:
    if route == "v1":
        return await w._post("/agent/v1/orders/create", _v1_payload(w, quote), agent=AGENT_A, tag=tag, **kwargs)
    return await post_v2(w, v2_body(quote), tag=tag, **kwargs)


#: The two raise sites, by the merchant status that reaches each: one that takes no orders at all,
#: and one that is not approved, for an order charged through the merchant's own payment provider.
_MERCHANT_SITES = [("rejected", "suppressed_status"), ("pending", "not_approved_for_its_own_provider")]


@pytest.mark.parametrize("route", ["v2", "v1"])
@pytest.mark.parametrize("status, site", _MERCHANT_SITES, ids=[s for _, s in _MERCHANT_SITES])
async def test_c2_merchant_not_accepting_orders(
    cw: World, monkeypatch: pytest.MonkeyPatch, route: str, status: str, site: str
) -> None:
    writes = _count_order_writes(monkeypatch)
    _merchant_statuses(monkeypatch, status)
    quote = cw.quotes.add()

    answer = await _post_route(cw, route, quote)

    assert (403, MERCHANT_CODE) in LISTED
    assert answer.status == 403, (answer.status, answer.body)
    err = answer.body["error"]
    assert err["details"] == {"error": MERCHANT_CODE, "message": MERCHANT_MESSAGE, "merchant_id": MERCHANT}
    assert err["message"] == MERCHANT_CODE
    assert err["code"] == ENVELOPE["status_code"]["403"][0]
    # Raised inside the write step's function: it was entered once, and its order write never.
    assert cw.create_runs == ["r1"] and writes == []
    assert cw.db.count("order_insert") == 0 and cw.db.count("claim_insert") == 0
    assert await cw.orders_for(quote) == []


@pytest.mark.parametrize("route", ["v2", "v1"])
@pytest.mark.parametrize("later", [s for s, _ in _MERCHANT_SITES])
@pytest.mark.parametrize("lookup", ["finds_the_row", "cannot_read", "finds_nothing"])
async def test_c3_the_busy_retry_never_answers_the_merchant_refusal_after_the_row(
    cw: World, monkeypatch: pytest.MonkeyPatch, route: str, later: str, lookup: str
) -> None:
    """The REAL write step writes the order row, then meets a busy error; from then on the merchant
    reads as not taking orders, so a second run of the write step would raise the 403.
      finds_the_row  the real lookup: the row is answered, the write step ran once.
      cannot_read    the lookup fails after the row: the retriable 503, the write step ran once.
      finds_nothing  a lookup that misses the row: the second run reads the quote's claim before
                     the merchant refusal, finds the row, and answers 409 QUOTE_CONSUMED with it
                     (the replay that would answer it is the lookup that misses)."""
    writes = _count_order_writes(monkeypatch)
    seen = _merchant_statuses(monkeypatch, "approved", later)
    cw.after_row_busy_runs = {1}
    if lookup != "finds_nothing":
        _real_replay_lookup(monkeypatch)
    if lookup == "cannot_read":
        cw.db.fail("replay_lookup", "before", times=999, armed_after="order_insert")
    quote = cw.quotes.add()

    answer = await _post_route(cw, route, quote)

    rows = await cw.orders_for(quote)
    assert len(rows) == 1 and writes == [1], "the case must have exactly one order row behind the answer"
    assert answer.status != 403
    assert_not_listed(answer)
    if lookup == "finds_the_row":
        assert answer.status == 200 and answer.order_id == rows[0]["order_id"], answer.body
        assert cw.create_runs == ["r1"] and seen == ["approved"]
    elif lookup == "cannot_read":
        assert answer.status == 503, answer.body
        assert answer.body["error"]["details"]["reason"] == "ORDER_REPLAY_LOOKUP_UNAVAILABLE"
        assert cw.create_runs == ["r1"] and seen == ["approved"], "the write step ran again although the lookup could not tell"
    else:
        assert answer.status == 409, answer.body
        assert answer.body["error"]["details"]["error"] == "QUOTE_CONSUMED"
        assert answer.body["error"]["details"]["details"]["order_id"] == rows[0]["order_id"]
        assert cw.create_runs == ["r1", "r1"] and seen == ["approved", later], "the second run did not read the merchant"
        # The prologue's read (before the row), then one per run of the write step.
        assert cw.db.count("claim_read") == (4 if route == "v2" else 3)


@pytest.mark.parametrize("route", ["v2", "v1"])
@pytest.mark.parametrize("later", [s for s, _ in _MERCHANT_SITES])
async def test_c3_a_resend_after_the_order_is_its_replay_never_the_merchant_refusal(
    cw: World, monkeypatch: pytest.MonkeyPatch, route: str, later: str
) -> None:
    """The same create sent again after its order was made, the merchant no longer taking orders:
    the prologue's replay lookup answers the order before the write step is entered."""
    _real_replay_lookup(monkeypatch)
    writes = _count_order_writes(monkeypatch)
    seen = _merchant_statuses(monkeypatch, "approved", later)
    quote = cw.quotes.add()
    first = await _post_route(cw, route, quote, tag="r1")
    assert first.status == 200, first.body

    again = await _post_route(cw, route, quote, tag="r2")

    assert again.status == 200 and again.order_id == first.order_id, again.body
    assert cw.create_runs == ["r1"] and writes == [1] and seen == ["approved"]



# ---------------------------------------------------------------------------
# A resend after its order was made, while a live read now refuses it: answered with the order
# ---------------------------------------------------------------------------


def _store_on_another_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.agent_api as agent_api

    async def another_platform(_merchant_id: str) -> Dict[str, Any]:
        return {"platform": "woocommerce", "store_id": "store_oopq_other"}

    monkeypatch.setattr(agent_api, "get_primary_store", another_platform)


def _no_store_and_the_policy_quote_read_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """No active store row, so the commerce path is read from the quote; that read fails (only the
    one agent_create_order makes: the v2 route's own quote read still works)."""
    import sys

    import routes.agent_api as agent_api
    import routes.order_routes as order_routes

    async def no_store(_merchant_id: str) -> None:
        return None

    real = order_routes.QuoteService.load_active_quote_or_raise

    async def fails_for_the_policy(self: Any, *, quote_id: str) -> Any:
        if sys._getframe(1).f_code.co_name == "agent_create_order":
            raise RuntimeError("connection reset by peer")
        return await real(self, quote_id=quote_id)

    monkeypatch.setattr(agent_api, "get_primary_store", no_store)
    monkeypatch.setattr(order_routes.QuoteService, "load_active_quote_or_raise", fails_for_the_policy)


def _preflight_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    import mvp.offer as offer_module

    real_preflight = offer_module.preflight_offers

    def failing_preflight(**kwargs: Any) -> Any:
        return [r.model_copy(update={"status": "fail"}) for r in real_preflight(**kwargs)]

    monkeypatch.setattr(offer_module, "preflight_offers", failing_preflight)
    monkeypatch.setenv("MVP_PREFLIGHT_ENFORCE", "true")


def _merchant_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _merchant_statuses(monkeypatch, "rejected")


#: (fault, what a FIRST create meets under it: a listed code, or the 503's reason)
_LIVE_READ_FAULTS: List[Tuple[str, Any, str]] = [
    ("store_on_another_platform", _store_on_another_platform, "UNSUPPORTED_COMMERCE_PATH"),
    ("no_store_and_the_policy_quote_read_fails", _no_store_and_the_policy_quote_read_fails, "COMMERCE_PATH_UNKNOWN"),
    ("preflight_fails", _preflight_fails, "PREFLIGHT_FAILED"),
    ("merchant_rejected", _merchant_rejected, MERCHANT_CODE),
]


@pytest.mark.parametrize("route", ["v2", "v1"])
@pytest.mark.parametrize("fault, apply, first", _LIVE_READ_FAULTS, ids=[f[0] for f in _LIVE_READ_FAULTS])
async def test_c3_the_fault_alone_refuses_a_first_create(
    cw: World, monkeypatch: pytest.MonkeyPatch, route: str, fault: str, apply: Any, first: str
) -> None:
    """The control for the resend case below: each fault, on a quote with no order, is the refusal
    it is named for. A policy read that fails is the retriable 503, never "unsupported"."""
    writes = _count_order_writes(monkeypatch)
    apply(monkeypatch)
    quote = cw.quotes.add()

    answer = await _post_route(cw, route, quote)

    listed_at = [status for status, code in LISTED if code == first]
    status = listed_at[0] if listed_at else 503
    name = first
    assert answer.status == status, (answer.status, answer.body)
    details = answer.body["error"]["details"]
    if status == 503:
        assert (details["error"], details["reason"]) == ("TEMPORARY_UNAVAILABLE", name)
        assert answer.retry_after == "1"
        assert_not_listed(answer)
    else:
        assert (status, details["error"]) in LISTED and details["error"] == name
    assert writes == [] and await cw.orders_for(quote) == []


@pytest.mark.parametrize("route", ["v2", "v1"])
@pytest.mark.parametrize("fault, apply, first", _LIVE_READ_FAULTS, ids=[f[0] for f in _LIVE_READ_FAULTS])
async def test_c3_a_resend_after_the_order_is_its_replay_whatever_a_live_read_now_says(
    cw: World, monkeypatch: pytest.MonkeyPatch, route: str, fault: str, apply: Any, first: str
) -> None:
    _real_replay_lookup(monkeypatch)
    writes = _count_order_writes(monkeypatch)
    quote = cw.quotes.add()
    made = await _post_route(cw, route, quote, tag="r1")
    assert made.status == 200, made.body

    apply(monkeypatch)
    again = await _post_route(cw, route, quote, tag="r2")

    assert again.status == 200 and again.order_id == made.order_id, (again.status, again.body)
    assert cw.create_runs == ["r1"] and writes == [1]
    assert [r["order_id"] for r in await cw.orders_for(quote)] == [made.order_id]


# ---------------------------------------------------------------------------
# A create for a quote that already holds its order, outside the first create's replay scope
# ---------------------------------------------------------------------------


def _checkout_token_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real auth dependency on the checkout-token path; the token names its intent."""
    import routes.agent_auth as agent_auth

    async def from_token(request: Any, token: str) -> Any:
        context = agent_auth.AgentContext(
            {"agent_id": AGENT_A, "agent_name": "Fixture Partner", "allowed_merchants": None}, request
        )
        setattr(context, "checkout_token_payload", {"intent_id": f"intent_oopq_{token}"})
        return context

    monkeypatch.setattr(agent_auth, "_get_agent_context_from_checkout_token", from_token)


_OUTSIDE_THE_REPLAY_SCOPE = ["another_provider", "another_checkout_intent", "another_metadata_key"]


@pytest.mark.parametrize("route", ["v2", "v1"])
@pytest.mark.parametrize("scope", _OUTSIDE_THE_REPLAY_SCOPE)
async def test_c3_a_quote_that_holds_its_order_is_never_told_the_merchant_refusal(
    cw: World, monkeypatch: pytest.MonkeyPatch, route: str, scope: str
) -> None:
    """The caller's first create for the quote makes the order. The merchant is then rejected, and
    the same caller creates for the same quote again, in a way its replay lookup does not match: a
    payment provider named, another checkout intent, or another key in the order's metadata. The
    write step reads the quote's claim before the merchant refusal: the answer names the order."""
    _real_replay_lookup(monkeypatch)
    writes = _count_order_writes(monkeypatch)
    seen = _merchant_statuses(monkeypatch, "approved", "rejected")
    quote = cw.quotes.add()
    first_kw: Dict[str, Any] = {}
    second_kw: Dict[str, Any] = {}
    if scope == "another_checkout_intent":
        _checkout_token_context(monkeypatch)
        first_kw = {"headers": {"X-Checkout-Token": "a"}, "real_auth": True}
        second_kw = {"headers": {"X-Checkout-Token": "b"}, "real_auth": True}

    def body(extra: Dict[str, Any]) -> Dict[str, Any]:
        b = _v1_payload(cw, quote) if route == "v1" else v2_body(quote)
        if route == "v2":
            b.setdefault("metadata", {})
        b.update({k: v for k, v in extra.items() if k != "metadata"})
        b["metadata"] = {**(b.get("metadata") or {}), **extra.get("metadata", {})}
        return b

    path = "/agent/v1/orders/create" if route == "v1" else "/agent/v2/orders"
    first_extra = {"metadata": {"idempotency_key": "oopq-meta-key-1"}} if scope == "another_metadata_key" else {}
    second_extra = {
        "another_provider": {"preferred_psp": "adyen"},
        "another_checkout_intent": {},
        "another_metadata_key": {"metadata": {"idempotency_key": "oopq-meta-key-2"}},
    }[scope]
    made = await cw._post(path, body(first_extra), agent=AGENT_A, tag="r1", **first_kw)
    assert made.status == 200, made.body

    again = await cw._post(path, body(second_extra), agent=AGENT_A, tag="r2", **second_kw)

    assert again.status != 403, again.body
    assert_not_listed(again)
    if scope == "another_metadata_key":
        # The quote-scoped replay matches (same agent, provider and intent): the order itself.
        assert again.status == 200 and again.order_id == made.order_id, (again.status, again.body)
    else:
        # Outside this caller's replay scope (another provider, another checkout intent): never the
        # order's full answer, only that the quote has its order.
        assert again.status == 409, (again.status, again.body)
        details = again.body["error"]["details"]
        assert details["error"] == "QUOTE_CONSUMED" and details["details"]["order_id"] == made.order_id
    assert writes == [1] and [r["order_id"] for r in await cw.orders_for(quote)] == [made.order_id]
    assert cw.create_runs == ["r1"], "the quote's claim was not answered before the write step"



#: How many reads of the quote's claim a create makes before its write step: v2's own, then the
#: order create's (v1 makes only the second).
_CLAIM_READS_BEFORE_THE_WRITE_STEP = {"v2": 2, "v1": 1}


@pytest.mark.parametrize("route", ["v2", "v1"])
@pytest.mark.parametrize("where", ["before_the_write_step", "in_the_write_step"])
async def test_c3_a_claim_read_that_fails_is_the_503_and_writes_nothing(
    cw: World, monkeypatch: pytest.MonkeyPatch, route: str, where: str
) -> None:
    """A read of the quote's claim fails: "could not tell" is the retriable 503, never "no order"
    (which would let a refusal below answer), both where the create reads it before any refusal and
    where the write step reads it again before the merchant refusal."""
    writes = _count_order_writes(monkeypatch)
    _merchant_statuses(monkeypatch, "rejected")
    skip = 0 if where == "before_the_write_step" else _CLAIM_READS_BEFORE_THE_WRITE_STEP[route]
    cw.db.fail("claim_read", "before", times=999, skip=skip, message="connection reset by peer")
    quote = cw.quotes.add()

    answer = await _post_route(cw, route, quote)

    assert answer.status == 503, (answer.status, answer.body)
    assert answer.body["error"]["details"]["reason"] == "ORDER_QUOTE_GUARD_UNAVAILABLE"
    assert_not_listed(answer)
    assert writes == [] and await cw.orders_for(quote) == []
    assert cw.create_runs == ([] if where == "before_the_write_step" else ["r1"])



# ---------------------------------------------------------------------------
# Another create for a quote that holds its order, while a live read now refuses: never listed
# ---------------------------------------------------------------------------


def _variant_required(monkeypatch: pytest.MonkeyPatch, cw: World) -> None:
    """The items carry no variant (v2: the quote's; v1: the request's, see _HELD_BODY)."""

    def no_variant(_id: str, snapshot: SimpleNamespace, _n: int) -> None:
        snapshot.request_json["items"] = [{"product_id": "prod_oopq_1", "quantity": 1}]

    cw.quotes.reshape = no_variant


#: (fault, how to apply it, the listed code it alone makes a create meet)
_HELD_QUOTE_FAULTS: List[Tuple[str, Any, str]] = [
    ("store_moved", lambda mp, _cw: _store_on_another_platform(mp), "UNSUPPORTED_COMMERCE_PATH"),
    ("preflight_failing", lambda mp, _cw: _preflight_fails(mp), "PREFLIGHT_FAILED"),
    ("variant_required", _variant_required, "VARIANT_REQUIRED"),
]


def _held_body(cw: World, route: str, quote: str, fault: str) -> Dict[str, Any]:
    """A create for `quote` with a provider named (so its own replay lookup does not match)."""
    if route == "v1":
        body = {**_v1_payload(cw, quote), "preferred_psp": "adyen"}
        if fault == "variant_required":
            body["items"] = [{"product_id": "prod_oopq_1", "quantity": 1}]
        return body
    return {**v2_body(quote), "preferred_psp": "adyen"}


def _route_path(route: str) -> str:
    return "/agent/v1/orders/create" if route == "v1" else "/agent/v2/orders"


@pytest.mark.parametrize("route", ["v2", "v1"])
@pytest.mark.parametrize("fault, apply, code", _HELD_QUOTE_FAULTS, ids=[f[0] for f in _HELD_QUOTE_FAULTS])
async def test_c3_the_fault_alone_refuses_that_create(
    cw: World, monkeypatch: pytest.MonkeyPatch, route: str, fault: str, apply: Any, code: str
) -> None:
    """The control: each fault, with the same create body, on a quote that has no order."""
    writes = _count_order_writes(monkeypatch)
    quote = cw.quotes.add()
    apply(monkeypatch, cw)

    answer = await cw._post(_route_path(route), _held_body(cw, route, quote, fault), agent=AGENT_A, tag="r1")

    assert answer.body["error"]["details"]["error"] == code, (answer.status, answer.body)
    assert (answer.status, code) in LISTED and writes == []


@pytest.mark.parametrize("route", ["v2", "v1"])
@pytest.mark.parametrize("fault, apply, code", _HELD_QUOTE_FAULTS, ids=[f[0] for f in _HELD_QUOTE_FAULTS])
async def test_c3_another_create_for_a_quote_that_holds_its_order_is_never_a_listed_refusal(
    cw: World, monkeypatch: pytest.MonkeyPatch, route: str, fault: str, apply: Any, code: str
) -> None:
    """The caller's first create makes the order. Then the store moves, the pre-flight starts
    failing, or the variants are missing, and the same caller creates for the same quote with a
    payment provider named (its own replay lookup does not match; on v2 the quote's consume has not
    landed). The quote's claim is answered first: 409 QUOTE_CONSUMED naming the order, one row."""
    _real_replay_lookup(monkeypatch)
    writes = _count_order_writes(monkeypatch)
    quote = cw.quotes.add()
    made = await _post_route(cw, route, quote, tag="r1")
    assert made.status == 200, made.body
    apply(monkeypatch, cw)

    again = await cw._post(_route_path(route), _held_body(cw, route, quote, fault), agent=AGENT_A, tag="r2")

    assert_not_listed(again)
    assert again.status == 409, (again.status, again.body)
    details = again.body["error"]["details"]
    assert details["error"] == "QUOTE_CONSUMED" and details["details"]["order_id"] == made.order_id
    assert writes == [1] and [r["order_id"] for r in await cw.orders_for(quote)] == [made.order_id]
    assert cw.create_runs == ["r1"]



#: The refusals POST /agent/v2/orders makes itself, before the order create: (fault, the body's
#: buyer context, what the quote's snapshot loses, the listed code it alone makes a create meet).
_V2_OWN_REFUSALS: List[Tuple[str, Optional[str], str, str]] = [
    ("no_buyer_email", None, "customer_email", "INVALID_BUYER_CONTEXT"),
    ("no_orderable_items", "buyer@example.com", "items", "INVALID_QUOTE"),
]


def _snapshot_loses(cw: World, field: str) -> None:
    def lose(_id: str, snapshot: SimpleNamespace, _n: int) -> None:
        if field == "items":
            snapshot.request_json["items"] = []
        else:
            snapshot.request_json.pop(field, None)

    cw.quotes.reshape = lose


@pytest.mark.parametrize("held", ["no_order", "order_held"])
@pytest.mark.parametrize("fault, email, field, code", _V2_OWN_REFUSALS, ids=[f[0] for f in _V2_OWN_REFUSALS])
async def test_c3_v2_answers_a_held_quote_before_its_own_refusals(
    cw: World, monkeypatch: pytest.MonkeyPatch, held: str, fault: str, email: Optional[str], field: str, code: str
) -> None:
    """v2 refuses a create's buyer context and the quote's items itself. With no order for the quote
    that is the listed refusal (the control); for a quote that holds its order, the answer is the
    409 QUOTE_CONSUMED naming it, never the listed code."""
    _real_replay_lookup(monkeypatch)
    writes = _count_order_writes(monkeypatch)
    quote = cw.quotes.add()
    made = None
    if held == "order_held":
        made = await post_v2(cw, v2_body(quote), tag="r1")
        assert made.status == 200, made.body
    _snapshot_loses(cw, field)

    again = await post_v2(cw, {**v2_body(quote, email=email), "preferred_psp": "adyen"}, tag="r2")

    if made is None:
        assert again.body["error"]["details"]["error"] == code and (again.status, code) in LISTED, again.body
        assert writes == []
        return
    assert_not_listed(again)
    assert again.status == 409, (again.status, again.body)
    details = again.body["error"]["details"]
    assert details["error"] == "QUOTE_CONSUMED" and details["details"]["order_id"] == made.order_id
    assert writes == [1] and len(await cw.orders_for(quote)) == 1



def _agent_status(monkeypatch: pytest.MonkeyPatch, status: str) -> None:
    """The real governance check, with the agent's policy at `status`."""
    import services.agent_governance as governance

    gov = governance.agent_governance

    async def policy(_agent_id: str, **_: Any) -> Dict[str, Any]:
        return {"status": status, "max_requests_per_minute": 1000}

    monkeypatch.setattr(gov, "validate_request", type(gov).validate_request.__get__(gov))
    monkeypatch.setattr(gov, "_get_policy", policy)


_PAYMENT_SURFACE = ("client_secret", "payment_action", "payment_intent_id", "instructions")


@pytest.mark.parametrize("route", ["v2", "v1"])
@pytest.mark.parametrize("status", ["blocked", "suspended"])
@pytest.mark.parametrize("second", ["identical_resend", "another_provider"])
@pytest.mark.parametrize("consume", ["lands", "does_not_land"])
async def test_c3_a_blocked_or_suspended_agent_gets_nothing_of_its_order(
    cw: World, monkeypatch: pytest.MonkeyPatch, route: str, status: str, second: str, consume: str
) -> None:
    """The agent made an order; an operator then blocks or suspends it. Its next create for that
    quote (the same create again, or with a provider named) is refused 403 by governance: no order
    id, no payment surface. Governance runs before any read of the quote on both routes, so this
    holds when the quote's consume landed (the usual case: v2's quote read would otherwise answer
    409 QUOTE_CONSUMED with the order id) and when it did not (the claim would answer)."""
    _real_replay_lookup(monkeypatch)
    cw.quotes.consume_lands = consume == "lands"
    quote = cw.quotes.add()
    made = await _post_route(cw, route, quote, tag="r1")
    assert made.status == 200, made.body
    _agent_status(monkeypatch, status)
    body = _v1_payload(cw, quote) if route == "v1" else v2_body(quote)
    if second == "another_provider":
        body["preferred_psp"] = "adyen"

    again = await cw._post(_route_path(route), body, agent=AGENT_A, tag="r2")

    assert again.status == 403, (again.status, again.body)
    assert_not_listed(again)
    text = json.dumps(again.body)
    assert made.order_id not in text and not any(field in text for field in _PAYMENT_SURFACE), text
    assert cw.create_runs == ["r1"]
    if consume == "lands":
        assert cw.quotes.quotes[quote]["status"] == "consumed", "the case needs the consume to have landed"



def test_c1_governance_is_the_first_awaited_call_of_each_create() -> None:
    """Governance decides from the caller alone. On both routes it is the first thing a create
    awaits: before the quote's owner is checked, the quote read (v2 answers a consumed quote with
    its order id) or any claim read."""
    for path, function in (("routes/agent_v2.py", "create_order_v2"), ("routes/agent_api.py", "agent_create_order")):
        fn = next(
            n for n in ast.walk(_tree(path))
            if isinstance(n, ast.AsyncFunctionDef) and n.name == function
        )
        awaited = sorted(
            (n for stmt in fn.body for n in ast.walk(stmt) if isinstance(n, ast.Await) and isinstance(n.value, ast.Call)),
            key=_position,
        )
        assert awaited and _callee(awaited[0].value) == "validate_order_create_governance", (
            path, [_callee(a.value) for a in awaited[:3]]
        )


def test_c1_governance_moved_below_the_quote_read_is_caught() -> None:
    tree = ast.parse((REPO / "routes/agent_v2.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "create_order_v2")
    gov = _statement_holding(fn, next(c for c in ast.walk(fn) if isinstance(c, ast.Call) and _callee(c) == "validate_order_create_governance"))
    quote_read = _statement_holding(fn, next(c for c in ast.walk(fn) if isinstance(c, ast.Call) and _callee(c) == "load_active_quote_or_raise"))
    _remove(fn, gov)
    _insert_after(fn, quote_read, [gov])
    moved = next(
        n for n in ast.walk(ast.parse(ast.unparse(tree)))
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "create_order_v2"
    )
    awaited = sorted(
        (n for stmt in moved.body for n in ast.walk(stmt) if isinstance(n, ast.Await) and isinstance(n.value, ast.Call)),
        key=_position,
    )

    assert _callee(awaited[0].value) != "validate_order_create_governance"


def _count_real_governance(monkeypatch: pytest.MonkeyPatch) -> Tuple[List[str], Any]:
    """The REAL governance check, counted: every call of `validate_request`, and the agent's real
    request counter. Only the policy row is given (the test database has no policies table)."""
    import services.agent_governance as governance

    gov = governance.agent_governance
    real = type(gov).validate_request.__get__(gov)
    calls: List[str] = []

    async def counted(agent_id: str, *, fail_closed: bool = False) -> None:
        calls.append(agent_id)
        await real(agent_id, fail_closed=fail_closed)

    async def policy(_agent_id: str, **_: Any) -> Dict[str, Any]:
        return {"status": "active", "max_requests_per_minute": 100000}

    monkeypatch.setattr(gov, "validate_request", counted)
    monkeypatch.setattr(gov, "_get_policy", policy)
    monkeypatch.setattr(gov, "_metrics_store", {})
    return calls, gov


@pytest.mark.parametrize("route", ["v2", "v1"])
@pytest.mark.parametrize("consume", ["lands", "does_not_land"])
async def test_c3_governance_counts_each_create_once(
    cw: World, monkeypatch: pytest.MonkeyPatch, route: str, consume: str
) -> None:
    """A create, its identical resend, and a create for the held quote with a provider named each
    pass governance exactly once, and count once against the agent's per-minute rate: v2 runs it
    itself and the order create then skips it."""
    _real_replay_lookup(monkeypatch)
    calls, gov = _count_real_governance(monkeypatch)
    cw.quotes.consume_lands = consume == "lands"
    quote = cw.quotes.add()

    answers = []
    for n, body in enumerate([None, None, {"preferred_psp": "adyen"}], start=1):
        payload = _v1_payload(cw, quote) if route == "v1" else v2_body(quote)
        payload.update(body or {})
        answers.append(await cw._post(_route_path(route), payload, agent=AGENT_A, tag=f"r{n}"))
        assert calls == [AGENT_A] * n, (n, calls, answers[-1].status, answers[-1].body)
        assert gov._metrics_store[AGENT_A].requests == n, (n, gov._metrics_store[AGENT_A].requests)

    made, again, other = answers
    assert made.status == 200, made.body
    assert again.status in (200, 409) and other.status == 409, (again.body, other.body)
    assert cw.create_runs == ["r1"]


async def test_c3_v2_a_claim_read_that_fails_once_on_a_held_quote_is_the_503(
    cw: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only v2's own read of the quote's claim fails, once, for a quote that holds its order, on a
    create v2 would itself refuse (no buyer email anywhere). "Could not tell" is the 503, never a
    listed refusal and never "no holder" (the order create's own read would not be reached)."""
    _real_replay_lookup(monkeypatch)
    writes = _count_order_writes(monkeypatch)
    quote = cw.quotes.add()
    made = await post_v2(cw, v2_body(quote), tag="r1")
    assert made.status == 200, made.body
    _snapshot_loses(cw, "customer_email")
    reads_before = cw.db.count("claim_read")
    cw.db.fail("claim_read", "before", times=1, tag="r2")

    again = await post_v2(cw, {**v2_body(quote, email=None), "preferred_psp": "adyen"}, tag="r2")

    assert again.status == 503, (again.status, again.body)
    assert again.body["error"]["details"]["reason"] == "ORDER_QUOTE_GUARD_UNAVAILABLE"
    assert_not_listed(again)
    assert cw.db.count("claim_read") == reads_before + 1, "more than v2's own read ran"
    assert cw.create_runs == ["r1"] and writes == [1]


@pytest.mark.parametrize("first_made_with", ["api_key", "another_checkout_intent"])
async def test_c3_v2_a_checkout_token_never_gets_an_order_made_outside_its_intent(
    cw: World, monkeypatch: pytest.MonkeyPatch, first_made_with: str
) -> None:
    """Independent of the merchant-refusal cases: the order is made under the agent's key (or under
    another checkout intent), and a checkout token of the same agent sends the identical v2 create.
    The token's intent narrows the replay: 409 QUOTE_CONSUMED, never the order's answer."""
    _real_replay_lookup(monkeypatch)
    _checkout_token_context(monkeypatch)
    quote = cw.quotes.add()
    first_kw: Dict[str, Any] = {}
    if first_made_with == "another_checkout_intent":
        first_kw = {"headers": {"X-Checkout-Token": "first"}, "real_auth": True}
    made = await post_v2(cw, v2_body(quote), tag="r1", **first_kw)
    assert made.status == 200, made.body

    again = await post_v2(cw, v2_body(quote), tag="r2", headers={"X-Checkout-Token": "second"}, real_auth=True)

    assert again.status == 409, (again.status, again.body)
    details = again.body["error"]["details"]
    assert details["error"] == "QUOTE_CONSUMED" and details["details"]["order_id"] == made.order_id
    assert not any(field in json.dumps(again.body) for field in _PAYMENT_SURFACE)
    assert cw.create_runs == ["r1"] and len(await cw.orders_for(quote)) == 1


async def test_c3_a_failed_policy_read_is_logged_at_error_with_its_type(
    cw: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 503 for a commerce path that could not be decided leaves an ERROR line naming the
    exception's type, on the service's own logger (the one production keeps), so a failure that is
    not transient does not hide behind "retry shortly"."""
    import routes.agent_api as agent_api

    errors: List[str] = []
    real_logger = agent_api.logger

    class _Spy:
        def __getattr__(self, name: str) -> Any:
            return getattr(real_logger, name)

        def error(self, message: str, *args: Any, **kwargs: Any) -> None:
            errors.append(message % args if args else message)
            real_logger.error(message, *args, **kwargs)

    from utils.logger import logger as service_logger

    assert real_logger is service_logger
    monkeypatch.setattr(agent_api, "logger", _Spy())
    _no_store_and_the_policy_quote_read_fails(monkeypatch)
    quote = cw.quotes.add()

    answer = await _post_route(cw, "v1", quote)

    assert answer.status == 503 and answer.body["error"]["details"]["reason"] == "COMMERCE_PATH_UNKNOWN"
    assert [e for e in errors if "commerce path unknown" in e and "RuntimeError" in e], errors


# ---------------------------------------------------------------------------
# Why no 429 is listed: a resend of an accepted create can be refused 429 while its order exists
# ---------------------------------------------------------------------------


def _rate_limiter(w: World) -> Any:
    """The service's own RateLimitMiddleware instance, in the app the cases call."""
    from middleware.rate_limiter import RateLimitMiddleware

    if w.app.middleware_stack is None:
        w.app.middleware_stack = w.app.build_middleware_stack()
    node = w.app.middleware_stack
    while node is not None:
        if isinstance(node, RateLimitMiddleware):
            return node
        node = getattr(node, "app", None)
    raise AssertionError("RateLimitMiddleware is not in the app's middleware stack")


def _keyed_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.agent_auth as agent_auth

    async def by_key(_key: str, **_: Any) -> Dict[str, Any]:
        return {"agent_id": AGENT_A, "agent_name": "Fixture Partner", "is_active": True, "allowed_merchants": None}

    monkeypatch.setattr(agent_auth, "get_agent_by_key", by_key)


def _auth_rate_limit_over(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.agent_auth as agent_auth

    async def over(*_: Any, **__: Any) -> Tuple[bool, int, int]:
        return False, 11, 10

    monkeypatch.setattr(agent_auth, "check_rate_limit", over)


def _governance_over_its_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.agent_governance as governance

    gov = governance.agent_governance

    async def policy(_agent_id: str, **_: Any) -> Dict[str, Any]:
        return {"status": "active", "max_requests_per_minute": 1}

    monkeypatch.setattr(gov, "validate_request", type(gov).validate_request.__get__(gov))
    monkeypatch.setattr(gov, "_get_policy", policy)
    monkeypatch.setattr(gov, "_get_current_rpm", lambda _agent_id: 5.0)


async def test_an_accepted_signed_request_delivered_again_can_be_refused_429_before_its_nonce(
    cw: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact signed request, delivered twice. The first copy makes the order. The second meets
    the rate-limit middleware's ceiling, which runs before the partner assertion's nonce is looked at
    and before any replay lookup: a 429 with the first copy's order behind it."""
    quote = cw.quotes.add()
    raw, headers = signed(v2_body(quote))
    first = await post_v2(cw, None, agent=GATEWAY, content=raw, headers=headers, tag="r1")
    assert first.status == 200, first.body
    lookups = cw.db.count("replay_lookup")

    limiter = _rate_limiter(cw)
    monkeypatch.setattr(limiter, "anon_enabled", True)
    monkeypatch.setattr(limiter, "anon_global_rpm", 0)
    again = await post_v2(cw, None, agent=GATEWAY, content=raw, headers=headers, tag="r2")

    assert again.status == 429, again.body
    assert again.retry_after is not None
    details = again.body["error"]["details"]
    assert details["error"] == "rate_limit_exceeded" and set(details) == {"error", "message", "retry_after"}
    assert (429, details["error"]) not in LISTED
    assert [r["order_id"] for r in await cw.orders_for(quote, AGENT_A)] == [first.order_id]
    assert len(cw.nonce_claims) == 1, "the resend was refused before its nonce was looked at"
    assert cw.db.count("replay_lookup") == lookups and cw.create_runs == ["r1"]


@pytest.mark.parametrize("route", ["v2", "v1"])
@pytest.mark.parametrize("limit", ["auth_dependency", "governance"])
async def test_a_resend_after_the_order_can_be_refused_429_before_the_replay_lookup(
    cw: World, monkeypatch: pytest.MonkeyPatch, route: str, limit: str
) -> None:
    """The same caller's create again, after its order was made. Without a limit the resend is
    answered with that order (the control). Over the auth dependency's limit, or governance's (the
    order create's first step), it is a 429 before the replay lookup reads anything."""
    _real_replay_lookup(monkeypatch)
    _keyed_agent(monkeypatch)
    keyed: Dict[str, Any] = {"headers": {"X-API-Key": KEYED}, "real_auth": True}
    quote = cw.quotes.add()
    first = await _post_route(cw, route, quote, tag="r1", **keyed)
    assert first.status == 200, first.body
    control = await _post_route(cw, route, quote, tag="r2", **keyed)
    assert control.status == 200 and control.order_id == first.order_id, control.body
    lookups = cw.db.count("replay_lookup")

    if limit == "auth_dependency":
        _auth_rate_limit_over(monkeypatch)
        sentence = "Rate limit exceeded: 11/10 requests per minute"
    else:
        _governance_over_its_limit(monkeypatch)
        sentence = "Rate limit exceeded: 5 requests/minute (limit: 1)"
    again = await _post_route(cw, route, quote, tag="r3", **keyed)

    assert again.status == 429, again.body
    assert again.body["error"]["details"] == {"error": sentence}
    assert again.body["error"]["code"] == ENVELOPE["status_code"]["429"][0]
    assert not (pairs_of(again) & LISTED)
    assert [r["order_id"] for r in await cw.orders_for(quote, AGENT_A)] == [first.order_id]
    assert cw.db.count("replay_lookup") == lookups, "the 429 came after the replay lookup"
    assert cw.create_runs == ["r1"]
