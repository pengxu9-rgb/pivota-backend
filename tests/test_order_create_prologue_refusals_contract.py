"""The order-create refusals that are answered before any order can exist: a contract with the gateway.

The agent gateway holds a guard on a quote while it creates the order, and gives the guard back only
when the backend answers one of nine exact (status, code) pairs. That is safe only while each pair
is answered from the request's PROLOGUE, strictly before the order write step (`create_new_order`)
is first entered, and while nothing that can run after an order row is written answers one of them.
The list lives in contracts/order_create_prologue_refusals.json; the gateway vendors it.

    C-0  the list itself (nine pairs, sha256 pinned, no 503, none of this change's new refusals)
    C-1  where each listed code is written in this repository (an AST inventory, pinned), and that
         the write step and everything it calls writes none of them
    C-2  each pair, through the real route and the real error envelope, with the write step entered
         0 times
    C-3  an order row written, then every failure that can follow it: never a listed pair
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
ENVELOPE_PATH = REPO / "contracts" / "error_envelope_status_codes.json"

# Changing either file is a change to what the gateway may do with money. Update the hash here and
# re-vendor the file in the gateway in the same change.
CONTRACT_SHA256 = "a849af28a400f09c0f8e02d2c2e2d1e7d213356e73c3ae96a352fbd79ee3ebf0"
ENVELOPE_SHA256 = "74e2875f61e8c3a96ba7a4a768731c7052d346d0ee3faec10c7c8954fdd1a3f5"

CONTRACT = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
ENVELOPE = json.loads(ENVELOPE_PATH.read_text(encoding="utf-8"))
LISTED: Set[Tuple[int, str]] = {
    (int(status), code) for status, codes in CONTRACT["refused_before_any_order"].items() for code in codes
}
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


def test_c0_the_list_is_the_nine_pairs_and_its_bytes_are_pinned() -> None:
    assert hashlib.sha256(CONTRACT_PATH.read_bytes()).hexdigest() == CONTRACT_SHA256, (
        "contracts/order_create_prologue_refusals.json changed: update CONTRACT_SHA256 and re-vendor it in the gateway"
    )
    assert hashlib.sha256(ENVELOPE_PATH.read_bytes()).hexdigest() == ENVELOPE_SHA256, (
        "contracts/error_envelope_status_codes.json changed: update ENVELOPE_SHA256 and re-vendor it in the gateway"
    )
    assert LISTED == {
        (400, "INVALID_QUOTE"),
        (400, "INVALID_BUYER_CONTEXT"),
        (400, "QUOTE_REQUIRED_BEFORE_PURCHASE"),
        (400, "QUOTE_MERCHANT_MISMATCH"),
        (400, "VARIANT_REQUIRED"),
        (404, "QUOTE_NOT_FOUND"),
        (409, "PARTNER_ASSERTION_REJECTED"),
        (409, "UNSUPPORTED_COMMERCE_PATH"),
        (409, "PREFLIGHT_FAILED"),
    }
    assert sum(len(codes) for codes in CONTRACT["refused_before_any_order"].values()) == 9, "a duplicate pair"
    assert CONTRACT["envelope"] == {**CONTRACT["envelope"], "code": "error.details.error", "reason": "error.details.reason"}
    assert CONTRACT["notes"]["PARTNER_ASSERTION_REJECTED"]["reason_that_is_not_a_plain_refusal"] == "replay"


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
    assert (409, "QUOTE_CONSUMED") not in LISTED and "QUOTE_CONSUMED" not in LISTED_CODES


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
#: ON the order-create path of POST /agent/v2/orders (all prologue or the route dependency):
ON_PATH = {
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


def test_c1_the_write_step_and_everything_it_calls_writes_no_listed_code() -> None:
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
    # The one exception: the quote re-read's QuoteError("QUOTE_NOT_FOUND"). The write step answers
    # every QuoteError with 409 (C-3 drives it), and the listed pair is the 404.
    assert hits == {("services/quote_service.py", "load_active_quote_or_raise", "QUOTE_NOT_FOUND")}


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
    assert sorted(reasons) == CONTRACT["notes"]["PARTNER_ASSERTION_REJECTED"]["reasons"]
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
    # as the busy 503. Either answer is acceptable here; a listed pair is not.
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
    assert details["reason"] in CONTRACT["notes"]["PARTNER_ASSERTION_REJECTED"]["reasons"]
    assert details["reason"] != CONTRACT["notes"]["PARTNER_ASSERTION_REJECTED"]["reason_that_is_not_a_plain_refusal"]


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
    assert details["reason"] == "replay" == CONTRACT["notes"]["PARTNER_ASSERTION_REJECTED"]["reason_that_is_not_a_plain_refusal"]
    assert cw.body_entries == [AGENT_A] and cw.create_runs == ["r1"]
    rows = await cw.orders_for(quote, AGENT_A)
    assert [r["order_id"] for r in rows] == [first.order_id]
