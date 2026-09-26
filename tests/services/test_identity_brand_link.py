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
    return link.build_proposals(listings if listings is not None else [LISTING],
                                families if families is not None else {BRAND_CK: [BRAND]},
                                groups if groups is not None else GROUPS)


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
    assert d["reverted_content_key"] and d["reverted_group"]


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
    assert back["detached"][0]["reverted_content_key"] is False


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
    assert seen["refresh"] == [BRAND_CK, LISTING_CK, "ck_boom", LISTING_CK]
    assert out == {"refreshed": 1, "reaped": 2, "recomputed": 3, "errors": 1}
