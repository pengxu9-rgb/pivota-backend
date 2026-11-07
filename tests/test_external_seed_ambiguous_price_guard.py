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
    seed_data = {"title": BUNDLE_TITLE, "brand": "Brand",
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
                                                   "url_relation": "other_product", "title_dice": 0.0}
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
                           "url_relation": "other_product", "title_dice": 0.25})
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
         "title_dice": 0.25, "url_relation": "other_product"},
        {"host": "b.example", "mode": "on", "priced_identities": 1, "refusal": "listing", "seed_id": "eps_3",
         "title_dice": None, "url_relation": None},
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


def _create(monkeypatch, html: str, mode: str, *, existing: Optional[List[Dict[str, Any]]] = None,
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
    monkeypatch.setattr(ep, "resolve_external_offer", AsyncMock(return_value=_snapshot_from_html(BUNDLE, html)))
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
            {"mode": "shadow", "priced_identities": 3, "refusal": "several_products", "title_dice": 0.0,
             "url_relation": "other_product"}]
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
