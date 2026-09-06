"""The Webflow order read-back and the offset-paged list.

Two things live here that nothing else in the suite can see, because everywhere
else the fetch layer is what a double stands in for:

* the ids that reach a URL PATH are allowlisted AND percent-encoded, and a value
  outside the allowlist never reaches the wire at all. Everything arriving at
  `fetch_webflow_order` is attacker-influenced — the order id comes out of a
  webhook body, and a signature (when there is one) proves the sender, not the
  shape of a field;
* the status mapping, which decides whether the receiver answers 503 (retry) or
  the sweep fails a lane alone.
"""

from __future__ import annotations

import json

import pytest

SITE_ID = "5f1a0000000000000000aaaa"
ORDER_ID = "0000-0001"


class _Response:
    def __init__(self, payload, status_code=200, headers=None, content=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        if content is not None:
            self.content = content
        elif isinstance(payload, Exception):
            self.content = b"<not json>"
        else:
            self.content = json.dumps(payload).encode()

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Client:
    def __init__(self, response=None):
        self.response = response or _Response({"orderId": ORDER_ID})
        self.calls = []

    async def get(self, url, headers=None, params=None):
        self.calls.append({"url": str(url), "params": dict(params or {}), "headers": dict(headers or {})})
        return self.response


# ---- the order fetch: path safety ------------------------------------------


async def test_the_fetch_url_carries_the_site_and_the_order():
    from services.webflow_order_fetch import fetch_webflow_order

    client = _Client()

    order = await fetch_webflow_order(
        api_token="tok", site_id=SITE_ID, order_id=ORDER_ID, client=client
    )

    assert order == {"orderId": ORDER_ID}
    assert client.calls[0]["url"] == (
        f"https://api.webflow.com/v2/sites/{SITE_ID}/orders/{ORDER_ID}"
    )
    assert client.calls[0]["headers"]["Authorization"] == "Bearer tok"


@pytest.mark.parametrize(
    "order_id",
    [
        # The one that matters: this walks out of the orders collection and makes
        # the fetch read a different endpoint entirely.
        "../../token/introspect",
        "..%2f..%2ftoken",
        "0000-0001/../../sites",
        "0000 0001",
        "0000-0001?expand=all",
        "0000-0001#frag",
        "",
        "x" * 65,
        "órder",
    ],
)
async def test_an_order_id_outside_the_allowlist_NEVER_REACHES_THE_WIRE(order_id):
    """Refused, not encoded-and-sent. A value outside this shape is not a Webflow
    order id, so the only thing a request built from it can do is reach somewhere
    it should not."""
    from services.webflow_order_fetch import WebflowOrderFetchError, fetch_webflow_order

    client = _Client()

    with pytest.raises(WebflowOrderFetchError):
        await fetch_webflow_order(
            api_token="tok", site_id=SITE_ID, order_id=order_id, client=client
        )

    assert client.calls == []


async def test_a_site_id_outside_the_allowlist_never_reaches_the_wire_either():
    """The site id comes out of the stored blob, which a reconnect or a hand edit
    can put anything into."""
    from services.webflow_order_fetch import WebflowOrderFetchError, fetch_webflow_order

    client = _Client()

    with pytest.raises(WebflowOrderFetchError):
        await fetch_webflow_order(
            api_token="tok",
            site_id="../../token/introspect",
            order_id=ORDER_ID,
            client=client,
        )

    assert client.calls == []


async def test_an_allowed_id_is_still_percent_encoded():
    """The second belt. Every character the pattern admits is URL-safe today, so
    this asserts the encoding is applied rather than that it changes anything —
    which is what keeps it applied if the pattern is ever widened."""
    from urllib.parse import quote

    from services.webflow_order_fetch import fetch_webflow_order

    client = _Client()
    await fetch_webflow_order(
        api_token="tok", site_id=SITE_ID, order_id="a_b-C9", client=client
    )

    assert client.calls[0]["url"].endswith("/orders/" + quote("a_b-C9", safe=""))


async def test_an_empty_token_is_refused_before_a_request():
    from services.webflow_order_fetch import WebflowOrderFetchError, fetch_webflow_order

    client = _Client()

    with pytest.raises(WebflowOrderFetchError):
        await fetch_webflow_order(
            api_token="", site_id=SITE_ID, order_id=ORDER_ID, client=client
        )

    assert client.calls == []


# ---- the order fetch: status mapping ---------------------------------------


@pytest.mark.parametrize(
    "status, expected",
    [
        (401, "WebflowOrderUnauthorizedError"),
        (403, "WebflowOrderUnauthorizedError"),
        # Its own type because it is the tell for a delivery naming an order that
        # belongs to a different site — the fetch is site-scoped, so it is simply
        # not there.
        (404, "WebflowOrderNotFoundError"),
        (429, "WebflowOrderFetchError"),
        (500, "WebflowOrderFetchError"),
        (502, "WebflowOrderFetchError"),
    ],
)
async def test_every_non_200_is_a_typed_fetch_error(status, expected):
    import services.webflow_order_fetch as fetch

    client = _Client(_Response({}, status_code=status))

    with pytest.raises(getattr(fetch, expected)):
        await fetch.fetch_webflow_order(
            api_token="tok", site_id=SITE_ID, order_id=ORDER_ID, client=client
        )


async def test_a_rate_limit_names_its_retry_after():
    """A 429 must not read as a broken credential; the operator needs the delay."""
    from services.webflow_order_fetch import WebflowOrderFetchError, fetch_webflow_order

    client = _Client(_Response({}, status_code=429, headers={"retry-after": "42"}))

    with pytest.raises(WebflowOrderFetchError) as excinfo:
        await fetch_webflow_order(
            api_token="tok", site_id=SITE_ID, order_id=ORDER_ID, client=client
        )

    assert "42" in str(excinfo.value)


async def test_an_oversized_response_is_refused_before_it_is_parsed():
    from services.webflow_order_fetch import WebflowOrderFetchError, fetch_webflow_order

    client = _Client(_Response({}, content=b"x" * 4_000_001))

    with pytest.raises(WebflowOrderFetchError):
        await fetch_webflow_order(
            api_token="tok", site_id=SITE_ID, order_id=ORDER_ID, client=client
        )


@pytest.mark.parametrize("payload", [ValueError("not json"), [1, 2, 3], "a string"])
async def test_a_response_that_is_not_an_object_is_a_fetch_error(payload):
    from services.webflow_order_fetch import WebflowOrderFetchError, fetch_webflow_order

    client = _Client(_Response(payload))

    with pytest.raises(WebflowOrderFetchError):
        await fetch_webflow_order(
            api_token="tok", site_id=SITE_ID, order_id=ORDER_ID, client=client
        )


# ---- the list --------------------------------------------------------------


async def test_the_list_sends_offset_limit_and_status():
    from services.webflow_order_fetch import fetch_webflow_order_page

    client = _Client(
        _Response({"orders": [{"orderId": "a"}], "pagination": {"total": 7}})
    )

    page = await fetch_webflow_order_page(
        api_token="tok", site_id=SITE_ID, offset=100, limit=50, status="refunded",
        client=client,
    )

    assert client.calls[0]["url"] == f"https://api.webflow.com/v2/sites/{SITE_ID}/orders"
    assert client.calls[0]["params"] == {"offset": 100, "limit": 50, "status": "refunded"}
    assert [o["orderId"] for o in page.orders] == ["a"]
    assert page.total == 7
    assert page.next_offset == 101


async def test_the_page_limit_is_clamped_to_webflows_maximum():
    """Webflow's documented maximum is 100. Sending more is a 400 that would fail
    every lane rather than one page."""
    from services.webflow_order_fetch import fetch_webflow_order_page

    client = _Client(_Response({"orders": []}))

    await fetch_webflow_order_page(
        api_token="tok", site_id=SITE_ID, limit=5000, client=client
    )

    assert client.calls[0]["params"]["limit"] == 100


async def test_a_negative_offset_is_clamped_rather_than_sent():
    from services.webflow_order_fetch import fetch_webflow_order_page

    client = _Client(_Response({"orders": []}))

    await fetch_webflow_order_page(
        api_token="tok", site_id=SITE_ID, offset=-5, client=client
    )

    assert client.calls[0]["params"]["offset"] == 0


async def test_no_status_filter_means_the_parameter_is_absent():
    """The unfiltered lane must not send `status=None` and have Webflow read it
    as a literal."""
    from services.webflow_order_fetch import fetch_webflow_order_page

    client = _Client(_Response({"orders": []}))

    await fetch_webflow_order_page(
        api_token="tok", site_id=SITE_ID, status=None, client=client
    )

    assert "status" not in client.calls[0]["params"]


async def test_items_is_accepted_as_a_second_collection_key():
    """A hedge: `orders` is documented, but reading neither would make every lane
    complete instantly on an empty page — a silent no-op rather than an error."""
    from services.webflow_order_fetch import fetch_webflow_order_page

    client = _Client(_Response({"items": [{"orderId": "a"}]}))

    page = await fetch_webflow_order_page(api_token="tok", site_id=SITE_ID, client=client)

    assert [o["orderId"] for o in page.orders] == ["a"]


async def test_a_list_response_with_no_recognised_collection_is_an_empty_page():
    from services.webflow_order_fetch import fetch_webflow_order_page

    client = _Client(_Response({"data": [{"orderId": "a"}]}))

    page = await fetch_webflow_order_page(api_token="tok", site_id=SITE_ID, client=client)

    assert page.orders == []
    assert page.next_offset == page.offset


async def test_the_list_refuses_a_bad_site_id_before_the_wire():
    from services.webflow_order_fetch import (
        WebflowOrderFetchError,
        fetch_webflow_order_page,
    )

    client = _Client()

    with pytest.raises(WebflowOrderFetchError):
        await fetch_webflow_order_page(
            api_token="tok", site_id="../../sites", client=client
        )

    assert client.calls == []
