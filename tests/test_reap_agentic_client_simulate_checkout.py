"""The sandbox-only `X-Simulate-Checkout: COMPLETED` header on `POST /agentic/checkouts`.

What this file has to prove is mostly NEGATIVE: the header never reaches a production host, never
rides on any other request, and never changes an idempotency key while the dial is unset. An
absence assertion passes just as well when the mechanism is absent, so every wire-level absence
test here runs through the SAME fixture as a presence assertion on the checkout path -- in the
same test where it matters -- so the absences are known to be measured against a live mechanism.

Nothing here reaches the network: `httpx.AsyncClient` is replaced by a recorder in every test.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from services import reap_agentic_client as rc

SANDBOX = "https://sandbox.api.reap.global"
HEADER = "X-Simulate-Checkout"
DIAL = "REAP_AGENTIC_SIMULATE_CHECKOUT"

ENROLLMENT_UUID = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
RETURN_URL = "https://agent.pivota.cc/reap/return?click=abc123"
QUOTE_ID = "f1e2d3c4"

HOST_REFUSAL = "simulate dial set but base is not the Reap sandbox; header withheld"
DIAL_REFUSAL = "value not recognised"


def _run(coro):
    return asyncio.run(coro)


# --- transport ------------------------------------------------------------------------------


class _Response:
    status_code = 200
    headers: dict = {}

    async def aiter_bytes(self, chunk_size=None):
        yield b"{}"


class _Stream:
    async def __aenter__(self):
        return _Response()

    async def __aexit__(self, *a):
        return False


class _Recorder:
    calls: list = []

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url, json=None, params=None, headers=None):
        _Recorder.calls.append({"method": method, "url": url, "headers": dict(headers or {})})
        return _Stream()


@pytest.fixture
def wire(monkeypatch):
    import httpx

    _Recorder.calls = []
    monkeypatch.setenv("REAP_API_BASE_URL", SANDBOX)
    monkeypatch.setenv("REAP_API_KEY", "sk_test_key")
    monkeypatch.delenv("REAP_API_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("REAP_RETURN_URL_HOSTS", raising=False)
    monkeypatch.delenv(DIAL, raising=False)
    monkeypatch.setattr(httpx, "AsyncClient", _Recorder)
    return _Recorder


@pytest.fixture
def ops_log(caplog):
    """The `pivota` logger does not propagate, so caplog's root handler never sees it. Attach the
    handler to THAT logger: the assertion is that the warning goes through `utils.logger.logger`,
    not through whichever logger happens to propagate."""
    lg = logging.getLogger("pivota")
    lg.addHandler(caplog.handler)
    caplog.set_level(logging.INFO, logger="pivota")
    try:
        yield caplog
    finally:
        lg.removeHandler(caplog.handler)


def _pivota_warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.name == "pivota" and r.levelno == logging.WARNING]


def _checkout():
    return _run(rc.create_checkout(quote_id=QUOTE_ID, enrollment_id=ENROLLMENT_UUID,
                                   return_url=RETURN_URL))


# --- the pure function: accepting -----------------------------------------------------------


@pytest.mark.parametrize("base,dial", [
    ("https://sandbox.api.reap.global", "COMPLETED"),
    ("https://mx.sandbox.api.reap.global", "COMPLETED"),
    # Hostnames are case-insensitive; `.strip()` removes surrounding whitespace only.
    ("https://SANDBOX.API.REAP.GLOBAL/", " COMPLETED\n"),
])
def test_the_sandbox_hosts_with_the_exact_dial_get_the_header(base, dial, ops_log):
    assert rc.simulate_checkout_header(base, dial) == {HEADER: "COMPLETED"}
    assert _pivota_warnings(ops_log) == []


# --- the pure function: refusing on the host ------------------------------------------------


@pytest.mark.parametrize("base", [
    "https://prod.api.reap.global",                          # production
    "https://api.reap.global",
    "https://mx.prod.api.reap.global",
    "https://sandbox.api.reap.global.evil.example",          # suffix attack
    "https://evil.example/sandbox.api.reap.global",          # host in the path
    "https://sandbox-api.reap.global",                       # near miss
    # Pass `validate_base_url` (they are under reap.global) and END WITH the sandbox host, so only
    # an EXACT comparison refuses them. These are what kill an `endswith(...)` mutant.
    "https://attacker.sandbox.api.reap.global",
    "https://notsandbox.api.reap.global",
    "http://sandbox.api.reap.global",                        # not https: validate_base_url refuses
])
def test_a_non_sandbox_base_withholds_the_header_and_says_so(base, ops_log):
    assert rc.simulate_checkout_header(base, "COMPLETED") is None
    assert HOST_REFUSAL in _pivota_warnings(ops_log)


def test_userinfo_in_the_base_withholds_the_header_without_raising_or_logging_it(ops_log):
    """`validate_base_url` raises on userinfo; this function must turn that into None, and the
    warning must not carry the userinfo (it is a credential)."""
    assert rc.simulate_checkout_header("https://u:p@sandbox.api.reap.global", "COMPLETED") is None
    assert HOST_REFUSAL in _pivota_warnings(ops_log)
    assert "u:p" not in ops_log.text


@pytest.mark.parametrize("base", [None, "", "   "])
def test_no_base_withholds_the_header_and_does_not_fall_back_to_the_environment(
        base, monkeypatch, ops_log):
    """`validate_base_url(None)` reads REAP_API_BASE_URL. With the env pointing at the sandbox, a
    function that fell through to it would say yes to a base it was never handed."""
    monkeypatch.setenv("REAP_API_BASE_URL", SANDBOX)
    assert rc.simulate_checkout_header(base, "COMPLETED") is None
    assert HOST_REFUSAL in _pivota_warnings(ops_log)


# --- the pure function: refusing on the dial ------------------------------------------------


@pytest.mark.parametrize("dial", [
    "completed", "Completed", "1", "true", "yes", "on",
    "COMP LETED",            # inner space: `.strip()` does not remove it
    "COMPLETED\x00",         # a trailing byte `.strip()` does not remove
    "COMPLETEDX", "FAILED",
])
def test_any_dial_value_but_the_exact_string_is_ignored_not_coerced(dial, ops_log):
    assert rc.simulate_checkout_header(SANDBOX, dial) is None
    warnings = _pivota_warnings(ops_log)
    assert any(DIAL + " " + DIAL_REFUSAL in w for w in warnings), warnings


@pytest.mark.parametrize("dial", [None, "", "  \n"])
def test_an_unset_dial_is_silent(dial, ops_log):
    assert rc.simulate_checkout_header(SANDBOX, dial) is None
    assert _pivota_warnings(ops_log) == []


def test_the_unrecognised_value_is_not_echoed_beyond_32_chars(ops_log):
    value = "A" * 32 + "B" * 200
    assert rc.simulate_checkout_header(SANDBOX, value) is None
    assert "A" * 32 in ops_log.text
    assert "B" not in ops_log.text


# --- the wire: the header rides on POST /agentic/checkouts and nothing else ------------------


def test_the_header_is_on_the_checkout_and_on_no_other_request(wire, monkeypatch):
    """ONE fixture, ONE dial setting, every request type. The presence assertion on the checkout
    is what makes the absence assertions on the others mean something."""
    monkeypatch.setenv(DIAL, "COMPLETED")
    _checkout()
    _run(rc.request_quote(items=[{"variantId": "var_x", "quantity": 1}], email="b@example.com"))
    _run(rc.create_enrollment(owner_id="cust_42", return_url=RETURN_URL, attempt_id="att-1",
                         email="buyer@example.com"))
    _run(rc.search_products(query="Fenty Eau de Parfum"))
    _run(rc.product_details(["prd_1"]))
    _run(rc.select_shipping_option(quote_id=QUOTE_ID, shipping_option_id="ship_std"))
    _run(rc.get_checkout("chk_7f3a"))
    _run(rc.get_quote(QUOTE_ID))

    by_path = [(c["method"], c["url"].split("reap.global", 1)[1], c["headers"]) for c in wire.calls]
    assert by_path[0][:2] == ("POST", "/agentic/checkouts")
    assert by_path[0][2].get(HEADER) == "COMPLETED"
    others = by_path[1:]
    assert [p for _, p, _ in others] == [
        "/agentic/quotes",
        "/agentic/enrollments",
        "/agentic/products/search",
        "/agentic/products/details",
        f"/agentic/quotes/{QUOTE_ID}/shipping-option",
        "/agentic/checkouts/chk_7f3a",
        f"/agentic/quotes/{QUOTE_ID}",
    ]
    for method, path, headers in others:
        assert not any(k.lower() == HEADER.lower() for k in headers), (method, path)


@pytest.mark.parametrize("call", [
    lambda: rc.request_quote(items=[{"variantId": "var_x", "quantity": 1}], email="b@example.com"),
    lambda: rc.create_enrollment(owner_id="cust_42", return_url=RETURN_URL, attempt_id="att-1",
                         email="buyer@example.com"),
    lambda: rc.search_products(query="x"),
    lambda: rc.select_shipping_option(quote_id=QUOTE_ID, shipping_option_id="ship_std"),
], ids=["quote", "enrollment", "products_search", "shipping_option"])
def test_each_other_post_lacks_the_header_while_the_checkout_in_the_same_run_has_it(
        wire, monkeypatch, call):
    monkeypatch.setenv(DIAL, "COMPLETED")
    _run(call())
    _checkout()
    assert HEADER not in wire.calls[0]["headers"]
    assert wire.calls[1]["headers"].get(HEADER) == "COMPLETED"


@pytest.mark.parametrize("base", [
    "https://prod.api.reap.global",
    "https://mx.prod.api.reap.global",
    "https://attacker.sandbox.api.reap.global",
])
def test_a_production_base_never_gets_the_header_on_the_wire(wire, monkeypatch, base):
    """These bases pass `validate_base_url`, so the request IS sent -- to the recorder -- and the
    header is withheld on it. The control: the same dial on the sandbox base does send it."""
    monkeypatch.setenv(DIAL, "COMPLETED")
    monkeypatch.setenv("REAP_API_BASE_URL", base)
    _checkout()
    assert wire.calls[0]["url"].startswith(base)
    assert HEADER not in wire.calls[0]["headers"]
    monkeypatch.setenv("REAP_API_BASE_URL", SANDBOX)
    _checkout()
    assert wire.calls[1]["headers"].get(HEADER) == "COMPLETED"


def test_the_dial_is_read_on_every_call_not_at_import(wire, monkeypatch):
    _checkout()
    monkeypatch.setenv(DIAL, "COMPLETED")
    _checkout()
    monkeypatch.delenv(DIAL)
    _checkout()
    assert [c["headers"].get(HEADER) for c in wire.calls] == [None, "COMPLETED", None]


def test_an_extra_header_cannot_replace_one_this_module_sets():
    with pytest.raises(rc.ReapRequestError):
        rc._headers("k", "/agentic/checkouts", {}, extra_headers={"authorization": "Basic x"})


# --- idempotency ------------------------------------------------------------------------------


#: MEASURED ON main AT f2fbc7bca, before this change, by
#: `rc._headers("k", "/agentic/checkouts", build_checkout_request(QUOTE_ID, ENROLLMENT_UUID,
#: RETURN_URL))["Idempotency-Key"]`. A literal rather than a value recomputed in the test: a
#: recomputation runs the CHANGED module and would agree with itself whatever it now does.
PRE_CHANGE_CHECKOUT_KEY = "pivota-checkouts-f9eeb262eb164ba882b7038a7a5f6689"


def test_with_the_dial_unset_the_checkout_key_is_byte_identical_to_before(wire, monkeypatch):
    """0-diff invariant: every checkout opened in production keeps its key."""
    monkeypatch.setattr(rc.time, "time", lambda: 1_000_000.0)
    _checkout()
    assert HEADER not in wire.calls[0]["headers"]
    assert wire.calls[0]["headers"]["Idempotency-Key"] == PRE_CHANGE_CHECKOUT_KEY


def test_with_an_unrecognised_dial_the_checkout_key_is_also_unchanged(wire, monkeypatch):
    monkeypatch.setenv(DIAL, "completed")
    _checkout()
    assert wire.calls[0]["headers"]["Idempotency-Key"] == PRE_CHANGE_CHECKOUT_KEY


def test_a_simulated_checkout_is_a_different_request_from_an_unsimulated_one(wire, monkeypatch):
    """Same quote, same enrollment. With the header the key must differ, or a replay after the
    dial was toggled would reuse the other checkout (or hit IDEMPOTENT_PARAMETER_MISMATCH).
    And it must still be STABLE with the header on: a retry is still a retry."""
    _checkout()
    monkeypatch.setenv(DIAL, "COMPLETED")
    _checkout()
    _checkout()
    off, on_1, on_2 = (c["headers"]["Idempotency-Key"] for c in wire.calls)
    assert wire.calls[1]["headers"].get(HEADER) == "COMPLETED"
    assert off == PRE_CHANGE_CHECKOUT_KEY
    assert on_1 != off
    assert on_1 == on_2
    assert on_1 == rc.idempotency_key(
        "checkouts",
        {"quoteId": QUOTE_ID, "enrollmentId": ENROLLMENT_UUID, "simulate": "COMPLETED"},
        bucket_seconds=None,
    )
