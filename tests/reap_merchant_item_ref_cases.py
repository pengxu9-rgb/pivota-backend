"""A purchase that names the product by the MERCHANT's product URL instead of our `product_key`.

Shared by the real SQLite and PostgreSQL collectors (test_reap_merchant_item_ref*.py). The catalog, seed,
proof and eligibility reads are real; only auth is fixture-controlled and the route suites' network ban
stays in force. The fixture is the selection-prepare suite's (one seed, one mirror product, one variant).
"""
from __future__ import annotations

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

URL = "https://judydoll.com/products/lip-ink"
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
        "'Judydoll',:domain,:source,:ref,'self',:reason)",
        {"pk": product_key, "domain": DOMAIN, "source": source_system, "ref": source_ref,
         "reason": "test_suppressed" if suppressed else None},
    )


async def add_seed(seed_id, *, url=URL, attached=None, market="US", domain=DOMAIN):
    await database.execute(
        "INSERT INTO external_product_seeds (id,status,domain,market,destination_url,attached_product_key,seed_data) "
        "VALUES (:id,'active',:domain,:market,:url,:pk,'{}')",
        {"id": seed_id, "domain": domain, "market": market, "url": url, "pk": attached},
    )


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
    "https://www.judydoll.com/products/lip-ink",
    "https://judydoll.com/products/lip-ink/",
    "HTTPS://JudyDoll.com/products/Lip-Ink?variant=49819267301653#reviews",
    "http://judydoll.com/products/lip-ink",
    "https://judydoll.com:443/products/lip-ink",
])
async def test_prepare_resolves_every_spelling_of_the_same_page(client, monkeypatch, spelling):
    await seed(monkeypatch)
    response = await client.post(BASE, json=url_body(spelling))
    assert response.status_code == 200, response.text
    assert response.json()["selection"]["product_key"] == PRODUCT


@pytest.mark.parametrize("bad", [
    "https://other-store.example/products/lip-ink",       # another store
    "https://uk.judydoll.com/products/lip-ink",           # a subdomain is another store
    "https://judydoll.com.evil.example/products/lip-ink",  # a lookalike
    "https://127.0.0.1/products/lip-ink",                  # an IP literal
    "https://judydoll.com/",                               # no product page
    "https://judydoll.com",
    "ftp://judydoll.com/products/lip-ink",
    "judydoll.com/products/lip-ink",
    "https://buyer:pw" + chr(64) + "judydoll.com/products/lip-ink",  # userinfo
    "https://judydoll.com:8443/products/lip-ink",          # a non-default port
    "https://judydoll.com/products/lip ink",               # whitespace inside
    "https://judydoll.com/products/lip-ink\u0000",         # NUL
    "https://judydoll.com/" + "a" * 2100,                  # longer than the cap
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
    response = await client.post(BASE, json=url_body("https://judydoll.com/products/another-thing"))
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
    response = await client.post(BASE, json=url_body("https://judydoll.com" + wildcard_path))
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
        "UPDATE external_product_seeds SET canonical_url='https://judydoll.com/products/lip-ink-canonical', "
        "destination_url='https://judydoll.com/collections/x?utm=1' WHERE id=:id", {"id": SEED})
    normalized = merchant_item.normalize_product_url("https://judydoll.com/products/lip-ink-canonical", DOMAIN)
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
    await add_seed("mini-seed", url="https://judydoll.com/products/lip-ink-mini", attached=PRODUCT + "-mini")
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
    await add_seed("wild-a", url="https://judydoll.com/products/aaa-1", attached=PRODUCT)
    await add_seed("wild-b", url="https://judydoll.com/products/aaa-2", attached=PRODUCT)
    monkeypatch.setattr(merchant_item, "MAX_SEED_CANDIDATES", 1)
    normalized = merchant_item.normalize_product_url("https://judydoll.com/products/aaa%", DOMAIN)
    with pytest.raises(svc.PurchaseRefused) as refused:
        await merchant_item.resolve_product_key(normalized_url=normalized, market_country="US")
    assert refused.value.reason == "row_not_found"


def test_a_url_fingerprint_differs_from_the_same_string_sent_as_a_product_key():
    """The prefix keeps a URL request apart from a `product_key` equal to its normalised URL. (A key spelled
    `product_url:<url>` would hash alike, exactly as one spelled `variant_id:<n>` does for variants; replay is
    scoped to one owner and one idempotency key, so that is the caller replaying itself.)"""
    from types import SimpleNamespace
    normalized = merchant_item.normalize_product_url(URL, DOMAIN)
    by_url = reap._product_selector(SimpleNamespace(product_url=URL, product_key=None), DOMAIN)
    by_key = reap._product_selector(SimpleNamespace(product_url=None, product_key=normalized), DOMAIN)
    assert by_url[1] != by_key[1]
    assert (by_url[0], by_url[2]) == (None, normalized) and by_key[0] == normalized



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
    monkeypatch.setenv("REAP_AGENTIC_PILOT_SCOPE", (
        '{"agent_ids":["agent_not_this_one"],"merchant_domains":["judydoll.com"],"markets":["US"],'
        '"product_keys":["%s"],"quantities":[1],"variant_keys":["shopify:%s"],"currency":"USD",'
        '"max_total_minor":100000}' % (PRODUCT, VARIANT)))
    forbid_resolution(monkeypatch)
    for path, request in ((BASE, url_body()), (CREATE, url_create_body(witness, "pilot-agent"))):
        response = await client.post(path, json=request)
        # A pilot refusal answers as the dark rail does (the route never tells a prober who is enrolled).
        assert response.status_code == 404 and error(response) == "not_available_on_this_rail", response.text


async def test_a_pilot_that_lists_another_product_refuses_the_resolved_one(client, monkeypatch, prepare_auth_agent):
    await seed(monkeypatch)
    witness = (await client.post(BASE, json=url_body())).json()["selection"]
    monkeypatch.setenv("REAP_AGENTIC_PILOT_SCOPE", (
        '{"agent_ids":["%s"],"merchant_domains":["judydoll.com"],"markets":["US"],'
        '"product_keys":["prod::some-other-product"],"quantities":[1],"variant_keys":["shopify:%s"],'
        '"currency":"USD","max_total_minor":100000}' % (prepare_auth_agent, VARIANT)))
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
        svc.enforce_pilot_scope(agent_id="a", merchant_domain="judydoll.com", market_country="US",
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
    await add_product(PRODUCT + "-moved")
    await database.execute(
        "UPDATE external_product_seeds SET attached_product_key=:pk WHERE id=:id", {"pk": PRODUCT + "-moved", "id": SEED})
    resumed = await client.post(CREATE + f"/{pid}/resume", json=original)
    assert resumed.status_code == 409, resumed.text
    assert (await ledger.get_purchase_internal(pid))["contact_revision"] in (0, None)
