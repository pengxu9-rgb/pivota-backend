"""The main audit's observe-only implausible-price list.

Why it exists: the stamped-vs-/meta.json currency check is structurally blind
to a Shopify-Markets store whose base currency IS 'USD' but whose crawled price
carries another currency's magnitude (Oiad: ₩400,000 as $400,000 — oiad.us's
base currency is genuinely USD, so currency_mismatch can never fire). Price
magnitude is the only remaining smoke for that class.

The list must be a pure detector: printed, never quarantined, never gating.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

mod = importlib.import_module("scripts.audit_offer_currency")


# ---- SQL guards ----------------------------------------------------------------


def test_alert_sql_is_sql_standard_trim_not_btrim():
    """btrim() is Postgres-ONLY and broke the SQLite suite once already (#1568's
    inverse trap). The alert query must run on both engines."""
    sql = mod._PRICE_ALERT_SQL.lower()
    assert "btrim" not in sql
    assert "upper(trim(coalesce(o.currency, ''))) = 'usd'" in " ".join(sql.split())


def test_alert_sql_includes_suppressed_rows_but_reports_the_split():
    """A row suppressed FOR a price defect must stay visible to price tooling
    (the 2026-07-27 backfill lesson): read suppressed_at, never filter on it."""
    sql = mod._PRICE_ALERT_SQL.lower()
    assert "suppressed_at is null" not in sql
    assert "is_suppressed" in sql


def test_alert_sql_scope_and_bounds():
    sql = " ".join(mod._PRICE_ALERT_SQL.split()).lower()
    # domain-keyed scope: domain-less rows belong to the sibling audit
    assert "coalesce(o.source_domain, '') <> ''" in sql
    # threshold is a bind parameter, deterministic order, bounded output
    assert ":price_alert" in sql
    assert "order by o.list_price desc, o.offer_id" in sql
    assert "limit 200" in sql


# ---- behaviour (fake DB) --------------------------------------------------------


class FakeDB:
    """Minimal database stand-in: routes each query to a canned result and
    records quarantine-side effects via the patched create_quarantine below."""

    is_connected = True

    def __init__(self, domains_rows=None, alert_rows=None, no_domain=0):
        self.domains_rows = domains_rows or []
        self.alert_rows = alert_rows or []
        self.no_domain = no_domain
        self.alert_queries = 0

    async def fetch_all(self, query, values=None):
        # route on the bind name, not a SQL substring — _DOMAINS_SQL also
        # mentions list_price, and a future edit to either query must not
        # silently swap the two result sets
        if values and "price_alert" in values:
            self.alert_queries += 1
            return list(self.alert_rows)
        return list(self.domains_rows)

    async def fetch_val(self, query, values=None):
        return self.no_domain


_OIAD_ALERT = {
    "offer_id": "offer:external_seed:94be62296ff56f60a707a7ae934f74e8",
    "domain": "oiad.us",
    "list_price": 400000.0,
    "is_suppressed": False,
}


# A genuine offender (mintree-class: stamped USD, storefront INR) so the
# --apply drives all the way THROUGH the quarantine loop. Without it the
# "never quarantined" assertion is vacuous — `if not offenders: return 0`
# short-circuits before the loop, as the review's uncaught mutation proved.
_MINTREE_DOMAIN = {"domain": "mintree.us", "currencies": ["USD"],
                   "offers": 10, "max_price": 1999.0}


@pytest.fixture
def rig(monkeypatch):
    quarantined = []

    async def _meta(domain, **kw):
        if "mintree" in domain:
            return {"currency": "INR", "country": "IN"}
        return None  # every other storefront unresolvable

    async def _create_quarantine(**kw):
        quarantined.append(kw)
        return type("Q", (), {"quarantine_id": len(quarantined)})()

    monkeypatch.setattr(mod, "fetch_storefront_meta", _meta)
    monkeypatch.setattr(mod, "create_quarantine", _create_quarantine)
    return quarantined


@pytest.mark.parametrize("apply_mode", [False, True])
def test_alert_rows_are_printed_but_never_quarantined(monkeypatch, capsys, rig, apply_mode):
    """The Oiad row appears on the list in BOTH dry-run and --apply modes, and
    --apply must not quarantine oiad.us off the back of a magnitude signal —
    proven NON-vacuously: a genuine offender rides along so the quarantine
    loop actually executes, and only the offender lands in it. The alert row
    also must not inflate the --max-quarantine offender count (here the cap
    is 1: one real offender passes it; alerts counting toward it would REFUSE)."""
    argv = ["--min-offers", "1"]
    if apply_mode:
        argv += ["--apply", "--confirm", mod.CONFIRM_TOKEN,
                 "--created-by", "test@x", "--max-quarantine", "1"]
    db = FakeDB(domains_rows=[_MINTREE_DOMAIN], alert_rows=[_OIAD_ALERT])
    monkeypatch.setattr(mod, "database", db)
    rc = mod.main(argv)
    out = capsys.readouterr().out
    assert rc == 0
    assert "IMPLAUSIBLE-PRICE REVIEW LIST: 1 USD-stamped" in out
    assert "oiad.us" in out and "400000.00 USD" in out and "live" in out
    if apply_mode:
        assert [q["match_value"] for q in rig] == ["mintree.us"], (
            "the genuine offender must be quarantined — and nothing else")
    else:
        assert rig == []


def test_suppressed_alert_rows_are_marked(monkeypatch, capsys, rig):
    db = FakeDB(alert_rows=[{**_OIAD_ALERT, "is_suppressed": True}])
    monkeypatch.setattr(mod, "database", db)
    assert mod.main([]) == 0
    assert "suppressed" in capsys.readouterr().out


def test_price_alert_zero_disables_the_list(monkeypatch, capsys, rig):
    db = FakeDB(alert_rows=[_OIAD_ALERT])
    monkeypatch.setattr(mod, "database", db)
    assert mod.main(["--price-alert", "0"]) == 0
    out = capsys.readouterr().out
    assert "IMPLAUSIBLE-PRICE" not in out
    assert db.alert_queries == 0, "disabled must mean no query, not an empty print"


def test_long_lists_are_truncated_with_a_count(monkeypatch, capsys, rig):
    rows = [{**_OIAD_ALERT, "offer_id": f"o{i}", "list_price": 1000.0 + i}
            for i in range(25)]
    db = FakeDB(alert_rows=rows)
    monkeypatch.setattr(mod, "database", db)
    assert mod.main([]) == 0
    out = capsys.readouterr().out
    assert "25 USD-stamped" in out
    assert "... and 5 more" in out


def test_unparseable_price_does_not_crash_the_report(monkeypatch, capsys, rig):
    db = FakeDB(alert_rows=[{**_OIAD_ALERT, "list_price": "not-a-number"}])
    monkeypatch.setattr(mod, "database", db)
    assert mod.main([]) == 0
    assert "IMPLAUSIBLE-PRICE REVIEW LIST: 1" in capsys.readouterr().out
