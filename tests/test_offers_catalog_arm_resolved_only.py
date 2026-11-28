"""The catalog arm serves RESOLVED siblings only (ADR-032 M11 item 1), behind
OFFERS_CATALOG_ARM_RESOLVED_ONLY (default off).

THE DEFECT. The arm's statement widens a listing id to EVERY listing sharing its content_key, and a
bare content_key reaches every listing on it. A content_key is sha(brand::title::gtin): two listings
on one key are a brand+title collision, not a judgement that they sell the same item, so the arm can
serve another seller's 30 ml beside the 50 ml the caller named, with no identity check.

THE FIX. With the flag on, the arm serves the listings the call NAMED plus only the members of their
content_key that the sibling lane's rule resolves (`_resolved_group_members`). A bare content_key
names no listing: its resolved members only. An unread resolution fails closed to the named listings.
`shadow` answers as off and counts what `on` would withhold. Off sends the statement main sends.

Driven through the REAL route with the reads faked by SQL shape (the group-sibling suite's harness);
the SQL itself runs on Postgres in tests/test_offers_catalog_arm_resolved_only_postgres.py.
"""

from __future__ import annotations

from typing import List

import pytest

import routes.agent_shop_gateway as gateway
from tests.test_offers_resolve_group_siblings import (
    BRAND_PK,
    BRAND_SIG,
    CK,
    MEMBERS,
    RETAILER_PK,
    RETAILER_SIG,
    _attach,
    _brand_seed,
    _catalog_row,
    _hosts,
    _invoke,
    _lone_seed,
    _member,
    client,  # noqa: F401  (the fixture)
)

from tests.test_exact_retailer_shades import FIRST, _wire_sources, fixture_rows  # noqa: E402
from tests.test_exact_retailer_shades import _invoke as exact_invoke  # noqa: E402
from tests.test_exact_retailer_shades import http  # noqa: E402,F401  (the fixture)

ARM_FLAG = "OFFERS_CATALOG_ARM_RESOLVED_ONLY"

# A third listing on the SAME content_key that no matcher verdict joined: the collision the widening
# serves today (say, the 30 ml of the same title from another retailer).
OTHER_PK, OTHER_SIG = "ext:retailer:serum-30ml-other", "sig_grp_other"
ARM_MEMBERS = [*MEMBERS, _member(OTHER_PK, OTHER_SIG, CK, "https://www.other-retailer.example/p/serum", OTHER_PK)]
ARM_ROWS = [
    _catalog_row("of_sk", RETAILER_PK, "stylekorean.com", 15.0),
    _catalog_row("of_other", OTHER_PK, "other-retailer.example", 9.0),
]
RESOLVED = [_attach(RETAILER_PK)]  # the retailer listing joined to the brand's: brand + retailer resolved


def _arm(monkeypatch, client, *, product_id, arm=None, proposals=None, calls=None, **kw) -> dict:  # noqa: F811
    if arm is None:
        monkeypatch.delenv(ARM_FLAG, raising=False)
    else:
        monkeypatch.setenv(ARM_FLAG, arm)
    return _invoke(monkeypatch, client, product_id=product_id, members=ARM_MEMBERS,
                   seeds=[_brand_seed(), _lone_seed()], catalog_rows=ARM_ROWS,
                   proposals=RESOLVED if proposals is None else proposals, calls=calls, **kw)


def _catalog_source(body) -> dict:
    [src] = [s for s in body["metadata"]["sources"] if s["source"] == "catalog_offers"]
    return src


def _catalog_ids(body) -> List[str]:
    return sorted(o["offer_id"] for o in body.get("offers") or []
                  if (o.get("source") or {}).get("type") == "catalog_offer")


def _arm_statements(calls) -> List[str]:
    return [q for q in calls if "JOIN catalog_offers" in q]


# ------------------------------------------------------------------------------- off: as on main


def test_flag_off_the_arm_still_widens_to_every_listing_on_the_key(monkeypatch, client):  # noqa: F811
    """Today's behaviour, pinned so the flag's effect is measured against it."""
    calls: list = []
    body = _arm(monkeypatch, client, product_id=BRAND_SIG, calls=calls)
    assert _catalog_ids(body) == ["of:catalog_offer:of_other", "of:catalog_offer:of_sk"]
    [q] = _arm_statements(calls)
    assert "SIBLING LISTINGS" in q and ":resolved_keys" not in q
    assert "resolved_only" not in _catalog_source(body)


@pytest.mark.parametrize("raw", ["", "0", "off", "false", "maybe", "ON please"])
def test_an_unrecognised_value_is_off(monkeypatch, client, raw):  # noqa: F811
    calls: list = []
    body = _arm(monkeypatch, client, product_id=BRAND_SIG, arm=raw, calls=calls)
    assert _catalog_ids(body) == ["of:catalog_offer:of_other", "of:catalog_offer:of_sk"]
    assert not any("FROM identity_resolution_proposals" in q for q in calls), "off reads no resolution"


def test_the_off_statement_is_the_widened_text_and_on_swaps_only_the_listing_subquery():
    off = gateway._catalog_offers_statement("", "")
    on = gateway._catalog_offers_statement("", "", resolved_only=True)
    assert off == gateway._catalog_offers_statement("", "", resolved_only=False)
    assert gateway._CATALOG_ARM_LISTINGS_WIDENED in off and gateway._CATALOG_ARM_LISTINGS_RESOLVED_ONLY not in off
    assert on == off.replace(gateway._CATALOG_ARM_LISTINGS_WIDENED, gateway._CATALOG_ARM_LISTINGS_RESOLVED_ONLY)
    assert "content_key = ANY(:aliases)" not in on and "s.content_key = a.content_key" not in on


@pytest.mark.parametrize("raw,mode", [("1", "on"), ("true", "on"), ("YES", "on"), (" on ", "on"),
                                      ("shadow", "shadow"), ("Shadow", "shadow"), ("2", "off")])
def test_the_mode_reader(monkeypatch, raw, mode):
    monkeypatch.setenv(ARM_FLAG, raw)
    assert gateway._catalog_arm_resolved_only_mode() == mode


# ------------------------------------------------------------------------------- on


@pytest.mark.parametrize("ident", [BRAND_SIG, BRAND_PK, RETAILER_SIG, RETAILER_PK])
def test_on_a_resolved_listing_gets_its_resolved_siblings_and_never_the_collision(monkeypatch, client, ident):  # noqa: F811
    calls: list = []
    body = _arm(monkeypatch, client, product_id=ident, arm="on", calls=calls)
    assert _catalog_ids(body) == ["of:catalog_offer:of_sk"], ident
    assert "other-retailer.example" not in _hosts(body)
    [q] = _arm_statements(calls)
    assert ":resolved_keys" in q and "SIBLING LISTINGS" not in q
    src = _catalog_source(body)
    assert src["resolved_only"] == "on" and src["resolved"] == 2
    assert "would_withhold" not in src and "would_withhold_listings" not in src, "a count is shadow's alone"


def test_on_the_resolved_keys_bind_is_the_resolved_listings(monkeypatch, client):  # noqa: F811
    seen: list = []
    real = gateway._read_resolved_members

    async def spy(db, **kw):
        out = await real(db, **kw)
        seen.append(sorted(str(m["product_key"]) for m in out["resolved"]))
        return out

    monkeypatch.setattr(gateway, "_read_resolved_members", spy)
    _arm(monkeypatch, client, product_id=BRAND_SIG, arm="on")
    assert seen == [sorted([BRAND_PK, RETAILER_PK])]


def test_on_an_unresolved_key_serves_only_the_named_listing(monkeypatch, client):  # noqa: F811
    body = _arm(monkeypatch, client, product_id=RETAILER_SIG, arm="on", proposals=[])
    assert _catalog_ids(body) == ["of:catalog_offer:of_sk"]
    assert _catalog_source(body)["resolved"] == 0
    # The brand's listing carries no catalog offer of its own, so its call gets none from the arm.
    assert _catalog_ids(_arm(monkeypatch, client, product_id=BRAND_SIG, arm="on", proposals=[])) == []


def test_on_an_unresolved_named_listing_does_not_inherit_the_resolved_group(monkeypatch, client):  # noqa: F811
    """The collision's own id answers with its own offer, never the resolved members' (the anchor
    must itself be resolved, as in the sibling lane)."""
    body = _arm(monkeypatch, client, product_id=OTHER_SIG, arm="on")
    assert _catalog_ids(body) == ["of:catalog_offer:of_other"]
    assert _catalog_source(body)["resolved"] == 0


def test_on_a_bare_content_key_reaches_its_resolved_members_only(monkeypatch, client):  # noqa: F811
    assert _catalog_ids(_arm(monkeypatch, client, product_id=CK, arm="on")) == ["of:catalog_offer:of_sk"]
    # No resolved member: a bare key names no listing, so the arm serves nothing.
    body = _arm(monkeypatch, client, product_id=CK, arm="on", proposals=[])
    assert _catalog_ids(body) == []
    assert _catalog_source(body)["resolved"] == 0


def test_on_an_unread_resolution_fails_closed_to_the_named_listing(monkeypatch, client):  # noqa: F811
    async def broken(db, **kw):
        raise RuntimeError("listings read failed")

    monkeypatch.setattr(gateway, "_read_resolved_members", broken)
    body = _arm(monkeypatch, client, product_id=RETAILER_SIG, arm="on")
    assert _catalog_ids(body) == ["of:catalog_offer:of_sk"]
    src = _catalog_source(body)
    assert src["resolution"] == "unread" and src["status"] == "ok"
    assert _catalog_ids(_arm(monkeypatch, client, product_id=CK, arm="on")) == []


def test_on_with_the_group_lane_on_reads_the_resolution_once(monkeypatch, client):  # noqa: F811
    calls: list = []
    body = _arm(monkeypatch, client, product_id=RETAILER_SIG, arm="on", flag="1", calls=calls)
    assert sum("FROM identity_resolution_proposals" in q for q in calls) == 1
    assert sum("SELECT s.product_key, s.source_ref, s.source_product_id" in q for q in calls) == 1
    assert "of:catalog_offer:of_other" not in _catalog_ids(body)
    assert _catalog_source(body)["resolved"] == 2


# ------------------------------------------------------------------------------- shadow


@pytest.mark.parametrize("ident", [BRAND_SIG, RETAILER_SIG, OTHER_SIG, CK])
def test_shadow_answers_as_off_and_counts_what_on_withholds(monkeypatch, client, ident):  # noqa: F811
    off = _arm(monkeypatch, client, product_id=ident, deterministic=True)
    shadow = _arm(monkeypatch, client, product_id=ident, arm="shadow", deterministic=True)
    on = _arm(monkeypatch, client, product_id=ident, arm="on", deterministic=True)
    assert shadow["offers"] == off["offers"], ident
    src = _catalog_source(shadow)
    assert src["resolved_only"] == "shadow"
    withheld = len(_catalog_ids(off)) - len(_catalog_ids(on))
    assert src["would_withhold"] == withheld, (ident, src)
    assert src["would_withhold_listings"] == withheld  # one offer per listing in this fixture


def test_shadow_with_an_unread_resolution_counts_nothing(monkeypatch, client):  # noqa: F811
    async def broken(db, **kw):
        raise RuntimeError("listings read failed")

    monkeypatch.setattr(gateway, "_read_resolved_members", broken)
    body = _arm(monkeypatch, client, product_id=BRAND_SIG, arm="shadow")
    assert _catalog_ids(body) == ["of:catalog_offer:of_other", "of:catalog_offer:of_sk"]
    src = _catalog_source(body)
    # Unread is said, and nothing is counted: a count would have to guess which rows were named.
    assert src["resolution"] == "unread"
    assert "would_withhold" not in src and "would_withhold_listings" not in src


def test_shadow_counts_offers_and_listings_apart(monkeypatch, client):  # noqa: F811
    monkeypatch.setenv(ARM_FLAG, "shadow")
    rows = [*ARM_ROWS, _catalog_row("of_other_2", OTHER_PK, "other-retailer-2.example", 10.0)]
    body = _invoke(monkeypatch, client, product_id=BRAND_SIG, members=ARM_MEMBERS,
                   seeds=[_brand_seed(), _lone_seed()], catalog_rows=rows, proposals=RESOLVED)
    src = _catalog_source(body)
    assert src["would_withhold"] == 2 and src["would_withhold_listings"] == 1, src


def test_on_the_group_shadow_still_sees_the_key_the_arm_read_no_row_on(monkeypatch, client):  # noqa: F811
    """The group shadow is gated on the content keys this request's reads named. With the arm on, a
    call whose key resolves nothing reads no catalog row on it; the resolution read still names it."""
    seen: list = []

    def spy(background_tasks, **kw):
        seen.append(set(kw["content_keys"]))
        return False

    monkeypatch.setattr(gateway, "_schedule_group_shadow", spy)
    body = _arm(monkeypatch, client, product_id=BRAND_SIG, arm="on", flag="shadow", proposals=[])
    assert _catalog_ids(body) == []
    assert seen and CK in seen[-1], seen


def test_on_an_exact_shade_call_reads_no_resolution(monkeypatch, http):  # noqa: F811
    """The exact-shade cohort is served by its own reader, never the arm's statement: no resolution read."""
    reads: list = []

    async def spy(db, **kw):
        reads.append(kw)
        return {"members": [], "resolved": [], "matched_offers": set(), "matched_seeds": {}}

    monkeypatch.setenv(ARM_FLAG, "on")
    monkeypatch.setattr(gateway, "_read_resolved_members", spy)
    exact_client, _warm = http
    calls = _wire_sources(monkeypatch, FIRST, fixture_rows(FIRST))
    offers = exact_invoke(exact_client, sku_id=f"gid://shopify/ProductVariant/{FIRST}")
    assert calls == ["exact"] and any(o["source"]["type"] == "catalog_offer" for o in offers)
    assert reads == []
