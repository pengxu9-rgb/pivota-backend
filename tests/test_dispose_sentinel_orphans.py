"""scripts/dispose_sentinel_orphans.py — doors, bind exactness, and dry-run safety.

The door layer is pure (`offer_doors` / `review_doors`), so it is tested
directly and in BOTH directions: every door has a passing population and a
failing one. The dry-run path is tested against a fake that RAISES on any
write, because "dry-run by default" is the property a reader most needs to be
true and the easiest to break silently.

Row-level arithmetic (does the re-key follow the product? does the delete take
only the residue?) is pinned in tests/test_dispose_sentinel_orphans_postgres.py
against a real engine — a fake that answers its own SQL cannot constrain it,
which is the lesson from the recon's review (12 surviving mutants).
"""

from __future__ import annotations

import re

import pytest

from scripts import dispose_sentinel_orphans as d

_B = d.BANNED_BUCKET_MERCHANT_ID


def _assert_binds_match(sql, params):
    named = set(re.findall(r"(?<!:):([a-zA-Z_][a-zA-Z0-9_]*)", sql))
    given = set((params or {}).keys())
    assert named == given, (
        f"bind mismatch: SQL names {sorted(named)}, params give {sorted(given)}\nSQL: {sql}")


def _offer(oid="o1", *, exists=True, target="merch_obs_a", backed=True, tomb=False):
    return {"offer_id": oid, "product_key": f"pk_{oid}", "sku_key": f"sku_{oid}",
            "merchant_id": _B, "source_system": "us_market_capture",
            "source_domain": "x.com", "currency": "USD", "list_price": 9,
            "created_at": None, "target_merchant": target if exists else None,
            "product_exists": exists, "product_tombstoned": tomb,
            "target_merchant_exists": backed}


def _review(rid=1, *, resolves=False, status="removed"):
    return {"id": rid, "product_key": f"{_B}|external_seed|sp{rid}", "merchant_id": _B,
            "status": status, "scope_resolves": resolves}


# -- doors, both directions -------------------------------------------------

def test_offer_doors_pass_on_the_measured_population():
    assert d.offer_doors(0, [_offer("o1"), _offer("o2", target="merch_obs_b")]) == []


def test_offer_doors_each_fail_for_its_own_reason():
    assert any("D1" in f for f in d.offer_doors(1, [_offer()]))
    assert any("D2" in f for f in d.offer_doors(0, [_offer(exists=False)]))
    assert any("D3" in f for f in d.offer_doors(0, [_offer(target=_B)]))
    assert any("D4" in f for f in d.offer_doors(0, [_offer(backed=False)]))
    # A tombstoned product is NOT a door failure: the offer still follows its
    # product's seller. Pinning this stops a future "tighten the doors" edit
    # from silently dropping rows the founder gated.
    assert d.offer_doors(0, [_offer(tomb=True)]) == []


def test_review_doors_pass_only_for_unresolvable_non_serving_rows():
    assert d.review_doors(0, [_review(1), _review(2, status="under_review")]) == []
    assert any("D5" in f for f in d.review_doors(0, [_review(resolves=True)]))
    assert any("D6" in f for f in d.review_doors(0, [_review(status="active")]))
    assert any("D1" in f for f in d.review_doors(3, [_review()]))


def test_a_door_failure_names_the_offending_rows_not_just_a_count():
    """An operator has to be able to go look at the row. A bare count would
    make every failure identical and unactionable."""
    fail = d.offer_doors(0, [_offer("o_bad", exists=False)])
    assert any("o_bad" in f for f in fail)
    fail = d.review_doors(0, [_review(4242, resolves=True)])
    assert any("4242" in f for f in fail)


# -- dry-run never writes ---------------------------------------------------

class ReadOnlyFake:
    """Answers the tool's reads; raises on anything that could write."""

    def __init__(self, bucket=0, offers=None, reviews=None, children_fks=None):
        self.bucket = bucket
        self.offers = offers if offers is not None else [_offer()]
        self.reviews = reviews if reviews is not None else [_review()]
        # The FK graph, as pg_constraint would report it.
        self.children_fks = children_fks if children_fks is not None else [
            {"child_table": "media_assets", "fk_column": "review_id", "on_delete": "c"},
            {"child_table": "buyer_review_ownership", "fk_column": "review_id",
             "on_delete": "c"},
            {"child_table": "buyer_review_idempotency_keys", "fk_column": "review_id",
             "on_delete": "n"},
        ]

    async def execute(self, sql, params=None):  # pragma: no cover
        raise AssertionError(f"dry-run wrote: {sql}")

    def transaction(self):  # pragma: no cover
        raise AssertionError("dry-run opened a transaction")

    async def fetch_one(self, sql, params=None):
        _assert_binds_match(sql, params)
        assert "catalog_products" in sql
        return {"c": self.bucket}

    async def fetch_all(self, sql, params=None):
        _assert_binds_match(sql, params)
        flat = " ".join(sql.split())
        if "FROM catalog_offers o" in flat:
            return self.offers
        if "FROM product_reviews r" in flat:
            return self.reviews
        if "pg_constraint" in flat:
            return self.children_fks
        if "= ANY(:ids)" in flat:
            return []
        raise AssertionError(f"unexpected read: {flat}")


def _plan(fake, tables=("catalog_offers", "product_reviews")):
    import asyncio
    return asyncio.run(d.plan(fake, list(tables)))


def test_plan_writes_nothing_and_reports_the_per_row_move():
    out = _plan(ReadOnlyFake())
    assert out["doors_failed"] == []
    move = out["tables"]["catalog_offers"]["plan"][0]
    assert move["from"] == _B and move["to"] == "merch_obs_a"
    # The review dump is the reversal record — it must carry the rows, not a count.
    assert out["tables"]["product_reviews"]["dump"][0]["id"] == 1


def test_plan_surfaces_door_failures_from_every_selected_table():
    out = _plan(ReadOnlyFake(offers=[_offer(exists=False)], reviews=[_review(resolves=True)]))
    assert any("D2" in f for f in out["doors_failed"])
    assert any("D5" in f for f in out["doors_failed"])


def test_selecting_one_table_does_not_read_or_plan_the_other():
    out = _plan(ReadOnlyFake(), tables=("catalog_offers",))
    assert set(out["tables"]) == {"catalog_offers"}


def test_an_unreadable_child_table_ABORTS_rather_than_deleting_blind():
    """A cascade child the FK graph names but that cannot be read is a hole in
    the reversal record. Recording it and deleting anyway is the silent-fallback
    shape the house rules ban: the rows would be destroyed with nothing written
    down. It must fail the door layer."""
    class NoChildren(ReadOnlyFake):
        async def fetch_all(self, sql, params=None):
            if "= ANY(:ids)" in " ".join(sql.split()):
                raise RuntimeError("relation does not exist")
            return await super().fetch_all(sql, params)

    out = _plan(NoChildren(), tables=("product_reviews",))
    assert any("D7" in f for f in out["doors_failed"])
    kids = out["tables"]["product_reviews"]["cascaded_children"]
    assert all("unreadable" in v for v in kids.values())


def test_cascade_children_come_from_the_fk_graph_not_a_hardcoded_list():
    """The first cut listed migration 040's four tables and missed three FKs
    (buyer_review_ownership, buyer_review_user_subject, and a SET NULL one),
    which made the 'reversible by re-insert' claim false."""
    out = _plan(ReadOnlyFake(), tables=("product_reviews",))
    kids = out["tables"]["product_reviews"]["cascaded_children"]
    assert set(kids) == {"media_assets", "buyer_review_ownership",
                         "buyer_review_idempotency_keys"}
    # the dump says WHAT happens to each child, so a SET NULL parent is not
    # mistaken for an untouched one
    assert kids["media_assets"]["on_delete"] == "cascade"
    assert kids["buyer_review_idempotency_keys"]["on_delete"] == "set null"


def test_a_child_identifier_that_cannot_be_interpolated_aborts():
    bad = [{"child_table": "media assets; DROP TABLE x", "fk_column": "review_id",
            "on_delete": "c"}]
    with pytest.raises(RuntimeError, match="refusing to interpolate"):
        _plan(ReadOnlyFake(children_fks=bad), tables=("product_reviews",))


# -- argument surface -------------------------------------------------------

def test_unknown_table_is_refused_rather_than_silently_ignored():
    with pytest.raises(SystemExit):
        d._parse(["--tables", "catalog_products"])
    with pytest.raises(SystemExit):
        d._parse(["--tables", ""])
    # A MIXED list is the case that matters: silently dropping the unknown half
    # would run a partial disposition while the operator believes both ran.
    with pytest.raises(SystemExit):
        d._parse(["--tables", "catalog_offers,not_a_table"])
    assert d._parse(["--tables", "catalog_offers"]).table_list == ["catalog_offers"]
    assert d._parse([]).table_list == list(d.ALL_TABLES)


def test_apply_is_off_by_default():
    assert d._parse([]).apply is False
    assert d._parse(["--apply"]).apply is True


def test_the_write_statements_never_name_a_merchant_literal():
    """The re-key target is read from the catalog row in the statement; a
    literal would let the tool invent a seller."""
    assert ":banned" in d.OFFERS_REKEY_SQL
    assert "cp.merchant_id" in d.OFFERS_REKEY_SQL
    assert "'merch" not in d.OFFERS_REKEY_SQL
    assert "'external_seed'" not in d.OFFERS_REKEY_SQL
    assert "'external_seed'" not in d.REVIEWS_DELETE_SQL
    # The delete is scoped by BOTH the id list and the sentinel, so a stale id
    # list can never reach a row that is not residue.
    assert "id = ANY(:ids)" in d.REVIEWS_DELETE_SQL
    assert "merchant_id = :banned" in d.REVIEWS_DELETE_SQL


# -- the _run orchestration -------------------------------------------------
#
# Review (2026-08-17) found this layer had NO test: six mutants survived here,
# including "ignore doors_failed and apply anyway", "print the dump AFTER the
# write", and "dry-run is no longer the default". Those are the three
# properties the module docstring sells hardest, so they are pinned here.

class _RunSpy:
    """Records the ORDER of everything _run does, so 'dump before write' is a
    checkable claim rather than a comment."""

    def __init__(self, *, doors=(), tmp_path=None):
        self.events = []
        self.doors = list(doors)
        self.tmp_path = tmp_path
        self.plan_calls = 0

    async def plan(self, db, tables):
        self.plan_calls += 1
        self.events.append(("plan", tuple(tables)))
        rows = 0 if self.plan_calls > 1 else 1
        return {"banned": _B, "catalog_products_under_sentinel": 0,
                "doors_failed": list(self.doors),
                "tables": {t: {"rows": rows, "plan": [], "dump": []} for t in tables}}

    async def apply(self, db, tables, run_id, plan_result):
        self.events.append(("apply", tuple(tables)))
        return {"run_id": run_id, "applied": {t: {} for t in tables}}


def _run_with(monkeypatch, spy, *, do_apply, dump_file=None):
    import asyncio

    class _DB:
        async def connect(self): spy.events.append(("connect", ()))
        async def disconnect(self): spy.events.append(("disconnect", ()))

    import db.database as dbmod
    monkeypatch.setattr(dbmod, "database", _DB(), raising=False)
    monkeypatch.setattr(d, "plan", spy.plan)
    monkeypatch.setattr(d, "apply", spy.apply)

    def _dump(path, payload):
        spy.events.append(("dump", payload.get("mode")))
        return path
    monkeypatch.setattr(d, "_write_dump", _dump)
    return asyncio.run(d._run(list(d.ALL_TABLES), do_apply, dump_file))


def test_run_never_applies_when_a_door_failed(monkeypatch):
    spy = _RunSpy(doors=["D5 something"])
    rc = _run_with(monkeypatch, spy, do_apply=True)
    assert rc == 2
    assert not any(e[0] == "apply" for e in spy.events), "applied despite a failed door"


def test_run_is_dry_by_default_and_writes_nothing(monkeypatch):
    spy = _RunSpy()
    rc = _run_with(monkeypatch, spy, do_apply=False)
    assert rc == 0
    assert not any(e[0] == "apply" for e in spy.events)


def test_run_dumps_BEFORE_it_writes(monkeypatch):
    """A dump written after the delete is not a reversal record."""
    spy = _RunSpy()
    rc = _run_with(monkeypatch, spy, do_apply=True)
    assert rc == 0
    order = [e[0] for e in spy.events]
    assert "dump" in order and "apply" in order
    assert order.index("dump") < order.index("apply"), spy.events


def test_run_reports_failure_when_rows_remain_after_apply(monkeypatch):
    class _Stuck(_RunSpy):
        async def plan(self, db, tables):
            out = await super().plan(db, tables)
            for t in out["tables"]:
                out["tables"][t]["rows"] = 3       # never drains
            return out

    assert _run_with(monkeypatch, _Stuck(), do_apply=True) == 1


def test_the_log_copy_redacts_review_text_but_the_dump_file_does_not():
    """The dump file is the reversal record and must stay complete; the CI log
    is readable by everyone with repo access, so the printed copy is redacted."""
    full = {"tables": {"product_reviews": {
        "dump": [{"id": 1, "body": "private text", "author_user_id": 7, "status": "removed"}],
        "cascaded_children": {"media_assets": {"fk_column": "review_id",
                                               "on_delete": "cascade",
                                               "rows": [{"id": 5}]}}}}}
    red = d._redacted_for_log(full)
    assert red["tables"]["product_reviews"]["dump"][0]["body"].startswith("<redacted")
    assert red["tables"]["product_reviews"]["dump"][0]["author_user_id"].startswith("<redacted")
    assert red["tables"]["product_reviews"]["dump"][0]["status"] == "removed"   # not secret
    # the original is untouched — the file still gets everything
    assert full["tables"]["product_reviews"]["dump"][0]["body"] == "private text"


def test_apply_refuses_a_plan_whose_doors_failed():
    """apply() is not reachable past _run's check today, but it is a public
    entry point; a second caller must not be able to skip the doors."""
    import asyncio

    p = {"doors_failed": ["D6 something"], "tables": {}}
    with pytest.raises(RuntimeError, match="doors failed"):
        asyncio.run(d.apply(ReadOnlyFake(), ["catalog_offers"], "rid", p))


def test_apply_only_touches_the_tables_it_was_given():
    """A mutant that ignored the table list deleted reviews when only
    catalog_offers was selected."""
    import asyncio

    class _T:
        def __init__(self): self.seen = []
        async def execute(self, sql, params=None): self.seen.append(sql)
        async def fetch_all(self, sql, params=None):
            flat = " ".join(sql.split())
            if "FROM catalog_offers o" in flat:
                return [_offer("o1")]
            return []
        async def fetch_one(self, sql, params=None): return {"c": 0}
        def transaction(self):
            class _Tx:
                async def __aenter__(s): return s
                async def __aexit__(s, *a): return False
            return _Tx()

    t = _T()
    # The plan describes BOTH tables; apply is told to do only one. A mutant
    # that keys off the plan instead of the argument passes a plan-with-one-table
    # test, so the plan here must contain the table that must NOT be touched.
    p = {"doors_failed": [],
         "tables": {"catalog_offers": {"plan": [{"offer_id": "o1"}]},
                    "product_reviews": {"dump": [{"id": 1}]}}}
    asyncio.run(d.apply(t, ["catalog_offers"], "rid", p))
    assert t.seen and all("product_reviews" not in s for s in t.seen)
