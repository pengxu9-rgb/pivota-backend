"""Prod test transactions: a FIXED list of test calls against the prod gateway with a dedicated first-party test agent
key. The ONE sanctioned exception to the no-prod-probe rule: only this job, only this key, only on the owner's go per
run, launched by infra/gcp/cloudbuild.prod_test_transactions.yaml (scripts/ops/prod_test_transactions_launch.py).

    python -m scripts.ops.prod_test_transactions        # only as the armed `prod-test-transactions` Cloud Run job

STOP POINT: a checkout is created, read back and CANCELLED. Nothing is ever submitted for payment (payment submission
is ENABLED in prod). Before a byte leaves the job, the transport checks the WHOLE request body against the call's own
key allowlist (CALL_SCHEMAS) and a normalised substring denylist (DENY_FRAGMENTS); anything else is refused.

Inputs (set ONLY on the armed job by the launcher; the disarmed job has none of them, so a re-execution refuses):
  PROD_TEST_TXN_ARMED=1
  PROD_TEST_TXN_CASES            comma-separated case ids from CASES, or `all`
  PROD_TEST_TXN_RUN_ID           the Cloud Build id; idempotency keys, sessions and token ids derive from it
  PROD_TEST_TXN_GATE             {verdict, observed_at, ...} from a real scripts/ops/oneoff_gate.py run
  PROD_TEST_TXN_RELEASE_COMMIT   must equal /app/.image_commit_sha
  /var/run/pivota-test-agent/key       the test agent key (Secret Manager, by version). Sent only as X-Agent-API-Key.
  /var/run/pivota-test-targets/targets.json
                                 the PRIVATE reviewed targets (merchant, product and variant ids, the storefront host,
                                 the test buyer email, the test issuer). Its sha256 must equal TARGETS_SHA256 (filled in
                                 review); receipts carry only that sha, never an id. Ids never live in this repository.
  /var/run/pivota-test-agent-user/key.pem
                                 ONLY when a selected case needs an end user: the test issuer's EC P-256 private key.
                                 The job signs a 5-minute ES256 X-Agent-User-JWT per case (iss, aud, azp, scope, jti,
                                 sub = the test buyer, acp_session_id per run and case). No email claim. The owner
                                 enables the issuer binding for a run and disables it after (runbook).

Storefront checkouts (`esc_...`) cannot be cancelled: the gateway refuses by design and the seller cart expires unpaid.
The owner accepted that (2026-11-19, "JWT, accept expiry"): reported under `expires_on_seller` as a hash (the id
carries the seller's cart id), never cancel-called, not a failure. Every other checkout that may exist must read back
canceled, or it is LEFT OPEN and the run fails.

Output: TEXT lines `PROD_TEST_TXN_RESULT <base64 compact JSON>`: one `"kind": "case"` line per case and a final
`"kind": "run"` line. Only asserted fields, each a bounded scalar, ever leave the job; no response body is printed.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
from typing import Any, Callable
from urllib.parse import urlsplit
import uuid

JOB_NAME = "prod-test-transactions"
ENV = "PROD_TEST_TXN_"
PREFIX = "PROD_TEST_TXN_RESULT "
KEY_SECRET = "prod-test-agent-key"
KEY_MOUNT_DIR = "/var/run/pivota-test-agent"
KEY_FILE = "key"
KEY_PATTERN = re.compile(r"ak_(?:live_)?[0-9a-f]{64}")  # the gateway's own key format
TARGETS_SECRET = "prod-test-transactions-targets"
TARGETS_MOUNT_DIR = "/var/run/pivota-test-targets"
TARGETS_FILE = "targets.json"
# sha256 of the reviewed targets file, filled in REVIEW. Empty refuses every run.
TARGETS_SHA256 = ""
USER_KEY_SECRET = "prod-test-agent-user-key"
USER_KEY_MOUNT_DIR = "/var/run/pivota-test-agent-user"
USER_KEY_FILE = "key.pem"
USER_TOKEN_SECONDS = 300
USER_SUBJECT = "ptt-test-buyer"
GATE_SOURCE = "scripts/ops/oneoff_gate.py"
GATE_MAX_AGE_SECONDS = 300      # arm-deploy + execute + cold start must fit; the seed batch's 180 s refuses too often
CLOSED_WINDOWS_UTC = ((5 * 60 + 15, 6 * 60 + 15), (10 * 60 + 30, 11 * 60 + 20))
GATEWAY_BASE = "https://gateway.pivota.cc"        # never mcp.pivota.cc (the public-read policy host)
ALLOWED_HOSTS = frozenset({"gateway.pivota.cc"})
MAX_CALLS_PER_RUN = 40
CHECKOUT_RESERVE_CALLS = 3      # read back + cancel + read back, reserved before every create
CALL_TIMEOUT_SECONDS = 20.0     # a TOTAL per call: the MCP heartbeat would keep a per-read timeout open
MAX_BODY_BYTES = 2_000_000
MAX_SCALAR_CHARS = 120
# Gateway releases whose answers the read cases were reviewed against: path 4's market refusals need gw#2602
# (c2ce318d2) and be#2848. A new gateway sha is added here by a reviewed PR before the cases run on it.
REVIEWED_GATEWAY_SHAS = frozenset({"080d7297f74e019fe5d88a25e78c3315ebdb080a"})


class CaseError(ValueError):
    """A case list, flag or input problem: the run refuses before any call."""


class NotSent(CaseError):
    """The transport refused BEFORE sending: nothing left the job, so nothing can exist on the gateway."""


def require(test, message: str, error=CaseError) -> None:
    if not test:
        raise error(message)


# ------------------------------------------------------------------ reviewed targets (private input)

@dataclass(frozen=True)
class Targets:
    offers_product_id: str
    kernel: dict          # merchant_id, product_id, variant_id of a connected TEST merchant
    storefront: dict      # merchant_id (merch_obs_), product_id, variant_id, host of a LIVE US/USD storefront seller
    buyer_email: str      # a test buyer mailbox Pivota owns
    user_token: dict      # issuer, audience, kid, azp, scope of the test issuer bound to the test agent


_ID = re.compile(r"[A-Za-z0-9_:./-]{1,120}")
_HOST = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
_EMAIL = re.compile(r"[a-z0-9._+-]{1,64}@[a-z0-9.-]{1,190}\.[a-z]{2,63}")


def parse_targets(raw: bytes, expected_sha256: str) -> Targets:
    require(re.fullmatch(r"[0-9a-f]{64}", expected_sha256 or ""), "TARGETS_SHA256 has not been reviewed in")
    require(hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected_sha256),
            "the mounted targets are not the reviewed targets (sha256 differs)")
    data = json.loads(raw)
    require(isinstance(data, dict) and set(data) == {"offers_product_id", "kernel_test_merchant", "storefront_seller",
                                                     "buyer_email", "user_token"}, "targets: unexpected members")

    def ids(value, names):
        require(isinstance(value, dict) and set(value) == set(names), "targets: unexpected members")
        require(all(isinstance(value[n], str) and _ID.fullmatch(value[n]) for n in names if n != "host"),
                "targets: an id is not a plain id")
        return dict(value)

    kernel = ids(data["kernel_test_merchant"], ("merchant_id", "product_id", "variant_id"))
    storefront = ids(data["storefront_seller"], ("merchant_id", "product_id", "variant_id", "host"))
    require(storefront["merchant_id"].startswith("merch_obs_"), "targets: the storefront seller must be merch_obs_")
    require(isinstance(storefront["host"], str) and _HOST.fullmatch(storefront["host"]), "targets: bad seller host")
    token = data["user_token"]
    require(isinstance(token, dict) and set(token) == {"issuer", "audience", "kid", "azp", "scope"}
            and all(isinstance(v, str) and 0 < len(v) <= 200 for v in token.values()), "targets: bad user_token")
    require(urlsplit(token["issuer"]).scheme == "https", "targets: the issuer must be an https URL")
    require(isinstance(data["buyer_email"], str) and _EMAIL.fullmatch(data["buyer_email"]), "targets: bad buyer_email")
    require(isinstance(data["offers_product_id"], str) and _ID.fullmatch(data["offers_product_id"]),
            "targets: bad offers_product_id")
    return Targets(data["offers_product_id"], kernel, storefront, data["buyer_email"], dict(token))


# ------------------------------------------------------------------ the case list (fixed, reviewed)

@dataclass(frozen=True)
class Case:
    case_id: str
    door: str
    kind: str                      # "read" (one call) or "checkout" (create -> read back -> cancel)
    call: str
    build: Callable[[Targets], dict]   # the request, from the reviewed targets
    market: str                    # as sent, for the report
    expected: dict                 # dotted path -> exact value, or {"$gte": n}
    requires_flags: dict = field(default_factory=dict)   # gateway env name -> allowed plain value(s)
    needs_user: bool = False
    storefront: bool = False       # a storefront create: the continue_url host must be the reviewed seller host
    shadow_lookup: bool = False    # a US storefront create: the shadow catalog lookup runs (advisory check)
    enabled: bool = True
    reviewed_gateway: bool = False  # the expected answers hold only on REVIEWED_GATEWAY_SHAS


OFF = (None, "", "0")  # an on/off flag that must be OFF


def _offers(market_member: dict | None, outer: dict | None = None) -> Callable[[Targets], dict]:
    def build(t: Targets) -> dict:
        offers = {"product": {"product_id": t.offers_product_id}, "limit": 3}
        if market_member is not None:
            offers.update(market_member)
        return {"operation": "offers.resolve", "payload": {"offers": offers, **(outer or {})}, "metadata": {}}
    return build


def _mcp_quote(kind: str, market: str | None) -> Callable[[Targets], dict]:
    def build(t: Targets) -> dict:
        target = t.storefront if kind == "storefront" else t.kernel
        arguments = {"quote": {"merchant_id": target["merchant_id"], "customer_email": t.buyer_email, "items": [
            {"product_id": target["product_id"], "variant_id": target["variant_id"], "quantity": 1}]}}
        if market is not None:
            arguments["market"] = market
        return arguments
    return build


def _ucp_checkout(country: str | None) -> Callable[[Targets], dict]:
    def build(t: Targets) -> dict:
        item = f"{t.storefront['product_id']}::v::{t.storefront['variant_id']}"
        checkout = {"line_items": [{"item": {"id": item}, "quantity": 1}]}
        if country is not None:
            checkout["context"] = {"address_country": country}
        return {"meta": {}, "checkout": checkout}
    return build


def _refused(code: str, reason: str) -> dict:
    return {"is_error": True, "data.error.code": code, "data.error.detail.reason": reason}


SERVED = {"status": "success", "error.code": None, "offers_count": {"$gte": 1}}
REFUSED_INVALID = {"status": "error", "error.code": "MARKET_INVALID", "error.reason": "market_invalid"}
REFUSED_NOT_PRICED = {"status": "error", "error.code": "MARKET_NOT_PRICED", "error.reason": "market_not_priced"}
STOREFRONT_FLAGS_MCP = {"AGENT_CHECKOUT_STRICT": "1", "AGENT_CHECKOUT_MCP_STOREFRONT_ESCALATION_ENABLED": "1",
                        "AGENT_CHECKOUT_UCP_CURRENCY_FOLLOWS_MARKET": "1",
                        "AGENT_CHECKOUT_UCP_MERCHANT_PRICING_ENABLED": "1", "SHOPIFY_CATALOG_ATTRIBUTION": "shadow",
                        "AGENT_CHECKOUT_UCP_CART_REFERRAL_ENABLED": OFF}
STOREFRONT_FLAGS_UCP = {"AGENT_CHECKOUT_STRICT": "1", "AGENT_CHECKOUT_UCP_TOOL_DOOR_ENABLED": "1",
                        "AGENT_CHECKOUT_UCP_ESCALATION_ENABLED": "1", "AGENT_CHECKOUT_UCP_CURRENCY_FOLLOWS_MARKET": "1",
                        "AGENT_CHECKOUT_UCP_MERCHANT_PRICING_ENABLED": "1", "SHOPIFY_CATALOG_ATTRIBUTION": "shadow",
                        "AGENT_CHECKOUT_UCP_CART_REFERRAL_ENABLED": OFF}
INVOKE = "/agent/shop/v1/invoke"

CASES: tuple[Case, ...] = (
    # Path 4: offers.resolve market refusals (gw#2601 / gw#2602; be#2843 / be#2848). Read only.
    *[Case(cid, INVOKE, "read", "offers.resolve", build, label, expected, reviewed_gateway=True)
      for cid, build, label, expected in (
          ("OR-US", _offers({"market": "US"}), "US", SERVED),
          ("OR-SG", _offers({"market": "SG"}), "SG", SERVED),
          ("OR-us-lower", _offers({"market": "us"}), "us", SERVED),
          ("OR-none", _offers(None), "none", SERVED),
          ("OR-USA", _offers({"market": "USA"}), "USA", REFUSED_INVALID),
          ("OR-long-s", _offers({"market": "ſg"}), "ſg", REFUSED_INVALID),
          ("OR-DE", _offers({"market": "DE"}), "DE", REFUSED_NOT_PRICED),
          ("OR-USA-beside-SG", _offers({"market": "USA"}, outer={"market": "SG"}), "USA+SG", SERVED),
      )],
    # Path 2: /mcp create_checkout_session with `market` (step 6); a kernel checkout on a connected test merchant,
    # cancelled. Path 3: the US storefront creates run the shadow catalog lookup.
    *[Case(cid, "/mcp", "checkout", "create_checkout_session", _mcp_quote(kind, market), label, expected,
           requires_flags=flags, needs_user=True, storefront=kind == "storefront" and created, shadow_lookup=shadow)
      for cid, kind, market, label, expected, flags, created, shadow in (
          ("MCP-SF-US", "storefront", "US", "US", {"is_error": False, "data.status": "completes_on_seller_storefront",
                                                   "data.currency": "USD", "data.amount_unit": "minor"},
           STOREFRONT_FLAGS_MCP, True, True),
          ("MCP-SF-SG", "storefront", "SG", "SG", _refused("NO_MERCHANT_OFFER", "seller_not_priced_in_market_currency"),
           STOREFRONT_FLAGS_MCP, False, False),
          ("MCP-SF-DE", "storefront", "DE", "DE", _refused("NO_MERCHANT_OFFER", "market_not_priced"),
           STOREFRONT_FLAGS_MCP, False, False),
          ("MCP-SF-none", "storefront", None, "none", _refused("QUOTE_REQUIRED", "market_required_for_seller_storefront"),
           STOREFRONT_FLAGS_MCP, False, False),
          ("MCP-SF-USA", "storefront", "USA", "USA", {"is_error": True, "data.error.code": "INVALID_ARGUMENTS"},
           STOREFRONT_FLAGS_MCP, False, False),
          ("MCP-K-US", "kernel", "US", "US", {"is_error": False, "data.status": "ready_for_payment",
                                             "data.currency": "USD"}, {"AGENT_CHECKOUT_STRICT": "1"}, False, False),
      )],
    # Path 1: /ucp/mcp create_checkout, the currency rule (step 1) on the storefront seller.
    *[Case(cid, "/ucp/mcp", "checkout", "create_checkout", _ucp_checkout(country), label, expected,
           requires_flags=STOREFRONT_FLAGS_UCP, needs_user=True, storefront=created, shadow_lookup=shadow)
      for cid, country, label, expected, created, shadow in (
          ("UCP-SF-US", "US", "US", {"is_error": False, "data.status": "requires_escalation", "data.currency": "USD"},
           True, True),
          ("UCP-SF-SG", "SG", "SG", _refused("NO_MERCHANT_OFFER", "seller_not_priced_in_market_currency"), False, False),
          ("UCP-SF-DE", "DE", "DE", _refused("NO_MERCHANT_OFFER", "market_not_priced"), False, False),
          ("UCP-SF-USA", "USA", "USA", _refused("QUOTE_REQUIRED", "ucp_market_invalid"), False, False),
          ("UCP-SF-none", None, "none", {"is_error": False, "data.status": "requires_escalation",
                                         "data.currency": "USD"}, True, False),
      )],
)

GATEWAY_FLAG_NAMES = tuple(sorted({name for case in CASES for name in case.requires_flags}))
CHECK_TARGETS = Targets("prod_check_1", {"merchant_id": "merch_check", "product_id": "p", "variant_id": "v"},
                        {"merchant_id": "merch_obs_check", "product_id": "p", "variant_id": "v", "host": "shop.example"},
                        "buyer@check.example", {"issuer": "https://issuer.check.example", "audience": "a", "kid": "k",
                                                "azp": "z", "scope": "s"})


def check_case_list(cases: tuple[Case, ...] = CASES) -> None:
    """Static rules every listed case must satisfy, enforced at import time and in the tests."""
    ids = [c.case_id for c in cases]
    require(len(ids) == len(set(ids)), "duplicate case ids")
    for case in cases:
        require(re.fullmatch(r"[A-Za-z0-9-]{2,40}", case.case_id), f"bad case id {case.case_id!r}")
        require(case.door in CALL_SCHEMAS and case.call in CALL_SCHEMAS[case.door],
                f"{case.case_id}: {case.call} on {case.door} is not an allowed call")
        require(case.kind in ("read", "checkout") and case.expected, f"{case.case_id}: kind / expected")
        check_request(case.door, envelope(case.door, case.call, case.build(CHECK_TARGETS)))


def select_cases(raw: str) -> list[Case]:
    """The cases a run executes, from the launcher's comma list or `all`. Unknown, disabled or repeated -> refuse."""
    text = (raw or "").strip()
    require(text, "no cases selected")
    by_id = {c.case_id: c for c in CASES}
    names = [c.case_id for c in CASES if c.enabled] if text == "all" else [n.strip() for n in text.split(",")]
    require(names and len(names) == len(set(names)), "case list is empty or repeats a case")
    chosen = []
    for name in names:
        require(name in by_id, f"unknown case {name!r}")
        require(by_id[name].enabled, f"case {name} is listed for review only")
        chosen.append(by_id[name])
    return chosen


def flag_ok(value, allowed) -> bool:
    return value in allowed if isinstance(allowed, tuple) else value == allowed


def require_flags(cases: list[Case], flags: dict) -> None:
    for case in cases:
        for name, allowed in case.requires_flags.items():
            require(flag_ok(flags.get(name), allowed),
                    f"case {case.case_id} needs {name}={allowed}; gateway has {flags.get(name)!r}")


def require_reviewed_gateway(cases: list[Case], gateway_sha: str | None) -> None:
    for case in cases:
        require(not case.reviewed_gateway or gateway_sha in REVIEWED_GATEWAY_SHAS,
                f"case {case.case_id} was reviewed against {sorted(REVIEWED_GATEWAY_SHAS)}; gateway is {gateway_sha!r}")


# ------------------------------------------------------------------ request guards: schema allowlist + denylist

S = "scalar"
_ITEM = {"product_id": S, "variant_id": S, "quantity": S}
CALL_SCHEMAS = {
    INVOKE: {"offers.resolve": {"operation": S, "metadata": {}, "payload": {
        "market": S, "offers": {"product": {"product_id": S}, "market": S, "limit": S}}}},
    "/mcp": {
        "create_checkout_session": {"idempotency_key": S, "market": S,
                                    "quote": {"merchant_id": S, "customer_email": S, "items": [_ITEM]}},
        "get_checkout_session": {"session_id": S},
        "cancel_checkout_session": {"idempotency_key": S, "session_id": S},
    },
    "/ucp/mcp": {
        "create_checkout": {"meta": {"idempotency-key": S}, "checkout": {
            "line_items": [{"item": {"id": S}, "quantity": S}], "context": {"address_country": S}}},
        "get_checkout": {"meta": {}, "id": S},
    },
}
MCP_ENVELOPE = {"jsonrpc": S, "id": S, "method": S}
# Normalised (lower-case, `_`/`-` removed) fragments no member name may contain, anywhere, except the exact paths in
# DENY_EXCEPTIONS. Payment, purchase lanes, credentials, buyer PII.
DENY_FRAGMENTS = ("pay", "card", "token", "credential", "instrument", "mandate", "authoriz", "reap", "cvv", "cvc",
                  "wallet", "address", "email", "secret", "password", "iban", "account", "order", "confirm",
                  "submit", "complete", "shipping", "billing", "phone")
DENY_EXCEPTIONS = frozenset({"payload", "params.arguments.quote.customer_email",
                             "params.arguments.checkout.context.address_country"})


def _norm(name: str) -> str:
    return re.sub(r"[_\-\s]", "", str(name).lower())


def _walk(value: Any, schema: Any, path: str) -> None:
    if schema == S:
        require(value is None or isinstance(value, (str, int, float, bool)), f"{path or 'body'} must be a scalar",
                NotSent)
        if isinstance(value, str):
            require(len(value) <= 300, f"{path} is too long", NotSent)
        return
    if isinstance(schema, list):
        require(isinstance(value, list) and len(value) <= 5, f"{path} must be a short list", NotSent)
        for index, inner in enumerate(value):
            _walk(inner, schema[0], f"{path}.{index}")
        return
    require(isinstance(value, dict), f"{path or 'body'} must be an object", NotSent)
    for key, inner in value.items():
        member = f"{path}.{key}" if path else str(key)
        require(isinstance(key, str) and key in schema, f"request member {member} is not allowed for this call", NotSent)
        if member not in DENY_EXCEPTIONS:
            hit = next((f for f in DENY_FRAGMENTS if f in _norm(key)), None)
            require(hit is None, f"forbidden request member {member}", NotSent)
        _walk(inner, schema[key], member)


def envelope(door: str, call: str, request: dict) -> dict:
    return request if door == INVOKE else mcp_body(call, request)


def check_request(door: str, body: Any) -> str:
    """The call's name, once the WHOLE body passed its schema and the denylist. Raises NotSent otherwise."""
    require(door in CALL_SCHEMAS, f"door {door} is not allowed", NotSent)
    require(isinstance(body, dict), "the body must be an object", NotSent)
    if door == INVOKE:
        name = body.get("operation")
        require(isinstance(name, str) and name in CALL_SCHEMAS[door], f"{name} on {door} is not an allowed call", NotSent)
        _walk(body, CALL_SCHEMAS[door][name], "")
        require(body.get("metadata") in (None, {}), "invoke metadata must be empty", NotSent)
        return name
    require(body.get("method") == "tools/call", "only tools/call is allowed on an MCP door", NotSent)
    params = body.get("params")
    name = params.get("name") if isinstance(params, dict) else None
    require(isinstance(name, str) and name in CALL_SCHEMAS[door], f"{name} on {door} is not an allowed call", NotSent)
    _walk({k: v for k, v in body.items() if k != "params"}, MCP_ENVELOPE, "")
    require(set(params) <= {"name", "arguments"}, "params may carry only name and arguments", NotSent)
    require(isinstance(params.get("arguments", {}), dict), "arguments must be an object", NotSent)
    _walk(params.get("arguments", {}), CALL_SCHEMAS[door][name], "params.arguments")
    return name


def mcp_body(tool: str, arguments: dict, request_id: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}


# ------------------------------------------------------------------ the transport

class Transport:
    """The ONLY way out of the job: allow-listed host, door, call and body; a hard call cap; a total deadline per
    call; no retries, no redirects, no proxy environment."""

    def __init__(self, key: str, *, post: Callable[[str, dict, dict, float], tuple[int, bytes]] | None = None,
                 base: str = GATEWAY_BASE, max_calls: int = MAX_CALLS_PER_RUN):
        require(KEY_PATTERN.fullmatch(key or ""), "the test agent key is not in the gateway's key format")
        parts = urlsplit(base)
        require(parts.scheme == "https" and parts.hostname in ALLOWED_HOSTS and parts.port is None
                and base == f"https://{parts.hostname}", "base host not allowed")
        self._key, self._post, self._base, self._max = key, post or _httpx_post, base, max_calls
        self.calls = 0

    def remaining(self) -> int:
        return self._max - self.calls

    def send(self, door: str, body: dict, *, user_jwt: str | None = None) -> tuple[int, Any]:
        check_request(door, body)
        require(self.calls < self._max, "the per-run call cap is reached", NotSent)
        if user_jwt is not None:
            require(door != INVOKE, "the user token is sent only on the checkout doors", NotSent)
        url = self._base + door
        require(urlsplit(url).hostname in ALLOWED_HOSTS, "host not allowed", NotSent)
        headers = {"X-Agent-API-Key": self._key, "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if user_jwt is not None:
            headers["X-Agent-User-JWT"] = user_jwt
        self.calls += 1
        status, raw = self._post(url, headers, body, CALL_TIMEOUT_SECONDS)
        require(len(raw) <= MAX_BODY_BYTES, "answer too large")
        return status, parse_answer(door, raw)


def _httpx_post(url: str, headers: dict, body: dict, timeout: float, *, transport=None) -> tuple[int, bytes]:
    """One attempt with a TOTAL deadline: the body is streamed and abandoned once `timeout` seconds have passed or
    MAX_BODY_BYTES arrived. No redirects (a redirect could leave the allowed host), no retries, no proxy env."""
    import httpx

    deadline = time.monotonic() + timeout
    with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False,
                      transport=transport or httpx.HTTPTransport(retries=0)) as client:
        with client.stream("POST", url, headers=headers, json=body) as response:
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                require(size <= MAX_BODY_BYTES, "answer too large")
                require(time.monotonic() <= deadline, "the call passed its total deadline")
                chunks.append(chunk)
            return response.status_code, b"".join(chunks)


def parse_answer(door: str, raw: bytes) -> Any:
    """invoke -> its JSON body; MCP -> {is_error, data} from result.content[0].text (else structuredContent)."""
    text = raw.decode("utf-8", "replace").strip()  # the gateway may write heartbeat newlines before the body
    try:
        body = json.loads(text) if text else None
    except ValueError:
        return {"unparsed": True}
    if door == INVOKE or not isinstance(body, dict):
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


def scalar(value: Any) -> Any:
    """A bounded scalar for a report: objects by type only, strings cut."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:MAX_SCALAR_CHARS]
    return f"<{type(value).__name__}>"


def scrub(answer: Any, paths) -> dict:
    return {path: scalar(dig(answer, path)) for path in paths}


def matches(expected: dict, got: dict) -> bool:
    for path, want in expected.items():
        have = got.get(path)
        if isinstance(want, dict) and set(want) == {"$gte"}:
            if not (type(have) is int and have >= want["$gte"]):
                return False
        elif have != want or type(have) is not type(want):
            return False
    return True


STOREFRONT_PREFIX = "esc_"


def report_id(session_id: str) -> str:
    """A storefront id carries the seller's cart id: report a hash. Kernel ids are reported as is."""
    if session_id.startswith(STOREFRONT_PREFIX):
        return "esc_#" + hashlib.sha256(session_id.encode()).hexdigest()[:16]
    return scalar(session_id)


# ------------------------------------------------------------------ running the cases

def user_session_id(run_id: str, case_id: str) -> str:
    return f"ptt-{run_id}-{case_id}"


def make_user_signer(private_pem: bytes, token: dict, *, now: Callable[[], datetime]) -> Callable[[str, str], str]:
    """(run_id, case_id) -> a 5-minute ES256 X-Agent-User-JWT for the test issuer. The gateway derives the kernel
    user_ref from (iss, sub) and the session from acp_session_id: one buyer and one session per case. No email claim:
    the buyer email rides only in the /mcp quote, so the token never attests buyer data."""
    import jwt
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    key = load_pem_private_key(private_pem, password=None)
    require(isinstance(key, ec.EllipticCurvePrivateKey) and key.curve.name == "secp256r1",
            "the test issuer key must be an EC P-256 private key")

    def sign(run_id: str, case_id: str) -> str:
        issued = int(now().timestamp())
        claims = {"iss": token["issuer"], "aud": token["audience"], "azp": token["azp"], "scope": token["scope"],
                  "sub": USER_SUBJECT, "iat": issued, "nbf": issued, "exp": issued + USER_TOKEN_SECONDS,
                  "jti": str(uuid.uuid4()), "acp_session_id": user_session_id(run_id, case_id)}
        return jwt.encode(claims, key, algorithm="ES256", headers={"kid": token["kid"]})

    return sign


def idempotency_key(run_id: str, case_id: str, step: str) -> str:
    return f"ptt-{run_id[:8]}-{case_id}-{step}"


def run_read(case: Case, request: dict, transport: Transport) -> dict:
    status, answer = transport.send(case.door, request)
    got = scrub(answer, case.expected)
    got["http_status"] = status
    return {"pass": status == 200 and matches(case.expected, got), "got": got}


CHECKOUT_DOORS = {
    "/mcp": {"get": "get_checkout_session", "id": "data.session_id", "status": "data.status",
             "get_args": lambda sid: {"session_id": sid}},
    "/ucp/mcp": {"get": "get_checkout", "id": "data.id", "status": "data.status",
                 "get_args": lambda sid: {"meta": {}, "id": sid}},
}


def continue_host(answer: Any) -> str | None:
    url = dig(answer, "data.continue_url")
    return urlsplit(url).hostname if isinstance(url, str) else None


def run_checkout(case: Case, request: dict, transport: Transport, run_id: str, row: dict, targets: Targets,
                 user_jwt: str | None) -> None:
    """create -> read back -> ALWAYS cancel (kernel) -> read back canceled. A create whose answer is not a parsed
    refusal and names no session may still have made one: `unknown:<case>`, and the run is not ok."""
    door = CHECKOUT_DOORS[case.door]
    require(transport.remaining() >= 1 + CHECKOUT_RESERVE_CALLS, "not enough calls left to cancel a checkout", NotSent)
    arguments = json.loads(json.dumps(request))
    if case.door == "/mcp":
        arguments["idempotency_key"] = idempotency_key(run_id, case.case_id, "create")
    else:
        arguments.setdefault("meta", {})["idempotency-key"] = idempotency_key(run_id, case.case_id, "create")
    try:
        status, answer = transport.send(case.door, mcp_body(case.call, arguments), user_jwt=user_jwt)
    except NotSent:
        raise                                        # nothing left the job
    except Exception:
        row["left_open"] = [f"unknown:{case.case_id}"]
        raise
    got = scrub(answer, case.expected)
    got["http_status"] = status
    row["got"] = got
    session_id = dig(answer, door["id"])
    if not (status == 200 and isinstance(session_id, str) and session_id):
        refused = status == 200 and isinstance(answer, dict) and answer.get("is_error") is True
        if not refused:
            row["left_open"] = [f"unknown:{case.case_id}"]   # a 5xx, an unreadable 200 or an id we cannot see
        row["pass"] = refused and matches(case.expected, got)
        return
    sid = report_id(session_id)
    row["created"], row["left_open"] = [sid], [sid]
    host_ok = True
    if case.storefront:
        got["continue_url.host_is_reviewed"] = continue_host(answer) == targets.storefront["host"]
        host_ok = got["continue_url.host_is_reviewed"]
    read_ok = False
    try:
        rstatus, read = transport.send(case.door, mcp_body(door["get"], door["get_args"](session_id), 2),
                                       user_jwt=user_jwt)
        got["read_back.status"] = scalar(dig(read, door["status"]))
        read_ok = rstatus == 200 and got["read_back.status"] == scalar(dig(answer, door["status"]))
    finally:
        cancel_checkout(case, transport, run_id, session_id, row, user_jwt)
    row["pass"] = status == 200 and matches(case.expected, got) and read_ok and host_ok and not row["left_open"]


def cancel_checkout(case: Case, transport: Transport, run_id: str, session_id: str, row: dict,
                    user_jwt: str | None) -> None:
    """Cancel through /mcp cancel_checkout_session (the kernel's one cancel), then confirm it reads back canceled.
    A storefront checkout is never cancel-called: the owner accepted that its seller cart expires (2026-11-19)."""
    sid = report_id(session_id)
    if session_id.startswith(STOREFRONT_PREFIX):
        row["cancel_note"] = "storefront checkout: the gateway refuses to cancel it; the seller cart expires unpaid"
        row["expires_on_seller"], row["left_open"] = [sid], []
        return
    try:
        cstatus, cancel = transport.send("/mcp", mcp_body("cancel_checkout_session", {
            "idempotency_key": idempotency_key(run_id, case.case_id, "cancel"), "session_id": session_id}, 3),
            user_jwt=user_jwt)
        canceled = cstatus == 200 and dig(cancel, "data.status") == "canceled"
        rstatus, read = transport.send("/mcp", mcp_body("get_checkout_session", {"session_id": session_id}, 4),
                                       user_jwt=user_jwt)
        if canceled and rstatus == 200 and dig(read, "data.status") == "canceled":
            row["cancelled"], row["left_open"] = [sid], []
        else:
            row["cancel_note"] = (f"cancel answered {scalar(dig(cancel, 'data.status'))!r} ({cstatus}), "
                                  f"read back {scalar(dig(read, 'data.status'))!r} ({rstatus})")[:200]
    except Exception as error:  # noqa: BLE001 - reported; the session stays in left_open
        row["cancel_note"] = f"cancel failed: {type(error).__name__}"


def run_case(case: Case, transport: Transport, run_id: str, targets: Targets,
             signer: Callable[[str, str], str] | None = None) -> dict:
    row = {"kind": "case", "case": case.case_id, "door": case.door, "call": case.call, "market": case.market,
           "expected": case.expected, "error": None, "created": [], "cancelled": [], "left_open": [],
           "expires_on_seller": [], "got": None, "pass": False}
    try:
        request = case.build(targets)
        if case.kind == "checkout":
            user_jwt = None
            if case.needs_user:
                require(signer is not None, f"{case.case_id} needs the test issuer key, which is not mounted", NotSent)
                user_jwt = signer(run_id, case.case_id)
            run_checkout(case, request, transport, run_id, row, targets, user_jwt)
        else:
            row.update(run_read(case, request, transport))
    except Exception as error:  # noqa: BLE001 - every failure is reported on the case, never swallowed
        row["pass"] = False
        # Only this module's own messages are kept: an exception from elsewhere could carry request or key text.
        row["error"] = type(error).__name__ + (f": {str(error)[:160]}" if isinstance(error, CaseError) else "")
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


def run_all(cases: list[Case], transport: Transport, run_id: str, targets: Targets, *,
            out: Callable[[dict], None] = emit, signer: Callable[[str, str], str] | None = None) -> dict:
    rows = []
    for case in cases:
        row = run_case(case, transport, run_id, targets, signer)
        rows.append(row)
        out(row)
    left_open = sorted({ref for r in rows for ref in r.get("left_open") or []})
    summary = {"kind": "run", "ok": bool(rows) and all(r["pass"] for r in rows) and not left_open,
               "cases": len(rows), "passed": sum(1 for r in rows if r["pass"]), "calls": transport.calls,
               "left_open": left_open, "targets_sha256": TARGETS_SHA256,
               "seller_host_sha256": hashlib.sha256(targets.storefront["host"].encode()).hexdigest(),
               "expires_on_seller": sorted({ref for r in rows for ref in r.get("expires_on_seller") or []})}
    out(summary)
    return summary


# ------------------------------------------------------------------ the job's own preconditions

def check_gate(raw: str | None, now: datetime) -> dict:
    """The launcher's gate observation: oneoff_gate.py's readings, CPU under 0.20, no database job running, at most
    GATE_MAX_AGE_SECONDS old, outside the closed windows."""
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
    utc = now.astimezone(timezone.utc)
    minute = utc.hour * 60 + utc.minute
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


def _small_file(path: Path, limit: int, what: str) -> bytes:
    info = path.stat()
    require(stat.S_ISREG(info.st_mode) and 0 < info.st_size <= limit, f"the {what} mount is not a small regular file")
    return path.read_bytes()


def read_key(path: Path) -> str:
    key = _small_file(path, 200, "key").decode("ascii", "replace").strip()
    require(KEY_PATTERN.fullmatch(key), "the mounted key is not in the gateway's key format")
    return key


def read_user_key(path: Path) -> bytes:
    pem = _small_file(path, 4000, "user key")
    require(pem.lstrip().startswith(b"-----BEGIN ") and b"PRIVATE KEY-----" in pem, "the user key mount is not a PEM key")
    return pem


def main(env: dict | None = None, *, now: Callable[[], datetime] | None = None,
         transport_factory: Callable[[str], Transport] | None = None, key_path: Path | None = None,
         user_key_path: Path | None = None, targets_path: Path | None = None,
         image_commit_path: Path = Path("/app/.image_commit_sha")) -> int:
    env = dict(os.environ if env is None else env)
    _EXECUTION["name"] = env.get("CLOUD_RUN_EXECUTION")
    now = now or (lambda: datetime.now(timezone.utc))
    try:
        check_case_list()
        run_id = check_environment(env, image_commit_path.read_text().strip())
        cases = select_cases(env.get(ENV + "CASES", ""))
        check_gate(env.get(ENV + "GATE"), now())
        targets = parse_targets(_small_file(targets_path or Path(TARGETS_MOUNT_DIR) / TARGETS_FILE, 20_000, "targets"),
                                TARGETS_SHA256)
        key = read_key(key_path or Path(KEY_MOUNT_DIR) / KEY_FILE)
        transport = (transport_factory or Transport)(key)
        del key
        signer = None
        if any(case.needs_user for case in cases):
            signer = make_user_signer(read_user_key(user_key_path or Path(USER_KEY_MOUNT_DIR) / USER_KEY_FILE),
                                      targets.user_token, now=now)
    except Exception as error:  # noqa: BLE001 - any setup failure refuses with ONE run line and no call
        detail = f": {str(error)[:200]}" if isinstance(error, CaseError) else ""
        emit({"kind": "run", "ok": False, "cases": 0, "passed": 0, "calls": 0, "left_open": [], "expires_on_seller": [],
              "refused": type(error).__name__ + detail})
        return 3
    summary = run_all(cases, transport, run_id, targets, signer=signer)
    return 0 if summary["ok"] else 1


check_case_list()

if __name__ == "__main__":
    sys.exit(main())
