"""Stamping the recovery key onto the link an agent follows.

This is the middle hop of the Prove join. The key is written on the task; the
interaction row can receive it; this is what carries it between them.

TWO CARRIERS, deliberately parallel to the click id's, because they survive
different distances: the Shopify cart attribute rides onto the ORDER, and the
referral param reaches the LANDING. Neither replaces the click id — an order
is attributable through `click_id -> interaction row -> recovery_key` even
where only the click id survives, so the key stamped here is a shortcut, not
the only path.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlsplit

import pytest

import db.merchant_tasks as mt
from services.outbound_links_service import (
    REFERRAL_CLICK_PARAM,
    REFERRAL_RECOVERY_PARAM,
    SHOPIFY_CART_RECOVERY_ATTRIBUTE,
    append_referral_click_param,
    append_shopify_cart_click_attribute,
)


def _params(url: str) -> Dict[str, List[str]]:
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


# ---- the carriers ----------------------------------------------------------

def test_the_referral_link_carries_both_ids():
    url = append_referral_click_param("https://anua.com/p/1", "c-1", "rk_abc")
    p = _params(url)
    assert p[REFERRAL_CLICK_PARAM] == ["c-1"]
    assert p[REFERRAL_RECOVERY_PARAM] == ["rk_abc"]


def test_the_recovery_key_never_takes_over_utm_content():
    """utm_content is the CLICK ID's order-side join on WooCommerce — core
    Order Attribution persists utm_* onto the order. Overwriting it would
    trade a working join for a new one, and the key is recoverable order-side
    anyway through click_id -> interaction row."""
    url = append_referral_click_param("https://anua.com/p/1", "c-1", "rk_abc")
    assert _params(url)["utm_content"] == ["c-1"]


def test_a_merchant_configured_utm_content_still_wins():
    url = append_referral_click_param(
        "https://anua.com/p/1?utm_content=theirs", "c-1", "rk_abc")
    assert _params(url)["utm_content"] == ["theirs"]
    assert _params(url)[REFERRAL_RECOVERY_PARAM] == ["rk_abc"]


def test_the_shopify_cart_carries_both_as_order_surviving_attributes():
    url = append_shopify_cart_click_attribute(
        "https://anua.com/cart/123:1", "c-1", "rk_abc")
    # Brackets stay literal per Shopify's cart-permalink form, so assert on the
    # raw string rather than through a parser that would normalise them.
    assert "attributes[pivota_click_id]=c-1" in url
    assert f"{SHOPIFY_CART_RECOVERY_ATTRIBUTE}=rk_abc" in url


def test_no_key_leaves_the_link_exactly_as_before():
    """The pre-existing behaviour is the default. Every destination minted
    without a recovery key must be byte-identical to what shipped before."""
    assert append_referral_click_param("https://anua.com/p/1", "c-1") == (
        "https://anua.com/p/1?pvt_click_id=c-1&utm_content=c-1"
    )
    assert append_shopify_cart_click_attribute(
        "https://anua.com/cart/123:1", "c-1"
    ) == "https://anua.com/cart/123:1?attributes[pivota_click_id]=c-1"


def test_an_empty_or_blank_key_is_not_stamped():
    for blank in (None, "", "   "):
        url = append_referral_click_param("https://anua.com/p/1", "c-1", blank)
        assert REFERRAL_RECOVERY_PARAM not in url


def test_the_key_is_url_encoded():
    url = append_referral_click_param("https://anua.com/p", "c-1", "rk a&b=c")
    assert "rk%20a%26b%3Dc" in url
    assert _params(url)[REFERRAL_RECOVERY_PARAM] == ["rk a&b=c"]


# ---- resolving WHICH key a destination belongs to ---------------------------

@pytest.fixture()
def tasks(monkeypatch):
    state: Dict[str, Any] = {"rows": []}

    class _DB:
        async def fetch_all(self, *a, **k):
            return state["rows"]

    monkeypatch.setattr(mt, "database", _DB())
    monkeypatch.setattr(mt, "_DDL_READY", True)
    return state


def _task(key: str, *, product=None, host=None):
    return {"recovery_key": key,
            "evidence_jsonb": {"product_key": product, "target_host": host}}


async def test_a_product_match_resolves_the_key(tasks):
    tasks["rows"] = [_task("rk_1", product="sku-1", host="anua.com")]
    assert await mt.active_recovery_key_for_destination(
        merchant_id="m-1", product_key="sku-1", target_host="anua.com",
    ) == "rk_1"


async def test_a_product_match_beats_a_brand_level_task(tasks):
    """A brand-level task must not steal attribution from the per-product one
    that actually describes this destination."""
    tasks["rows"] = [
        _task("rk_brand", product=None, host="anua.com"),
        _task("rk_product", product="sku-1", host="anua.com"),
    ]
    assert await mt.active_recovery_key_for_destination(
        merchant_id="m-1", product_key="sku-1", target_host="anua.com",
    ) == "rk_product"


async def test_a_host_match_is_used_only_when_there_is_no_product(tasks):
    tasks["rows"] = [_task("rk_brand", product=None, host="anua.com")]
    assert await mt.active_recovery_key_for_destination(
        merchant_id="m-1", target_host="anua.com") == "rk_brand"
    # ...but a product the tasks do not mention resolves to nothing rather
    # than falling back to the brand task.
    assert await mt.active_recovery_key_for_destination(
        merchant_id="m-1", product_key="sku-9", target_host="anua.com") is None


def test_the_FIRST_matching_brand_task_wins():
    """Rows arrive `updated_at DESC`, so first-wins means the most recently
    touched task. Last-wins would silently hand attribution to the stalest
    open task on the host."""
    rows = [
        _task("rk_newest", product=None, host="anua.com"),
        _task("rk_older", product=None, host="anua.com"),
    ]
    assert mt.match_recovery_key(rows, target_host="anua.com") == "rk_newest"


async def test_a_different_destination_resolves_to_nothing(tasks):
    tasks["rows"] = [_task("rk_1", product="sku-1", host="anua.com")]
    assert await mt.active_recovery_key_for_destination(
        merchant_id="m-1", product_key="sku-2", target_host="other.com") is None


async def test_it_never_raises_into_a_link(tasks, monkeypatch):
    """A link that carries no key is still attributable through
    click_id -> interaction row. A link that FAILS is a lost sale."""
    class _Boom:
        async def fetch_all(self, *a, **k):
            raise RuntimeError("pool exhausted")

    monkeypatch.setattr(mt, "database", _Boom())
    assert await mt.active_recovery_key_for_destination(
        merchant_id="m-1", product_key="sku-1") is None


async def test_a_caller_with_nothing_to_match_on_does_not_query(tasks, monkeypatch):
    """No product and no host cannot identify a destination, so the lookup
    must short-circuit rather than pick an arbitrary open task."""
    called = {"n": 0}

    class _Counting:
        async def fetch_all(self, *a, **k):
            called["n"] += 1
            return []

    monkeypatch.setattr(mt, "database", _Counting())
    assert await mt.active_recovery_key_for_destination(merchant_id="m-1") is None
    assert await mt.active_recovery_key_for_destination(
        merchant_id="", product_key="sku-1") is None
    assert called["n"] == 0


# ---- one query per resolve, not one per variant -----------------------------

async def test_the_matcher_is_pure_and_issues_no_query(tasks, monkeypatch):
    """The reason fetch and match are separate. The offer-resolve path walks
    every matched variant; a lookup inside that loop is a query per variant on
    an agent-facing path. Fetch once above the loop, match in memory."""
    called = {"n": 0}

    class _Counting:
        async def fetch_all(self, *a, **k):
            called["n"] += 1
            return []

    monkeypatch.setattr(mt, "database", _Counting())
    rows = [_task("rk_1", product="sku-1", host="anua.com")]
    for _ in range(25):
        assert mt.match_recovery_key(rows, product_key="sku-1") == "rk_1"
    assert called["n"] == 0, "matching must not touch the database"


async def test_the_fetch_runs_once_for_many_destinations(tasks):
    """The composed shape the call site uses: one fetch, then N matches."""
    tasks["rows"] = [
        _task("rk_a", product="sku-1", host="anua.com"),
        _task("rk_b", product="sku-2", host="anua.com"),
    ]
    open_tasks = await mt.list_open_recovery_tasks(merchant_id="m-1")
    assert mt.match_recovery_key(open_tasks, product_key="sku-1") == "rk_a"
    assert mt.match_recovery_key(open_tasks, product_key="sku-2") == "rk_b"
    assert mt.match_recovery_key(open_tasks, product_key="sku-9") is None


async def test_the_fetch_fails_soft_to_an_empty_list(monkeypatch):
    class _Boom:
        async def fetch_all(self, *a, **k):
            raise RuntimeError("pool exhausted")

    monkeypatch.setattr(mt, "database", _Boom())
    monkeypatch.setattr(mt, "_DDL_READY", True)
    assert await mt.list_open_recovery_tasks(merchant_id="m-1") == []


def test_the_query_actually_filters_on_merchant_status_and_limit():
    """The WHERE clause, observed.

    The fixture above mocks fetch_all, so every filter is invisible to it —
    dropping the merchant_id filter, both status filters, or the LIMIT all
    survived the suite. This compiles the statement and reads the SQL, which
    is the cheapest thing that can see them at all.
    """
    from sqlalchemy.dialects import postgresql
    import inspect

    src = inspect.getsource(mt.list_open_recovery_tasks)
    # The four properties, asserted on the source of the one statement:
    assert "merchant_tasks.c.merchant_id == merchant_id" in src, (
        "without this one merchant's key can be stamped on another's link"
    )
    assert "recovery_key.isnot(None)" in src
    assert "status.notin_(sorted(TERMINAL_STATUSES))" in src, (
        "a closed fix must stop collecting credit for new orders"
    )
    assert 'status != "superseded"' in src
    assert ".limit(50)" in src, "an unbounded read on an agent path"


def test_the_call_site_derives_its_merchant_from_the_ROW():
    """The bug this replaces a brittle text-order check with.

    The fetch was hoisted above `for v in matched_variants`, but read
    `redirect_identity` — a name bound INSIDE that loop. First seed:
    UnboundLocalError, swallowed by the except, so the feature silently never
    ran. Later seeds: the PREVIOUS seed's identity, stamping one merchant's
    recovery key onto another merchant's link.

    So the property is not "the fetch is above the loop" (the broken code
    satisfied that) but "the fetch does not depend on a loop-local".
    """
    import inspect
    import routes.agent_shop_gateway as gw

    src = inspect.getsource(gw._handle_offers_resolve)
    block = src[src.index("# ONCE per seed, not once per variant"):
                src.index("for v in matched_variants:")]
    assert "_external_seed_redirect_identity(" in block, (
        "the identity must be derived here, from the row"
    )
    assert "redirect_identity.get(" not in block, (
        "reading the loop-local identity is the UnboundLocalError bug"
    )
    assert "await asyncio.wait_for(" in block, (
        "the read needs the same budget its neighbours have"
    )
    # ...and no query may run inside the loop.
    after = src[src.index("for v in matched_variants:"):]
    assert "await list_open_recovery_tasks" not in after
    assert "match_recovery_key(" in after, "the in-loop call is the pure matcher"

    # And the RESULT must reach the composer. Passing `recovery_key=None`, or
    # matching against a literal `[]`, both left every link unstamped while
    # the whole suite stayed green — the delivering line, unasserted.
    assert "recovery_key=_recovery_key," in after, (
        "the resolved key must be handed to compose_attributed_destinations"
    )
    matcher_call = after[after.index("match_recovery_key("):]
    assert "_open_recovery_tasks," in matcher_call[:200], (
        "the matcher must read the list fetched above, not an empty literal"
    )
