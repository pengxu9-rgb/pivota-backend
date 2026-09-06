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
                 site_status=200):
        self.orders_by_status = pages_by_status or {}
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
    merged = {}

    async def fake_find(store_id):
        return {
            "store_id": STORE_ID,
            "merchant_id": MERCHANT_ID,
            "domain": "shop.webflow.io",
            "api_key": json.dumps(blob),
        }

    async def fake_merge(*, store_id, updates=None, **kwargs):
        merged.update(updates or {})
        return {**blob, **(updates or {})}

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
    return merged, recorded


async def _run(monkeypatch, client, *, credentials=None, recorder=None, **kwargs):
    from services.webflow_order_sweep import sweep_webflow_store

    merged, recorded = _install(monkeypatch, credentials=credentials, recorder=recorder)
    stats = await sweep_webflow_store(
        store_id=STORE_ID, client=client, now=NOW, **kwargs
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
