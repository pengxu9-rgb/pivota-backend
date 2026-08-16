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

    def __init__(self, bucket=0, offers=None, reviews=None):
        self.bucket = bucket
        self.offers = offers if offers is not None else [_offer()]
        self.reviews = reviews if reviews is not None else [_review()]

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
        if "review_id = ANY(:ids)" in flat:
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


def test_an_unreadable_child_table_is_recorded_never_treated_as_empty():
    """A missing cascade child must not read as 'no children to lose'."""
    class NoChildren(ReadOnlyFake):
        async def fetch_all(self, sql, params=None):
            if "review_id = ANY(:ids)" in " ".join(sql.split()):
                raise RuntimeError("relation does not exist")
            return await super().fetch_all(sql, params)

    out = _plan(NoChildren(), tables=("product_reviews",))
    kids = out["tables"]["product_reviews"]["cascaded_children"]
    assert all(v and "__unreadable__" in v[0] for v in kids.values())


# -- argument surface -------------------------------------------------------

def test_unknown_table_is_refused_rather_than_silently_ignored():
    with pytest.raises(SystemExit):
        d._parse(["--tables", "catalog_products"])
    with pytest.raises(SystemExit):
        d._parse(["--tables", ""])
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
