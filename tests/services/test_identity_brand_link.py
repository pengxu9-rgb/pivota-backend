"""attach_membership: an existing retailer listing moves onto the brand's own product, reversibly.

Rows are shaped like prod's (2026-09-26): koolseoul.com's "Missha Artemisia Calming Ampoule" listing
minted its own product beside misshaus.com's "Artemisia Calming Ampoule".
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from services import identity_brand_link as link
from services.catalog_identity import make_content_key
from services.identity_resolution import apply_approved, new_proposal, revert_run

SYN = "external_seed"
BRAND_CK = make_content_key("Missha", "Artemisia Calming Ampoule")
LISTING_CK = make_content_key("Missha", "Missha Artemisia Calming Ampoule")
BRAND = {"product_key": "ext:missha-artemisia-calming-ampoule::57eacff8", "merchant_id": SYN, "platform": SYN,
         "source_product_id": "missha-artemisia-calming-ampoule::57eacff8", "content_key": BRAND_CK,
         "gtin": None, "created_at": "2026-09-20", "brand": "Missha", "title": "Artemisia Calming Ampoule"}
LISTING = {"product_key": "ext:retailer:" + "a" * 32, "merchant_id": SYN, "platform": SYN,
           "source_product_id": "retailer:" + "a" * 32, "content_key": LISTING_CK, "gtin": None,
           "brand": "Missha", "title": "Missha Artemisia Calming Ampoule", "created_at": "2026-09-24"}
GROUPS = {link.member_key(BRAND): "pg_brand", link.member_key(LISTING): "pg_listing"}


def _build(listings=None, families=None, groups=None):
    listings = listings if listings is not None else [LISTING]
    groups = groups if groups is not None else GROUPS
    on_key: Dict[str, List[str]] = {}
    for r in listings:
        on_key.setdefault(r.get("content_key"), []).append(r["product_key"])
    sizes: Dict[str, int] = {}
    for r in listings:
        g = groups.get(link.member_key(r))
        if g:
            sizes[g] = sizes.get(g, 0) + 1
    return link.build_proposals(listings, families if families is not None else {BRAND_CK: [BRAND]},
                                groups, on_key, sizes)


def test_a_brand_prefixed_listing_is_proposed_onto_the_brand_product():
    proposals, counts = _build()
    assert counts == {"proposed": 1}
    [p] = proposals
    assert (p["kind"], p["strategy"], p["keeper_product_key"], p["content_key"]) == (
        "attach_membership", link.STRATEGY, BRAND["product_key"], BRAND_CK)
    ev = p["evidence"]
    assert (ev["listing_product_key"], ev["from_content_key"], ev["from_product_group_id"],
            ev["to_product_group_id"]) == (LISTING["product_key"], LISTING_CK, "pg_listing", "pg_brand")


@pytest.mark.parametrize("change,reason", [
    ({"title": "Artemisia Calming Ampoule by Missha"}, "no_brand_prefix"),
    ({"title": "Missha Artemisia Calming Ampoule 75ml"}, "no_brand_product"),   # sizes are never stripped
    ({"content_key": BRAND_CK}, "already_on_family"),
    ({"gtin": "08809643069999"}, "gtin_conflict"),
    ({"product_key": "ext:missha-x::1"}, "not_a_listing"),
])
def test_listings_that_are_not_moved(change, reason):
    family = [{**BRAND, "gtin": "08809643062982"}] if reason == "gtin_conflict" else [BRAND]
    proposals, counts = _build(listings=[{**LISTING, **change}], families={BRAND_CK: family})
    assert proposals == [] and counts == {reason: 1}


def test_a_family_of_only_listings_is_not_a_brand_product():
    other = {**LISTING, "product_key": "ext:retailer:" + "b" * 32, "content_key": BRAND_CK}
    assert _build(families={BRAND_CK: [other]}) == ([], {"no_brand_product": 1})


def test_rows_without_group_membership_or_already_grouped_are_not_moved():
    assert _build(groups={link.member_key(BRAND): "pg_brand"})[1] == {"no_group_membership": 1}
    assert _build(groups={link.member_key(BRAND): "pg_x", link.member_key(LISTING): "pg_x"})[1] == \
        {"already_in_group": 1}


def test_the_oldest_brand_row_is_the_one_joined():
    newer = {**BRAND, "product_key": "ext:missha-artemisia-calming-ampoule::0000", "created_at": "2026-09-25",
             "source_product_id": "newer"}
    groups = {**GROUPS, link.member_key(newer): "pg_newer"}
    [p], _ = _build(families={BRAND_CK: [newer, BRAND]}, groups=groups)
    assert p["keeper_product_key"] == BRAND["product_key"]


def test_a_proposal_must_carry_what_apply_and_revert_need():
    with pytest.raises(ValueError):
        new_proposal(kind="attach_membership", strategy=link.STRATEGY, subject_product_keys=["a", "b"],
                     keeper_product_key="b", evidence={"listing_product_key": "a"})
    with pytest.raises(ValueError):
        new_proposal(kind="attach_membership", strategy=link.STRATEGY, subject_product_keys=["a", "b", "c"],
                     keeper_product_key="b", evidence={})


# --- the engine: apply, drift, revert ---------------------------------------------------------------


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class StateConn:
    """In-memory catalog_products + product_group_members, enough for attach_membership's SQL."""

    def __init__(self, rows: List[Dict[str, Any]], groups: Dict[tuple, str], approved: List[Dict[str, Any]]):
        self.rows = {r["product_key"]: {**r, "suppression_reason": r.get("suppression_reason")} for r in rows}
        self.groups = dict(groups)
        self.approved = approved
        self.events: List[Dict[str, Any]] = []
        self.status: Dict[str, str] = {p["proposal_id"]: "approved" for p in approved}

    def transaction(self):
        return _Tx()

    async def fetch(self, sql, *args):
        s = " ".join(sql.split())
        if "WHERE status = 'approved'" in s:
            return [p for p in self.approved if self.status[p["proposal_id"]] == "approved"]
        if "FROM identity_resolution_events" in s:
            return [{"proposal_id": e["proposal_id"], "detail": json.dumps(e["detail"])}
                    for e in self.events if e["run_id"] == args[0] and e["action"] == "applied"]
        if "SET status = 'reverted'" in s:
            return [{"proposal_id": k} for k, v in self.status.items() if v == "applied"]
        if "suppression_metadata->>'run_id'" in s:
            return []
        raise AssertionError(s[:80])

    async def fetchrow(self, sql, *args):
        return self.rows.get(args[0])

    async def fetchval(self, sql, *args):
        if "SELECT count(*) FROM catalog_products WHERE content_key = $1" in " ".join(sql.split()):
            return sum(1 for r in self.rows.values()
                       if r["content_key"] == args[0] and r["suppression_reason"] is None)
        return self.groups.get((args[0], args[1], args[2]))

    async def execute(self, sql, *args):
        s = " ".join(sql.split())
        if s.startswith("UPDATE catalog_products SET content_key"):
            row = self.rows.get(args[0])
            if row and row["content_key"] == args[2] and row["suppression_reason"] is None:
                row["content_key"] = args[1]
                return "UPDATE 1"
            return "UPDATE 0"
        if s.startswith("UPDATE product_group_members"):
            key = (args[0], args[1], args[2])
            if self.groups.get(key) == args[4]:
                self.groups[key] = args[3]
                return "UPDATE 1"
            return "UPDATE 0"
        if "INSERT INTO identity_resolution_events" in s:
            self.events.append({"proposal_id": args[0], "action": args[1], "run_id": args[2],
                                "detail": json.loads(args[3])})
            return "INSERT 0 1"
        if "SET status = 'applied'" in s:
            self.status[args[0]] = "applied"
            return "UPDATE 1"
        raise AssertionError(s[:80])


def _approved_proposal():
    [p], _ = _build()
    return {**p, "status": "approved", "evidence": json.dumps(p["evidence"])}


def _conn(**over):
    rows = over.pop("rows", [LISTING, BRAND])
    groups = over.pop("groups", {("external_seed", "external_seed", LISTING["source_product_id"]): "pg_listing",
                                 ("external_seed", "external_seed", BRAND["source_product_id"]): "pg_brand"})
    return StateConn(rows, groups, [_approved_proposal()])


@pytest.mark.asyncio
async def test_apply_moves_the_listing_and_revert_moves_it_back():
    conn = _conn()
    out = await apply_approved(conn, run_id="R1", strategies=[link.STRATEGY])
    assert out["skipped"] == [] and len(out["applied"]) == 1
    assert conn.rows[LISTING["product_key"]]["content_key"] == BRAND_CK
    assert conn.groups[("external_seed", "external_seed", LISTING["source_product_id"])] == "pg_brand"
    assert conn.rows[BRAND["product_key"]]["content_key"] == BRAND_CK  # the brand row is untouched

    back = await revert_run(conn, "R1")
    assert conn.rows[LISTING["product_key"]]["content_key"] == LISTING_CK
    assert conn.groups[("external_seed", "external_seed", LISTING["source_product_id"])] == "pg_listing"
    [d] = back["detached"]
    assert d["reverted"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("mutate,reason", [
    (lambda c: c.rows[LISTING["product_key"]].update(content_key="ck_other"), "listing_content_key_drift"),
    (lambda c: c.rows[LISTING["product_key"]].update(suppression_reason="x"), "listing_not_live"),
    (lambda c: c.rows[BRAND["product_key"]].update(content_key="ck_other"), "keeper_content_key_drift"),
    (lambda c: c.rows[BRAND["product_key"]].update(suppression_reason="x"), "keeper_not_live"),
    (lambda c: c.groups.update({("external_seed", "external_seed", LISTING["source_product_id"]): "pg_x"}),
     "listing_group_drift"),
    (lambda c: c.groups.update({("external_seed", "external_seed", BRAND["source_product_id"]): "pg_x"}),
     "keeper_group_drift"),
])
async def test_anything_that_moved_since_propose_time_is_skipped_not_forced(mutate, reason):
    conn = _conn()
    mutate(conn)
    before = ({k: dict(v) for k, v in conn.rows.items()}, dict(conn.groups))
    out = await apply_approved(conn, run_id="R2", strategies=[link.STRATEGY])
    assert out["applied"] == [] and [r for _, r in out["skipped"]] == [reason]
    assert (conn.rows, conn.groups) == before


@pytest.mark.asyncio
async def test_revert_leaves_a_listing_that_moved_again_alone():
    conn = _conn()
    await apply_approved(conn, run_id="R3", strategies=[link.STRATEGY])
    conn.rows[LISTING["product_key"]]["content_key"] = "ck_later"  # something re-keyed it after the run
    back = await revert_run(conn, "R3")
    assert conn.rows[LISTING["product_key"]]["content_key"] == "ck_later"
    # all or nothing: the group is NOT moved back alone either
    assert conn.groups[("external_seed", "external_seed", LISTING["source_product_id"])] == "pg_brand"
    assert back["detached"][0]["reverted"] is False


@pytest.mark.asyncio
async def test_a_strategy_filter_keeps_other_approved_proposals_out():
    conn = _conn()
    out = await apply_approved(conn, run_id="R4", strategies=["same_url_dup"])
    assert out["applied"] == [] and conn.rows[LISTING["product_key"]]["content_key"] == LISTING_CK


@pytest.mark.asyncio
async def test_the_refresh_rebuilds_both_sides_and_counts_failures(monkeypatch):
    import services.agent_pdp_view_assembler as apv
    import services.index_pipeline_state_service as ips

    seen: Dict[str, List[str]] = {"refresh": [], "reap": [], "recompute": []}

    async def refresh(ck, **kw):
        seen["refresh"].append(ck)
        if ck == "ck_boom":
            raise RuntimeError("x")
        return ck != LISTING_CK

    async def reap(ck, **kw):
        seen["reap"].append(ck)
        return ck == LISTING_CK

    async def recompute(ck, **kw):
        seen["recompute"].append(ck)
        return True

    monkeypatch.setattr(apv, "refresh_agent_pdp_view_for_content_key", refresh)
    monkeypatch.setattr(apv, "delete_agent_pdp_view_if_orphaned", reap)
    monkeypatch.setattr(ips, "recompute_serving_eligibility", recompute)
    out = await link.refresh_after_move(
        [{"to_content_key": BRAND_CK, "from_content_key": LISTING_CK},
         {"to_content_key": "ck_boom", "from_content_key": LISTING_CK}], source="t")
    # the side that LOST the listing first (its view may hold the listing's unique signature)
    assert seen["refresh"] == [LISTING_CK, BRAND_CK, LISTING_CK, "ck_boom"]
    assert out == {"refreshed": 1, "reaped": 2, "recomputed": 3, "errors": 1}
    seen["refresh"].clear()
    await link.refresh_after_move([{"to_content_key": BRAND_CK, "from_content_key": LISTING_CK, "reverted": True},
                                   {"to_content_key": "ck_x", "from_content_key": "ck_y", "reverted": False}],
                                  source="t")
    assert seen["refresh"] == [BRAND_CK, LISTING_CK]  # a revert: the brand side lost it; a skipped one: nothing


# --- review of the attach_membership PR ---------------------------------------------------------------


def test_nothing_may_stay_behind_on_the_old_content_key():
    """A row left on the listing's old content_key would pull it back on the next crawl (exact tier) and
    fail the store job on the group; so a listing moves only with every row on that key."""
    sibling = {**LISTING, "product_key": "ext:retailer:" + "c" * 32, "source_product_id": "retailer:" + "c" * 32}
    groups = {**GROUPS, link.member_key(sibling): "pg_listing"}
    both = {LISTING_CK: [LISTING["product_key"], sibling["product_key"]]}
    proposals, counts = link.build_proposals([LISTING, sibling], {BRAND_CK: [BRAND]}, groups, both,
                                             {"pg_listing": 2})
    assert len(proposals) == 2 and counts == {"proposed": 2}          # they move together
    stuck = {**sibling, "gtin": "08809643069999"}
    proposals, counts = link.build_proposals([LISTING, stuck], {BRAND_CK: [{**BRAND, "gtin": "08809643062982"}]},
                                             groups, both, {"pg_listing": 2})
    assert proposals == [] and counts["rows_left_on_old_content_key"] == 1   # one can't move -> neither does
    store_row = {LISTING_CK: [LISTING["product_key"], "ext:cocomo-missha-artemisia::1"]}  # not a listing at all
    assert link.build_proposals([LISTING], {BRAND_CK: [BRAND]}, GROUPS, store_row, {})[1] == \
        {"rows_left_on_old_content_key": 1}


def test_a_group_with_other_members_is_not_emptied_of_one():
    proposals, counts = link.build_proposals([LISTING], {BRAND_CK: [BRAND]}, GROUPS,
                                             {LISTING_CK: [LISTING["product_key"]]}, {"pg_listing": 3})
    assert proposals == [] and counts == {"old_group_has_other_members": 1}


def test_a_drifted_move_mints_a_new_proposal():
    [a], _ = _build()
    [b], _ = _build(groups={**GROUPS, link.member_key(LISTING): "pg_listing_v2"})
    assert a["proposal_key"] != b["proposal_key"]
    assert _build()[0][0]["proposal_key"] == a["proposal_key"]  # an unchanged move dedupes


def test_the_move_sql_is_conditional_on_what_propose_time_saw():
    from services import identity_resolution as ir
    ck = " ".join(ir.MOVE_CONTENT_KEY_SQL.split())
    pg = " ".join(ir.MOVE_GROUP_SQL.split())
    assert "WHERE product_key = $1 AND content_key = $3 AND suppression_reason IS NULL" in ck
    assert "AND product_group_id = $5" in pg


@pytest.mark.asyncio
async def test_a_write_that_touches_no_row_aborts_the_run():
    conn = _conn()

    async def lost_race(sql, *args):
        if " ".join(sql.split()).startswith("UPDATE product_group_members"):
            return "UPDATE 0"
        return await StateConn.execute(conn, sql, *args)
    conn.execute = lost_race
    with pytest.raises(RuntimeError):
        await apply_approved(conn, run_id="R5", strategies=[link.STRATEGY])


@pytest.mark.asyncio
async def test_a_revert_write_that_loses_a_race_aborts_the_revert():
    """The pre-check passed but the conditional write touched nothing: roll the whole revert back."""
    conn = _conn()
    await apply_approved(conn, run_id="R6", strategies=[link.STRATEGY])
    real = conn.execute

    async def lost_race(sql, *args):
        if " ".join(sql.split()).startswith("UPDATE product_group_members"):
            return "UPDATE 0"
        return await real(sql, *args)
    conn.execute = lost_race
    with pytest.raises(RuntimeError):
        await revert_run(conn, "R6")



# --- re-review of #2390 -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_row_that_lands_on_the_old_key_after_propose_aborts_the_run():
    """Checked again INSIDE the apply transaction, after every move: a row written onto the old content_key
    since propose time would pull the moved listing back on its next crawl."""
    newcomer = {**LISTING, "product_key": "ext:retailer:" + "d" * 32, "source_product_id": "retailer:" + "d" * 32}
    conn = _conn(rows=[LISTING, BRAND, newcomer])
    with pytest.raises(RuntimeError, match="left on"):
        await apply_approved(conn, run_id="R7", strategies=[link.STRATEGY])


@pytest.mark.asyncio
async def test_a_listing_suppressed_after_the_run_is_left_alone_by_revert():
    conn = _conn()
    await apply_approved(conn, run_id="R8", strategies=[link.STRATEGY])
    conn.rows[LISTING["product_key"]]["suppression_reason"] = "d2_same_url_dup"
    back = await revert_run(conn, "R8")
    assert back["detached"][0]["reverted"] is False
    assert conn.groups[("external_seed", "external_seed", LISTING["source_product_id"])] == "pg_brand"


@pytest.mark.asyncio
async def test_revert_needs_the_group_to_still_be_this_runs_too():
    conn = _conn()
    await apply_approved(conn, run_id="R9", strategies=[link.STRATEGY])
    conn.groups[("external_seed", "external_seed", LISTING["source_product_id"])] = "pg_curated_later"
    back = await revert_run(conn, "R9")
    assert back["detached"][0]["reverted"] is False
    assert conn.rows[LISTING["product_key"]]["content_key"] == BRAND_CK  # not half-reverted


def test_the_listings_own_merchant_brand_row_is_joined_first():
    """Tier-0e's order: the listing's merchant before an older row of another merchant."""
    other = {**BRAND, "product_key": "prod::m_other::shopify::1", "merchant_id": "m_other",
             "source_product_id": "1", "created_at": "2026-01-01"}
    groups = {**GROUPS, link.member_key(other): "pg_other"}
    [p], _ = _build(families={BRAND_CK: [other, BRAND]}, groups=groups)
    assert p["keeper_product_key"] == BRAND["product_key"]


def test_an_old_content_key_that_was_not_read_is_never_assumed_empty():
    proposals, counts = link.build_proposals([LISTING], {BRAND_CK: [BRAND]}, GROUPS, {}, {"pg_listing": 1})
    assert proposals == [] and counts == {"old_content_key_not_read": 1}
    proposals, counts = link.build_proposals([LISTING], {BRAND_CK: [BRAND]}, GROUPS,
                                             {LISTING_CK: [LISTING["product_key"]]}, {})
    assert proposals == [] and counts == {"old_group_has_other_members": 1}  # an unread group size too
