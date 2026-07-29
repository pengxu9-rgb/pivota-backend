"""Quarantined-source exclusion on the find_products connected/seed lane.

On 2026-07-28 the public UNAUTHENTICATED ACP feed served seven Mintree rows
priced 847-3927.70 and labelled "USD". Mintree is an Indian store: rupee prices
published as US dollars. Measured the same day -- canonical index feed 0 rows,
sitemap 0, PDP 404s, **connected lane 20**. The lane reads none of the gates.

Three properties this file holds, in descending order of how easy they are to
lose:

1. The gate is WIRED into the SERVED slate. A first review round found a mutant
   that switched the gate on `emit_decision_event` -- so it ran on the
   *intermediate* slates and not the served ones -- and the suite stayed green,
   because every test drove the intermediate configuration. `_run_served`
   below exists for that reason and is the default everywhere.
2. The gate reads the source that has a WRITER. The first implementation read
   `catalog_offers.suppression_reason`, which nothing in the repo writes and
   which is NULL-keyed on precisely the mirror rows that leak. `catalog_source_
   quarantine` is what actually blocks these stores elsewhere.
3. The matching rule is NOT restated here. It is delegated to
   `source_quarantine.quarantine_matches_source`, so state, expiry and the
   exact-domain comparison cannot drift from the module that owns them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest

from services.offer_currency_policy import (
    filter_out_quarantined_rows,
    get_quarantined_sources,
    is_quarantined_row,
    product_hosts,
    reset_cache,
    url_host,
)
from services.source_quarantine import MATCH_TYPE_DOMAIN, Quarantine


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_cache()
    yield
    reset_cache()


def _q(
    match_value: str = "mintree.us",
    *,
    match_type: str = MATCH_TYPE_DOMAIN,
    state: str = "active",
    expires_at=None,
) -> Quarantine:
    return Quarantine(
        quarantine_id=1,
        match_type=match_type,
        match_value=match_value,
        state=state,
        reason="currency mismatch: stamped USD but storefront is INR",
        expires_at=expires_at,
        created_by="audit_offer_currency",
        created_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
        revoked_at=None,
        revoked_by=None,
        metadata=None,
    )


def _mintree_row(**over: Any) -> Dict[str, Any]:
    row = {
        "id": "ext_fb756aa379b28bd89a",
        "merchant_id": None,  # why the sibling rig filter cannot see this row
        "brand": "Mintree",
        "price": 1058,
        "currency": "USD",  # the defect: these are rupees
        "external_destination_url": "https://mintree.us/products/kumkumadi-oil",
    }
    row.update(over)
    return row


# ---------------------------------------------------------------------------
# host extraction -- the one normalisation this module owns
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("mintree.us", "mintree.us"),
        ("MINTREE.US", "mintree.us"),
        ("www.mintree.us", "mintree.us"),
        ("https://www.mintree.us/products/foo?x=1", "mintree.us"),
        ("http://mintree.us:8443/a/b", "mintree.us"),
        ("mintree.us/products/foo", "mintree.us"),
        ("  mintree.us.  ", "mintree.us"),
        ("", ""),
        (None, ""),
    ],
)
def test_url_host(raw, expected):
    assert url_host(raw) == expected


@pytest.mark.parametrize(
    "field",
    [
        "external_destination_url",
        "canonical_url",
        "destination_url",
        "online_store_url",
        "product_url",
        "url",
        "source_domain",
        "domain",
    ],
)
def test_every_declared_url_field_is_actually_read(field):
    """Each lane populates a different field.

    The external-seed projection emits `external_destination_url`, the pivot
    lane `canonical_url`, and `_standard_to_shop_product` `online_store_url`
    only when the sync captured one. A field in the list that is not really
    read is a lane that silently passes.
    """
    row = {field: "https://mintree.us/p/1"}
    assert "mintree.us" in product_hosts(row)
    assert is_quarantined_row(row, [_q()]) is True


# ---------------------------------------------------------------------------
# the source -- catalog_source_quarantine, which has a writer
# ---------------------------------------------------------------------------

class _FakeDB:
    def __init__(self, rows: Any = None, raises: bool = False):
        self._rows = rows if rows is not None else []
        self._raises = raises
        self.queries: List[str] = []

    async def fetch_all(self, query, values=None):
        self.queries.append(str(query))
        if self._raises:
            raise RuntimeError("db down")
        return self._rows


def _row(match_value: str = "mintree.us", state: str = "active", expires_at=None):
    return {
        "quarantine_id": 1,
        "match_type": MATCH_TYPE_DOMAIN,
        "match_value": match_value,
        "state": state,
        "reason": "currency mismatch",
        "expires_at": expires_at,
        "created_by": "audit_offer_currency",
        "created_at": datetime(2026, 7, 22, tzinfo=timezone.utc),
        "revoked_at": None,
        "revoked_by": None,
        "metadata": None,
    }


@pytest.mark.asyncio
async def test_reads_catalog_source_quarantine_not_catalog_offers():
    """Pins WHICH source. The first implementation read a column nothing writes."""
    db = _FakeDB([_row()])
    got = await get_quarantined_sources(db)
    assert [q.match_value for q in got] == ["mintree.us"]

    sql = db.queries[0].lower()
    assert "catalog_source_quarantine" in sql
    assert "catalog_offers" not in sql, (
        "reading catalog_offers.suppression_reason again: nothing writes that "
        "value, and source_domain is NULL on the mirror rows that leak"
    )


@pytest.mark.asyncio
async def test_the_resolver_returns_EVERY_quarantine_not_just_the_first():
    """Prod holds 16 active quarantines. Everything else here uses one.

    A mutant truncating the cached list to `[:1]` survived a suite that already
    had a multi-quarantine test — because that test called the FILTER directly
    with a hand-built list and never went through the resolver, which is where
    the truncation lived. The layer under test has to be the mutated one.
    """
    db = _FakeDB([_row("mintree.us"), _row("reddane.co.za"), _row("bijin-shop.com")])
    got = await get_quarantined_sources(db)
    assert sorted(q.match_value for q in got) == [
        "bijin-shop.com",
        "mintree.us",
        "reddane.co.za",
    ]

    # ...and the cached read must return all of them too, not just the first.
    again = await get_quarantined_sources(db)
    assert len(again) == 3, "the cache truncated the set"


@pytest.mark.asyncio
async def test_a_failed_resolve_is_not_cached_as_empty():
    """One transient blip must not disable the gate for a full TTL.

    Executed against the first implementation, this is what happened:
      t=1000 db error -> cached set()
      t=1001..1299    -> set(), db healthy, no retry, Mintree served
    The empty set was indistinguishable from "nothing is quarantined".
    """
    healthy = _FakeDB([_row()])
    assert len(await get_quarantined_sources(healthy, now=1000.0)) == 1

    reset_cache()
    broken = _FakeDB(raises=True)
    assert await get_quarantined_sources(broken, now=2000.0) == []
    # ...and the NEXT call re-resolves rather than serving a memoized failure.
    assert len(await get_quarantined_sources(healthy, now=2001.0)) == 1


@pytest.mark.asyncio
async def test_a_failed_resolve_keeps_the_previous_value():
    db_ok = _FakeDB([_row()])
    assert len(await get_quarantined_sources(db_ok, now=1000.0)) == 1
    db_bad = _FakeDB(raises=True)
    # TTL expired, resolve fails -> degrade to the last known set, not to none.
    assert len(await get_quarantined_sources(db_bad, now=1400.0)) == 1


@pytest.mark.asyncio
async def test_caches_within_ttl():
    db = _FakeDB([_row()])
    await get_quarantined_sources(db, now=1000.0)
    await get_quarantined_sources(db, now=1100.0)
    assert len(db.queries) == 1
    await get_quarantined_sources(db, now=1301.0)
    assert len(db.queries) == 2


@pytest.mark.asyncio
async def test_none_db_returns_empty():
    assert await get_quarantined_sources(None) == []


# ---------------------------------------------------------------------------
# matching is DELEGATED -- state and expiry come from source_quarantine
# ---------------------------------------------------------------------------

def test_the_live_defect_row_is_dropped():
    assert is_quarantined_row(_mintree_row(), [_q()]) is True


def test_a_real_usd_store_is_kept():
    row = _mintree_row(brand="Beplain", external_destination_url="https://beplain.com/p/1")
    assert is_quarantined_row(row, [_q()]) is False


def test_revoked_quarantine_does_not_block():
    """Delegated state handling — a revoked quarantine must release the store."""
    assert is_quarantined_row(_mintree_row(), [_q(state="revoked")]) is False


def test_expired_quarantine_does_not_block():
    past = datetime.now(timezone.utc) - timedelta(days=1)
    assert is_quarantined_row(_mintree_row(), [_q(expires_at=past)]) is False


def test_domain_match_is_exact_not_suffix():
    """source_quarantine's rule is exact. This module must not widen it.

    A suffix rule would lift a per-offer signal to every subdomain of a store,
    and the underlying currency-mismatch heuristic has known false positives
    (a Shopify Markets store selling to the US in USD looks identical).
    """
    assert is_quarantined_row(
        _mintree_row(external_destination_url="https://shop.mintree.us/p/1"), [_q()]
    ) is False
    assert is_quarantined_row(
        _mintree_row(external_destination_url="https://notmintree.us/p/1"), [_q()]
    ) is False


def test_merchant_platform_quarantines_are_honoured_too():
    """Not just domain — adding any supported quarantine type works here."""
    row = {"merchant_id": "merch_x", "platform": "shopify"}
    q = _q(match_value="merch_x:shopify", match_type="merchant_platform")
    assert is_quarantined_row(row, [q]) is True


def test_rows_with_no_resolvable_identity_are_kept():
    assert is_quarantined_row({"id": "x", "brand": "Y"}, [_q()]) is False


def test_object_rows_work_not_just_dicts():
    class _P:
        external_destination_url = "https://mintree.us/p/1"
        merchant_id = None
        platform = "external"

    assert is_quarantined_row(_P(), [_q()]) is True


def test_filter_drops_only_quarantined_rows():
    rows = [
        _mintree_row(),
        _mintree_row(external_destination_url="https://beplain.com/p/1", brand="Beplain"),
        {"id": "no-domain"},
    ]
    kept = filter_out_quarantined_rows(rows, [_q()])
    assert [r.get("brand") or r.get("id") for r in kept] == ["Beplain", "no-domain"]


def test_all_quarantines_are_enforced_not_just_the_first():
    """Prod holds 16 active quarantines; every other test here uses ONE.

    A mutant truncating the resolved list to `[:1]` passed the entire suite. In
    production it would enforce mintree.us and silently leak the other fifteen
    domains, including reddane.co.za and wholesale.publicgoods.com.
    """
    quarantines = [_q("mintree.us"), _q("reddane.co.za"), _q("bijin-shop.com")]
    rows = [
        _mintree_row(),
        _mintree_row(external_destination_url="https://reddane.co.za/p/1", brand="RedDane"),
        _mintree_row(external_destination_url="https://bijin-shop.com/p/1", brand="Bijin"),
        _mintree_row(external_destination_url="https://beplain.com/p/1", brand="Beplain"),
    ]
    kept = filter_out_quarantined_rows(rows, quarantines)
    assert [r["brand"] for r in kept] == ["Beplain"]


def test_a_blank_match_value_does_not_drop_every_urlless_row():
    """`match_value=''` would match every row that has no host.

    The delegated comparison normalises both sides, so `"" == normalize(None)`
    is True. The migration declares `match_value TEXT NOT NULL` with no
    non-empty CHECK, and these rows are inserted by direct-SQL ops, so one bad
    insert would erase every connected-merchant card from the unauthenticated
    lane — logged as an ordinary exclusion, indistinguishable from a real drop.
    """
    urlless = {"id": "p1", "brand": "Beplain", "merchant_id": "m1", "platform": "shopify"}
    assert is_quarantined_row(urlless, [_q(match_value="")]) is False
    assert is_quarantined_row(urlless, [_q(match_value="   ")]) is False
    # ...and a blank entry alongside a real one must not disable the real one.
    assert is_quarantined_row(_mintree_row(), [_q(match_value=""), _q("mintree.us")]) is True


def test_empty_quarantine_set_is_a_no_op():
    rows = [_mintree_row()]
    assert filter_out_quarantined_rows(rows, []) == rows


def test_filter_handles_empty_input():
    assert filter_out_quarantined_rows(None, [_q()]) == []
    assert filter_out_quarantined_rows([], [_q()]) == []


# ---------------------------------------------------------------------------
# THE WIRING -- proves the LANE is gated, in the configuration that SERVES
# ---------------------------------------------------------------------------

@pytest.fixture
def _lane(monkeypatch):
    """The real choke-point wrapper with only the inner slate stubbed."""
    import routes.agent_shop_gateway as gw

    def _install(products: List[Dict[str, Any]], quarantine_rows: Any = None):
        async def _fake_inner(payload, request_metadata, background_tasks):
            return {
                "products": list(products),
                "total": len(products),
                "page": 1,
                "page_size": len(products),
            }

        monkeypatch.setattr(gw, "_handle_find_products_multi_inner", _fake_inner)

        # RECORDING stubs, not no-ops. With no-ops, moving the whole gate block
        # BELOW redirect stamping and decision recording left the served slate
        # correct and every test green -- while in production it would mint
        # attributed /r links for quarantined rows and deposit them in the
        # behavioural ledger. Ordering is only observable if the stubs remember.
        seen: Dict[str, List[Any]] = {"redirects": [], "decisions": []}

        def _record_decisions(result, *a, **k):
            seen["decisions"].extend(
                (result or {}).get("products", []) if isinstance(result, dict) else []
            )

        async def _record_redirects(products, tool=None):
            seen["redirects"].extend(products or [])
            return None

        monkeypatch.setattr(gw, "_record_gateway_decision_events", _record_decisions)
        monkeypatch.setattr(gw, "_attach_connected_product_redirects", _record_redirects)
        gw._test_seen = seen  # read by the ordering test

        import db.database as dbmod

        monkeypatch.setattr(
            dbmod, "database", _FakeDB(quarantine_rows if quarantine_rows is not None else [])
        )
        return gw

    return _install


def _run_served(gw):
    """Drive the SERVED configuration.

    emit_decision_event defaults to True on every served slate; False is the
    intermediate building block (find_similar, fragrance retry). A previous
    round's mutant keyed the gate on this flag and stayed green precisely
    because every test used False. Default to True here, always.
    """
    import asyncio

    from fastapi import BackgroundTasks

    payload = gw.FindProductsMultiPayload.model_validate({"search": {"query": "serum"}})
    return asyncio.run(gw._handle_find_products_multi(payload, None, BackgroundTasks()))


def test_wiring_the_served_slate_drops_the_mintree_row(_lane):
    """The exact live defect, through the real wrapper, as served."""
    gw = _lane(
        [_mintree_row(), _mintree_row(external_destination_url="https://beplain.com/p/1", brand="Beplain")],
        quarantine_rows=[_row()],
    )
    result = _run_served(gw)
    brands = [p.get("brand") for p in result["products"]]
    assert brands == ["Beplain"], f"the gate is not wired into the served slate: {brands}"


def test_wiring_also_holds_for_the_intermediate_configuration(_lane):
    """Both configurations, so neither can be the one that is gated."""
    import asyncio

    from fastapi import BackgroundTasks

    gw = _lane([_mintree_row()], quarantine_rows=[_row()])
    payload = gw.FindProductsMultiPayload.model_validate({"search": {"query": "serum"}})
    result = asyncio.run(
        gw._handle_find_products_multi(payload, None, BackgroundTasks(), emit_decision_event=False)
    )
    assert result["products"] == []


def test_wiring_gate_runs_BEFORE_redirect_stamping_and_decision_recording(_lane):
    """Position, not just presence.

    Moving the gate block below these two leaves the served slate correct and
    passes every other test here — while minting attributed /r links for
    quarantined rows and depositing them into the behavioural ledger, which is
    exactly the attribution and eval pollution the ordering exists to prevent.
    """
    gw = _lane(
        [_mintree_row(), _mintree_row(external_destination_url="https://beplain.com/p/1", brand="Beplain")],
        quarantine_rows=[_row()],
    )
    _run_served(gw)
    seen = gw._test_seen

    redirect_brands = [p.get("brand") for p in seen["redirects"]]
    decision_brands = [p.get("brand") for p in seen["decisions"]]
    assert "Mintree" not in redirect_brands, (
        f"a quarantined row reached redirect stamping — it will be /r-attributed: {redirect_brands}"
    )
    assert "Mintree" not in decision_brands, (
        f"a quarantined row reached the decision ledger — it pollutes the eval baseline: {decision_brands}"
    )
    # The clean row must still reach both, or this would pass by doing nothing.
    assert redirect_brands == ["Beplain"] and decision_brands == ["Beplain"]


def test_wiring_keeps_counters_honest(_lane):
    gw = _lane(
        [_mintree_row(), _mintree_row(external_destination_url="https://beplain.com/p/1")],
        quarantine_rows=[_row()],
    )
    result = _run_served(gw)
    assert result["total"] == 1
    assert result["page_size"] == 1


def test_wiring_is_a_no_op_when_nothing_is_quarantined(_lane):
    rows = [_mintree_row(), _mintree_row(external_destination_url="https://beplain.com/p/1")]
    gw = _lane(rows, quarantine_rows=[])
    result = _run_served(gw)
    assert len(result["products"]) == 2
    assert result["total"] == 2


def test_wiring_the_merchant_scoped_twin_is_also_gated(monkeypatch):
    """`POST /agent/shop/v1/invoke` has NO auth dependency.

    `_normalize_find_products_payload` routes a payload carrying a top-level
    merchant_id to `_handle_find_products`, NOT the multi lane. So an unsigned
    `GET /acp/feed` body of {"query":{"merchant_id":"...","query":"serum"}}
    reaches the merchant-scoped handler. Review executed exactly that against
    the first version of this PR and got the Mintree row back with the gate
    never invoked — gating one lane and not its twin is half a fix.
    """
    import asyncio

    import routes.agent_shop_gateway as gw
    from fastapi import BackgroundTasks
    from models.standard_product import ProductStatus, StandardProduct

    mintree = StandardProduct(
        id="p1",
        title="Kumkumadi Oil",
        price=1058.0,
        currency="USD",
        merchant_id="merch_real_indian_store",
        platform="wix",
        status=ProductStatus.ACTIVE,
        online_store_url="https://mintree.us/products/kumkumadi-oil",
    )
    clean = StandardProduct(
        id="p2",
        title="Snail Essence",
        price=20.0,
        currency="USD",
        merchant_id="merch_real_indian_store",
        platform="wix",
        status=ProductStatus.ACTIVE,
        online_store_url="https://beplain.com/products/snail",
    )

    async def _fake_hybrid(**kwargs):
        return [mintree, clean], "cache", None

    async def _no_currency(*a, **k):
        return None

    monkeypatch.setattr(gw, "get_products_hybrid", _fake_hybrid)
    monkeypatch.setattr(gw, "_resolve_shopify_currency_for_merchant", _no_currency)

    import db.database as dbmod

    monkeypatch.setattr(dbmod, "database", _FakeDB([_row()]))

    filters = gw.SearchFilters.model_validate(
        {"merchant_id": "merch_real_indian_store", "query": "oil"}
    )
    result = asyncio.run(gw._handle_find_products(filters, BackgroundTasks()))
    titles = [p.get("title") if isinstance(p, dict) else p.title for p in result["products"]]
    assert "Kumkumadi Oil" not in titles, (
        f"the merchant-scoped lane is ungated — reachable unauthenticated: {titles}"
    )


def test_wiring_serves_the_slate_when_the_gate_itself_fails(_lane, monkeypatch, caplog):
    """Fail OPEN, never SILENTLY."""
    import logging

    import services.offer_currency_policy as pol

    gw = _lane([_mintree_row()], quarantine_rows=[_row()])

    async def _explode(*a, **k):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(pol, "get_quarantined_sources", _explode)

    with caplog.at_level(logging.WARNING):
        result = _run_served(gw)

    assert len(result["products"]) == 1, "a gate failure must not empty the lane"
    assert any(
        "quarantine" in r.message.lower() and "fail" in r.message.lower()
        for r in caplog.records
    ), "the gate failed open with no warning — the leak would be invisible again"
