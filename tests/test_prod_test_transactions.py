"""Prod test-transaction job: the case list is fixed and safe, the transport can only make allow-listed calls, every
expected/got comparison is exact, and a created checkout is always cancelled or reported LEFT OPEN. Offline: every
gateway answer here is a fake shaped like the gateway's own (file refs in scripts/ops/prod_test_transactions.py)."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import json

import pytest

from scripts.ops import prod_test_transactions as job

KEY = "ak_" + "1" * 64
RUN_ID = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
RELEASE = "a" * 40
NOW = datetime(2026, 1, 2, 7, 0, tzinfo=timezone.utc)
PRODUCT = "prod_test_reviewed_0001"


def gate(observed=NOW, cpu=0.05, running=0, **extra):
    return json.dumps({"verdict": "GATE_OPEN", "observed_at": observed.isoformat(), "summary": "s",
                       "source": "scripts/ops/oneoff_gate.py", "cpu_peak_5m": cpu,
                       "db_job_executions_running": running, **extra})


def mcp_answer(data, is_error=False):
    body = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": json.dumps(data)}],
                                                   **({"isError": True} if is_error else {})}}
    return json.dumps(body).encode()


class FakeGateway:
    """Records every call (url, headers, body) and answers from a script of (status, bytes) or exceptions."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append((url, dict(headers), json.loads(json.dumps(body)), timeout))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def reviewed_product(monkeypatch):
    monkeypatch.setattr(job, "OFFERS_PRODUCT_ID", PRODUCT)
    # the case requests were built at import with the empty constant: rebuild them for the reviewed value
    rebuilt = []
    for case in job.CASES:
        request = json.loads(json.dumps(case.request))
        if case.door == "/agent/shop/v1/invoke":
            request["payload"]["offers"]["product"]["product_id"] = PRODUCT
        rebuilt.append(job.Case(**{**case.__dict__, "request": request}))
    monkeypatch.setattr(job, "CASES", tuple(rebuilt))
    return PRODUCT


# ------------------------------------------------------------------ the case list

def test_case_list_is_valid_and_every_case_states_its_expected_outcome():
    job.check_case_list()
    assert {c.case_id for c in job.CASES} >= {"OR-US", "OR-USA", "OR-long-s", "OR-DE", "OR-none"}
    for case in job.CASES:
        assert case.call in job.ALLOWED_CALLS[case.door]
        assert case.expected


def test_no_case_ever_calls_a_payment_or_completion_tool():
    paying = {"complete_checkout_session", "complete_checkout", "create_payment_link", "submit_payment",
              "confirm_payment", "create_order", "start_purchase", "update_checkout_session", "update_checkout",
              "resume_checkout", "preview_merchant_checkout_quote", "preview_quote", "checkout_handoff"}
    for door, calls in job.ALLOWED_CALLS.items():
        assert not calls & paying, door
    assert all(c.call not in paying for c in job.CASES)


def test_selection_refuses_unknown_repeated_disabled_and_unreviewed_cases(monkeypatch):
    with pytest.raises(job.CaseError, match="needs OFFERS_PRODUCT_ID"):
        job.select_cases("OR-US")                          # the product id has not been reviewed in
    monkeypatch.setattr(job, "OFFERS_PRODUCT_ID", PRODUCT)
    assert [c.case_id for c in job.select_cases("OR-US,OR-DE")] == ["OR-US", "OR-DE"]
    assert len(job.select_cases("all")) == len([c for c in job.CASES if c.enabled])
    for raw, message in (("", "no cases"), ("OR-US,OR-US", "repeats"), ("OR-NOPE", "unknown case")):
        with pytest.raises(job.CaseError, match=message):
            job.select_cases(raw)
    disabled = job.Case("X-OFF", "/agent/shop/v1/invoke", "read", "offers.resolve", {"operation": "offers.resolve"},
                        "US", {"error.code": None}, enabled=False)
    monkeypatch.setattr(job, "CASES", job.CASES + (disabled,))
    with pytest.raises(job.CaseError, match="review only"):
        job.select_cases("X-OFF")
    assert "X-OFF" not in [c.case_id for c in job.select_cases("all")]


def test_flags_must_match_exactly():
    case = job.Case("F-1", "/mcp", "checkout", "create_checkout_session", {}, "US", {"data.status": "x"},
                    requires_flags={"AGENT_CHECKOUT_MCP_STOREFRONT_ESCALATION_ENABLED": "1"})
    job.require_flags([case], {"AGENT_CHECKOUT_MCP_STOREFRONT_ESCALATION_ENABLED": "1"})
    for value in (None, "true", "0"):
        with pytest.raises(job.CaseError, match="needs AGENT_CHECKOUT_MCP_STOREFRONT_ESCALATION_ENABLED=1"):
            job.require_flags([case], {"AGENT_CHECKOUT_MCP_STOREFRONT_ESCALATION_ENABLED": value})


# ------------------------------------------------------------------ the transport

@pytest.mark.parametrize(("door", "body", "message"), [
    ("/mcp", job.mcp_body("complete_checkout_session", {"session_id": "s"}), "not an allowed call"),
    ("/mcp", job.mcp_body("create_payment_link", {}), "not an allowed call"),
    ("/ucp/mcp", job.mcp_body("complete_checkout", {"id": "x"}), "not an allowed call"),
    ("/ucp/mcp", job.mcp_body("create_checkout", {"checkout": {"reap": {}}}), "forbidden request member"),
    ("/ucp/mcp", job.mcp_body("create_checkout", {"checkout": {"payment": {"x": 1}}}), "forbidden request member"),
    ("/mcp", job.mcp_body("create_checkout_session", {"quote": {"items": [{"card": "4"}]}}), "forbidden request member"),
    ("/agent/shop/v1/invoke", {"operation": "submit_payment", "payload": {}}, "not an allowed call"),
    ("/agent/shop/v1/invoke", {"operation": "create_order", "payload": {}}, "not an allowed call"),
    ("/agent/shop/v1/invoke", {"operation": "offers.resolve", "payload": {"payment_method": {}}}, "forbidden"),
    ("/checkout/confirm", {"operation": "x"}, "door /checkout/confirm is not allowed"),
    ("/mcp", {"jsonrpc": "2.0", "method": "tools/list"}, "only tools/call"),
])
def test_transport_refuses_anything_but_the_allowed_calls_before_sending(door, body, message):
    fake = FakeGateway([])
    transport = job.Transport(KEY, post=fake)
    with pytest.raises(job.CaseError, match=message):
        transport.send(door, body)
    assert fake.calls == [] and transport.calls == 0


def test_transport_sends_the_key_only_as_its_header_to_the_allowed_host_with_one_timeout():
    fake = FakeGateway([(200, b'{"status":"success"}')])
    transport = job.Transport(KEY, post=fake)
    transport.send("/agent/shop/v1/invoke", {"operation": "offers.resolve", "payload": {}})
    (url, headers, body, timeout), = fake.calls
    assert url == "https://gateway.pivota.cc/agent/shop/v1/invoke"
    assert headers["X-Agent-API-Key"] == KEY and KEY not in json.dumps(body)
    assert timeout == job.CALL_TIMEOUT_SECONDS


def test_transport_has_a_hard_call_cap_and_refuses_bad_keys_and_hosts():
    fake = FakeGateway([(200, b"{}")] * 3)
    transport = job.Transport(KEY, post=fake, max_calls=2)
    body = {"operation": "offers.resolve", "payload": {}}
    transport.send("/agent/shop/v1/invoke", body)
    transport.send("/agent/shop/v1/invoke", body)
    with pytest.raises(job.CaseError, match="call cap"):
        transport.send("/agent/shop/v1/invoke", body)
    assert len(fake.calls) == 2
    for key in ("", "ak_123", "sk_live_" + "1" * 64, KEY + "\n" + KEY):
        with pytest.raises(job.CaseError, match="key format"):
            job.Transport(key, post=fake)
    for base in ("http://gateway.pivota.cc", "https://evil.example", "https://gateway.pivota.cc.evil.example"):
        with pytest.raises(job.CaseError, match="not allowed"):
            job.Transport(KEY, post=fake, base=base)


def test_answers_parse_with_heartbeat_newlines_and_mcp_errors():
    assert job.parse_answer("/mcp", b"\n\n" + mcp_answer({"session_id": "s1"})) == {
        "is_error": False, "data": {"session_id": "s1"}}
    assert job.parse_answer("/mcp", mcp_answer({"error": {"code": "USER_AUTH_REQUIRED"}}, True))["is_error"] is True
    assert job.parse_answer("/mcp", b'{"jsonrpc":"2.0","id":1,"error":{"code":-32601,"message":"m"}}') == {
        "is_error": True, "data": {"error": {"code": -32601}}}
    assert job.parse_answer("/agent/shop/v1/invoke", b"not json") == {"unparsed": True}


def test_scrub_keeps_only_asserted_scalars():
    answer = {"status": "error", "error": {"code": "X", "detail": {"secret": "s"}}, "offers": [{"price": 1}]}
    assert job.scrub(answer, ["status", "error.code", "error.detail", "offers", "missing.path"]) == {
        "status": "error", "error.code": "X", "error.detail": "<dict>", "offers": "<list>", "missing.path": None}


# ------------------------------------------------------------------ read cases: expected vs got

REFUSED_INVALID = {"status": "error", "offers": [], "offers_count": 0,
                   "error": {"code": "MARKET_INVALID", "reason": "market_invalid", "message": "m"}}
REFUSED_NOT_PRICED = {"status": "error", "offers": [], "offers_count": 0,
                      "error": {"code": "MARKET_NOT_PRICED", "reason": "market_not_priced", "market": "DE"}}
SERVED_ANSWER = {"status": "success", "offers": [{"merchant_id": "m"}], "offers_count": 1}


@pytest.mark.parametrize(("case_id", "answer", "passes"), [
    ("OR-US", SERVED_ANSWER, True), ("OR-US", REFUSED_INVALID, False),
    ("OR-SG", SERVED_ANSWER, True), ("OR-us-lower", SERVED_ANSWER, True), ("OR-none", SERVED_ANSWER, True),
    ("OR-USA", REFUSED_INVALID, True), ("OR-USA", SERVED_ANSWER, False), ("OR-USA", REFUSED_NOT_PRICED, False),
    ("OR-long-s", REFUSED_INVALID, True), ("OR-long-s", SERVED_ANSWER, False),
    ("OR-DE", REFUSED_NOT_PRICED, True), ("OR-DE", REFUSED_INVALID, False),
    ("OR-USA-beside-SG", SERVED_ANSWER, True), ("OR-USA-beside-SG", REFUSED_INVALID, False),
])
def test_every_read_case_passes_only_on_its_exact_expected_answer(reviewed_product, case_id, answer, passes):
    case, = job.select_cases(case_id)
    fake = FakeGateway([(200, json.dumps(answer).encode())])
    row = job.run_case(case, job.Transport(KEY, post=fake), RUN_ID)
    assert row["pass"] is passes
    assert set(row["got"]) == set(case.expected) | {"http_status"}
    assert fake.calls[0][2]["payload"]["offers"]["product"]["product_id"] == PRODUCT


def test_the_long_s_case_sends_the_long_s_and_the_beside_case_sends_both_markets(reviewed_product):
    long_s, beside = job.select_cases("OR-long-s,OR-USA-beside-SG")
    assert long_s.request["payload"]["offers"]["market"] == "ſg"
    assert beside.request["payload"]["offers"]["market"] == "USA" and beside.request["payload"]["market"] == "SG"


def test_a_non_200_or_a_transport_error_fails_the_case_and_the_run(reviewed_product):
    cases = job.select_cases("OR-US,OR-DE")
    fake = FakeGateway([(503, b"{}"), TimeoutError("read timed out")])
    rows = []
    summary = job.run_all(cases, job.Transport(KEY, post=fake), RUN_ID, out=rows.append)
    assert [r["pass"] for r in rows[:2]] == [False, False]
    assert rows[1]["error"].startswith("TimeoutError")
    assert summary == {"kind": "run", "ok": False, "cases": 2, "passed": 0, "calls": 2, "left_open": []}


# ------------------------------------------------------------------ checkout cases: cancel ALWAYS

def checkout_case(door="/mcp", **expected):
    request = ({"market": "US", "quote": {"merchant_id": "merch_test", "items": [{"product_id": "p", "quantity": 1}]}}
               if door == "/mcp" else {"checkout": {"line_items": [{"item": {"id": "p"}, "quantity": 1}],
                                                    "context": {"address_country": "US"}}})
    call = "create_checkout_session" if door == "/mcp" else "create_checkout"
    return job.Case("CO-1", door, "checkout", call, request, "US", expected or {"data.status": "ready_for_payment"})


def test_a_created_checkout_is_read_back_cancelled_and_confirmed():
    fake = FakeGateway([(200, mcp_answer({"session_id": "cs_1", "status": "ready_for_payment", "currency": "USD"})),
                        (200, mcp_answer({"session_id": "cs_1", "status": "ready_for_payment"})),
                        (200, mcp_answer({"session_id": "cs_1", "order_id": None, "status": "canceled"})),
                        (200, mcp_answer({"session_id": "cs_1", "status": "canceled"}))])
    row = job.run_case(checkout_case(), job.Transport(KEY, post=fake), RUN_ID)
    assert row["pass"] is True and row["created"] == ["cs_1"] and row["cancelled"] == ["cs_1"]
    assert row["left_open"] == []
    tools = [c[2]["params"]["name"] for c in fake.calls]
    assert tools == ["create_checkout_session", "get_checkout_session", "cancel_checkout_session", "get_checkout_session"]
    create_args, cancel_args = fake.calls[0][2]["params"]["arguments"], fake.calls[2][2]["params"]["arguments"]
    assert create_args["idempotency_key"] == f"ptt-{RUN_ID[:8]}-CO-1-create"
    assert cancel_args == {"idempotency_key": f"ptt-{RUN_ID[:8]}-CO-1-cancel", "session_id": "cs_1"}


def test_a_ucp_checkout_is_cancelled_through_the_kernel_cancel_on_mcp():
    fake = FakeGateway([(200, mcp_answer({"id": "cs_2", "status": "ready_for_payment"})),
                        (200, mcp_answer({"id": "cs_2", "status": "ready_for_payment"})),
                        (200, mcp_answer({"session_id": "cs_2", "status": "canceled"})),
                        (200, mcp_answer({"session_id": "cs_2", "status": "canceled"}))])
    row = job.run_case(checkout_case("/ucp/mcp"), job.Transport(KEY, post=fake), RUN_ID)
    assert row["pass"] is True and row["cancelled"] == ["cs_2"]
    assert [c[0].rsplit("/", 1)[-1] for c in fake.calls] == ["mcp", "mcp", "mcp", "mcp"]
    assert fake.calls[0][0].endswith("/ucp/mcp") and fake.calls[2][0].endswith("/mcp")
    assert fake.calls[0][2]["params"]["arguments"]["meta"]["idempotency-key"] == f"ptt-{RUN_ID[:8]}-CO-1-create"


def test_the_cancel_runs_even_when_the_read_back_fails():
    fake = FakeGateway([(200, mcp_answer({"session_id": "cs_3", "status": "ready_for_payment"})),
                        TimeoutError("read back timed out"),
                        (200, mcp_answer({"session_id": "cs_3", "status": "canceled"})),
                        (200, mcp_answer({"session_id": "cs_3", "status": "canceled"}))])
    row = job.run_case(checkout_case(), job.Transport(KEY, post=fake), RUN_ID)
    assert row["pass"] is False and row["error"].startswith("TimeoutError")
    assert row["cancelled"] == ["cs_3"] and row["left_open"] == []


@pytest.mark.parametrize("cancel_answers", [
    [TimeoutError("cancel timed out")],
    [(200, mcp_answer({"error": {"code": "OPERATION_NOT_ALLOWED"}}, True)),
     (200, mcp_answer({"session_id": "cs_4", "status": "ready_for_payment"}))],
    [(200, mcp_answer({"session_id": "cs_4", "status": "canceled"})),
     (200, mcp_answer({"session_id": "cs_4", "status": "ready_for_payment"}))],     # read back disagrees
    [(500, b"{}"), (200, mcp_answer({"session_id": "cs_4", "status": "ready_for_payment"}))],
])
def test_a_failed_cancel_is_reported_left_open_and_the_run_is_not_ok(cancel_answers):
    fake = FakeGateway([(200, mcp_answer({"session_id": "cs_4", "status": "ready_for_payment"})),
                        (200, mcp_answer({"session_id": "cs_4", "status": "ready_for_payment"})), *cancel_answers])
    rows = []
    summary = job.run_all([checkout_case()], job.Transport(KEY, post=fake), RUN_ID, out=rows.append)
    assert rows[0]["pass"] is False and rows[0]["left_open"] == ["cs_4"] and rows[0]["cancelled"] == []
    assert rows[0]["cancel_note"]
    assert summary["ok"] is False and summary["left_open"] == ["cs_4"]


def test_a_create_that_fails_without_an_answer_is_an_unknown_left_open():
    fake = FakeGateway([TimeoutError("create timed out")])
    rows = []
    summary = job.run_all([checkout_case()], job.Transport(KEY, post=fake), RUN_ID, out=rows.append)
    assert rows[0]["pass"] is False and rows[0]["left_open"] == ["unknown:CO-1"]
    assert summary["ok"] is False and summary["left_open"] == ["unknown:CO-1"]
    assert len(fake.calls) == 1                                   # no retry on create


def test_a_designed_refusal_creates_nothing_and_passes_on_its_reason():
    refused = {"error": {"code": "NO_MERCHANT_OFFER", "detail": {"reason": "market_not_priced"}}}
    case = checkout_case(**{"is_error": True, "data.error.code": "NO_MERCHANT_OFFER",
                            "data.error.detail.reason": "market_not_priced"})
    fake = FakeGateway([(200, mcp_answer(refused, True))])
    row = job.run_case(case, job.Transport(KEY, post=fake), RUN_ID)
    assert row["pass"] is True and row["created"] == [] and row["left_open"] == [] and len(fake.calls) == 1


def test_a_storefront_checkout_is_never_cancel_called_and_stays_reported_left_open():
    fake = FakeGateway([(200, mcp_answer({"session_id": "esc_abc", "status": "completes_on_seller_storefront"})),
                        (200, mcp_answer({"session_id": "esc_abc", "status": "completes_on_seller_storefront"}))])
    rows = []
    summary = job.run_all([checkout_case(**{"data.status": "completes_on_seller_storefront"})],
                          job.Transport(KEY, post=fake), RUN_ID, out=rows.append)
    assert len(fake.calls) == 2 and rows[0]["left_open"] == ["esc_abc"] and "expires" in rows[0]["cancel_note"]
    assert summary["ok"] is False


# ------------------------------------------------------------------ the job's preconditions and output

def test_gate_rules():
    assert job.check_gate(gate(), NOW)["verdict"] == "GATE_OPEN"
    for raw, now, message in ((gate(observed=NOW - timedelta(seconds=181)), NOW, "stale"),
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


def run_main(tmp_path, capsys, env, answers, *, key=KEY):
    commit = tmp_path / "commit"
    commit.write_text(RELEASE + "\n")
    key_path = tmp_path / "key"
    key_path.write_text(key + "\n")
    fake = FakeGateway(answers)
    code = job.main(env, now=lambda: NOW, key_path=key_path, image_commit_path=commit,
                    transport_factory=lambda k: job.Transport(k, post=fake))
    out = capsys.readouterr().out
    rows, bad = job.parse_result_lines(out)
    assert bad == []
    return code, rows, out, fake


def test_main_runs_the_selected_cases_and_never_prints_the_key(reviewed_product, tmp_path, capsys):
    code, rows, out, fake = run_main(tmp_path, capsys, armed_env(), [
        (200, json.dumps(SERVED_ANSWER).encode()), (200, json.dumps(REFUSED_NOT_PRICED).encode())])
    assert code == 0 and len(fake.calls) == 2
    assert [r["kind"] for r in rows] == ["case", "case", "run"]
    assert rows[-1] == {"kind": "run", "ok": True, "cases": 2, "passed": 2, "calls": 2, "left_open": [],
                        "scope": job.JOB_NAME, "execution": "prod-test-transactions-abc12"}
    assert KEY not in out and KEY[3:20] not in base64.b64decode(out.split(job.PREFIX)[1].split()[0]).decode()
    for row in rows[:2]:
        assert KEY not in json.dumps(row)


def test_main_refuses_with_one_run_line_and_no_call(reviewed_product, tmp_path, capsys):
    code, rows, _, fake = run_main(tmp_path, capsys, armed_env(**{job.ENV + "GATE": None}), [])
    assert code == 3 and fake.calls == []
    assert rows[0]["kind"] == "run" and rows[0]["ok"] is False and "not GATE_OPEN" in rows[0]["refused"]
    code, rows, _, fake = run_main(tmp_path, capsys, armed_env(), [], key="not-a-key")
    assert code == 3 and fake.calls == [] and "key format" in rows[0]["refused"]
    assert "not-a-key" not in json.dumps(rows)


def test_main_exits_nonzero_when_a_case_fails(reviewed_product, tmp_path, capsys):
    code, rows, _, _ = run_main(tmp_path, capsys, armed_env(), [
        (200, json.dumps(SERVED_ANSWER).encode()), (200, json.dumps(SERVED_ANSWER).encode())])
    assert code == 1 and rows[-1]["ok"] is False and rows[-1]["passed"] == 1
