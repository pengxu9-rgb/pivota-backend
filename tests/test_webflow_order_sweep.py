"""The Webflow reconciliation sweep, driven through the REAL fetch layer.

The double is an httpx-shaped client, not the fetch functions: the URL, the
`offset`/`limit`/`status` parameters, the envelope parsing and the page
arithmetic are all production code, so a sweep that paged wrongly or read the
wrong collection key would fail here rather than pass against a convenient stub.

What is pinned:

* the site is proven once per run BEFORE anything is listed, and a mismatch
  refuses the run with every cursor untouched;
* pagination walks by offset and stops at a short page;
* the early stop fires only while the list is actually non-increasing, and the
  ordering claim is checked against what arrived rather than assumed;
* the cursor never moves backwards, and never moves at all on a truncated pass;
* a truncated pass RESUMES from its offset on the next run instead of re-reading
  the same prefix forever;
* each lane keeps its own cursor;
* a lane whose status filter Webflow rejects fails alone.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import json

import pytest

MERCHANT_ID = "merchant-wf"
STORE_ID = "store-wf"
SITE_ID = "5f1a0000000000000000aaaa"
NOW = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.replace(tzinfo=None).isoformat(timespec="milliseconds") + "Z"


def _order(order_id, *, accepted, status="unfulfilled", refunded=None, value=5898):
    order = {
        "orderId": order_id,
        "status": status,
        "acceptedOn": _iso(accepted),
        "customerPaid": {"unit": "USD", "value": value},
    }
    if refunded is not None:
        order["refundedOn"] = _iso(refunded)
    return order


class _Response:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.content = json.dumps(payload).encode()

    def json(self):
        return self._payload


class _Client:
    """An httpx-shaped double over the Webflow Data API.

    `pages_by_status` maps a status filter (None for the unfiltered lane) to the
    list of orders that lane's list call should return; the double slices it by
    the `offset`/`limit` the SWEEP actually sent, so a paging bug shows up as a
    wrong slice rather than as a convenient stub.
    """

    def __init__(self, *, pages_by_status=None, site_id=SITE_ID, status_errors=None,
                 site_status=200, orders_by_id=None):
        self.orders_by_status = pages_by_status or {}
        self.orders_by_id = orders_by_id or {}
        self.site_id = site_id
        self.site_status = site_status
        self.status_errors = status_errors or {}
        self.calls = []

    async def get(self, url, headers=None, params=None):
        url = str(url)
        if "/orders" not in url:
            self.calls.append({"kind": "site", "url": url})
            if self.site_status != 200:
                return _Response({}, status_code=self.site_status)
            return _Response({"id": self.site_id, "displayName": "Shop"})
        head, _, tail = url.partition("/orders/")
        if tail:
            # `GET /v2/sites/{site}/orders/{id}` — the keyed read the pending
            # replay uses. Served by the same double as the list so the real
            # `fetch_webflow_order` (its id validation, its status mapping, its
            # envelope check) is what runs.
            self.calls.append({"kind": "order", "order_id": tail, "status": None})
            order = self.orders_by_id.get(tail)
            if order is None:
                return _Response({"message": "not found"}, status_code=404)
            if isinstance(order, int):
                return _Response({"message": "boom"}, status_code=order)
            return _Response(order)
        params = dict(params or {})
        status = params.get("status")
        self.calls.append({"kind": "orders", "status": status, **params})
        if status in self.status_errors:
            return _Response({}, status_code=self.status_errors[status])
        rows = self.orders_by_status.get(status, [])
        offset = int(params.get("offset") or 0)
        limit = int(params.get("limit") or 100)
        window = rows[offset : offset + limit]
        return _Response(
            {"orders": window, "pagination": {"offset": offset, "limit": limit}}
        )

    async def aclose(self):
        return None

    def order_calls(self, status=None):
        return [c for c in self.calls if c["kind"] == "orders" and c["status"] == status]


def _install(monkeypatch, *, credentials=None, recorder=None):
    """Patch the store lookup, the credential merge, and the ingest."""
    from services import webflow_order_sweep as sweep

    blob = credentials if credentials is not None else {
        "api_token": "wf-token",
        "site_id": SITE_ID,
    }
    # What the row HOLDS, as distinct from what the run read at the top. They
    # are the same object's contents until something writes between the two,
    # which is exactly what `_write_between` in the concurrency test does.
    stored = dict(blob)
    merged = {}

    async def fake_find(store_id):
        return {
            "store_id": STORE_ID,
            "merchant_id": MERCHANT_ID,
            "domain": "shop.webflow.io",
            "api_key": json.dumps(blob),
        }

    async def fake_merge(*, store_id, updates=None, mutate=None, **kwargs):
        """A merge that genuinely READS the current blob, mutates it, writes back.

        The sweep's final write is a `mutate` that merges only the lanes THIS
        run walked into whatever `reconciliation` holds at write time. A double
        that ignored the callback, or ran it against an empty dict, would make
        that claim untestable — and would have kept passing while the sweep went
        back to overwriting the whole subtree.
        """
        if mutate is not None:
            blob.clear()
            blob.update(mutate(dict(stored)))
        if updates:
            blob.update(updates)
        stored.clear()
        stored.update(blob)
        merged.clear()
        merged.update(blob)
        return dict(blob)

    recorded = []

    async def fake_record(**kwargs):
        from services.webflow_ledger import WebflowIngestResult

        recorded.append(kwargs)
        return (recorder or (lambda _: WebflowIngestResult(status="recorded", accepted=2)))(
            kwargs
        )

    monkeypatch.setattr(sweep, "find_webflow_store", fake_find)
    monkeypatch.setattr(sweep, "merge_webflow_credentials", fake_merge)
    monkeypatch.setattr(sweep, "record_webflow_order", fake_record)
    return merged, recorded, stored


async def _run(monkeypatch, client, *, credentials=None, recorder=None, now=NOW, **kwargs):
    from services.webflow_order_sweep import sweep_webflow_store

    merged, recorded, _stored = _install(
        monkeypatch, credentials=credentials, recorder=recorder
    )
    stats = await sweep_webflow_store(
        store_id=STORE_ID, client=client, now=now, **kwargs
    )
    return stats, merged, recorded


def _lane(stats, name):
    return next(lane for lane in stats["lanes"] if lane["lane"] == name)


# ---- the site check ---------------------------------------------------------


async def test_the_site_is_proven_before_anything_is_listed(monkeypatch):
    client = _Client(pages_by_status={None: [_order("o-1", accepted=NOW)]})

    stats, _merged, _recorded = await _run(monkeypatch, client)

    assert stats["status"] == "success"
    assert client.calls[0]["kind"] == "site"
    assert f"/sites/{SITE_ID}" in client.calls[0]["url"]


async def test_a_credential_naming_a_different_site_refuses_the_run(monkeypatch):
    """A store re-pointed at another site, with the old token still in the blob.
    Nothing is listed, nothing is recorded, and no cursor moves."""
    from services.webflow_order_sweep import WebflowSweepError

    client = _Client(
        pages_by_status={None: [_order("o-1", accepted=NOW)]}, site_id="another-site"
    )

    with pytest.raises(WebflowSweepError):
        await _run(monkeypatch, client)

    assert client.order_calls() == []


async def test_a_revoked_token_refuses_the_run_rather_than_sweeping_empty(monkeypatch):
    """A 401 on the site check must be a LOUD failure. An empty sweep reporting
    success would look exactly like a quiet store."""
    from services.webflow_order_sweep import WebflowSweepError

    client = _Client(pages_by_status={None: []}, site_status=401)

    with pytest.raises(WebflowSweepError):
        await _run(monkeypatch, client)


async def test_a_store_with_no_site_binding_refuses_the_run(monkeypatch):
    from services.webflow_order_sweep import WebflowSweepError

    with pytest.raises(WebflowSweepError):
        await _run(
            monkeypatch, _Client(), credentials={"api_token": "wf-token"}
        )


# ---- pagination -------------------------------------------------------------


async def test_offset_pagination_walks_to_the_end_of_the_list(monkeypatch):
    orders = [
        _order(f"o-{i}", accepted=NOW - timedelta(minutes=i)) for i in range(7)
    ]
    client = _Client(pages_by_status={None: orders})

    stats, _merged, recorded = await _run(monkeypatch, client, page_limit=3)

    lane = _lane(stats, "orders")
    assert lane["seen"] == 7
    assert lane["pages"] == 3
    assert lane["complete"] is True
    assert [c["offset"] for c in client.order_calls()] == [0, 3, 6]
    assert {c["limit"] for c in client.order_calls()} == {3}
    assert len(recorded) == 7


async def test_an_empty_lane_completes_in_one_page(monkeypatch):
    client = _Client(pages_by_status={None: []})

    stats, _merged, _recorded = await _run(monkeypatch, client)

    assert _lane(stats, "orders")["pages"] == 1
    assert _lane(stats, "orders")["complete"] is True


# ---- the early stop, and the ordering claim ---------------------------------


async def test_the_early_stop_fires_on_a_newest_first_list(monkeypatch):
    """The steady state: one page, and the walk ends because everything on it is
    older than the cursor minus the overlap."""
    old = [
        _order(f"old-{i}", accepted=NOW - timedelta(days=10 + i)) for i in range(4)
    ]
    fresh = [_order("new-1", accepted=NOW)]
    client = _Client(pages_by_status={None: fresh + old})
    credentials = {
        "api_token": "wf-token",
        "site_id": SITE_ID,
        "reconciliation": {
            # `ordering_verified` was established by a previous COMPLETE pass;
            # without it the early stop is not armed at all.
            "orders": {
                "cursor": _iso(NOW - timedelta(days=1)),
                "next_offset": 0,
                "ordering_verified": True,
            }
        },
    }

    stats, _merged, recorded = await _run(
        monkeypatch, client, credentials=credentials, page_limit=2, overlap_minutes=60
    )

    lane = _lane(stats, "orders")
    # Page 0 carries `new-1` and `old-0`; page 1 is entirely below the
    # threshold, so the walk stops there rather than reading pages 2 and 3.
    assert lane["stopped_early"] is True
    assert lane["complete"] is True
    assert [c["offset"] for c in client.order_calls()] == [0, 2]
    assert [call["order"]["orderId"] for call in recorded] == ["new-1"]
    assert lane["skipped_already_recorded"] == 3


async def test_an_out_of_order_list_disables_the_early_stop(monkeypatch):
    """The ordering is an ASSUMED claim, so it is checked against what arrived.

    A lane that stops early on an unordered list skips everything past the stop.
    Here the second row of the first page is NEWER than the first, and from that
    moment the lane walks the whole list instead — and finds the fresh order it
    would otherwise have stopped short of.
    """
    rows = [
        _order("old-0", accepted=NOW - timedelta(days=10)),
        # Out of order: newer than the row above it.
        _order("fresh", accepted=NOW),
        _order("old-1", accepted=NOW - timedelta(days=11)),
        _order("old-2", accepted=NOW - timedelta(days=12)),
    ]
    client = _Client(pages_by_status={None: rows})
    credentials = {
        "api_token": "wf-token",
        "site_id": SITE_ID,
        "reconciliation": {
            "orders": {
                "cursor": _iso(NOW - timedelta(days=1)),
                "ordering_verified": True,
            }
        },
    }

    stats, merged, recorded = await _run(
        monkeypatch, client, credentials=credentials, page_limit=2
    )

    lane = _lane(stats, "orders")
    assert lane["ordering_verified"] is False
    assert "stopped_early" not in lane
    # Page 1 is entirely below the threshold and would have ended an armed walk.
    assert [c["offset"] for c in client.order_calls()] == [0, 2, 4]
    assert [call["order"]["orderId"] for call in recorded] == ["fresh"]
    # And the verdict PERSISTS, so the next run does not re-arm on the strength
    # of a clean prefix.
    assert merged["reconciliation"]["orders"]["ordering_verified"] is False


async def test_the_early_stop_is_not_armed_until_a_COMPLETE_pass_verified_it(
    monkeypatch,
):
    """The gap a within-run check alone leaves open, closed by arming.

    A run whose FIRST page happens to be entirely below the threshold would stop
    there having seen a perfectly ordered two-row prefix — and never reach the
    out-of-order row further down that the check exists to catch. So a lane with
    no established verdict walks the whole list, whatever its first page looks
    like, and that walk is what establishes the verdict.
    """
    rows = [
        _order("old-0", accepted=NOW - timedelta(days=10)),
        _order("old-1", accepted=NOW - timedelta(days=11)),
        # Beyond where an armed walk would have stopped.
        _order("fresh", accepted=NOW),
    ]
    client = _Client(pages_by_status={None: rows})
    credentials = {
        "api_token": "wf-token",
        "site_id": SITE_ID,
        # A cursor, but NO ordering verdict: this store has never completed a
        # pass that could establish one.
        "reconciliation": {"orders": {"cursor": _iso(NOW - timedelta(days=1))}},
    }

    stats, merged, recorded = await _run(
        monkeypatch, client, credentials=credentials, page_limit=2
    )

    assert _lane(stats, "orders")["early_stop_armed"] is False
    assert [call["order"]["orderId"] for call in recorded] == ["fresh"]
    assert merged["reconciliation"]["orders"]["ordering_verified"] is False


async def test_a_truncated_pass_does_not_promote_a_clean_prefix_to_a_clean_list(
    monkeypatch,
):
    """A violation is PROOF and lands immediately; "no violation seen" is only
    proof when the whole list was read."""
    rows = [_order(f"o-{i}", accepted=NOW - timedelta(minutes=i)) for i in range(10)]
    client = _Client(pages_by_status={None: rows})

    _stats, merged, _recorded = await _run(
        monkeypatch, client, page_limit=2, max_pages=2
    )

    assert merged["reconciliation"]["orders"]["ordering_verified"] is False


async def test_the_first_ever_run_has_no_threshold_and_reads_everything(monkeypatch):
    client = _Client(
        pages_by_status={
            None: [_order(f"o-{i}", accepted=NOW - timedelta(days=i)) for i in range(5)]
        }
    )

    stats, _merged, recorded = await _run(monkeypatch, client, page_limit=2)

    assert _lane(stats, "orders")["seen"] == 5
    assert len(recorded) == 5


# ---- the cursor -------------------------------------------------------------


async def test_a_completed_pass_advances_the_cursor_to_its_high_water_mark(monkeypatch):
    newest = NOW - timedelta(hours=2)
    client = _Client(
        pages_by_status={
            None: [
                _order("o-1", accepted=newest),
                _order("o-2", accepted=NOW - timedelta(days=3)),
            ]
        }
    )

    stats, merged, _recorded = await _run(monkeypatch, client)

    assert _lane(stats, "orders")["cursor_after"] == _iso(newest)
    assert merged["reconciliation"]["orders"]["cursor"] == _iso(newest)


async def test_the_cursor_never_moves_backwards(monkeypatch):
    """A back-dated order, or a clock skew, must not re-open a closed pass."""
    held = NOW - timedelta(hours=1)
    client = _Client(
        pages_by_status={None: [_order("o-1", accepted=NOW - timedelta(days=30))]}
    )
    credentials = {
        "api_token": "wf-token",
        "site_id": SITE_ID,
        "reconciliation": {"orders": {"cursor": _iso(held)}},
    }

    stats, merged, _recorded = await _run(
        monkeypatch, client, credentials=credentials
    )

    assert _lane(stats, "orders")["cursor_after"] == _iso(held)
    assert merged["reconciliation"]["orders"]["cursor"] == _iso(held)


async def test_a_truncated_pass_does_NOT_advance_the_cursor(monkeypatch):
    """The pass is incomplete, so orders below the maximum anchor it saw may
    still be unread. Advancing past them would lose them for good."""
    client = _Client(
        pages_by_status={
            None: [_order(f"o-{i}", accepted=NOW - timedelta(minutes=i)) for i in range(20)]
        }
    )

    stats, merged, _recorded = await _run(
        monkeypatch, client, page_limit=2, max_pages=2
    )

    lane = _lane(stats, "orders")
    assert lane["truncated"] is True
    assert lane["complete"] is False
    assert lane["cursor_after"] is None
    assert "cursor" not in merged["reconciliation"]["orders"]


async def test_a_truncated_pass_RESUMES_from_its_offset_on_the_next_run(monkeypatch):
    """The trap this avoids: restarting at offset 0 every run re-reads the same
    page-cap prefix and never reaches the rest of the backlog."""
    rows = [_order(f"o-{i}", accepted=NOW - timedelta(minutes=i)) for i in range(10)]
    first_client = _Client(pages_by_status={None: rows})

    first, merged, first_recorded = await _run(
        monkeypatch, first_client, page_limit=2, max_pages=2
    )

    assert _lane(first, "orders")["next_offset"] == 4
    assert merged["reconciliation"]["orders"]["next_offset"] == 4
    assert [c["order"]["orderId"] for c in first_recorded] == ["o-0", "o-1", "o-2", "o-3"]

    second_client = _Client(pages_by_status={None: rows})
    second, merged_2, second_recorded = await _run(
        monkeypatch,
        second_client,
        credentials={
            "api_token": "wf-token",
            "site_id": SITE_ID,
            "reconciliation": merged["reconciliation"],
        },
        page_limit=2,
        max_pages=2,
    )

    # It picked the walk up at 4 rather than re-reading 0..3.
    assert [c["offset"] for c in second_client.order_calls()] == [4, 6]
    assert [c["order"]["orderId"] for c in second_recorded] == ["o-4", "o-5", "o-6", "o-7"]
    assert merged_2["reconciliation"]["orders"]["next_offset"] == 8


async def test_the_resume_offset_is_cleared_once_the_pass_completes(monkeypatch):
    rows = [_order(f"o-{i}", accepted=NOW - timedelta(minutes=i)) for i in range(5)]
    client = _Client(pages_by_status={None: rows})

    stats, merged, _recorded = await _run(
        monkeypatch,
        client,
        credentials={
            "api_token": "wf-token",
            "site_id": SITE_ID,
            "reconciliation": {"orders": {"next_offset": 2}},
        },
        page_limit=2,
        max_pages=5,
    )

    assert [c["offset"] for c in client.order_calls()] == [2, 4]
    assert _lane(stats, "orders")["complete"] is True
    assert merged["reconciliation"]["orders"]["next_offset"] == 0


# ---- lanes ------------------------------------------------------------------


async def test_each_lane_lists_with_its_own_status_filter(monkeypatch):
    client = _Client(pages_by_status={None: [], "refunded": [], "dispute-lost": []})

    stats, _merged, _recorded = await _run(monkeypatch, client)

    assert [lane["lane"] for lane in stats["lanes"]] == [
        "orders",
        "refunded",
        "dispute_lost",
    ]
    assert [lane["status_filter"] for lane in stats["lanes"]] == [
        None,
        "refunded",
        "dispute-lost",
    ]


async def test_the_refund_lane_is_anchored_on_refundedOn_not_acceptedOn(monkeypatch):
    """A refund of a year-old order is FRESH news anchored on `refundedOn`.

    Anchored on `acceptedOn` it would sit below any cursor the orders lane had
    already passed, and the refund would never be recorded — which is precisely
    why the lanes are separate.
    """
    ancient = NOW - timedelta(days=365)
    client = _Client(
        pages_by_status={
            None: [],
            "refunded": [
                _order(
                    "old-order",
                    accepted=ancient,
                    status="refunded",
                    refunded=NOW - timedelta(minutes=5),
                )
            ],
            "dispute-lost": [],
        }
    )
    credentials = {
        "api_token": "wf-token",
        "site_id": SITE_ID,
        "reconciliation": {
            "orders": {"cursor": _iso(NOW - timedelta(days=1))},
            "refunded": {
                "cursor": _iso(NOW - timedelta(days=1)),
                "ordering_verified": True,
            },
        },
    }

    stats, merged, recorded = await _run(
        monkeypatch, client, credentials=credentials
    )

    assert [call["order"]["orderId"] for call in recorded] == ["old-order"]
    assert merged["reconciliation"]["refunded"]["cursor"] == _iso(
        NOW - timedelta(minutes=5)
    )


async def test_each_lane_keeps_its_own_cursor(monkeypatch):
    client = _Client(
        pages_by_status={
            None: [_order("o-1", accepted=NOW - timedelta(hours=1))],
            "refunded": [
                _order(
                    "r-1",
                    accepted=NOW - timedelta(days=5),
                    status="refunded",
                    refunded=NOW - timedelta(hours=3),
                )
            ],
            "dispute-lost": [],
        }
    )

    _stats, merged, _recorded = await _run(monkeypatch, client)

    state = merged["reconciliation"]
    assert state["orders"]["cursor"] == _iso(NOW - timedelta(hours=1))
    assert state["refunded"]["cursor"] == _iso(NOW - timedelta(hours=3))
    assert state["orders"]["cursor"] != state["refunded"]["cursor"]


async def test_a_lane_whose_status_filter_is_rejected_fails_ALONE(monkeypatch):
    """`dispute-lost` as a query value is an ASSUMED claim. If Webflow rejects
    it, that must not take down the lane that reads new orders."""
    client = _Client(
        pages_by_status={None: [_order("o-1", accepted=NOW)], "refunded": []},
        status_errors={"dispute-lost": 400},
    )

    stats, merged, recorded = await _run(monkeypatch, client)

    assert stats["status"] == "partial_failure"
    assert [f["lane"] for f in stats["lane_failures"]] == ["dispute_lost"]
    assert [lane["lane"] for lane in stats["lanes"]] == ["orders", "refunded"]
    assert [call["order"]["orderId"] for call in recorded] == ["o-1"]
    # The healthy lanes still persisted their state.
    assert "orders" in merged["reconciliation"]


async def test_a_single_lane_can_be_selected(monkeypatch):
    client = _Client(pages_by_status={"refunded": []})

    stats, _merged, _recorded = await _run(monkeypatch, client, lanes=["refunded"])

    assert [lane["lane"] for lane in stats["lanes"]] == ["refunded"]
    assert client.order_calls(None) == []


# ---- dry run, and the counters ----------------------------------------------


async def test_a_dry_run_writes_nothing_and_moves_no_cursor(monkeypatch):
    client = _Client(pages_by_status={None: [_order("o-1", accepted=NOW)]})

    stats, merged, recorded = await _run(monkeypatch, client, apply=False)

    assert stats["dry_run"] is True
    assert recorded == []
    assert merged == {}
    assert _lane(stats, "orders")["ignored"] == 1


async def test_duplicates_and_acceptances_are_counted_separately(monkeypatch):
    from services.webflow_ledger import WebflowIngestResult

    client = _Client(
        pages_by_status={
            None: [
                _order("o-1", accepted=NOW),
                _order("o-2", accepted=NOW - timedelta(minutes=1)),
            ]
        }
    )

    def _result(kwargs):
        if kwargs["order"]["orderId"] == "o-1":
            return WebflowIngestResult(status="recorded", accepted=2, duplicates=0)
        return WebflowIngestResult(status="recorded", accepted=0, duplicates=2)

    stats, _merged, _recorded = await _run(monkeypatch, client, recorder=_result)

    assert stats["accepted"] == 2
    assert stats["duplicates"] == 2


async def test_a_malformed_order_is_counted_and_does_not_stop_the_lane(monkeypatch):
    from services.webflow_ledger import WebflowIngestResult

    client = _Client(
        pages_by_status={
            None: [
                _order("o-1", accepted=NOW),
                _order("o-2", accepted=NOW - timedelta(minutes=1)),
            ]
        }
    )

    def _result(kwargs):
        if kwargs["order"]["orderId"] == "o-1":
            raise ValueError("customerPaid.value is not whole minor units")
        return WebflowIngestResult(status="recorded", accepted=2)

    stats, _merged, _recorded = await _run(monkeypatch, client, recorder=_result)

    assert _lane(stats, "orders")["invalid"] == 1
    assert stats["accepted"] == 2


async def test_a_flagged_test_order_is_counted_and_never_ingested(monkeypatch):
    order = _order("t-1", accepted=NOW)
    order["metadata"] = {"isTest": True}
    client = _Client(pages_by_status={None: [order]})

    stats, _merged, recorded = await _run(monkeypatch, client)

    assert _lane(stats, "orders")["test_orders_skipped"] == 1
    assert recorded == []


async def test_the_ordering_verdict_is_reported_on_the_run(monkeypatch):
    """A claim the operator can act on, rather than one buried in a docstring."""
    client = _Client(
        pages_by_status={
            None: [
                _order("a", accepted=NOW - timedelta(days=2)),
                _order("b", accepted=NOW),
            ]
        }
    )

    stats, _merged, _recorded = await _run(monkeypatch, client)

    assert stats["ordering_verified"] is False


# ---- all-stores mode --------------------------------------------------------


async def test_one_stores_failure_does_not_stop_the_others(monkeypatch):
    from services import webflow_order_sweep as sweep

    calls = []

    async def fake_store_sweep(*, store_id, **kwargs):
        calls.append(store_id)
        if store_id == "bad":
            raise sweep.WebflowSweepError("site verification failed")
        return {"store_id": store_id, "accepted": 1, "lane_failures": []}

    async def fake_active():
        return [{"store_id": "good-1"}, {"store_id": "bad"}, {"store_id": "good-2"}]

    monkeypatch.setattr(sweep, "sweep_webflow_store", fake_store_sweep)
    monkeypatch.setattr(
        "services.webflow_connection.active_webflow_stores", fake_active
    )

    result = await sweep.sweep_all_webflow_stores()

    assert calls == ["good-1", "bad", "good-2"]
    assert result["status"] == "partial_failure"
    assert result["processed"] == 2
    assert result["failed"] == 1
    assert result["accepted"] == 2


# ---- the ordering claim applies to the `orders` lane ALONE -------------------


async def test_the_money_out_lanes_are_never_armed_and_never_judged(monkeypatch):
    """Their anchor is not the field the list is sorted by.

    Assumption 7 is about `acceptedOn`. A `refunded` lane anchored on
    `refundedOn` reads a list that arrives in `acceptedOn` order, so its anchors
    arrive in essentially arbitrary sequence — and judging them reported a
    "violation" on almost every store on almost every run, which is the same as
    never reporting one and would bury a real `orders`-lane violation.
    """
    refunds = [
        # Deliberately ASCENDING by `refundedOn` while descending by
        # `acceptedOn`: under the old check this is a violation on every run.
        _order("r-1", accepted=NOW - timedelta(days=9), status="refunded",
               refunded=NOW - timedelta(hours=9)),
        _order("r-2", accepted=NOW - timedelta(days=10), status="refunded",
               refunded=NOW - timedelta(hours=1)),
    ]
    client = _Client(
        pages_by_status={
            None: [_order("o-1", accepted=NOW)],
            "refunded": refunds,
            "dispute-lost": [],
        }
    )

    stats, merged, _recorded = await _run(
        monkeypatch,
        client,
        credentials={
            "api_token": "wf-token",
            "site_id": SITE_ID,
            "reconciliation": {
                "refunded": {
                    "cursor": _iso(NOW - timedelta(days=1)),
                    "ordering_verified": True,
                }
            },
        },
    )

    refunded = _lane(stats, "refunded")
    assert refunded["ordering_applicable"] is False
    assert refunded["ordering_verified"] is None, (
        "a money-out lane reported an ordering verdict it cannot earn"
    )
    assert refunded["early_stop_armed"] is False, (
        "a money-out lane armed an early stop on an ordering it does not have — "
        "a stop there skips the rest of the list permanently"
    )
    assert refunded.get("stopped_early") is not True
    # A stale verdict from an earlier build is REMOVED, not merely ignored:
    # left in place it would re-arm the stop the moment this guard regressed.
    assert "ordering_verified" not in merged["reconciliation"]["refunded"]
    # ...and the run no longer claims the store's list is out of order.
    assert stats["ordering_verified"] is True
    assert stats["unordered_lanes"] == []

    # THE POSITIVE COUNTERPART: the `orders` lane IS judged, on the same run.
    orders = _lane(stats, "orders")
    assert orders["ordering_applicable"] is True
    assert orders["ordering_verified"] is True


async def test_only_the_orders_lane_can_report_a_violation(monkeypatch):
    """The counterpart to the test above: a genuinely out-of-order unfiltered
    list still disarms and still gets named, and the money-out lanes do not
    dilute it."""
    client = _Client(
        pages_by_status={
            None: [
                _order("o-1", accepted=NOW - timedelta(hours=5)),
                _order("o-2", accepted=NOW - timedelta(hours=1)),  # NEWER: a violation
            ],
            "refunded": [
                _order("r-1", accepted=NOW - timedelta(days=9), status="refunded",
                       refunded=NOW - timedelta(hours=9)),
                _order("r-2", accepted=NOW - timedelta(days=10), status="refunded",
                       refunded=NOW - timedelta(hours=1)),
            ],
            "dispute-lost": [],
        }
    )

    stats, merged, _recorded = await _run(monkeypatch, client)

    assert _lane(stats, "orders")["ordering_verified"] is False
    assert merged["reconciliation"]["orders"]["ordering_verified"] is False
    assert stats["ordering_verified"] is False
    assert stats["unordered_lanes"] == ["orders"]


async def test_a_run_that_judged_no_lane_reports_no_ordering_verdict(monkeypatch):
    """`--lane refunded` must not answer `ordering_verified: false` and make the
    script's NOTE fire for a run that checked nothing."""
    client = _Client(pages_by_status={"refunded": []})

    stats, _merged, _recorded = await _run(monkeypatch, client, lanes=["refunded"])

    assert stats["ordering_verified"] is None
    assert stats["unordered_lanes"] == []


# ---- the state write merges; it does not overwrite the subtree ---------------


async def test_the_state_write_touches_only_the_lanes_this_run_walked(monkeypatch):
    """A lane this run did not run must survive its write.

    The run reads `reconciliation` once and writes it many network calls later.
    The row lock covers the WRITE, not that whole span, so persisting the
    subtree computed from the original read discards every cursor another
    replica wrote meanwhile.
    """
    client = _Client(pages_by_status={None: [_order("o-1", accepted=NOW)]})

    _stats, merged, _recorded = await _run(
        monkeypatch,
        client,
        lanes=["orders"],
        credentials={
            "api_token": "wf-token",
            "site_id": SITE_ID,
            "reconciliation": {
                "refunded": {"cursor": _iso(NOW - timedelta(days=2))},
                "dispute_lost": {"cursor": _iso(NOW - timedelta(days=3))},
            },
        },
    )

    state = merged["reconciliation"]
    assert state["orders"]["cursor"] == _iso(NOW)
    assert state["refunded"] == {"cursor": _iso(NOW - timedelta(days=2))}
    assert state["dispute_lost"] == {"cursor": _iso(NOW - timedelta(days=3))}


async def test_a_cursor_written_DURING_the_run_is_not_clobbered(monkeypatch):
    """The interleaving itself, not just the shape.

    A second replica persists the `refunded` lane while this run is still
    walking `orders`. The read-modify-write spans that gap, so the only thing
    that saves the other replica's cursor is that this run's final write merges
    into the CURRENT row rather than into the copy it read at the top.
    """
    from services import webflow_order_sweep as sweep

    client = _Client(pages_by_status={None: [_order("o-1", accepted=NOW)]})
    _merged, _recorded, stored = _install(
        monkeypatch,
        credentials={
            "api_token": "wf-token",
            "site_id": SITE_ID,
            "reconciliation": {"refunded": {"cursor": _iso(NOW - timedelta(days=2))}},
        },
    )

    later = _iso(NOW - timedelta(minutes=5))
    original_record = sweep.record_webflow_order

    async def _write_between(**kwargs):
        # The other replica lands its refunded cursor mid-walk.
        stored["reconciliation"] = {
            **stored.get("reconciliation", {}),
            "refunded": {"cursor": later},
        }
        return await original_record(**kwargs)

    monkeypatch.setattr(sweep, "record_webflow_order", _write_between)

    await sweep.sweep_webflow_store(
        store_id=STORE_ID, client=client, now=NOW, lanes=["orders"]
    )

    assert stored["reconciliation"]["refunded"] == {"cursor": later}, (
        "this run's write discarded a cursor another writer landed while it was "
        "walking Webflow"
    )
    assert stored["reconciliation"]["orders"]["cursor"] == _iso(NOW)


# ---- an unreadable refund is counted, not dropped ---------------------------


async def test_an_unreadable_refund_amount_is_counted_under_its_own_name(monkeypatch):
    """The order still lands; the missing refund row is visible in the run.

    Raising instead dropped the whole batch, so the sweep counted the order as
    `invalid` and the purchase never reached the ledger at all — a strictly
    worse trade than under-reporting money out and saying so.
    """
    from services.webflow_ledger import WebflowIngestResult

    def _result(kwargs):
        if kwargs["order"]["orderId"] == "r-1":
            return WebflowIngestResult(
                status="recorded",
                accepted=1,
                ignored_reasons=("refund_amount_unreadable: ...",),
            )
        return WebflowIngestResult(status="recorded", accepted=2)

    client = _Client(
        pages_by_status={
            None: [_order("o-1", accepted=NOW)],
            "refunded": [
                _order("r-1", accepted=NOW - timedelta(hours=2), status="refunded",
                       refunded=NOW - timedelta(hours=1), value=0)
            ],
            "dispute-lost": [],
        }
    )

    stats, _merged, _recorded = await _run(monkeypatch, client, recorder=_result)

    assert _lane(stats, "refunded")["refunds_unreadable"] == 1
    assert _lane(stats, "orders")["refunds_unreadable"] == 0
    assert stats["refunds_unreadable"] == 1
    # It is NOT counted as invalid: the order itself was recorded.
    assert stats["invalid"] == 0
    assert stats["accepted"] == 3


# ---- the sticky ordering verdict --------------------------------------------


async def test_a_violation_is_NOT_undone_by_two_clean_complete_passes(monkeypatch):
    """"A violation permanently disarms the early stop" was false.

    The verdict used to be recomputed every run: a violation wrote False, and
    the very next COMPLETE pass that happened to see no violation wrote True
    again and re-armed the stop. That made the disarm last exactly one run — and
    an unstable list is not unstable on every pass, so the re-arm is the common
    case, not the rare one. `ordering_violated_at` is therefore persisted and
    sticky, and only an operator removes it.
    """
    unordered = [
        _order("old-0", accepted=NOW - timedelta(days=10)),
        _order("fresh", accepted=NOW),  # newer than the row above: a violation
        _order("old-1", accepted=NOW - timedelta(days=11)),
    ]
    ordered = [
        _order(f"o-{i}", accepted=NOW - timedelta(minutes=i)) for i in range(4)
    ]

    first, merged, _recorded = await _run(
        monkeypatch,
        _Client(pages_by_status={None: unordered}),
        credentials={
            "api_token": "wf-token",
            "site_id": SITE_ID,
            "reconciliation": {
                "orders": {
                    "cursor": _iso(NOW - timedelta(days=1)),
                    "ordering_verified": True,
                }
            },
        },
        page_limit=2,
    )

    assert _lane(first, "orders")["ordering_verified"] is False
    violated_at = merged["reconciliation"]["orders"]["ordering_violated_at"]
    assert violated_at == _iso(NOW)

    state = merged["reconciliation"]
    for run in (1, 2):
        later = NOW + timedelta(days=run)
        stats, merged, _recorded = await _run(
            monkeypatch,
            _Client(pages_by_status={None: ordered}),
            credentials={
                "api_token": "wf-token",
                "site_id": SITE_ID,
                "reconciliation": state,
            },
            now=later,
            page_limit=2,
        )
        lane = _lane(stats, "orders")
        assert lane["early_stop_armed"] is False, (
            f"clean pass {run} re-armed the early stop on a store whose list has "
            "been observed out of order"
        )
        assert lane["ordering_verified"] is False, f"clean pass {run} re-armed the verdict"
        assert "stopped_early" not in lane
        state = merged["reconciliation"]
        assert state["orders"]["ordering_verified"] is False
        # And it is the FIRST violation's time that is kept, not the last run's.
        assert state["orders"]["ordering_violated_at"] == violated_at


async def test_the_run_reports_WHY_the_stop_is_off(monkeypatch):
    """An operator must be able to read the reason off a run rather than out of
    the credential blob."""
    stats, _merged, _recorded = await _run(
        monkeypatch,
        _Client(pages_by_status={None: [_order("o-1", accepted=NOW)]}),
        credentials={
            "api_token": "wf-token",
            "site_id": SITE_ID,
            "reconciliation": {
                "orders": {
                    "cursor": _iso(NOW - timedelta(days=1)),
                    "ordering_verified": True,
                    "ordering_violated_at": "2026-08-01T00:00:00.000Z",
                }
            },
        },
    )

    lane = _lane(stats, "orders")
    assert lane["ordering_violated_at"] == "2026-08-01T00:00:00.000Z"
    assert lane["early_stop_armed"] is False


async def test_a_money_out_lane_never_carries_a_violation_marker(monkeypatch):
    """The money-out lanes are never judged, so a marker left in their state by
    an earlier build is REMOVED rather than left to be read as a verdict."""
    _stats, merged, _recorded = await _run(
        monkeypatch,
        _Client(pages_by_status={None: [], "refunded": [], "dispute-lost": []}),
        credentials={
            "api_token": "wf-token",
            "site_id": SITE_ID,
            "reconciliation": {
                "refunded": {"ordering_violated_at": "2026-08-01T00:00:00.000Z"}
            },
        },
    )

    assert "ordering_violated_at" not in merged["reconciliation"]["refunded"]


# ---- `pending` orders no lane can ever come back for -------------------------


def _mapping_recorder(seen_events):
    """A recorder that maps the order for REAL and reports what it produced.

    The claim being tested is that a completed pending order gets its
    `order.paid` ROW — not merely that some function was called with it — and a
    recorder that returned a canned result would leave that claim untested while
    looking green.
    """
    from services.webflow_ledger import WebflowIngestResult

    def _record(kwargs):
        from services.webflow_event_adapter import map_webflow_order

        mapping = map_webflow_order(
            kwargs["order"],
            store_id=kwargs["store_id"],
            source="webflow_reconciliation",
        )
        types = [event.event_type for event in mapping.batch.events]
        seen_events.append(
            {"order_id": kwargs["order"].get("orderId"), "event_types": types}
        )
        return WebflowIngestResult(status="recorded", accepted=len(types))

    return _record


def _pending_ids(state):
    return [entry["order_id"] for entry in state["reconciliation"]["pending_order_ids"]]


async def test_an_order_that_was_pending_gets_its_paid_row_on_a_LATER_run(monkeypatch):
    """The bug: `acceptedOn` does not move when a payment is captured.

    A PayPal order is `pending` when the lane walks past it; the lane's cursor
    then advances past its `acceptedOn` and NEVER comes back, so the order sits
    in the ledger with `order.created` and no money, permanently. The webhook
    would normally carry the transition — but the sweep exists precisely for the
    store whose webhooks are unprovisioned or whose deliveries were dropped.
    """
    pending = _order("p-1", accepted=NOW - timedelta(days=2), status="pending")
    fresh = _order("fresh", accepted=NOW)
    events = []

    first, merged, _recorded = await _run(
        monkeypatch,
        _Client(pages_by_status={None: [fresh, pending]}),
        recorder=_mapping_recorder(events),
    )

    # Run 1: the order is pending, so it is created and NOT paid — and it is
    # remembered, because nothing else ever will come back for it.
    assert {"order_id": "p-1", "event_types": ["order.created"]} in events
    assert _pending_ids(merged) == ["p-1"]
    assert merged["reconciliation"]["orders"]["cursor"] == _iso(NOW)

    # Run 2: the payment was captured. `acceptedOn` is unchanged and now sits
    # below the cursor, so the LANE can only skip it.
    captured = _order("p-1", accepted=NOW - timedelta(days=2), status="unfulfilled")
    events.clear()
    second_client = _Client(
        pages_by_status={None: [fresh, captured]}, orders_by_id={"p-1": captured}
    )
    second, merged_2, _recorded_2 = await _run(
        monkeypatch,
        second_client,
        credentials={
            "api_token": "wf-token",
            "site_id": SITE_ID,
            "reconciliation": merged["reconciliation"],
        },
        recorder=_mapping_recorder(events),
    )

    assert _lane(second, "orders")["skipped_already_recorded"] == 1, (
        "the lane no longer skips this order, so this test is not exercising "
        "the gap the replay exists to close"
    )
    # The replay read it BY ID and recorded it.
    assert [call["order_id"] for call in second_client.calls if call["kind"] == "order"] == ["p-1"]
    assert {"order_id": "p-1", "event_types": ["order.created", "order.paid"]} in events
    assert second["pending"]["completed"] == 1
    assert second["accepted"] >= 2, "the replay's rows are missing from the run totals"
    # ...and it is dropped, so run 3 costs nothing.
    assert "pending_order_ids" not in merged_2["reconciliation"]


async def test_a_pending_order_completed_by_a_WEBHOOK_is_dropped_on_the_next_run(
    monkeypatch,
):
    """The common case: the webhook delivered the transition and the ledger
    already holds the row. The replay must still notice, and must let the id go
    rather than re-reading it forever."""
    captured = _order("p-1", accepted=NOW - timedelta(days=2), status="unfulfilled")
    events = []
    client = _Client(pages_by_status={None: []}, orders_by_id={"p-1": captured})

    stats, merged, _recorded = await _run(
        monkeypatch,
        client,
        credentials={
            "api_token": "wf-token",
            "site_id": SITE_ID,
            "reconciliation": {"pending_order_ids": [{"order_id": "p-1", "misses": 0}]},
        },
        recorder=_mapping_recorder(events),
    )

    assert stats["pending"]["completed"] == 1
    assert "pending_order_ids" not in merged["reconciliation"]


async def test_an_order_still_pending_is_kept_and_recorded_by_nobody(monkeypatch):
    still = _order("p-1", accepted=NOW - timedelta(days=2), status="pending")
    events = []

    stats, merged, _recorded = await _run(
        monkeypatch,
        _Client(pages_by_status={None: []}, orders_by_id={"p-1": still}),
        credentials={
            "api_token": "wf-token",
            "site_id": SITE_ID,
            "reconciliation": {"pending_order_ids": [{"order_id": "p-1", "misses": 2}]},
        },
        recorder=_mapping_recorder(events),
    )

    assert stats["pending"]["still_pending"] == 1
    assert events == []
    assert _pending_ids(merged) == ["p-1"]
    # A successful READ resets the miss counter: it counts un-readability, and
    # this order was perfectly readable.
    assert merged["reconciliation"]["pending_order_ids"][0]["misses"] == 0


async def test_an_id_that_404s_is_dropped_only_after_N_runs(monkeypatch):
    """One 404 is usually the read racing Webflow. Three across three runs is
    an order that is not coming back — and dropping it is money nobody will
    reconcile, so it is counted and logged rather than forgotten."""
    from services.webflow_order_sweep import PENDING_ORDER_MAX_MISSES

    state = {"pending_order_ids": [{"order_id": "gone", "misses": 0}]}
    for attempt in range(1, PENDING_ORDER_MAX_MISSES):
        stats, merged, _recorded = await _run(
            monkeypatch,
            _Client(pages_by_status={None: []}),
            credentials={
                "api_token": "wf-token",
                "site_id": SITE_ID,
                "reconciliation": state,
            },
        )
        assert stats["pending"]["dropped_not_found"] == 0, attempt
        assert _pending_ids(merged) == ["gone"], attempt
        state = merged["reconciliation"]
        assert state["pending_order_ids"][0]["misses"] == attempt

    stats, merged, _recorded = await _run(
        monkeypatch,
        _Client(pages_by_status={None: []}),
        credentials={
            "api_token": "wf-token",
            "site_id": SITE_ID,
            "reconciliation": state,
        },
    )

    assert stats["pending"]["dropped_not_found"] == 1
    assert "pending_order_ids" not in merged["reconciliation"]


async def test_a_transport_failure_keeps_the_id_without_burning_a_miss(monkeypatch):
    """A 429 or a 500 says nothing about the ORDER. Counting it as a miss would
    let a bad afternoon at Webflow expire a store's whole pending set."""
    client = _Client(pages_by_status={None: []}, orders_by_id={"p-1": 429})

    stats, merged, _recorded = await _run(
        monkeypatch,
        client,
        credentials={
            "api_token": "wf-token",
            "site_id": SITE_ID,
            "reconciliation": {"pending_order_ids": [{"order_id": "p-1", "misses": 1}]},
        },
    )

    assert stats["pending"]["fetch_failures"] == 1
    assert merged["reconciliation"]["pending_order_ids"] == [
        {"order_id": "p-1", "misses": 1}
    ]


async def test_the_tracked_set_is_BOUNDED_and_names_what_it_drops(monkeypatch, caplog):
    """It lives in one database cell. An unbounded list of ids in a cell is a
    slow-motion outage, and a silent truncation is money silently dropped."""
    import logging

    from services.webflow_order_sweep import PENDING_ORDER_ID_CAP

    already = PENDING_ORDER_ID_CAP - 20
    new_pending = [
        _order(f"new-{i}", accepted=NOW - timedelta(minutes=i), status="pending")
        for i in range(40)
    ]

    with caplog.at_level(logging.WARNING, logger="webflow_order_sweep"):
        stats, merged, _recorded = await _run(
            monkeypatch,
            _Client(pages_by_status={None: new_pending}),
            credentials={
                "api_token": "wf-token",
                "site_id": SITE_ID,
                "reconciliation": {
                    "pending_order_ids": [
                        {"order_id": f"old-{i}", "misses": 0} for i in range(already)
                    ]
                },
            },
        )

    ids = _pending_ids(merged)
    assert len(ids) == PENDING_ORDER_ID_CAP
    # The OLDEST went, and the newly observed ones are all there.
    assert "old-0" not in ids and "old-19" not in ids
    assert "old-20" in ids
    assert [f"new-{i}" for i in range(40)] == ids[-40:]
    dropped_lines = [
        record.getMessage()
        for record in caplog.records
        if "dropped" in record.getMessage() and "cap" in record.getMessage()
    ]
    assert dropped_lines, "the cap dropped ids silently"
    assert "old-0" in dropped_lines[0] and "old-19" in dropped_lines[0]
    assert stats["pending"]["tracked"] == already


async def test_a_pending_id_is_tracked_even_when_the_lane_SKIPS_the_order(monkeypatch):
    """The ordering of the two checks inside the lane, pinned.

    An order that went `pending` before the cursor is exactly the one the set
    exists for. Collecting ids after the `skipped_already_recorded` branch would
    track only the orders that did not need tracking.
    """
    old_pending = _order("p-old", accepted=NOW - timedelta(days=30), status="pending")

    stats, merged, _recorded = await _run(
        monkeypatch,
        _Client(pages_by_status={None: [old_pending]}),
        credentials={
            "api_token": "wf-token",
            "site_id": SITE_ID,
            "reconciliation": {"orders": {"cursor": _iso(NOW - timedelta(days=1))}},
        },
    )

    assert _lane(stats, "orders")["skipped_already_recorded"] == 1
    assert _pending_ids(merged) == ["p-old"]


async def test_a_dry_run_reads_the_pending_set_and_changes_nothing(monkeypatch):
    captured = _order("p-1", accepted=NOW - timedelta(days=2), status="unfulfilled")
    events = []

    stats, merged, _recorded = await _run(
        monkeypatch,
        _Client(pages_by_status={None: []}, orders_by_id={"p-1": captured}),
        credentials={
            "api_token": "wf-token",
            "site_id": SITE_ID,
            "reconciliation": {"pending_order_ids": [{"order_id": "p-1", "misses": 0}]},
        },
        recorder=_mapping_recorder(events),
        apply=False,
    )

    assert stats["pending"]["refetched"] == 1
    assert stats["pending"]["completed"] == 0
    assert events == []
    assert merged == {}, "a dry run wrote state"


# ---- a refused order is a FAILED run ----------------------------------------


async def test_a_money_shape_this_bridge_refuses_makes_the_run_partial(monkeypatch):
    """`invalid` used to be a number in the JSON and nothing else.

    `WebflowMoneyFormatError` is what a changed money shape looks like — the one
    thing this integration refuses rather than guesses at — and a store whose
    every order tripped it reported `status: success` with `accepted: 0`, which
    is indistinguishable from a quiet store.
    """
    from services.webflow_event_adapter import WebflowMoneyFormatError

    def _refuse(_kwargs):
        raise WebflowMoneyFormatError("Webflow customerPaid disagrees with itself")

    stats, _merged, _recorded = await _run(
        monkeypatch,
        _Client(
            pages_by_status={
                None: [
                    _order("bad-1", accepted=NOW),
                    _order("bad-2", accepted=NOW - timedelta(minutes=1)),
                ]
            }
        ),
        recorder=_refuse,
    )

    assert stats["invalid"] == 2
    assert stats["accepted"] == 0
    assert stats["status"] == "partial_failure"
    # And the ids, so the NOTE has somewhere to point.
    assert stats["invalid_order_ids"] == ["bad-1", "bad-2"]


async def test_a_clean_run_is_still_a_success(monkeypatch):
    """The counterpart. A status that were always partial would be ignored."""
    stats, _merged, _recorded = await _run(
        monkeypatch, _Client(pages_by_status={None: [_order("o-1", accepted=NOW)]})
    )

    assert stats["invalid"] == 0
    assert stats["status"] == "success"
    assert stats["invalid_order_ids"] == []


async def test_one_stores_refusal_makes_the_ALL_STORES_run_partial(monkeypatch):
    """The roll-up read `lane_failures` alone, so a store that refused every
    order rolled up green."""
    from services import webflow_order_sweep as sweep

    async def fake_store_sweep(*, store_id, **kwargs):
        return {
            "status": "partial_failure",
            "store_id": store_id,
            "invalid": 3,
            "lane_failures": [],
        }

    async def fake_active():
        return [{"store_id": "store-a"}]

    monkeypatch.setattr(sweep, "sweep_webflow_store", fake_store_sweep)
    monkeypatch.setattr(
        "services.webflow_connection.active_webflow_stores", fake_active
    )

    result = await sweep.sweep_all_webflow_stores()

    assert result["invalid"] == 3
    assert result["status"] == "partial_failure"


# ---- the script's exit code -------------------------------------------------
#
# A scheduled run is only as loud as its exit code. These drive `main()` — the
# real argument parsing, the real NOTE printing, the real return value — over a
# stubbed sweep result, because what is under test is the reporting rather than
# the sweep.


class _FakeDb:
    is_connected = False

    def __init__(self):
        self.connected = 0

    async def connect(self):
        self.connected += 1

    async def disconnect(self):
        return None


def _drive_script(monkeypatch, result, argv=("prog", "--apply")):
    import sys

    from scripts import sweep_webflow_orders as script

    async def fake_sweep(**kwargs):
        return result

    monkeypatch.setattr(script, "database", _FakeDb())
    monkeypatch.setattr(script, "sweep_all_webflow_stores", fake_sweep)
    monkeypatch.setattr(sys, "argv", list(argv))
    return script.main()


def test_the_script_exits_non_zero_when_orders_were_REFUSED(monkeypatch, capsys):
    """`invalid > 0` is a failed run, and a failed run must be visibly red.

    Before this, a store whose money shape had changed exited 0 with
    `accepted: 0` — a green cron job recording nothing.
    """
    exit_code = _drive_script(
        monkeypatch,
        {
            "status": "partial_failure",
            "dry_run": False,
            "processed": 1,
            "invalid": 2,
            "stores": [
                {
                    "store_id": "store-wf",
                    "status": "partial_failure",
                    "invalid": 2,
                    "invalid_order_ids": ["bad-1", "bad-2"],
                }
            ],
        },
    )

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out
    assert "store-wf (2: bad-1, bad-2)" in out


def test_the_script_exits_zero_on_a_clean_run(monkeypatch, capsys):
    """The contrast. Without it, an exit code that is always 1 proves nothing."""
    exit_code = _drive_script(
        monkeypatch,
        {
            "status": "success",
            "dry_run": False,
            "processed": 1,
            "invalid": 0,
            "stores": [{"store_id": "store-wf", "status": "success", "invalid": 0}],
        },
    )

    assert exit_code == 0
    assert "REFUSED" not in capsys.readouterr().out


def test_the_script_names_pending_ids_it_dropped(monkeypatch, capsys):
    exit_code = _drive_script(
        monkeypatch,
        {
            "status": "success",
            "dry_run": False,
            "processed": 1,
            "invalid": 0,
            "stores": [
                {
                    "store_id": "store-wf",
                    "status": "success",
                    "invalid": 0,
                    "pending": {"dropped_not_found": 4},
                }
            ],
        },
    )

    assert exit_code == 0
    assert "tracked `pending` orders dropped" in capsys.readouterr().out


async def test_a_garbage_pending_id_is_dropped_rather_than_retried_forever(monkeypatch):
    """The state is hand-editable (that is the documented way to clear an
    `ordering_violated_at`), so it can hold anything.

    An id the fetch's allowlist would refuse can never become valid, so keeping
    it means a slot in a bounded set and a WARNING every run, forever.
    """
    client = _Client(pages_by_status={None: []})

    stats, merged, _recorded = await _run(
        monkeypatch,
        client,
        credentials={
            "api_token": "wf-token",
            "site_id": SITE_ID,
            "reconciliation": {
                "pending_order_ids": [
                    {"order_id": "../../token/introspect", "misses": 0},
                    "0000-0007",
                ]
            },
        },
    )

    assert stats["pending"]["tracked"] == 1
    # It never reached the wire, and only the real id is carried forward.
    assert [call["order_id"] for call in client.calls if call["kind"] == "order"] == [
        "0000-0007"
    ]
    assert _pending_ids(merged) == ["0000-0007"]
