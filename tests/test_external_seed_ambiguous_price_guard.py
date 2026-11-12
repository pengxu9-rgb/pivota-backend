"""A page that lists several priced products prices the seed as whichever node scored best: on a bundle page, a COMPONENT.

`external_offers_service._extract_jsonld_offer_and_node` scores every JSON-LD Product alone and keeps the first of
equals, so served bundle cards ("The ... Collection") were created at one component's price and title (2026-11-07).
`EXTERNAL_SEED_AMBIGUOUS_PRICE_GUARD` (off | shadow | on) asks, on a page with more than one priced product or a
listing, whether the PRICING node names this seed's product (its own url, or its name:
`destination_liveness.ambiguous_price_decision`). `off` changes nothing; `shadow` reports; `on` refuses the read
(refresh) or the snapshot's price (creation). Every page goes through the REAL extractor; the refresh through the real
`_refresh_external_seed_by_id` (stubs at the process edge only, the handle-less read suite's harness).
"""

from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock

import pytest

from services import external_seed_destination_liveness as liveness
from tests.test_external_seed_handleless_read import (
    STORED_PRICE,
    _observed,
    _offer,
    _page,
    _refresh,
    _seed_row,
    _snapshot_from_html,
)

BUNDLE = "https://shop.example/products/glow-collection"
BUNDLE_TITLE = "Brand Glow Collection"


def _component(name: str, handle: str, price: str) -> Dict[str, Any]:
    return {"@context": "https://schema.org", "@type": "Product", "name": name,
            "url": f"https://shop.example/products/{handle}", "image": f"https://cdn.example.com/{handle}.jpg",
            "description": f"{name}.", "brand": {"@type": "Brand", "name": "Brand"},
            "offers": _offer(price, sku=handle.upper())}


def _bundle_node(price: str = "48.00", **extra: Any) -> Dict[str, Any]:
    return {"@context": "https://schema.org", "@type": "Product", "name": BUNDLE_TITLE, "url": BUNDLE,
            "image": "https://cdn.example.com/glow.jpg", "description": "Three products.",
            "brand": {"@type": "Brand", "name": "Brand"}, "offers": _offer(price, sku="GLOW-SET"), **extra}


COMPONENTS = (
    _component("Brand Niacinamide Serum", "niacinamide-serum", "6.00"),
    _component("Brand Squalane Cleanser", "squalane-cleanser", "10.50"),
    _component("Brand Barrier Cream", "barrier-cream", "14.00"),
)
BUNDLE_PAGE = _page(*COMPONENTS)                   # the shape that priced the hidden cards: components only
NAMED_BUNDLE_PAGE = _page(_bundle_node(), *COMPONENTS)   # the bundle's own node first: it prices the page


def _decide(html: str, url: str = BUNDLE, title: Any = BUNDLE_TITLE, brand: Any = "Brand") -> Optional[Dict[str, Any]]:
    snap = _snapshot_from_html(url, html)
    return liveness.ambiguous_price_decision(evidence=snap.evidence, requested_url=url, seed_title=title, seed_brand=brand)


def _bundle_row(**overrides: Any) -> Dict[str, Any]:
    # A person typed the title (be#2746: only an `employee` stamp vouches for one; unstamped rows are tested below).
    seed_data = {"title": BUNDLE_TITLE, "brand": "Brand", "title_source": "employee",
                 "snapshot": {"price_amount": STORED_PRICE, "price_currency": "USD",
                              "extracted_at": "2026-11-06T12:00:00+00:00"}}
    return _seed_row(destination_url=BUNDLE, canonical_url=BUNDLE, domain="shop.example", title=BUNDLE_TITLE,
                     seed_data=seed_data, **overrides)


def _run(monkeypatch, html: str, mode: str, **kw: Any) -> Dict[str, Any]:
    monkeypatch.setenv(liveness.AMBIGUOUS_PRICE_GUARD_ENV, mode) if mode != "unset" else \
        monkeypatch.delenv(liveness.AMBIGUOUS_PRICE_GUARD_ENV, raising=False)
    # The handle-less rule stays off: these URLs name a handle, and its flag is not what is under test.
    return _refresh(monkeypatch, row=kw.pop("row", None) or _bundle_row(), html=html,
                    observed=kw.pop("observed", None) or _observed(BUNDLE), mode="off", **kw)


# --------------------------------------------------------------------------- the premise


def test_the_extractor_prices_a_bundle_page_as_its_first_component() -> None:
    snap = _snapshot_from_html(BUNDLE, BUNDLE_PAGE)
    assert (snap.title, snap.price_amount) == ("Brand Niacinamide Serum", 6.0)
    assert snap.evidence["jsonld_identity"]["priced_identities"] == 3


# --------------------------------------------------------------------------- the decision (pure)


def test_a_bundle_page_priced_by_a_component_refuses() -> None:
    # The pricing component's own url names ANOTHER product handle on the served host: no title can name it.
    assert _decide(BUNDLE_PAGE) == {"refusal": "several_products", "priced_identities": 3, "named_by": None,
                                    "url_relation": "other_product", "title_dice": 0.0}


def test_a_bundle_page_priced_by_its_own_node_is_named_by_its_url() -> None:
    assert _decide(NAMED_BUNDLE_PAGE) == {"refusal": None, "priced_identities": 4, "named_by": "url",
                                          "url_relation": "matched", "title_dice": 1.0}


def test_a_pricing_node_with_no_url_is_named_by_its_title() -> None:
    node = _bundle_node()
    node.pop("url")
    assert _decide(_page(node, *COMPONENTS))["named_by"] == "title"


def test_a_component_named_like_the_seed_is_still_another_product() -> None:
    # A component's own url names another page; its name is not the bundle's.
    assert _decide(BUNDLE_PAGE, title="Brand Glow Collection Set")["refusal"] == "several_products"


def test_a_product_page_with_a_priced_cross_sell_is_named() -> None:
    own = _component("Brand Niacinamide Serum", "niacinamide-serum", "6.00")
    page = _page(own, _component("Brand Barrier Cream", "barrier-cream", "14.00"))
    url = "https://shop.example/products/niacinamide-serum"
    assert _decide(page, url=url, title="Brand Niacinamide Serum") == {
        "refusal": None, "priced_identities": 2, "named_by": "url", "url_relation": "matched", "title_dice": 1.0}


@pytest.mark.parametrize("html", [
    _page(_component("Brand Niacinamide Serum", "glow-collection", "6.00")),
    _page({"@context": "https://schema.org", "@type": "ProductGroup", "name": BUNDLE_TITLE, "url": BUNDLE,
           "hasVariant": [{"@type": "Product", "name": "30 ml", "offers": _offer("6.00", sku="S-30")},
                          {"@type": "Product", "name": "60 ml", "offers": _offer("10.00", sku="S-60")}]}),
    _page(_bundle_node(), {"@context": "https://schema.org", "@type": "Product", "name": "Reviews",
                           "aggregateRating": {"@type": "AggregateRating", "ratingValue": "4.6", "reviewCount": "8"}}),
    "<html><head><title>No JSON-LD</title></head><body></body></html>",
], ids=["one_product", "product_group_variants", "review_widget", "no_jsonld"])
def test_one_priced_product_is_not_ambiguous(html: str) -> None:
    assert _decide(html) is None


def test_a_listing_page_whose_pricing_node_is_another_product_refuses_as_a_listing() -> None:
    listing = {"@context": "https://schema.org", "@type": "ItemList", "itemListElement": []}
    assert _decide(_page(listing, COMPONENTS[0]))["refusal"] == "listing"


@pytest.mark.parametrize("evidence", [None, {}, {"jsonld_identity": None}, {"jsonld_identity": {"pricing_node": False}},
                                      {"jsonld_identity": {"pricing_node": True, "priced_identities": "3"}},
                                      {"jsonld_identity": {"pricing_node": True, "priced_identities": True}},
                                      "not a dict"],
                         ids=["none", "empty", "no_identity", "no_pricing_node", "count_text", "count_bool", "text"])
def test_no_identity_evidence_never_refuses(evidence: Any) -> None:
    assert liveness.ambiguous_price_decision(evidence=evidence, requested_url=BUNDLE, seed_title=BUNDLE_TITLE) is None


def test_the_decision_never_raises(monkeypatch) -> None:
    def boom(*_a: Any, **_k: Any):
        raise RuntimeError("matcher bug")

    monkeypatch.setattr(liveness, "_same_product_url", boom)
    snap = _snapshot_from_html(BUNDLE, BUNDLE_PAGE)
    assert liveness.ambiguous_price_decision(evidence=snap.evidence, requested_url=BUNDLE, seed_title=BUNDLE_TITLE) is None


def test_an_unparsable_identity_url_falls_back_to_the_title() -> None:
    snap = _snapshot_from_html(BUNDLE, NAMED_BUNDLE_PAGE)
    evidence = copy.deepcopy(snap.evidence)
    evidence["jsonld_identity"]["url"] = "https://shop.example:99999/products/glow-collection"
    assert liveness.ambiguous_price_decision(evidence=evidence, requested_url=BUNDLE, seed_title=BUNDLE_TITLE)[
        "named_by"] == "title"


@pytest.mark.parametrize("raw,mode", [(None, "off"), ("", "off"), ("SHADOW", "shadow"), (" on ", "on"), ("yes", "off")])
def test_the_flag_reads_off_shadow_on_and_nothing_else(monkeypatch, raw: Any, mode: str) -> None:
    if raw is None:
        monkeypatch.delenv(liveness.AMBIGUOUS_PRICE_GUARD_ENV, raising=False)
    else:
        monkeypatch.setenv(liveness.AMBIGUOUS_PRICE_GUARD_ENV, raw)
    assert liveness.ambiguous_price_guard_mode() == mode


# --------------------------------------------------------------------------- the refresh


def _comparable(out: Dict[str, Any]) -> Dict[str, Any]:
    seed_data = copy.deepcopy(out["seed_data"])
    (seed_data.get("review_summary") or {}).pop("audited_at", None)
    (seed_data.get("snapshot") or {}).pop("extracted_at", None)
    written = {k: v for k, v in out["written"].items() if k != "seed_data"}
    return {"seed_data": seed_data, "written": written, "recorded": [repr(r) for r in out["recorded"]],
            "projected": out["projected"]}


@pytest.mark.parametrize("mode", ["off", "unset"])
def test_off_reads_the_components_price_as_before(monkeypatch, mode: str) -> None:
    out = _run(monkeypatch, BUNDLE_PAGE, mode)
    assert "ambiguous_price" not in out["result"]
    assert out["result"]["price_refresh"]["status"] == "applied"
    assert out["written"]["price_amount"] == 6.0


def test_shadow_reports_and_writes_exactly_what_off_writes(monkeypatch) -> None:
    shadow = _run(monkeypatch, BUNDLE_PAGE, "shadow")
    off = _run(monkeypatch, BUNDLE_PAGE, "off")
    assert shadow["result"]["ambiguous_price"] == {"mode": "shadow", "refusal": "several_products",
                                                   "priced_identities": 3, "named_by": None,
                                                   "url_relation": "other_product", "title_dice": 0.0,
                                                   "title_from": "employee"}
    assert _comparable(shadow) == _comparable(off)
    assert "ambiguous" not in json.dumps(shadow["seed_data"], default=str)


def test_on_refuses_the_components_price_and_keeps_the_stored_one(monkeypatch) -> None:
    out = _run(monkeypatch, BUNDLE_PAGE, "on")
    result = out["result"]
    assert result["ambiguous_price"]["refusal"] == "several_products"
    assert result["price_refresh"]["status"] == "skipped_ambiguous_product"
    assert result["availability_refresh"]["status"] == "not_read"
    assert out["written"]["price_amount"] == STORED_PRICE
    assert out["written"]["read_the_served_product"] is False
    assert not out["projected"]
    # The stamp an earlier read left would vouch for a stored price this page says may be a component's.
    assert "extracted_at" not in out["seed_data"]["snapshot"]
    # Nothing about the decision reaches the stored row (seed_data is served whole).
    assert "ambiguous" not in json.dumps(out["seed_data"], default=str)


def test_on_adopts_no_gallery_from_the_component_page(monkeypatch) -> None:
    # The content gate follows the read: the components' declared images are not this seed's gallery.
    off = _run(monkeypatch, BUNDLE_PAGE, "off")
    on = _run(monkeypatch, BUNDLE_PAGE, "on")
    assert off["seed_data"].get("image_urls"), "premise: a read of this page adopts its declared gallery"
    assert not on["seed_data"].get("image_urls")


def test_on_keeps_the_liveness_observation_of_the_page(monkeypatch) -> None:
    on = _run(monkeypatch, BUNDLE_PAGE, "on")
    off = _run(monkeypatch, BUNDLE_PAGE, "off")
    assert [repr(r) for r in on["recorded"]] == [repr(r) for r in off["recorded"]]


def test_on_reads_a_bundle_page_its_own_node_prices(monkeypatch) -> None:
    out = _run(monkeypatch, NAMED_BUNDLE_PAGE, "on")
    assert out["result"]["ambiguous_price"]["refusal"] is None
    assert out["result"]["price_refresh"]["status"] == "applied"
    assert out["written"]["price_amount"] == 48.0


@pytest.mark.parametrize("mode", ["shadow", "on"])
def test_a_single_product_page_is_not_asked(monkeypatch, mode: str) -> None:
    html = _page(_component(BUNDLE_TITLE, "glow-collection", "48.00"))
    on = _run(monkeypatch, html, mode)
    off = _run(monkeypatch, html, "off")
    assert "ambiguous_price" not in on["result"]
    assert _comparable(on) == _comparable(off)


def test_a_page_that_is_not_a_read_is_not_asked(monkeypatch) -> None:
    out = _run(monkeypatch, BUNDLE_PAGE, "on", observed=_observed(BUNDLE, redirect_hops=1,
                                                                  final_url="https://shop.example/products/other"))
    assert "ambiguous_price" not in out["result"]


def test_a_guard_that_raises_costs_the_row_nothing(monkeypatch) -> None:
    import routes.employee_products as ep

    def boom(**_k: Any):
        raise RuntimeError("guard bug")

    off = _run(monkeypatch, BUNDLE_PAGE, "off")
    monkeypatch.setattr(ep.destination_liveness, "ambiguous_price_decision", boom)
    on = _run(monkeypatch, BUNDLE_PAGE, "on")
    assert on["result"]["status"] == "success"
    assert "ambiguous_price" not in on["result"]
    assert _comparable(on) == _comparable(off)


def test_a_guard_that_raises_at_creation_costs_the_creation_nothing(monkeypatch) -> None:
    import routes.employee_products as ep

    def boom(**_k: Any):
        raise RuntimeError("guard bug")

    monkeypatch.setattr(ep.destination_liveness, "ambiguous_price_decision", boom)
    response, write, _ = _create(monkeypatch, BUNDLE_PAGE, "on")
    assert (write["price_amount"], response["snapshot_price_refused"]) == (6.0, None)


# --------------------------------------------------------------------------- the batch and the job line


class _Lines:
    def __init__(self) -> None:
        self.messages: List[str] = []

    def info(self, message: str, *args: Any) -> None:
        self.messages.append(message % args if args else message)


def test_the_batch_counts_per_host_and_logs_refusals_only(monkeypatch) -> None:
    import utils.logger as ul
    from services.external_referral_readiness import ambiguous_price_summary, count_ambiguous_price

    lines = _Lines()
    monkeypatch.setattr(ul, "logger", lines)
    totals: Dict[str, int] = {}
    hosts: Dict[str, Dict[str, int]] = {}
    state: Dict[str, int] = {}
    count_ambiguous_price(totals, hosts, state, "eps_1", "a.example",
                          {"mode": "shadow", "refusal": "several_products", "priced_identities": 3, "named_by": None,
                           "url_relation": "other_product", "title_dice": 0.25, "title_from": "legacy_differs"})
    count_ambiguous_price(totals, hosts, state, "eps_2", "a.example",
                          {"mode": "shadow", "refusal": None, "priced_identities": 2, "named_by": "url"})
    count_ambiguous_price(totals, hosts, state, "eps_3", "b.example",
                          {"mode": "on", "refusal": "listing", "priced_identities": 1, "named_by": None})
    count_ambiguous_price(totals, hosts, state, "eps_4", "b.example", None)
    assert totals == {"decided": 3, "named_by:url": 1, "would_refuse": 1, "refused": 1,
                      "refused:several_products": 1, "refused:listing": 1}
    assert hosts["a.example"] == {"decided": 2, "named_by:url": 1, "would_refuse": 1, "refused:several_products": 1}
    got = [json.loads(m[len("AMBIGUOUS_PRICE_DECISION "):]) for m in lines.messages]
    assert got == [
        {"host": "a.example", "mode": "shadow", "priced_identities": 3, "refusal": "several_products", "seed_id": "eps_1",
         "title_dice": 0.25, "title_from": "legacy_differs", "url_relation": "other_product"},
        {"host": "b.example", "mode": "on", "priced_identities": 1, "refusal": "listing", "seed_id": "eps_3",
         "title_dice": None, "title_from": None, "url_relation": None},
    ]
    assert all("http" not in m and "?" not in m for m in lines.messages)
    summary = ambiguous_price_summary("shadow", totals, hosts, state)
    assert list(summary["top_hosts"]) == ["a.example", "b.example"]
    assert summary["decision_lines"] == {"logged": 2, "suppressed": 0}


def test_the_decision_log_is_bounded_per_run(monkeypatch) -> None:
    import services.external_referral_readiness as err
    import utils.logger as ul

    lines = _Lines()
    monkeypatch.setattr(ul, "logger", lines)
    monkeypatch.setattr(err, "AMBIGUOUS_PRICE_LOG_MAX", 2)
    totals: Dict[str, int] = {}
    hosts: Dict[str, Dict[str, int]] = {}
    state: Dict[str, int] = {}
    for i in range(5):
        err.count_ambiguous_price(totals, hosts, state, f"eps_{i}", "a.example",
                                  {"mode": "shadow", "refusal": "several_products"})
    assert state == {"logged": 2, "suppressed": 3}
    assert len(lines.messages) == 2
    assert totals["would_refuse"] == 5, "every decision is counted, logged or not"


def test_the_count_never_raises(monkeypatch) -> None:
    import utils.logger as ul
    from services.external_referral_readiness import count_ambiguous_price

    class Broken:
        def info(self, *_a: Any, **_k: Any) -> None:
            raise RuntimeError("handler bug")

    monkeypatch.setattr(ul, "logger", Broken())
    count_ambiguous_price({}, {}, {}, "eps_1", "a.example", {"mode": "shadow", "refusal": "listing"})
    count_ambiguous_price({}, {}, {}, "eps_1", "a.example", {"mode": "shadow", "refusal": object(), "named_by": []})


def test_the_decision_log_goes_through_the_logger_prod_keeps() -> None:
    from utils.logger import logger as pivota

    assert pivota.name == "pivota" and pivota.isEnabledFor(20)


def _batch(monkeypatch, produced: Dict[str, Any]) -> Dict[str, Any]:
    import services.external_referral_readiness as err

    ids = list(produced)
    monkeypatch.setattr(err, "get_external_referral_refresh_candidate_seed_ids", lambda *a, **k: asyncio.sleep(0, result=ids))
    monkeypatch.setattr(err, "_fetch_refresh_candidate_hosts", lambda _ids: asyncio.sleep(0, result={i: "shop.example" for i in ids}))

    async def scripted_refresh(seed_id, **_kwargs):
        return produced[seed_id]

    return asyncio.run(err.run_external_referral_refresh_batch(refresh_seed_by_id=scripted_refresh, limit=10))


def test_the_batch_reports_the_runs_decisions_and_the_job_prints_its_line(monkeypatch) -> None:
    import jobs.external_referral_refresh as job
    import utils.logger as ul

    produced = {
        "eps_bundle": _run(monkeypatch, BUNDLE_PAGE, "shadow")["result"],
        "eps_named": _run(monkeypatch, NAMED_BUNDLE_PAGE, "shadow")["result"],
    }
    lines = _Lines()
    monkeypatch.setattr(ul, "logger", lines)
    summary = _batch(monkeypatch, produced)
    block = summary["ambiguous_price"]
    assert block["mode"] == "shadow"
    assert block["totals"] == {"decided": 2, "named_by:url": 1, "would_refuse": 1, "refused:several_products": 1}
    assert block["decision_lines"] == {"logged": 1, "suppressed": 0}
    assert job.ambiguous_price_line(summary) == block
    assert [json.loads(m.split(" ", 1)[1])["seed_id"] for m in lines.messages] == ["eps_bundle"]


def test_off_adds_nothing_to_the_runs_summary(monkeypatch) -> None:
    import jobs.external_referral_refresh as job

    produced = {"eps_bundle": _run(monkeypatch, BUNDLE_PAGE, "off")["result"]}
    summary = _batch(monkeypatch, produced)
    assert "ambiguous_price" not in summary
    assert job.ambiguous_price_line(summary) is None


def test_the_job_prints_nothing_for_a_run_that_decided_no_row() -> None:
    from jobs.external_referral_refresh import ambiguous_price_line

    assert ambiguous_price_line({"ambiguous_price": {"mode": "shadow", "totals": {}, "top_hosts": {}}}) is None


# --------------------------------------------------------------------------- creation


_KEEP = object()


def _create(monkeypatch, html: str, mode: str, *, existing: Optional[List[Dict[str, Any]]] = None,
            snapshot_title: Any = _KEEP, no_read: bool = False,
            **body: Any) -> Tuple[Dict[str, Any], Dict[str, Any], List[str]]:
    import routes.employee_products as ep
    import utils.logger as ul

    if mode == "unset":
        monkeypatch.delenv(liveness.AMBIGUOUS_PRICE_GUARD_ENV, raising=False)
    else:
        monkeypatch.setenv(liveness.AMBIGUOUS_PRICE_GUARD_ENV, mode)
    lines = _Lines()
    monkeypatch.setattr(ul, "logger", lines)
    writes: List[Dict[str, Any]] = []

    async def execute_seed(query, values):
        writes.append(values)

    monkeypatch.setattr(ep, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    snapshot = _snapshot_from_html(BUNDLE, html)
    if snapshot_title is not _KEEP:
        snapshot.title = snapshot_title
    monkeypatch.setattr(ep, "resolve_external_offer", AsyncMock(return_value=None if no_read else snapshot))
    monkeypatch.setattr(ep.database, "fetch_all", AsyncMock(return_value=copy.deepcopy(existing or [])))
    monkeypatch.setattr(ep.database, "execute", AsyncMock(return_value=None))
    monkeypatch.setattr(ep, "_execute_seed_data_stmt", execute_seed)
    monkeypatch.setattr(ep, "_derive_seed_seller_columns", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(ep, "_make_redirect_url", AsyncMock(return_value="https://api.pivota.cc/r?token=t"))
    request_body = {"destination_url": BUNDLE, "market": "US", "title": BUNDLE_TITLE, "brand": "Brand", **body}
    response = asyncio.run(
        ep.create_external_seed(
            ep.CreateExternalSeedRequest(**{k: v for k, v in request_body.items() if v is not None}),
            request=SimpleNamespace(),
            current_user={"employee_id": "emp_test"},
        )
    )
    (write,) = [w for w in writes if "seed_data" in w]
    seed_data = write["seed_data"] if isinstance(write["seed_data"], dict) else json.loads(write["seed_data"])
    return response, {**write, "seed_data": seed_data}, lines.messages


@pytest.mark.parametrize("mode", ["off", "unset", "shadow"])
def test_creation_off_and_shadow_take_the_components_price_as_before(monkeypatch, mode: str) -> None:
    response, write, messages = _create(monkeypatch, BUNDLE_PAGE, mode)
    assert (write["price_amount"], write["price_currency"]) == (6.0, "USD")
    assert response["snapshot_price_refused"] is None
    if mode == "shadow":
        assert [json.loads(m.split(" ", 1)[1]) for m in messages] == [
            {"mode": "shadow", "page_values": [], "priced_identities": 3, "refusal": "several_products",
             "title_dice": 0.0, "title_from": "employee", "url_relation": "other_product"}]
    else:
        assert messages == []


def test_creation_shadow_writes_exactly_what_off_writes(monkeypatch) -> None:
    _, shadow, _ = _create(monkeypatch, BUNDLE_PAGE, "shadow")
    _, off, _ = _create(monkeypatch, BUNDLE_PAGE, "off")

    def same(write: Dict[str, Any]) -> str:
        # A new seed's id is minted per call and the content audit stamps its own clock; every other value matches.
        write = copy.deepcopy(write)
        (write["seed_data"].get("review_summary") or {}).pop("audited_at", None)
        text = json.dumps(write, sort_keys=True, default=str)
        return text.replace(str(write.get("id")), "<id>")

    assert same(shadow) == same(off)


def test_creation_on_takes_no_price_and_no_variants_from_a_component(monkeypatch) -> None:
    response, write, _ = _create(monkeypatch, BUNDLE_PAGE, "on")
    assert (write["price_amount"], write["price_currency"]) == (None, None)
    assert (write["seed_data"]["snapshot"]["price_amount"], write["seed_data"]["snapshot"]["price_currency"]) == (None, None)
    assert not write["seed_data"].get("variants")
    refused = response["snapshot_price_refused"]
    assert refused["reason"] == "ambiguous_product"
    assert refused["refusal"] == "several_products"
    assert refused["snapshot_price"] == {"amount": 6.0, "currency": "USD"}


def test_creation_on_keeps_an_employee_stated_price(monkeypatch) -> None:
    _, write, _ = _create(monkeypatch, BUNDLE_PAGE, "on", price_amount=48.0, price_currency="USD")
    assert (write["price_amount"], write["price_currency"]) == (48.0, "USD")


def test_creation_on_prices_a_bundle_its_own_node_prices(monkeypatch) -> None:
    response, write, _ = _create(monkeypatch, NAMED_BUNDLE_PAGE, "on")
    assert (write["price_amount"], write["price_currency"]) == (48.0, "USD")
    assert response["snapshot_price_refused"] is None



# --------------------------------------------------------------------------- review of be#2746 (controller, 11-07)


def _claiming_bundle_page() -> str:
    """Every component names the served page as its own url (a theme that stamps the page url on each node)."""
    nodes = []
    for comp in COMPONENTS:
        node = dict(comp)
        node["url"] = BUNDLE
        nodes.append(node)
    return _page(*nodes)


def test_p2_1_a_seed_created_without_a_title_is_refused_by_its_urls(monkeypatch) -> None:
    # `title` falls back to the page's, the pricing node's own name: it must never name the node by itself.
    response, write, _ = _create(monkeypatch, BUNDLE_PAGE, "on", title=None)
    assert (write["price_amount"], response["snapshot_price_refused"]["reason"]) == (None, "ambiguous_product")


def test_p2_1_an_untitled_bundle_its_own_node_prices_is_priced(monkeypatch) -> None:
    response, write, _ = _create(monkeypatch, NAMED_BUNDLE_PAGE, "on", title=None)
    assert (write["price_amount"], response["snapshot_price_refused"]) == (48.0, None)


def test_p2_1_a_node_whose_url_names_another_handle_is_refused_whatever_its_name() -> None:
    # The seed's title even matches the component's name: the component's own url says it is another product.
    decision = _decide(BUNDLE_PAGE, title="Brand Niacinamide Serum")
    assert (decision["refusal"], decision["url_relation"]) == ("several_products", "other_product")


def test_p2_1_a_collection_path_url_of_the_same_handle_is_not_another_product() -> None:
    served = "https://shop.example/collections/skin/products/niacinamide-serum"
    page = _page(_component("Brand Niacinamide Serum", "niacinamide-serum", "6.00"),
                 _component("Brand Barrier Cream", "barrier-cream", "14.00"))
    decision = _decide(page, url=served, title="Brand Niacinamide Serum")
    assert (decision["url_relation"], decision["named_by"], decision["refusal"]) == ("mismatch", "title", None)


def test_p2_2_the_extractor_counts_nodes_that_claim_the_served_url() -> None:
    snap = _snapshot_from_html(BUNDLE, _claiming_bundle_page())
    assert snap.evidence["jsonld_identity"]["url_conflicts"] == 2
    assert _snapshot_from_html(BUNDLE, NAMED_BUNDLE_PAGE).evidence["jsonld_identity"]["url_conflicts"] == 0


def test_p2_2_a_url_several_products_claim_names_nothing_and_the_title_decides() -> None:
    decision = _decide(_claiming_bundle_page())
    assert (decision["url_relation"], decision["named_by"], decision["refusal"]) == (
        "matched", None, "several_products")


def test_p2_2_a_claimed_url_still_reads_when_the_title_names_the_pricing_node() -> None:
    decision = _decide(_claiming_bundle_page(), title="Brand Niacinamide Serum")
    assert (decision["named_by"], decision["refusal"]) == ("title", None)


def test_p2_2_the_refresh_refuses_a_claimed_url_bundle(monkeypatch) -> None:
    out = _run(monkeypatch, _claiming_bundle_page(), "on")
    assert out["result"]["price_refresh"]["status"] == "skipped_ambiguous_product"
    assert out["written"]["price_amount"] == STORED_PRICE


def test_p2_3_an_employee_amount_keeps_the_pages_currency(monkeypatch) -> None:
    _, on, _ = _create(monkeypatch, BUNDLE_PAGE, "on", price_amount=48.0)
    _, off, _ = _create(monkeypatch, BUNDLE_PAGE, "off", price_amount=48.0)
    assert (on["price_amount"], on["price_currency"]) == (48.0, "USD") == (off["price_amount"], off["price_currency"])


def test_p2_3_an_employee_currency_alone_stores_no_amount(monkeypatch) -> None:
    _, write, _ = _create(monkeypatch, BUNDLE_PAGE, "on", price_currency="USD")
    assert (write["price_amount"], write["price_currency"]) == (None, "USD")


def _stored_bundle_row() -> Dict[str, Any]:
    row = _bundle_row(price_amount=6.0)
    row["seed_data"]["snapshot"]["extracted_at"] = "2026-11-06T12:00:00+00:00"
    return row


def test_p3_3_a_re_create_keeps_the_stored_price_and_drops_its_stamp(monkeypatch) -> None:
    response, write, _ = _create(monkeypatch, BUNDLE_PAGE, "on", existing=[_stored_bundle_row()])
    assert response["snapshot_price_refused"]["reason"] == "ambiguous_product"
    assert write["price_amount"] == 6.0, "a re-create keeps the row's stored price (stated, not corrected)"
    assert "extracted_at" not in write["seed_data"]["snapshot"]


def test_p3_3_an_off_re_create_keeps_its_stamp(monkeypatch) -> None:
    _, write, _ = _create(monkeypatch, BUNDLE_PAGE, "off", existing=[_stored_bundle_row()])
    assert write["seed_data"]["snapshot"].get("extracted_at") == "2026-11-06T12:00:00+00:00"


def test_p3_4_a_re_create_without_a_brand_uses_the_stored_one(monkeypatch) -> None:
    import routes.employee_products as ep

    seen: List[Any] = []
    real = ep.destination_liveness.ambiguous_price_decision

    def spy(**kwargs: Any):
        seen.append(kwargs.get("seed_brand"))
        return real(**kwargs)

    monkeypatch.setattr(ep.destination_liveness, "ambiguous_price_decision", spy)
    _create(monkeypatch, BUNDLE_PAGE, "on", existing=[_stored_bundle_row()], brand=None)
    assert seen == ["Brand"]


@pytest.mark.parametrize("mode", ["on", "shadow", "off"])
def test_p3_2_price_refresh_counts_the_guards_refusals_in_its_own_bucket(monkeypatch, mode: str) -> None:
    import jobs.external_referral_refresh as job

    produced = {"eps_bundle": _run(monkeypatch, BUNDLE_PAGE, mode)["result"]}
    line = job.price_refresh_line(_batch(monkeypatch, produced))
    if mode == "off":
        assert "price_skipped_ambiguous" not in line
    else:
        assert line["price_skipped_ambiguous"] == (1 if mode == "on" else 0)


def test_p2_1_a_node_url_on_another_host_is_a_mismatch_not_another_product() -> None:
    # Only a handle on the SERVED host says "another product of this store"; another host is just another page,
    # and the title may still name the node.
    other_host = dict(COMPONENTS[0])
    other_host["url"] = "https://brand.example/products/niacinamide-serum"
    decision = _decide(_page(other_host, *COMPONENTS[1:]), title="Brand Niacinamide Serum")
    assert (decision["url_relation"], decision["named_by"]) == ("mismatch", "title")


def test_p2_1_an_untitled_seed_on_a_url_less_bundle_is_refused(monkeypatch) -> None:
    # No node carries a url, so only a title can name the pricing node; the page's own title (the pricing node's
    # name) must not be that title, or the node names itself and its price is stored.
    nodes = []
    for comp in COMPONENTS:
        node = dict(comp)
        node.pop("url")
        nodes.append(node)
    response, write, _ = _create(monkeypatch, _page(*nodes), "on", title=None)
    assert write["price_amount"] is None
    assert response["snapshot_price_refused"]["reason"] == "ambiguous_product"


# --------------------------------------------------------------------------- review P2-A: a page-derived title

def _url_less(nodes: Any) -> List[Dict[str, Any]]:
    out = []
    for node in nodes:
        node = dict(node)
        node.pop("url", None)
        out.append(node)
    return out


def _refresh_what_create_wrote(monkeypatch, write: Dict[str, Any], html: str) -> Dict[str, Any]:
    """The row an untitled creation wrote, refreshed against the same page: the chain the review reproduced."""
    row = _seed_row(destination_url=BUNDLE, canonical_url=BUNDLE, domain="shop.example", title=write["title"],
                    price_amount=write.get("price_amount"), price_currency=write.get("price_currency"),
                    seed_data=copy.deepcopy(write["seed_data"]))
    return _run(monkeypatch, html, "on", row=row)


@pytest.mark.parametrize("shape", ["url_less_components", "components_claim_the_page"])
def test_p2a_an_untitled_create_then_its_first_refresh_writes_no_component_price(monkeypatch, shape: str) -> None:
    html = _page(*_url_less(COMPONENTS)) if shape == "url_less_components" else _claiming_bundle_page()
    response, write, _ = _create(monkeypatch, html, "on", title=None)
    assert write["price_amount"] is None
    assert response["snapshot_price_refused"]["reason"] == "ambiguous_product"
    # The stored title is the pricing node's own name, and the row says the page wrote it.
    assert write["seed_data"]["title_source"] == "page"
    out = _refresh_what_create_wrote(monkeypatch, write, html)
    assert out["result"]["price_refresh"]["status"] == "skipped_ambiguous_product"
    assert out["result"]["ambiguous_price"]["refusal"]
    assert out["written"].get("price_amount") is None, "the first refresh must not store the component's price"


URL_LESS_BUNDLE_PAGE = _page(*_url_less([_bundle_node(), *COMPONENTS]))   # the url-less bundle node prices it


def test_p2a_a_typed_title_still_names_a_url_less_pricing_node(monkeypatch) -> None:
    # The twin: a person typed the bundle's name (not the page's spelling of it), the url-less bundle node carries it,
    # so creation and the refresh read it.
    response, write, _ = _create(monkeypatch, URL_LESS_BUNDLE_PAGE, "on", title="The Glow Collection")
    assert write["seed_data"]["title_source"] == "employee"
    assert (write["price_amount"], response["snapshot_price_refused"]) == (48.0, None)
    out = _refresh_what_create_wrote(monkeypatch, write, URL_LESS_BUNDLE_PAGE)
    assert out["result"]["price_refresh"]["status"] != "skipped_ambiguous_product"
    assert out["result"]["ambiguous_price"]["named_by"] == "title"
    assert out["result"]["ambiguous_price"]["title_from"] == "employee"


def _legacy_row(title: str, snapshot_title: Any) -> Dict[str, Any]:
    """A row written before the stamp: no `title_source`."""
    row = _bundle_row()
    row["title"] = title
    row["seed_data"].pop("title_source")
    row["seed_data"].update(title=title)
    row["seed_data"]["snapshot"]["title"] = snapshot_title
    return row


def test_p2a_a_legacy_unstamped_row_whose_title_is_the_pages_name_is_decided_by_url(monkeypatch) -> None:
    # Rows created untitled before the stamp carry the page's name as both title and snapshot title.
    first = COMPONENTS[0]["name"]
    out = _run(monkeypatch, _page(*_url_less(COMPONENTS)), "on", row=_legacy_row(first, first))
    assert out["result"]["price_refresh"]["status"] == "skipped_ambiguous_product"
    assert out["result"]["ambiguous_price"]["title_from"] == "legacy_equal"


# --------------------------------------------------------------------------- delta-2 review P2-D: unstamped = untyped

@pytest.mark.parametrize("drifted", ["Just a moment...", "Shop Example | Skincare", None])
@pytest.mark.parametrize("shape", ["url_less_components", "components_claim_the_page"])
def test_p2d_a_legacy_row_whose_snapshot_title_drifted_is_still_decided_by_url(monkeypatch, shape: str,
                                                                               drifted: Any) -> None:
    # A night that fetched the page without reading the product (a bot-challenge page, a page without JSON-LD)
    # rewrote `snapshot.title`. The legacy title (the component's own name) then no longer equals it; it must still
    # not name the component on the next good read.
    html = _page(*_url_less(COMPONENTS)) if shape == "url_less_components" else _claiming_bundle_page()
    first = _snapshot_from_html(BUNDLE, html).title
    out = _run(monkeypatch, html, "on", row=_legacy_row(first, drifted))
    assert out["result"]["price_refresh"]["status"] == "skipped_ambiguous_product"
    assert out["result"]["ambiguous_price"]["named_by"] is None
    assert out["result"]["ambiguous_price"]["title_from"] == "legacy_differs"
    assert out["written"].get("price_amount") in (None, STORED_PRICE), "the component's price is never written"
    assert out["written"].get("price_amount") != 6.0


def test_p2d_a_legacy_typed_title_is_decided_by_url_too_and_shadow_says_so(monkeypatch) -> None:
    # The cost: an unstamped title a person may have typed no longer names the node. Shadow labels it, so the false
    # refusals can be sized before `on` (review P3-5).
    out = _run(monkeypatch, URL_LESS_BUNDLE_PAGE, "shadow", row=_legacy_row("The Glow Collection", "Brand Glow Collection"))
    assert out["result"]["ambiguous_price"]["refusal"] == "several_products"
    assert out["result"]["ambiguous_price"]["title_from"] == "legacy_differs"
    stamped = _bundle_row()
    stamped["title"] = "The Glow Collection"
    stamped["seed_data"]["title"] = "The Glow Collection"
    out = _run(monkeypatch, URL_LESS_BUNDLE_PAGE, "shadow", row=stamped)
    assert (out["result"]["ambiguous_price"]["refusal"], out["result"]["ambiguous_price"]["named_by"]) == (None, "title")


@pytest.mark.parametrize(
    "title, seed_data, expected, origin",
    [
        ("Brand Glow Collection", {"title_source": "employee", "snapshot": {"title": "Brand Glow Collection"}},
         "Brand Glow Collection", "employee"),
        ("Brand Glow Collection", {"title_source": "page"}, None, "page"),
        ("Brand Glow Collection", {"title_source": "drain"}, None, "unknown_stamp"),
        ("Brand Glow Collection", {"snapshot": {"title": "brand glow-collection"}}, None, "legacy_equal"),
        # P2-D: an unstamped title is never vouched for, whatever the stored snapshot says.
        ("Brand Glow Collection", {"snapshot": {"title": "Brand Glow Serum"}}, None, "legacy_differs"),
        ("Brand Glow Collection", {"snapshot": {"title": "Just a moment..."}}, None, "legacy_differs"),
        ("Brand Glow Collection", {}, None, "legacy_differs"),
        ("Brand Glow Collection", "not a dict", None, "legacy_differs"),
        ("  ", {"title_source": "employee"}, None, "none"),
        (None, None, None, "none"),
        ("수분 크림", {"title_source": "employee"}, "수분 크림", "employee"),
        ("수분 크림", {"snapshot": {"title": "수분크림"}}, None, "legacy_equal"),
        ("수분 크림", {"snapshot": {"title": "진정 토너"}}, None, "legacy_differs"),
    ],
)
def test_p2a_only_a_title_a_person_typed_is_the_guards_title(title: Any, seed_data: Any, expected: Any,
                                                             origin: str) -> None:
    import routes.employee_products as ep

    assert ep._seed_title_a_person_typed(title, seed_data) == expected
    assert ep._guard_title_and_origin(title, seed_data) == (expected, origin)


@pytest.mark.parametrize(
    "a, b, same",
    [
        ("Brand Glow Collection", "brand glow-collection", True),
        ("ＧＬＯＷ　Collection", "glow collection", True),      # NFKC folds full-width forms
        ("CRÈME Riche", "crème riche", True),
        ("Crème Riche", "Creme Riche", False),
        ("수분 크림", "수분크림", True),
        ("수분 크림", "진정 토너", False),                      # the ASCII-only key made these equal ("" == "")
        ("保湿クリーム", "保湿 クリーム!", True),
        ("保湿クリーム", "美白クリーム", False),
        ("Brand Glow Collection", None, False),                 # a missing title is unknown, never equal
        (None, None, False),
        ("!!!", "???", False),
    ],
)
def test_p3_3_titles_compare_in_every_script(a: Any, b: Any, same: bool) -> None:
    import routes.employee_products as ep

    assert ep._same_title(a, b) is same


# --------------------------------------------------------------------------- delta-2 review P2-C: a prefilled title

@pytest.mark.parametrize("prefill_price", [False, True])
@pytest.mark.parametrize("shape", ["url_less_components", "components_claim_the_page"])
def test_p2c_a_title_prefilled_from_the_preview_is_the_pages(monkeypatch, shape: str, prefill_price: bool) -> None:
    # The portal's create form copies `/external-seeds/preview`'s title (the pricing component's name) into the body.
    html = _page(*_url_less(COMPONENTS)) if shape == "url_less_components" else _claiming_bundle_page()
    snap = _snapshot_from_html(BUNDLE, html)
    body: Dict[str, Any] = {"title": snap.title}
    if prefill_price:
        body.update(price_amount=snap.price_amount, price_currency=snap.price_currency)
    response, write, messages = _create(monkeypatch, html, "on", **body)
    assert write["seed_data"]["title_source"] == "page"
    assert response["snapshot_price_refused"]["reason"] == "ambiguous_product"
    if not prefill_price:
        assert write["price_amount"] is None
    assert json.loads(messages[0].split(" ", 1)[1])["title_from"] == "page"
    out = _refresh_what_create_wrote(monkeypatch, write, html)
    assert out["result"]["price_refresh"]["status"] == "skipped_ambiguous_product"
    assert out["result"]["ambiguous_price"]["title_from"] == "page"
    if not prefill_price:
        assert out["written"].get("price_amount") is None, "the refresh must not store the component's price"


def test_p2c_a_title_equal_to_the_pricing_nodes_name_is_the_pages_even_when_the_snapshot_title_is_not(
    monkeypatch,
) -> None:
    # The create read's snapshot title can differ from what the preview showed (a cached or drifted read); the
    # pricing node's own name is the page's name too.
    html = _page(*_url_less(COMPONENTS))
    first = COMPONENTS[0]["name"]
    response, write, _ = _create(monkeypatch, html, "on", title=first, snapshot_title="Just a moment...")
    assert write["seed_data"]["title_source"] == "page"
    assert response["snapshot_price_refused"]["reason"] == "ambiguous_product"


def test_p2c_a_typed_title_equal_to_the_pages_name_is_decided_by_url(monkeypatch) -> None:
    # The accepted cost: a person who types exactly the page's name is indistinguishable from the prefill.
    response, write, _ = _create(monkeypatch, URL_LESS_BUNDLE_PAGE, "on", title="  brand GLOW collection ")
    assert write["seed_data"]["title_source"] == "page"
    assert response["snapshot_price_refused"]["reason"] == "ambiguous_product"


def test_p2c_a_typed_title_with_no_page_name_to_compare_is_the_employees(monkeypatch) -> None:
    # No snapshot title and no pricing-node name: the preview showed none either, so the body title was typed.
    _, write, _ = _create(monkeypatch, "<html><head></head><body>nothing</body></html>", "on",
                          title="Brand Glow Collection", snapshot_title=None)
    assert write["seed_data"]["title_source"] == "employee"


def test_p2c_a_non_latin_prefilled_title_is_the_pages(monkeypatch) -> None:
    html = _page(*_url_less([_component("수분 크림", "moist-cream", "6.00"), *COMPONENTS[1:]]))
    _, write, _ = _create(monkeypatch, html, "on", title="수분크림")
    assert write["seed_data"]["title_source"] == "page"
    _, write, _ = _create(monkeypatch, html, "on", title="진정 토너")
    assert write["seed_data"]["title_source"] == "employee"


# --------------------------------------------------------------------------- P3-1 / P3-4: a re-create's stamp

def _stamped_row(title: str, source: Optional[str]) -> Dict[str, Any]:
    row = _stored_bundle_row()
    row["title"] = title
    row["seed_data"]["title"] = title
    if source is None:
        row["seed_data"].pop("title_source", None)
    else:
        row["seed_data"]["title_source"] = source
    return row


def test_p3_1_a_titled_re_create_over_an_untitled_row_is_typed(monkeypatch) -> None:
    first = COMPONENTS[0]["name"]
    _, write, _ = _create(monkeypatch, URL_LESS_BUNDLE_PAGE, "on", existing=[_stamped_row(first, "page")],
                          title="The Glow Collection")
    assert write["seed_data"]["title_source"] == "employee"
    assert write["price_amount"] == 48.0


def test_p3_1_an_untitled_re_create_over_a_typed_row_takes_the_pages_name(monkeypatch) -> None:
    # This read names the page, so the stored title becomes the page's name, and so does its stamp.
    _, write, _ = _create(monkeypatch, URL_LESS_BUNDLE_PAGE, "on", existing=[_stamped_row("The Glow Collection", "employee")],
                          title=None)
    assert write["seed_data"]["title"] == BUNDLE_TITLE
    assert write["seed_data"]["title_source"] == "page"


@pytest.mark.parametrize("source, kept", [("employee", "employee"), ("page", "page"), (None, "page")])
def test_p3_4_an_untitled_re_create_that_keeps_the_rows_title_keeps_its_stamp(monkeypatch, source: Any,
                                                                             kept: str) -> None:
    # This read gave no name, so the row's own title is kept, and with it who wrote it.
    _, write, _ = _create(monkeypatch, URL_LESS_BUNDLE_PAGE, "on", existing=[_stamped_row("The Glow Collection", source)],
                          title=None, snapshot_title=None)
    assert write["seed_data"]["title"] == "The Glow Collection"
    assert write["seed_data"]["title_source"] == kept


def test_p3_4_a_kept_typed_title_still_names_the_node_at_the_re_create(monkeypatch) -> None:
    seen: List[Any] = []
    import routes.employee_products as ep

    real = ep.destination_liveness.ambiguous_price_decision

    def spy(**kwargs: Any):
        seen.append(kwargs.get("seed_title"))
        return real(**kwargs)

    monkeypatch.setattr(ep.destination_liveness, "ambiguous_price_decision", spy)
    _create(monkeypatch, URL_LESS_BUNDLE_PAGE, "on", existing=[_stamped_row("The Glow Collection", "employee")],
            title=None, snapshot_title=None)
    _create(monkeypatch, URL_LESS_BUNDLE_PAGE, "on", existing=[_stamped_row("The Glow Collection", "page")],
            title=None, snapshot_title=None)
    assert seen == ["The Glow Collection", None]


def test_p3_1_a_re_create_re_sending_the_rows_typed_title_keeps_it_typed(monkeypatch) -> None:
    # The person typed the page's exact name earlier; the create form re-sends it.
    _, write, _ = _create(monkeypatch, URL_LESS_BUNDLE_PAGE, "on",
                          existing=[_stamped_row(BUNDLE_TITLE, "employee")], title=BUNDLE_TITLE)
    assert write["seed_data"]["title_source"] == "employee"
    _, write, _ = _create(monkeypatch, URL_LESS_BUNDLE_PAGE, "on",
                          existing=[_stamped_row(BUNDLE_TITLE, "page")], title=BUNDLE_TITLE)
    assert write["seed_data"]["title_source"] == "page"


# --------------------------------------------------------------------------- delta-2 review P2-B: an editor save

def _patch(monkeypatch, row: Dict[str, Any], **body: Any) -> Dict[str, Any]:
    """The real PATCH over `row`; returns the row as it would read back after the write."""
    import routes.employee_products as ep

    writes: List[Dict[str, Any]] = []

    async def execute_seed(query, values):
        writes.append(dict(values))

    monkeypatch.setattr(ep, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(ep.database, "fetch_one", AsyncMock(return_value=copy.deepcopy(row)))
    monkeypatch.setattr(ep, "_execute_seed_data_stmt", execute_seed)
    monkeypatch.setattr(ep, "_project_refreshed_seed_to_serving_surfaces", AsyncMock(return_value={}))
    asyncio.run(ep.update_external_seed(row["id"], ep.UpdateExternalSeedRequest(**body),
                                        current_user={"employee_id": "emp_test"}))
    (write,) = writes
    after = copy.deepcopy(row)
    after["seed_data"] = ep._ensure_json_obj(write["seed_data"])
    for key in ("title", "notes", "status", "price_amount", "price_currency"):
        if key in write:
            after[key] = write[key]
    return after


def _row_from_create(write: Dict[str, Any]) -> Dict[str, Any]:
    return _seed_row(destination_url=BUNDLE, canonical_url=BUNDLE, domain="shop.example", title=write["title"],
                     price_amount=write.get("price_amount"), price_currency=write.get("price_currency"),
                     seed_data=copy.deepcopy(write["seed_data"]))


@pytest.mark.parametrize("editor", ["seed_editor", "audit_queue"])
@pytest.mark.parametrize("shape", ["url_less_components", "components_claim_the_page"])
def test_p2b_an_editor_save_re_sending_the_title_keeps_it_the_pages(monkeypatch, shape: str, editor: str) -> None:
    # SeedEditor re-sends `title` with every save (here a notes edit); the audit queue re-sends it with a status edit.
    html = _page(*_url_less(COMPONENTS)) if shape == "url_less_components" else _claiming_bundle_page()
    _, write, _ = _create(monkeypatch, html, "on", title=None)
    row = _row_from_create(write)
    assert row["seed_data"]["title_source"] == "page"
    edit = {"notes": "checked"} if editor == "seed_editor" else {"status": "active"}
    # The editor shows the stored title with its own whitespace / case; a re-send is not a change.
    resent = f"  {row['title'].upper()} " if editor == "audit_queue" else row["title"]
    saved = _patch(monkeypatch, row, title=resent, **edit)
    assert saved["seed_data"]["title_source"] == "page"
    out = _run(monkeypatch, html, "on", row=saved)
    assert out["result"]["price_refresh"]["status"] == "skipped_ambiguous_product"
    assert out["written"].get("price_amount") is None, "the save must not let the component name itself"


def test_p2b_an_editor_save_that_changes_the_title_makes_it_typed(monkeypatch) -> None:
    # The twin: a person retitles the untitled seed, and the refresh then reads the bundle node it names.
    _, write, _ = _create(monkeypatch, URL_LESS_BUNDLE_PAGE, "on", title=None)
    row = _row_from_create(write)
    out = _run(monkeypatch, URL_LESS_BUNDLE_PAGE, "on", row=row)
    assert out["result"]["price_refresh"]["status"] == "skipped_ambiguous_product"
    saved = _patch(monkeypatch, row, title="The Glow Collection", notes="retitled")
    assert (saved["title"], saved["seed_data"]["title_source"]) == ("The Glow Collection", "employee")
    out = _run(monkeypatch, URL_LESS_BUNDLE_PAGE, "on", row=saved)
    assert out["result"]["ambiguous_price"]["named_by"] == "title"
    assert out["result"]["price_refresh"]["status"] != "skipped_ambiguous_product"


@pytest.mark.parametrize("source", ["employee", "page", None])
def test_p2b_a_re_sent_title_keeps_whatever_stamp_the_row_had(monkeypatch, source: Any) -> None:
    row = _stamped_row("The Glow Collection", source)
    saved = _patch(monkeypatch, row, title="the glow collection", status="active")
    assert saved["seed_data"].get("title_source") == source


def test_p2b_a_re_sent_title_matching_either_stored_copy_is_unchanged(monkeypatch) -> None:
    # A refresh fills `seed_data.title` only; the editor may have loaded either copy.
    row = _stamped_row("The Glow Collection", "page")
    row["seed_data"]["title"] = BUNDLE_TITLE
    assert _patch(monkeypatch, row, title="The Glow Collection")["seed_data"]["title_source"] == "page"
    assert _patch(monkeypatch, row, title=BUNDLE_TITLE)["seed_data"]["title_source"] == "page"
    assert _patch(monkeypatch, row, title="Glow Set")["seed_data"]["title_source"] == "employee"


def test_p2b_a_re_sent_non_latin_title_is_unchanged(monkeypatch) -> None:
    row = _stamped_row("수분 크림", "page")
    assert _patch(monkeypatch, row, title="수분크림")["seed_data"]["title_source"] == "page"
    assert _patch(monkeypatch, row, title="진정 토너")["seed_data"]["title_source"] == "employee"


@pytest.mark.parametrize("title", ["", "   ", "\t\n"])
@pytest.mark.parametrize("source", ["employee", "page"])
def test_p2b_an_empty_title_clears_the_stamp(monkeypatch, source: str, title: str) -> None:
    # The audit queue sends `title: ""` for a row with no title; a whitespace-only title is no title either
    # (be#2764 review P3-E, mutant N16).
    saved = _patch(monkeypatch, _stamped_row("The Glow Collection", source), title=title, status="active")
    assert "title_source" not in saved["seed_data"]
    assert saved["title"] == title


def test_p2b_a_save_without_a_title_leaves_the_stamp(monkeypatch) -> None:
    saved = _patch(monkeypatch, _stamped_row("The Glow Collection", "page"), notes="x")
    assert saved["seed_data"]["title_source"] == "page"
    assert saved["title"] == "The Glow Collection"


def test_p2a_the_title_stamp_never_reaches_a_partner() -> None:
    from services.seed_data_egress_projection import INTERNAL_STATE_KEYS

    assert "title_source" in INTERNAL_STATE_KEYS


# --------------------------------------------------------------------------- be#2764 review P2-E: prefilled values

def _portal_variants(html: str) -> List[Dict[str, Any]]:
    """The variants the portal's create form copies from the preview (its own normalisation of each entry)."""
    snap = _snapshot_from_html(BUNDLE, html)
    return [{"variant_id": str(v["variant_id"]), "title": v.get("title") or "", "image_url": None,
             "label_image_url": None, "price_amount": v.get("price_amount"),
             "price_currency": v.get("price_currency") or "", "availability": v.get("availability") or ""}
            for v in snap.evidence["variants"] if v.get("variant_id")]


def _prefilled_body(html: str, **over: Any) -> Dict[str, Any]:
    snap = _snapshot_from_html(BUNDLE, html)
    body = {"title": snap.title, "price_amount": snap.price_amount, "price_currency": snap.price_currency,
            "variants": _portal_variants(html)}
    body.update(over)
    return body


@pytest.mark.parametrize("shape", ["url_less_components", "components_claim_the_page"])
def test_p2e_a_refused_creation_stores_no_prefilled_price_and_no_prefilled_variants(monkeypatch, shape: str) -> None:
    # What the portal sends after a preview of a bundle page: the component's name, price and the components.
    html = _page(*_url_less(COMPONENTS)) if shape == "url_less_components" else _claiming_bundle_page()
    response, write, messages = _create(monkeypatch, html, "on", **_prefilled_body(html))
    refused = response["snapshot_price_refused"]
    assert refused["reason"] == "ambiguous_product"
    assert refused["body_values_from_the_page"] == ["price", "variants"]
    assert (write["price_amount"], write["price_currency"]) == (None, None)
    assert write["seed_data"]["variants"] == []
    assert json.loads(messages[0].split(" ", 1)[1])["page_values"] == ["price", "variants"]
    out = _refresh_what_create_wrote(monkeypatch, write, html)
    assert out["result"]["price_refresh"]["status"] == "skipped_ambiguous_product"
    assert out["written"].get("price_amount") is None, "nothing the page priced is ever stored"
    assert not out["seed_data"].get("variants")


def test_p2e_untitled_prefilled_variants_alone_are_the_pages(monkeypatch) -> None:
    html = _page(*_url_less(COMPONENTS))
    response, write, _ = _create(monkeypatch, html, "on", title=None, variants=_portal_variants(html))
    assert response["snapshot_price_refused"]["body_values_from_the_page"] == ["variants"]
    assert write["seed_data"]["variants"] == []


def test_p2e_some_of_the_pages_variants_are_still_the_pages(monkeypatch) -> None:
    # The person deleted two of the copied components: what is left is still the page's.
    html = _page(*_url_less(COMPONENTS))
    _, write, _ = _create(monkeypatch, html, "on", title=None, variants=_portal_variants(html)[:1])
    assert write["seed_data"]["variants"] == []


def test_p2e_a_typed_price_and_typed_variants_stand(monkeypatch) -> None:
    html = _page(*_url_less(COMPONENTS))
    variants = _portal_variants(html)
    variants[0]["price_amount"] = 48.0                       # a person repriced one: the list is theirs
    response, write, _ = _create(monkeypatch, html, "on", title=None, price_amount=48.0, price_currency="USD",
                                 variants=variants)
    assert response["snapshot_price_refused"]["body_values_from_the_page"] == []
    assert (write["price_amount"], write["price_currency"]) == (48.0, "USD")
    assert write["seed_data"]["variants"] == variants
    added = _portal_variants(html) + [{"variant_id": "GLOW-SET", "price_amount": 48.0, "price_currency": "USD"}]
    _, write, _ = _create(monkeypatch, html, "on", title=None, variants=added)
    assert write["seed_data"]["variants"] == added, "a variant a person added makes the list theirs"


@pytest.mark.parametrize("amount, currency, dropped", [
    (6.0, "USD", True), (6, None, True), (6.001, "usd", True),
    (6.0, "EUR", False),                                     # another currency was typed: not the page's price
    (6.5, "USD", False),
])
def test_p2e_only_the_refused_nodes_own_price_is_the_pages(monkeypatch, amount: Any, currency: Any,
                                                           dropped: bool) -> None:
    html = _page(*_url_less(COMPONENTS))
    _, write, _ = _create(monkeypatch, html, "on", title=None, price_amount=amount, price_currency=currency)
    if dropped:
        assert (write["price_amount"], write["price_currency"]) == (None, None)
    else:
        assert write["price_amount"] == amount


def test_p2e_a_re_create_with_the_prefilled_price_keeps_the_rows_own(monkeypatch) -> None:
    html = _page(*_url_less(COMPONENTS))
    row = _stored_bundle_row()
    row["price_amount"] = 20.0
    _, write, _ = _create(monkeypatch, html, "on", existing=[row], **_prefilled_body(html))
    assert write["price_amount"] == 20.0, "the page's price is not stated: the row's stored one stands"
    assert not write["seed_data"].get("variants"), "nor are the page's variants"
    typed = [{"variant_id": "GLOW-SET", "price_amount": 48.0, "price_currency": "USD"}]
    _, write, _ = _create(monkeypatch, html, "on", existing=[row], **_prefilled_body(html, variants=typed))
    assert write["seed_data"]["variants"] == typed


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_p2e_off_and_shadow_keep_the_prefilled_values_as_before(monkeypatch, mode: str) -> None:
    html = _page(*_url_less(COMPONENTS))
    response, write, messages = _create(monkeypatch, html, mode, **_prefilled_body(html))
    assert response["snapshot_price_refused"] is None
    assert write["price_amount"] == 6.0
    assert len(write["seed_data"]["variants"]) == 3
    if mode == "shadow":
        assert json.loads(messages[0].split(" ", 1)[1])["page_values"] == ["price", "variants"]


def test_p2e_a_bundle_its_own_node_prices_keeps_the_prefilled_values(monkeypatch) -> None:
    # Not refused: the page's price IS the seed's, prefilled or not.
    response, write, _ = _create(monkeypatch, NAMED_BUNDLE_PAGE, "on", **_prefilled_body(NAMED_BUNDLE_PAGE))
    assert response["snapshot_price_refused"] is None
    assert write["price_amount"] == 48.0
    assert write["seed_data"]["variants"]


@pytest.mark.parametrize("variants, expected", [
    (None, []), ([], []), ("x", []), ([{"price_amount": 6.0}], []),          # no id: never the page's
    ([{"variant_id": "NIACINAMIDE-SERUM", "price_amount": "6.00", "price_currency": "usd"}], ["variants"]),
    ([{"variant_id": "NIACINAMIDE-SERUM", "price_amount": None, "price_currency": "USD"}], []),
])
def test_p2e_variant_comparison(variants: Any, expected: List[str]) -> None:
    import routes.employee_products as ep

    evidence = _snapshot_from_html(BUNDLE, _page(*_url_less(COMPONENTS))).evidence
    assert ep._body_values_from_the_page(body_price_amount=None, body_price_currency=None, body_variants=variants,
                                         page_price_amount=6.0, page_price_currency="USD",
                                         evidence=evidence) == expected
    assert ep._body_values_from_the_page(body_price_amount=object(), body_price_currency=object(),
                                         body_variants=object(), page_price_amount=object(),
                                         page_price_currency=None, evidence="not a dict") == []


def _preview(monkeypatch, html: str, mode: str) -> Dict[str, Any]:
    import routes.employee_products as ep

    if mode == "unset":
        monkeypatch.delenv(liveness.AMBIGUOUS_PRICE_GUARD_ENV, raising=False)
    else:
        monkeypatch.setenv(liveness.AMBIGUOUS_PRICE_GUARD_ENV, mode)
    monkeypatch.setattr(ep, "resolve_external_offer", AsyncMock(return_value=_snapshot_from_html(BUNDLE, html)))
    monkeypatch.setattr(ep, "_is_domain_allowed", AsyncMock(return_value=True))
    return asyncio.run(ep.preview_external_seed(
        ep.PreviewExternalSeedRequest(destination_url=BUNDLE, market="US"), current_user={"employee_id": "emp_test"}))


@pytest.mark.parametrize("shape", ["url_less_components", "components_claim_the_page", "components"])
def test_p2e_the_preview_shows_no_refused_price_or_variants_when_on(monkeypatch, shape: str) -> None:
    html = {"url_less_components": _page(*_url_less(COMPONENTS)), "components_claim_the_page": _claiming_bundle_page(),
            "components": BUNDLE_PAGE}[shape]
    out = _preview(monkeypatch, html, "on")
    assert out["ambiguous_price"]["refused"] is True
    assert out["ambiguous_price"]["refusal"] == "several_products"
    assert (out["preview"]["price_amount"], out["preview"]["price_currency"], out["preview"]["variants"]) == (
        None, None, [])
    assert out["preview"]["title"], "the title still shows: creation decides who wrote it"


@pytest.mark.parametrize("mode", ["off", "unset", "shadow"])
def test_p2e_off_and_shadow_previews_show_what_they_showed(monkeypatch, mode: str) -> None:
    out = _preview(monkeypatch, BUNDLE_PAGE, mode)
    assert out["preview"]["price_amount"] == 6.0 and len(out["preview"]["variants"]) == 3
    if mode == "shadow":
        assert out["ambiguous_price"]["refused"] is False and out["ambiguous_price"]["refusal"] == "several_products"
    else:
        assert "ambiguous_price" not in out


def test_p2e_a_preview_of_a_page_its_own_node_prices_is_not_withheld(monkeypatch) -> None:
    out = _preview(monkeypatch, NAMED_BUNDLE_PAGE, "on")
    assert out["ambiguous_price"]["refused"] is False
    assert out["preview"]["price_amount"] == 48.0 and out["preview"]["variants"]
    single = _preview(monkeypatch, _page(COMPONENTS[0]), "on")
    assert "ambiguous_price" not in single


def _ops_script():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "ops_refresh_external_seeds.py"
    spec = importlib.util.spec_from_file_location("ops_refresh_external_seeds_g2764", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("mode, skipped", [("on", True), ("shadow", False), ("off", False)])
def test_p2e_ops_sync_variants_skips_a_page_the_guard_refused(monkeypatch, mode: str, skipped: bool) -> None:
    # The REAL preview's answer, through the script's own preview reader (an in-process transport, no network).
    import httpx

    payload = _preview(monkeypatch, BUNDLE_PAGE, mode)
    ops = _ops_script()
    assert (ops.preview_refused_by_ambiguous_price_guard(payload) is not None) is skipped

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=json.loads(json.dumps(payload, default=str)))

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await ops._preview_seed(client, "http://ops.test", "t", destination_url=BUNDLE, market="US",
                                           timeout=1.0)

    ok, msg, variants, images = asyncio.run(go())
    if skipped:
        assert (ok, msg, variants, images) == (False, "ambiguous_product:several_products", [], [])
    else:
        assert ok and len(variants) == 3


# --------------------------------------------------------------------------- be#2764 review P3-A / P3-B

CHALLENGE_PAGE = "<html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>"


@pytest.mark.parametrize("read", ["none", "challenge"])
@pytest.mark.parametrize("source, expected", [("page", "page"), (None, "page"), ("employee", "employee")])
def test_p3a_a_re_create_re_sending_the_rows_title_keeps_who_wrote_it(monkeypatch, read: str, source: Any,
                                                                      expected: str) -> None:
    # This read has no page name (no read at all), or a challenge page's, to compare with: the row's stamp decides.
    first = COMPONENTS[0]["name"]
    html = _page(*_url_less(COMPONENTS))
    _, write, _ = _create(monkeypatch, CHALLENGE_PAGE, "on", existing=[_stamped_row(first, source)], title=first,
                          no_read=read == "none")
    assert write["seed_data"]["title_source"] == expected
    if expected == "page":
        # The next good read must not let the component name itself.
        row = _stamped_row(first, "page")
        row["seed_data"] = copy.deepcopy(write["seed_data"])
        out = _run(monkeypatch, html, "on", row=row)
        assert out["result"]["price_refresh"]["status"] == "skipped_ambiguous_product"


@pytest.mark.parametrize("brand", ["Brand", None])          # None: the pricing node's own brand
@pytest.mark.parametrize("typed", ["Niacinamide Serum", "niacinamide serum brand", "BRAND Niacinamide Serum BRAND"])
def test_p3b_cutting_the_brand_from_a_prefilled_name_is_not_typing_a_title(monkeypatch, typed: str,
                                                                           brand: Any) -> None:
    html = _page(*_url_less(COMPONENTS))
    response, write, _ = _create(monkeypatch, html, "on", title=typed, brand=brand)
    assert write["seed_data"]["title_source"] == "page"
    assert response["snapshot_price_refused"]["reason"] == "ambiguous_product"
    out = _refresh_what_create_wrote(monkeypatch, write, html)
    assert out["written"].get("price_amount") is None


def test_p3b_a_patch_cutting_the_brand_keeps_the_stamp(monkeypatch) -> None:
    html = _page(*_url_less(COMPONENTS))
    _, write, _ = _create(monkeypatch, html, "on", title=None)
    row = _row_from_create(write)
    saved = _patch(monkeypatch, row, title="Niacinamide Serum", notes="tidied")
    assert saved["seed_data"]["title_source"] == "page"
    out = _run(monkeypatch, html, "on", row=saved)
    assert out["written"].get("price_amount") is None, "the component must not name itself"
    assert _patch(monkeypatch, row, title="Niacinamide Toner")["seed_data"]["title_source"] == "employee"


@pytest.mark.parametrize("a, b, brands, same", [
    ("Niacinamide Serum", "Brand Niacinamide Serum", ("Brand",), True),
    ("Niacinamide Serum Brand", "Brand Niacinamide Serum", ("Brand",), True),
    ("Niacinamide Serum", "Brand Niacinamide Serum", (), False),
    ("Niacinamide Serum", "Brand Niacinamide Serum", ({"name": "Brand"},), True),
    ("Brand", "Brand Niacinamide Serum", ("Brand",), False),          # the brand alone is not the product's name
    ("Brand", "Brand", ("Brand",), True),
    ("Niacinamide Toner", "Brand Niacinamide Serum", ("Brand",), False),
    ("세럼", "브랜드 세럼", ("브랜드",), True),
    (None, "Brand Niacinamide Serum", ("Brand",), False),
    ("Niacinamide Serum", None, ("Brand", None, 7), False),
])
def test_p3b_titles_compare_without_a_brand_prefix_or_suffix(a: Any, b: Any, brands: Any, same: bool) -> None:
    import routes.employee_products as ep

    assert ep._same_title_but_brand(a, b, brands) is same
