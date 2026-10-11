"""The brand-key retire hands a content_key's canonical URL to the new key when the old row holds it.

THE STALEMATE (measured on prod 2026-10-09/10). The tower28beauty.com re-run wrote 8 gift sets under "Tower 28" that
share a content_key with their "Tower 28 Beauty" rows. The old row held the content_key's canonical election (a
step-5 keeper since 2026-07-27), so the new row landed shadow NON_CANONICAL_DUPLICATE; not searchable, so the retire
kept the old row (`new_not_serving`); kept live, the old row kept its election. Nothing on either side moves.

These drive the REAL pieces: `pick_winner` (inside select_handovers), `plan_for_cohort`, `write_retire`,
`revert_manifest`, and the real trust policy through `preview_serving_decisions`. Only the database is a
stand-in; the SQL itself runs in tests/test_retire_canonical_handover_postgres.py. Every positive case has a
refusing twin.
"""
import json

import pytest

from scripts import retire_superseded_brand_keys as tool
from services import catalog_row_trust_upserter as upserter
from services.content_canonical_election import (
    HANDOVER_ELECTION_SQL,
    KEEPER_SIGS_FOR_CONTENT_KEYS_SQL,
    REASON_DEDUPE_KEEPER,
    REASON_SITEMAP_INCUMBENT,
    REASON_SOLE_CANDIDATE,
    keeper_after_retire,
)
from tests.test_catalog_row_trust_upserter import FakeDb as TrustFakeDb, make_joined_row

CK = "ck_cheeky"
OLD_SIG, NEW_SIG = "sig_26b604a83751dcf88b18c79da81a7716", "sig_6afee0ba02f17d5646215bf12a51df87"
OLD, NEW = "ext:tower-28-beauty-the-cheeky-duo::46e75de5", "ext:tower-28-the-cheeky-duo::6508df56"
HOST = "tower28beauty.com"
PAIR = {"stale_key": OLD, "new_key": NEW, "brand": "Tower 28", "title": "The Cheeky Duo"}


def _old(**kw):
    return {"product_key": OLD, "content_key": CK, "pivota_signature_id": OLD_SIG, "source_domain": HOST,
            "suppression_reason": None, "suppressed_at": None, "suppression_metadata": None,
            "pdp_lifecycle_stage": "published", "brand": "Tower 28 Beauty", "title": "The Cheeky Duo", **kw}


def _new(**kw):
    return {"product_key": NEW, "content_key": CK, "pivota_signature_id": NEW_SIG, "source_domain": HOST,
            "suppression_reason": None, "suppressed_at": None, "suppression_metadata": None,
            "pdp_lifecycle_stage": "published", "brand": "Tower 28", "title": "The Cheeky Duo", **kw}


def _handover_pair(**kw):
    return {"content_key": CK, "stale_key": OLD, "new_key": NEW, "from_sig": OLD_SIG, "to_sig": NEW_SIG, **kw}


def _select(*, election=OLD_SIG, candidates=(OLD_SIG, NEW_SIG), keepers=(OLD_SIG,), public=(NEW,)):
    return tool.select_handovers(
        [_handover_pair()],
        elections={CK: {"canonical_sig_id": election, "election_reason": REASON_DEDUPE_KEEPER}} if election else {},
        candidates={CK: list(candidates)}, live_keepers={CK: list(keepers)}, public_if_elected=set(public))


# ---------------------------------------------------------------------------------------------------------------
# select_handovers -- the election's own pick_winner decides
# ---------------------------------------------------------------------------------------------------------------

def test_the_tower_28_shape_hands_the_url_to_the_new_key():
    out = _select()
    assert out == {OLD: {**_handover_pair(), "prior_election_reason": REASON_DEDUPE_KEEPER,
                         "election_reason": REASON_DEDUPE_KEEPER}}


def test_a_url_the_old_row_does_not_hold_is_not_the_retires_to_hand_over():
    assert _select(election="sig_ffffffffffffffffffffffffffffffff") == {}
    assert _select(election=None) == {}  # no election at all: the new row is not shadowed by one


def test_a_new_row_the_policy_keeps_shadowed_even_when_elected_is_not_handed_the_url():
    assert _select(public=()) == {}


def test_a_new_row_that_is_not_an_election_candidate_is_not_handed_the_url():
    assert _select(candidates=(OLD_SIG,)) == {}


def test_a_competing_keeper_that_sorts_first_keeps_the_url_from_the_new_key():
    """KEEPER_SIGS_SQL keeps the LOWEST live keeper sig. Another live keeper below the new sig wins the next sweep,
    so the URL would land there, not on the new key: refuse. The same keeper sorting AFTER the new sig loses to it
    and the handover goes ahead -- the min rule is load-bearing in both directions."""
    low, high = "sig_00000000000000000000000000000000", "sig_ffffffffffffffffffffffffffffffff"
    assert _select(candidates=(OLD_SIG, NEW_SIG, low), keepers=(OLD_SIG, low)) == {}
    assert OLD in _select(candidates=(OLD_SIG, NEW_SIG, high), keepers=(OLD_SIG, high))


def test_a_competing_keeper_that_is_not_a_candidate_leaves_the_url_to_the_new_key():
    """The sweep's keeper rung skips a keeper that is not a candidate, and the stored winner (the old sig) is gone
    once tombstoned, so the URL falls to the new key as the sole candidate left. Judged on the post-retire pool:
    with the old sig still in it, stickiness would wrongly keep the URL where the retire is taking it from."""
    low = "sig_00000000000000000000000000000000"
    out = _select(candidates=(OLD_SIG, NEW_SIG), keepers=(OLD_SIG, low))
    assert out[OLD]["election_reason"] == REASON_SOLE_CANDIDATE


def test_without_a_keeper_the_new_key_wins_only_as_the_sole_remaining_candidate():
    """The successor is the keeper the retire itself writes, so even a content_key with no step-5 keeper today
    hands over -- and pick_winner names it as the keeper, not by luck of the lexicographic order."""
    out = _select(keepers=())
    assert out[OLD]["election_reason"] == REASON_DEDUPE_KEEPER


def test_keeper_after_retire_mirrors_keeper_sigs_sql_ordering():
    assert keeper_after_retire([OLD_SIG], retired_sig=OLD_SIG, successor_sig=NEW_SIG) == NEW_SIG
    assert keeper_after_retire([], retired_sig=OLD_SIG, successor_sig=NEW_SIG) == NEW_SIG
    assert keeper_after_retire(["sig_0", OLD_SIG], retired_sig=OLD_SIG, successor_sig=NEW_SIG) == "sig_0"
    assert keeper_after_retire(["sig_z"], retired_sig=OLD_SIG, successor_sig=NEW_SIG) == NEW_SIG


# ---------------------------------------------------------------------------------------------------------------
# handover_candidates -- which pairs are worth asking about
# ---------------------------------------------------------------------------------------------------------------

def _candidates(old=None, new=None, searchable=(OLD,)):
    return tool.handover_candidates([PAIR], {OLD: old or _old()}, {NEW: new or _new()}, searchable=set(searchable))


def test_the_tower_28_pair_is_a_handover_candidate():
    assert _candidates() == [_handover_pair()]


@pytest.mark.parametrize("old,new,searchable", [
    (_old(content_key="ck_other"), None, (OLD,)),                       # two different pages
    (None, _new(content_key=None), (OLD,)),
    (None, _new(pivota_signature_id=None), (OLD,)),                     # no URL to hand to
    (None, _new(pivota_signature_id="legacy_123"), (OLD,)),
    (_old(pivota_signature_id=None), None, (OLD,)),
    (None, _new(pdp_lifecycle_stage="candidate"), (OLD,)),              # recall would not admit it anyway
    (None, None, ()),                                                   # old not searchable: plain retire
    (None, None, (OLD, NEW)),                                           # new already searchable: plain retire
    (_old(suppression_reason="x"), None, (OLD,)),
])
def test_pairs_that_are_not_one_page_or_not_blocked_by_search_are_not_candidates(old, new, searchable):
    assert _candidates(old, new, searchable) == []


def test_a_new_row_at_a_null_or_validated_stage_is_a_candidate():
    assert _candidates(new=_new(pdp_lifecycle_stage=None)) and _candidates(new=_new(pdp_lifecycle_stage="validated"))


# ---------------------------------------------------------------------------------------------------------------
# select_retirable -- a handover moves the pair from NEW NOT SERVING to retire, never past the serving rule
# ---------------------------------------------------------------------------------------------------------------

def _split(*, serving=(OLD, NEW), handovers=None):
    return tool.select_retirable([PAIR], {OLD: _old()}, {NEW}, HOST, serving=set(serving), searchable={OLD},
                                 handovers=handovers)


def test_a_handed_over_pair_is_retired():
    h = {OLD: {**_handover_pair(), "election_reason": REASON_DEDUPE_KEEPER}}
    out = _split(handovers=h)
    assert out["live"] == [PAIR] and out["new_not_serving"] == [] and out["handovers"] == [h[OLD]]


def test_without_a_handover_the_pair_waits_as_before():
    out = _split()
    assert out["live"] == [] and out["new_not_serving"] == [PAIR] and out["handovers"] == []


def test_a_handover_never_retires_a_served_old_row_whose_new_row_does_not_serve():
    out = _split(serving=(OLD,), handovers={OLD: _handover_pair()})
    assert out["live"] == [] and out["new_not_serving"] == [PAIR] and out["handovers"] == []


# ---------------------------------------------------------------------------------------------------------------
# preview_serving_decisions -- the real trust policy, read-only
# ---------------------------------------------------------------------------------------------------------------

async def test_the_preview_asks_the_real_policy_and_writes_nothing():
    db = TrustFakeDb(joined_rows=[make_joined_row(product_key=NEW, row_is_elected_canonical=False)])
    assert await upserter.preview_serving_decisions(db=db, product_keys=[NEW]) == {NEW: "shadow"}
    assert await upserter.preview_serving_decisions(db=db, product_keys=[NEW], row_is_elected_canonical=True) == {
        NEW: "public"}
    assert db.executes == []


async def test_a_row_shadowed_for_another_reason_stays_shadow_when_elected():
    db = TrustFakeDb(joined_rows=[make_joined_row(product_key=NEW, row_is_elected_canonical=False,
                                                  identity_status="review_required", review_required=True)])
    assert await upserter.preview_serving_decisions(db=db, product_keys=[NEW], row_is_elected_canonical=True) == {
        NEW: "shadow"}


# ---------------------------------------------------------------------------------------------------------------
# plan_for_cohort -- end to end through the real plan, against a stand-in database
# ---------------------------------------------------------------------------------------------------------------

class PlanDB:
    """Answers each query plan_for_cohort makes from in-memory state; raises on any other."""

    def __init__(self, *, election=OLD_SIG, candidates=(OLD_SIG, NEW_SIG), keepers=(OLD_SIG,), elected_ok=True,
                 searchable=(OLD,)):
        self.rows = {OLD: _old(), NEW: _new()}
        self.election, self.candidates, self.keepers = election, list(candidates), list(keepers)
        self.searchable = set(searchable)
        self.joined = [make_joined_row(product_key=NEW, content_key=CK, row_is_elected_canonical=False,
                                       **({} if elected_ok else {"identity_status": "review_required",
                                                                 "review_required": True}))]
        self.asked = []

    async def fetch_all(self, sql, values=None):
        values = values or {}
        if not isinstance(sql, str):  # candidates_query(content_keys=...): a SQLAlchemy select
            asked = [v for k, v in sql.compile().params.items() if k.startswith("content_key")]
            self.asked.append(("candidates", asked))
            return [{"content_key": CK, "pivota_signature_id": s} for s in self.candidates if [CK] in asked]
        self.asked.append(sql)
        if sql == tool.LIVE_ROWS_SQL:
            return [self.rows[k] for k in values["keys"] if k in self.rows]
        if sql == tool.SERVING_SQL:
            return [{"content_key": CK, "serving_eligible": True}]
        if sql == tool.SEARCHABLE_SQL:
            return [{"product_key": k} for k in values["keys"] if k in self.searchable]
        if sql == tool.ELECTIONS_SQL:
            return ([{"content_key": CK, "canonical_sig_id": self.election, "election_reason": REASON_DEDUPE_KEEPER}]
                    if self.election and CK in values["keys"] else [])
        if sql == KEEPER_SIGS_FOR_CONTENT_KEYS_SQL:
            return [{"content_key": CK, "keeper_sig_id": s} for s in self.keepers]
        if sql == tool.SEEDS_FOR_KEYS_SQL:
            return []
        if "FROM catalog_source_quarantine" in sql:
            return []
        if "product_key = ANY(:product_keys)" in sql:  # the upserter's own join, for the trust preview
            return [r for r in self.joined if r["product_key"] in values["product_keys"]]
        raise AssertionError(f"unexpected SQL: {sql[:120]}")


@pytest.fixture
def plan_db(monkeypatch):
    def make(**kw):
        db = PlanDB(**kw)
        monkeypatch.setattr(tool, "database", db)

        async def cascade(keys, apply=False):
            return []
        monkeypatch.setattr(tool, "cascade_for_suppressed_product_keys", cascade)
        return db
    return make


async def _plan():
    return await tool.plan_for_cohort([PAIR], HOST, "Tower 28", "beauty", "Tower 28 Beauty")


async def test_the_plan_retires_the_tower_28_pair_with_its_url(plan_db):
    db = plan_db()
    p = await _plan()
    assert p["live"] == [PAIR] and p["new_not_serving"] == []
    assert [h["to_sig"] for h in p["handovers"]] == [NEW_SIG] and p["handover_error"] is None
    assert ("candidates", [[CK]]) in db.asked  # the election's candidate set, for THIS content_key only


async def test_the_plan_keeps_the_pair_when_the_policy_would_still_shadow_the_new_row(plan_db):
    plan_db(elected_ok=False)
    p = await _plan()
    assert p["live"] == [] and p["new_not_serving"] == [PAIR] and p["handovers"] == []


async def test_the_plan_keeps_the_pair_when_another_keeper_would_take_the_url(plan_db):
    low = "sig_00000000000000000000000000000000"
    plan_db(candidates=(OLD_SIG, NEW_SIG, low), keepers=(OLD_SIG, low))
    p = await _plan()
    assert p["live"] == [] and p["new_not_serving"] == [PAIR]


async def test_the_plan_asks_nothing_more_when_the_old_row_does_not_hold_the_url(plan_db):
    db = plan_db(election="sig_ffffffffffffffffffffffffffffffff")
    p = await _plan()
    assert p["new_not_serving"] == [PAIR]
    assert KEEPER_SIGS_FOR_CONTENT_KEYS_SQL not in db.asked and not any(isinstance(a, tuple) for a in db.asked)


async def test_a_failed_handover_check_hands_over_nothing_and_says_so(plan_db, monkeypatch):
    plan_db()

    async def boom(**kw):
        raise RuntimeError("trust join timed out")
    monkeypatch.setattr(tool, "preview_serving_decisions", boom)
    p = await _plan()
    assert p["live"] == [] and p["new_not_serving"] == [PAIR] and "trust join timed out" in p["handover_error"]


async def test_the_old_order_never_hands_over(plan_db):
    db = plan_db()
    p = await tool.plan_for_cohort([PAIR], HOST, "Tower 28", "beauty", "Tower 28 Beauty", before_rewrite=True)
    assert p["handovers"] == [] and tool.ELECTIONS_SQL not in db.asked


# ---------------------------------------------------------------------------------------------------------------
# prepare / write / revert -- one transaction each way
# ---------------------------------------------------------------------------------------------------------------

class Tx:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        self.db.events.append("begin")
        self.db.snapshot = (json.dumps(self.db.rows), dict(self.db.election))
        return self

    async def __aexit__(self, exc_type, *a):
        self.db.events.append("rollback" if exc_type else "commit")
        if exc_type:
            self.db.rows, self.db.election = json.loads(self.db.snapshot[0]), self.db.snapshot[1]
        return False


class WriteDB:
    """catalog_products (suppression columns), content_canonical_election and the product trust decisions."""

    def __init__(self):
        self.rows = {k: {"product_key": k, "suppression_reason": None, "suppression_metadata": None,
                         "canonical_url": f"https://{HOST}/{k}"} for k in (OLD, NEW)}
        self.election = {CK: {"canonical_sig_id": OLD_SIG, "election_reason": REASON_DEDUPE_KEEPER}}
        self.public = {OLD}
        self.events, self.snapshot = [], None

    def transaction(self):
        return Tx(self)

    def _run_of(self, k):
        return (self.rows[k]["suppression_metadata"] or {}).get("run_id")

    async def execute(self, sql, values):
        if sql == tool.SUPPRESS_SQL:
            for k in values["keys"]:
                if not self.rows[k]["suppression_reason"]:
                    self.rows[k].update(suppression_reason=values["reason"],
                                        suppression_metadata=json.loads(values["metadata"]))
            return
        if sql == tool.REACTIVATE_SEED_SQL:
            return
        raise AssertionError(f"unexpected execute: {sql[:80]}")

    async def fetch_one(self, sql, values):
        assert sql == tool.UNSUPPRESS_SQL
        k = values["key"]
        if self.rows[k]["suppression_reason"] == values["retired_reason"] and self._run_of(k) == values["run_id"]:
            self.rows[k].update(suppression_reason=values["reason"],
                                suppression_metadata=json.loads(values["metadata"]) if values["metadata"] else None)
            return {"product_key": k}
        return None

    async def fetch_all(self, sql, values):
        if sql == tool.DEACTIVATE_SEEDS_SQL:
            return []
        if sql == tool.LIVE_ROWS_SQL:
            return [dict(self.rows[k]) for k in values["keys"]]
        if sql == tool.URLS_FOR_KEYS_SQL:
            return [{"product_key": k, "canonical_url": self.rows[k]["canonical_url"]} for k in values["keys"]]
        if sql == tool.NAME_KEEPER_SQL:
            k = values["key"]
            if self.rows[k]["suppression_reason"] == values["reason"] and self._run_of(k) == values["run_id"]:
                self.rows[k]["suppression_metadata"]["keeper_product_key"] = values["keeper"]
                return [{"product_key": k}]
            return []
        if sql == HANDOVER_ELECTION_SQL:
            self.events.append(("election", values["to_sig"]))
            e = self.election.get(values["content_key"])
            if e and e["canonical_sig_id"] == values["from_sig"]:
                e.update(canonical_sig_id=values["to_sig"], election_reason=values["election_reason"])
                return [{"content_key": values["content_key"]}]
            return []
        if sql == tool.PUBLIC_TRUST_SQL:
            return [{"subject_key": k} for k in values["keys"] if k in self.public]
        if sql == tool.STILL_RETIRED_SQL:
            return [{"product_key": k} for k in values["keys"]
                    if self.rows[k]["suppression_reason"] == values["reason"] and self._run_of(k) == values["run_id"]]
        raise AssertionError(f"unexpected SQL: {sql[:80]}")


@pytest.fixture
def write_db(monkeypatch):
    db = WriteDB()
    monkeypatch.setattr(tool, "database", db)
    calls = []

    async def upsert_many(*, db, product_keys):
        """Stands in for the trust upserter: tombstone -> blocked, the elected sig's live row -> public, else
        shadow (what catalog_trust_policy decides for these rows; the policy itself is tested above)."""
        calls.append(list(product_keys))
        events = tool.database.events
        events.append("trust")
        for k in product_keys:
            row, sig = tool.database.rows[k], OLD_SIG if k == OLD else NEW_SIG
            if not row["suppression_reason"] and tool.database.election[CK]["canonical_sig_id"] == sig:
                tool.database.public.add(k)
            else:
                tool.database.public.discard(k)
        return len(product_keys)

    async def cascade(keys, apply=False):
        return []

    async def no_owner(db, url):
        return None
    monkeypatch.setattr(tool, "upsert_catalog_row_trust_many", upsert_many)
    monkeypatch.setattr(tool, "cascade_for_suppressed_product_keys", cascade)
    monkeypatch.setattr(tool, "live_retailer_listing_owner", no_owner)
    db.trust_calls = calls
    return db


def _plan_with_handover():
    return {"live": [PAIR], "rows": {OLD: _old()}, "active_seeds": [], "domain": HOST, "brand_override": "Tower 28",
            "category_path": "beauty", "stale_brand": "Tower 28 Beauty",
            "handovers": [{**_handover_pair(), "prior_election_reason": REASON_DEDUPE_KEEPER,
                           "election_reason": REASON_DEDUPE_KEEPER}]}


async def test_the_retire_hands_the_url_over_in_its_own_transaction(write_db):
    prepared = tool.prepare_retire(_plan_with_handover())
    assert prepared["manifest"]["canonical_handovers"][0]["from_sig"] == OLD_SIG  # before-state, for revert
    counts = await tool.write_retire(prepared)
    assert write_db.events == ["begin", ("election", NEW_SIG), "commit", "trust"]
    assert write_db.election[CK] == {"canonical_sig_id": NEW_SIG, "election_reason": REASON_DEDUPE_KEEPER}
    assert write_db.rows[OLD]["suppression_metadata"]["keeper_product_key"] == NEW
    assert write_db.trust_calls == [[OLD, NEW]]  # the new row's trust too, so it is public at once
    assert write_db.public == {NEW}
    assert counts["canonical_handovers"] == 1 and "trust_problems" not in counts


async def test_an_election_that_moved_since_the_plan_refuses_the_whole_retire(write_db):
    write_db.election[CK]["canonical_sig_id"] = "sig_ffffffffffffffffffffffffffffffff"
    with pytest.raises(RuntimeError, match="canonical not handed over"):
        await tool.write_retire(tool.prepare_retire(_plan_with_handover()))
    assert write_db.events[-1] == "rollback" and "trust" not in write_db.events
    assert write_db.rows[OLD]["suppression_reason"] is None  # nothing retired


async def test_a_handed_over_key_that_does_not_go_public_is_a_trust_problem(write_db, monkeypatch, capsys):
    async def stuck_upsert(*, db, product_keys):  # the trust refresh leaves the new row shadowed
        return len(product_keys)
    monkeypatch.setattr(tool, "upsert_catalog_row_trust_many", stuck_upsert)
    counts = await tool.write_retire(tool.prepare_retire(_plan_with_handover()))
    assert any("handed-over key(s) not public" in p for p in counts["trust_problems"])


async def test_a_retire_without_handovers_writes_no_election_and_no_keeper(write_db):
    p = {**_plan_with_handover(), "handovers": []}
    prepared = tool.prepare_retire(p)
    counts = await tool.write_retire(prepared)
    assert "canonical_handovers" not in prepared["manifest"] and "canonical_handovers" not in counts
    assert write_db.election[CK]["canonical_sig_id"] == OLD_SIG
    assert "keeper_product_key" not in write_db.rows[OLD]["suppression_metadata"]
    assert write_db.trust_calls == [[OLD]]


async def test_revert_hands_the_url_back_with_its_prior_reason(write_db, capsys):
    plan = _plan_with_handover()
    plan["handovers"][0]["prior_election_reason"] = REASON_SITEMAP_INCUMBENT  # not the handover's own reason
    write_db.election[CK]["election_reason"] = REASON_SITEMAP_INCUMBENT
    prepared = tool.prepare_retire(plan)
    await tool.write_retire(prepared)
    assert write_db.election[CK]["election_reason"] == REASON_DEDUPE_KEEPER
    await tool.revert_manifest(json.loads(json.dumps(prepared["manifest"])))
    assert write_db.rows[OLD]["suppression_reason"] is None
    assert write_db.rows[OLD]["suppression_metadata"] is None  # the keeper pointer goes with the tombstone
    assert write_db.election[CK] == {"canonical_sig_id": OLD_SIG, "election_reason": REASON_SITEMAP_INCUMBENT}
    assert "1 canonical URL(s) handed back" in capsys.readouterr().out
    # Only the restored row's trust is recomputed by the revert; the new row stays public until refresh-trust.
    assert write_db.trust_calls[-1] == [OLD] and NEW in write_db.public


async def test_revert_leaves_an_election_that_moved_since_alone(write_db, capsys):
    prepared = tool.prepare_retire(_plan_with_handover())
    await tool.write_retire(prepared)
    write_db.election[CK]["canonical_sig_id"] = "sig_ffffffffffffffffffffffffffffffff"
    await tool.revert_manifest(prepared["manifest"])
    assert write_db.election[CK]["canonical_sig_id"] == "sig_ffffffffffffffffffffffffffffffff"
    assert write_db.rows[OLD]["suppression_reason"] is None
    assert "canonical URL not handed back" in capsys.readouterr().out


async def test_revert_never_points_the_url_at_a_row_it_did_not_restore(write_db, monkeypatch, capsys):
    prepared = tool.prepare_retire(_plan_with_handover())
    await tool.write_retire(prepared)

    async def owner(db, url):  # a retailer listing now owns the old row's URL: the old row stays retired
        return "listing_x" if url.endswith(OLD) else None
    monkeypatch.setattr(tool, "live_retailer_listing_owner", owner)
    await tool.revert_manifest(prepared["manifest"])
    assert write_db.rows[OLD]["suppression_reason"] == tool.REASON
    assert write_db.election[CK]["canonical_sig_id"] == NEW_SIG


async def test_refresh_trust_for_a_manifest_checks_the_handed_over_key_is_public(write_db, monkeypatch):
    prepared = tool.prepare_retire(_plan_with_handover())
    await tool.write_retire(prepared)
    out = await tool.refresh_trust_for_manifest(prepared["manifest"])
    assert write_db.trust_calls[-1] == [OLD, NEW] and "trust_problems" not in out
    write_db.public.discard(NEW)

    async def stuck_upsert(*, db, product_keys):
        return len(product_keys)
    monkeypatch.setattr(tool, "upsert_catalog_row_trust_many", stuck_upsert)
    out = await tool.refresh_trust_for_manifest(prepared["manifest"])
    assert any("handed-over key(s) not public" in p for p in out["trust_problems"])


async def test_after_a_revert_refresh_trust_no_longer_requires_the_new_key_public(write_db):
    prepared = tool.prepare_retire(_plan_with_handover())
    await tool.write_retire(prepared)
    await tool.revert_manifest(prepared["manifest"])
    out = await tool.refresh_trust_for_manifest(prepared["manifest"])
    assert "trust_problems" not in out and write_db.public == {OLD}  # back to shadow, old row public again


def test_the_candidate_set_narrowed_to_content_keys_is_the_sweeps_own_predicate():
    """candidates_query(content_keys=...) is the sweep's query plus one IN filter -- never a second rule."""
    from sqlalchemy.dialects import postgresql

    from services.content_canonical_election import candidates_query

    def sql(**kw):
        return str(candidates_query(widen=True, **kw).compile(dialect=postgresql.dialect()))
    whole, narrowed = sql(), sql(content_keys=[CK])
    assert "catalog_products.content_key IN" in narrowed and "catalog_products.content_key IN" not in whole
    assert narrowed.split(" ORDER BY")[0].startswith(whole.split(" ORDER BY")[0])
