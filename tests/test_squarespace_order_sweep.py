"""The Squarespace reconciliation sweep: pagination, the cursor, and the cap.

For an API-key store this sweep is the ONLY telemetry path, so its cursor
arithmetic is not a convenience — an over-eager advance loses orders outright.
The HTTP client is a fake that answers documented page envelopes; everything
above it (window construction, paging, testmode handling, cursor advance, and
the credential-blob persistence) is production code.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

MERCHANT_ID = "merchant-sq"
STORE_ID = "store-sq"
NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.replace(tzinfo=None).isoformat(timespec="milliseconds") + "Z"


def _order(order_id: str, modified: datetime, **overrides):
    order = {
        "id": order_id,
        "orderNumber": order_id,
        "createdOn": _iso(modified - timedelta(hours=1)),
        "modifiedOn": _iso(modified),
        "testmode": False,
        "fulfillmentStatus": "PENDING",
        "grandTotal": {"value": "40.00", "currency": "USD"},
    }
    order.update(overrides)
    return order


class _FakeClient:
    """Answers `fetch_squarespace_order_page` with scripted pages.

    It also answers the SITE LOOKUP the sweep now makes once per run before it
    lists anything (`GET /1.0/authorization/website`). That call is routed by
    URL rather than consuming a scripted page, so the page script still reads
    as the sequence of order pages a run walks. `website_id` is what the
    lookup reports; `unauthorized_tokens` makes named credentials 401 there, so
    the OAuth-expiry fallback can be exercised without a second fake.

    `list_requests` excludes the site lookup: the bounds and cursor assertions
    are about the ORDER LIST calls, and folding an extra request into that
    sequence would silently shift every index.
    """

    def __init__(self, pages, *, website_id="site-1", unauthorized_tokens=()):
        self._pages = list(pages)
        self.requests = []
        self.website_id = website_id
        self.unauthorized_tokens = set(unauthorized_tokens)
        self.website_lookups = []

    async def get(self, url, headers=None, params=None):
        if "/authorization/website" in str(url):
            token = str((headers or {}).get("Authorization", "")).removeprefix("Bearer ").strip()
            self.website_lookups.append(token)
            if token in self.unauthorized_tokens:
                return _FakeResponse({"type": "AUTHORIZATION"}, status_code=401)
            return _FakeResponse({"id": self.website_id})
        self.requests.append({"url": url, "params": dict(params or {})})
        page = self._pages.pop(0) if self._pages else {"result": [], "pagination": {}}
        return _FakeResponse(page)

    @property
    def list_requests(self):
        return self.requests

    async def aclose(self):
        return None


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.content = b"{}"
        self.status_code = status_code

    def json(self):
        return self._payload


def _page(orders, next_cursor=None):
    pagination = {"hasNextPage": bool(next_cursor)}
    if next_cursor:
        pagination["nextPageCursor"] = next_cursor
    return {"result": orders, "pagination": pagination}


class _Recorder:
    def __init__(self, status="recorded", accepted=2):
        self.calls = []
        self._status = status
        self._accepted = accepted

    async def __call__(self, **kwargs):
        from services.squarespace_ledger import SquarespaceIngestResult

        self.calls.append(kwargs)
        return SquarespaceIngestResult(
            status=self._status, accepted=self._accepted, duplicates=0
        )


def _wire(monkeypatch, *, credentials, recorder=None, store=True):
    """Patch the sweep's three collaborators: the store row, the ledger write,
    and the credential-blob persistence.

    The persisted blob is STATEFUL across runs within one test: what a merge
    writes is what the next `find` reads back. That is what makes a multi-run
    claim — "repeated runs against an always-truncating store still make
    progress" — a claim about the real cursor arithmetic rather than about the
    same first run repeated. `credentials` itself is copied, never mutated, so
    the module-level fixture cannot leak between tests.
    """
    import json

    from services import squarespace_order_sweep as sweep

    persisted = {}
    live = dict(credentials)

    async def fake_find(store_id):
        if not store:
            return None
        return {
            "store_id": STORE_ID,
            "merchant_id": MERCHANT_ID,
            "domain": "shop.example",
            "api_key": json.dumps(live),
        }

    async def fake_merge(*, store_id, updates=None, **kwargs):
        persisted.update(updates or {})
        live.update(updates or {})
        return dict(live)

    recorder = recorder or _Recorder()
    monkeypatch.setattr(sweep, "find_squarespace_store", fake_find)
    monkeypatch.setattr(sweep, "merge_squarespace_credentials", fake_merge)
    monkeypatch.setattr(sweep, "record_squarespace_order", recorder)
    return persisted, recorder


_CREDENTIALS = {"api_key": "sq-api-key", "website_id": "site-1"}


# ---- the window ------------------------------------------------------------


async def test_the_first_page_carries_the_window_and_later_pages_carry_the_cursor(
    monkeypatch,
):
    """`cursor` and the modified bounds are mutually exclusive: sending both
    would be rejected, and sending the bounds again with a cursor would restart
    the walk from page one forever."""
    from services.squarespace_order_sweep import sweep_squarespace_store

    _wire(monkeypatch, credentials=_CREDENTIALS)
    client = _FakeClient(
        [
            _page([_order("a", NOW - timedelta(hours=2))], next_cursor="cur-2"),
            _page([_order("b", NOW - timedelta(hours=1))]),
        ]
    )

    result = await sweep_squarespace_store(
        store_id=STORE_ID, now=NOW, client=client, overlap_minutes=30
    )

    assert result["pages"] == 2
    assert result["seen"] == 2
    first, second = client.requests
    assert set(first["params"]) == {"modifiedAfter", "modifiedBefore"}
    assert second["params"] == {"cursor": "cur-2"}


async def test_a_store_with_a_cursor_starts_the_window_one_overlap_earlier(
    monkeypatch,
):
    """The overlap is what keeps an order modified in the same instant the last
    run ended from falling between two windows. Re-reading is free: the event
    ids are deterministic and the ledger dedupes."""
    from services.squarespace_order_sweep import sweep_squarespace_store

    cursor = NOW - timedelta(hours=4)
    _wire(
        monkeypatch,
        credentials={
            **_CREDENTIALS,
            "reconciliation": {"orders_cursor": _iso(cursor)},
        },
    )
    client = _FakeClient([_page([])])

    await sweep_squarespace_store(
        store_id=STORE_ID, now=NOW, client=client, overlap_minutes=30
    )

    assert client.requests[0]["params"]["modifiedAfter"] == _iso(
        cursor - timedelta(minutes=30)
    )
    assert client.requests[0]["params"]["modifiedBefore"] == _iso(NOW)


async def test_a_store_with_no_cursor_uses_the_initial_lookback(monkeypatch):
    from services.squarespace_order_sweep import sweep_squarespace_store

    _wire(monkeypatch, credentials=_CREDENTIALS)
    client = _FakeClient([_page([])])

    await sweep_squarespace_store(
        store_id=STORE_ID,
        now=NOW,
        client=client,
        overlap_minutes=0,
        initial_lookback_days=3,
    )

    assert client.requests[0]["params"]["modifiedAfter"] == _iso(
        NOW - timedelta(days=3)
    )


async def test_a_stale_cursor_is_clamped_to_the_maximum_lookback(monkeypatch):
    """A store dark for a year must not ask for a year of history in one run."""
    from services.squarespace_order_sweep import MAX_LOOKBACK_DAYS, sweep_squarespace_store

    _wire(
        monkeypatch,
        credentials={
            **_CREDENTIALS,
            "reconciliation": {"orders_cursor": _iso(NOW - timedelta(days=400))},
        },
    )
    client = _FakeClient([_page([])])

    await sweep_squarespace_store(store_id=STORE_ID, now=NOW, client=client)

    assert client.requests[0]["params"]["modifiedAfter"] == _iso(
        NOW - timedelta(days=MAX_LOOKBACK_DAYS)
    )


async def test_a_cursor_from_the_future_does_not_invert_the_window(monkeypatch):
    from services.squarespace_order_sweep import sweep_squarespace_store

    _wire(
        monkeypatch,
        credentials={
            **_CREDENTIALS,
            "reconciliation": {"orders_cursor": _iso(NOW + timedelta(days=2))},
        },
    )
    client = _FakeClient([_page([])])

    result = await sweep_squarespace_store(store_id=STORE_ID, now=NOW, client=client)

    assert result["window_start"] < result["window_end"]


# ---- the cursor ------------------------------------------------------------


async def test_the_cursor_advances_to_the_highest_modified_on_seen(monkeypatch):
    from services.squarespace_order_sweep import sweep_squarespace_store

    persisted, _ = _wire(monkeypatch, credentials=_CREDENTIALS)
    newest = NOW - timedelta(minutes=5)
    client = _FakeClient(
        [_page([_order("a", NOW - timedelta(hours=3)), _order("b", newest)])]
    )

    result = await sweep_squarespace_store(store_id=STORE_ID, now=NOW, client=client)

    assert result["cursor_after"] == _iso(newest)
    assert persisted["reconciliation"]["orders_cursor"] == _iso(newest)


async def test_the_cursor_never_moves_backwards(monkeypatch):
    """A window that saw only orders OLDER than the cursor (a back-dated edit,
    or clock skew) must not re-open a window that was already closed."""
    from services.squarespace_order_sweep import sweep_squarespace_store

    cursor = NOW - timedelta(hours=1)
    persisted, _ = _wire(
        monkeypatch,
        credentials={
            **_CREDENTIALS,
            "reconciliation": {"orders_cursor": _iso(cursor)},
        },
    )
    client = _FakeClient([_page([_order("a", NOW - timedelta(days=3))])])

    result = await sweep_squarespace_store(store_id=STORE_ID, now=NOW, client=client)

    assert result["cursor_after"] == _iso(cursor)
    assert persisted["reconciliation"]["orders_cursor"] == _iso(cursor)


async def test_an_empty_but_complete_window_advances_the_cursor_to_its_end(
    monkeypatch,
):
    """Nothing modified in a window that was fully read means nothing was
    missed in it. Leaving the cursor put would make the window grow without
    bound over a quiet period."""
    from services.squarespace_order_sweep import sweep_squarespace_store

    _wire(monkeypatch, credentials=_CREDENTIALS)
    client = _FakeClient([_page([])])

    result = await sweep_squarespace_store(store_id=STORE_ID, now=NOW, client=client)

    assert result["cursor_after"] == _iso(NOW)


async def test_a_truncated_run_does_not_advance_the_cursor(monkeypatch):
    """The page cap is the one case where advancing loses orders.

    Squarespace documents no ordering for the orders list, so a run stopped
    early may have left behind orders whose `modifiedOn` is below the maximum
    it saw. Moving the cursor past them would skip them for good, so a
    truncated run reports itself and re-reads the same window next time.
    """
    from services.squarespace_order_sweep import sweep_squarespace_store

    persisted, _ = _wire(monkeypatch, credentials=_CREDENTIALS)
    client = _FakeClient(
        [
            _page([_order("a", NOW - timedelta(hours=3))], next_cursor="cur-2"),
            _page([_order("b", NOW - timedelta(hours=2))], next_cursor="cur-3"),
        ]
    )

    result = await sweep_squarespace_store(
        store_id=STORE_ID, now=NOW, client=client, max_pages=2
    )

    assert result["truncated"] is True
    assert result["pages"] == 2
    assert result["cursor_after"] is None
    assert persisted["reconciliation"]["orders_cursor"] is None


async def test_a_dry_run_writes_nothing_and_moves_no_cursor(monkeypatch):
    from services.squarespace_order_sweep import sweep_squarespace_store

    persisted, recorder = _wire(monkeypatch, credentials=_CREDENTIALS)
    client = _FakeClient([_page([_order("a", NOW - timedelta(hours=1))])])

    result = await sweep_squarespace_store(
        store_id=STORE_ID, now=NOW, client=client, apply=False
    )

    assert result["dry_run"] is True
    assert result["seen"] == 1
    assert result["accepted"] == 0
    assert recorder.calls == []
    assert persisted == {}


# ---- what the sweep does with each order -----------------------------------


async def test_every_order_is_recorded_under_the_reconciliation_write_path(
    monkeypatch,
):
    from services.squarespace_order_sweep import sweep_squarespace_store

    _, recorder = _wire(monkeypatch, credentials=_CREDENTIALS)
    client = _FakeClient([_page([_order("a", NOW - timedelta(hours=1))])])

    result = await sweep_squarespace_store(store_id=STORE_ID, now=NOW, client=client)

    assert result["accepted"] == 2
    assert recorder.calls[0]["from_webhook"] is False
    assert recorder.calls[0]["merchant_id"] == MERCHANT_ID
    assert recorder.calls[0]["store_id"] == STORE_ID


async def test_a_testmode_order_is_counted_and_never_recorded(monkeypatch):
    from services.squarespace_order_sweep import sweep_squarespace_store

    _, recorder = _wire(monkeypatch, credentials=_CREDENTIALS)
    client = _FakeClient(
        [
            _page(
                [
                    _order("real", NOW - timedelta(hours=2)),
                    _order("test", NOW - timedelta(hours=1), testmode=True),
                ]
            )
        ]
    )

    result = await sweep_squarespace_store(store_id=STORE_ID, now=NOW, client=client)

    assert result["seen"] == 2
    assert result["testmode_skipped"] == 1
    assert [call["order"]["id"] for call in recorder.calls] == ["real"]
    # ...but it still counts toward the cursor: it was modified in this window,
    # and skipping it must not make the window re-open for it forever.
    assert result["cursor_after"] == _iso(NOW - timedelta(hours=1))


async def test_an_order_with_no_id_is_counted_invalid_and_skipped(monkeypatch):
    from services.squarespace_order_sweep import sweep_squarespace_store

    _, recorder = _wire(monkeypatch, credentials=_CREDENTIALS)
    bad = _order("x", NOW - timedelta(hours=1))
    bad["id"] = ""
    client = _FakeClient([_page([bad, _order("good", NOW - timedelta(hours=1))])])

    result = await sweep_squarespace_store(store_id=STORE_ID, now=NOW, client=client)

    assert result["invalid"] == 1
    assert [call["order"]["id"] for call in recorder.calls] == ["good"]


async def test_a_malformed_order_does_not_abort_the_rest_of_the_page(monkeypatch):
    """One order the mapper refuses must not cost the sweep the whole page —
    and must not advance past the orders it never reached."""
    from services.squarespace_ledger import SquarespaceIngestResult
    from services.squarespace_order_sweep import sweep_squarespace_store

    class _Exploding:
        def __init__(self):
            self.calls = []

        async def __call__(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs["order"]["id"] == "bad":
                raise ValueError("malformed money claim")
            return SquarespaceIngestResult(status="recorded", accepted=1)

    recorder = _Exploding()
    _wire(monkeypatch, credentials=_CREDENTIALS, recorder=recorder)
    client = _FakeClient(
        [
            _page(
                [
                    _order("bad", NOW - timedelta(hours=2)),
                    _order("good", NOW - timedelta(hours=1)),
                ]
            )
        ]
    )

    result = await sweep_squarespace_store(store_id=STORE_ID, now=NOW, client=client)

    assert result["invalid"] == 1
    assert result["accepted"] == 1
    assert [call["order"]["id"] for call in recorder.calls] == ["bad", "good"]


async def test_an_ignored_order_is_counted_separately_from_an_accepted_one(
    monkeypatch,
):
    from services.squarespace_order_sweep import sweep_squarespace_store

    _wire(
        monkeypatch,
        credentials=_CREDENTIALS,
        recorder=_Recorder(status="ignored", accepted=0),
    )
    client = _FakeClient([_page([_order("a", NOW - timedelta(hours=1))])])

    result = await sweep_squarespace_store(store_id=STORE_ID, now=NOW, client=client)

    assert result["ignored"] == 1
    assert result["accepted"] == 0


# ---- failure modes ---------------------------------------------------------


async def test_a_store_with_no_credential_is_a_sweep_error(monkeypatch):
    from services.squarespace_order_sweep import (
        SquarespaceSweepError,
        sweep_squarespace_store,
    )

    _wire(monkeypatch, credentials={"website_id": "site-1"})

    with pytest.raises(SquarespaceSweepError):
        await sweep_squarespace_store(
            store_id=STORE_ID, now=NOW, client=_FakeClient([_page([])])
        )


async def test_an_unknown_store_is_a_sweep_error(monkeypatch):
    from services.squarespace_order_sweep import (
        SquarespaceSweepError,
        sweep_squarespace_store,
    )

    _wire(monkeypatch, credentials=_CREDENTIALS, store=False)

    with pytest.raises(SquarespaceSweepError):
        await sweep_squarespace_store(
            store_id=STORE_ID, now=NOW, client=_FakeClient([_page([])])
        )


async def test_an_http_failure_leaves_the_cursor_alone(monkeypatch):
    """A sweep that dies mid-window must not persist a cursor covering orders
    it never read."""
    from services.squarespace_order_sweep import (
        SquarespaceSweepError,
        sweep_squarespace_store,
    )

    persisted, _ = _wire(monkeypatch, credentials=_CREDENTIALS)

    class _Failing(_FakeClient):
        async def get(self, url, headers=None, params=None):
            return _FakeErrorResponse()

    with pytest.raises(SquarespaceSweepError):
        await sweep_squarespace_store(store_id=STORE_ID, now=NOW, client=_Failing([]))

    assert persisted == {}


class _FakeErrorResponse:
    status_code = 500
    content = b"{}"

    def json(self):
        return {}


async def test_the_oauth_token_is_preferred_for_the_list_read(monkeypatch):
    from services.squarespace_order_sweep import sweep_squarespace_store

    _wire(
        monkeypatch,
        credentials={**_CREDENTIALS, "oauth_access_token": "sq-oauth"},
    )

    captured = {}

    class _HeaderCapturing(_FakeClient):
        async def get(self, url, headers=None, params=None):
            captured.update(headers or {})
            return await super().get(url, headers=headers, params=params)

    await sweep_squarespace_store(
        store_id=STORE_ID, now=NOW, client=_HeaderCapturing([_page([])])
    )

    assert captured["Authorization"] == "Bearer sq-oauth"
    # Squarespace answers 400 to a request with no User-Agent; it is required,
    # not a courtesy.
    assert captured["User-Agent"]


# ---- the fetch layer's own guard -------------------------------------------
#
# The sweep passes `modified_after=None` on every page after the first, so the
# fetch layer's own exclusivity check never sees the conflicting pair through
# that caller — an upstream guard makes a downstream one untestable. These
# drive `fetch_squarespace_order_page` directly so the second guard is
# independently killable, which matters because it is the one that decides what
# actually goes on the wire.


async def test_the_fetch_layer_sends_the_cursor_alone_even_if_handed_bounds():
    from services.squarespace_order_fetch import fetch_squarespace_order_page

    client = _FakeClient([_page([])])

    await fetch_squarespace_order_page(
        access_token="t",
        modified_after=_iso(NOW - timedelta(days=1)),
        modified_before=_iso(NOW),
        cursor="cur-2",
        client=client,
    )

    assert client.requests[0]["params"] == {"cursor": "cur-2"}


async def test_the_fetch_layer_refuses_a_half_specified_window():
    """`modifiedAfter` and `modifiedBefore` are a pair. One without the other
    is rejected by Squarespace, and a silently-dropped bound would turn a
    30-minute window into the whole of history."""
    from services.squarespace_order_fetch import (
        SquarespaceOrderFetchError,
        fetch_squarespace_order_page,
    )

    for after, before in (
        (_iso(NOW - timedelta(days=1)), None),
        (None, _iso(NOW)),
        (None, None),
    ):
        with pytest.raises(SquarespaceOrderFetchError):
            await fetch_squarespace_order_page(
                access_token="t",
                modified_after=after,
                modified_before=before,
                client=_FakeClient([_page([])]),
            )


async def test_the_fetch_layer_stops_when_no_next_cursor_is_returned():
    """The continuation signal is the CURSOR, not `hasNextPage`: the cursor is
    what the next request needs, and gating on the boolean alone would truncate
    a sweep if the envelope stopped sending that flag."""
    from services.squarespace_order_fetch import fetch_squarespace_order_page

    page = await fetch_squarespace_order_page(
        access_token="t",
        modified_after=_iso(NOW - timedelta(days=1)),
        modified_before=_iso(NOW),
        client=_FakeClient([{"result": [], "pagination": {"hasNextPage": True}}]),
    )
    assert page.next_cursor is None

    page = await fetch_squarespace_order_page(
        access_token="t",
        modified_after=_iso(NOW - timedelta(days=1)),
        modified_before=_iso(NOW),
        client=_FakeClient([{"result": [], "pagination": {"nextPageCursor": "c"}}]),
    )
    assert page.next_cursor == "c"


async def test_the_sweep_hands_the_fetch_layer_no_bounds_once_it_holds_a_cursor(
    monkeypatch,
):
    """The sweep's OWN decision, separately from what the fetch layer then does
    with it. Asserting only the wire cannot see this: the fetch layer drops the
    bounds when a cursor is present, so it would mask a sweep that kept sending
    them."""
    from services import squarespace_order_sweep as sweep
    from services.squarespace_order_fetch import SquarespaceOrderPage

    _wire(monkeypatch, credentials=_CREDENTIALS)
    calls = []
    pages = [
        SquarespaceOrderPage(orders=[], next_cursor="cur-2"),
        SquarespaceOrderPage(orders=[], next_cursor=None),
    ]

    async def spying_fetch(**kwargs):
        calls.append(kwargs)
        return pages[len(calls) - 1]

    monkeypatch.setattr(sweep, "fetch_squarespace_order_page", spying_fetch)

    await sweep.sweep_squarespace_store(
        store_id=STORE_ID, now=NOW, client=_FakeClient([])
    )

    assert calls[0]["cursor"] is None
    assert calls[0]["modified_after"] and calls[0]["modified_before"]
    assert calls[1]["cursor"] == "cur-2"
    assert calls[1]["modified_after"] is None
    assert calls[1]["modified_before"] is None


# ---- truncation converges instead of freezing ------------------------------


class _AlwaysTruncatingClient(_FakeClient):
    """Every page claims another page after it, forever.

    The worst honest case: a window whose order volume the page cap can never
    read in one pass. It is also the shape that exposed the frozen cursor —
    holding the cursor while `now` advances means the NEXT window is wider than
    the one that just failed, so the run truncates on the same prefix again and
    the rest of the range is never read at all.
    """

    def __init__(self, *, website_id="site-1"):
        super().__init__([], website_id=website_id)
        self.windows = []

    async def get(self, url, headers=None, params=None):
        if "/authorization/website" in str(url):
            return await super().get(url, headers=headers, params=params)
        params = dict(params or {})
        if "modifiedAfter" in params:
            self.windows.append((params["modifiedAfter"], params["modifiedBefore"]))
        self.requests.append({"url": url, "params": params})
        return _FakeResponse(
            {
                "result": [_order("x", NOW - timedelta(days=1))],
                "pagination": {"hasNextPage": True, "nextPageCursor": "more"},
            }
        )


async def test_a_truncated_sweep_makes_cursor_progress_over_repeated_runs(
    monkeypatch, caplog
):
    """The frozen-cursor trap, driven the only way it shows: by running twice.

    A single truncated run looks correct — it holds the cursor, which is the
    conservative thing to do. The bug is only visible across runs: with the
    cursor held and `now` advancing, run 2 asks for a WIDER window than run 1,
    truncates on the same page-cap prefix, and so does every run after it. The
    store never advances past its first 20 pages.

    So this asserts the property that actually matters — the cursor MOVES —
    against a client that never stops truncating.
    """
    from services.squarespace_order_sweep import sweep_squarespace_store

    _wire(monkeypatch, credentials=_CREDENTIALS)
    client = _AlwaysTruncatingClient()

    cursors = []
    moment = NOW
    for _ in range(24):
        result = await sweep_squarespace_store(
            store_id=STORE_ID,
            now=moment,
            client=client,
            overlap_minutes=30,
            max_pages=1,
        )
        assert result["truncated"] is True
        cursors.append(result["cursor_after"])
        # Real time moves on between runs; that is exactly what made the frozen
        # window widen instead of narrow.
        moment += timedelta(minutes=10)

    advanced = [c for c in cursors if c]
    assert advanced, (
        "the cursor never moved across 24 truncated runs — the window is frozen "
        f"and the store is stuck; windows tried: {client.windows[:6]}"
    )
    assert advanced[-1] > advanced[0] or len(set(advanced)) > 1, advanced

    # And it converged by NARROWING. Without the bisect the windows widen with
    # `now`, which is the failure this test exists to name.
    spans = [
        (datetime.fromisoformat(end.replace("Z", "+00:00"))
         - datetime.fromisoformat(start.replace("Z", "+00:00")))
        for start, end in client.windows
    ]
    assert spans[1] < spans[0], f"the second window did not narrow: {spans[:3]}"
    assert min(spans) < spans[0] / 10, spans[:6]

    # A range that could not be read even at the bisect floor is advanced PAST,
    # and that is a loud event: staying put would be an unbounded outage, but a
    # silent skip would be worse than a loud one. The log must name the range.
    floor_errors = [
        record.getMessage()
        for record in caplog.records
        if record.levelname == "ERROR" and "bisect floor" in record.getMessage()
    ]
    assert floor_errors, "advancing past an unreadable range must be an ERROR"
    assert "..' " not in floor_errors[0]
    assert STORE_ID in floor_errors[0]
    assert ".." in floor_errors[0], floor_errors[0]


async def test_a_bounded_window_that_completes_advances_the_cursor_to_its_end(
    monkeypatch,
):
    """A bisected window that FITS must move the cursor to that window's end.

    Not to the highest `modifiedOn` seen: the bisect is digging forward out of
    a range it could not read, and stopping at the high-water mark would leave
    the next window overlapping the prefix it just finished — the same stall,
    one step further along.
    """
    from services.squarespace_order_sweep import sweep_squarespace_store

    cursor = NOW - timedelta(days=2)
    credentials = {
        **_CREDENTIALS,
        "reconciliation": {
            "orders_cursor": _iso(cursor),
            # The previous run truncated at this point and could not reach it.
            "truncated_window_end": _iso(NOW),
        },
    }
    persisted, _ = _wire(monkeypatch, credentials=credentials)
    # One page, no next page: this bounded window fits.
    client = _FakeClient([_page([_order("a", cursor + timedelta(hours=1))])])

    result = await sweep_squarespace_store(
        store_id=STORE_ID, now=NOW, client=client, overlap_minutes=30, apply=True
    )

    assert result["truncated"] is False
    assert result["window_bounded"] is True
    assert result["cursor_after"] == result["window_end"], (
        "a completed bounded window must advance to its END, not to the highest "
        "modifiedOn it happened to see"
    )
    # The midpoint of the range the previous run could not finish.
    assert result["window_end"] < _iso(NOW)
    assert result["window_end"] > result["window_start"]
    # The truncation marker is CLEARED, or the next run would bisect forever.
    assert persisted["reconciliation"].get("truncated_window_end") is None


async def test_the_next_run_resumes_from_a_completed_bounded_window(monkeypatch):
    """Two runs in sequence: bisect, then keep stepping forward.

    The cursor of run 2 must be strictly ahead of run 1's. This is the "climbs
    back to real time" half of the fix; without it a store that fell behind
    would bisect once and then sit at that point.
    """
    from services.squarespace_order_sweep import sweep_squarespace_store

    cursor = NOW - timedelta(days=4)
    credentials = {
        **_CREDENTIALS,
        "reconciliation": {
            "orders_cursor": _iso(cursor),
            "truncated_window_end": _iso(NOW),
        },
    }
    _wire(monkeypatch, credentials=credentials)

    first = await sweep_squarespace_store(
        store_id=STORE_ID,
        now=NOW,
        client=_FakeClient([_page([])]),
        overlap_minutes=30,
        apply=True,
    )
    second = await sweep_squarespace_store(
        store_id=STORE_ID,
        now=NOW + timedelta(minutes=5),
        client=_FakeClient([_page([])]),
        overlap_minutes=30,
        apply=True,
    )

    assert first["cursor_after"] < second["cursor_after"], (first, second)
    # Widening, not crawling: the second bounded window reaches further than the
    # first, so a store that fell days behind climbs back geometrically.
    assert second["window_end"] > first["window_end"]


async def test_the_operator_can_pin_the_window_end(monkeypatch):
    """The escape hatch. An operator digging a store out of a busy range needs
    to choose the bound rather than wait for the halving to find it."""
    from services.squarespace_order_sweep import sweep_squarespace_store

    _wire(monkeypatch, credentials=_CREDENTIALS)
    client = _FakeClient([_page([])])

    result = await sweep_squarespace_store(
        store_id=STORE_ID,
        now=NOW,
        client=client,
        overlap_minutes=30,
        modified_before="2026-09-03T00:00:00.000Z",
    )

    assert client.requests[0]["params"]["modifiedBefore"] == "2026-09-03T00:00:00.000Z"
    assert result["window_end"] == "2026-09-03T00:00:00.000Z"
    assert result["window_bounded"] is True


async def test_an_unparseable_window_override_is_refused(monkeypatch):
    """Not silently ignored: an operator who mistypes the bound and is answered
    with an ordinary `now` run believes they read a range they did not."""
    from services.squarespace_order_sweep import (
        SquarespaceSweepError,
        sweep_squarespace_store,
    )

    _wire(monkeypatch, credentials=_CREDENTIALS)

    with pytest.raises(SquarespaceSweepError, match="ISO-8601"):
        await sweep_squarespace_store(
            store_id=STORE_ID,
            now=NOW,
            client=_FakeClient([_page([])]),
            modified_before="last tuesday",
        )


# ---- the site binding ------------------------------------------------------


async def test_a_credential_naming_a_different_site_refuses_and_holds_the_cursor(
    monkeypatch,
):
    """Cross-site contamination, which nothing downstream can detect.

    Re-point a store from site A to site B while site A's OAuth token is still
    in the blob and every read still reaches site A. Its orders are well-formed
    — they just belong to somebody else's shop, and they land in the ledger
    under this merchant. So the site is proven BEFORE the first list call.
    """
    from services.squarespace_order_sweep import (
        SquarespaceSweepError,
        sweep_squarespace_store,
    )

    persisted, recorder = _wire(
        monkeypatch,
        credentials={
            "api_key": "sq-api-key",
            "oauth_access_token": "token-for-site-OLD",
            "website_id": "site-1",
        },
    )
    client = _AlwaysTruncatingClient(website_id="site-SOMEONE-ELSE")

    with pytest.raises(SquarespaceSweepError, match="different site"):
        await sweep_squarespace_store(
            store_id=STORE_ID, now=NOW, client=client, apply=True
        )

    # Nothing was listed, nothing was recorded, and the cursor is untouched.
    assert client.requests == []
    assert recorder.calls == []
    assert persisted == {}


async def test_a_matching_site_is_verified_once_per_run_not_once_per_page(
    monkeypatch,
):
    """The positive counterpart, and a bound on the cost.

    Without the second half, the check could be implemented per page — one
    extra upstream call for every page of every store on every run — and this
    file would still be green.
    """
    from services.squarespace_order_sweep import sweep_squarespace_store

    _wire(monkeypatch, credentials=_CREDENTIALS)
    client = _FakeClient(
        [
            _page([_order("a", NOW - timedelta(hours=2))], next_cursor="cur-2"),
            _page([_order("b", NOW - timedelta(hours=1))]),
        ]
    )

    result = await sweep_squarespace_store(store_id=STORE_ID, now=NOW, client=client)

    assert result["status"] == "success"
    assert result["pages"] == 2
    assert client.website_lookups == ["sq-api-key"]


async def test_a_store_with_no_website_binding_refuses_rather_than_sweeping(
    monkeypatch,
):
    """No binding means nothing to compare the credential against, so the check
    would be vacuous. Refuse and ask for a reconnect instead of sweeping
    unverified."""
    from services.squarespace_order_sweep import (
        SquarespaceSweepError,
        sweep_squarespace_store,
    )

    _wire(monkeypatch, credentials={"api_key": "sq-api-key"})
    client = _FakeClient([_page([])])

    with pytest.raises(SquarespaceSweepError, match="website_id"):
        await sweep_squarespace_store(store_id=STORE_ID, now=NOW, client=client)
    assert client.requests == []


# ---- the OAuth token expires; the API key does not -------------------------


async def test_an_expired_oauth_token_falls_back_to_the_api_key(monkeypatch):
    """A Developer-Platform access token is short-lived and there is no refresh
    path in this repo yet.

    Preferring it unconditionally takes a store that holds a perfectly good
    per-site API key dark within the hour, and the sweep is that store's ONLY
    telemetry path. So a 401 falls back rather than failing the run.
    """
    from services.squarespace_order_sweep import sweep_squarespace_store

    _wire(
        monkeypatch,
        credentials={
            "api_key": "sq-api-key",
            "oauth_access_token": "expired-token",
            "website_id": "site-1",
        },
    )
    client = _FakeClient(
        [_page([_order("a", NOW - timedelta(hours=1))])],
        unauthorized_tokens={"expired-token"},
    )

    result = await sweep_squarespace_store(store_id=STORE_ID, now=NOW, client=client)

    assert result["status"] == "success"
    assert result["seen"] == 1
    # It TRIED the OAuth token first — the preference is still real — and then
    # read with the key.
    assert client.website_lookups == ["expired-token", "sq-api-key"]


async def test_every_credential_being_refused_is_a_sweep_failure(monkeypatch):
    """The fallback must not become "any 401 is fine". When nothing works the
    run fails loudly and the cursor stays put; a silent success would report a
    quiet, permanently empty sweep."""
    from services.squarespace_order_sweep import (
        SquarespaceSweepError,
        sweep_squarespace_store,
    )

    persisted, _ = _wire(
        monkeypatch,
        credentials={
            "api_key": "sq-api-key",
            "oauth_access_token": "expired-token",
            "website_id": "site-1",
        },
    )
    client = _FakeClient(
        [_page([])], unauthorized_tokens={"expired-token", "sq-api-key"}
    )

    with pytest.raises(SquarespaceSweepError, match="refused every stored credential"):
        await sweep_squarespace_store(
            store_id=STORE_ID, now=NOW, client=client, apply=True
        )
    assert persisted == {}
