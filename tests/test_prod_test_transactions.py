"""Prod test-transaction job: the case list is fixed and safe, the transport sends only allow-listed bodies, every
expected/got comparison is exact and non-vacuous, and any checkout that may exist is cancelled or reported LEFT OPEN.
Offline: every gateway answer is a fake shaped like the gateway's own, and the /mcp fake enforces buyer intake the way
the gateway does (commerceToolSurface applyBuyerIntake -> buyerIntake.resolveBuyerEmail)."""

from __future__ import annotations

import hashlib
import json

from datetime import datetime, timedelta, timezone
import pytest

from scripts.ops import prod_test_transactions as job

KEY = "ak_" + "1" * 64
RUN_ID = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
RELEASE = "a" * 40
NOW = datetime(2026, 1, 2, 7, 0, tzinfo=timezone.utc)
GW_DIGEST = "sha256:908df3e4b47324610fd535f0969c923f03aa63e145875a93c94e385885c5846d"   # 7d650dce8
TARGETS_DOC = {
    "offers_product_id": "prod_test_reviewed_0001",
    "kernel_test_merchant": {"merchant_id": "merch_test_reviewed", "product_id": "prod_k_1", "variant_id": "var_k_1"},
    "storefront_seller": {"merchant_id": "merch_obs_reviewed_seller", "product_id": "prod_sf_1", "variant_id": "var_sf_1",
                          "host": "shop.reviewed.example"},
    "buyer_email": "ptt-buyer@owned.example",
    "user_token": {"issuer": "https://test-issuer.example/ptt", "audience": "pivota-gateway-test", "kid": "ptt-key-1",
                   "azp": "ptt-job", "scope": "ptt:checkout"},
}
TARGETS_RAW = json.dumps(TARGETS_DOC, sort_keys=True).encode()
TARGETS_SHA = hashlib.sha256(TARGETS_RAW).hexdigest()
TARGETS = job.parse_targets(TARGETS_RAW, TARGETS_SHA)


def gate(observed=NOW, cpu=0.05, running=0, **extra):
    return json.dumps({"verdict": "GATE_OPEN", "observed_at": observed.isoformat(), "summary": "s",
                       "source": "scripts/ops/oneoff_gate.py", "cpu_peak_5m": cpu,
                       "db_job_executions_running": running, **extra})


def mcp_answer(data, is_error=False):
    body = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": json.dumps(data)}],
                                                   **({"isError": True} if is_error else {})}}
    return json.dumps(body).encode()


INTAKE_REFUSAL = mcp_answer({"error": {"code": "QUOTE_REQUIRED", "detail": {"reason": "acp_buyer_email_required"}}},
                            True)


class FakeGateway:
    """Records every call (url, headers, body) and answers from a script of (status, bytes) or exceptions. Like the
    gateway, an /mcp create_checkout_session without quote.customer_email (and no email claim) is refused at intake
    BEFORE any market or storefront logic."""

    def __init__(self, answers, *, intake=True):
        self.answers = list(answers)
        self.calls = []
        self.intake = intake

    def __call__(self, url, headers, body, timeout):
        self.calls.append((url, dict(headers), json.loads(json.dumps(body)), timeout))
        params = body.get("params") or {}
        if (self.intake and url.endswith("/mcp") and not url.endswith("/ucp/mcp")
                and params.get("name") == "create_checkout_session"
                and not ((params.get("arguments") or {}).get("quote") or {}).get("customer_email")):
            return 200, INTAKE_REFUSAL
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def case(case_id):
    found, = job.select_cases(case_id)
    return found


def transport(fake, **kw):
    return job.Transport(KEY, post=fake, **kw)


# ------------------------------------------------------------------ the private targets

def test_targets_are_accepted_only_with_the_reviewed_sha_and_shape():
    assert TARGETS.storefront["host"] == "shop.reviewed.example" and TARGETS.buyer_email == "ptt-buyer@owned.example"
    with pytest.raises(job.CaseError, match="not been reviewed"):
        job.parse_targets(TARGETS_RAW, "")
    with pytest.raises(job.CaseError, match="sha256 differs"):
        job.parse_targets(TARGETS_RAW + b" ", TARGETS_SHA)
    for mutate, message in (
        (lambda d: d["storefront_seller"].update(merchant_id="merch_connected"), "merch_obs_"),
        (lambda d: d["storefront_seller"].update(host="Shop.Example/"), "seller host"),
        (lambda d: d.update(buyer_email="not an email"), "buyer_email"),
        (lambda d: d["user_token"].update(issuer="http://x.example"), "https"),
        (lambda d: d["user_token"].pop("scope"), "user_token"),
        (lambda d: d.update(extra=1), "unexpected"),
        (lambda d: d["kernel_test_merchant"].update(product_id="a b"), "plain id"),
    ):
        doc = json.loads(TARGETS_RAW)
        mutate(doc)
        raw = json.dumps(doc).encode()
        with pytest.raises(job.CaseError, match=message):
            job.parse_targets(raw, hashlib.sha256(raw).hexdigest())


def test_the_repository_holds_no_target_ids():
    source = open(job.__file__).read()
    assert job.TARGETS_SHA256 == "" or len(job.TARGETS_SHA256) == 64
    for value in ("merch_obs_reviewed", "merch_test_reviewed", "owned.example"):
        assert value not in source


# ------------------------------------------------------------------ the case list

def test_case_list_is_valid_and_every_case_states_its_expected_outcome():
    job.check_case_list()
    assert len(job.CASES) == 19
    for c in job.CASES:
        assert c.call in job.CALL_SCHEMAS[c.door] and c.expected
        job.check_request(c.door, job.envelope(c.door, c.call, c.build(TARGETS)))


def test_no_allowed_call_pays_or_completes():
    paying = {"complete_checkout_session", "complete_checkout", "create_payment_link", "submit_payment",
              "confirm_payment", "create_order", "start_purchase", "update_checkout_session", "update_checkout",
              "resume_checkout", "preview_merchant_checkout_quote", "preview_quote", "checkout_handoff",
              "request_after_sales", "get_offers"}
    for door, calls in job.CALL_SCHEMAS.items():
        assert not set(calls) & paying, door


def test_selection_refuses_unknown_repeated_and_disabled_cases(monkeypatch):
    assert [c.case_id for c in job.select_cases("OR-US,OR-DE")] == ["OR-US", "OR-DE"]
    assert len(job.select_cases("all")) == 19
    for raw, message in (("", "no cases"), ("OR-US,OR-US", "repeats"), ("OR-NOPE", "unknown case")):
        with pytest.raises(job.CaseError, match=message):
            job.select_cases(raw)
    monkeypatch.setattr(job, "CASES", tuple(job.Case(**{**c.__dict__, "enabled": c.case_id != "OR-DE"})
                                            for c in job.CASES))
    with pytest.raises(job.CaseError, match="review only"):
        job.select_cases("OR-DE")


LIVE_FLAGS = {"AGENT_CHECKOUT_STRICT": "1", "AGENT_CHECKOUT_MCP_STOREFRONT_ESCALATION_ENABLED": "1",
              "AGENT_CHECKOUT_UCP_CURRENCY_FOLLOWS_MARKET": "1", "AGENT_CHECKOUT_UCP_MERCHANT_PRICING_ENABLED": "1",
              "AGENT_CHECKOUT_UCP_TOOL_DOOR_ENABLED": "1", "AGENT_CHECKOUT_UCP_ESCALATION_ENABLED": "1",
              "SHOPIFY_CATALOG_ATTRIBUTION": "shadow", "AGENT_CHECKOUT_UCP_CART_REFERRAL_ENABLED": None}


def test_flags_must_match_and_cart_referral_must_be_off():
    cases = job.select_cases("all")
    job.require_flags(cases, LIVE_FLAGS)
    for value in ("", "0"):
        job.require_flags(cases, {**LIVE_FLAGS, "AGENT_CHECKOUT_UCP_CART_REFERRAL_ENABLED": value})
    for name, value in (("AGENT_CHECKOUT_UCP_CART_REFERRAL_ENABLED", "1"), ("SHOPIFY_CATALOG_ATTRIBUTION", "enforce"),
                        ("AGENT_CHECKOUT_UCP_CURRENCY_FOLLOWS_MARKET", None),
                        ("AGENT_CHECKOUT_MCP_STOREFRONT_ESCALATION_ENABLED", "true")):
        with pytest.raises(job.CaseError):
            job.require_flags(cases, {**LIVE_FLAGS, name: value})


def test_path_four_needs_a_reviewed_gateway_image_by_digest():
    cases = job.select_cases("OR-DE,MCP-K-US")
    job.require_reviewed_gateway(cases, GW_DIGEST)
    job.require_reviewed_gateway(cases, "sha256:67a5940f18c10231684cc6e15786318e0f5369215fcf34407ce33f248f2c2f31")
    job.require_reviewed_gateway(cases, "sha256:c07e945db5707366c5dd7f0d89ca7edbb845ddbcafa01204837a475220b51088")
    assert set(job.REVIEWED_GATEWAY_DIGESTS.values()) == {"080d7297f74e019fe5d88a25e78c3315ebdb080a",
                                                          "7d650dce841828059c060a7593992478ddf4f11d",
                                                          "16477d8341b125a269dce86fabdc0029bcc430a0"}
    for digest in (None, "sha256:" + "0" * 64, "7d650dce841828059c060a7593992478ddf4f11d"):   # a commit is not a digest
        with pytest.raises(job.CaseError, match="reviewed against"):
            job.require_reviewed_gateway(cases, digest)
    job.require_reviewed_gateway(job.select_cases("MCP-K-US"), None)   # checkout cases are not pinned this way


def test_requests_carry_the_market_and_the_owned_buyer_email_where_each_door_reads_them():
    by_id = {c.case_id: c.build(TARGETS) for c in job.CASES}
    assert by_id["MCP-SF-SG"]["market"] == "SG" and "market" not in by_id["MCP-SF-none"]
    assert all(by_id[c]["quote"]["customer_email"] == "ptt-buyer@owned.example"
               for c in ("MCP-SF-US", "MCP-SF-SG", "MCP-SF-DE", "MCP-SF-none", "MCP-SF-USA", "MCP-K-US"))
    assert by_id["MCP-K-US"]["quote"]["merchant_id"] == "merch_test_reviewed"
    assert by_id["UCP-SF-DE"]["checkout"]["context"] == {"address_country": "DE"}
    assert "context" not in by_id["UCP-SF-none"]["checkout"] and "email" not in json.dumps(by_id["UCP-SF-US"])
    assert by_id["UCP-SF-US"]["checkout"]["line_items"][0]["item"]["id"] == "prod_sf_1::v::var_sf_1"
    assert by_id["OR-long-s"]["payload"]["offers"]["market"] == "ſg"
    assert by_id["OR-USA-beside-SG"]["payload"] == {"offers": {"product": {"product_id": "prod_test_reviewed_0001"},
                                                               "limit": 3, "market": "USA"}, "market": "SG"}
    assert {c.case_id for c in job.CASES if c.shadow_lookup} == {"MCP-SF-US", "UCP-SF-US"}
    assert {c.case_id for c in job.CASES if c.storefront} == {"MCP-SF-US", "UCP-SF-US", "UCP-SF-none"}


# ------------------------------------------------------------------ the request guard (whole body)

def body_for(door, call, arguments):
    return job.envelope(door, call, arguments)


OK_CREATE = {"idempotency_key": "ptt-12345678-X-create", "market": "US",
             "quote": {"merchant_id": "m", "customer_email": "b@owned.example", "items": [{"product_id": "p", "quantity": 1}]}}


@pytest.mark.parametrize(("door", "body", "message"), [
    ("/mcp", body_for("/mcp", "complete_checkout_session", {"session_id": "s"}), "not an allowed call"),
    ("/mcp", body_for("/mcp", "create_payment_link", {}), "not an allowed call"),
    ("/ucp/mcp", body_for("/ucp/mcp", "complete_checkout", {"id": "x"}), "not an allowed call"),
    ("/ucp/mcp", body_for("/ucp/mcp", "create_checkout", {"checkout": {"reap": {}}}), "not allowed for this call"),
    ("/ucp/mcp", body_for("/ucp/mcp", "create_checkout", {"checkout": {"payment": {}}}), "not allowed"),
    ("/ucp/mcp", body_for("/ucp/mcp", "create_checkout", {"checkout": {"payment_data": {}}}), "not allowed"),
    ("/ucp/mcp", body_for("/ucp/mcp", "create_checkout", {"checkout": {"buyer": {"email": "x"}}}), "not allowed"),
    ("/mcp", body_for("/mcp", "create_checkout_session", {**OK_CREATE, "payment_authorization": {}}), "not allowed"),
    ("/mcp", body_for("/mcp", "create_checkout_session", {**OK_CREATE, "quote": {**OK_CREATE["quote"],
                                                                              "paymentMethod": "x"}}), "not allowed"),
    ("/mcp", body_for("/mcp", "create_checkout_session", {**OK_CREATE, "quote": {**OK_CREATE["quote"],
                                                                              "shipping_address": {}}}), "not allowed"),
    ("/mcp", body_for("/mcp", "create_checkout_session", {**OK_CREATE, "quote": {**OK_CREATE["quote"], "items": [
        {"product_id": "p", "card": "4"}]}}), "not allowed"),
    ("/mcp", {**body_for("/mcp", "get_checkout_session", {"session_id": "s"}),
              "params": {"name": "get_checkout_session", "arguments": {"session_id": "s"}, "_meta": {"payment": 1}}},
     "only name and arguments"),
    ("/mcp", {**body_for("/mcp", "get_checkout_session", {"session_id": "s"}), "checkout_token": "t"}, "not allowed"),
    ("/mcp", {"jsonrpc": "2.0", "method": "tools/list"}, "only tools/call"),
    ("/agent/shop/v1/invoke", {"operation": "submit_payment", "payload": {}}, "not an allowed call"),
    ("/agent/shop/v1/invoke", {"operation": "create_order", "payload": {}}, "not an allowed call"),
    ("/agent/shop/v1/invoke", {"operation": "offers.resolve", "payload": {"payment_method": {}}}, "not allowed"),
    ("/agent/shop/v1/invoke", {"operation": "offers.resolve", "payload": {}, "payment": {}}, "not allowed"),
    ("/agent/shop/v1/invoke", {"operation": "offers.resolve", "payload": {}, "metadata": {"payment_token": "t"}},
     "not allowed"),
    ("/agent/shop/v1/invoke", {"operation": "offers.resolve", "payload": {"offers": {"product": {"product_id": {}}}}},
     "must be a scalar"),
    ("/checkout/confirm", {"operation": "x"}, "door /checkout/confirm is not allowed"),
])
def test_the_guard_refuses_anything_outside_the_calls_schema_before_sending(door, body, message):
    fake = FakeGateway([])
    t = transport(fake)
    with pytest.raises(job.NotSent, match=message):
        t.send(door, body)
    assert fake.calls == [] and t.calls == 0


@pytest.mark.parametrize("name", ["Payment", "PAYMENT_TOKEN", "TOKEN", "token", "Card-Number", "cvc", "wallet",
                                  "paymentAuthorization", "payment_authorizations", "Credentials", "REAP",
                                  "billingAddress", "phone", "orderId", "Email"])
def test_the_denylist_folds_case_and_separators(monkeypatch, name):
    # Kill M2 (case-sensitive) and M18 (`token` dropped): the name is admitted by the schema, refused by the denylist.
    schema = {"offers.resolve": {"operation": job.S, "metadata": {}, "payload": {name: job.S}}}
    monkeypatch.setitem(job.CALL_SCHEMAS, job.INVOKE, schema)
    with pytest.raises(job.NotSent, match="forbidden request member"):
        job.check_request(job.INVOKE, {"operation": "offers.resolve", "payload": {name: "x"}})


def test_the_only_denylist_exceptions_are_the_payload_the_reviewed_email_and_the_ucp_country():
    assert job.DENY_EXCEPTIONS == {"payload", "params.arguments.quote.customer_email",
                                   "params.arguments.checkout.context.address_country"}
    job.check_request("/mcp", body_for("/mcp", "create_checkout_session", OK_CREATE))


# ------------------------------------------------------------------ the transport

def test_transport_sends_the_key_only_as_its_header_to_the_allowed_host():
    fake = FakeGateway([(200, b'{"status":"success","offers_count":1}')])
    t = transport(fake)
    t.send(job.INVOKE, case("OR-US").build(TARGETS))
    (url, headers, body, timeout), = fake.calls
    assert url == "https://gateway.pivota.cc/agent/shop/v1/invoke"
    assert headers["X-Agent-API-Key"] == KEY and KEY not in json.dumps(body) and "X-Agent-User-JWT" not in headers
    assert timeout == job.CALL_TIMEOUT_SECONDS


@pytest.mark.parametrize("base", ["http://gateway.pivota.cc", "https://evil.example", "https://xgateway.pivota.cc",
                                  "https://a.gateway.pivota.cc", "https://gateway.pivota.cc.evil.example",
                                  "https://gateway.pivota.cc:8443", "https://gateway.pivota.cc/x",
                                  "https://mcp.pivota.cc"])
def test_transport_refuses_any_other_base(base):
    with pytest.raises(job.CaseError, match="not allowed"):
        job.Transport(KEY, post=FakeGateway([]), base=base)


def test_transport_call_cap_and_key_format():
    fake = FakeGateway([(200, b"{}")] * 3)
    t = transport(fake, max_calls=2)
    body = case("OR-US").build(TARGETS)
    t.send(job.INVOKE, body)
    t.send(job.INVOKE, body)
    with pytest.raises(job.NotSent, match="call cap"):
        t.send(job.INVOKE, body)
    assert len(fake.calls) == 2
    for key in ("", "ak_123", "sk_live_" + "1" * 64, KEY + "\n" + KEY):
        with pytest.raises(job.CaseError, match="key format"):
            job.Transport(key, post=fake)


def test_the_user_token_never_rides_invoke():
    with pytest.raises(job.NotSent, match="only on the checkout doors"):
        transport(FakeGateway([])).send(job.INVOKE, case("OR-US").build(TARGETS), user_jwt="t")


def test_httpx_post_follows_no_redirect_ignores_proxy_env_and_has_a_total_deadline(monkeypatch):
    import httpx

    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"location": "https://evil.example/x"})
        return httpx.Response(200, content=b'{"ok":true}')

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    status, body = job._httpx_post("https://gateway.pivota.cc/redirect", {}, {}, 5.0,
                                   transport=httpx.MockTransport(handler))
    assert status == 302 and seen == ["https://gateway.pivota.cc/redirect"]

    def slow(request):
        def chunks():
            yield b"\n"
            clock["t"] += 30
            yield b"{}"
        return httpx.Response(200, content=chunks())

    clock = {"t": 0.0}
    monkeypatch.setattr(job.time, "monotonic", lambda: clock["t"])
    with pytest.raises(job.CaseError, match="total deadline"):
        job._httpx_post("https://gateway.pivota.cc/mcp", {}, {}, 20.0, transport=httpx.MockTransport(slow))


def test_answers_parse_with_heartbeat_newlines_errors_and_structured_content():
    assert job.parse_answer("/mcp", b"\n\n" + mcp_answer({"session_id": "s1"})) == {
        "is_error": False, "data": {"session_id": "s1"}}
    assert job.parse_answer("/mcp", INTAKE_REFUSAL)["is_error"] is True
    assert job.parse_answer("/mcp", b'{"jsonrpc":"2.0","id":1,"error":{"code":-32601,"message":"m"}}') == {
        "is_error": True, "rpc_error": True, "data": {"error": {"code": -32601}}}
    assert job.parse_answer("/mcp", b'{"result":{"structuredContent":{"session_id":"s2"}}}')["data"] == {
        "session_id": "s2"}
    assert job.parse_answer(job.INVOKE, b"<html>") == {"unparsed": True}


def test_scrub_keeps_only_bounded_scalars():
    answer = {"status": "x" * 500, "error": {"code": "X", "detail": {"secret": "s"}}, "offers": [{"price": 1}]}
    got = job.scrub(answer, ["status", "error.code", "error.detail", "offers", "missing.path"])
    assert got == {"status": "x" * job.MAX_SCALAR_CHARS, "error.code": "X", "error.detail": "<dict>",
                   "offers": "<list>", "missing.path": None}


# ------------------------------------------------------------------ read cases: exact and non-vacuous

REFUSED_INVALID = {"status": "error", "offers": [], "offers_count": 0,
                   "error": {"code": "MARKET_INVALID", "reason": "market_invalid", "message": "m"}}
REFUSED_NOT_PRICED = {"status": "error", "offers": [], "offers_count": 0,
                      "error": {"code": "MARKET_NOT_PRICED", "reason": "market_not_priced", "market": "DE"}}
SERVED_ANSWER = {"status": "success", "offers": [{"merchant_id": "m"}], "offers_count": 1}
NO_OFFER = {"status": "success", "offers": [], "offers_count": 0, "reason": "no_offer_available"}


@pytest.mark.parametrize(("case_id", "answer", "passes"), [
    ("OR-US", SERVED_ANSWER, True), ("OR-US", NO_OFFER, False), ("OR-US", REFUSED_INVALID, False),
    ("OR-US", None, False), ("OR-US", "error", False), ("OR-US", {"status": "success", "offers_count": "1"}, False),
    ("OR-SG", SERVED_ANSWER, True), ("OR-us-lower", SERVED_ANSWER, True), ("OR-none", SERVED_ANSWER, True),
    ("OR-USA", REFUSED_INVALID, True), ("OR-USA", SERVED_ANSWER, False), ("OR-USA", REFUSED_NOT_PRICED, False),
    ("OR-long-s", REFUSED_INVALID, True), ("OR-long-s", SERVED_ANSWER, False),
    ("OR-DE", REFUSED_NOT_PRICED, True), ("OR-DE", REFUSED_INVALID, False),
    ("OR-USA-beside-SG", SERVED_ANSWER, True), ("OR-USA-beside-SG", REFUSED_INVALID, False),
])
def test_every_read_case_passes_only_on_its_exact_expected_answer(case_id, answer, passes):
    fake = FakeGateway([(200, json.dumps(answer).encode())])
    row = job.run_case(case(case_id), transport(fake), RUN_ID, TARGETS)
    assert row["pass"] is passes
    assert fake.calls[0][2]["payload"]["offers"]["product"]["product_id"] == "prod_test_reviewed_0001"


def test_an_html_answer_or_a_non_200_fails_a_read_case():
    for answer in ((200, b"<html>"), (503, json.dumps(SERVED_ANSWER).encode())):
        row = job.run_case(case("OR-US"), transport(FakeGateway([answer])), RUN_ID, TARGETS)
        assert row["pass"] is False


# ------------------------------------------------------------------ checkout cases: cancel ALWAYS

def created(sid, status="ready_for_payment", **extra):
    return 200, mcp_answer({"session_id": sid, "status": status, "currency": "USD", **extra})


def kernel_ok(sid):
    return [created(sid), created(sid), (200, mcp_answer({"session_id": sid, "order_id": None, "status": "canceled"})),
            (200, mcp_answer({"session_id": sid, "status": "canceled"}))]


def sign(run_id, case_id):
    return f"tok-{case_id}"


def test_without_the_owned_email_the_mcp_create_is_refused_at_intake_like_the_gateway(monkeypatch):
    stripped = job.Case(**{**case("MCP-K-US").__dict__, "build": lambda t: {
        "market": "US", "quote": {"merchant_id": "m", "items": [{"product_id": "p", "variant_id": "v", "quantity": 1}]}}})
    fake = FakeGateway([])
    row = job.run_case(stripped, transport(fake), RUN_ID, TARGETS, sign)
    assert row["pass"] is False and row["got"]["data.status"] is None and row["created"] == []


def test_the_kernel_checkout_is_created_read_back_cancelled_and_confirmed():
    fake = FakeGateway(kernel_ok("cs_1"))
    row = job.run_case(case("MCP-K-US"), transport(fake), RUN_ID, TARGETS, sign)
    assert row["pass"] is True and row["created"] == ["cs_1"] and row["cancelled"] == ["cs_1"] and row["left_open"] == []
    assert [c[2]["params"]["name"] for c in fake.calls] == [
        "create_checkout_session", "get_checkout_session", "cancel_checkout_session", "get_checkout_session"]
    assert [c[1]["X-Agent-User-JWT"] for c in fake.calls] == ["tok-MCP-K-US"] * 4
    create_args, cancel_args = fake.calls[0][2]["params"]["arguments"], fake.calls[2][2]["params"]["arguments"]
    assert create_args["idempotency_key"] == f"ptt-{RUN_ID[:8]}-MCP-K-US-create"
    assert create_args["quote"]["customer_email"] == "ptt-buyer@owned.example"
    assert cancel_args == {"idempotency_key": f"ptt-{RUN_ID[:8]}-MCP-K-US-cancel", "session_id": "cs_1"}


def test_the_cancel_runs_even_when_the_read_back_fails():
    fake = FakeGateway([created("cs_3"), TimeoutError("read back timed out"),
                        (200, mcp_answer({"session_id": "cs_3", "status": "canceled"})),
                        (200, mcp_answer({"session_id": "cs_3", "status": "canceled"}))])
    row = job.run_case(case("MCP-K-US"), transport(fake), RUN_ID, TARGETS, sign)
    assert row["pass"] is False and row["error"] == "TimeoutError" and row["cancelled"] == ["cs_3"]
    assert row["left_open"] == []


@pytest.mark.parametrize("cancel_answers", [
    [TimeoutError("cancel timed out")],
    [(200, mcp_answer({"error": {"code": "OPERATION_NOT_ALLOWED"}}, True)), created("cs_4")],
    [(200, mcp_answer({"session_id": "cs_4", "status": "canceled"})), created("cs_4")],     # read back disagrees
    [(500, b"{}"), created("cs_4")],
])
def test_a_failed_cancel_is_left_open_and_the_run_is_not_ok(cancel_answers):
    fake = FakeGateway([created("cs_4"), created("cs_4"), *cancel_answers])
    rows = []
    summary = job.run_all([case("MCP-K-US")], transport(fake), RUN_ID, TARGETS, out=rows.append, signer=sign)
    assert rows[0]["pass"] is False and rows[0]["left_open"] == ["cs_4"] and rows[0]["cancelled"] == []
    assert summary["ok"] is False and summary["left_open"] == ["cs_4"]


def test_the_cancel_note_carries_no_answer_content():
    leaky = {"session_id": "cs_5", "status": {"buyer": {"email": "someone@x.example"}, "shipping_address": "1 St"}}
    fake = FakeGateway([created("cs_5"), created("cs_5"), (200, mcp_answer(leaky)), (200, mcp_answer(leaky))])
    row = job.run_case(case("MCP-K-US"), transport(fake), RUN_ID, TARGETS, sign)
    assert "someone@" not in json.dumps(row) and "1 St" not in json.dumps(row) and "<dict>" in row["cancel_note"]


@pytest.mark.parametrize("create_answer", [
    TimeoutError("create timed out"),
    (502, b"bad gateway"),
    (200, b"event: message\ndata: {...}"),                          # unreadable 200
    (200, mcp_answer({"status": "ready_for_payment"})),             # created, id not where we read it
    # V3-P2-1: a failure INSIDE the gateway after the heartbeat committed the 200 may follow a minted quote.
    (200, b'\n\n{"jsonrpc":"2.0","id":1,"error":{"code":-32603,"message":"internal"}}'),
    (200, mcp_answer({"error": {"code": "UNEXPECTED_ERROR"}}, True)),
    (200, mcp_answer({"error": {"code": "CHECKOUT_OUTCOME_UNKNOWN"}}, True)),
    (200, mcp_answer({"error": {"code": "INTERNAL_ERROR"}}, True)),   # V6-P3-1: inside result.isError (kills W4)
    (200, mcp_answer({"error": {}}, True)),                         # no code at all
    (500, mcp_answer({"error": {"code": "NO_MERCHANT_OFFER", "detail": {"reason": "market_not_priced"}}}, True)),
    # V5-P3-1: a top-level JSON-RPC error is the transport failing, never the tool's named refusal (kills W4).
    (200, b'{"jsonrpc":"2.0","id":1,"error":{"code":"INTERNAL_ERROR","message":"m"}}'),
    (200, b'{"jsonrpc":"2.0","id":1,"error":{"code":"NO_MERCHANT_OFFER","message":"m"}}'),
])
def test_a_create_whose_outcome_cannot_be_read_is_an_unknown_left_open(create_answer):
    fake = FakeGateway([create_answer])
    rows = []
    summary = job.run_all([case("MCP-K-US")], transport(fake), RUN_ID, TARGETS, out=rows.append, signer=sign)
    assert rows[0]["pass"] is False and rows[0]["left_open"] == ["unknown:MCP-K-US"]
    assert summary["ok"] is False and len(fake.calls) == 1                                   # no retry on create


def test_only_a_200_named_refusal_counts_as_nothing_created():
    assert job.nothing_created(200, {"is_error": True, "data": {"error": {"code": "NO_MERCHANT_OFFER"}}})
    assert job.nothing_created(200, {"is_error": True, "data": {"error": {"code": "USER_AUTH_REQUIRED"}}})
    for status, answer in ((500, {"is_error": True, "data": {"error": {"code": "NO_MERCHANT_OFFER"}}}),
                           (200, {"is_error": True, "data": {"error": {"code": -32603}}}),
                           (200, {"is_error": True, "data": {"error": {"code": "UNEXPECTED_ERROR"}}}),
                           (200, {"is_error": False, "data": {}}), (200, {"unparsed": True}), (200, None)):
        assert not job.nothing_created(status, answer)
    rpc = job.parse_answer("/mcp", b'{"jsonrpc":"2.0","id":1,"error":{"code":"NO_MERCHANT_OFFER"}}')
    assert rpc["rpc_error"] is True and not job.nothing_created(200, rpc)


def test_the_kernel_case_needs_the_flags_that_declare_market_on_mcp():
    flags = case("MCP-K-US").requires_flags
    assert flags["AGENT_CHECKOUT_MCP_STOREFRONT_ESCALATION_ENABLED"] == "1"
    assert flags["AGENT_CHECKOUT_UCP_CURRENCY_FOLLOWS_MARKET"] == "1"
    with pytest.raises(job.CaseError):
        job.require_flags([case("MCP-K-US")], {**LIVE_FLAGS, "AGENT_CHECKOUT_MCP_STOREFRONT_ESCALATION_ENABLED": None})


def test_a_create_refused_before_sending_is_not_unknown():
    fake = FakeGateway([])
    t = transport(fake, max_calls=job.CHECKOUT_RESERVE_CALLS)          # not enough to create AND cancel
    row = job.run_case(case("MCP-K-US"), t, RUN_ID, TARGETS, sign)
    assert row["pass"] is False and row["left_open"] == [] and "cancel a checkout" in row["error"]
    assert fake.calls == []


@pytest.mark.parametrize(("case_id", "reason", "code"), [
    ("MCP-SF-SG", "seller_not_priced_in_market_currency", "NO_MERCHANT_OFFER"),
    ("MCP-SF-DE", "market_not_priced", "NO_MERCHANT_OFFER"),
    ("MCP-SF-none", "market_required_for_seller_storefront", "QUOTE_REQUIRED"),
    ("UCP-SF-SG", "seller_not_priced_in_market_currency", "NO_MERCHANT_OFFER"),
    ("UCP-SF-USA", "ucp_market_invalid", "QUOTE_REQUIRED"),
])
def test_a_designed_refusal_creates_nothing_and_passes_on_its_reason(case_id, reason, code):
    fake = FakeGateway([(200, mcp_answer({"error": {"code": code, "detail": {"reason": reason}}}, True))])
    row = job.run_case(case(case_id), transport(fake), RUN_ID, TARGETS, sign)
    assert row["pass"] is True and row["created"] == [] and row["left_open"] == [] and len(fake.calls) == 1
    wrong = FakeGateway([(200, mcp_answer({"error": {"code": code, "detail": {"reason": "other"}}}, True))])
    assert job.run_case(case(case_id), transport(wrong), RUN_ID, TARGETS, sign)["pass"] is False


def storefront_answer(sid, host, status="completes_on_seller_storefront"):
    return 200, mcp_answer({"session_id": sid, "id": sid, "status": status, "currency": "USD", "amount_unit": "minor",
                            "continue_url": f"https://{host}/cart/c/abc?key=k"})


def test_a_storefront_cart_is_never_cancel_called_and_is_reported_as_an_expiring_hash():
    sid = "esc_eyJjIjoiY2FydC1pZC1zZWNyZXQifQ"
    fake = FakeGateway([storefront_answer(sid, "shop.reviewed.example"), storefront_answer(sid, "shop.reviewed.example")])
    rows = []
    summary = job.run_all([case("MCP-SF-US")], transport(fake), RUN_ID, TARGETS, out=rows.append, signer=sign)
    assert [c[2]["params"]["name"] for c in fake.calls] == ["create_checkout_session", "get_checkout_session"]
    assert rows[0]["pass"] is True and rows[0]["left_open"] == []
    assert rows[0]["expires_on_seller"] == rows[0]["created"] and rows[0]["created"][0].startswith("esc_#")
    assert sid not in json.dumps(rows) and "cart-id-secret" not in json.dumps(rows)
    assert summary["ok"] is True and summary["expires_on_seller"] == rows[0]["created"]


def test_a_storefront_cart_at_another_store_fails_the_case():
    sid = "esc_other"
    fake = FakeGateway([storefront_answer(sid, "other.example"), storefront_answer(sid, "other.example")])
    row = job.run_case(case("MCP-SF-US"), transport(fake), RUN_ID, TARGETS, sign)
    assert row["pass"] is False and row["got"]["continue_url.host_is_reviewed"] is False
    assert "other.example" not in json.dumps(row) and row["expires_on_seller"]          # still reported


def test_the_ucp_storefront_create_reads_back_on_its_own_door():
    sid = "esc_ucp"
    answer = storefront_answer(sid, "shop.reviewed.example", status="requires_escalation")
    fake = FakeGateway([answer, answer])
    row = job.run_case(case("UCP-SF-US"), transport(fake), RUN_ID, TARGETS, sign)
    assert row["pass"] is True and [c[0].rsplit("/", 2)[-2:] for c in fake.calls] == [["ucp", "mcp"]] * 2
    assert fake.calls[0][2]["params"]["arguments"]["meta"]["idempotency-key"] == f"ptt-{RUN_ID[:8]}-UCP-SF-US-create"


def test_a_checkout_case_without_the_issuer_key_fails_before_any_call():
    fake = FakeGateway([])
    row = job.run_case(case("MCP-K-US"), transport(fake), RUN_ID, TARGETS, None)
    assert row["pass"] is False and "issuer key" in row["error"] and fake.calls == [] and row["left_open"] == []


# ------------------------------------------------------------------ the user token

def ec_key_pem(curve=None):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(curve or ec.SECP256R1())
    return key, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption())


def test_the_user_token_verifies_like_the_gateway_checks_it_and_attests_no_buyer_data():
    import jwt

    key, pem = ec_key_pem()
    signer = job.make_user_signer(pem, TARGETS.user_token, now=lambda: NOW)
    token = signer(RUN_ID, "MCP-K-US")
    assert jwt.get_unverified_header(token) == {"alg": "ES256", "kid": "ptt-key-1", "typ": "JWT"}
    claims = jwt.decode(token, key.public_key(), algorithms=["ES256"], audience="pivota-gateway-test",
                        issuer="https://test-issuer.example/ptt", options={"verify_exp": False})
    jti = claims.pop("jti")
    assert len(jti) == 36 and claims == {
        "iss": "https://test-issuer.example/ptt", "aud": "pivota-gateway-test", "azp": "ptt-job", "scope": "ptt:checkout",
        "sub": "ptt-test-buyer", "iat": int(NOW.timestamp()), "nbf": int(NOW.timestamp()),
        "exp": int(NOW.timestamp()) + 300, "acp_session_id": f"ptt-{RUN_ID}-MCP-K-US"}
    assert jwt.decode(signer(RUN_ID, "MCP-K-US"), key.public_key(), algorithms=["ES256"], audience="pivota-gateway-test",
                      options={"verify_exp": False})["jti"] != jti


def test_the_signer_refuses_a_non_p256_key():
    from cryptography.hazmat.primitives.asymmetric import ec

    with pytest.raises(job.CaseError, match="P-256"):
        job.make_user_signer(ec_key_pem(ec.SECP384R1())[1], TARGETS.user_token, now=lambda: NOW)


# ------------------------------------------------------------------ the job's preconditions and output

def test_gate_rules():
    assert job.check_gate(gate(), NOW)["verdict"] == "GATE_OPEN"
    assert job.check_gate(gate(observed=NOW - timedelta(seconds=299)), NOW)
    for raw, now, message in ((gate(observed=NOW - timedelta(seconds=301)), NOW, "stale"),
                              (gate(observed=NOW + timedelta(seconds=5)), NOW, "stale"),
                              (gate(cpu=0.2), NOW, "readings"), (gate(running=1), NOW, "readings"),
                              (gate(cpu=1), NOW, "readings"), (None, NOW, "not GATE_OPEN"), ("{", NOW, "not GATE_OPEN"),
                              (gate(observed=NOW.replace(hour=5, minute=20)), NOW.replace(hour=5, minute=20), "closed"),
                              (gate(observed=NOW.replace(hour=10, minute=45)), NOW.replace(hour=10, minute=45), "closed")):
        with pytest.raises(job.CaseError, match=message):
            job.check_gate(raw, now)


def armed_env(**over):
    env = {"CLOUD_RUN_JOB": job.JOB_NAME, job.ENV + "ARMED": "1", job.ENV + "RELEASE_COMMIT": RELEASE,
           job.ENV + "RUN_ID": RUN_ID, job.ENV + "CASES": "OR-US,OR-DE", job.ENV + "GATE": gate(),
           "CLOUD_RUN_EXECUTION": "prod-test-transactions-abc12"}
    env.update(over)
    return {k: v for k, v in env.items() if v is not None}


@pytest.mark.parametrize(("over", "message"), [
    ({job.ENV + "ARMED": None}, "not armed"), ({job.ENV + "ARMED": "true"}, "not armed"),
    ({"K_SERVICE": "web"}, "runs only"), ({"CLOUD_RUN_JOB": "other-job"}, "runs only"),
    ({job.ENV + "RELEASE_COMMIT": "b" * 40}, "release commit"), ({job.ENV + "RUN_ID": "x"}, "run id"),
])
def test_environment_refusals(over, message):
    with pytest.raises(job.CaseError, match=message):
        job.check_environment(armed_env(**over), RELEASE)


@pytest.fixture
def mounts(tmp_path, monkeypatch):
    monkeypatch.setattr(job, "TARGETS_SHA256", TARGETS_SHA)
    commit = tmp_path / "commit"
    commit.write_text(RELEASE + "\n")
    key_path = tmp_path / "key"
    key_path.write_text(KEY + "\n")
    targets_path = tmp_path / "targets.json"
    targets_path.write_bytes(TARGETS_RAW)
    _, pem = ec_key_pem()
    user_key = tmp_path / "key.pem"
    user_key.write_bytes(pem)
    return {"image_commit_path": commit, "key_path": key_path, "targets_path": targets_path, "user_key_path": user_key,
            "pem": pem}


def run_main(capsys, mounts, env, fake, **over):
    paths = {k: v for k, v in mounts.items() if k != "pem"}
    paths.update(over)
    code = job.main(env, now=lambda: NOW, transport_factory=lambda k: job.Transport(k, post=fake), **paths)
    captured = capsys.readouterr()
    rows, bad = job.parse_result_lines(captured.out)
    assert bad == []
    return code, rows, captured.out + captured.err


def test_main_runs_the_selected_cases_and_reports_only_shas(capsys, mounts):
    fake = FakeGateway([(200, json.dumps(SERVED_ANSWER).encode()), (200, json.dumps(REFUSED_NOT_PRICED).encode())])
    code, rows, out = run_main(capsys, mounts, armed_env(), fake)
    assert code == 0 and len(fake.calls) == 2 and [r["kind"] for r in rows] == ["case", "case", "run"]
    assert rows[-1]["ok"] is True and rows[-1]["targets_sha256"] == TARGETS_SHA
    assert "seller_host_sha256" not in rows[-1]                      # an unsalted host hash is reversible: none at all
    host_sha = hashlib.sha256(b"shop.reviewed.example").hexdigest()
    assert "shop.reviewed.example" not in out and "merch_obs_reviewed" not in out and host_sha not in out


def test_the_key_token_and_pem_never_leave_the_job_even_when_every_call_fails(capsys, mounts):
    # Kill M11: every transport call raises; nothing secret may appear in a row, stdout or stderr.
    class Boom(Exception):
        def __str__(self):
            return f"connection reset while sending {KEY}"

    fake = FakeGateway([Boom()] * 8, intake=False)
    env = armed_env(**{job.ENV + "CASES": "OR-US,MCP-K-US,MCP-SF-DE,UCP-SF-US"})
    code, rows, out = run_main(capsys, mounts, env, fake)
    assert code == 1 and rows[-1]["ok"] is False
    tokens = [c[1].get("X-Agent-User-JWT") for c in fake.calls if c[1].get("X-Agent-User-JWT")]
    assert tokens
    # Search the DECODED rows and the raw output: the key, the PEM body and each token's signature, in 8-char slices.
    # (A token's header and claims are public, and base64 output can collide with them, so they are not searched.)
    haystack = out + json.dumps(rows)
    pem_body = "".join(line for line in mounts["pem"].decode().splitlines() if "-----" not in line)
    secrets = [KEY, pem_body, *[t.rsplit(".", 1)[1] for t in tokens]]
    for secret in secrets:
        for i in range(0, len(secret) - 8, 4):
            assert secret[i:i + 8] not in haystack
    for token in tokens:
        assert token not in haystack
    assert all(r.get("error") in (None, "Boom") for r in rows if r["kind"] == "case")


def test_main_refuses_with_one_run_line_and_no_call(capsys, mounts, tmp_path, monkeypatch):
    for over, env, message in (
        ({}, armed_env(**{job.ENV + "GATE": None}), "not GATE_OPEN"),
        ({"key_path": tmp_path / "absent"}, armed_env(), "FileNotFoundError"),
        ({"user_key_path": tmp_path / "absent"}, armed_env(**{job.ENV + "CASES": "MCP-K-US"}), "FileNotFoundError"),
    ):
        fake = FakeGateway([])
        code, rows, _ = run_main(capsys, mounts, env, fake, **over)
        assert code == 3 and fake.calls == [] and len(rows) == 1 and message in rows[0]["refused"]
    monkeypatch.setattr(job, "TARGETS_SHA256", "")
    code, rows, _ = run_main(capsys, mounts, armed_env(), FakeGateway([]))
    assert code == 3 and "not been reviewed" in rows[0]["refused"]


def test_an_encrypted_or_unsupported_issuer_key_refuses_with_a_run_line(capsys, mounts, tmp_path):
    from cryptography.hazmat.primitives import serialization

    key, _ = ec_key_pem()
    encrypted = tmp_path / "enc.pem"
    encrypted.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                            serialization.BestAvailableEncryption(b"pw")))
    code, rows, _ = run_main(capsys, mounts, armed_env(**{job.ENV + "CASES": "MCP-K-US"}), FakeGateway([]),
                             user_key_path=encrypted)
    assert code == 3 and rows[0]["refused"] == "TypeError"


def test_main_exits_nonzero_when_a_case_fails(capsys, mounts):
    fake = FakeGateway([(200, json.dumps(SERVED_ANSWER).encode()), (200, json.dumps(SERVED_ANSWER).encode())])
    code, rows, _ = run_main(capsys, mounts, armed_env(), fake)
    assert code == 1 and rows[-1]["ok"] is False and rows[-1]["passed"] == 1


def test_the_http_client_ignores_proxy_env_and_netrc_and_never_retries(monkeypatch):
    # Kill M20: the client itself must be built with trust_env=False, follow_redirects=False and retries=0.
    import httpx

    built = {}
    real_client = httpx.Client

    def spy(**kwargs):
        built.update(kwargs)
        return real_client(**{**kwargs, "transport": httpx.MockTransport(lambda r: httpx.Response(200, content=b"{}"))})

    monkeypatch.setattr(httpx, "Client", spy)
    job._httpx_post("https://gateway.pivota.cc/mcp", {}, {}, 5.0)
    assert built["trust_env"] is False and built["follow_redirects"] is False
    assert built["transport"]._pool._retries == 0
