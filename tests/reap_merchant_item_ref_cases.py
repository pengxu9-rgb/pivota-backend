"""A purchase that names the product by the MERCHANT's product URL instead of our `product_key`.

Shared by the real SQLite and PostgreSQL collectors (test_reap_merchant_item_ref*.py). The catalog, seed,
proof and eligibility reads are real; only auth is fixture-controlled and the route suites' network ban
stays in force. The fixture is the selection-prepare suite's (one seed, one mirror product, one variant).
"""
from __future__ import annotations

import json

import pytest

from db.database import IS_POSTGRES, database
from db import reap_agentic_ledger as ledger
import routes.agent_commerce_reap as reap
import services.merchant_item_resolver as merchant_item
import services.reap_agentic_purchase as svc
from reap_selection_prepare_cases import (  # noqa: F401  (prepare_seed_cleanup is an autouse fixture)
    BASE, DOMAIN, PRODUCT, SEED, SKU, VARIANT, body, create_body, error, prepare_seed_cleanup, seed,
    unchanged_state,
)

URL = f"https://{DOMAIN}/products/lip-ink"
#: The fixture's store host spelled in mixed case (the shared fixture owns the host; nothing here spells it).
MIXED = DOMAIN.title()
CREATE = "/agent/v2/commerce/reap/purchases"
DOOR = "/agent/v2/commerce/purchases"


def url_body(url=URL, **changes):
    request = body(**changes)
    request.pop("product_key")
    request["product_url"] = url
    return request


def url_create_body(witness, key, url=URL):
    request = create_body(witness, key, by="variant_id")
    request.pop("product_key")
    request["product_url"] = url
    return request


async def add_product(product_key, *, suppressed=False, source_system="shopify_ingest", source_ref=None):
    await database.execute(
        "INSERT INTO catalog_products (product_key,merchant_id,platform,source_product_id,title,brand,source_domain,"
        "source_system,source_ref,seed_kind,suppression_reason) VALUES (:pk,'merch_other','shopify',:pk,'Other',"
        "'Brand A',:domain,:source,:ref,'self',:reason)",
        {"pk": product_key, "domain": DOMAIN, "source": source_system, "ref": source_ref,
         "reason": "test_suppressed" if suppressed else None},
    )


async def add_seed(seed_id, *, url=URL, attached=None, market="US", domain=DOMAIN):
    await database.execute(
        "INSERT INTO external_product_seeds (id,status,domain,market,destination_url,attached_product_key,seed_data) "
        "VALUES (:id,'active',:domain,:market,:url,:pk,'{}')",
        {"id": seed_id, "domain": domain, "market": market, "url": url, "pk": attached},
    )


def pilot_scope(agent_id, product_key):
    """A complete pilot scope (every key the scope reader requires) for this fixture's store."""
    return json.dumps({"agent_ids": [agent_id], "merchant_domains": [DOMAIN], "markets": ["US"],
                       "product_keys": [product_key], "quantities": [1], "variant_keys": ["shopify:" + VARIANT],
                       "currency": "USD", "max_total_minor": 100000})


def forbid_sql(patches):
    async def forbidden(*args, **kwargs):
        raise AssertionError("an invalid product_url reached SQL")
    for name in ("fetch_one", "fetch_all", "fetch_val", "execute"):
        patches.setattr(database, name, forbidden)


# ── prepare ──────────────────────────────────────────────────────────────────────────────────────


async def test_prepare_by_product_url_is_exactly_the_product_key_witness(client, monkeypatch):
    await seed(monkeypatch)
    before = await unchanged_state()
    by_key = await client.post(BASE, json=body())
    by_url = await client.post(BASE, json=url_body())
    assert by_key.status_code == 200 and by_url.status_code == 200, by_url.text
    assert by_url.json() == by_key.json()
    assert by_url.json()["selection"]["product_key"] == PRODUCT
    assert await unchanged_state() == before


@pytest.mark.parametrize("spelling", [
    f"https://www.{DOMAIN}/products/lip-ink",
    f"https://{DOMAIN}/products/lip-ink/",
    f"HTTPS://{MIXED}/products/Lip-Ink?variant=49819267301653#reviews",
    f"http://{DOMAIN}/products/lip-ink",
    f"https://{DOMAIN}:443/products/lip-ink",
])
async def test_prepare_resolves_every_spelling_of_the_same_page(client, monkeypatch, spelling):
    await seed(monkeypatch)
    response = await client.post(BASE, json=url_body(spelling))
    assert response.status_code == 200, response.text
    assert response.json()["selection"]["product_key"] == PRODUCT


@pytest.mark.parametrize("bad", [
    "https://other-store.example/products/lip-ink",       # another store
    f"https://uk.{DOMAIN}/products/lip-ink",           # a subdomain is another store
    f"https://{DOMAIN}.evil.example/products/lip-ink",  # a lookalike
    "https://127.0.0.1/products/lip-ink",                  # an IP literal
    f"https://{DOMAIN}/",                               # no product page
    f"https://{DOMAIN}",
    f"ftp://{DOMAIN}/products/lip-ink",
    f"{DOMAIN}/products/lip-ink",
    "https://buyer:pw" + chr(64) + f"{DOMAIN}/products/lip-ink",  # userinfo
    f"https://{DOMAIN}:8443/products/lip-ink",          # a non-default port
    f"https://{DOMAIN}/products/lip ink",               # whitespace inside
    f"https://{DOMAIN}/products/lip-ink\u0000",         # NUL
    f"https://{DOMAIN}/products/lip\u200b-ink",         # zero-width space (a format character)
    f"\u00a0https://{DOMAIN}/products/lip-ink",         # NBSP, never silently trimmed
    f" https://{DOMAIN}/products/lip-ink",              # leading space, never silently trimmed
    f"https://{DOMAIN}/products/lip-ink\u00e9",         # raw non-ASCII (Python and SQL fold it apart)
    f"https://{DOMAIN}/" + "a" * 2100,                  # longer than the cap
])
async def test_prepare_refuses_an_unusable_product_url_before_any_sql(client, monkeypatch, bad):
    monkeypatch.setenv("REAP_AGENTIC_CART_LINK_ENABLED", "1")
    with monkeypatch.context() as patches:
        forbid_sql(patches)
        response = await client.post(BASE, json=url_body(bad))
    assert response.status_code == 400, response.text
    assert error(response) == "invalid_request"


@pytest.mark.parametrize("shape", ["both", "neither", "not_a_string"])
async def test_prepare_needs_exactly_one_product_selector(client, monkeypatch, shape):
    monkeypatch.setenv("REAP_AGENTIC_CART_LINK_ENABLED", "1")
    request = url_body()
    if shape == "both":
        request["product_key"] = PRODUCT
    elif shape == "neither":
        request.pop("product_url")
    else:
        request["product_url"] = 12345
    with monkeypatch.context() as patches:
        forbid_sql(patches)
        response = await client.post(BASE, json=request)
    assert response.status_code == 400 and error(response) == "invalid_request", response.text


async def test_a_page_with_no_seed_is_row_not_found(client, monkeypatch):
    await seed(monkeypatch)
    response = await client.post(BASE, json=url_body(f"https://{DOMAIN}/products/another-thing"))
    assert response.status_code == 409 and error(response) == "row_not_found", response.text


async def test_a_seed_of_another_market_is_not_this_buyers_product(client, monkeypatch):
    await seed(monkeypatch)
    # Eligible in SG too, so the request passes the merchant gates and reaches resolution.
    await database.execute("INSERT INTO tierb_cart_link_eligibility (shop_domain,market,verdict,checked_at) "
                           "VALUES (:d,'SG','ELIGIBLE',CURRENT_TIMESTAMP)", {"d": DOMAIN})
    response = await client.post(BASE, json=url_body(market_country="SG"))
    assert response.status_code == 409 and error(response) == "row_not_found", response.text


@pytest.mark.parametrize("wildcard_path", ["/products/lip_ink", "/products/lip%ink", "/products/lip!ink"])
async def test_like_wildcards_in_the_path_are_literal(client, monkeypatch, wildcard_path):
    """`_` and `%` are SQL LIKE wildcards: unescaped, `lip_ink` would match the stored `lip-ink`."""
    await seed(monkeypatch)
    response = await client.post(BASE, json=url_body(f"https://{DOMAIN}" + wildcard_path))
    assert response.status_code == 409 and error(response) == "row_not_found", response.text


async def test_a_page_whose_seeds_name_two_products_is_ambiguous_never_a_pick(client, monkeypatch):
    await seed(monkeypatch)
    await add_product(PRODUCT + "-twin")
    await add_seed("twin-seed", attached=PRODUCT + "-twin")
    response = await client.post(BASE, json=url_body())
    assert response.status_code == 409 and error(response) == "product_ambiguous", response.text


async def test_two_seeds_of_one_page_naming_one_product_resolve(client, monkeypatch):
    await seed(monkeypatch)
    await add_seed("second-seed-same-product", attached=PRODUCT)
    response = await client.post(BASE, json=url_body())
    assert response.status_code == 200, response.text
    assert response.json()["selection"]["product_key"] == PRODUCT


async def test_a_suppressed_attachment_names_nothing_and_never_falls_back_to_the_mirror(client, monkeypatch):
    """The catalog withdrew the attached product; the seed's mirror (PRODUCT) must not bring it back."""
    await seed(monkeypatch)  # PRODUCT is SEED's mirror (source_ref = SEED)
    await add_product("prod::suppressed", suppressed=True)
    await database.execute(
        "UPDATE external_product_seeds SET attached_product_key='prod::suppressed' WHERE id=:id", {"id": SEED})
    response = await client.post(BASE, json=url_body())
    assert response.status_code == 409 and error(response) == "row_not_found", response.text
    # At the resolver itself too (the catalog load downstream would also refuse a suppressed product).
    normalized = merchant_item.normalize_product_url(URL, DOMAIN)
    with pytest.raises(svc.PurchaseRefused) as refused:
        await merchant_item.resolve_product_key(normalized_url=normalized, market_country="US")
    assert refused.value.reason == "row_not_found"


async def test_an_unattached_seed_names_nothing_even_with_a_live_mirror(monkeypatch):
    """The lane buys a mirror only when its seed is attached to it, so an unattached seed's mirror is not
    an answer (the reviewer's probe: prepare by URL then refused `row_variant_unverified`)."""
    await seed(monkeypatch)  # PRODUCT is SEED's mirror (source_ref = SEED)
    await database.execute(
        "UPDATE external_product_seeds SET attached_product_key=NULL WHERE id=:id", {"id": SEED})
    normalized = merchant_item.normalize_product_url(URL, DOMAIN)
    with pytest.raises(svc.PurchaseRefused) as refused:
        await merchant_item.resolve_product_key(normalized_url=normalized, market_country="US")
    assert refused.value.reason == "row_not_found"


async def test_a_seed_matched_by_its_canonical_url_resolves(monkeypatch):
    await seed(monkeypatch)
    await database.execute(
        f"UPDATE external_product_seeds SET canonical_url='https://{DOMAIN}/products/lip-ink-canonical', "
        f"destination_url='https://{DOMAIN}/collections/x?utm=1' WHERE id=:id", {"id": SEED})
    normalized = merchant_item.normalize_product_url(f"https://{DOMAIN}/products/lip-ink-canonical", DOMAIN)
    assert await merchant_item.resolve_product_key(normalized_url=normalized, market_country="US") == PRODUCT


async def test_a_seed_that_spells_its_store_www_is_found(monkeypatch):
    await seed(monkeypatch)
    await database.execute(
        "UPDATE external_product_seeds SET domain=:d WHERE id=:id", {"d": "www." + DOMAIN, "id": SEED})
    normalized = merchant_item.normalize_product_url(URL, DOMAIN)
    assert await merchant_item.resolve_product_key(normalized_url=normalized, market_country="US") == PRODUCT


async def test_the_path_match_is_anchored_so_a_longer_page_never_crowds_the_cap(monkeypatch):
    """`/products/lip-ink` must not read `/products/lip-ink-mini`: unanchored, that second seed would fill
    a cap of one and answer `product_ambiguous` for a page with exactly one seed."""
    await seed(monkeypatch)
    await add_product(PRODUCT + "-mini")
    await add_seed("mini-seed", url=f"https://{DOMAIN}/products/lip-ink-mini", attached=PRODUCT + "-mini")
    monkeypatch.setattr(merchant_item, "MAX_SEED_CANDIDATES", 1)
    normalized = merchant_item.normalize_product_url(URL, DOMAIN)
    assert await merchant_item.resolve_product_key(normalized_url=normalized, market_country="US") == PRODUCT


async def test_inactive_seeds_and_seeds_of_another_store_are_not_read(client, monkeypatch):
    await seed(monkeypatch)
    await add_product(PRODUCT + "-other-store")
    await add_seed("other-store-seed", attached=PRODUCT + "-other-store", domain="other-store.example",
                   url="https://other-store.example/products/lip-ink")
    await add_product(PRODUCT + "-retired")
    await add_seed("retired-seed", attached=PRODUCT + "-retired")
    await database.execute("UPDATE external_product_seeds SET status='inactive' WHERE id='retired-seed'")
    response = await client.post(BASE, json=url_body())
    assert response.status_code == 200, response.text
    assert response.json()["selection"]["product_key"] == PRODUCT


# ── the resolver itself ─────────────────────────────────────────────────────────────────────────


async def test_a_live_attachment_wins_over_the_mirror(monkeypatch):
    await seed(monkeypatch)
    await add_product("prod::enriched")
    await database.execute(
        "UPDATE external_product_seeds SET attached_product_key='prod::enriched' WHERE id=:id", {"id": SEED})
    normalized = merchant_item.normalize_product_url(URL, DOMAIN)
    assert await merchant_item.resolve_product_key(normalized_url=normalized, market_country="US") == "prod::enriched"


async def test_a_read_that_fills_the_candidate_cap_is_refused_not_trusted(monkeypatch):
    await seed(monkeypatch)
    await add_seed("cap-seed", attached=PRODUCT)
    monkeypatch.setattr(merchant_item, "MAX_SEED_CANDIDATES", 1)
    normalized = merchant_item.normalize_product_url(URL, DOMAIN)
    with pytest.raises(svc.PurchaseRefused) as refused:
        await merchant_item.resolve_product_key(normalized_url=normalized, market_country="US")
    assert refused.value.reason == "product_ambiguous"


# ── create, replay, recovery ────────────────────────────────────────────────────────────────────


async def test_create_by_product_url_buys_our_record_and_replays_without_resolving(client, monkeypatch):
    await seed(monkeypatch)
    witness = (await client.post(BASE, json=url_body())).json()["selection"]
    original = url_create_body(witness, "by-url-original")
    created = await client.post(CREATE, json=original)
    assert created.status_code == 202, created.text
    row = dict(await database.fetch_one("SELECT * FROM reap_agentic_purchases"))
    assert row["product_key"] == PRODUCT and row["variant_key"] == "shopify:" + VARIANT
    assert "lip-ink" not in str(row.get("cart_url") or "").split("/cart/")[-1]

    # KEY FIRST: a retry replays the original even when today's catalog could no longer resolve the URL.
    async def unresolvable(**kwargs):
        raise AssertionError("a replay resolved the product URL")
    monkeypatch.setattr(merchant_item, "resolve_product_key", unresolvable)
    again = await client.post(CREATE, json=original)
    assert again.status_code == 202 and again.json()["purchase_id"] == created.json()["purchase_id"], again.text

    # The same key with the product named by its catalog key is a DIFFERENT request.
    by_key = create_body(witness, "by-url-original", by="variant_id")
    conflict = await client.post(CREATE, json=by_key)
    assert conflict.status_code == 409 and error(conflict) == "idempotency_conflict", conflict.text

    # Recovery is read-only and never resolves; it finds the attempt by the URL fingerprint.
    monkeypatch.setenv("REAP_AGENTIC_CREATE_ENABLED", "0")
    recovered = await client.post(CREATE + "/recover", json=original)
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["id"] == created.json()["purchase_id"]
    assert await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_purchases") == 1


async def test_create_refuses_a_product_url_on_the_variant_lane_before_any_sql(client, monkeypatch):
    await seed(monkeypatch)
    witness = (await client.post(BASE, json=body())).json()["selection"]
    request = url_create_body(witness, "variant-lane-url")
    request["item_source"] = "reap_variant"
    request.pop("variant_id")
    with monkeypatch.context() as patches:
        forbid_sql(patches)
        response = await client.post(CREATE, json=request)
    assert response.status_code == 400 and error(response) == "invalid_request", response.text


@pytest.fixture
def door_mounted(prepare_auth_app):
    """The rail-neutral door on this collector's app: the SQLite arm's is the real app (mounted already);
    the PostgreSQL arm's is the Reap router alone, so the door is added for this test and removed after."""
    import routes.agent_commerce_purchases as unified
    paths = {getattr(r, "path", None) for r in prepare_auth_app.router.routes}
    added = []
    if DOOR not in paths:
        before = list(prepare_auth_app.router.routes)
        prepare_auth_app.include_router(unified.router)
        added = [r for r in prepare_auth_app.router.routes if r not in before]
    yield
    for r in added:
        prepare_auth_app.router.routes.remove(r)


async def test_both_doors_fingerprint_a_product_url_request_identically(client, monkeypatch, door_mounted):
    """One key is one attempt on either rail: the rail-neutral door and the Reap route must hash a URL
    request byte-for-byte alike, or a retry through the other door would be an `idempotency_conflict`."""
    await seed(monkeypatch)
    witness = (await client.post(BASE, json=url_body())).json()["selection"]
    request = url_create_body(witness, "both-doors")
    seen = []

    async def capture(**kwargs):
        seen.append(kwargs["request_hash"])
        raise svc.PurchaseRefused("idempotency_conflict")
    monkeypatch.setattr(reap, "_replayed_purchase_id", capture)
    monkeypatch.setenv("PAYMENT_ORCHESTRATOR_ENABLED", "1")
    monkeypatch.setenv("AGENT_PURCHASE_LEDGER_ENABLED", "1")
    door = await client.post(DOOR, json=request)
    assert error(door) == "idempotency_conflict" and len(seen) == 1, door.text
    route = await client.post(CREATE, json=request)
    assert error(route) == "idempotency_conflict", route.text
    assert len(seen) == 2 and seen[0] == seen[1]
    by_key = create_body(witness, "both-doors", by="variant_id")
    await client.post(CREATE, json=by_key)
    assert seen[2] != seen[0]


async def test_the_door_resolves_a_product_url_before_opening_on_the_a2a_rail(client, monkeypatch, door_mounted):
    """The A2A rail opens in the door itself (Reap re-reads the raw body): its catalog load must be handed
    OUR record's key, resolved after the replay lookup and the merchant gates, never the URL and never None."""
    from services.payment_orchestration import router as rail_router

    await seed(monkeypatch)
    witness = (await client.post(BASE, json=url_body())).json()["selection"]
    monkeypatch.setenv("PAYMENT_ORCHESTRATOR_ENABLED", "1")
    monkeypatch.setenv("AGENT_PURCHASE_LEDGER_ENABLED", "1")
    monkeypatch.setattr(rail_router, "choose_rail", lambda **kwargs: rail_router.RailChoice(
        rail="a2a", basis="orchestrator_selected_rail"))
    loaded = {}

    async def stop_at_the_load(**kwargs):
        loaded.update(kwargs)
        raise svc.PurchaseRefused("row_unpriced")
    monkeypatch.setattr(reap, "_load_cart_link_item", stop_at_the_load)
    response = await client.post(DOOR, json=url_create_body(witness, "door-a2a"))
    assert error(response) == "row_unpriced", response.text
    assert loaded["product_key"] == PRODUCT


async def test_an_unescaped_wildcard_never_widens_the_candidate_read(monkeypatch):
    """`%` in the caller's path must be literal in SQL too, not only in the exact comparison: a wildcard
    that widened the read past the cap would answer `product_ambiguous` for a page with no seed at all."""
    await seed(monkeypatch)
    await add_seed("wild-a", url=f"https://{DOMAIN}/products/aaa-1", attached=PRODUCT)
    await add_seed("wild-b", url=f"https://{DOMAIN}/products/aaa-2", attached=PRODUCT)
    monkeypatch.setattr(merchant_item, "MAX_SEED_CANDIDATES", 1)
    normalized = merchant_item.normalize_product_url(f"https://{DOMAIN}/products/aaa%", DOMAIN)
    with pytest.raises(svc.PurchaseRefused) as refused:
        await merchant_item.resolve_product_key(normalized_url=normalized, market_country="US")
    assert refused.value.reason == "row_not_found"


def test_a_url_fingerprint_differs_from_the_same_string_sent_as_a_product_key(monkeypatch):
    """The versioned prefix keeps a URL request apart from a `product_key` equal to its normalised URL, and a
    key (or a sku key) spelled with a fingerprint prefix is refused, so no key request hashes like a URL or
    `variant_id` one."""
    from types import SimpleNamespace
    monkeypatch.setenv("REAP_AGENTIC_CART_LINK_ENABLED", "1")
    cart = dict(item_source="cart_link")
    normalized = merchant_item.normalize_product_url(URL, DOMAIN)
    by_url = reap._product_selector(SimpleNamespace(product_url=URL, product_key=None, variant_id=VARIANT, **cart), DOMAIN)
    by_key = reap._product_selector(SimpleNamespace(product_url=None, product_key=normalized, **cart), DOMAIN)
    assert by_url[1] == "product_url:v1:" + normalized and by_url[1] != by_key[1]
    assert (by_url[0], by_url[2]) == (None, normalized) and by_key[0] == normalized
    for spelled in (by_url[1], "product_url:" + normalized):
        with pytest.raises(svc.PurchaseRefused) as refused:
            reap._product_selector(SimpleNamespace(product_url=None, product_key=spelled, **cart), DOMAIN)
        assert refused.value.reason == "invalid_request"
    with pytest.raises(svc.PurchaseRefused) as refused:
        reap._variant_selector(SimpleNamespace(variant_id=None, variant_key="variant_id:" + VARIANT, **cart))
    assert refused.value.reason == "invalid_request"


async def test_a_product_key_spelled_as_a_url_fingerprint_never_replays_the_url_purchase(client, monkeypatch):
    """The reviewer's repro: under one idempotency key, a URL create then a key create spelled
    `product_url:<normalised>` replayed the URL purchase. It is refused before any lookup."""
    await seed(monkeypatch)
    witness = (await client.post(BASE, json=url_body())).json()["selection"]
    created = await client.post(CREATE, json=url_create_body(witness, "spelled-as-url"))
    assert created.status_code == 202, created.text
    spelled = create_body(witness, "spelled-as-url", by="variant_id")
    spelled["product_key"] = merchant_item.PRODUCT_URL_HASH_PREFIX + merchant_item.normalize_product_url(URL, DOMAIN)
    with monkeypatch.context() as patches:
        forbid_sql(patches)
        response = await client.post(CREATE, json=spelled)
    assert response.status_code == 400 and error(response) == "invalid_request", response.text



# ── the merchant gates come before any resolution (review P2) ───────────────────────────────────


def forbid_resolution(monkeypatch):
    async def forbidden(**kwargs):
        raise AssertionError("the product URL was resolved before the merchant gates")
    monkeypatch.setattr(merchant_item, "resolve_product_key", forbidden)


async def test_prepare_for_a_merchant_we_never_enabled_is_refused_like_a_key_request(client, monkeypatch):
    await seed(monkeypatch)
    await database.execute("DELETE FROM tierb_cart_link_eligibility")
    by_key = await client.post(BASE, json=body())
    forbid_resolution(monkeypatch)
    by_url = await client.post(BASE, json=url_body())
    assert by_key.status_code == by_url.status_code == 409
    assert error(by_key) == error(by_url) == "merchant_not_eligible", by_url.text


async def test_create_for_a_merchant_we_never_enabled_is_refused_like_a_key_request(client, monkeypatch):
    await seed(monkeypatch)
    witness = (await client.post(BASE, json=url_body())).json()["selection"]
    await database.execute("DELETE FROM tierb_cart_link_eligibility")
    forbid_resolution(monkeypatch)
    response = await client.post(CREATE, json=url_create_body(witness, "never-enabled"))
    assert response.status_code == 409 and error(response) == "merchant_not_eligible", response.text


async def test_a_pilot_that_excludes_the_agent_refuses_before_any_resolution(client, monkeypatch):
    await seed(monkeypatch)
    witness = (await client.post(BASE, json=url_body())).json()["selection"]
    monkeypatch.setenv("REAP_AGENTIC_PILOT_SCOPE", pilot_scope("agent_not_this_one", PRODUCT))
    forbid_resolution(monkeypatch)
    for path, request in ((BASE, url_body()), (CREATE, url_create_body(witness, "pilot-agent"))):
        response = await client.post(path, json=request)
        # A pilot refusal answers as the dark rail does (the route never tells a prober who is enrolled).
        assert response.status_code == 404 and error(response) == "not_available_on_this_rail", response.text


async def test_a_pilot_that_lists_another_product_refuses_the_resolved_one(client, monkeypatch, prepare_auth_agent):
    await seed(monkeypatch)
    witness = (await client.post(BASE, json=url_body())).json()["selection"]
    monkeypatch.setenv("REAP_AGENTIC_PILOT_SCOPE", pilot_scope(prepare_auth_agent, "prod::some-other-product"))
    real = merchant_item.resolve_product_key
    calls = []

    async def spy(**kwargs):
        calls.append(kwargs)
        return await real(**kwargs)
    monkeypatch.setattr(merchant_item, "resolve_product_key", spy)

    async def no_load(**kwargs):
        raise AssertionError("an out-of-pilot product reached the catalog load")
    monkeypatch.setattr(reap, "_load_cart_link_item", no_load)
    for path, request in ((BASE, url_body()), (CREATE, url_create_body(witness, "pilot-product"))):
        before = len(calls)
        response = await client.post(path, json=request)
        assert response.status_code == 404 and error(response) == "not_available_on_this_rail", response.text
        assert len(calls) == before + 1  # refused on the RESOLVED key, after the pre-check passed
    assert await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_purchases") == 0


def test_a_pilot_pre_check_that_names_a_product_is_refused():
    with pytest.raises(svc.PurchaseRefused) as refused:
        svc.enforce_pilot_scope(agent_id="a", merchant_domain=DOMAIN, market_country="US",
                                product_key=PRODUCT, quantity=1, resolved=False, product_pending=True)
    assert refused.value.reason == "invalid_request"


# ── resume (review P2: untested) ────────────────────────────────────────────────────────────────


async def test_a_purchase_made_by_product_url_resumes_by_resolving_the_url_again(client, monkeypatch):
    await seed(monkeypatch)
    witness = (await client.post(BASE, json=url_body())).json()["selection"]
    original = url_create_body(witness, "by-url-resume")
    created = await client.post(CREATE, json=original)
    assert created.status_code == 202, created.text
    pid = created.json()["purchase_id"]
    aged = ("clock_timestamp()-INTERVAL '1000 seconds'" if IS_POSTGRES else "datetime('now','-1000 seconds')")
    await database.execute(f"UPDATE reap_agentic_purchases SET created_at={aged} WHERE id=:id", {"id": pid})
    assert await ledger.scrub_reconciling_purchase_pii() == [pid]
    resumed = await client.post(CREATE + f"/{pid}/resume", json=original)
    assert resumed.status_code == 200, resumed.text
    assert (await ledger.get_purchase_internal(pid))["contact_revision"] == 1


async def test_a_url_resume_whose_page_now_names_another_record_is_a_changed_selection(client, monkeypatch):
    await seed(monkeypatch)
    witness = (await client.post(BASE, json=url_body())).json()["selection"]
    original = url_create_body(witness, "by-url-resume-moved")
    created = await client.post(CREATE, json=original)
    assert created.status_code == 202, created.text
    pid = created.json()["purchase_id"]
    aged = ("clock_timestamp()-INTERVAL '1000 seconds'" if IS_POSTGRES else "datetime('now','-1000 seconds')")
    await database.execute(f"UPDATE reap_agentic_purchases SET created_at={aged} WHERE id=:id", {"id": pid})
    assert await ledger.scrub_reconciling_purchase_pii() == [pid]
    before = dict(await ledger.get_purchase_internal(pid))
    moved = PRODUCT + "-moved"
    # The page now names another record (the resolver's answer), and that record WOULD load as the same
    # variant at the same price (its reads are answered from the original's, whose seed proof still holds),
    # so the only thing that stands between it and a resumed purchase is the row compare.
    resolved = []

    async def resolves_to_the_moved_record(**kwargs):
        resolved.append(kwargs["normalized_url"])
        return moved
    monkeypatch.setattr(merchant_item, "resolve_product_key", resolves_to_the_moved_record)
    real_load, real_variant = reap._load_cart_link_item, reap._resolve_selected_variant_key
    seen = []

    async def load_as_original(**kwargs):
        seen.append(kwargs["product_key"])
        return await real_load(**{**kwargs, "product_key": PRODUCT})

    async def variant_as_original(**kwargs):
        return await real_variant(**{**kwargs, "product_key": PRODUCT})
    monkeypatch.setattr(reap, "_load_cart_link_item", load_as_original)
    monkeypatch.setattr(reap, "_resolve_selected_variant_key", variant_as_original)
    resumed = await client.post(CREATE + f"/{pid}/resume", json=original)
    assert resumed.status_code == 409 and error(resumed) == "resume_selection_changed", resumed.text
    assert seen == [moved] and resolved == [merchant_item.normalize_product_url(URL, DOMAIN)]
    after = dict(await ledger.get_purchase_internal(pid))
    assert after["contact_revision"] == before["contact_revision"]
    assert after["product_key"] == PRODUCT and after["state"] == before["state"]


# ── review 2: the URL's variant, the seed's page, the left side of the match, retries ───────────


async def test_a_url_create_must_name_its_variant_and_the_urls_variant_must_be_it(client, monkeypatch):
    """The reviewer's P2 repro: a variant-grain seed (`?variant=A`) and a caller's `?variant=999` with no
    `variant_id` bought A. Every such body is now refused before any SQL; the agreeing one buys exactly it."""
    await seed(monkeypatch)
    await database.execute("UPDATE external_product_seeds SET destination_url=:u WHERE id=:id",
                           {"u": URL + "?variant=" + VARIANT, "id": SEED})
    await add_seed("variant-999-seed", url=URL + "?variant=999")
    witness = (await client.post(BASE, json=url_body())).json()["selection"]

    no_variant = url_create_body(witness, "url-no-variant", url=URL + "?variant=999")
    no_variant.pop("variant_id")
    disagreeing = url_create_body(witness, "url-disagrees", url=URL + "?variant=111")
    repeated = url_create_body(witness, "url-repeated", url=URL + "?variant=" + VARIANT + "&variant=111")
    cased = url_create_body(witness, "url-cased", url=URL + "?Variant=111")
    bare = url_create_body(witness, "url-bare", url=URL)
    bare.pop("variant_id")
    for request in (no_variant, disagreeing, repeated, cased, bare):
        with monkeypatch.context() as patches:
            forbid_sql(patches)
            response = await client.post(CREATE, json=request)
        assert response.status_code == 400 and error(response) == "invalid_request", (request["product_url"], response.text)
    with monkeypatch.context() as patches:
        forbid_sql(patches)
        prepared = await client.post(BASE, json=url_body(URL + "?variant=111"))
    assert prepared.status_code == 400 and error(prepared) == "invalid_request", prepared.text
    assert await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_purchases") == 0

    agreeing = await client.post(CREATE, json=url_create_body(witness, "url-agrees", url=URL + "?variant=" + VARIANT))
    assert agreeing.status_code == 202, agreeing.text
    row = dict(await database.fetch_one("SELECT * FROM reap_agentic_purchases"))
    assert row["product_key"] == PRODUCT and row["variant_key"] == "shopify:" + VARIANT


async def test_a_destination_beside_a_different_canonical_page_is_not_the_seeds_page(client, monkeypatch):
    """The lane's page of a seed is `canonical_url` else `destination_url`; a destination that sits beside
    another canonical page names nothing."""
    await seed(monkeypatch)
    await database.execute(
        "UPDATE external_product_seeds SET canonical_url=:c, destination_url=:d WHERE id=:id",
        {"c": URL, "d": f"https://{DOMAIN}/products/a-different-page", "id": SEED})
    response = await client.post(BASE, json=url_body(f"https://{DOMAIN}/products/a-different-page"))
    assert response.status_code == 409 and error(response) == "row_not_found", response.text
    assert (await client.post(BASE, json=url_body())).json()["selection"]["product_key"] == PRODUCT


async def test_an_empty_canonical_url_falls_back_to_the_destination(monkeypatch):
    await seed(monkeypatch)
    await database.execute("UPDATE external_product_seeds SET canonical_url='' WHERE id=:id", {"id": SEED})
    normalized = merchant_item.normalize_product_url(URL, DOMAIN)
    assert await merchant_item.resolve_product_key(normalized_url=normalized, market_country="US") == PRODUCT


async def test_the_left_side_of_the_match_is_the_whole_host_and_path(client, monkeypatch):
    """SQL anchors the path on its right only; the exact compare carries the left. A seed one folder deeper
    (`/en-gb/products/lip-ink`) under the same store, attached to another product, is not this page."""
    await seed(monkeypatch)
    await add_product(PRODUCT + "-gb")
    await add_seed("gb-seed", url=f"https://{DOMAIN}/en-gb/products/lip-ink", attached=PRODUCT + "-gb")
    response = await client.post(BASE, json=url_body())
    assert response.status_code == 200, response.text
    assert response.json()["selection"]["product_key"] == PRODUCT


async def test_a_seed_with_no_or_an_unlowered_domain_is_not_read(client, monkeypatch):
    """Documented fail-closed rule: the storefront filter is the indexed `domain`, so a seed placed by its URL
    alone (NULL domain) or with an upper-case one is not a candidate, even at this exact page."""
    await seed(monkeypatch)
    await add_product(PRODUCT + "-nodomain")
    await add_seed("nodomain-seed", attached=PRODUCT + "-nodomain", domain=None)
    await add_product(PRODUCT + "-upper")
    await add_seed("upper-seed", attached=PRODUCT + "-upper", domain=DOMAIN.upper())
    response = await client.post(BASE, json=url_body())
    assert response.status_code == 200, response.text
    assert response.json()["selection"]["product_key"] == PRODUCT


async def test_a_retry_with_another_spelling_of_the_same_page_replays(client, monkeypatch):
    """The fingerprint holds the NORMALISED URL: a retry that spells the page another way (case, `www.`, a
    trailing slash, the agreeing `variant=`, a fragment) is the same purchase, replayed without resolving."""
    await seed(monkeypatch)
    witness = (await client.post(BASE, json=url_body())).json()["selection"]
    created = await client.post(CREATE, json=url_create_body(witness, "two-spellings"))
    assert created.status_code == 202, created.text

    async def unresolvable(**kwargs):
        raise AssertionError("a replay resolved the product URL")
    monkeypatch.setattr(merchant_item, "resolve_product_key", unresolvable)
    respelled = url_create_body(
        witness, "two-spellings", url=f"HTTPS://WWW.{MIXED}/products/Lip-Ink/?variant=" + VARIANT + "#reviews")
    again = await client.post(CREATE, json=respelled)
    assert again.status_code == 202 and again.json()["purchase_id"] == created.json()["purchase_id"], again.text
    assert await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_purchases") == 1


# ── follow-up (delta review N1-N4) ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("tail", [
    "?variant[]=111", "?a=1;variant=111", "?a=1&amp;variant=111", "#variant=111", "#a=1;variant=111",
    "?%76ariant=111", "?VARIANT=111", "?variant=0" + VARIANT, "?variant=", "?variant",
    "?variant=+" + VARIANT, "?variant=" + VARIANT + "&variant=111", "?variant=%2531%2531%2531",
    "?variant=" + VARIANT + "x", "?variant=" + VARIANT + "1",
    "?variant=" + VARIANT + "%3Bx", "?variant=" + VARIANT + "%23x", "?variant=" + VARIANT + "%26x=1",
    "?%2576ariant=111",              # D1: a key that needs a SECOND decoding round to read `variant`
    "?%26%23118%3Bariant=111",       # an HTML entity behind percent-encoding
])
def test_every_spelling_of_another_variant_in_the_url_is_refused(tail):
    """N1: every key that DECODES to `variant` (any case, either section, `;` or `&` separated) must have
    exactly `variant_id` as its decoded value; the value is split off before decoding, so an encoded
    separator stays inside it."""
    with pytest.raises(svc.PurchaseRefused) as refused:
        merchant_item.check_url_variant(URL + tail, VARIANT)
    assert refused.value.reason == "invalid_request"
    assert "does not match variant_id" in str(refused.value)


@pytest.mark.parametrize("tail", ["?variant%3D111", "?variant%3D" + VARIANT, "?VARIANT%3d111"])
def test_a_variant_key_hiding_its_value_behind_an_encoded_equals_is_refused_as_unreadable(tail):
    with pytest.raises(svc.PurchaseRefused) as refused:
        merchant_item.check_url_variant(URL + tail, VARIANT)
    assert refused.value.reason == "invalid_request" and "cannot read" in str(refused.value)


def test_an_encoding_deeper_than_the_lane_reads_is_refused_not_half_decoded():
    """P3-1: five levels of `%25` still change after the last round; refused, never matched half-decoded."""
    with pytest.raises(svc.PurchaseRefused) as refused:
        merchant_item.check_url_variant(URL + "?%2525252576ariant=111", VARIANT)
    assert refused.value.reason == "invalid_request" and "deeper" in str(refused.value)
    merchant_item.check_url_variant(URL + "?%25252576ariant=" + VARIANT, VARIANT)  # four levels: read


@pytest.mark.parametrize("tail", [
    "", "?variant=" + VARIANT, "?variant[]=" + VARIANT, "#variant=" + VARIANT, "?a=1;variant=" + VARIANT,
    "?Variant=" + VARIANT, "?variant=" + VARIANT + "&utm_source=x#reviews", "?utm_source=x",
    "?variant=" + VARIANT + ";utm=1",              # T2: `;` ends the value
    "?%2576ariant=" + VARIANT,                     # D1: two rounds, agreeing
    "?variant=%3" + VARIANT[0] + VARIANT[1:],      # the VALUE is decoded too: `%34…` is `4…`
    # P3-4: `variant` that is not a variant KEY, as a store reads it
    "?utm_content=variant_test", "#variant-picker", "?variant=" + VARIANT + "&utm_content=variant_a",
    "?sections=product-variant-picker", "?variant_title=red", "?x=variant%3D111", "?a=1&#118;ariant=111",
])
def test_the_agreeing_variant_and_other_parameters_are_accepted(tail):
    merchant_item.check_url_variant(URL + tail, VARIANT)


async def test_a_card_url_with_a_variant_word_in_another_parameter_is_bought(client, monkeypatch):
    """P3-4 at the route: an A/B `utm_content=variant_a` on the card's URL is not a variant; the purchase
    buys exactly `variant_id`."""
    await seed(monkeypatch)
    witness = (await client.post(BASE, json=url_body(URL + "?utm_content=variant_a#variant-picker"))).json()["selection"]
    created = await client.post(CREATE, json=url_create_body(witness, "utm-variant-word",
                                                             url=URL + "?utm_content=variant_a#variant-picker"))
    assert created.status_code == 202, created.text
    row = dict(await database.fetch_one("SELECT * FROM reap_agentic_purchases"))
    assert row["variant_key"] == "shopify:" + VARIANT


async def test_prepare_refuses_a_hidden_disagreeing_variant_before_any_sql(client, monkeypatch):
    monkeypatch.setenv("REAP_AGENTIC_CART_LINK_ENABLED", "1")
    with monkeypatch.context() as patches:
        forbid_sql(patches)
        response = await client.post(BASE, json=url_body(URL + "?a=1;variant=111"))
    assert response.status_code == 400 and error(response) == "invalid_request", response.text


def test_the_prefix_refusals_are_dark_off_the_cart_link_lane_or_with_its_dial_off(monkeypatch):
    """N2: a fingerprint-prefixed key is refused only where a URL or `variant_id` request can exist (the hash
    holds `item_source`); everywhere else such a key answers exactly as before these selectors."""
    from types import SimpleNamespace
    spelled_product, spelled_variant = "product_url:abc", "variant_id:" + VARIANT
    for item_source, dial in (("cart_link", "0"), ("reap_variant", "1"), ("reap_variant", "0")):
        monkeypatch.setenv("REAP_AGENTIC_CART_LINK_ENABLED", dial)
        request = SimpleNamespace(product_url=None, product_key=spelled_product, variant_id=None,
                                  variant_key=spelled_variant, item_source=item_source)
        assert reap._product_selector(request, DOMAIN) == (spelled_product, spelled_product, None)
        assert reap._variant_selector(request) == (spelled_variant, spelled_variant)


async def test_a_variant_lane_create_with_a_prefixed_key_answers_as_any_unknown_key(client, monkeypatch):
    """N2, at the route: with the cart-link dial off, a `reap_variant` create naming `product_url:…` gets the
    same answer as one naming any other key we do not have, not a new 400."""
    await seed(monkeypatch)
    monkeypatch.setenv("REAP_AGENTIC_CART_LINK_ENABLED", "0")
    witness = (await client.post(BASE, json=body())).json().get("selection") or {
        "merchant_domain": DOMAIN, "product_key": PRODUCT, "variant_id": VARIANT, "variant_key": SKU,
        "quantity": 1, "unit_price_minor": 1000, "currency": "USD"}
    answers = []
    for n, key in enumerate(("product_url:abc", "prod::not-in-the-catalog")):
        request = create_body(witness, f"prefixed-dark-{n}", by="variant_key")
        request.update(item_source="reap_variant", product_key=key)
        request.pop("variant_key")
        response = await client.post(CREATE, json=request)
        answers.append((response.status_code, error(response)))
    assert answers[0] == answers[1] and answers[0] != (400, "invalid_request"), answers


async def test_the_python_compare_reads_the_seeds_page_not_either_column(client, monkeypatch):
    """N3 (C4): a seed whose PAGE is `/en-gb/products/lip-ink` and whose destination is this page, attached to
    another product. SQL reads it (the LIKE is anchored on the right only); only the Python compare of the
    seed's page, not of either column, keeps it out."""
    await seed(monkeypatch)
    await add_product(PRODUCT + "-gb-page")
    await add_seed("gb-page-seed", attached=PRODUCT + "-gb-page")
    await database.execute("UPDATE external_product_seeds SET canonical_url=:c WHERE id='gb-page-seed'",
                           {"c": f"https://{DOMAIN}/en-gb/products/lip-ink"})
    response = await client.post(BASE, json=url_body())
    assert response.status_code == 200, response.text
    assert response.json()["selection"]["product_key"] == PRODUCT


async def test_the_python_compare_holds_the_host(client, monkeypatch):
    """N3 (C7): a seed of this store's `domain` whose stored page is on ANOTHER host with the same path. The
    domain filter reads it; only the host in the exact compare keeps it out."""
    await seed(monkeypatch)
    await add_product(PRODUCT + "-other-host")
    await add_seed("other-host-seed", attached=PRODUCT + "-other-host",
                   url="https://other-store.example/products/lip-ink")
    response = await client.post(BASE, json=url_body())
    assert response.status_code == 200, response.text
    assert response.json()["selection"]["product_key"] == PRODUCT


async def test_a_twin_whose_stored_url_has_spaces_is_still_a_twin(client, monkeypatch):
    """N4: Python trims a stored URL's spaces, and so must the SQL read, or a twin seed of this page attached
    to another product would be missed and the ambiguity answered with a pick."""
    await seed(monkeypatch)
    await add_product(PRODUCT + "-spaced-twin")
    await add_seed("spaced-twin", url="  " + URL + "  ", attached=PRODUCT + "-spaced-twin")
    response = await client.post(BASE, json=url_body())
    assert response.status_code == 409 and error(response) == "product_ambiguous", response.text


@pytest.mark.parametrize("stored", [URL + "\t", "\n" + URL, URL + "\u00e9", URL.replace("lip-", "lip\u200b-")])
async def test_a_stored_url_that_is_not_printable_ascii_is_not_a_page(monkeypatch, stored):
    """N4: the other side of the one rule. A tab, a newline or a non-ASCII character in the part a stored URL is
    COMPARED by (scheme, host, path) makes it no page (no caller URL can equal it), whatever the SQL read finds."""
    assert merchant_item._normalized(stored) is None
    await seed(monkeypatch)
    await add_product(PRODUCT + "-dirty-twin")
    await add_seed("dirty-twin", url=stored, attached=PRODUCT + "-dirty-twin")
    normalized = merchant_item.normalize_product_url(URL, DOMAIN)
    assert await merchant_item.resolve_product_key(normalized_url=normalized, market_country="US") == PRODUCT


async def test_a_punycode_storefront_seed_resolves(monkeypatch):
    """N4 does not hide an IDN store: its host is stored and sent as punycode (ASCII), so both sides agree."""
    await seed(monkeypatch)
    host = "xn--shp-a-xxa.example"
    page = "https://" + host + "/products/lip-ink"
    await add_product(PRODUCT + "-puny")
    await add_seed("puny-seed", url=page, domain=host, attached=PRODUCT + "-puny")
    normalized = merchant_item.normalize_product_url("https://WWW." + host.upper() + "/products/lip-ink/", host)
    assert await merchant_item.resolve_product_key(normalized_url=normalized, market_country="US") == PRODUCT + "-puny"


@pytest.mark.parametrize("sent", [f"https://{DOMAIN}/products/l%C3%A9vre", f"https://{DOMAIN}/products/l%c3%a9vre"])
async def test_a_percent_encoded_page_resolves(monkeypatch, sent):
    """N4 does not hide a non-ASCII page stored percent-encoded (ASCII): the same encoding resolves it, hex case
    folded. (The same page stored RAW is no page: `test_a_stored_url_that_is_not_printable_ascii_is_not_a_page`.)"""
    await seed(monkeypatch)
    await add_product(PRODUCT + "-encoded")
    await add_seed("encoded-seed", url=f"https://{DOMAIN}/products/l%C3%A9vre", attached=PRODUCT + "-encoded")
    normalized = merchant_item.normalize_product_url(sent, DOMAIN)
    assert await merchant_item.resolve_product_key(normalized_url=normalized, market_country="US") == PRODUCT + "-encoded"


@pytest.mark.parametrize("dirty_query", ["?ref=caf\u00e9", "?a=1\tb", "#caf\u00e9"])
async def test_a_dirty_query_on_a_stored_url_is_dropped_unread(monkeypatch, dirty_query):
    """P3-3: the query and fragment are dropped by the compare, so a raw non-ASCII or tab there does not make a
    stored URL less of a page: the product's only seed still resolves, and such a twin is still a twin."""
    await seed(monkeypatch)
    await database.execute("UPDATE external_product_seeds SET destination_url=:u WHERE id=:id",
                           {"u": URL + dirty_query, "id": SEED})
    normalized = merchant_item.normalize_product_url(URL, DOMAIN)
    assert await merchant_item.resolve_product_key(normalized_url=normalized, market_country="US") == PRODUCT
    await add_product(PRODUCT + "-dirty-query-twin")
    await add_seed("dirty-query-twin", url=URL + dirty_query, attached=PRODUCT + "-dirty-query-twin")
    with pytest.raises(svc.PurchaseRefused) as refused:
        await merchant_item.resolve_product_key(normalized_url=normalized, market_country="US")
    assert refused.value.reason == "product_ambiguous"


def test_the_callers_whole_url_stays_printable_ascii():
    """The caller's URL is checked whole (its query included) before anything else; only the STORED side
    reads past a dirty query."""
    with pytest.raises(svc.PurchaseRefused) as refused:
        merchant_item.normalize_product_url(URL + "?ref=caf\u00e9", DOMAIN)
    assert refused.value.reason == "invalid_request"
