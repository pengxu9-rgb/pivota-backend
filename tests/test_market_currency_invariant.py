"""ADR-024 Phase 0 item 2 — the market/currency disagreement invariant.

Three layers, because the check has three independently-breakable halves and a
bug in any one of them reads as the same reassuring "0":

  1. SCOPE (executed SQL, real dialect) — which offers are served supply, and
     how a currency normalises. Run against the suite's real database with the
     REAL `db.catalog` table definitions; no hand-rolled DDL.
  2. POLICY (pure) — what a (market, currency) pair means: agree, disagree,
     unmapped, or none of this check's business.
  3. WIRING (fake db, full runner) — that the check is REPORT-ONLY and yet its
     number and samples are still published. Both directions are pinned: a
     mutant promoting it to enforcing dies, and a mutant zeroing a real
     violation dies.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.catalog import catalog_offers, catalog_products  # noqa: E402
from db.database import database, engine, metadata  # noqa: E402
from services.catalog_invariant_checks import (  # noqa: E402
    _CHECKS,
    _MARKET_CURRENCY_PAIRS_SQL,
    _market_currency_rows_sql,
    _run_market_currency_disagreement,
    classify_market_currency_pairs,
    run_catalog_invariant_checks,
)
from services.priced_offer_sql import priced_offer_row_conjuncts  # noqa: E402
from services.source_quarantine import QUARANTINE_COLUMNS  # noqa: E402

CHECK_NAME = "market_currency_disagreement"


def _check() -> Dict[str, Any]:
    return next(c for c in _CHECKS if c["name"] == CHECK_NAME)


def _pair(market: str, currency: str, n: int) -> Dict[str, Any]:
    return {"market_norm": market, "currency_norm": currency, "n": n}


def _quarantine_row(match_type: str, match_value: str, **overrides) -> Dict[str, Any]:
    row = {
        "quarantine_id": 1,
        "match_type": match_type,
        "match_value": match_value,
        "state": "active",
        "reason": "currency defect",
        "expires_at": None,
        "created_by": "audit_offer_currency",
        "created_at": datetime(2026, 8, 21, tzinfo=timezone.utc),
        "revoked_at": None,
        "revoked_by": None,
        "metadata": None,
    }
    row.update(overrides)
    return row


class FakeDb:
    """Answers the three queries the check issues, and records what it was asked.

    Deliberately dispatches on a FRAGMENT of each statement rather than on
    equality: the row query is built per call (one bound pair per disagreeing
    group), so an equality fake would silently stop matching the moment the
    pair count changed and every row would read as "no rows" — i.e. clean.
    """

    def __init__(
        self,
        pairs: Optional[List[Dict[str, Any]]] = None,
        rows: Optional[List[Dict[str, Any]]] = None,
        quarantines: Optional[List[Dict[str, Any]]] = None,
        quarantine_error: Optional[Exception] = None,
    ):
        self._pairs = pairs or []
        self._rows = rows or []
        self._quarantines = quarantines or []
        self._quarantine_error = quarantine_error
        self.row_queries: List[Dict[str, Any]] = []
        self.quarantine_queries = 0

    async def fetch_all(self, sql: str, values=None):
        if "GROUP BY market_norm" in sql:
            return list(self._pairs)
        if "FROM served_offers" in sql:
            self.row_queries.append(dict(values or {}))
            return list(self._rows)
        if "catalog_source_quarantine" in sql:
            self.quarantine_queries += 1
            if self._quarantine_error is not None:
                raise self._quarantine_error
            return list(self._quarantines)
        raise AssertionError(f"unexpected sql: {sql}")

    async def fetch_one(self, sql: str, values=None):  # pragma: no cover - unused
        raise AssertionError("the market/currency check issues no fetch_one")


def _offer_row(offer_id: str, market: str, currency: str, **overrides) -> Dict[str, Any]:
    row = {
        "offer_id": offer_id,
        "merchant_id": "merch_e68c20b0189746d0",
        "source_system": "universal_product_sync",
        "source_ref": "ref-1",
        "source_domain": None,
        "domain": None,
        "platform": "shopify",
        "market_norm": market,
        "currency_norm": currency,
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# 2. POLICY — pure classification of (market, currency) groups
# ---------------------------------------------------------------------------


class TestPairClassification:
    def test_agreeing_offers_are_clean(self):
        buckets = classify_market_currency_pairs(
            [_pair("US", "USD", 10), _pair("GB", "GBP", 4), _pair("FR", "EUR", 2)]
        )
        assert buckets["agreeing"] == 16
        assert buckets["disagreeing"] == 0
        assert buckets["disagreeing_pairs"] == []
        assert buckets["served_offers"] == 16

    def test_disagreeing_offers_are_counted_and_carried_forward(self):
        # The live cohort's exact shape: EUR priced, stamped market US.
        buckets = classify_market_currency_pairs(
            [_pair("US", "USD", 100), _pair("US", "EUR", 433)]
        )
        assert buckets["disagreeing"] == 433
        assert buckets["agreeing"] == 100
        assert buckets["disagreeing_pairs"] == [("US", "EUR")]

    def test_unmapped_market_is_skipped_and_counted_never_a_violation(self):
        # 'DE' is a real ISO code region_pricing has not measured supply for.
        # It must not become "disagrees with USD" — that is the assume-USD
        # reflex ADR-024 names as the root of all four currency defects.
        buckets = classify_market_currency_pairs(
            [_pair("DE", "EUR", 5), _pair("XX", "GBP", 2)]
        )
        assert buckets["unmapped_market"] == 7
        assert buckets["disagreeing"] == 0
        assert buckets["disagreeing_pairs"] == []
        assert buckets["unmapped_market_codes"] == ["DE", "XX"]

    def test_the_reported_unmapped_code_is_normalized(self):
        # The bucket LABEL is what an operator reads to decide whether a market
        # deserves a map entry. Un-normalized, ' de ' and 'DE' report as two
        # different unknown markets and the same gap looks like two.
        buckets = classify_market_currency_pairs(
            [_pair(" de ", "EUR", 1), _pair("DE", "EUR", 1)]
        )
        assert buckets["unmapped_market_codes"] == ["DE"]
        assert buckets["unmapped_market"] == 2

    def test_a_blank_market_is_unmapped_not_us(self):
        buckets = classify_market_currency_pairs([_pair("", "EUR", 3)])
        assert buckets["unmapped_market"] == 3
        assert buckets["unmapped_market_codes"] == ["(blank)"]
        assert buckets["disagreeing"] == 0

    def test_blank_currency_is_out_of_scope_and_counted_separately(self):
        # The no_price / currency gates own a missing currency. Reading a blank
        # as "disagrees with USD" would double-count their defect as ours.
        buckets = classify_market_currency_pairs(
            [_pair("US", "", 7), _pair("US", "USD", 1)]
        )
        assert buckets["blank_currency"] == 7
        assert buckets["disagreeing"] == 0
        assert buckets["agreeing"] == 1
        assert buckets["served_offers"] == 8

    def test_currency_case_and_padding_are_normalized_not_disagreements(self):
        buckets = classify_market_currency_pairs(
            [_pair("US", " usd ", 2), _pair(" gb ", "gbp", 3)]
        )
        assert buckets["agreeing"] == 5
        assert buckets["disagreeing"] == 0

    def test_the_eurozone_map_is_not_inverted(self):
        # FR expects EUR; a USD offer stamped FR disagrees. The map is
        # region-keyed and must not be read backwards ("EUR is a euro country
        # currency, close enough").
        buckets = classify_market_currency_pairs([_pair("FR", "USD", 6)])
        assert buckets["disagreeing"] == 6


# ---------------------------------------------------------------------------
# 3. QUARANTINE — the recorded reason, and what happens when it cannot be read
# ---------------------------------------------------------------------------


class TestQuarantineExemption:
    @pytest.mark.asyncio
    async def test_disagreeing_offer_is_a_violation_with_sample_detail(self):
        db = FakeDb(
            pairs=[_pair("US", "USD", 100), _pair("US", "EUR", 2)],
            rows=[
                _offer_row("off_1", "US", "EUR"),
                _offer_row("off_2", "US", "EUR"),
            ],
        )
        result = await _run_market_currency_disagreement(db)
        assert result["count"] == 2
        assert result["sample_keys"] == ["off_1", "off_2"]
        detail = result["detail"]
        assert detail["violations"] == 2
        assert detail["quarantined"] == 0
        assert detail["disagreeing"] == 2
        assert detail["quarantine_lookup"] == "ok"
        assert detail["sample"][0] == {
            "offer_id": "off_1",
            "merchant_id": "merch_e68c20b0189746d0",
            "market": "US",
            "currency": "EUR",
            "source_system": "universal_product_sync",
        }
        # Only the disagreeing pair is bound into the row query.
        assert db.row_queries == [{"m0": "US", "c0": "EUR"}]

    @pytest.mark.asyncio
    async def test_quarantined_domain_is_the_recorded_reason(self):
        db = FakeDb(
            pairs=[_pair("US", "EUR", 2)],
            rows=[
                _offer_row("off_q", "US", "EUR", source_domain="https://www.mintree.us/p/1"),
                _offer_row("off_live", "US", "EUR", source_domain="other.example"),
            ],
            quarantines=[_quarantine_row("domain", "mintree.us")],
        )
        result = await _run_market_currency_disagreement(db)
        assert result["count"] == 1
        assert result["sample_keys"] == ["off_live"]
        assert result["detail"]["quarantined"] == 1
        assert result["detail"]["violations"] == 1

    @pytest.mark.asyncio
    async def test_quarantined_merchant_platform_is_also_a_recorded_reason(self):
        db = FakeDb(
            pairs=[_pair("US", "EUR", 1)],
            rows=[_offer_row("off_mp", "US", "EUR")],
            quarantines=[
                _quarantine_row(
                    "merchant_platform", "merch_e68c20b0189746d0:shopify"
                )
            ],
        )
        result = await _run_market_currency_disagreement(db)
        assert result["count"] == 0
        assert result["detail"]["quarantined"] == 1

    @pytest.mark.asyncio
    async def test_a_revoked_quarantine_does_not_exempt(self):
        # Delegated straight to quarantine_matches_source's state handling —
        # this asserts the delegation is real, not that we re-implemented it.
        db = FakeDb(
            pairs=[_pair("US", "EUR", 1)],
            rows=[_offer_row("off_r", "US", "EUR", source_domain="mintree.us")],
            quarantines=[
                _quarantine_row("domain", "mintree.us", state="revoked"),
            ],
        )
        result = await _run_market_currency_disagreement(db)
        assert result["count"] == 1
        assert result["detail"]["quarantined"] == 0

    @pytest.mark.asyncio
    async def test_an_expired_quarantine_does_not_exempt(self):
        db = FakeDb(
            pairs=[_pair("US", "EUR", 1)],
            rows=[_offer_row("off_e", "US", "EUR", source_domain="mintree.us")],
            quarantines=[
                _quarantine_row(
                    "domain",
                    "mintree.us",
                    expires_at=datetime.now(timezone.utc) - timedelta(days=1),
                ),
            ],
        )
        result = await _run_market_currency_disagreement(db)
        assert result["count"] == 1

    @pytest.mark.asyncio
    async def test_a_blank_quarantine_value_exempts_nothing(self):
        # `match_value` is TEXT NOT NULL with no non-empty CHECK and these rows
        # come from direct SQL ops, so a blank is reachable — and a blank that
        # matched every domain-less row would silently empty this check.
        db = FakeDb(
            pairs=[_pair("US", "EUR", 1)],
            rows=[_offer_row("off_b", "US", "EUR")],
            quarantines=[_quarantine_row("domain", "   ")],
        )
        result = await _run_market_currency_disagreement(db)
        assert result["count"] == 1
        assert result["detail"]["quarantined"] == 0

    @pytest.mark.asyncio
    async def test_quarantine_resolve_failure_is_not_emptiness(self):
        # A failed resolve must not be read as "nothing is quarantined" (which
        # would publish 2 violations we cannot substantiate) NOR as "everything
        # is" (which would hide 2 real ones). Neither number is supported by
        # the data, so the rows are reported UNCLASSIFIED.
        db = FakeDb(
            pairs=[_pair("US", "EUR", 2)],
            rows=[_offer_row("off_1", "US", "EUR"), _offer_row("off_2", "US", "EUR")],
            quarantine_error=RuntimeError("pool exhausted"),
        )
        result = await _run_market_currency_disagreement(db)
        detail = result["detail"]
        assert detail["quarantine_lookup"] == "failed"
        assert detail["unclassified_quarantine_unknown"] == 2
        assert detail["violations"] == 0
        assert detail["quarantined"] == 0
        assert detail["disagreeing"] == 2, (
            "the disagreement itself is still measured — only the exemption "
            "verdict is unknown"
        )
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_clean_corpus_never_touches_the_row_or_quarantine_queries(self):
        db = FakeDb(pairs=[_pair("US", "USD", 12), _pair("DE", "EUR", 3)])
        result = await _run_market_currency_disagreement(db)
        assert result["count"] == 0
        assert result["sample_keys"] == []
        assert db.row_queries == []
        assert db.quarantine_queries == 0
        assert result["detail"]["quarantine_lookup"] == "not_needed"
        assert result["detail"]["unmapped_market"] == 3


# ---------------------------------------------------------------------------
# 4. WIRING — report-only tier, through the real runner
# ---------------------------------------------------------------------------


class _RunnerDb:
    """The whole `_CHECKS` list against one fake: every SQL-driven check reads
    0, and the market/currency runner reads whatever `market` says."""

    def __init__(self, market: FakeDb, sql_counts: Optional[Dict[str, int]] = None):
        self._market = market
        self._sql_counts = sql_counts or {}

    def _name_for(self, sql):
        for check in _CHECKS:
            if check.get("count_sql") == sql or check.get("sample_sql") == sql:
                return check["name"]
        return None

    async def fetch_one(self, sql, values=None):
        return {"c": self._sql_counts.get(self._name_for(sql), 0)}

    async def fetch_all(self, sql, values=None):
        if (
            "GROUP BY market_norm" in sql
            or "FROM served_offers" in sql
            or "catalog_source_quarantine" in sql
        ):
            return await self._market.fetch_all(sql, values)
        name = self._name_for(sql)
        n = min(self._sql_counts.get(name, 0), 5)
        return [{"subject_key": f"pk_{name}_{i}"} for i in range(n)]


def _entry(report, name=CHECK_NAME):
    return next(c for c in report["checks"] if c["name"] == name)


class TestReportOnlyTier:
    def test_the_check_is_registered_warn_only_at_threshold_zero(self):
        check = _check()
        assert check["warn_only"] is True, (
            "promoting this to enforcing is a one-key change that must happen "
            "in the SAME change that disposes of the 433-EUR-as-US cohort"
        )
        assert check["default_threshold"] == 0, (
            "the threshold is already where an enforcing check needs it; the "
            "report-only tier — not a raised threshold — is what keeps a known "
            "live cohort from failing the build"
        )

    def test_it_is_the_only_warn_only_check(self):
        # If `warn_only` ever leaks onto another check, the module quietly
        # stops enforcing something it used to enforce.
        assert [c["name"] for c in _CHECKS if c.get("warn_only")] == [CHECK_NAME]

    @pytest.mark.asyncio
    async def test_violations_are_reported_but_do_not_fail_the_build(self):
        market = FakeDb(
            pairs=[_pair("US", "USD", 100), _pair("US", "EUR", 433)],
            rows=[_offer_row(f"off_{i}", "US", "EUR") for i in range(433)],
        )
        report = await run_catalog_invariant_checks(_RunnerDb(market))
        entry = _entry(report)

        # The verdict is withheld ...
        assert entry["violated"] is False
        assert report["violated_count"] == 0
        # ... and NOTHING else is. The value must stay visible or this is just
        # an always-passing check wearing an invariant's clothes.
        assert entry["warn_only"] is True
        assert entry["over_threshold"] is True
        assert entry["count"] == 433
        assert entry["threshold"] == 0
        assert len(entry["sample_keys"]) == 5
        assert entry["detail"]["disagreeing"] == 433
        assert entry["detail"]["agreeing"] == 100
        assert report["warned_count"] == 1

    @pytest.mark.asyncio
    async def test_a_clean_corpus_is_neither_violated_nor_warned(self):
        market = FakeDb(pairs=[_pair("US", "USD", 100)])
        report = await run_catalog_invariant_checks(_RunnerDb(market))
        entry = _entry(report)
        assert entry["count"] == 0
        assert entry["over_threshold"] is False
        assert entry["violated"] is False
        assert "sample_keys" not in entry
        assert report["warned_count"] == 0
        assert report["violated_count"] == 0

    @pytest.mark.asyncio
    async def test_an_enforcing_check_still_fails_the_build(self):
        # The other direction of the same wiring: `warn_only` must suppress the
        # verdict for ITS check only. A mutant applying it to every check — or
        # one that stops computing `violated` at all — dies here.
        market = FakeDb(pairs=[_pair("US", "USD", 5)])
        report = await run_catalog_invariant_checks(
            _RunnerDb(market, sql_counts={"public_but_suppressed": 3})
        )
        entry = _entry(report, "public_but_suppressed")
        assert entry["violated"] is True
        assert entry["warn_only"] is False
        assert entry["over_threshold"] is True
        assert len(entry["sample_keys"]) == 3
        assert report["violated_count"] == 1

    @pytest.mark.asyncio
    async def test_threshold_env_still_applies_to_the_reporting_check(
        self, monkeypatch
    ):
        monkeypatch.setenv("CATALOG_INVARIANT_MARKET_CURRENCY_THRESHOLD", "5")
        market = FakeDb(
            pairs=[_pair("US", "EUR", 3)],
            rows=[_offer_row(f"off_{i}", "US", "EUR") for i in range(3)],
        )
        report = await run_catalog_invariant_checks(_RunnerDb(market))
        entry = _entry(report)
        assert entry["count"] == 3
        assert entry["threshold"] == 5
        assert entry["over_threshold"] is False
        assert report["warned_count"] == 0

    @pytest.mark.asyncio
    async def test_a_failing_market_check_does_not_sink_the_sweep(self):
        class Boom(FakeDb):
            async def fetch_all(self, sql, values=None):
                if "GROUP BY market_norm" in sql:
                    raise RuntimeError("boom")
                return await super().fetch_all(sql, values)

        report = await run_catalog_invariant_checks(
            _RunnerDb(Boom(), sql_counts={"missing_trust_rows": 999})
        )
        assert "error" in _entry(report)
        assert _entry(report, "missing_trust_rows")["violated"] is True


class TestTheSweepPublishesTheReportingTier:
    """The daily sweep is the only place this check is ever READ in prod.

    A report-only check whose output the sweep drops is worth exactly nothing,
    so the log line is part of the feature, not decoration. (This is also the
    first test this job has ever had.)
    """

    async def _tick(self, monkeypatch, caplog, report):
        import logging

        import jobs.catalog_invariant_sweep_job as job
        import services.catalog_invariant_checks as checks

        async def _fake(_db):
            return report

        monkeypatch.setattr(checks, "run_catalog_invariant_checks", _fake)
        monkeypatch.delenv("CATALOG_INVARIANT_SWEEP_ENABLED", raising=False)
        with caplog.at_level(logging.DEBUG, logger=job.logger.name):
            await job.run_catalog_invariant_sweep_tick()
        return caplog.records

    @pytest.mark.asyncio
    async def test_a_reporting_check_is_logged_with_its_detail(
        self, monkeypatch, caplog
    ):
        records = await self._tick(
            monkeypatch,
            caplog,
            {
                "violated_count": 0,
                "warned_count": 1,
                "checks": [
                    {
                        "name": CHECK_NAME,
                        "description": "d",
                        "count": 433,
                        "threshold": 0,
                        "warn_only": True,
                        "over_threshold": True,
                        "violated": False,
                        "sample_keys": ["off_1"],
                        "detail": {"violations": 433, "quarantined": 0},
                    }
                ],
            },
        )
        warnings = [r for r in records if r.levelname == "WARNING"]
        assert len(warnings) == 1, "the reporting tier vanished from the sweep"
        message = warnings[0].getMessage()
        assert "REPORTING (warn_only)" in message
        assert "433" in message
        assert "quarantined" in message, (
            "a bare count cannot distinguish 433 unexplained from 433 exempt"
        )
        assert not [r for r in records if r.levelname == "ERROR"]

    @pytest.mark.asyncio
    async def test_a_real_violation_is_still_an_error(self, monkeypatch, caplog):
        records = await self._tick(
            monkeypatch,
            caplog,
            {
                "violated_count": 1,
                "warned_count": 0,
                "checks": [
                    {
                        "name": "public_but_suppressed",
                        "description": "d",
                        "count": 3,
                        "threshold": 0,
                        "warn_only": False,
                        "over_threshold": True,
                        "violated": True,
                        "sample_keys": ["pk_1"],
                    }
                ],
            },
        )
        errors = [r for r in records if r.levelname == "ERROR"]
        assert len(errors) == 1
        assert "VIOLATED" in errors[0].getMessage()


# ---------------------------------------------------------------------------
# 1. SCOPE — executed against the real database with the real table definitions
# ---------------------------------------------------------------------------


class TestServedScopeSql:
    """`suppressed_at IS NULL AND coalesce(effective, list) > 0`, executed.

    A string pin alone would not catch a CTE that selects the predicate but
    never applies it, so these rows are really inserted and really queried.
    """

    @pytest.fixture(autouse=True)
    async def _db(self):
        metadata.create_all(
            engine, tables=[catalog_products, catalog_offers], checkfirst=True
        )
        if not database.is_connected:
            await database.connect()
        await database.execute(
            catalog_offers.delete().where(
                catalog_offers.c.merchant_id == "merch_mkt_cur"
            )
        )
        await database.execute(
            catalog_products.delete().where(
                catalog_products.c.merchant_id == "merch_mkt_cur"
            )
        )
        yield
        await database.execute(
            catalog_offers.delete().where(
                catalog_offers.c.merchant_id == "merch_mkt_cur"
            )
        )
        await database.execute(
            catalog_products.delete().where(
                catalog_products.c.merchant_id == "merch_mkt_cur"
            )
        )

    async def _insert(self, offer_id, market, currency, **cols):
        values = {
            "offer_id": offer_id,
            "sku_key": f"sku_{offer_id}",
            "product_key": "pk_mkt_cur",
            "merchant_id": "merch_mkt_cur",
            "market": market,
            "currency": currency,
            "list_price": 10,
            "merchant_effective_price": None,
            "suppressed_at": None,
        }
        values.update(cols)
        await database.execute(catalog_offers.insert().values(**values))

    async def _pairs(self):
        rows = await database.fetch_all(_MARKET_CURRENCY_PAIRS_SQL)
        return {(r["market_norm"], r["currency_norm"]): int(r["n"]) for r in rows}

    async def _delta(self, before):
        """Groups this test ADDED. The suite shares one sqlite file and other
        modules leave catalog_offers rows behind, so an absolute count here
        would be an order-dependent test — green alone, red in the sweep."""
        after = await self._pairs()
        return {
            key: after[key] - before.get(key, 0)
            for key in after
            if after[key] - before.get(key, 0)
        }

    @pytest.mark.asyncio
    async def test_suppressed_and_unpriced_offers_are_out_of_scope(self):
        before = await self._pairs()
        await self._insert("o_ok", "US", "EUR")
        await self._insert(
            "o_suppressed", "US", "EUR", suppressed_at=datetime(2026, 8, 1)
        )
        await self._insert("o_no_price", "US", "EUR", list_price=None)
        await self._insert("o_zero_price", "US", "EUR", list_price=0)

        assert await self._delta(before) == {("US", "EUR"): 1}, (
            "exactly one served offer — the suppressed, the price-less and the "
            "zero-priced rows are withdrawn or unknown supply, not disagreements"
        )

    @pytest.mark.asyncio
    async def test_effective_price_alone_is_served_supply(self):
        # The coalesce order is the one the served surface prints; an offer
        # priced ONLY by merchant_effective_price is real supply and must not
        # fall out of the denominator.
        before = await self._pairs()
        await self._insert(
            "o_eff", "US", "EUR", list_price=None, merchant_effective_price=25
        )
        assert await self._delta(before) == {("US", "EUR"): 1}

    @pytest.mark.asyncio
    async def test_currency_and_market_are_normalized_by_the_dialect(self):
        before = await self._pairs()
        await self._insert("o_lower", " us ", " eur ")
        assert await self._delta(before) == {("US", "EUR"): 1}, (
            "upper(trim(coalesce(...))) is the SERVING predicate's own "
            "normalisation; a padded lower-case row is the same row"
        )

    @pytest.mark.asyncio
    async def test_a_null_currency_groups_as_blank_not_as_null(self):
        before = await self._pairs()
        await self._insert("o_null", "US", None)
        assert await self._delta(before) == {("US", ""): 1}
        assert classify_market_currency_pairs(
            [{"market_norm": "US", "currency_norm": "", "n": 1}]
        )["blank_currency"] == 1

    @pytest.mark.asyncio
    async def test_the_row_query_binds_its_pairs_and_returns_the_right_offers(self):
        await self._insert("o_usd", "US", "USD")
        await self._insert("o_eur", "US", "EUR")
        rows = await database.fetch_all(
            _market_currency_rows_sql(1), {"m0": "US", "c0": "EUR"}
        )
        ids = [r["offer_id"] for r in rows]
        assert "o_eur" in ids
        assert "o_usd" not in ids

    @pytest.mark.asyncio
    async def test_the_executed_scope_feeds_the_real_classifier(self):
        # SQL half and policy half, joined on real rows: a disagreement that
        # survives the served predicate must arrive in `disagreeing`.
        before = await self._pairs()
        await self._insert("o_agree", "US", "USD")
        await self._insert("o_disagree", "US", "EUR")
        await self._insert("o_unmapped", "DE", "EUR")
        await self._insert("o_blank", "US", "")
        delta = await self._delta(before)
        buckets = classify_market_currency_pairs(
            [{"market_norm": m, "currency_norm": c, "n": n} for (m, c), n in delta.items()]
        )
        assert buckets["agreeing"] == 1
        assert buckets["disagreeing"] == 1
        assert buckets["unmapped_market"] == 1
        assert buckets["blank_currency"] == 1
        assert buckets["served_offers"] == 4


# ---------------------------------------------------------------------------
# Structural pins
# ---------------------------------------------------------------------------


def test_the_served_predicate_is_the_shared_one_not_a_re_spelling():
    # services/priced_offer_sql exists because this rule was spelled twice and
    # drifted. It must appear here by import, conjunct for conjunct.
    for conjunct in priced_offer_row_conjuncts(alias="co"):
        assert conjunct in _MARKET_CURRENCY_PAIRS_SQL
    assert "estimated_best_price" not in _MARKET_CURRENCY_PAIRS_SQL, (
        "estimated_best_price is a derived guess, deliberately excluded from "
        "the priced predicate"
    )


def test_the_currency_map_is_never_inlined_into_sql():
    # The region -> currency map lives in ONE module. A VALUES list here would
    # be the split-brain priced_offer_sql exists to prevent, on a second table.
    for code in ("USD", "GBP", "JPY", "EUR", "KRW"):
        assert code not in _MARKET_CURRENCY_PAIRS_SQL
        assert code not in _market_currency_rows_sql(1)


def test_pair_values_are_bound_never_interpolated():
    # currency_norm is writer-supplied data read out of catalog_offers.
    sql = _market_currency_rows_sql(3)
    for i in range(3):
        assert f":m{i}" in sql and f":c{i}" in sql


def test_the_row_query_refuses_to_build_with_no_pairs():
    with pytest.raises(ValueError):
        _market_currency_rows_sql(0)


def test_the_check_docstring_cites_the_adr_and_both_incidents():
    doc = _check()["runner"].__doc__ or ""
    assert "ADR-024" in doc and "Phase 0 item 2" in doc
    assert "Mintree" in doc and "2026-07-28" in doc
    assert "433" in doc


def test_quarantine_columns_are_the_shared_constant():
    # The check reads quarantines through load_active_quarantines, so it can
    # never see a different column set than every other consumer.
    assert "match_type" in QUARANTINE_COLUMNS and "expires_at" in QUARANTINE_COLUMNS
