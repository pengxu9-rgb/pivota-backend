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
    """Answers `fetch_squarespace_order_page` with scripted pages."""

    def __init__(self, pages):
        self._pages = list(pages)
        self.requests = []

    async def get(self, url, headers=None, params=None):
        self.requests.append({"url": url, "params": dict(params or {})})
        page = self._pages.pop(0) if self._pages else {"result": [], "pagination": {}}
        return _FakeResponse(page)

    async def aclose(self):
        return None


class _FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload
        self.content = b"{}"

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
    and the credential-blob persistence."""
    import json

    from services import squarespace_order_sweep as sweep

    persisted = {}

    async def fake_find(store_id):
        if not store:
            return None
        return {
            "store_id": STORE_ID,
            "merchant_id": MERCHANT_ID,
            "domain": "shop.example",
            "api_key": json.dumps(credentials),
        }

    async def fake_merge(*, store_id, updates):
        persisted.update(updates)
        return {**credentials, **updates}

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
