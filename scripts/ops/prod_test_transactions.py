"""Prod test transactions: a FIXED list of test calls against the prod gateway with a dedicated first-party test agent
key. The ONE sanctioned exception to the no-prod-probe rule: only this job, only this key, only on the owner's go per
run, launched by infra/gcp/cloudbuild.prod_test_transactions.yaml (scripts/ops/prod_test_transactions_launch.py).

    python -m scripts.ops.prod_test_transactions        # only as the armed `prod-test-transactions` Cloud Run job

STOP POINT: a checkout is created, read back and CANCELLED. Nothing is ever submitted for payment. The transport
refuses every tool or operation outside ALLOWED_CALLS and any argument that carries payment or purchase members, before
a byte leaves the job.

Inputs (set ONLY on the armed job by the launcher; the disarmed job has none of them, so a re-execution refuses):
  PROD_TEST_TXN_ARMED=1
  PROD_TEST_TXN_CASES            comma-separated case ids from CASES, or `all`
  PROD_TEST_TXN_RUN_ID           the Cloud Build id; every idempotency key is derived from it and the case id
  PROD_TEST_TXN_GATE             {verdict, observed_at, summary, ...} from a real scripts/ops/oneoff_gate.py run
  PROD_TEST_TXN_RELEASE_COMMIT   must equal /app/.image_commit_sha
  /var/run/pivota-test-agent/key the test agent key, a Secret Manager file mount by version. Read once, kept in memory,
                                 sent only as the X-Agent-API-Key header, never logged, never hashed into output

Output: TEXT lines `PROD_TEST_TXN_RESULT <base64 compact JSON>`: one `"kind": "case"` line per case
{case, door, pass, expected, got, created, cancelled, left_open, error} and a final `"kind": "run"` line
{ok, cases, passed, calls, left_open}. `got` holds ONLY the asserted fields; no response body is ever printed.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any, Callable
from urllib.parse import urlsplit

JOB_NAME = "prod-test-transactions"
ENV = "PROD_TEST_TXN_"
PREFIX = "PROD_TEST_TXN_RESULT "
KEY_SECRET = "prod-test-agent-key"
KEY_MOUNT_DIR = "/var/run/pivota-test-agent"
KEY_FILE = "key"
KEY_PATTERN = re.compile(r"ak_(?:live_)?[0-9a-f]{64}")  # the gateway's own key format (server.js agent key check)
GATE_SOURCE = "scripts/ops/oneoff_gate.py"
GATE_MAX_AGE_SECONDS = 180
CLOSED_WINDOWS_UTC = ((5 * 60 + 15, 6 * 60 + 15), (10 * 60 + 30, 11 * 60 + 20))
GATEWAY_BASE = "https://gateway.pivota.cc"
ALLOWED_HOSTS = frozenset({"gateway.pivota.cc"})
MAX_CALLS_PER_RUN = 40
CALL_TIMEOUT_SECONDS = 20.0
MAX_BODY_BYTES = 2_000_000

# The ONLY calls this job may make, per door. Everything else (complete_checkout_session, complete_checkout,
# create_payment_link, update_*, start_purchase, preview_merchant_checkout_quote, submit_payment, confirm_payment,
# create_order, ...) is refused at the transport. Payment submission is ENABLED in prod, so this list is the guard.
ALLOWED_CALLS = {
    "/agent/shop/v1/invoke": frozenset({"offers.resolve"}),
    "/mcp": frozenset({"create_checkout_session", "get_checkout_session", "cancel_checkout_session"}),
    "/ucp/mcp": frozenset({"create_checkout", "get_checkout"}),
}
# Members that must never appear anywhere in a request: they pay, buy, or open the Reap purchase lane.
FORBIDDEN_MEMBERS = frozenset({"payment", "payment_method", "payment_token", "payment_handlers", "reap", "card",
                               "token", "credential", "cvv", "pan", "confirm", "submit", "order_id"})

# A product the offers.resolve cases read (read-only). Filled in REVIEW with a connected TEST merchant's product id;
# empty refuses every case that needs it, so an unreviewed run cannot pick its own target.
OFFERS_PRODUCT_ID = ""


class CaseError(ValueError):
    """A case list, flag or input problem: the run refuses before any call."""


def require(test, message: str) -> None:
    if not test:
        raise CaseError(message)


# ------------------------------------------------------------------ the case list (fixed, reviewed)

@dataclass(frozen=True)
class Case:
    case_id: str
    door: str
    kind: str                      # "read" (one call, assert the answer) or "checkout" (create -> read -> cancel)
    call: str                      # the operation or tool name
    request: dict
    market: str                    # the market as sent, for the report: US / SG / GB / DE / none / USA / ſg ...
    expected: dict                 # dotted path in the parsed answer -> exact expected value
    requires_flags: dict = field(default_factory=dict)   # gateway env name -> exact plain value
    needs: tuple = ()              # inputs that must be reviewed in before the case can run ("OFFERS_PRODUCT_ID")
    enabled: bool = True           # False: listed for review, refused by select_cases until decided


def _offers(market_member: dict | None, outer: dict | None = None) -> dict:
    offers = {"product": {"product_id": OFFERS_PRODUCT_ID}, "limit": 3}
    if market_member is not None:
        offers.update(market_member)
    return {"operation": "offers.resolve", "payload": {"offers": offers, **(outer or {})}, "metadata": {}}


SERVED = {"error.code": None}
CASES: tuple[Case, ...] = (
    # Path 4: offers.resolve market refusals (gw#2601 / gw#2602; backend be#2843 / be#2848). Read only.
    Case("OR-US", "/agent/shop/v1/invoke", "read", "offers.resolve", _offers({"market": "US"}), "US", SERVED,
         needs=("OFFERS_PRODUCT_ID",)),
    Case("OR-SG", "/agent/shop/v1/invoke", "read", "offers.resolve", _offers({"market": "SG"}), "SG", SERVED,
         needs=("OFFERS_PRODUCT_ID",)),
    Case("OR-us-lower", "/agent/shop/v1/invoke", "read", "offers.resolve", _offers({"market": "us"}), "us", SERVED,
         needs=("OFFERS_PRODUCT_ID",)),
    Case("OR-none", "/agent/shop/v1/invoke", "read", "offers.resolve", _offers(None), "none", SERVED,
         needs=("OFFERS_PRODUCT_ID",)),
    Case("OR-USA", "/agent/shop/v1/invoke", "read", "offers.resolve", _offers({"market": "USA"}), "USA",
         {"status": "error", "error.code": "MARKET_INVALID", "error.reason": "market_invalid"},
         needs=("OFFERS_PRODUCT_ID",)),
    Case("OR-long-s", "/agent/shop/v1/invoke", "read", "offers.resolve", _offers({"market": "ſg"}), "ſg",
         {"status": "error", "error.code": "MARKET_INVALID", "error.reason": "market_invalid"},
         needs=("OFFERS_PRODUCT_ID",)),
    Case("OR-DE", "/agent/shop/v1/invoke", "read", "offers.resolve", _offers({"market": "DE"}), "DE",
         {"status": "error", "error.code": "MARKET_NOT_PRICED", "error.reason": "market_not_priced"},
         needs=("OFFERS_PRODUCT_ID",)),
    Case("OR-USA-beside-SG", "/agent/shop/v1/invoke", "read", "offers.resolve",
         _offers({"market": "USA"}, outer={"market": "SG"}), "USA+SG", SERVED, needs=("OFFERS_PRODUCT_ID",)),
    # Paths 1-3 (UCP checkout currency, /mcp storefront cart with `market`, a store-cart create for the shadow catalog
    # lookup) need an end-user identity (create/get/cancel are user_ref tools) and a decision on storefront carts,
    # which cannot be cancelled. They are added once the owner decides; see docs/runbooks/prod_test_transactions.md.
)

GATEWAY_FLAG_NAMES = tuple(sorted({name for case in CASES for name in case.requires_flags}))
_REVIEWED_INPUTS = {"OFFERS_PRODUCT_ID": lambda: OFFERS_PRODUCT_ID}


def check_case_list(cases: tuple[Case, ...] = CASES) -> None:
    """Static rules every listed case must satisfy, enforced at import time and in the tests."""
    ids = [c.case_id for c in cases]
    require(len(ids) == len(set(ids)), "duplicate case ids")
    for case in cases:
        require(re.fullmatch(r"[A-Za-z0-9-]{2,40}", case.case_id), f"bad case id {case.case_id!r}")
        require(case.door in ALLOWED_CALLS and case.call in ALLOWED_CALLS[case.door],
                f"{case.case_id}: {case.call} on {case.door} is not an allowed call")
        require(case.kind in ("read", "checkout"), f"{case.case_id}: unknown kind")
        require(case.expected, f"{case.case_id}: a case must state its expected outcome")
        forbidden_members(case.request)


def select_cases(raw: str) -> list[Case]:
    """The cases a run executes, from the launcher's comma list or `all`. Unknown, disabled or unreviewed -> refuse."""
    text = (raw or "").strip()
    require(text, "no cases selected")
    by_id = {c.case_id: c for c in CASES}
    names = [c.case_id for c in CASES if c.enabled] if text == "all" else [n.strip() for n in text.split(",")]
    require(names and len(names) == len(set(names)), "case list is empty or repeats a case")
    chosen = []
    for name in names:
        require(name in by_id, f"unknown case {name!r}")
        case = by_id[name]
        require(case.enabled, f"case {name} is listed for review only")
        for need in case.needs:
            require(_REVIEWED_INPUTS[need](), f"case {name} needs {need}, which has not been reviewed in")
        chosen.append(case)
    return chosen


def require_flags(cases: list[Case], flags: dict) -> None:
    for case in cases:
        for name, value in case.requires_flags.items():
            require(flags.get(name) == value, f"case {case.case_id} needs {name}={value}; gateway has {flags.get(name)!r}")


# ------------------------------------------------------------------ guards

def forbidden_members(value: Any, path: str = "") -> None:
    """Refuse any request member that pays, buys or opens a purchase lane, at any depth."""
    if isinstance(value, dict):
        for key, inner in value.items():
            require(str(key).lower() not in FORBIDDEN_MEMBERS, f"forbidden request member {path}{key}")
            forbidden_members(inner, f"{path}{key}.")
    elif isinstance(value, list):
        for index, inner in enumerate(value):
            forbidden_members(inner, f"{path}{index}.")


def call_name(door: str, body: dict) -> str:
    if door == "/agent/shop/v1/invoke":
        return str(body.get("operation") or "")
    require(body.get("method") == "tools/call", "only tools/call is allowed on an MCP door")
    return str((body.get("params") or {}).get("name") or "")


class Transport:
    """The ONLY way out of the job: allow-listed host, door and call; a hard call cap; one timeout; no retries."""

    def __init__(self, key: str, *, post: Callable[[str, dict, dict, float], tuple[int, bytes]] | None = None,
                 base: str = GATEWAY_BASE, max_calls: int = MAX_CALLS_PER_RUN):
        require(KEY_PATTERN.fullmatch(key or ""), "the test agent key is not in the gateway's key format")
        require(urlsplit(base).scheme == "https" and urlsplit(base).hostname in ALLOWED_HOSTS, "base host not allowed")
        self._key, self._post, self._base, self._max = key, post or _httpx_post, base, max_calls
        self.calls = 0

    def send(self, door: str, body: dict) -> tuple[int, Any]:
        require(door in ALLOWED_CALLS, f"door {door} is not allowed")
        name = call_name(door, body)
        require(name in ALLOWED_CALLS[door], f"{name} on {door} is not an allowed call")
        forbidden_members(body.get("payload") if door == "/agent/shop/v1/invoke" else (body.get("params") or {}).get(
            "arguments"))
        require(self.calls < self._max, "the per-run call cap is reached")
        self.calls += 1
        url = self._base + door
        require(urlsplit(url).hostname in ALLOWED_HOSTS, "host not allowed")
        status, raw = self._post(url, {"X-Agent-API-Key": self._key, "Content-Type": "application/json",
                                       "Accept": "application/json, text/event-stream"}, body, CALL_TIMEOUT_SECONDS)
        require(len(raw) <= MAX_BODY_BYTES, "answer too large")
        return status, parse_answer(door, raw)


def _httpx_post(url: str, headers: dict, body: dict, timeout: float) -> tuple[int, bytes]:  # pragma: no cover
    import httpx

    # One attempt, no redirects (a redirect could leave the allowed host), no retries.
    with httpx.Client(timeout=timeout, follow_redirects=False, transport=httpx.HTTPTransport(retries=0)) as client:
        response = client.post(url, headers=headers, json=body)
        return response.status_code, response.content


def parse_answer(door: str, raw: bytes) -> Any:
    """The parsed answer: invoke -> its JSON body; MCP -> {is_error, data} from result.content[0].text."""
    text = raw.decode("utf-8", "replace").strip()  # the gateway may write heartbeat newlines before the body
    try:
        body = json.loads(text) if text else None
    except ValueError:
        return {"unparsed": True}
    if door == "/agent/shop/v1/invoke" or not isinstance(body, dict):
        return body
    if "error" in body and "result" not in body:
        return {"is_error": True, "data": {"error": {"code": (body["error"] or {}).get("code")}}}
    result = body.get("result") or {}
    content = result.get("content") or []
    inner = content[0].get("text") if content and isinstance(content[0], dict) else None
    try:
        data = json.loads(inner) if isinstance(inner, str) else result.get("structuredContent")
    except ValueError:
        data = {"unparsed": True}
    return {"is_error": bool(result.get("isError")), "data": data}


def dig(value: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def scrub(answer: Any, paths) -> dict:
    """ONLY the asserted fields, each a scalar or None: a nested object is reported by its type, never its content."""
    got = {}
    for path in paths:
        value = dig(answer, path)
        got[path] = value if value is None or isinstance(value, (str, int, float, bool)) else f"<{type(value).__name__}>"
    return got


# ------------------------------------------------------------------ running the cases

def idempotency_key(run_id: str, case_id: str, step: str) -> str:
    return f"ptt-{run_id[:8]}-{case_id}-{step}"


def run_read(case: Case, transport: Transport) -> dict:
    status, answer = transport.send(case.door, case.request)
    got = scrub(answer, case.expected)
    got["http_status"] = status
    passed = status == 200 and all(got[p] == v for p, v in case.expected.items())
    return {"pass": passed, "got": got, "created": [], "cancelled": [], "left_open": []}


def mcp_body(tool: str, arguments: dict, request_id: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}


# Where a checkout's id and status sit in each door's create/get answer, and the tool that reads it back.
CHECKOUT_DOORS = {
    "/mcp": {"get": "get_checkout_session", "id": "data.session_id", "status": "data.status",
             "get_args": lambda sid: {"session_id": sid}},
    "/ucp/mcp": {"get": "get_checkout", "id": "data.id", "status": "data.status",
                 "get_args": lambda sid: {"meta": {}, "id": sid}},
}
STOREFRONT_PREFIX = "esc_"     # a stateless storefront checkout: the gateway refuses to cancel it by design


def run_checkout(case: Case, transport: Transport, run_id: str, row: dict) -> None:
    """create -> read back -> ALWAYS cancel (kernel sessions) -> read back canceled. Updates `row` as it goes, so a
    failure at any step still reports what was created. A create that fails WITHOUT an answer may still have made a
    session the job cannot name: that is `left_open` as `unknown:<case>` and the run is not ok."""
    door = CHECKOUT_DOORS[case.door]
    row.update(created=[], cancelled=[], left_open=[], got={})
    arguments = json.loads(json.dumps(case.request))
    if case.door == "/mcp":
        arguments["idempotency_key"] = idempotency_key(run_id, case.case_id, "create")
    else:
        arguments.setdefault("meta", {})["idempotency-key"] = idempotency_key(run_id, case.case_id, "create")
    try:
        status, answer = transport.send(case.door, mcp_body(case.call, arguments))
    except Exception:
        row["left_open"] = [f"unknown:{case.case_id}"]
        raise
    got = scrub(answer, case.expected)
    got["http_status"] = status
    row["got"] = got
    session_id = dig(answer, door["id"])
    if not (status == 200 and isinstance(session_id, str) and session_id):
        # No session named: either a designed refusal (expected says so) or a failure; nothing to cancel.
        row["pass"] = status == 200 and all(got[p] == v for p, v in case.expected.items())
        return
    row["created"] = [session_id]
    row["left_open"] = [session_id]          # until the cancel is confirmed
    read_ok = False
    try:
        rstatus, read = transport.send(case.door, mcp_body(door["get"], door["get_args"](session_id), 2))
        got["read_back.status"] = dig(read, door["status"])
        read_ok = rstatus == 200 and got["read_back.status"] == dig(answer, door["status"])
    finally:
        cancel_checkout(case, transport, run_id, session_id, row)
    row["pass"] = (status == 200 and all(got[p] == v for p, v in case.expected.items()) and read_ok
                   and not row["left_open"])


def cancel_checkout(case: Case, transport: Transport, run_id: str, session_id: str, row: dict) -> None:
    """Cancel through /mcp cancel_checkout_session (the kernel's one cancel), then confirm it reads back canceled."""
    if session_id.startswith(STOREFRONT_PREFIX):
        row["cancel_note"] = "storefront checkout: the gateway refuses to cancel it; the seller cart expires"
        return                               # stays in left_open: reported, and the run is not ok
    try:
        cstatus, cancel = transport.send("/mcp", mcp_body("cancel_checkout_session", {
            "idempotency_key": idempotency_key(run_id, case.case_id, "cancel"), "session_id": session_id}, 3))
        canceled = cstatus == 200 and dig(cancel, "data.status") == "canceled"
        rstatus, read = transport.send("/mcp", mcp_body("get_checkout_session", {"session_id": session_id}, 4))
        if canceled and rstatus == 200 and dig(read, "data.status") == "canceled":
            row["cancelled"] = [session_id]
            row["left_open"] = []
        else:
            row["cancel_note"] = f"cancel answered {dig(cancel, 'data.status')!r}, read back {dig(read, 'data.status')!r}"
    except Exception as error:  # noqa: BLE001 - reported; the session stays in left_open
        row["cancel_note"] = f"cancel failed: {type(error).__name__}: {str(error)[:150]}"


def run_case(case: Case, transport: Transport, run_id: str) -> dict:
    row = {"kind": "case", "case": case.case_id, "door": case.door, "call": case.call, "market": case.market,
           "expected": case.expected, "error": None, "created": [], "cancelled": [], "left_open": [], "got": None}
    try:
        if case.kind == "checkout":
            run_checkout(case, transport, run_id, row)
            return row
        row.update(run_read(case, transport))
    except Exception as error:  # noqa: BLE001 - every failure is reported on the case, never swallowed
        row.update({"pass": False, "error": f"{type(error).__name__}: {str(error)[:200]}"})
    return row


_EXECUTION = {"name": None}   # set by main() from the job's own CLOUD_RUN_EXECUTION


def emit(payload: dict) -> None:
    body = {"scope": JOB_NAME, "execution": _EXECUTION["name"], **payload}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    sys.stdout.write(PREFIX + base64.b64encode(text.encode()).decode() + "\n")
    sys.stdout.flush()


def parse_result_lines(text: str) -> tuple[list[dict], list[str]]:
    """Every result line decoded on its own; unreadable ones are kept, never fatal. The launcher uses THIS."""
    rows, bad = [], []
    for line in text.splitlines():
        if PREFIX not in line:
            continue
        payload = line.split(PREFIX, 1)[1].strip()
        try:
            rows.append(json.loads(base64.b64decode(payload, validate=True)))
        except ValueError:
            bad.append(payload[:200])
    return rows, bad


def run_all(cases: list[Case], transport: Transport, run_id: str, *, out: Callable[[dict], None] = emit) -> dict:
    rows = []
    for case in cases:
        row = run_case(case, transport, run_id)
        rows.append(row)
        out(row)
    left_open = sorted({ref for r in rows for ref in r.get("left_open") or []})
    summary = {"kind": "run", "ok": bool(rows) and all(r["pass"] for r in rows) and not left_open,
               "cases": len(rows), "passed": sum(1 for r in rows if r["pass"]), "calls": transport.calls,
               "left_open": left_open}
    out(summary)
    return summary


# ------------------------------------------------------------------ the job's own preconditions

def check_gate(raw: str | None, now: datetime) -> dict:
    """The launcher's gate observation, held to the same rules as the seed batch job: oneoff_gate.py's readings,
    CPU under 0.20, no database job running, at most 180 s old, outside the closed windows."""
    try:
        gate = json.loads(raw or "null")
    except ValueError:
        gate = None
    require(isinstance(gate, dict) and gate.get("verdict") == "GATE_OPEN", "launcher gate is not GATE_OPEN")
    cpu, running = gate.get("cpu_peak_5m"), gate.get("db_job_executions_running")
    require(gate.get("source") == GATE_SOURCE and type(cpu) is float and 0 <= cpu < 0.20
            and type(running) is int and running == 0, "launcher gate lacks oneoff_gate.py's readings")
    try:
        observed = datetime.fromisoformat(str(gate.get("observed_at")))
    except ValueError:
        observed = None
    require(observed is not None and observed.tzinfo is not None
            and 0 <= (now - observed).total_seconds() <= GATE_MAX_AGE_SECONDS, "launcher gate is stale")
    minute = now.astimezone(timezone.utc).hour * 60 + now.astimezone(timezone.utc).minute
    require(not any(start <= minute < end for start, end in CLOSED_WINDOWS_UTC), "inside a closed one-off window")
    return gate


def check_environment(env: dict, image_commit: str) -> str:
    require(not env.get("K_SERVICE") and env.get("CLOUD_RUN_JOB") == JOB_NAME, "runs only as the prod-test-transactions job")
    require(env.get(ENV + "ARMED") == "1", "the job is not armed")
    release = env.get(ENV + "RELEASE_COMMIT", "")
    require(re.fullmatch(r"[0-9a-f]{40}", release) and release == image_commit, "release commit is not the image's")
    run_id = env.get(ENV + "RUN_ID", "")
    require(re.fullmatch(r"[0-9a-f-]{36}", run_id), "run id is not a build id")
    return run_id


def read_key(path: Path) -> str:
    """The mounted key: a regular file, small, in the gateway's key format. Never printed."""
    info = path.stat()
    require(stat.S_ISREG(info.st_mode) and 0 < info.st_size <= 200, "the key mount is not a small regular file")
    key = path.read_text().strip()
    require(KEY_PATTERN.fullmatch(key), "the mounted key is not in the gateway's key format")
    return key


def main(env: dict | None = None, *, now: Callable[[], datetime] | None = None,
         transport_factory: Callable[[str], Transport] | None = None, key_path: Path | None = None,
         image_commit_path: Path = Path("/app/.image_commit_sha")) -> int:
    env = dict(os.environ if env is None else env)
    _EXECUTION["name"] = env.get("CLOUD_RUN_EXECUTION")
    now = now or (lambda: datetime.now(timezone.utc))
    try:
        check_case_list()
        run_id = check_environment(env, image_commit_path.read_text().strip())
        cases = select_cases(env.get(ENV + "CASES", ""))
        check_gate(env.get(ENV + "GATE"), now())
        key = read_key(key_path or Path(KEY_MOUNT_DIR) / KEY_FILE)
        transport = (transport_factory or Transport)(key)
        del key
    except (CaseError, OSError, ValueError) as error:
        emit({"kind": "run", "ok": False, "cases": 0, "passed": 0, "calls": 0, "left_open": [],
              "refused": f"{type(error).__name__}: {str(error)[:300]}"})
        return 3
    summary = run_all(cases, transport, run_id)
    return 0 if summary["ok"] else 1


check_case_list()

if __name__ == "__main__":
    sys.exit(main())
