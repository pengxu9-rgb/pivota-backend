"""The client supplies quote inputs and must never become a payment path.

Three things are under test, in order of how badly they fail:

1. **Configuration is the safety boundary.** A mistyped `REAP_API_BASE_URL` does not produce a
   failed call — it produces our API key delivered to whatever host the typo names. That is the
   product-URL SSRF class arriving through configuration instead of a payload.
2. **Nothing leaks.** The key never reaches a log, an error string or a URL; the response body is
   never echoed, because a partner's error payload can quote the request back and the request
   carries a buyer's shipping address.
3. **Attribution is observed, not assumed.** Reap said it does not survive checkout today. A
   caller that read "we sent it" as "it survived" would report a rail as earning when it earns
   nothing, which is the commercial question this whole integration turns on.
"""

import json

import pytest

from services import reap_quote_client as rq

ITEMS = [{"ucpItemId": "43062643884185", "quantity": 2}]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("REAP_API_BASE_URL", "REAP_API_KEY", "REAP_API_TIMEOUT_SECONDS"):
        monkeypatch.delenv(var, raising=False)


def _configured(monkeypatch, url="https://sandbox.reap.global/v1"):
    monkeypatch.setenv("REAP_API_BASE_URL", url)
    monkeypatch.setenv("REAP_API_KEY", "sk_live_SUPERSECRET")


# ---------------------------------------------------------------------------
# 1. Configuration is the safety boundary
# ---------------------------------------------------------------------------


def test_unconfigured_makes_no_request_and_says_so():
    assert rq.is_configured() is False


def test_half_configured_is_not_configured(monkeypatch):
    """A base URL with no key would produce a stream of 401s at a partner; a key with no base URL
    is a credential sitting in the environment for no reason. Neither is 'ready'."""
    monkeypatch.setenv("REAP_API_BASE_URL", "https://sandbox.reap.global/v1")
    assert rq.is_configured() is False
    monkeypatch.delenv("REAP_API_BASE_URL")
    monkeypatch.setenv("REAP_API_KEY", "sk_x")
    assert rq.is_configured() is False


@pytest.mark.parametrize("url", [
    "http://sandbox.reap.global/v1",          # not https
    "https://reap.global.evil.com/v1",        # suffix-looking, different host
    "https://notreap.global/v1",              # substring, not a suffix boundary
    "https://evil.com/v1",
    "https://localhost:8080",
    "https://169.254.169.254/",               # metadata
])
def test_a_base_url_that_is_not_reap_is_refused_before_the_key_is_attached(url):
    """The failure mode is not a failed call — it is the API key delivered to the typo's host."""
    with pytest.raises(rq.ReapConfigError):
        rq.validate_base_url(url)


@pytest.mark.parametrize("url", [
    # The four hosts Reap's published OpenAPI lists under `servers`, confirmed 8 Sep. Earlier
    # this list also carried an `api.eu.reap.so` case, which asserted that an allowlist entry I
    # had GUESSED was correct. No Reap host uses that domain. A test written from the same guess
    # as the code cannot catch the guess; these four came from the spec.
    "https://sandbox.api.reap.global/v1",
    "https://prod.api.reap.global/v1",
    "https://mx.sandbox.api.reap.global/v1",
    "https://mx.prod.api.reap.global/v1",
])
def test_real_reap_hosts_are_accepted(url):
    assert rq.validate_base_url(url) == url


@pytest.mark.parametrize("url", [
    "https://api.eu.reap.so/v2",
    "https://api.reapfin.com/v1",
])
def test_domains_that_are_not_reaps_are_refused(url):
    """Guards the correction itself. `reap.so` and `reapfin.com` were in the shipped allowlist and
    are not Reap's; a widened allowlist is not a harmless guess, it is a set of extra hosts this
    client would hand an API key to."""
    with pytest.raises(rq.ReapConfigError):
        rq.validate_base_url(url)


def test_the_host_check_reads_the_environment_each_call(monkeypatch):
    """Checked at request time, not at import: the value can be corrected without a deploy, and
    a check that ran once at startup would pass for a value that has since changed."""
    monkeypatch.setenv("REAP_API_BASE_URL", "https://sandbox.reap.global/v1")
    assert rq.validate_base_url().endswith("/v1")
    monkeypatch.setenv("REAP_API_BASE_URL", "https://evil.com")
    with pytest.raises(rq.ReapConfigError):
        rq.validate_base_url()


def test_a_config_error_never_names_the_key(monkeypatch):
    _configured(monkeypatch, url="https://evil.com/v1")
    with pytest.raises(rq.ReapConfigError) as caught:
        rq.validate_base_url()
    assert "SUPERSECRET" not in str(caught.value)


# ---------------------------------------------------------------------------
# 2. The request body
# ---------------------------------------------------------------------------


def test_the_body_is_the_shape_reap_named():
    body = rq.build_quote_request(merchant="fentybeauty.com", items=ITEMS)
    assert body["source"] == {"type": "CLIENT_SUPPLIED_UCP", "merchant": "fentybeauty.com"}
    assert body["items"] == [{"ucpItemId": "43062643884185", "quantity": 2}]
    assert "attribution" not in body, "absent attribution must not become an empty block"
    assert "shippingAddress" not in body


def test_an_item_we_cannot_name_is_refused():
    """A row whose variant identity we could not justify has no business reaching a quote —
    sending a product key would ask Reap to buy a thing that does not exist. This is the case the
    identity work spent the week separating out."""
    with pytest.raises(rq.ReapQuoteError):
        rq.build_quote_request(merchant="m", items=[{"quantity": 1}])
    with pytest.raises(rq.ReapQuoteError):
        rq.build_quote_request(merchant="m", items=[])


def test_quantity_absent_is_one_and_zero_is_refused():
    """Absent and present-but-zero are different facts. A caller that computed 0 meant something,
    and it was not 'one'."""
    assert rq.build_items([{"ucpItemId": "1"}])[0]["quantity"] == 1
    for bad in (0, -1, "x"):
        with pytest.raises(rq.ReapQuoteError):
            rq.build_items([{"ucpItemId": "1", "quantity": bad}])


def test_a_quote_needs_a_merchant():
    with pytest.raises(rq.ReapQuoteError):
        rq.build_quote_request(merchant="   ", items=ITEMS)


def test_only_the_five_named_attribution_fields_are_sent():
    """A caller cannot smuggle buyer data to a third party by adding keys to a dict."""
    block = rq.build_attribution({
        "ref": "pivota", "click_id": "clk_1", "campaign_source": "agent",
        "campaign_medium": "", "buyer_email": "someone@example.com", "note": "x",
    })
    assert block == {"ref": "pivota", "click_id": "clk_1", "campaign_source": "agent"}
    assert "buyer_email" not in block and "note" not in block


def test_the_idempotency_key_is_stable_in_the_request(monkeypatch):
    """The dangerous retry is the one where we never saw the response. A fresh key there would
    ask Reap for a SECOND quote for the same cart."""
    a = rq.build_quote_request(merchant="m", items=ITEMS)
    b = rq.build_quote_request(merchant="m", items=ITEMS)
    assert rq.idempotency_key(a) == rq.idempotency_key(b)
    c = rq.build_quote_request(merchant="m", items=[{"ucpItemId": "999", "quantity": 1}])
    assert rq.idempotency_key(c) != rq.idempotency_key(a)


def test_the_key_travels_in_a_header_not_the_url(monkeypatch):
    body = rq.build_quote_request(merchant="m", items=ITEMS)
    headers = rq._headers("sk_live_SUPERSECRET", body)
    assert headers["Authorization"] == "Bearer sk_live_SUPERSECRET"
    assert "Idempotency-Key" in headers
    assert not any("SUPERSECRET" in k for k in headers)


# ---------------------------------------------------------------------------
# 3. Attribution is observed, never assumed
# ---------------------------------------------------------------------------


def test_attribution_sent_but_not_returned_reads_as_not_echoed():
    """Reap's stated current behaviour. Reading 'we sent it' as 'it survived' would report a rail
    as earning when it earns nothing."""
    result = rq._parse({"quote": {"id": "q_1"}}, 200, sent_attribution=True)
    assert result.ok is True and result.quote_id == "q_1"
    assert result.attribution_echoed is False


def test_attribution_returned_is_echoed():
    """The positive counterpart — this is the signal that Reap has shipped pass-through."""
    result = rq._parse(
        {"quote": {"id": "q_1", "attribution": {"ref": "pivota"}}}, 200, sent_attribution=True)
    assert result.attribution_echoed is True


def test_not_sending_attribution_never_reads_as_echoed():
    result = rq._parse({"quote": {"id": "q", "attribution": {"ref": "x"}}}, 200,
                       sent_attribution=False)
    assert result.attribution_echoed is False


# ---------------------------------------------------------------------------
# 4. Failures degrade, and leak nothing
# ---------------------------------------------------------------------------


async def test_an_unconfigured_client_returns_rather_than_calling(monkeypatch):
    called = {"n": 0}

    class _Boom:
        def __init__(self, *a, **k):
            called["n"] += 1
            raise AssertionError("must not construct a client when unconfigured")

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Boom)
    res = await rq.request_quote(merchant="m", items=ITEMS)
    assert res.ok is False and res.error == "reap_client_not_configured"
    assert called["n"] == 0


async def test_a_misconfigured_host_raises_rather_than_degrading(monkeypatch):
    """An operator error must be visible. Degrading it into 'Reap is unavailable' on every
    request hides a fixable mistake behind a plausible one."""
    _configured(monkeypatch, url="https://evil.com/v1")
    with pytest.raises(rq.ReapConfigError):
        await rq.request_quote(merchant="m", items=ITEMS)


async def test_a_transport_failure_degrades_and_names_no_detail(monkeypatch, caplog):
    import logging

    import httpx
    _configured(monkeypatch)

    class _Failing:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): raise httpx.ConnectError("dial tcp 1.2.3.4:443 refused")

    monkeypatch.setattr(httpx, "AsyncClient", _Failing)
    with caplog.at_level(logging.WARNING):
        res = await rq.request_quote(
            merchant="m", items=ITEMS,
            shipping_address={"line1": "12 Privacy Road", "postal_code": "SW1"})
    assert res.ok is False and res.error.startswith("transport_error:")
    assert "SUPERSECRET" not in caplog.text
    assert "Privacy Road" not in caplog.text, "an exception string must not carry the address"


async def test_an_error_status_does_not_echo_the_response_body(monkeypatch, caplog):
    """A partner's error payload can quote the request back, and the request carries a buyer's
    shipping address."""
    import logging

    import httpx
    _configured(monkeypatch)

    class _Rejecting:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k):
            return httpx.Response(
                422, json={"error": "bad", "echo": {"shippingAddress": {"line1": "12 Privacy Road"}}},
                request=httpx.Request("POST", "https://sandbox.reap.global/v1/quotes"))

    monkeypatch.setattr(httpx, "AsyncClient", _Rejecting)
    with caplog.at_level(logging.WARNING):
        res = await rq.request_quote(merchant="m", items=ITEMS)
    assert res.ok is False and res.status == 422 and res.error == "reap_status_422"
    assert res.raw == {}, "the response body must not be carried out of the client"
    assert "Privacy Road" not in caplog.text
    assert "Privacy Road" not in json.dumps(res.__dict__, default=str)


async def test_a_successful_quote_carries_the_id_and_the_checkout_url(monkeypatch):
    import httpx
    _configured(monkeypatch)

    class _Ok:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None):
            assert url.endswith("/quotes")
            assert json["source"]["type"] == "CLIENT_SUPPLIED_UCP"
            assert headers["Idempotency-Key"].startswith("pivota-quote-")
            return httpx.Response(
                200, json={"quote": {"id": "q_9", "checkoutUrl": "https://pay.example/q_9"}},
                request=httpx.Request("POST", "https://sandbox.reap.global/v1/quotes"))

    monkeypatch.setattr(httpx, "AsyncClient", _Ok)
    res = await rq.request_quote(merchant="fentybeauty.com", items=ITEMS,
                                 attribution={"ref": "pivota", "click_id": "clk_1"})
    assert res.ok is True and res.quote_id == "q_9"
    assert res.checkout_url == "https://pay.example/q_9"
    assert res.attribution_echoed is False


def test_the_only_endpoint_this_module_calls_is_quotes():
    """A guard on the constraint, not on an implementation detail: Pivota never deposits,
    prefunds, custodies or is liable for a balance, so this module may ask for a QUOTE and
    nothing else. If a future change adds a capture, a card or a funding call, it fails here.

    Read from the AST, not by grepping the source: a first version matched plain text and
    tripped on its own docstring, which explains at length what the module must not do. A
    ratchet that cannot tell code from prose is the same text-vs-semantics mistake this
    codebase keeps paying for.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(rq))
    literals = {
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    # every path-shaped literal the module could POST to
    # len > 1 so the bare "/" from `rstrip("/")` is not mistaken for an endpoint
    paths = {v for v in literals if v.startswith("/") and len(v) > 1 and " " not in v}
    assert paths == {"/quotes"}, f"this module addresses more than /quotes: {sorted(paths)}"

    # and the f-string that builds the URL joins the base to exactly that path
    joined = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr)
        and any(isinstance(v, ast.Constant) and v.value == "/quotes" for v in node.values)
    ]
    assert joined, "the request URL is no longer built from /quotes"
